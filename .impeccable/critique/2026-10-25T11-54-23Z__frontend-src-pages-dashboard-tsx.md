---
target: 首页(市场全景·组合工作台)
total_score: 23
max_score: 40
na_heuristics: 
p0_count: 2
p1_count: 2
timestamp: 2026-10-25T11-54-23Z
slug: frontend-src-pages-dashboard-tsx
---
# Critique: 首页（市场全景·组合工作台）frontend/src/pages/Dashboard.tsx
Method: dual-agent (A: 设计总监评审 · B: impeccable detect.mjs) / 2026-10-25 / 基线 f5dd179f (v0.69.1)

## Design Health Score: 23/40 (Acceptable)
| # | 启发式 | 分 | 关键问题 |
|---|---|---|---|
| 1 | 系统状态可见 | 3 | 共振面板 8s 静默兜底无可见状态 |
| 2 | 贴近真实世界 | 2 | 「数智决策」首页缺席；趋/强/资黑话无解释 |
| 3 | 用户控制与自由 | 2 | 扫描强制离页；分享下拉无 Esc |
| 4 | 一致性与标准 | 2 | SectionHeader 只 3 处在用；button/div onClick 混用 |
| 5 | 错误预防 | 2 | single-flight 有；场景少 |
| 6 | 识别而非回忆 | 2 | 快捷键零提示，? 不可发现 |
| 7 | 灵活与效率 | 3 | 快捷键/定制/右键真实有效 |
| 8 | 美学与极简 | 2 | 12 模块平铺、同级标题 9+、KPI 重复 |
| 9 | 错误恢复 | 3 | ErrorBanner 按源聚合+四态不伪造 |
| 10 | 帮助与文档 | 2 | 引导入口 11px 灰字；解释 hover-only |

## 设计专属性判定
骨架通用、器官专属：结论句/口径徽章/趋强资状态机/四态不伪造是真专属，但排成普通段落与通用零件平权；视觉投资与产品独特性错位。

## 优先问题
- [P0] 可交互元素长成标签（侧栏 AI 卡无箭头 App.tsx:557-579；共振行无 chevron；4 处 11px 灰字假按钮 Dashboard.tsx:1352/1300/1041；扫描 ghost）→ onboard
- [P0] 核心列表键盘不可达（要紧事 1086/待办 1062/机会精选 1315 div+onClick 无 tab/role）→ harden
- [P1] 「数智决策」首页无名（ResonancePanel.tsx:93；侧栏副标题黑话 App.tsx:574）→ clarify
- [P1] 12 模块均质平铺（三区结构：市场/组合/找机会）→ layout
- [P2] 长等待无安抚（基准 1 分钟空盒 1191-1206；AI 体检无界 1275-1282；扫描离页）→ polish

## 检测器发现（B，267 文件仅 3 条）
- gradient-text ×2：Landing.tsx:170/210（真实命中，无令牌背书）
- border-accent-on-rounded ×1：WorkspaceTabs.tsx:144（误报：标准 tab 指示器）

## 人物红旗
- Alex：快捷键全埋没；扫描离页；无批量
- Sam：核心列表键盘断裂；共振行 24px<32px；details 无 Esc；折叠态仅 title
- Jordan：数智决策搜不到；黑话副标题；趋强资无解释；引导不可见

## 保持项
CollapsibleSection 折叠≠藏；四态不伪造+ErrorBanner 按源聚合；红绿+▲▼±文字三冗余
