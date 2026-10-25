---
name: SIDA Pro（数智分析 Pro）
description: 个人股票交易分析终端——专家密度、口径诚实、结论先行的行情驾驶舱
colors:
  stock-up: "#ff5252"
  stock-down: "#43a047"
  primary: "hsl(240 70% 45%)"
  canvas: "hsl(240 25% 99.5%)"
  background: "hsl(240 20% 95.5%)"
  panel: "hsl(240 25% 99%)"
  raised: "hsl(240 20% 96%)"
  float: "hsl(240 15% 92%)"
  foreground: "hsl(240 20% 10%)"
  muted-foreground: "hsl(240 10% 48%)"
  border: "hsl(240 15% 90%)"
  border-strong: "hsl(240 17% 27.5%)"
  destructive: "hsl(0 72% 51%)"
  success: "hsl(152 60% 42%)"
  accent-primary: "hsl(38 92% 50%)"
  role-watch: "hsl(245 65% 62%)"
  role-opp: "hsl(42 95% 55%)"
  role-risk: "hsl(25 85% 50%)"
typography:
  display:
    fontFamily: "DIN Alternate, Bahnschrift, SF Mono, Cascadia Code, ui-monospace, monospace"
    fontSize: "20px"
    fontWeight: 700
    fontFeature: "tabular-nums"
  title:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 700
  body:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "11px"
    fontWeight: 500
  micro:
    fontFamily: "Inter, system-ui, -apple-system, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif"
    fontSize: "10px"
    fontWeight: 400
    fontFeature: "tabular-nums"
rounded:
  sm: "2px"
  md: "4px"
  lg: "6px"
  xl: "8px"
  2xl: "10px"
spacing:
  unit: "4px"
  compact-unit: "3px"
  comfortable-unit: "6px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#ffffff"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
    height: "42px"
  button-secondary:
    backgroundColor: "{colors.float}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    padding: "10px 16px"
    height: "42px"
  input:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
    height: "40px"
  card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.xl}"
  row:
    backgroundColor: "transparent"
    textColor: "{colors.foreground}"
    height: "32px"
---

# Design System: SIDA Pro（数智分析 Pro）

## Overview

**Creative North Star: "操盘手的驾驶舱"（The Operator's Cockpit）**

这是一台给人**在高压力、高节奏的交易时段里快速做决策**的行情终端，不是营销页也不是数据看板。视觉世界的第一性原理是「结论先行、口径诚实」：每一屏先回答"现在怎么办"，再铺开证据；每一个数字都可追溯到明确的数据口径。中性色统一收敛在 **240 蓝紫锚点相**（反 AI 模板：不用无相灰、不用 shadcn 默认 234 蓝），卡片是"纸面白"而非纯白——密度高但层级清楚。

**2026-10-24 起「去卡片化」不再是铁律**（用户裁定：UI 收益优先）。hairline 分隔仍是默认语言，但卡片、阴影、分组容器按收益自由使用；代码里已同步"卡片化翻案"（暗色 card 提亮 + 顶部内高光）。旧约束（买红卖绿、字号档位、口径标注）不受影响。

**Key Characteristics:**
- 涨跌语义色是全站最强的两种颜色，任何其他颜色不得与之竞争（"红涨绿跌即秩序"）
- Surface 阶梯（s0→s4）表达空间层级，不表达涨跌
- 字号六档制（10/11/12/13/16/20），所有数字 tabular-nums
- 微圆角（基准 6px，档位从它派生）、hairline 优先、暗色靠明度差不靠阴影
- 图表画布永远比外壳"更空一档"——K 线是主角

## Colors

一句话：蓝紫锚点的中性阶承载全部结构，红绿两个语义色承载全部方向，其余彩色只做角色标记。

### Primary
- **锚点钢蓝**（hsl(240 70% 45%)，暗色提亮至 60%）：品牌锚点、主按钮、选中态、焦点环（ring）。"A股数据感"，替代模板蓝。

### Secondary
- **Bloomberg 强调橙**（hsl(38 92% 50%)）：极少量金融终端式强调，用于关键指标点睛，不进大面积色块。

### Tertiary
- **角色四色**（自选紫 hsl(245 65% 62%) / 机会琥珀 hsl(42 95% 55%) / 风险橙 hsl(25 85% 50%) / 系统灰 hsl(240 8% 58%)）：2px 角色条（role-strip）与导航分组标识，只表达模块归属，不得抢涨跌色。

