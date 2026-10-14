# AI 看图形态 影子证据层 — 影子运行验证模板

> **基线标识**: main = `dd6f811`(v0.13.70);本模板对应分支 `feat/ai-vision-pattern-20261014`。
> **审计/盘点对象完整路径**:
> - 看图识别编排/渲染/解析:`src/core/ai_vision_pattern.py`
> - 影子落库表 `ai_pattern_shadow`:`src/web/migrations.py::_m195`
> - 调度:`src/bootstrap/startup.py`(交易日 15:40, 独立作业 `ai-vision-pattern`)
> - 只读展示/触发:`src/web/api/ai_pattern.py`(`GET /api/ai-pattern/shadow`)、
>   注册:`src/web/app.py`(prefix `/api/ai-pattern`)
> - 规则形态对照口径:`src/core/kline_patterns.py`(`detect_pattern_marks`)
> - 视觉模型解析:`src/core/ai_client.py::get_model_for_scene(db, "vision")`
> - 标的池(自选∪持仓):`src/core/quote_snapshots.py::collect_market_symbols`
> **覆盖范围**: 覆盖 K 线→PNG 本地渲染、视觉模型解析/调用/输出解析、与规则形态同日同票对照、
> 影子表迁移、每日盘后打标作业入口、只读 API。
> **未覆盖(诚实声明)**: ① 未做真实视觉模型网关联调(测试全 mock; 生产首日盘后需人工核对
> 识别真值); ② 未在前端渲染 AI 形态标注(留给后续任务, 本任务不碰 `KlineChart.tsx`);
> ③ 渲染器为**本仓库新建**的 Pillow 版(`screenshot_collector` 截的是外部东财/雪球页面, 不可控
> 窗口且需外网, 不适合作同一窗口的对照口径); ④ 标的池为全用户自选∪持仓并集(无 user_id 隔离,
> 与 `limit_strength_shadow` 同类, 供全局证据累积); ⑤ 未触碰任何现有信号/决策/展示语义(影子运行)。

---

## 0. 一句话

**影子运行**:每日盘后对**自选∪持仓**逐票做「K线渲染 → 视觉 LLM 认形态(形态名/置信度/依据)」,
与 `kline_patterns` 规则形态**同日同票并排**落 `ai_pattern_shadow` 一张只读表,**不改任何现有信号/
决策**。AI 看图形态是否比纯规则更有价值,靠**20 个交易日后的分桶对比**回填判定,不提前下结论。

信号是**证据非建议**;全流程禁「建议买入」类措辞;形态名只客观描述。

## 1. 识别口径

| 维度 | 取值 | 来源 |
|---|---|---|
| 标的池 | 自选∪启用账户持仓(CN) | `quote_snapshots.collect_market_symbols()` |
| K 线窗口 | 最近 60 根日 K(可配 `DEFAULT_WINDOW`) | `KlineCollector.get_klines`(PG 优先) |
| 渲染 | 蜡烛图 PNG(Pillow, 本地无网络, 红涨绿跌+成交量副图) | `ai_vision_pattern.render_kline_png` |
| 视觉模型 | scene=`vision` 绑定模型(需 `capabilities` 含 `vision`) | `ai_client.get_model_for_scene` |
| AI 输出契约 | 严格 JSON:`{"patterns":[{name,confidence,basis}],"summary"}` | `build_vision_prompt` / `parse_vision_result` |
| 规则侧对照 | `detect_pattern_marks(bars, lookback=5)` 的形态名集合 | `kline_patterns` |

**状态语义(缺数据显式, 禁编造)**:

| status | 含义 | 是否出形态 |
|---|---|---|
| `ok` | 视觉可用并完成判读(可能判无形态) | 是(可空) |
| `degraded` | 无视觉模型 / 调用失败 / 输出不可解析 | **否**(绝不编造) |
| `no_data` | K 线不足(<3 根)或取不到 | 否 |

**对照结论 `compare_status`(四组分桶口径)**:

