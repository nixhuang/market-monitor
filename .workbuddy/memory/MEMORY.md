# AI市场监测

## 真源与边界
- 项目 C:/Users/nixhuang/WorkBuddy/AI市场监测；正式站 https://nixhuang.github.io/market-monitor/ 不迁移。index.html/status.json 是产物禁手工同步；CSS/JS/规则模板真源 page_assets.py，HTML 由 monitor_render 生成；cost_panel.css/js 独立内联。
- 禁擅自加入口/改布局/对称化规则/注入 DEMO 交互；DEMO 只换合成数据再 build_snapshot→render。不写云端仓位。
- **浏览器基线（2026-10-30 用户确认，旧约定「兼顾微信 WKWebView」作废）：只按 Chrome 能力放开写**——可用 overflow-anchor、::details-content、interpolate-size、transition-behavior:allow-discrete、平滑滚动、Web Animations 等。注意 overflow-anchor 自动锚点在批量折叠时会挑错锚点元素，**视口补偿仍以「getBoundingClientRect 前后差 + scrollBy」手写为准**（现成范式见 page_assets.py:1290-1299 与 1539-1563：读 topBefore → 改 DOM → 读新 top → scrollBy 补差）。
- push 源码触发 daily 抓数发布；**本地改动 ≠ 上线授权**，每次新改都要新授权。发布用本机 Git manager 凭据（禁输出/落盘）。改 edit.html/run-status.js 后须 `--sync-edit` 再 `--check`。

## CSS 编写约定（2026-11-01 立，A+B 整理的产物，以后照此写）
1. **颜色只认 :root 色板**：`--head-blue #bfd5ff`（收起态标题）、`--head-blue-on #8fb8f0`（展开态/焦点蓝）、
   `--lamp-red #ff8580`、`--lamp-yellow #f0c674`、`--frame-green #2c8f3e`（持仓/仓位外框）、
   `--signal-border #6686c6` + `--frame-w 1.5px`、`--purple #b084ff`。新颜色先在 :root 定义、再引用；
   老值统一带 fallback（`var(--x,#原值)`），变量缺失时自动回落，不会出现"没颜色"。
2. **一组共用参数 = 一个变量**：列宽 `--cost-cols` / `--cost-mini-cols`、列距 `--cost-gap`、
   栏目条高 `--cost-head-h`。**窄屏只覆盖变量值，禁止在 @media 里重抄整条 grid-template-columns**
   （原来四档窄屏各抄一遍，改一处漏三处）。
3. **改样式必须改现有规则，不许追加同选择器覆盖块**（19 组重复已清理；真要覆盖必须在注释里写原因）。
4. **列宽/字号要有依据**：沿用 10-30 的做法，注释里写明"最长内容墨宽 vs 列宽"，并用
   `Temp/_verify_pos.py`（320/360/390/430/601/1440 六档）验证零溢出、零横向滚动。
5. **死代码零容忍**：`mm_test_rules.py` 的 retired 选择器测试盯着；`Temp/_css_audit.py` 可随时复查
   （重复选择器 / 疑似死代码 / 注释占比）。
6. 产物在构建期剥 CSS 注释（`monitor_render._strip_css_comments`），**源码注释照旧保留**——
   注释记的是"这个值为什么这么定"，删了以后改版式会分不清哪些是有意设计。

## 当前显示口径（最新，覆盖旧约定）
- 站名 **NIX-Matrix**（2026-10-30 由 MatrixPulse 改名，改名要动 4 处真源：monitor_render.py 的 `<title>` 与
  `<h1 class="brand">`、manifest.webmanifest 的 name/short_name、edit.html 的 `<title>`，
  + tests/mm_test_render.py 断言 + Temp/_mobile_text_demo.py 的两条自检与 ③e2 站名投影）；状态灯只写「成功 ·YYYY-MM-DD HH:MM（北京时间）」。手机仓位标题行总资产·盈亏 13px。