### Neutral
- **Surface 阶梯**（亮色 s0 95.5% 页底 → s1 99% 面板 → s2 96% 卡内 hover → s3 92% 浮层/选中 → s4 100% tooltip；图表画布 99.5% 比一切更空）：空间层级唯一表达方式。暗色对应 5.7%→21%，靠每档 3~4 个明度点差分层。
- **文字两级**（前景 hsl(240 20% 10%) / 次级 hsl(240 10% 48%)，暗色对比度锁定 ≥4.9:1）。
- **边框两级**（常规 90% / 强 27.5%）：常规用于 hairline 分隔，强用于卡片边界。

### 语义色（本项目最重要的一组）
- **涨红**（#ff5252）：上涨、买入、机会、GS 买点（--gs-go 同源）。
- **跌绿**（#43a047）：下跌、卖出、炸板/风险方向、GS 卖点。
- **强度阶**（OKLCH heat-n5…p5，绿系 155 相 → 中性 → 红系 25 相，共 11 档）：热力图/涨跌分布的均匀发散阶，每档文字对比度 ≥3:1 由测试锁定。
- **情景关键位**（rose #e11d48 / emerald #059669）：与涨跌色**分开**——涨跌色=价格方向，关键位色=位置性质，语义不同各自演进。
- **活跃度三色**（大牛紫/强势红/生命绿）：机构活跃度分档，弱档走平盘灰，不编造第四色。

### Named Rules
**红绿即秩序 Rule。** 涨跌语义色是屏幕上最强对比，primary/角色色/强调橙任何情况下不得比红绿更抢眼。同屏出现多口径资金数据必须各带 caliber 标签，禁止跨源混算。

**无数据即无数据 Rule。** 缺数据显式标注「无数据/口径缺口」，禁止用 0、`--` 编造或 LLM 推测填充；基准日滞后必须在 UI 显式声明。

## Typography

**Display/数字字体：** DIN Alternate → Bahnschrift → SF Mono → Cascadia Code（`--font-num`，金融终端式数字）
**正文字体：** Inter → 系统栈 → PingFang SC / Microsoft YaHei（`--font-ui` + `--font-cn`）
**等宽：** SF Mono / Cascadia Code / Source Code Pro

**Character:** 中文黑体的端正 + DIN 数字的速度感。数字永远是表格里最对齐的东西。

### Hierarchy
- **Display**（20px/700，font-num）：指数点位、大额资金、温度读数等"一眼要看到"的数字。
- **Title**（16px/700）：区块标题、K 线页股票名。
- **Body**（13px/400）：默认正文、按钮、输入框。
- **Label**（11px/500）：列表行、表格单元格、徽章。
- **Micro**（10px/400，tabular-nums）：口径标签、来源标注、时间戳。

### Named Rules
**六档铁律 Rule。** 字号只允许 10/11/12/13/16/20（px）；一切数字用 tabular-nums；金额量自动「亿/万」缩放（走 `lib/format.ts` 的 toAmount 系，禁止手抄换算）。

## Layout

桌面双栏外壳：左侧分组导航（可折叠，AI 参谋为独立高亮卡）+ 主内容区；顶部多页签工作台（keep-alive，hidden 页签停轮询）。密度体系 `--density-unit: 4px` 基准（compact 3px / comfortable 6px，`html[data-density]` 切换），页面间距走 `--density-*` 派生。移动端：底部导航 + 免责条占位补偿（`--disclaimer-h`）。焦点模式（`data-focus-mode`）收起侧栏与免责条，只留一枚 chip——盯盘时一屏只有行情。长列表用 `content-visibility: auto`（.list-window）代替虚拟滚动。

## Elevation & Depth

**明度分层为主、阴影为辅的混合系统。** 暗色几乎完全靠明度差（bg 15.0 → card 19.8 → hover 23.1 → muted 25.1 → accent 27.9 的 oklch L 梯度）；亮色用纸面白微提 + 轻中性阴影（无彩色阴影）。

### Shadow Vocabulary
- **shadow-float**（`0 8px 24px hsl(fg/0.10), 0 2px 6px hsl(fg/0.06)`）：全站唯一阴影档，只给真浮层（弹窗/菜单/命令面板）。
- **card 静态**（`0 1px 2px …0.05` 微影 + 暗色 `inset 0 1px 0 …0.045` 顶部内高光）：卡片"浮起一档"的暗色表达。
- **card-hover**（亮色提亮一档 + `0 4px 14px -6px` 中性影；暗色 s2 提亮 + 内高光 0.06）：hover 层级 = 亮度抬升，禁彩色光晕。

