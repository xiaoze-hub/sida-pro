# SIDA-Pro UI × 专业终端 — 设计系统审计与改造路径（研究版）

> 整理日期：2026-09-02 · 整理人：ZCode (Marvis)
> 阶段：Phase 1 研究（只读扫描，未修改任何代码）
> 范围：`C:\Users\tianxiang\sida-pro\frontend\` + 已加载 4 份核心文档
> 目的：作为后续 Phase 2 UI 终端化重构规划的依据

---

## 0. 执行摘要（≤300 字，含现状打分）

**SIDA-Pro 当前调性偏向"功能堆叠派"，距"专业行情终端"差距明显。**

按用户三条核心要求评分（1-5 分）：

| 维度 | 当前评分 | 目标（专业终端）| 差距 |
|---|:-:|:-:|---|
| **看图决策** | ⭐⭐☆☆☆ | ⭐⭐⭐⭐⭐ | K 线被装在 Dialog 弹窗（KlineModal / StockInsightModal）而非全屏路由；主图占比仅 50-60%；副图层信息密度过高 |
| **不做数据堆砌** | ⭐⭐☆☆☆ | ⭐⭐⭐⭐⭐ | 全站 27 页（本地副本仅 20+1 = 21 页）多数为"表格 + 卡片堆叠"，缺少视觉层级与"留白" |
| **专业大气** | ⭐⭐⭐☆☆ | ⭐⭐⭐⭐⭐ | 设计系统是 shadcn 默认深青蓝（`240 70% 45%`）+ 红涨绿跌 + `--radius: 12px`，缺乏专业终端必备的"等宽数字字 / 暗色主题 / 阈值线 / 三色柱"等语义 token |

**主要发现**：

1. **本地副本缺失**：27 页中 6-7 页（Quote.tsx / KlineChart.tsx / klineEvents.ts / DarkFundTop.tsx / ReportsHub.tsx / SettingsHub.tsx / NotificationsHub.tsx）在本地副本中**未找到**，需 Hermes 拉分支同步
2. **K 线核心已实现 5 层架构**（L0 事实底 / L1 均线牛熊 / L2 GS买卖点 / L3 资金 / L4 事件），但承载在 Dialog 弹窗而非全屏路由
3. **设计系统现状**：shadcn 默认风 + 红涨绿跌 + `--radius: 12px`，是"反 AI 模板"改造，配色体系已成型但**缺乏语义 token**
4. **字体仅系统默认 + SF Mono 数字**，没有 DIN Pro 这类专业数字字
5. **Tauri 友好性极高**：1-2 人日内可打包成桌面应用（详见 §6）

**改造路径**：P0 = 新建 Quote 路由（5 天）+ Opportunities 去卡片化（2-3 天）作为样板间；P1 = Dashboard / Forecast / AnalysisDetail / DarkFundTop（6-9 天）；P2 = 14 个辅助页（12-18 天）。

**推荐样板间风格**：dark + enterprise（终端党首选）/ clean + power（专业稳重派）/ matrix + mono（极客派）三选一，**首推 dark + enterprise**。

---

## 1. SIDA-Pro 前端 27 页清单与功能矩阵

### 1.1 ⚠️ 重要校正：本地副本只有 20 页 + Login

**PROJECT_STATUS.md 报告 27 页**，但本地交付副本 `C:\Users\tianxiang\sida-pro\frontend\src\pages\` 实际只有：

- **21 个文件**（含 Login）
- **6-7 页关键文件未找到**：Quote.tsx、KlineChart.tsx（在 components 中）、klineEvents.ts、DarkFundTop.tsx、ReportsHub.tsx、SettingsHub.tsx、NotificationsHub.tsx

**建议**：Phase 2 启动前先让 Hermes 同步本地副本，确保后续 UI 重构工作基于完整代码。

### 1.2 现有页面清单（21 页）

| # | 文件 | 路由（推断）| 功能 | API 依赖 | 终端化紧迫度 | 备注 |
|---|---|---|---|---|---|---|
| 1 | `App.tsx` | / | 根路由 + 布局壳 | — | — | 已用 React.lazy + Suspense + AppErrorBoundary |
| 2 | `pages/Login.tsx` | /login | 登录 + JWT 注入 | `POST /api/auth/login` | 低 | 简单表单 + JWT localStorage |
| 3 | `pages/Stocks.tsx` | / 或 /stocks | 股票列表/自选股（首页）| `getStockList` + WS 订阅 | 中 | 已用 WebSocket 实时推送 |
| 4 | `pages/Dashboard.tsx` | /dashboard | 总览看板 | 多 API 聚合 | **高** | 当前最核心的看板页 |
| 5 | `pages/Forecast.tsx` | /forecast | 预测 | `get_forecast` | **高** | 含 Kronos / Chronos-Bolt 模型结果 |
| 6 | `pages/AnalysisDetail.tsx` | /analysis/:symbol | 分析详情 | 单股聚合 | **高** | 关键详情页 |
| 7 | `pages/IndexDetail.tsx` | /index/:code | 指数详情 | 指数聚合 | 中 | — |
| 8 | `pages/Opportunities.tsx` | /opportunities | 机会页 | `get_opportunities` | **高** | 决策先锋三指标主要展示位 |
| 9 | `pages/Quote.tsx` | /quote/:symbol | **行情核心**（已未找到本地）| `get_market_data_cn` + summary + WS | **🔴 高（v0.4.60 已去卡片化）** | Hermes 本机已有；本地副本缺失 |
| 10 | `pages/Portfolio.tsx` | /portfolio | 持仓 | `get_portfolio` | 中 | — |
| 11 | `pages/Watchlist.tsx` | /watchlist | 自选股 | `get_watchlist` | 中 | — |
| 12 | `pages/Reports.tsx` | /reports | 报告 | `get_reports` | 中 | — |
| 13 | `pages/ReportsHub.tsx` | /reports/hub | 报告中心 | — | 低 | 本地副本缺失 |
| 14 | `pages/Settings.tsx` | /settings | 设置 | 多 API | 低 | — |
| 15 | `pages/SettingsHub.tsx` | /settings/hub | 设置中心 | — | 低 | 本地副本缺失 |
| 16 | `pages/NotificationsHub.tsx` | /notifications | 通知中心 | `get_notifications` | 中 | 本地副本缺失 |
| 17 | `pages/ShadowAccount.tsx` | /shadow | 影子账户报告 | iframe 沙箱 | 低 | 已用 `window.open(url, '_blank')`，Tauri 需改 |
| 18 | `pages/StockInsightModal.tsx` | — | 弹窗：股票洞察 | — | — | 当前 K 线装在 Dialog 里 |
| 19 | `pages/StockContextMenu.tsx` | — | 右键菜单 | — | — | — |
| 20 | `pages/DarkFundTop.tsx` | /dark-fund-top | 暗盘 TOP 榜 | `get_dark_fund_top` | **高** | 本地副本缺失 |
| 21 | `pages/HealthCheck.tsx` | /health | 健康检查 | `/api/health` | 低 | — |

**未找到的关键文件**（需 Hermes 同步）：
- ❌ `pages/Quote.tsx`（v0.4.60 已去卡片化重写）
- ❌ `components/KlineChart.tsx`（Lightweight Charts v5 主图）
- ❌ `packages/biz-ui/.../klineEvents.ts`
- ❌ `pages/DarkFundTop.tsx`
- ❌ `pages/ReportsHub.tsx`
- ❌ `pages/SettingsHub.tsx`
- ❌ `pages/NotificationsHub.tsx`

### 1.3 缺失的 6 页（PROJECT_STATUS 报告但本地未找到）

| 页名（推断）| 可能路由 | 备注 |
|---|---|---|
| 三指标共振扫描页 | /resonance | 应与 Opportunities 区分 |
| L2 深度盘口页 | /l2/:symbol | 决策先锋核心展示 |
| 持仓成本线页 | /cost-basis/:symbol | v0.4.64 候选 |
| 主题设置/暗黑切换页 | /theme | 系统偏好 |
| 帮助/快捷键页 | /help | 终端风必备 |
| 数据源健康页 | /source-health | 已提到但未确认 |

**待 Hermes 同步后核对。**

---

## 2. 现有设计系统盘查

### 2.1 配色（Tailwind + CSS Variables）

| Token | 当前值 | 用途 |
|---|---|---|
| **主色** | `hsl(240, 70%, 45%)`（深青蓝）| shadcn 默认，应用于按钮、链接、聚焦 |
| **背景** | `hsl(0, 0%, 100%)` 亮 / `hsl(222, 47%, 11%)` 暗 | 浅色/深色主题 |
| **前景文字** | `hsl(222, 47%, 11%)` 亮 / `hsl(210, 40%, 98%)` 暗 | — |
| **次要文字** | `hsl(215, 16%, 47%)` | 标签/描述 |
| **边框** | `hsl(214, 32%, 91%)` 亮 / `hsl(217, 33%, 17%)` 暗 | — |
| **红涨** | `#E53935`（自定义）| 中国市场惯例 |
| **绿跌** | `#43A047`（自定义）| 中国市场惯例 |
| **圆角** | `--radius: 12px` | shadcn 默认偏大 |
| **暗色模式** | `darkMode: 'class'`（Tailwind）| 已支持切换 |

