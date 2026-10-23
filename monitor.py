#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场监控脚本
- 抓 FRED 宏观指标（免费，无需 API Key）
- 抓 Yahoo 个股/ETF 行情（免费，无需 API Key）
- 按规则筛选出「今晚重点关注」
- 生成 index.html 供 GitHub Pages 展示

数据来源：
  Cboe  https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
  FRED  https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
  Yahoo https://query1.finance.yahoo.com/v8/finance/chart/<SYMBOL>
"""

import argparse
import atexit
import csv
import hashlib
import html as html_lib
import io
import json
import math
from types import SimpleNamespace
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
import policy_alerts
import earnings_sources
import market_sessions
from runtime_state import RuntimeState, atomic_write_json, build_identity, write_artifacts

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
    "nfci":     {"id": "NFCI",         "name": "金融压力NFCI", "unit": "",    "scale": 1},
}

# ---------------------------------------------------------------- 可调阈值
# 这些默认值会被同目录下的 settings.json 覆盖；settings.json 缺失或字段写错就退回默认值。
# 你可以在 edit.html 里直接改，改完保存会自动重跑，不用碰代码。
from rules_config import (DEFAULT_SETTINGS, SETTING_LIMITS, SETTING_ORDER,
                          valid_settings, resolve_settings)
from page_assets import (PAGE_CSS, PAGE_INTERACTION_JS, RULES_CSS, RULES_SUMMARY,
                         rules_body, rules_template, script_asset, json_script)


from zoneinfo import ZoneInfo
US_TZ = ZoneInfo("America/New_York")
BJ_TZ = ZoneInfo("Asia/Shanghai")


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", flush=True)


def session_date(on_or_before):
    """交易所日历：节假日/周末回退到最近有效交易日，不能只按周一至周五猜。"""
    return market_sessions.session_on_or_before(on_or_before)


def plan_run(event, when):
    """schedule锚定最近一次美东20:00（23/5交易休盘窗口20:00-21:00的起点）；手动/保存重跑保留盘中行为。"""
    ny = market_sessions.eastern(when)
    day = ny.date()
    if event == "schedule":
        anchor = ny.replace(hour=20, minute=0, second=0, microsecond=0)
        if ny < anchor:
            day -= timedelta(days=1)
        return {"closed_only": True, "target": session_date(day)}
    bounds = market_sessions.session_bounds(day)
    if bounds and ny < bounds[0]:
        day -= timedelta(days=1)
    day_target = session_date(day)
    return {"closed_only": market_sessions.active_session(ny) is None, "target": day_target}


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


# 收盘口径下，「停在较早日线 + 本该有价却没取到」占比超过这个比例，视为源端当日日线还没就绪。
# 实测：美东 20:15 前后各源会出现 99/100 停在前一交易日的空档（10-15/16/17 三次），19:12 与 22:30 后正常。
FRESH_MAX_STALE_RATIO = 0.2


def snapshot_freshness(snap, target=None):
    """快照是否「当日数据齐」。返回 (fresh, today, expected)。

    expected = 当日 + 较早 + 缺价（扣掉不支持的代码）；参考点位（reference_only）不计。
    旧快照没有 today_prices 字段时退回看 actual_dates.max 是否到目标日。
    """
    snap = snap if isinstance(snap, dict) else {}
    su = snap.get("summary") or {}
    today, prior, missing = su.get("today_prices"), su.get("prior_prices"), su.get("missing_prices")
    if not isinstance(today, int) or not isinstance(prior, int):
        latest = (snap.get("actual_dates") or {}).get("max") or ""
        return bool(target) and latest >= target, None, None
    unsupported = su.get("unsupported_symbols")
    unsupported = len(unsupported) if isinstance(unsupported, list) else 0
    missing = max(0, (missing if isinstance(missing, int) else 0) - unsupported)
    expected = today + prior + missing
    if expected <= 0:
        return False, today, expected
    return (prior + missing) / expected <= FRESH_MAX_STALE_RATIO, today, expected


def ensure_schedule_run(runtime=None):
    """保险：GitHub 定时任务可能被延迟或丢弃。若今晚（美东20:00之后）的收盘结果还没生成，
    或生成了但当日日线不齐（源端还没出当日数据），就补派发一次与定时任务等价的运行；
    结果齐全或已有任务在排队/运行则不重复派发。"""
    runtime = runtime or RuntimeState(BASE, target=TARGET_DATE, event="ensure-schedule")
    now_ny = datetime.now(timezone.utc).astimezone(US_TZ)
    if now_ny.hour < 20:
        # 窗口＝美东 20:00–24:00：20:15/20:45 由 Worker 派发（Worker 自己只放行 20 点）；
        # 之后的是「当日日线未就绪」时 daily.yml 安排的延迟重试。美东零点后（含次日盘前）一律不做。
        print(f"美东 {now_ny:%H:%M} 不在 20:00-24:00 收盘补跑窗口内，不补跑")
        runtime.receipt("skipped", "outside_schedule_window")
        return 0
    plan = plan_run("schedule", datetime.now(timezone.utc))
    runtime.target = plan["target"]
    try:
        with open(os.path.join(BASE, "status.json"), encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = {}
    snap = snap if isinstance(snap, dict) else {}
    if snap.get("target_trade_date") == plan["target"] and snap.get("mode") == "closed" \
            and (snap.get("summary") or {}).get("total"):
        fresh, today, expected = snapshot_freshness(snap, plan["target"])
        runtime.fresh, runtime.today, runtime.expected = fresh, today, expected
        if fresh:
            print(f"目标交易日 {plan['target']} 的收盘结果已存在且当日数据齐（{today}/{expected}），无需补跑")
            runtime.receipt("skipped", "target_already_fresh")
            return 0
        print(f"目标交易日 {plan['target']} 的结果已存在但当日日线不齐（当日 {today}/{expected}），补跑")
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}/actions/workflows/daily.yml"
    r = requests.get(base + "/runs?branch=main&per_page=10", headers=headers, timeout=20)
    r.raise_for_status()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    for run in r.json().get("workflow_runs", []):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if run.get("status") in ("queued", "in_progress", "waiting", "pending") and created >= cutoff:
            print(f"已有候选运行 {run['id']} 在排队或执行（{run['status']}），不重复派发；"
                  f"未确认其目标交易日为 {plan['target']}，也未确认本轮生成完成")
            runtime.receipt("skipped", "candidate_run_busy", candidate_run_id=run["id"],
                            candidate_status=run["status"], candidate_target_verified=False)
            return 0
    d = requests.post(base + "/dispatches", headers=headers, timeout=20,
                      json={"ref": "main", "inputs": {"source": "schedule-fallback"}})
    d.raise_for_status()
    print(f"今晚收盘结果缺失，已补派发运行（目标交易日 {plan['target']}）")
    runtime.receipt("skipped", "dispatch_requested", dispatched=True)
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
    for key in ("dates", "closes", "volumes", "highs", "lows", "ma_range_valid"):
        seq = data.get(key)
        # 源本身没有这个字段就别补空列表：会被下游当成「有效位清单长度不符」，
        # 52周区间和日内高低判断会整段判废。stooq / Nasdaq 都不带 ma_range_valid。
        if seq is None:
            continue
        if seq and len(seq) != len(dates):
            return None
        out[key] = [seq[i] for i in keep] if seq else []
    if len(out["closes"]) < 30:
        return None
    out.update(price=out["closes"][-1],
               prev_close=out["closes"][-2], realtime=False, rt_ts="")
    return out


def load_settings():
    """按 schema 读取覆盖值；逐项警告与回退，不改写 settings.json。"""
    try:
        with open(os.path.join(BASE, "settings.json"), encoding="utf-8") as f:
            return resolve_settings(json.load(f), log)
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except (OSError, ValueError) as e:
        log(f"  ! settings.json 读取失败，用默认值：{e}")
        return dict(DEFAULT_SETTINGS)


TARGET_SPACE_PCT = 30
TARGET_HIGH_SPACE_PCT = 70
S = {**load_settings(), "target_space_pct": TARGET_SPACE_PCT,
     "target_high_space_pct": TARGET_HIGH_SPACE_PCT}

# 布林带"逼近"阈值（%）：距上轨/下轨不足这个百分比就算命中，不用等真的穿过去
# 因为盘中价格一直在动，等收盘才确认会错过时机
BOLL_NEAR_PCT = S["boll_near_pct"]


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
CBOE_VIX_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


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
# NFCI 周五观测通常次周三发布，联邦假日可顺延到周四；旧10日上限会在假日周误杀仍属最新的观测。
# 最多容纳顺延至周四的 13 天；再旧仍判缺失，绝不把数月前的官方CSV当作最新。
NFCI_MAX_LAG_DAYS = 13
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


def cached_financial_conditions(previous=None, state=None):
    """金融压力NFCI：复用已核验的周度观测，仅在既有运行中按公布节奏重新查询。"""
    prior = previous.get('nfci_cache') if isinstance(previous, dict) else None
    prior = prior if isinstance(prior, dict) else {}
    now_et = NOW.astimezone(US_TZ)
    target = TARGET_DATE or now_et.date().isoformat()
    target_end = datetime.strptime(target, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=US_TZ)
    sources = ('NFCI原始周度数据', 'NFCI周度数据（FRED同步备用）')

    def release_time(friday, next_week=False):
        # 本周周五观测次周三（+5日）公布；下一个观测的预期公布为+12日。
        # 假日周官网可顺延周四：周三未更新记 pending，下一美东自然日随原运行重查。
        day = datetime.strptime(friday, '%Y-%m-%d').date() + timedelta(days=12 if next_week else 5)
        return datetime(day.year, day.month, day.day, 8, 30, tzinfo=US_TZ)

    def valid_rows(values, cached=False):
        if not isinstance(values, list):
            return []
        checked, previous_day = [], ''
        try:
            for item in values:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    return []
                day, value = item
                date = datetime.strptime(day, '%Y-%m-%d').date()
                if (date.isoformat() != day or date.weekday() != 4 or
                        not isinstance(value, (int, float)) or isinstance(value, bool) or
                        not math.isfinite(value) or abs(value) > 20 or day <= previous_day):
                    return []
                previous_day = day
                if day > target or release_time(day) > min(now_et, target_end):
                    if cached:
                        return []  # 缓存不可包含目标日之后或目标交易日尚未发布的观测
                    continue     # 原始完整序列可能含历史回测目标日以后的数据
                checked.append((day, value))
                if len(checked) > 8:
                    checked.pop(0)
        except (ValueError, TypeError, OverflowError):
            return []
        return checked

    source = prior.get('source') if prior.get('source') in sources else ''
    rows = valid_rows(prior.get('rows'), cached=True) if source else []
    checked_at = None
    try:
        stamp = datetime.fromisoformat(prior.get('checked_at', ''))
        if stamp.tzinfo is not None and stamp <= now_et:
            checked_at = stamp.astimezone(US_TZ)
    except (TypeError, ValueError):
        pass
    if not checked_at or prior.get('result') not in ('ok', 'pending', 'error'):
        rows, source = [], ''
    due = release_time(rows[-1][0], next_week=True) if rows else None
    # 到期未发布或取源失败，至少隔一小时再试；避免白天先查一次后漏掉夜间正式运行。
    throttled = bool(rows and checked_at and prior.get('result') in ('pending', 'error') and
                     now_et - checked_at < timedelta(hours=1))
    if rows and ((due and now_et < due) or throttled):
        if state is not None:
            state.update(rows=rows, source=source, checked_at=prior.get('checked_at', ''),
                         result=prior.get('result', 'ok'))
        log(f"  · 金融压力NFCI沿用已核验周度缓存（截至 {rows[-1][0]}）")
        return rows, source

    try:
        official = valid_rows(financial_conditions_series())
    except Exception as exc:
        log(f'  ! 金融压力NFCI原始数据异常：{str(exc)[:90]}')
        official = []
    selected, selected_source = official, sources[0]
    # 官网上一周的值仍在13日窗口内，不代表新周已发布；到点但没有推进时也查FRED。
    official_due = release_time(official[-1][0], next_week=True) if official else None
    if (not official or official_due <= now_et or
            (rows and official[-1][0] <= rows[-1][0])):
        try:
            backup = valid_rows(fred_series('NFCI'))
        except Exception as exc:
            log(f'  ! 金融压力NFCI备用数据异常：{str(exc)[:90]}')
            backup = []
        if backup and (not selected or backup[-1][0] > selected[-1][0]):
            selected, selected_source = backup, sources[1]

    advanced = bool(selected and (not rows or selected[-1][0] > rows[-1][0]))
    if selected and (not rows or selected[-1][0] >= rows[-1][0]):
        rows, source = selected, selected_source
    fresh = bool(rows and (datetime.strptime(target, '%Y-%m-%d').date() -
                           datetime.strptime(rows[-1][0], '%Y-%m-%d').date()).days <= NFCI_MAX_LAG_DAYS)
    result = ('ok' if advanced and fresh else
              'pending' if selected and fresh and rows and selected[-1][0] >= rows[-1][0] else 'error')
    if state is not None:
        state.update(rows=rows, source=source, checked_at=now_et.isoformat(), result=result)
    return rows, source


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


def cboe_vix_series(days=400):
    """Cboe 官方 VIX 指数日收盘；仅接受完整、升序且真实日期的 CSV，不拿期货/旧指数替代。"""
    target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    try:
        response = requests.get(CBOE_VIX_URL, headers=UA, timeout=15)
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        fields = ('DATE', 'OPEN', 'HIGH', 'LOW', 'CLOSE')
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError('Cboe VIX 文件字段不符')
        rows = []
        previous = ''
        for row in reader:
            if set(row) != set(fields) or any(row[field] is None for field in fields):
                raise ValueError('Cboe VIX 行字段不完整')
            day = datetime.strptime(row['DATE'], '%m/%d/%Y').date().isoformat()
            close = float(row['CLOSE'])
            if day <= previous or not math.isfinite(close) or not 0 < close < 200:
                raise ValueError('Cboe VIX 日期顺序或收盘值异常')
            previous = day
            if day <= target:
                rows.append((day, close))
        return rows[-days:] if len(rows) >= 30 else []
    except (requests.RequestException, ValueError, TypeError, KeyError, csv.Error) as e:
        log(f'  ! Cboe VIX 官方日线暂不可用：{str(e)[:100]}')
        return []


def vix_index_series(source_info, days=400):
    """官方最新目标日优先；官方未更新时，用日期更近的 Yahoo ^VIX 收盘兜底。"""
    target = TARGET_DATE or NOW.astimezone(US_TZ).date().isoformat()
    official = cboe_vix_series(days)
    if official and official[-1][0] == target:
        source_info['source'] = 'Cboe VIX 官方日线'
        return official
    yahoo = market_index_series('^VIX', days)
    if yahoo and (not official or yahoo[-1][0] > official[-1][0]):
        source_info['source'] = 'Yahoo ^VIX'
        return yahoo
    if official:
        source_info['source'] = 'Cboe VIX 官方日线'
    return official


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


def build_macro(previous=None, nfci_state=None):
    log("抓取宏观指标...")
    macro = {}
    SP500_RS_ROWS.clear()
    for key, cfg in FRED_SERIES.items():
        source_info = {}
        if key == "hy_oas":
            rows = fred_series(cfg["id"])
            source_info['source'] = 'FRED'
        elif key == 'vix':
            rows = vix_index_series(source_info)
        elif key == 'sp500':
            rows = market_index_series('^GSPC')
            source_info['source'] = 'Yahoo ^GSPC'
        elif key == "ust10":
            rows = treasury_yield_series(source_info=source_info)
        elif key == "nfci":
            rows, source_info['source'] = cached_financial_conditions(previous, nfci_state)
        else:
            continue
        if not rows and key in ("vix", "sp500", "ust10"):
            # 只有 3 个请求，值得多等一会儿再试一次：这三项决定风险参考的综合灯
            log(f"  · {cfg['name']} 首次取数失败，稍后重试一次")
            time.sleep(MACRO_RETRY_WAIT)
            if key == 'vix':
                rows = vix_index_series(source_info)
            elif key == "ust10":
                rows = treasury_yield_series(source_info=source_info)
            else:
                rows = market_index_series('^GSPC')
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
        limit = NFCI_MAX_LAG_DAYS if key == "nfci" else 7
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
                or recent(nfci, NFCI_MAX_LAG_DAYS) and nfci["value"] >= 0))
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
               "breadth": "上涨参与度", "nfci": "金融压力NFCI"}
RISK_RULE_TEXT = (
    "五项指标分三类：信用／金融环境（垃圾债利差、金融压力NFCI）、情绪（VIX）、趋势（标普500回撤、上涨参与度）。"
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
            limit = NFCI_MAX_LAG_DAYS if key == "nfci" else 5 if key == "breadth" else 7
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
    label = {"red": "升高", "yellow": "留意", "green": "平稳"}[level]
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

def _finite_pos(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0


def _yahoo_meta_close(meta, dates, closes, vols, highs, lows, range_valid):
    """Yahoo 日线在美东收盘后约 20:00–22:00 会暂时缺最后一根（实测 10-15/16/17/20 都是 98/99 停在前一日），
    但 meta 里的 regularMarket* 已是当日正式收盘。只在「收盘之后、且日线确实缺这一天」时用它补一根，
    高低量都齐才算真实日内范围；任何一项缺失或不自洽就原样返回。"""
    try:
        price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if not _finite_pos(price) or isinstance(ts, bool) or not isinstance(ts, (int, float)) or not dates:
            return dates, closes, vols, highs, lows, range_valid, False
        quoted = datetime.fromtimestamp(ts, timezone.utc)
        day = quoted.astimezone(US_TZ).date().isoformat()
        bounds = market_sessions.session_bounds(day)
        if not bounds or quoted < bounds[1] or day <= dates[-1] \
                or market_sessions.previous_session(day) != dates[-1]:
            return dates, closes, vols, highs, lows, range_valid, False
        hi, lo, vol = meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow"), meta.get("regularMarketVolume")
        real_range = _finite_pos(hi) and _finite_pos(lo) and lo <= price <= hi
        if abs(price / closes[-1] - 1) > 0.6:
            return dates, closes, vols, highs, lows, range_valid, False   # 疑似拆股/错代码，交给换源逻辑
        return (dates + [day], closes + [price], vols + [vol if _finite_pos(vol) else 0],
                highs + [hi if real_range else price], lows + [lo if real_range else price],
                range_valid + [bool(real_range)], True)
    except (ValueError, TypeError, OverflowError, KeyError):
        return dates, closes, vols, highs, lows, range_valid, False


def nasdaq_closing_quote(symbol, day, prev_close):
    """Nasdaq 报价页的收盘价（日线还没落地时用）：成交日期必须是 day，且「收盘价 − 涨跌额」要接上前一交易日收盘，
    防止错代码/拆股。只给收盘价与成交量（与历史日线同口径，已核对一致），日内高低不可得。"""
    syms = [symbol] + ([symbol.replace("-", ".")] if "-" in symbol else [])
    hdr = dict(UA)
    hdr.update({"Accept": "application/json", "Referer": "https://www.nasdaq.com/"})
    want = datetime.strptime(day, "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
    num = lambda v: float(str(v or "").replace("$", "").replace(",", "").replace("+", "").strip())
    for sym in syms:
        for ac in ("stocks", "etf"):
            try:
                r = requests.get(f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass={ac}",
                                 headers=hdr, timeout=quote_timeout(10))
                if r.status_code != 200:
                    continue
                data = r.json().get("data") or {}
                status = str(data.get("marketStatus") or "")
                cands = [data.get("secondaryData"), data.get("primaryData")] if status != "Closed" \
                    else [data.get("primaryData")]
                for c in cands:
                    if not isinstance(c, dict):
                        continue
                    stamp = str(c.get("lastTradeTimestamp") or "")
                    if want not in stamp.replace(" 0", " ") or (status != "Closed" and "Closed" not in stamp):
                        continue
                    px, chg = num(c.get("lastSalePrice")), num(c.get("netChange"))
                    if not _finite_pos(px) or not math.isfinite(chg):
                        continue
                    if not prev_close or abs((px - chg) / prev_close - 1) > 0.005:
                        continue
                    try:
                        vol = num(c.get("volume"))
                    except ValueError:
                        vol = 0
                    return {"price": px, "volume": int(vol) if _finite_pos(vol) else 0}
            except Exception:
                continue
    return None


def patch_closing_bar(symbol, data):
    """收盘口径：最佳日线停在目标日的前一交易日时，用 Nasdaq 收盘报价补目标日一根（高低记为不可用）。"""
    try:
        if not (CLOSED_ONLY and TARGET_DATE and valid_history(data)):
            return data
        bounds = market_sessions.session_bounds(TARGET_DATE)
        if not bounds or datetime.now(timezone.utc) < bounds[1] + timedelta(minutes=10):
            return data
        if data["dates"][-1] != market_sessions.previous_session(TARGET_DATE):
            return data
        q = nasdaq_closing_quote(symbol, TARGET_DATE, data["closes"][-1])
        if not q:
            return data
        out = dict(data)
        for key in ("dates", "closes", "highs", "lows", "volumes"):
            if data.get(key) is not None:
                out[key] = list(data[key])
        validity = data.get("ma_range_valid")
        out["ma_range_valid"] = list(validity or [True] * len(data["dates"])) + [False]
        out["dates"].append(TARGET_DATE)
        out["closes"].append(q["price"])
        for key in ("highs", "lows"):
            if out.get(key) is not None:
                out[key].append(q["price"])
        if out.get("volumes") is not None:
            out["volumes"].append(q["volume"])
        out.update(price=q["price"], prev_close=out["closes"][-2], realtime=False, rt_ts="",
                   close_patch="nasdaq-quote")
        return out if valid_history(out) else data
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return data


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
                # 兼容其他指标的旧填补行为，但长均线不能把填补值冒充真实日内高低。
                range_valid = [all(i < len(q.get(key) or []) and (q.get(key) or [])[i] is not None
                                   for key in ("high", "low")) for i in valid]
                meta = res.get("meta", {})
                if len(closes) < 30:
                    last_err = "历史数据不足"
                    continue
                YAHOO_GATE.ok()
                dates, closes, vols, highs, lows, range_valid, patched = _yahoo_meta_close(
                    meta, dates, closes, vols, highs, lows, range_valid)
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
                    "ma_range_valid": range_valid,
                    "price": closes[-1],
                    "prev_close": closes[-2] if len(closes) >= 2 else None,
                    "currency": meta.get("currency", ""),
                    "quote_symbol": meta.get("symbol", ""),
                    "quote_name": meta.get("shortName") or meta.get("longName") or "",
                    "quote_type": meta.get("instrumentType", ""),
                    "quote_exchange": meta.get("exchangeName", ""),
                    "quote_timezone": meta.get("exchangeTimezoneName", ""),
                    **({"close_patch": "yahoo-meta"} if patched else {}),
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


def nasdaq_realtime(symbol, asof=None):
    """返回经常规时段和时间戳校验的 price/ts/volume；无效报价返回 None。

    isRealTime 必须为 true，且成交时间属于当前交易日、未超前、延迟不超过五分钟。
    BRK-B 保留 BRK.B 变体及 stocks/etf 尝试；不推定缺失的成交时间。
    """
    if market_sessions.active_session(asof or datetime.now(US_TZ)) is None:
        return None
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
                if p.get("isRealTime") is not True:
                    continue
                if market_sessions.validated_quote_time(
                        p.get("lastTradeTimestamp"), asof or datetime.now(US_TZ)) is None:
                    continue
                px = float(str(p.get("lastSalePrice", "")).replace("$", "").replace(",", ""))
                if not math.isfinite(px) or px <= 0:
                    continue
                # 当日累计成交量：实测实时递增（35 秒内 VRT/AAPL/UNH 全部增加），且不含盘前量。
                # 源给的是带小数的加权值（如 6,549,805.577674），取整后用于盘中量比。
                try:
                    vol = float(str(p.get("volume", "")).replace(",", "") or 0)
                except ValueError:
                    vol = 0.0
                return {"price": px, "ts": p.get("lastTradeTimestamp", ""),
                        "volume": int(round(vol)) if math.isfinite(vol) and vol > 0 else 0}
            except Exception:
                continue
    return None


def apply_realtime(symbol, data, asof=None):
    """校验成交时间和昨收连续性后，在副本合并实时价；失败不污染原始历史。"""
    if not data or CLOSED_ONLY or not S.get("use_realtime"):
        return data
    try:
        session = market_sessions.active_session(asof or datetime.now(US_TZ))
        if session is None or (TARGET_DATE and TARGET_DATE != session[0]) or not valid_history(data):
            return data
        today = session[0]
        dates = data["dates"]
        previous = market_sessions.previous_session(today)
        replacing = dates[-1] == today
        if (dates[-2] if replacing else dates[-1]) != previous:
            return data
        validity = data.get("ma_range_valid")
        if validity and (not isinstance(validity, list) or len(validity) != len(dates)):
            return data
        rt = nasdaq_realtime(symbol) if asof is None else nasdaq_realtime(symbol, asof=asof)
        if not rt or market_sessions.validated_quote_time(
                rt.get("ts"), asof or datetime.now(US_TZ)) is None:
            return data
        px = rt.get("price")
        if isinstance(px, bool) or not isinstance(px, (int, float)) or not math.isfinite(px) or px <= 0:
            return data
        rt_vol = rt.get("volume")
        if (isinstance(rt_vol, bool) or not isinstance(rt_vol, (int, float))
                or not math.isfinite(rt_vol) or rt_vol <= 0):
            rt_vol = 0
        out = dict(data)
        for key in ("dates", "closes", "highs", "lows", "volumes", "ma_range_valid"):
            if data.get(key) is not None:
                out[key] = list(data[key])
        if replacing:
            out["closes"][-1] = px
            if out.get("highs"):
                out["highs"][-1] = max(out["highs"][-1], px)
            if out.get("lows"):
                out["lows"][-1] = min(out["lows"][-1], px)
            if out.get("volumes"):
                out["volumes"][-1] = rt_vol
        else:
            # 仅有成交价，新增 bar 不具备真实日内范围；沿用范围有效位契约。
            out["ma_range_valid"] = list(validity or [True] * len(dates)) + [False]
            out["dates"].append(today)
            out["closes"].append(px)
            for key in ("highs", "lows"):
                if out.get(key):
                    out[key].append(px)
            if out.get("volumes"):
                out["volumes"].append(rt_vol)
        out.update(price=px, prev_close=out["closes"][-2], realtime=True, rt_ts=rt["ts"])
        out.pop("rt_volume", None)
        if rt_vol > 0:
            out["rt_volume"] = rt_vol
        return out if valid_history(out) else data
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as e:
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


# ---------------------------------------------------------------- 下一次财报日期
# 主源 Nasdaq（Zacks）；缺失时按 earnings_sources 的官方白名单补查。
# official-earnings.json 仅存已核验来源快照；活动钟点不当作文件发布钟点。
# 全部非 ETF 个股都查，结果跨次缓存到 status.json（离财报越近刷新越勤）。ETF、指数、期货不适用。
_EARN_CACHE = {}
_EARN_TLS = threading.local()


def _earn_session():
    """财报日期请求每线程复用一个 Session，不与行情线程共享连接。"""
    s = getattr(_EARN_TLS, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({**UA, "Accept": "application/json"})
        _EARN_TLS.s = s
    return s


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
    """只把明确不适用/HTTP 404 记为 na；业务错误、空结构、解析异常均可重试。"""
    key = normalize_symbol(sym).upper()
    if is_etf(key):
        return {"status": "na", "reason": "etf"}
    if key in _EARN_CACHE:
        return _EARN_CACHE[key]
    res = {"status": "error"}
    try:
        url = f"https://api.nasdaq.com/api/analyst/{key.replace('-', '.')}/earnings-date"
        r = _earn_session().get(url, timeout=timeout)
        if r.status_code == 404:
            res = {"status": "na", "reason": "http404"}
        elif r.status_code == 200:
            body = r.json()
            status = body.get("status") if isinstance(body, dict) else None
            data = body.get("data") if isinstance(body, dict) else None
            # Nasdaq 的 HTTP 成功不等于业务成功；缺失状态也不能作为长期负缓存的依据。
            if (isinstance(status, dict) and str(status.get("rCode")) == "200"
                    and not status.get("bCodeMessage") and not status.get("developerMessage")
                    and isinstance(data, dict) and isinstance(data.get("reportText"), str)
                    and data["reportText"].strip()):
                res = parse_earnings_text(data["reportText"], _earn_today())
    except Exception as e:
        log(f"  · {key} 财报日期读取失败：{str(e)[:60]}")
    _EARN_CACHE[key] = res
    return res


def verified_earnings_events(today):
    """读取可追溯来源快照；无效、未来核查日和过期日期均不能补入。"""
    try:
        with open(os.path.join(BASE, "official-earnings.json"), encoding="utf-8") as f:
            raw = json.load(f)
        events = raw.get("events") if isinstance(raw, dict) else None
        if not isinstance(events, dict):
            return {}
        return {sym: dict(e) for sym, e in events.items()
                if _official_earnings_valid(e, sym, today)}
    except (OSError, ValueError, TypeError):
        return {}


def _official_earnings_valid(e, sym, today):
    return (bool(_iso_date(today)) and isinstance(e, dict) and e.get("status") == "ok" and e.get("kind") == "official"
            and bool(_iso_date(e.get("date"))) and e["date"] >= today
            and bool(_iso_date(e.get("official_checked"))) and e["official_checked"] <= today
            and isinstance(e.get("title"), str) and earnings_sources._is_earnings(e["title"])
            and bool(earnings_sources._allowed_url(e.get("source_url"), sym)))


def _official_seed_hash(e):
    return hashlib.sha256(json.dumps({k: e.get(k) for k in
        ("date", "source_url", "title", "official_checked")}, sort_keys=True).encode()).hexdigest()


def _new_official_seed(seed, old, sym, today):
    if not _official_earnings_valid(seed, sym, today):
        return False
    if not _official_earnings_valid(old, sym, today):
        return True
    return (seed["official_checked"] >= old["official_checked"]
            and old.get("official_seed_sha") != _official_seed_hash(seed))


EARN_SA_PRIMARY = True      # 财报日主源＝stockanalysis（页面内嵌 earningsDate / earningsDateLabel）
EARN_CROSS_CHECK = True    # 第三来源（Yahoo）只在前两者都拿不到时兜底；失败/限流一律静默忽略
# ---- 财报日第二来源：stockanalysis.com 个股页内嵌 earningsDate / earningsDateLabel ----
# 为什么换成它：Yahoo quoteSummary 要 cookie+crumb，本机与云端 IP 都经常 429；
# stockanalysis 是服务端渲染的页面（共识价、季度财报都在用），字段就在内嵌对象里：
#   earningsDate:"Oct 20, 2026", earningsDateLabel:"Est. Earnings"（估算时才有 Est. 前缀）
# 所以它既给日期，也直接告诉我们「这是估算还是确认」，不需要再猜。
SA_EARN_URL = "https://stockanalysis.com/stocks/{slug}/"
SA_EARN_RE = re.compile(r'earningsDate:\s*"([A-Z][a-z]{2} \d{1,2}, \d{4})"')
SA_EARN_EST_RE = re.compile(r'earningsDateLabel:\s*"([^"]*)"')
SA_EARN_GATE = SourceGate(threshold=3, cooldown=1800)
_SA_EARN_CACHE = {}
_SA_EARN_SESSION = {"sess": None, "t": 0.0}
_SA_EARN_LOCK = threading.Lock()
_SA_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _sa_earn_session():
    """复用一条会话（连接复用，带仓库 UA，跟共识价一致），避免每次重新握手。"""
    with _SA_EARN_LOCK:
        now = time.monotonic()
        if _SA_EARN_SESSION["sess"] and now - _SA_EARN_SESSION["t"] < 1800:
            return _SA_EARN_SESSION["sess"]
        sess = requests.Session()
        sess.headers.update(CONSENSUS_UA)
        _SA_EARN_SESSION.update(sess=sess, t=now)
        return sess


def parse_stockanalysis_earnings(text, today):
    """从个股页文本取下一次财报日。返回 {'status','date','timing','kind','label'}。
    earningsDateLabel 带 "Est." → estimated；否则 expected（供应商确认）。
    页面没有该字段（部分标的确实没有覆盖）→ unknown，可重试。"""
    m = SA_EARN_RE.search(text or "")
    if not m:
        return {"status": "unknown"}
    try:
        month, day, year = m.group(1).replace(",", "").split(" ")
        date = datetime(int(year), _SA_MONTHS[month], int(day)).date().isoformat()
    except (ValueError, KeyError):
        return {"status": "unknown"}
    if not _iso_date(today) or date < today:
        return {"status": "unknown"}      # 还停在上一期，不拿过去日期冒充
    label = SA_EARN_EST_RE.search(text or "")
    label = label.group(1).strip() if label else ""
    kind = "estimated" if "est" in label.lower() else "expected"
    return {"status": "ok", "date": date, "timing": "", "kind": kind, "label": label}


def stockanalysis_earnings(sym, timeout=15):
    """第二来源。任何失败返回 error（调用方静默忽略）；限流走 SA_EARN_GATE 熔断。"""
    key = normalize_symbol(sym).upper()
    if is_etf(key):
        return {"status": "na", "reason": "etf"}
    if key in _SA_EARN_CACHE:
        return _SA_EARN_CACHE[key]
    slug = consensus_slug(key)
    if not slug:
        return {"status": "na", "reason": "not_supported"}
    res = {"status": "error"}
    try:
        if not SA_EARN_GATE.allow():
            return res
        r = _sa_earn_session().get(SA_EARN_URL.format(slug=slug), timeout=timeout)
        if r.status_code == 429:
            SA_EARN_GATE.fail()
        elif r.status_code == 404:
            res = {"status": "na", "reason": "http404"}
        elif r.status_code == 200:
            SA_EARN_GATE.ok()
            res = parse_stockanalysis_earnings(r.text, _earn_today())
    except Exception as e:
        log(f"  · {key} stockanalysis 财报日期读取失败：{str(e)[:60]}")
    if res.get("status") != "error":
        _SA_EARN_CACHE[key] = res
    return res


_YAHOO_CRUMB = {"crumb": None, "session": None, "t": 0.0}
_YAHOO_CRUMB_LOCK = threading.Lock()
_YAHOO_EARN_CACHE = {}


def _yahoo_crumb_session():
    """Yahoo quoteSummary 需要 cookie + crumb；整轮复用一份，30 分钟后重取。拿不到返回 (None, None)。"""
    with _YAHOO_CRUMB_LOCK:
        now = time.monotonic()
        if _YAHOO_CRUMB["crumb"] and now - _YAHOO_CRUMB["t"] < 1800:
            return _YAHOO_CRUMB["session"], _YAHOO_CRUMB["crumb"]
        sess = requests.Session()
        sess.headers.update({**UA, "Accept": "*/*"})
        sess.get("https://fc.yahoo.com/", timeout=8)                      # 只为拿 cookie，返回 404 也正常
        r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        crumb = (r.text or "").strip()
        if r.status_code == 429:
            YAHOO_GATE.fail()
            return None, None
        if r.status_code != 200 or not crumb or len(crumb) > 64 or "<" in crumb or " " in crumb:
            return None, None
        _YAHOO_CRUMB.update(crumb=crumb, session=sess, t=now)
        return sess, crumb


def parse_yahoo_earnings(body, today):
    """解析 quoteSummary calendarEvents。Yahoo 给的是时间戳（财报电话会钟点），统一换算成美东日历日。
    给出区间（多个时间戳）或 isEarningsDateEstimate=true 时视为估算；只取今天及以后的最早一个。"""
    try:
        res = body["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]
        raws = [d["raw"] for d in (res.get("earningsDate") or []) if isinstance(d, dict)
                and isinstance(d.get("raw"), (int, float)) and not isinstance(d.get("raw"), bool)]
    except (KeyError, IndexError, TypeError):
        return {"status": "unknown"}
    dates = sorted({datetime.fromtimestamp(t, US_TZ).date().isoformat() for t in raws})
    dates = [d for d in dates if d >= today]
    if not dates:
        return {"status": "unknown"}
    estimated = bool(res.get("isEarningsDateEstimate")) or len(dates) > 1
    return {"status": "ok", "date": dates[0], "timing": "", "kind": "estimated" if estimated else "expected"}


def yahoo_earnings(sym, timeout=8):
    """第二来源：Yahoo calendarEvents。任何失败都返回 error（调用方静默忽略），限流走 YAHOO_GATE 熔断。"""
    key = normalize_symbol(sym).upper()
    if is_etf(key):
        return {"status": "na", "reason": "etf"}
    if key in _YAHOO_EARN_CACHE:
        return _YAHOO_EARN_CACHE[key]
    res = {"status": "error"}
    try:
        if not YAHOO_GATE.allow():
            return res
        sess, crumb = _yahoo_crumb_session()
        if not sess:
            return res
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{key}"
        r = sess.get(url, params={"modules": "calendarEvents", "crumb": crumb}, timeout=timeout)
        if r.status_code == 429:
            YAHOO_GATE.fail()
        elif r.status_code == 404:
            res = {"status": "na", "reason": "http404"}
        elif r.status_code == 200:
            YAHOO_GATE.ok()
            res = parse_yahoo_earnings(r.json(), _earn_today())
        elif r.status_code in (401, 403):
            _YAHOO_CRUMB["crumb"] = None                                   # crumb 失效，下次重取
    except Exception as e:
        log(f"  · {key} Yahoo 财报日期读取失败：{str(e)[:60]}")
    if res.get("status") != "error":
        _YAHOO_EARN_CACHE[key] = res
    return res


def cross_check_earnings(primary, secondary, today, secondary_source="yahoo"):
    """两源合并。规则（宁可早提示，不能错过财报）：
    - 日期一致：沿用主源，标 cross=agree。
    - 一个是确认日期、另一个是估算：取确认的；标 cross=conflict，附 alt_date。
    - 同类（都确认或都估算）但日期不同：取较早的；标 cross=conflict，附 alt_date。
    - 主源没有有效日期、第二源有：采用第二源（kind 沿用其确认/估算性质）。
    - 第二源没有有效日期：原样返回主源，不加任何标记。"""
    p_ok = isinstance(primary, dict) and primary.get("status") == "ok" and _iso_date(primary.get("date"))
    s_ok = isinstance(secondary, dict) and secondary.get("status") == "ok" and _iso_date(secondary.get("date"))
    if not s_ok or secondary["date"] < today:
        return primary
    if not p_ok:
        return {**secondary, "source2": secondary_source}
    if primary["date"] == secondary["date"]:
        return {**primary, "cross": "agree"}
    rank = lambda e: 1 if e.get("kind") in ("expected", "official") else 0     # 确认 > 估算
    if rank(secondary) > rank(primary):
        chosen, other = secondary, primary
    elif rank(secondary) < rank(primary):
        chosen, other = primary, secondary
    else:
        chosen, other = (primary, secondary) if primary["date"] <= secondary["date"] else (secondary, primary)
    out = dict(chosen)
    if chosen is secondary:
        out["source2"] = secondary_source
    out.update(cross="conflict", alt_date=other["date"])
    return out


def _with_cross_check(sym, primary, today, force=False):
    """主源已是供应商确认日期时不再多花一次请求；估算/缺失/读取失败才去问第二来源。
    force=True 时无条件问一次（前两个来源都没给出日期，兜底用）。"""
    if not EARN_CROSS_CHECK:
        return primary
    if not force:
        if primary.get("status") == "ok" and primary.get("kind") != "estimated":
            return primary
        if primary.get("status") == "na":
            return primary
    try:
        second = yahoo_earnings(sym)
    except Exception:
        return primary
    merged = cross_check_earnings(primary, second, today)
    return merged if isinstance(merged, dict) else primary


def resolve_earnings_sources(sym, today):
    """财报日取数顺序：stockanalysis（主）→ Nasdaq（补时段 / 交叉验证）→ Yahoo（第三，失败静默）。

    为什么主源换成 stockanalysis：Yahoo 需要 cookie+crumb，本机与云端 IP 频繁 429；
    stockanalysis 页面内嵌 earningsDate + earningsDateLabel，"Est. Earnings" 直接告诉我们是估算。
    Nasdaq 仍要跑，因为只有它给盘前/盘后（页面写「美东 10/20 盘后」靠这个）。
    两源日期不一致时沿用交叉核对规则：确认优先，同类取较早，并在 label 里标「待核实」。"""
    try:
        sa = stockanalysis_earnings(sym) if EARN_SA_PRIMARY else {"status": "error"}
    except Exception:
        sa = {"status": "error"}
    if not isinstance(sa, dict):
        sa = {"status": "error"}
    try:
        nd = nasdaq_earnings(sym)
    except Exception:
        nd = {"status": "error"}
    if not isinstance(nd, dict):
        nd = {"status": "error"}
    if sa.get("status") == "ok" and nd.get("status") == "ok" and sa.get("date") == nd.get("date"):
        # 同一交易日：用 stockanalysis 的确认/估算性质，补上 Nasdaq 的盘前/盘后
        out = dict(sa, cross="agree")
        if nd.get("timing"):
            out["timing"] = nd["timing"]
        return out
    if sa.get("status") == "ok":
        merged = cross_check_earnings(sa, nd, today, secondary_source="nasdaq")
        merged = dict(merged) if isinstance(merged, dict) else dict(sa)
        merged.setdefault("source2", "nasdaq")
        # 日期以 stockanalysis 为准；时段仍可从 Nasdaq 借
        if nd.get("status") == "ok" and nd.get("date") == merged.get("date") and nd.get("timing"):
            merged["timing"] = nd["timing"]
        return merged
    # stockanalysis 没给：Nasdaq 有就直接用；Nasdaq 也没有（error/unknown）才去问 Yahoo 兜一层。
    # Nasdaq 明确 na（ETF／404）说明这一只没有财报日可查，别再打 Yahoo。
    if nd.get("status") == "ok" or nd.get("status") == "na":
        return nd
    return _with_cross_check(sym, nd, today, force=True)


def resolve_earnings(sym, today, previous=None, verified=None):
    """主源缺失才补查官网；已核验官方事件优先，失败保留核查时间和来源。"""
    old = previous if isinstance(previous, dict) else {}
    seed = verified if _official_earnings_valid(verified, sym, today) else {}
    trusted = old if _official_earnings_valid(old, sym, today) else seed
    # 新核验的文件快照比旧运行记录更新时可直接接入，避免重复请求。
    if _new_official_seed(seed, trusted, sym, today):
        trusted = dict(seed, official_seed_sha=_official_seed_hash(seed))
    if trusted and trusted["official_checked"] == today:
        return dict(trusted, official_attempted=today)
    try:
        primary = resolve_earnings_sources(sym, today)
    except Exception:
        primary = {"status": "error"}
    if not isinstance(primary, dict):
        primary = {"status": "error"}
    if not trusted:
        if primary.get("status") == "ok":
            return primary
    if sym not in earnings_sources.SOURCES:
        return primary
    try:
        official = earnings_sources.fetch_official_earnings(sym, today)
    except Exception as exc:
        official = {"status": "error", "reason": type(exc).__name__}
    if not isinstance(official, dict):
        official = {"status": "error", "reason": "invalid_official_result"}
    if _official_earnings_valid(official, sym, today):
        result = {**official, "official_attempted": today}
        if trusted.get("official_seed_sha"):
            result["official_seed_sha"] = trusted["official_seed_sha"]
        return result
    issue = str(official.get("reason") or "official_date_unavailable")[:160]
    if trusted:
        return {**trusted, "official_attempted": today, "official_error": issue}
    return {**primary, "official_attempted": today, "official_error": issue}


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
# 表头已于 2026-10-15 按用户要求整行删除：各行自己写「美东 10/22 盘后」，不需要「名称｜财报」提示
STK_HEAD = ''


def earnings_label(e):
    """返回 (页面文字, 说明) 或 None。时间只写数据源真实提供的：盘前/盘后/盘中，不编造钟点。"""
    if not e or e.get("status") in (None, "na"):
        return None
    status = e["status"]
    if status == "unknown":
        return "日期待确认", "未取得"
    if status == "error":
        return "日期暂未取得", "本轮读取失败，不代表没有财报"
    year, month, day = e["date"].split("-")
    this_year = datetime.now(US_TZ).year
    date_txt = f"{month}/{day}" if int(year) == this_year else f"{year}/{month}/{day}"
    timing = {"pre": "盘前", "post": "盘后", "during": "盘中"}.get(e.get("timing"), "具体时段待定")
    nxt = (datetime(int(year), int(month), int(day)) + timedelta(days=1)).strftime("%m/%d")
    # 财报列只写时间，不展示来源性质；真实时段缺失时不从会议钟点推断。
    note = {"pre": f"北京 {month}/{day} 晚间", "post": f"北京 {nxt} 凌晨",
            "during": f"北京 {month}/{day} 夜–{nxt} 凌晨"}.get(e.get("timing"), "北京 待定")
    if e.get("kind") == "official" and e.get("official_error"):
        note += " · 待复核"
    if e.get("kind") != "official" and e.get("cross") == "conflict" and _iso_date(e.get("alt_date")):
        alt = e["alt_date"][5:].replace("-", "/")
        note += f" · 日期待核实（{alt}）"
    return f"美东 {date_txt} {timing}", note


EARN_SOON_DAYS = 7    # 美东财报日期 D 的前 7 天；以北京日历比较，红字优先于黄字
EARN_BEFORE_DAYS = 1  # 北京日期 D-1、D、D+1 三个完整日统一标红，不因盘前/盘后平移
EARN_AFTER_DAYS = 1   # D+1 仍保留财报名单及主表，D+2 起退出本次事件提醒


def _bj_today():
    """页面上财报的「今天」按北京时间算（用户在北京看页面，盘前=北京晚间、盘后=北京次日凌晨）。"""
    return NOW.astimezone(BJ_TZ).date().isoformat()


def earnings_urgency(e, today=None):
    """以北京今天对比美东日期 D：D-1/D/D+1 三个整日红字；
    D 前 7 天内其余日期黄字，盘前/盘后/盘中/未知时段同一窗口。
    这只是文字及展示条件，不参与报灯；待公布、失败、异常日期均不命中。"""
    if not isinstance(e, dict) or e.get("status") != "ok" or not isinstance(e.get("date"), str):
        return ""
    try:
        today = today or _bj_today()
        base = datetime.strptime(today, "%Y-%m-%d").date()
        event = datetime.strptime(e["date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    gap = (event - base).days
    if -EARN_AFTER_DAYS <= gap <= EARN_BEFORE_DAYS:
        return "red"
    if EARN_BEFORE_DAYS < gap <= EARN_SOON_DAYS:
        return "yellow"
    return ""


def earnings_within_week(e, today=None):
    """红字（D-1/D/D+1）或黄字（D 前 EARN_SOON_DAYS 天内、红字优先）期间为 True：
    个股行不收进「无异动」折叠栏。待公布、失败、日期异常不算。"""
    return bool(earnings_urgency(e, today))


EARN_LIST_DAYS = 15   # 统计面板「财报条件」名单：北京时间今天起至第 15 天（含）；不影响黄字与「无异动」折叠


def earnings_within_list(e, today=None):
    """统计面板财报名单/筛选口径：北京时间今天起至第 EARN_LIST_DAYS 天（含），
    D-1/D/D+1 红字期均保留；D+1 对应的昨天事件额外留在名单中。"""
    if not isinstance(e, dict) or e.get("status") != "ok" or not isinstance(e.get("date"), str):
        return False
    try:
        today = today or _bj_today()
        start = datetime.strptime(today, "%Y-%m-%d").date()
        event = datetime.strptime(e["date"], "%Y-%m-%d").date()
        return -EARN_AFTER_DAYS <= (event - start).days <= EARN_LIST_DAYS
    except (ValueError, TypeError):
        return False


def earnings_soon(e, today=None):
    """财报临近（红或黄）；待公布、失败、日期异常都不算。"""
    return bool(earnings_urgency(e, today))


# 重抓间隔表：(财报日距今至少多少天, 缓存多少天内不重抓)，自上而下取第一条命中的。
# expected = 供应商已确认的日期，改期极少，抓得疏；estimated = 算法估算，临近时可能被确认或调整，抓得勤些。
# 财报日一过（距今 < 0）一律立即重抓下一次；财报日当天及前 3 天，确认过的日期不再追抓。
EARN_REFETCH_DAYS = {
    "expected": ((15, 14), (4, 7), (0, 3)),
    "official": ((15, 7), (0, 3)),
    "estimated": ((15, 7), (0, 1)),   # 估算日期最容易被公司临时改动（SPCX 事故）：15 天内每次运行都复查
}
EARN_UNKNOWN_REFETCH_DAYS = 7    # 本站未取得：每 7 天查一次，不能据此断言公司未公布
EARN_NA_REFETCH_DAYS = 30        # 不适用（如接口无此代码）：每 30 天复查一次
EARN_MAX_FETCH_PER_RUN = 60      # 单次运行最多发这么多财报请求，其余留到下一轮，防止限流


def _earn_today():
    # 财报是自然日事件，不随行情目标交易日回退（周末/节假日也会推进）。
    return NOW.astimezone(US_TZ).date().isoformat()


def _days_between(a, b):
    return (datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days


def _iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def _event_dates(values):
    """只接受有效日期列表；兼容缺失或损坏的旧快照。"""
    return sorted({_iso_date(v) for v in values if _iso_date(v)}) if isinstance(values, (list, tuple)) else []


def _remember_earnings_events(prev, resolved, history, today):
    """下一季日期/unknown 不得抹掉刚发生的财报事件；未来改期只以最新日期为准。"""
    history = history if isinstance(history, dict) else {}
    out = {}
    for k in set(prev) | set(resolved):
        dates = _event_dates(history.get(k))
        for e in (prev.get(k), resolved.get(k)):
            date = _iso_date(e.get("date")) if isinstance(e, dict) and e.get("status") == "ok" else None
            if date and date <= today:
                dates.append(date)
        dates = sorted({d for d in dates if d <= today})[-4:]
        if dates:
            out[k] = dates
    return out


def earnings_refetch_due(entry, today=None):
    """缓存条目是否该重抓。没有缓存、读不懂、日期已过、失败过的一律重抓。"""
    today = today or _earn_today()
    try:
        status = entry.get("status")
        age = _days_between(today, entry["fetched"])
        if age < 0:
            return True                       # 缓存时间在“未来”，时钟或数据异常，重抓
        if status == "na":
            return entry.get("reason") not in ("etf", "http404") or age >= EARN_NA_REFETCH_DAYS
        if status == "unknown":
            return age >= EARN_UNKNOWN_REFETCH_DAYS
        if status != "ok":
            return True
        days = _days_between(entry["date"], today)
    except (AttributeError, KeyError, ValueError, TypeError):
        return True
    if days < 0:
        return True                           # 财报日已过，立即抓下一次
    table = EARN_REFETCH_DAYS.get(entry.get("kind")) or EARN_REFETCH_DAYS["estimated"]
    for min_days, keep in table:
        if days >= min_days:
            return age >= keep
    return True


def _earn_cache_usable(entry, today):
    """缓存里的日期还没过期时，抓取失败也可以继续沿用。"""
    try:
        return entry.get("status") == "ok" and bool(_iso_date(entry.get("date"))) and entry["date"] >= today
    except (AttributeError, KeyError, TypeError):
        return False


def attach_earnings(items, prev=None, today=None, prev_events=None):
    """全部非 ETF 个股都查财报日期，但靠跨次缓存按远近分档刷新，大部分运行只需少量请求。
    抓到的日期全部挂到页面上；财报在 EARN_SOON_DAYS 天内的文字标黄（由 earnings_soon 决定）。
    ETF、指数、期货和参考值不适用。返回统计，其中 data 是下一轮复用的缓存。"""
    today = today or _earn_today()
    prev = prev if isinstance(prev, dict) else {}

    def wants(d):
        sym = d.get("symbol") or ""
        ident = instrument_identity(sym)
        if not sym or d.get("reference_only") or not quote_supported(sym) or is_etf(sym):
            return False
        return not (ident.startswith("^") or ident.endswith("=F"))

    def key_of(d):
        return normalize_symbol(d["symbol"]).upper()

    def priority(d):
        if d.get("group") in ("position", "focus"):
            return 0
        return 1 if d.get("level") in ("red", "yellow") else 2

    targets = [d for d in items if wants(d)]
    stat = {"targets": len(targets), "dated": 0, "pending": 0, "failed": 0,
            "fetched": 0, "cached": 0, "deferred": 0, "data": {}, "events": {}}
    if not targets:
        return stat
    due, resolved = [], {}
    verified = verified_earnings_events(today)
    for d in targets:
        k = key_of(d)
        old = prev.get(k)
        # 新增官方源后仅迁移待确认/不适用项，不重抓已有正常Nasdaq日期。
        needs_official = (k in earnings_sources.SOURCES and isinstance(old, dict)
                          and old.get("status") in ("unknown", "na")
                          and not _iso_date(old.get("official_attempted")))
        seed = verified.get(k)
        old_is_red = (isinstance(old, dict) and old.get("status") == "ok"
                      and _iso_date(old.get("date")) and earnings_urgency(old) == "red")
        keep_recent = old_is_red and old["date"] < today
        seed_is_next_cycle = (isinstance(seed, dict) and _iso_date(seed.get("date"))
                              and old_is_red and _days_between(seed["date"], old["date"]) > EARN_LIST_DAYS)
        if seed_is_next_cycle or (keep_recent and not _new_official_seed(seed, old, k, today)):
            # 财报虽已过美东日期，仍保留北京 D+1；下一季种子不得提前吞掉这次红字。
            resolved[k] = old
        elif _new_official_seed(seed, old, k, today):
            # 同一次事件的官方近日期修订照常接入，不用红字保留拦住真实改期。
            resolved[k] = dict(seed, fetched=today, official_attempted=today,
                               official_seed_sha=_official_seed_hash(seed))
        elif keep_recent:
            resolved[k] = old
        elif isinstance(old, dict) and not needs_official and not earnings_refetch_due(old, today):
            resolved[k] = old
        else:
            due.append(d)
    due.sort(key=priority)                                  # 稳定排序：同级保持原顺序
    todo = {}
    for d in due:
        todo.setdefault(key_of(d), d)
    queue = list(todo)[:EARN_MAX_FETCH_PER_RUN]
    stat["deferred"] = len(todo) - len(queue)
    # 延后不是失败，更不能删除仍有效的旧缓存；保留 fetched，下一轮依然到期。
    for k in list(todo)[len(queue):]:
        if isinstance(prev.get(k), dict):
            resolved[k] = prev[k]
    log(f"财报日期：{len(targets)} 只个股，缓存可用 {len(resolved)}，本轮需抓 {len(queue)}"
        + (f"，{stat['deferred']} 只留到下一轮" if stat["deferred"] else "") + "…")
    try:
        if queue:
            with ThreadPoolExecutor(max_workers=4) as ex:
                got = list(ex.map(lambda k: resolve_earnings(k, today, prev.get(k), verified.get(k)), queue))
            for k, e in zip(queue, got):
                stat["fetched"] += 1
                old = prev.get(k)
                old_is_red = (isinstance(old, dict) and _iso_date(old.get("date"))
                              and earnings_urgency(old) == "red")
                next_cycle = (_iso_date(e.get("date")) and old_is_red
                              and _days_between(e["date"], old["date"]) > EARN_LIST_DAYS)
                if old_is_red and (e.get("status") in ("unknown", "na", "error") or next_cycle):
                    # D-1 仍可核查真实近日期改期，但取数空值/下一季不能抹掉当前红字事件。
                    resolved[k] = old
                elif e.get("status") == "error":
                    old = prev.get(k)
                    if _earn_cache_usable(old, today):
                        resolved[k] = old                   # 日期还有效，继续用旧的，下一轮再试
                    else:
                        resolved[k] = {"status": "error"}   # 不写进缓存，下一轮必重抓
                else:
                    resolved[k] = {**e, "fetched": today}
    except Exception as e:
        log(f"  ! 财报日期读取中断，用已有缓存：{e}")      # 加分项，不能拖垮页面生成
        for k in todo:
            old = prev.get(k)
            if k not in resolved and _earn_cache_usable(old, today):
                resolved[k] = old
    stat["cached"] = len(targets) - len(todo)
    for d in targets:
        k = key_of(d)
        e = resolved.get(k)
        if not e:
            continue
        if e.get("status") != "error":
            stat["data"][k] = e
        if e.get("status") == "na":
            continue
        # 财报按北京 D-1/D/D+1 三整日展示；红字期后，过期缓存不冒充“下一次”。
        expired = e.get("status") == "ok" and not _earn_cache_usable(e, today)
        shown = {"status": "unknown"} if expired and earnings_urgency(e) != "red" else e
        d["earnings"] = shown
        key = {"ok": "dated", "unknown": "pending"}.get(shown.get("status"), "failed")
        stat[key] += 1
    active_prev = {key_of(d): prev.get(key_of(d)) for d in targets}
    stat["events"] = _remember_earnings_events(active_prev, resolved, prev_events, today)
    log(f"  财报日期：已取得 {stat['dated']} · 待确认 {stat['pending']} · 失败 {stat['failed']}"
        f" · 本轮请求 {stat['fetched']}")
    return stat


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


def valid_futures_identity(data, identity):
    """主连只接受供应商明确返回的期货身份，不用ETF、现金指数或CFD冒充。
    ESMAIN 接 Yahoo ES=F 的供应商连续序列；换月/复权口径不承诺与其他平台一致。
    """
    return (isinstance(data, dict) and data.get("quote_symbol") == identity
            and data.get("quote_type") == "FUTURE" and data.get("currency") == "USD"
            and data.get("quote_timezone") == "America/New_York"
            and "CFD" not in str(data.get("quote_name", "")).upper())


_SRC_DAYS = {}               # 本轮各数据源返回的最新日线日期分布：{源: {日期或"无数据": 只数}}
_SRC_DAYS_LOCK = threading.Lock()


def _note_source_day(name, data):
    """记一笔「这个源给到哪天」。只用于日志与 status.json 的 data_lag，不参与取舍。"""
    day = (data.get("dates") or [""])[-1] if isinstance(data, dict) else ""
    with _SRC_DAYS_LOCK:
        bucket = _SRC_DAYS.setdefault(name, {})
        key = day or "无数据"
        bucket[key] = bucket.get(key, 0) + 1


def source_day_summary():
    with _SRC_DAYS_LOCK:
        return {name: dict(sorted(days.items(), reverse=True)) for name, days in _SRC_DAYS.items()}


QUOTE_CLOSED_SOURCES = ("nasdaq", "yahoo", "stooq")
QUOTE_INTRADAY_SOURCES = ("yahoo", "stooq", "nasdaq")


def fetch_history(symbol):
    """普通标的收盘 Nasdaq → Yahoo → stooq，盘中 Yahoo → stooq → Nasdaq。
    指数／期货只取经映射的 Yahoo 日线；先规范化代码，再校验日线与来源身份。
    """
    symbol = normalize_symbol(symbol)
    identity = instrument_identity(symbol)
    is_future = identity.endswith('=F')
    alias = INDEX_YAHOO_ALIAS.get(symbol) or (identity if identity.startswith('^') or is_future else None)
    if identity == '^TNX':
        return None  # 国债收益率不使用报价指数替代百分比口径。
    sources = QUOTE_CLOSED_SOURCES if CLOSED_ONLY else QUOTE_INTRADAY_SOURCES
    history_loaders = {"nasdaq": nasdaq_history, "yahoo": yahoo_history, "stooq": stooq_history}
    loaders = (("yahoo", yahoo_history),) if alias else tuple((name, history_loaders[name]) for name in sources)
    fallback = None
    suspicious = None
    for name, loader in loaders:
        data = loader(alias or symbol)
        if not data:
            _note_source_day(name, None)
            continue
        if is_future and not valid_futures_identity(data, identity):
            log(f"  ! {symbol} 期货身份/币种/日线时区不匹配，拒绝替代报价")
            continue
        if CLOSED_ONLY:
            raw_last = (data.get("dates") or [""])[-1]
            data = trim_history(data, TARGET_DATE)
            # 记录「裁剪后」给到哪天；源给了目标日之后的K线（被裁掉）单独标出来，便于判断是不是换日把当日K线挪走了
            _note_source_day(name if raw_last <= TARGET_DATE else f"{name}(含>{TARGET_DATE}的K线)", data)
        else:
            _note_source_day(name, data)
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
    # 期货按看板目标美股交易日日线观察，不宣称覆盖全部CME夜盘/节假日。
    # 目标日缺失时不能把前一天的期货信号冒充本轮信号，也不借用SPY/标普现金指数。
    if is_future:
        log(f"  ! {symbol} 未取得 {TARGET_DATE} 的有效期货日线，显示无数据")
        return None
    # 收盘口径且日线源都还停在前一交易日：用收盘报价补当天一根（指数/期货不适用）。
    if fallback and CLOSED_ONLY and not alias:
        patched = patch_closing_bar(symbol, fallback)
        if patched is not fallback:
            _note_source_day("nasdaq收盘报价补当日", patched)
            return patched
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
            # 尚未开始：距首个定投日还差几个交易日（last 本身是交易日，不计入）
            return {"every": every, "start": start, "pending": True,
                    "first": first.date().isoformat(),
                    "next_in": len(cal.sessions_in_range(last, first)) - 1}
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


def dca_badge(d):
    """顶栏最右侧的短提醒「定投：N」（N=距定投日还差几个美股交易日）；仅定投日当天黄字。
    完整日期放读屏标签；天数缺失或异常时不显示，避免给出错误天数。"""
    if not isinstance(d, dict):
        return ""
    n = d.get("next_in")
    if type(n) is not int or n < 0:
        return ""
    due = d.get("due") is True and not d.get("pending")
    try:
        detail = f"定投提醒：{dca_text(d)}"
    except (KeyError, TypeError):
        detail = "定投提醒"
    return (f'<span class="dca-badge{" on" if due else ""}" '
            f'aria-label="{html_lib.escape(detail, quote=True)}">定投：{n}</span>')


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


def cost_rule(cfg):
    """读成本价，返回价格；没填或写错一律返回 None。

    cost 必须是大于 0 的有限数字（布尔、字符串都不算），写错就忽略并记一条日志。
    成本价只在 52 周位置条上画一条黄色竖线（在区间外则贴端点并加朝外箭头），
    不参与任何灯级与统计计数。"""
    v = cfg.get("cost")
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        log(f"  ! 成本价无效，已忽略：{v!r}")
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
# 全部分组个股使用手填优先的目标价；ETF、指数、期货不参与目标状态。
# 共识价每只满 14 天到期，财报后至少 2 个美东自然日提前刷新；北京时间每天最多 12 只。
# 获取失败仅灰色虚线展示旧价，不参与空间或达到；有效手填值不受共识失败影响。
# 空间 = (目标价 / 现价 − 1) × 100：低 <30%，中 [30%,70%)，高 ≥70%，不改变红黄绿。
# 达到仅限持仓/重点关注：当日上穿、跌破或处于目标价 ±target_gap_pct% 内；优先于高空间。
CONSENSUS_URL = "https://stockanalysis.com/stocks/{slug}/forecast/"
CONSENSUS_ROBOTS = "https://stockanalysis.com/robots.txt"
CONSENSUS_UA = {"User-Agent": "market-monitor/1.0 (personal dashboard; +https://github.com/nixhuang/market-monitor)"}
CONSENSUS_SOURCE = "stockanalysis.com（S&P Global）"
CONSENSUS_SKIP = {"BRK-B", "BRK-A"}   # 伯克希尔：分析师太少，不抓
CONSENSUS_GAP = 2.0                   # 两次请求的间隔（秒）
CONSENSUS_BUDGET = 7 * 60             # 单次运行最多花这么久；没取完的下次运行接着取
CONSENSUS_BREAK = 3                   # 连续失败这么多次就停手，不再继续敲对方网站
CONSENSUS_GROUPS = ("position", "focus")   # 仅达到规则限这两组；空间规则覆盖全部个股
CONSENSUS_REFETCH_DAYS = 14           # 每只各自满这么多天重取（不再用统一的 1、15 号节拍）
CONSENSUS_NONE_RECHECK_DAYS = 30      # 页面上没有共识价（无覆盖）的，隔这么久再看一次
CONSENSUS_POST_EARN_LAG_DAYS = 2      # 财报后至少等 2 个美东自然日；不保证所有分析师已更新
CONSENSUS_MAX_PER_DAY = 12           # 北京自然日的独立台账，删除/关闭分组不返还额度
CONSENSUS_RETRY_PER_DAY = 3          # 失败重试最多占用 3 个名额，避免挤掉正常到期项
CONSENSUS_RETRY_BACKOFF = (1, 2, 4, 7)  # 连续失败：首次次日重试，此后逐步退避
AMP_GROUPS = ("position", "focus")   # 振幅仅持仓/重点；涨跌幅对所有分组生效
BOLL_UP_SIGNAL_GROUPS = ("position", "focus")  # 上轨普通过热仅持仓/重点；三超卖红覆盖全组，持仓不分长短。
# 单独上轨（RSI 零条）仅持仓提醒；有 RSI 的上轨组合仍按各分组规则处理。

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


def consensus_slug(sym):
    s = normalize_symbol(sym)
    return s.lower().replace("-", ".") if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", s) else None


def consensus_wanted(sym, cfg, group):
    """该不该自动取共识价：任何分组里的美股个股都取；ETF、指数、期货、伯克希尔不取。
    任何个股手填有效目标价就不取（手填优先）。"""
    if not sym or not quote_supported(sym) or is_etf(sym):
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
    """明确无覆盖与页面解析失败分开；不再根据一个批次的数量猜测改版。"""
    try:
        r = sess.get(CONSENSUS_URL.format(slug=consensus_slug(sym)), timeout=25)
    except Exception as e:
        return {"s": "fail", "why": str(e)[:60]}
    if r.status_code == 404:
        return {"s": "none", "reason": "http404"}
    if r.status_code != 200:
        return {"s": "fail", "why": f"HTTP {r.status_code}", "retryable_block": r.status_code in (403, 429)}
    got = parse_consensus(r.text)
    if got:
        return {"s": "ok", **got}
    # 只有来源数据块明确声明零覆盖才进入长期负缓存；空页/挑战页/非美元均保守重试。
    if re.search(r'Targets:\{source:"spg",[^{}]*numPriceTargets:0(?:[,}])', r.text or ""):
        return {"s": "none", "reason": "zero_coverage"}
    return {"s": "fail", "why": "页面结构或共识数据无法解析", "kind": "parse"}


def _consensus_fetch_day(entry):
    date = _iso_date(entry.get("r"))
    if date and entry.get("r_tz") != "America/New_York":
        # 旧 r 是北京日，且更早版本还是1/15号节拍；无法恢复钟点，保守前移一天，宁可补取不漏取。
        return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    return date


def _consensus_events(entry, next_date, events, today):
    """事件与价格缓存独立同步：改期更新未来日期，发生过的事件保留到完成财后刷新。"""
    out = dict(entry) if isinstance(entry, dict) else {}
    pending = _event_dates(out.get("earn_pending")) + _event_dates(events)
    old_ne, new_ne = _iso_date(out.get("ne")), _iso_date(next_date)
    if old_ne and old_ne < today:
        pending.append(old_ne)
    if new_ne:
        out["ne"] = new_ne
    ne = _iso_date(out.get("ne"))
    if ne and ne <= today:
        pending.append(ne)
    fetched = _consensus_fetch_day(out)
    pending = sorted({d for d in pending if d <= today and
                      (not fetched or _days_between(fetched, d) < CONSENSUS_POST_EARN_LAG_DAYS)})
    if pending:
        out["earn_pending"] = pending[-4:]
    else:
        out.pop("earn_pending", None)
    if ne and ne < today and ne not in pending:
        out.pop("ne", None)
    return out


def consensus_priority(entry, today_iso, quota_day=None):
    """0 新加入，1 失败重试，2 财后到期，3 常规14天，4 无覆盖30天；事件按美东日。"""
    quota_day = quota_day or today_iso
    if not isinstance(entry, dict) or entry.get("s") not in ("ok", "none", "fail"):
        return 0
    if entry["s"] == "fail":
        retry = _iso_date(entry.get("next_retry"))
        return None if entry.get("try") == quota_day or (retry and retry > quota_day) else 1
    try:
        age = _days_between(today_iso, entry["r"])
    except (KeyError, ValueError, TypeError):
        return 3
    if age < 0:
        return 3
    if entry["s"] == "none":
        return 4 if entry.get("reason") not in ("http404", "zero_coverage") or age >= CONSENSUS_NONE_RECHECK_DAYS else None
    pending = _event_dates(entry.get("earn_pending"))
    if _iso_date(entry.get("ne")):
        pending.append(entry["ne"])
    if any(_days_between(today_iso, d) >= CONSENSUS_POST_EARN_LAG_DAYS and
           _days_between(_consensus_fetch_day(entry), d) < CONSENSUS_POST_EARN_LAG_DAYS for d in pending):
        return 2
    return 3 if age >= CONSENSUS_REFETCH_DAYS else None


def consensus_due(entry, today_iso):
    return consensus_priority(entry, today_iso) is not None


def _consensus_failed(old_entry, today_iso, why):
    old_entry = old_entry if isinstance(old_entry, dict) else {}
    count = old_entry.get("failures", 0)
    count = max(0, count) if isinstance(count, int) and not isinstance(count, bool) else 0
    count = min(count + 1, len(CONSENSUS_RETRY_BACKOFF))
    retry = (datetime.strptime(today_iso, "%Y-%m-%d") + timedelta(days=CONSENSUS_RETRY_BACKOFF[count - 1])).date().isoformat()
    e = {"s": "fail", "r": old_entry.get("r", ""), "try": today_iso, "why": why,
         "failures": count, "next_retry": retry}
    for k in ("avg", "upd", "ne", "earn_pending", "r_tz"):
        if old_entry.get(k) not in (None, ""):
            e[k] = old_entry[k]
    return e


def _consensus_quota(prev, quota_day):
    """独立台账不受当前清单裁剪影响；旧快照从所有条目的 try 迁移。"""
    prev = prev if isinstance(prev, dict) else {}
    prior_stat = prev.get("stat") if isinstance(prev.get("stat"), dict) else {}
    ledger = prev.get("quota") or prior_stat.get("quota")
    ledger = ledger if isinstance(ledger, dict) and ledger.get("date") == quota_day else {}
    raw_symbols, raw_retries = ledger.get("symbols"), ledger.get("retry_symbols")
    symbols = {s for s in raw_symbols if isinstance(s, str)} if isinstance(raw_symbols, list) else set()
    retries = {s for s in raw_retries if isinstance(s, str)} if isinstance(raw_retries, list) else set()
    old = prev.get("data") if isinstance(prev.get("data"), dict) else {}
    for s, e in old.items():
        if isinstance(e, dict) and e.get("try") == quota_day:
            symbols.add(s)
    return {"date": quota_day, "symbols": sorted(symbols), "retry_symbols": sorted(retries)}


def update_consensus(symbols, prev, today_iso, sess=None, sleep=time.sleep, clock=time.monotonic,
                     force=False, next_earn=None, earnings_events=None, quota_day=None):
    """事件/TTL 用美东日，额度用独立北京日；force 仅强制到期，不绕过日额度或同日去重。"""
    quota_day = quota_day or today_iso
    old = prev.get("data") if isinstance(prev, dict) else None
    old = {k: v for k, v in (old or {}).items() if isinstance(v, dict)} if isinstance(old, dict) else {}
    next_earn = next_earn if isinstance(next_earn, dict) else {}
    earnings_events = earnings_events if isinstance(earnings_events, dict) else {}
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    data = {s: _consensus_events(old.get(s), next_earn.get(s), earnings_events.get(s), today_iso) for s in syms}
    ledger = _consensus_quota(prev, quota_day)
    attempted, retried = set(ledger["symbols"]), set(ledger["retry_symbols"])
    ranked = []
    for s in syms:
        rank = 0 if force else consensus_priority(data[s], today_iso, quota_day)
        if rank is not None and s not in attempted:
            ranked.append((rank, str(data[s].get("r") or ""), s))
    ranked.sort()
    quota = max(0, CONSENSUS_MAX_PER_DAY - len(attempted))
    retry_left = max(0, CONSENSUS_RETRY_PER_DAY - len(retried))
    todo = []
    for rank, _, s in ranked:
        if len(todo) >= quota:
            break
        is_retry = data[s].get("s") == "fail"
        if is_retry and not retry_left:
            continue
        if is_retry:
            retry_left -= 1
        todo.append(s)
    stat = {"due": len(ranked), "deferred": len(ranked), "tried": 0, "ok": 0, "none": 0,
            "fail": 0, "stopped": "", "quota": ledger}
    # 未取到任何数据的全新代码不制造空缓存，已知事件元数据则照常保留。
    data = {s: e for s, e in data.items() if e}
    if not todo:
        return data, stat
    sess = sess or requests.Session()
    sess.headers.update(CONSENSUS_UA)
    if not consensus_robots_ok(sess):
        stat["stopped"] = "robots"
        log("  · 共识价：robots.txt 不允许或读取失败，本轮不取，沿用旧值")
        return data, stat
    log(f"  取共识价：{len(ranked)} 只到期，本轮最多取 {len(todo)} 只，每只间隔 {CONSENSUS_GAP:g} 秒…")
    start, streak = clock(), 0
    for i, s in enumerate(todo):
        if i:
            sleep(CONSENSUS_GAP)
        if clock() - start > CONSENSUS_BUDGET:
            stat["stopped"] = "budget"
            break
        entry = data.get(s, {})
        attempted.add(s)
        if entry.get("s") == "fail":
            retried.add(s)
        ledger.update(symbols=sorted(attempted), retry_symbols=sorted(retried))
        try:
            res = fetch_consensus_one(sess, s)
        except Exception as e:
            res = {"s": "fail", "why": str(e)[:60]}
        stat["tried"] += 1
        stat[res["s"]] += 1
        if res["s"] == "fail":
            data[s] = _consensus_failed(entry, quota_day, res.get("why", ""))
            # 单页解析失败不能阻塞正常标的；连续网络/HTTP错误仍熔断保护供应商。
            streak = 0 if res.get("kind") == "parse" else streak + 1
            if streak >= CONSENSUS_BREAK:
                stat["stopped"] = "blocked"
                break
        else:
            streak = 0
            fresh = {**res, "r": today_iso, "r_tz": "America/New_York", "try": quota_day}
            for k in ("ne", "earn_pending"):
                if k in entry:
                    fresh[k] = entry[k]
            data[s] = _consensus_events(fresh, next_earn.get(s), earnings_events.get(s), today_iso)
    stat["deferred"] = len(ranked) - stat["tried"]
    log(f"  共识价：成功 {stat['ok']} · 无覆盖 {stat['none']} · 失败 {stat['fail']}"
        + (f" · 提前停止（{stat['stopped']}）" if stat["stopped"] else ""))
    return data, stat


def _next_earnings_dates(earn_stat):
    """上一次 status.json 里的财报缓存 → {代码: 下一次财报日}；格式不对返回空字典。"""
    data = earn_stat.get("data") if isinstance(earn_stat, dict) else None
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): v["date"] for k, v in data.items()
            if isinstance(v, dict) and v.get("status") == "ok" and isinstance(v.get("date"), str)}


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


def target_alerts(price, prev, target, label, gap_pct):
    """仅计算达到，返回具体上穿/跌破/区间文字；空间不参与达到条件。"""
    if _pos_num(price) is None or _pos_num(target) is None:
        return []
    tstr = _price_text(target)
    if _pos_num(prev) is not None and prev < target <= price:
        return [f"今日上穿{label} {tstr}"]
    if _pos_num(prev) is not None and prev > target >= price:
        return [f"今日跌破{label} {tstr}"]
    p, t = Decimal(str(price)), Decimal(str(target))
    dev = (p / t - 1) * 100
    if abs(p - t) * 100 <= Decimal(str(gap_pct)) * t:
        return [f"在{label} {tstr} ±{gap_pct:g}% 内（偏离 {dev:+.1f}%）"]
    return []


def evaluate_target(price, prev, target, group, gap_pct):
    """返回新目标字典（或 None）；调用方先确认个股身份，不修改输入。"""
    if not target:
        return None
    t = {key: value for key, value in target.items() if key != "anomaly"}
    t.update(space=None, hit=False, space_hit=False, band=None,
             space_detail="", reached_detail="")
    if t.get("failed") or _pos_num(t.get("price")) is None or _pos_num(price) is None:
        t["failed"] = True
        return t
    p, tp = Decimal(str(price)), Decimal(str(t["price"]))
    space = float((tp / p - 1) * 100)
    # 十进制交叉相乘判边界，避免 1.3/1.7 的二进制浮点误差。
    band = ("high" if tp * 100 >= p * (100 + TARGET_HIGH_SPACE_PCT)
            else "medium" if tp * 100 >= p * (100 + TARGET_SPACE_PCT) else "low")
    for boundary in (TARGET_SPACE_PCT, TARGET_HIGH_SPACE_PCT):
        if tp * 100 == p * (100 + boundary):
            space = float(boundary)
    reached = (target_alerts(price, prev, t["price"], target_label(t), gap_pct)
               if group in CONSENSUS_GROUPS else [])
    space_hit = band in ("medium", "high")
    detail = (f"{target_label(t)} {_price_text(t['price'])}，"
              f"空间 {space:+.1f}%") if space_hit else ""
    t.update(failed=False, space=space, hit=bool(reached), space_hit=space_hit, band=band,
             space_detail=detail, reached_detail=" / ".join(reached))
    return t


def target_display_state(t):
    """纯函数：failed/reached/high/space/low；达到优先，缺行情不推断状态。"""
    if not isinstance(t, dict) or t.get("failed") or _pos_num(t.get("price")) is None:
        return "failed"
    space = t.get("space")
    if isinstance(space, bool) or not isinstance(space, (int, float)) or not math.isfinite(space):
        return "failed"
    if t.get("hit"):
        return "reached"
    if t.get("band") == "high" and t.get("space_hit"):
        return "high"
    if t.get("band") == "medium" and t.get("space_hit"):
        return "space"
    return "low"


def _consensus_description(text, target):
    """只识别自动共识描述；手填目标及原技术文字不按关键字误删。"""
    return (isinstance(target, dict) and target.get("kind") == "consensus" and isinstance(text, str)
            and text.startswith(("共识价", "旧共识价", "共识获取失败",
                                 "今日上穿共识价", "今日跌破共识价", "在共识价")))


def _target_stock(d):
    symbol = d.get("symbol")
    identity = instrument_identity(symbol)
    asset = str(d.get("asset") or "").lower()
    return bool(symbol and quote_supported(symbol) and not d.get("reference_only")
                and not d.get("etf") and not is_etf(symbol)
                and asset not in ("etf", "index", "future", "futures")
                and not identity.startswith("^") and not identity.endswith("=F"))


def _target_active(d):
    return _target_stock(d) and target_display_state(d.get("target")) in ("reached", "high", "space")


def _apply_target(d):
    """在副本中迁移旧条目；只用真实目标与行情重算，不相信旧紫计数或异常灯。"""
    out = dict(d)
    marks = list(d.get("signal_marks") or [])
    target = d.get("target")
    if isinstance(target, dict) and _target_stock(d):
        if not all(key in target for key in ("band", "space_hit", "space_detail", "reached_detail")):
            prev = _pos_num(d.get("prev_close")) or _pos_num(d.get("prev"))
            chg = d.get("chg")
            if (prev is None and _pos_num(d.get("price")) is not None
                    and type(chg) in (int, float) and math.isfinite(chg) and chg > -100):
                prev = d["price"] / (1 + chg / 100)
            target = evaluate_target(d.get("price"), prev, target, d.get("group"), S["target_gap_pct"])
        else:
            target = {key: value for key, value in target.items() if key != "anomaly"}
            if target.get("failed") or _pos_num(d.get("price")) is None:
                target = evaluate_target(d.get("price"), None, target, d.get("group"), S["target_gap_pct"])
            elif d.get("group") not in CONSENSUS_GROUPS:
                target.update(hit=False, reached_detail="")
    else:
        target = None
    out["target"] = target
    out["purple"] = _target_stock(out) and target_display_state(target) in ("reached", "high", "space")
    out["signal_marks"] = [mark for mark in marks if mark[0] != "target"]
    if (any(kind == "target" and level in ("red", "yellow") for kind, level in marks)
            or isinstance(d.get("target"), dict) and d["target"].get("anomaly")):
        remaining = {level for _, level in out["signal_marks"]}
        out["level"] = ("red" if "red" in remaining else "yellow" if "yellow" in remaining
                        else "green" if _pos_num(d.get("price")) is not None else "gray")
    out["signals"] = [text for text in d.get("signals") or []
                      if not _consensus_description(text, d.get("target") or {})
                      and not (not d.get("target") and any(kind == "target" for kind, _ in marks)
                               and text == "目标价疑似数据异常")]
    if out["purple"]:
        for field in ("reached_detail", "space_detail"):
            if target.get(field) and target[field] not in out["signals"]:
                out["signals"].append(target[field])
        out["signal_marks"].append(("target", "purple"))
    return out


def target_overlay_members(items):
    """空间与达到分别去重，沿用首页身份优先级；两段允许同一身份重叠。"""
    priority = {g["item_group"]: i for i, g in enumerate(GROUPS)}
    seen = set(RISK_IDENTITIES)
    out = {"space": [], "reached": []}
    for item in sorted(items, key=lambda d: priority.get(d.get("group"), len(GROUPS))):
        symbol = normalize_symbol(item.get("symbol"))
        identity = instrument_identity(symbol)
        if identity in seen:
            continue
        seen.add(identity)
        d = _apply_target(item)
        if not _target_active(d):
            continue
        target = d["target"]
        record = {"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                  "hold": str(d.get("hold") or ""), "level": d.get("level") or "",
                  "space": target["space"], "band": target["band"], "price": target["price"],
                  "kind": target.get("kind") or ""}
        if target.get("space_hit"):
            out["space"].append({**record, "detail": target["space_detail"]})
        if target.get("hit") and d.get("group") in CONSENSUS_GROUPS:
            out["reached"].append({**record, "detail": target["reached_detail"]})
    return out


def _target_overlay_union(overlays):
    """旧紫成员接口使用两段的身份并集，并保留重叠标的的两条具体说明。"""
    members = {}
    for section in ("space", "reached"):
        for record in overlays.get(section) or []:
            identity = instrument_identity(record["symbol"])
            if identity not in members:
                members[identity] = dict(record)
            elif record.get("detail"):
                members[identity]["detail"] += " / " + record["detail"]
    return list(members.values())


def purple_counts(items):
    return len(_target_overlay_union(target_overlay_members(items)))


def target_tag(t):
    """目标标签：低/中/高空心，达到实心，失败灰虚线且不显示旧空间。"""
    if not t:
        return ""
    state = target_display_state(t)
    cls = "tgt " + {"failed": "bad", "reached": "hit", "high": "high",
                    "space": "space", "low": "low"}[state]
    parts = []
    if state != "failed":
        # 30/70边界附近保留小数，避免69.9%被四舍五入显示为高档70%。
        space_text = f'{t["space"]:+.1f}'.rstrip('0').rstrip('.')
        parts.append(f'<span>{space_text}%</span>')
    if _pos_num(t.get("price")) is not None:
        parts.append(f'<span>{_price_text(t["price"])}</span>')
    if state != "failed" and t.get("month"):
        parts.append(f'<span>· {html_lib.escape(str(t["month"]))}月</span>')
    if state == "failed":
        parts.append('<span>获取失败</span>')
    body = " ".join(parts)
    return f'<span class="tgs"><span class="lvtag {cls}">{body}</span></span>'


MA_CONFIRM_LOOKBACK = 10   # 50 日线确认穿越：最近多少个交易日内收盘价曾在均线另一侧
MA_CONFIRM_MIN, MA_CONFIRM_MAX, MA_CONFIRM_DEFAULT = 0.5, 3.0, 1.0
MA_LONG_BAND_PCT = 1.0     # 第二条均线：当日日线高低范围触及本轮SMA±1%独立亮黄；穿越独立判断


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
    ("ma", "均线"), ("low52", "52周低位"),
    ("add_price", "加仓价"), ("reduce_price", "减仓价"), ("target", "共识价"),
)



VOL_RATIO_BASE = 20    # 量比的均量窗口：最近完整日量 ÷ 此前 20 个完整交易日均量
VOL_Z_WINDOW = 60      # 量比 z 值的回看长度：这只股自己 60 个交易日的放量分布


def _latest_completed_session_index(dates, asof=None):
    """最近一个已结束的美股交易日在日线序列里的下标；不可用返回 None。

    盘中排除今天未完结的日线，收盘后留 15 分钟给日线汇总；历史锚点不向后读未来日线。
    """
    if not isinstance(dates, list) or not dates:
        return None
    try:
        expected = market_sessions.latest_completed_session(asof or NOW, TARGET_DATE)
        return dates.index(expected) if dates.count(expected) == 1 else None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def completed_day_volume_ratio(sym, data, asof=None):
    """最近一个已结束的美股交易日成交量 / 此前20个完整交易日均量。

    盘中排除今天未完结的日线，收盘后留15分钟给日线汇总；行情源缺量、
    落后于最近完整交易日或历史不足时不报放量。期货/指数没有同一股票日历口径。
    """
    if instrument_identity(sym).startswith('^') or instrument_identity(sym).endswith('=F'):
        return None, ''
    dates, volumes = data.get('dates') or [], data.get('volumes') or []
    if len(dates) != len(volumes) or len(dates) < VOL_RATIO_BASE + 1:
        return None, ''
    index = _latest_completed_session_index(dates, asof)
    if index is None:
        return None, ''
    # 不跳过缺量日凑均值：分子与此前20根日线必须来自同一行情源且有效。
    sample = volumes[index-VOL_RATIO_BASE:index+1]
    if len(sample) != VOL_RATIO_BASE + 1 or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                                                or not math.isfinite(v) or v <= 0 for v in sample):
        return None, ''
    average = sum(sample[:-1]) / VOL_RATIO_BASE
    return (sample[-1] / average, dates[index]) if average > 0 else (None, '')


def _volume_ratio_zscore(volumes, index, window=VOL_Z_WINDOW, base=VOL_RATIO_BASE):
    """把 index 那天的量比换算成这只股自己的「放量 z 值」。

    样本：此前 window 个完整交易日各自的 ln(当日量 ÷ 此前 base 日均量)。
    尺度用中位数与 MAD（1.4826×MAD ≈ 标准差），单日天量不会把门槛整体抬走；
    平稳的票 1.3× 也可能是异常，常年巨量的票 2× 也未必异常，门槛随股而异。
    数据不足、量非正、分布尺度为 0（天天一模一样）时返回 None，调用方退回固定倍数。
    """
    start = index - window
    if start - base < 0:
        return None
    window_vols = volumes[start - base:index + 1]
    if len(window_vols) != window + base + 1:
        return None
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
           for v in window_vols):
        return None
    logs = []
    for t in range(base, len(window_vols)):
        mean_prev = sum(window_vols[t - base:t]) / base
        if mean_prev <= 0:
            return None
        logs.append(math.log(window_vols[t] / mean_prev))
    if len(logs) != window + 1:
        return None
    median = statistics.median(logs)
    scale = 1.4826 * statistics.median([abs(value - median) for value in logs])
    if not math.isfinite(scale) or scale <= 0:
        return None
    return (logs[-1] - median) / scale


def completed_day_volume_z(sym, data, asof=None):
    """最近完整日量比的「自身波动 z 值」；算不出返回 None，由调用方退回固定倍数。"""
    if instrument_identity(sym).startswith('^') or instrument_identity(sym).endswith('=F'):
        return None
    dates, volumes = data.get('dates') or [], data.get('volumes') or []
    if not isinstance(dates, list) or not isinstance(volumes, list) or len(dates) != len(volumes):
        return None
    index = _latest_completed_session_index(dates, asof)
    if index is None:
        return None
    try:
        return _volume_ratio_zscore(volumes, index)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


INTRADAY_VOL_QUIET_MIN = 15   # 开盘后这段分钟数内不报盘中量比：分母太小会把比值放大到没有意义


def intraday_volume_ratio(sym, data, asof=None):
    """盘中量比 =（当日累计成交量 ÷ 已开盘分钟数）÷（此前20个完整交易日均量 ÷ 当日总交易分钟数）。

    用「每分钟成交速度」相比，时间进度天然被约掉，所以盘中不用等到收盘也能算。
    /info 的累计量实测实时递增且不含盘前；均量窗口沿用 completed_day_volume_ratio 的 20 日。
    期货/指数不走同一股票日历口径；静默期内、非交易时段、源没给量或历史不足时一律不报。
    rt_ts 必须通过时效校验，进度与返回标签均取成交时间；此前20个日期必须连续且完整。
    """
    if instrument_identity(sym).startswith('^') or instrument_identity(sym).endswith('=F'):
        return None, ''
    vol = data.get("rt_volume")
    if not isinstance(vol, (int, float)) or isinstance(vol, bool) or not math.isfinite(vol) or vol <= 0:
        return None, ''
    dates, volumes = data.get('dates') or [], data.get('volumes') or []
    if (not isinstance(dates, list) or not isinstance(volumes, list)
            or len(dates) != len(volumes) or len(dates) < VOL_RATIO_BASE + 1):
        return None, ''
    try:
        # 实时量以成交时点计进度；NOW 是任务启动时间，不能拿它误判抓取后的报价为未来。
        now = asof or datetime.now(US_TZ)
        session = market_sessions.active_session(now)
        quoted = market_sessions.validated_quote_time(data.get("rt_ts"), now)
        if (not session or quoted is None or dates[-1] != session[0]
                or (TARGET_DATE and TARGET_DATE != session[0])):
            return None, ''
        if dates[-VOL_RATIO_BASE-1:-1] != market_sessions.preceding_sessions(session[0], VOL_RATIO_BASE):
            return None, ''
        elapsed = (quoted - session[1]).total_seconds() / 60
        total = (session[2] - session[1]).total_seconds() / 60
        if elapsed < INTRADAY_VOL_QUIET_MIN or total <= 0:
            return None, ''
        sample = volumes[-VOL_RATIO_BASE-1:-1]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool)
               or not math.isfinite(v) or v <= 0 for v in sample):
            return None, ''
        average = sum(sample) / VOL_RATIO_BASE
        if average <= 0:
            return None, ''
        return (vol / elapsed) / (average / total), quoted.strftime("%H:%M")
    except (ValueError, TypeError, KeyError, OverflowError):
        return None, ''


def _range52_from_history(data):
    """仅用同一日线序列的真实高低价给价格位置条供数；缺失/错位不拿收盘价冒充极值。"""
    dates, highs, lows = (data.get(key) for key in ("dates", "highs", "lows"))
    validity = data.get("ma_range_valid") or None   # 空列表当「源没给有效位」，不当成整段无效
    price = data.get("price")
    valid_num = lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and value > 0)
    if (not isinstance(dates, list) or len(dates) < 30 or any(not isinstance(day, str) for day in dates)
            or not isinstance(highs, list) or not isinstance(lows, list)
            or len(dates) != len(highs) or len(dates) != len(lows)
            or not valid_num(price)
            or (validity is not None and (not isinstance(validity, list)
                or len(validity) != len(dates)))):
        return None
    try:
        end = datetime.fromisoformat(dates[-1]).date()
        cutoff = (end - timedelta(weeks=52)).isoformat()
        start = next((i for i, day in enumerate(dates) if day >= cutoff), len(dates))
        if (start >= len(dates) or (validity is not None and not all(v is True for v in validity[start:]))
                or any(not valid_num(high) or not valid_num(low) or low > high
                       for high, low in zip(highs[start:], lows[start:]))):
            return None
        low, high = min(lows[start:]), max(highs[start:])
        if low >= high or not low <= price <= high:
            return None
        first = datetime.fromisoformat(dates[start]).date()
        scope = "52周" if (end - first).days >= 357 else "可用历史"
        return {"low": low, "high": high, "position": (price - low) / (high - low) * 100,
                "scope": scope, "from": dates[start], "to": dates[-1]}
    except (ValueError, TypeError, OverflowError):
        return None


def _analysis_context(sym, cfg, data, group):
    """把一只标的需要的指标一次算好，各条规则只读不算。"""
    closes = data["closes"]
    price = data["price"]
    prev = data["prev_close"]
    x = SimpleNamespace(sym=sym, cfg=cfg, data=data, group=group,
                        closes=closes, price=price, prev=prev)
    x.hold = str((cfg or {}).get("hold") or "short").lower()
    x.cost = cost_rule(cfg or {})   # 成本价：只在位置条上画黄竖线，不参与报灯
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
    x.low52 = low52          # 52 周区间最低价本身：统计名单里要写出这个价格
    x.range52 = _range52_from_history(data)
    x.vol_ratio, x.vol_ratio_date = completed_day_volume_ratio(sym, data)
    x.vol_ratio_z = completed_day_volume_z(sym, data)
    x.rt_vol_ratio, x.rt_vol_time = intraday_volume_ratio(sym, data)
    x.vol_ratio_intraday = bool(x.rt_vol_ratio)
    ma_periods = (int(S["ma_short"]), int(S["ma_long"]))
    x.moving_averages = [(period, sma(closes, period)) for period in dict.fromkeys(ma_periods) if period > 0]
    x.boll_mid, x.boll_up, x.boll_dn = boll(closes, int(S["boll_n"]), float(S["boll_k"]))
    return x


def _apply_vol_tier(x):
    """涨跌 / 振幅阈值：设置页的值是中档，按这只股自己的平时波动缩放；
    涨跌幅对所有分组生效；振幅仅「持仓」和「重点关注」报灯。"""
    x.vol_tier, x.vol_sigma = vol_profile(x.closes)
    mult = VOL_MULT[x.vol_tier]
    x.chg_red, x.chg_yellow = S["chg_red"] * mult, S["chg_yellow"] * mult
    x.amp_red, x.amp_yellow = S["amp_red"] * mult, S["amp_yellow"] * mult
    x.usual = f"（平时约 ±{x.vol_sigma:.1f}%）" if x.vol_sigma is not None else ""
    x.amp_on = x.group in AMP_GROUPS


def _rule_chg_red(al, x):
    if x.chg is not None and abs(x.chg) >= x.chg_red:
        al.add("chg", f"异动 {x.chg:+.1f}%{x.usual}", "red")


def _rule_chg_yellow(al, x):
    if x.chg is not None and x.chg_yellow <= abs(x.chg) < x.chg_red:
        al.add("chg", f"波动 {x.chg:+.1f}%{x.usual}", "yellow")


def classify_technical_facts(group, high_hits, low_hits, selected_side):
    """纯事实分类，不计算 RSI 或轨距，也不推断选中轨。

    selected_side 必须明确传 None / 上轨 / 下轨。命中条数为 0..3 整数且和不超过 3。
    返回 kind、level、foldable：kind=None / level=green 表示无提醒；gray 表示技术文字，
    不改变主表整体绿灯；foldable 是独立于红黄灯的折叠资格。
    """
    if not isinstance(group, str) or not group:
        raise ValueError("group 必须是非空分组名称")
    if (type(high_hits) is not int or type(low_hits) is not int
            or not 0 <= high_hits <= 3 or not 0 <= low_hits <= 3
            or high_hits + low_hits > 3):
        raise ValueError("RSI 命中条数必须为 0..3 整数，且超买与超卖条数之和不超过 3")
    if selected_side not in (None, "上轨", "下轨"):
        raise ValueError("selected_side 必须明确为 None、上轨或下轨")
    active_up = group in BOLL_UP_SIGNAL_GROUPS
    count = max(high_hits, low_hits)
    if selected_side is None:
        if count < 2:
            return {"kind": None, "level": "green", "foldable": True}
        level = "red" if low_hits == 3 else "yellow" if high_hits == 3 and active_up else "gray"
        return {"kind": "rsi", "level": level, "foldable": True}
    up = selected_side == "上轨"
    if count == 0:
        if up and group != "position":
            return {"kind": None, "level": "green", "foldable": True}
        return {"kind": "boll", "level": "gray", "foldable": True}
    foldable = not (not up and count == 1 or up and not active_up and low_hits == 2)
    # 三超卖全覆盖优先于上轨分组豁免；有轨时只归组合。
    if low_hits == 3:
        level = "red"
    elif up and active_up and (high_hits >= 2 or low_hits == 2):
        level = "red" if high_hits == 3 else "yellow"
    elif not up and count >= 2:
        level = "yellow"
    else:
        level = "gray"
    return {"kind": "boll_rsi", "level": level, "foldable": foldable}


def _rule_rsi(al, x):
    """无轨用纯事实 API 判灯；有轨只保留 RSI 事实，由布林选轨后统一归组合。"""
    x.high_hits = sum(v is not None and math.isfinite(v) and v >= S["rsi_high"] for v in x.rsi.values())
    x.low_hits = sum(v is not None and math.isfinite(v) and v <= S["rsi_low"] for v in x.rsi.values())
    facts = (classify_technical_facts(x.group, x.high_hits, x.low_hits, None)
             if not getattr(x, "boll_hits", []) else None)
    if facts:
        x.foldable = facts["foldable"]
    for count, tag, boundary in ((x.high_hits, "超买", f"≥{S['rsi_high']:g}"),
                                 (x.low_hits, "超卖", f"≤{S['rsi_low']:g}")):
        if count >= 2:
            text = f"RSI {count}条{tag}（{boundary}）"
            if facts and facts["level"] in ("red", "yellow"):
                al.add(facts["kind"], text, facts["level"])
            else:
                al.signals.append(text)
    # 已有两条同侧提醒时，补齐反向单条事实；单独一条仍然无提醒。
    if max(x.high_hits, x.low_hits) >= 2:
        for count, tag, boundary in ((x.high_hits, "超买", f"≥{S['rsi_high']:g}"),
                                     (x.low_hits, "超卖", f"≤{S['rsi_low']:g}")):
            if count == 1:
                al.signals.append(f"RSI 1条{tag}（{boundary}）")


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
    price, target = Decimal(str(x.price)), Decimal(str(trig))
    distance = price - target
    tstr = f"{trig:,.2f}"
    if distance <= 0:
        al.add("add_price", f"已跌破加仓价 {tstr}", "red")
    elif distance * 100 <= Decimal(str(S["trigger_gap_pct"])) * target:
        gap = distance * 100 / target
        al.add("add_price", f"距加仓价 {tstr} 还差 {gap:.1f}%", "red")


def _rule_reduce_price(al, x):
    """减仓价：只对「持仓」分组生效，和加仓价对称：
    现价已涨到减仓价以上（不管高出多少）→ 红；还没到但距离不足 x%（按减仓价算）→ 红。"""
    x.reduce = reduce_rule(x.cfg) if x.group == "position" else None
    if not x.reduce:
        return
    price, target = Decimal(str(x.price)), Decimal(str(x.reduce))
    distance = target - price
    rstr = f"{x.reduce:,.2f}"
    if distance <= 0:
        rgap = -distance * 100 / target
        al.add("reduce_price", f"已涨到减仓价 {rstr}（高出 {rgap:.1f}%）", "red")
    elif distance * 100 <= Decimal(str(S["reduce_gap_pct"])) * target:
        gap = distance * 100 / target
        al.add("reduce_price", f"距减仓价 {rstr} 还差 {gap:.1f}%", "red")


def _rule_target(al, x):
    """所有分组个股计算空间；达到仅持仓/重点，二者只叠加紫色，不改原灯级。"""
    stock = {"symbol": x.sym, "asset": x.data.get("asset") or x.cfg.get("asset"),
             "etf": x.cfg.get("etf"), "reference_only": x.data.get("reference_only")}
    x.tgt = (evaluate_target(x.price, x.prev, resolve_target(x.cfg, x.cfg.get("_consensus")),
                             x.group, S["target_gap_pct"]) if _target_stock(stock) else None)
    if not x.tgt or x.tgt.get("failed"):
        return
    for field in ("reached_detail", "space_detail"):
        if x.tgt.get(field):
            al.add("target", x.tgt[field], "purple")


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


# 随机游走下，20 日窗口内「收盘价 − MA20」的标准差 ≈ 1.29 × 日收益率标准差。
# 用它把 vol_profile 估出的日波动换算成布林带宽（带宽 = 上下各 2σ ≈ 5.16 × 日σ），
# 逼近门槛就随个股波动自动缩放。刻意不用「当日带宽」：财报、暴跌会让带宽暴涨，
# 门槛跟着变宽等于越危险越迟钝；vol_profile 用的是 60 日中位数绝对偏差，抗异常值。
_BOLL_SIGMA_FACTOR = 1.29


def _boll_near_pct(x):
    """本只标的的「算不算贴近轨道」门槛（%）。
    优先按个股平时波动归一化后的布林带宽取比例；算不出波动（新股、样本不足）才回退固定百分比。"""
    band_pct = float(S.get("boll_near_band_pct") or 0)
    sigma = getattr(x, "vol_sigma", None)
    if band_pct > 0 and isinstance(sigma, (int, float)) and not isinstance(sigma, bool) \
            and math.isfinite(sigma) and sigma > 0:
        return band_pct / 100 * 4 * _BOLL_SIGMA_FACTOR * sigma
    return float(S["boll_near_pct"])


def _boll_hits(x):
    """布林上下轨命中情况 → [(还差百分之多少, 文字)]；<=0 表示已经穿过去了。
    另外标出「连续第几日贴近轨道」——远离一天就断，再贴近重新从第一日算。"""
    near = _boll_near_pct(x)
    streak = boll_streak(x.closes, int(S["boll_n"]), float(S["boll_k"]), near,
                         highs=x.data.get("highs"), lows=x.data.get("lows"))
    hits = []
    if x.boll_up is not None:
        gap_up = (x.boll_up - x.touch_up) / x.boll_up * 100
        if gap_up <= near:
            if gap_up < 0:
                action = "突破" if x.price >= x.boll_up else "盘中突破"
                text = f"{action}布林上轨"
            elif gap_up == 0:
                text = f"触及布林上轨"
            else:
                text = f"逼近布林上轨 还差{gap_up:.2f}%"
            hits.append((gap_up, text + _touch_note(x.price, x.touch_up) + streak_tail(streak["up"], "上轨")))
    if x.boll_dn is not None:
        gap_dn = (x.touch_dn - x.boll_dn) / x.boll_dn * 100
        if gap_dn <= near:
            text = (f"跌破布林下轨" if gap_dn < 0
                    else f"触及布林下轨" if gap_dn == 0
                    else f"逼近布林下轨 还差{gap_dn:.2f}%")
            hits.append((gap_dn, text + _touch_note(x.price, x.touch_dn) + streak_tail(streak["dn"], "下轨")))
    return hits


def _rule_boll(al, x):
    """先按带符号距离选轨（相等上轨），再按分组限制；双轨保留双方事实，不换边。"""
    hits = x.boll_hits if hasattr(x, "boll_hits") else _boll_hits(x)
    if not hits:
        return
    best = min(hits, key=lambda h: h[0])
    up = "布林上轨" in best[1]
    side = "上轨" if up else "下轨"
    facts = classify_technical_facts(x.group, x.high_hits, x.low_hits, side)
    x.foldable = facts["foldable"]
    if len(hits) == 1 and facts["kind"] is None:
        return
    x.boll_alert_text = best[1]
    al.signals.extend(hit[1] for hit in hits)
    same_hits = x.high_hits if up else x.low_hits
    oppo_hits = x.low_hits if up else x.high_hits
    # 单条 RSI 仅在触轨时补文字；不把单独 RSI 1 条变成提醒。
    for count, tag, boundary in ((x.high_hits, "超买", f"≥{S['rsi_high']:g}"),
                                 (x.low_hits, "超卖", f"≤{S['rsi_low']:g}")):
        if count == 1:
            text = f"RSI 1条{tag}（{boundary}）"
            if text not in al.signals:
                al.signals.append(text)
    level = facts["level"]
    if level not in ("red", "yellow"):
        return
    if same_hits >= 2:
        x.boll_rsi_direction = f"{side}（{'超买' if up else '超卖'}）"
        x.boll_rsi_hits = same_hits
    else:
        x.boll_rsi_direction = f"{side}（RSI{'超卖' if up else '超买'}，方向不一致）"
        x.boll_rsi_hits = oppo_hits
    al.add(facts["kind"], f"布林 + RSI → {'红' if level == 'red' else '黄'}", level)


def _vol_ratio_z_enabled():
    """收盘后的「自身波动」自适应开关：两条 z 阈值都大于 0 才生效，否则退回固定倍数。"""
    return S.get("vol_ratio_z", 0) > 0 and S.get("vol_ratio_red_z", 0) > 0


def _rule_vol_ratio(al, x):
    """量比：盘中用当日累计量按已开盘分钟数折算的「盘中量比」，收盘后回落到最近完整日量比。
    收盘后先按这只股自己的放量分布（z 值）判灯，z 算不出才退回固定倍数；盘中始终用固定倍数。
    达到红线报红、达到黄线报黄，所有分组适用；不分涨跌方向。"""
    if getattr(x, "vol_ratio_intraday", False):
        ratio = x.rt_vol_ratio
        level = "red" if ratio >= S["vol_ratio_red"] else "yellow" if ratio >= S["vol_ratio"] else ""
        if level:
            al.add("vol_ratio", f"盘中量比（截至 {x.rt_vol_time} ET）{ratio:.1f}x", level)
        return   # 盘中已拿到当时量比，不再叠加昨天的完整日口径
    if not x.vol_ratio:
        return
    ratio = x.vol_ratio
    if _vol_ratio_z_enabled() and ratio >= 1.0 and isinstance(getattr(x, "vol_ratio_z", None), (int, float)) \
            and not isinstance(x.vol_ratio_z, bool) and math.isfinite(x.vol_ratio_z):
        # 缩量（<1×）不报灯：z 值只用来判断「放得有多异常」，方向仍然是放量。
        z = x.vol_ratio_z
        level = "red" if z >= S["vol_ratio_red_z"] else "yellow" if z >= S["vol_ratio_z"] else ""
        if level:
            # 不把 σ 写进文案：z 只是内部判定尺度，页面只需要「哪天、放量到几倍」
            al.add("vol_ratio", f"最近完整日量比（{x.vol_ratio_date}）{ratio:.1f}x", level)
        return
    level = "red" if ratio >= S["vol_ratio_red"] else "yellow" if ratio >= S["vol_ratio"] else ""
    if level:
        al.add("vol_ratio", f"最近完整日量比（{x.vol_ratio_date}）{ratio:.1f}x", level)


def _rule_amplitude(al, x):
    """日内振幅：涨跌幅只看「收盘 vs 昨收」，盘中冲高又回落、最后收平的票不会响，这条专门抓它。
    用当日最高/最低（盘中重跑时再和当前价取极值）除以昨收。"""
    x.amp = None
    if x.touch_up is None or x.touch_dn is None or not x.prev:
        return
    x.amp = (x.touch_up - x.touch_dn) / x.prev * 100
    if not x.amp_on:
        return
    text = f"振幅 {x.amp:.1f}%（{x.touch_dn:,.2f}~{x.touch_up:,.2f}）"
    if x.amp >= x.amp_red:
        al.add("amp", text, "red")
    elif x.amp >= x.amp_yellow:
        al.add("amp", text, "yellow")


def _ma_day_range(x):
    """只用与本轮收盘序列对齐的最后一根高低价；缺失/损坏退回现价，不冒充完整日内范围。"""
    data = getattr(x, "data", {})
    highs, lows = data.get("highs"), data.get("lows")
    validity = data.get("ma_range_valid") or None   # 空列表当「源没给有效位」，与缺失同处理
    if validity is not None and (not isinstance(validity, (list, tuple))
                                 or len(validity) != len(x.closes) or not validity or validity[-1] is not True):
        return x.price, x.price, False
    if (isinstance(highs, (list, tuple)) and isinstance(lows, (list, tuple))
            and len(highs) == len(lows) == len(x.closes) and highs and lows):
        high, low = highs[-1], lows[-1]
        if (all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and v > 0 for v in (high, low)) and low <= high):
            return min(low, x.price), max(high, x.price), True
    return x.price, x.price, False


def _rule_moving_averages(al, x):
    """第一条确认穿越，第二条日内范围触及加简单穿越；0独立关闭。
    第二条以昨日收盘/昨日SMA和现价/本轮SMA比较，不要求带宽确认。
    两条启用且同周期时，保留原先去重后的简单穿越规则。"""
    closes, price, mas = x.closes, x.price, x.moving_averages
    if len(closes) < 2:
        return
    prev_c = closes[-2]
    for period, ma_val in mas:
        ma_name = f"{period}日"
        if not ma_val:
            continue
        weak = period == S["ma_short"] and S["ma_short"] != S["ma_long"]
        strong = period == S["ma_long"] and S["ma_short"] != S["ma_long"]
        if strong:
            # 源降级可能返回旧日线：不要把昨日触及/穿越重复说成本轮目标日事件。
            dates = getattr(x, "data", {}).get("dates") or []
            if TARGET_DATE and (not dates or dates[-1] != TARGET_DATE):
                continue
            gap_ma = (price / ma_val - 1) * 100
            day_low, day_high, has_range = _ma_day_range(x)
            band = MA_LONG_BAND_PCT / 100.0
            eps = ma_val * 1e-11
            if day_low <= ma_val * (1 + band) + eps and day_high >= ma_val * (1 - band) - eps:
                if has_range:
                    text = (f"日内触及{ma_name}均线 ±{MA_LONG_BAND_PCT:g}%区间"
                            f"（日内 {day_low:,.2f}~{day_high:,.2f}，现价偏离 {gap_ma:+.1f}%）")
                else:
                    text = f"现价贴近{ma_name}均线 {gap_ma:+.1f}%（日内高低缺失，仅按现价）"
                x.ma_band_direction = "up" if price > ma_val else "down" if price < ma_val else "level"
                al.add("ma_band", text, "yellow")
            prev_ma = sma(closes[:-1], period)
            if prev_ma:
                if prev_c < prev_ma and price >= ma_val:
                    al.add("ma_cross", f"上穿{ma_name}均线", "yellow")
                elif prev_c > prev_ma and price <= ma_val:
                    al.add("ma_cross", f"跌破{ma_name}均线", "yellow")
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
        "low52": getattr(x, "low52", None),
        "range52": x.range52,
        "vol_ratio": x.vol_ratio,
        "vol_ratio_z": getattr(x, "vol_ratio_z", None),
        "vol_ratio_date": x.vol_ratio_date,
        "rt_vol_ratio": x.rt_vol_ratio,
        "rt_vol_time": x.rt_vol_time,
        "vol_ratio_intraday": x.vol_ratio_intraday,
        "amp": x.amp,
        "vol_tier": x.vol_tier,
        "vol_sigma": x.vol_sigma,
        "trigger": x.trig,
        "reduce": x.reduce,
        "cost": getattr(x, "cost", None),
        "target": x.tgt,
        "purple": bool(x.tgt and (x.tgt.get("hit") or x.tgt.get("space_hit"))),
        "prev_close": x.prev,
        "asset": x.data.get("asset") or x.cfg.get("asset"),
        "source": x.data.get("source", ""),
        "data_date": (x.data.get("dates") or [""])[-1],
        "realtime": bool(x.data.get("realtime")),
        "rt_ts": x.data.get("rt_ts", ""),
        "group": x.group,
        "hold": getattr(x, "hold", ""),
        "boll_up": x.boll_up,
        "boll_dn": x.boll_dn,
        "boll_rsi_direction": getattr(x, "boll_rsi_direction", ""),
        "boll_rsi_hits": getattr(x, "boll_rsi_hits", None),
        "boll_alert_text": getattr(x, "boll_alert_text", ""),
        "ma_band_direction": getattr(x, "ma_band_direction", ""),
        "ma_settings": {key: S.get(key) for key in ("ma_short", "ma_long")},
        # ETF 标签：静态表命中，或行情源明确给出 assetclass=etf
        "etf": is_etf(x.sym) or x.data.get("asset") == "etf",
        "signals": al.signals,
        "signal_marks": al.marks,
        "level": al.level,
        "foldable": getattr(x, "foldable", True),
    }


def analyze_symbol(sym, cfg, data, group="technology"):
    """返回等级、信号和详情，分组定义见 groups.json。
    规则按「红 → 加减仓价/目标价 → 布林 → 黄」的顺序依次跑，顺序也是页面上警示文字的顺序。"""
    x = _analysis_context(sym, cfg, data, group)
    al = _Alerts()
    _apply_vol_tier(x)
    _intraday_extremes(x)
    x.boll_hits = _boll_hits(x)
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


# 保留公开常量兼容旧调用；render 每次重新读取资源内容计算 hash。
RUN_JS = script_asset(BASE, "run-status.js")


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
            # 全部拿到当日价时这一串（当日价 99 只 / 较早日线 0 只 / 无数据 0 只）是废话，
            # 只在真的有「较早日线」或「无数据」时才写出来，且只写非零的那几项
            detail = ""
            if "today_prices" in counts:
                parts = []
                if counts.get("prior_prices") or counts.get("missing_prices"):
                    if counts.get("today_prices"):
                        parts.append(f"当日价 {counts['today_prices']} 只")
                    if counts.get("prior_prices"):
                        parts.append(f"较早日线 {counts['prior_prices']} 只")
                    if counts.get("missing_prices"):
                        parts.append(f"无数据 {counts['missing_prices']} 只")
                detail = (" · " + " / ".join(parts)) if parts else ""
            if latest == plan["target"]:
                return finished[:19].replace("T", " ") + "（盘中快照，北京时间）" + detail
            return finished[:19].replace("T", " ") + f"（盘中运行，北京时间 · 仍为 {date_text} 日线）" + detail
    return date_text + " 收盘（美东交易日）"


def signal_counts(items):
    """各类信号的触发数量，写进 status.json，方便看「黄灯到底是被哪条规则点亮的」。
    red / yellow：这一类信号以红 / 黄级别触发的标的数；purple：目标价提示数。
    只出文字不报灯的标的（灰灯）不在这里统计，单独走 text_only_members()。"""
    members = signal_count_members(items)
    return {kind: {"label": label, **{level: len(members[kind][level])
                                    for level in ("red", "yellow", "purple")}}
            for kind, label in SIGNAL_KINDS}


_TEXT_RSI_RE = re.compile(r"^RSI [123]条(超买|超卖)")
_TEXT_BOLL_PREFIX = ("逼近布林", "触及布林", "跌破布林", "突破布林", "盘中突破布林")


def _text_only_kind(signals):
    """技术灰归因：单独 RSI 至少两条才归 rsi；只有布林文字归 boll；
    两者都有归 boll_rsi（含单条 RSI），不重复归入单独指标。"""
    texts = [str(s) for s in (signals or []) if isinstance(s, str)]
    has_rsi = any(_TEXT_RSI_RE.match(t) for t in texts)
    has_boll = any(t.startswith(_TEXT_BOLL_PREFIX) for t in texts)
    if has_rsi and has_boll:
        return "boll_rsi"
    if has_rsi and any(re.match(r"^RSI [23]条(超买|超卖)", t) for t in texts):
        return "rsi"
    if has_boll:
        return "boll"
    return ""


def _text_only_shape(signals):
    """灰灯名单里的方向：优先看布林轨道，没有布林文字才看 RSI 的买卖方向。"""
    texts = [str(s) for s in (signals or []) if isinstance(s, str)]
    boll = next((t for t in texts if t.startswith(_TEXT_BOLL_PREFIX)), "")
    if boll:
        return "▲" if "上轨" in boll else "▼"
    rsi = next((_TEXT_RSI_RE.match(t) for t in texts if _TEXT_RSI_RE.match(t)), None)
    return ("▲" if rsi.group(1) == "超买" else "▼") if rsi else ""


def text_only_members(items):
    """技术灰按所属指标独立计，与同股其它指标红黄并存；组合与单独指标互斥。
    主表最高灯级不变；重复身份沿用首页分类优先级，只保留第一条。"""
    priority = {g["item_group"]: i for i, g in enumerate(GROUPS)}
    seen = set(RISK_IDENTITIES)
    out = []
    for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(GROUPS))):
        symbol = normalize_symbol(d.get("symbol"))
        identity = instrument_identity(symbol)
        if identity in seen:
            continue
        seen.add(identity)
        if (d.get("level") == "gray" or d.get("reference_only")
                or "price" in d and (not isinstance(d["price"], (int, float))
                    or isinstance(d["price"], bool) or not math.isfinite(d["price"]) or d["price"] <= 0)):
            continue
        kind = _text_only_kind(d.get("signals"))
        technical_marks = {k for k, level in d.get("signal_marks") or []
                           if k in ("boll", "rsi", "boll_rsi") and level in ("red", "yellow")}
        # 同一组技术事实已有红黄就不能再计灰；涨跌幅/均线等不影响技术灰资格。
        if not kind or technical_marks:
            continue
        texts = [str(s) for s in (d.get("signals") or []) if isinstance(s, str)]
        selected = d.get("boll_alert_text")
        if isinstance(selected, str) and selected in texts and selected.startswith(_TEXT_BOLL_PREFIX):
            texts = [selected] + [text for text in texts if text != selected]
        # 只留主句：括号里的「盘中触及 / 连续第几日贴近」太长，占满一行反而不好看。
        # 组合被挡下的（boll_rsi）优先说轨道，因为那才是被规则拦下的原因。
        pool = [t for t in texts if t.startswith(_TEXT_BOLL_PREFIX)]
        if kind != "boll_rsi":
            pool = [t for t in texts
                    if _TEXT_RSI_RE.match(t) or t.startswith(_TEXT_BOLL_PREFIX)]
        detail = re.split(r"（", pool[0])[0].strip() if pool else ""
        rsi_texts = [t for t in texts if _TEXT_RSI_RE.match(t)]
        rsi_main = " / ".join(dict.fromkeys(re.split(r"（", t)[0].strip() for t in rsi_texts))
        if kind == "boll_rsi" and rsi_main:
            detail = f"{detail} · {rsi_main}" if detail else rsi_main
        elif kind == "rsi":
            detail = rsi_main
        boll_text = next((t for t in texts if t.startswith(_TEXT_BOLL_PREFIX)), "")
        # 非持仓零条上轨被限制后，不能借保留的另一轨事实改归下轨灰灯。
        if kind == "boll" and "上轨" in boll_text and d.get("group") != "position":
            continue
        rsi_match = max((_TEXT_RSI_RE.match(t) for t in rsi_texts),
                        key=lambda match: int(match.group(0)[4]), default=None)
        out.append({"symbol": symbol, "kind": kind, "shape": _text_only_shape(texts),
                    "side": ("上轨" if "上轨" in boll_text else "下轨") if boll_text else "",
                    "rsi_n": int(rsi_match.group(0)[4]) if rsi_match else 0,
                    "rsi_side": rsi_match.group(1) if rsi_match else "",
                    "detail": detail, "group": d.get("group") or "",
                    "level": d.get("level") if d.get("level") in ("red", "yellow", "green") else "green",
                    "hold": str(d.get("hold") or ""),
                    "note": str(d.get("note") or "")})
    return out


def _ma_display_text(text, settings=None):
    """均线展示投影：使用该事实的配置归属，不改变内部周期、原始信号或计算。
    历史周期不匹配时明确待确认；同周期共用短/长身份，绝不拆成两条计数。"""
    config = settings if isinstance(settings, dict) else S
    if " / " in str(text):
        return " / ".join(_ma_display_text(part, config) for part in str(text).split(" / "))
    def name(match):
        period = int(match[1])
        short, long = period == config.get("ma_short"), period == config.get("ma_long")
        return "短均线/长均线" if short and long else "短均线" if short else "长均线" if long else "均线（归属待确认）"
    output = re.sub(r"(\d+)日(?:弱)?均线", name, str(text))
    named = set(re.findall(r"(?:短|长)均线", output))
    if len(named) == 1:
        label = next(iter(named))
        output = output.replace("现价在均线", "现价在" + label).replace("现价与均线", "现价与" + label)
    return output


def _member_direction_markers(d, kind):
    """仅从本轮已经触发的信号取方向；图形只描述触轨/穿越位置，不预测后续涨跌。"""
    signals = [s for s in (d.get("signals") or []) if isinstance(s, str)]
    markers = []
    def add(shape, tone, label):
        marker = {"shape": shape, "tone": tone, "label": label}
        if marker not in markers:
            markers.append(marker)
    if kind == "rsi":
        for s in signals:
            if not s.startswith("RSI "):
                continue
            for match in re.finditer(r"([123])条(超买|超卖)", s):
                count, side = int(match[1]), match[2]
                add("▲" if side == "超买" else "▼", "red" if count == 3 and side == "超卖" else "yellow",
                    f"RSI{count}条{side}")
    elif kind in ("chg", "amp", "vol_ratio"):
        chg = d.get("chg")
        if isinstance(chg, (int, float)) and not isinstance(chg, bool) and math.isfinite(chg) and chg != 0:
            day = d.get("data_date")
            day = day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "本轮行情日"
            add("▲" if chg > 0 else "▼", "white",
                f"{day}股价{'上涨' if chg > 0 else '下跌'}{abs(chg):.2f}%")
    elif kind == "boll":
        # 新快照只取真正触发黄灯的那条；上轨仅作说明时不制造黄灯图标。
        if "boll_alert_text" in d:
            signals = [d["boll_alert_text"]] if d["boll_alert_text"] else []
        for s in signals:
            for prefix, shape, tone, label in (
                    ("盘中突破布林上轨", "▲", "yellow", "盘中突破上轨"),
                    ("突破布林上轨", "▲", "yellow", "突破上轨"),
                    ("跌破布林下轨", "▼", "yellow", "盘中跌破下轨"),
                    ("触及布林上轨", "▲", "white", "触及上轨"),
                    ("逼近布林上轨", "▲", "white", "逼近上轨"),
                    ("触及布林下轨", "▼", "white", "触及下轨"),
                    ("逼近布林下轨", "▼", "white", "逼近下轨")):
                if s.startswith(prefix):
                    add(shape, tone, label)
                    break
    elif kind == "ma":
        marks = {k for k, _ in d.get("signal_marks") or []}
        if marks & {"ma", "ma_cross"}:
            markers.extend(_member_direction_markers(d, "ma_cross"))
        if marks & {"ma", "ma_band"}:
            side = d.get("ma_band_direction")
            for s in signals:
                match = re.match(r"^(日内触及|现价贴近)(\d+)日(?:弱)?均线", s)
                if match and side in ("up", "down", "level"):
                    label = f"{match[1]}{match[2]}日均线，" + {
                        "up": "现价在均线上方", "down": "现价在均线下方", "level": "现价与均线相等"}[side]
                    add({"up": "▲", "down": "▼", "level": "■"}[side], "cyan", label)
    elif kind == "ma_cross":
        for s in signals:
            match = re.match(r"^(上穿|跌破)(\d+)日(?:弱)?均线", s)
            if match:
                add("▲" if match[1] == "上穿" else "▼", "cyan",
                    f"{match[1]}{match[2]}日均线")
    elif kind == "ma_band":
        side = d.get("ma_band_direction")
        if side in ("up", "down", "level"):
            shape = {"up": "▲", "down": "▼", "level": "■"}[side]
            for text in signals:
                match = re.match(r"^(日内触及|现价贴近)(\d+)日(?:弱)?均线", text)
                if match:
                    position = {"up": "现价在均线上方", "down": "现价在均线下方",
                                "level": "现价与均线相等"}[side]
                    add(shape, "cyan", f"{match[1]}{match[2]}日均线，{position}")
    elif kind == "target":
        for s in signals:
            if s.startswith(("今日上穿目标价", "今日上穿共识价")):
                add("▲", "purple", "当日上穿目标价或共识价")
            elif s.startswith(("今日跌破目标价", "今日跌破共识价")):
                add("▼", "purple", "当日跌破目标价或共识价")
            elif s.startswith(("在目标价", "在共识价")) and " ±" in s:
                add("■", "purple", "现价在目标价或共识价范围内")
            elif s.startswith(("目标价 ", "共识价 ")) and "，空间 +" in s:
                pct = re.search(r"，空间 \+(\d+(?:\.\d+)?)%", s)
                add("□", "purple", f"目标价或共识价上涨空间达到 {pct[1]}%" if pct
                    else "目标价或共识价上涨空间达到提示标准")
    return markers


def _member_metric(d, kind):
    """仅把本轮已触发信号的数值带入成员明细；旧快照缺值时不回查行情。"""
    if kind not in ("chg", "amp", "vol_ratio"):
        return ""
    if kind == "vol_ratio" and d.get("vol_ratio_intraday"):
        value = d.get("rt_vol_ratio")
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                or value < 0):
            return ""
        moment = d.get("rt_vol_time")
        moment_text = (f"（截至 {moment} ET）" if isinstance(moment, str)
                       and re.fullmatch(r"\d{2}:\d{2}", moment) else "")
        return f"盘中量比 {value:.1f}×{moment_text}"
    value = d.get({"chg": "chg", "amp": "amp", "vol_ratio": "vol_ratio"}[kind])
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
            or (kind != "chg" and value < 0)):
        return ""
    if kind == "chg":
        return f"涨跌幅 {value:+.2f}%"
    if kind == "amp":
        return f"振幅 {value:.1f}%"
    day = d.get("vol_ratio_date")
    date_text = f"（{day}完整日）" if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""
    # σ 不出现在页面上：判灯照常用 z，展示只留「哪天、几倍」
    return f"量比 {value:.1f}×{date_text}"


# 方向提到「框标题」上的栏目：名单里不再逐条画三角，▲/▼ 只出现在每个框的标题里。
# 量比按涨跌、布林+RSI 按上下轨、均线穿越按上穿/跌破、长均线触及按现价在均线哪一侧。
# 只改这几栏——涨跌幅、振幅等栏目行内三角照旧，别把没要求的栏目一起改了。
_BLOCK_PANEL_KINDS = ("vol_ratio", "boll_rsi", "ma")

# 框标题已经写明方向的栏目，行尾不再重复同一句话（布林+RSI 曾经标题行尾都写
# 「上轨 · RSI 2条过界」）。量比那种带数值的照旧保留。
_NO_ROW_METRIC_KINDS = ("boll_rsi",)


def _member_block(d, kind):
    """分框栏目里这一行归到哪个框，返回 (图标, 框标题)。

    量比按当天股价涨跌分上下；布林+RSI 按轨道 + RSI 条数分上下；
    方向算不出来时给「平盘 / 方向待下次更新」，不硬塞进 ▲ 或 ▼。"""
    if kind == "vol_ratio":
        # 名单里存的是已经算好的方向标记，不回查 chg（成员字典里没有行情字段）
        for marker in d.get("indicators") or []:
            if isinstance(marker, dict) and marker.get("shape") in ("▲", "▼"):
                return (marker["shape"], "上涨" if marker["shape"] == "▲" else "下跌")
        return ("", "平盘")   # 平盘或旧快照缺方向
    if kind == "boll_rsi":
        state = str(d.get("direction") or "")
        hits = d.get("boll_rsi_hits")
        side = next((s for s in ("上轨", "下轨") if state.startswith(s)), "")
        if side and type(hits) is int and hits in (2, 3):
            tail = ("方向不一致" if "不一致" in state
                    else "双向命中" if "均命中" in state else "")
            title = f"{side} · RSI {hits}条过界" + (f" · {tail}" if tail else "")
            return ("▲" if side == "上轨" else "▼", title)
        return ("", "方向待下次更新")
    if kind == "ma":
        markers = [m for m in d.get("indicators") or [] if isinstance(m, dict)
                   and m.get("shape") in ("▲", "▼", "■") and m.get("label")]
        if not markers:
            facts = [f.get("text") for f in d.get("ma_facts") or []
                     if isinstance(f, dict) and isinstance(f.get("text"), str)]
            return ("", _ma_display_text(" / ".join(facts), d.get("ma_settings")) or "方向待下次更新")
        shapes = {m["shape"] for m in markers}
        labels = []
        for m in markers:
            label = str(m["label"])
            match = re.match(r"^(上穿|跌破)(\d+)日均线$", label)
            if match:
                label = f"{match[2]}日均线{match[1]}"
            label = _ma_display_text(label, d.get("ma_settings"))
            labels.append((f'{m["shape"]} ' if len(shapes) > 1 else "") + label)
        return (markers[0]["shape"] if len(shapes) == 1 else "", " / ".join(dict.fromkeys(labels)))
    if kind in ("ma_cross", "ma_band"):
        # 旧成员仍可读取；对外统计统一使用 ma。
        for marker in d.get("indicators") or []:
            if not (isinstance(marker, dict) and marker.get("shape") in ("▲", "▼", "■")):
                continue
            label = str(marker.get("label") or "")
            match = re.match(r"^(上穿|跌破)(\d+)日均线$", label)
            if match:
                # 标题说「250日均线上穿」，跟用户口头说法一致
                label = f"{match.group(2)}日均线{match.group(1)}"
            return (marker["shape"], _ma_display_text(label, d.get("ma_settings")) or ("均线穿越" if kind == "ma_cross" else "日内触及长均线"))
        return ("", "方向待下次更新")
    return None


def _member_belong(d):
    """名单里的分组标识，写在代码后面；持仓内部按 hold 分长短，跟首页两区一致。"""
    group = str(d.get("group") or "")
    if group == "position":
        return "长持仓" if str(d.get("hold") or "short").lower() == "long" else "短持仓"
    return GROUP_LABELS.get(group) or "未分类"


def _member_metric_text(d, kind):
    """成员行里跟在代码后面的数值；只带本轮已算出来的值，缺就留空不编。"""
    if kind in ("chg", "amp", "vol_ratio"):
        return d.get("metric") or "数值待下次更新"
    if kind == "rsi":
        return d.get("rsi_hits") or "条数待下次更新"
    if kind == "boll":
        return d.get("boll_state") or "状态待下次更新"
    if kind == "boll_rsi":
        # 轨道方向要写成可见文字（手机没有悬停提示），不能只靠箭头上的 title
        state = str(d.get("direction") or "")
        hits = d.get("boll_rsi_hits")
        side = next((s for s in ("上轨", "下轨") if state.startswith(s)), "")
        if side and type(hits) is int and hits in (2, 3):
            tail = ("方向不一致" if "不一致" in state
                    else "双向命中" if "均命中" in state else "")
            return f"{side} · RSI {hits}条过界" + (f" · {tail}" if tail else "")
        return "条数待下次更新"
    if kind == "low52":
        # 52周低位把低点价本身写出来：只说「距低点 +2.6%」看不见价格
        price = d.get("low52_price")
        return f"52周低 {price}" if price else ""
    if kind in ("add_price", "reduce_price"):
        return d.get("level_price") or ""
    return ""


def _member_signal_details(d, kind, member, level):
    indicators = _member_direction_markers(d, kind)
    if kind == "rsi":
        # 同只股票可能另有红灯；计数格只展示与本格告警等级相符的 RSI 条数。
        indicators = [marker for marker in indicators if marker["tone"] == level]
    if indicators:
        member["indicators"] = indicators
    if kind == "ma":
        settings = d.get("ma_settings") if isinstance(d.get("ma_settings"), dict) else S
        member["ma_settings"] = {key: settings.get(key) for key in ("ma_short", "ma_long")}
        marks = {k for k, _ in d.get("signal_marks") or []}
        facts = []
        for text in d.get("signals") or []:
            if not isinstance(text, str):
                continue
            fact_kind = ("ma_cross" if re.match(r"^(上穿|跌破)\d+日(?:弱)?均线", text)
                         else "ma_band" if re.match(r"^(日内触及|现价贴近)\d+日(?:弱)?均线", text) else "")
            if fact_kind and (fact_kind in marks or "ma" in marks):
                fact = {"kind": fact_kind, "text": text}
                if fact not in facts:
                    facts.append(fact)
        if facts:
            member["ma_facts"] = facts
    metric = _member_metric(d, kind)
    if metric:
        member["metric"] = metric
    if kind == "low52":
        # 52周低位那一栏把低点价本身写出来：只说「距低点 +2.6%」看不见价格
        low = d.get("low52")
        if (isinstance(low, (int, float)) and not isinstance(low, bool)
                and math.isfinite(low) and low > 0):
            member["low52_price"] = f"{low:,.2f}"
    if kind == "rsi" and indicators:
        member["rsi_hits"] = " / ".join(marker["label"][3:] for marker in indicators)
    if kind == "boll" and indicators:
        member["boll_state"] = " / ".join(marker["label"] for marker in indicators)
    if kind in ("chg", "amp", "vol_ratio") and d.get("chg") == 0 and not isinstance(d.get("chg"), bool):
        member["flat"] = True


def _merge_ma_members(candidates):
    """均线按真实身份取最高信号等级，所有已触发事实保留在同一成员中。"""
    levels = ("red", "yellow", "purple")
    priority = {g["item_group"]: i for i, g in enumerate(GROUPS)}
    unique = {}
    for level, source in sorted(candidates, key=lambda pair: priority.get(pair[1].get("group"), len(GROUPS))):
        identity = instrument_identity(source["symbol"])
        if identity in RISK_IDENTITIES:
            continue
        entry = {**source, "symbol": normalize_symbol(source["symbol"])}
        previous = unique.get(identity)
        if previous:
            old_level, old = previous
            chosen_level = min((old_level, level), key=levels.index)
            entry = dict(old if chosen_level == old_level else entry)
            for field in ("indicators", "ma_facts"):
                values = list(old.get(field) or [])
                values += [value for value in source.get(field) or [] if value not in values]
                if values:
                    entry[field] = values
            unique[identity] = (chosen_level, entry)
        else:
            unique[identity] = (level, entry)
    return {level: [entry for chosen, entry in unique.values() if chosen == level] for level in levels}


def signal_count_members(items):
    """原规则口径不变；均线合并后按真实身份最高等级统计，事实不互相覆盖。"""
    levels = ("red", "yellow", "purple")
    out = {kind: {level: [] for level in levels} for kind, _ in SIGNAL_KINDS}
    priority = {g["item_group"]: i for i, g in enumerate(GROUPS)}
    seen = set(RISK_IDENTITIES)
    ma_candidates = []
    for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(GROUPS))):
        if (d.get("reference_only") or d.get("level") == "gray"
                or "price" in d and d["price"] is None):
            continue
        ma_levels = {level for kind, level in d.get("signal_marks") or []
                     if kind in ("ma", "ma_cross", "ma_band") and level in levels}
        if ma_levels:
            level = min(ma_levels, key=levels.index)
            entry = {"symbol": normalize_symbol(d.get("symbol")), "note": d.get("note") or "",
                     "group": d.get("group") or "", "hold": str(d.get("hold") or ""),
                     "level": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else ""}
            _member_signal_details(d, "ma", entry, level)
            ma_candidates.append((level, entry))
    out["ma"] = _merge_ma_members(ma_candidates)
    for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(GROUPS))):
        symbol = normalize_symbol(d.get("symbol"))
        identity = instrument_identity(symbol)
        if identity in seen:
            continue
        seen.add(identity)
        if (d.get("reference_only") or d.get("level") == "gray"
                or "price" in d and d["price"] is None):
            continue
        member = {"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                  # hold 只给持仓组用：统计面板要在名单里标「长持仓 / 短持仓」
                  "hold": str(d.get("hold") or ""),
                  "level": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else ""}
        marks = d.get("signal_marks") or []
        if any(kind == "boll_rsi" for kind, _ in marks) or _text_only_kind(d.get("signals")) == "boll_rsi":
            marks = [(kind, level) for kind, level in marks if kind not in ("boll", "rsi")]
        for kind in {k for k, _ in marks}:
            if kind not in out or kind in ("ma", "target"):
                continue
            for level in levels:
                if any(k == kind and m == level for k, m in marks):
                    entry = dict(member)
                    # 加减仓价：名单里把价位本身写出来，光看代码不知道挂在哪一档。
                    # 用「加 325」这套跟个股行胶囊一致的缩写，不再写「加仓价」长名。
                    if kind in ("add_price", "reduce_price"):
                        price = _level_num(d.get("trigger" if kind == "add_price" else "reduce"))
                        if price:
                            entry["level_price"] = ("加" if kind == "add_price" else "减") + f" {price}"
                    # 组合信号现在红黄两档都有，方向说明不能只给红
                    if kind == "boll_rsi" and level in ("red", "yellow") and d.get("boll_rsi_direction"):
                        entry["direction"] = d["boll_rsi_direction"]
                        if type(d.get("boll_rsi_hits")) is int and d["boll_rsi_hits"] in (2, 3):
                            entry["boll_rsi_hits"] = d["boll_rsi_hits"]
                    if kind != "target" or level == "purple":
                        _member_signal_details(d, kind, entry, level)
                    out[kind][level].append(entry)
    out["target"]["purple"] = _target_overlay_union(target_overlay_members(items))
    return out


def earnings_week_members(items, today):
    """独立信息名单，复用首页分类优先级去重，不进入警示信号或灯级。"""
    priority = {group["item_group"]: index for index, group in enumerate(GROUPS)}
    seen = set(RISK_IDENTITIES)
    members = []
    for d in sorted(items, key=lambda item: priority.get(item.get("group"), len(GROUPS))):
        symbol = normalize_symbol(d.get("symbol"))
        identity = instrument_identity(symbol)
        if identity in seen:
            continue
        seen.add(identity)
        if (d.get("reference_only") or d.get("etf") or is_etf(symbol)
                or not earnings_within_list(d.get("earnings"), today)):
            continue
        e = d["earnings"]
        members.append({"symbol": symbol, "note": d.get("note") or "", "group": d.get("group") or "",
                        "level": d.get("level") or "", "hold": str(d.get("hold") or ""), "date": e["date"],
                        "timing": e.get("timing") or "", "kind": e.get("kind") or ""})
    return sorted(members, key=lambda m: (m["date"], priority.get(m["group"], len(GROUPS)), m["symbol"]))


_RETIRED_SNAPSHOT_KEYS = frozenset({
    "fund", "fundamental", "fund_cache_note", "fund_hits", "fund_period",
    "fund_enable", "fund_rev_red", "fund_rev_yellow", "fund_om_drop", "fund_gm_drop",
    "fund_de_rise", "fund_cr_drop", "fund_roe_drop", "fund_pm_abnormal", "fund_report_grace_days",
    "target_anomaly_pct",
})


def _sanitize_snapshot_fields(value):
    """浅复制并过滤已知退役键；其它字段保持原值。"""
    out = {key: item for key, item in value.items() if key not in _RETIRED_SNAPSHOT_KEYS}
    if "signal_marks" in out:
        out["signal_marks"] = [mark for mark in out["signal_marks"] or [] if mark[0] != "fund"]
    if "symbol" in out and any(key in out for key in ("target", "purple", "signal_marks", "signals")):
        out = _apply_target(out)
    return out


def _merge_ma_snapshot(snapshot):
    """兼容旧双均线快照；名单可核验才计算并集，缺名单时不猜重叠数。"""
    counts = snapshot.get("signal_counts") or {}
    members = snapshot.get("signal_members") or {}
    keys = [kind for kind in ("ma", "ma_cross", "ma_band") if kind in counts or kind in members]
    if not keys or keys == ["ma"] and "ma" not in members:
        return snapshot
    candidates, valid = [], True
    for kind in keys:
        source_counts = counts.get(kind) or {}
        source_members = members.get(kind) or {}
        if not isinstance(source_counts, dict) or not isinstance(source_members, dict):
            valid = False
            continue
        for level in ("red", "yellow", "purple"):
            expected = source_counts.get(level, 0)
            entries = source_members.get(level, [])
            if (not isinstance(entries, list) or type(expected) is not int or expected < 0
                    or kind in counts and len(entries) != expected
                    or any(not isinstance(d, dict) or not isinstance(d.get("symbol"), str)
                           or not d["symbol"].strip() for d in entries)):
                valid = False
                continue
            candidates.extend((level, _snapshot_ma_member(d, kind, snapshot.get("effective_settings") or {})) for d in entries)
    merged = _merge_ma_members(candidates) if valid else {level: [] for level in ("red", "yellow", "purple")}
    ma_counts = {"label": "均线", **{level: len(merged[level]) if valid else None for level in merged}}
    if not valid:
        ma_counts["pending"] = True
    out = dict(snapshot)
    out["signal_members"] = {kind: value for kind, value in members.items() if kind not in keys}
    # 放回原均线的位置，不改变其它信号的展示顺序。
    out["signal_counts"] = {kind: (ma_counts if kind == "ma" else counts[kind])
                            for kind in dict.fromkeys("ma" if kind in keys else kind for kind in counts)}
    out["signal_counts"].setdefault("ma", ma_counts)
    out["signal_members"]["ma"] = merged
    return out


def _snapshot_ma_member(entry, kind, settings):
    """旧均线成员补展示归属；数字原事实保留，不用当前设置解释历史周期。"""
    if not isinstance(entry, dict):
        return entry
    out = _sanitize_snapshot_fields(entry)
    if kind not in ("ma", "ma_cross", "ma_band"):
        return out
    config = out.get("ma_settings") if isinstance(out.get("ma_settings"), dict) else settings
    out["ma_settings"] = {key: config.get(key) for key in ("ma_short", "ma_long")}
    if kind == "ma_band":
        markers = []
        for marker in out.get("indicators") or []:
            if not isinstance(marker, dict):
                markers.append(marker)
                continue
            label = str(marker.get("label") or "")
            if label.startswith(("现价在均线", "现价与均线")) and "日内触及" in label:
                period = config.get("ma_long")
                name = f"{period}日均线" if type(period) is int and period > 0 else "均线（归属待确认）"
                label = f"日内触及{name}，" + label.replace("，日内触及", "")
            markers.append({**marker, "label": label})
        if markers:
            out["indicators"] = markers
    return out


def _sanitize_snapshot(snapshot):
    """旧快照只在渲染副本中清理，原对象与磁盘保存快照不变。"""
    out = _sanitize_snapshot_fields(snapshot)
    old_target_counts = (snapshot.get("signal_counts") or {}).get("target") or {}
    old_target_light = bool(old_target_counts.get("red") or old_target_counts.get("yellow"))
    for field in ("summary", "list_counts", "signal_counts", "effective_settings"):
        if isinstance(out.get(field), dict):
            out[field] = _sanitize_snapshot_fields(out[field])
    if isinstance(out.get("signal_members"), dict):
        out["signal_members"] = {
            kind: {level: [_snapshot_ma_member(entry, kind, out.get("effective_settings") or {})
                           for entry in entries] if isinstance(entries, list) else entries
                   for level, entries in members.items()} if isinstance(members, dict) else members
            for kind, members in out["signal_members"].items() if kind != "fund"
        }
    for field in ("items", "stocks", "text_only"):
        if isinstance(out.get(field), list):
            out[field] = [_sanitize_snapshot_fields(item) if isinstance(item, dict) else item
                          for item in out[field]]
            if field in ("items", "stocks"):
                out[field] = [{**item, "ma_settings": item.get("ma_settings")
                               if isinstance(item.get("ma_settings"), dict) else
                               {key: (out.get("effective_settings") or {}).get(key) for key in ("ma_short", "ma_long")}}
                              if isinstance(item, dict) else item for item in out[field]]
    rows = out.get("items") if isinstance(out.get("items"), list) else out.get("stocks")
    if isinstance(rows, list):
        out["text_only"] = text_only_members(rows)
        out["target_overlays"] = target_overlay_members(rows)
        if isinstance(out.get("summary"), dict):
            out["summary"].update({level: sum(d.get("level") == level for d in rows)
                                   for level in ("red", "yellow", "green", "gray")})
    overlays = out.get("target_overlays")
    union = _target_overlay_union(overlays) if isinstance(overlays, dict) else []
    if isinstance(out.get("signal_counts"), dict):
        out["signal_counts"]["target"] = {"label": dict(SIGNAL_KINDS)["target"],
                                            "red": 0, "yellow": 0, "purple": len(union)}
    if isinstance(out.get("signal_members"), dict):
        out["signal_members"]["target"] = {"red": [], "yellow": [], "purple": union}
    if isinstance(out.get("summary"), dict):
        out["summary"]["purple"] = len(union)
        if old_target_light and not isinstance(rows, list):
            for level in ("red", "yellow"):
                entries = out.get("signal_members") or {}
                counts = out.get("signal_counts") or {}
                if isinstance(entries, dict) and counts and all(
                        not value.get(level) or isinstance(entries.get(kind), dict)
                        and isinstance(entries[kind].get(level), list)
                        and len(entries[kind][level]) == value[level]
                        for kind, value in counts.items() if kind != "target"):
                    out["summary"][level] = len({instrument_identity(d["symbol"])
                        for kind, levels in entries.items() if kind != "target" and isinstance(levels, dict)
                        for d in levels.get(level) or [] if isinstance(d, dict) and d.get("symbol")})
                else:
                    out["summary"][level] = None
    if isinstance(rows, list) and isinstance(out.get("signal_counts"), dict):
        ma_members = signal_count_members(rows)["ma"]
        out["signal_counts"] = {kind: value for kind, value in out["signal_counts"].items()
                                if kind not in ("ma_cross", "ma_band")}
        out["signal_counts"]["ma"] = {"label": "均线", **{level: len(entries) for level, entries in ma_members.items()}}
        out["signal_members"] = {**(out.get("signal_members") or {}), "ma": ma_members}
        for kind in ("ma_cross", "ma_band"):
            out["signal_members"].pop(kind, None)
    return _merge_ma_snapshot(out)


def build_snapshot(macro, items, cfg, group_counts=None):
    ma_members = signal_count_members(items)["ma"]
    priority = {g["item_group"]: i for i, g in enumerate(GROUPS)}
    unique = {}
    for item in sorted(items, key=lambda d: priority.get(d.get("group"), len(GROUPS))):
        identity = instrument_identity(item["symbol"])
        if identity not in RISK_IDENTITIES:
            unique.setdefault(identity, item)
    items = [_sanitize_snapshot_fields(item) for item in unique.values()]
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
    earnings_asof = _bj_today()
    snapshot = {
        "dca_reminder": dca,
        "run_id": str(os.environ.get("GITHUB_RUN_ID") or "local-" + NOW.strftime("%Y%m%d%H%M%S")),
        "request_id": os.environ.get("MM_REQUEST_ID", ""),
        **build_identity(),
        "started_at": os.environ.get("MM_STARTED_AT", NOW.isoformat()),
        "started_at_bj": beijing_iso(os.environ.get("MM_STARTED_AT", NOW.isoformat())),
        "finished_at": datetime.now(TZ).isoformat(),
        "finished_at_bj": datetime.now(TZ).isoformat(timespec="seconds"),
        "event": EVENT,
        "target_trade_date": TARGET_DATE,
        "mode": "closed" if CLOSED_ONLY else "manual_or_config",
        "schedule": "美东周一至周五20:15；北京时间夏季次日08:15、冬季次日09:15",
        "config_files": {f: config_hash(f) for f in ("holdings.json", "settings.json")},
        "effective_settings": _sanitize_snapshot_fields(S),
        "groups": GROUPS,
        "group_monitoring": monitoring,
        "registered_counts": {g["key"]: len(cfg.get(g["key"]) or {}) for g in GROUPS},
        "list_counts": {**counts,
                        "triggers": sum(bool(c.get("trigger")) for c in positions.values()),
                        "reduces": sum(bool(reduce_rule(c)) for c in positions.values()),
                        "dca": 1 if dca else 0},
        "summary": {**{lv: sum(d["level"] == lv and (lv not in ("red", "yellow")
                              or not d.get("reference_only") and ("price" not in d or d["price"] is not None))
                              for d in items) for lv in ("red", "yellow", "green", "gray")},
                    "purple": purple_counts(items),
                    "total": len(items), "macro_ok": sum(bool(m.get("ok")) for m in macro.values()),
                    "stale_symbols": stale, "missing_symbols": missing,
                    "unsupported_symbols": unsupported, "reference_dates": references,
                    "today_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") == TARGET_DATE for d in items),
                    "prior_prices": sum(not d.get("reference_only") and d.get("price") is not None and d.get("data_date") != TARGET_DATE for d in items),
                    "missing_prices": sum(d.get("price") is None for d in items)},
        "signal_counts": signal_counts(items),
        "signal_members": signal_count_members(items),
        "target_overlays": target_overlay_members(items),
        "text_only": text_only_members(items),
        "earnings_week": {"asof": earnings_asof, "members": earnings_week_members(items, earnings_asof)},
        "actual_dates": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
        "macro_dates": {k: m.get("date", "") for k, m in macro.items()},
        "treasury_reference": {key: macro.get("ust10", {}).get(key) for key in
                               ("ok", "value", "date", "source", "unit", "lagging", "reason", "quote_name", "observed_date", "observed_value", "delta_month")},
        "coverage": "数据源日线；完整23小时夜盘/日盘覆盖尚未验证。自动日报不并入实时价；手动常规盘中按当前报价重算。",
    }
    snapshot["signal_members"]["ma"] = ma_members
    snapshot["signal_counts"]["ma"] = {"label": "均线", **{level: len(entries) for level, entries in ma_members.items()}}
    snapshot["data_time_text"] = market_data_time(snapshot)
    snapshot["market_risk"] = market_risk_summary(macro)
    snapshot["risk_quotes"] = {key: {field: (macro.get(key) or {}).get(field) for field in
                             ('ok', 'value', 'date', 'source', 'drawdown')} for key in ('vix', 'sp500')}
    return snapshot


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
            val = (f'{it["value"]:.1f}%<span class="unit">的成分股站上宏观长均线（长期趋势）</span>'
                   f'<span class="unit">站上宏观短均线（短期）：{it["pct50"]:.1f}%</span>')
        elif key == "nfci":
            val = f'{it["value"]:+.2f}'
        else:
            val = f'{it["value"]:.2f}'

        dw = it.get("delta_week")
        dw_txt = f'{dw:+.0f}' if (key == "hy_oas" and dw is not None) else "—"
        label = html_lib.escape(str(it.get("name") or RISK_LABELS[key]))
        label += f'<span class="unit">截至 {html_lib.escape(it["date"])}</span>'
        # 数据来源（FRED / Cboe / Yahoo …）按 2026-10-15 要求不再在指标行里展示，页底仍保留整体来源说明
        rows_macro.append(
            f'<tr class="{lv}"><td>{label}</td><td class="num">{val}</td>'
            f'<td class="num dim">{dw_txt}<span class="unit">周变化</span></td><td>{txt}</td></tr>'
        )

    # 参考行：10Y 美债一个月变化。只看利率冲击，不进 RISK_INDICATORS，不影响综合灯和有效指标数
    ut = macro.get("ust10") or {}
    if ut.get("ok") and ut.get("delta_month") is not None:
        ulv, utxt = macro_status("ust10", ut)
        uw = ut.get("delta_week")
        uw_txt = f'{uw * 100:+.0f}' if uw is not None else "—"
        ulabel = (f'{html_lib.escape(str(ut.get("name") or "10Y美债"))}'
                  f'<span class="unit">参考</span>'
                  f'<span class="unit">截至 {html_lib.escape(ut["date"])}</span>')
        rows_macro.append(
            f'<tr class="{ulv}" id="ust10Ref"><td>{ulabel}</td><td class="num">{ut["value"]:.2f}%</td>'
            f'<td class="num dim">{uw_txt}<span class="unit">周变化 bp</span></td><td>{utxt}</td></tr>')
    else:
        rows_macro.append('<tr class="gray" id="ust10Ref"><td>10Y美债<span class="unit">参考</span></td>'
                          '<td colspan="3">无数据或已过期</td></tr>')
    return "".join(rows_macro)


_LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2, "gray": 3}


def _sort_key(d):
    return (_LEVEL_ORDER[d["level"]], -(abs(d["chg"]) if d["chg"] else 0))


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


def _earnings_html(d):
    """财报列只写美东日期/时段与北京时间，不混入来源说明。"""
    earn = earnings_label(d.get("earnings"))
    if not earn:
        return ""
    urgency = earnings_urgency(d.get("earnings"))
    soon = {"red": " urgent", "yellow": " soon"}.get(urgency, "")
    main = html_lib.escape(earn[0])
    sub = html_lib.escape(earn[1])
    unit = f'<span class="unit">{sub}</span>' if sub else ""
    return f'<span class="earn{soon}">{main}{unit}</span>'


def _signal_text_html(text):
    """只为提示文字着色，不改变信号等级、计数或公式；组合信号优先。"""
    text = str(text)
    if "共识价" in text or "目标价" in text:
        cls = "tp"
    elif "布林" in text and "RSI" in text:
        # 组合信号现在红黄两档都有，文案里带「→ 红 / → 黄」，按文案分色，别一律染红
        cls = "signal-red" if "→ 红" in text else "signal-yellow"
    elif text.startswith("RSI") and "超卖" in text:
        cls = "signal-magenta"
    elif any(text.startswith(prefix) for prefix in ("逼近布林下轨", "触及布林下轨", "跌破布林下轨")):
        cls = "signal-yellow"
    elif "均线" in text and ("触及" in text or "贴近" in text or "上穿" in text or "跌破" in text):
        period = re.search(r"(\d+)日(?:弱)?均线", text)
        cls = "signal-cyan" if period and int(period.group(1)) == S.get("ma_long") and S.get("ma_long", 0) > 0 else ""
    else:
        cls = ""
    escaped = html_lib.escape(text)
    return f'<span class="{cls}">{escaped}</span>' if cls else escaped


# 描述分类只用于排版，灯色只认对应指标 marks，不从整行灯色或文案猜灯。
_SIGNAL_DESCRIPTION_RULES = (
    ("rsi", re.compile(r"^RSI\b")),
    ("boll", re.compile(r"^(?:盘中突破|突破|跌破|触及|逼近)布林[上下]轨")),
    ("boll_rsi", re.compile(r"^布林\s*\+\s*RSI")),
    ("chg", re.compile(r"^(?:异动|波动)\s*[+-]?\d")),
    ("amp", re.compile(r"^(?:日内)?振幅")),
    ("vol_ratio", re.compile(r"^(?:盘中|最近完整日)?量比")),
    ("ma", re.compile(r"^(?:日内触及|现价贴近|贴近|触及|上穿|跌破)\d+日(?:弱)?均线")),
    ("low52", re.compile(r"^(?:触及52周新低|仍在52周低位|距52周低点)")),
    ("add_price", re.compile(r"^(?:已跌破|距)加仓价")),
    ("reduce_price", re.compile(r"^(?:已涨到|距)减仓价")),
    ("target", re.compile(r"^(?:距目标价|目标价|手填目标价|今日上穿目标价|今日跌破目标价|在目标价)")),
)
_SIGNAL_DESCRIPTION_LABELS = dict(SIGNAL_KINDS, target="目标价")


def _description_signals(signals):
    """仅恢复旧描述中被中点拆开的布林尾句；不新增信号、判灯或突变输入。"""
    result = []
    for value in signals or []:
        text = str(value)
        tail = re.fullmatch(r"连续第≥?\d+日贴近([上下]轨)(?:（其中\d+日穿越）)?", text)
        if (tail and result and result[-1].startswith(_TEXT_BOLL_PREFIX)
                and "布林" + tail[1] in result[-1]):
            result[-1] += "，" + text
        else:
            result.append(text)
    return result


def _signal_description_groups(d):
    """聚合同一指标的事实，布林+RSI互斥显示，绝不修改原 signals/marks。"""
    tg = d.get("target") or {}
    signals = [x for x in _description_signals(d.get("signals")) if not _consensus_description(x, tg)]
    levels = {}
    rank = {"gray": 0, "purple": 1, "yellow": 2, "red": 3}
    for kind, level in d.get("signal_marks") or []:
        kind = "ma" if kind in ("ma_cross", "ma_band") else kind
        if level in rank and rank[level] > rank.get(levels.get(kind), -1):
            levels[kind] = level
    gray_kind = next((m["kind"] for m in text_only_members([d])), "")
    combo = "boll_rsi" in levels or _text_only_kind(signals) == "boll_rsi"
    if gray_kind:
        levels.setdefault(gray_kind, "gray")
    groups = {}
    for index, text in enumerate(signals):
        kind = next((kind for kind, pattern in _SIGNAL_DESCRIPTION_RULES if pattern.match(text)), "")
        if combo and kind in ("rsi", "boll"):
            kind = "boll_rsi"
        if kind == "boll_rsi" and re.fullmatch(r"布林\s*\+\s*RSI\s*→\s*[红黄]", text):
            # 原总结只重复 marks；前置名称和灯点替代它，事实不删除。
            continue
        text = re.sub(r"\s*·\s*", "，", text)
        if kind == "ma":
            text = (_ma_display_text(text, d.get("ma_settings"))
                    .replace(" / ", "，").replace("短均线/长均线", "短均线与长均线"))
        key = kind or f"context-{index}"
        entry = groups.setdefault(key, {"kind": kind, "level": levels.get(kind), "texts": []})
        if text not in entry["texts"]:
            entry["texts"].append(text)
    order = {"red": 0, "yellow": 1, "gray": 2}
    return sorted(groups.values(), key=lambda g: 4 if g["kind"] == "target" or g["level"] == "purple"
                  else order.get(g["level"], 3))


def _signals_html(d):
    """每项真实触发独立灯色；不同指标用 /，组合事实用逗号；共识仍仅隐藏描述。"""
    parts = []
    for group in _signal_description_groups(d):
        kind, level, texts = group["kind"], group["level"], group["texts"]
        label = _SIGNAL_DESCRIPTION_LABELS.get(kind, "")
        # RSI/量比等名称已在原事实开头，不重复添加；组合名称始终前置。
        prefix = label + " " if label and not texts[0].startswith(label) and kind not in ("vol_ratio", "amp", "low52", "add_price", "reduce_price", "target") else ""
        body = html_lib.escape(prefix + "，".join(texts))
        if level:
            label_text = {"red": "红灯", "yellow": "黄灯", "purple": "紫灯", "gray": "灰色文字提示"}[level]
            body = (f'<span class="signal-dot {level}" role="img" '
                    f'aria-label="{label_text}：{html_lib.escape(label)}"></span>' + body)
        parts.append(f'<span class="signal-item" data-signal-kind="{kind}"'
                     + (f' data-signal-level="{level}"' if level else "") + f'>{body}</span>')
    tg = d.get("target") or {}
    if tg.get("kind") == "manual" and target_display_state(tg) == "low":
        parts.append(f'<span class="tnote">目标价 {_price_text(tg["price"])}，空间 {tg["space"]:+.0f}%</span>')
    return ' <span class="signal-separator">/</span> '.join(parts) if parts else "—"


def _range52_row(d):
    """每只标的下面显示当前价在已核验的日线高低区间的位置；旧快照绝不推算极值。"""
    if d.get("reference_only"):
        return ""
    value = d.get("range52")
    price = d.get("price")
    valid = lambda n: isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) and n > 0
    if not isinstance(value, dict):
        return '<tr class="range52-row"><td colspan="4"><span class="range52-missing">52周区间：数据待更新</span></td></tr>'
    low, high = value.get("low"), value.get("high")
    if (not all(valid(n) for n in (price, low, high)) or not low < high or not low <= price <= high
            or value.get("scope") not in ("52周", "可用历史")
            or not all(isinstance(value.get(key), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[key])
                       for key in ("from", "to"))
            or value["from"] > value["to"] or value["to"] != d.get("data_date")
            or value.get("position") is None or type(value["position"]) not in (int, float)
            or not math.isfinite(value["position"])):
        return '<tr class="range52-row"><td colspan="4"><span class="range52-missing">52周区间：数据待更新</span></td></tr>'
    pos = (price - low) / (high - low) * 100
    scope = value["scope"]
    caption = (f'{scope}低 {fmt(low)}，本轮价 {fmt(price)} 位于区间 {pos:.1f}%，'
               f'{scope}高 {fmt(high)}；{value["from"]} 至 {value["to"]} 日线高低价')
    # 成本价：紫方块是本轮价，黄竖线是成本价，形状区分开。
    # 成本价落在 52 周区间外时不再按 0~100% 夹取了事：竖线贴到对应端点，
    # 再加一个朝外的小箭头（class out-lo / out-hi），读屏标签里写清实际数值与方向
    cost_html = ""
    if valid(d.get("cost")):
        cost = d["cost"]
        cpos = (cost - low) / (high - low) * 100
        if cpos < 0:
            cost_html = '<span class="range52-cost out-lo"></span>'
            caption += f'；成本价 {fmt(cost)} 低于{scope}低点 {fmt(low)}'
        elif cpos > 100:
            cost_html = '<span class="range52-cost out-hi"></span>'
            caption += f'；成本价 {fmt(cost)} 高于{scope}高点 {fmt(high)}'
        else:
            cost_html = f'<span class="range52-cost" style="left:{cpos:.1f}%"></span>'
            caption += f'；成本价 {fmt(cost)} 位于区间 {cpos:.1f}%'
    # 持仓两端第二行＝从本轮价到低／高点的变化幅度，以本轮价为基数；不是区间位置。
    # 仅展示，不参与报灯、评分或折叠判定；非持仓保留原单行端点。
    low_gap_html = high_gap_html = ""
    rail_class = "range52-rail"
    if d.get("group") == "position":
        low_gap = (low / price - 1) * 100
        high_gap = (high / price - 1) * 100
        gap_text = lambda n: "0.0%" if abs(n) < 0.05 else f"{n:+.1f}%"
        low_text, high_text = gap_text(low_gap), gap_text(high_gap)
        low_gap_html = f'<span class="range52-gap">{low_text}</span>'
        high_gap_html = f'<span class="range52-gap">{high_text}</span>'
        rail_class += " has-endpoint-gaps"
        caption += f'；从本轮价到低点 {low_text}、到高点 {high_text}（均以本轮价为基数）'
    # 成本价是横跨灰轨的黄色竖线，区间位置百分比仍在现价三角之上。
    return (f'<tr class="range52-row"><td colspan="4">'
            f'<div class="{rail_class}" role="img" aria-label="{caption}" style="--range-pos:{pos:.1f}%">'
            f'<span class="range52-lo" aria-hidden="true">{fmt(low)}{low_gap_html}</span>'
            f'<div class="range52-track">'
            f'<span class="range52-percent" aria-hidden="true">{pos:.1f}%</span>'
            f'<span class="range52-marker" style="left:{pos:.1f}%"></span>'
            f'{cost_html}</div>'
            f'<span class="range52-hi" aria-hidden="true">{fmt(high)}{high_gap_html}</span>'
            f'</div></td></tr>')


def _stock_anchor_id(symbol):
    """个股行的锚点 id；同身份的标的只会在优先分组出现一次，不会撞 id。"""
    ident = instrument_identity(normalize_symbol(symbol))
    return "s-" + re.sub(r"[^0-9A-Za-z]+", "-", str(ident)).strip("-").lower()


def _quiet_keep(d, earnings_week):
    """独立于红黄灯的主表保留原因；已读降绿后仍适用。"""
    return bool(d.get("reference_only") or not d.get("foldable", True)
                or _target_active(d) or earnings_soon(d.get("earnings"), earnings_week))


def _stock_row(d, snapshot, earnings_week=None):
    if d.get("target") or d.get("purple") or any(kind == "target" for kind, _ in d.get("signal_marks") or []):
        d = _apply_target(d)
    earnings_week = earnings_week or (snapshot.get("earnings_week") or {}).get("asof") or _bj_today()
    quiet_keep = "true" if _quiet_keep(d, earnings_week) else "false"
    state = target_display_state(d.get("target"))
    stripe = {"reached": "pur-hit", "space": "pur-space", "high": "pur-high"}.get(state)
    cls = d["level"] + (" pur " + stripe if _target_active(d) and stripe else "")
    chg_cls = "up" if (d["chg"] or 0) > 0 else (
        "down" if (d["chg"] or 0) < 0 else "")
    chg_txt = f'{d["chg"]:+.2f}%' if d["chg"] is not None else "—"
    note = f'<span class="note">{html_lib.escape(str(d["note"]))}</span>' if d["note"] else ""
    lv_html = level_note(d.get("trigger"), d.get("reduce"))
    tg_html = target_tag(d.get("target") or {})
    # 持仓内部分长短：长持仓屏蔽上轨普通过热，页面上标出来，不然看不出区别
    hold_html = ('<span class="unit" title="长持仓：不看一般过热，RSI 三条全过界才提醒">长</span>'
                 if d.get("group") == "position" and str(d.get("hold") or "") == "long" else "")
    return (
        f'<tr class="{cls}" id="{_stock_anchor_id(d["symbol"])}" data-quiet-keep="{quiet_keep}" '
        f'data-price="{html_lib.escape(str(d.get("price") or ""), quote=True)}" '
        f'data-prev-close="{html_lib.escape(str(d.get("prev_close") or ""), quote=True)}"><td class="sym">'
        f'{normalize_symbol(d["symbol"])}{note}{hold_html}'
        f'{lv_html}{_row_tags(d, snapshot)}</td>'
        f'<td class="earn-cell">{_earnings_html(d)}</td>'
        f'<td class="num px"><span class="px-price">{fmt(d["price"])}{html_lib.escape(str(d.get("unit") or ""))}</span>'
        f'<span class="px-chg {chg_cls}">{chg_txt}</span>{tg_html}</td>'
        f'<td class="sig">{_signals_html(d)}</td></tr>'
        + _range52_row(d)
    )


def _stock_rows(lst, snapshot, earnings_week=None):
    out = "".join(_stock_row(d, snapshot, earnings_week) for d in lst)
    return out or '<tr class="gray"><td colspan="4">今晚无异动，不用盯</td></tr>'


def _count_badge(level, count, label):
    """灯级计数徽章：数字直接放在灯色圆里（比「圆点 + 数字」省版面，手机上一眼看到数）。"""
    return (f'<span class="stat-badge {level}" aria-label="{html_lib.escape(label)} {count} 项">'
            f'{count}</span>')


def _group_stats_html(g, group, items_by_symbol, enabled):
    """分组标题仅计红、黄、缺失、紫；技术灰只在统计面板计数。"""
    stats = ''.join(_count_badge(lv, count, label)
                    for lv, label in (("red", "红色警示"), ("yellow", "黄色警示"))
                    if (count := sum(d["level"] == lv for d in g)))
    # 技术灰仍进入统计面板及描述，不在个股/持仓分组标题另加数字。
    # 无数据（缺失）单独用虚线圈，避免和灰灯撞名
    if (n_miss := sum(d["level"] == "gray" for d in g)):
        stats += _count_badge("missing", n_miss, "缺失")
    # 紫色（目标价 / 共识价警示）：条数跟在红黄灯后面；一条都没有就不显示
    if (n_pur := purple_counts(g)):
        stats += _count_badge("purple", n_pur, "紫色警示")
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
                  f'{rs["days"]}日对{".SPX" if bn == RS_BENCH_NAME else html_lib.escape(bn)} {diff:+.1f}%</span>')
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


def _display_groups():
    """首页指数基置底；不改变 GROUPS 的清单去重、抓取与监测优先级。"""
    return [g for g in GROUPS if g["key"] != "index_funds"] + [g for g in GROUPS if g["key"] == "index_funds"]


def _collapse_button(card_id, label):
    """只显示小三角，保留触控区域及读屏名称。"""
    card_id = html_lib.escape(str(card_id), quote=True)
    label = html_lib.escape(str(label), quote=True)
    return (f'<div class="group-bottom"><button type="button" class="collapse-group" '
            f'aria-controls="{card_id}" aria-label="收起{label}">'
            f'<span class="collapse-icon" aria-hidden="true">▲</span></button></div>')


POSITION_ZONES = (("long", "长持仓"), ("short", "短持仓"))


def _position_zone(d):
    """持仓内部按 hold 分两区；没标的一律算短持仓，规则不缩水。"""
    return "long" if str(d.get("hold") or "short").lower() == "long" else "short"


def _group_body(g, group, snapshot, earnings_week, enabled, collapse_id, collapse_label, empty_note=""):
    """分组（或持仓内部某个区）的展开内容：空提示 + 警示表 + 「无异动 N 只」折叠表 + 底部收起条。"""
    earnings_week = earnings_week or (snapshot.get("earnings_week") or {}).get("asof") or _bj_today()
    g = [dict(d, group=group["item_group"]) if d.get("group") != group["item_group"] else d for d in g]
    g = [_apply_target(d) if d.get("target") or d.get("purple")
         or any(kind == "target" for kind, _ in d.get("signal_marks") or []) else d for d in g]
    shown = [d for d in g if d["level"] in ("red", "yellow", "gray") or _quiet_keep(d, earnings_week)]
    quiet = [d for d in g if d["level"] == "green" and not _quiet_keep(d, earnings_week)]
    parts = []
    if empty_note:
        parts.append(f'<div class="quiet">{empty_note}</div>')
    if shown:
        parts.append(f'<table class="stk">{STK_COLS}{STK_HEAD}{_stock_rows(shown, snapshot, earnings_week)}</table>')
    if quiet:
        parts.append(f'<details class="quiet-list"><summary>无异动 {len(quiet)} 只 · 点击查看</summary>'
                     f'<table class="stk">{STK_COLS}{STK_HEAD}{_stock_rows(quiet, snapshot, earnings_week)}</table></details>')
    parts.append(_collapse_button(collapse_id, collapse_label))
    return "\n".join(parts)


def _position_long_zone_card(g, group, items_by_symbol, snapshot, earnings_week, enabled):
    """持仓里只有「长持仓」单独成一块可折叠区；没有长持仓就不占位。

    短持仓不再单包一层——展开「持仓」直接就是短持仓的表，少一次多余折叠。
    """
    zlabel = dict(POSITION_ZONES)["long"]
    zg = [d for d in g if _position_zone(d) == "long"]
    if not zg:
        return ""
    zid = "group_positions_long"
    zbody = _group_body(zg, group, snapshot, earnings_week, enabled, zid, zlabel)
    zstats = _group_stats_html(zg, group, items_by_symbol, enabled)
    return (f'<details class="zone-card" id="{zid}" open>'
            f'<summary><span class="zone-name">{zlabel} ({len(zg)})</span>'
            f'<span class="group-stats">{zstats}</span></summary>{zbody}</details>')


def _group_card(group, g, items_by_symbol, snapshot):
    """一个分组的折叠卡片：展开的警示表 + 「无异动 N 只」折叠表。持仓内部再分长/短两区。"""
    monitoring = snapshot.get("group_monitoring", {})
    registered = snapshot.get("registered_counts", {})
    g = _sort_group_items(g, group)
    earnings_week = (snapshot.get("earnings_week") or {}).get("asof") or _bj_today()
    parts = []
    enabled = monitoring.get(group["key"], True)
    if not enabled:
        note = f"行业监测关闭 · {group['sector_etf']} 仍监测（仅限原组已有标的）" if group.get("sector_etf") else "分组监测已关闭"
        parts.append(f'<div class="quiet">{note} · 清单保留，普通标的暂停抓取和报警</div>')
    empty_note = ''
    if not g and enabled:
        empty_note = ('本组标的已在优先分组展示，避免重复信号' if registered.get(group["key"], 0)
                      else '暂无标的，去设置页录入或导入 CSV / EBK')
    if group["key"] == "positions":
        # 上方是「长持仓」可折叠区（没有就不出现），下方主体就是短持仓，不再多包一层
        parts.append(_position_long_zone_card(g, group, items_by_symbol, snapshot, earnings_week, enabled))
        short_g = [d for d in g if _position_zone(d) == "short"]
        parts.append(_group_body(short_g, group, snapshot, earnings_week, enabled,
                                 f'group_{group["key"]}', group["label"], empty_note))
    else:
        parts.append(_group_body(g, group, snapshot, earnings_week, enabled,
                                 f'group_{group["key"]}', group["label"], empty_note))
    body = "\n".join(parts)
    stats = _group_stats_html(g, group, items_by_symbol, enabled)
    # 打开网页时只展开「统计」；所有分组（含持仓、重点）默认折叠。持仓点开后长/短两区默认展开。
    return (f'<details class="card group-card" id="group_{group["key"]}">'
            f'<summary class="grp"><span class="grp-name">{group["label"]} ({len(g)})</span>'
            f'<span class="group-stats">{stats}</span></summary>{body}</details>\n')


def _run_light(snapshot, finished_txt):
    """首页状态灯（第三排）：个股行情失败、市场风险部分缺失、空清单或完整成功。"""
    summary = snapshot.get("summary", {})
    registered = snapshot.get("registered_counts", {})
    # 无数据状态改叫「缺失」，避免和统计面板里「只出文字不报灯」的灰灯撞名
    # （具体文案在下面按「有没有异常」拼，不再单独拼一段）
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
    risk = snapshot.get("market_risk") or {}
    missing_risk = risk.get("missing") or []
    kind = {"schedule": "自动", "workflow_dispatch": "手动", "push": "保存后"}.get(snapshot.get("event"), "")
    # 仅列出缺失或不支持的项目，不重复显示完整数据的已更新计数。
    deficits = []
    if summary.get("gray"):
        deficits.append(f"缺失 {summary.get('gray')}")
    if unsupported_count:
        deficits.append(f"{unsupported_count} 个特殊代码暂不支持报价")
    if isinstance(missing_risk, list) and missing_risk:
        names = "、".join(html_lib.escape(str(name)) for name in missing_risk)
        valid = risk.get("valid_count", 5 - len(missing_risk))
        parts = [f"{kind}运行已完成",
                 f"市场风险数据缺失：{names}（有效指标 {valid}/5）"]
        parts.extend(deficits)
        parts.append(f"完成于 {finished_txt}（北京时间）")
        return "partial", " · ".join(parts)
    parts = [f"完成于 {finished_txt}（北京时间）"] + deficits
    return "ok", f"{kind}抓取成功 · " + " · ".join(parts)


def _member_icons(d, kind, level):
    """只渲染已验证的方向标记；旧快照缺方向时显示待更新，绝不按当前报价反推。"""
    esc = lambda value: html_lib.escape(str(value), quote=True)
    # 组合信号现在红黄两档都有，箭头不能只给红档
    if kind == "boll_rsi" and level in ("red", "yellow"):
        direction = d.get("direction") if isinstance(d.get("direction"), str) else ""
        hits = d.get("boll_rsi_hits")
        if direction.startswith(("上轨", "下轨")) and type(hits) is int and hits in (2, 3):
            shape = "▲" if direction.startswith("上轨") else "▼"
            markers = [{"shape": shape, "tone": "red" if level == "red" else "yellow",
                        "label": f"{direction}，RSI{hits}条过界"}]
        else:
            known = ("上轨（超买）", "下轨（超卖）", "上轨（RSI超卖，方向不一致）",
                     "下轨（RSI超买，方向不一致）", "上轨（RSI超买/超卖均命中）",
                     "下轨（RSI超买/超卖均命中）")
            value = direction if direction in known else "方向待下次更新"
            return f' <span class="signal-member-direction">{esc(value)}</span>'
    elif kind in ("rsi", "boll", "ma", "ma_cross", "ma_band", "chg", "amp", "vol_ratio") or (kind == "target" and level == "purple"):
        markers = d.get("indicators")
        if kind in ("chg", "amp", "vol_ratio") and d.get("flat") is True:
            return ' <span class="signal-member-direction">平盘</span>'
        if not isinstance(markers, list) or not markers:
            return ' <span class="signal-member-direction">方向待下次更新</span>'
        if kind in ("ma", "ma_cross") and {"▲", "▼"} <= {m.get("shape") for m in markers
                                                 if isinstance(m, dict)}:
            # 不同周期均线方向相反（如站上短均线却跌破长均线）：同格上下箭头会互相打架，
            # 跟布林+RSI 方向不一致时一样改成文字，不画三角。
            labels = [_ma_display_text(m["label"], d.get("ma_settings")) for m in markers
                      if isinstance(m, dict) and isinstance(m.get("label"), str) and m["label"]]
            return (f' <span class="signal-member-direction">'
                    f'{esc(" / ".join(labels) or "多周期均线方向不一致")}</span>')
    else:
        return ""
    icons = []
    for marker in markers[:4]:
        if not isinstance(marker, dict):
            continue
        shape, tone, label = marker.get("shape"), marker.get("tone"), marker.get("label")
        if shape not in ("▲", "▼", "■", "□") or tone not in ("white", "yellow", "red", "cyan", "purple"):
            continue
        allowed = {"chg": ("white",), "amp": ("white",), "vol_ratio": ("white",),
                   "rsi": ("yellow", "red"), "boll": ("white", "yellow"),
                   "boll_rsi": ("yellow", "red"), "ma": ("cyan",), "ma_cross": ("cyan",),
                   "ma_band": ("cyan",), "target": ("purple",)}
        if tone not in allowed.get(kind, ()):
            continue
        if shape not in ("▲", "▼") and kind not in ("target", "ma", "ma_band"):
            continue
        if shape == "□" and (kind != "target" or level != "purple" or tone != "purple"):
            continue
        if not isinstance(label, str) or not label or len(label) > 100:
            continue
        if kind in ("ma", "ma_cross", "ma_band"):
            label = _ma_display_text(label, d.get("ma_settings"))
        icons.append(f'<span class="signal-member-icon tone-{tone}" role="img" '
                     f'aria-label="{esc(label)}">{shape}</span>')
    return "".join(icons) or ' <span class="signal-member-direction">方向待下次更新</span>'


def _filter_detail(d, kind, label):
    """同时满足筛选里每个条件的简短数值，与计数明细口径一致；缺值时只写信号名。"""
    if kind in ("chg", "amp", "vol_ratio") and isinstance(d.get("metric"), str) and d["metric"]:
        return d["metric"]
    if kind == "rsi" and isinstance(d.get("rsi_hits"), str) and d["rsi_hits"]:
        return f"RSI {d['rsi_hits']}"
    if kind == "boll" and isinstance(d.get("boll_state"), str) and d["boll_state"]:
        return f"布林 {d['boll_state']}"
    if kind == "boll_rsi" and isinstance(d.get("direction"), str) and d["direction"]:
        hits = d.get("boll_rsi_hits")
        extra = f" RSI{hits}条" if type(hits) is int and hits in (2, 3) else ""
        return f"布林+RSI {d['direction']}{extra}"
    marks = [m.get("label") for m in d.get("indicators") or []
             if isinstance(m, dict) and isinstance(m.get("label"), str) and m.get("label")]
    detail = f"{label} {' / '.join(marks)}" if marks else label
    return _ma_display_text(detail, d.get("ma_settings")) if kind in ("ma", "ma_cross", "ma_band") else detail


def _earnings_week_panel(snapshot, count_only=False):
    """计数表下方的未来15天（美东今天起至第15天，含）财报名单，不是交易信号，不挤进红黄紫列。
    count_only=True 时只返回已核验名单条数；快照缺失或无效返回 None，不当作 0。"""
    pending = None if count_only else '<details class="earnings-week"><summary>未来15天财报 · 名单待下次更新</summary></details>'
    data = snapshot.get("earnings_week")
    if not isinstance(data, dict):
        return None if count_only else ""
    asof, members = data.get("asof"), data.get("members")
    if not isinstance(asof, str) or not isinstance(members, list):
        return pending
    try:
        asof = datetime.strptime(asof, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return pending
    esc = lambda value: html_lib.escape(str(value), quote=True)
    lines = []
    for entry in members:
        if (not isinstance(entry, dict) or not isinstance(entry.get("symbol"), str)
                or not entry["symbol"].strip() or not isinstance(entry.get("note", ""), str)
                or not earnings_within_list({"status": "ok", "date": entry.get("date")}, asof)):
            return pending
        date = entry["date"]
        timing = {"pre": "盘前", "post": "盘后", "during": "盘中"}.get(entry.get("timing"), "时段待定")
        kind = {"official": "官方活动日期", "expected": "预计", "estimated": "估算"}.get(entry.get("kind"), "来源待确认")
        note = f'<span class="note">{esc(entry["note"])}</span>' if entry.get("note") else ""
        belong = _member_belong(entry)   # 跟其它名单一样：分组写在代码后面的括号里，持仓分长/短
        level = entry.get("level")
        lamp_name = {"red": "红灯", "yellow": "黄灯", "green": "绿灯", "gray": "缺失"}.get(level)
        # 灯跟统计表其它栏目同一套：行首的列表圆点，颜色跟着这只股自己的灯级。
        # marker 读屏读不到，补一段不可见文字，别让灯级信息只靠颜色传达。
        lamp_cls = f' class="lamp-{level}"' if lamp_name else ""
        lamp_text = f'<span class="lamp-text">（{lamp_name}）</span>' if lamp_name else ""
        # 名字（含中文备注）整体可点：跳到首页该股所在行，和其它灯的名单同一套 signal-member-jump
        anchor = _stock_anchor_id(entry["symbol"])
        urg = earnings_urgency({"status": "ok", "date": date, "timing": entry.get("timing")}, asof)
        urg_cls = {"red": " is-red", "yellow": " is-yellow"}.get(urg, "")
        lines.append(f'<li{lamp_cls}><a class="signal-member-jump" href="#{anchor}">'
                     f'<span class="signal-member-symbol">{esc(entry["symbol"])}</span>{note}</a>'
                     f'<span class="signal-member-belong">（{esc(belong)}）</span> '
                     f'<span class="earnings-week-meta{urg_cls}">美东 {esc(date[5:].replace("-", "/"))} {timing} · {kind}</span>'
                     f'{lamp_text}</li>')
    if count_only:
        return len(lines)
    if not lines:
        return ""
    summary = f'未来15天财报 · {len(lines)} 只'
    return (f'<div class="signal-extra-row earnings-extra">'
            f'<button type="button" class="signal-filter-toggle" data-kind="earnings_week" '
            f'aria-pressed="false" aria-label="选择未来15天财报叠加条件">财报条件</button>'
            f'<details class="earnings-week"><summary>{summary}</summary>'
            f'<ul>{"".join(lines)}</ul></details></div>')


def _signal_counts_card(snapshot):
    """各信号计数及分组明细；空计数不显示，缺失或不一致的成员等待下次更新。"""
    snapshot = _merge_ma_snapshot(snapshot)
    counts = snapshot.get("signal_counts") or {}
    members = snapshot.get("signal_members") or {}
    levels = (("red", "红色信号"), ("yellow", "黄色信号"))
    group_order = {g["item_group"]: i for i, g in enumerate(_display_groups())}
    group_labels = {g["item_group"]: g["label"] for g in GROUPS}
    esc = lambda value: html_lib.escape(str(value), quote=True)
    rows = ""
    filters = {}  # 同时满足筛选：每类信号的已核验成员（红/黄/紫合并）
    gray_by_kind = _gray_by_kind(snapshot)
    kinds_iter = [(kind, value) for kind, value in counts.items() if kind not in ("fund", "target")]
    for gkind in ("rsi", "boll", "boll_rsi"):
        if gray_by_kind.get(gkind) and gkind not in counts:
            kinds_iter.append((gkind, {"label": dict(SIGNAL_KINDS)[gkind]}))
    for kind, v in kinds_iter:
        # 只显示这一轮真的有计数的信号：全 0 的行不占位。
        # 布林和 RSI 现在都不再单独报灯，计数恒为 0，这两行会自然消失——
        # 不需要在这里写死排除，免得以后规则再变时要改两处。
        if v.get("pending") and kind == "ma":
            filters[kind] = {"label": "均线", "ok": False, "members": {}}
            rows += ('<tr><td><button type="button" class="signal-filter-toggle" data-kind="ma" '
                     'aria-pressed="false">均线</button></td><td colspan="3">明细待下次更新</td></tr>')
            continue
        if not any(v.get(level) for level, _ in levels) and not gray_by_kind.get(kind):
            continue
        raw_label = str(v.get("label", ""))
        label = esc(raw_label)
        flt = filters.setdefault(kind, {"label": raw_label, "ok": True, "members": {}})
        kind_id = hashlib.sha256(str(kind).encode("utf-8")).hexdigest()
        cells, panels = [], []
        kind_members = members.get(kind) if isinstance(members, dict) else None
        for level, level_name in levels:
            n = v.get(level, 0)
            if not n:
                cells.append('<td class="num">—</td>')
                continue
            panel_id = f"signal-members-{kind_id}-{level}"
            cells.append(f'<td class="num"><button type="button" class="signal-count-toggle" '
                         f'data-target="{panel_id}" aria-controls="{panel_id}" aria-expanded="false" '
                         f'aria-label="{label} · {level_name} · {esc(n)} 只，展开或收起标的">{esc(n)}</button></td>')
            entries = kind_members.get(level) if isinstance(kind_members, dict) else None
            valid = (isinstance(entries, list) and len(entries) == n
                     and all(isinstance(d, dict) and isinstance(d.get("symbol"), str)
                             and d["symbol"].strip() for d in entries))
            if valid:
                valid = len({instrument_identity(d["symbol"]) for d in entries}) == n
            if not valid:
                flt["ok"] = False
            else:
                for d in entries:
                    rec = flt["members"].setdefault(instrument_identity(d["symbol"]), {
                        "s": d["symbol"], "n": d.get("note") if isinstance(d.get("note"), str) else "",
                        "g": group_labels.get(str(d.get("group") or ""), str(d.get("group") or "") or "未分类"),
                        "o": group_order.get(str(d.get("group") or ""), len(GROUPS)),
                        "l": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "",
                        "d": []})
                    text = _filter_detail(d, kind, raw_label)
                    if text not in rec["d"]:
                        rec["d"].append(text)
            if not valid:
                content = '<p>明细待下次更新：当前快照缺少成员或成员人数与计数不一致，暂不展示名单。</p>'
            else:
                # 明细按方向分两段：先全部 ▲，一条横线，再全部 ▼，最后是没有三角的
                # （文字方向/方向待更新）。不写分组标题，也不写分组名——点名字直接跳过去。
                # 中文备注名保留，跟在代码后面，跟首页个股行一致。
                def _member_row(d, icon=True):
                    direction = _member_icons(d, kind, level).strip() if icon else ""
                    # 分框栏目的框标题已经写了方向，行尾不再重复同一句
                    metric = "" if kind in _NO_ROW_METRIC_KINDS else _member_metric_text(d, kind)
                    value = f' <span class="signal-member-metric">{esc(metric)}</span>' if metric else ""
                    note = (f'<span class="note">{esc(d["note"])}</span>'
                            if isinstance(d.get("note"), str) and d["note"].strip() else "")
                    # 分组写在代码后面的括号里：持仓内部标长/短，跟首页分区对得上
                    belong = f'<span class="signal-member-belong">（{esc(_member_belong(d))}）</span>'
                    # 灯统一用列表圆点（marker）画在最前面：
                    # 52周低位、布林+RSI 等栏目都是这个位置、这个大小、按这一格灯色着色。
                    return (f'<li>'
                            f'<a class="signal-member-jump" href="#{_stock_anchor_id(d["symbol"])}">'
                            f'<span class="signal-member-symbol">{esc(d["symbol"])}</span>{note}</a>'
                            f'{belong}{direction}{value}</li>')

                ordered = sorted(entries, key=lambda x: group_order.get(str(x.get("group") or ""), len(GROUPS)))
                if kind in _BLOCK_PANEL_KINDS:
                    # 方向放到框标题上：▲ 一框、▼ 一框，名单里不再逐条画三角
                    blocks, order = {}, []
                    for d in ordered:
                        shape, title = _member_block(d, kind)
                        if (shape, title) not in blocks:
                            blocks[(shape, title)] = []
                            order.append((shape, title))
                        blocks[(shape, title)].append(_member_row(d, icon=False))
                    order.sort(key=lambda k: (0 if k[0] == "▲" else 1 if k[0] == "▼" else 2))
                    content = '<div class="signal-member-groups">' + "".join(
                        f'<div class="signal-member-block"><h5>{esc(f"{shape} {title}".strip())}</h5>'
                        f'<ul class="signal-member-list">{"".join(blocks[(shape, title)])}</ul></div>'
                        for shape, title in order) + '</div>'
                else:
                    buckets = {"up": [], "down": [], "other": []}
                    for d in ordered:
                        row = _member_row(d)
                        buckets["up" if "▲" in row else "down" if "▼" in row else "other"].append(row)
                    parts = ["".join(buckets[name]) for name in ("up", "down", "other") if buckets[name]]
                    # 分隔线必须单独成变量：写成相邻字符串字面量会先被隐式拼接，
                    # 结果把 <div>/<ul> 一起当成连接符插进每一段之间。
                    divider = '<li class="signal-member-split" role="separator"></li>'
                    content = ('<div class="signal-member-groups"><ul class="signal-member-list">'
                               + divider.join(parts) + '</ul></div>')
            # tone-* 只用来给名单前面的圆点上色：这一格是红档，圆点就是红的
            panels.append(f'<div class="signal-members-panel tone-{level}" id="{panel_id}" hidden>{content}</div>')
        gray_rows = gray_by_kind.get(kind) or []
        if gray_rows:
            g_cell, g_panel, g_ok = _gray_cell(kind, gray_rows, raw_label, flt["members"], group_labels, group_order)
            cells.append(g_cell)
            panels.append(g_panel)
            if not g_ok:
                flt["ok"] = False
        else:
            cells.append('<td class="num">—</td>')
        rows += (f'<tr><td><button type="button" class="signal-filter-toggle" data-kind="{esc(kind)}" '
                 f'aria-pressed="false">{label}</button></td>{"".join(cells)}</tr>'
                 f'<tr class="signal-members-row"><td colspan="4">{"".join(panels)}</td></tr>')
    earnings_panel = _earnings_week_panel(snapshot)
    purple_panel = _target_purple_panel(snapshot)
    if not rows and not earnings_panel and not purple_panel and (
            "target_overlays" in snapshot or not any(
                type((snapshot.get("summary") or {}).get(level)) is int
                and snapshot["summary"][level] > 0 for level in ("red", "yellow"))):
        return ""
    if "boll" in filters and "boll_rsi" in filters:  # 点「布林」时也算上布林+RSI组合里的标的
        merged = {key: {**rec, "d": list(rec["d"])} for key, rec in filters["boll"]["members"].items()}
        for key, rec in filters["boll_rsi"]["members"].items():
            target = merged.setdefault(key, {**rec, "d": []})
            target["d"] += [t for t in rec["d"] if t not in target["d"]]
        filters["boll"] = {**filters["boll"], "ok": filters["boll"]["ok"] and filters["boll_rsi"]["ok"],
                           "members": merged}
    group_order = {g["item_group"]: i for i, g in enumerate(_display_groups())}
    group_labels = {g["item_group"]: g["label"] for g in GROUPS}

    def add_overlay_filter(kind, label, entries, expected, detail_fn):
        valid = (isinstance(entries, list) and type(expected) is int and expected >= 0
                 and len(entries) == expected
                 and all(isinstance(d, dict) and isinstance(d.get("symbol"), str) and d["symbol"].strip()
                         for d in entries))
        if valid:
            valid = len({instrument_identity(d["symbol"]) for d in entries}) == expected
        rec = {"label": label, "ok": valid, "members": {}}
        if valid:
            for d in entries:
                symbol = normalize_symbol(d["symbol"])
                identity = instrument_identity(symbol)
                group = str(d.get("group") or "")
                rec["members"][identity] = {
                    "s": symbol,
                    "n": d.get("note") if isinstance(d.get("note"), str) else "",
                    "g": group_labels.get(group, group or "未分类"),
                    "o": group_order.get(group, len(GROUPS)),
                    "l": d.get("level") if d.get("level") in ("red", "yellow", "green", "gray") else "",
                    "d": [detail_fn(d)],
                }
        filters[kind] = rec

    overlays = snapshot.get("target_overlays")
    if isinstance(overlays, dict):
        purple_entries = _target_overlay_union(overlays)
        if purple_entries:
            add_overlay_filter("target_purple", "空间 / 达到", purple_entries, len(purple_entries),
                               lambda d: str(d.get("detail") or "空间 / 达到"))

    earnings = snapshot.get("earnings_week")
    if isinstance(earnings, dict):
        earn_members = earnings.get("members")
        asof = earnings.get("asof")
        valid_earnings = isinstance(asof, str) and isinstance(earn_members, list)
        if valid_earnings:
            try:
                asof = datetime.strptime(asof, "%Y-%m-%d").date().isoformat()
            except ValueError:
                valid_earnings = False
        if valid_earnings:
            valid_earnings = all(isinstance(d, dict) and isinstance(d.get("symbol"), str)
                                 and d["symbol"].strip()
                                 and earnings_within_list({"status": "ok", "date": d.get("date")}, asof)
                                 for d in earn_members)
        if valid_earnings and earn_members:
            timing_labels = {"pre": "盘前", "post": "盘后", "during": "盘中"}
            kind_labels = {"official": "官方日期", "expected": "预计", "estimated": "估算"}
            add_overlay_filter("earnings_week", "未来15天财报", earn_members, len(earn_members),
                               lambda d: f"财报 {d.get('date', '')} "
                               f"{timing_labels.get(d.get('timing'), '时段待定')} · "
                               f"{kind_labels.get(d.get('kind'), '来源待确认')}")

    filter_data = {kind: {"label": f["label"], "ok": f["ok"],
                          "members": sorted(({"id": key, **rec} for key, rec in f["members"].items()),
                                            key=lambda m: (m["o"], m["s"]))}
                   for kind, f in filters.items()}
    filter_json = json.dumps(filter_data, ensure_ascii=False, separators=(",", ":"))
    filter_html = (f'<div class="signal-filter" id="signalFilterResult" role="status" '
                   f'data-filter="{esc(filter_json)}" hidden></div>') if filter_data else ""
    dim = 'style="color:var(--dim)"'
    head = (f'<tr class="gray mh"><td {dim}>信号</td><td class="num" {dim}>红</td><td class="num" {dim}>黄</td>'
            f'<td class="num" {dim}>灰</td></tr>')
    summary = snapshot.get("summary") or {}
    whole = lambda n: n if type(n) is int and n >= 0 else None
    gray_count = len({instrument_identity(m["symbol"]) for m in snapshot.get("text_only") or []
                      if isinstance(m, dict) and m.get("symbol")}) if isinstance(snapshot.get("text_only"), list) else 0
    totals = (("red", "红", whole(summary.get("red"))), ("yellow", "黄", whole(summary.get("yellow"))),
              ("gray", "灰", gray_count), ("earn", "财报", _earnings_week_panel(snapshot, count_only=True)))
    stats = "".join(
        (_count_badge(key, n, name) if n is not None and key in ("red", "yellow", "gray")
         else f'<span class="count-total count-{key}">{name} {"—" if n is None else n}</span>')
        for key, name, n in totals)
    return (f'<details class="card group-card" id="signalCounts" open><summary class="grp"><span class="grp-name">统计</span>'
            f'<span class="group-stats">{stats}</span></summary>'
            f'{f"<table>{head}{rows}</table>" if rows else ""}{earnings_panel}'
            f'{purple_panel}{filter_html}'
            f'{_collapse_button("signalCounts", "统计")}</details>\n')


_GRAY_RSI_RE = re.compile(r"RSI ([123])条(超买|超卖)")


def _gray_by_kind(snapshot):
    """灰灯成员按 rsi / boll / boll_rsi 归类。"""
    rows = snapshot.get("text_only") or []
    out = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if (isinstance(r, dict) and str(r.get("kind") or "") in ("rsi", "boll", "boll_rsi")
                and isinstance(r.get("symbol"), str) and r["symbol"].strip()):
            out.setdefault(r["kind"], []).append(r)
    return out


def _gray_title(kind, r):
    """灰灯名单的框标题 (图形, 文字, RSI条数)：只分上轨 / 下轨和 RSI 条数，不分突破还是逼近。
    旧快照没有结构化字段时，从 detail 文字里解析。"""
    detail = str(r.get("detail") or "")
    side = r.get("side") or ("上轨" if "上轨" in detail else "下轨" if "下轨" in detail else "")
    n, rs = r.get("rsi_n") or 0, r.get("rsi_side") or ""
    if not n:
        m = _GRAY_RSI_RE.search(detail)
        if m:
            n, rs = int(m.group(1)), m.group(2)
    rsi = f"RSI {n}条{rs}" if n and rs else ""
    if kind == "rsi":
        return ("▲" if rs == "超买" else "▼" if rs == "超卖" else "", rsi or "方向待下次更新", n)
    if not side:
        return ("", "方向待下次更新", n)
    shape = "▲" if side == "上轨" else "▼"
    return (shape, side if kind == "boll" else (f"{side} · {rsi}" if rsi else side), n)


def _gray_cell(kind, picked, label, filter_members, group_labels, group_order):
    """灰列单元格 + 展开名单；名单按框标题分组（▲ 在上、▼ 在下），写法同其他分框栏目。
    同时把成员并进该行的「同时满足」筛选数据。返回 (单元格, 名单面板, 是否有效)。"""
    esc = lambda v: html_lib.escape(str(v), quote=True)
    panel_id = "signal-members-gray-" + hashlib.sha256(kind.encode("utf-8")).hexdigest()
    ok = len({instrument_identity(r["symbol"]) for r in picked}) == len(picked)
    if ok:
        blocks, order = {}, []
        for r in picked:
            shape, title, n = _gray_title(kind, r)
            key = (shape, title)
            if key not in blocks:
                blocks[key] = []
                order.append((shape, title, n))
            note = f'<span class="note">{esc(r.get("note"))}</span>' if str(r.get("note") or "").strip() else ""
            belong = f'<span class="signal-member-belong">（{esc(_member_belong(r))}）</span>'
            blocks[key].append((group_order.get(str(r.get("group") or ""), len(GROUPS)),
                                f'<li><a class="signal-member-jump" href="#{_stock_anchor_id(r["symbol"])}">'
                                f'<span class="signal-member-symbol">{esc(r["symbol"])}</span>{note}</a>{belong}</li>'))
            rec = filter_members.setdefault(instrument_identity(r["symbol"]), {
                "s": r["symbol"], "n": r.get("note") if isinstance(r.get("note"), str) else "",
                "g": group_labels.get(str(r.get("group") or ""), str(r.get("group") or "") or "未分类"),
                "o": group_order.get(str(r.get("group") or ""), len(GROUPS)),
                "l": r.get("level") if r.get("level") in ("red", "yellow", "green") else "green", "d": []})
            text = f"灰灯 · {label} · {title}"
            if text not in rec["d"]:
                rec["d"].append(text)
        order.sort(key=lambda k: (0 if k[0] == "▲" else 1 if k[0] == "▼" else 2, -k[2], k[1]))
        content = '<div class="signal-member-groups">' + "".join(
            f'<div class="signal-member-block"><h5>{esc(f"{shape} {title}".strip())}</h5><ul class="signal-member-list">'
            + "".join(li for _, li in sorted(blocks[(shape, title)], key=lambda x: x[0])) + '</ul></div>'
            for shape, title, _ in order) + '</div>'
    else:
        content = '<p>明细待下次更新：名单中有重复标的，暂不展示。</p>'
    cell = (f'<td class="num"><button type="button" class="signal-count-toggle" data-target="{panel_id}" '
            f'aria-controls="{panel_id}" aria-expanded="false" '
            f'aria-label="灰灯 · {esc(label)} · {len(picked)} 只，展开或收起标的">{len(picked)}</button></td>')
    panel = f'<div class="signal-members-panel tone-gray" id="{panel_id}" hidden>{content}</div>'
    return cell, panel, ok


def _target_purple_panel(snapshot):
    """展开空间/达到两段；高空间紫红空心优先，达到一律紫色实心并写具体事件。"""
    overlays = snapshot.get("target_overlays")
    if not isinstance(overlays, dict):
        return ""
    space, reached = overlays.get("space"), overlays.get("reached")
    if not isinstance(space, list) or not isinstance(reached, list) or not (space or reached):
        return ""
    valid = all(isinstance(d, dict) and isinstance(d.get("symbol"), str) and d["symbol"].strip()
                and isinstance(d.get("detail"), str) and d["detail"]
                for entries in (space, reached) for d in entries)
    valid = valid and all(len({instrument_identity(d["symbol"]) for d in entries}) == len(entries)
                          for entries in (space, reached))
    esc = lambda v: html_lib.escape(str(v), quote=True)
    order = {g["item_group"]: i for i, g in enumerate(_display_groups())}
    parts = []
    if valid:
        for section, entries, label in (("space", space, "空间"), ("reached", reached, "达到")):
            lines = []
            ordered = sorted(entries, key=lambda d: (0 if section == "space" and d.get("band") == "high" else 1,
                             order.get(d.get("group"), len(order)), d["symbol"]))
            for d in ordered:
                symbol = normalize_symbol(d["symbol"])
                note = f'<span class="note">{esc(d.get("note"))}</span>' if str(d.get("note") or "").strip() else ""
                tone = "tone-target-high" if section == "space" and d.get("band") == "high" else "tone-purple"
                shape = "□" if section == "space" else "■"
                lead = f'<span class="signal-member-icon {tone}" aria-hidden="true">{shape}</span>'
                high_class = ' class="target-space-high"' if tone == "tone-target-high" else ""
                lines.append(f'<li{high_class}>{lead}<a class="signal-member-jump" href="#{_stock_anchor_id(symbol)}">'
                             f'<span class="signal-member-symbol">{esc(symbol)}</span>{note}</a>'
                             f'<span class="signal-member-belong">（{esc(_member_belong(d))}）</span>'
                             f' <span class="signal-member-direction">{esc(d["detail"])}</span></li>')
            parts.append(f'<h5>{label}</h5><ul class="signal-member-list">{"".join(lines)}</ul>')
        content = "".join(parts)
    else:
        content = '<p>明细待下次更新：当前快照名单无效或有重复标的，暂不展示。</p>'
    return (f'<div class="signal-extra-row purple-extra">'
            f'<button type="button" class="signal-filter-toggle" data-kind="target_purple" '
            f'aria-pressed="false" aria-label="选择空间或达到叠加条件">紫灯条件</button>'
            f'<details class="text-only target-purple"><summary>空间{len(space)}只　达到{len(reached)}只</summary>'
            f'{content}</details></div>')


def _policy_details(snapshot):
    """决议信息仅保留在页底折叠详情中，不生成常驻卡片或接入风险评分。"""
    if "reference_alerts" not in snapshot:
        return ""
    refs = snapshot.get("reference_alerts")
    refs = refs if isinstance(refs, dict) else {}
    policy = refs.get("policy")
    policy = policy if isinstance(policy, dict) else {}
    fed = policy.get("fed")
    fed = fed if isinstance(fed, dict) else {}
    assessment_time = policy_alerts.moment(refs.get("assessed_at"))
    decision = policy_alerts.fed_decision(fed, assessment_time)
    esc = lambda value: html_lib.escape(str(value))
    status = decision["status"]
    published = policy_alerts.moment(fed.get("date"))
    valid = status != "unavailable"
    action = fed.get("action")
    action = action if isinstance(action, str) else ""
    label = {"raise": "加息", "lower": "降息", "maintain": "维持不变", "keep": "维持不变"}.get(action, "待更新")
    bps = fed.get("change_bps")
    if valid and action in ("raise", "lower") and policy_alerts.valid_number(bps) and 0 < bps <= 1000:
        label += f" {bps:g} 个基点"
    message = f'{label} · {published.date().isoformat()}（美东公布日）' if valid else "尚未取得可核实的正式决议"
    if status == "pending":
        message += " · 新决议信息待更新，以上为上次已核实消息"
    details = f'<p>{esc(message)}</p>'
    if valid:
        details += f'<p>联邦基金目标利率区间：{fed["low"]:.2f}%–{fed["high"]:.2f}%。</p>'
    released = policy_alerts.moment(fed.get("released_at"))
    if valid and released:
        details += (f'<p>正式公布：{released.astimezone(policy_alerts.NY).strftime("%Y-%m-%d %H:%M %Z")}（美东）'
                    f'／{released.astimezone(TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）。</p>')
    next_fed = policy_alerts.moment(fed.get("next_release"))
    if next_fed:
        details += (f'<p>下次计划决议：{next_fed.astimezone(policy_alerts.NY).strftime("%Y-%m-%d %H:%M %Z")}（美东）'
                    f'／{next_fed.astimezone(TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）；以官方实际发布为准。</p>')
    if fed.get("fetch_error"):
        details += f'<p>最近查询未成功：{esc(fed["fetch_error"])}。旧消息保留日期，不冒充最新结果。</p>'
    if fed.get("schedule_error"):
        details += f'<p>日程待更新：{esc(fed["schedule_error"])}。最近已核实决议继续保留。</p>'
    if valid:
        details += (f'<p><a href="{esc(fed["url"])}" target="_blank" rel="noopener noreferrer">美联储官方决议声明</a></p>')
    details += (f'<p><a href="{policy_alerts.FED_URL}" target="_blank" rel="noopener noreferrer">美联储官方会议日历</a></p>')
    checked = policy_alerts.moment(fed.get("checked_at"))
    if checked:
        details += f'<p>最近查询：{checked.astimezone(TZ).strftime("%Y-%m-%d %H:%M")}（北京时间）。</p>'
    details += ('<p>抓取来源与缓存间隔见名词解释和抓取。'
                '仅按例行会议日程更新，临时决议可能延迟发现。</p>')
    return (f'<details class="macro-help" data-status="{status}" id="policyAlertDetails">'
            '<summary>美联储利率决议 · 查看数据与来源</summary>' + details + '</details>')


def _oil_reference_row(snapshot):
    """风险表中的WTI报价行；日变化与风险灯分开，不进入综合评分。"""
    if "reference_alerts" not in snapshot:
        return ""
    refs = snapshot.get("reference_alerts")
    refs = refs if isinstance(refs, dict) else {}
    oil = refs.get("oil")
    oil = oil if isinstance(oil, dict) else {}
    esc = lambda value: html_lib.escape(str(value))
    valid = policy_alerts.valid_number
    level = oil.get("level", "gray")
    names = {"red": "警惕", "yellow": "注意", "green": "未触发", "gray": "待更新"}
    price, day = oil.get("price"), oil.get("day")
    if (not isinstance(level, str) or level not in names or oil.get("fetch_error")
            or not valid(price) or price <= 0 or not policy_alerts.moment(oil.get("date"))
            or (level == "green" and (oil.get("incomplete")
                or not all(valid(oil.get(key)) for key in ("day", "week", "month"))))):
        level = "gray"
    value = f'{price:.2f}<span class="unit">美元／桶</span>' if valid(price) else '—<span class="unit">美元／桶</span>'
    day_class = "oil-rise" if valid(day) and day > 0 else "oil-fall" if valid(day) and day < 0 else "dim"
    change = f'{day:+.2f}%' if valid(day) else "—"
    changes = ''.join(f'<p>{period}交易日涨幅：{oil[key]:+.1f}%</p>' if valid(oil.get(key))
                      else f'<p>{period}交易日涨幅：待核实</p>' for period, key in ((5, "week"), (20, "month")))
    details = (f'<details class="reference-detail"><summary>依据与来源</summary>{changes}'
               f'<p>日变化：{esc(oil.get("day_from") or "前一日待核实")} → {esc(oil.get("date") or "本期待更新")}。'
               f'主连价格来自CL=F；所有涨幅使用同一实际交割合约 '
               f'{esc(oil.get("contract_symbol") or "未核实")} / {esc(oil.get("contract") or "未核实")}，避免换月误报。</p>'
               f'<p><a href="{esc(oil.get("url") or "https://finance.yahoo.com/quote/CL=F/")}" target="_blank" rel="noopener noreferrer">'
               f'{esc(oil.get("source") or "Yahoo NYMEX WTI期货")}</a> · 日线，非实时夜盘</p>')
    if oil.get("fetch_error"):
        details += f'<p>取数状态：{esc(oil["fetch_error"])}</p>'
    checked = policy_alerts.moment(oil.get("checked_at") or refs.get("assessed_at"))
    if checked:
        details += f'<p>最近核对：{checked.astimezone(TZ).strftime("%Y-%m-%d %H:%M")} 北京时间</p>'
    details += ('<p>自定义规则：5日涨≥10%注意／≥20%警惕，20日涨≥20%注意／≥30%警惕。'
                '上涨速度反映价格冲击，不推断必然加息。</p></details>')  # 「不计入综合灯」2026-10-15 从面板移除，页底规则仍写明
    text = oil.get("text")
    if (not isinstance(text, str) or not text.strip()
            or (level == "gray" and oil.get("level") != "gray")):
        text = "原油数据待更新，暂不能判断成本压力。"
    return (f'<tr class="{level} oil-reference-row" id="oilAlert" data-level="{level}">'
            f'<td>WTI原油<span class="unit">CLmain · 参考</span><span class="unit">{esc(oil.get("date") or "日线待更新")}</span></td>'
            f'<td class="num">{value}</td><td class="num {day_class}">{change}<span class="unit">日涨跌</span></td>'
            f'<td><b class="oil-state">{names[level]}</b><div class="oil-explanation">{esc(text)}</div>{details}</td></tr>')


def _page_notes(snapshot):
    """页底集中收纳说明；不改变任何指标阈值和判断规则。"""
    return f'''<section class="card page-notes" id="pageNotes" aria-label="规则与数据说明">
<div class="grp">规则与数据说明</div>
<details class="macro-help" id="marketRiskRules"><summary>综合判断与宏观细则</summary>
<h5>综合评分</h5>
<p>{html_lib.escape(RISK_RULE_TEXT)}</p>
<h5>宏观指标细则</h5>
<p>垃圾债利差：低于黄线为绿、达到黄线为黄、达到红线为红；即使未到黄线，一周扩大至少 {HY_WEEK_YELLOW} bp 为黄、至少 {HY_WEEK_RED} bp 为红（水平仍低于黄线也照常报灯，说明另提醒观察是否持续）；达到 500 bp 会标注“信用压力很高”。红灯代表信用风险升温，不等于股市必跌。</p>
<p>VIX：低于绿线为绿；从绿线起为黄（紧张），从黄线起仍为黄（恐慌）；达到红线才为红。标普500距近一段高点回撤不足10%为绿、达到10%或15%为黄、达到20%为红，这些是已发生的跌幅。</p>
<p>上涨参与度：站上宏观长均线的股票不足50%为黄；不足30%时，还需近期垃圾债利差已为红，或金融压力NFCI达到或高于0，才升级红；站上宏观短均线的股票不足30%也为黄。宏观均线沿用数据源固定口径，不随个股均线设置变化。NFCI为周度：达到或高于0为黄，低于0为绿。没有可用数据时显示“无数据”，不当作安全信号。</p>
<h5>数据有效性</h5>
<p>综合判定按本轮目标交易日检查有效性：垃圾债利差、VIX、标普500最多落后7个自然日；上涨参与度最多5日；周度金融压力NFCI最多13日。未来日期、缺值或非有限数无效，缺失不当作绿。上涨参与度不足30%升级红还要求自身最多落后5日，且垃圾债利差最多落后5日并已为红，或NFCI最多落后13日且达到或高于0；垃圾债利差仅为黄不足以确认。</p>
<h5>补充参考</h5>
<p>美联储利率决议、WTI原油、10年美债收益率仅作参考，不计入五指标综合分或有效指标数。</p>
<p>10年美债收益率：相对约30个自然日前可用观测，上行至少{UST_MONTH_YELLOW} bp为参考黄，至少{UST_MONTH_WATCH}但不足{UST_MONTH_YELLOW} bp只提示、仍绿；缺历史则不判断。</p>
{_policy_details(snapshot)}
</details>
</section>'''


def _position_symbols():
    """持仓标的符号（holdings.json 的 positions 键）。抓数时据此给持仓个股也算 20 日收益率。"""
    try:
        import io as _io
        import json as _json
        with _io.open(os.path.join(BASE, "holdings.json"), encoding="utf-8") as stream:
            h = _json.load(stream)
        return [str(s) for s in (h.get("positions") or {}).keys()]
    except Exception:
        return []


def _cost_seed_json(fetched=None):
    """页面里的持仓种子：成本价来自 holdings.json 的 cost（本来就公开）；
    数量 / 现金 / 固定收益来自 position-costs.seed.json（公开入库，构建环境也读得到；
    旧名 position-costs.local.json 仍兼容）。只是「第一次打开时的兜底值」——
    本机 localStorage 和云端 position-costs.json 都优先于它。"""
    import json as _json
    items, cash, cash_extra, fixed, fixed_cost = {}, 0, 0, 0, 0
    try:
        import io as _io
        with _io.open(os.path.join(BASE, "holdings.json"), encoding="utf-8") as stream:
            h = _json.load(stream)
        for sym, c in (h.get("positions") or {}).items():
            cost = (c or {}).get("cost")
            if isinstance(cost, (int, float)) and cost > 0:
                items[sym] = {"cost": cost, "qty": 0}
    except Exception as e:
        log(f"  ! 持仓成本种子读取失败：{e}")
    asof = ""
    p = os.path.join(BASE, "position-costs.seed.json")
    if not os.path.exists(p):
        p = os.path.join(BASE, "position-costs.local.json")
    if os.path.exists(p):
        try:
            import io as _io
            with _io.open(p, encoding="utf-8") as stream:
                loc = _json.load(stream)
            asof = str(loc.get("asof") or "")[:10]
            for sym, q in (loc.get("positions") or {}).items():
                if sym in items and isinstance(q, (int, float)) and q > 0:
                    items[sym]["qty"] = q
            c = loc.get("cash")
            cash = c if isinstance(c, (int, float)) else ((c or {}).get("cash") or 0)
            extra = loc.get("cash_extra", 0)
            if (isinstance(extra, (int, float)) and not isinstance(extra, bool)
                    and math.isfinite(extra) and 0 <= extra <= cash):
                cash_extra = extra
            fi = loc.get("fixed_income") or {}
            fixed, fixed_cost = fi.get("market") or 0, fi.get("cost") or 0
        except Exception as e:
            log(f"  ! 持仓数量种子读取失败（只用成本价）：{e}")
    # rs20：持仓组合「20日对.SPX」用的快照——每只持仓自己的 20 日涨跌 + 同期基准涨跌。
    # 只提供收益率，市值加权在浏览器里按本机数量即时算（改数量不用重跑抓数）。
    rs20 = None
    try:
        pos = {str(s).upper() for s in _position_symbols()}
        rs_items, bench, meta = {}, None, None
        for it in fetched or []:
            rs = (it or {}).get("rs_spy") or {} if isinstance(it, dict) else {}
            if isinstance(rs.get("bench"), (int, float)) and rs.get("days"):
                bench, meta = rs["bench"], rs
            sym = str(it.get("symbol") or "").upper()
            if sym in pos and isinstance(rs.get("etf"), (int, float)):
                rs_items[sym] = round(float(rs["etf"]), 4)
        if bench is not None and meta and rs_items:
            rs20 = {"asof": meta.get("end") or "", "start": meta.get("start") or "",
                    "end": meta.get("end") or "", "days": meta.get("days") or 20,
                    "bench": round(float(bench), 4),
                    "bench_name": meta.get("bench_name") or "标普500", "items": rs_items}
            # 这里刻意不打日志：本函数在页面渲染路径上执行，
            # render 期间调 log 会破坏离线测试契约（tests/mm_test_render 断言 log 未被调用）
            pass
    except Exception:
        rs20 = None
    # asof＝内置快照的日期（种子文件里的券商截图日期）：换设备取不到云端时页面要拿它说明数据来源
    return _json.dumps({"asof": asof, "cash": cash, "cash_extra": cash_extra, "fixed": fixed,
                        "fixed_cost": fixed_cost, "items": items, "rs20": rs20}, ensure_ascii=False)


def _cost_panel_assets(items=None):
    """仓位管理面板：CSS / JS 放在仓库根的 cost_panel.css / cost_panel.js 两个独立文件里，
    构建时内联进 index.html。文件缺失就跳过这块，页面其它部分照常生成。"""
    css = js = ""
    try:
        import io as _io
        import re as _re
        with _io.open(os.path.join(BASE, "cost_panel.css"), encoding="utf-8") as stream:
            css = stream.read()
        # 这个文件自带 <style>…</style>，但这里是插进页面已有的 <style> 里，必须剥掉标签，
        # 否则 HTML 里出现嵌套 <style>，浏览器会把后面的 </style> 当正文渲染出来
        css = _re.sub(r"</?style[^>]*>", "", css)
        with _io.open(os.path.join(BASE, "cost_panel.js"), encoding="utf-8") as stream:
            js = stream.read()
    except Exception as e:
        log(f"  ! 仓位面板资源缺失，跳过：{e}")
        return "", ""
    effective = json_script({**DEFAULT_SETTINGS, **S})
    rules_js = f"<script>window.__MM_RULES__={effective};</script>\n"
    return css, rules_js + js.replace("__MM_REAL__", _cost_seed_json(items))


def _rule_constants():
    """规则说明中的固定参数直接引用真实引擎，避免另写一份默认数字。"""
    source_labels = {"nasdaq": "Nasdaq", "yahoo": "Yahoo", "stooq": "stooq"}
    return {"quote_closed_sources": " → ".join(source_labels[name] for name in QUOTE_CLOSED_SOURCES),
            "quote_intraday_sources": " → ".join(source_labels[name] for name in QUOTE_INTRADAY_SOURCES),
            "rsi_periods": " / ".join(map(str, RSI_PERIODS)),
            "vol_ratio_base": VOL_RATIO_BASE, "vol_z_window": VOL_Z_WINDOW,
            "intraday_vol_quiet_min": INTRADAY_VOL_QUIET_MIN,
            "vol_lookback": VOL_LOOKBACK, "vol_min_returns": VOL_MIN_RETURNS,
            "vol_low_sigma": VOL_LOW_SIGMA, "vol_high_sigma": VOL_HIGH_SIGMA,
            "vol_mult": " / ".join(f"×{VOL_MULT[k]:g}" for k in ("low", "mid", "high")),
            "ma_confirm_lookback": MA_CONFIRM_LOOKBACK,
            "ma_confirm_min": MA_CONFIRM_MIN, "ma_confirm_max": MA_CONFIRM_MAX,
            "ma_confirm_default": MA_CONFIRM_DEFAULT, "ma_long_band_pct": MA_LONG_BAND_PCT,
            "boll_sigma_factor": _BOLL_SIGMA_FACTOR, "target_space_pct": TARGET_SPACE_PCT,
            "target_high_space_pct": TARGET_HIGH_SPACE_PCT,
            "earn_soon_days": EARN_SOON_DAYS,
            "hy_week_yellow": HY_WEEK_YELLOW, "hy_week_red": HY_WEEK_RED}


# 兼容既有导入；页面渲染不使用这份 import 时快照，而按当时 S 重新生成。
RULES_BODY = rules_body(S, _rule_constants())
RULES_TEMPLATE = rules_template(S, _rule_constants())


def render(macro, items, watch_count, data_down=False, snapshot=None, dup_hidden=0):
    legacy_target_light = (any(kind == "target" and level in ("red", "yellow")
                               for d in items for kind, level in d.get("signal_marks") or [])
                           or any(isinstance(d.get("target"), dict) and d["target"].get("anomaly")
                                  for d in items))
    snapshot = _sanitize_snapshot(snapshot or {})
    items = [_sanitize_snapshot_fields(item) for item in items]
    overlays = target_overlay_members(items)
    union = _target_overlay_union(overlays)
    snapshot["target_overlays"] = overlays
    snapshot["signal_counts"] = {**(snapshot.get("signal_counts") or {}),
        "target": {"label": dict(SIGNAL_KINDS)["target"], "red": 0, "yellow": 0, "purple": len(union)}}
    snapshot["signal_members"] = {**(snapshot.get("signal_members") or {}),
        "target": {"red": [], "yellow": [], "purple": union}}
    snapshot["summary"] = {**(snapshot.get("summary") or {}), "purple": len(union)}
    if legacy_target_light:
        snapshot["summary"].update({level: sum(d.get("level") == level for d in items)
                                    for level in ("red", "yellow", "green", "gray")})
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
                          for group in _display_groups())

    # 来源与抓取间隔集中到独立名词／抓取区块；页脚只保留导航入口。
    risk_title = html_lib.escape(f"综合判断：{risk['label']} · 有效指标 {risk['valid_count']}/5")
    macro_stats = (f'<span class="market-risk-badge" id="marketRiskLight" data-level="{risk["level"]}" '
                   f'role="status" aria-label="{risk_title}">'
                   f'<i class="stat-dot {risk["level"]}" aria-hidden="true"></i>'
                   f'综合：{html_lib.escape(risk["label"])}</span>')
    dca_title = dca_badge(snapshot.get("dca_reminder"))  # 顶栏最右侧（2026-10-19：从市场风险标题挪过来）
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    snapshot_json = snapshot_json.replace("</", "<\\/")
    finished_iso = beijing_iso(snapshot.get("finished_at_bj") or snapshot.get("finished_at"))
    finished_txt = finished_iso[:19].replace("T", " ") if finished_iso else "未记录"
    # 一行摘要升级为状态灯文案（首页第三排）
    init_light_phase, init_light = _run_light(snapshot, finished_txt)
    # 第二排：数据时间 + 自动计划。冬夏令时只显示当日适用的那条（以当天美东是否夏令时为准）。
    ny_now = datetime.now(US_TZ)
    bj_auto = "夏令时次日 08:15" if ny_now.dst() != timedelta(0) else "冬令时次日 09:15"
    sub_line = (f"数据时间 {market_data_time(snapshot)} · "
                f"自动计划：美东周一至五 20:15（北京 {bj_auto}）")
    # 「在册：…」一整行按 2026-10-15 要求删除，不再在首页展示分组在册数与去重说明。

    cost_css, cost_js = _cost_panel_assets(items)
    current_rules = rules_template(S, _rule_constants())
    # 离线/回放可把 BASE 指向仅含配置的数据目录；静态脚本仍来自实际源码目录。
    asset_base = BASE if os.path.isfile(os.path.join(BASE, "run-status.js")) else os.path.dirname(__file__)
    run_js = script_asset(asset_base, "run-status.js")

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
<title>AI监测</title>
<style>
{PAGE_CSS.removesuffix(RULES_CSS)}
{cost_css}
{RULES_CSS}</style>
</head>
<body>{current_rules}<div class="wrap">
<h1>AI监测</h1>
<div class="sub">{sub_line}</div>

<div class="runbar">
  <button class="primary" id="btnRunNow">运行</button>
  <button id="btnCheckStatus">状态</button>
  <button id="btnHardRefresh">刷新</button>
  <a class="btnlink" href="./edit.html">设置</a>
  {dca_title}
  <script>{HARD_REFRESH_JS}</script>
</div>
<div class="statusrow">
  <div class="runlight" id="runLight" data-phase="{init_light_phase}"><i class="dot"></i><span id="runLightTxt">{init_light}</span></div>
  <div class="runlight rulelight" id="ruleLight" data-phase="idle"><i class="dot"></i><span id="ruleLightTxt">正在核对最新规则…</span></div>
</div>
<div id="checkResult" class="check-result" role="status" hidden></div>

<div class="script-data" hidden><script id="snapshotData" type="application/json">{snapshot_json}</script></div>

{('<div class="warn">本轮标的均未取得可用报价；可能是接口故障、异常行情或抓取时间预算不足，请查看个股原因。宏观数据另行展示。</div>' if data_down else "")}

{signal_card}
<details class="card macro-card" id="macroCard">
<summary class="grp"><span class="grp-name">市场风险</span><span class="group-stats">{macro_stats}</span></summary>
<p class="macro-help" id="marketRiskReason">{html_lib.escape(risk['text'])} · 有效指标 {risk['valid_count']}/5</p>
<table>
<tr class="gray mh"><td style="color:var(--dim)">指标</td><td class="num" style="color:var(--dim)">当前</td><td class="num" style="color:var(--dim)">变化</td><td style="color:var(--dim)">状态</td></tr>
{rows_macro}
{_oil_reference_row(snapshot)}
</table>
{_collapse_button("macroCard", "市场风险")}
</details>

{group_cards}

<div class="acts">
<span id="runMsg"></span>
</div>
{_page_notes(snapshot)}
{run_js}
<script>window.MM_SETTINGS={{"signal_single_open":{int(S.get("signal_single_open", 1))}}};</script>
<script>{PAGE_INTERACTION_JS}</script>
{cost_js}
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
    log(f"行情抓取完成：取到 {done}/{len(symbols)}（是不是目标交易日的日线，看下面「当日日线」那行）")
    return results


def build_reference_alerts(prev=None):
    """额外指标独立取数入口；任何失败只影响参考提示，不拖垮原行情。"""
    prev = prev if isinstance(prev, dict) else {}
    old_policy = prev.get("policy")
    old_policy = old_policy if isinstance(old_policy, dict) else {}
    try:
        policy = policy_alerts.update_policy(old_policy, NOW)
    except Exception as exc:
        log(f"美联储决议独立取数失败：{str(exc)[:100]}")
        old_fed = old_policy.get("fed")
        old_fed = old_fed if isinstance(old_fed, dict) else {}
        fed = {**old_fed, "fetch_error": str(exc)[:180], "pending_update": True, "checked_at": NOW.isoformat()}
        decision = policy_alerts.fed_decision(fed, NOW)
        policy = {"fed": fed, "summary": {k: decision[k] for k in ("level", "status", "text", "incomplete")}}
    oil_continuous, oil_contract = None, None
    try:
        oil_continuous = fetch_history("CLMAIN")
        if oil_continuous:
            code = policy_alerts.oil_contract_symbol(oil_continuous.get("quote_name"))
            if code:
                oil_contract = yahoo_history(code)
                if oil_contract and CLOSED_ONLY:
                    oil_contract = trim_history(oil_contract, TARGET_DATE)
    except Exception as exc:
        log(f"原油独立参考取数失败：{str(exc)[:100]}")
    oil = policy_alerts.oil_warning(oil_continuous, oil_contract, TARGET_DATE)
    return {"policy": policy, "oil": oil, "assessed_at": NOW.isoformat()}


# ---------------------------------------------------------------- main

class _ReplayResponse:
    """回放用的假响应，只覆盖代码里实际用到的字段。"""

    def __init__(self, status, text):
        self.status_code = status
        self.text = text or ""
        self.content = self.text.encode("utf-8")
        self.encoding = "utf-8"
        self.headers = {"Content-Type": "application/json"}
        self.ok = 200 <= status < 300

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"replay http {self.status_code}")


def install_http_snapshot(mode=None, path=None):
    """把 requests 的底层请求换成「记录 / 回放」。

    mode='dump'   边跑边记下成功的 GET 响应；
    mode='replay' 只读快照，未命中的一律按取数失败处理（不联网）。
    POST（派发、推送）不参与快照，照常走原逻辑。
    """
    state = {"mode": mode, "path": path, "data": {}, "miss": 0}
    if mode == "replay" and path:
        try:
            with open(path, encoding="utf-8") as f:
                state["data"] = json.load(f)
        except Exception as e:
            log(f"HTTP 快照读取失败，本次全部按取数失败处理：{e}")
    original = requests.Session.request

    def patched(self, method, url, *a, **kw):
        if str(method).upper() != "GET":
            return original(self, method, url, *a, **kw)
        key = f"GET {url}"
        if state["mode"] == "replay":
            hit = state["data"].get(key)
            if hit is None:
                state["miss"] += 1
                return _ReplayResponse(599, "")
            return _ReplayResponse(hit.get("status", 200), hit.get("text", ""))
        resp = original(self, method, url, *a, **kw)
        if state["mode"] == "dump" and getattr(resp, "status_code", 0) == 200:
            try:
                state["data"][key] = {"status": resp.status_code, "text": resp.text}
            except Exception:
                pass
        return resp

    requests.Session.request = patched
    return state


def dump_http_snapshot(state):
    if not isinstance(state, dict) or state.get("mode") != "dump" or not state.get("path"):
        return
    try:
        atomic_write_json(state["path"], state["data"])
        log(f"HTTP 快照已写入 {state['path']}（{len(state['data'])} 条请求）")
    except Exception as e:
        log(f"HTTP 快照写入失败：{e}")


def main(argv=None):
    global QUOTE_DEADLINE
    runtime = RuntimeState(BASE, target=TARGET_DATE, event=EVENT)
    try:
        return _main(argv, runtime)
    except Exception as exc:
        runtime.receipt("failed", f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    finally:
        QUOTE_DEADLINE = None
        YAHOO_GATE.reset(False)
        STOOQ_GATE.reset(False)


def _main(argv, runtime):
    global TARGET_DATE, CLOSED_ONLY, QUOTE_DEADLINE
    parser = argparse.ArgumentParser(description="市场自检生成器")
    parser.add_argument("--prepare-run", action="store_true", help="锁定本次运行目标交易日并输出 GitHub Actions 环境变量")
    parser.add_argument("--ensure-schedule-run", action="store_true", help="定时任务保险：缺失时补派发一次运行")
    parser.add_argument("--dump-http", metavar="PATH", help="记录本次成功的 GET 响应，供 --replay-http 离线重跑")
    parser.add_argument("--replay-http", metavar="PATH", help="只读 HTTP 快照渲染，全程零网络（未命中的按取数失败）")
    args = parser.parse_args(argv)
    if args.prepare_run:
        prepare_run()
        return 0
    if args.ensure_schedule_run:
        return ensure_schedule_run(runtime)
    if args.dump_http or args.replay_http:
        snap_mode = "dump" if args.dump_http else "replay"
        snap_path = args.dump_http or args.replay_http
        snap_state = install_http_snapshot(snap_mode, snap_path)
        atexit.register(dump_http_snapshot, snap_state)
        log(f"HTTP 快照模式：{snap_mode}（{snap_path}）")

    if not TARGET_DATE:
        plan = plan_run(EVENT, datetime.now(timezone.utc))
        TARGET_DATE, CLOSED_ONLY = plan["target"], plan["closed_only"]
    runtime.target = TARGET_DATE

    cfg_path = os.path.join(BASE, "holdings.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"holdings.json 读取失败，按空清单处理：{e}")
        cfg = {}

    universe, group_counts, dup_hidden = grouped_universe(cfg)
    # 一轮只读一个快照；进程内重复调用也必须重新取本轮到期数据。
    _EARN_CACHE.clear()
    try:
        with open(os.path.join(BASE, "status.json"), encoding="utf-8") as f:
            prev_status = json.load(f)
    except Exception:
        prev_status = {}
    prev_status = runtime.load_previous(prev_status)
    prev_cons = prev_status.get("consensus")
    old_cons = prev_cons.get("data") if isinstance(prev_cons, dict) else None
    old_cons = old_cons if isinstance(old_cons, dict) else {}
    cons_data, cons_stat, fetched = old_cons, {}, {}
    event_today, quota_day = _earn_today(), NOW.astimezone(TZ).date().isoformat()
    if not universe:
        log("holdings.json 里没有自选股，跳过个股部分")

    nfci_state = {}
    try:
        macro = build_macro(prev_status, nfci_state)
    finally:
        if nfci_state:
            nfci_state = runtime.checkpoint("nfci_cache", nfci_state)
    reference_alerts = build_reference_alerts(prev_status.get("reference_alerts"))
    reference_alerts = runtime.checkpoint("reference_alerts", reference_alerts)
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
        # 持仓个股也要算 20 日收益率：页面「持仓」卡标题的「20日对.SPX」按市值加权，
        # 需要每只持仓自己的 20 日涨跌（此前只给板块 ETF 算，持仓卡就永远没这个数）。
        # 只扩大「谁算rs」，不扩大「要不要抓基准」——基准本来就因为板块 ETF 而取，
        # 持仓是蹭现成数据，绝不为它额外发一次请求。
        rs_targets = sector_etfs | {normalize_symbol(s) for s in _position_symbols()}
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
        with _SRC_DAYS_LOCK:
            _SRC_DAYS.clear()
        fetched = collect_quotes(to_fetch)
        for src, days in source_day_summary().items():
            log(f"  {src} 最新日线：" + "，".join(f"{d} {n} 只" for d, n in days.items()))
        bench_data = None
        if bench_rows:
            bench_data = {"dates": [d for d, _ in bench_rows], "closes": [v for _, v in bench_rows]}
        elif spy_key and isinstance(fetched.get(spy_key), dict):
            bench_data = fetched[spy_key]
        YAHOO_GATE.reset(False)
        STOOQ_GATE.reset(False)
        # 先用旧共识价解析当前技术信号，为财报请求提供持仓/红黄优先级；刷新后仅重算变化项。
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
                if bench_data and normalize_symbol(sym) in rs_targets:
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
    prev_earn = prev_status.get("earnings")
    prev_earn = prev_earn if isinstance(prev_earn, dict) else {}
    earn_stat = attach_earnings(items, prev_earn.get("data"), event_today, prev_earn.get("events"))
    earn_stat = runtime.checkpoint("earnings", earn_stat)
    cons_syms = [normalize_symbol(sym) for sym, sc, grp in universe if consensus_wanted(sym, sc, grp)]
    try:
        cons_data, cons_stat = update_consensus(cons_syms, prev_cons, event_today,
                                               next_earn=_next_earnings_dates(earn_stat),
                                               earnings_events=earn_stat.get("events"), quota_day=quota_day)
    except Exception as e:
        log(f"  ! 共识价更新中断，沿用旧值：{str(e)[:90]}")
        cons_data = {k: v for k, v in old_cons.items() if k in cons_syms and isinstance(v, dict)}
        cons_stat = {"stopped": "error", "quota": _consensus_quota(prev_cons, quota_day)}
    consensus_state = runtime.checkpoint("consensus", {
        "source": CONSENSUS_SOURCE, "stat": cons_stat, "data": cons_data,
        "quota": cons_stat.get("quota", _consensus_quota(prev_cons, quota_day))})
    configs = {sym: (sc, grp) for sym, sc, grp in universe}
    for i, d in enumerate(items):
        sym = d["symbol"]
        key = normalize_symbol(sym)
        if not isinstance(fetched.get(sym), dict) or cons_data.get(key) == old_cons.get(key):
            continue
        sc, grp = configs[sym]
        cons = cons_data.get(key)
        _, _, detail = analyze_symbol(sym, {**sc, "_consensus": cons} if cons else sc, fetched[sym], group=grp)
        for field in ("earnings", "rs_spy"):
            if field in d:
                detail[field] = d[field]
        items[i] = detail
    snapshot = build_snapshot(macro, items, cfg, group_counts=group_counts)
    snapshot["nfci_cache"] = nfci_state
    snapshot["consensus"] = consensus_state
    snapshot["earnings"] = earn_stat
    snapshot["reference_alerts"] = reference_alerts
    fresh, today_n, expected_n = snapshot_freshness(snapshot, TARGET_DATE)
    runtime.fresh, runtime.today, runtime.expected = fresh, today_n, expected_n
    snapshot["data_lag"] = {"target": TARGET_DATE, "fresh": fresh, "today": today_n, "expected": expected_n,
                            "by_source": source_day_summary()}
    if universe:
        log(f"当日日线 {today_n}/{expected_n}（目标交易日 {TARGET_DATE}）" + ("" if fresh else " —— 不齐"))
    # 发布门槛：只拦定时口径（含保险补跑）。当日日线不齐就不覆盖线上页面，也不推送，
    # 让 status.json 保持旧的目标日 → 20:45 保险会判「缺失」再补跑一次。手动/保存触发照常发布。
    if EVENT == "schedule" and CLOSED_ONLY and universe and not fresh:
        msg = (f"定时运行：目标交易日 {TARGET_DATE} 当日日线只有 {today_n}/{expected_n}，"
               f"源端当日数据未就绪，本轮不发布、不推送（页面保留上一版）")
        log("! " + msg)
        print(f"::warning title=当日数据不齐，未发布::{msg}", flush=True)
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write("publish=no\n")
        runtime.receipt("skipped", "freshness_below_threshold", detail=msg)
        return 0
    supported_items = [d for d in items if quote_supported(d["symbol"])]
    html = render(macro, items, len(universe), data_down=bool(supported_items)
                  and all(d["level"] == "gray" for d in supported_items), snapshot=snapshot,
                  dup_hidden=dup_hidden)
    out = os.path.join(BASE, "index.html")
    status_path = os.path.join(BASE, "status.json")
    write_artifacts(BASE, html, snapshot,
                    receipt=runtime.receipt_data("generated", "awaiting_publish"))
    log(f"已生成 {out} 与 {status_path}，待发布；目标交易日 {TARGET_DATE}，实际行情日期 {snapshot['actual_dates']}")

    try:
        push_serverchan(macro, items, snapshot=snapshot)
    except Exception as e:
        log(f"推送环节异常（忽略）：{str(e)[:90]}")
    return 0


def push_serverchan(macro, items, snapshot=None):
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

    identity = snapshot if isinstance(snapshot, dict) else build_identity()
    target = identity.get("target_trade_date", TARGET_DATE)
    build_id = identity.get("build_id", "")
    lines = ["本轮已生成待发布；尚未确认提交或 Pages 更新，在线页面可能仍为上一版。",
             f"目标交易日：{target or '未提供'}；build_id：{build_id or '未提供'}", ""]
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
    lines.append("[打开在线看板（可能仍为上一版）](https://nixhuang.github.io/market-monitor/)")

    title = "%s 市场自检 🔴%d 🟡%d" % (
        NOW.strftime("%m-%d"), len(reds), len(yellows))
    title += " · 已生成待发布"
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
