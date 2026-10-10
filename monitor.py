#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场监控脚本
- 抓 FRED 宏观指标（免费，无需 API Key）
- 抓 Yahoo 个股/ETF 行情（免费，无需 API Key）
- 按规则筛选出「今晚重点关注」
- 生成 index.html 供 GitHub Pages 展示

数据来源：
  FRED  https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
  Yahoo https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>
"""

import argparse
import hashlib
import html as html_lib
import json
import math
from types import SimpleNamespace
import os
import re
import subprocess
import sys
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

# 北京时间与运行口径
BASE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
EVENT = os.environ.get("MM_EVENT", "local")
TARGET_DATE = os.environ.get("MM_TARGET_TRADE_DATE", "").strip()
CLOSED_ONLY = os.environ.get("MM_CLOSED_ONLY", "0") == "1"
QUOTE_DEADLINE = None  # 仅主任务取数阶段启用；为页面生成与推送预留时间
QUOTE_BUDGET_SECONDS = 8 * 60   # 全部行情抓取的总预算
QUOTE_SYMBOL_SECONDS = 40       # 单只标的最长耗时：超时就换下一只，不拖垮整批
QUOTE_WORKERS = 6               # 有界并发：既比串行快，又不至于把行情源打到限流
QUOTE_RETRY_PASSES = 1          # 第一轮失败的标的，预算够时再补抓一轮
_QUOTE_TLS = threading.local()


def quote_timeout(seconds):
    deadlines = [d for d in (QUOTE_DEADLINE, getattr(_QUOTE_TLS, "deadline", None)) if d is not None]
    if not deadlines:
        return seconds
    remaining = min(deadlines) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("本轮行情抓取时间预算已用完")
    return max(0.1, min(seconds, remaining))


class SourceGate:
    """行情源熔断：连续被限流/失败就暂停一小段时间，直接走备用源，避免每只都白等重试。"""

    def __init__(self, threshold=3, cooldown=90):
        self.threshold, self.cooldown = threshold, cooldown
        self.fails, self.blocked_until = 0, 0.0
        self.enabled = False  # 只在批量个股抓取阶段启用；宏观指标没有备用源，不能被熔断误伤
        self.lock = threading.Lock()

    def reset(self, enabled=False):
        with self.lock:
            self.fails, self.blocked_until, self.enabled = 0, 0.0, enabled

    def allow(self):
        return not self.enabled or time.monotonic() >= self.blocked_until

    def ok(self):
        with self.lock:
            self.fails = 0

    def fail(self):
        if not self.enabled:
            return False
        with self.lock:
            self.fails += 1
            if self.fails >= self.threshold:
                self.blocked_until = time.monotonic() + self.cooldown
                self.fails = 0
                return True
        return False


YAHOO_GATE = SourceGate(threshold=3, cooldown=90)
STOOQ_GATE = SourceGate(threshold=3, cooldown=3600)


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# FRED 官方 API key（免费申请：https://fredaccount.stlouisfed.org/apikeys）
# 配成仓库 Secret FRED_API_KEY 即可；不配也能跑，只是 Actions 上拿不到垃圾债利差和收益率曲线。
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# ---------------------------------------------------------------- 配置

with open(os.path.join(BASE, "groups.json"), encoding="utf-8") as f:
    GROUPS = json.load(f)
GROUP_LABELS = {g["item_group"]: g["label"] for g in GROUPS}


def group_monitoring(cfg):
    saved = cfg.get("group_monitoring") or {}
    if not isinstance(saved, dict):
        saved = {}
    return {g["key"]: True if g["key"] in ("positions", "focus")
            else saved[g["key"]] if isinstance(saved.get(g["key"]), bool)
            else False for g in GROUPS}


def instrument_identity(symbol):
    normalized = normalize_symbol(symbol)
    aliases = {".SPX": "^GSPC", "^SPX": "^GSPC", ".VIX": "^VIX",
               ".NDX": "^NDX", ".VXN": "^VXN", ".SOX": "^SOX",
               "BD#US10Y": "^TNX", ".TNX": "^TNX", "ESMAIN": "ES=F", "CLMAIN": "CL=F"}
    return aliases.get(normalized, normalized).replace('/', '-')


RISK_IDENTITIES = {"^GSPC", "^VIX", "^TNX"}   # 已在「市场风险参考」里显示的品种：清单里再出现就跳过（标普500、VIX、10Y美债）


def grouped_universe(cfg):
    monitoring = group_monitoring(cfg)
    seen, universe, counts, duplicates = set(RISK_IDENTITIES), [], {}, 0
    for group in GROUPS:
        counts[group["key"]] = 0
        for symbol, settings in (cfg.get(group["key"]) or {}).items():
            normalized = instrument_identity(symbol)
            if not monitoring[group["key"]] and normalized != group.get("sector_etf"):
                continue
            if normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            universe.append((symbol, settings, group["item_group"]))
            counts[group["key"]] += 1
    return universe, counts, duplicates


FRED_SERIES = {
    "hy_oas":   {"id": "BAMLH0A0HYM2", "name": "垃圾债利差", "unit": "bp",  "scale": 100},
    "vix":      {"id": "VIXCLS",       "name": "VIX",        "unit": "",    "scale": 1},
    "sp500":    {"id": "SP500",        "name": "标普500",     "unit": "",    "scale": 1},
    "ust10":    {"id": "DGS10",        "name": "10Y美债",     "unit": "%",   "scale": 1},
    "curve":    {"id": "T10Y2Y",       "name": "收益率曲线",   "unit": "",    "scale": 1},
    "dxy":      {"id": "DTWEXBGS",     "name": "美元指数",     "unit": "",    "scale": 1},
    "nfci":     {"id": "NFCI",         "name": "金融压力",     "unit": "",    "scale": 1},
}

# ---------------------------------------------------------------- 可调阈值
# 这些默认值会被同目录下的 settings.json 覆盖；settings.json 缺失或字段写错就退回默认值。
# 你可以在 edit.html 里直接改，改完保存会自动重跑，不用碰代码。
DEFAULT_SETTINGS = {
    # 布林带
    "boll_n": 20,           # 均线周期
    "boll_k": 2.0,          # 标准差倍数
    "boll_near_pct": 0.5,   # 距上/下轨不足 x% 就算命中（盘中不用等真的穿过去）

    # 个股
    "chg_red": 4.0,         # 单日涨跌 >= x% → 红
    "chg_yellow": 2.0,      # 单日涨跌 >= x% 且 < chg_red → 黄
    "rsi_high": 70,         # RSI 6/12/24 同侧 >= x：两条黄、三条红
    "rsi_low": 30,          # RSI 6/12/24 同侧 <= x：两条黄、三条红
    "near_52w_low_pct": 1.0,  # 距 52 周低点不足 x% → 红
    "trigger_gap_pct": 5.0,  # 距加仓价不足 x% → 红（已跌破则无视这条直接红）
    "reduce_gap_pct": 3.0,   # 持仓的减仓价：现价在减仓价 ±x%（按减仓价算）内 → 红，不分涨跌方向
    "target_space_pct": 50.0,  # 目标价（共识价 / 手填）高出现价超过 x% → 紫色（竖条 + 实心标签 + 警示栏，不改红黄绿）
    "target_gap_pct": 3.0,     # 现价在目标价 ±x% 内、或当日跨越目标价 → 紫色
    "vol_ratio": 1.5,       # 量比 >= x 倍 → 黄
    "quiet_chg": 2.0,       # quiet 标的（货币基金等）单日涨跌 >= x% → 黄
    "amp_yellow": 5.0,      # 日内振幅（最高-最低）/昨收 >= x% → 黄
    "amp_red": 8.0,         # 日内振幅 >= x% → 红（上下插针、剧烈震荡）
    "ma_short": 50,
    "ma_long": 200,

    # 宏观
    "hy_green": 350,        # 垃圾债利差 bp：< 350 绿 / 350~400 黄 / >= 400 红
    "hy_red": 400,
    "vix_green": 20,        # VIX：< 20 绿 / 20~ 黄 / >= 40 红
    "vix_yellow": 30,
    "vix_red": 40,

    # 行情源：1=用 Nasdaq 实时报价覆盖最新价（0 延迟），0=只用日线收盘价
    "use_realtime": 1,

    # 定投提醒（全局一条）：从 dca_start 那个交易日算起，每 dca_every 个交易日提醒一次
    # dca_every=0 或 dca_start 为空 = 不提醒
    "dca_every": 0,
    "dca_start": "",
}


from zoneinfo import ZoneInfo
US_TZ = ZoneInfo("America/New_York")


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def session_date(on_or_before):
    """交易所日历：节假日/周末回退到最近有效交易日，不能只按周一至周五猜。"""
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS")
    return cal.date_to_session(on_or_before.isoformat(), direction="previous").date().isoformat()


def plan_run(event, when):
    """schedule锚定最近一次美东20:00（23/5交易休盘窗口20:00-21:00的起点）；手动/保存重跑保留盘中行为。"""
    ny = when.astimezone(US_TZ)
    day = ny.date()
    if event == "schedule":
        anchor = ny.replace(hour=20, minute=0, second=0, microsecond=0)
        if ny < anchor:
            day -= timedelta(days=1)
        return {"closed_only": True, "target": session_date(day)}
    # 当前接口口径仍是常规时段日线：09:30–16:00可用实时覆盖。
    if ny.weekday() < 5 and (ny.hour, ny.minute) >= (9, 30):
        day_target = session_date(day)
    else:
        day_target = session_date(day - timedelta(days=1))
    in_regular_session = day_target == day.isoformat() and (9, 30) <= (ny.hour, ny.minute) < (16, 0)
    return {"closed_only": not in_regular_session, "target": day_target}


def prepare_run():
    """在workflow最早步骤锁定目标，后续排队/抓取跨日也不挪动目标。"""
    when = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if run_id and repo:
        # 用运行创建时间，而不是job开跑时间；极端排队跨午夜仍能守住目标。
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}",
                         headers={"Authorization": "Bearer " + os.environ.get("GH_TOKEN", ""),
                                  "Accept": "application/vnd.github+json"}, timeout=20)
        r.raise_for_status()
        when = datetime.fromisoformat(r.json()["created_at"].replace("Z", "+00:00"))
    plan = plan_run(EVENT, when)
    print("MM_TARGET_TRADE_DATE=" + plan["target"])
    print("MM_CLOSED_ONLY=" + ("1" if plan["closed_only"] else "0"))
    print("MM_STARTED_AT=" + when.isoformat())


def ensure_schedule_run():
    """保险：GitHub 定时任务可能被延迟或丢弃。若今晚（美东20:00之后）的收盘结果还没生成，
    就补派发一次与定时任务等价的运行；已有结果或已有任务在排队/运行则不重复派发。"""
    now_ny = datetime.now(timezone.utc).astimezone(US_TZ)
    if not 20 * 60 <= now_ny.hour * 60 + now_ny.minute < 21 * 60:
        # 夏冬令时两组时点每天都会触发；不在休盘窗口内（错季节的时点或被严重拖延）就什么都不做，避免抓到新一轮盘中价
        print(f"美东 {now_ny:%H:%M} 不在 20:00-21:00 休盘窗口内，不补跑")
        return 0
    plan = plan_run("schedule", datetime.now(timezone.utc))
    try:
        with open(os.path.join(BASE, "status.json"), encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    if snap.get("target_trade_date") == plan["target"] and snap.get("mode") == "closed" \
            and (snap.get("summary") or {}).get("total"):
        print(f"目标交易日 {plan['target']} 的收盘结果已存在，无需补跑")
        return 0
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}/actions/workflows/daily.yml"
    r = requests.get(base + "/runs?branch=main&per_page=10", headers=headers, timeout=20)
    r.raise_for_status()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    for run in r.json().get("workflow_runs", []):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if run.get("status") in ("queued", "in_progress", "waiting", "pending") and created >= cutoff:
            print(f"已有运行 {run['id']} 在排队或执行（{run['status']}），不重复派发")
            return 0
    d = requests.post(base + "/dispatches", headers=headers, timeout=20,
                      json={"ref": "main", "inputs": {"source": "schedule-fallback"}})
    d.raise_for_status()
    print(f"今晚收盘结果缺失，已补派发运行（目标交易日 {plan['target']}）")
    return 0


def trim_history(data, cutoff):
    """按日期同步裁剪所有OHLCV，禁止把cutoff之后的新bar混入自动日报。"""
    if not data:
        return None
    dates, closes = data.get("dates") or [], data.get("closes") or []
    if len(dates) != len(closes):
        return None
    keep = [i for i, d in enumerate(dates) if d and d <= cutoff]
    if not keep:
        return None
    out = dict(data)
    for key in ("dates", "closes", "volumes", "highs", "lows"):
        seq = data.get(key) or []
        if seq and len(seq) != len(dates):
            return None
        out[key] = [seq[i] for i in keep] if seq else []
    if len(out["closes"]) < 30:
        return None
    out.update(price=out["closes"][-1],
               prev_close=out["closes"][-2], realtime=False, rt_ts="")
    return out


SETTING_LIMITS = {
    "boll_n": (2, 250), "boll_k": (0.1, 10), "boll_near_pct": (0, 100),
    "ma_short": (2, 250), "ma_long": (2, 250),
    "chg_yellow": (0, 100), "chg_red": (0, 100),
    "rsi_low": (0, 100), "rsi_high": (0, 100),
    "near_52w_low_pct": (0, 100), "trigger_gap_pct": (0, 100), "reduce_gap_pct": (0, 100),
    "target_space_pct": (0, 1000), "target_gap_pct": (0, 100),
    "vol_ratio": (0.1, 100), "quiet_chg": (0, 100),
    "amp_yellow": (0, 100), "amp_red": (0, 100),
    "hy_green": (0, 10000), "hy_red": (0, 10000),
    "vix_green": (0, 200), "vix_yellow": (0, 200), "vix_red": (0, 200),
    "dca_every": (0, 10000), "use_realtime": (0, 1),
}
SETTING_ORDER = (("chg_yellow", "chg_red"), ("amp_yellow", "amp_red"),
                 ("hy_green", "hy_red"),
                 ("vix_green", "vix_yellow", "vix_red"), ("ma_short", "ma_long"))


def valid_settings(s):
    for k, (lo, hi) in SETTING_LIMITS.items():
        v = s.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            return k
    if any(s[k] != int(s[k]) for k in ("boll_n", "ma_short", "ma_long", "dca_every")) or s["use_realtime"] not in (0, 1):
        return "整数设置"
    if not s["rsi_low"] < s["rsi_high"]:
        return "RSI 上下限"
    for keys in SETTING_ORDER:
        if any(a > b for a, b in zip((s[k] for k in keys), (s[k] for k in keys[1:]))):
            return " / ".join(keys)
    start = s.get("dca_start")
    if start and (not isinstance(start, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start)):
        return "dca_start"
    if start:
        try:
            datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            return "dca_start"
    return ""


def load_settings():
    """读 settings.json 覆盖默认值。文件不存在 / 写错都安全退回默认值。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(BASE, "settings.json"), encoding="utf-8") as f:
            u = json.load(f)
        if not isinstance(u, dict):
            raise ValueError("settings.json 顶层不是对象")
        for k, v in u.items():
            if k not in s:
                continue
            if k == "dca_start":
                # 起始日是 YYYY-MM-DD 字符串（或空）；写错就当没设
                if isinstance(v, str) and (v == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip())):
                    try:
                        if v.strip():
                            datetime.strptime(v.strip(), "%Y-%m-%d")
                        s[k] = v.strip()
                    except ValueError:
                        log("  ! 定投起点日无效，沿用默认值")
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue  # 类型不对就跳过，不让它污染默认值
            if k in SETTING_LIMITS and (not math.isfinite(v) or not SETTING_LIMITS[k][0] <= v <= SETTING_LIMITS[k][1]):
                continue
            if k in ("boll_n", "ma_short", "ma_long", "dca_every", "use_realtime") and v != int(v):
                continue
            s[k] = type(DEFAULT_SETTINGS[k])(v)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"  ! settings.json 读取失败，用默认值：{e}")
    if not s["rsi_low"] < s["rsi_high"]:
        log("  ! RSI 上下限冲突，仅该组退回默认值")
        s["rsi_low"], s["rsi_high"] = DEFAULT_SETTINGS["rsi_low"], DEFAULT_SETTINGS["rsi_high"]
    for keys in SETTING_ORDER:
        if any(s[a] > s[b] for a, b in zip(keys, keys[1:])):
            log(f"  ! {'/'.join(keys)} 阈值顺序冲突，仅该组退回默认值")
            for k in keys:
                s[k] = DEFAULT_SETTINGS[k]
    if valid_settings(s):
        log(f"  ! settings.json 存在无效参数（{valid_settings(s)}），使用安全默认值")
        return dict(DEFAULT_SETTINGS)
    return s


S = load_settings()

# 布林带"逼近"阈值（%）：距上轨/下轨不足这个百分比就算命中，不用等真的穿过去
# 因为盘中价格一直在动，等收盘才确认会错过时机
BOLL_NEAR_PCT = S["boll_near_pct"]

def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- FRED

# FRED 在 GitHub Actions（Azure IP）上会被挡，导致整段 Read timed out。
# 这几个指标可以用 Yahoo 的指数代码兜底（Yahoo 在 Actions 上通、在大陆不通；
# FRED 反过来在大陆通、在 Actions 不通 —— 两个源正好互补）。
FRED_YAHOO_ALIAS = {
    "vix":   "^VIX",     # VIX
    "sp500": "^GSPC",    # 标普500
    "ust10": "^TNX",     # 10Y 美债收益率（单位 %）
    "dxy":   "DX-Y.NYB", # 美元指数
}


def _rows_from_closes(closes, days=400, dates=None):
    """Yahoo 后备日线仅采用真实交易日期；绝不从位置推测日期。"""
    if not dates or len(dates) != len(closes):
        return []
    return [(d, v) for d, v in zip(dates, closes) if d and isinstance(v, (int, float))
            and math.isfinite(v) and v > 0][-days:]


def fred_api(series_id, days=400):
    """FRED 官方 API（api.stlouisfed.org）。需要 FRED_API_KEY。

    实测可达性：GitHub Actions ✅ 通（约 1.5s）；中国大陆 ❌ 不通。
    跟下面的 CSV 接口正好互补，所以两个都要留着。
    """
    if not FRED_API_KEY:
        return []
    end = NOW.date()
    start = end - timedelta(days=730)
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id={series_id}&api_key={FRED_API_KEY}"
           f"&file_type=json&observation_start={start.isoformat()}"
           f"&observation_end={end.isoformat()}")
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    obs = (r.json() or {}).get("observations") or []
    rows = []
    for o in obs:
        v = str(o.get("value", "")).strip()
        if v in (".", "", "None"):
            continue
        try:
            rows.append((o["date"], float(v)))
        except (KeyError, ValueError):
            continue
    return rows[-days:] if len(rows) >= 30 else []


def fred_csv(series_id, days=400):
    """FRED CSV 图形接口（fred.stlouisfed.org），不需要 key。

    实测可达性：中国大陆 ✅ 通；GitHub Actions ❌ 被 CDN 掐断（超时 / HTTP2 INTERNAL_ERROR）。
    """
    end = NOW.date()
    start = end - timedelta(days=730)
    urls = [
        f"https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start.isoformat()}&coed={end.isoformat()}",
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=UA, timeout=12)
            r.raise_for_status()
            rows = []
            for line in r.text.strip().split("\n")[1:]:
                parts = line.split(",")
                if len(parts) != 2:
                    continue
                d, v = parts[0].strip(), parts[1].strip()
                if v in (".", "", "nan"):
                    continue
                try:
                    rows.append((d, float(v)))
                except ValueError:
                    continue
            if len(rows) >= 30:
                return rows[-days:]
        except Exception as e:
            log(f"  ! FRED CSV {series_id} 失败: {str(e)[:70]}")
    return []


def treasury_yield_series(days=400, source_info=None):
    """10Y 收益率使用 Yahoo ^TNX 的百分比日线，不混用 FRED 或债券价格。"""
    target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()

    def validated(rows):
        result = []
        try:
            for date, value in rows:
                if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                    return []
                if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value < 25:
                    return []
                if result and date <= result[-1][0]:
                    return []
                result.append((date, value))
        except (ValueError, TypeError):
            return []
        result = [(date, value) for date, value in result if date <= target]
        return result[-days:] if len(result) >= 30 else []

    try:
        data = yahoo_history("^TNX")
        rows = validated(_rows_from_closes(data.get("closes", []), days, data.get("dates"))) if data else []
        name = data.get("quote_name", "").lower() if data else ""
        compatible = bool(data and data.get("quote_symbol") == "^TNX" and
                          isinstance(data.get('price'), (int, float)) and math.isfinite(data['price']) and
                          data.get('closes') and math.isclose(data['price'], data['closes'][-1]))
        if name:
            compatible = compatible and "10" in name and ("yield" in name or "interest rate" in name or name == "10-year bond")
        if source_info is not None and not (rows and compatible):
            source_info.update(source='Yahoo ^TNX', reason='收益率序列或标的校验未通过',
                               quote_name=data.get('quote_name', '') if data else '',
                               observed_date=(data.get('dates') or [''])[-1] if data else '',
                               observed_value=data.get('price') if data else None)
        if rows and compatible:
            if source_info is not None:
                source_info.update(source="Yahoo ^TNX", unit="%", date=rows[-1][0], lagging=rows[-1][0] < target)
            return rows
    except Exception as e:
        log(f"  ! 10Y美债 Yahoo 获取失败: {str(e)[:70]}")
    return []