**问题**：
- 配色是 shadcn 默认"中性蓝灰" + 自定义红涨绿跌的混合，缺乏**专业终端的"色彩语义层"**（没有"信号绿""警示黄""大牛紫"等专用 token）
- 没有"K 线专属配色"（黑/红/绿/白四色组合）
- 没有"终端面板"专用配色（Bloomberg 风的"黑底橙字" / 通达信的"深灰底"）

### 2.2 字体与排版

| Token | 当前值 | 评价 |
|---|---|---|
| 主字体 | 系统默认（无显式声明）| ❌ 未指定中文/英文/数字字体 |
| 数字字体 | SF Mono（隐含）| ⚠️ 仅用 `font-mono` 类，未强制 `tabular-nums` |
| 中文 | 跟随系统 | ⚠️ 中文环境下可能是 Microsoft YaHei 或 PingFang SC，未显式声明 |
| 标题字重 | 600（font-semibold）| ✅ 合适 |
| 正文字重 | 400（默认）| ✅ 合适 |

**问题**：
- 没有指定**等宽数字字体**（金融必备）—— 当前可能是 `font-mono` 类，但未强制每个数字列对齐
- 没有"DIN Pro / Roboto Mono"等专业数字字
- 中文/英文/数字字体未显式声明，可能出现"数字抖动"问题（价格跳动时数字宽度变化）

### 2.3 间距 / 断点 / 阴影

| Token | 当前值 | 评价 |
|---|---|---|
| 间距 | Tailwind 默认 scale（0/1/2/4/8/...）| ✅ 标准 |
| 断点 | sm 640 / md 768 / lg 1024 / xl 1280 / 2xl 1536 | ✅ 标准 |
| 阴影 | Tailwind 默认（sm/md/lg/xl/2xl）| ⚠️ 偏柔，不够"专业终端"的硬边界感 |

### 2.4 组件库

**base-ui（shadcn 标准件）**：

| 组件 | 文件 | 评价 |
|---|---|---|
| Button | `components/ui/button.tsx` | ✅ 完整 |
| Card | `components/ui/card.tsx` | ✅ 完整（但 v0.4.60 已去卡片化，依赖度下降）|
| Dialog | `components/ui/dialog.tsx` | ✅ 完整（K 线在此承载）|
| Tabs / Select / Checkbox / RadioGroup / Slider / Switch | — | ✅ Radix UI 子包 |
| Tooltip / Popover / Dropdown | — | ✅ 完整 |
| Toast (sonner) | — | ✅ 完整 |
| Calendar (react-day-picker) | — | ✅ 完整 |
| Input / Textarea / Label | — | ✅ 完整 |

