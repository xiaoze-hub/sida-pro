# 借鉴 self-hosting-agent 的工程机制设计方案

> 来源：`github.com/jiafeimao-gjf/self-hosting-agent`（Apache-2.0，零依赖 TS）
> 目标：给 SIDA-Pro / 数智参谋补上**「承诺可追溯」**与**「自我改动可回滚」**两块能力
> 日期：2026-10-22

---

## 〇、为什么要做这个（真实触发点）

不是"抄个新鲜玩意"，是今天踩了两个坑：

**坑 1：承诺没有守卫 → 反复人工核对**
今天核对「数智参谋 59 个工具到底注册了没、CYQ 修没修」，结论是**我此前的判断是错的**
（我曾说"未注册、CYQ 未修"，复核后发现 59 个工具全在、`_k_get()` 已修）。
根因不是不认真，是**"承诺"只存在于对话和文档里，没有机器可查的绑定关系**。
`tests/test_w33_chat_registry.py` 里硬编码了一份 `EXPECTED_CORE_ORDER` 列表——
但它和 `src/agents/chat/registry.py` **没有规格 ID 绑定**，改了一边另一边不会报错。

**坑 2：自我改动没有回滚 → 靠人肉救火**
今天重建恒生官网前端时，我手写重建丢了原版一半内容，用户发现"以前不长这样"，
最后靠服务器备份 + 逆向 bundle 还原。**过程里三次部署失败**，每次都是
"看起来成功实际失败"（rsync 静默跳过 / 上传顺序错 / 目录同步互相覆盖）。
自举项目给了一条更硬的路：**改坏留不下来**。

---

## 一、要借什么，不借什么

### ✅ 借（机制，可直接移植）

| 机制 | 源码出处 | 移植形态 |
|---|---|---|
| **规格⇄测试双向门禁** | `scripts/trace.mjs`（179 行 JS） | Python 版 `scripts/trace_specs.py` + CI job |
| **自举三闸门** | `src/orchestrator/client-source.ts`（434 行） | `src/harness/selfmod.py`，管 harness 自身的改动 |
| **界面 = f(事件日志)** | `src/eventlog/` + `src/surface/` | 数智参谋的「回放即还原」 |
| **不安全的 shell 的正确姿势** | `src/runtime/agent-main.ts` + SPEC-023 | 已有 Hermes 审批门，可借鉴其"默认不存在"思路 |

### ❌ 不借（架构假设不匹配）

| 项目的东西 | 为什么不借 |
|---|---|
| 浏览器 + 本地服务客户端 | Hermes 是微信/企微/Telegram 多端，SIDA-Pro 是 Web 大屏 |
| Node 24 原生跑 TS | 我们是 Python 3.11 + FastAPI，栈不同 |
| 自己写 Markdown 渲染器 | Hermes 已有成熟渲染链 |
| 内置浏览器沙箱 iframe | 我们走 Playwright/浏览器工具，能力更强 |
| `tool.reply` 帧协议 | Hermes 的 `delegate_task` 已覆盖编排需求 |
| 零依赖约束 | 我们的依赖栈（FastAPI/PG/TimescaleDB/Redis）不需要为此让步 |

**结论：借机制，不借代码。** 11K 行 TS 硬搬进 Python 项目，架构假设差异大于收益。

---

## 二、方案一：规格追溯门禁（优先做，收益最大）

### 2.1 核心思想

给 SIDA-Pro 建立**机器可查的承诺↔守卫绑定**：

```
specs/                          ← 规格事实源（人写）
  harness/
    premarket_strategist.md      ← 每个 recipe 一份
    tools/
      chat_registry.md           ← 59 个工具一份
  schemas/
    tool_contract.schema.json    ← 工具返回契约

tests/
  test_spec_premarket_strategist.py
  test_spec_chat_registry.py

scripts/trace_specs.py           ← 双向校验门禁（CI 硬门禁）
```

**双向**校验（缺一不可）：

