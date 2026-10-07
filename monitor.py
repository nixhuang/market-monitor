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
import os
import re
import subprocess
import sys
import threading
import time
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

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# FRED 官方 API key（免费申请：https://fredaccount.stlouisfed.org/apikeys）
# 配成仓库 Secret FRED_API_KEY 即可；不配也能跑，只是 Actions 上拿不到跑路价签和收益率曲线。
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()

# ---------------------------------------------------------------- 配置

FRED_SERIES = {
    "hy_oas":   {"id": "BAMLH0A0HYM2", "name": "跑路价签",   "unit": "bp",  "scale": 100},
    "vix":      {"id": "VIXCLS",       "name": "VIX",        "unit": "",    "scale": 1},
    "sp500":    {"id": "SP500",        "name": "标普500",     "unit": "",    "scale": 1},
    "ust10":    {"id": "DGS10",        "name": "10Y美债",     "unit": "%",   "scale": 1},
    "curve":    {"id": "T10Y2Y",       "name": "收益率曲线",   "unit": "",    "scale": 1},
    "dxy":      {"id": "DTWEXBGS",     "name": "美元指数",     "unit": "",    "scale": 1},
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
    "rsi_high": 70,         # RSI >= x → 红（超买）
    "rsi_low": 30,          # RSI <= x → 红（超卖）
    "near_52w_low_pct": 1.0,  # 距 52 周低点不足 x% → 红
    "trigger_gap_pct": 5.0,  # 距加仓价不足 x% → 红（已跌破则无视这条直接红）
    "vol_ratio": 1.5,       # 量比 >= x 倍 → 黄
    "quiet_chg": 2.0,       # quiet 标的（货币基金等）单日涨跌 >= x% → 黄
    "amp_yellow": 5.0,      # 日内振幅（最高-最低）/昨收 >= x% → 黄
    "amp_red": 8.0,         # 日内振幅 >= x% → 红（上下插针、剧烈震荡）

    # 宏观
    "hy_green": 350,        # 跑路价签 bp：< 350 绿 / 350~ 黄 / >= 400 红
    "hy_yellow": 400,
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
    """schedule锚定最近一次美东20:30；手动/保存重跑保留盘中行为。"""
    ny = when.astimezone(US_TZ)
    day = ny.date()
    if event == "schedule":
        anchor = ny.replace(hour=20, minute=30, second=0, microsecond=0)
        if ny < anchor:
            day -= timedelta(days=1)
        return {"closed_only": True, "target": session_date(day)}
    # 当前接口口径仍是常规时段日线：09:30–16:00可用实时覆盖。
    in_regular_session = ny.weekday() < 5 and (9, 30) <= (ny.hour, ny.minute) < (16, 0)
    if ny.weekday() < 5 and (ny.hour, ny.minute) >= (9, 30):
        day_target = session_date(day)
    else:
        day_target = session_date(day - timedelta(days=1))
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
                    s[k] = v.strip()
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue  # 类型不对就跳过，不让它污染默认值
            s[k] = type(DEFAULT_SETTINGS[k])(v)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"  ! settings.json 读取失败，用默认值：{e}")
    return s


S = load_settings()

# 布林带"逼近"阈值（%）：距上轨/下轨不足这个百分比就算命中，不用等真的穿过去
# 因为盘中价格一直在动，等收盘才确认会错过时机
BOLL_NEAR_PCT = S["boll_near_pct"]

# 宏观阈值（跑路价签单位为 bp）
THRESH = {
    "hy_oas":  {"green": S["hy_green"], "yellow": S["hy_yellow"], "red": S["hy_red"]},
    "vix":     {"green": S["vix_green"], "yellow": S["vix_yellow"], "red": S["vix_red"]},
}


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


def _rows_from_closes(closes, days=400):
    """把收盘价序列伪造成 (date, value) 列表。日期只用于内部排序，页面不展示。"""
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        d = (NOW.date() - timedelta(days=(n - 1 - i))).isoformat()
        out.append((d, c))
    return out[-days:]


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


def fred_series(series_id, days=400, alias=None):
    """抓取 FRED 序列，返回 [(date_str, value), ...]，失败返回 []

    三级降级：
      1. 官方 API（有 FRED_API_KEY 时）—— Actions 上唯一能通的路
      2. CSV 图形接口（免 key）—— 大陆本地能通，Actions 上不通
      3. Yahoo 指数别名兜底（仅 vix/sp500/ust10/dxy 有）
    """
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
            return _rows_from_closes(data["closes"], days)
    return []


def pct_from_high(series):
    """当前值相对区间最高点的回撤百分比（负数表示回撤）"""
    if not series:
        return None
    values = [v for _, v in series]
    cur, high = values[-1], max(values)
    return (cur / high - 1) * 100 if high else None


def build_macro():
    log("抓取宏观指标...")
    macro = {}
    for key, cfg in FRED_SERIES.items():
        rows = fred_series(cfg["id"], alias=FRED_YAHOO_ALIAS.get(key))
        if not rows:
            macro[key] = {"ok": False, "name": cfg["name"]}
            continue
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
        if key == "sp500":
            dd = pct_from_high(rows)
            item["drawdown"] = dd
        macro[key] = item
        log(f"  {cfg['name']}: {cur:.2f} ({rows[-1][0]})")
    return macro


def macro_status(key, item, drawdown=None):
    """返回 (等级, 文案)  等级: green/yellow/red/gray"""
    if not item.get("ok"):
        return "gray", "无数据"

    if key == "hy_oas":
        v = item["value"]
        dw = item.get("delta_week")
        if dw is not None and dw >= 50:
            return "red", "周变化 +%.0f ⚠" % dw
        if v >= 500:
            return "red", "熊市中段"
        if v >= S["hy_red"]:
            return "red", "危机确认"
        if v >= S["hy_green"]:
            return "yellow", "收紧"
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

    if key == "curve":
        v = item["value"]
        if v < 0:
            return "yellow", "倒挂"
        return "green", "正常"

    return "gray", "—"


