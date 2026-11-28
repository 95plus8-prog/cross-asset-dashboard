const QUOTE_SYMBOLS = {
  vix: { symbol: ".VIX", name: "VIX 恐慌指数", format: "number" },
  spy: { symbol: "SPY", name: "SPY 标普500", format: "price" },
  rsp: { symbol: "RSP", name: "RSP 等权标普500", format: "price" },
  iwm: { symbol: "IWM", name: "IWM 小盘股", format: "price" },
  hyg: { symbol: "HYG", name: "HYG 高收益债", format: "price" },
  jnk: { symbol: "JNK", name: "JNK 高收益债", format: "price" },
  tnx: { symbol: "US10Y", name: "10 年期美债收益率", format: "yield" },
  dxy: { symbol: ".DXY", name: "美元指数 DXY", format: "number" },
  gold: { symbol: "@GC.1", name: "黄金期货", format: "price" },
};

const FRED_SERIES = {
  hy_spread: {
    series: "BAMLH0A0HYM2",
    name: "高收益债利差",
    unit: "%",
  },
};

const EXPIRE_HOURS = 72; // 沿用旧值超过这个时间，就不再参与判断
const KEY_METRICS = ["vix", "hy_spread"]; // 缺任何一项，都不给出偏乐观的结论

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123 Safari/537.36";

