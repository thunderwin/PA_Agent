# PA Agent Web —— 纯静态工具版

网址（Cloudflare Workers 静态资源）：<https://pa-agent-web.super-hall-4845.workers.dev>

## 它是什么

把桌面的 PA Agent 做成**纯前端工具**：整站只有静态文件，没有任何后端。

- **密钥只存在你自己的浏览器里**（localStorage），直接发给 OKX 和你配置的 AI 接口，不经过任何服务器；
- **分析引擎是真的**：通过 Pyodide 在浏览器里跑仓库里原本的 Python 代码
  （提示词组装、两阶段编排、校验、以损定量），不是用 JS 重写的简化版；
- OKX 行情、账户、下单全部走 OKX 官方接口（已确认开放 CORS），
  下单签名用浏览器 WebCrypto 在本地完成。

## 怎么用

1. 打开网址 → 右上「设置」里填 **AI 模型**（Base URL / 模型名 / API Key）→ 保存
2. 顶部选品种（如 `BTC-USDT-SWAP`、`XAU-USDT-SWAP`）与周期 → **获取数据** → **提交分析**
3. 想下单就在「设置」里补 OKX API Key / Secret / Passphrase（默认勾选模拟盘）→ 决策出来后点 **执行下单**
4. 「多品种」页可以加监控列表：勾选自动监控后，每个品种只在**自己出现新的收盘 K 线**时跑一次分析

> 首次打开需要下载 Pyodide 运行时（约 10MB，来自 jsDelivr CDN），之后会被浏览器缓存。

## 与桌面版的差异

| 能力 | 桌面版 | Web 版 |
| --- | --- | --- |
| 分析引擎（提示词/校验/决策） | ✅ | ✅ 同一套代码 |
| 以损定量下单 | ✅ | ✅ 同一套代码 |
| 密钥存放 | 本机文件/环境变量 | 本机浏览器 localStorage |
| MT5 数据源 | ✅（需本机终端） | ❌ |
| 复用本机 IDE 登录态的免费模型路线 | ✅ | ❌（需要自己的 API Key） |
| 分析记录落盘（records/、trade_records/） | ✅ | ❌（暂只在内存） |
| 手机/换电脑可用 | ❌ | ✅ |

## 二次开发 / 重新部署

```bash
cd web
python3 build.py                      # 生成 dist/（静态站 + 打包好的 Python 引擎）
CLOUDFLARE_ACCOUNT_ID=<你的账号ID> wrangler deploy
```

本地预览：`python3 -m http.server 8811 --directory dist`，然后打开 <http://127.0.0.1:8811>。

改动引擎代码后重新 `build.py` 再部署即可 —— `build.py` 会自动把
`pa_agent/**/*.py`（跳过 GUI、MT5）与 `prompt_engineering/*.txt` 复制进 `dist/engine/repo/`。