# ---------------------------------------------------------------- Yahoo

def yahoo_history(symbol):
    """返回 dict: closes(list), volumes(list), highs, lows；失败返回 None
    多端点轮换 + 429 退避重试，尽量扛住限流"""
    endpoints = [
        "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=1y&interval=1d",
        "https://query2.finance.yahoo.com/v8/finance/chart/{s}?range=1y&interval=1d",
    ]
    last_err = None
    for attempt in range(2):
        for ep in endpoints:
            url = ep.format(s=symbol)
            try:
                r = requests.get(url, headers=UA, timeout=20)
                if r.status_code == 429:
                    last_err = "429 限流"
                    time.sleep(3 * (attempt + 1))
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
    code = symbol.lower().replace(".", "-") + ".us"
    url = f"https://stooq.com/q/d/l/?s={code}&i=d"
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        lines = [l for l in r.text.strip().split("\n") if l]
        if len(lines) < 40 or lines[0].startswith("Date") is False:
            return None
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
                    headers=hdr, timeout=12)
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
    rt = nasdaq_realtime(symbol)
    if not rt:
        return data
    px = rt["price"]
    dates = data.get("dates") or []
    now_ny = datetime.now(US_TZ)
    today = now_ny.strftime("%Y-%m-%d")
    # 常规日线实时层不能把夜盘冒充完整23小时bar。
    if not ((9, 30) <= (now_ny.hour, now_ny.minute) < (16, 0)):
        return data
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
                r = requests.get(url, headers=UA, timeout=25)
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


def normalize_symbol(symbol):
    """把富途 moomoo 导出的「代码-市场」写法转成行情源认的格式

    UNH-US → UNH   00700-HK → 00700.HK   600519-SH → 600519.SS
    已经是 Yahoo 写法的原样返回。
    """
    s = (symbol or "").strip().upper()
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
                res = {
                    "period": ((d.get("incomeStatementTable") or {}).get("headers") or {}).get("value2"),
                    "inc": inc, "bs": table("balanceSheetTable"),
                    "cf": table("cashFlowTable"), "rt": table("financialRatiosTable"),
                }
    except Exception as e:
        log(f"  · {key} 财报读取失败：{e}")
    _FUND_CACHE[key] = res
    return res


def _fcell(tbl, label, col="value2"):
    return _fund_num((tbl or {}).get(label, {}).get(col)) if tbl else None


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
    NOW, YR = "value2", "value5"  # 4 期窗口：value2 最新季、value5 去年同期

    rev0, rev4 = _fcell(inc, "Total Revenue", NOW), _fcell(inc, "Total Revenue", YR)
    gm0, gm4 = _fcell(rt, "Gross Margin", NOW), _fcell(rt, "Gross Margin", YR)
    om0, om4 = _fcell(rt, "Operating Margin", NOW), _fcell(rt, "Operating Margin", YR)
    roe0, roe4 = _fcell(rt, "After Tax ROE", NOW), _fcell(rt, "After Tax ROE", YR)
    cr0, cr4 = _fcell(rt, "Current Ratio", NOW), _fcell(rt, "Current Ratio", YR)
    pm0, tax0 = _fcell(rt, "Profit Margin", NOW), _fcell(inc, "Income Tax", NOW)
    cfo0, cfo4 = _fcell(cf, "Net Cash Flow-Operating", NOW), _fcell(cf, "Net Cash Flow-Operating", YR)
    tl0, te0 = _fcell(bs, "Total Liabilities", NOW), _fcell(bs, "Total Equity", NOW)
    tl4, te4 = _fcell(bs, "Total Liabilities", YR), _fcell(bs, "Total Equity", YR)

    hits, red = [], False

    def add(t, is_red=False):
        nonlocal red
        hits.append(t)
        if is_red:
            red = True

    # 一次性损益污染：净利率离谱高，或税项为负（大额税收返还/递延税资产释放）
    oneoff = (pm0 is not None and abs(pm0) > fund_rule("fund_pm_abnormal")) or \
             (tax0 is not None and tax0 < 0)

    # 核心 1 · 营收
    if rev0 is not None and rev4 and rev4 > 0:
        c = (rev0 / rev4 - 1) * 100
        if c <= fund_rule("fund_rev_red"):
            add(f"营收同比 {c:.0f}%", True)
        elif c <= fund_rule("fund_rev_yellow"):
            add(f"营收同比 {c:.0f}%")
    # 核心 2 · 营业利润率（不含税项和一次性损益，最干净的盈利口径）
    if om0 is not None:
        if om0 < 0:
            add(f"营业亏损 {om0:.0f}%", True)
        elif om4 is not None and om0 - om4 <= -fund_rule("fund_om_drop"):
            add(f"营业利率 {om4:.1f}→{om0:.1f}%")
    # 核心 3 · 经营现金流
    if cfo0 is not None:
        if cfo0 < 0 and cfo4 is not None and cfo4 > 0:
            add("经营现金流转负", True)
        elif cfo0 < 0:
            add(f"经营现金流为负 {cfo0 / 1000:.0f}M")

    # 扩展 · 毛利率
    if gm0 is not None and gm4 is not None and gm0 - gm4 <= -fund_rule("fund_gm_drop"):
        add(f"毛利率 {gm4:.1f}→{gm0:.1f}%")
    # 扩展 · 负债/权益（要求负债绝对额也上升，否则亏损把权益做小会误报加杠杆）
    de0 = tl0 / te0 if (tl0 and te0) else None
    de4 = tl4 / te4 if (tl4 and te4) else None
    if de0 and de4 and de4 > 0 and de0 / de4 - 1 >= fund_rule("fund_de_rise") / 100.0 \
            and tl0 is not None and tl4 is not None and tl0 > tl4:
        add(f"负债/权益 {de4:.2f}→{de0:.2f}")
    # 扩展 · 短期偿债能力
    if cr0 and cr4 and cr4 > 0 and cr0 / cr4 - 1 <= -fund_rule("fund_cr_drop") / 100.0:
        add(f"流动比率 {cr4:.0f}→{cr0:.0f}")
    # 扩展 · ROE（一次性损益时不参与）
    if not oneoff and roe0 is not None and roe4 is not None and roe4 > 0 \
            and roe0 / roe4 - 1 <= -fund_rule("fund_roe_drop") / 100.0:
        add(f"ROE {roe4:.1f}→{roe0:.1f}%")

    # 一次性损益只作附加说明，不单独点亮徽章（否则 GOOGL/NTNX 会被无谓判黄）
    if oneoff and hits:
        hits.append("净利含一次性损益，未参与判定")
    if not hits:
        return {"level": "green", "hits": [], "period": f["period"], "oneoff": oneoff}
    return {"level": "red" if red else "yellow", "hits": hits,
            "period": f["period"], "oneoff": oneoff}