def fred_series(series_id, days=400, alias=None, source_info=None):
    """抓取 FRED 序列，返回 [(date_str, value), ...]，失败返回 []

    三级降级：
      1. 官方 API（有 FRED_API_KEY 时）—— Actions 上唯一能通的路
      2. CSV 图形接口（免 key）—— 大陆本地能通，Actions 上不通
      3. Yahoo 指数别名兜底（仅 vix/sp500/ust10/dxy 有）
    """
    if series_id == "DGS10":
        return treasury_yield_series(days, source_info)
    if FRED_API_KEY:
        try:
            rows = fred_api(series_id, days)
            if rows:
                return rows
        except Exception as e:
            log(f"  ! FRED API {series_id} 失败: {str(e)[:70]}")

    rows = fred_csv(series_id, days)
    if rows:
        return rows

    if alias:
        log(f"  · FRED {series_id} 不可用，改用 Yahoo {alias}")
        data = yahoo_history(alias)
        if data and len(data["closes"]) >= 30:
            return _rows_from_closes(data["closes"], days, data.get("dates"))
    return []


def pct_from_high(series):
    """当前值相对区间最高点的回撤百分比（负数表示回撤）"""
    if not series:
        return None
    values = [v for _, v in series]
    cur, high = values[-1], max(values)
    return (cur / high - 1) * 100 if high else None


BREADTH_URL = "https://historyofmarket.com/api/sp500/breadth.json"
BREADTH_CREDIT = "https://historyofmarket.com/zh-cn/sp500/sp500-breadth/"


def market_breadth(target_date):
    """用当前成分股的均线覆盖率观察涨势广度，过期或格式异常时不亮绿灯。"""
    item = {"ok": False, "name": "上涨参与度"}
    try:
        r = requests.get(BREADTH_URL, headers=UA, timeout=10)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or body.get("_canonical") != BREADTH_URL or \
                "CC BY 4.0" not in str(body.get("_license", "")) or \
                body.get("source") != "Member daily closes, current constituents":
            raise ValueError("市场宽度来源或授权不符")
        latest = body.get("latest") or {}
        if not isinstance(latest, dict) or latest.get("date") != body.get("updated"):
            raise ValueError("市场宽度最新观测与更新日期不一致")
        day = datetime.strptime(str(latest.get("date", "")), "%Y-%m-%d").date()
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        if day > target or (target - day).days > 7:
            raise ValueError("市场宽度观测日期过旧或超出本轮日期")
        p50, p200 = float(latest["pct50"]), float(latest["pct200"])
        if not all(math.isfinite(v) and 0 <= v <= 100 for v in (p50, p200)) or \
                not 450 <= int(body.get("members", 0)) <= 550:
            raise ValueError("市场宽度数值或成员数异常")
        return {"ok": True, "name": "上涨参与度", "date": day.isoformat(),
                "value": p200, "pct50": p50, "unit": "%", "source": "History of Market"}
    except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError) as e:
        log(f"  ! 市场宽度暂不可用：{str(e)[:80]}")
    return item


MACRO_RETRY_WAIT = 20  # 风险参考的 VIX/标普/10Y 首次失败后的重试间隔（秒）
NFCI_URL = "https://www.chicagofed.org/-/media/publications/nfci/nfci-data-series-csv.csv"


def financial_conditions_series(days=400):
    """NFCI原始周度指标，不以股票/ETF价格替代。"""
    try:
        r = requests.get(NFCI_URL, headers=UA, timeout=12)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if lines[0].split(',')[:2] != ['Friday_of_Week', 'NFCI']:
            return []
        target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
        rows = []
        for line in lines[1:]:
            fields = line.split(',')
            date = datetime.strptime(fields[0], '%m/%d/%Y').date().isoformat()
            value = float(fields[1])
            if not math.isfinite(value) or rows and date <= rows[-1][0]:
                return []
            if date <= target:
                rows.append((date, value))
        return rows[-days:]
    except (requests.RequestException, ValueError, IndexError) as e:
        log(f"  ! NFCI原始数据暂不可用: {str(e)[:70]}")
        return []


SP500_RS_ROWS = []   # build_macro 取到的标普500指数日线（最近约 60 根），供板块「相对标普500」强弱使用
RS_BENCH_NAME = "标普500"
HOM_PRICE_URL = "https://historyofmarket.com/api/sp500/price.json"


def sp500_rows_fallback(target=None, keep=60):
    """标普500指数日线的第二来源：History of Market（项目已在用其市场宽度，CC BY 4.0，免 key）。

    Yahoo 的 ^GSPC 取不到时才用（例如大陆本机跑不通 Yahoo）。它比 Yahoo 晚 1~2 个交易日，
    relative_strength 按日期对齐，终点会自动退到双方共有的最新日；落后太多就对不上、页面不显示。
    格式、授权、日期顺序有任何异常一律返回空列表，不拿不确定的数据凑数。"""
    target = target or TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    try:
        r = requests.get(HOM_PRICE_URL, headers=UA, timeout=30)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, dict) or "CC BY 4.0" not in str(body.get("_license", "")) or \
                not isinstance(body.get("series"), list):
            raise ValueError("标普500日线来源或授权不符")
        rows = []
        for row in body["series"][-keep * 3:]:
            date, close = str(row["date"]), float(row["close"])
            datetime.strptime(date, "%Y-%m-%d")
            if not math.isfinite(close) or close <= 0 or (rows and date <= rows[-1][0]):
                raise ValueError("标普500日线数值或日期顺序异常")
            if date <= target:
                rows.append((date, close))
        rows = rows[-keep:]
        if not rows or (datetime.strptime(target, "%Y-%m-%d") - datetime.strptime(rows[-1][0], "%Y-%m-%d")).days > 7:
            raise ValueError("标普500日线过旧")
        return rows
    except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError) as e:
        log(f"  ! 标普500日线第二来源暂不可用：{str(e)[:80]}")
        return []


def market_index_series(alias, days=400):
    data = yahoo_history(alias)
    if not data or not valid_history(data):
        return []
    target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    return [(date, value) for date, value in _rows_from_closes(data['closes'], days, data['dates'])
            if date <= target]


