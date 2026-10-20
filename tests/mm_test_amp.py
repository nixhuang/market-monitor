"""日内振幅规则 + 首页布局改版 测试"""
import hashlib
import re
import os
import sys
from pathlib import Path
from unittest.mock import patch
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402
import page_assets  # noqa: E402

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)

fails = []


def check(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


def mk(prev=100.0, hi=101.0, lo=99.0, last=100.0):
    closes = [100 + (0.6 if i % 2 == 0 else -0.6) + i * 0.002 for i in range(260)]
    closes[-1] = last
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    highs[-1], lows[-1] = hi, lo
    return {"closes": closes, "highs": highs, "lows": lows, "volumes": [1e6] * 260,
            "price": last, "prev_close": prev, "dates": ["2026-10-06"] * 260,
            "source": "test", "realtime": False}


# --- 1. 振幅 12%（106/94，昨收100）→ 红 ---
lv, sig, d = monitor.analyze_symbol("AMPRED", {}, mk(hi=106, lo=94, last=100), group="position")
check(lv == "red", f"振幅 12% → 红（实测 {lv}）")
check(any("振幅 12.0%" in s for s in sig), f"振幅信号文案正确：{sig}")
check(abs((d.get("amp") or 0) - 12.0) < 0.01, f"detail.amp = {d.get('amp')}")

# --- 2. 振幅 6% → 黄（且收盘没动，涨跌幅规则不响） ---
lv2, sig2, _ = monitor.analyze_symbol("AMPYEL", {}, mk(hi=103, lo=97, last=100), group="position")
check(any("振幅 6.0%" in s for s in sig2), f"振幅 6% 有信号：{sig2}")
check(not any("异动" in s or "波动" in s for s in sig2), "收盘没动 → 涨跌幅规则静默（振幅规则的价值）")

# --- 3. 振幅 2% → 不响 ---
lv3, sig3, _ = monitor.analyze_symbol("AMPOK", {}, mk(hi=101, lo=99, last=100), group="position")
check(not any("振幅" in s for s in sig3), f"振幅 2% 不报警：{sig3}")

# --- 4. 阈值可从 settings 覆盖 ---
old = monitor.S["amp_yellow"]
monitor.S["amp_yellow"] = 3.0
lv4, sig4, _ = monitor.analyze_symbol("AMPCFG", {}, mk(hi=102.5, lo=99.5, last=100), group="position")
check(any("振幅 3.0%" in s for s in sig4), f"阈值改成 3% 后生效：{sig4}")
monitor.S["amp_yellow"] = old

# --- 5. 验证实际渲染：静态资源抽离后，不再通过 monitor 源码猜测页面 ---
with patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    page = monitor.render({}, [], 0)
m = re.search(r'<div class="runbar">(.*?)</div>\s*<div class="statusrow">(.*?)</div>\s*\n', page, re.S)
check(bool(m), "runbar 与 statusrow 两排结构存在")
if m:
    bar, row = m.group(1), m.group(2)
    check('id="btnRunNow"' in bar and 'btnHardRefresh' in bar
          and bar.index('btnRunNow') < bar.index('btnCheckStatus') < bar.index('btnHardRefresh') < bar.index('btnlink'),
          "第三排顺序：立即运行 → 查运行状态 → 强制刷新 → 设置")
    check('>设置</a>' in bar and './edit.html' in bar, "按钮改名「设置」且指向 edit.html")
    check('id="runLight"' in row and 'id="runLightTxt"' in row, "第四排：状态灯 + 文案")
    check('runLight' not in bar, "灯已从第三排移走")
styles = ''.join(re.findall(r'<style>(.*?)</style>', page, re.S))
check(monitor.PAGE_CSS == page_assets.PAGE_CSS and '.statusrow{' in page_assets.PAGE_CSS
      and page_assets.PAGE_CSS in styles, "statusrow 共用样式已实际嵌入页面")
check('改自选清单' not in page, "旧的「改自选清单」已删除")
# 脚本内容 hash 代替手工日期版本号；独立计算期望值，避免只验证同一个 helper 自洽。
content = (Path(ROOT) / 'run-status.js').read_bytes().replace(b'\r\n', b'\n')
expected_url = './run-status.js?v=' + hashlib.sha256(content).hexdigest()[:16]
check(page_assets.asset_url(ROOT, 'run-status.js') == expected_url
      and re.findall(r'<script src="([^"]*run-status\.js[^"]*)"></script>', page) == [expected_url],
      "运行状态脚本唯一引用携带当前内容 hash")
check((monitor.DEFAULT_SETTINGS['amp_yellow'], monitor.DEFAULT_SETTINGS['amp_red']) == (5.0, 8.0),
      "振幅默认阈值仍为5%／8%")
check(page_assets.rules_template(monitor.S, monitor._rule_constants()) in page,
      "首页实际使用共用规则模板")
with patch.dict(monitor.S, {'amp_yellow': 3.0, 'amp_red': 9.0}), \
        patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    changed_page = monitor.render({}, [], 0)
    changed_rules = page_assets.rules_template(monitor.S, monitor._rule_constants())
check(changed_rules in changed_page and '振幅 ≥ <b>9%</b>' in changed_rules
      and '≥ <b>3%</b> <span class="lamp-yellow">黄</span>' in changed_rules,
      "覆盖振幅阈值后，共用说明和实际首页同步更新")

print()
if fails:
    print(f"{len(fails)} 项失败")
    sys.exit(1)
print("全部通过")
