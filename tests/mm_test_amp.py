"""日内振幅规则 + 首页布局改版 测试"""
import hashlib
import html
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
      and page_assets.PAGE_CSS.removesuffix(page_assets.RULES_CSS) in styles
      and styles.count(page_assets.RULES_CSS) == 1, "statusrow 共用样式完整嵌入且规则样式只一份")
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
check(changed_rules in changed_page,
      "覆盖振幅阈值后，实际首页仍完整嵌入最新双模板")
# 独立固定三档数值，精确检查两表的标题、列序与全部行，避免生成器自证。
for label, rendered, expected_amp in (
        ('默认', page, [['低 ×0.6', '≥3%', '≥4.8%'],
                        ['中 ×1', '≥5%', '≥8%'], ['高 ×1.4', '≥7%', '≥11.2%']]),
        ('覆盖3%／9%', changed_page, [['低 ×0.6', '≥1.8%', '≥5.4%'],
                                    ['中 ×1', '≥3%', '≥9%'], ['高 ×1.4', '≥4.2%', '≥12.6%']])):
    rules_matches = re.findall(r'<template id="rulesTpl">(.*?)</template>', rendered, re.S)
    glossary_matches = re.findall(r'<template id="glossaryTpl">(.*?)</template>', rendered, re.S)
    check(len(rules_matches) == len(glossary_matches) == 1, label + '：两模板各出现一次')
    rules = rules_matches[0] if len(rules_matches) == 1 else ''
    glossary = glossary_matches[0] if len(glossary_matches) == 1 else ''
    sections = re.findall(r'<section id="glossary-calculation">(.*?)</section>', glossary, re.S)
    check(len(sections) == 1, label + '：阈值表位于唯一名词计算区')
    calculation = sections[0] if len(sections) == 1 else ''
    for kind, expected_rows in (
            ('chg', [['低 ×0.6', '≥1.2%', '≥2.4%'],
                     ['中 ×1', '≥2%', '≥4%'], ['高 ×1.4', '≥2.8%', '≥5.6%']]),
            ('amp', expected_amp)):
        ident = 'glossary-' + kind + '-table'
        pattern = r'<table\b[^>]*\bid="' + ident + r'"[^>]*>(.*?)</table>'
        tables = re.findall(pattern, calculation, re.S)
        check(len(tables) == 1 and rendered.count('id="' + ident + '"') == 1,
              label + '：' + ident + ' 仅在名词区出现一次')
        table = tables[0] if len(tables) == 1 else ''
        cells = [[html.unescape(re.sub(r'<[^>]+>', '', cell)).strip()
                  for cell in re.findall(r'<t[hd]\b[^>]*>(.*?)</t[hd]>', row, re.S)]
                 for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S)]
        check(cells == [['波动档位', '黄灯', '红灯'], *expected_rows],
              label + '：' + ident + ' 列序及三档全部阈值精确匹配')
        check('id="' + ident + '"' not in rules and
              rules.count('href="#' + ident + '"') == 1,
              label + '：预警区不重复阈值表且唯一跳转到' + ident)

print()
if fails:
    print(f"{len(fails)} 项失败")
    sys.exit(1)
print("全部通过")