def fetch_history(symbol):
    """数据源优先级：Yahoo → stooq → Nasdaq

    GitHub Actions（美国 IP）走 Yahoo 正常；中国大陆本地跑 Yahoo 会被整段封禁
    （返回 403 且提示 mainland China 不可访问），stooq 也上了 JS 人机验证，
    所以补第三级 Nasdaq，保证本地也能跑出真实数据验证筛选逻辑。

    代码先过一遍 normalize_symbol：富途导出的 UNH-US 这类后缀行情源不认，
    剥掉后缀才抓得到（否则整只标的变灰）。
    """
    symbol = normalize_symbol(symbol)
    fallback = None
    for name, loader in (("yahoo", yahoo_history), ("stooq", stooq_history), ("nasdaq", nasdaq_history)):
        data = loader(symbol)
        if not data:
            continue
        data["source"] = name
        if CLOSED_ONLY:
            data = trim_history(data, TARGET_DATE)
            if not data:
                continue
            # 日期落后先试另一个源；都落后时明确展示，不冒充新数据。
            if data["dates"][-1] < TARGET_DATE:
                if fallback is None or data["dates"][-1] > fallback["dates"][-1]:
                    fallback = data
                continue
            return data
        return apply_realtime(symbol, data)
    return fallback


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
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


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
    head = f"每 {d['every']} 个交易日一次 · 起点 {d['start']}"
    if d.get("pending"):
        return f"{head} · 尚未开始（首个定投日 {d['first']}）"
    if d["due"]:
        return f"{head} · <b>美东 {d['date'][5:]} 是定投日</b>（第 {d['times']} 次）"
    return f"{head} · 距下次定投还有 {d['next_in']} 个交易日（{d['next_date']}）"


# ---------------------------------------------------------------- 筛选

