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

# --- 5. 全部现有分组及自定义分组：同一自适应红黄规则，0与低振幅静默 ---
groups = [g['item_group'] for g in monitor.GROUPS] + ['custom_group']
for group in groups:
    for amplitude, expected in ((0, 'green'), (.01, 'green'), (2, 'green'),
                                (5.999, 'green'), (6, 'yellow'), (9.999, 'yellow'), (10, 'red'), (12, 'red')):
        with patch.dict(monitor.S, {'amp_yellow': 6, 'amp_red': 10,
                                   'near_52w_low_pct': 0, 'ma_short': 0, 'ma_long': 0}), \
                patch.object(monitor, 'vol_profile', return_value=('mid', None)), \
                patch.object(monitor, 'AMP_GROUPS', ()), \
                patch.object(monitor, 'calc_rsi', return_value=50), \
                patch.object(monitor, 'boll', return_value=(None, None, None)):
            level, signals, detail = monitor.analyze_symbol('AMPALL', {},
                mk(hi=100 + amplitude / 2, lo=100 - amplitude / 2), group=group)
        marks = [] if expected == 'green' else [('amp', expected)]
        check((level, detail['foldable'], detail['signal_marks']) == (expected, True, marks),
              f'{group} 振幅{amplitude:g}%：统一红黄或静默，不强制不折叠')
        check(abs(detail['amp'] - amplitude) < 1e-10 and
              (signals == [] if expected == 'green' else len(signals) == 1 and signals[0].startswith(f'振幅 {amplitude:.1f}%')),
              f'{group} 振幅{amplitude:g}%：保留真实数值，未达黄线无描述')
        check(monitor.text_only_members([detail]) == [] and
              monitor.signal_counts([detail])['amp'] == {'label': '振幅', 'red': int(expected == 'red'),
                                                        'yellow': int(expected == 'yellow'), 'purple': 0},
              f'{group} 振幅{amplitude:g}%：只计真实红黄，不计振幅灰')

# 缺失/合成/错位的日内范围不能以现价回退制造0%或低估振幅。
from types import SimpleNamespace
for group in groups:
    cases = ('missing', 'empty', 'high_none', 'low_none', 'mismatched', 'synthetic', 'bad_validity',
             'reversed', 'high_bool', 'low_string', 'high_nan', 'low_inf', 'high_zero', 'low_negative')
    for case in cases:
        data = mk(hi=106, lo=94)
        if case == 'missing':
            data.pop('highs'); data.pop('lows')
        elif case == 'empty':
            data['highs'] = []; data['lows'] = []
        elif case == 'mismatched':
            data['highs'] = data['highs'][:-1]
        elif case == 'synthetic':
            data['ma_range_valid'] = [True] * 259 + [False]
        elif case == 'bad_validity':
            data['ma_range_valid'] = [True]
        elif case == 'reversed':
            data['highs'][-1], data['lows'][-1] = 94, 106
        else:
            key, bad = {'high_none': ('highs', None), 'low_none': ('lows', None),
                        'high_bool': ('highs', True), 'low_string': ('lows', '94'),
                        'high_nan': ('highs', float('nan')), 'low_inf': ('lows', float('inf')),
                        'high_zero': ('highs', 0), 'low_negative': ('lows', -1)}[case]
            data[key][-1] = bad
        x = SimpleNamespace(data=data, closes=data['closes'], price=data['price'], prev=data['prev_close'],
                            group=group, touch_up=106, touch_dn=94, amp_yellow=6, amp_red=10, foldable=True)
        al = monitor._Alerts()
        monitor._rule_amplitude(al, x)
        check(x.amp is None and x.foldable is True and al.marks == [] and al.signals == [] and al.level == 'green',
              f'{group} {case}：全组无效范围不制造振幅灯、不改变折叠')
    for validity in (None, [], [True] * 260):
        valid_zero = mk(hi=100, lo=100)
        if validity is not None:
            valid_zero['ma_range_valid'] = validity
        with patch.dict(monitor.S, {'near_52w_low_pct': 0, 'ma_short': 0, 'ma_long': 0}), \
                patch.object(monitor, 'calc_rsi', return_value=50), \
                patch.object(monitor, 'boll', return_value=(None, None, None)):
            _, _, detail = monitor.analyze_symbol('AMPVALIDZERO', {}, valid_zero, group=group)
        check(detail['amp'] == 0 and detail['signal_marks'] == [] and detail['foldable'] and detail['signals'] == [],
              f'{group} 范围有效位{bool(validity)}：真实0%保留数值，默认静默且可折叠')

# --- 6. 验证实际渲染：静态资源抽离后，不再通过 monitor 源码猜测页面 ---
with patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    page = monitor.render({}, [], 0)
m = re.search(r'<div class="runbar">(.*?)</div>\s*<div class="statusrow">(.*?)</div>\s*\n', page, re.S)
check(bool(m), "runbar 与 statusrow 两排结构存在")
if m:
    bar, row = m.group(1), m.group(2)
    check('id="btnRunNow"' in bar and 'btnHardRefresh' in bar
          and bar.index('btnRunNow') < bar.index('btnCheckStatus') < bar.index('btnHardRefresh'),
          "第三排顺序：立即运行 → 查运行状态 → 强制刷新")
    check('btnlink' not in bar, "「设置」已从顶栏搬走（顶栏只留三个按钮 + 定投）")
    check('id="runLight"' in row and 'id="runLightTxt"' in row, "第四排：状态灯 + 文案")
    check('runLight' not in bar, "灯已从第三排移走")
# 「设置」与「返回看板」同在页底 .acts 一排，顺序：返回看板 → 设置（2026-10-29）
_acts = re.search(r'<div class="acts">(.*?)</div>', page, re.S)
check(bool(_acts) and 'id="btnReturnBoard"' in _acts[1] and '>设置</a>' in _acts[1]
      and './edit.html' in _acts[1]
      and _acts[1].index('btnReturnBoard') < _acts[1].index('./edit.html'),
      "页底一排：「返回看板」与「设置」同排且「设置」指向 edit.html")
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
              rules.count('href="#' + ident + '"') == 0,
              label + '：预警区不重复阈值表且不再跳转到' + ident)

print()
if fails:
    print(f"{len(fails)} 项失败")
    sys.exit(1)
print("全部通过")
