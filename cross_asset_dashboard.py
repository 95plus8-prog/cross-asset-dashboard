#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import socketserver
import sys
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "cross_asset_dashboard"
CACHE_PATH = DASHBOARD_DIR / "market_snapshot.json"
PORT = 8765

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
)

QUOTE_SYMBOLS = {
    "vix": {"symbol": ".VIX", "name": "VIX 恐慌指数", "format": "number"},
    "spy": {"symbol": "SPY", "name": "SPY 标普500", "format": "price"},
    "rsp": {"symbol": "RSP", "name": "RSP 等权标普500", "format": "price"},
    "iwm": {"symbol": "IWM", "name": "IWM 小盘股", "format": "price"},
    "hyg": {"symbol": "HYG", "name": "HYG 高收益债", "format": "price"},
    "jnk": {"symbol": "JNK", "name": "JNK 高收益债", "format": "price"},
    "tnx": {"symbol": "US10Y", "name": "10 年期美债收益率", "format": "yield"},
    "dxy": {"symbol": ".DXY", "name": "美元指数 DXY", "format": "number"},
    "gold": {"symbol": "@GC.1", "name": "黄金期货", "format": "price"},
}

FRED_SERIES = {
    "hy_spread": {
        "series": "BAMLH0A0HYM2",
        "name": "高收益债利差",
        "unit": "%",
    },
}