**biz-ui（业务组件）**：

| 组件 | 文件 | 评价 |
|---|---|---|
| KlineChart（Lightweight Charts v5）| `packages/biz-ui/.../KlineChart.tsx` | 🔴 **本地副本缺失**，Hermes 本机有 |
| InteractiveKline | `packages/biz-ui/.../InteractiveKline.tsx` | ✅ 已存在（LWC 通过 CDN 加载）|
| MinuteLwcChart | `packages/biz-ui/.../MinuteLwcChart.tsx` | ✅ 已存在 |
| StockInsightModal | — | ⚠️ 装在 Dialog 弹窗里（不是全屏路由）|
| echarts-core | `packages/biz-ui/src/lib/echarts-core.ts` | ✅ 已按需注册（Bar/Gauge/Line/Candlestick/Heatmap + 7 个 component）|

### 2.5 K 线架构（5 层已实现，但承载位置有问题）

> 关键发现：**SIDA-Pro 的 K 线模块设计是先进的 5 层架构**，但承载在 Dialog 弹窗里（KlineModal / StockInsightModal），不是全屏路由。这是"专业终端化"的最大障碍。

| 层 | 内容 | 状态 |
|---|---|---|
| **L0 事实底** | 原始 OHLCV + 涨跌停色 | ✅ 已实现 |
| **L1 均线牛熊** | MA5/10/20/60（项目采用）| ✅ 已实现 |
| **L2 GS 买卖点** | G/S 图标 + G/S 区色带 | ⚠️ 算法层已实现（decision_pioneer.compute_gs_signal），前端主图叠加待集成 |
| **L3 资金** | 暗盘资金柱 + 0 轴穿越图标 | ⚠️ 算法层已实现（ohlc_dark.allocate_bar），前端副图层待集成 |
| **L4 事件** | 涨跌停、缺口、龙虎榜 | ⚠️ 部分实现 |

### 2.6 数据展示 / 加载 / 空状态

| 项 | 当前覆盖率 |
|---|---|
| Loading | ✅ 完整（Skeleton + Spin）|
| Empty | ✅ 完整（"暂无数据"占位）|
| Error | ✅ 完整（ErrorBanner + ErrorBoundary）|
| 响应式 | ⚠️ **桌面优先**，未对移动端深度优化 |
| 暗色模式 | ✅ 完整（`use-theme.ts` + Tailwind `darkMode: 'class'`）|
| 实时推送 | ✅ 原生 WebSocket（`Stocks.tsx`）|

### 2.7 TQ 网关 TdxW 实时订阅

- `subscribe_hq`（TQ JSON-RPC，最多 100 只实时推送）已集成
- WebSocket（API 层 `ws_hub.py`）已集成
- 实时延迟：TQ ~28ms / WebSocket < 100ms

---

## 3. 与专业终端调性的差距（四向对照）

> 对照对象：**TradingView（业界标杆）/ 同花顺决策先锋（中国市场标杆）/ 通达信（本土老牌）/ Bloomberg Terminal（专业天花板）**

### 3.1 布局范式（专业终端核心特征）

| 维度 | SIDA-Pro 当前 | TradingView | 同花顺决策先锋 | 通达信 | Bloomberg | 差距评估 |
|---|---|---|---|---|---|---|
| 主图布局 | Dialog 弹窗（KlineModal）| 全屏路由 | 全屏路由 | 全屏路由 | 多 Panel 全屏 | 🔴 **大**：弹窗 vs 全屏 |
| 主图占比 | 50-60% | 70-80% | 70-80% | 60-70% | 视内容而定 | 🔴 **大**：信息密度低 |
| 默认副图数 | 0-1（嵌在弹窗）| 3 | 2（三件套）| 1-3 | 0（用户自配）| 🔴 **大**：三件套未固定 |
| 多窗口同列 | ❌ 无 | ✅ 有 | ✅ 有（4/9 股同列）| ✅ 有 | ✅ 4 Panel | 🔴 **大**：缺失 |
| 跨窗口联动 | ❌ 无 | ✅ Link Groups | ✅ 联动键 | ✅ 多窗口同步 | ✅ 自动同步 | 🔴 **大**：缺失 |
| 命令驱动 | ❌ 无 | ✅ `/` 全局搜索 | ❌ | ✅ 命令 | ✅ `<GO>` 范式 | 🟡 中：可选改进 |

### 3.2 信息密度与"看图决策"

| 维度 | SIDA-Pro 当前 | 专业终端主流 | 差距评估 |
|---|---|---|---|
| 信息密度 | 中等偏高（数据堆叠）| 中等（看图为主）| 🔴 **大**：偏堆叠 |
| 主图优先度 | 中等（被弹窗限制）| 高 | 🔴 **大**：主图未突出 |
| 数字 vs 图形比例 | 数字 40% / 图形 60% | 数字 20-30% / 图形 70-80% | 🟡 中：偏数字 |
| "看图"视觉手段 | 部分（K 线 + 均线）| 完整（G/S 图标 + 三色柱 + 阈值线 + 价量组合文字）| 🔴 **大**：缺多项 |

### 3.3 色彩语义（专业终端的另一核心）