def month_change(rows, days=30):
    """收益率类序列：最新值相对约一个月（自然日 days 天）前最近一个交易日的变化，单位同原序列。
    历史不够长（找不到 days 天前的记录）时返回 (None, None)。"""
    if len(rows) < 2:
        return None, None
    try:
        cutoff = (datetime.strptime(rows[-1][0], "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return None, None
    base = [v for d, v in rows if d <= cutoff]
    if not base:
        return None, None
    return base[-1], rows[-1][1] - base[-1]


def build_macro():
    log("抓取宏观指标...")
    macro = {}
    SP500_RS_ROWS.clear()
    for key, cfg in FRED_SERIES.items():
        source_info = {}
        if key == "hy_oas":
            rows = fred_series(cfg["id"])
            source_info['source'] = 'FRED'
        elif key in ('vix', 'sp500'):
            alias = FRED_YAHOO_ALIAS[key]
            rows = market_index_series(alias)
            source_info['source'] = 'Yahoo ' + alias
        elif key == "ust10":
            rows = treasury_yield_series(source_info=source_info)
        elif key == "nfci":
            rows = financial_conditions_series()
            source_info['source'] = 'NFCI原始周度数据'
            target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
            if not rows or (datetime.fromisoformat(target) - datetime.fromisoformat(rows[-1][0])).days > 10:
                backup = fred_series('NFCI')
                if backup and (not rows or backup[-1][0] > rows[-1][0]):
                    rows = backup
                    source_info['source'] = 'NFCI周度数据（FRED同步备用）'
        else:
            continue
        if not rows and key in ("vix", "sp500", "ust10"):
            # 只有 3 个请求，值得多等一会儿再试一次：这三项决定风险参考的综合灯
            log(f"  · {cfg['name']} 首次取数失败，稍后重试一次")
            time.sleep(MACRO_RETRY_WAIT)
            if key == "ust10":
                rows = treasury_yield_series(source_info=source_info)
            else:
                rows = market_index_series(FRED_YAHOO_ALIAS[key])
        if not rows:
            macro[key] = {"ok": False, "name": cfg["name"], **source_info}
            continue
        if key == "sp500":
            SP500_RS_ROWS[:] = rows[-60:]
        cur = rows[-1][1] * cfg["scale"]
        prev = rows[-2][1] * cfg["scale"] if len(rows) >= 2 else None
        week_ago = rows[-6][1] * cfg["scale"] if len(rows) >= 6 else None

        item = {
            "ok": True,
            "name": cfg["name"],
            "unit": cfg["unit"],
            "value": cur,
            "date": rows[-1][0],
            "prev": prev,
            "week_ago": week_ago,
            "delta_week": (cur - week_ago) if week_ago is not None else None,
        }
        item.update(source_info)
        if key == "sp500":
            dd = pct_from_high(rows)
            item["drawdown"] = dd
        if key == "ust10":
            item["month_ago"], item["delta_month"] = month_change(rows)
        limit = 10 if key == "nfci" else 7
        try:
            age = (datetime.strptime(TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat(), "%Y-%m-%d") -
                   datetime.strptime(item["date"], "%Y-%m-%d")).days
        except ValueError:
            age = 999
        if not 0 <= age <= limit:
            macro[key] = {"ok": False, "name": cfg["name"]}
            log(f"  ! {cfg['name']} 数据日期过期，不参与判断")
            continue
        macro[key] = item
        log(f"  {cfg['name']}: {cur:.2f} ({rows[-1][0]})")
    target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    macro["breadth"] = market_breadth(target)
    def recent(item, days):
        if not item.get("ok"):
            return False
        try:
            age = (datetime.strptime(target, "%Y-%m-%d") -
                   datetime.strptime(item["date"], "%Y-%m-%d")).days
            return 0 <= age <= days
        except (ValueError, KeyError):
            return False
    if macro["breadth"].get("ok"):
        hy, nfci = macro.get("hy_oas", {}), macro.get("nfci", {})
        macro["breadth"]["pressure_confirmed"] = bool(
            recent(macro["breadth"], 5) and (
                recent(hy, 5) and macro_status("hy_oas", hy)[0] == "red"
                or recent(nfci, 8) and nfci["value"] >= 0))
    return macro


UST_MONTH_WATCH = 30  # 10Y 美债一个月上行 >= 30 bp：偏快（仅提示，不变色）
UST_MONTH_YELLOW = 50  # 一个月上行 >= 50 bp：急升，利率冲击风险（参考行，不计入综合灯）
HY_WEEK_YELLOW = 30   # 垃圾债利差一周（5个交易日）扩大 >= 30 bp → 黄
HY_WEEK_RED = 50      # 一周扩大 >= 50 bp → 红


def macro_status(key, item, drawdown=None):
    """返回 (等级, 文案)  等级: green/yellow/red/gray"""
    if not item.get("ok"):
        return "gray", "无数据"

    if key == "hy_oas":
        v = item["value"]
        dw = item.get("delta_week")
        if dw is not None and dw >= HY_WEEK_RED:
            if v >= S["hy_green"]:
                return "red", "一周急升，信用风险升温"
            return "red", "一周急升但水平不高，观察是否持续"
        if v >= 500:
            return "red", "信用压力很高"
        if v >= S["hy_red"]:
            return "red", "信用压力升高"
        if v >= S["hy_green"]:
            return "yellow", "收紧"
        if dw is not None and dw >= HY_WEEK_YELLOW:
            return "yellow", "一周走阔，信用转弱"
        return "green", "平静"

    if key == "vix":
        v = item["value"]
        if v >= S["vix_red"]:
            return "red", "极度恐慌"
        if v >= S["vix_yellow"]:
            return "yellow", "恐慌"
        if v >= S["vix_green"]:
            return "yellow", "紧张"
        return "green", "平静"

    if key == "sp500":
        dd = item.get("drawdown")
        if dd is None:
            return "gray", "—"
        if dd <= -20:
            return "red", "第3档"
        if dd <= -15:
            return "yellow", "第2档"
        if dd <= -10:
            return "yellow", "第1档"
        return "green", "第0档"

    if key == "ust10":
        dm = item.get("delta_month")
        if dm is None:
            return "gray", "一个月前数据不足"
        bp = round(dm * 100, 6)  # 避免 0.4999999 这类浮点误差让刚好 50 bp 漏判
        if bp >= UST_MONTH_YELLOW:
            return "yellow", f"一个月急升 {bp:+.0f} bp，留意利率冲击"
        if bp >= UST_MONTH_WATCH:
            return "green", f"一个月 {bp:+.0f} bp，上行偏快"
        return "green", f"一个月 {bp:+.0f} bp，利率平稳"

    if key == "curve":
        v = item["value"]
        if v < 0:
            return "yellow", "倒挂"
        return "green", "正常"

    if key == "nfci":
        return ("yellow", "金融环境偏紧，留意风险") if item["value"] >= 0 else ("green", "金融环境偏宽松")

    if key == "breadth":
        long_term, short_term = item["value"], item["pct50"]
        if long_term < 30 and item.get("pressure_confirmed"):
            return "red", "多数股票走弱，且信用或金融压力也在升高"
        if long_term < 50:
            return "yellow", "过半股票跌破长期均线，注意风险"
        if short_term < 30:
            return "yellow", "短期上涨只由少数股票支撑"
        return "green", "多数股票趋势尚稳"

    return "gray", "—"


RISK_INDICATORS = ("hy_oas", "vix", "sp500", "breadth", "nfci")
RISK_LABELS = {"hy_oas": "垃圾债利差", "vix": "VIX", "sp500": "标普500回撤",
               "breadth": "上涨参与度", "nfci": "金融压力"}
RISK_RULE_TEXT = (
    "五项指标分三类：信用／金融环境（垃圾债利差、金融压力）、情绪（VIX）、趋势（标普500回撤、上涨参与度）。"
    "每项黄记1分、红记2分；同类只取最高分，不重复计分；信用／金融环境类按2倍计分，情绪、趋势各按1倍，总分0至8。"
    "0至1分为绿灯（有轻微提示会写在说明里），2至4分为黄灯，5分及以上为红灯。"
    "数据缺失时最低为黄灯，缺失不代表安全。这是风险参考规则，不预测涨跌，也不是买卖指令。"
)
RISK_CATEGORIES = (("hy_oas", "nfci"), ("vix",), ("sp500", "breadth"))
RISK_CATEGORY_WEIGHT = (2, 1, 1)
RISK_SCORE = {"green": 0, "yellow": 1, "red": 2, "gray": 0}


def market_risk_summary(macro, target_date=None):
    """严重度计分（黄1红2，同类取最高，信用／金融类×2）；缺失或过期数据不能得到绿灯。"""
    target_date = target_date or TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    levels, missing = {}, []
    for key in RISK_INDICATORS:
        item = macro.get(key) or {}
        try:
            age = (datetime.strptime(target_date, "%Y-%m-%d") -
                   datetime.strptime(item["date"], "%Y-%m-%d")).days
            limit = 10 if key == "nfci" else 5 if key == "breadth" else 7
            fields = ("value", "drawdown") if key == "sp500" else (
                ("value", "pct50") if key == "breadth" else ("value",))
            valid = item.get("ok") and 0 <= age <= limit and all(
                isinstance(item.get(field), (int, float)) and math.isfinite(item[field]) for field in fields)
            level = macro_status(key, item)[0] if valid else "gray"
        except (KeyError, TypeError, ValueError):
            level = "gray"
        levels[key] = level
        if level == "gray":
            missing.append(RISK_LABELS[key])
    score = sum(weight * max(RISK_SCORE[levels[key]] for key in category)
                for category, weight in zip(RISK_CATEGORIES, RISK_CATEGORY_WEIGHT))
    warnings = [RISK_LABELS[key] for key in RISK_INDICATORS if levels[key] in ("red", "yellow")]
    level = "red" if score >= 5 else "yellow" if score >= 2 or missing else "green"
    label = {"red": "风险升高", "yellow": "留意风险", "green": "风险平稳"}[level]
    if not warnings and missing:
        label = "数据不足"
    if warnings:
        reasons = [("轻微提示：" if level == "green" else "警示：") + "、".join(warnings)]
    else:
        reasons = ["已取得的指标未触发警示"]
    reasons.append(f"风险得分 {score}/8")
    if missing:
        reasons.append("缺失或过期：" + "、".join(missing))
    return {"level": level, "label": label, "text": "；".join(reasons), "score": score,
            "levels": levels, "valid_count": 5 - len(missing), "missing": missing}


# ---------------------------------------------------------------- Yahoo

def yahoo_history(symbol):
    """返回 dict: closes(list), volumes(list), highs, lows；失败返回 None
    多端点轮换 + 429 退避重试，尽量扛住限流"""
    span = "2y" if max(S["ma_short"], S["ma_long"]) > 200 and symbol not in FRED_YAHOO_ALIAS.values() else "1y"
    endpoints = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{{s}}?range={span}&interval=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{{s}}?range={span}&interval=1d",
    ]
    last_err = None
    if not YAHOO_GATE.allow():
        return None  # 刚被限流：冷却期内直接走备用源，不重复撞墙
    for attempt in range(2):
        for ep in endpoints:
            url = ep.format(s=symbol)
            try:
                r = requests.get(url, headers=UA, timeout=quote_timeout(8))
                if r.status_code == 429:
                    last_err = "429 限流"
                    if YAHOO_GATE.fail():
                        log("  ! Yahoo 连续限流，暂停约90秒，期间改用备用行情源")
                        return None
                    time.sleep(min(1.5 * (attempt + 1), max(0.0, quote_timeout(1.5))))
                    continue
                r.raise_for_status()
                j = r.json()
                res = j["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                # OHLCV沿同一索引过滤，空volume填0，避免裁剪时错位。
                ticks, raw = res.get("timestamp") or [], q.get("close") or []
                valid = [i for i, c in enumerate(raw) if c is not None and i < len(ticks)]
                dates = [datetime.fromtimestamp(ticks[i], US_TZ).strftime("%Y-%m-%d") for i in valid]
                closes = [raw[i] for i in valid]
                def aligned(key, fallback):
                    seq = q.get(key) or []
                    return [seq[i] if i < len(seq) and seq[i] is not None else fallback(i) for i in valid]
                vols = aligned("volume", lambda i: 0)
                highs = aligned("high", lambda i: raw[i])
                lows = aligned("low", lambda i: raw[i])
                meta = res.get("meta", {})
                if len(closes) < 30:
                    last_err = "历史数据不足"
                    continue
                YAHOO_GATE.ok()
                # 注意：不要用 meta.chartPreviousClose！
                # 它返回的是「请求区间起点之前」的那根 K 线收盘价，range=1y 时等于一年前的价格，
                # 用它算涨跌幅会得到 +40% / +95% 这种荒谬数字。
                # 统一用最后两根日线：closes[-1] 最新收盘，closes[-2] 上一交易日收盘。
                return {
                    "closes": closes,
                    "dates": dates,
                    "volumes": vols,
                    "highs": highs,
                    "lows": lows,
                    "price": closes[-1],
                    "prev_close": closes[-2] if len(closes) >= 2 else None,
                    "currency": meta.get("currency", ""),
                    "quote_symbol": meta.get("symbol", ""),
                    "quote_name": meta.get("shortName") or meta.get("longName") or "",
                }
            except requests.HTTPError as e:
                last_err = str(e)[:60]
                continue
            except Exception as e:
                last_err = str(e)[:60]
                continue
    log(f"  ! {symbol} 抓取失败: {last_err}")
    return None


def stooq_history(symbol):
    """备用数据源 stooq（无限流，稳定）。失败返回 None"""
    # Yahoo 代码 BRK-B -> stooq 的 brk-b.us
    if not STOOQ_GATE.allow():
        return None  # 该源已连续返回非CSV（人机验证等），本轮不再浪费时间
    code = symbol.lower().replace(".", "-") + ".us"
    url = f"https://stooq.com/q/d/l/?s={code}&i=d"
    try:
        r = requests.get(url, headers=UA, timeout=quote_timeout(6))
        r.raise_for_status()
        lines = [l for l in r.text.strip().split("\n") if l]
        if len(lines) < 40 or lines[0].startswith("Date") is False:
            STOOQ_GATE.fail()
            return None
        STOOQ_GATE.ok()
        rows = []
        for line in lines[1:]:
            p = line.split(",")
            if len(p) < 6:
                continue
            try:
                rows.append({
                    "d": p[0].strip(),
                    "o": float(p[1]), "h": float(p[2]), "l": float(p[3]),
                    "c": float(p[4]), "v": float(p[5]) if p[5] else 0.0,
                })
            except ValueError:
                continue
        if len(rows) < 30:
            return None
        return {
            "closes": [r["c"] for r in rows],
            "dates": [r["d"] for r in rows],
            "volumes": [r["v"] for r in rows],
            "highs": [r["h"] for r in rows],
            "lows": [r["l"] for r in rows],
            "price": rows[-1]["c"],
            "prev_close": rows[-2]["c"] if len(rows) >= 2 else None,
            "currency": "USD",
            "source": "stooq",
        }
    except Exception as e:
        log(f"  ! {symbol} stooq 失败: {e}")
        return None


def _nasdaq_date(s):
    """Nasdaq 的日期 '10/06/2026' → '2026-10-06'；认不出来返回 ''"""
    try:
        m, d, y = str(s).split("/")
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    except Exception:
        return ""


def nasdaq_realtime(symbol):
    """Nasdaq 实时报价：返回 {"price": float, "ts": str}；拿不到返回 None

    实测 api.nasdaq.com 的 /info 端点带 isRealTime=true，时间戳跟美东当前时间同步（0 延迟），
    且中国大陆可直连、免 key。只用它取「最新价」，均线 / 布林 / RSI 仍用历史序列算。
    注意 BRK-B 在这个源要写成 BRK.B，代码里已做变体尝试。
    """
    syms = [symbol]
    if "-" in symbol:
        syms.append(symbol.replace("-", "."))
    hdr = dict(UA)
    hdr.update({"Accept": "application/json", "Referer": "https://www.nasdaq.com/"})
    for sym in syms:
        for ac in ("stocks", "etf"):
            try:
                r = requests.get(
                    f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass={ac}",
                    headers=hdr, timeout=quote_timeout(12))
                if r.status_code != 200:
                    continue
                p = ((r.json().get("data") or {}).get("primaryData") or {})
                if not p.get("isRealTime"):
                    continue
                px = float(str(p.get("lastSalePrice", "")).replace("$", "").replace(",", ""))
                if px <= 0:
                    continue
                return {"price": px, "ts": p.get("lastTradeTimestamp", "")}
            except Exception:
                continue
    return None


def apply_realtime(symbol, data):
    """把实时价并进历史序列。

    历史最后一根就是今天 → 替换它（收盘前它本来就是未定稿的那根）；
    历史还没更新到今天 → 补一根新的，昨收顺延为 prev_close。
    这样盘中重跑时 涨跌幅 / RSI / 均线 / 布林 全部按当前价算，而不是昨天的收盘价。
    """
    if not data or CLOSED_ONLY or not S.get("use_realtime"):
        return data
    now_ny = datetime.now(US_TZ)
    # 常规日线实时层不能把夜盘冒充完整23小时bar。
    if not ((9, 30) <= (now_ny.hour, now_ny.minute) < (16, 0)):
        return data
    rt = nasdaq_realtime(symbol)
    if not rt:
        return data
    px = rt["price"]
    dates = data.get("dates") or []
    today = now_ny.strftime("%Y-%m-%d")
    try:
        if dates and dates[-1] == today:
            data["closes"][-1] = px
            if data.get("highs"):
                data["highs"][-1] = max(data["highs"][-1], px)
            if data.get("lows"):
                data["lows"][-1] = min(data["lows"][-1], px)
        else:
            data["prev_close"] = data["price"]
            data["closes"].append(px)
            if data.get("highs"):
                data["highs"].append(px)
            if data.get("lows"):
                data["lows"].append(px)
            # dates 必须跟着补一天，否则定投的「交易日计数」会少算今天
            if isinstance(data.get("dates"), list):
                data["dates"].append(today)
            if isinstance(data.get("volumes"), list):
                data["volumes"].append(0)  # 报价接口没返回当日成交量，不拿昨量冒充今天。
        data["price"] = px
        data["realtime"] = True
        data["rt_ts"] = rt["ts"]
    except Exception as e:
        log(f"  ! {symbol} 实时价合并失败: {e}")
    return data


def nasdaq_history(symbol):
    """备用数据源 Nasdaq（api.nasdaq.com，免 key，中国大陆可直连）。失败返回 None

    注意：
      - ETF（QQQ/SPY/TLT/HYG/SGOV/XLV）必须 assetclass=etf，股票用 stocks，逐个试
      - 返回行是「新→旧」，要反转成「旧→新」
      - BRK-B 这类含连字符的 B 股，这个源要写成 BRK.B，代码里已做变体尝试
    """
    end = NOW.date()
    start = end - timedelta(days=560)  # 留足 200 日均线所需的交易日
    base = ("https://api.nasdaq.com/api/quote/{s}/historical"
            "?assetclass={ac}&fromdate={f}&todate={t}&limit=400")

    # 代码变体：Yahoo 的 BRK-B 在 Nasdaq 要写成 BRK.B
    syms = [symbol]
    if "-" in symbol:
        syms.append(symbol.replace("-", "."))

    for sym in syms:
        for ac in ("stocks", "etf"):
            url = base.format(s=sym, ac=ac, f=start.isoformat(), t=end.isoformat())
            try:
                r = requests.get(url, headers=UA, timeout=quote_timeout(8))
                r.raise_for_status()
                j = r.json()
                rows = ((j.get("data") or {}).get("tradesTable") or {}).get("rows") or []
                if len(rows) < 30:
                    continue
                parsed = []
                for row in rows:
                    try:
                        parsed.append({
                            # Nasdaq 的日期是 10/06/2026 这种，统一成 2026-10-06 好比较
                            "d": _nasdaq_date(row.get("date", "")),
                            "c": float(str(row["close"]).replace("$", "").replace(",", "")),
                            "h": float(str(row["high"]).replace("$", "").replace(",", "")),
                            "l": float(str(row["low"]).replace("$", "").replace(",", "")),
                            "o": float(str(row["open"]).replace("$", "").replace(",", "")),
                            "v": float(str(row.get("volume", "0")).replace(",", "") or 0),
                        })
                    except (KeyError, ValueError):
                        continue
                if len(parsed) < 30:
                    continue
                parsed.reverse()  # 旧 → 新
                return {
                    "closes": [p["c"] for p in parsed],
                    "dates": [p["d"] for p in parsed],
                    "volumes": [p["v"] for p in parsed],
                    "highs": [p["h"] for p in parsed],
                    "lows": [p["l"] for p in parsed],
                    "price": parsed[-1]["c"],
                    "prev_close": parsed[-2]["c"] if len(parsed) >= 2 else None,
                    "currency": "USD",
                    # 哪个 assetclass 试通了就记下来：stocks / etf，用于给 ETF 打标签
                    "asset": ac,
                    "source": "nasdaq",
                }
            except Exception as e:
                log(f"  ! {symbol} nasdaq({sym}/{ac}) 失败: {str(e)[:60]}")
                continue
    return None


FUTU_MARKET = {"US": None, "HK": ".HK", "SH": ".SS", "SS": ".SS", "SZ": ".SZ"}

# 清单保留原代码；仅抓取时映射到行情提供方的指数／期货代码。
INDEX_YAHOO_ALIAS = {
    ".VIX": "^VIX", ".SPX": "^GSPC", ".NDX": "^NDX",
    ".VXN": "^VXN", ".SOX": "^SOX",
    "ESMAIN": "ES=F", "CLMAIN": "CL=F",
}
INDEX_MACRO_ALIAS = {".VIX": "vix", ".SPX": "sp500", "BD#US10Y": "ust10"}


def quote_supported(symbol):
    s = normalize_symbol(symbol)
    return instrument_identity(s) == '^TNX' or s in INDEX_YAHOO_ALIAS or s in INDEX_MACRO_ALIAS or not (
        s.startswith((".", "BD#")) or s.endswith("MAIN") or s in ("2USDCNY", "2XAUUSD"))


def normalize_symbol(symbol):
    """把富途 moomoo 导出的「代码-市场」写法转成行情源认的格式

    UNH-US → UNH   00700-HK → 00700.HK   600519-SH → 600519.SS
    已经是 Yahoo 写法的原样返回。
    """
    s = (symbol or "").strip().upper()
    if s.startswith("31#"):
        s = s[3:]
    if s == "BRK.B":
        s = "BRK-B"
    if "-" not in s and "." not in s:
        return s
    for sep in ("-", "."):
        if sep in s:
            code, _, mk = s.rpartition(sep)
            if code and mk in FUTU_MARKET:
                suffix = FUTU_MARKET[mk]
                if suffix is None:
                    return code.replace(".", "-")  # BRK.B-US → BRK-B
                if mk == "HK":
                    digits = code.lstrip("0") or "0"
                    return digits.zfill(4) + ".HK"
                return code + suffix
    return s


# ETF / 基金类代码：看板上给这些标的一个「ETF」小标签，和个股区分开。
# 静态表是主力（离线可判、不依赖行情源）；行情源若能给出 assetclass=etf 则以此为准，
# 两者取其一命中即算 ETF。以后新增 ETF，往这里加代码即可。
ETF_SYMBOLS = {
    # 宽基 / 规模
    "SPY", "VOO", "IVV", "VTI", "ITOT", "QQQ", "QQQM", "SPYM", "SPYX", "IWM", "DIA",
    "SCHX", "SCHA", "SCHB", "VB", "VO", "VUG", "VTV", "VBR", "MGK", "MGV",
    "SPYV", "SPYG", "VOOG", "VOOV", "SPMO", "QUAL", "MTUM", "USMV", "VLUE", "SIZE",
    "SPHQ", "NOBL", "DGRO", "DGRW", "VYM", "SDY", "SPHD", "SCHD", "VIG",
    # 国际 / 新兴
    "EFA", "VEA", "EEM", "VWO", "VXUS", "SCHF", "SCHE", "IEFA", "IEMG", "EWJ", "FXI",
    # 债券 / 利率（TLT、SGOV 已从清单移除，保留在表里以便日后加回仍能识别）
    "AGG", "BND", "TLT", "IEF", "IEI", "SHY", "SHV", "BIL", "SGOV", "TFLO", "ICSH",
    "JPST", "NEAR", "FLOT", "LQD", "HYG", "JNK", "EMB", "TIP", "SCHZ", "SCHP", "SCHR",
    "MUB", "HYD", "HYMB", "PFF", "PGX", "VCSH", "VCIT", "BSV", "BIV",
    # 行业 SPDR
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE", "XLC",
    "VGT", "VHT", "VFH", "VPU", "VDC", "VCR", "VDE", "VIS", "VAW",
    "SMH", "SOXX", "XBI", "XAR", "XRT", "XOP", "XME", "XPH", "XSW", "XTN", "XHB",
    "KBE", "KRE", "KIE", "IYT", "IYR", "VNQ", "VNQI", "REM", "IAUM",
    # 商品 / 加密 / 主题
    "GLD", "IAU", "GLDM", "SGOL", "SIVR", "BAR", "SLV", "PPLT", "PALL", "GLTR",
    "DBC", "GSG", "DBP", "USO", "UNG", "IBIT", "FBTC", "GBTC", "ETHA", "BITO",
    "ARKK", "ARKW", "ARKQ", "ARKF", "ARKX", "SOXL", "SOXS", "TQQQ", "SQQQ",
    "UVXY", "SVXY", "VXX", "USMV",
    # 经 Nasdaq assetclass 实测确认为 ETF（名字不典型，容易漏）
    "EUV", "DRAM",
}


def is_etf(symbol):
    """判断标的是不是 ETF / 基金类。静态表命中即算。"""
    return normalize_symbol(symbol).upper() in ETF_SYMBOLS


# ---------------------------------------------------------------- 基本面恶化
# 数据源：Nasdaq 财报（季度，一次给最近 4 期）。value2=最新季，value5=去年同期 → 直接同比。
# 只对「持仓 + 重点关注」查询：这两个组才是真会交易的；其他关注看财报意义不大，
# 而且每只一个请求，全量查会把运行时间拉长并招来限流。
# 阈值默认值写在这里；想调就在 settings.json 里加同名键覆盖（改完点保存即生效）。
FUND_DEFAULT = {
    "fund_enable":     1,      # 0 = 关闭基本面检查
    "fund_rev_red":    -20.0,  # 营收同比 ≤ 此值 → 红
    "fund_rev_yellow": -10.0,  # 营收同比 ≤ 此值 → 黄
    "fund_om_drop":    3.0,    # 营业利润率同比下滑（百分点）→ 黄
    "fund_gm_drop":    3.0,    # 毛利率同比下滑（百分点）→ 黄
    "fund_de_rise":    30.0,   # 负债/权益同比上升 % → 黄
    "fund_cr_drop":    20.0,   # 流动比率同比下滑 % → 黄
    "fund_roe_drop":   30.0,   # ROE 同比下滑 % → 黄
    "fund_pm_abnormal": 60.0,  # |净利率| 超过此值视为一次性损益污染，净利口径不参与判定
}


def fund_rule(key):
    v = S.get(key, FUND_DEFAULT.get(key))
    try:
        return float(v)
    except Exception:
        return float(FUND_DEFAULT[key])


_FUND_CACHE = {}
_FUND_TLS = threading.local()


def _fund_session():
    """每线程一个长连接 Session：既复用 TCP 握手，又避免多线程共用一个 Session。"""
    s = getattr(_FUND_TLS, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({**UA, "Accept": "application/json"})
        _FUND_TLS.s = s
    return s


def _fund_num(v):
    """Nasdaq 财报值形如 '$112,032,000'、'(91,154)'、'12.3%'、'--'，统一转 float。"""
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "--", "-", "—"):
        return None
    neg = s.startswith("(") or s.startswith("-")
    s = s.strip("()").lstrip("-")
    try:
        n = float(s)
    except Exception:
        return None
    return -n if neg else n


def nasdaq_financials(sym, timeout=12):
    """取最近 4 个季度财报。ETF / 无财报 / 请求失败都返回 None，不影响主流程。"""
    key = normalize_symbol(sym).upper()
    if key in _FUND_CACHE:
        return _FUND_CACHE[key]
    res = None
    try:
        # Nasdaq 用 BRK.B 而不是 BRK-B，带横杠的代码拿不到数据
        url = f"https://api.nasdaq.com/api/company/{key.replace('-', '.')}/financials?frequency=2"
        r = _fund_session().get(url, timeout=timeout)
        if r.status_code == 200:
            d = (r.json() or {}).get("data") or {}

            def table(name):
                return {x.get("value1"): x for x in ((d.get(name) or {}).get("rows") or [])}

            inc = table("incomeStatementTable")
            if inc:  # ETF 返回空表
                headers = (d.get("incomeStatementTable") or {}).get("headers") or {}
                res = {
                    "period": headers.get("value2"),
                    # 接口只给最近4个季度：value2 最新季，value5 是 3 个季度前（不是去年同期）
                    "base_period": headers.get("value5"),
                    "inc": inc, "bs": table("balanceSheetTable"),
                    "cf": table("cashFlowTable"), "rt": table("financialRatiosTable"),
                }
    except Exception as e:
        log(f"  · {key} 财报读取失败：{e}")
    _FUND_CACHE[key] = res
    return res


def _fcell(tbl, label, col="value2"):
    return _fund_num((tbl or {}).get(label, {}).get(col)) if tbl else None


def _period_label(period):
    """Nasdaq 报告期 '6/30/2026' → '2026/6'；认不出来返回空串。"""
    date = _nasdaq_date(period)
    if not date:
        return ""
    year, month, _ = date.split("-")
    return f"{year}/{int(month)}"


# ---------------------------------------------------------------- 下一次财报日期
# 数据源：Nasdaq analyst/earnings-date（Zacks 提供）。接口只给日期和盘前/盘后，不给具体钟点。
# 只对持仓与重点关注常驻查询；其他分组仅在个股有红/黄警示时查询。ETF、指数、期货不适用。
_EARN_CACHE = {}
_EARN_RE = re.compile(
    r"(expected\*?|estimated)\s+to\s+report\s+earnings\s+on\s+(\d{1,2})/(\d{1,2})/(\d{4})"
    r"(?:\s+(before market open|after market close|during market hours))?", re.I)
_EARN_TIMING = {"before market open": "pre", "after market close": "post", "during market hours": "during"}


def parse_earnings_text(text, today=None):
    """解析 Nasdaq 财报日期文案。返回 {'status': ...}：
    ok 有未来日期；unknown 供应商尚未提供/日期已过期待更新。"""
    today = today or datetime.now(US_TZ).date().isoformat()
    m = _EARN_RE.search(" ".join(str(text or "").split()))
    if not m:
        return {"status": "unknown"}
    kind, month, day, year, timing = m.groups()
    try:
        date = datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return {"status": "unknown"}
    if date < today:
        return {"status": "unknown"}  # 供应商还没更新到下一次，不拿过去日期冒充
    return {"status": "ok", "date": date, "timing": _EARN_TIMING.get((timing or "").lower(), ""),
            "kind": "estimated" if kind.lower() == "estimated" else "expected"}


def nasdaq_earnings(sym, timeout=12):
    """取下一次财报日期。状态：ok / unknown(待公布) / na(不适用,如ETF) / error(请求失败)。"""
    key = normalize_symbol(sym).upper()
    if key in _EARN_CACHE:
        return _EARN_CACHE[key]
    res = {"status": "error"}
    try:
        url = f"https://api.nasdaq.com/api/analyst/{key.replace('-', '.')}/earnings-date"
        r = _fund_session().get(url, timeout=timeout)
        if r.status_code == 200:
            data = (r.json() or {}).get("data")
            if isinstance(data, dict) and data.get("reportText") is not None:
                res = parse_earnings_text(data["reportText"])
            else:
                res = {"status": "na"}
    except Exception as e:
        log(f"  · {key} 财报日期读取失败：{str(e)[:60]}")
    _EARN_CACHE[key] = res
    return res


# 「强制刷新」按钮：清掉 Cache Storage / Service Worker，再带时间戳参数重新加载，绕开浏览器和 CDN 缓存
HARD_REFRESH_JS = (
    "(function(){var b=document.getElementById('btnHardRefresh');if(!b)return;"
    "b.onclick=async function(){b.disabled=true;b.textContent='刷新中…';"
    "try{if(window.caches){var ks=await caches.keys();await Promise.all(ks.map(function(k){return caches.delete(k);}));}"
    "if(navigator.serviceWorker){var rs=await navigator.serviceWorker.getRegistrations();"
    "await Promise.all(rs.map(function(r){return r.unregister();}));}}catch(e){}"
    "var u=new URL(location.href);u.searchParams.set('_r',String(Date.now()));location.replace(u.toString());};})();"
)


# 个股/ETF 表统一用固定列宽，三张表（警示 / ETF / 折叠的无异动）栏位才能上下对齐
STK_COLS = ('<colgroup><col class="c-sym"><col class="c-earn"><col class="c-px"><col class="c-sig"></colgroup>')
# 表头：「财报」两个字只在这里写一次，每行只写日期和时段；价格、信号两列不写表头
STK_HEAD = '<thead><tr><th>名称</th><th class="h-earn">财报</th><th></th><th></th></tr></thead>'


def earnings_label(e):
    """返回 (页面文字, 说明) 或 None。时间只写数据源真实提供的：盘前/盘后/盘中，不编造钟点。"""
    if not e or e.get("status") in (None, "na"):
        return None
    status = e["status"]
    if status == "unknown":
        return "时间待公布", "下一次财报时间供应商尚未提供，不使用过去的报告期代替"
    if status == "error":
        return "日期暂未取得", "下一次财报日期本轮读取失败，不代表没有财报"
    year, month, day = e["date"].split("-")
    this_year = datetime.now(US_TZ).year
    date_txt = f"{month}/{day}" if int(year) == this_year else f"{year}/{month}/{day}"
    timing = {"pre": "盘前", "post": "盘后", "during": "盘中"}.get(e.get("timing"), "具体时段待定")
    nxt = (datetime(int(year), int(month), int(day)) + timedelta(days=1)).strftime("%m/%d")
    bj = {"pre": f"北京时间约{month}/{day}傍晚至晚间", "post": f"北京时间约{nxt}凌晨",
          "during": f"北京时间约{month}/{day}夜间至{nxt}凌晨"}.get(e.get("timing"), "北京时间待定")
    kind = "预计" if e.get("kind") == "expected" else "算法估算，可能调整"
    return f"美东 {date_txt} {timing}", f"{kind} · {bj}"


EARN_SOON_DAYS = 14   # 财报日距今 0~14 天（含）时，页面上的财报文字标黄


def earnings_soon(e, today=None):
    """财报日期落在今天起 EARN_SOON_DAYS 天内（含当天、含第 14 天）返回 True；待公布、失败、已过期都不算。"""
    if not e or e.get("status") != "ok" or not e.get("date"):
        return False
    try:
        today = today or TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
        days = (datetime.strptime(e["date"], "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return False
    return 0 <= days <= EARN_SOON_DAYS


def attach_earnings(items):
    """持仓/重点关注个股常驻；其他分组仅红黄警示个股。ETF、指数、期货和参考值不适用。"""
    def wants(d):
        sym = d.get("symbol") or ""
        ident = instrument_identity(sym)
        if not sym or d.get("reference_only") or not quote_supported(sym) or is_etf(sym):
            return False
        if ident.startswith("^") or ident.endswith("=F"):
            return False
        return d.get("group") in ("position", "focus") or d.get("level") in ("red", "yellow")

    targets = [d for d in items if wants(d)]
    stat = {"targets": len(targets), "dated": 0, "pending": 0, "failed": 0}
    if not targets:
        return stat
    log(f"读取 {len(targets)} 只个股的下一次财报日期…")
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda d: nasdaq_earnings(d["symbol"]), targets))
        for d, e in zip(targets, results):
            if e.get("status") == "na":
                continue
            d["earnings"] = e
            key = {"ok": "dated", "unknown": "pending"}.get(e.get("status"), "failed")
            stat[key] += 1
    except Exception as e:
        log(f"  ! 财报日期读取中断，跳过：{e}")  # 加分项，不能拖垮页面生成
    log(f"  财报日期：已取得 {stat['dated']} · 待公布 {stat['pending']} · 失败 {stat['failed']}")
    return stat


def fundamental_check(sym):
    """基本面恶化判定（全套 + 分级）。

    核心三项决定红/黄：营收同比、营业利润率、经营现金流。
    扩展四项只贡献黄：毛利率、负债/权益、流动比率、ROE。
    主轴刻意避开净利润——一次性损益（如 NTNX 递延税资产释放、-11.9 亿税项）
    会把净利抬高到营收之上，用净利判基本面必然误判。
    """
    if not fund_rule("fund_enable"):
        return None
    # ETF / 基金没有财报，直接跳过，省掉一次必然无结果的请求
    if is_etf(sym):
        return None
    f = nasdaq_financials(sym)
    if not f:
        return None
    inc, bs, cf, rt = f["inc"], f["bs"], f["cf"], f["rt"]
    NOW, YR = "value2", "value5"  # 4 季窗口：value2 最新季、value5 是 3 个季度前（接口取不到去年同期）
    now_label, base_label = _period_label(f.get("period")), _period_label(f.get("base_period"))
    span = f"{base_label}→{now_label}" if base_label and now_label else ""

    rev0, rev4 = _fcell(inc, "Total Revenue", NOW), _fcell(inc, "Total Revenue", YR)
    gm0, gm4 = _fcell(rt, "Gross Margin", NOW), _fcell(rt, "Gross Margin", YR)
    om0, om4 = _fcell(rt, "Operating Margin", NOW), _fcell(rt, "Operating Margin", YR)
    roe0, roe4 = _fcell(rt, "After Tax ROE", NOW), _fcell(rt, "After Tax ROE", YR)
    cr0, cr4 = _fcell(rt, "Current Ratio", NOW), _fcell(rt, "Current Ratio", YR)
    pm0, tax0 = _fcell(rt, "Profit Margin", NOW), _fcell(inc, "Income Tax", NOW)
    pm4, tax4 = _fcell(rt, "Profit Margin", YR), _fcell(inc, "Income Tax", YR)
    cfo0, cfo4 = _fcell(cf, "Net Cash Flow-Operating", NOW), _fcell(cf, "Net Cash Flow-Operating", YR)
    tl0, te0 = _fcell(bs, "Total Liabilities", NOW), _fcell(bs, "Total Equity", NOW)
    tl4, te4 = _fcell(bs, "Total Liabilities", YR), _fcell(bs, "Total Equity", YR)

    hits, red = [], False

    def add(t, is_red=False):
        nonlocal red
        hits.append(t)
        if is_red:
            red = True

    # 一次性损益污染：净利率离谱高、税项为负（税收返还/递延税资产释放），
    # 或净利率远高于营业利润率（说明净利里有大量非经营收益）。最新季和对比基准季都要查，
    # 否则基准季被一次性收益抬高，会把正常回落误读成"盈利腰斩"。
    def distorted(pm, om, tax):
        return (pm is not None and abs(pm) > fund_rule("fund_pm_abnormal")) or \
               (tax is not None and tax < 0) or \
               (pm is not None and om is not None and pm - om > 20)

    oneoff, base_oneoff = distorted(pm0, om0, tax0), distorted(pm4, om4, tax4)
    tag = f"（{span}）" if span else ""

    # 核心 1 · 营收（对比 3 个季度前；Nasdaq 只提供最近 4 个季度，取不到去年同期）
    if rev0 is not None and rev4 and rev4 > 0:
        c = (rev0 / rev4 - 1) * 100
        if c <= fund_rule("fund_rev_red"):
            add(f"营收较{base_label or '3季前'} {c:+.0f}%", True)
        elif c <= fund_rule("fund_rev_yellow"):
            add(f"营收较{base_label or '3季前'} {c:+.0f}%")
    # 核心 2 · 营业利润率（不含税项和一次性损益，最干净的盈利口径）
    if om0 is not None:
        if om0 < 0:
            add(f"营业亏损 {om0:.0f}%", True)
        elif om4 is not None and om0 - om4 <= -fund_rule("fund_om_drop"):
            add(f"营业利率 {om4:.1f}%→{om0:.1f}%{tag}")
    # 核心 3 · 经营现金流
    if cfo0 is not None:
        if cfo0 < 0 and cfo4 is not None and cfo4 > 0:
            add("经营现金流转负", True)
        elif cfo0 < 0:
            add(f"经营现金流为负 {cfo0 / 1000:.0f}M")

    # 扩展 · 毛利率
    if gm0 is not None and gm4 is not None and gm0 - gm4 <= -fund_rule("fund_gm_drop"):
        add(f"毛利率 {gm4:.1f}%→{gm0:.1f}%{tag}")
    # 扩展 · 负债/权益（要求负债绝对额也上升，否则亏损把权益做小会误报加杠杆）
    de0 = tl0 / te0 if (tl0 and te0) else None
    de4 = tl4 / te4 if (tl4 and te4) else None
    if de0 and de4 and de4 > 0 and de0 / de4 - 1 >= fund_rule("fund_de_rise") / 100.0 \
            and tl0 is not None and tl4 is not None and tl0 > tl4:
        add(f"负债/权益 {de4:.2f}→{de0:.2f}倍{tag}")
    # 扩展 · 短期偿债能力（Nasdaq 以百分比给出：84.25% 即 0.84 倍，页面统一显示成倍数）
    if cr0 and cr4 and cr4 > 0 and cr0 / cr4 - 1 <= -fund_rule("fund_cr_drop") / 100.0:
        add(f"流动比率 {cr4 / 100:.2f}→{cr0 / 100:.2f}倍{tag}")
    # 扩展 · ROE（本季净利含一次性损益时不比较；基准季含一次性收益时照常比较，但在文字里注明仅供参考）
    if not oneoff and roe0 is not None and roe4 is not None and roe4 > 0 \
            and roe0 / roe4 - 1 <= -fund_rule("fund_roe_drop") / 100.0:
        ref = "（基准季含一次性收益，仅供参考）" if base_oneoff else ""
        add(f"季度ROE {roe4:.1f}%→{roe0:.1f}%{tag}{ref}")

    # 一次性损益只作附加说明，不单独点亮徽章（否则 GOOGL/NTNX 会被无谓判黄）
    notes = []
    if hits and oneoff:
        notes.append("本季净利含一次性损益，ROE未参与判定")
    hits.extend(notes)
    # 参考背景：单项恶化要结合经营利润和现金流判断，不能把比率变化直接等同于主营变差
    context = []
    if om0 is not None:
        context.append(f"本季营业利率 {om0:.1f}%")
    if cfo0 is not None:
        context.append("本季经营现金流为" + ("正" if cfo0 > 0 else "负"))
    base = {"period": f["period"], "base_period": f.get("base_period"),
            "compare": span, "context": context, "oneoff": oneoff}
    if not hits:
        return {"level": "green", "hits": [], **base}
    return {"level": "red" if red else "yellow", "hits": hits, **base}


def valid_history(data):
    """拒绝损坏/错位的日线，不把异常 OHLCV 送进信号计算。"""
    dates = data.get("dates") or []
    if len(dates) < 30 or len(dates) != len(data.get("closes") or []):
        return False
    try:
        if any(a >= b for a, b in zip(dates, dates[1:])):
            return False
        if any(datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d") != d for d in dates):
            return False
        for key in ("closes", "highs", "lows", "volumes"):
            seq = data.get(key) or []
            if seq and len(seq) != len(dates):
                return False
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or
                   (v < 0 if key == "volumes" else v <= 0) for v in seq):
                return False
        for low, close, high in zip(data.get("lows") or [], data["closes"], data.get("highs") or []):
            if low > close * 1.001 or close > high * 1.001:
                return False
        return (math.isclose(data["price"], data["closes"][-1], rel_tol=1e-5) and
                math.isclose(data["prev_close"], data["closes"][-2], rel_tol=1e-5))
    except (ValueError, TypeError, OverflowError, KeyError):
        return False


def fetch_history(symbol):
    """普通标的 Yahoo → stooq → Nasdaq；指数／期货只取经映射的 Yahoo 日线。

    GitHub Actions（美国 IP）走 Yahoo 正常；中国大陆本地跑 Yahoo 会被整段封禁
    （返回 403 且提示 mainland China 不可访问），stooq 也上了 JS 人机验证，
    所以补第三级 Nasdaq，保证本地也能跑出真实数据验证筛选逻辑。

    代码先过一遍 normalize_symbol：富途导出的 UNH-US 这类后缀行情源不认，
    剥掉后缀才抓得到（否则整只标的变灰）。
    """
    symbol = normalize_symbol(symbol)
    identity = instrument_identity(symbol)
    alias = INDEX_YAHOO_ALIAS.get(symbol) or (identity if identity.startswith('^') or identity.endswith('=F') else None)
    if identity == '^TNX':
        return None  # 国债收益率不使用报价指数替代百分比口径。
    loaders = (("yahoo", yahoo_history),) if alias else (
        ("yahoo", yahoo_history), ("stooq", stooq_history), ("nasdaq", nasdaq_history))
    fallback = None
    suspicious = None
    for name, loader in loaders:
        data = loader(alias or symbol)
        if not data:
            continue
        if CLOSED_ONLY:
            data = trim_history(data, TARGET_DATE)
        if not data or not valid_history(data) or data["dates"][-1] > TARGET_DATE:
            log(f"  ! {symbol} {name} 日线不合法或超过目标交易日，换源")
            continue
        data["source"] = name
        # 大幅跳变可能是拆股口径错位；找另一个同日来源佐证，不直接制造红信号。
        prev = data["closes"][-2]
        jump = abs(data["price"] / prev - 1) > 0.6
        if jump:
            if suspicious and suspicious["dates"][-1] == data["dates"][-1] and \
                    (not suspicious.get("currency") or not data.get("currency") or
                     suspicious["currency"] == data["currency"]) and \
                    abs(data["price"] / suspicious["price"] - 1) <= 0.1:
                data = suspicious
            else:
                suspicious = data
                continue
        if CLOSED_ONLY:
            if data["dates"][-1] == TARGET_DATE:
                return data
        elif data["dates"][-1] == TARGET_DATE:
            return data if alias else apply_realtime(symbol, data)
        if fallback is None or data["dates"][-1] > fallback["dates"][-1]:
            fallback = data
    # Nasdaq 股票/ETF 实时报价不可套用在指数或期货别名上。
    return apply_realtime(symbol, fallback) if fallback and not CLOSED_ONLY and not alias else fallback


def macro_index_quote(symbol, cfg, group, macro):
    """Yahoo 日线不可用时复用已验证的市场参考点位；不伪造 OHLC 或交易警示。"""
    key = {'^VIX': 'vix', '^GSPC': 'sp500', '^TNX': 'ust10'}.get(instrument_identity(symbol))
    item = macro.get(key) if key else None
    if not item or not item.get("ok") or not isinstance(item.get("value"), (int, float)) or not math.isfinite(item["value"]) or item["value"] <= 0 or not item.get("date"):
        return None
    price, prev = item["value"], item.get("prev")
    return {
        "symbol": symbol, "note": cfg.get("note", ""), "price": price,
        "chg": (price / prev - 1) * 100 if prev else None,
        "rsi": {period: None for period in RSI_PERIODS},
        "dist_high": None, "dist_low": None, "vol_ratio": None,
        "trigger": cfg.get("trigger"), "source": item.get("source") or "市场参考", "data_date": item["date"],
        "reference_lagging": bool(item.get("lagging")),
        "unit": "%" if key == "ust10" else "", "group": group, "boll_up": None, "boll_dn": None,
        "reference_only": True, "signals": ["参考点位 · 不计算交易警示"], "level": "green",
    }


RSI_PERIODS = (6, 12, 24)


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 50.0 if ag == 0 else 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


RS_DAYS = 20          # 板块相对标普500强弱的回看交易日数（约一个月）
RS_NEUTRAL_PP = 0.5   # 相对强弱绝对值不足该百分点，按「持平」显示（不着色）


def relative_strength(etf, bench, days=RS_DAYS, bench_name=RS_BENCH_NAME):
    """板块 ETF 相对基准（默认标普500指数）的强弱：两者近 days 个交易日涨幅之差（单位：百分点）。

    按日期对齐而不是按下标：两边任何一端缺一天（或盘中补了实时价的那一根不同步），
    下标就会错位一天。最近 3 根里找双方共有的最新日期作终点，起点必须是 ETF 序列里
    终点往前数第 days 根、且基准也有该日；对不上就返回 None，页面不显示，绝不拿错位数据凑数。
    """
    try:
        ed, ec = list(etf.get("dates") or []), list(etf.get("closes") or [])
        sd, sc = list(bench.get("dates") or []), list(bench.get("closes") or [])
        if not ed or len(ed) != len(ec) or len(sd) != len(sc) or len(ec) <= days:
            return None
        bench_close = dict(zip(sd, sc))
        end_i = next((i for i in range(len(ed) - 1, max(len(ed) - 4, -1), -1) if ed[i] in bench_close), None)
        if end_i is None or end_i - days < 0 or ed[end_i - days] not in bench_close:
            return None
        start_i = end_i - days
        e0, e1 = float(ec[start_i]), float(ec[end_i])
        s0, s1 = float(bench_close[ed[start_i]]), float(bench_close[ed[end_i]])
        if min(e0, e1, s0, s1) <= 0 or not all(math.isfinite(x) for x in (e0, e1, s0, s1)):
            return None
        etf_ret, bench_ret = (e1 / e0 - 1) * 100, (s1 / s0 - 1) * 100
        return {"days": days, "etf": etf_ret, "bench": bench_ret, "bench_name": bench_name, "diff": etf_ret - bench_ret,
                "start": ed[start_i], "end": ed[end_i]}
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None


def boll(closes, n=20, k=2.0):
    """布林带：返回 (中轨, 上轨, 下轨)，数据不足返回三个 None"""
    if len(closes) < n:
        return None, None, None
    win = closes[-n:]
    mid = sum(win) / n
    sd = (sum((x - mid) ** 2 for x in win) / n) ** 0.5
    return mid, mid + k * sd, mid - k * sd


def boll_streak(closes, n=20, k=2.0, near_pct=0.5, max_days=120, highs=None, lows=None):
    """连续贴近布林上轨 / 下轨的天数，从今天往回数。

    两条口径（2026-10-07 修正，此前与图上对不上就是这两点）：
    1. 窗口包含当天：win = closes[i-n+1 : i+1]，和 boll() 完全一致。
       修正前这里用 closes[i-n:i]（不含当天），等于拿「昨天的轨道」比「今天的价格」，
       和看板主信号、和图上画出来的轨道都差一天。
    2. 盘中触及即算：上轨用当日最高价、下轨用当日最低价，不用收盘价。
       用户要的规则是「盘中穿过就算」，收盘价会把「冲上去又回落」的那天漏掉。

    只要某天距轨道超过 near_pct 就中断计数 —— 「远离一天就不算连续，再贴近重新从第一日算」。

    返回 {"up": {"days": int, "cross": int}, "dn": {...}}
      days  连续贴合天数（距轨道 <= near_pct）
      cross 其中真正穿越轨道的天数（距轨道 <= 0）
    """
    out = {"up": {"days": 0, "cross": 0}, "dn": {"days": 0, "cross": 0}}
    if len(closes) < n:
        return out
    # 没有日内高低价时退回收盘价，不至于整只股失去这条信号
    hi = highs if (highs and len(highs) == len(closes)) else closes
    lo = lows if (lows and len(lows) == len(closes)) else closes
    for side in ("up", "dn"):
        days = 0
        cross = 0
        i = len(closes) - 1
        while i >= n - 1 and days < max_days:
            win = closes[i - n + 1:i + 1]  # 含第 i 天本身，与 boll() 同口径
            mid = sum(win) / n
            sd = (sum((x - mid) ** 2 for x in win) / n) ** 0.5
            up = mid + k * sd
            dn = mid - k * sd
            px = hi[i] if side == "up" else lo[i]
            gap = (up - px) / up * 100 if side == "up" else (px - dn) / dn * 100
            if gap > near_pct:             # 这一天远离了，连续性断掉
                break
            days += 1
            if gap <= 0:
                cross += 1
            i -= 1
        out[side] = {"days": days, "cross": cross}
    return out


def dca_status(dates, start, every):
    """定投节奏：从 start 那个交易日算起，每 every 个交易日提醒一次。

    无状态设计——不用记「上次提醒是哪天」，只按日历推，所以改间隔、改起始日随时生效，
    也不存在脚本写回持仓文件导致的循环触发。

    返回 None（没配置 / 数据不够）或
      {"count": int, "due": bool, "next_in": int}
        count   从 start 到今天一共经过几个交易日（start 当天算第 1 个）
        due     今天是不是定投日
        next_in 距下一个定投日还差几个交易日（0 = 就是今天）
    """
    if not dates or not start or not every:
        return None
    try:
        every = int(every)
    except (TypeError, ValueError):
        return None
    if every <= 0:
        return None
    start = str(start).strip()
    cnt = sum(1 for d in dates if d and str(d) >= start)
    if cnt == 0:
        return None                      # 起始日在数据范围之后，还没开始
    idx = cnt - 1
    rem = (-idx) % every
    return {"count": cnt, "due": rem == 0, "next_in": rem, "every": every}


def streak_tail(st, rail="上轨"):
    """把连续天数拼成信号尾巴。连续 1 日不啰嗦，>=2 日才标。"""
    if not st or st["days"] < 2:
        return ""
    # 撞上 max_days 上限说明「从有数据起从没中断过」（货币基金之类），
    # 写「连续第120日」没信息量，改成「≥」如实表达
    n = f"≥{st['days']}" if st["days"] >= 120 else str(st["days"])
    t = f" · 连续第{n}日贴近{rail}"
    if st["cross"]:
        t += f"（其中{st['cross']}日穿越）"
    return t


def global_dca(target_date):
    """全局定投提醒：按交易所日历数「起点日 → 数据日」经过了几个交易日。

    返回 None（没开启）或 dict：
      every, start, count(已过交易日数), times(第几次定投), due(数据日是否定投日),
      next_in(距下次还差几个交易日), next_date(下一个定投日，YYYY-MM-DD)
    """
    every, start = int(S.get("dca_every") or 0), (S.get("dca_start") or "").strip()
    if every <= 0 or not start or not target_date:
        return None
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XNYS")
        first = cal.date_to_session(start, direction="next")
        last = cal.date_to_session(target_date, direction="previous")
        if first > last:
            return {"every": every, "start": start, "pending": True,
                    "first": first.date().isoformat()}
        count = len(cal.sessions_in_range(first, last))
        rem = (-(count - 1)) % every
        upcoming = cal.sessions_window(last, rem + 1)
        next_date = upcoming[-1].date().isoformat()
        return {"every": every, "start": start, "count": count, "date": last.date().isoformat(),
                "times": (count - 1) // every + 1, "due": rem == 0,
                "next_in": rem, "next_date": next_date}
    except Exception as e:
        log(f"  ! 定投计算失败：{e}")
        return None


def dca_text(d):
    if not d:
        return ""
    # 页面只留一句：还有几个交易日（日期）；定投日当天整行变黄（样式 .dca.on）
    if d.get("pending"):
        return f"尚未开始（首个定投日 {d['first']}）"
    if d["due"]:
        return f"今天是定投日（{d['date']}，第 {d['times']} 次）"
    return f"还有 {d['next_in']} 个交易日（{d['next_date']}）"


# ---------------------------------------------------------------- 筛选

def reduce_rule(cfg):
    """读持仓条目的减仓价，返回价格；没填或写错一律返回 None。

    reduce 必须是大于 0 的有限数字（布尔、字符串都不算），写错就忽略并记一条日志。"""
    v = cfg.get("reduce")
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        log(f"  ! 减仓价无效，已忽略：{v!r}")
        return None
    return float(v)


def _level_num(v):
    """加仓价／减仓价是否可显示：必须是大于 0 的有限数字（布尔、字符串都不算）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return f"{v:.4f}".rstrip("0").rstrip(".")


def level_note(trigger, reduce):
    """标的名称下面的小标签：加仓价 = 橙色「加 325」，减仓价 = 青色「减 400」，都有就并排；都没有返回空串。
    样式是空心胶囊，和 ETF 的实心蓝色小方标签区分开。"""
    parts = []
    t, r = _level_num(trigger), _level_num(reduce)
    if t:
        parts.append(f'<span class="lvtag add">加 {t}</span>')
    if r:
        parts.append(f'<span class="lvtag cut">减 {r}</span>')
    if not parts:
        return ""
    return f'<span class="lvs">{"".join(parts)}</span>'


# ---------------------------------------------------------------- 目标价（手填 / S&P Global 共识价）
# 只对「持仓」「重点关注」里的个股生效；首页每只最多显示一个目标价标签，手填优先于共识价。
#   手填：设置页填的 target，月份取保存当月（target_at）。
#   共识价：S&P Global，取自 stockanalysis.com 个股预测页里内嵌的数据块；
#           每月 1 号、15 号各取一轮（遇周末顺延到下一次运行），结果缓存在 status.json，不新增文件；
#           月份取页面「分析师最近更新日」；失败时保留旧值并把标签标红，不影响红黄绿，也不触发警示。
#           手填了目标价的个股不取共识价；ETF、伯克希尔不取。
# 两者共用同一套警示（目标价 T，现价 P），命中只出紫色（紫色竖条 + 实心紫标签 + 警示栏文字），不改红黄绿：
#   ① 当日跨越：昨收和现价分别在 T 的两侧（含刚好到达）；
#   ② 在 T 的 ±target_gap_pct% 内（按 P/T−1 算）；
#   ③ 空间 T/P−1 超过 target_space_pct%；
#   空间超过 TARGET_ANOMALY_PCT（100%）→ 疑似数据异常（如拆股后共识价没同步），改成红色警示、不出紫色；
#   其余（已远超目标价、离得很远、共识价获取失败）→ 不警示。
CONSENSUS_URL = "https://stockanalysis.com/stocks/{slug}/forecast/"
CONSENSUS_ROBOTS = "https://stockanalysis.com/robots.txt"
CONSENSUS_UA = {"User-Agent": "market-monitor/1.0 (personal dashboard; +https://github.com/nixhuang/market-monitor)"}
CONSENSUS_SOURCE = "stockanalysis.com（S&P Global）"
CONSENSUS_SKIP = {"BRK-B", "BRK-A"}   # 伯克希尔：分析师太少，不抓
CONSENSUS_GAP = 2.0                   # 两次请求的间隔（秒）
CONSENSUS_BUDGET = 7 * 60             # 单次运行最多花这么久；没取完的下次运行接着取
CONSENSUS_BREAK = 3                   # 连续失败这么多次就停手，不再继续敲对方网站
CONSENSUS_GROUPS = ("position", "focus")
CHG_AMP_GROUPS = ("position", "focus")   # 涨跌幅、振幅警示只对这两组生效
TARGET_ANOMALY_PCT = 100.0
_CONS_SPG = re.compile(r'Targets:\{source:"spg",currency:"([A-Z]+)",avg:([0-9.]+)')
_CONS_UPD = re.compile(r'targets:\{low:[0-9.]+,high:[0-9.]+,count:\d+,median:[0-9.]+,average:[0-9.]+,updated:"(\d{4}-\d{2}-\d{2})"')


def _pos_num(v):
    """大于 0 的有限数字（布尔、字符串都不算）→ float；否则 None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return None
    return float(v)


def _month_of(text):
    """'2026-10-07' / '2026-10' → 10；其它 → None。"""
    m = re.match(r"\d{4}-(0[1-9]|1[0-2])(?!\d)", text) if isinstance(text, str) else None
    return int(m.group(1)) if m else None


def consensus_marker(today_iso):
    """今天对应的取数节拍：当月 1 号或 15 号（已到的最近一个）。"""
    return today_iso[:8] + ("15" if int(today_iso[8:10]) >= 15 else "01")


def consensus_slug(sym):
    s = normalize_symbol(sym)
    return s.lower().replace("-", ".") if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", s) else None


def consensus_wanted(sym, cfg, group):
    """该不该自动取共识价：只有持仓/重点关注里的美股个股；手填了目标价的、ETF、指数、期货、伯克希尔都不取。"""
    if group not in CONSENSUS_GROUPS or not sym or not quote_supported(sym) or is_etf(sym):
        return False
    if manual_target(cfg, quiet=True):
        return False
    ident = instrument_identity(sym)
    return (not ident.startswith("^") and not ident.endswith("=F")
            and normalize_symbol(sym) not in CONSENSUS_SKIP and consensus_slug(sym) is not None)


def parse_consensus(text):
    """从预测页文本里取 S&P Global 共识价 → {"avg", "upd"}；页面没有（或不是美元）返回 None。"""
    m = _CONS_SPG.search(text or "")
    if not m or m.group(1) != "USD":
        return None
    avg = _pos_num(float(m.group(2))) if re.fullmatch(r"[0-9]+(\.[0-9]+)?", m.group(2)) else None
    if avg is None:
        return None
    u = _CONS_UPD.search(text)
    return {"avg": avg, "upd": u.group(1) if u else ""}


def consensus_robots_ok(sess):
    """先读对方 robots.txt：读不到（网络/5xx）或明确禁止就不取；4xx 视为没有限制。"""
    try:
        r = sess.get(CONSENSUS_ROBOTS, timeout=15)
    except Exception as e:
        log(f"  · 共识价：robots.txt 读取失败，本轮不取：{str(e)[:60]}")
        return False
    if r.status_code >= 500:
        return False
    if r.status_code != 200:
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(CONSENSUS_UA["User-Agent"], CONSENSUS_URL.format(slug="aapl"))


def fetch_consensus_one(sess, sym):
    """取一只：{"s":"ok","avg","upd"} / {"s":"none"}（页面没有共识，属于无覆盖）/ {"s":"fail","why"}。"""
    try:
        r = sess.get(CONSENSUS_URL.format(slug=consensus_slug(sym)), timeout=25)
    except Exception as e:
        return {"s": "fail", "why": str(e)[:60]}
    if r.status_code == 404:
        return {"s": "none"}
    if r.status_code != 200:
        return {"s": "fail", "why": f"HTTP {r.status_code}"}
    got = parse_consensus(r.text)
    return {"s": "ok", **got} if got else {"s": "none"}


def consensus_due(entry, marker, today_iso):
    """该不该取：没有记录、记录是上一个节拍的、或上次失败且今天还没试过。"""
    if not isinstance(entry, dict) or entry.get("s") not in ("ok", "none", "fail"):
        return True
    if entry["s"] == "fail" and entry.get("try") == today_iso:
        return False
    return entry.get("r") != marker or entry["s"] == "fail"


def _consensus_failed(old_entry, today_iso, why):
    old_entry = old_entry if isinstance(old_entry, dict) else {}
    e = {"s": "fail", "r": old_entry.get("r", ""), "try": today_iso, "why": why}
    for k in ("avg", "upd"):
        if old_entry.get(k) not in (None, ""):
            e[k] = old_entry[k]
    return e


def update_consensus(symbols, prev, today_iso, sess=None, sleep=time.sleep, clock=time.monotonic, force=False):
    """按节拍更新共识价缓存。prev 是上一次 status.json 里的 consensus。返回 (新缓存 data, 统计)。
    不在清单里的代码会被清掉；失败保留旧值；整批都「无覆盖」视为页面改版，按失败处理。"""
    marker = consensus_marker(today_iso)
    old = {}
    if isinstance(prev, dict) and isinstance(prev.get("data"), dict):
        old = {k: v for k, v in prev["data"].items() if isinstance(v, dict)}
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    data = {s: old[s] for s in syms if s in old}
    todo = [s for s in syms if force or consensus_due(old.get(s), marker, today_iso)]
    stat = {"marker": marker, "due": len(todo), "tried": 0, "ok": 0, "none": 0, "fail": 0, "stopped": ""}
    if not todo:
        return data, stat
    sess = sess or requests.Session()
    sess.headers.update(CONSENSUS_UA)
    if not consensus_robots_ok(sess):
        stat["stopped"] = "robots"
        log("  · 共识价：robots.txt 不允许或读取失败，本轮不取，沿用旧值")
        return data, stat
    log(f"  取共识价：{len(todo)} 只到期（节拍 {marker}），每只间隔 {CONSENSUS_GAP:g} 秒…")
    start, streak, fresh = clock(), 0, []
    for i, s in enumerate(todo):
        if clock() - start > CONSENSUS_BUDGET:
            stat["stopped"] = "budget"
            break
        if i:
            sleep(CONSENSUS_GAP)
        res = fetch_consensus_one(sess, s)
        stat["tried"] += 1
        stat[res["s"]] += 1
        if res["s"] == "fail":
            streak += 1
            data[s] = _consensus_failed(old.get(s), today_iso, res.get("why", ""))
            if streak >= CONSENSUS_BREAK:
                stat["stopped"] = "blocked"
                break
        else:
            streak = 0
            data[s] = {**res, "r": marker, "try": today_iso}
            fresh.append(s)
    if stat["tried"] >= 5 and stat["ok"] == 0 and stat["none"] == stat["tried"]:
        # 全部「无覆盖」不可能（AAPL 这类一定有）→ 多半是页面改版，按失败处理，保留旧值并标红
        for s in fresh:
            data[s] = _consensus_failed(old.get(s), today_iso, "页面结构疑似改版")
        stat["fail"], stat["none"], stat["stopped"] = stat["tried"], 0, "layout"
    log(f"  共识价：成功 {stat['ok']} · 无覆盖 {stat['none']} · 失败 {stat['fail']}"
        + (f" · 提前停止（{stat['stopped']}）" if stat["stopped"] else ""))
    return data, stat


def manual_target(entry, quiet=False):
    """设置页手填的目标价 → (价格, 月份 或 None)；没填或写错返回 None（写错会记一条日志）。"""
    if not isinstance(entry, dict):
        return None
    v = entry.get("target")
    price = _pos_num(v)
    if price is None:
        if v not in (None, "") and not quiet:
            log(f"  ! 目标价无效，已忽略：{v!r}")
        return None
    return price, _month_of(entry.get("target_at"))


def resolve_target(cfg, cons):
    """手填优先于共识价。返回 {"kind","price","month","failed"}；都没有返回 None。
    共识价获取失败但有旧值：price 是旧值、failed=True（只显示标签，不参与警示）；没有旧值：price=None。"""
    m = manual_target(cfg)
    if m:
        return {"kind": "manual", "price": m[0], "month": m[1], "failed": False}
    if not isinstance(cons, dict) or cons.get("s") not in ("ok", "fail"):
        return None
    month = _month_of(cons.get("upd")) or _month_of(cons.get("r"))
    if cons["s"] == "ok":
        price = _pos_num(cons.get("avg"))
        return {"kind": "consensus", "price": price, "month": month, "failed": False} if price else None
    return {"kind": "consensus", "price": _pos_num(cons.get("avg")), "month": month, "failed": True}


def _price_text(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def target_label(t):
    return "目标价" if t.get("kind") == "manual" else "共识价"


def target_alerts(price, prev, target, label, space_pct, gap_pct):
    """目标价警示 → (紫色文字列表, 红色文字列表, 空间%)。空间 = 目标价 / 现价 − 1。"""
    space = (target / price - 1) * 100
    tstr = _price_text(target)
    if space > TARGET_ANOMALY_PCT:
        return [], [f"{label} {tstr} 疑似数据异常（高出现价 {space:.0f}%）"], space
    purple = []
    dev = (price / target - 1) * 100
    if prev and prev < target <= price:
        purple.append(f"今日上穿{label} {tstr}")
    elif prev and prev > target >= price:
        purple.append(f"今日跌破{label} {tstr}")
    elif abs(dev) <= gap_pct:
        purple.append(f"在{label} {tstr} ±{gap_pct:g}% 内（偏离 {dev:+.1f}%）")
    if space > space_pct:
        purple.append(f"{label} {tstr}，空间 +{space:.0f}%")
    return purple, [], space


def target_tag(t):
    """价格涨跌幅下面的标签：正常 = 空心紫色胶囊；命中紫色警示 = 实心紫色；共识价取数失败 = 红色。
    手填写「目标 108 · 10月」，共识价写「共识 429 · 10月」。没有目标价返回空串。"""
    if not t:
        return ""
    manual = t.get("kind") == "manual"
    month = f'<span>· {t["month"]}月</span>' if t.get("month") else ""
    if t.get("price") is None:
        body, cls = '<span>共识</span> <span>获取失败</span>', "tgt bad"
    else:
        body = f'<span>{"目标" if manual else "共识"}</span> <span>{_price_text(t["price"])}</span> {month}'
        cls = "tgt bad" if t.get("failed") else ("tgt hit" if t.get("hit") else "tgt")
    return f'<span class="tgs"><span class="lvtag {cls}">{body}</span></span>'


MA_CONFIRM_LOOKBACK = 10   # 50 日线确认穿越：最近多少个交易日内收盘价曾在均线另一侧
MA_CONFIRM_MIN, MA_CONFIRM_MAX, MA_CONFIRM_DEFAULT = 0.5, 3.0, 1.0
MA_LONG_BAND_PCT = 1.0     # 长均线（250 日）：现价在均线 ±1% 内亮黄，超出不报


def ma_confirm_pct(vol_sigma):
    """短均线确认穿越所需的偏离幅度（%）：取这只股平时日波动的一半，限制在 0.5~3；新股没有波动数据按 1。"""
    if vol_sigma is None:
        return MA_CONFIRM_DEFAULT
    return min(MA_CONFIRM_MAX, max(MA_CONFIRM_MIN, 0.5 * vol_sigma))


def ma_confirmed_cross(closes, price, period, th_pct, lookback=MA_CONFIRM_LOOKBACK):
    """短均线「带宽确认」穿越：今天首次收到均线 ±th_pct 之外，昨天还没在带外，且最近 lookback 日内
    收盘价曾在均线另一侧。返回 "up" / "down" / None；数据不够返回 "n/a"（调用方退回旧规则）。
    比「昨收在一侧、今收在另一侧」多一道幅度门槛，价格贴着均线来回晃时不再每次都响。"""
    series = list(closes)
    if not series:
        return "n/a"
    series[-1] = price
    n = len(series)
    if n < period + lookback + 1:
        return "n/a"

    def ma_at(i):
        return sum(series[i - period + 1:i + 1]) / period

    t = n - 1
    th = th_pct / 100.0
    ma_t, ma_y = ma_at(t), ma_at(t - 1)
    if series[t] >= ma_t * (1 + th) and series[t - 1] < ma_y * (1 + th):
        if any(series[j] < ma_at(j) for j in range(t - lookback, t)):
            return "up"
    if series[t] <= ma_t * (1 - th) and series[t - 1] > ma_y * (1 - th):
        if any(series[j] > ma_at(j) for j in range(t - lookback, t)):
            return "down"
    return None


# 涨跌 / 振幅阈值按个股自己的平时波动分档：设置页里的数字是「中档」，低档更灵敏、高档更迟钝。
VOL_LOOKBACK = 60          # 取最近多少个交易日算平时波动
VOL_MIN_RETURNS = 40       # 少于这么多个日收益（新上市）就按中档
VOL_LOW_SIGMA = 1.2        # 日收益标准差 < 此值 → 低档
VOL_HIGH_SIGMA = 2.5       # 日收益标准差 > 此值 → 高档
VOL_MULT = {"low": 0.6, "mid": 1.0, "high": 1.4}


def vol_profile(closes):
    """返回 (档位, 平时日波动% 或 None)。不含最后一根（避免今天的异动把自己的门槛抬高）。"""
    series = [c for c in (closes or [])[:-1] if c]
    series = series[-(VOL_LOOKBACK + 1):]
    rets = [(b / a - 1) * 100 for a, b in zip(series, series[1:]) if a]
    if len(rets) < VOL_MIN_RETURNS:
        return "mid", None
    # 用中位数绝对偏差(MAD)估算，单日财报跳空 / 数据异常不会把整档拉高
    ordered = sorted(rets)
    mid = len(ordered) // 2
    med = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    devs = sorted(abs(r - med) for r in rets)
    dmid = len(devs) // 2
    mad = devs[dmid] if len(devs) % 2 else (devs[dmid - 1] + devs[dmid]) / 2
    sigma = 1.4826 * mad
    if sigma < VOL_LOW_SIGMA:
        return "low", sigma
    if sigma > VOL_HIGH_SIGMA:
        return "high", sigma
    return "mid", sigma


class _Alerts:
    """收集一只标的触发的信号：文字、整体等级、以及每条信号的（类别, 等级）用于统计。"""
    RANK = {"green": 0, "yellow": 1, "red": 2}

    def __init__(self):
        self.signals = []
        self.marks = []
        self.level = "green"

    def add(self, kind, text, lv):
        """lv 为 red / yellow 时抬高整体等级；purple（目标价提示）只记录，不改红黄绿。"""
        self.signals.append(text)
        self.marks.append((kind, lv))
        if self.RANK.get(lv, -1) > self.RANK[self.level]:
            self.level = lv


# 各类信号的统计名称（status.json 的 signal_counts 用）；顺序即展示顺序
SIGNAL_KINDS = (
    ("chg", "涨跌幅"), ("amp", "振幅"), ("vol_ratio", "量比"),
    ("rsi", "RSI"), ("boll", "布林"), ("boll_rsi", "布林+RSI"),
    ("ma_cross", "50日线穿越"), ("ma_band", "250日线贴近"), ("low52", "52周低位"),
    ("add_price", "加仓价"), ("reduce_price", "减仓价"), ("target", "目标价/共识价"),
)


def _analysis_context(sym, cfg, data, group):
    """把一只标的需要的指标一次算好，各条规则只读不算。"""
    closes = data["closes"]
    vols = data["volumes"]
    price = data["price"]
    prev = data["prev_close"]
    x = SimpleNamespace(sym=sym, cfg=cfg, data=data, group=group,
                        closes=closes, price=price, prev=prev)
    x.chg = (price / prev - 1) * 100 if prev else None
    x.rsi = {period: calc_rsi(closes, period) for period in RSI_PERIODS}
    x.dates = data.get("dates") or []
    start52 = 0
    if x.dates:
        cutoff = (datetime.fromisoformat(x.dates[-1]) - timedelta(weeks=52)).date().isoformat()
        start52 = next((i for i, date in enumerate(x.dates) if date >= cutoff), 0)
    high52 = max((data["highs"] or closes)[start52:])
    low52 = min((data["lows"] or closes)[start52:])
    x.dist_high = (price / high52 - 1) * 100
    x.dist_low = (price / low52 - 1) * 100
    x.vol_ratio = (vols[-1] / sma(vols, 20)) if vols and sma(vols, 20) else None
    ma_periods = (int(S["ma_short"]), int(S["ma_long"]))
    x.moving_averages = [(period, sma(closes, period)) for period in dict.fromkeys(ma_periods)]
    x.boll_mid, x.boll_up, x.boll_dn = boll(closes, int(S["boll_n"]), float(S["boll_k"]))
    return x


def _quiet_result(x):
    """quiet 标的（如 SGOV 货币基金）：波动极小，RSI/均线无意义，只在真正异动（>=2%）时才出声，避免噪音。"""
    hot = x.chg is not None and abs(x.chg) >= S["quiet_chg"]
    signals = [f"异动 {x.chg:+.1f}%"] if hot else []
    level = "yellow" if hot else "green"
    detail = {
        "symbol": x.sym, "note": x.cfg.get("note", ""), "price": x.price,
        "chg": x.chg, "rsi": {period: None for period in RSI_PERIODS}, "dist_high": x.dist_high,
        "dist_low": x.dist_low, "vol_ratio": x.vol_ratio,
        "trigger": x.cfg.get("trigger"), "source": x.data.get("source", ""),
        "data_date": (x.data.get("dates") or [""])[-1],
        "group": x.group, "boll_up": x.boll_up, "boll_dn": x.boll_dn,
        "signals": signals, "level": level,
        "signal_marks": [("chg", "yellow")] if hot else [],
    }
    return level, signals, detail


def _apply_vol_tier(x):
    """涨跌 / 振幅阈值：设置页的值是中档，按这只股自己的平时波动缩放；
    涨跌幅 / 振幅警示只对「持仓」和「重点关注」生效，其他分组不报这两类。"""
    x.vol_tier, x.vol_sigma = vol_profile(x.closes)
    mult = VOL_MULT[x.vol_tier]
    x.chg_red, x.chg_yellow = S["chg_red"] * mult, S["chg_yellow"] * mult
    x.amp_red, x.amp_yellow = S["amp_red"] * mult, S["amp_yellow"] * mult
    x.usual = f"（平时约 ±{x.vol_sigma:.1f}%）" if x.vol_sigma is not None else ""
    x.chg_amp_on = x.group in CHG_AMP_GROUPS


def _rule_chg_red(al, x):
    if x.chg_amp_on and x.chg is not None and abs(x.chg) >= x.chg_red:
        al.add("chg", f"异动 {x.chg:+.1f}%{x.usual}", "red")


def _rule_chg_yellow(al, x):
    if x.chg_amp_on and x.chg is not None and x.chg_yellow <= abs(x.chg) < x.chg_red:
        al.add("chg", f"波动 {x.chg:+.1f}%{x.usual}", "yellow")


def _rule_rsi(al, x):
    x.high_hits = sum(v is not None and math.isfinite(v) and v >= S["rsi_high"] for v in x.rsi.values())
    x.low_hits = sum(v is not None and math.isfinite(v) and v <= S["rsi_low"] for v in x.rsi.values())
    for count, tag, boundary in ((x.high_hits, "超买", f"≥{S['rsi_high']:g}"),
                                 (x.low_hits, "超卖", f"≤{S['rsi_low']:g}")):
        if count >= 2:
            values = " / ".join(f"RSI{period}={v:.1f}" if v is not None and math.isfinite(v)
                                else f"RSI{period}=无数据" for period, v in x.rsi.items())
            al.add("rsi", f"RSI {count}条{tag}（{boundary}）：{values}", "red" if count == 3 else "yellow")


def _fresh_52w_low(x):
    """已处在低位区时，判断是不是「刚进入 / 再创新低」。算不出昨天的情况就当作新的。"""
    closes, dates, data, price = x.closes, x.dates, x.data, x.price
    if len(dates) < 2 or len(closes) < 2:
        return True
    try:
        cut_y = (datetime.fromisoformat(dates[-2]) - timedelta(weeks=52)).date().isoformat()
        sy = next((i for i, d_ in enumerate(dates) if d_ >= cut_y), 0)
        lows_all = data["lows"] or closes
        prior_low = min(lows_all[sy:-1])
        yesterday_dist = (closes[-2] / prior_low - 1) * 100
        new_low = min(price, lows_all[-1]) <= prior_low
        return new_low or yesterday_dist > S["near_52w_low_pct"]
    except (ValueError, TypeError, ZeroDivisionError):
        return True


def _rule_52w_low(al, x):
    """52 周新低：刚进入低位区、或再创新低 → 红；已在低位区且没再创新低 → 黄；远离后不报。"""
    if x.dist_low > S["near_52w_low_pct"]:
        return
    if _fresh_52w_low(x):
        al.add("low52", "触及52周新低", "red")
    else:
        al.add("low52", f"仍在52周低位（距低点 +{x.dist_low:.1f}%）", "yellow")


def _rule_add_price(al, x):
    """距加仓触发价。"""
    trig = x.cfg.get("trigger")
    x.trig = trig
    if not trig:
        return
    gap = (x.price / trig - 1) * 100
    tstr = f"{trig:,.2f}"
    if gap <= 0:
        al.add("add_price", f"已跌破加仓价 {tstr}", "red")
    elif gap <= S["trigger_gap_pct"]:
        al.add("add_price", f"距加仓价 {tstr} 还差 {gap:.1f}%", "red")


def _rule_reduce_price(al, x):
    """减仓价：只对「持仓」分组生效，和加仓价对称：
    现价已涨到减仓价以上（不管高出多少）→ 红；还没到但距离不足 x%（按减仓价算）→ 红。"""
    x.reduce = reduce_rule(x.cfg) if x.group == "position" else None
    if not x.reduce:
        return
    rgap = (x.price / x.reduce - 1) * 100
    rstr = f"{x.reduce:,.2f}"
    if rgap >= 0:
        al.add("reduce_price", f"已涨到减仓价 {rstr}（高出 {rgap:.1f}%）", "red")
    elif -rgap <= S["reduce_gap_pct"]:
        al.add("reduce_price", f"距减仓价 {rstr} 还差 {-rgap:.1f}%", "red")


def _rule_target(al, x):
    """目标价（手填优先于 S&P Global 共识价）：只对持仓 / 重点关注；命中只出紫色，不改红黄绿；
    疑似数据异常（空间 > 100%）才升成红色警示；共识价取数失败只标红标签，不参与警示。"""
    x.tgt = resolve_target(x.cfg, x.cfg.get("_consensus")) if x.group in CONSENSUS_GROUPS else None
    tgt = x.tgt
    if not tgt:
        return
    tgt["hit"] = False
    if tgt["price"] and not tgt["failed"]:
        purple, reds, space = target_alerts(x.price, x.prev, tgt["price"], target_label(tgt),
                                            S["target_space_pct"], S["target_gap_pct"])
        for text in reds:
            al.add("target", text, "red")
        for text in purple:
            al.add("target", text, "purple")
        tgt.update(hit=bool(purple), space=space, anomaly=bool(reds))


def _intraday_extremes(x):
    """盘中触及即算：上轨比当日最高价，下轨比当日最低价；盘中重跑时当前价可能比已有极值更极端。"""
    day_high = (x.data.get("highs") or [None])[-1]
    day_low = (x.data.get("lows") or [None])[-1]
    x.touch_up = x.price if day_high is None else max(day_high, x.price)
    x.touch_dn = x.price if day_low is None else min(day_low, x.price)


def _touch_note(price, touch):
    """触及价和展示的现价不一致时，把触及价写出来，
    否则会出现「现价 54.00 却写着突破上轨 59.15」的观感矛盾：其实是当天盘中最高冲到 60.68 穿过去了，收盘又跌回来。"""
    if touch is None or abs(touch - price) < 1e-9:
        return ""
    return f"（盘中触及 {touch:,.2f}）"


def _boll_hits(x):
    """布林上下轨命中情况 → [(还差百分之多少, 文字)]；<=0 表示已经穿过去了。
    另外标出「连续第几日贴近轨道」——远离一天就断，再贴近重新从第一日算。"""
    streak = boll_streak(x.closes, int(S["boll_n"]), float(S["boll_k"]), S["boll_near_pct"],
                         highs=x.data.get("highs"), lows=x.data.get("lows"))
    hits = []
    if x.boll_up is not None:
        gap_up = (x.boll_up - x.touch_up) / x.boll_up * 100
        if gap_up <= S["boll_near_pct"]:
            text = (f"突破布林上轨 {x.boll_up:,.2f}" if gap_up <= 0
                    else f"逼近布林上轨 还差{gap_up:.2f}%")
            hits.append((gap_up, text + _touch_note(x.price, x.touch_up) + streak_tail(streak["up"], "上轨")))
    if x.boll_dn is not None:
        gap_dn = (x.touch_dn - x.boll_dn) / x.boll_dn * 100
        if gap_dn <= S["boll_near_pct"]:
            text = (f"跌破布林下轨 {x.boll_dn:,.2f}" if gap_dn <= 0
                    else f"逼近布林下轨 还差{gap_dn:.2f}%")
            hits.append((gap_dn, text + _touch_note(x.price, x.touch_dn) + streak_tail(streak["dn"], "下轨")))
    return hits


def _rule_boll(al, x):
    """布林带：逼近即算，盘中不用等收盘真的穿过去；逼近 / 盘中触及 / 收盘穿出一律黄。
    上下轨都命中时只留更贴近的那条：SGOV 这类短债/货币 ETF 波动极小，布林带宽不到 1%，
    上下轨会同时命中，两条一起显示等于自相矛盾（既逼近上轨又逼近下轨）。"""
    hits = _boll_hits(x)
    if not hits:
        return
    best = min(hits, key=lambda h: h[0])
    al.add("boll", best[1], "yellow")
    if max(x.high_hits, x.low_hits) >= 2:
        al.add("boll_rsi", "布林 + RSI 双重信号 → 红", "red")


def _rule_vol_ratio(al, x):
    if x.vol_ratio and x.vol_ratio >= S["vol_ratio"]:
        al.add("vol_ratio", f"量比 {x.vol_ratio:.1f}x", "yellow")


def _rule_amplitude(al, x):
    """日内振幅：涨跌幅只看「收盘 vs 昨收」，盘中冲高又回落、最后收平的票不会响，这条专门抓它。
    用当日最高/最低（盘中重跑时再和当前价取极值）除以昨收。"""
    x.amp = None
    if x.touch_up is None or x.touch_dn is None or not x.prev:
        return
    x.amp = (x.touch_up - x.touch_dn) / x.prev * 100
    if not x.chg_amp_on:
        return
    text = f"振幅 {x.amp:.1f}%（{x.touch_dn:,.2f}~{x.touch_up:,.2f}）"
    if x.amp >= x.amp_red:
        al.add("amp", text, "red")
    elif x.amp >= x.amp_yellow:
        al.add("amp", text, "yellow")


def _rule_moving_averages(al, x):
    """均线：周期最短的一条（50 日）要「确认穿越」才报；周期最长的一条（250 日）只看是否贴近（±MA_LONG_BAND_PCT%）；
    只设了一条均线时走最朴素的穿越规则。"""
    closes, price, mas = x.closes, x.price, x.moving_averages
    if len(closes) < 2:
        return
    prev_c = closes[-2]
    for period, ma_val in mas:
        ma_name = f"{period}日"
        if not ma_val:
            continue
        weak = len(mas) > 1 and period == min(p_ for p_, _ in mas)
        strong = len(mas) > 1 and period == max(p_ for p_, _ in mas)
        if strong:
            gap_ma = (price / ma_val - 1) * 100
            if abs(gap_ma) <= MA_LONG_BAND_PCT + 1e-9:
                al.add("ma_band", f"贴近{ma_name}均线 {gap_ma:+.1f}%", "yellow")
            continue
        confirmed = ma_confirmed_cross(closes, price, period, ma_confirm_pct(x.vol_sigma)) if weak else "n/a"
        if confirmed == "up" or (confirmed == "n/a" and prev_c < ma_val <= price):
            al.add("ma_cross", f"上穿{ma_name}均线", "yellow")
        elif confirmed == "down" or (confirmed == "n/a" and prev_c > ma_val >= price):
            al.add("ma_cross", f"跌破{ma_name}均线", "yellow")


def _analysis_detail(x, al):
    return {
        "symbol": x.sym,
        "note": x.cfg.get("note", ""),
        "price": x.price,
        "chg": x.chg,
        "rsi": x.rsi,
        "dist_high": x.dist_high,
        "dist_low": x.dist_low,
        "vol_ratio": x.vol_ratio,
        "amp": x.amp,
        "vol_tier": x.vol_tier,
        "vol_sigma": x.vol_sigma,
        "trigger": x.trig,
        "reduce": x.reduce,
        "target": x.tgt,
        "purple": bool(x.tgt and x.tgt.get("hit")),
        "source": x.data.get("source", ""),
        "data_date": (x.data.get("dates") or [""])[-1],
        "realtime": bool(x.data.get("realtime")),
        "rt_ts": x.data.get("rt_ts", ""),
        "group": x.group,
        "boll_up": x.boll_up,
        "boll_dn": x.boll_dn,
        # ETF 标签：静态表命中，或行情源明确给出 assetclass=etf
        "etf": is_etf(x.sym) or x.data.get("asset") == "etf",
        "signals": al.signals,
        "signal_marks": al.marks,
        "level": al.level,
    }


def analyze_symbol(sym, cfg, data, group="technology"):
    """返回等级、信号和详情，分组定义见 groups.json。
    规则按「红 → 加减仓价/目标价 → 布林 → 黄」的顺序依次跑，顺序也是页面上警示文字的顺序。"""
    x = _analysis_context(sym, cfg, data, group)
    if cfg.get("quiet"):
        return _quiet_result(x)
    al = _Alerts()
    _apply_vol_tier(x)
    _intraday_extremes(x)
    _rule_chg_red(al, x)
    _rule_rsi(al, x)
    _rule_52w_low(al, x)
    _rule_add_price(al, x)
    _rule_reduce_price(al, x)
    _rule_target(al, x)
    _rule_boll(al, x)
    _rule_chg_yellow(al, x)
    _rule_vol_ratio(al, x)
    _rule_amplitude(al, x)
    _rule_moving_averages(al, x)
    return al.level, al.signals, _analysis_detail(x, al)


# ---------------------------------------------------------------- HTML

def fmt(v, unit="", nd=2):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}{unit}"


RUN_JS = '<script src="./run-status.js?v=20261009-1"></script>'


def config_hash(filename):
    """与GitHub Contents API content.sha一致，用于精确验证哪版配置已生效。"""
    with open(os.path.join(BASE, filename), "rb") as f:
        raw = f.read()
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def beijing_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        return ""


def market_data_time(snapshot):
    dates = snapshot.get("actual_dates") or {}
    latest = dates.get("max") or dates.get("min")
    if not latest:
        finished = beijing_iso(snapshot.get("finished_at_bj") or snapshot.get("finished_at"))
        if snapshot.get("mode") == "manual_or_config" and finished and not plan_run("local", datetime.fromisoformat(finished))["closed_only"]:
            missing = snapshot.get("summary", {}).get("missing_prices", 0)
            return f'{finished[:19].replace("T", " ")}（盘中运行，北京时间 · 未取得行情） · 无数据 {missing} 只'
        return "未取得行情"
    earliest = dates.get("min") or latest
    date_text = f"{earliest}～{latest}" if earliest != latest else latest
    finished = beijing_iso(snapshot.get("finished_at_bj") or snapshot.get("finished_at"))
    if snapshot.get("mode") == "manual_or_config" and finished:
        when = datetime.fromisoformat(finished)
        plan = plan_run("local", when)
        if not plan["closed_only"]:
            counts = snapshot.get("summary", {})
            detail = (f" · 当日价 {counts['today_prices']} 只 / 较早日线 {counts['prior_prices']} 只 / "
                      f"无数据 {counts['missing_prices']} 只" if "today_prices" in counts else "")
            if latest == plan["target"]:
                return finished[:19].replace("T", " ") + "（盘中快照，北京时间）" + detail
            return finished[:19].replace("T", " ") + f"（盘中运行，北京时间 · 仍为 {date_text} 日线）" + detail
    return date_text + " 收盘（美东交易日）"


def signal_counts(items):
    """各类信号的触发数量，写进 status.json，方便看「黄灯到底是被哪条规则点亮的」。
    red / yellow：这一类信号以红 / 黄级别触发的标的数；purple：目标价提示数；
    sole_yellow：整只标的是黄灯、且黄灯只由这一类信号造成的数量（删掉这条规则就会少几盏黄灯）。"""
    out = {kind: {"label": label, "red": 0, "yellow": 0, "purple": 0, "sole_yellow": 0}
           for kind, label in SIGNAL_KINDS}
    for d in items:
        marks = d.get("signal_marks") or []
        for kind in {k for k, _ in marks}:
            if kind not in out:
                continue
            for lv in ("red", "yellow", "purple"):
                if any(k == kind and m == lv for k, m in marks):
                    out[kind][lv] += 1
        if d.get("level") == "yellow":
            yellow_kinds = {k for k, m in marks if m == "yellow"}
            if len(yellow_kinds) == 1 and next(iter(yellow_kinds)) in out:
                out[next(iter(yellow_kinds))]["sole_yellow"] += 1
    return out


def build_snapshot(macro, items, cfg, group_counts=None):
    dates = sorted({d.get("data_date") for d in items if d.get("data_date") and not d.get("reference_only")})
    references = {normalize_symbol(d["symbol"]): d["data_date"] for d in items
                  if d.get("reference_only") and d.get("data_date")}
    unsupported = [normalize_symbol(d["symbol"]) for d in items if not quote_supported(d["symbol"])]
    missing = [normalize_symbol(d["symbol"]) for d in items
               if d.get("price") is None and quote_supported(d["symbol"])]
    stale = [normalize_symbol(d["symbol"]) for d in items
             if not d.get("reference_only") and d.get("data_date") and TARGET_DATE and d["data_date"] < TARGET_DATE]
    monitoring = group_monitoring(cfg)
    positions = cfg.get("positions", {}) if monitoring["positions"] else {}
    counts = group_counts if group_counts is not None else grouped_universe(cfg)[1]
    dca = global_dca(TARGET_DATE)
    snapshot = {
        "dca_reminder": dca,
        "run_id": str(os.environ.get("GITHUB_RUN_ID") or "local-" + NOW.strftime("%Y%m%d%H%M%S")),
        "request_id": os.environ.get("MM_REQUEST_ID", ""),
        "source_sha": os.environ.get("GITHUB_SHA", ""),
        "started_at": os.environ.get("MM_STARTED_AT", NOW.isoformat()),
        "started_at_bj": beijing_iso(os.environ.get("MM_STARTED_AT", NOW.isoformat())),
        "finished_at": datetime.now(TZ).isoformat(),
        "finished_at_bj": datetime.now(TZ).isoformat(timespec="seconds"),
        "event": EVENT,
        "target_trade_date": TARGET_DATE,
        "mode": "closed" if CLOSED_ONLY else "manual_or_config",
        "schedule": "美东周一至周五20:15；北京时间夏季次日08:15、冬季次日09:15",
        "config_files": {f: config_hash(f) for f in ("holdings.json", "settings.json")},
        "effective_settings": dict(S),
        "groups": GROUPS,
        "group_monitoring": monitoring,
        "registered_counts": {g["key"]: len(cfg.get(g["key"]) or {}) for g in GROUPS},
        "list_counts": {**counts,
                        "triggers": sum(bool(c.get("trigger")) for c in positions.values()),
                        "reduces": sum(bool(reduce_rule(c)) for c in positions.values()),
                        "dca": 1 if dca else 0},
        "summary": {**{lv: sum(d["level"] == lv for d in items) for lv in ("red", "yellow", "green", "gray")},
                    "total": len(items), "macro_ok": sum(bool(m.get("ok")) for m in macro.values()),
                    "stale_symbols": stale, "missing_symbols": missing,
                    "unsupported_symbols": unsupported, "reference_dates": references,
                    "today_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") == TARGET_DATE for d in items),
                    "prior_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") != TARGET_DATE for d in items),
                    "missing_prices": sum(d.get("price") is None for d in items)},
        "signal_counts": signal_counts(items),
        "actual_dates": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
        "macro_dates": {k: m.get("date", "") for k, m in macro.items()},
        "treasury_reference": {key: macro.get("ust10", {}).get(key) for key in
                               ("ok", "value", "date", "source", "unit", "lagging", "reason", "quote_name", "observed_date", "observed_value", "delta_month")},
        "coverage": "数据源日线；完整23小时夜盘/日盘覆盖尚未验证。自动日报不并入实时价；手动常规盘中按当前报价重算。",
    }
    snapshot["data_time_text"] = market_data_time(snapshot)
    snapshot["market_risk"] = market_risk_summary(macro)
    snapshot["risk_quotes"] = {key: {field: (macro.get(key) or {}).get(field) for field in
                             ('ok', 'value', 'date', 'source', 'drawdown')} for key in ('vix', 'sp500')}
    return snapshot


