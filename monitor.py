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
import base64
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
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
import policy_alerts
import earnings_sources
import market_sessions
from runtime_state import RuntimeState, atomic_write_json, build_identity, write_artifacts


# 职责实例只保存当前入口模块；兼容导出仍支持旧调用与动态 patch。
import monitor_indicators
import monitor_rules
import monitor_snapshot
import monitor_render
_indicator_calculator = monitor_indicators.IndicatorCalculator(sys.modules[__name__])
_rule_engine = monitor_rules.RuleEngine(sys.modules[__name__])
_snapshot_builder = monitor_snapshot.SnapshotBuilder(sys.modules[__name__])
_page_renderer = monitor_render.PageRenderer(sys.modules[__name__])

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
RUN_DEADLINE = None
RUN_BUDGET_SECONDS = 12 * 60
FINALIZE_RESERVE_SECONDS = 90  # 生成、封存及推送留白；早于外部15分钟任务上限
MACRO_BUDGET_SECONDS = 120
REFERENCE_BUDGET_SECONDS = 45
EARN_BUDGET_SECONDS = 90
_REQUEST_TLS = threading.local()


class BudgetExhausted(requests.Timeout, TimeoutError):
    """协作式预算已用完；不承诺终止DNS或持续分块响应等阻塞。"""


def request_remaining(deadline=None, clock=None):
    clock = clock or getattr(_REQUEST_TLS, "clock", None) or time.monotonic
    limits = [d for d in (RUN_DEADLINE, deadline, getattr(_REQUEST_TLS, "deadline", None))
              if d is not None]
    return min(limits) - clock() if limits else float("inf")


def request_timeout(seconds, deadline=None, clock=None):
    remaining = request_remaining(deadline, clock)
    if remaining <= 0:
        raise BudgetExhausted("本轮取数时间预算已用完，沿用旧辅助值或标记缺失")
    # requests 的标量超时分别用于连接与读取，两阶段各分至多一半剩余预算。
    return min(seconds, remaining / 2)


@contextmanager
def request_budget(deadline, clock=None):
    old = getattr(_REQUEST_TLS, "deadline", None), getattr(_REQUEST_TLS, "clock", None)
    _REQUEST_TLS.deadline = min(d for d in (deadline, old[0]) if d is not None)
    _REQUEST_TLS.clock = clock or old[1] or time.monotonic
    try:
        yield
    finally:
        _REQUEST_TLS.deadline, _REQUEST_TLS.clock = old


def budget_sleep(seconds, sleep=None, deadline=None, clock=None):
    if request_remaining(deadline, clock) <= seconds:
        return False  # 不为已无余量的下一次请求继续等待
    (sleep or time.sleep)(seconds)
    return request_remaining(deadline, clock) > 0


@contextmanager
def bounded_http_requests():
    """入口统一约束跨模块GET；嵌套的HTTP快照先恢复，本包装后恢复。"""
    original = requests.Session.request

    def bounded(session, method, url, *args, **kwargs):
        if str(method).upper() == "GET" and request_remaining() != float("inf"):
            timeout = kwargs.get("timeout")
            if isinstance(timeout, tuple):
                kwargs["timeout"] = tuple(request_timeout(t if t is not None else float("inf"))
                                          for t in timeout)
            else:
                kwargs["timeout"] = request_timeout(timeout if timeout is not None else float("inf"))
        return original(session, method, url, *args, **kwargs)

    requests.Session.request = bounded
    try:
        yield
    finally:
        requests.Session.request = original


def quote_timeout(seconds):
    deadlines = [d for d in (QUOTE_DEADLINE, getattr(_QUOTE_TLS, "deadline", None)) if d is not None]
    return request_timeout(seconds, min(deadlines) if deadlines else None)


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


TARGET_SPACE_PCT = monitor_rules.TARGET_SPACE_PCT


TARGET_HIGH_SPACE_PCT = monitor_rules.TARGET_HIGH_SPACE_PCT
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

    if request_remaining() <= 0:
        if state is not None and prior:
            state.update(prior, deferred="budget")
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


RS_BENCH_NAME = monitor_indicators.RS_BENCH_NAME
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
        if key not in ("hy_oas", "vix", "sp500", "ust10", "nfci"):
            continue
        if key != "nfci" and request_remaining() <= 0:
            macro[key] = {"ok": False, "name": cfg["name"], "deferred": "budget"}
            continue
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
        if not rows and key in ("vix", "sp500", "ust10") and budget_sleep(MACRO_RETRY_WAIT):
            log(f"  · {cfg['name']} 首次取数失败，重试一次")
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
    macro["breadth"] = (market_breadth(target) if request_remaining() > 0 else
                        {"ok": False, "name": "上涨参与度", "deferred": "budget"})
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
                return "red", "一周急升，信用升温"
            return "red", "一周急升，待观察"
        if v >= 500:
            return "red", "压力很高"
        if v >= S["hy_red"]:
            return "red", "压力升高"
        if v >= S["hy_green"]:
            return "yellow", "收紧"
        if dw is not None and dw >= HY_WEEK_YELLOW:
            return "yellow", "走阔转弱"
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
            return "yellow", f"急升 {bp:+.0f} bp"
        if bp >= UST_MONTH_WATCH:
            return "green", f"{bp:+.0f} bp 偏快"
        return "green", f"{bp:+.0f} bp 平稳"

    if key == "curve":
        v = item["value"]
        if v < 0:
            return "yellow", "倒挂"
        return "green", "正常"

    if key == "nfci":
        return ("yellow", "偏紧，留意风险") if item["value"] >= 0 else ("green", "偏宽松")

    if key == "breadth":
        long_term, short_term = item["value"], item["pct50"]
        if long_term < 30 and item.get("pressure_confirmed"):
            return "red", "走弱，压力同步升高"
        if long_term < 50:
            return "yellow", "过半跌破长均线"
        if short_term < 30:
            return "yellow", "短期仅少数支撑"
        return "green", "趋势尚稳"

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
        r = _earn_session().get(url, timeout=request_timeout(timeout))
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
        r = _sa_earn_session().get(SA_EARN_URL.format(slug=slug), timeout=request_timeout(timeout))
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