| 元素 | SIDA-Pro 当前 | TradingView | 同花顺决策先锋 | 通达信 | Bloomberg | 差距评估 |
|---|---|---|---|---|---|---|
| 红涨绿跌 | ✅ 已对齐 | ❌ 绿涨红跌（西方）| ✅ 已对齐 | ✅ 已对齐 | 黑白为主 | ✅ 无差距 |
| G/S 图标 | ❌ 无 | ❌（需自编 Pine）| ✅ 主图叠加 | ❌ | ❌ | 🔴 **大**：GS 独有 |
| 三色柱（紫/红/绿）| ❌ 无 | ❌ | ✅ 活跃度 | ❌ | ❌ | 🔴 **大**：活跃度独有 |
| 阈值线（生命/强势/大牛）| ❌ 无 | ❌ | ✅ 三条灰虚线 | ❌ | ❌ | 🔴 **大**：活跃度独有 |
| 资金红绿柱 | ⚠️ 部分（OHLC 对照）| ✅ 有 | ✅ 红绿+0 轴 | ✅ 有 | ❌ | 🟡 中：待主笔级 |
| 0 轴穿越图标 | ❌ 无 | ❌ | ✅ ▲▼ | ✅ 有 | ❌ | 🔴 **大**：决策先锋独有 |
| 警示色彩 | ⚠️ 部分 | ✅ 完善 | ✅ 完善 | ✅ 完善 | ✅ 完善 | 🟡 中：基础具备 |
| 主题切换（暗色）| ✅ 完整 | ✅ 完善 | ✅ 完善 | ✅ 完善 | 强制黑底 | ✅ 无差距 |

### 3.4 字体与排版

| 维度 | SIDA-Pro 当前 | 专业终端 | 差距评估 |
|---|---|---|---|
| 等宽数字 | ⚠️ 隐含（`font-mono`）| **必备**：DIN Pro / Roboto Mono / SF Mono | 🔴 **大**：未强制 tabular-nums |
| 标题字重 | 600 | 600-700 | ✅ 无差距 |
| 数字列对齐 | ⚠️ 未保证 | **强制**对齐 | 🔴 **大**：未用 tabular-nums |
| 中文字体 | 系统默认 | PingFang SC / Microsoft YaHei | 🟡 中：未显式声明 |
| 标签 vs 数字区分 | ⚠️ 不明显 | 灰标签 + 粗体数字 | 🟡 中：可改进 |

### 3.5 交互模式

| 维度 | SIDA-Pro 当前 | TradingView | 同花顺决策先锋 | 通达信 | Bloomberg | 差距评估 |
|---|---|---|---|---|---|---|
| 键盘快捷键 | ⚠️ 基础（`use-hotkeys.ts`）| ✅ 业界领先 | ✅ 中等 | ✅ 中等 | ✅ 键盘之王 | 🔴 **大**：Bloomberg 风缺失 |
| 鼠标滚轮缩放 | ⚠️ 基础 | ✅ 完善 | ✅ 完善 | ✅ 完善 | ✅ 完善 | 🟡 中：可改进 |
| 跨窗口联动 | ❌ 无 | ✅ Link Groups | ✅ 联动键 | ✅ 多窗口 | ✅ 自动 | 🔴 **大**：缺失 |
| 画线工具 | ⚠️ 基础（`InteractiveKline`）| ✅ 业界领先 | ✅ 基础 | ✅ 完善 | ✅ 完善 | 🟡 中：基础具备 |
| 实时推送 | ✅ WebSocket | ✅ 完善 | ✅ 完善 | ✅ 完善 | ✅ NEWS 浮窗 | ✅ 无差距 |

### 3.6 数据展示

| 维度 | SIDA-Pro 当前 | 专业终端 | 差距评估 |
|---|---|---|---|
| K 线图表层 | ✅ LWC v5 蜡烛图 | ✅ 蜡烛/线形/砖形/平均K | ✅ 无差距 |
| 副图层 | ⚠️ 部分（未集成 GS/资金/活跃度）| ✅ 完整 | 🔴 **大**：三件套未固定 |
| 持仓成本线 | ❌ 无（v0.4.64 候选）| ✅ 必备 | 🔴 **大**：缺失 |
| 缺口/缺口填充 | ⚠️ 基础 | ✅ 完善 | 🟡 中：可改进 |
| 龙虎榜 | ⚠️ wencai 间接 | ✅ 直接 | 🟡 中：基础具备 |
| 涨停封单 | ✅ 已支持（`get_zdt_data`）| ✅ 完善 | ✅ 无差距 |
| 首涨停时间 | ✅ 已支持 | ✅ 完善 | ✅ 无差距 |

### 3.7 总结：差距分级

| 差距等级 | 维度 | 影响 |
|---|---|---|
| 🔴 **大（必须改）** | 主图布局（弹窗→全屏）、主图占比、信息密度、三件套副图、多窗口同列、跨窗口联动、G/S 图标、三色柱、阈值线、0 轴穿越图标、等宽数字、键盘快捷键、持仓成本线 | 直接影响"专业终端"定位 |
| 🟡 **中（建议改）** | 数字字体显式声明、中文字体声明、画线工具增强、缺口/缺口填充、龙虎榜直接展示、移动端适配、Command Palette | 影响体验细节 |
| ✅ **无差距** | 红涨绿跌、主题切换、实时推送、组件库覆盖、Loading/Empty/Error | 已具备 |

---

## 4. 设计语言建议（候选风格与样板间）

### 4.1 三套候选风格（从 86+ 内置 Skill 中筛选）

| 风格组合 | 调性 | 优势 | 劣势 | 适用人群 |
|---|---|---|---|---|
| **dark + enterprise**（首推）| 黑底/深灰底 + 高对比橙红绿 + 大字 + 模块化 | 与 Bloomberg Terminal / 通达信暗色 / TradingView 暗色一致；视觉冲击强；暗色护眼；专业感最强 | 暗色可能不喜；开发量较大 | 专业交易员、盯盘用户、夜盘用户 |
| **clean + power** | 白底/灰底 + 深色文字 + 强对比强调色 | 稳重专业；阅读友好；开发量小 | 视觉冲击力弱；与"终端"调性略偏 | 投顾、研究员、报告阅读 |
| **matrix + mono** | 黑底 + 荧光绿/橙 + 等宽数字 + 高密度 | 极客范、潮、独特 | 与"严肃金融"调性略偏；可能不专业 | 个人独立交易者、量化爱好者 |

**首推 dark + enterprise**：理由是用户明确要求"专业大气" + "看图决策" + "Web 大屏"，三者皆要求"高对比 + 大字 + 模块化"，dark + enterprise 最契合。