PAGE_CSS = """:root{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --text:#e6e8ec; --dim:#8b93a1;
  --green:#3fb950; --yellow:#d29922; --red:#f85149; --up:#3fb950; --down:#f85149;
  --blue:#2f6bd8; --purple:#a371f7;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
  padding:env(safe-area-inset-top) 14px 40px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:19px;margin:18px 0 4px;font-weight:600}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:6px 4px;margin-bottom:14px;overflow:hidden}
h2{font-size:13px;color:var(--dim);font-weight:600;margin:10px 12px 8px;letter-spacing:.3px}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:9px 10px;border-top:1px solid var(--line);vertical-align:middle}
tr:first-child td{border-top:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.dim{color:var(--dim);font-size:12px}
.sym{font-weight:600}
.note{color:var(--dim);font-weight:400;font-size:11px;margin-left:6px}
.tag{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:4px;
  font-size:10px;font-weight:600;letter-spacing:.3px;vertical-align:1px;
  color:#8fb8f0;background:rgba(107,163,240,.14);border:1px solid rgba(107,163,240,.32)}
.tag.f-red{color:#ff9b95;background:rgba(248,81,73,.16);border-color:rgba(248,81,73,.42)}
.tag.f-yellow{color:#f0c674;background:rgba(210,153,34,.16);border-color:rgba(210,153,34,.42)}
.tag.quote-note{color:var(--dim);background:rgba(139,147,161,.1);border-color:var(--line);font-weight:400}
.lvs{display:block;margin:4px 0 0;line-height:1.5}
.lvtag{display:inline-block;margin:0 4px 2px 0;padding:0 7px;border-radius:999px;border:1px solid;
  background:transparent;font-size:10px;font-weight:400;line-height:16px;white-space:nowrap;font-variant-numeric:tabular-nums}
.lvtag.add{color:#ffa94d;border-color:rgba(255,169,77,.6)}
.lvtag.cut{color:#4dd0e1;border-color:rgba(77,208,225,.6)}
.tgs{display:block;margin:3px 0 0;line-height:1.4;white-space:normal}
.tgs .lvtag{margin:0;padding:1px 5px;border-radius:4px;line-height:1.35;white-space:normal;text-align:center}
.tgs .lvtag span{display:inline-block;white-space:nowrap}
.lvtag.tgt{color:#c4a3ff;border-color:rgba(163,113,247,.65)}
.lvtag.tgt.hit{color:#fff;background:#8957e5;border-color:#8957e5}
.lvtag.tgt.bad{color:#ff9b95;border-color:rgba(248,81,73,.7)}
.tnote,.sig .tp{color:#c4a3ff}
.fund-detail{margin-top:6px}
.fund-detail summary{margin-left:0;padding:7px 9px;cursor:pointer;list-style:none;touch-action:manipulation}
.fund-detail summary::-webkit-details-marker{display:none}
.fund-detail summary::after{content:"";display:inline-block;margin-left:6px;
  border:4px solid transparent;border-top-color:currentColor;transform:translateY(2px)}
.fund-detail[open] summary::after{transform:translateY(-2px) rotate(180deg)}
.fund-detail summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:3px}
.fund-body{margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:6px;
  font-size:12px;font-weight:400;line-height:1.6;overflow-wrap:anywhere}
.fund-body ul{margin:4px 0 0;padding-left:16px}
.sig{font-size:12.5px;color:var(--text);overflow-wrap:anywhere}
.earn{display:block;margin:0;color:#8fb8f0;font-size:11.5px;font-weight:400;line-height:1.5}
.earn .unit{display:block;color:var(--dim);font-size:11px}
.earn.soon{color:#f0c674;font-weight:600}
table.stk{table-layout:fixed}
.c-sym{width:19%} .c-earn{width:29%} .c-px{width:13%}
.earn-cell{vertical-align:middle;overflow-wrap:anywhere}
table.stk tr td:first-child{padding-left:14px}
.stk td.sym{overflow-wrap:anywhere}
.stk th{font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.3px;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
.stk th:first-child{padding-left:14px}
.stk th.h-earn,.earn-cell{text-align:center}
.px{white-space:nowrap}
.px-price,.px-chg{display:block}
.px-chg{font-size:12.5px}
@media (max-width:600px){
  /* 手机：保持电脑端同样的四栏（名称｜财报｜价格涨跌幅｜警示），每栏在自己的区域内换行，不再拆成上下三层 */
  .c-sym{width:22%} .c-earn{width:23%} .c-px{width:19%}
  .stk td{padding:8px 5px}
  table.stk tr td:first-child,.stk th:first-child{padding-left:11px}
  .stk th{padding:7px 5px;font-size:12px}
  .stk th.h-earn,.earn-cell{text-align:left}
  .stk td.sym{font-size:13.5px}
  .stk td.sym .note{font-size:10.5px;line-height:1.35}
  .stk .earn{font-size:10.5px;line-height:1.4}
  .stk .earn .unit{font-size:10px;line-height:1.35}
  .stk .px-price{font-size:13.5px}
  .stk .px-chg{font-size:11.5px} .stk .tgs .lvtag{padding:1px 3px;font-size:9.5px;max-width:100%;box-sizing:border-box} .stk .tgs .lvtag span{white-space:normal;overflow-wrap:anywhere}
  .stk td.sig{font-size:11.5px;line-height:1.45}
  .stk .fund-detail summary{padding:6px 7px}
}
.fund-context{margin-top:6px;color:var(--dim)}
.up{color:var(--up)} .down{color:var(--down)}
tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
tr.yellow td:first-child{box-shadow:inset 4px 0 0 var(--yellow);padding-left:12px}
tr.green td:first-child{box-shadow:inset 2px 0 0 var(--green)}
tr.gray td{color:var(--dim)}
.group-card tr.red td:first-child{box-shadow:inset 6px 0 0 var(--red);padding-left:14px}
.group-card tr.pur td.sig{box-shadow:inset -2px 0 0 var(--purple);padding-right:12px}
#macroCard tr.mh td{padding:5px 10px;font-size:11.5px;white-space:nowrap} #macroCard tr.mh td:nth-child(3){padding-left:2px}
.macro-help{margin:8px 12px;color:var(--dim);font-size:12px;line-height:1.7}
.macro-help>summary{cursor:pointer;color:#8fb8f0;padding:8px 0;touch-action:manipulation}
.macro-help p{margin:6px 0}
.macro-help>summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:2px}
.unit{display:block;font-size:10px;color:var(--dim);font-weight:400}
.foot{color:var(--dim);font-size:11.5px;text-align:center;margin-top:22px;line-height:1.7}
.quiet{color:var(--dim);font-size:12px;padding:8px 12px 12px}
.warn{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
  color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:13px}
.acts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.acts button{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}
.acts button:disabled{opacity:.5;cursor:not-allowed}
#runMsg{font-size:12px;color:var(--dim);flex:1;min-width:180px;line-height:1.5;overflow-wrap:anywhere}
#runMsg.ok{color:var(--green)}
#runMsg.err{color:var(--red)}
.runbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}
.runbar button{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}
.runbar button:disabled{opacity:.5;cursor:not-allowed}
.runbar button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
.runbar a.btnlink{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;text-decoration:none;display:inline-block}
.statusrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin:-4px 0 14px;font-size:12.5px;line-height:1.5}
.check-result{padding:10px 12px;margin:0 0 14px;border:1px solid var(--line);border-radius:8px;
  font-size:12px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text)}
.check-result[data-phase="bad"]{border-color:var(--red)}
.check-result[data-phase="busy"]{border-color:var(--yellow)}
.check-result[hidden]{display:none}
.group-card>summary.grp,.macro-card>summary.grp{display:flex;align-items:center;gap:10px;cursor:pointer;
  padding:12px;font-size:14px;font-weight:600;list-style:none;touch-action:manipulation}
.group-card>summary::-webkit-details-marker,.macro-card>summary::-webkit-details-marker{display:none}
.group-card>summary::before,.macro-card>summary::before{content:"›";color:var(--dim);font-size:20px;line-height:1}
.group-card[open]>summary::before,.macro-card[open]>summary::before{transform:rotate(90deg)}
.group-card[open]>summary,.macro-card[open]>summary{border-bottom:1px solid var(--line)}
.group-stats{margin-left:auto;color:var(--dim);font-size:11px;font-weight:400}
.group-card>summary .group-stats{margin-left:6px;display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-start;text-align:left}
.stat-count{display:inline-flex;align-items:center;gap:5px;font-variant-numeric:tabular-nums}
.rs-spy{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--dim)}
.rs-spy.up{color:var(--up)} .rs-spy.down{color:var(--down)}
.stat-dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex:none}
.stat-dot.red{background:var(--red)} .stat-dot.yellow{background:var(--yellow)}
.stat-dot.gray{background:var(--dim)} .stat-dot.green{background:var(--green)} .stat-dot.purple{background:var(--purple)}
.market-risk-badge{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.market-risk-badge[data-level="red"]{color:var(--red)}
.market-risk-badge[data-level="yellow"]{color:var(--yellow)}
.market-risk-badge[data-level="green"]{color:var(--green)}
.macro-card>summary .group-stats{margin-left:6px;text-align:left}
.quiet-list{margin:8px 0}
.quiet-list>summary{padding:8px 12px;color:var(--dim);font-size:12px;cursor:pointer}
.sym .note{display:block;margin:3px 0 0;overflow-wrap:anywhere}
.group-card summary:focus-visible,.macro-card summary:focus-visible{outline:2px solid #6ba3f0;outline-offset:-2px}
.runlight{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--dim);line-height:1.4}
.runlight .dot{width:9px;height:9px;border-radius:50%;background:#4b5563;flex:none}
.runlight[data-phase="busy"]{color:var(--yellow)}
.runlight[data-phase="busy"] .dot{background:var(--yellow);box-shadow:0 0 0 3px rgba(210,153,34,.18)}
.runlight[data-phase="ok"]{color:var(--green)}
.runlight[data-phase="ok"] .dot{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.18)}
.runlight[data-phase="bad"]{color:var(--red)}
.runlight[data-phase="bad"] .dot{background:var(--red);box-shadow:0 0 0 3px rgba(248,81,73,.18)}
.dca{background:rgba(139,147,161,.12);border:1px solid var(--line);border-radius:10px;
  padding:10px 14px;margin-bottom:14px;font-size:13px;color:var(--dim)}
.dca b{color:var(--text)}
.dca.on{background:rgba(210,153,34,.14);border-color:rgba(210,153,34,.4);color:var(--yellow)}
.dca.on b{color:var(--yellow)}
"""