def reset_run_caches():
    """进程内重跑不得把上一轮解析结果/认证会话当成本轮新取数。"""
    _EARN_CACHE.clear()
    _SA_EARN_CACHE.clear()
    _YAHOO_EARN_CACHE.clear()
    sessions = (getattr(_EARN_TLS, "s", None), _SA_EARN_SESSION.get("sess"),
                _YAHOO_CRUMB.get("session"))
    for session in sessions:
        if session is not None:
            session.close()
    _EARN_TLS.s = None
    _SA_EARN_SESSION.update(sess=None, t=0.0)
    _YAHOO_CRUMB.update(crumb=None, session=None, t=0.0)
    SA_EARN_GATE.reset(False)
    YAHOO_GATE.reset(False)
    STOOQ_GATE.reset(False)
    SP500_RS_ROWS.clear()
    with _SRC_DAYS_LOCK:
        _SRC_DAYS.clear()


def _yahoo_crumb_session():
    """Yahoo quoteSummary 需要 cookie + crumb；整轮复用一份，30 分钟后重取。拿不到返回 (None, None)。"""
    with _YAHOO_CRUMB_LOCK:
        now = time.monotonic()
        if _YAHOO_CRUMB["crumb"] and now - _YAHOO_CRUMB["t"] < 1800:
            return _YAHOO_CRUMB["session"], _YAHOO_CRUMB["crumb"]
        sess = requests.Session()
        sess.headers.update({**UA, "Accept": "*/*"})
        sess.get("https://fc.yahoo.com/", timeout=request_timeout(8))  # 只为拿 cookie，返回 404 也正常
        r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=request_timeout(8))
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
        r = sess.get(url, params={"modules": "calendarEvents", "crumb": crumb}, timeout=request_timeout(timeout))
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
    remaining = request_remaining()
    if remaining <= 0:
        return {"status": "error", "reason": "budget"} if trusted else primary
    try:
        if remaining == float("inf"):
            official = earnings_sources.fetch_official_earnings(sym, today)
        else:
            clock = getattr(_REQUEST_TLS, "clock", None) or time.monotonic
            deadline = min(d for d in (RUN_DEADLINE, getattr(_REQUEST_TLS, "deadline", None)) if d is not None)
            official = earnings_sources.fetch_official_earnings(sym, today, deadline=deadline, clock=clock)
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


HARD_REFRESH_JS = monitor_render.HARD_REFRESH_JS


STK_COLS = monitor_render.STK_COLS


STK_HEAD = monitor_render.STK_HEAD


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