def analyze_symbol(sym, cfg, data, group="watch"):
    """返回 (等级, 信号列表, 详情dict)  group: position=持仓 / watch=关注"""
    closes = data["closes"]
    vols = data["volumes"]
    price = data["price"]
    prev = data["prev_close"]

    chg = (price / prev - 1) * 100 if prev else None
    rsi = calc_rsi(closes)
    high52 = max(data["highs"]) if data["highs"] else max(closes)
    low52 = min(data["lows"]) if data["lows"] else min(closes)
    dist_high = (price / high52 - 1) * 100
    dist_low = (price / low52 - 1) * 100
    vol_ratio = (vols[-1] / sma(vols, 20)) if vols and sma(vols, 20) else None
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    boll_mid, boll_up, boll_dn = boll(closes, int(S["boll_n"]), float(S["boll_k"]))

    signals = []
    level = "green"

    # quiet 标的（如 SGOV 货币基金）：波动极小，RSI/均线无意义，
    # 只在真正异动（>=2%）时才出声，避免噪音
    if cfg.get("quiet"):
        if chg is not None and abs(chg) >= S["quiet_chg"]:
            return "yellow", [f"异动 {chg:+.1f}%"], {
                "symbol": sym, "note": cfg.get("note", ""), "price": price,
                "chg": chg, "rsi": None, "dist_high": dist_high,
                "dist_low": dist_low, "vol_ratio": vol_ratio,
                "trigger": cfg.get("trigger"), "source": data.get("source", ""),
                "data_date": (data.get("dates") or [""])[-1],
                "group": group, "boll_up": boll_up, "boll_dn": boll_dn,
                "signals": [f"异动 {chg:+.1f}%"], "level": "yellow",
            }
        return "green", [], {
            "symbol": sym, "note": cfg.get("note", ""), "price": price,
            "chg": chg, "rsi": None, "dist_high": dist_high,
            "dist_low": dist_low, "vol_ratio": vol_ratio,
            "trigger": cfg.get("trigger"), "source": data.get("source", ""),
            "data_date": (data.get("dates") or [""])[-1],
            "group": group, "boll_up": boll_up, "boll_dn": boll_dn,
            "signals": [], "level": "green",
        }

    def bump(lv):
        nonlocal level
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[lv] > order[level]:
            level = lv

    # 🔴 红色规则
    if chg is not None and abs(chg) >= S["chg_red"]:
        signals.append(f"异动 {chg:+.1f}%")
        bump("red")
    if rsi is not None and (rsi >= S["rsi_high"] or rsi <= S["rsi_low"]):
        tag = "超买" if rsi >= S["rsi_high"] else "超卖"
        signals.append(f"RSI {rsi:.0f} {tag}")
        bump("red")
    if dist_low <= S["near_52w_low_pct"]:
        signals.append("触及52周新低")
        bump("red")

    # 距加仓触发价
    trig = cfg.get("trigger")
    if trig:
        gap = (price / trig - 1) * 100
        tstr = f"{trig:,.2f}"
        if gap <= 0:
            signals.append(f"已跌破加仓价 {tstr}")
            bump("red")
        elif gap <= S["trigger_gap_pct"]:
            signals.append(f"距加仓价 {tstr} 还差 {gap:.1f}%")
            bump("red")

    # 布林带：逼近即算，盘中不用等收盘真的穿过去
    # gap = 距离轨道还差百分之多少；<=0 表示已经穿过去了
    # 另外标出「连续第几日贴近轨道」——远离一天就断，再贴近重新从第一日算
    streak = boll_streak(closes, int(S["boll_n"]), float(S["boll_k"]), S["boll_near_pct"],
                         highs=data.get("highs"), lows=data.get("lows"))
    streak_txt_up = streak_tail(streak["up"], "上轨")
    streak_txt_dn = streak_tail(streak["dn"], "下轨")
    # 盘中触及即算：上轨比当日最高价，下轨比当日最低价；盘中重跑时当前价可能比已有极值更极端
    day_high = (data.get("highs") or [None])[-1]
    day_low = (data.get("lows") or [None])[-1]
    touch_up = price if day_high is None else max(day_high, price)
    touch_dn = price if day_low is None else min(day_low, price)
    # 触及价和展示的现价不一致时，把触及价写出来。
    # 否则会出现「现价 54.00 却写着突破上轨 59.15」的观感矛盾 ——
    # 其实是当天盘中最高冲到 60.68 穿过去了，收盘又跌回来。
    def touch_note(touch):
        if touch is None or abs(touch - price) < 1e-9:
            return ""
        return f"（盘中触及 {touch:,.2f}）"

    # 上下轨都命中时只留更贴近的那条：
    # SGOV 这类短债/货币 ETF 波动极小，布林带宽不到 1%，上下轨会同时命中，
    # 两条一起显示等于自相矛盾（既逼近上轨又逼近下轨）。
    hits = []
    if boll_up is not None:
        gap_up = (boll_up - touch_up) / boll_up * 100
        if gap_up <= S["boll_near_pct"]:
            text = (f"突破布林上轨 {boll_up:,.2f}" if gap_up <= 0
                    else f"逼近布林上轨 还差{gap_up:.2f}%")
            hits.append((gap_up, text + touch_note(touch_up) + streak_txt_up, "red"))
    if boll_dn is not None:
        gap_dn = (touch_dn - boll_dn) / boll_dn * 100
        if gap_dn <= S["boll_near_pct"]:
            text = (f"跌破布林下轨 {boll_dn:,.2f}" if gap_dn <= 0
                    else f"逼近布林下轨 还差{gap_dn:.2f}%")
            hits.append((gap_dn, text + touch_note(touch_dn) + streak_txt_dn, "yellow"))
    if hits:
        hits.sort(key=lambda x: x[0])
        signals.append(hits[0][1])
        bump(hits[0][2])

    # 🟡 黄色规则
    if chg is not None and S["chg_yellow"] <= abs(chg) < S["chg_red"]:
        signals.append(f"波动 {chg:+.1f}%")
        bump("yellow")
    if vol_ratio and vol_ratio >= S["vol_ratio"]:
        signals.append(f"量比 {vol_ratio:.1f}x")
        bump("yellow")

    # 日内振幅：涨跌幅只看「收盘 vs 昨收」，盘中冲高又回落、最后收平的票不会响，这条专门抓它。
    # 用当日最高/最低（盘中重跑时再和当前价取极值）除以昨收。
    amp = None
    if touch_up is not None and touch_dn is not None and prev:
        amp = (touch_up - touch_dn) / prev * 100
        if amp >= S["amp_red"]:
            signals.append(f"振幅 {amp:.1f}%（{touch_dn:,.2f}~{touch_up:,.2f}）")
            bump("red")
        elif amp >= S["amp_yellow"]:
            signals.append(f"振幅 {amp:.1f}%（{touch_dn:,.2f}~{touch_up:,.2f}）")
            bump("yellow")

    # 均线穿越（比"贴近"有意义得多）
    if len(closes) >= 2:
        prev_c = closes[-2]
        for ma_val, ma_name in ((ma50, "50日"), (ma200, "200日")):
            if not ma_val:
                continue
            if prev_c < ma_val <= price:
                signals.append(f"上穿{ma_name}均线")
                bump("yellow")
            elif prev_c > ma_val >= price:
                signals.append(f"跌破{ma_name}均线")
                bump("yellow")

    detail = {
        "symbol": sym,
        "note": cfg.get("note", ""),
        "price": price,
        "chg": chg,
        "rsi": rsi,
        "dist_high": dist_high,
        "dist_low": dist_low,
        "vol_ratio": vol_ratio,
        "amp": amp,
        "trigger": trig,
        "source": data.get("source", ""),
        "data_date": (data.get("dates") or [""])[-1],
        "realtime": bool(data.get("realtime")),
        "rt_ts": data.get("rt_ts", ""),
        "group": group,
        "boll_up": boll_up,
        "boll_dn": boll_dn,
        # ETF 标签：静态表命中，或行情源明确给出 assetclass=etf
        "etf": is_etf(sym) or data.get("asset") == "etf",
        "signals": signals,
        "level": level,
    }
    return level, signals, detail


# ---------------------------------------------------------------- HTML

def fmt(v, unit="", nd=2):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}{unit}"


RUN_JS = '<script src="./run-status.js?v=20261007-10"></script>'


def config_hash(filename):
    """与GitHub Contents API content.sha一致，用于精确验证哪版配置已生效。"""
    raw = open(os.path.join(BASE, filename), "rb").read()
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def beijing_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        return ""