- 52周条：三角只收底边 8.4px、高度恒 9px、内层深紫恒 7px；rail 33px；编辑按钮恒 7px 圆角（不得全圆）。
- 灯与色：可见灰灯→白灯；描述按条件自身等级红 #ff8580 / 黄 #f0c674 / 白 var(--text)，仅文字灰 #8b93a1；ACK 白删除线。紫 --purple=#b084ff（10/28 改，变量只 1 处）、紫红 #dc62b8、达到实心底 #8957e5。
- **统计名单描述性数值跟该条条件的等级走**（红名单红字/黄名单黄字/白名单白字），靠 `.signal-members-panel.tone-{red|yellow|gray} .signal-member-metric`；▲▼ 三角仍涨跌色 ▲绿 var(--up)/▼红 var(--down)，不跟条件等级。
- 仓位面板（10/28）：标题「**仓位**」（原「仓位管理」）；表格线加明显（竖线 .20、手机档 .18、列头横线 #4a5772、框内分色 .45、新增行间 .12）；行首灯条改统计同款 **3×12px 竖条**（绝对定位在 ul 左内边距，不占文档流，列宽仍对齐），不再用圆点 marker；「无异动 N 只」不写「· 点击查看」；紫灯/财报胶囊 --chip-w **74px**（桌面 72→74、手机 62→74）。
- 外框化（10/28）：:root 唯一源 **--signal-border #6686c6 + --frame-w 1.5px**；统计面板与展开分组共用外框、左侧蓝条已删、内部底色全去；仓位外框 **#2c8f3e 1.5px**、标头无底色；长持仓 .zone-card[open] 单独 1px，框左右外扩 2px + 区内补 2px（灯条不动，框落在持仓框与灯条正中），区内 ▲ 收起条 `.zone-card .group-bottom{margin:0 -2px 0}`；折叠回默认灰框。统计数字胶囊写死 width:44px、右侧三角 16px/占位 14px、gap 6px。**手机 ≤600px 整页左右各留 4px**。手机分组框收起高 **53**（原 62，-15%）／长持仓子框 **36**：
**数字灯圈 .stat-badge＝17px 圈；数字不加粗（font-weight:400）且与同行「20日对.SPX」同号**：基础 **14px**、手机 **14.5px**（10/30 晚先收回 17px/14，10/31 改同号同重；原 20px/15px/700 手机上占比过大、窄屏还会撑高标题行）。一位数 17×17、两位数 24×17。
**分组栏收起高度全档统一**（10/31 定稿）：桌面 **50**（summary 锁 44px）；**手机 ≤600 全 57**（summary min-height **51**）——基准是「加高其它栏目对齐仓位那档」，不是把仓位降下来（10/30 一度降到 53，用户看到的是旧缓存的 57，故 10/31 反过来全体加高到 57）。做法＝手机档 group-stats gap 8→6，再新增 `@media (max-width:340px)`（summary gap 4 / padding-right 6 / stats gap 4 / 灯圈 padding 2 / .monitor-note 12px；**此 340 档已于 11-01 随"只适配三台设备"删除**）把最长的标题压回单行——360 的「工航防建机」原 55、320 的「房地产」原 62。
**持仓板块 #group_positions 底色已去**（10/31：原 2% 绿叠层 → `background:var(--card)`）；长持仓 #group_positions_long **底色也全去**（`background:none`，连 .zone-card 自带 2.2% 白提亮一起清，否决了中间方案 .07），层级改由**外框**表达：**绿框 #2c8f3e 1px，与仓位外框 --cost-accent 同色，折起／展开两态都绿**（原来折起灰 --line 在深色卡片上等于看不见、展开蓝 signal-border，两态不一致；用 id 选择器压过 `.zone-card[open]`，一条写死）。长持仓折叠高度：桌面 summary 36（卡 38）／手机 min-height 38（卡 40）。个股下面「数 N · ±x% · ±$x · 市值 · 仓位」那一排（cost_panel.js 的 `r.info`）**已删除**、节点清空并 `display:none`，只留成本价那一排 `.cost-inline`。
实现写在 PAGE_CSS 末尾那条 `@media (max-width:600px)`（「手机文字加大」块）**最后**，规则是
`padding-top:2px`（收起底 2、展开底 6）+ summary **min-height**——
**「仓位」与所有分组同一个 47**（曾为凑百分比单独给 51，结果比别人高 4px，10/30 改回统一）、长持仓 34；
**不能用 height**（320 窄屏标题行会折两行，硬锁会裁）+ padding 上下 0 居中。同一档视口内先保齐再谈百分比。
手机标题字号 grp-name **16.5px**、group-stats/rs-spy **14.5px**（基线是 17/15，行高恒 24px）。统计表「信号/红/黄/白」表头排已删；信号列胶囊 74px 居中。
- **折叠语义（10/30 定，别再改错）**：点分组**标题** = `<details>` 自带一步收拢（现状，不用改）；点分组**底部 ▲**（.collapse-group）= **只收本组**，收起后以「本组之后的第一个元素」为锚点、`getBoundingClientRect().top` 前后差 + `window.scrollBy` 补回，**下方内容原地不动、按钮以上部分向下折进按钮那一行**。**禁止用 `scrollIntoView`**（会把整页拉到分组标题＝用户口中的"页面整体上移"）。实现见 page_assets.py 的 collapse-group 分支，测试见 mm_test_rules.py 的 `test_quiet_list_reset_and_regular_collapse_still_execute`（vm 沙箱需 mock `nextElementSibling` + `window.scrollBy`）。
- **桌面分组标题两态不许跳（10/30 修）**：`@media(min-width:601px)` 的压矮规则必须**两态同一套顶部链路**——
  `.group-card,.macro-card,#costManage.cost-manage{padding-top:2px}`（去掉 `:not([open])`）+ `summary.grp`
  两态都 `box-sizing:border-box;height:44px;padding-top:0;padding-bottom:0`；`.zone-card>summary` 两态都锁 34px。
  底部保持两态不同（收起 2px 保压矮、展开 6px 防贴底）。只锁高不统一 padding 仍会跳（历史上就是这么漏的）。
