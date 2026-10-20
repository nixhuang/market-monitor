"""M7 离线回归：仅用独立临时 Git 仓库，不 fetch/reset/commit/push 真实仓库。"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_support as b

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
        for name in ("fundamentals.py", "rules_config.py", "page_assets.py", "future.js", "future.css",
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
        for path in b.REQUIRED_INPUTS | {"fundamentals.py", "page_assets.py", "rules_config.py",
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


if __name__ == "__main__":
    unittest.main()