def build_snapshot(macro, items, cfg, watch_shown=None):
    dates = sorted({d.get("data_date") for d in items if d.get("data_date")})
    missing = [normalize_symbol(d["symbol"]) for d in items if d.get("price") is None]
    stale = [normalize_symbol(d["symbol"]) for d in items
             if d.get("data_date") and TARGET_DATE and d["data_date"] < TARGET_DATE]
    positions, focus, watch = (cfg.get("positions", {}), cfg.get("focus", {}),
                               cfg.get("watch", {}))
    dca = global_dca(TARGET_DATE)
    return {
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
        "schedule": "美东周一至周五20:30；北京时间夏季次日08:30、冬季次日09:30",
        "config_files": {f: config_hash(f) for f in ("holdings.json", "settings.json")},
        "effective_settings": dict(S),
        # watch 计数用「看板实际展示/抓取」的只数，不含与持仓/重点关注重复被隐藏的那些
        "list_counts": {"positions": len(positions), "focus": len(focus),
                        "watch": len(watch) if watch_shown is None else watch_shown,
                        "triggers": sum(bool(c.get("trigger")) for c in positions.values()),
                        "dca": 1 if dca else 0},
        "summary": {**{lv: sum(d["level"] == lv for d in items) for lv in ("red", "yellow", "green", "gray")},
                    "total": len(items), "macro_ok": sum(bool(m.get("ok")) for m in macro.values()),
                    "stale_symbols": stale, "missing_symbols": missing},
        "actual_dates": {"min": dates[0] if dates else "", "max": dates[-1] if dates else ""},
        "macro_dates": {k: m.get("date", "") for k, m in macro.items()},
        "coverage": "数据源日线；完整23小时夜盘/日盘覆盖尚未验证。自动日报不并入实时价；手动常规盘中按当前报价重算。",
    }