| 值 | 含义 |
|---|---|
| `agree` | AI 与规则都出形态且形态名有交集 |
| `conflict` | 两侧都出形态但**无交集**(分歧) |
| `ai_only` | 仅 AI 出形态 |
| `rules_only` | 仅规则出形态 |
| `none` | 两侧皆判无形态(不计入四组) |
| `degraded` | AI 侧降级(无模型/失败), 无法对照 |

## 2. 分桶对比口径(判定是否有效)

**目标**:验证「AI 看图形态」相对「纯规则形态」的 T+1 / T+3 命中率是否有显著差异。

- **样本单位**:单个 (交易日, 标的) 对。
- **命中定义**(择一,须在结论里锁定并写死):
  - 口径 H1(收盘命中):T+1 收盘涨跌幅 > 0 记命中;
  - 口径 H2(隔日日内命中):T+1 最高价 > T 日收盘价(有超额空间)记命中。
- **分组**:**四组对照** —— `agree` / `conflict` / `ai_only` / `rules_only`(`none`/`degraded`
  单列观察, 不进四组主判决)。可再按 `ai_pattern` 主形态名细分。
- **统计量**:每组命中率、样本数、平均 T+1/T+3 收益;关键差值:
  `ai_only − rules_only`(AI 独有形态的增量价值)、`agree − conflict`(共识 vs 分歧)。
- **样本门槛**:**每组样本 ≥ 30 才出结论**;不足则该组标「样本不足, 不出结论」。
- **观察窗口**:**20 个交易日**内只累积、不判定;第 20 个交易日后回填判定。
- **显著性提示**:给 Wilson 95% 置信区间(命中率是比例, 别用正态近似糊弄小样本)。

## 3. 回填判定表(第 20 个交易日后填写)

| 组 | 样本数 | T+1 命中率 | T+3 平均收益 | Wilson 95% CI | 结论 |
|---|---|---|---|---|---|
| agree | | | | | 样本<30 → 不出结论 |
| conflict | | | | | |
| ai_only | | | | | |
| rules_only | | | | | |
| none(单列) | | | | | |
| degraded(单列) | | | | | |

**判定规则(先写死,防事后调参)**:
1. `ai_only` 组命中率**显著高于** `rules_only` 组(95% CI 不重叠)→ AI 看图形态有规则之外的
   增量价值,考虑把 AI 形态纳入图标注/信号候选;
2. `agree` 显著高于 `conflict` → 「AI 与规则共识」是有效条件化维度;
3. 无显著差异 → AI 看图形态**未证伪未证实**,记入否决/或改提示词与窗口后重跑;
4. 结论**一律不得**表述为「建议买入」,只写「统计上 X 组优于 Y 组」。

## 4. 只读取数(能跑多少跑多少,缺口显式)

- 读影子表:`GET /api/ai-pattern/shadow?date=YYYYMMDD`(缺省最近一日)→ `rows` 含 AI 主形态/
  置信度/依据/模型名/窗口 + 规则形态 + `compare_status`;`counts` 为对照分桶计数。
- 回填收益:用 `get_klines` 取样本日后 T+1/T+3 收盘/最高价计算,**只读, 不写任何表**。
- 缺口:样本日 AI 侧为 `degraded`(无视觉模型/调用失败)时显式计入 `degraded`, **不进四组、
  不臆造形态**;缺口占比过高时明说「AI 侧不可用, 结论推迟」。

## 5. 验收清单(影子期)

- [ ] 每日盘后 `ai-vision-pattern` 作业成功(日志「AI 看图形态影子」),表内当日行 status/compare 分布合理;
- [ ] `GET /api/ai-pattern/shadow?date=` 返回 `available/trade_date/rows/counts`;
- [ ] **无任何现有信号/决策/展示语义改变**(diff 仅新增模块/表/端点/作业/文档);
- [ ] 无视觉模型时全为 `degraded`(绝不出现编造形态);
- [ ] 满 20 交易日后按 §3 回填判定,结论回写本文件(带 commit)。