def _macro_rows(macro, risk):
    """市场风险参考表的各行（五项指标 + 10Y 美债参考行）。"""
    rows_macro = []
    for key in RISK_INDICATORS:
        it = macro.get(key, {})
        if risk["levels"][key] == "gray":
            label = html_lib.escape(str(it.get("name") or RISK_LABELS[key]))
            rows_macro.append(f'<tr class="gray"><td>{label}</td><td colspan="3">无数据或已过期</td></tr>')
            continue
        lv, txt = macro_status(key, it)
        if key == "sp500":
            val = f'{it["value"]:,.2f}<span class="unit">较近一年高点回撤 {it["drawdown"]:.1f}%</span>' if it.get("drawdown") is not None else "—"
        elif key == "hy_oas":
            val = f'{it["value"]:.0f} bp'
        elif key == "vix":
            val = f'{it["value"]:.2f}'
        elif key == "ust10":
            val = f'{it["value"]:.2f}%'
        elif key == "curve":
            val = f'{it["value"]:.2f}<span class="unit">个百分点</span>'
        elif key == "breadth":
            val = (f'{it["value"]:.1f}%<span class="unit">的成分股站上200日线（长期趋势）</span>'
                   f'<span class="unit">站上50日线（短期）：{it["pct50"]:.1f}%</span>')
        elif key == "nfci":
            val = f'{it["value"]:+.2f}'
        else:
            val = f'{it["value"]:.2f}'

        dw = it.get("delta_week")
        dw_txt = f'{dw:+.0f}' if (key == "hy_oas" and dw is not None) else "—"
        label = html_lib.escape(str(it.get("name") or RISK_LABELS[key]))
        label += f'<span class="unit">截至 {html_lib.escape(it["date"])}</span>'
        if it.get("source"):
            label += f'<span class="unit">{html_lib.escape(str(it["source"]))}</span>'
        rows_macro.append(
            f'<tr class="{lv}"><td>{label}</td><td class="num">{val}</td>'
            f'<td class="num dim">{dw_txt}</td><td>{txt}</td></tr>'
        )

    # 参考行：10Y 美债一个月变化。只看利率冲击，不进 RISK_INDICATORS，不影响综合灯和有效指标数
    ut = macro.get("ust10") or {}
    if ut.get("ok") and ut.get("delta_month") is not None:
        ulv, utxt = macro_status("ust10", ut)
        uw = ut.get("delta_week")
        uw_txt = f'{uw * 100:+.0f}' if uw is not None else "—"
        ulabel = (f'{html_lib.escape(str(ut.get("name") or "10Y美债"))}'
                  f'<span class="unit">参考 · 不计入综合灯</span>'
                  f'<span class="unit">截至 {html_lib.escape(ut["date"])}</span>')
        rows_macro.append(
            f'<tr class="{ulv}" id="ust10Ref"><td>{ulabel}</td><td class="num">{ut["value"]:.2f}%</td>'
            f'<td class="num dim">{uw_txt}</td><td>{utxt}</td></tr>')
    else:
        rows_macro.append('<tr class="gray" id="ust10Ref"><td>10Y美债<span class="unit">参考 · 不计入综合灯</span></td>'
                          '<td colspan="3">无数据或已过期</td></tr>')
    return "".join(rows_macro)


_LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2, "gray": 3}


def _fund_alert(d):
    """行情没异动但基本面亮了警示的，要上表并往前排，否则徽章永远藏在「无异动 N 只」里看不见。"""
    return (d.get("fund") or {}).get("level") in ("red", "yellow")


def _sort_key(d):
    base = _LEVEL_ORDER[d["level"]]
    if _fund_alert(d) and base > 1:
        base = 1.5  # 夹在黄和绿之间
    return (base, -(abs(d["chg"]) if d["chg"] else 0))


def _dedupe_items(items):
    """同一只标的只留一行：按分组优先级（风险参考／持仓优先，其次重点关注，最后其他）保留第一次出现的。"""
    priority = {g['item_group']: i for i, g in enumerate(GROUPS)}
    seen = set(RISK_IDENTITIES)
    unique_items = []
    for item in sorted(items, key=lambda d: priority.get(d.get('group'), len(GROUPS))):
        identity = instrument_identity(item['symbol'])
        if identity not in seen:
            seen.add(identity)
            unique_items.append(item)
    return sorted(unique_items, key=_sort_key)


def _row_tags(d, snapshot):
    """名称旁的小标签：ETF、无数据、参考值、日线日期。"""
    # ETF / 基金类标一个小标签，和个股区分开；抓不到数据时用静态表兜底判断
    tag = '<span class="tag">ETF</span>' if (d.get("etf") or is_etf(d["symbol"])) else ""
    if d.get("price") is None:
        tag += '<span class="tag quote-note">无数据</span>'
    elif d.get("reference_only"):
        date = html_lib.escape(str(d.get("data_date") or "日期未知"))
        tag += f'<span class="tag quote-note">参考值 · 截至 {date}</span>'
        if d.get("source") and d["source"] != "市场参考":
            tag += f'<span class="unit">来源 {html_lib.escape(str(d["source"]))} · 收益率百分比</span>'
        if d.get("reference_lagging"):
            tag += '<span class="unit">来源尚未提供目标日有效值，保留最近参考值</span>'
    elif snapshot.get("mode") == "manual_or_config" and d.get("data_date") != snapshot.get("target_trade_date"):
        date = html_lib.escape(str(d.get("data_date") or "日期未知"))
        tag += f'<span class="tag quote-note">日线 {date} 收盘</span>'
    return tag