| 违规 | 含义 | 对应今天的事故 |
|---|---|---|
| 规格条目没有测试引用 | 文档里的空头承诺 | 「CYQ 承诺修了但没人验」 |
| 测试引用不存在的规格 ID | 测试在验证幻觉 | 测试写了个不存在的需求编号 |
| schema 与代码不一致 | 契约漂移 | 工具返回结构变了，前端静默崩 |
| **规格文件一条标准都没识别出来** | **门禁自己骗人** | `BROWSER-` 前缀超正则导致整条规格隐形 |

最后一条是 self-hosting-agent 里最值得学的一点——它自己的注释写着：
> 自检：某个规格文件一条标准都没识别出来，多半是 ID 命名超出正则（历史上就发生过）
> 于是整条规格对门禁隐形、还报「全部覆盖」

**门禁骗人比没有门禁更危险**，因为它给人虚假的安全感。

### 2.2 规格 ID 命名规范

```
PREFIX-NNN     前缀 2-7 位大写字母+数字，NNNN 三位序号

RECIPE-001     harness recipe 的行为约定
TOOL-001       单个 chat 工具的契约
DATA-001       数据源口径约定
RISK-001       风险/红线约定
E2E-001        端到端场景
```

**正则必须与实际前缀长度一致**，且门禁要自检"每个规格文件至少识别出 1 条标准"。

### 2.3 落点：优先覆盖三个高价值区

**① 59 个 chat 工具的契约**（收益最高）

现在 `tests/test_w33_chat_registry.py` 硬编码工具列表。改成：

```python
# specs/tools/chat_registry.md
## 验收标准

- **TOOL-001** 每个 chat 工具必须注册 caliber(口径标签)且非空
- **TOOL-002** get_capital_flow 的 caliber 必须是固定红线文案(第三方估算≠机构持仓)
- **TOOL-003** 工具总数与 specs 声明一致, 新增工具必须同步 specs
- **TOOL-004** 每个工具必须有 requires 权限点
- **TOOL-005** 每个工具的返回 schema 必须符合 tool_contract.schema.json
```

```python
# tests/test_spec_chat_registry.py
"""@spec TOOL-001
验证: 每个 chat 工具注册 caliber 且非空。
"""
def test_every_tool_has_caliber():
    ...

"""@spec TOOL-003
验证: 工具总数与 specs 声明一致 —— 新增工具漏改 specs 会在此失败。
"""
def test_tool_count_matches_specs():
    declared = count_from_specs()      # 从 specs/ 解析
    actual = len(CHAT_TOOLS)
    assert actual == declared
```

**这一条直接根治今天的核对痛苦**：以后新增工具忘了改 specs，门禁立刻红。

**② 11 个 harness recipe**

每个 recipe 的 YAML 里补 `acceptance:` 段，列出该 recipe 必须满足的行为：

```yaml
id: premarket_strategist
...
acceptance:
  - id: RECIPE-001
    rule: 输出必须含 output_sections 列出的全部段落名, 原文照抄
  - id: RECIPE-002
    rule: 禁止出现"买入/卖出"字样(该 recipe 明确只给观察点)
  - id: RECIPE-003
    rule: max_tool_calls 不超过 budget 声明
```

**③ 数据口径**（`data_catalog.yaml` 已有雏形，正好接上）

```
DATA-001  get_capital_flow 返回的是第三方估算, caliber 必须标注
DATA-002  主力意图必须走 get_main_intent(逐笔), 不得用 get_capital_flow 替代
DATA-003  K线基准日滞后时 UI 必须显式标注, 不得改数据
```

### 2.4 门禁实现（Python 版，约 200 行）

```python
#!/usr/bin/env python3
"""规格追溯门禁 —— 借鉴 self-hosting-agent/scripts/trace.mjs。

双向校验, 任一条不满足即非零退出:
  1. 规格有验收标准, 却没有任何测试引用它   → 文档里的空头承诺
  2. 测试引用了不存在的规格 ID              → 测试在验证幻觉
  3. 每个规格文件至少识别出 1 条标准        → 门禁自己骗人
  4. schema 与代码不一致                    → 契约漂移(可选, P2)
"""
```

