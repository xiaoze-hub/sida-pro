# 提案面板设计方案（v1，2026-10-22）

> 状态：设计定稿，待派活实现。
> 关联：`src/core/harness_proposals.py`（采集层）、`src/web/api/copilot.py:723-781`（三个端点）、
> `src/agents/chat/registry.py:2560-2627`（`note_new_dimension` 工具，当前 `schema=None`）。

---

## 1. 它解决什么

参谋的分析维度写死在 11 份手写 YAML 配方里。用户在对话里问到一个配方没覆盖的维度
（「股东户数变化」「龙虎榜席位风格」）时，助手只能拿现有工具凑合，或者说「平台没有」。
用户每次都要重新解释一遍，能力缺口没人知道。

采集层已经在记这件事（`harness_proposals` 表 + `note_new_dimension` 工具），
但**页面上看不见** —— 攒下来的维度躺在库里，用户不知道参谋缺什么，也没法拍板补哪个。

本面板 = 「参谋缺什么能力」的可见清单 + 人的裁决入口。

## 2. 定位（不是卡片墙）

这个面板的价值是**排序**，不是展示。用户要一眼看出「哪个维度我提得最多、
最该补」，然后花 3 秒决定补不补。所以：

- **一行一条**，hairline 分隔，不用卡片
- **次数排最前**（决策依据），不是时间
- 理由 2 行截断，证据点开才看
- 三个操作永远在行内，不用进详情页

## 3. 信息层级（三层）

```
┌ 提案 ─────────────────────── ✕ ┐
│ 待审 3 · 采纳 1 · 忽略 0 · 搁置 0     │  ① 汇总带（一行）
│ 最该补：股东户数变化 ×5               │
├───────────────────────────────────┤
│ [待审] [采纳] [忽略] [搁置] [全部]    │  ② 过滤带
├───────────────────────────────────┤
│ 股东户数变化                ×5 待审  │  ③ 列表（一行一条）
│ 配方只覆盖十大股东，看不到户数变化趋势 │
│ 600111 · demon_diagnosis · 10-20 起  │
│              [采纳] [忽略] [搁置]     │
├───────────────────────────────────┤  ← hairline
│ 龙虎榜席位风格              ×2 待审  │
│ …                                 │
```

**① 汇总带**：`summary` 端点的四个数。数字用 `tabular-nums`，标签 `muted` 11px。
第二行「最该补」取 `stats` 的 `top`，维度名 + 次数用 `accent` 色 —— 全屏唯一彩色权重，
它就是这句话的重点。

**② 过滤带**：五个 tab，11px，选中态 `border-role-watch/60 text-role-watch`
（与既有 `过程/记忆/定时` 三个 tab 完全同款，不另立样式）。

**③ 列表行**：
| 位置 | 内容 | 规格 |
|---|---|---|
| 首行左 | 维度名 | 13px `ink`，单行截断 |
| 首行右 | `×N` + 状态徽标 | 11px，N `tabular-nums`；徽标按状态上色 |
| 次行 | 理由 | 11px `muted`，最多 2 行，超出省略 |
| 三行 | 元信息 | 10px `muted`：`标的 · 配方 · 首现日期`，缺失项整段省略（不显示 `-- ·  --`） |
| 四行 | 操作 | 11px 文本按钮，右对齐 |
| 展开 | 证据 | 点行展开，`evidence` 原样展示（`whitespace-pre-wrap`，≤200 字 + 滚动） |

**排序**：待审 tab 内**前端按 `seen_count` 降序**重排（后端列表端点按 `last_seen_at` 降序，
但决策依据是次数）。次数相同按 `last_seen_at` 降序。其余 tab 保持后端顺序（时间序更自然）。

## 4. 状态机

```
pending ──accept──→ accept ┐
        ──reject──→ reject ├─ 都是终态：不再出现在「待审」
        ──snooze──→ snooze ┘   （同一维度再次被提 → 新建 pending 行，不受历史压住）
```

后端唯一约束 `(user_id, normalized_dimension, status)` **故意带 status**，
所以「拒绝过的维度再次出现」会重新生成 pending 行 —— 面板上会看到同一维度
出现在不同 tab 里，这是设计意图，不是 bug。

## 5. 红线（必须在 UI 上说清）

