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

GitHub Pages 读取根目录的 `market_snapshot.json`。本仓库已经配置 GitHub Actions，会定时抓取数据并更新这个文件。

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