def render(macro, items, watch_count, data_down=False, snapshot=None, dup_hidden=0):
    rows_macro = []
    for key in ["hy_oas", "vix", "sp500", "ust10", "curve", "dxy"]:
        it = macro.get(key, {})
        if not it.get("ok"):
            rows_macro.append(
                f'<tr class="gray"><td>{it.get("name", key)}</td><td colspan="3">无数据</td></tr>'
            )
            continue
        lv, txt = macro_status(key, it)
        if key == "sp500":
            val = f'−{abs(it["drawdown"]):.1f}%' if it.get("drawdown") is not None else "—"
        elif key == "hy_oas":
            val = f'{it["value"]:.0f} bp'
        elif key == "vix":
            val = f'{it["value"]:.1f}'
        elif key == "ust10":
            val = f'{it["value"]:.2f}%'
        else:
            val = f'{it["value"]:.2f}'

        dw = it.get("delta_week")
        dw_txt = f'{dw:+.0f}' if (key == "hy_oas" and dw is not None) else "—"
        rows_macro.append(
            f'<tr class="{lv}"><td>{it["name"]}</td><td class="num">{val}</td>'
            f'<td class="num dim">{dw_txt}</td><td>{txt}</td></tr>'
        )

    order = {"red": 0, "yellow": 1, "green": 2, "gray": 3}
    # 行情没异动但基本面亮了警示的，要上表并往前排，否则徽章永远藏在「无异动 N 只」里看不见
    def fund_alert(d):
        return (d.get("fund") or {}).get("level") in ("red", "yellow")

    def sort_key(d):
        base = order[d["level"]]
        if fund_alert(d) and base > 1:
            base = 1.5  # 夹在黄和绿之间
        return (base, -(abs(d["chg"]) if d["chg"] else 0))

    items.sort(key=sort_key)

    def rows_of(lst):
        out = ""
        for d in lst:
            cls = d["level"]
            sig = " · ".join(d["signals"]) if d["signals"] else "—"
            chg_cls = "up" if (d["chg"] or 0) > 0 else (
                "down" if (d["chg"] or 0) < 0 else "")
            chg_txt = f'{d["chg"]:+.2f}%' if d["chg"] is not None else "—"
            note = f'<span class="note">{d["note"]}</span>' if d["note"] else ""
            # ETF / 基金类标一个小标签，和个股区分开；抓不到数据时用静态表兜底判断
            tag = '<span class="tag">ETF</span>' if (d.get("etf") or is_etf(d["symbol"])) else ""
            # 基本面警示徽章：只对持仓/重点关注查询，鼠标悬停看具体哪几项恶化
            fund = d.get("fund") or {}
            if fund.get("level") in ("red", "yellow") and fund.get("hits"):
                detail = html_lib.escape(
                    (f"{fund.get('period') or ''} 报告期 · " if fund.get("period") else "")
                    + "；".join(fund["hits"]))
                fcls = "tag f-red" if fund["level"] == "red" else "tag f-yellow"
                tag += f'<span class="{fcls}" title="基本面：{detail}">基本面</span>'
            out += (
                f'<tr class="{cls}"><td class="sym">{normalize_symbol(d["symbol"])}{note}{tag}</td>'
                f'<td class="num">{fmt(d["price"])}</td>'
                f'<td class="num {chg_cls}">{chg_txt}</td>'
                f'<td class="sig">{sig}</td></tr>'
            )
        if not out:
            out = '<tr class="gray"><td colspan="4">今晚无异动，不用盯</td></tr>'
        return out

    # 三个分区：持仓 > 重点关注 > 其他关注；组内再拆「个股」和「ETF 基金」，不混排。
    # 红/黄是警示行；灰（取数失败）也展示出来，否则失败会被静默吞掉；绿的不上表只计数。
    is_etf_row = lambda d: bool(d.get("etf")) or is_etf(d["symbol"])
    group_defs = [("持仓", "position"), ("重点关注", "focus"), ("其他关注", "watch")]
    group_cards = ""
    for gtitle, gkey in group_defs:
        g = [d for d in items if d.get("group") == gkey]
        # 基本面红/黄的即使行情是绿灯也要上表，不然警示等于没显示
        shown = [d for d in g if d["level"] in ("red", "yellow", "gray") or fund_alert(d)]
        n_quiet = len([d for d in g if d["level"] == "green" and not fund_alert(d)])
        if gkey == "focus" and not g:
            body = '<div class="quiet">重点关注清单还是空的：去编辑页添加，或从其他关注里挑几只</div>'
        else:
            stocks = [d for d in shown if not is_etf_row(d)]
            etfs = [d for d in shown if is_etf_row(d)]
            parts = []
            if stocks:
                parts.append(f"<h2>个股</h2>\n<table>{rows_of(stocks)}</table>")
            if etfs:
                parts.append(f"<h2>ETF 基金</h2>\n<table>{rows_of(etfs)}</table>")
            if not parts:
                parts.append('<div class="quiet">今晚无异动，不用盯</div>')
            if n_quiet:
                parts.append(f'<div class="quiet">无异动 {n_quiet} 只</div>')
            body = "\n".join(parts)
        group_cards += f'<div class="card">\n<h2 class="grp">{gtitle}</h2>\n{body}\n</div>\n'

    # 宏观总判断
    hy = macro.get("hy_oas", {})
    vix = macro.get("vix", {})
    sp = macro.get("sp500", {})
    overall, overall_txt = "green", "绿框 · 不用动"
    if not hy.get("ok"):
        # 核心指标缺失时不能假装"绿框不用动"，否则会误导下单
        overall, overall_txt = "gray", "跑路价签数据缺失 · 别据此下单"
    elif hy.get("ok") and hy["value"] >= S["hy_red"]:
        overall, overall_txt = "red", f"跑路价签≥{S['hy_red']:.0f} · 进入危机确认"
    elif hy.get("ok") and hy.get("delta_week") and hy["delta_week"] >= 50:
        overall, overall_txt = "red", "利差一周急剧扩大 · 立刻警戒"
    elif sp.get("ok") and (sp.get("drawdown") or 0) <= -10:
        overall, overall_txt = "yellow", "指数进入加仓档位 · 按表执行"
    elif hy.get("ok") and hy["value"] >= S["hy_green"]:
        overall, overall_txt = "yellow", "利差收紧 · 弹药就位"

    src_name = {"yahoo": "Yahoo Finance", "stooq": "Stooq", "nasdaq": "Nasdaq"}
    srcs = sorted({d.get("source") for d in items if d.get("source")})
    src_txt = " · ".join(src_name.get(x, x) for x in srcs) if srcs else "本轮不可用"
    n_rt = len([d for d in items if d.get("realtime")])
    if n_rt:
        src_txt += f" · {n_rt} 只用了实时价（Nasdaq，0 延迟）"
    # 之前漏了 join，整段被当成 list 的 str() 插进表格，页面上会多出 [' 和 ']
    rows_macro = "".join(rows_macro)
    snapshot = snapshot or {}
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    snapshot_json = snapshot_json.replace("</", "<\\/")
    summary = snapshot.get("summary", {})
    counts = snapshot.get("list_counts", {})
    actual_dates = snapshot.get("actual_dates", {})
    # 一行摘要升级为状态灯文案（首页第三排）；这里只算红黄绿计数和异常清单。
    bad = summary.get("stale_symbols", []) + summary.get("missing_symbols", [])
    gray_txt = f" 灰{summary.get('gray')}" if summary.get("gray") else ""
    if bad:
        init_light_phase = "bad"
        init_light = (f"抓取失败 {len(bad)} 只：{html_lib.escape('、'.join(bad[:6]))}"
                      + (" 等" if len(bad) > 6 else "") + " · 规则未完全生效")
    else:
        init_light_phase = "ok"
        init_light = (f"红{summary.get('red', 0)} 黄{summary.get('yellow', 0)} "
                      f"绿{summary.get('green', 0)}{gray_txt} · "
                      f"{summary.get('total', 0)} 只全部更新 · 规则按当前设置生效")
    dca_info = dca_text(snapshot.get("dca_reminder"))
    # 第二排：数据时间 + 自动计划。冬夏令时只显示当日适用的那条（以当天美东是否夏令时为准）。
    ny_now = datetime.now(US_TZ)
    bj_auto = "夏令时次日 08:30" if ny_now.dst() != timedelta(0) else "冬令时次日 09:30"
    max_date = actual_dates.get("max") or "—"
    if snapshot.get("mode") and snapshot["mode"] != "closed":
        m_fin = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})",
                         snapshot.get("finished_at_bj") or "")
        dt_txt = f"{m_fin.group(1)} {m_fin.group(2)}（盘中，北京时间）" if m_fin else max_date
    else:
        dt_txt = max_date
    sub_line = f"数据时间 {dt_txt} · 自动计划：美东周一至五 20:30（北京 {bj_auto}）"
    lc = counts
    reg_line = (f'<div class="quiet">在册：持仓 {lc.get("positions", 0)} · '
                f'重点关注 {lc.get("focus", 0)} · 其他关注 {lc.get("watch", 0)}'
                + (f' · 已隐藏 {dup_hidden} 只与持仓/重点关注重复（只在更高分区显示一次）'
                   if dup_hidden else "") + "</div>")
    dca_html = (f'<div class="dca {"on" if (snapshot.get("dca_reminder") or {}).get("due") else ""}">'
                f'定投提醒：{dca_info}</div>') if dca_info else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>AI监测市场</title>
<style>
:root{{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --text:#e6e8ec; --dim:#8b93a1;
  --green:#3fb950; --yellow:#d29922; --red:#f85149; --up:#f85149; --down:#3fb950;
  --blue:#2f6bd8;
}}
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;
  padding:env(safe-area-inset-top) 14px 40px}}