**要点**：
- ID 正则 `[A-Z][A-Z0-9]{1,7}-\d{3}` —— 与实际前缀长度对齐
- **自检**：逐个规格文件数识别到的标准数，为 0 直接抛错（这是关键）
- 测试绑定：约定 `# @spec TOOL-001` 注释紧邻测试函数
- 产出 `specs/traceability.md`（自动生成，勿手改）

### 2.5 CI 接入

```yaml
# .github/workflows/spec-gate.yml
name: spec-traceability
on: [push, pull_request]
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<pin>
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
      - run: python scripts/trace_specs.py
      - run: pytest -q tests/test_spec_*.py
```

**门禁位置很重要**：放在 **push 必过**，不是 schedule。schedule 是"事后发现"，
push 才是"事前拦截"。

### 2.6 渐进落地（3 期，别一次全上）

| 期 | 范围 | 工作量 | 立即收益 |
|---|---|---|---|
| **P0** | 门禁脚本 + 59 工具契约 + 3 条最关键标准 | 半天 | 新增工具漏改 specs 立刻红 |
| **P1** | 11 个 recipe 验收标准 + E2E 场景 | 1 天 | recipe 改坏有守卫 |
| **P2** | schema 一致性 + 数据口径标准 | 2 天 | 契约漂移提前发现 |

**P0 的 3 条标准建议**（先挑最痛的三条）：
```
TOOL-001  每个工具 caliber 非空
TOOL-002  get_capital_flow caliber 是固定红线文案
TOOL-003  工具总数与 specs 声明一致
```

---

## 三、方案二：自举闸门（守护 harness 自身）

### 3.1 为什么 SIDA-Pro 需要

今天恒生官网的事故暴露了一个通用风险：**Agent/脚本改自己的代码时，改坏了要靠人肉救**。
SIDA-Pro 的 harness（11 个 recipe + 4 个 skill + 59 个工具契约）正是"Agent 会改的对象"——
将来 Harness² 那套"自我改进"跑起来，改的就是这些。

### 3.2 三条规矩（直接照搬 self-hosting-agent）

```
1. 只能改白名单目录 —— 内核不可被被监管者改写
2. 改完必须过自检, 不过就逐字节回滚
3. 每次成功变更留版本、留 diff、留审计
```

**映射到 SIDA-Pro**：

| self-hosting-agent | SIDA-Pro 对应 |
|---|---|
| 只能改 `src/client/**` | 只能改 `src/harness/recipes/`、`src/harness/skills/`、recipe YAML |
| 内核不可改 | `src/agents/chat/registry.py`、`src/core/`、`src/harness/trace.py` **禁止**被自举流程改动 |
| 自检不过回滚 | 跑 `pytest tests/test_spec_*.py` + 门禁；不过 → 逐字节还原 |
| 留版本/diff/审计 | 落 `harness/changes/<ts>/` + 事件表 |

### 3.3 实现要点

```python
# src/harness/selfmod.py
class HarnessSelfMod:
    """Harness 自改动闸门。

    三条规矩(每条都有测试守着):
      1. 只能改白名单目录 —— 内核不可被被监管者改写
      2. 改完必须过自检, 不过就逐字节回滚
      3. 每次成功变更留版本、留 diff、留审计
    """
    WRITABLE = ["src/harness/recipes/", "src/harness/skills/"]
    FORBIDDEN = ["src/agents/", "src/core/", "src/harness/trace.py"]
```

**关键实现细节**（都是 self-hosting-agent 踩出来的）：
- **逐字节回滚**：不是 git checkout（可能有未提交改动），而是存 `previous_content` 字节流
- **回滚也记审计**：否则"回滚→重启→回滚失效"
- **自检失败不留半个文件**：先跑自检再落盘，或临时文件 + 原子 rename
- **路径校验在动磁盘之前完成**：绝对路径/盘符/`..`/符号链接全拒

### 3.4 边界：明确不做什么

```
❌ 不允许自举流程改 registry.py(改了就是绕过工具契约)
❌ 不允许自举流程改 trace.py(改了就是绕过审计)
❌ 不允许自动改 SYSTEM_PROMPT / guardrails / 交易红线
❌ 不允许自动合并到主干 —— 产出只是「建议 + diff」，合不合并由人决定
```