def http_get(url: str, *, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.google.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def pct_change(now: float | None, old: float | None) -> float | None:
    if now is None or old in (None, 0):
        return None
    return (now / old - 1) * 100


def point_change(now: float | None, old: float | None) -> float | None:
    if now is None or old is None:
        return None
    return now - old


def last_valid(values: list[float | None], offset: int = 0) -> float | None:
    seen = []
    for value in values:
        if value is not None and not math.isnan(value):
            seen.append(value)
    if not seen or len(seen) <= offset:
        return None
    return seen[-1 - offset]


def to_float(value: str | float | int | None) -> float | None:
    if value in (None, "", "N/A", "--"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def fetch_cnbc_quotes() -> dict:
    symbol_string = "|".join(config["symbol"] for config in QUOTE_SYMBOLS.values())
    url = (
        "https://quote.cnbc.com/quote-html-webservice/quote.htm?"
        f"symbols={urllib.parse.quote(symbol_string, safe='|.@')}&output=json"
    )
    raw = http_get(url).decode("utf-8")
    data = json.loads(raw)
    quotes = data["QuickQuoteResult"]["QuickQuote"]
    by_symbol = {quote.get("symbol"): quote for quote in quotes}
    metrics = {}

    for key, config in QUOTE_SYMBOLS.items():
        quote = by_symbol.get(config["symbol"])
        if not quote:
            raise RuntimeError(f"CNBC quote missing for {config['symbol']}")
        latest = to_float(quote.get("last"))
        previous = to_float(quote.get("previous_day_closing"))
        one_day_pct = to_float(quote.get("change_pct"))
        item = {
            "symbol": config["symbol"],
            "latest": latest,
            "previous": previous,
            "five_ago": None,
            "one_day_pct": one_day_pct if one_day_pct is not None else pct_change(latest, previous),
            "five_day_pct": None,
            "one_day_change": point_change(latest, previous),
            "five_day_change": None,
            "open": to_float(quote.get("open")),
            "date": quote.get("reg_last_time") or quote.get("last_time"),
            "source": "CNBC quote",
            **config,
        }
        item["status"], item["interpretation"] = classify_value(key, item["latest"])
        metrics[key] = item

    return metrics


def fetch_fred(series: str) -> dict:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        text = resp.read().decode("utf-8")
    rows = list(csv.DictReader(text.splitlines()))
    valid = []
    for row in rows:
        raw = row.get(series)
        if not raw or raw == ".":
            continue
        try:
            valid.append((row["observation_date"], float(raw)))
        except ValueError:
            continue
    latest_date, latest = valid[-1]
    previous = valid[-2][1] if len(valid) >= 2 else None
    five_ago = valid[-6][1] if len(valid) >= 6 else None
    return {
        "series": series,
        "latest": latest,
        "previous": previous,
        "five_ago": five_ago,
        "one_day_change": point_change(latest, previous),
        "five_day_change": point_change(latest, five_ago),
        "date": latest_date,
        "source": "FRED",
    }


def fetch_fear_greed() -> dict:
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    item = data["fear_and_greed"]
    return {
        "latest": float(item["score"]),
        "rating": item.get("rating"),
        "previous": float(item["previous_close"]),
        "one_week": float(item["previous_1_week"]),
        "one_month": float(item["previous_1_month"]),
        "one_day_change": float(item["score"]) - float(item["previous_close"]),
        "one_week_change": float(item["score"]) - float(item["previous_1_week"]),
        "date": item.get("timestamp"),
        "source": "CNN Fear & Greed",
    }


def classify_value(metric: str, value: float | None) -> tuple[str, str]:
    if value is None:
        return "unknown", "暂无数据"
    if metric == "vix":
        if value < 15:
            return "hot", "极度平静，警惕自满"
        if value < 25:
            return "neutral", "正常波动"
        if value < 30:
            return "watch", "恐慌升温，可准备分批"
        if value < 40:
            return "risk", "明显恐慌，需结合信用市场"
        return "extreme", "极端恐慌，勿一次性梭哈"
    if metric == "fear_greed":
        if value < 25:
            return "watch", "极度恐惧，关注反向机会"
        if value < 45:
            return "neutral", "偏恐惧"
        if value <= 55:
            return "neutral", "中性"
        if value <= 75:
            return "hot", "偏贪婪"
        return "risk", "极度贪婪，考虑降风险"
    if metric == "hy_spread":
        if value < 3.5:
            return "neutral", "信用压力低"
        if value < 5:
            return "watch", "信用压力抬升"
        return "risk", "信用压力高，防守优先"
    return "neutral", "参考趋势"


def add_ratio(metrics: dict, key: str, left: str, right: str, name: str) -> None:
    l_now = metrics[left]["latest"]
    r_now = metrics[right]["latest"]
    l_prev = metrics[left]["previous"]
    r_prev = metrics[right]["previous"]
    l_five = metrics[left]["five_ago"]
    r_five = metrics[right]["five_ago"]

    now = l_now / r_now if l_now and r_now else None
    prev = l_prev / r_prev if l_prev and r_prev else None
    five_ago = l_five / r_five if l_five and r_five else None
    metrics[key] = {
        "name": name,
        "latest": now,
        "previous": prev,
        "five_ago": five_ago,
        "one_day_pct": pct_change(now, prev),
        "five_day_pct": pct_change(now, five_ago),
        "one_day_change": point_change(now, prev),
        "five_day_change": point_change(now, five_ago),
        "source": f"{left.upper()} / {right.upper()}",
    }


def risk_reading(metrics: dict) -> dict:
    flags = []
    positives = []

    vix = metrics["vix"]["latest"]
    fear = metrics["fear_greed"]["latest"]
    hy_spread = metrics.get("hy_spread", {}).get("latest")
    hy_spread_5d = metrics.get("hy_spread", {}).get("five_day_change")
    hyg_5d = metrics["hyg"].get("five_day_pct")
    jnk_5d = metrics["jnk"].get("five_day_pct")
    rsp_spy_5d = metrics["rsp_spy"].get("five_day_pct")
    rsp_spy_1d = metrics["rsp_spy"].get("one_day_pct")
    iwm_spy_5d = metrics["iwm_spy"].get("five_day_pct")
    iwm_spy_1d = metrics["iwm_spy"].get("one_day_pct")
    tnx_5d = metrics["tnx"].get("five_day_change")
    tnx_1d = metrics["tnx"].get("one_day_change")
    dxy_5d = metrics["dxy"].get("five_day_pct")
    dxy_1d = metrics["dxy"].get("one_day_pct")
    gold_5d = metrics["gold"].get("five_day_pct")
    gold_1d = metrics["gold"].get("one_day_pct")

    if vix is not None and vix >= 30:
        flags.append("VIX 已进入明显恐慌区间")
    elif vix is not None and vix >= 25:
        positives.append("VIX 显示恐慌升温，可准备分批计划")

    if fear is not None and fear <= 25:
        positives.append("Fear & Greed 处于极度恐惧")
    elif fear is not None and fear >= 75:
        flags.append("Fear & Greed 处于极度贪婪")

    if hy_spread is not None and hy_spread >= 5:
        flags.append("高收益债利差处于高压区")
    if hy_spread_5d is not None and hy_spread_5d >= 0.25:
        flags.append("信用利差 5 日明显扩大")
    if (hyg_5d is not None and hyg_5d <= -1) or (jnk_5d is not None and jnk_5d <= -1):
        flags.append("高收益债 ETF 走弱，需警惕信用压力")

    breadth = rsp_spy_5d if rsp_spy_5d is not None else rsp_spy_1d
    small_caps = iwm_spy_5d if iwm_spy_5d is not None else iwm_spy_1d
    rates = tnx_5d if tnx_5d is not None else tnx_1d
    dollar = dxy_5d if dxy_5d is not None else dxy_1d
    gold = gold_5d if gold_5d is not None else gold_1d

    if breadth is not None and breadth < -0.4:
        flags.append("RSP/SPY 走弱，上涨可能集中在权重股")
    elif breadth is not None and breadth > 0.4:
        positives.append("RSP/SPY 走强，市场广度改善")

    if small_caps is not None and small_caps < -0.7:
        flags.append("IWM/SPY 走弱，小盘风险偏好不足")
    elif small_caps is not None and small_caps > 0.7:
        positives.append("IWM/SPY 走强，风险偏好扩散")

    if rates is not None and rates > 0.15:
        flags.append("10 年期美债快速上行，高估值资产承压")
    if dollar is not None and dollar > 1:
        flags.append("美元指数明显走强，全球风险偏好收缩")
    if gold is not None and gold > 2 and rates is not None and rates > 0.05:
        flags.append("黄金和美债收益率同涨，偏通胀或货币信用压力")

    if flags and len(flags) >= 3:
        regime = "防守优先"
        action = "不要急着抄底；先降杠杆、降高 beta、保留现金，等信用和美元压力缓和。"
        tone = "risk"
    elif positives and not flags:
        regime = "可分批进攻"
        action = "按计划小步加仓，优先核心资产和现金流强的公司，不一次性打满。"
        tone = "opportunity"
    elif flags:
        regime = "谨慎观察"
        action = "保持仓位纪律，先确认风险是否扩散到信用市场，再决定加仓或减仓。"
        tone = "watch"
    else:
        regime = "中性运行"
        action = "按原计划定投或再平衡，不因单日涨跌改变大方向。"
        tone = "neutral"

    return {
        "regime": regime,
        "tone": tone,
        "action": action,
        "flags": flags,
        "positives": positives,
    }


def build_snapshot() -> dict:
    metrics = {}
    errors = []

    try:
        metrics.update(fetch_cnbc_quotes())
    except Exception as exc:
        errors.append({"key": "cnbc_quotes", "error": str(exc)})

    try:
        fg = fetch_fear_greed()
        fg["name"] = "Fear & Greed"
        fg["status"], fg["interpretation"] = classify_value("fear_greed", fg["latest"])
        metrics["fear_greed"] = fg
    except Exception as exc:
        errors.append({"key": "fear_greed", "error": str(exc)})

    for key, config in FRED_SERIES.items():
        try:
            item = fetch_fred(config["series"])
            item.update(config)
            item["status"], item["interpretation"] = classify_value(key, item["latest"])
            metrics[key] = item
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})

    if "spy" in metrics and "rsp" in metrics:
        add_ratio(metrics, "rsp_spy", "rsp", "spy", "RSP/SPY 市场广度")
    if "spy" in metrics and "iwm" in metrics:
        add_ratio(metrics, "iwm_spy", "iwm", "spy", "IWM/SPY 风险扩散")

    reading = risk_reading(metrics) if len(metrics) >= 8 else {
        "regime": "数据不足",
        "tone": "unknown",
        "action": "部分数据源暂时不可用，稍后刷新。",
        "flags": [],
        "positives": [],
    }

    snapshot = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "metrics": metrics,
        "reading": reading,
        "errors": errors,
        "notes": [
            "所有行情仅用于监测和辅助判断，不构成投资建议。",
            "Yahoo/CNN/FRED/Stooq 等公共数据源可能延迟或短暂不可用。",
            "VIX、ETF、美元、黄金和 10 年期美债使用 CNBC quote；高收益债利差使用 FRED BAMLH0A0HYM2。",
        ],
    }
    DASHBOARD_DIR.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/markets"):
            try:
                snapshot = build_snapshot()
                body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        return super().do_GET()


def run_server(port: int) -> None:
    DASHBOARD_DIR.mkdir(exist_ok=True)
    if not (ROOT / "index.html").exists():
        raise SystemExit("缺少 index.html")
    build_snapshot()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"跨资产仪表盘已启动: http://127.0.0.1:{port}/")
        print("按 Ctrl+C 停止。")
        httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="跨资产监测仪表盘")
    parser.add_argument("--snapshot", action="store_true", help="只抓取一次数据并写入 JSON")
    parser.add_argument("--port", type=int, default=PORT, help="本地服务端口")
    args = parser.parse_args()

    if args.snapshot:
        snapshot = build_snapshot()
        print(json.dumps({
            "generated_at": snapshot["generated_at"],
            "regime": snapshot["reading"]["regime"],
            "action": snapshot["reading"]["action"],
            "errors": snapshot["errors"],
            "snapshot": str(CACHE_PATH),
        }, ensure_ascii=False, indent=2))
        return

    run_server(args.port)


if __name__ == "__main__":
    main()