function toFloat(value) {
  if (value === null || value === undefined || value === "" || value === "N/A" || value === "--") {
    return null;
  }
  const parsed = Number(String(value).replaceAll(",", "").replace("%", ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function pctChange(now, old) {
  if (now === null || now === undefined || !old) return null;
  return (now / old - 1) * 100;
}

function pointChange(now, old) {
  if (now === null || now === undefined || old === null || old === undefined) return null;
  return now - old;
}

async function fetchText(url, headers = {}, timeoutMs = 9000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const res = await fetch(url, {
    signal: controller.signal,
    headers: {
      "user-agent": UA,
      accept: "application/json,text/plain,*/*",
      ...headers,
    },
  }).finally(() => clearTimeout(timeout));
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} from ${url}`);
  }
  return res.text();
}

function classifyValue(metric, value) {
  if (value === null || value === undefined) return ["unknown", "暂无数据"];
  if (metric === "vix") {
    if (value < 15) return ["hot", "极度平静，警惕自满"];
    if (value < 25) return ["neutral", "正常波动"];
    if (value < 30) return ["watch", "恐慌升温，可准备分批"];
    if (value < 40) return ["risk", "明显恐慌，需结合信用市场"];
    return ["extreme", "极端恐慌，勿一次性梭哈"];
  }
  if (metric === "fear_greed") {
    if (value < 25) return ["watch", "极度恐惧，关注反向机会"];
    if (value < 45) return ["neutral", "偏恐惧"];
    if (value <= 55) return ["neutral", "中性"];
    if (value <= 75) return ["hot", "偏贪婪"];
    return ["risk", "极度贪婪，考虑降风险"];
  }
  if (metric === "hy_spread") {
    if (value < 3.5) return ["neutral", "信用压力低"];
    if (value < 5) return ["watch", "信用压力抬升"];
    return ["risk", "信用压力高，防守优先"];
  }
  return ["neutral", "参考趋势"];
}

async function fetchCnbcQuotes() {
  const symbolString = Object.values(QUOTE_SYMBOLS).map((item) => item.symbol).join("|");
  const url = `https://quote.cnbc.com/quote-html-webservice/quote.htm?symbols=${encodeURIComponent(symbolString).replaceAll("%7C", "|")}&output=json`;
  const data = JSON.parse(await fetchText(url));
  const quotes = data.QuickQuoteResult.QuickQuote;
  const bySymbol = Object.fromEntries(quotes.map((quote) => [quote.symbol, quote]));
  const metrics = {};
  const errors = [];

  for (const [key, config] of Object.entries(QUOTE_SYMBOLS)) {
    const quote = bySymbol[config.symbol] ?? {};
    const latest = toFloat(quote.last);
    if (latest === null) {
      errors.push({ key, error: `CNBC quote missing for ${config.symbol}` });
      continue;
    }
    const previous = toFloat(quote.previous_day_closing);
    const oneDayPct = toFloat(quote.change_pct);
    const [status, interpretation] = classifyValue(key, latest);
    metrics[key] = {
      symbol: config.symbol,
      latest,
      previous,
      five_ago: null,
      one_day_pct: oneDayPct ?? pctChange(latest, previous),
      five_day_pct: null,
      one_day_change: pointChange(latest, previous),
      five_day_change: null,
      open: toFloat(quote.open),
      date: quote.reg_last_time || quote.last_time,
      source: "CNBC quote",
      ...config,
      status,
      interpretation,
    };
  }

  return { metrics, errors };
}

async function fetchFearGreed() {
  const today = new Date().toISOString().slice(0, 10);
  const url = `https://production.dataviz.cnn.io/index/fearandgreed/graphdata/${today}`;
  const data = JSON.parse(
    await fetchText(url, {
      referer: "https://www.cnn.com/markets/fear-and-greed",
    }),
  );
  const item = data.fear_and_greed;
  const latest = toFloat(item.score);
  if (latest === null) throw new Error("CNN 没有返回有效的 Fear & Greed 分数");
  const previous = toFloat(item.previous_close);
  const oneWeek = toFloat(item.previous_1_week);
  const [status, interpretation] = classifyValue("fear_greed", latest);
  return {
    latest,
    rating: item.rating,
    previous,
    one_week: oneWeek,
    one_month: toFloat(item.previous_1_month),
    one_day_change: pointChange(latest, previous),
    one_week_change: pointChange(latest, oneWeek),
    date: item.timestamp,
    source: "CNN Fear & Greed",
    name: "Fear & Greed",
    status,
    interpretation,
  };
}

function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const headers = lines.shift().split(",");
  return lines.map((line) => {
    const values = line.split(",");
    return Object.fromEntries(headers.map((header, index) => [header, values[index]]));
  });
}

async function fetchFred(series) {
  const url = `https://fred.stlouisfed.org/graph/fredgraph.csv?id=${series}`;
  const rows = parseCsv(await fetchText(url, { accept: "text/csv,*/*" }, 3500));
  const valid = rows
    .map((row) => [row.observation_date, toFloat(row[series])])
    .filter(([, value]) => value !== null);
  if (!valid.length) throw new Error(`FRED 没有返回 ${series} 的有效数据`);
  const [date, latest] = valid.at(-1);
  const previous = valid.at(-2)?.[1] ?? null;
  const fiveAgo = valid.at(-6)?.[1] ?? null;
  return {
    series,
    latest,
    previous,
    five_ago: fiveAgo,
    one_day_change: pointChange(latest, previous),
    five_day_change: pointChange(latest, fiveAgo),
    date,
    source: "FRED",
  };
}

function parseDay(value) {
  const day = String(value ?? "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const parsed = new Date(`${day}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().startsWith(day) ? day : null;
}

const isWeekday = (date) => date.getUTCDay() !== 0 && date.getUTCDay() !== 6;

// (start, end] 之间的工作日数目
function weekdaysBetween(start, end) {
  let count = 0;
  const cursor = new Date(`${start}T00:00:00Z`);
  const last = new Date(`${end}T00:00:00Z`);
  while (cursor < last) {
    cursor.setUTCDate(cursor.getUTCDate() + 1);
    if (isWeekday(cursor)) count += 1;
  }
  return count;
}

// 只读取已发布的历史；历史由 Python 程序维护
export function applyHistory(metrics, history) {
  for (const key of Object.keys(QUOTE_SYMBOLS)) {
    const item = metrics[key];
    if (!item || item.stale) continue;
    const day = parseDay(item.date);
    if (!day) continue;
    const series = history[key] ?? {};
    // 周六的报价属于周五交易日，周日晚的期货报价属于周一交易日
    const date = new Date(`${day}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + ({ 6: -1, 0: 1 }[date.getUTCDay()] ?? 0));
    const session = date.toISOString().slice(0, 10);
    const earlier = Object.keys(series).filter((d) => d < session).sort();
    if (earlier.length < 5) continue;
    const ref = earlier.at(-5);
    // 5 个交易日，最多容许 1 个假期
    if (weekdaysBetween(ref, session) > 6) continue;
    item.five_ago = series[ref];
    item.five_day_pct = pctChange(item.latest, series[ref]);
    item.five_day_change = pointChange(item.latest, series[ref]);
  }
}

// 抓取失败的项目沿用上次成功的值，并清楚标记
export function carryForward(metrics, previous, now) {
  const oldMetrics = previous?.metrics ?? {};
  for (const key of [...Object.keys(QUOTE_SYMBOLS), "fear_greed", ...Object.keys(FRED_SERIES)]) {
    const old = oldMetrics[key];
    if (metrics[key] || !old || old.latest === null || old.latest === undefined) continue;
    // 旧版写死的后备数值没有可靠的抓取时间，不沿用
    if (String(old.source ?? "").includes("fallback")) continue;
    const { fallback, ...item } = old;
    item.stale = true;
    item.fetched_at = old.fetched_at || previous.generated_at;
    const age = now.getTime() - Date.parse(item.fetched_at);
    item.expired = !(age <= EXPIRE_HOURS * 3600 * 1000);
    if (item.expired) Object.assign(item, { status: "unknown", interpretation: "数据已过期" });
    metrics[key] = item;
  }
}

export function addRatio(metrics, key, left, right, name) {
  const ratio = (a, b) => (a && b ? a / b : null);
  const l = metrics[left];
  const r = metrics[right];
  const now = ratio(l.latest, r.latest);
  const previous = ratio(l.previous, r.previous);
  const fiveAgo = ratio(l.five_ago, r.five_ago);
  metrics[key] = {
    name,
    latest: now,
    previous,
    five_ago: fiveAgo,
    one_day_pct: pctChange(now, previous),
    five_day_pct: pctChange(now, fiveAgo),
    one_day_change: pointChange(now, previous),
    five_day_change: pointChange(now, fiveAgo),
    source: `${left.toUpperCase()} / ${right.toUpperCase()}`,
  };
  if (l.stale || r.stale) {
    metrics[key].stale = true;
    // 新旧数据来自不同交易日时，比率没有意义
    const mixed = parseDay(l.date) !== parseDay(r.date);
    metrics[key].expired = Boolean(mixed || l.expired || r.expired);
  }
}

export function riskReading(metrics) {
  const flags = [];
  const positives = [];
  let score = 0;

  const get = (key, field = "latest") => {
    const item = metrics[key];
    return !item || item.expired ? null : item[field] ?? null;
  };
  const prefer5d = (key, five, one) => get(key, five) ?? get(key, one);
  const has = (value) => value !== null;
  const flag = (text, weight = 1) => {
    flags.push(text);
    score += weight;
  };

  const vix = get("vix");
  const fear = get("fear_greed");
  const hySpread = get("hy_spread");
  const hySpread5d = get("hy_spread", "five_day_change");
  const hyg5d = get("hyg", "five_day_pct");
  const jnk5d = get("jnk", "five_day_pct");

  if (has(vix) && vix >= 40) flag("VIX 处于极端恐慌区间", 3);
  else if (has(vix) && vix >= 30) flag("VIX 已进入明显恐慌区间", 2);
  else if (has(vix) && vix >= 25) positives.push("VIX 显示恐慌升温，可准备分批计划");

  if (has(fear) && fear < 25) positives.push("Fear & Greed 处于极度恐惧");
  else if (has(fear) && fear > 75) flag("Fear & Greed 处于极度贪婪");

  if (has(hySpread) && hySpread >= 5) flag("高收益债利差处于高压区", 3);
  if (has(hySpread5d) && hySpread5d >= 0.25) flag("信用利差 5 日明显扩大");
  if ((has(hyg5d) && hyg5d <= -1) || (has(jnk5d) && jnk5d <= -1)) flag("高收益债 ETF 走弱，需警惕信用压力");

  const breadth = prefer5d("rsp_spy", "five_day_pct", "one_day_pct");
  const smallCaps = prefer5d("iwm_spy", "five_day_pct", "one_day_pct");
  const rates = prefer5d("tnx", "five_day_change", "one_day_change");
  const dollar = prefer5d("dxy", "five_day_pct", "one_day_pct");
  const gold = prefer5d("gold", "five_day_pct", "one_day_pct");

  if (has(breadth) && breadth < -0.4) flag("RSP/SPY 走弱，上涨可能集中在权重股");
  else if (has(breadth) && breadth > 0.4) positives.push("RSP/SPY 走强，市场广度改善");

  if (has(smallCaps) && smallCaps < -0.7) flag("IWM/SPY 走弱，小盘风险偏好不足");
  else if (has(smallCaps) && smallCaps > 0.7) positives.push("IWM/SPY 走强，风险偏好扩散");

  if (has(rates) && rates > 0.15) flag("10 年期美债快速上行，高估值资产承压");
  if (has(dollar) && dollar > 1) flag("美元指数明显走强，全球风险偏好收缩");
  if (has(gold) && gold > 2 && has(rates) && rates > 0.05) flag("黄金和美债收益率同涨，偏通胀或货币信用压力");

  const missing = KEY_METRICS.some((key) => !has(get(key)));
  const result = (regime, tone, action) => ({ regime, tone, action, flags, positives });

  if (score >= 3) {
    return result("防守优先", "risk", "不要急着抄底；先降杠杆、降高 beta、保留现金，等信用和美元压力缓和。");
  }
  if (missing) {
    return result("数据不足", "unknown", "关键数据（VIX 或高收益债利差）缺失或过期，暂不下结论。");
  }
  if (flags.length) {
    return result("谨慎观察", "watch", "保持仓位纪律，先确认风险是否扩散到信用市场，再决定加仓或减仓。");
  }
  if (positives.length >= 2) {
    return result("可分批进攻", "opportunity", "按计划小步加仓，优先核心资产和现金流强的公司，不一次性打满。");
  }
  return result("中性运行", "neutral", "按原计划定投或再平衡，不因单日涨跌改变大方向。");
}

// 读取随站点发布的 JSON；读不到就当作没有
async function fetchPublished(origin, file) {
  if (!origin) return {};
  try {
    return JSON.parse(await fetchText(`${origin}/${file}`, {}, 2500)) ?? {};
  } catch {
    return {};
  }
}

export async function buildSnapshot(origin) {
  const metrics = {};
  const errors = [];
  const now = new Date();
  const published = Promise.all([
    fetchPublished(origin, "market_snapshot.json"),
    fetchPublished(origin, "market_history.json"),
  ]);

  const jobs = [
    ["cnbc_quotes", fetchCnbcQuotes()],
    ["fear_greed", fetchFearGreed()],
    ...Object.entries(FRED_SERIES).map(([key, config]) => [
      key,
      fetchFred(config.series).then((item) => {
        const [status, interpretation] = classifyValue(key, item.latest);
        return { ...item, ...config, status, interpretation };
      }),
    ]),
  ];

  const results = await Promise.allSettled(jobs.map(([, job]) => job));
  jobs.forEach(([key], index) => {
    const result = results[index];
    if (result.status !== "fulfilled") {
      errors.push({ key, error: result.reason?.message || String(result.reason) });
    } else if (key === "cnbc_quotes") {
      Object.assign(metrics, result.value.metrics);
      errors.push(...result.value.errors);
    } else {
      metrics[key] = result.value;
    }
  });

  if (!Object.keys(metrics).length) {
    throw new Error("所有数据源都失败：" + errors.map((e) => e.error).join("; "));
  }
  for (const item of Object.values(metrics)) item.fetched_at = now.toISOString();

  const [previous, history] = await published;
  applyHistory(metrics, history);
  carryForward(metrics, previous, now);

  if (metrics.spy && metrics.rsp) addRatio(metrics, "rsp_spy", "rsp", "spy", "RSP/SPY 市场广度");
  if (metrics.spy && metrics.iwm) addRatio(metrics, "iwm_spy", "iwm", "spy", "IWM/SPY 风险扩散");

  return {
    generated_at: now.toISOString(),
    metrics,
    reading: riskReading(metrics),
    errors,
    notes: [
      "所有行情仅用于监测和辅助判断，不构成投资建议。",
      "CNBC/CNN/FRED 等公共数据源可能延迟或短暂不可用。",
      "VIX、ETF、美元、黄金和 10 年期美债使用 CNBC quote；高收益债利差使用 FRED BAMLH0A0HYM2。",
      "行情的 5 日变化按本项目每个交易日最后一次抓取的价格估算，不一定等于收市价。",
    ],
  };
}

export default async (request) => {
  try {
    const origin = request ? new URL(request.url).origin : null;
    return Response.json(await buildSnapshot(origin), {
      headers: {
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 502 });
  }
};

export const config = {
  path: "/api/markets",
};
