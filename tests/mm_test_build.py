"""M7 离线回归：仅用独立临时 Git 仓库，不 fetch/reset/commit/push 真实仓库。"""
import ast
import contextlib
import html
import copy
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_support as b
import page_assets as assets

ROOT = Path(__file__).resolve().parents[1]


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mm-build-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()
        # 禁掉 Git 全部网络协议及全局配置；所有对象和提交历史仅用本地 plumbing 生成。
        guard = patch.dict(os.environ, {
            "GIT_ALLOW_PROTOCOL": "", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "offline-test", "GIT_AUTHOR_EMAIL": "test@offline.invalid",
            "GIT_COMMITTER_NAME": "offline-test", "GIT_COMMITTER_EMAIL": "test@offline.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        })
        guard.start()
        self.addCleanup(guard.stop)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "core.autocrlf", "false")
        self.files = {name: "初始输入\n" for name in b.REQUIRED_INPUTS}
        self.files.update({"index.html": "旧页面", "status.json": "{}", "README.md": "说明"})
        for name, value in self.files.items():
            (self.root / name).write_text(value, encoding="utf-8")
        self.git("add", "--", ".")
        tree = self.git("write-tree").strip().decode()
        self.base = self.git("commit-tree", tree, input=b"fixture\n").strip().decode()
        self.git("update-ref", "refs/heads/main", self.base)
        self.frozen = b.snapshot(self.root, "123-1")
        self.bundle = Path(self.temp.name) / "bundle"

    def git(self, *args, input=None):
        result = subprocess.run(["git", "-C", str(self.root), *args], input=input,
                                capture_output=True, env=os.environ.copy(), check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def revision(self, changes, modes=None):
        # 独立 index，不更改 checkout 或其真实 index；便于模拟构建期间 main 前进。
        index = Path(self.temp.name) / "revision-index"
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(index)}):
            self.git("read-tree", self.base)
            for name, content in changes.items():
                if content is None:
                    self.git("update-index", "--force-remove", "--", name)
                else:
                    payload = content.encode("utf-8") if isinstance(content, str) else content
                    oid = self.git("hash-object", "-w", "--stdin", input=payload).strip().decode()
                    mode = (modes or {}).get(name, "100644")
                    self.git("update-index", "--add", "--cacheinfo", f"{mode},{oid},{name}")
            tree = self.git("write-tree").strip().decode()
            return self.git("commit-tree", tree, "-p", self.base, input=b"update\n").strip().decode()

    def generated(self):
        (self.root / "index.html").write_text("<html>本轮生成</html>", encoding="utf-8")
        status = {k: self.frozen[k] for k in ("source_sha", "input_hash", "build_id")}
        (self.root / "status.json").write_text(json.dumps(status), encoding="utf-8")
        return status

    def sealed(self):
        self.generated()
        b.seal_artifacts(self.root, self.frozen, self.bundle)

    def test_snapshot_is_stable_and_has_actual_source(self):
        other = b.snapshot(self.root, "123-2")
        self.assertEqual(self.frozen["input_hash"], other["input_hash"])
        self.assertEqual(self.frozen["source_sha"], self.base)
        self.assertEqual(list(self.frozen["files"]), sorted(self.frozen["files"]))
        self.assertNotEqual(self.frozen["build_id"], other["build_id"])
        self.assertEqual(b.verify_inputs(self.root, self.frozen, self.base), self.frozen["input_hash"])

    def test_all_production_input_changes_are_rejected(self):
        for name in ("monitor.py", "holdings.json", "settings.json", "cost_panel.css", "cost_panel.js",
                     "edit.html", "run-status.js", "list-import.js", "official-earnings.json",
                     "position-costs.seed.json", "requirements.txt", "build_support.py"):
            with self.subTest(path=name):
                sha = self.revision({name: "新输入"})
                with self.assertRaisesRegex(b.InputChanged, re.escape(name)):
                    b.verify_inputs(self.root, self.frozen, sha)

    def test_new_modules_and_resources_are_discovered(self):
        for name in ("quote_adapter.py", "rules_config.py", "page_assets.py", "future.js", "future.css",
                     "new-config.json", "new-editor.html", "templates/cards/item.html",
                     "assets/cards/theme.css", "static/tools.js", "icons/new.png", "config/new.json"):
            with self.subTest(path=name):
                sha = self.revision({name: "新模块或资源"})
                with self.assertRaises(b.InputChanged):
                    b.verify_inputs(self.root, self.frozen, sha)

    def test_missing_required_input_fails_closed(self):
        sha = self.revision({"holdings.json": None})
        with self.assertRaisesRegex(b.BuildError, "缺少必要"):
            b.verify_inputs(self.root, self.frozen, sha)

    def test_optional_input_removal_and_rename_are_detected(self):
        first = self.revision({"future.py": "value=1"})
        frozen = copy.deepcopy(self.frozen)
        frozen["source_sha"] = first
        frozen["files"] = b.input_manifest(self.root, first)
        frozen["input_hash"] = b._digest(frozen["files"])
        for target in (self.base, self.revision({"renamed.py": "value=1"})):
            with self.subTest(target=target), self.assertRaises(b.InputChanged):
                b.verify_inputs(self.root, frozen, target)

    def test_unrelated_changes_allow_reuse(self):
        sha = self.revision({"README.md": "新文档", "tests/test_new.py": "assert True",
                             "outputs/report.html": "报告", ".cache/market-monitor.json": "{}",
                             "run-receipt.json": "{}", "status.json": "{}", "index.html": "另一个产物",
                             "position-costs.json": "{}", "import-audit.json": "{}",
                             ".github/workflows/tests.yml": "独立测试变更"})
        self.assertEqual(b.verify_inputs(self.root, self.frozen, sha), self.frozen["input_hash"])
        self.assertEqual(self.frozen["source_sha"], self.base)

    def test_retry_rechecks_each_new_target(self):
        first = self.revision({"README.md": "无关更新"})
        b.verify_inputs(self.root, self.frozen, first)
        second = self.revision({"holdings.json": "下一次 main 已变"})
        with self.assertRaises(b.InputChanged):
            b.verify_inputs(self.root, self.frozen, second)

    def test_failed_git_lookup_is_not_no_change(self):
        with self.assertRaises(b.BuildError):
            b.verify_inputs(self.root, self.frozen, "missing-ref")
        self.sealed()
        with self.assertRaises(b.BuildError):
            b.artifacts_changed(self.root, self.frozen, self.bundle, "missing-ref")

    def test_symlink_input_is_rejected(self):
        sha = self.revision({"future.py": "monitor.py"}, {"future.py": "120000"})
        with self.assertRaisesRegex(b.BuildError, "普通文件"):
            b.verify_inputs(self.root, self.frozen, sha)

    def test_file_mode_change_is_input_change(self):
        sha = self.revision({"monitor.py": self.files["monitor.py"]}, {"monitor.py": "100755"})
        with self.assertRaises(b.InputChanged):
            b.verify_inputs(self.root, self.frozen, sha)

    def test_dirty_tracked_input_cannot_be_frozen(self):
        (self.root / "monitor.py").write_text("new", encoding="utf-8")
        with self.assertRaisesRegex(b.BuildError, "未提交"):
            b.snapshot(self.root, "123-1")

    def test_untracked_production_module_cannot_be_frozen(self):
        (self.root / "future.py").write_text("new", encoding="utf-8")
        with self.assertRaisesRegex(b.BuildError, "未提交"):
            b.snapshot(self.root, "123-1")

    def test_cache_and_test_edits_do_not_dirty_inputs(self):
        (self.root / ".cache").mkdir()
        (self.root / ".cache/market-monitor.json").write_text("{}", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests/new.py").write_text("assert True", encoding="utf-8")
        self.assertEqual(b.snapshot(self.root, "123-2")["input_hash"], self.frozen["input_hash"])

    def test_changed_inputs_during_generation_cannot_be_sealed(self):
        self.generated()
        (self.root / "settings.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(b.BuildError):
            b.seal_artifacts(self.root, self.frozen, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_old_or_mislabelled_status_cannot_be_sealed(self):
        for field in ("source_sha", "input_hash", "build_id"):
            with self.subTest(field=field):
                status = self.generated()
                status[field] = "old"
                (self.root / "status.json").write_text(json.dumps(status), encoding="utf-8")
                with self.assertRaisesRegex(b.BuildError, "禁止复用旧产物"):
                    b.seal_artifacts(self.root, self.frozen, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_legacy_status_without_identity_is_rejected(self):
        with self.assertRaises(b.BuildError):
            b.seal_artifacts(self.root, self.frozen, self.bundle)

    def test_no_change_skips_commit_even_when_main_sha_differs(self):
        self.sealed()
        changes = {name: (self.bundle / name).read_bytes() for name in b.ARTIFACTS}
        changes["README.md"] = "同时有无关变更"
        sha = self.revision(changes)
        b.verify_inputs(self.root, self.frozen, sha)
        self.assertFalse(b.artifacts_changed(self.root, self.frozen, self.bundle, sha))
        self.assertEqual(json.loads((self.bundle / "status.json").read_bytes())["source_sha"], self.base)

    def test_changed_artifacts_need_commit(self):
        self.sealed()
        self.assertTrue(b.artifacts_changed(self.root, self.frozen, self.bundle, self.base))

    def test_absent_target_artifacts_need_commit(self):
        self.sealed()
        sha = self.revision({"index.html": None, "status.json": None})
        self.assertTrue(b.artifacts_changed(self.root, self.frozen, self.bundle, sha))

    def test_tampered_bundle_fails_closed(self):
        self.sealed()
        (self.bundle / "index.html").write_text("被改过", encoding="utf-8")
        with self.assertRaisesRegex(b.BuildError, "封存产物摘要"):
            b.artifacts_changed(self.root, self.frozen, self.bundle, self.base)

    def test_missing_or_empty_artifact_fails_closed(self):
        self.generated()
        (self.root / "index.html").unlink()
        with self.assertRaises(b.BuildError):
            b.seal_artifacts(self.root, self.frozen, self.bundle)
        (self.root / "index.html").write_bytes(b"")
        with self.assertRaises(b.BuildError):
            b.seal_artifacts(self.root, self.frozen, self.bundle)

    def test_corrupt_snapshot_fails_closed(self):
        for changes in ({"files": {}}, {"input_hash": "bad"}, {"source_sha": ""},
                        {"schema_version": 99}, {"build_id": "123\nMM_INPUT_HASH=bad"}):
            with self.subTest(changes=changes), self.assertRaises(b.BuildError):
                b.verify_inputs(self.root, dict(self.frozen, **changes), self.base)

    def test_cli_exports_identity_and_does_not_overwrite_snapshot(self):
        path = str(Path(self.temp.name) / "build-inputs.json")
        args = ["snapshot", "--root", str(self.root), "--snapshot", path, "--build-id", "456-2"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(b.main(args), 0)
        env = dict(line.split("=", 1) for line in output.getvalue().splitlines())
        self.assertEqual(env, {"MM_SOURCE_SHA": self.base, "MM_INPUT_HASH": self.frozen["input_hash"],
                               "MM_BUILD_ID": "456-2"})
        before = Path(path).read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.main(args), 1)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_cli_failure_exit_codes(self):
        path = Path(self.temp.name) / "build-inputs.json"
        path.write_text(json.dumps(self.frozen), encoding="utf-8")
        args = ["verify", "--root", str(self.root), "--snapshot", str(path), "--ref"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.main(args + ["missing-ref"]), 1)
            sha = self.revision({"monitor.py": "changed"})
            self.assertEqual(b.main(args + [sha]), 3)
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(b.main(args + [self.base]), 1)


class WorkflowContractTests(unittest.TestCase):
    def test_path_policy_covers_production_but_not_tests_or_outputs(self):
        for path in b.REQUIRED_INPUTS | {"quote_adapter.py", "page_assets.py", "rules_config.py",
                                       "assets/deep/card.css", "manifest.webmanifest"}:
            with self.subTest(path=path):
                self.assertTrue(b.is_build_input(path))
        for path in b.EXCLUDED_INPUTS | {"tests/new.py", "tests/new.js", "outputs/report.html",
                                        "README.md", ".cache/market-monitor.json"}:
            with self.subTest(path=path):
                self.assertFalse(b.is_build_input(path))

    def test_daily_trigger_patterns_match_helper(self):
        daily = (ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8")
        paths = re.findall(r"^      - '([^']+)'$", daily, re.M)
        self.assertEqual([p for p in paths if not p.startswith("!")], list(b.BUILD_INPUT_PATTERNS))
        self.assertEqual({p[1:] for p in paths if p.startswith("!")}, b.EXCLUDED_INPUTS)

    def test_ci_cache_artifact_matrix_and_publish_order(self):
        daily = (ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8")
        tests = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        ensure = (ROOT / ".github/workflows/ensure-morning.yml").read_text(encoding="utf-8")
        self.assertIn("python-version: ['3.11', '3.13']", tests)
        for workflow in (daily, tests, ensure):
            self.assertIn("python -m pip install -r requirements.txt", workflow)
            self.assertNotRegex(workflow, r"(?m)^\s+schedule:")
        self.assertIn("python-version: '3.11'", daily)
        self.assertIn("python-version: '3.11'", ensure)
        self.assertIn("      - 'tests/**'", tests)
        self.assertNotIn("      - '*.py'", tests)
        self.assertIn("actions/cache/restore@v4", daily)
        save_block = daily[daily.index("      - name: 保存成功辅助缓存"):daily.index("      - name: 上传运行回执")]
        self.assertIn("actions/cache/save@v4", save_block)
        self.assertNotIn("publish", save_block)
        self.assertIn("always()", save_block)
        self.assertLess(daily.index("actions/cache/save@v4"), daily.index("git reset"))
        self.assertLess(daily.index("actions/upload-artifact@v4"), daily.index("git reset"))
        self.assertIn("run-receipt.json", daily)
        loop = daily[daily.index("for i in 1 2 3 4 5;"):]
        self.assertLess(loop.index('verify \\'), loop.index('git reset --hard "$target"'))
        self.assertLess(loop.index('artifact-state \\'), loop.index('git reset --hard "$target"'))
        self.assertLess(loop.index("no-change"), loop.index("git commit"))
        self.assertIn("git push origin HEAD:main", loop)
        self.assertNotIn("--force", loop)


class RulesTemplateTests(unittest.TestCase):
    """独立纯内存模板专项，不导入 monitor、不生成正式产物。"""

    @staticmethod
    def plain(markup):
        return html.unescape(re.sub(r'<[^>]+>', '', markup))

    def test_templates_render_without_files_network_or_monitor(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('禁止读临时素材')), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('禁止读文件')), \
                patch.object(socket, 'socket', side_effect=AssertionError('禁止联网')):
            markup = assets.rules_template({}, {})
        self.assertEqual(markup.count('<template id="rulesTpl">'), 1)
        self.assertEqual(markup.count('<template id="glossaryTpl">'), 1)
        self.assertNotIn('${', markup)
        self.assertNotIn('<details', markup)
        self.assertNotIn('<script', markup)
        for summary in ('预警规则', '名词解释和抓取'):
            self.assertIn('<summary>' + summary + '</summary>', markup)

    def test_rule_ids_remain_unique_without_internal_links_or_navigation(self):
        rules = assets.rules_body({}, {})
        glossary = assets.glossary_body({}, {})
        ids = re.findall(r'\bid="([^"]+)"', rules + glossary)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(hasattr(assets, 'RULE_CROSS_LINKS'))
        self.assertFalse(hasattr(assets, 'RULE_LINK_LABELS'))
        for markup in (rules, glossary, assets.rules_template({}, {})):
            self.assertNotRegex(markup, r'href\s*=\s*[\"\']\s*#')
            self.assertNotRegex(markup, r'<nav\b|rule-jump|rule-cross-link|rule-cross-nav')
            self.assertNotRegex(markup, r'\bon\w+\s*=|\btabindex\s*=')
        source_links = re.findall(r'<a\b[^>]+href="([^"]+)"[^>]*>.*?</a>', glossary, re.S)
        self.assertTrue(source_links, '保留外部来源链接，不能将所有链接一起删除')
        self.assertTrue(all(link.startswith('https://') for link in source_links))
        self.assertIn('History of Market', self.plain(glossary))
        for alias in ('alerts', 'display', 'quiet', 'prices', 'loss', 'space', 'reached',
                      'earnings', 'missing', 'terms', 'fetch', 'read-terms',
                      'change-terms', 'amplitude-terms', 'boll-terms', 'rsi-terms',
                      'volume-terms', 'ma-terms', 'target-terms', 'earnings-terms',
                      'fetch-consensus', 'fetch-after-earnings', 'fetch-earnings-cache',
                      'dataSourceRules'):
            self.assertIn(alias, ids)

    def test_latest_rules_and_sorting_keep_gray_and_group_conditions(self):
        rules = assets.rules_body({}, {})
        text = self.plain(rules)
        for term in ('RSI 3条 ↓超卖红灯', 'RSI 3条 ↑超买黄灯灰灯灰灯（折）',
                     '加仓价提醒标记已读 → 灰灯。',
                     '减仓价提醒标记已读 → 灰灯。', '浮亏提醒标记已读 → 灰灯。',
                     '浮亏10%／20%每只标的只计一次。',
                     '两者只保留在主表，不进入“无异动”，不计入统计面板',
                     '布林下轨▼ + RSI 0条灰灯灰灯（折）',
                     'RSI 2条 ↓超卖灰灯',
                     '布林上轨▲ + RSI 1条 ↑超买灰灯仅文字无提示',
                     '布林上轨▲ + RSI 1条 ↓超卖（反）灰灯仅文字无提示', '短均线', '长均线'):
            self.assertIn(term, text)
        for deleted in ('进入下轨报警区（逼近自适应）', '排除指数、期货', '行灯取最严重',
                        '单独RSI 3条向上超买 → 文字。', '基本面黄灯'):
            self.assertNotIn(deleted, text)
        second = rules.split('<section id="rule-text">')[1].split('</section>')[0]
        self.assertNotIn('id="nav-up-sell-other"', second)
        self.assertLess(second.index('id="rule-earnings"'), second.index('id="rule-quotes"'))
        third = rules.split('<section id="rule-folded">')[1]
        headings = re.findall(r'<h5 class="rule-layout-heading">(.*?)</h5>', third)
        self.assertEqual(headings, ['目标／共识价', '财报'])
        self.assertEqual(rules.count('class="nav-gray"'), 21)  # 重点上轨两条两方向新增技术灰灯。

    def test_threshold_tables_are_only_in_glossary_and_follow_settings_and_constants(self):
        settings = {'chg_yellow': 7, 'chg_red': 11, 'amp_yellow': 13, 'amp_red': 17,
                    'ma_short': 25, 'ma_long': 180, 'boll_n': 30, 'boll_k': 2.7,
                    'boll_near_band_pct': 10, 'boll_near_pct': 0.9,
                    'rsi_high': 82, 'rsi_low': 24, 'near_52w_low_pct': 2.2,
                    'trigger_gap_pct': 7, 'reduce_gap_pct': 9,
                    'vol_ratio': 2.2, 'vol_ratio_red': 4.4, 'vol_ratio_z': 2.1, 'vol_ratio_red_z': 4.2}
        constants = {'vol_mult': '×0.5 / ×1.1 / ×1.8', 'rsi_periods': '5／10／21',
                     'vol_low_sigma': 0.8, 'vol_high_sigma': 3.1, 'vol_min_returns': 33,
                     'ma_long_band_pct': 1.7, 'ma_confirm_lookback': 8,
                     'ma_confirm_min': 0.75, 'ma_confirm_max': 1.25, 'ma_confirm_default': 0.8,
                     'vol_ratio_base': 22, 'intraday_vol_quiet_min': 19,
                     'boll_sigma_factor': 1.5, 'target_space_pct': 40,
                     'target_high_space_pct': 90, 'earn_soon_days': 5, 'earn_list_days': 12}
        before = copy.deepcopy((settings, constants))
        rules = assets.rules_body(settings, constants)
        glossary = assets.glossary_body(settings, constants)
        self.assertEqual(re.findall(r'<table[^>]*id="([^"]+)"', rules), ['rules-lamp-table'])
        self.assertNotIn('glossary-chg-table', rules)
        self.assertNotIn('glossary-amp-table', rules)
        self.assertEqual(glossary.count('<table'), 10)
        for kind, expected in (('chg', ['≥3.5%', '≥5.5%', '≥7.7%', '≥12.1%', '≥12.6%', '≥19.8%']),
                               ('amp', ['≥6.5%', '≥8.5%', '≥14.3%', '≥18.7%', '≥23.4%', '≥30.6%'])):
            table = re.search(r'<table[^>]+id="glossary-' + kind + r'-table">.*?</table>', glossary, re.S)[0]
            for threshold in expected:
                self.assertIn(threshold, table)
            self.assertIn('中 ×1.1', table)
        combined = self.plain(rules + glossary)
        for dynamic in ('短均线（启用）', '长均线（启用）', '布林为30日、±2.7倍', '×0.6（当前10%设置）',
                        '≤约0.6%', '≤约1.2%', '≤约1.8%', '固定0.9%', '周期5／10／21日',
                        '≥82超买', '≤24超卖', '距低点≤2.2%', '设置价×1.07', '设置价×0.91',
                        '红k＝4.2', '黄k＝2.1', '红F＝4.4×', '黄F＝2.2×',
                        '红≥4.4×、黄≥2.2×', '新门槛＝√(T×F)', '固定±1.7%',
                        '限0.75%～1.25%', '检查近8日', '此前22日', '不足19分钟',
                        '有效空间≥40%', '≥90%', '未来6～12天', '&lt;0.8%'):
            self.assertIn(html.unescape(dynamic), combined)
        self.assertIn('有效空间≥40%', self.plain(rules))
        self.assertEqual((settings, constants), before)

    def test_focus_upper_single_rsi_table_cells_are_text_only_in_both_directions(self):
        for markup in (assets.LAMP_RULE_TABLE, assets.rules_body({}, {})):
            table = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', markup, re.S)[0]
            self.assertEqual([self.plain(cell) for cell in re.findall(r'<th[^>]*scope="col"[^>]*>(.*?)</th>', table)],
                             ['分类', '条件', '持仓分组', '重点', '其他分组'])
            for condition, position, css_class in (
                    ('布林上轨▲ + RSI 1条 ↑超买', '灰灯', 'cell-gray'),
                    ('布林上轨▲ + RSI 1条 ↓超卖（反）', '灰灯', 'cell-gray')):
                with self.subTest(condition=condition, rendered=markup != assets.LAMP_RULE_TABLE):
                    rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', table, re.S)
                            if '<td' in row and self.plain(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
                    self.assertEqual(len(rows), 1, condition + '规则行必须唯一')
                    cells = [(attrs, self.plain(body)) for attrs, body in
                             re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)]
                    self.assertEqual(cells, [('', condition), (' class="' + css_class + '"', position),
                                             ('', '仅文字'), ('', '无提示')])
                    self.assertNotRegex(cells[2][0], r'cell-gray|nav-gray|colspan')
                    self.assertIn('<td>仅文字</td>', rows[0])

    def test_current_three_rule_rows_sync_only_authorized_cells(self):
        cases = (
            ('布林上轨▲ + RSI 1条 ↓超卖（反）', [(' class="cell-gray"', '灰灯'), ('', '仅文字'), ('', '无提示')]),
            ('RSI 2条 ↑超买', [(' class="cell-gray"', '灰灯'), ('', '仅文字'), ('', '无提示')]),
            ('RSI 1条 ↓超卖', [(' colspan="3"', '仅文字')]),
            ('RSI 1条 ↑超买', [(' colspan="3"', '无提示')]),
            ('RSI 2条 ↓超卖', [(' class="cell-gray" colspan="3"', '灰灯')]),
            ('布林下轨▼ + RSI 1条 ↓超卖', [(' class="cell-gray" colspan="3"', '灰灯')]),
        )
        for markup in (assets.LAMP_RULE_TABLE, assets.rules_body({}, {}), assets.rules_template({}, {})):
            table = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', markup, re.S)[0]
            for condition, expected in cases:
                with self.subTest(condition=condition, source=markup[:35]):
                    rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', table, re.S)
                            if '<td' in row and self.plain(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
                    self.assertEqual(len(rows), 1)
                    cells = [(attrs, self.plain(body)) for attrs, body in
                             re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)]
                    self.assertEqual(cells, [('', condition)] + expected)

    def test_reverse_condition_text_and_amplitude_gray_description_are_scoped(self):
        rules = assets.rules_body({}, {})
        reverse = re.findall(r'<span class="rule-reverse">(.*?)</span></td>', rules, re.S)
        self.assertEqual(len(reverse), 6)
        for text in reverse:
            self.assertIn('（反）', self.plain(text))
            self.assertEqual(len(re.findall(r'<span class="ar-(?:band|rsi)-(?:up|down)">', text)), 2)
        self.assertIn('.card .rules-body #rules-lamp-table .rule-reverse{color:var(--dim)}', assets.RULES_CSS)
        self.assertNotIn('rule-reverse', assets.glossary_body({}, {}))
        row = next(row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', rules, re.S)
                   if '振幅 · 自适应' in self.plain(row))
        cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', row, re.S)
        self.assertEqual([self.plain(text) for _, text in cells], ['振幅 · 自适应', '红灯／黄灯', '红灯／黄灯', '灰灯'])
        self.assertIn('class="cell-gray"', cells[-1][0])
        self.assertIn('振幅有效（含0%）就报灰灯', self.plain(assets.glossary_body({}, {})))
        self.assertIn('不设门槛、不升红黄', self.plain(assets.glossary_body({}, {})))

    def test_lamp_table_horizontal_center_is_scoped_and_keeps_other_layout(self):
        css = assets.RULES_CSS
        blocks = re.findall(r'([^{}]+)\{([^{}]*)\}', css)
        centered = set()
        for selectors, declarations in blocks:
            selector_list = [selector.strip() for selector in selectors.split(',')]
            targets = [selector for selector in selector_list
                       if re.search(r'#rules-lamp-table\s+(?:th|td)$', selector)]
            if targets:
                self.assertEqual(len(targets), len(selector_list), '水平居中不得覆盖其它表')
                self.assertEqual(declarations.strip().rstrip(';'), 'text-align:center', '只允许改水平对齐')
                centered.update(selector.rsplit(' ', 1)[1] for selector in targets)
        self.assertEqual(centered, {'th', 'td'}, '分类、表头、条件及全部灯级单元格均需居中')
        common = re.search(r'\.card \.rules-body \.rule-table th,\.card \.rules-body \.rule-table td\{([^}]*)\}', css)[1]
        for unchanged in ('text-align:left', 'vertical-align:top', 'padding:6px 7px',
                          'font-size:12px', 'line-height:1.65', 'border:1px solid #5a6478'):
            self.assertIn(unchanged, common)
        category = re.search(r'\.card \.rules-body \.rule-table th\.cat-col\{([^}]*)\}', css)[1]
        for unchanged in ('vertical-align:middle', 'writing-mode:vertical-rl', 'padding:6px 0', 'width:18px'):
            self.assertIn(unchanged, category)

    def test_four_authorized_rule_rows_preserve_adjacent_group_cells(self):
        cases = (
            ('布林下轨▼ + RSI 0条', [(' class="cell-gray"', '灰灯'),
                                     (' class="cell-gray-fold" colspan="2"', '灰灯（折）')]),
            ('RSI 3条 ↑超买', [(' class="cell-yellow"', '黄灯'), (' class="cell-gray"', '灰灯'),
                               (' class="cell-gray-fold"', '灰灯（折）')]),
            ('布林上轨▲ + RSI 2条 ↑超买', [(' class="cell-yellow"', '黄灯'),
                                        (' class="cell-gray"', '灰灯'), ('', '仅文字')]),
            ('布林上轨▲ + RSI 2条 ↓超卖（反）', [(' class="cell-yellow"', '黄灯'),
                                               (' class="cell-gray"', '灰灯'), ('', '仅文字')]),
            ('布林上轨▲ + RSI 3条 ↓超卖（反）', [(' class="cell-yellow"', '黄灯'),
                                               (' class="cell-yellow"', '黄灯'),
                                               (' class="cell-gray-fold"', '灰灯（折）')]),
        )
        for markup in (assets.LAMP_RULE_TABLE, assets.rules_body({}, {}), assets.rules_template({}, {})):
            table = re.search(r'<table[^>]*id="rules-lamp-table"[^>]*>.*?</table>', markup, re.S)[0]
            for condition, expected in cases:
                with self.subTest(condition=condition, source=markup[:35]):
                    rows = [row for row in re.findall(r'<tr\b[^>]*>.*?</tr>', table, re.S)
                            if '<td' in row and self.plain(re.search(r'<td\b[^>]*>(.*?)</td>', row, re.S)[1]) == condition]
                    self.assertEqual(len(rows), 1)
                    cells = [(attrs, self.plain(body)) for attrs, body in
                             re.findall(r'<td\b([^>]*)>(.*?)</td>', rows[0], re.S)]
                    self.assertEqual(cells, [('', condition)] + expected)
        # 上轨三反向超卖持仓/重点实际黄灯，与既有规则表一致；其他组仍灰灯可折叠。
        self.assertIn('<td class="cell-yellow">黄灯</td><td class="cell-yellow">黄灯</td>'
                      '<td class="cell-gray-fold">灰灯（折）</td>', assets.LAMP_RULE_TABLE)

    def test_volume_example_table_follows_all_four_settings_and_constant_overrides(self):
        defaults = dict(vol_ratio=1.5, vol_ratio_red=3, vol_ratio_z=2, vol_ratio_red_z=3.6)
        cases = [('默认', {}, {}, [('2.27', '1.42'), ('2.97', '1.65'), ('4.26', '2.02')])]
        for name, value in (('vol_ratio', 2.2), ('vol_ratio_red', 4.4),
                            ('vol_ratio_z', 2.7), ('vol_ratio_red_z', 4.2)):
            cases.append((name, {name: value}, {}, None))
        cases += [('全部设置', dict(vol_ratio=2.2, vol_ratio_red=4.4,
                                   vol_ratio_z=2.1, vol_ratio_red_z=4.2), {}, None),
                  ('常量覆盖', dict(vol_ratio=2.2, vol_ratio_red=4.4),
                   dict(vol_ratio=1.8, vol_ratio_red=3.5, vol_ratio_z=2.5, vol_ratio_red_z=4), None)]
        for label, settings, constants, expected in cases:
            with self.subTest(case=label):
                before = copy.deepcopy((settings, constants))
                values = dict(defaults, **settings)
                values.update(constants)
                glossary = assets.glossary_body(settings, constants)
                self.assertEqual(glossary.count('id="glossary-vol-table"'), 1)
                self.assertNotIn('id="glossary-vol-table"', assets.rules_body(settings, constants))
                table = re.search(r'<table[^>]*id="glossary-vol-table"[^>]*>.*?</table>', glossary, re.S)[0]
                self.assertEqual(re.findall(r'<th scope="col">(.*?)</th>', table), ['波动', '红灯', '黄灯'])
                rows = re.findall(r'<tr><th scope="row">(.*?)</th><td>(.*?)</td><td>(.*?)</td></tr>', table)
                if expected is None:
                    expected = [(f'{math.sqrt(math.exp(values["vol_ratio_red_z"] * s) * values["vol_ratio_red"]):.2f}',
                                 f'{math.sqrt(math.exp(values["vol_ratio_z"] * s) * values["vol_ratio"]):.2f}')
                                for s in (0.15, 0.30, 0.50)]
                self.assertEqual(rows, [(name, '≥约' + red + '倍', '≥约' + yellow + '倍')
                                        for name, (red, yellow) in zip(('低', '中', '高'), expected)])
                volume = self.plain(glossary.split('id="glossary-vol"', 1)[1].split('id="glossary-ma"', 1)[0])
                for term in ('1.4826×MAD', 'T＝exp(m＋k×s)', '新门槛＝√(T×F)',
                             '不是固定三档', '达到红门槛优先报红', '两条自适应参数启用且量比≥1×时',
                             '自适应未命中，不再用固定倍数补灯', '收盘分布无效或自适应关闭时也用固定门槛'):
                    self.assertIn(term, volume)
                for prefix, key in (('红k＝', 'vol_ratio_red_z'), ('黄k＝', 'vol_ratio_z'),
                                    ('红F＝', 'vol_ratio_red'), ('黄F＝', 'vol_ratio')):
                    self.assertIn(prefix + str(values[key]), volume)
                self.assertNotRegex(volume, r'≥[\d.]+σ|σ 或|两条σ阈值')
                self.assertEqual((settings, constants), before)
        self.assertIn('#glossary-vol-table thead th:nth-child(2){color:#ff8580}', assets.RULES_CSS)
        self.assertIn('#glossary-vol-table thead th:nth-child(3){color:#f0c674}', assets.RULES_CSS)

    def test_disabled_and_equal_moving_averages_and_adaptive_settings_keep_rules(self):
        for short, long in ((0, 0), (0, 250), (50, 50), (25, 180)):
            with self.subTest(short=short, long=long):
                markup = assets.rules_template({'ma_short': short, 'ma_long': long,
                    'boll_near_band_pct': 0, 'vol_ratio_z': 0, 'vol_ratio_red_z': 0}, {})
                text = self.plain(markup)
                for name, n in (('短均线', short), ('长均线', long)):
                    self.assertIn(name + '（' + ('启用' if n else '关闭') + '）', text)
                self.assertNotRegex(text, r'当前\d+日|\d+日均线|第一条|第二条|2～250')
                self.assertIn('两条同周期只计一次，按简单穿越判断', text)
                self.assertIn('关闭自适应或波动无效时固定0.5%', text)
                self.assertIn('两条自适应参数启用', text)
                self.assertIn('收盘分布无效或自适应关闭时也用固定门槛', text)
                self.assertIn('均线（短均线穿越；长均线触及±1%或穿越）', text)
                self.assertNotIn('${', markup)

    def test_unique_tables_and_dual_track_sentence_remain(self):
        glossary = assets.glossary_body({}, {})
        self.assertEqual(glossary.count('class="glossary-entry"'), 16)
        for unique in ('穿越确认幅度', '连续失败', '正常缓存＋已记录财报事件',
                       '来源日期仍早于财报', '供应商确认', '官方核验', '估算日期',
                       'BAMLH0A0HYM2', 'Cboe官方VIX日线优先', 'History of Market',
                       '金融压力NFCI', '至少隔1小时重试', '不是实时夜盘报价'):
            self.assertIn(unique, glossary)
        self.assertIn('双轨命中时，只选带符号距离较小的一轨；越轨距离为负，相等选上轨。',
                      self.plain(glossary))

    def test_fetch_quote_wording_uses_actual_loader_order_and_explicit_override(self):
        import monitor

        day = '2026-10-20'
        dates = monitor.market_sessions.preceding_sessions(day, 34) + [day]
        fixture = {'dates': dates, 'closes': [100.0] * 35, 'highs': [105.0] * 35,
                   'lows': [95.0] * 35, 'volumes': [1000.0] * 35,
                   'price': 100.0, 'prev_close': 100.0, 'source': 'offline_fixture'}
        labels = {'nasdaq': 'Nasdaq', 'yahoo': 'Yahoo', 'stooq': 'stooq'}
        constants = monitor._rule_constants()

        def fetch(providers, closed, symbol='AAPL-US'):
            calls = []

            def load(name, requested):
                calls.append((name, requested))
                return copy.deepcopy(providers.get(name))

            # 只替换外部行情接口，真实执行规范化、分源循环和日线校验。
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(socket, 'socket', side_effect=AssertionError('禁止联网')))
                stack.enter_context(patch.object(monitor.requests.sessions.Session, 'request',
                                                 side_effect=AssertionError('禁止 HTTP 请求')))
                stack.enter_context(patch.object(monitor, 'CLOSED_ONLY', closed))
                stack.enter_context(patch.object(monitor, 'TARGET_DATE', day))
                stack.enter_context(patch.object(monitor, 'S', dict(monitor.DEFAULT_SETTINGS, use_realtime=1)))
                stack.enter_context(patch.object(monitor, 'log'))
                stack.enter_context(patch.object(monitor, '_note_source_day'))
                live = stack.enter_context(patch.object(monitor, 'nasdaq_realtime', return_value=None))
                closing = stack.enter_context(patch.object(monitor, 'nasdaq_closing_quote', return_value=None))
                for name in labels:
                    stack.enter_context(patch.object(monitor, name + '_history',
                        side_effect=lambda requested, name=name: load(name, requested)))
                result = monitor.fetch_history(symbol)
                closing.assert_not_called()
                if closed or symbol != 'AAPL-US':
                    live.assert_not_called()
            return result, calls

        for closed, order in ((True, ('nasdaq', 'yahoo', 'stooq')),
                              (False, ('yahoo', 'stooq', 'nasdaq'))):
            key = 'quote_closed_sources' if closed else 'quote_intraday_sources'
            result, calls = fetch({}, closed)
            self.assertIsNone(result)
            self.assertEqual(calls, [(name, 'AAPL') for name in order])
            self.assertEqual(constants[key], ' → '.join(labels[name] for name, _ in calls))
            self.assertEqual(assets._rule_values({}, constants)[key], constants[key])
            for index, source in enumerate(order):
                with self.subTest(closed=closed, source=source):
                    providers = {name: fixture for name in order[index:]}
                    result, calls = fetch(providers, closed)
                    self.assertEqual(calls, [(name, 'AAPL') for name in order[:index + 1]])
                    self.assertEqual((result['source'], result['dates'][-1]), (source, day))
                    self.assertEqual(fixture['source'], 'offline_fixture')
            for symbol, requested in (('.SPX', '^GSPC'), ('^VIX', '^VIX'),
                                      ('ESMAIN', 'ES=F'), ('ES=F', 'ES=F')):
                with self.subTest(closed=closed, symbol=symbol):
                    data = copy.deepcopy(fixture)
                    if requested == 'ES=F':
                        data.update(quote_symbol='ES=F', quote_type='FUTURE', currency='USD',
                                    quote_timezone='America/New_York', quote_name='E-Mini S&P 500 Dec 26')
                    result, calls = fetch({'yahoo': data}, closed, symbol)
                    self.assertEqual(calls, [('yahoo', requested)])
                    self.assertEqual(result['source'], 'yahoo')
                    result, calls = fetch({}, closed, symbol)
                    self.assertIsNone(result)
                    self.assertEqual(calls, [('yahoo', requested)])

        text = self.plain(assets.glossary_body({}, constants))
        self.assertIn('收盘日线：Nasdaq → Yahoo → stooq；盘中日线：Yahoo → stooq → Nasdaq', text)
        self.assertIn('Nasdaq收盘报价可补当日一根，不冒充有效日内高低', text)
        override = dict(constants, quote_closed_sources='stooq → Yahoo → Nasdaq')
        self.assertIn('收盘日线：stooq → Yahoo → Nasdaq', self.plain(assets.glossary_body({}, override)))

    def test_svg_never_changes_attributes_or_weak_link_labels_and_escapes_values(self):
        markup = assets.rules_template({}, {'rsi_periods': '<script>超买</script>\"&'})
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('aria-hidden="true" focusable="false"', markup)
        for attribute in re.findall(r'(?:data-original|aria-label)="([^"]*)"', markup):
            self.assertNotIn('<', attribute)
        for link in re.findall(r'<a\b[^>]*>.*?</a>', markup, re.S):
            self.assertNotIn('<svg', link)
            self.assertNotIn('<span', link)
        for path in ('M3 3H15 M9 15V3 M5 7L9 3L13 7', 'M3 15H15 M9 3V15 M5 11L9 15L13 11',
                     'M7 15V3 M3 7L7 3L11 7', 'M7 3V15 M3 11L7 15L11 11'):
            self.assertIn('d="' + path + '"', markup)

    def test_filter_arrows_preserve_html_as_text_and_use_existing_classes(self):
        if not shutil.which('node'):
            self.skipTest('Node 未安装，跳过箭头离线执行')
        script = assets.PAGE_INTERACTION_JS
        start = script.index('function appendRsiArrows(')
        end = script.index('function renderSignalFilter(', start)
        code = r'''
const assert=require('assert/strict'),vm=require('vm'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const node=()=>({children:[],appendChild(n){this.children.push(n);}});
let htmlWrites=0;
const document={createTextNode:text=>({textContent:text}),createElement:tag=>{
  const n=node();n.tag=tag;Object.defineProperty(n,'innerHTML',{set(){htmlWrites++;throw Error('不能解析HTML');}});return n;
}};
const context={document};vm.createContext(context);vm.runInContext(input.js,context);
const parent=node();context.appendRsiArrows(parent,input.text);
assert.equal(parent.children.map(n=>n.textContent).join(''),input.text);
assert.equal(parent.children.filter(n=>n.className==='arrow-rsi-up').length,1);
assert.equal(parent.children.filter(n=>n.className==='arrow-rsi-down').length,1);
assert.equal(htmlWrites,0);assert(parent.children.some(n=>n.textContent.includes('<img')));
'''
        text = '布林上轨▲ · RSI 3条 ↓超卖 / RSI ↑超买 <img onerror=bad()> & <script>bad()</script>'
        result = subprocess.run(['node', '-e', code], input=json.dumps({'js': script[start:end], 'text': text}),
                                text=True, encoding='utf-8', capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_contract_and_python311_syntax(self):
        self.assertTrue(assets.PAGE_CSS.endswith(assets.RULES_CSS))
        self.assertTrue(assets.PAGE_INTERACTION_JS.startswith(assets.RULES_INTERACTION_JS))
        self.assertEqual(assets.RULES_INTERACTION_JS, '')
        for removed in ('revealRuleAnchor', 'initRuleAnchors', 'ruleHighlightTimer', 'nav-hit'):
            self.assertNotIn(removed, assets.PAGE_INTERACTION_JS)
        self.assertIn('function revealSignalPanel(', assets.PAGE_INTERACTION_JS)
        for source in ('page_assets.py', 'tests/mm_test_build.py'):
            ast.parse((ROOT / source).read_text(encoding='utf-8'), feature_version=(3, 11))
        if not shutil.which('node'):
            self.skipTest('Node 未安装，跳过 JS 语法检查')
        code = 'const vm=require("vm"),fs=require("fs");new vm.Script(fs.readFileSync(0,"utf8"));'
        for script in (assets.RULES_INTERACTION_JS, assets.PAGE_INTERACTION_JS):
            result = subprocess.run(['node', '-e', code], input=script, text=True,
                                    encoding='utf-8', capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get('MM_RULES_BROWSER_TEST') == '1',
                         '设置 MM_RULES_BROWSER_TEST=1 可运行本机已有 Playwright/Chrome 离线验收')
    def test_browser_rules_navigation_and_compact_styles_without_network(self):
        code = r'''
const assert=require('assert/strict'),fs=require('fs'),vm=require('vm'),{chromium}=require('playwright');
(async()=>{
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  new vm.Script(input.js);
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try{
    for(const width of [1280,390]){
      const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:'block'});
      let requests=0;await context.route('**/*',route=>{requests++;return route.abort();});
      const page=await context.newPage();
      await page.setContent('<style>'+input.css+'</style>'+input.html,{waitUntil:'domcontentloaded'});
      await page.evaluate(()=>{
        for(const [id,target] of [['rulesTpl','signalRules'],['glossaryTpl','glossaryRules']]){
          const box=document.createElement('details');box.className='card rule-card';box.id=target;
          box.innerHTML=document.getElementById(id).innerHTML;document.body.appendChild(box);
        }
        window.__timers=new Map();let seq=0;
        window.setTimeout=(fn,ms)=>{window.__timers.set(++seq,{fn,ms});return seq;};
        window.clearTimeout=id=>window.__timers.delete(id);
      });
      if(input.js)await page.addScriptTag({content:input.js});
      assert.deepEqual(await page.locator('details[open]').evaluateAll(ds=>ds.map(d=>d.id)),['signalCounts']);
      assert.equal(await page.locator('.runbar a[href="#signalRules"]').count(),0);
      assert.equal(await page.locator('#signalRules a[href^="#"],#glossaryRules a[href^="#"],.rule-cross-nav').count(),0);
      assert.equal(await page.evaluate(()=>window.__timers.size),0);
      assert.equal(await page.evaluate(()=>typeof window.revealRuleAnchor),'undefined');
      // 原生折叠仍有效，移除跳转不能影响查看完整正文。
      await page.locator('#signalRules > summary').click();
      await page.locator('#glossaryRules > summary').click();
      assert(await page.locator('#signalRules').evaluate(d=>d.open));
      assert.equal(await page.locator('#signalRulesBody svg').count(),input.ruleSvgCount);
      assert.equal(await page.locator('#glossaryRulesBody svg').count(),input.glossarySvgCount);
      assert(input.glossarySvgCount>0);
      const appearance=await page.evaluate(()=>{
        document.getElementById('glossaryRules').open=true;
        const p=document.querySelector('#signalRulesBody p.rule-original');
        const g=document.querySelector('#glossaryRulesBody p');
        const arrow=document.querySelector('#glossaryRulesBody svg');
        const table=document.getElementById('glossary-chg-table');
        return {pMargin:getComputedStyle(p).marginTop,gMargin:getComputedStyle(g).marginTop,
          arrowHeight:arrow.getBoundingClientRect().height,
          yellow:getComputedStyle(table.rows[0].cells[1]).color,
          red:getComputedStyle(table.rows[0].cells[2]).color,
          horizontal:document.documentElement.scrollWidth>window.innerWidth};
      });
      assert.equal(appearance.pMargin,'1px');assert.equal(appearance.gMargin,'2px');
      assert.equal(appearance.arrowHeight,18);assert.equal(appearance.yellow,'rgb(240, 198, 116)');
      assert.equal(appearance.red,'rgb(255, 133, 128)');assert(!appearance.horizontal);
      await page.locator('#signalRules > summary').click();
      await page.locator('#glossaryRules > summary').click();
      assert.deepEqual(await page.locator('details[open]').evaluateAll(ds=>ds.map(d=>d.id)),['signalCounts']);
      assert.equal(requests,0);await context.close();
    }
  }finally{await browser.close();}
  console.log('桌面与手机无规则跳转、原生折叠及正文样式验收通过');
})().catch(e=>{console.error(e);process.exit(1);});
'''
        markup = (assets.rules_template({}, {}) + '<div class="runbar"></div>'
                  '<details id="signalCounts" open><summary>统计</summary></details>'
                  '<details class="card" id="marketRiskRules"><summary>宏观细则</summary></details>')
        rule_body = re.search(r'<div[^>]*id="signalRulesBody"[^>]*>(.*?)</template>', markup, re.S)
        self.assertIsNotNone(rule_body, '规则模板须包含完整正文')
        glossary_body = re.search(r'<div[^>]*id="glossaryRulesBody"[^>]*>(.*?)</template>', markup, re.S)
        self.assertIsNotNone(glossary_body, '名词模板须包含完整正文')
        payload = {'html': markup, 'css': assets.PAGE_CSS,
                   'js': assets.RULES_INTERACTION_JS,
                   'ruleSvgCount': rule_body.group(1).count('<svg'),
                   'glossarySvgCount': glossary_body.group(1).count('<svg')}
        result = subprocess.run(['node', '-e', code], input=json.dumps(payload), text=True,
                                encoding='utf-8', capture_output=True, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_navigation_registers_no_listeners_timers_or_dom_changes(self):
        if not shutil.which('node'):
            self.skipTest('Node 未安装，跳过离线执行')
        code = r'''
const assert=require('assert/strict'),vm=require('vm'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
for(const hash of ['', '#nav-up-buy-three', '#glossary-amp-table']){
  let calls=0;
  const forbidden=()=>{calls++;throw Error('删除后的规则脚本不应操作DOM、注册监听或计时器');};
  const context={document:new Proxy({},{get:()=>forbidden}),
    window:{location:{hash},addEventListener:forbidden,setTimeout:forbidden},
    requestAnimationFrame:forbidden};
  vm.createContext(context);vm.runInContext(input.js,context);
  assert.equal(calls,0);assert.equal(typeof context.revealRuleAnchor,'undefined');
  assert.equal(typeof context.initRuleAnchors,'undefined');
}
console.log('无规则跳转处理器、监听或高亮计时器');
'''
        result = subprocess.run(['node', '-e', code], input=json.dumps({'js': assets.RULES_INTERACTION_JS}),
                                text=True, encoding='utf-8', capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