### Named Rules
**单浮层阴影 Rule。** shadow-lg/xl/2xl 禁用；结构与"浮不浮起来"无关的信息用 hairline 表达。（2026-10-24 起卡片阴影按收益解禁，但"多档阴影混用表达伪层级"仍是禁手。）

## Shapes

微圆角终端语言：基准 `--radius: 0.375rem`（6px），Tailwind 档位由它派生（sm −4px=2px / md −2px=4px / lg 基准 / xl +2px / 2xl +4px）。行类元素走 4px 内圆角（sida-flash border-radius: 4px）；徽章/圆点全圆（9999px）。边框即结构：hairline（1px，`border-border`）是默认分组语言，`border-border-strong` 只用于卡片边界。角色条是 2px 左侧竖条（role-strip::before），是全站唯一的位置性彩色标记。

## Components

### Buttons
- **Shape:** 圆角 lg（6px），高 42px（h-10 + py-2.5），text-13px。
- **Primary:** 锚点钢蓝底白字（btn-primary），active 轻微下压 scale(0.96)。
- **Secondary:** 浮层灰底 + hairline 边框（btn-secondary）。
- **Destructive:** 红 destructive，仅用于危险操作（如空头司令按钮），与涨跌红语义区分靠上下文与文字标签。
- **Focus:** 全站统一 2px 品牌色外环（:focus-visible，offset 2px）；行内元素用 inset 3px 左侧环（row-focusable）。

### Chips / 徽章
- 口径徽章（tdx_exday4 等）：10px 字号 + muted 底，必须随资金数据出现；涨跌相关徽章红绿底 tint（--stock-up-tint/-down-tint）。

### Cards / Containers
- **Corner Style:** xl（8px）。
- **Background:** s1 纸面白（暗色 13.5%）。
- **Shadow Strategy:** 静态微影/内高光，hover 亮度抬升（见 Elevation）。
- **Border:** border-strong 1px。
- **变体:** card-subtle（accent/20 底，次级信息）；hairline 分隔面板（s-panel，无卡片）仍是列表类内容的默认。

### Inputs / Fields
- h-10，hairline 边框，focus 时 ring-primary/20 + 边框转 primary；placeholder muted-foreground。

### Navigation
- 侧栏：分组标签 10px muted；AI 参谋独立高亮卡（role-watch 边框 + 箭头暗示 + 动作化副标题）；当前项 s3 底。
- 行：row-hit 32px 最小命中区，row-hover 提亮到 s2，row-selected s3，左侧角色条标识归属。

### Signature: 涨跌闪 + 数字入场
- `.sida-flash-up/down`：价格变化的背景闪烁（HSL 三元组 + --flash-alpha 0.22，强度按 |Δ| 走 --flash-dur，上限 700ms）。
- `.sida-digit-in`：大数字变化的逐位入场（35% 上移入场，--dur-fast）。
- 全部动效走 --dur-*/--ease-* token，`prefers-reduced-motion` 在 token 层全局归零。

## Do's and Don'ts

### Do:
- **Do** 用 Surface 阶梯（--s0…--s4）表达空间层级，新代码一律走阶梯不再手调透明度。
- **Do** 给每个可交互元素可点击暗示：箭头（ChevronRight 12px）、hover 亮度抬升、按钮形态三选一。
- **Do** 涨跌场景同时给颜色 + 符号/文字/位置（兼容色盲）。
- **Do** 同屏多口径资金数据各带 caliber 徽章；结论先行的区块把结论放表头。
- **Do** 卡片/hairline 按内容形态选择：列表行走 hairline，需要"一块结论"时用 card（2026-10-24 解禁后按收益判断）。

### Don't:
- **Don't** 让任何非语义色比红绿更抢眼；不要用彩色阴影/彩色光晕表达 hover。
- **Don't** 编造数据：0 冒充缺失、跨源换算、无基准的胜率，一律禁止。
- **Don't** 混用「数智参谋」（AI 对话工作台）与「数智决策」（三指标共振选股）两个功能名。
- **Don't** 引入字号六档之外的字号；不要手写金额换算字符串。
- **Don't** 动画绕过 motion token（新动效只用 --dur-*/--ease-*，reduced-motion 自动降级）。