**采纳 ≠ 已生效。** `decide()` 只改 `status`，一个字都不改 YAML 配方。
用户点「采纳」后若以为参谋下次就会用这个维度，是**产品级欺骗**。

处置：采纳成功后显示一行提示 —— 「已采纳 · 配方仍需人工修改后才会生效」，
`muted` 11px，不弹 toast 不弹窗。这句文案是本面板的诚信底线，删了就是幻觉。

同理：**空不等于零**。没提案 = 「还没有提案」，不是「0 个提案」；
`seen_count` 缺失显示 `--`，不补 0。

## 6. 空 / 错 / 载 三态

| 态 | 呈现 |
|---|---|
| 加载 | 行骨架 ×3（`animate-pulse`，高度对齐真实行），不用 spinner 居中 |
| 空（无提案） | 居中一行：`还没有提案`（13px）+ 一行说明：`当你在对话里问到一个配方没覆盖的维度时，参谋会记在这里`（11px muted） |
| 空（某 tab 无记录） | 只有一行 `这个分类下没有提案`，不显示说明句 |
| 错误 | `加载失败` + `重试` 文本按钮；**不许伪装成空态** —— 接口挂了和真的没有是两件事 |

## 7. 视觉规范（照 `sida-ui-craft`）

- 主色 ≤5；面板内唯一彩色是「次数 ×N」的 `accent` 与状态徽标
- 间距只用 `p-2/p-3/p-5/p-8`、`gap-2/3/5/8`；**禁 16/24**
- 字号只取 `[10,11,12,13,16,20]`
- 阴影零层：层级靠 hairline（`border-border/40`）
- 数字列 `font-variant-numeric: tabular-nums`
- 圆角 ≤ `rounded-lg`

## 8. 数据契约（后端已就绪，不改）

```
GET  /api/copilot/proposals?status=pending&limit=50
     → [{id, dimension, normalized_dimension, reason, evidence, symbol,
         recipe, status, seen_count, first_seen_at, last_seen_at, decided_at}]
     注：status 不传 = 全部；传空串按不传处理

POST /api/copilot/proposals/{id}/decide   body {action: accept|reject|snooze}
     → {ok: true, proposal: {...}}
     404 = 不存在或不属于本人；400 = action 非法

GET  /api/copilot/proposals/stats
     → {items: [...], top: {...}|null, summary: {total,pending,accept,reject,snooze}}
```

`limit` 后端夹在 1..200。面板固定用 50。

## 9. 文件清单

**新增**
- `frontend/src/lib/copilot/proposals.ts` — 类型 + 纯函数（排序 / 时间格式化 / 元信息拼接 / 状态文案）
- `frontend/src/hooks/useCopilotProposals.ts` — 取数（列表 + stats）+ decide + 局部刷新
- `frontend/src/components/copilot/ProposalPanel.tsx` — 面板

**修改**
- `frontend/src/pages/AiCopilot.tsx` — `panelTab` 联合类型加 `'proposals'`；tab bar 加第 4 个；
  aside 标题与内容分支

**测试**
- `frontend/src/lib/copilot/__tests__/proposals.test.ts` — 纯函数（排序稳定性、缺失值、时间格式）
- `frontend/src/components/copilot/__tests__/proposal-panel.test.tsx` — 三态 + 采纳后提示文案 +
  「采纳 ≠ 生效」文案存在性 + 按次数排序

## 10. 验收线

1. `npx tsc -b` 0 错
2. `npx vitest run src/components/copilot src/lib/copilot src/pages` 全绿
3. `node scripts/check_ui_rules.mjs` 通过（字号白名单 / `safeFixed` / 无 `shadow-lg`）
4. 面板在无提案时显示空态而非 0
5. 点「采纳」后出现「配方仍需人工修改」提示
6. 待审列表按 `seen_count` 降序

## 11. 明确不做（本轮）

- **不改 `note_new_dimension` 的 `schema=None`** —— 让模型真的会调用属后端改动，
  单独一轮做（含 schema 契约测试）。本轮面板先能显示库里已有数据。
- 不做提案进配方的自动化（红线：配方只由人改）
- 不做提案的编辑 / 合并 / 手动新建
- 不动 `/copilot-governance` 治理页
- 不改后端任何文件