最后一条尤其重要：**借鉴项目里 Agent 能改自己源码并自动生效**，
但那是"客户端"场景；SIDA-Pro 是**金融决策系统**，自动生效的爆炸半径太大。
我们只借鉴「改完能回滚」和「留 diff 留审计」，**不借鉴「自动生效」**。

---

## 四、方案三：界面 = f(事件日志)

### 4.1 借鉴点

self-hosting-agent 的一条漂亮不变量：
> 持久化没有新增一套存储，而是让**已有不变量覆盖界面自己**：`界面 = f(事件日志)` 对界面也成立。

它因此得到了：界面版本持久化、版本下拉（`v7 · 21:04:12 · 1 个区块`）、一键回滚、
且回滚后重启仍是回滚后的样子。

### 4.2 SIDA-Pro 的对应

我们已有 `src/harness/trace.py`（`copilot_runs` / `copilot_steps`）。
**现在 trace 只做排障与回放，不参与渲染**。可以补一层：

```
trace 事件 → 投影 → 参谋界面的「回放」视图
```

价值场景：
- 「这周的参谋建议是怎么来的」→ 按 trace 回放
- 「改了 recipe 后效果变好还是变坏」→ 同一问题在两个 recipe 版本下的 trace 对比
- 「这个结论当时的数据快照是什么」→ trace 里已有工具调用摘要

### 4.3 工作量与优先级

**P2，可延后**。收益真实但不如前两个方案紧迫。

---

## 五、落地顺序与验收

```
P0 (半天)  门禁脚本 + TOOL-001/002/003 + CI job
            验收: 故意删一个工具 → CI 红; 故意改 specs 不改代码 → CI 红
P1 (1天)   11 个 recipe 验收标准
            验收: 故意改坏 recipe 输出段落 → 对应测试红
P2 (2天)   数据口径标准 + schema 一致性
P2 (2天)   selfmod.py 自举闸门(只做"建议+diff", 不做自动生效)
P3         trace 回放视图
```

**每期独立可交付**，不要一次全上。

---

## 六、与现有体系的关系

```
现有:
  src/harness/          ← 11 recipes + 4 skills + trace.py
  src/agents/chat/      ← 59 工具注册表
  tests/                ← 481 个测试文件
  .github/workflows/    ← audit / nightly / build-push

新增:
  specs/                ← 规格事实源(人写, 唯一需要人维护的东西)
  scripts/trace_specs.py ← 门禁
  tests/test_spec_*.py   ← 规格守卫测试
  .github/workflows/spec-gate.yml
```

**不新增运行时依赖**，不改变任何业务代码路径。
门禁是**旁路**：它只读 specs 和 tests，不进生产链路。

---

## 七、风险与放弃条件

| 风险 | 缓解 | 放弃条件 |
|---|---|---|
| 规格变成"给测试写的作文" | 标准必须描述**行为契约**而非实现；审查时问"删了这个测试，会漏掉什么 bug" | P0 后没人愿意写新标准 → 停 |
| 门禁误报拖慢 CI | 只在 push 跑；单次 < 10s | 门禁本身不稳定 → 先修再继续 |
| 维护成本高于收益 | P0 只做 3 条标准，验证价值再扩 | P0 跑一个月没抓到过一次问题 → 停 |
| 自举机制被误用 | 明确不做自动生效；白名单 + 禁改清单 | — |

**关键判断**：这套东西的价值**不在工具本身，在"让承诺变成可执行的事实"**。
如果 P0 落地后大家仍然靠人工核对工具清单，说明文化没跟上，应该停而不是加码。

---

## 附：为什么是这三样

| 借鉴的东西 | 为什么对 SIDA-Pro 特别重要 |
|---|---|
| 规格追溯门禁 | 59 工具 + 11 recipe + 数百数据口径，人工核对必然漏 |
| 自举闸门 | Harness² 那套"自我改进"将来改的就是这些，必须先有回滚能力 |
| 界面=f(事件日志) | 让"配方改好改坏"从人工抽检 18 次变成可回放对比 |

**不借的**：客户端外壳、TS 栈、浏览器沙箱、帧协议 —— 这些是它的场景约束，不是它的优点。
