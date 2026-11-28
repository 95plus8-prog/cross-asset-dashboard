#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import socketserver
import time
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "cross_asset_dashboard"
CACHE_PATH = DASHBOARD_DIR / "market_snapshot.json"
PUBLISHED_PATH = ROOT / "market_snapshot.json"
HISTORY_PATH = ROOT / "market_history.json"
PORT = 8765

RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0
EXPIRE_HOURS = 72  # 沿用旧值超过这个时间，就不再参与判断
HISTORY_DAYS = 40
KEY_METRICS = ("vix", "hy_spread")  # 缺任何一项，都不给出偏乐观的结论

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


class NoDataError(RuntimeError):
    """所有数据源都失败。"""


def with_retry(fn):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception:
            if attempt == RETRY_ATTEMPTS:
                raise
            time.sleep(RETRY_DELAY * attempt)


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


def to_float(value: str | float | int | None) -> float | None:
    if value in (None, "", "N/A", "--"):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def fetch_cnbc_quotes() -> tuple[dict, list[dict]]:
    """逐个代号解析：个别代号缺失时，保留其余数据。"""
    symbol_string = "|".join(config["symbol"] for config in QUOTE_SYMBOLS.values())
    url = (
        "https://quote.cnbc.com/quote-html-webservice/quote.htm?"
        f"symbols={urllib.parse.quote(symbol_string, safe='|.@')}&output=json"
    )
    data = with_retry(lambda: json.loads(http_get(url).decode("utf-8")))
    quotes = data["QuickQuoteResult"]["QuickQuote"]
    by_symbol = {quote.get("symbol"): quote for quote in quotes}
    metrics = {}
    errors = []

    for key, config in QUOTE_SYMBOLS.items():
        quote = by_symbol.get(config["symbol"]) or {}
        latest = to_float(quote.get("last"))
        if latest is None:
            errors.append({"key": key, "error": f"CNBC quote missing for {config['symbol']}"})
            continue
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

    return metrics, errors


