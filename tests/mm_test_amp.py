"""日内振幅规则 + 首页布局改版 测试"""
import re
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, ROOT)
import monitor  # noqa: E402

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
lv, sig, d = monitor.analyze_symbol("AMPRED", {}, mk(hi=106, lo=94, last=100), group="watch")
check(lv == "red", f"振幅 12% → 红（实测 {lv}）")
check(any("振幅 12.0%" in s for s in sig), f"振幅信号文案正确：{sig}")
check(abs((d.get("amp") or 0) - 12.0) < 0.01, f"detail.amp = {d.get('amp')}")

# --- 2. 振幅 6% → 黄（且收盘没动，涨跌幅规则不响） ---
lv2, sig2, _ = monitor.analyze_symbol("AMPYEL", {}, mk(hi=103, lo=97, last=100), group="watch")
check(any("振幅 6.0%" in s for s in sig2), f"振幅 6% 有信号：{sig2}")
check(not any("异动" in s or "波动" in s for s in sig2), "收盘没动 → 涨跌幅规则静默（振幅规则的价值）")

# --- 3. 振幅 2% → 不响 ---
lv3, sig3, _ = monitor.analyze_symbol("AMPOK", {}, mk(hi=101, lo=99, last=100), group="watch")
check(not any("振幅" in s for s in sig3), f"振幅 2% 不报警：{sig3}")

# --- 4. 阈值可从 settings 覆盖 ---
old = monitor.S["amp_yellow"]
monitor.S["amp_yellow"] = 3.0
lv4, sig4, _ = monitor.analyze_symbol("AMPCFG", {}, mk(hi=102.5, lo=99.5, last=100), group="watch")
check(any("振幅 3.0%" in s for s in sig4), f"阈值改成 3% 后生效：{sig4}")
monitor.S["amp_yellow"] = old

# --- 5. 首页布局：设置按钮在刷新状态右边，灯单独一排 ---
src = open(os.path.join(ROOT, 'monitor.py'), encoding='utf-8').read()
m = re.search(r'<div class="runbar">(.*?)</div>\s*<div class="statusrow">(.*?)</div>\s*\n', src, re.S)
check(bool(m), "runbar 与 statusrow 两排结构存在")
if m:
    bar, row = m.group(1), m.group(2)
    check('id="btnRunNow"' in bar and 'btnHardRefresh' in bar
          and bar.index('btnRunNow') < bar.index('btnCheckStatus') < bar.index('btnHardRefresh') < bar.index('btnlink'),
          "第三排顺序：立即运行 → 查运行状态 → 强制刷新 → 设置")
    check('>设置</a>' in bar and './edit.html' in bar, "按钮改名「设置」且指向 edit.html")
    check('id="runLight"' in row and 'id="runLightTxt"' in row, "第四排：状态灯 + 文案")
    check('runLight' not in bar, "灯已从第三排移走")
check('statusrow{{' in src, "statusrow 样式已定义")
check('改自选清单' not in src, "旧的「改自选清单」已删除")
check('v=20261008-10' in src, "版本号已升到 20261008-10")

print()
if fails:
    print(f"{len(fails)} 项失败")
    sys.exit(1)
print("全部通过")
