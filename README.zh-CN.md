<div align="center">

# 数智分析 Pro · SIDA Pro

**用回测检验想法，让 AI 拿数据说话，把盯盘交给提醒。**

[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-red.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-v0.11.29-blue) <!-- Keep aligned with VERSION. -->

[English](README.md) · [核心能力](#核心能力) · [操作演示](#操作演示) · [开始使用](#开始使用) · [开发者 API](https://sida.win/developers)

</div>

SIDA Pro 是一套可自部署的 **A 股量化投研工作台**。盘前找主线和候选股，用因子检验、历史回测和模拟盘验证想法；盘中问数智参谋，回看它调了什么工具、查了哪些数据；走势触发预警条件时，由已配置的渠道送达。外部 Agent 或脚本也可通过 **Skill API** 接入，不必打开网页。

> **试着问：「这只股的资金、筹码和走势，能不能互相印证？」** 数智参谋可以结合当前标的调用投研工具。你可以展开运行过程，核对工具参数、状态和耗时，再回到原数据判断。

![数智参谋：展开已有会话的投研工具调用记录](frontend/public/media/product/assistant-process.webp)

> 截图与视频录制于 **2026-10-27 · v0.82.2**，来自真实本地浏览器。画面是当时的数据与配置快照，不是实时行情或实盘业绩。缺失数据、未配置模型和未连接的可选服务均如实保留。

## 操作演示

项目仓库：[SIDA Pro](https://github.com/xiaoze-hub/sida-pro)。免费自行部署请前往[社区开源版 Stock-Intelligent-Data-Analytics](https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics)，按仓库说明安装。社区版采用 AGPL-3.0，其功能与发布节奏和当前 Pro 实例分别维护。

[![操作预览，点击打开行情演示视频](frontend/public/media/product/preview.gif)](frontend/public/media/product/market.mp4)

**点击预览图打开 MP4 视频。** 官网落地页内置封面、播放器、中文字幕与文字步骤；GitHub 可能以文件形式打开仓库视频，不保证直接内嵌播放。

| 演示 | 操作内容 | 视频 / 字幕 |
| --- | --- | --- |
| 行情工作台 · 31 秒 | 打开持仓 / 关注列表，切换 K 线周期与 MACD 指标 | [MP4](frontend/public/media/product/market.mp4) · [VTT](frontend/public/media/product/market.zh.vtt) |
| 数智参谋 · 25 秒 | 重开已有会话，展开工具步骤并查看运行面板 | [MP4](frontend/public/media/product/assistant.mp4) · [VTT](frontend/public/media/product/assistant.zh.vtt) |
| 系统状态 · 18 秒 | 查看模块连接，打开数据库详情并刷新快照 | [MP4](frontend/public/media/product/system.mp4) · [VTT](frontend/public/media/product/system.zh.vtt) |
| 手机助手 · 16 秒 | 全屏对话、默认单行输入与回车换行 | [MP4](frontend/public/media/product/mobile.mp4) · [VTT](frontend/public/media/product/mobile.zh.vtt) |

AI 短片回放已有工具历史，没有生成或暗示新的 AI 结论；手机短片只编辑草稿，没有提交分析请求。录制范围、限制与可复用推广文案见[媒体说明](docs/PRODUCT_DEMO.md)。

## 核心能力

| 模块 | 用途 |
| --- | --- |
| **市场扫描** | 指数与 ETF、板块热力、题材情绪、涨停梯队、集合竞价、机会扫描。 |
| **个股与资金证据** | 个股行情、技术指标与筹码、基本面和消息、主力意图、资金口径对照、龙虎榜席位、机构季度披露持仓。 |
| **量化与策略验证** | 因子 IC、IR 和样本外检验；通过回测 API 跑内置均线金叉策略，调整持有期和止盈止损，输出交易明细、净值曲线及指标。另有模拟盘和决策账本。 |
| **AI 分析与原生 Agent** | 数智参谋读取当前标的与工作台上下文，按问题调用投研工具，或执行竞价、题材、持仓风控等专用工作流；调用记录可回看，建议的图表标注需用户确认。 |
| **预测实验** | 可选预测引擎提供模型对照和历史验证，与策略回测、模拟盘分开查看。 |
| **持仓与交易复盘** | 分账号管理持仓和关注列表，导入影子账户交割单，查看分析报告与历史记录。 |
| **多渠道推送** | 用调度与订阅投递报告或预警，支持站内通知、个人微信、企业微信、Telegram 等渠道；可用性取决于部署和账号配置。 |
| **Skill 调用** | 查看 [Skill 目录](https://sida.win/api/skills/catalog)，通过 HTTP 调用行情、主力意图、技术分析等能力；认证、权限与限额见[开发者文档](https://sida.win/developers)。 |
| **部署与治理** | 多用户权限隔离、模型与渠道配置、Agent 运行记录与用量、数据源和依赖健康状态。 |

回测目前通过 API 提供，不是可视化策略编辑器。模拟盘是模拟撮合，不是实盘下单；回测和 AI 分析不构成收益承诺或投资建议。

### 界面截图

| 市场全景 | 个股工作台 |
| --- | --- |
| ![市场全景](frontend/public/media/product/dashboard-dark.webp) | ![个股研究工作台](frontend/public/media/product/workbench-desktop.webp) |
| **系统连接状态** | **分类设置** |
| ![系统状态](frontend/public/media/product/system-light.webp) | ![设置](frontend/public/media/product/settings-light.webp) |

<details>
<summary>查看手机助手界面</summary>

<img src="frontend/public/media/product/mobile-assistant.webp" width="320" alt="手机全屏助手与单行输入工具栏" />

</details>

## 数据与能力边界

正式官网：[sida.win](https://sida.win/)。公开首页、开发者文档与账户方案共用服务端 SEO 元信息、分享预览与三页 sitemap；自部署可用 `SIDA_PUBLIC_SITE_URL` 指定自己的公开域名，私有实例设 `SIDA_SEO_INDEXABLE=false` 关闭索引。本地 HTML 与工作区功能页不参与索引，配置示例见 `.env.example`，主 Compose 服务已透传。

- **口径明确区分。** 腾讯逐笔意图、东财按单金额四档和 TQ L2 指标的定义不同，不能把它们当作可互换的方向性信号。使用时保留来源、口径和基准日。
- **缺失就是缺失。** 不可用的字段显示 `--` / 无数据，不能用猜测补齐。
- **AI 需要配置。** 先配置模型与服务商再使用 AI。分析取决于模型和可取得的数据，不承诺准确率或收益。
- **可选能力需单独准备。** 预测引擎、通知投递、专有数据网关和自动调度各有配置要求；主服务健康不等于所有业务源可用。
- **用户上下文隔离。** 持仓、关注列表、会话与个人通知配置按账号管理。

## 开始使用

按环境选择运行方式。**生产数据库采用 PostgreSQL / TimescaleDB，SQLite 仅供本地开发与隔离单测。** Redis 用于共享缓存与协调。

| 场景 | 操作说明 |
| --- | --- |
| 已有实例 | 打开实例首页查看演示并登录；注册是否开放以管理员设置为准。 |
| 本地 Docker 开发 | 按[本地运行手册](docs/knowledge-base/local-docker.md)准备应用、PostgreSQL、Redis 与源码挂载。手册中的 arm64 缓存基底有机器要求，其他环境需先核对镜像。 |
| 标准部署 | 先核对[架构与环境说明](docs/knowledge-base/architecture.md)、`.env.example` 和 `docker-compose.yml`，配置认证、数据库、缓存与所需服务。 |
| 源码开发 | 准备数据库与 `.env` 后，在两个终端分别运行以下命令。 |

```bash
# 后端 :8000，自动创建虚拟环境并安装依赖
make dev-api

# 前端 :5183，自动安装依赖并启动 Vite
make dev-web

# 检查演示素材、TypeScript 并构建前端
pnpm --dir frontend build
```

日常源码修改使用热更新或重建前端；打包依赖、镜像内容或正式发布变化时再构建 Docker。常规更新不删除数据卷。

## 架构与技术整合

React + TypeScript + Vite → FastAPI → PostgreSQL / TimescaleDB + Redis。采集器提供市场数据，Agent 与 Skill 接口组织分析；模型服务与通知渠道独立配置。

项目整合 [TradingAgents](https://github.com/TauricResearch/TradingAgents)、[Kronos](https://github.com/shiyu-coder/Kronos)、[Chronos](https://github.com/amazon-science/chronos-forecasting)、XGBoost 与 TA-Lib 等能力，可用性取决于引擎安装、运行环境和数据。图表使用 [Lightweight Charts](https://github.com/tradingview/lightweight-charts)，可选可观测性编排见 `docker-compose.infra.yml`。

- [项目知识库](docs/knowledge-base/README.md) · [架构说明](docs/knowledge-base/architecture.md)
- [UI 设计准则](docs/UI_GUIDELINES.md) · [演示素材与推广文案](docs/PRODUCT_DEMO.md)
- [通达信公式包](docs/tq-formulas/README.md)：安装步骤、参数与相对 SIDA 实现的差异说明

## 免责声明

本项目用于技术研究。AI 输出、图表标注、预测与报告均仅供参考，**不构成投资建议或股票推荐**，不保证收益。行情可能延迟、缺失或不可用。市场有风险，投资决策由使用者自行负责。

---

## 使用支持

使用说明、部署问题、问题反馈或申请 API Key（市场数据接口）遇到问题，欢迎加入微信群交流：

<img src="./assets/wechat-group-qr.png" width="200" alt="微信交流群二维码" />

> 二维码失效后，请添加微信 `winter1920977` 联系。

---

## 赞助

如果 SIDA 对你有帮助，请作者喝杯咖啡 ☕ — 你的支持让项目持续下去！

| 方式 | 入口 |
|:---:|:---:|
| **微信赞赏** | <img src="./assets/sponsor-wechat.png" width="200" alt="微信赞赏二维码" /> |

> 觉得不错的话，点一下右上角 ⭐ **Star** 支持一下吧。