def fetch_fred(series: str) -> dict:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

    def download() -> str:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode("utf-8")

    text = with_retry(download)
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
    if not valid:
        raise ValueError(f"FRED 没有返回 {series} 的有效数据")
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
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    url = f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{today}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.cnn.com/markets/fear-and-greed",
        },
    )

    def download() -> dict:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    item = with_retry(download)["fear_and_greed"]
    score = to_float(item.get("score"))
    if score is None:
        raise ValueError("CNN 没有返回有效的 Fear & Greed 分数")
    previous = to_float(item.get("previous_close"))
    one_week = to_float(item.get("previous_1_week"))
    return {
        "latest": score,
        "rating": item.get("rating"),
        "previous": previous,
        "one_week": one_week,
        "one_month": to_float(item.get("previous_1_month")),
        "one_day_change": point_change(score, previous),
        "one_week_change": point_change(score, one_week),
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
    parts = (metrics[left], metrics[right])
    if any(p.get("stale") for p in parts):
        metrics[key]["stale"] = True
        # 新旧数据来自不同交易日时，比率没有意义
        mixed = parse_day(parts[0].get("date")) != parse_day(parts[1].get("date"))
        metrics[key]["expired"] = mixed or any(p.get("expired") for p in parts)


def parse_day(value: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def weekdays_between(start: dt.date, end: dt.date) -> int:
    """(start, end] 之间的工作日数目。"""
    days = (end - start).days
    return sum((start + dt.timedelta(n)).weekday() < 5 for n in range(1, days + 1))


def apply_history(metrics: dict, history: dict) -> None:
    """记录每个交易日最后一次抓到的价格，并据此计算 5 日变化。

    只处理本次成功抓取的行情。历史缺口过大时宁可留空。
    """
    for key in QUOTE_SYMBOLS:
        item = metrics.get(key)
        if not item or item.get("stale"):
            continue
        day = parse_day(item.get("date"))
        if day is None:
            continue
        series = history.setdefault(key, {})
        earlier = sorted(d for d in series if d < day.isoformat())
        if len(earlier) >= 5:
            ref = earlier[-5]
            # 5 个交易日，最多容许 1 个假期
            if weekdays_between(dt.date.fromisoformat(ref), day) <= 6:
                five_ago = series[ref]
                item["five_ago"] = five_ago
                item["five_day_pct"] = pct_change(item["latest"], five_ago)
                item["five_day_change"] = point_change(item["latest"], five_ago)
        if day.weekday() < 5:
            series[day.isoformat()] = item["latest"]
        history[key] = dict(sorted(series.items())[-HISTORY_DAYS:])


def carry_forward(metrics: dict, previous: dict, now: dt.datetime) -> None:
    """抓取失败的项目沿用上次成功的值，并清楚标记。"""
    old_metrics = previous.get("metrics", {})
    for key in [*QUOTE_SYMBOLS, "fear_greed", *FRED_SERIES]:
        old = old_metrics.get(key)
        if key in metrics or not old or old.get("latest") is None:
            continue
        # 旧版 Netlify 写死的后备数值没有可靠的抓取时间，不沿用
        if "fallback" in str(old.get("source", "")):
            continue
        item = {k: v for k, v in old.items() if k != "fallback"}
        item["stale"] = True
        item["fetched_at"] = old.get("fetched_at") or previous.get("generated_at")
        try:
            age = now - dt.datetime.fromisoformat(item["fetched_at"].replace("Z", "+00:00"))
            item["expired"] = age > dt.timedelta(hours=EXPIRE_HOURS)
        except (AttributeError, TypeError, ValueError):
            item["expired"] = True
        if item["expired"]:
            item["status"], item["interpretation"] = "unknown", "数据已过期"
        metrics[key] = item


def risk_reading(metrics: dict) -> dict:
    flags = []
    positives = []
    score = 0

    def get(key: str, field: str = "latest") -> float | None:
        item = metrics.get(key) or {}
        return None if item.get("expired") else item.get(field)

    def prefer_5d(key: str, five: str, one: str) -> float | None:
        value = get(key, five)
        return value if value is not None else get(key, one)

    def flag(text: str, weight: int = 1) -> None:
        nonlocal score
        flags.append(text)
        score += weight

    vix = get("vix")
    fear = get("fear_greed")
    hy_spread = get("hy_spread")
    hy_spread_5d = get("hy_spread", "five_day_change")
    hyg_5d = get("hyg", "five_day_pct")
    jnk_5d = get("jnk", "five_day_pct")

    if vix is not None and vix >= 40:
        flag("VIX 处于极端恐慌区间", 3)
    elif vix is not None and vix >= 30:
        flag("VIX 已进入明显恐慌区间", 2)
    elif vix is not None and vix >= 25:
        positives.append("VIX 显示恐慌升温，可准备分批计划")

    if fear is not None and fear < 25:
        positives.append("Fear & Greed 处于极度恐惧")
    elif fear is not None and fear > 75:
        flag("Fear & Greed 处于极度贪婪")

    if hy_spread is not None and hy_spread >= 5:
        flag("高收益债利差处于高压区", 3)
    if hy_spread_5d is not None and hy_spread_5d >= 0.25:
        flag("信用利差 5 日明显扩大")
    if (hyg_5d is not None and hyg_5d <= -1) or (jnk_5d is not None and jnk_5d <= -1):
        flag("高收益债 ETF 走弱，需警惕信用压力")

    breadth = prefer_5d("rsp_spy", "five_day_pct", "one_day_pct")
    small_caps = prefer_5d("iwm_spy", "five_day_pct", "one_day_pct")
    rates = prefer_5d("tnx", "five_day_change", "one_day_change")
    dollar = prefer_5d("dxy", "five_day_pct", "one_day_pct")
    gold = prefer_5d("gold", "five_day_pct", "one_day_pct")

    if breadth is not None and breadth < -0.4:
        flag("RSP/SPY 走弱，上涨可能集中在权重股")
    elif breadth is not None and breadth > 0.4:
        positives.append("RSP/SPY 走强，市场广度改善")

    if small_caps is not None and small_caps < -0.7:
        flag("IWM/SPY 走弱，小盘风险偏好不足")
    elif small_caps is not None and small_caps > 0.7:
        positives.append("IWM/SPY 走强，风险偏好扩散")

    if rates is not None and rates > 0.15:
        flag("10 年期美债快速上行，高估值资产承压")
    if dollar is not None and dollar > 1:
        flag("美元指数明显走强，全球风险偏好收缩")
    if gold is not None and gold > 2 and rates is not None and rates > 0.05:
        flag("黄金和美债收益率同涨，偏通胀或货币信用压力")

    missing = [key for key in KEY_METRICS if get(key) is None]

    if score >= 3:
        regime = "防守优先"
        action = "不要急着抄底；先降杠杆、降高 beta、保留现金，等信用和美元压力缓和。"
        tone = "risk"
    elif missing:
        regime = "数据不足"
        action = "关键数据（VIX 或高收益债利差）缺失或过期，暂不下结论。"
        tone = "unknown"
    elif flags:
        regime = "谨慎观察"
        action = "保持仓位纪律，先确认风险是否扩散到信用市场，再决定加仓或减仓。"
        tone = "watch"
    elif len(positives) >= 2:
        regime = "可分批进攻"
        action = "按计划小步加仓，优先核心资产和现金流强的公司，不一次性打满。"
        tone = "opportunity"
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


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_previous_snapshot() -> dict:
    candidates = [read_json(CACHE_PATH), read_json(PUBLISHED_PATH)]
    return max(candidates, key=lambda s: str(s.get("generated_at", "")))


def build_snapshot() -> dict:
    metrics = {}
    errors = []
    now = dt.datetime.now(dt.timezone.utc)

    try:
        quotes, quote_errors = fetch_cnbc_quotes()
        metrics.update(quotes)
        errors.extend(quote_errors)
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

    if not metrics:
        raise NoDataError("所有数据源都失败，保留上次的快照：" + "; ".join(e["error"] for e in errors))

    for item in metrics.values():
        item["fetched_at"] = now.isoformat()

    history = read_json(HISTORY_PATH)
    apply_history(metrics, history)
    carry_forward(metrics, load_previous_snapshot(), now)

    for key, left, name in (("rsp_spy", "rsp", "RSP/SPY 市场广度"), ("iwm_spy", "iwm", "IWM/SPY 风险扩散")):
        if left in metrics and "spy" in metrics:
            add_ratio(metrics, key, left, "spy", name)

    snapshot = {
        "generated_at": now.isoformat(),
        "metrics": metrics,
        "reading": risk_reading(metrics),
        "errors": errors,
        "notes": [
            "所有行情仅用于监测和辅助判断，不构成投资建议。",
            "CNBC/CNN/FRED 等公共数据源可能延迟或短暂不可用。",
            "VIX、ETF、美元、黄金和 10 年期美债使用 CNBC quote；高收益债利差使用 FRED BAMLH0A0HYM2。",
            "行情的 5 日变化按本项目每个交易日最后一次抓取的价格估算，不一定等于收市价。",
        ],
    }
    write_json(CACHE_PATH, snapshot)
    write_json(HISTORY_PATH, history)
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
    if not (ROOT / "index.html").exists():
        raise SystemExit("缺少 index.html")
    socketserver.TCPServer.allow_reuse_address = True
    handler = partial(Handler, directory=str(ROOT))
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        print(f"跨资产仪表盘已启动: http://127.0.0.1:{port}/")
        print("按 Ctrl+C 停止。")
        httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="跨资产监测仪表盘")
    parser.add_argument("--snapshot", action="store_true", help="只抓取一次数据并写入 JSON")
    parser.add_argument("--port", type=int, default=PORT, help="本地服务端口")
    args = parser.parse_args()

    if args.snapshot:
        try:
            snapshot = build_snapshot()
        except NoDataError as exc:
            raise SystemExit(str(exc))
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