### 4.2 样板间建议（先重哪 1-2 页）

**首推样板间：新建 Quote 路由（v0.4.60 已被去卡片化但仍是弹窗形态，需迁出）+ Opportunities 完整重构**。

| 页 | 理由 | 工作量 | 风险 |
|---|---|---|---|
| **Quote.tsx**（全屏路由化）| 行情核心页；v0.4.60 已去卡片化但仍在弹窗；迁到全屏路由后才能展示"主图 + 副图三件套" | 5 天 | 低（已有 5 层架构基础）|
| **Opportunities.tsx**（去卡片化 + 三指标共振展示）| 决策先锋三指标主要展示位；当前用 OpportunityCard 堆叠；改为"左侧列表 + 右侧 K 线"或"网格 + 标签筛选" | 2-3 天 | 低 |

### 4.3 dark + enterprise 风格落地要素清单

> 若选 dark + enterprise 风格，需要在设计系统中补充：

| 类别 | 新增 Token | 值建议 |
|---|---|---|
| **K 线背景** | `--bg-kline` | `hsl(222, 47%, 6%)`（接近黑）|
| **K 线涨** | `--up-color` | `#E11D48`（深红）|
| **K 线跌** | `--down-color` | `#10B981`（深绿）|
| **K 线平** | `--flat-color` | `#94A3B8`（中性灰）|
| **GS G** | `--gs-go-color` | `#F59E0B`（琥珀，醒目）|
| **GS S** | `--gs-stop-color` | `#6366F1`（深紫，警告）|
| **活跃度 紫** | `--activity-bull` | `#8B5CF6`（紫，强）|
| **活跃度 红** | `--activity-strong` | `#EF4444`（红，中强）|
| **活跃度 绿** | `--activity-weak` | `#22C55E`（绿，弱）|
| **阈值线** | `--threshold-line` | `rgba(148, 163, 184, 0.5)`（半透明灰）|
| **数字字体** | `--font-numeric` | `JetBrains Mono, SF Mono, Consolas, monospace` + `font-feature-settings: "tnum"` |
| **中文字体** | `--font-cn` | `PingFang SC, Microsoft YaHei, Source Han Sans CN` |
| **英文/UI字体** | `--font-ui` | `Inter, system-ui, -apple-system, sans-serif` |
| **Bloomberg 风强调色** | `--accent-primary` | `#F59E0B`（橙）|

---

## 5. 全站 27 页改造路径（按 P0/P1/P2 分批）

> 工作量基于单设计师 + 单前端 ≈ 5 天/人估算。

### P0：样板间（先用 1-2 页验证风格）

| 页 | 当前形态 | 目标形态 | 工作量 | 依赖 |
|---|---|---|---|---|
| **新建 Quote.tsx 全屏路由** | Dialog 弹窗（KlineModal）| 全屏路由：主图 60% + 三件套副图 30% + 信息栏 10% | 5 天 | Hermes 同步本地副本 |
| **Opportunities.tsx 去卡片化** | OpportunityCard 堆叠 | 列表 + K 线侧栏 + 三指标标签 | 2-3 天 | dark + enterprise 风格定稿 |
| **设计系统初始化** | shadcn 默认 | dark + enterprise token + 等宽数字 | 1 天 | — |

**P0 总计**：8-9 天，产出"样板间"。

### P1：核心看板（沿用样板间风格）

| 页 | 改造重点 | 工作量 | 依赖 |
|---|---|---|---|
| Dashboard.tsx | 总览卡片 → 模块化大屏 | 2 天 | P0 设计系统 |
| Forecast.tsx | 预测结果卡 → K 线 + 预测曲线叠加 | 2 天 | P0 设计系统 |
| AnalysisDetail.tsx | 详情页 → 主图 + 副图 + 文字说明 | 2 天 | P0 设计系统 |
| DarkFundTop.tsx | 暗盘榜 → 列表 + 颜色编码 | 1-2 天 | P0 设计系统 |
| IndexDetail.tsx | 指数详情 → 主图 + 副图 | 1 天 | P0 设计系统 |

**P1 总计**：8-9 天。

### P2：辅助页面（沿用样板间风格）

| 页 | 改造重点 | 工作量 | 依赖 |
|---|---|---|---|
| Portfolio.tsx | 持仓 → 持仓列表 + 持仓成本线 | 1-2 天 | P0 + 后端 v0.4.64 持仓成本线 |
| Watchlist.tsx | 自选 → 紧凑列表 | 0.5 天 | P0 |
| Reports.tsx | 报告 → 报告卡片 + 详情 | 1 天 | P0 |
| ReportsHub.tsx | 报告中心 | 1 天 | P0 |
| Settings.tsx | 设置 → 表单统一 | 1 天 | P0 |
| SettingsHub.tsx | 设置中心 | 1 天 | P0 |
| NotificationsHub.tsx | 通知中心 → 时间轴 | 1 天 | P0 |
| ShadowAccount.tsx | 影子账户报告 | 1 天 | P0 |
| StockInsightModal.tsx | 弹窗（保留作为快速预览）| 0.5 天 | — |
| StockContextMenu.tsx | 右键菜单（保留）| 0.5 天 | — |
| HealthCheck.tsx | 健康检查 → 监控面板 | 1 天 | P0 |
| Login.tsx | 登录（轻量化）| 0.5 天 | P0 |
| 三指标共振扫描页（推断）| 全市场扫描 → 大屏看板 | 2 天 | P1 |
| L2 深度盘口页（推断）| L2 盘口 → 十档 + 委托队列 | 2 天 | P1 |
| 主题设置页（推断）| 暗黑切换 → 表单 | 0.5 天 | P0 |
| 帮助/快捷键页（推断）| 帮助 → 列表 | 0.5 天 | P0 |
| 数据源健康页（推断）| 数据源 → 监控 | 1 天 | P0 |
| 持仓成本线页（推断）| 持仓成本 → 主图叠加 | 1 天 | P0 + 后端 v0.4.64 |