- 三块说明（预警规则／名词解释和抓取／综合判断与宏观细则）只在设置页：首页只留 rulesTpl/glossaryTpl/notesTpl 三个 inert 模板；「自动计划：美东…」移到返回看板下方。
- 均线同指标多事实用「，」；财报任何位置不加粗；窗口前1/当/后1 红、前2–7 黄、8–15 仅名单。
- **断点只剩两档（2026-11-01 用户定，别再加细分）**：手机档 `@media(max-width:600px)`，设计基准
  **iPhone 18 Pro Max 竖屏 440px**；桌面档 `@media(min-width:601px)`，iPad mini（竖 744 / 横 1133）与
  PC（1440/1920）**共用同一套**。iPhone 横屏 956 落桌面档，不做特殊处理。
  比 440 更窄的屏（320/340/360/365/390/400/430/520/560）**一律不再支持**，对应 @media 块已整段删除。
  守门测试：mm_test_rules.py `test_only_two_breakpoints_phone_600_and_desktop_601` + mm_test_cost.js
  「手机档只允许 ≤600 一个断点」。验收视口固定 440/956/744/1133/1440/1920（Temp/_verify_pos.py、_fold_height.py）。

## 规则要点
- 量比：收盘 sqrt(exp(m+k*s)*F)，黄 k2/F1.5、红 k3.6/F3；盘中静默 30 分钟并按已开盘分钟放宽，显示原始折算值。
- 技术布林/RSI/组合互斥，上下不对称是有意设计禁止拉平；RSI6/12/24 超买≥70/超卖≤35；快照只认 level=gray（灰条数细则见 10/26 日志）。
- 目标空间≥40% 紫、≥90% 紫红；达到仅持仓/重点穿越或 ±3% 实心紫；价位 ±3% 用 Decimal/BigInt 原始价。

