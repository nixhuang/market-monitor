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

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# 北京时间
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)

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

# 宏观阈值（跑路价签单位为 bp）
THRESH = {
    "hy_oas":  {"green": 350, "yellow": 400, "red": 400},   # >=400 危机确认
    "vix":     {"green": 20,  "yellow": 30,  "red": 40},
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
        if v >= 400:
            return "red", "危机确认"
        if v >= 350:
            return "yellow", "收紧"
        return "green", "平静"

    if key == "vix":
        v = item["value"]
        if v >= 40:
            return "red", "极度恐慌"
        if v >= 30:
            return "yellow", "恐慌"
        if v >= 20:
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
                closes = [c for c in q.get("close", []) if c is not None]
                vols = [v for v in q.get("volume", []) if v is not None]
                highs = [h for h in q.get("high", []) if h is not None]
                lows = [l for l in q.get("low", []) if l is not None]
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
                    "o": float(p[1]), "h": float(p[2]), "l": float(p[3]),
                    "c": float(p[4]), "v": float(p[5]) if p[5] else 0.0,
                })
            except ValueError:
                continue
        if len(rows) < 30:
            return None
        return {
            "closes": [r["c"] for r in rows],
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
                    "volumes": [p["v"] for p in parsed],
                    "highs": [p["h"] for p in parsed],
                    "lows": [p["l"] for p in parsed],
                    "price": parsed[-1]["c"],
                    "prev_close": parsed[-2]["c"] if len(parsed) >= 2 else None,
                    "currency": "USD",
                    "source": "nasdaq",
                }
            except Exception as e:
                log(f"  ! {symbol} nasdaq({sym}/{ac}) 失败: {str(e)[:60]}")
                continue
    return None


def fetch_history(symbol):
    """数据源优先级：Yahoo → stooq → Nasdaq

    GitHub Actions（美国 IP）走 Yahoo 正常；中国大陆本地跑 Yahoo 会被整段封禁
    （返回 403 且提示 mainland China 不可访问），stooq 也上了 JS 人机验证，
    所以补第三级 Nasdaq，保证本地也能跑出真实数据验证筛选逻辑。
    """
    data = yahoo_history(symbol)
    if data:
        data["source"] = "yahoo"
        return data
    time.sleep(1.2)
    data = stooq_history(symbol)
    if data:
        return data
    time.sleep(0.8)
    return nasdaq_history(symbol)


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


# ---------------------------------------------------------------- 筛选

def analyze_symbol(sym, cfg, data):
    """返回 (等级, 信号列表, 详情dict)"""
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

    signals = []
    level = "green"

    # quiet 标的（如 SGOV 货币基金）：波动极小，RSI/均线无意义，
    # 只在真正异动（>=2%）时才出声，避免噪音
    if cfg.get("quiet"):
        if chg is not None and abs(chg) >= 2:
            return "yellow", [f"异动 {chg:+.1f}%"], {
                "symbol": sym, "note": cfg.get("note", ""), "price": price,
                "chg": chg, "rsi": None, "dist_high": dist_high,
                "dist_low": dist_low, "vol_ratio": vol_ratio,
                "trigger": cfg.get("trigger"), "source": data.get("source", ""),
                "signals": [f"异动 {chg:+.1f}%"], "level": "yellow",
            }
        return "green", [], {
            "symbol": sym, "note": cfg.get("note", ""), "price": price,
            "chg": chg, "rsi": None, "dist_high": dist_high,
            "dist_low": dist_low, "vol_ratio": vol_ratio,
            "trigger": cfg.get("trigger"), "source": data.get("source", ""),
            "signals": [], "level": "green",
        }

    def bump(lv):
        nonlocal level
        order = {"green": 0, "yellow": 1, "red": 2}
        if order[lv] > order[level]:
            level = lv

    # 🔴 红色规则
    if chg is not None and abs(chg) >= 4:
        signals.append(f"异动 {chg:+.1f}%")
        bump("red")
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        tag = "超买" if rsi >= 70 else "超卖"
        signals.append(f"RSI {rsi:.0f} {tag}")
        bump("red")
    if dist_low <= 1:
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
        elif gap <= 5:
            signals.append(f"距加仓价 {tstr} 还差 {gap:.1f}%")
            bump("red")

    # 🟡 黄色规则
    if chg is not None and 2 <= abs(chg) < 4:
        signals.append(f"波动 {chg:+.1f}%")
        bump("yellow")
    if vol_ratio and vol_ratio >= 1.5:
        signals.append(f"量比 {vol_ratio:.1f}x")
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
        "trigger": trig,
        "source": data.get("source", ""),
        "signals": signals,
        "level": level,
    }
    return level, signals, detail


# ---------------------------------------------------------------- HTML

def fmt(v, unit="", nd=2):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}{unit}"


