"""规则 schema、页面注入与资源 hash 的纯离线回归；不改用户配置或公开页面。"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor
import page_assets
import rules_config
from fundamentals import FUND_DEFAULT, FUND_LIMITS


class RulesRegression(unittest.TestCase):
    def setUp(self):
        p = patch.object(monitor, "S", dict(monitor.DEFAULT_SETTINGS))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(monitor, "_cost_seed_json", return_value="{}")
        p.start()
        self.addCleanup(p.stop)

    def render(self):
        return monitor.render({}, [], 0, snapshot={})

    def test_defaults_and_public_compatibility(self):
        self.assertIs(monitor.DEFAULT_SETTINGS, rules_config.DEFAULT_SETTINGS)
        self.assertIs(monitor.SETTING_LIMITS, rules_config.SETTING_LIMITS)
        self.assertIs(monitor.SETTING_ORDER, rules_config.SETTING_ORDER)
        self.assertEqual(monitor.valid_settings(monitor.DEFAULT_SETTINGS), "")
        self.assertEqual(monitor.DEFAULT_SETTINGS["trigger_gap_pct"], 5)
        self.assertEqual(monitor.DEFAULT_SETTINGS["reduce_gap_pct"], 3)
        self.assertEqual(monitor.DEFAULT_SETTINGS["rsi_low"], 35)
        self.assertEqual(monitor.DEFAULT_SETTINGS["ma_long"], 200)
        for key, value in FUND_DEFAULT.items():
            self.assertEqual(monitor.DEFAULT_SETTINGS[key], value)
            self.assertEqual(monitor.SETTING_LIMITS[key], FUND_LIMITS[key])
        self.assertIn('<template id="rulesTpl">', monitor.RULES_TEMPLATE)
        self.assertIn(monitor.RULES_BODY, monitor.RULES_TEMPLATE)

    def test_invalid_fields_warn_and_preserve_other_overrides(self):
        invalid = {"boll_n": 1, "ma_short": 1, "ma_long": 200.5, "use_realtime": True,
                   "trigger_gap_pct": "7", "reduce_gap_pct": float("nan"),
                   "vol_ratio": float("inf"), "dca_start": "2026-02-30",
                   "fund_enable": 0.5, "unknown_option": 8, "fund_unknown": 3}
        warnings = []
        got = rules_config.resolve_settings(dict(invalid, fund_om_drop=7, chg_red=9), warnings.append)
        for key in invalid:
            self.assertTrue(any(key in line for line in warnings), key)
            if key in monitor.DEFAULT_SETTINGS:
                self.assertEqual(got[key], monitor.DEFAULT_SETTINGS[key])
        self.assertEqual(got["fund_om_drop"], 7)
        self.assertEqual(got["chg_red"], 9)
        self.assertEqual(monitor.valid_settings(got), "")

    def test_order_conflicts_only_reset_their_group(self):
        warnings = []
        got = rules_config.resolve_settings({"rsi_low": 90, "rsi_high": 80,
            "chg_yellow": 20, "chg_red": 10, "fund_rev_red": -5, "fund_rev_yellow": -15,
            "fund_gm_drop": 8, "trigger_gap_pct": 7}, warnings.append)
        for key in ("rsi_low", "rsi_high", "chg_yellow", "chg_red", "fund_rev_red", "fund_rev_yellow"):
            self.assertEqual(got[key], monitor.DEFAULT_SETTINGS[key])
        self.assertEqual(got["fund_gm_drop"], 8)
        self.assertEqual(got["trigger_gap_pct"], 7)
        self.assertEqual(len(warnings), 3)

    def test_load_settings_retains_fund_overrides_and_does_not_write(self):
        data = {"fund_enable": 0, "fund_om_drop": 8, "fund_report_grace_days": 31,
                "trigger_gap_pct": 7, "reduce_gap_pct": 2, "ma_long": 1}
        with patch("builtins.open", mock_open(read_data=json.dumps(data))) as opened, \
                patch.object(monitor, "log") as warning:
            got = monitor.load_settings()
        for key in data.keys() - {"ma_long"}:
            self.assertEqual(got[key], data[key])
        self.assertEqual(got["ma_long"], 200)
        self.assertIn("ma_long", warning.call_args.args[0])
        opened().write.assert_not_called()

    def test_absent_and_malformed_settings_use_safe_defaults(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            self.assertEqual(monitor.load_settings(), monitor.DEFAULT_SETTINGS)
        for raw in ("[]", "null", "{broken"):
            with self.subTest(raw=raw), patch("builtins.open", mock_open(read_data=raw)), \
                    patch.object(monitor, "log") as warn:
                self.assertEqual(monitor.load_settings(), monitor.DEFAULT_SETTINGS)
                self.assertTrue(warn.called)

    def test_complete_nondefault_rules_injected_before_real_cost_script(self):
        monitor.S.update(trigger_gap_pct=7.25, reduce_gap_pct=1.75, fund_om_drop=6,
                         fund_report_grace_days=45, signal_single_open=0)
        page = self.render()
        match = re.search(r"window\.__MM_RULES__=(.*?);</script>", page)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match[1]), monitor.S)
        self.assertLess(match.start(), page.index("var RULES=window.__MM_RULES__"))
        self.assertEqual(page.count("window.__MM_RULES__="), 1)
        self.assertIn("<b>7.25%</b>", page)
        self.assertIn("<b>1.75%</b>", page)
        self.assertIn("<b>45</b> 天宽限", page)
        self.assertIn('window.MM_SETTINGS={"signal_single_open":0}', page)

    def test_render_does_not_use_import_time_rules_snapshot(self):
        first = self.render()
        monitor.S.update(trigger_gap_pct=9, reduce_gap_pct=0, rsi_low=28, rsi_high=82)
        with patch.object(monitor, "RULES_TEMPLATE", "过期模板不能进入页面"):
            second = self.render()
        self.assertNotEqual(first, second)
        self.assertNotIn("过期模板不能进入页面", second)
        self.assertIn("<b>9%</b>", second)
        self.assertIn("超买 ≥ <b>82</b>", second)
        self.assertIn("超卖 ≤ <b>28</b>", second)

    def test_add_and_reduce_rules_keep_actual_scope_and_distinct_thresholds(self):
        monitor.S.update(trigger_gap_pct=7, reduce_gap_pct=2)
        for group in ("position", "focus", "technology"):
            x = SimpleNamespace(cfg={"trigger": 100, "reduce": 110}, price=106, group=group)
            add, reduce = monitor._Alerts(), monitor._Alerts()
            monitor._rule_add_price(add, x)
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(add.level, "red")
            self.assertEqual(reduce.level, "green")
            x.price = 109
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(reduce.level, "red" if group == "position" else "green")
            x.price = 500
            monitor._rule_reduce_price(reduce, x)
            self.assertEqual(reduce.level, "red" if group == "position" else "green")
        text = page_assets.rules_body(monitor.S, monitor._rule_constants())
        self.assertIn("所有分组", text)
        self.assertIn("position（持仓）", text)
        self.assertIn("含重点关注", text)
        self.assertIn("不是正负区间", text)

    def test_ma_zero_and_equal_period_explanations(self):
        for short, long in ((0, 0), (0, 200), (50, 0), (50, 50)):
            with self.subTest(short=short, long=long):
                monitor.S.update(ma_short=short, ma_long=long)
                self.assertFalse(monitor.valid_settings(monitor.S))
                body = page_assets.rules_body(monitor.S, monitor._rule_constants())
                self.assertEqual("第一条均线已关闭" in body, short == 0)
                self.assertEqual("第二条均线已关闭" in body, long == 0)
                self.assertEqual("两条均线同为" in body, short == long and short > 0)
                x = monitor._analysis_context("OFFLINE", {},
                    {"closes": [100.0] * 60, "price": 100, "prev_close": 100,
                     "highs": [], "lows": [], "dates": []}, "position")
                self.assertEqual([p for p, _ in x.moving_averages], list(dict.fromkeys(p for p in (short, long) if p)))

    def test_volume_adaptive_off_on_and_fixed_fallback(self):
        for yellow, red in ((0, 3.6), (1.8, 0), (0, 0), (1.8, 3.6)):
            with self.subTest(yellow=yellow, red=red):
                monitor.S.update(vol_ratio_z=yellow, vol_ratio_red_z=red, vol_ratio=2, vol_ratio_red=4)
                self.assertFalse(monitor.valid_settings(monitor.S))
                active = yellow > 0 and red > 0
                self.assertEqual(monitor._vol_ratio_z_enabled(), active)
                al = monitor._Alerts()
                monitor._rule_vol_ratio(al, SimpleNamespace(vol_ratio=2.5, vol_ratio_z=5,
                    vol_ratio_date="2026-10-01", vol_ratio_intraday=False))
                self.assertEqual(al.level, "red" if active else "yellow")
                text = page_assets.rules_body(monitor.S, monitor._rule_constants())
                self.assertEqual("量比自适应已开启" in text, active)
                self.assertEqual("量比自适应已关闭" in text, not active)
                self.assertIn("<b>4×</b>", text)
                self.assertIn("<b>2×</b>", text)

    def test_boll_adaptive_matches_actual_formula_and_off_switch(self):
        monitor.S.update(boll_near_band_pct=25, boll_near_pct=0.75, boll_n=30, boll_k=3)
        self.assertAlmostEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=2)),
                               0.25 * 4 * monitor._BOLL_SIGMA_FACTOR * 2)
        self.assertEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=None)), 0.75)
        body = page_assets.rules_body(monitor.S, monitor._rule_constants())
        self.assertIn("布林带宽自适应已开启", body)
        self.assertIn("不随 boll_k 改变", body)
        self.assertIn("<b>30</b>", body)
        self.assertIn("<b>25%</b>", body)
        monitor.S["boll_near_band_pct"] = 0
        self.assertEqual(monitor._boll_near_pct(SimpleNamespace(vol_sigma=2)), 0.75)
        body = page_assets.rules_body(monitor.S, monitor._rule_constants())
        self.assertIn("布林带宽自适应已关闭", body)
        self.assertNotIn("布林带宽自适应已开启", body)

    def test_fund_current_parameters_and_gray_are_not_yoy(self):
        monitor.S.update(fund_rev_red=-35, fund_rev_yellow=-15, fund_om_drop=6,
            fund_gm_drop=7, fund_de_rise=45, fund_cr_drop=25, fund_roe_drop=40, fund_pm_abnormal=65)
        body = page_assets.rules_body(monitor.S, monitor._rule_constants())
        for value in ("-35%", "-15%", "6", "7", "45%", "25%", "40%", "65%"):
            self.assertIn(f"<b>{value}</b>", body)
        self.assertIn("3 个季度前", body)
        self.assertIn("不是同比", body)
        self.assertNotIn("营收同比", body)
        self.assertIn("灰色待更新", body)
        self.assertEqual(monitor.fund_rule("fund_om_drop"), 6)
        monitor.S["fund_enable"] = 0
        self.assertIn("基本面检查已关闭", page_assets.rules_body(monitor.S, monitor._rule_constants()))

    def test_json_injection_escapes_script_end(self):
        monitor.S["dca_start"] = '</script><script>alert("示例")</script>\u2028'
        _, js = monitor._cost_panel_assets([])
        payload = re.search(r"window\.__MM_RULES__=(.*?);</script>", js)[1]
        self.assertNotIn("<", payload)
        self.assertEqual(json.loads(payload), monitor.S)

    def test_static_assets_remain_public_without_duplicate_definitions(self):
        self.assertIs(monitor.PAGE_CSS, page_assets.PAGE_CSS)
        self.assertIs(monitor.PAGE_INTERACTION_JS, page_assets.PAGE_INTERACTION_JS)
        tree = ast.parse((ROOT / "monitor.py").read_text(encoding="utf-8"))
        assigned = {t.id for node in tree.body if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)}
        self.assertNotIn("PAGE_CSS", assigned)
        self.assertNotIn("PAGE_INTERACTION_JS", assigned)
        page = self.render()
        self.assertIn(page_assets.PAGE_CSS, page)
        self.assertIn(page_assets.PAGE_INTERACTION_JS, page)

    def test_content_hash_changes_even_with_same_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-status.js"
            path.write_bytes(b"const flag=1;")
            before = path.stat()
            first = page_assets.asset_url(tmp, path.name)
            path.write_bytes(b"const flag=2;")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = page_assets.asset_url(tmp, path.name)
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith(hashlib.sha256(path.read_bytes()).hexdigest()[:16]))
            with patch.object(monitor, "BASE", tmp), patch.object(monitor, "_cost_panel_assets", return_value=("", "")):
                self.assertIn(second, self.render())
                self.assertNotIn(first, self.render())

    def test_data_only_base_uses_actual_source_asset(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(monitor, "BASE", tmp), \
                patch.object(monitor, "_cost_panel_assets", return_value=("", "")):
            self.assertIn(page_assets.asset_url(ROOT, "run-status.js"), self.render())

    def test_resource_hash_and_generated_block_are_cross_platform(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        windows = source.replace("\n", "\r\n")
        self.assertEqual(page_assets.sync_edit_html(windows, ROOT, monitor._rule_constants()), windows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-status.js"
            path.write_bytes(b"const flag=1;\nconst other=2;\n")
            unix = page_assets.asset_url(tmp, path.name)
            path.write_bytes(b"const flag=1;\r\nconst other=2;\r\n")
            self.assertEqual(page_assets.asset_url(tmp, path.name), unix)

    def test_editor_generated_schema_rules_hash_are_current_and_idempotent(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        expected = page_assets.sync_edit_html(source, ROOT, monitor._rule_constants())
        self.assertEqual(source, expected, "请运行 page_assets.py --sync-edit 同步生成片段")
        self.assertEqual(expected, page_assets.sync_edit_html(expected, ROOT, monitor._rule_constants()))
        schema = json.loads(re.search(r"const SET_SCHEMA=(.*?);\n", source)[1])
        self.assertEqual(schema, json.loads(json.dumps(rules_config.browser_schema())))
        for name in ("run-status.js", "list-import.js"):
            self.assertIn(page_assets.asset_url(ROOT, name), source)
        self.assertNotRegex(source, r'\.js\?v=2026\d{4}(?:-\d+)?"')

    def test_editor_hash_refresh_is_limited_to_generated_parts(self):
        source = (ROOT / "edit.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("run-status.js", "list-import.js"):
                (Path(tmp) / name).write_bytes((ROOT / name).read_bytes() + b"\n/* offline */")
            result = page_assets.sync_edit_html(source, tmp, monitor._rule_constants())
            self.assertNotEqual(source, result)
            for name in ("run-status.js", "list-import.js"):
                result = result.replace(page_assets.asset_url(tmp, name), page_assets.asset_url(ROOT, name))
            self.assertEqual(result, source)

    def test_browser_and_python_rules_normalization_and_thresholds_agree(self):
        settings = [dict(monitor.DEFAULT_SETTINGS), dict(monitor.DEFAULT_SETTINGS,
            trigger_gap_pct=7.25, reduce_gap_pct=1.75, ma_short=0, ma_long=0,
            boll_near_band_pct=0, vol_ratio_z=0, vol_ratio_red_z=4,
            rsi_low=26.5, rsi_high=82, fund_om_drop=7, fund_enable=0)]
        settings += [dict(monitor.DEFAULT_SETTINGS, ma_short=s, ma_long=l, trigger_gap_pct=t, reduce_gap_pct=r)
                     for s, l, t, r in ((50, 0, 0, 4), (0, 200, 6, 0), (50, 50, 8, 2))]
        overrides = [{"rsi_low": 26.5, "fund_om_drop": 7, "fund_enable": 0},
                     {"ma_short": 0, "ma_long": 25, "vol_ratio_z": 0, "vol_ratio_red_z": 0.5},
                     {"boll_n": True, "ma_long": 1, "dca_start": "2026-02-30",
                      "rsi_low": 80, "rsi_high": 70, "fund_rev_red": -1, "fund_rev_yellow": -10},
                     {"trigger_gap_pct": "8", "reduce_gap_pct": None, "signal_single_open": 0.5}]
        payload = {"settings": settings, "overrides": overrides}
        # 真实生成块与真实编辑页函数均在 VM 中执行；Node 本身也经同一个离线入口。
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('edit.html','utf8'),input=JSON.parse(fs.readFileSync(0,'utf8'));
const generated=source.match(/\/\* MM_SCHEMA_BEGIN[^\n]*\*\/[\s\S]*?\/\* MM_SCHEMA_END \*\//)[0];
const validate=source.slice(source.indexOf('const DATE_RE='),source.indexOf('let SET='));
const normalize=source.slice(source.indexOf('function normalizeSettings('),source.indexOf('async function loadSettings('));
const collect=source.slice(source.indexOf('function collectSettings('),source.indexOf('function normalizeSettings('));
const context=vm.createContext({});
vm.runInContext(generated+'\n'+validate+'\nlet SET={...SET_DEFS};let els={};const $=id=>els[id]||null;\n'+normalize+'\n'+collect,context);
context.input=input;
const result=vm.runInContext(`(()=>{
 const bodies=input.settings.map(s=>renderRuleText(s));
 const normalized=input.overrides.map(o=>normalizeSettings(o));
 const errors=input.settings.map(s=>{
   SET={...s};els={};Object.entries(s).forEach(([k,v])=>{if(!k.startsWith('fund_'))els['set_'+k]={value:String(v)};});
   return settingsError();
 });
 SET={...SET_DEFS,fund_om_drop:7,fund_report_grace_days:45};els={set_trigger_gap_pct:{value:'8'},set_reduce_gap_pct:{value:'2'}};
 collectSettings();const saved=JSON.parse(setJsonText());
 return {bodies,normalized,errors,saved};
})()`,context);
const cost=fs.readFileSync('cost_panel.js','utf8');
const bindings=cost.slice(cost.indexOf('var RULES=window.__MM_RULES__'),cost.indexOf('// 清仓通过移组完成'));
result.bindings=input.settings.map(s=>{const c=vm.createContext({window:{__MM_RULES__:s},own:(o,k)=>Object.prototype.hasOwnProperty.call(o,k)});vm.runInContext(bindings,c);return [c.GAP_TRIGGER,c.GAP_REDUCE];});
process.stdout.write(JSON.stringify(result));
'''
        result = subprocess.run([sys.executable, "-B", str(ROOT / "tests/run_all.py"), "--offline-node", "-e", script],
            cwd=ROOT, input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        got = json.loads(result.stdout)
        self.assertEqual(got["bodies"], [page_assets.rules_body(s, monitor._rule_constants()) for s in settings])
        self.assertEqual(got["errors"], [""] * len(settings))
        self.assertEqual(got["bindings"], [[s["trigger_gap_pct"], s["reduce_gap_pct"]] for s in settings])
        self.assertEqual(got["saved"]["fund_om_drop"], 7)
        self.assertEqual(got["saved"]["fund_report_grace_days"], 45)
        self.assertEqual(got["saved"]["trigger_gap_pct"], 8)
        for raw, actual in zip(overrides, got["normalized"]):
            warnings = []
            self.assertEqual(actual["values"], rules_config.resolve_settings(raw, warnings.append))
            self.assertEqual(bool(actual["warnings"]), bool(warnings))

    def test_changed_modules_parse_as_python_311(self):
        for name in ("monitor.py", "rules_config.py", "page_assets.py", "tests/mm_test_rules.py"):
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name, feature_version=(3, 11))


if __name__ == "__main__":
    unittest.main(verbosity=2)