def _fund_detail_html(d):
    """基本面警示的折叠详情；没有红黄警示就返回空串。"""
    fund = d.get("fund") or {}
    if fund.get("level") not in ("red", "yellow") or not fund.get("hits"):
        return ""
    period = html_lib.escape(str(fund.get("period") or "未提供"))
    hits = "".join(f'<li>{html_lib.escape(str(hit))}</li>' for hit in fund["hits"])
    fcls = "tag f-red" if fund["level"] == "red" else "tag f-yellow"
    label = html_lib.escape(f"查看 {normalize_symbol(d['symbol'])} 基本面详情")
    compare = html_lib.escape(str(fund.get("compare") or ""))
    compare_html = (f'<div>对比：{compare}（接口只有最近4个季度，不是去年同期）</div>'
                    if compare else "")
    context = "；".join(str(x) for x in fund.get("context") or [])
    context_html = (f'<div class="fund-context">参考：{html_lib.escape(context)}。'
                    f'单项比率变化不等于主营恶化，请结合行业和现金流判断</div>' if context else "")
    return (f'<details class="fund-detail"><summary class="{fcls}" aria-label="{label}">'
            f'基本面</summary><div class="fund-body"><div>{period} 报告期</div>{compare_html}'
            f'<ul>{hits}</ul>{context_html}</div></details>')


def _earnings_html(d):
    earn = earnings_label(d.get("earnings"))
    if not earn:
        return ""
    soon = " soon" if earnings_soon(d.get("earnings")) else ""
    return (f'<span class="earn{soon}">'
            f'{html_lib.escape(earn[0])}<span class="unit">{html_lib.escape(earn[1])}</span></span>')


def _signals_html(d):
    """警示文字：目标价 / 共识价相关的一律用紫色，并统一排在最后；手填目标价没触发时也写出空间。"""
    is_tp = lambda x: "共识价" in x or "目标价" in x
    parts = ([x for x in d["signals"] if not is_tp(x)]
             + [f'<span class="tp">{x}</span>' for x in d["signals"] if is_tp(x)])
    sig = " · ".join(parts) if parts else "—"
    tg = d.get("target") or {}
    if tg.get("kind") == "manual" and tg.get("space") is not None and not tg.get("hit") and not tg.get("anomaly"):
        sig = ("" if sig == "—" else sig + " · ") + f'<span class="tnote">目标价 {_price_text(tg["price"])}，空间 {tg["space"]:+.0f}%</span>'
    return sig


def _stock_row(d, snapshot):
    cls = d["level"] + (" pur" if d.get("purple") else "")
    chg_cls = "up" if (d["chg"] or 0) > 0 else (
        "down" if (d["chg"] or 0) < 0 else "")
    chg_txt = f'{d["chg"]:+.2f}%' if d["chg"] is not None else "—"
    note = f'<span class="note">{html_lib.escape(str(d["note"]))}</span>' if d["note"] else ""
    lv_html = level_note(d.get("trigger"), d.get("reduce"))
    tg_html = target_tag(d.get("target") or {})
    return (
        f'<tr class="{cls}"><td class="sym">{normalize_symbol(d["symbol"])}{note}{lv_html}{_row_tags(d, snapshot)}</td>'
        f'<td class="earn-cell">{_earnings_html(d)}</td>'
        f'<td class="num px"><span class="px-price">{fmt(d["price"])}{html_lib.escape(str(d.get("unit") or ""))}</span>'
        f'<span class="px-chg {chg_cls}">{chg_txt}</span>{tg_html}</td>'
        f'<td class="sig">{_signals_html(d)}{_fund_detail_html(d)}</td></tr>'
    )


def _stock_rows(lst, snapshot):
    out = "".join(_stock_row(d, snapshot) for d in lst)
    return out or '<tr class="gray"><td colspan="4">今晚无异动，不用盯</td></tr>'


def _group_stats_html(g, group, items_by_symbol, enabled):
    """分组标题右侧的小圆点计数：红、黄、灰、紫，板块组再跟「相对标普500的强弱」。"""
    stats = ''.join(
        f'<span class="stat-count" aria-label="{label} {count} 项">'
        f'<i class="stat-dot {lv}" aria-hidden="true"></i>{count}</span>'
        for lv, label in (("red", "红色警示"), ("yellow", "黄色警示"), ("gray", "无数据"))
        if (count := sum(d["level"] == lv for d in g)))
    # 紫色（目标价 / 共识价警示）：条数跟在红黄灯后面；一条都没有就不显示
    if (n_pur := sum(bool(d.get("purple")) for d in g)):
        stats += (f'<span class="stat-count" aria-label="紫色警示 {n_pur} 项">'
                  f'<i class="stat-dot purple" aria-hidden="true"></i>{n_pur}</span>')
    # 板块组：红黄灯后面跟「板块 ETF 近 20 日相对标普500的强弱」，折叠时也能看到
    rs = (items_by_symbol.get(group["sector_etf"]) or {}).get("rs_spy") if group.get("sector_etf") else None
    if rs:
        diff = rs["diff"]
        rs_cls = "up" if diff >= RS_NEUTRAL_PP else "down" if diff <= -RS_NEUTRAL_PP else ""
        bn = rs.get("bench_name") or RS_BENCH_NAME
        rs_tip = html_lib.escape(
            f'{group["sector_etf"]} {rs["days"]}日 {rs["etf"]:+.1f}% − {bn} {rs["bench"]:+.1f}% = {diff:+.1f} 个百分点'
            f'（{rs["start"]} → {rs["end"]}）')
        stats += (f'<span class="rs-spy {rs_cls}" aria-label="{rs_tip}">'
                  f'{rs["days"]}日相对{html_lib.escape(bn)} {diff:+.1f}%</span>')
    if not enabled:
        note = "仅板块ETF" if g else "监测关闭"
        stats = f'<span class="monitor-note">{note}</span>' + stats
    return stats


def _sort_group_items(g, group):
    etf_row = lambda d: bool(d.get("etf")) or is_etf(d["symbol"])
    if group["item_group"] in ("position", "focus"):
        # 持仓 / 重点关注：同一预警级别里 ETF 排在个股后面（稳定排序，级别和涨跌幅顺序不变）
        return sorted(g, key=lambda d: (_sort_key(d)[0], etf_row(d)))
    if group.get("sector_etf"):
        # 行业分类：本行业板块 ETF（XLK、XLV…）只在同一预警级别内排第一；无警示（绿灯）时照常折叠
        return sorted(g, key=lambda d: (_sort_key(d)[0],
                                        normalize_symbol(d["symbol"]) != group["sector_etf"],
                                        _sort_key(d)[1]))
    return g


def _group_card(group, g, items_by_symbol, snapshot):
    """一个分组的折叠卡片：展开的警示表 + 「无异动 N 只」折叠表。"""
    monitoring = snapshot.get("group_monitoring", {})
    registered = snapshot.get("registered_counts", {})
    g = _sort_group_items(g, group)
    shown = [d for d in g if d["level"] in ("red", "yellow", "gray") or _fund_alert(d) or d.get("reference_only")
             or d.get("purple")]
    quiet = [d for d in g if d["level"] == "green" and not _fund_alert(d) and not d.get("reference_only")
             and not d.get("purple")]
    parts = []
    enabled = monitoring.get(group["key"], True)
    if not enabled:
        note = f"行业监测关闭 · {group['sector_etf']} 仍监测（仅限原组已有标的）" if group.get("sector_etf") else "分组监测已关闭"
        parts.append(f'<div class="quiet">{note} · 清单保留，普通标的暂停抓取和报警</div>')
    if shown:
        parts.append(f'<table class="stk">{STK_COLS}{STK_HEAD}{_stock_rows(shown, snapshot)}</table>')
    if quiet:
        parts.append(f'<details class="quiet-list"><summary>无异动 {len(quiet)} 只 · 点击查看</summary>'
                     f'<table class="stk">{STK_COLS}{STK_HEAD}{_stock_rows(quiet, snapshot)}</table></details>')
    if not g and enabled:
        parts.append('<div class="quiet">本组标的已在优先分组展示，避免重复信号</div>' if registered.get(group["key"], 0)
                     else '<div class="quiet">暂无标的，去设置页录入或导入 CSV / EBK</div>')
    body = "\n".join(parts)
    stats = _group_stats_html(g, group, items_by_symbol, enabled)
    expanded = " open" if enabled and group["key"] in ("positions", "focus") else ""
    return (f'<details class="card group-card" id="group_{group["key"]}"{expanded}>'
            f'<summary class="grp"><span>{group["label"]} ({len(g)})</span>'
            f'<span class="group-stats">{stats}</span></summary>{body}</details>\n')


def _run_light(snapshot, finished_txt):
    """首页状态灯（第三排）的 (状态, 文案)：取数失败 / 空清单 / 成功。"""
    summary = snapshot.get("summary", {})
    registered = snapshot.get("registered_counts", {})
    gray_txt = f" 灰{summary.get('gray')}" if summary.get("gray") else ""
    bad = list(dict.fromkeys(summary.get("stale_symbols", []) + summary.get("missing_symbols", [])))
    if bad:
        reason = "取数失败" if not summary.get("stale_symbols") else "当日行情未取得"
        return "bad", (f"{reason} {len(bad)} 只：{html_lib.escape('、'.join(bad[:6]))}"
                       + (" 等" if len(bad) > 6 else ""))
    total = summary.get("total", 0)
    unsupported_count = len(summary.get("unsupported_symbols", []))
    if not total:
        has_registered = any(registered.values())
        return "idle", ("没有开启的监测标的，未抓取报价" if has_registered else "清单为空，未抓取报价")
    if unsupported_count >= total:
        return "idle", f"清单中 {unsupported_count} 个特殊代码暂不支持报价，未抓取报价"
    coverage = (f"{total - unsupported_count} 项数据已更新 · "
                f"{unsupported_count} 个特殊代码暂不支持报价" if unsupported_count
                else f"{total} 项数据已更新")
    kind = {"schedule": "自动", "workflow_dispatch": "手动", "push": "保存后"}.get(snapshot.get("event"), "")
    return "ok", (f"{kind}抓取成功 · 完成于 {finished_txt}（北京时间） · "
                  f"红{summary.get('red', 0)} 黄{summary.get('yellow', 0)} "
                  f"绿{summary.get('green', 0)}{gray_txt} · {coverage}")


def _signal_counts_card(snapshot):
    """首页最末尾的折叠卡片「各信号计数」：哪类信号点亮了多少只标的、哪些黄灯只由一类信号造成。
    快照里没有 signal_counts 或一条信号都没有，就整块不显示。"""
    counts = snapshot.get("signal_counts") or {}
    rows = ""
    for v in counts.values():
        if not (v.get("red") or v.get("yellow") or v.get("purple")):
            continue
        cell = lambda n: f'<td class="num">{n if n else "—"}</td>'
        rows += (f'<tr><td>{html_lib.escape(str(v.get("label", "")))}</td>'
                 f'{cell(v.get("red"))}{cell(v.get("yellow"))}{cell(v.get("purple"))}{cell(v.get("sole_yellow"))}</tr>')
    if not rows:
        return ""
    dim = 'style="color:var(--dim)"'
    head = (f'<tr class="gray mh"><td {dim}>信号</td><td class="num" {dim}>红</td><td class="num" {dim}>黄</td>'
            f'<td class="num" {dim}>紫</td><td class="num" {dim}>仅此一条黄灯</td></tr>')
    yellow = (snapshot.get("summary") or {}).get("yellow", 0)
    return (f'<details class="card group-card" id="signalCounts"><summary class="grp"><span>各信号计数</span>'
            f'<span class="group-stats">本轮黄灯 {yellow} 只</span></summary>'
            f'<table>{head}{rows}</table>'
            f'<p class="macro-help">每只标的每类信号只算一次；“仅此一条黄灯”＝这只标的的黄灯完全由这一类信号造成，'
            f'关掉这条规则它就变绿。紫色为目标价／共识价提示。</p></details>\n')