## 数据与仓位
- 只统计股票/ETF 盈亏（不对齐嘉信）；现金含 SGOV、固收不计盈亏与分母，仍计总资产。全链路未复权；收盘 Nasdaq→Yahoo→stooq，盘中 Yahoo 优先；调度仅 Cloudflare trigger→ensure-morning；指数基仅 IBIT。
- **盘前/盘后/夜盘行情一律不抓（10/29 核查）**：闸门是 `market_sessions.active_session()`，只认 XNYS 常规时段；不在时段内 → `closed_only=True` → 走收盘源，取最近已完成交易日的收盘日线。盘中价要过 `validated_quote_time` 三关（`isRealTime=true`、成交时间落在当日常规时段、`0≤延迟≤5min`），成交量明确不含盘前量。**盘中运行本来就是正常功能**（盘中价过三关就出盘中快照，页面标「盘中快照，北京时间」），不是异常；
只是**自动调度固定在美东 20:15/20:45，所以自动跑出来恒是收盘口径**。页面上的「盘前/盘后」是**财报发布时段**，不是行情口径。
- 云端同步：打开/刷新读 data 分支 position-costs.json 后完整覆盖本机，dirty 不得阻挡；只有明确保存/导入授权才上传；ACK 用远端基底；更新时间统一毫秒 ISO。

## 测试、发布与图标
- **必须用 ./.venv/Scripts/python.exe 跑 tests/run_all.py**（默认 python 缺 requests 会全红）；16 套 + CI 3.11/3.13；node mm_test_rs.js、mm_test_cost.js。
- **口径由触发方式决定（10/30 澄清，别当成 bug）**：Cloudflare 自动调度固定在美东 20:15/20:45 → 恒收盘口径；
  **push / 手动派发按当时的美东时段跑 —— 盘中 push 就出盘中快照（量比带「截至 HH:MM ET」），这是预期行为**。
  `_gh_dispatch.py` 只认 request_id、不能指定 target 日期。想让线上回到收盘口径就等下一次自动调度覆盖。
  页面口径看 status.json 的 mode 与量比文案即可分辨，无需干预。
- 流程：提交→push main→daily→产物→Pages；验收核线上 CSS/JS/文案关键片段 + 三视口无溢出。最近发布 10/31 00:45：源码 **9c93288** / 产物 **942cad7**（手机分组栏全体 57 与仓位对齐、长持仓去底色改绿框并加高、灯圈同号不加粗、持仓板块去底色、删个股下方那排；daily 41092765027 含 browser 全绿；核验 **18/18** + 线上浏览器实测手机高度 [40,57]、▲ 折叠下方 0px）。上一版 10/30 23:30：源码 1c024b4 / 产物 172ba22（手机仓位栏高与其它统一 53，去掉单独 51px）。上一版 10/30 22:49：源码 **7179500** / 产物 **69bcb0d**（桌面标题两态不跳 + ▲ 向下折 + 手机分组框压 15% 与标题字 16.5/14.5；daily 41074586157 含 browser 全绿；核验 **17/17**，另线上浏览器实测全过；target=10/30 盘中，见上一条）。上一版 10/30 20:33：源码 46d9055（手机仓位列宽防溢出 + 金额按价位降档 trim/trim4/qtyInt + 设置页折叠 46px/15px/600 统一与间距收窄）/ 产物 fa6da47（daily 41055684403 自动触发，含 browser 套件全绿，目标 10/29 收盘，97/97 fresh；核验 14/14 + 改动片段 14/14）。**Pages CDN 有约 20~40s 旧缓存，核验前先比对 index.html 字节数**（旧 576400 → 新 578390），否则误判未生效。上一版 10/30 15:46：源码 fecf7d0 + 67e1bb3 / 产物 32b2f91（daily 41021912051 手动派发，目标 10/29 收盘口径，97/97 fresh；线上核验脚本 Temp/_live_verify_nix.py 14/14）。上一版最近发布 10/28 18:21：源码 eb0a905 / 产物 e5879df（daily40671101742、Pages40671443921、build 40671101742-1、目标 10/27、97/97 fresh 无缺失过期）。上一版 10/28 17:09：源码 79955e9 / 产物 4784d9e。gh 不在 PATH，用 git credential fill 取 token 调 API：Temp/_gh_runs.py（查运行）、_gh_watch.py（等待）、_gh_log.py（拉日志）、_gh_dispatch.py（派发 daily）、_gh_rerun.py（重跑失败项）。**只改 tests/ 不触发 daily，须 workflow_dispatch**。
- 自检：run_all 16/16、page_assets --check、图标 PNG depth=8 type=2（RGB 真彩）且线上 sha256 一致。icons/ 六文件（32/180/192/512/maskable-192/512），iOS 主屏图标必须真彩；未引用的 apple-touch-icon.png 是不提交的残留。
- **DEMO/预览投影致命坑（10/29）**：本地 index.html 是发布产物，内联 `<style>` = **PAGE_CSS 前半 + cost_panel.css + PAGE_CSS 后半**（cost_panel 拼在中间）。只替换 cost_panel.css 的投影脚本（Temp/_plain_demo.py 那一系）**投影不到 page_assets.py 的改动**，量取/截图全是旧 CSS，结论无效。正确做法见 Temp/_cost_preview.py 的 ⓪：git show 旧 page_assets.py → 动态 import 取旧 PAGE_CSS → 用旧 cost_panel.css 定位切点 → 锚点在新 PAGE_CSS 找 split → 重建三段。
- DEMO：本地未发布改动用线上旧缓存 Temp/_live_index.html + 投影脚本 Temp/_frame_edge_demo.py（任何新改动都要在里面投影，否则 DEMO 看起来"没改"）；**改动已发布后直接以本地 index.html（产物）作基底**，见 Temp/_plain_demo.py（纯看板-无仓位信息 DEMO：不插提示、整段删除内联 cost_panel.js 使仓位面板与云端读取全部消失、内联 run-status.js 供离线打开）。