def attach_earnings(items, prev=None, today=None, prev_events=None, deadline=None, clock=None):
    """全部非 ETF 个股都查财报日期，但靠跨次缓存按远近分档刷新，大部分运行只需少量请求。
    抓到的日期全部挂到页面上；财报在 EARN_SOON_DAYS 天内的文字标黄（由 earnings_soon 决定）。
    ETF、指数、期货和参考值不适用。返回统计，其中 data 是下一轮复用的缓存。"""
    today = today or _earn_today()
    prev = prev if isinstance(prev, dict) else {}
    clock = clock or time.monotonic
    deadline = min(clock() + EARN_BUDGET_SECONDS, deadline if deadline is not None else float("inf"))

    def resolve_one(k):
        with request_budget(deadline, clock):
            if request_remaining() <= 0:
                return None
            result = resolve_earnings(k, today, prev.get(k), verified.get(k))
            if request_remaining() <= 0 and result.get("status") == "error":
                return None
            return result

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
                got = list(ex.map(resolve_one, queue))
            for k, e in zip(queue, got):
                if e is None:
                    stat["deferred"] += 1
                    stat["stopped"] = "budget"
                    if isinstance(prev.get(k), dict):
                        resolved[k] = prev[k]
                    continue
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
            if isinstance(data.get(key), bool):
                return False
            seq = data.get(key) or []
            if seq and len(seq) != len(dates):
                return False
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or
                   (v < 0 if key == "volumes" else v <= 0) for v in seq):
                return False
        for low, close, high in zip(data.get("lows") or [], data["closes"], data.get("highs") or []):
            if low > close * 1.001 or close > high * 1.001:
                return False
        for key in ("price", "prev_close"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
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


RSI_PERIODS = monitor_indicators.RSI_PERIODS


calc_rsi = monitor_indicators.calc_rsi


sma = monitor_indicators.sma


RS_DAYS = monitor_indicators.RS_DAYS


RS_NEUTRAL_PP = monitor_indicators.RS_NEUTRAL_PP


relative_strength = monitor_indicators.relative_strength


boll = monitor_indicators.boll


boll_streak = monitor_indicators.boll_streak


streak_tail = monitor_rules.streak_tail


global_dca = _rule_engine.global_dca


dca_text = monitor_rules.dca_text


dca_badge = _page_renderer.dca_badge


reduce_rule = _rule_engine.reduce_rule


cost_rule = _rule_engine.cost_rule


_level_num = monitor_rules._level_num


level_note = _page_renderer.level_note


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


CONSENSUS_GROUPS = monitor_rules.CONSENSUS_GROUPS
CONSENSUS_REFETCH_DAYS = 14           # 每只各自满这么多天重取（不再用统一的 1、15 号节拍）
CONSENSUS_NONE_RECHECK_DAYS = 30      # 页面上没有共识价（无覆盖）的，隔这么久再看一次
CONSENSUS_POST_EARN_LAG_DAYS = 2      # 财报后至少等 2 个美东自然日；不保证所有分析师已更新
CONSENSUS_MAX_PER_DAY = 12           # 北京自然日的独立台账，删除/关闭分组不返还额度
CONSENSUS_RETRY_PER_DAY = 3          # 失败重试最多占用 3 个名额，避免挤掉正常到期项
CONSENSUS_RETRY_BACKOFF = (1, 2, 4, 7)  # 连续失败：首次次日重试，此后逐步退避


AMP_GROUPS = monitor_rules.AMP_GROUPS  # 兼容旧导出，不参与振幅规则门控。


BOLL_UP_SIGNAL_GROUPS = monitor_rules.BOLL_UP_SIGNAL_GROUPS
# 单独上轨（RSI 零条）仅持仓提醒；有 RSI 的上轨组合仍按各分组规则处理。

_CONS_SPG = re.compile(r'Targets:\{source:"spg",currency:"([A-Z]+)",avg:([0-9.]+)')
_CONS_UPD = re.compile(r'targets:\{low:[0-9.]+,high:[0-9.]+,count:\d+,median:[0-9.]+,average:[0-9.]+,updated:"(\d{4}-\d{2}-\d{2})"')


_pos_num = monitor_rules._pos_num


_month_of = monitor_rules._month_of


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
        r = sess.get(CONSENSUS_ROBOTS, timeout=request_timeout(15))
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
        r = sess.get(CONSENSUS_URL.format(slug=consensus_slug(sym)), timeout=request_timeout(25))
    except BudgetExhausted:
        raise  # 尚未请求，不写入失败缓存或消费额度
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


def update_consensus(symbols, prev, today_iso, sess=None, sleep=None, clock=None,
                     force=False, next_earn=None, earnings_events=None, quota_day=None, deadline=None):
    """事件/TTL 用美东日，额度用独立北京日；force 仅强制到期，不绕过日额度或同日去重。"""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = min(clock() + CONSENSUS_BUDGET, deadline if deadline is not None else float("inf"))
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
    if request_remaining(deadline, clock) <= 0:
        stat["stopped"] = "budget"
        return data, stat
    sess = sess or requests.Session()
    sess.headers.update(CONSENSUS_UA)
    with request_budget(deadline, clock):
        robots_ok = consensus_robots_ok(sess)
    if not robots_ok:
        stat["stopped"] = "budget" if request_remaining(deadline, clock) <= 0 else "robots"
        log("  · 共识价：robots.txt 不允许或读取失败，本轮不取，沿用旧值")
        return data, stat
    log(f"  取共识价：{len(ranked)} 只到期，本轮最多取 {len(todo)} 只，每只间隔 {CONSENSUS_GAP:g} 秒…")
    streak = 0
    for i, s in enumerate(todo):
        if (request_remaining(deadline, clock) <= 0 or
                i and not budget_sleep(CONSENSUS_GAP, sleep, deadline, clock)):
            stat["stopped"] = "budget"
            break
        entry = data.get(s, {})
        try:
            with request_budget(deadline, clock):
                res = fetch_consensus_one(sess, s)
        except BudgetExhausted:
            stat["stopped"] = "budget"
            break
        except Exception as e:
            res = {"s": "fail", "why": str(e)[:60]}
        attempted.add(s)
        if entry.get("s") == "fail":
            retried.add(s)
        ledger.update(symbols=sorted(attempted), retry_symbols=sorted(retried))
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


manual_target = _rule_engine.manual_target


resolve_target = _rule_engine.resolve_target


_price_text = monitor_rules._price_text


target_label = monitor_rules.target_label


target_alerts = _rule_engine.target_alerts


evaluate_target = _rule_engine.evaluate_target


target_display_state = _rule_engine.target_display_state


_consensus_description = monitor_rules._consensus_description


_target_stock = _rule_engine._target_stock


_target_active = _rule_engine._target_active


_apply_target = _snapshot_builder._apply_target


target_overlay_members = _snapshot_builder.target_overlay_members


_target_overlay_union = _snapshot_builder._target_overlay_union


purple_counts = _snapshot_builder.purple_counts


target_tag = _page_renderer.target_tag


MA_CONFIRM_LOOKBACK = monitor_indicators.MA_CONFIRM_LOOKBACK


MA_CONFIRM_DEFAULT = monitor_indicators.MA_CONFIRM_DEFAULT
MA_CONFIRM_MAX = monitor_indicators.MA_CONFIRM_MAX
MA_CONFIRM_MIN = monitor_indicators.MA_CONFIRM_MIN


MA_LONG_BAND_PCT = monitor_indicators.MA_LONG_BAND_PCT


ma_confirm_pct = _indicator_calculator.ma_confirm_pct


ma_confirmed_cross = monitor_indicators.ma_confirmed_cross


VOL_LOOKBACK = monitor_indicators.VOL_LOOKBACK


VOL_MIN_RETURNS = monitor_indicators.VOL_MIN_RETURNS


VOL_LOW_SIGMA = monitor_indicators.VOL_LOW_SIGMA


VOL_HIGH_SIGMA = monitor_indicators.VOL_HIGH_SIGMA
VOL_MULT = {"low": 0.6, "mid": 1.0, "high": 1.4}


vol_profile = _indicator_calculator.vol_profile


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


SIGNAL_KINDS = monitor_snapshot.SIGNAL_KINDS


VOL_RATIO_BASE = monitor_indicators.VOL_RATIO_BASE


VOL_Z_WINDOW = monitor_indicators.VOL_Z_WINDOW


_latest_completed_session_index = _indicator_calculator._latest_completed_session_index


completed_day_volume_ratio = _indicator_calculator.completed_day_volume_ratio


_volume_ratio_zscore = monitor_indicators._volume_ratio_zscore


completed_day_volume_z = _indicator_calculator.completed_day_volume_z


completed_day_volume_distribution = _indicator_calculator.completed_day_volume_distribution


INTRADAY_VOL_QUIET_MIN = monitor_indicators.INTRADAY_VOL_QUIET_MIN


INTRADAY_VOL_BANDS = monitor_indicators.INTRADAY_VOL_BANDS


FULL_SESSION_MINUTES = monitor_indicators.FULL_SESSION_MINUTES


intraday_volume_thresholds = monitor_indicators.intraday_volume_thresholds


intraday_volume_ratio_detail = _indicator_calculator.intraday_volume_ratio_detail


intraday_volume_ratio = _indicator_calculator.intraday_volume_ratio


_range52_from_history = _indicator_calculator._range52_from_history


_analysis_context = _rule_engine._analysis_context


_apply_vol_tier = _rule_engine._apply_vol_tier


_rule_chg_red = monitor_rules._rule_chg_red


_rule_chg_yellow = monitor_rules._rule_chg_yellow


classify_technical_facts = _rule_engine.classify_technical_facts


_rule_rsi = _rule_engine._rule_rsi


_fresh_52w_low = _rule_engine._fresh_52w_low


_rule_52w_low = _rule_engine._rule_52w_low


_rule_add_price = _rule_engine._rule_add_price


_rule_reduce_price = _rule_engine._rule_reduce_price


_rule_target = _rule_engine._rule_target


_intraday_extremes = monitor_rules._intraday_extremes


_touch_note = monitor_rules._touch_note


_BOLL_SIGMA_FACTOR = monitor_rules._BOLL_SIGMA_FACTOR


_boll_near_pct = _rule_engine._boll_near_pct


_boll_hits = _rule_engine._boll_hits


_rule_boll = _rule_engine._rule_boll


_vol_ratio_z_enabled = _rule_engine._vol_ratio_z_enabled


_rule_vol_ratio = _rule_engine._rule_vol_ratio


_rule_amplitude = monitor_rules._rule_amplitude


_ma_day_range = monitor_rules._ma_day_range


_rule_moving_averages = _rule_engine._rule_moving_averages


_analysis_detail = _rule_engine._analysis_detail


analyze_symbol = _rule_engine.analyze_symbol


fmt = monitor_render.fmt


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
            return f'{finished[:19].replace("T", " ")}（盘中，北京 · 未取得行情） · 无数据 {missing} 只'
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
                return finished[:19].replace("T", " ") + "（盘中，北京）" + detail
            return finished[:19].replace("T", " ") + f"（盘中，北京 · 仍为 {date_text} 日线）" + detail
    return date_text + " 收盘（美东交易日）"


signal_counts = _snapshot_builder.signal_counts


_TEXT_RSI_RE = monitor_snapshot._TEXT_RSI_RE


_TEXT_BOLL_PREFIX = monitor_snapshot._TEXT_BOLL_PREFIX


_text_only_kind = _snapshot_builder._text_only_kind


_text_only_shape = _snapshot_builder._text_only_shape


text_only_members = _snapshot_builder.text_only_members


_ma_display_text = _snapshot_builder._ma_display_text


# 统计侧均线措辞唯一出口：穿越倒装与分框标题一致，同一指标内部用「，」。
_ma_stat_label = _snapshot_builder._ma_stat_label


_ma_stat_text = _snapshot_builder._ma_stat_text


_member_direction_markers = _snapshot_builder._member_direction_markers


_member_metric = monitor_snapshot._member_metric


_BLOCK_PANEL_KINDS = monitor_render._BLOCK_PANEL_KINDS


_NO_ROW_METRIC_KINDS = monitor_render._NO_ROW_METRIC_KINDS


_ARROW_BAND_DOWN = monitor_rules._ARROW_BAND_DOWN
_ARROW_BAND_UP = monitor_rules._ARROW_BAND_UP


_ARROW_RSI_DOWN = monitor_rules._ARROW_RSI_DOWN
_ARROW_RSI_UP = monitor_rules._ARROW_RSI_UP

# 统计面板里技术三栏的展示顺序：组合条件与单独条件统一按
# 「布林+RSI → RSI → 布林」排列（与规则表格一致），组合不再排在单独后面。
_KIND_PANEL_ORDER = {kind: i * 2 for i, (kind, _) in enumerate(SIGNAL_KINDS)}
_KIND_PANEL_ORDER["boll_rsi"] = _KIND_PANEL_ORDER["rsi"] - 1
_KIND_PANEL_ORDER["boll"] = _KIND_PANEL_ORDER["boll_rsi"] + 2


boll_side_text = _rule_engine.boll_side_text


rsi_hits_text = _rule_engine.rsi_hits_text


_parse_boll_rsi_direction = monitor_rules._parse_boll_rsi_direction


boll_rsi_text = _rule_engine.boll_rsi_text


_ARROW_CLASSES = monitor_render._ARROW_CLASSES


_grp_arrow = _page_renderer._grp_arrow


# 框标题排序：▲ 与 ↑ 同档（排在前面），▼ 与 ↓ 同档，其余垫底。
_SHAPE_RANK = {"▲": 0, _ARROW_RSI_UP: 0, "▼": 1, _ARROW_RSI_DOWN: 1}


_shape_rank = _page_renderer._shape_rank


_block_head = monitor_render._block_head


_block_title_html = _page_renderer._block_title_html


arrow_html = _page_renderer.arrow_html


_member_block = _page_renderer._member_block


_member_belong = _page_renderer._member_belong


_member_metric_text = _page_renderer._member_metric_text


_member_signal_details = _snapshot_builder._member_signal_details


_merge_ma_members = _snapshot_builder._merge_ma_members


signal_count_members = _snapshot_builder.signal_count_members


earnings_week_members = _snapshot_builder.earnings_week_members


_RETIRED_SNAPSHOT_KEYS = monitor_snapshot._RETIRED_SNAPSHOT_KEYS


_sanitize_snapshot_fields = _snapshot_builder._sanitize_snapshot_fields


_merge_ma_snapshot = _snapshot_builder._merge_ma_snapshot


_snapshot_ma_member = _snapshot_builder._snapshot_ma_member


_sanitize_snapshot = _snapshot_builder._sanitize_snapshot


build_snapshot = _snapshot_builder.build_snapshot


_macro_rows = _page_renderer._macro_rows


_LEVEL_ORDER = {"red": 0, "yellow": 1, "green": 2, "gray": 3}


_sort_key = _page_renderer._sort_key


_dedupe_items = _page_renderer._dedupe_items


_row_tags = _page_renderer._row_tags


_earnings_html = _page_renderer._earnings_html


_signal_text_html = _page_renderer._signal_text_html


_SIGNAL_DESCRIPTION_RULES = monitor_render._SIGNAL_DESCRIPTION_RULES
_SIGNAL_DESCRIPTION_LABELS = dict(SIGNAL_KINDS, target="目标价")


_description_signals = _page_renderer._description_signals


_signal_description_groups = _page_renderer._signal_description_groups


_signal_lamps = _page_renderer._signal_lamps


_signals_html = _page_renderer._signals_html


_range52_row = _page_renderer._range52_row


_stock_anchor_id = _page_renderer._stock_anchor_id


_quiet_keep = _page_renderer._quiet_keep


_stock_row = _page_renderer._stock_row


_stock_rows = _page_renderer._stock_rows


_count_badge = monitor_render._count_badge


_group_stats_html = _page_renderer._group_stats_html


_sort_group_items = _page_renderer._sort_group_items


_display_groups = _page_renderer._display_groups


_collapse_button = monitor_render._collapse_button


POSITION_ZONES = monitor_render.POSITION_ZONES


_position_zone = monitor_render._position_zone


_group_body = _page_renderer._group_body


_position_long_zone_card = _page_renderer._position_long_zone_card


_group_card = _page_renderer._group_card


_run_light = monitor_render._run_light


_member_icons = _page_renderer._member_icons


_filter_detail = _page_renderer._filter_detail


_earnings_week_panel = _page_renderer._earnings_week_panel


_signal_counts_card = _page_renderer._signal_counts_card


_GRAY_RSI_RE = monitor_render._GRAY_RSI_RE


_gray_by_kind = monitor_render._gray_by_kind


_gray_title = _page_renderer._gray_title


_gray_cell = _page_renderer._gray_cell


_target_purple_panel = _page_renderer._target_purple_panel


_policy_details = _page_renderer._policy_details


_oil_reference_row = _page_renderer._oil_reference_row


_page_notes = _page_renderer._page_notes


_position_symbols = _page_renderer._position_symbols


_cost_seed_json = _page_renderer._cost_seed_json


_cost_panel_assets = _page_renderer._cost_panel_assets


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


render = _page_renderer.render


QUOTE_EXHAUSTED = object()  # 总预算用完、这只根本没来得及尝试（与"尝试了但失败"区分）


def _fetch_quote(symbol):
    """工作线程里抓一只：返回行情 dict / None（失败）/ QUOTE_EXHAUSTED（总预算已用完）。"""
    if request_remaining(QUOTE_DEADLINE) <= 0:
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
        remaining = request_remaining(QUOTE_DEADLINE)
        if not failed or remaining < 60:
            break
        log(f"补抓 {len(failed)} 只第一轮失败的标的…")
        # 指数/期货只有 Yahoo 一个源，熔断冷却期内会被跳过；预算充足就等冷却结束再补抓，
        # 否则补抓轮还是撞同一堵墙。等待时间受总预算约束。
        wait = max(0.0, max(YAHOO_GATE.blocked_until, STOOQ_GATE.blocked_until) - time.monotonic())
        if wait and remaining > wait + 45:
            log(f"  等待行情源限流冷却约 {wait:.0f} 秒后再补抓…")
            delay = wait + 1
        else:
            delay = 3
        if not budget_sleep(delay, deadline=QUOTE_DEADLINE):
            break
        YAHOO_GATE.reset(True)
        STOOQ_GATE.reset(True)
        run(failed, max(1, QUOTE_WORKERS // 2), keep_better=True)
    done = sum(isinstance(v, dict) for v in results.values())
    log(f"行情抓取完成：取到 {done}/{len(symbols)}（是不是目标交易日的日线，看下面「当日日线」那行）")
    return results


def build_reference_alerts(prev=None):
    """额外指标独立取数入口；任何失败只影响参考提示，不拖垮原行情。"""
    prev = prev if isinstance(prev, dict) else {}
    if request_remaining() <= 0:
        return {**prev, "deferred": "budget"}
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

_HTTP_REDACTED = "[REDACTED]"
_HTTP_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:api[_-]?key|(?:access[_-]?|refresh[_-]?|auth[_-]?)?token|"
    r"client[_-]?secret|secret|password|authorization|signature|key)\b[\"']?\s*[:=]\s*[\"']?"
    r"(?:(?:Bearer|Basic)\s+)?)"
    r"([^\s&<>\"',;}]+)")


def _http_sensitive(name):
    name = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return (name in {"key", "auth", "authorization", "cookie", "setcookie", "signature", "credential"}
            or name.endswith(("apikey", "token", "secret", "password")))


def _http_cookie_secrets(value):
    if hasattr(value, "get_dict"):
        value = value.get_dict()
    if isinstance(value, dict):
        return {item for item in value.values() if isinstance(item, str) and item}
    if not isinstance(value, str):
        return set()
    attributes = {"path", "domain", "expires", "max-age", "samesite"}
    return {match[2].strip().strip('"') for match in
            re.finditer(r'(?:^|[;,]\s*)([^=;,\s]+)=("[^"]*"|[^;,]*)', value)
            if match[1].lower() not in attributes and match[2].strip().strip('"')}


def _http_secrets(value):
    secrets = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _http_sensitive(key) and isinstance(item, str) and item and item != _HTTP_REDACTED:
                secrets.add(item)
                if str(key).lower() == "authorization" and " " in item:
                    scheme, credential = item.split(" ", 1)
                    secrets.add(credential)
                    if scheme.lower() == "basic":
                        try:
                            secrets.update(part for part in base64.b64decode(credential, validate=True)
                                           .decode("latin-1").split(":", 1) if part)
                        except (ValueError, UnicodeError):
                            pass
                if str(key).lower() in {"cookie", "set-cookie"}:
                    secrets.update(_http_cookie_secrets(item))
            secrets.update(_http_secrets(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            secrets.update(_http_secrets(item))
    return secrets


def _http_redact(value, secrets=()):
    if isinstance(value, dict):
        secrets = set(secrets) | _http_secrets(value)
        return {_http_redact(key, secrets): _HTTP_REDACTED if _http_sensitive(key) else _http_redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_http_redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and secret != _HTTP_REDACTED:
            for encoded in {secret, quote(secret, safe=""), quote_plus(secret),
                            html_lib.escape(secret), json.dumps(secret)[1:-1]}:
                value = value.replace(encoded, _HTTP_REDACTED)
    return _HTTP_SECRET_FIELD.sub(lambda match: match[1] + _HTTP_REDACTED, value)


def _http_snapshot_key(url, secrets=()):
    parts = urlsplit(str(url))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    secrets = set(secrets) | {value for name, value in pairs if _http_sensitive(name) and value}
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        secrets.update(unquote(value) for value in userinfo.split(":") if value)
        netloc = quote(_HTTP_REDACTED, safe="") + "@" + netloc
    query = urlencode(sorted(((name, _HTTP_REDACTED if _http_sensitive(name) else _http_redact(value, secrets))
                              for name, value in pairs), key=lambda pair: pair[0]))
    key = "GET " + urlunsplit((parts.scheme, netloc, _http_redact(parts.path, secrets), query, ""))
    return key, secrets


def _http_snapshot_hit(hit, secrets=()):
    out = {key: value for key, value in hit.items() if key in {"status", "text", "encoding", "headers"}}
    if isinstance(out.get("headers"), dict):
        secrets = set(secrets) | _http_secrets(out["headers"])
        out["headers"] = {key: value for key, value in out["headers"].items() if key.lower() == "content-type"}
    text = out.get("text")
    if isinstance(text, str):
        try:
            body = json.loads(text)
        except ValueError:
            secrets = set(secrets) | {unquote(match[2]) for match in _HTTP_SECRET_FIELD.finditer(text)}
            out["text"] = _http_redact(text, secrets)
        else:
            secrets = set(secrets) | _http_secrets(body)
            out["text"] = json.dumps(_http_redact(body, secrets), ensure_ascii=False)
    return _http_redact(out, secrets)


def _http_snapshot_data(data):
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key, hit in data.items():
        if not isinstance(key, str) or not key.startswith("GET ") or not isinstance(hit, dict):
            continue
        safe_key, secrets = _http_snapshot_key(key[4:])
        clean[safe_key] = _http_snapshot_hit(hit, secrets)
    return clean


class _ReplayResponse(requests.Response):
    """内存响应复用 requests 的上下文、分块读取与 close 语义。"""

    def __init__(self, status, text, headers=None, encoding="utf-8"):
        super().__init__()
        self.status_code = status
        self.encoding = encoding or "utf-8"
        self._content = (text or "").encode(self.encoding)
        self._content_consumed = True
        self.headers.update(headers or {})


def install_http_snapshot(mode=None, path=None):
    """记录脱敏 GET；回放 GET 未命中返回 599，非 GET 抛连接错误且绝不外发。"""
    state = {"mode": mode, "path": path, "data": {}, "miss": 0, "blocked": 0}
    if mode == "replay" and path:
        try:
            with open(path, encoding="utf-8") as f:
                state["data"] = _http_snapshot_data(json.load(f))
        except Exception as exc:
            log(f"HTTP 快照读取失败，本次全部按取数失败处理：{type(exc).__name__}")
    previous = getattr(requests.Session.request, "__dict__", {}).get("_http_snapshot_state")
    if previous:
        restore_http_snapshot(previous)
    original = requests.Session.request
    original_send = requests.Session.send
    if mode not in {"dump", "replay"}:
        return state

    def lookup(url, secrets=(), legacy_url=None):
        key, secrets = _http_snapshot_key(url, secrets)
        hit = state["data"].get(key)
        if hit is None and legacy_url:
            legacy = state["data"].get(_http_snapshot_key(legacy_url, secrets)[0])
            if legacy is not None and "headers" not in legacy and "encoding" not in legacy:
                hit = legacy
        if hit is None:
            state["miss"] += 1
            return _ReplayResponse(599, "")
        hit = _http_snapshot_hit(hit, secrets)
        response = _ReplayResponse(hit.get("status", 200), hit.get("text", ""),
                                   hit.get("headers"), hit.get("encoding"))
        response.url = url
        return response

    def record(url, response, secrets):
        if state["mode"] != "dump" or getattr(response, "status_code", 0) != 200:
            return
        try:
            secrets |= _http_secrets(dict(response.headers))
            # 响应中披露的敏感字段也可能回显到请求 URL。
            hit = {"status": response.status_code, "text": response.text,
                   "encoding": response.encoding or "utf-8", "headers": dict(response.headers)}
            try:
                secrets |= _http_secrets(json.loads(response.text))
            except ValueError:
                secrets |= {unquote(match[2]) for match in _HTTP_SECRET_FIELD.finditer(response.text)}
            key, secrets = _http_snapshot_key(url, secrets)
            state["data"][key] = _http_snapshot_hit(hit, secrets)
        except Exception as exc:
            log(f"HTTP 快照记录失败，已跳过该响应：{type(exc).__name__}")

    def blocked():
        state["blocked"] += 1
        raise requests.ConnectionError("HTTP replay 模式禁止非 GET 请求，未发送")

    def patched_send(self, request, **kw):
        secrets = _http_secrets(dict(request.headers)) | _http_cookie_secrets(self.cookies)
        if state["mode"] == "replay":
            if str(request.method).upper() != "GET":
                blocked()
            response = lookup(request.url, secrets)
            response.request = request
            return response
        response = original_send(self, request, **kw)
        if str(request.method).upper() == "GET":
            record(request.url, response, secrets)
        return response

    def patched(self, method, url, *a, **kw):
        if str(method).upper() != "GET":
            if state["mode"] == "replay":
                state["blocked"] += 1
                raise requests.ConnectionError("HTTP replay 模式禁止非 GET 请求，未发送")
            return original(self, method, url, *a, **kw)
        params = kw.get("params", a[0] if a else None)
        params = requests.sessions.merge_setting(params, self.params)
        prepared = requests.PreparedRequest()
        prepared.prepare_url(url, params)
        headers = kw.get("headers", a[2] if len(a) > 2 else {})
        secrets = _http_secrets(dict(self.headers)) | _http_secrets(headers)
        cookies = kw.get("cookies", a[3] if len(a) > 3 else None)
        secrets |= _http_cookie_secrets(self.cookies) | _http_cookie_secrets(cookies)
        auth = kw.get("auth", a[5] if len(a) > 5 else self.auth)
        if isinstance(auth, tuple):
            auth = requests.auth.HTTPBasicAuth(*auth)
        if isinstance(auth, requests.auth.HTTPBasicAuth):
            secrets.update(value for value in (auth.username, auth.password) if isinstance(value, str) and value)
            prepared.headers = {}
            auth(prepared)
            secrets |= _http_secrets(dict(prepared.headers))
        if state["mode"] == "replay":
            return lookup(prepared.url, secrets, url if params else None)
        resp = original(self, method, url, *a, **kw)
        record(prepared.url, resp, secrets)
        return resp

    patched._http_snapshot_original = original
    patched._http_snapshot_mode = mode
    patched._http_snapshot_state = state
    state.update(original_request=original, original_send=original_send,
                 patched_request=patched, patched_send=patched_send)
    requests.Session.request = patched
    requests.Session.send = patched_send
    return state


def restore_http_snapshot(state):
    """仅恢复自己安装的包装，不覆盖调用方后续的 mock 或包装。"""
    if not isinstance(state, dict):
        return
    if requests.Session.request is state.get("patched_request"):
        requests.Session.request = state["original_request"]
    if requests.Session.send is state.get("patched_send"):
        requests.Session.send = state["original_send"]


def dump_http_snapshot(state):
    if not isinstance(state, dict) or state.get("mode") != "dump" or not state.get("path"):
        return
    try:
        state["data"] = _http_snapshot_data(state["data"])
        atomic_write_json(state["path"], state["data"])
        log(f"HTTP 快照已写入 {state['path']}（{len(state['data'])} 条请求）")
    except Exception as exc:
        log(f"HTTP 快照写入失败：{type(exc).__name__}")


def main(argv=None):
    global QUOTE_DEADLINE, RUN_DEADLINE
    runtime = RuntimeState(BASE, target=TARGET_DATE, event=EVENT)
    try:
        with bounded_http_requests():
            snapshots = []
            try:
                return _main(argv, runtime, snapshots)
            finally:
                for state in reversed(snapshots):
                    try:
                        dump_http_snapshot(state)
                    finally:
                        restore_http_snapshot(state)
    except Exception as exc:
        runtime.receipt("failed", f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    finally:
        QUOTE_DEADLINE = RUN_DEADLINE = None
        YAHOO_GATE.reset(False)
        STOOQ_GATE.reset(False)


def _main(argv, runtime, snapshots=None):
    global TARGET_DATE, CLOSED_ONLY, QUOTE_DEADLINE, RUN_DEADLINE
    parser = argparse.ArgumentParser(description="市场自检生成器")
    parser.add_argument("--prepare-run", action="store_true", help="锁定本次运行目标交易日并输出 GitHub Actions 环境变量")
    parser.add_argument("--ensure-schedule-run", action="store_true", help="定时任务保险：缺失时补派发一次运行")
    parser.add_argument("--dump-http", metavar="PATH", help="记录本次成功的 GET 响应，供 --replay-http 离线重跑")
    parser.add_argument("--replay-http", metavar="PATH", help="只读 HTTP 快照渲染，全程零网络（未命中的按取数失败）")
    args = parser.parse_args(argv)
    if args.dump_http and args.replay_http:
        parser.error("--dump-http 与 --replay-http 不能同时使用")
    if args.dump_http or args.replay_http:
        snap_mode = "dump" if args.dump_http else "replay"
        snap_path = args.dump_http or args.replay_http
        snap_state = install_http_snapshot(snap_mode, snap_path)
        if snapshots is not None:
            snapshots.append(snap_state)
        log(f"HTTP 快照模式：{snap_mode}（{snap_path}）")
    if args.prepare_run:
        prepare_run()
        return 0
    if args.ensure_schedule_run:
        return ensure_schedule_run(runtime)

    if not TARGET_DATE:
        plan = plan_run(EVENT, datetime.now(timezone.utc))
        TARGET_DATE, CLOSED_ONLY = plan["target"], plan["closed_only"]
    runtime.target = TARGET_DATE
    RUN_DEADLINE = time.monotonic() + RUN_BUDGET_SECONDS - FINALIZE_RESERVE_SECONDS
    reset_run_caches()

    cfg_path = os.path.join(BASE, "holdings.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        log(f"holdings.json 读取失败，按空清单处理：{e}")
        cfg = {}

    universe, group_counts, dup_hidden = grouped_universe(cfg)
    # 一轮只读一个快照；进程内数据和会话已在入口重置。
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
        with request_budget(time.monotonic() + MACRO_BUDGET_SECONDS):
            macro = build_macro(prev_status, nfci_state)
    finally:
        if nfci_state:
            nfci_state = runtime.checkpoint("nfci_cache", nfci_state)
    with request_budget(time.monotonic() + REFERENCE_BUDGET_SECONDS):
        reference_alerts = build_reference_alerts(prev_status.get("reference_alerts"))
    reference_alerts = runtime.checkpoint("reference_alerts", reference_alerts)
    items = []
    if universe:
        log("抓取 " + " + ".join(f"{group_counts[g['key']]} 只{g['label']}" for g in GROUPS
                                 if group_counts[g['key']])
            + (f"（另有 {dup_hidden} 只重复，已跳过）" if dup_hidden else "") + "...")
        QUOTE_DEADLINE = min(RUN_DEADLINE, time.monotonic() + QUOTE_BUDGET_SECONDS)
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
    if getattr(requests.Session.request, "_http_snapshot_mode", None) == "replay":
        log("HTTP replay 模式跳过微信推送，未发送")
        return
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