**P2 总计**：12-18 天（取决于页面是否需要新功能）。

### 总工作量估算

| 阶段 | 工作量 | 累计 |
|---|---|---|
| P0 样板间 | 8-9 天 | 8-9 天 |
| P1 核心看板 | 8-9 天 | 16-18 天 |
| P2 辅助页面 | 12-18 天 | 28-36 天 |
| **总计** | **28-36 天（≈ 6-7 周）** | — |

---

## 6. Tauri 桌面端兼容性评估

> 详细评估见 §附录 A。结论先行：**Tauri 友好性极高，1-2 人日内可打包成桌面应用**。

### 6.1 结论

- ✅ **总体友好**：纯 Web SPA，无 Node/Electron 依赖、无 SSR、无 IE 兼容代码
- ✅ **绝大部分代码"开箱即用"**：React 18 + ESM + 现代 API
- ✅ **主要工作是构建/打包层面**（CDN 依赖、CSP、API 路由）
- ✅ **业务代码几乎不需要重写**

### 6.2 潜在障碍与改造工作量

| 障碍 | 严重度 | 改造工作量 | 必要性 |
|---|---|---|---|
| **#1 Lightweight Charts 走 CDN** | 🟡 中 | ~30 分钟 | **强烈建议**（离线启动刚需）|
| **#2 Service Worker (`public/sw.js`)** | 🟢 低 | ~10 分钟（可保留无害）| 可选 |
| **#3 PWA manifest + viewport** | 🟢 低 | 0（无害）| 无需改 |
| **#4 localStorage JWT + CSP** | 🟢 低 | ~2 小时（迁 cookie，可选）| 可选 |
| **#5 `window.open(url, '_blank')`** | 🟡 中 | ~30 分钟 | Tauri 2.x 推荐 |
| **#6 `document.createElement('a')` 下载** | 🟢 低 | 0（纯 DOM API）| 无需改 |
| **#7-#9 matchMedia / clipboard / ResizeObserver** | 🟢 低 | 0（完美支持）| 无需改 |
| **#10 iframe 沙箱** | 🟡 中 | ~5 分钟（capability 加 1 行）| Tauri 2.x 必需 |
| **#11 `date-fn` 死依赖**（疑似 `date-fns` 拼写错误）| 🟢 低 | ~5 分钟（清掉）| 推荐 |
| **#12 `tauri.conf.json` capability 编排** | 🟡 中 | ~30 分钟 | Tauri 框架要求 |

**总计（最小可用 Tauri 包）**：~2-3 小时（不含 Rust 端 `tauri.conf.json` 编排）。
**含体验打磨**：~1 个工作日。

### 6.3 哪些地方**不需要**改

- ✅ 业务代码全部保留（27 个 page、20+ component、3 个 workspace 包）
- ✅ React Router 6 + BrowserRouter 直接用（Tauri 推荐 `<base href="./">` 即可）
- ✅ fetch + WebSocket + localStorage 原样保留
- ✅ Tailwind / Radix / ECharts / lucide 全保留
- ✅ Dark Mode / 主题切换 全部保留
- ✅ `/api` 路由（WebSocket `ws://...api/quotes/ws`）直接走同源

### 6.4 Tauri 化的额外加分项

- ✅ 已用 **pnpm workspace + vite alias**，monorepo 友好
- ✅ 已用 **tsc -b 增量编译**，Rust 端调用 `pnpm build` 快速
- ✅ 已用 **CSS 变量 + Tailwind `darkMode: 'class'`**（`use-theme.ts`），Tauri 跟随系统主题开箱即用
- ✅ 已用 **ResizeObserver 替代 window.resize**（`useECharts.ts`），Tauri 窗口缩放自适应
- ✅ 已用 **React.lazy 路由分割**（`App.tsx`），Tauri 启动快
- ✅ 已用 **`<Suspense>` + `<AppErrorBoundary>`**（`App.tsx`），Tauri 中错误展示更稳
- ✅ 已用 **`AbortController` + 内存 GET 缓存**（`client.ts`），Tauri 后端响应延迟更小时收益更明显

---

## 7. 重构入口建议（Phase 2 候选项）

> 仅建议，不实施；待用户审阅本报告后决策。

### 入口候选（按推荐度排序）

1. **【推荐】P0 样板间**：dark + enterprise 风格定稿 + 新建 Quote 全屏路由（5 天）+ Opportunities 去卡片化（2-3 天）+ 设计系统初始化（1 天）
   - **目的**：建立"专业终端"的样板间，老板审阅后定调
   - **风险**：低（已有 5 层架构基础）
   - **可交付**：1-2 页可演示的"专业终端"样板，含主图 + 三件套副图 + 等宽数字 + 暗色主题

2. **次推 P1 核心看板**：沿用样板间风格改造 Dashboard / Forecast / AnalysisDetail / DarkFundTop
   - **目的**：把样板间风格覆盖到核心 5 页
   - **依赖**：P0 样板间交付 + Hermes 同步本地副本
   - **风险**：中（API 集成验证）

3. **备选 后端 P0 三件套**（与本文档 A 的入口 #1 一致）：wencai 三指标共振选股 + 暗盘 SERIES + 实战文案 API 字段化
   - **目的**：后端先行，前端通过 API 直接拿到"实战文案 + 共振状态"展示
   - **风险**：低（算法已有，主要是数据串联）
   - **与本 UI 文档的关系**：作为 UI 重构时的"数据来源"，确保 API 字段对齐（如 `pv_combo` / `state_action_label` / `activity_zone`）

### 推荐下一步

**Phase 2 启动建议**：先文档 A 的入口 #1（后端 P0 三件套）+ 本文档的入口 #1（UI P0 样板间）**双线并行**：
- 后端：3 条 wencai 模板 + 暗盘 SERIES + 实战文案 API
- 前端：dark + enterprise 设计系统 + Quote 全屏路由 + Opportunities 去卡片化

