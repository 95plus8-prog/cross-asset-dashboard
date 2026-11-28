# 跨资产仪表盘

一个可以部署到 GitHub Pages 的跨资产市场监测仪表盘。

页面监测：

- VIX 恐慌指数
- CNN Fear & Greed
- RSP/SPY 市场广度
- IWM/SPY 小盘风险偏好
- HYG/JNK 高收益债
- FRED 高收益债利差
- 10 年期美债收益率
- 美元指数
- 黄金

## 本地运行

需要 Python 3.9 或以上，不需要安装其他套件。

```bash
python3 cross_asset_dashboard.py
```

打开：

```text
http://127.0.0.1:8765/
```

## 本地生成一次静态数据

```bash
python3 cross_asset_dashboard.py --snapshot
cp cross_asset_dashboard/market_snapshot.json market_snapshot.json
```

GitHub Pages 读取根目录的 `market_snapshot.json`。本仓库已经配置 GitHub Actions，会定时抓取数据并更新这个文件。GitHub 的定时任务经常延迟或跳过，实际更新频率会低于设定的每 30 分钟。

## 5 日变化与旧数据

- `market_history.json` 记录每个交易日最后一次抓到的行情，用来估算 5 日变化（不一定等于收市价）。历史缺口过大时留空。
- 个别数据源失败时沿用上次成功的值，并标记为“旧数据”；超过 72 小时标记为过期，不再参与判断。
- 页面数据超过 12 小时没有更新时会显示警告。

## 判断规则

风险项按权重计分：VIX ≥ 40 或高收益债利差 ≥ 5 计 3 分，VIX ≥ 30 计 2 分，其他风险项计 1 分。

| 结论 | 条件 |
|---|---|
| 防守优先 | 风险分 ≥ 3 |
| 数据不足 | 未达防守条件，且 VIX 或高收益债利差缺失、过期 |
| 谨慎观察 | 有风险项 |
| 可分批进攻 | 没有风险项，且至少 2 个机会信号 |
| 中性运行 | 其他情况 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

安装了 Node.js 时，会同时检查 Netlify 函数与 Python 的判断结果是否一致。

## Netlify 部署

本仓库包含 `netlify.toml` 和 `netlify/functions/markets.mjs`。部署到 Netlify 后，页面会优先请求：

```text
/api/markets
```

这个 Netlify Function 会实时抓取 CNBC、CNN 和 FRED 数据；如果函数不可用，页面会回退读取静态 `market_snapshot.json`。

## 部署到 GitHub Pages

1. 在 GitHub 创建一个新仓库，例如 `cross-asset-dashboard`。
2. 把本目录推送到该仓库。
3. 进入仓库 `Settings -> Pages`。
4. Source 选择 `GitHub Actions`。
5. 等待 `Update market snapshot and deploy Pages` 工作流完成。

如果仓库地址是：

```text
https://github.com/<你的用户名>/cross-asset-dashboard
```

Pages 地址通常是：

```text
https://<你的用户名>.github.io/cross-asset-dashboard/
```

## 数据源

- CNBC quote：VIX、ETF、美元指数、黄金、10 年期美债。
- CNN Fear & Greed：市场情绪。
- FRED：高收益债利差。

公共数据源可能延迟或短暂不可用。本工具仅用于监测和辅助判断，不构成投资建议。