def render(macro, items, watch_count, data_down=False, snapshot=None, dup_hidden=0):
    snapshot = snapshot or {}
    registered = snapshot.get("registered_counts", {})
    risk = market_risk_summary(macro, snapshot.get("target_trade_date"))
    rows_macro = _macro_rows(macro, risk)

    items = _dedupe_items(items)
    signal_card = _signal_counts_card(snapshot)
    items_by_group = {g["item_group"]: [] for g in GROUPS}
    for item in items:
        if item.get("group") in items_by_group:
            items_by_group[item["group"]].append(item)
    # 板块 ETF 可能因去重落在持仓／重点关注里，按代码在全部条目里找，不限本组
    items_by_symbol = {normalize_symbol(d["symbol"]): d for d in items}
    group_cards = "".join(_group_card(group, items_by_group[group["item_group"]], items_by_symbol, snapshot)
                          for group in GROUPS)

    src_name = {"yahoo": "Yahoo Finance", "stooq": "Stooq", "nasdaq": "Nasdaq"}
    srcs = sorted({d.get("source") for d in items if d.get("source")})
    src_txt = " · ".join(src_name.get(x, x) for x in srcs) if srcs else "本轮不可用"
    n_rt = len([d for d in items if d.get("realtime")])
    if n_rt:
        src_txt += f" · {n_rt} 只用了实时价（Nasdaq，0 延迟）"
    risk_title = html_lib.escape(f"综合判断：{risk['label']} · 有效指标 {risk['valid_count']}/5")
    macro_stats = (f'<span class="market-risk-badge" id="marketRiskLight" data-level="{risk["level"]}" '
                   f'role="status" aria-label="{risk_title}">'
                   f'<i class="stat-dot {risk["level"]}" aria-hidden="true"></i>'
                   f'综合：{html_lib.escape(risk["label"])}</span>')
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    snapshot_json = snapshot_json.replace("</", "<\\/")
    summary = snapshot.get("summary", {})
    counts = snapshot.get("list_counts", {})
    finished_iso = beijing_iso(snapshot.get("finished_at_bj") or snapshot.get("finished_at"))
    finished_txt = finished_iso[:19].replace("T", " ") if finished_iso else "未记录"
    # 一行摘要升级为状态灯文案（首页第三排）
    init_light_phase, init_light = _run_light(snapshot, finished_txt)
    dca_info = dca_text(snapshot.get("dca_reminder"))
    # 第二排：数据时间 + 自动计划。冬夏令时只显示当日适用的那条（以当天美东是否夏令时为准）。
    ny_now = datetime.now(US_TZ)
    bj_auto = "夏令时次日 08:15" if ny_now.dst() != timedelta(0) else "冬令时次日 09:15"
    sub_line = (f"数据时间 {market_data_time(snapshot)} · "
                f"自动计划：美东周一至五 20:15（北京 {bj_auto}）")
    lc = registered or counts
    reg_line = ('<div class="quiet">在册：' + ' · '.join(
                    f"{g['label']} {lc.get(g['key'], 0)}" for g in GROUPS if lc.get(g['key'], 0))
                + (f" · 本轮监测 {summary.get('total', 0)} 个唯一标的" if registered else "")
                + (f' · 已隐藏 {dup_hidden} 只重复标的（风险参考／持仓优先，其次重点关注，最后其他）'
                   if dup_hidden else "") + "</div>")
    consensus_credit = (f'共识价来源：{CONSENSUS_SOURCE}，每月 1、15 日更新<br>\n'
                        if any((d.get("target") or {}).get("kind") == "consensus" for d in items) else "")
    dca_html = (f'<div class="dca {"on" if (snapshot.get("dca_reminder") or {}).get("due") else ""}">'
                f'定投提醒：{dca_info}</div>') if dca_info else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0f1115">
<link rel="icon" type="image/png" sizes="32x32" href="./icons/favicon-32.png">
<link rel="apple-touch-icon" href="./icons/apple-touch-icon.png">
<link rel="manifest" href="./manifest.webmanifest">
<title>AI监测市场</title>
<style>
{PAGE_CSS}</style>
</head>
<body><div class="wrap">
<h1>AI监测市场</h1>
<div class="sub">{sub_line}</div>

<div class="runbar">
  <button class="primary" id="btnRunNow">立即运行</button>
  <button id="btnCheckStatus">查运行状态</button>
  <button id="btnHardRefresh">强制刷新</button>
  <a class="btnlink" href="./edit.html">设置</a>
  <script>{HARD_REFRESH_JS}</script>
</div>
<div class="statusrow">
  <div class="runlight" id="runLight" data-phase="{init_light_phase}"><i class="dot"></i><span id="runLightTxt">{init_light}</span></div>
  <div class="runlight rulelight" id="ruleLight" data-phase="idle"><i class="dot"></i><span id="ruleLightTxt">正在核对最新规则…</span></div>
</div>
<div id="checkResult" class="check-result" role="status" hidden></div>

{dca_html}
<div class="script-data" hidden><script id="snapshotData" type="application/json">{snapshot_json}</script></div>

{('<div class="warn">本轮标的均未取得可用报价；可能是接口故障、异常行情或抓取时间预算不足，请查看个股原因。宏观数据另行展示。</div>' if data_down else "")}

<details class="card macro-card" id="macroCard">
<summary class="grp"><span>市场风险参考</span><span class="group-stats">{macro_stats}</span></summary>
<p class="macro-help" id="marketRiskReason">{html_lib.escape(risk['text'])} · 有效指标 {risk['valid_count']}/5</p>
<details class="macro-help" id="marketRiskRules"><summary>综合判断规则</summary><p>{html_lib.escape(RISK_RULE_TEXT)}</p></details>
<table>
<tr class="gray mh"><td style="color:var(--dim)">指标</td><td class="num" style="color:var(--dim)">当前</td><td class="num" style="color:var(--dim)">周变化</td><td style="color:var(--dim)">状态</td></tr>
{rows_macro}
</table>
<details class="macro-help" id="marketRiskNotes"><summary>指标阈值与数据说明</summary>
<p>垃圾债利差：≥{S['hy_green']:.0f} bp 黄、≥{S['hy_red']:.0f} bp 红；一周扩大 ≥30 bp 为黄、≥50 bp 为红。VIX：≥{S['vix_green']:g} 黄、≥{S['vix_red']:g} 红。标普500回撤是已经发生的跌幅，不是提前预测。</p>
<p>10Y美债是参考行，不计入综合灯：一个月上行 ≥{UST_MONTH_YELLOW} bp 标黄（利率冲击风险），≥{UST_MONTH_WATCH} bp 只提示偏快；周变化列单位为 bp。</p>
<p>上涨参与度：站上长期均线的股票不足一半为黄；不足三成且信用／金融压力也升高才为红。金融压力是周度数据，达到历史平均紧张程度为黄。指标缺失时显示无数据，不代表安全，也不预测具体跌幅。</p>
<p>市场宽度：<a href="{BREADTH_CREDIT}" rel="noopener noreferrer" target="_blank">History of Market (historyofmarket.com)</a>，数据依据当前成分股计算，回看历史可能有成分股选择偏差。</p>
</details>
</details>

{group_cards}
{signal_card}{reg_line}

<div class="foot">
行情：{src_txt} · 垃圾债利差：FRED · 金融压力：NFCI原始周度数据／FRED同步备用<br>
市场宽度：<a href="{BREADTH_CREDIT}" rel="noopener noreferrer" target="_blank">History of Market (historyofmarket.com)</a> · 基本面：Nasdaq<br>
{consensus_credit}<a href="./edit.html?rules=1" style="color:#6ba3f0;text-decoration:none">查看完整规则与阈值</a>
</div>

<div class="acts">
<span id="runMsg"></span>
</div>
{RUN_JS}
</div></body></html>"""


QUOTE_EXHAUSTED = object()  # 总预算用完、这只根本没来得及尝试（与"尝试了但失败"区分）


def _fetch_quote(symbol):
    """工作线程里抓一只：返回行情 dict / None（失败）/ QUOTE_EXHAUSTED（总预算已用完）。"""
    if QUOTE_DEADLINE is not None and time.monotonic() >= QUOTE_DEADLINE:
        return QUOTE_EXHAUSTED
    _QUOTE_TLS.deadline = time.monotonic() + QUOTE_SYMBOL_SECONDS  # 单只限时，卡住也不拖累整批
    try:
        return fetch_history(symbol)
    except Exception as e:
        log(f"  ! {symbol} 抓取异常：{str(e)[:70]}")
        return None
    finally:
        _QUOTE_TLS.deadline = None


def collect_quotes(symbols):
    """有界并发抓取，保持结果与输入一一对应；失败的标的在预算充足时补抓一轮。"""
    results = {}

    def run(batch, workers, keep_better=False):
        if not batch:
            return
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for sym, data in zip(batch, ex.map(_fetch_quote, batch)):
                if keep_better and (data is None or data is QUOTE_EXHAUSTED):
                    continue  # 补抓没成功就保留第一轮的失败原因
                results[sym] = data

    run(symbols, QUOTE_WORKERS)
    for _ in range(QUOTE_RETRY_PASSES):
        failed = [s for s in symbols if results.get(s) is None]
        remaining = (QUOTE_DEADLINE - time.monotonic()) if QUOTE_DEADLINE is not None else 999
        if not failed or remaining < 60:
            break
        log(f"补抓 {len(failed)} 只第一轮失败的标的…")
        # 指数/期货只有 Yahoo 一个源，熔断冷却期内会被跳过；预算充足就等冷却结束再补抓，
        # 否则补抓轮还是撞同一堵墙。等待时间受总预算约束。
        wait = max(0.0, max(YAHOO_GATE.blocked_until, STOOQ_GATE.blocked_until) - time.monotonic())
        if wait and (QUOTE_DEADLINE is None or remaining > wait + 45):
            log(f"  等待行情源限流冷却约 {wait:.0f} 秒后再补抓…")
            time.sleep(wait + 1)
        else:
            time.sleep(3)
        YAHOO_GATE.reset(True)
        STOOQ_GATE.reset(True)
        run(failed, max(1, QUOTE_WORKERS // 2), keep_better=True)
    done = sum(isinstance(v, dict) for v in results.values())
    log(f"行情抓取完成：成功 {done}/{len(symbols)}")
    return results


# ---------------------------------------------------------------- main

def main(argv=None):
    global TARGET_DATE, CLOSED_ONLY, QUOTE_DEADLINE
    parser = argparse.ArgumentParser(description="市场自检生成器")
    parser.add_argument("--prepare-run", action="store_true", help="锁定本次运行目标交易日并输出 GitHub Actions 环境变量")
    parser.add_argument("--ensure-schedule-run", action="store_true", help="定时任务保险：缺失时补派发一次运行")
    args = parser.parse_args(argv)
    if args.prepare_run:
        prepare_run()
        return 0
    if args.ensure_schedule_run:
        return ensure_schedule_run()

    if not TARGET_DATE:
        plan = plan_run(EVENT, datetime.now(timezone.utc))
        TARGET_DATE, CLOSED_ONLY = plan["target"], plan["closed_only"]

    cfg_path = os.path.join(BASE, "holdings.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"holdings.json 读取失败，按空清单处理：{e}")
        cfg = {}

    universe, group_counts, dup_hidden = grouped_universe(cfg)
    cons_data, cons_stat = {}, {}
    if not universe:
        log("holdings.json 里没有自选股，跳过个股部分")

    macro = build_macro()
    items = []
    if universe:
        log("抓取 " + " + ".join(f"{group_counts[g['key']]} 只{g['label']}" for g in GROUPS
                                 if group_counts[g['key']])
            + (f"（另有 {dup_hidden} 只重复，已跳过）" if dup_hidden else "") + "...")
        QUOTE_DEADLINE = time.monotonic() + QUOTE_BUDGET_SECONDS
        YAHOO_GATE.reset(True)
        STOOQ_GATE.reset(True)
        to_fetch = [sym for sym, _, _ in universe
                    if quote_supported(sym) and instrument_identity(sym) != '^TNX']
        sector_etfs = {g["sector_etf"] for g in GROUPS if g.get("sector_etf")}
        # 板块 ETF 相对强弱的基准是标普500指数本身，来源优先级：
        #   1) 风险参考已取的 Yahoo ^GSPC 日线（不额外请求）  2) History of Market 日线  3) 都没有才抓 SPY 作近似
        need_bench = any(normalize_symbol(s) in sector_etfs for s in to_fetch)
        bench_rows, bench_name, spy_key = [], RS_BENCH_NAME, None
        if need_bench:
            bench_rows = list(SP500_RS_ROWS) or sp500_rows_fallback()
            if not bench_rows:
                bench_name = "SPY"
                spy_key = next((s for s in to_fetch if normalize_symbol(s) == "SPY"), None)
                if not spy_key:
                    spy_key = "SPY"
                    to_fetch = to_fetch + [spy_key]
        fetched = collect_quotes(to_fetch)
        bench_data = None
        if bench_rows:
            bench_data = {"dates": [d for d, _ in bench_rows], "closes": [v for _, v in bench_rows]}
        elif spy_key and isinstance(fetched.get(spy_key), dict):
            bench_data = fetched[spy_key]
        YAHOO_GATE.reset(False)
        STOOQ_GATE.reset(False)
        # S&P Global 共识价：只取持仓 / 重点关注，每月 1、15 号一轮；任何异常都不能拖垮行情和页面
        cons_syms = [normalize_symbol(sym) for sym, sc, grp in universe if consensus_wanted(sym, sc, grp)]
        try:
            with open(os.path.join(BASE, "status.json"), encoding="utf-8") as f:
                prev_cons = json.load(f).get("consensus")
        except Exception:
            prev_cons = None
        try:
            cons_data, cons_stat = update_consensus(cons_syms, prev_cons, NOW.strftime("%Y-%m-%d"))
        except Exception as e:
            log(f"  ! 共识价更新中断，沿用旧值：{str(e)[:90]}")
            old_data = prev_cons.get("data") if isinstance(prev_cons, dict) else None
            cons_data = {k: v for k, v in (old_data or {}).items() if k in cons_syms and isinstance(v, dict)}
            cons_stat = {"stopped": "error"}
        for sym, sc, grp in universe:
            supported = quote_supported(sym)
            is_yield = instrument_identity(sym) == '^TNX'
            raw = fetched.get(sym)
            exhausted = raw is QUOTE_EXHAUSTED
            data = None if exhausted else raw
            if not data and (grp == "index_funds" or is_yield):
                reference = macro_index_quote(sym, sc, grp, macro)
                if reference:
                    items.append(reference)
                    continue
            if data:
                cons = cons_data.get(normalize_symbol(sym))
                lv, sig, detail = analyze_symbol(sym, {**sc, "_consensus": cons} if cons else sc, data, group=grp)
                if bench_data and normalize_symbol(sym) in sector_etfs:
                    rs = relative_strength(data, bench_data, bench_name=bench_name)
                    if rs:
                        detail["rs_spy"] = rs
                items.append(detail)
                if lv != "green":
                    log(f"  {sym}: {lv} — {', '.join(sig)}")
                continue

            if exhausted:
                log(f"  ! {sym} 未取数：个股抓取总预算已用完")
            items.append({
                "symbol": sym, "note": sc.get("note", ""), "price": None,
                "chg": None, "rsi": {period: None for period in RSI_PERIODS}, "dist_high": None, "dist_low": None,
                "vol_ratio": None, "trigger": sc.get("trigger"),
                "group": grp, "data_date": "", "boll_up": None, "boll_dn": None,
                "signals": ["无数据：暂不支持该代码报价"] if not supported else
                           (["本轮抓取时间预算不足，尚未取数"] if exhausted else ["数据获取失败或报价异常"]),
                "level": "gray",
            })

    QUOTE_DEADLINE = None
    # 基本面只查持仓 + 重点关注：其他关注只数大、交易价值低，全量查既拖慢运行又容易限流
    fund_stat = {"checked": 0, "red": 0, "yellow": 0}
    fund_targets = [d for d in items
                    if d.get("group") in ("position", "focus") and d.get("symbol")
                    and quote_supported(d["symbol"])]
    if fund_targets:
        log(f"读取 {len(fund_targets)} 只持仓/重点关注的财报…")
        try:
            # 4 路并发：串行一只约 2 秒，30 只会拖到 1 分钟；并发后 15 秒内结束
            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(lambda d: fundamental_check(d["symbol"]), fund_targets))
            for d, fd in zip(fund_targets, results):
                if not fd:
                    continue
                d["fund"] = fd
                fund_stat["checked"] += 1
                if fd["level"] == "red":
                    fund_stat["red"] += 1
                    log(f"  {d['symbol']} 基本面红：{'；'.join(fd['hits'])}")
                elif fd["level"] == "yellow":
                    fund_stat["yellow"] += 1
                    log(f"  {d['symbol']} 基本面黄：{'；'.join(fd['hits'])}")
        except Exception as e:
            # 财报是加分项，任何异常都不能拖垮行情抓取和页面生成
            log(f"  ! 基本面检查中断，跳过：{e}")
        log(f"  基本面：{fund_stat['checked']} 份财报 · "
            f"红 {fund_stat['red']} · 黄 {fund_stat['yellow']}")

    earn_stat = attach_earnings(items)

    snapshot = build_snapshot(macro, items, cfg, group_counts=group_counts)
    snapshot["fundamental"] = fund_stat
    snapshot["consensus"] = {"source": CONSENSUS_SOURCE, "stat": cons_stat, "data": cons_data}
    snapshot["earnings"] = earn_stat
    supported_items = [d for d in items if quote_supported(d["symbol"])]
    html = render(macro, items, len(universe), data_down=bool(supported_items)
                  and all(d["level"] == "gray" for d in supported_items), snapshot=snapshot,
                  dup_hidden=dup_hidden)
    out = os.path.join(BASE, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    status_path = os.path.join(BASE, "status.json")
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log(f"已生成 {out} 与 {status_path}；目标交易日 {TARGET_DATE}，实际行情日期 {snapshot['actual_dates']}")

    try:
        push_serverchan(macro, items)
    except Exception as e:
        log(f"推送环节异常（忽略）：{str(e)[:90]}")
    return 0


def push_serverchan(macro, items):
    """有红/黄信号时推微信（Server酱 https://sct.ftqq.com）

    仓库 Secret 里配 SERVERCHAN_KEY 才生效；没配就静默跳过。
    推送失败只记日志，绝不影响页面生成 —— 页面是主产物，推送是附赠。
    """
    key = (os.environ.get("SERVERCHAN_KEY") or "").strip()
    if not key:
        log("未配置 SERVERCHAN_KEY，跳过微信推送")
        return

    reds = [d for d in items if d["level"] == "red"]
    yellows = [d for d in items if d["level"] == "yellow"]
    grays = [d for d in items if d["level"] == "gray"]
    if not reds and not yellows:
        log("今晚无异动，不推送微信")
        return

    lines = []
    for icon, label, group in (("🔴", "需要动手", reds), ("🟡", "留意", yellows)):
        if not group:
            continue
        lines.append("## %s %s（%d）" % (icon, label, len(group)))
        for d in group:
            price = "%.2f" % d["price"] if d.get("price") else "—"
            chg = " %+.2f%%" % d["chg"] if d.get("chg") is not None else ""
            gname = GROUP_LABELS.get(d.get("group"), "未分类")
            sig = " / ".join(d.get("signals") or []) or "—"
            lines.append("- **%s** %s%s · %s（%s）" % (
                normalize_symbol(d["symbol"]), price, chg, sig, gname))
        lines.append("")

    lines.append("## 宏观")
    for k in RISK_INDICATORS:
        m = macro.get(k) or {}
        if not m.get("ok"):
            lines.append("- %s：无数据" % m.get("name", k))
        else:
            lines.append("- %s：%.2f%s（%s）" % (
                m.get("name", k), m["value"], m.get("unit", ""), m.get("date", "")))

    if grays:
        lines.append("")
        lines.append("> ⚪ %d 只取数失败：%s" % (
            len(grays), "、".join(normalize_symbol(d["symbol"]) for d in grays[:8])))
    lines.append("")
    lines.append("[打开完整看板](https://nixhuang.github.io/market-monitor/)")

    title = "%s 市场自检 🔴%d 🟡%d" % (
        NOW.strftime("%m-%d"), len(reds), len(yellows))
    try:
        r = requests.post("https://sctapi.ftqq.com/%s.send" % key,
                          data={"title": title, "desp": "\n".join(lines)},
                          timeout=25)
        try:
            j = r.json()
        except ValueError:
            j = {}
        ok = (j.get("code") == 0) or j.get("success") or r.status_code == 200
        log("微信推送：%s（%s）" % ("成功" if ok else "失败", str(j)[:90]))
    except Exception as e:
        log("微信推送异常（不影响页面）：%s" % str(e)[:90])


if __name__ == "__main__":
    sys.exit(main())