.wrap{{max-width:720px;margin:0 auto}}
h1{{font-size:19px;margin:18px 0 4px;font-weight:600}}
.sub{{color:var(--dim);font-size:12px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:6px 4px;margin-bottom:14px;overflow:hidden}}
h2{{font-size:13px;color:var(--dim);font-weight:600;margin:10px 12px 8px;letter-spacing:.3px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td{{padding:9px 10px;border-top:1px solid var(--line);vertical-align:middle}}
tr:first-child td{{border-top:none}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.dim{{color:var(--dim);font-size:12px}}
.sym{{font-weight:600}}
.note{{color:var(--dim);font-weight:400;font-size:11px;margin-left:6px}}
.tag{{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:4px;
  font-size:10px;font-weight:600;letter-spacing:.3px;vertical-align:1px;
  color:#8fb8f0;background:rgba(107,163,240,.14);border:1px solid rgba(107,163,240,.32)}}
.tag.f-red{{color:#ff9b95;background:rgba(248,81,73,.16);border-color:rgba(248,81,73,.42)}}
.tag.f-yellow{{color:#f0c674;background:rgba(210,153,34,.16);border-color:rgba(210,153,34,.42)}}
.sig{{font-size:12.5px;color:var(--text)}}
.up{{color:var(--up)}} .down{{color:var(--down)}}
tr.red td:first-child{{box-shadow:inset 3px 0 0 var(--red)}}
tr.yellow td:first-child{{box-shadow:inset 3px 0 0 var(--yellow)}}
tr.green td:first-child{{box-shadow:inset 3px 0 0 var(--green)}}
tr.gray td{{color:var(--dim)}}
.badge{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}}
tr.red .badge{{background:rgba(248,81,73,.15);color:var(--red)}}
tr.yellow .badge{{background:rgba(210,153,34,.15);color:var(--yellow)}}
tr.green .badge{{background:rgba(63,185,80,.15);color:var(--green)}}
.overall{{border-radius:12px;padding:14px 16px;margin-bottom:14px;
  font-weight:600;font-size:15px;border:1px solid var(--line)}}
.o-green{{background:rgba(63,185,80,.12);color:var(--green)}}
.o-yellow{{background:rgba(210,153,34,.14);color:var(--yellow)}}
.o-red{{background:rgba(248,81,73,.14);color:var(--red)}}
.o-gray{{background:rgba(139,147,161,.14);color:var(--dim)}}
.foot{{color:var(--dim);font-size:11.5px;text-align:center;margin-top:22px;line-height:1.7}}
.quiet{{color:var(--dim);font-size:12px;padding:8px 12px 12px}}
.warn{{background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.35);
  color:var(--yellow);border-radius:10px;padding:11px 14px;margin-bottom:14px;font-size:13px}}
.acts{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}}
.acts button{{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}}
.acts button:disabled{{opacity:.5;cursor:not-allowed}}
#runMsg{{font-size:12px;color:var(--dim);flex:1;min-width:180px;line-height:1.5}}
#runMsg.ok{{color:var(--green)}}
#runMsg.err{{color:var(--red)}}
.runbar{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}}
.runbar button{{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;cursor:pointer;font-family:inherit}}
.runbar button:disabled{{opacity:.5;cursor:not-allowed}}
.runbar button.primary{{background:var(--blue);border-color:var(--blue);color:#fff}}
.runbar a.btnlink{{padding:8px 14px;font-size:13px;color:var(--text);background:#232833;
  border:1px solid #333a47;border-radius:8px;text-decoration:none;display:inline-block}}
.statusrow{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin:-4px 0 14px;font-size:12.5px;line-height:1.5}}
h2.grp{{color:var(--text);font-size:14px;margin-top:14px}}
.runlight{{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--dim);line-height:1.4}}
.runlight .dot{{width:9px;height:9px;border-radius:50%;background:#4b5563;flex:none}}
.runlight[data-phase="busy"]{{color:var(--yellow)}}
.runlight[data-phase="busy"] .dot{{background:var(--yellow);box-shadow:0 0 0 3px rgba(210,153,34,.18)}}
.runlight[data-phase="ok"]{{color:var(--green)}}
.runlight[data-phase="ok"] .dot{{background:var(--green);box-shadow:0 0 0 3px rgba(63,185,80,.18)}}
.runlight[data-phase="bad"]{{color:var(--red)}}
.runlight[data-phase="bad"] .dot{{background:var(--red);box-shadow:0 0 0 3px rgba(248,81,73,.18)}}
.dca{{background:rgba(139,147,161,.12);border:1px solid var(--line);border-radius:10px;
  padding:10px 14px;margin-bottom:14px;font-size:13px;color:var(--dim)}}
.dca b{{color:var(--text)}}
.dca.on{{background:rgba(210,153,34,.14);border-color:rgba(210,153,34,.4);color:var(--yellow)}}
.dca.on b{{color:var(--yellow)}}
</style>
</head>
<body><div class="wrap">
<h1>AI监测市场</h1>
<div class="sub">{sub_line}</div>

<div class="runbar">
  <button class="primary" id="btnRunNow">立即运行</button>
  <button id="btnCheckStatus">刷新状态</button>
  <a class="btnlink" href="./edit.html">设置</a>
</div>
<div class="statusrow">
  <div class="runlight" id="runLight" data-phase="{init_light_phase}"><i class="dot"></i><span id="runLightTxt">{init_light}</span></div>
</div>

{dca_html}
<div class="script-data" hidden><script id="snapshotData" type="application/json">{snapshot_json}</script></div>

<div class="overall o-{overall}">{overall_txt}</div>
{('<div class="warn">⚠ 行情源暂时不可用（Yahoo 限流），个股数据未更新，宏观数据正常。通常隔一阵会自动恢复。</div>' if data_down else "")}

<div class="card">
<h2>宏观</h2>
<table>
<tr class="gray"><td style="color:var(--dim)">指标</td><td class="num" style="color:var(--dim)">当前</td><td class="num" style="color:var(--dim)">周变化</td><td style="color:var(--dim)">状态</td></tr>
{rows_macro}
</table>
</div>

{group_cards}
{reg_line}

<div class="foot">
宏观：FRED · 个股：{src_txt}<br>
基本面：Nasdaq 季度财报（只查持仓 + 重点关注，ETF 不判）· 鼠标悬停徽章看具体哪几项恶化<br>
跑路价签 &lt;{S['hy_green']:.0f} 平静 · {S['hy_green']:.0f}–{S['hy_yellow']:.0f} 收紧 · ≥{S['hy_red']:.0f} 危机确认<br>
布林带 {int(S['boll_n'])} 日 / {S['boll_k']:g} 倍标准差 · 逼近上下轨即算（差 ≤{S['boll_near_pct']:g}%）<br>
轨道按当日收盘价滚动计算；是否触及按<b>当日最高/最低价</b>判定，盘中穿过就算<br>
连续贴近轨道按日累计，远离一天（差 &gt;{S['boll_near_pct']:g}%）就断，再次贴近重新从第一日算 · 连续 2 日起才标注<br>
定投提醒按<b>交易日</b>计数（不含周末休市），间隔与起始日在编辑页逐只设置<br>
异动 ≥{S['chg_red']:g}% 红 · ≥{S['chg_yellow']:g}% 黄 · RSI ≥{S['rsi_high']:.0f} 或 ≤{S['rsi_low']:.0f} 红 · 量比 ≥{S['vol_ratio']:g}x 黄<br>
日内振幅（最高-最低）/昨收 ≥{S['amp_red']:g}% 红 · ≥{S['amp_yellow']:g}% 黄（抓冲高回落、收盘却没动的票）<br>
<a href="./settings.json" style="color:#6ba3f0;text-decoration:none">查看当前阈值 settings.json</a>
</div>

<div class="acts">
<span id="runMsg"></span>
</div>
{RUN_JS}
</div></body></html>"""


# ---------------------------------------------------------------- main

def main(argv=None):
    global TARGET_DATE, CLOSED_ONLY
    parser = argparse.ArgumentParser(description="市场自检生成器")
    parser.add_argument("--prepare-run", action="store_true", help="锁定本次运行目标交易日并输出 GitHub Actions 环境变量")
    args = parser.parse_args(argv)
    if args.prepare_run:
        prepare_run()
        return 0

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

    # 三组分开存：持仓 / 重点关注 / 其他关注。旧文件没有 focus 字段就当空组，完全兼容。
    positions = cfg.get("positions", {})
    focus = cfg.get("focus", {})
    watch = cfg.get("watch", {})
    # 同一只代码只出现在优先级最高的分区：持仓 > 重点关注 > 其他关注。
    # 重复的不重复抓取、不重复报警，也不会被数两遍。
    pos_norm = {normalize_symbol(s).upper() for s in positions}
    focus_eff = {s: c for s, c in focus.items()
                 if normalize_symbol(s).upper() not in pos_norm}
    focus_norm = {normalize_symbol(s).upper() for s in focus_eff}
    watch_eff = {s: c for s, c in watch.items()
                 if normalize_symbol(s).upper() not in pos_norm
                 and normalize_symbol(s).upper() not in focus_norm}
    dup_hidden = (len(focus) - len(focus_eff)) + (len(watch) - len(watch_eff))
    universe = [(s, c, "position") for s, c in positions.items()] + \
               [(s, c, "focus") for s, c in focus_eff.items()] + \
               [(s, c, "watch") for s, c in watch_eff.items()]
    if not universe:
        log("holdings.json 里没有自选股，跳过个股部分")

    macro = build_macro()
    items = []
    if universe:
        log(f"抓取 {len(positions)} 只持仓 + {len(focus_eff)} 只重点关注 + {len(watch_eff)} 只其他关注"
            + (f"（另有 {dup_hidden} 只重复，已跳过）" if dup_hidden else "") + "...")
        fail_streak = 0
        data_down = False
        for i, (sym, sc, grp) in enumerate(universe):
            data = fetch_history(sym) if not data_down else None
            if i < len(universe) - 1:
                time.sleep(0.5)  # 轻微限速，降低被封概率
            if data:
                fail_streak = 0
                lv, sig, detail = analyze_symbol(sym, sc, data, group=grp)
                items.append(detail)
                if lv != "green":
                    log(f"  {sym}: {lv} — {', '.join(sig)}")
                continue

            fail_streak += 1
            if fail_streak >= 3 and not data_down:
                data_down = True
                log("  ! 连续失败 3 次，判定行情源不可用，跳过剩余")
            items.append({
                "symbol": sym, "note": sc.get("note", ""), "price": None,
                "chg": None, "rsi": None, "dist_high": None, "dist_low": None,
                "vol_ratio": None, "trigger": sc.get("trigger"),
                "group": grp, "data_date": "", "boll_up": None, "boll_dn": None,
                "signals": ["行情源暂时不可用"] if data_down else ["数据获取失败"],
                "level": "gray",
            })

    # 基本面只查持仓 + 重点关注：其他关注只数大、交易价值低，全量查既拖慢运行又容易限流
    fund_stat = {"checked": 0, "red": 0, "yellow": 0}
    fund_targets = [d for d in items
                    if d.get("group") in ("position", "focus") and d.get("symbol")]
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

    snapshot = build_snapshot(macro, items, cfg, watch_shown=len(watch_eff))
    snapshot["fundamental"] = fund_stat
    html = render(macro, items, len(universe), data_down=not universe
                  or all(d["level"] == "gray" for d in items), snapshot=snapshot,
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
            gname = {"position": "持仓", "focus": "重点关注", "watch": "关注"}.get(
                d.get("group"), "关注")
            sig = " / ".join(d.get("signals") or []) or "—"
            lines.append("- **%s** %s%s · %s（%s）" % (
                normalize_symbol(d["symbol"]), price, chg, sig, gname))
        lines.append("")

    lines.append("## 宏观")
    for k in ("hy_oas", "vix", "sp500", "ust10", "curve", "dxy"):
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