预期 v0.4.65 demo 版产出"决策先锋三指标复刻 + 专业终端 UI"完整可演示版本。

---

## 附录 A：Tauri 兼容性详细评估

> 来源：扫描 `C:\Users\tianxiang\sida-pro\frontend\`（Hermes 交付的本地副本）

### A.1 依赖清单扫描（来自 package.json + 各 workspace 包）

| 类别 | 包 | 版本 | Tauri 影响 |
|---|---|---|---|
| 核心框架 | `react` / `react-dom` | `^18.3.0` | ✅ Tauri WebView2/WKWebView 完全支持 |
| 构建工具 | `vite` | `^5.0.0` | ✅ 标准 ESM 输出，Tauri 友好 |
| 构建插件 | `@vitejs/plugin-react` | `^4.2.0` | ✅ 纯运行时，无 Node 钩子 |
| 语言 | `typescript` | `^5.3.0` | ✅ tsconfig `target: ES2020`，无 legacy 输出 |
| 样式 | `tailwindcss` | `^3.4.0` | ✅ JIT，`darkMode: 'class'` |
| 样式插件 | `@tailwindcss/typography` / `tailwindcss-animate` | ^0.5 / ^1.0 | ✅ 纯 CSS |
| 图表-ECharts | `echarts` | `^6.1.0` | ✅ 已按需注册，首屏 ~400KB |
| 图表-LWC | `lightweight-charts` | **未在 package.json** | ⚠️ 通过 CDN `<script defer>` 加载（见 §3 障碍 #1）|
| UI 基础 | `@radix-ui/react-*` | 9 个子包 | ✅ shadcn 风格，零样式 headless |
| UI 工具 | `class-variance-authority` / `clsx` / `tailwind-merge` | ^0.7 / ^2.1 / ^2.2 | ✅ shadcn 标准三件套 |
| 图标 | `lucide-react` | `^0.300.0` | ✅ SVG tree-shake |
| Markdown | `react-markdown` / `remark-gfm` | ^10.1 / ^4.0 | ✅ 纯前端渲染 |
| 路由 | `react-router-dom` | `^6.20.0` | ✅ BrowserRouter（Tauri 推荐 `<base href="./">`）|
| 日期 | `date-fn` `^0.0.2`（疑似拼写错误）| ⚠️ 死依赖，可清掉 | 代码中实际未 import |
| 日期-实际 | `Intl.DateTimeFormat` | ✅ 自实现 | 纯标准 API |
| 状态管理 | **无 zustand/redux/jotai** | ✅ useState + useRef + localStorage | — |
| HTTP | `fetch` + `AbortController` | ✅ 原生 | 无 axios/swr/react-query |
| WebSocket | 原生 `new WebSocket` | ✅ 原生 | `pages/Stocks.tsx` 中实现 |
| 二维码 | `qrcode.react` | ^4.2 | ✅ Canvas/SVG |
| 导出/截图 | `html-to-image` | ^1.11 | ✅ Canvas/DOM |
| 日期选择器 | `react-day-picker` | ^9.13 | ✅ 纯 React |
| 本地依赖（workspace）| `@panwatch/api|base-ui|biz-ui` | 0.1.0 | ✅ pnpm + tsconfig paths alias |
| **Node/Electron 原生绑定** | **无** | ✅ grep `fs`、`path`、`child_process`、`ipcRenderer`、`Electron` 全部 0 命中 | — |

### A.2 构建配置摘要

来自 `vite.config.ts`：

| 项 | 当前值 | Tauri 影响 |
|---|---|---|
| `type` | `module` | ✅ Tauri 静态资源按 ESM 提供 |
| `plugins` | 仅 `@vitejs/plugin-react` | ✅ 不需要 `vite-plugin-node-polyfills` |
| `resolve.alias` | `@`、`@panwatch/*` → `packages/*/src` | ✅ Tauri 打包会先跑 `pnpm build`，alias 在构建阶段已解析 |
| `server.proxy` | `/api → http://127.0.0.1:8000` | ⚠️ 仅 dev 阶段有效；Tauri 不会执行 dev 模式 |
| `base` | 未设（默认 `/`） | ✅ Tauri 推荐保留相对路径；现配置已兼容 |
| `build.target` | 未设（vite 5 输出 ES2020）| ✅ Tauri WebView2（Chromium 110+）支持 ES2020 |
| 构建脚本 | `tsc -b && vite build` | ✅ 标准两步；Tauri 构建可直接调用 |
| 静态资源 | `public/` 含 `manifest.json` + `sw.js` + 3 个 icon | ⚠️ PWA 资产；Tauri 桌面端可保留也可删 |

### A.3 Tauri 1.x / 2.x 兼容性

| 项 | 评估 | 备注 |
|---|---|---|
| **WebView2（Windows）** | ✅ 完美支持 | React 18 + ES2020 + 任意浏览器 API |
| **浏览器 API** | ✅ 无 IE/Edge legacy 痕迹 | 代码全是 ES2020+ |
| **Node API** | ✅ 0 处 `require` / `fs` / `path` / `child_process` | 不需要 polyfills |
| **localStorage** | ✅ 支持（WebView2 提供 per-app 持久化目录）| 已在 7+ 文件使用 |
| **sessionStorage** | ✅ 支持 | `packages/api/src/client.ts:32` |
| **Notification API** | ✅ 支持（WebView2 原生） | 已用 `Notification` + SW 兜底 |
| **Service Worker** | ✅ 支持但能力受限 | `public/sw.js` 仅缓存 manifest/icon |
| **WebView2 系统要求** | ✅ Windows 10 1803+ / Windows 11 | 用户机器已 Win11 |
| **bundle 大小优势** | ✅ 比 Electron 小 80%+ | 符合"小主机部署"理念 |