## CI 浏览器门槛坑（10/28，发布时两次阻断）
- hosted runner 可能没有 chromium/chrome 命令：tests.yml 先按命令+绝对路径探测（/usr/bin/chromium、/usr/bin/google-chrome、/opt/google/chrome/chrome、/snap/bin/chromium），未命中用 browser-actions/setup-chrome@v1 兜底，仍取不到才报错；质量门槛不能跳过。
- **改 --purple 等颜色必须同步 tests/mm_test_render.py 里写死的 rgb() 断言**（紫徽章边框、space 胶囊字色/虚线边框、low/bad 稀疏虚线首项），否则本地 16/16 全绿但 CI browser 失败（10230 项里 9 项＝三视口×三项）。
- 本机已无 playwright 独立 Chromium；本地不跑 --chrome 套件，浏览器门槛交给 CI 合成实例。
- 10/28 已发布整包：三块说明搬设置页、自动计划下移、外框化（贴边 4px/1.5px/长持仓 1px/外扩与收起条/三角加大）、名单数值跟灯、无异动文案、仓位改名与表格线及灯条、胶囊 74px、紫 #b084ff；线上核验 28 项全过。
- **改灯条宽度必须同步 tests/mm_test_render.py:1784 的 stripes 期望表** `[['red',W],['yellow',W],['green',W]]`（10/30 红灯条 6→4px 与黄等宽，断言未改 → CI browser 10241 项中 3 项失败＝三视口×1）；绿条恒 2px 是有意保留。同类还有 mm_test_groups.py:3133 的 4/4/2 注释。
- 10/28 18:21 追加发布：数字胶囊**个位居中＋三角右移**（三角仍 16px，改绝对定位 right:2px 垂直居中；胶囊 padding 0 17px 0 4px + flex-end + nowrap，个位右缘固定 x=26＝中线 22+半个字宽 4，十位向左长，恒 44×38 单行）；设置页「← 回看板」放大（class=back-board，12→15px + 9/16px 内边距 + 按钮同款描边圆角，实测 96×45.5，href 与文案不变）。edit.html 是**手工源**不是生成物，但改完仍需 page_assets --check（sync_edit_html 校验 schema/规则 CSS 生成块）。
