"""首页布局改版 测试（原「日内振幅规则」部分已随振幅整体删除而移除，2026-11-12）"""
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
from monitor_render import strip_css_comments  # noqa: E402

# 测试一律用代码里的默认设置，不读线上 settings.json（线上值一改，测试就会无故失败）
monitor.S.clear()
monitor.S.update(monitor.DEFAULT_SETTINGS)

fails = []


def check(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


# --- 验证实际渲染：静态资源抽离后，不再通过 monitor 源码猜测页面 ---
with patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    page = monitor.render({}, [], 0)
m = re.search(r'<div class="runbar">(.*?)</div>\s*<div class="statusrow">(.*?)</div>\s*\n', page, re.S)
check(bool(m), "runbar 与 statusrow 两排结构存在")
if m:
    bar, row = m.group(1), m.group(2)
    check('id="btnRunNow"' in bar and 'btnHardRefresh' in bar
          and bar.index('btnRunNow') < bar.index('btnCheckStatus') < bar.index('btnHardRefresh'),
          "第三排顺序：立即运行 → 查运行状态 → 强制刷新")
    # 2026-11-02：「编辑」从仓位卡标题行搬到顶栏，紧跟「刷新」右边；脚本接管前必须是禁用的
    _edit_btn = re.search(r'<button[^>]*id="costEditBtn"[^>]*>', bar)
    check(bool(_edit_btn) and 'disabled' in _edit_btn.group(0)
          and bar.index('btnHardRefresh') < bar.index('costEditBtn')
          and 'cost-edit-btn' in _edit_btn.group(0),
          "「编辑」在顶栏「刷新」右边，保留原按钮类且绑定前禁用")
    check('btnlink' not in bar, "「设置」已从顶栏搬走（顶栏只留四个按钮 + 定投）")
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
      and strip_css_comments(page_assets.PAGE_CSS.removesuffix(page_assets.RULES_CSS)) in styles
      and styles.count(strip_css_comments(page_assets.RULES_CSS)) == 1, "statusrow 共用样式完整嵌入且规则样式只一份")
check('改自选清单' not in page, "旧的「改自选清单」已删除")
# 2026-11-02：市场风险栏的「绿灯 综合：平稳」靠右排，与其它栏目右侧的「20日对.SPX …」同一条右端线
check(strip_css_comments('.macro-card>summary .group-stats{margin-left:auto') in strip_css_comments(styles)
      and 'justify-content:flex-end' in styles,
      "市场风险标题行读数靠右，与其它栏目右侧读数对齐")
# PC 顶栏整体往中间收「一些」（不是居中）：左右各内缩 --head-inset，改一个数即可调幅度
check('--head-inset:64px' in styles and 'padding-left:var(--head-inset)' in styles,
      "PC 顶栏内缩变量与内缩规则存在")
# 脚本内容 hash 代替手工日期版本号；独立计算期望值，避免只验证同一个 helper 自洽。
content = (Path(ROOT) / 'run-status.js').read_bytes().replace(b'\r\n', b'\n')
expected_url = './run-status.js?v=' + hashlib.sha256(content).hexdigest()[:16]
# 2026-10-31 起首页这个外链带 async（不阻塞解析、也不推迟 DOMContentLoaded）；属性一并校验。
check(page_assets.asset_url(ROOT, 'run-status.js') == expected_url
      and re.findall(r'<script src="([^"]*run-status\.js[^"]*)"([^>]*)></script>', page) == [(expected_url, ' async')],
      "运行状态脚本唯一引用携带当前内容 hash 且首页异步加载")
check('amp_yellow' not in monitor.DEFAULT_SETTINGS and 'amp_red' not in monitor.DEFAULT_SETTINGS
      and ('amp', '振幅') not in monitor.SIGNAL_KINDS,
      "振幅设置项与统计类别已整体删除")
check(page_assets.rules_template(monitor.S, monitor._rule_constants()) in page,
      "首页实际使用共用规则模板")
with patch.dict(monitor.S, {'chg_yellow': 3.0, 'chg_red': 9.0}), \
        patch.object(monitor, '_cost_panel_assets', return_value=('', '')):
    changed_page = monitor.render({}, [], 0)
    changed_rules = page_assets.rules_template(monitor.S, monitor._rule_constants())
check(changed_rules in changed_page,
      "覆盖涨跌幅阈值后，实际首页仍完整嵌入最新双模板")
# 独立固定三档数值，精确检查两表的标题、列序与全部行，避免生成器自证。
for label, rendered, expected_chg in (
        ('默认', page, [['低 ×0.6', '≥1.2%', '≥2.4%'],
                        ['中 ×1', '≥2%', '≥4%'], ['高 ×1.4', '≥2.8%', '≥5.6%']]),
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
    for kind, expected_rows in (('chg', expected_chg),):
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