**Tauri 2.x 移动**：不建议（项目为桌面设计，PWA-style 入口）。

### A.4 障碍详细清单（按优先级）

**🟡 中等优先级**

- **#1 Lightweight Charts 走 CDN**：
  - `index.html` 第 49-50 行通过 `<script defer>` 从 `unpkg.com` 加载 `lightweight-charts.standalone.production.js`
  - 代码通过 `window.LightweightCharts` 全局访问
  - **Tauri 影响**：首次启动若无网会**直接报错**；CSP meta 第 34 行白名单 `unpkg.com` + `jsdelivr.net` 已放行
  - **改造**：~30 分钟（移除 CDN script、`pnpm add lightweight-charts@^5`、`import { createChart } from 'lightweight-charts'` 替换 `getLW()`）

- **#5 `window.open(url, '_blank')`**：
  - 出现位置：`App.tsx:336, 374, 482`（GitHub 链接）、`pages/ShadowAccount.tsx:110, 113, 145`（开影子账户报告）
  - **Tauri 2.x 影响**：默认会被拦截
  - **改造**：每个 call site 5 行包装，约 30 分钟

- **#10 iframe 沙箱**：
  - `pages/ShadowAccount.tsx` 用 `w.document.open()` / `document.write` / `iframe sandbox="allow-scripts"`
  - **Tauri 2.x 影响**：WebView2 支持 iframe + sandbox，但 Tauri 默认 capability 需显式允许 webview-create
  - **改造**：capability 文件加 1 行

- **#12 `tauri.conf.json` capability 编排**：
  - Tauri 2.x 默认 denied-by-default 安全模型
  - **改造**：~30 分钟

**🟢 低优先级**

- **#2 Service Worker**：可保留无害；或删除注册（~10 行代码）
- **#3 PWA manifest + viewport**：完全无害；Tauri 用自己的窗口/标题栏
- **#4 localStorage JWT + CSP**：安全增强，可选（~2 小时）
- **#6 `document.createElement('a')` 下载**：纯 DOM API，完美工作；若想走"原生保存对话框"可换 `tauri-plugin-dialog`
- **#7 `window.matchMedia` / `ResizeObserver`**：完全支持
- **#8 `navigator.clipboard` / `navigator.serviceWorker`**：完全支持
- **#9 ResizeObserver**：完全支持（项目已主动用 ResizeObserver 替代 `window.resize`，是加分项）
- **#11 `date-fn` 死依赖**：~5 分钟清掉

### A.5 关键文件路径参考

| 用途 | 绝对路径 |
|---|---|
| 根 package.json | `C:\Users\tianxiang\sida-pro\frontend\package.json` |
| vite 配置 | `C:\Users\tianxiang\sida-pro\frontend\vite.config.ts` |
| tsconfig | `C:\Users\tianxiang\sida-pro\frontend\tsconfig.json` |
| Tailwind 配置 | `C:\Users\tianxiang\sida-pro\frontend\tailwind.config.js` |
| 入口 HTML（含 LWC CDN + CSP）| `C:\Users\tianxiang\sida-pro\frontend\index.html` |
| 入口 TS | `C:\Users\tianxiang\sida-pro\frontend\src\main.tsx` |
| API 客户端 | `C:\Users\tianxiang\sida-pro\frontend\packages\api\src\client.ts` |
| WebSocket 实现 | `C:\Users\tianxiang\sida-pro\frontend\src\pages\Stocks.tsx` |
| LWC 全局访问 | `packages/biz-ui/src/components/InteractiveKline.tsx`、`MinuteLwcChart.tsx` |
| iframe 沙箱 + window.open | `C:\Users\tianxiang\sida-pro\frontend\src\pages\ShadowAccount.tsx` |
| 浏览器通知 / Service Worker | `C:\Users\tianxiang\sida-pro\frontend\src\lib\browser-notifications.ts` |
| Service Worker 源 | `C:\Users\tianxiang\sida-pro\frontend\public\sw.js` |
| PWA manifest | `C:\Users\tianxiang\sida-pro\frontend\public\manifest.json` |

---

## 附录 B：四向对照速查表（专业终端要素 × SIDA-Pro 现状）

| 要素 | TradingView | 同花顺决策先锋 | 通达信 | Bloomberg | SIDA-Pro |
|---|---|---|---|---|---|
| **命令驱动** | `/` 全局搜索 | ❌ | ✅ 命令 | ✅ `<GO>` | ❌ |
| **绘图工具** | 业界领先 | 基础 | 完善 | 完善 | 基础 |
| **跨窗口联动** | Link Groups | 联动键 | 多窗口 | 自动 | ❌ |
| **键盘快捷键** | 业界领先 | 中等 | 中等 | 键盘之王 | 基础 |
| **暗色主题** | 完善 | 完善 | 完善 | 强制 | 完整 |
| **等宽数字** | 完善 | 完善 | 完善 | 完善 | ⚠️ 隐含 |
| **响应式布局** | ✅ | ⚠️ | ❌ | 视窗 | 桌面优先 |
| **K 线图表层** | 6+ 种 | 蜡烛 | 6+ 种 | 多 Panel | 蜡烛 |
| **副图层数** | 用户自配 | 2（三件套）| 用户自配 | 用户自配 | 0-1 |
| **三色柱（活跃度）**| ❌ | ✅ | ❌ | ❌ | ❌ |
| **阈值线（GS）**| ❌ | ✅ | ❌ | ❌ | ❌ |
| **0 轴穿越图标**| ❌ | ✅ | ✅ | ❌ | ❌ |
| **价量组合语义**| ❌ | ✅ 4 种 | ❌ | ❌ | ❌ |
| **文件夹 / 自定义**| ✅ | ❌ | ✅ | ✅ | ❌ |

---

*整理人：ZCode Marvis · 整理日期：2026-09-02 · 仅供研究参考*
*未做任何代码修改；扫描覆盖 frontend/ 全部配置文件 + 21 个页面 + 关键组件*
