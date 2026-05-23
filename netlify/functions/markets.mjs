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

  for (const [key, config] of Object.entries(QUOTE_SYMBOLS)) {
    const quote = bySymbol[config.symbol];
    if (!quote) throw new Error(`CNBC quote missing for ${config.symbol}`);
    const latest = toFloat(quote.last);
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

  return metrics;
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
  const latest = Number(item.score);
  const previous = Number(item.previous_close);
  const [status, interpretation] = classifyValue("fear_greed", latest);
  return {
    latest,
    rating: item.rating,
    previous,
    one_week: Number(item.previous_1_week),
    one_month: Number(item.previous_1_month),
    one_day_change: latest - previous,
    one_week_change: latest - Number(item.previous_1_week),
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
  const rows = parseCsv(await fetchText(url, { accept: "text/csv,*/*" }, 18000));
  const valid = rows
    .map((row) => [row.observation_date, toFloat(row[series])])
    .filter(([, value]) => value !== null);
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

function addRatio(metrics, key, left, right, name) {
  const lNow = metrics[left].latest;
  const rNow = metrics[right].latest;
  const lPrev = metrics[left].previous;
  const rPrev = metrics[right].previous;
  const now = lNow && rNow ? lNow / rNow : null;
  const previous = lPrev && rPrev ? lPrev / rPrev : null;
  metrics[key] = {
    name,
    latest: now,
    previous,
    five_ago: null,
    one_day_pct: pctChange(now, previous),
    five_day_pct: null,
    one_day_change: pointChange(now, previous),
    five_day_change: null,
    source: `${left.toUpperCase()} / ${right.toUpperCase()}`,
  };
}

function riskReading(metrics) {
  const flags = [];
  const positives = [];
  const vix = metrics.vix?.latest;
  const fear = metrics.fear_greed?.latest;
  const hySpread = metrics.hy_spread?.latest;
  const hySpread5d = metrics.hy_spread?.five_day_change;
  const breadth = metrics.rsp_spy?.one_day_pct;
  const smallCaps = metrics.iwm_spy?.one_day_pct;
  const rates = metrics.tnx?.one_day_change;
  const dollar = metrics.dxy?.one_day_pct;
  const gold = metrics.gold?.one_day_pct;

  if (vix !== undefined && vix >= 30) flags.push("VIX 已进入明显恐慌区间");
  else if (vix !== undefined && vix >= 25) positives.push("VIX 显示恐慌升温，可准备分批计划");

  if (fear !== undefined && fear <= 25) positives.push("Fear & Greed 处于极度恐惧");
  else if (fear !== undefined && fear >= 75) flags.push("Fear & Greed 处于极度贪婪");

  if (hySpread !== undefined && hySpread >= 5) flags.push("高收益债利差处于高压区");
  if (hySpread5d !== undefined && hySpread5d >= 0.25) flags.push("信用利差 5 日明显扩大");
  if (breadth !== undefined && breadth < -0.4) flags.push("RSP/SPY 走弱，上涨可能集中在权重股");
  else if (breadth !== undefined && breadth > 0.4) positives.push("RSP/SPY 走强，市场广度改善");
  if (smallCaps !== undefined && smallCaps < -0.7) flags.push("IWM/SPY 走弱，小盘风险偏好不足");
  else if (smallCaps !== undefined && smallCaps > 0.7) positives.push("IWM/SPY 走强，风险偏好扩散");
  if (rates !== undefined && rates > 0.15) flags.push("10 年期美债快速上行，高估值资产承压");
  if (dollar !== undefined && dollar > 1) flags.push("美元指数明显走强，全球风险偏好收缩");
  if (gold !== undefined && gold > 2 && rates !== undefined && rates > 0.05) {
    flags.push("黄金和美债收益率同涨，偏通胀或货币信用压力");
  }

  if (flags.length >= 3) {
    return {
      regime: "防守优先",
      tone: "risk",
      action: "不要急着抄底；先降杠杆、降高 beta、保留现金，等信用和美元压力缓和。",
      flags,
      positives,
    };
  }
  if (positives.length && !flags.length) {
    return {
      regime: "可分批进攻",
      tone: "opportunity",
      action: "按计划小步加仓，优先核心资产和现金流强的公司，不一次性打满。",
      flags,
      positives,
    };
  }
  if (flags.length) {
    return {
      regime: "谨慎观察",
      tone: "watch",
      action: "保持仓位纪律，先确认风险是否扩散到信用市场，再决定加仓或减仓。",
      flags,
      positives,
    };
  }
  return {
    regime: "中性运行",
    tone: "neutral",
    action: "按原计划定投或再平衡，不因单日涨跌改变大方向。",
    flags,
    positives,
  };
}

async function buildSnapshot() {
  const metrics = {};
  const errors = [];

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
    if (result.status === "fulfilled") {
      if (key === "cnbc_quotes") Object.assign(metrics, result.value);
      else metrics[key] = result.value;
    } else {
      errors.push({ key, error: result.reason?.message || String(result.reason) });
    }
  });

  if (metrics.spy && metrics.rsp) addRatio(metrics, "rsp_spy", "rsp", "spy", "RSP/SPY 市场广度");
  if (metrics.spy && metrics.iwm) addRatio(metrics, "iwm_spy", "iwm", "spy", "IWM/SPY 风险扩散");

  return {
    generated_at: new Date().toISOString(),
    metrics,
    reading: Object.keys(metrics).length >= 8 ? riskReading(metrics) : {
      regime: "数据不足",
      tone: "unknown",
      action: "部分数据源暂时不可用，稍后刷新。",
      flags: [],
      positives: [],
    },
    errors,
    notes: [
      "所有行情仅用于监测和辅助判断，不构成投资建议。",
      "CNBC/CNN/FRED 等公共数据源可能延迟或短暂不可用。",
      "VIX、ETF、美元、黄金和 10 年期美债使用 CNBC quote；高收益债利差使用 FRED BAMLH0A0HYM2。",
    ],
  };
}

export default async () => {
  try {
    return Response.json(await buildSnapshot(), {
      headers: {
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    return Response.json({ error: error.message }, { status: 500 });
  }
};

export const config = {
  path: "/api/markets",
};
