# SIDA 产品演示与推广素材

> 基线标识：`2691c3eb64f45031cdb09637d2455551e81e2516` / VERSION `v0.82.2`，录制日期 2026-10-27。
> 对象完整路径：`/Users/winter/Documents/zicalWork/sida-pro/frontend/src/pages/Landing.tsx`、`/Users/winter/Documents/zicalWork/sida-pro/frontend/public/media/product/`、`/Users/winter/Documents/zicalWork/sida-pro/README.md`、`/Users/winter/Documents/zicalWork/sida-pro/README.zh-CN.md`。
> 覆盖：本地浏览器核心操作录屏、真实截图、官网展示、中英文 README 和可复用介绍文案；行情工作台、AI 现有会话、系统状态与手机输入。
> 未覆盖：生产发布、外部渠道投放、实时行情准确性、实盘业绩、完整模型回答、真实手机系统键盘和所有业务模块验收。

## 素材与来源

素材统一托管在 `frontend/public/media/product/`，官网路径为 `/media/product/`；README 使用同目录的相对链接。`manifest.json` 记录录制基线、文件尺寸、字节数和视频时长。原始录屏与 QA 截图位于忽略目录 `output/playwright/product-demo/`，不随发布打包。

| 素材 | 操作与实际边界 |
| --- | --- |
| `market.mp4` | 持仓 / 关注切换器、MACD、周 K / 日 K；真实行情快照，标的仅为界面演示，不代表推荐。 |
| `assistant.mp4` | 打开已有会话，展开五步工具历史与过程面板；本地当前无模型配置，未生成新的回答或补写结论。 |
| `system.mp4` | 连接图、数据库详情、刷新状态；保留待命、未配置与预测引擎未连通状态，流动非实时流量。 |
| `mobile.mp4` | 390×844 浏览器视口，打开全屏助手、输入一行、Enter 换行、清空草稿；未提交请求，未调用模型。 |
| `*-poster.webp` | 对应桌面短片的真实视频帧；手机使用 `mobile-assistant.webp` 实拍作为封面。 |
| `workbench-desktop.webp` | 1440×1100 真实深色个股研究工作台。 |
| `dashboard-dark.webp` / `assistant-process.webp` | 首页、已有工具调用过程，缺失提示与配置状态原样保留。 |
| `system-light.webp` / `settings-light.webp` | 浅色连接图与分类设置，未显示凭据或个人持仓金额。 |
| `preview.gif` | 行情片段的短循环预览，供 README 点击进入 MP4。 |
| `*.zh.vtt` | 中文字幕；官网另外提供文字步骤，视频不含配音。 |

剪辑只删除等待段，保留正常播放速度；视频采用 H.264 / yuv420p 与 faststart。截图转 WebP，不替换界面内容或造数。桌面 1440×960，手机 390×844。录屏前认证，登录口令与 token 不进入素材。

## 维护与打包

1. 更新 UI 后，用 Playwright CLI 登录后录制；公开素材只保留有授权的界面与脱敏结果。
2. 每个视频生成可读封面、字幕与文字步骤；截取视频帧时保持真实 UI。
3. 更新 `manifest.json` 中对应文件字节数、尺寸、时长和录制基线；不要只改页面路径。
4. 执行 `pnpm --dir frontend media:check`、`pnpm --dir frontend build`。构建会先验证封面、视频与字幕齐全，再由 Vite 复制 public；不得在素材生成前发布构建目录。
5. 用 GET 验证构建后的图片 / 视频 / VTT 类型与大小，再在未登录浏览器检查预览、播放、拖动与手机排版。不能仅看 public 文件存在就宣称已上线。
6. 官网首屏无需预加载视频。所有视频默认封面，点击后挂载播放器；切换演示会卸载旧播放器。浏览器播放失败时允许重试或下载。

## 推广文案

### 中文短介绍

SIDA Pro 数智分析，把市场全景、个股图表、关注列表与 AI 技术分析连接成同一个研究工作台。查看行情、切换图表、提出问题，再回看工具过程与数据口径。支持桌面、手机与自行部署。观看真实操作演示，了解产品如何组织分析，而非只看功能清单。

### English introduction

SIDA Pro brings market context, stock charts, watchlists and AI technical analysis into one research workspace. Inspect a symbol, ask a question and review the tool history and data definitions behind the process. Explore real desktop and mobile walkthroughs, with self-hosted deployment and configurable model providers.

### 发布配图组合

- 产品总览：`workbench-desktop.webp` + `market.mp4`。
- AI 工作流：`assistant-process.webp` + `assistant.mp4`，明确“已有工具历史回放”。
- 运行状态：`system-light.webp` + `system.mp4`。
- 手机体验：`mobile-assistant.webp` + `mobile.mp4`。

发布时保留“技术研究、非投资建议”，不把演示标的写成推荐，不把工具执行成功写成分析准确率，不把离线截图写成实时数据。此文案为待发布素材，本次未向外部平台发布或发送。