def render(macro, items, watch_count, data_down=False):
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
    items.sort(key=lambda d: (order[d["level"]], -(abs(d["chg"]) if d["chg"] else 0)))
    focus = [d for d in items if d["level"] in ("red", "yellow")]
    quiet = [d for d in items if d["level"] == "green"]

    rows_focus = ""
    for d in focus:
        cls = d["level"]
        sig = " · ".join(d["signals"]) if d["signals"] else "—"
        chg_cls = "up" if (d["chg"] or 0) > 0 else ("down" if (d["chg"] or 0) < 0 else "")
        chg_txt = f'{d["chg"]:+.2f}%' if d["chg"] is not None else "—"
        note = f'<span class="note">{d["note"]}</span>' if d["note"] else ""
        rows_focus += (
            f'<tr class="{cls}"><td class="sym">{d["symbol"]}{note}</td>'
            f'<td class="num">{fmt(d["price"])}</td>'
            f'<td class="num {chg_cls}">{chg_txt}</td>'
            f'<td class="sig">{sig}</td></tr>'
        )
    if not rows_focus:
        rows_focus = '<tr class="gray"><td colspan="4">今晚无异动，不用盯</td></tr>'

    # 宏观总判断
    hy = macro.get("hy_oas", {})
    vix = macro.get("vix", {})
    sp = macro.get("sp500", {})
    overall, overall_txt = "green", "绿框 · 不用动"
    if not hy.get("ok"):
        # 核心指标缺失时不能假装"绿框不用动"，否则会误导下单
        overall, overall_txt = "gray", "跑路价签数据缺失 · 别据此下单"
    elif hy.get("ok") and hy["value"] >= 400:
        overall, overall_txt = "red", "跑路价签破400 · 进入危机确认"
    elif hy.get("ok") and hy.get("delta_week") and hy["delta_week"] >= 50:
        overall, overall_txt = "red", "利差一周急剧扩大 · 立刻警戒"
    elif sp.get("ok") and (sp.get("drawdown") or 0) <= -10:
        overall, overall_txt = "yellow", "指数进入加仓档位 · 按表执行"
    elif hy.get("ok") and hy["value"] >= 350:
        overall, overall_txt = "yellow", "利差收紧 · 弹药就位"

    src_name = {"yahoo": "Yahoo Finance", "stooq": "Stooq", "nasdaq": "Nasdaq"}
    srcs = sorted({d.get("source") for d in items if d.get("source")})
    src_txt = " · ".join(src_name.get(x, x) for x in srcs) if srcs else "本轮不可用"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>市场自检</title>
<style>
:root{{
  --bg:#0f1115; --card:#171a21; --line:#252a33; --text:#e6e8ec; --dim:#8b93a1;
  --green:#3fb950; --yellow:#d29922; --red:#f85149; --up:#f85149; --down:#3fb950;
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
</style>
</head>
<body><div class="wrap">
<h1>市场自检</h1>
<div class="sub">{NOW.strftime('%Y-%m-%d %H:%M')} 北京时间 · 数据自动更新</div>

<div class="overall o-{overall}">{overall_txt}</div>
{('<div class="warn">⚠ 行情源暂时不可用（Yahoo 限流），个股数据未更新，宏观数据正常。通常隔一阵会自动恢复。</div>' if data_down else "")}

<div class="card">
<h2>宏观</h2>
<table>
<tr class="gray"><td style="color:var(--dim)">指标</td><td class="num" style="color:var(--dim)">当前</td><td class="num" style="color:var(--dim)">周变化</td><td style="color:var(--dim)">状态</td></tr>
{rows_macro}
</table>
</div>

<div class="card">
<h2>今晚重点关注</h2>
<table>{rows_focus}</table>
<div class="quiet">其余 {len(quiet)} 只 / 共 {watch_count} 只自选无异动</div>
</div>

<div class="foot">
宏观：FRED · 个股：{src_txt}<br>
跑路价签 &lt;350 平静 · 350–400 收紧 · ≥400 危机确认
</div>
</div></body></html>"""


# ---------------------------------------------------------------- main

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(base, "holdings.json")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    watch = cfg.get("watch", {})
    if not watch:
        log("holdings.json 里没有自选股，跳过个股部分")

    macro = build_macro()

    items = []
    if watch:
        log(f"抓取 {len(watch)} 只自选...")
        fail_streak = 0
        data_down = False
        for i, (sym, sc) in enumerate(watch.items()):
            data = None
            if not data_down:
                data = fetch_history(sym)
                if i < len(watch) - 1:
                    time.sleep(0.5)  # 轻微限速，降低被封概率
            if data:
                fail_streak = 0
                lv, sig, detail = analyze_symbol(sym, sc, data)
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
                "signals": ["行情源暂时不可用"] if data_down else ["数据获取失败"],
                "level": "gray",
            })

    html = render(macro, items, len(watch), data_down=not watch
                  or all(d["level"] == "gray" for d in items))
    out = os.path.join(base, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"已生成 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
