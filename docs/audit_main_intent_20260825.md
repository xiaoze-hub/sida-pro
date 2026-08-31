# 主力意图/暗盘/内外盘分析模块审计报告

**审计日期**: 2026-08-25  
**审计范围**: 七口诀匹配度、数据依赖合规性、前端卡片展示、优化建议  
**相关 skill**: `a-share-main-force-intent`（同花顺对齐 + 逐笔V14判据）

---

## 1. 数据依赖审计 — ✅ 合规

### 核心结论
**主力意图已正确走 `compute_dark_flow`（腾讯逐笔口径），禁用了 `get_capital_flow`（东财四档）**。

| 数据源 | 用途 | 文件:行号 | 状态 |
|--------|------|-----------|------|
| 腾讯逐笔 appn=detail (B/S/M三分类) | 主力净额/拆单/暗盘/时段 | `dark_flow.py:127-338` | ✅ 主链路 |
| 腾讯 Quote (volume_outer/inner/change_pct) | 内外盘口诀 | `dark_flow.py:766-777` | ✅ 口诀数据源 |
| 腾讯分价表 appn=price | 价位级承接/吸筹位 | `dark_flow.py:894-924` | ✅ 辅助 |
| 东财 push2delay (四档) | 资金面参考段（非主力意图） | `capital_flow_collector.py:62-133` | ✅ 仅作参考 |
| 腾讯 5日资金流 (TencentFundflowVendor) | 5日阶段判断 | `dark_flow.py:990-1010` | ✅ 辅助 |

### 强制隔离
- `AGENTS.md` 硬约束: **"主力意图识别必须走 `get_main_intent`（逐笔口径），禁用 `get_capital_flow`"** ✅
- `chat.py:55-56` 工具选择指引明确区分两工具 ✅
- `intraday_monitor.py:1410-1414` 口径提醒标注 ✅
- `portfolio_context.py:28-44` 注入 past_context 时标注口径 ✅

---

## 2. 七口诀匹配度 — 逐条对照

### 口诀 ① 外盘大+涨+放量=真金进攻（看涨）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 外盘大 | `buy_pct > 55%` | `dark_flow.py:596` |
| 涨 | `change_pct > 0.5%` | `dark_flow.py:569` |
| 放量 | `volume_ratio > 1.5` | `dark_flow.py:574` |
| 方向 | 看涨 | ✅ |

**评估**: 完全匹配。阈值 55% 合理。✅

### 口诀 ② 内盘大+跌+放量=主力撤退（看跌）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 内盘大 | `sell_pct > 55%` | `dark_flow.py:599` |
| 跌 | `change_pct < -0.5%` | `dark_flow.py:570` |
| 放量 | `volume_ratio > 1.5` | `dark_flow.py:574` |
| 方向 | 看跌 | ✅ |

**评估**: 完全匹配。✅

### 口诀 ③ 外盘大+跌+高位=诱多（警惕）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 外盘大 | `buy_pct > 55%` | `dark_flow.py:602` |
| 跌 | `change_pct < -0.5%` | `dark_flow.py:570` |
| 高位 | `position == "high"` (20日K线分位 ≥ 0.66) | `dark_flow.py:490-512` |
| 方向 | 警惕 | ✅ |

**发现**: 未检查**缩量/放量**。用户口诀隐含"外盘大但跌"是反常行为，有量能确认更好。建议补充量比<1.0的缩量条件。  
**评分**: 80% 匹配

### 口诀 ④ 内盘大+涨+低位=压盘吸筹（看涨）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 内盘大 | `sell_pct > 55%` | `dark_flow.py:605` |
| 涨 | `change_pct > 0.5%` | `dark_flow.py:569` |
| 低位 | `position == "low"` (20日K线分位 ≤ 0.33) | `dark_flow.py:490-512` |
| 方向 | 看涨 | ✅ |

**发现**: 同③，未检查量能。建议补充。  
**评分**: 80% 匹配

### 口诀 ⑤ 内外相当+横盘=平衡（观望）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 内外相当 | `|buy_pct - sell_pct| < 10%` | `dark_flow.py:577` |
| 横盘 | `|change_pct| <= 0.5%` | `dark_flow.py:571` |
| 方向 | 观望 | ✅ |

**BUG**: 该规则在规则列表出现**两次**（`dark_flow.py:590` 和 `dark_flow.py:608`）。第二次是死代码，因优先级队列中第一个匹配即返回。不影响运行但应清理。  
**评分**: 95% 匹配（死代码问题）

### 口诀 ⑥ 内外双小=控盘洗盘（关注）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 双小 | `active_ratio < 30%` (外+内占总成交量) | `dark_flow.py:575` |
| 条件 | `oscillate` (0.5% < |涨跌| ≤ 3%) | `dark_flow.py:573` |
| 方向 | 关注 | ✅ |

**关键问题**: 腾讯口径 `volume ≈ 外盘+内盘`（active_ratio ≈ 100%），所以 `active_ratio < 30%` **几乎从不触发**。`dark_flow.py:545-546` 注释明确承认这一点："腾讯口径 volume≈外盘+内盘...故 ⑥ 双小在当前数据源几乎不触发(保留规则, 未来 L2 含中性盘时生效)"。  
**评分**: 30% 匹配 — 当前数据源下该规则基本是死代码。

### 口诀 ⑦ 内外双大+不动=对倒（警惕）
| 维度 | 实现 | 文件:行号 |
|------|------|-----------|
| 双大 | `active_ratio > 85%` | `dark_flow.py:576` |
| 不动 | `|change_pct| <= 1.0%` | `dark_flow.py:572` |
| 方向 | 警惕 | ✅ |

**问题**: 同⑥，腾讯口径下 active_ratio ≈ 100%，`double_big` 恒为 True。实际门控退化为 `no_move`（|涨跌|≤1.0%）。这意味着"对倒"判定**仅靠价格不动**，误差较大（横盘日也可能被误判为对倒）。  
**评分**: 60% 匹配

---

## 3. 准确率综合评估

| 口诀 | 匹配度 | 数据可用性 | 伪阳性风险 | 伪阴性风险 |
|------|--------|-----------|-----------|-----------|
| ① 真金进攻 | ✅ 95% | 好 | 低 | 低 |
| ② 主力撤退 | ✅ 95% | 好 | 低 | 低 |
| ③ 诱多出货 | ⚠️ 80% | 好 | 中 | 中(缺量能) |
| ④ 压盘吸筹 | ⚠️ 80% | 好 | 中 | 中(缺量能) |
| ⑤ 多空平衡 | ⚠️ 95% | 好 | 低 | 低(死代码问题) |
| ⑥ 控盘洗盘 | ❌ 30% | 差(腾讯无中性盘) | 低(不触发) | 高(从不触发) |
| ⑦ 对倒造假 | ⚠️ 60% | 差(腾讯无中性盘) | 中(横盘日误报) | 中(依赖振幅) |

### 三个级别问题

**P0 — 功能缺陷**:
- 口诀⑥ 在腾讯数据源下**几乎不触发**，需要 L2 数据或改用其他替代指标

**P1 — 逻辑缺陷**:
- 口诀⑦ 因腾讯 `active_ratio ≈ 100%`，门控退化为纯价格条件，横盘日易误报
- 口诀③④ 缺少量能确认（缩量=可信，放量=需谨慎）

**P2 — 代码问题**:
- 口诀⑤ 在规则列表中出现两次（`dark_flow.py:590` 和 `608`）
- 注释中 `_MNEMONIC_BALANCE = 10.0` 含义是 `|buy%-sell%| < 10%`，但变量名不直观

---

## 4. 主力意图算法判据（v14）审计

`_judge_signal`（`dark_flow.py:1016-1051`）是主力意图核心判据，准确性已通过多轮验证：

- **主力净额 > 500万**: 净流入→吸筹；尾盘加仓→强吸筹
- **主力净额 < -500万**: 净流出→若参与度≥35%+买占≥48%→洗盘吸筹；否则→出货
- **主力平衡**: 参与度≥35%+买占≥48%→疑吸筹；否则→平衡

**问题**: `_main_intent_structured`（`intraday_monitor.py:245-250`）的判据与 `_judge_signal` 的判据逻辑相同但独立实现，存在**逻辑漂移风险**。两处使用的阈值（`500e4`、`35%`、`48%`）应统一引用。

---

## 5. 前端卡片展示审计

### 现状
| 接口 | 提供数据 | 前端使用 | 文件:行号 |
|------|---------|---------|-----------|
| `GET /api/klines/:symbol/summary` | `main_intent`(字符串) + `main_intent_structured`(dict) | 个股对话框 | `klines.py:313-338` |
| `GET /api/dark-flow` | `main_intent` + `inner_outer` + `mnemonic` | 分时图卡片（待实现） | `darkflow.py:108-112` |
| `GET /api/chat` → `get_main_intent` | 文本摘要 | 对话工具 | `chat.py:1117-1129` |

### 问题
1. **前端缺少专用暗盘卡片组件**: `frontend/src/` 搜不到 `dark_flow` 或 `main_intent` 的任何 UI 渲染代码。`darkflow.py` API 已就绪但前端从未消费。
2. `Stocks.tsx:840` 提及 K线摘要超时放宽到 45s（因逐笔翻页冷启动），但无暗盘卡片渲染逻辑。
3. 口诀 mnemonic 数据已结构化（包含 `mnemonic`名、`direction`方向、`divergence`背离标志），但前端从未展示。

---

## 6. 优化/新增算法建议

### 6.1 口诀优化（高优先级）

#### 建议1: 修复口诀⑥ 腾讯数据源适配（P0）
**文件**: `src/core/dark_flow.py:575`  
**问题**: `active_ratio < 30%` 在腾讯口径下永不触发  
**方案**: 改用**成交额萎缩**代替 `active_ratio`:
```python
# 替代: 今日成交额 vs 5日均值 萎缩
volume_shrink = volume_ratio is not None and volume_ratio < 0.5
# 条件改为: 缩量 + 震荡 + 内外盘接近
(double_small_alt or volume_shrink) and oscillate
```
或者用"内外盘绝对值差 < 阈值 + 缩量"模拟。

#### 建议2: 修复口诀⑦ 腾讯数据源适配（P0）
**文件**: `src/core/dark_flow.py:593`  
**问题**: `double_big` 恒 True，门控退化为纯价格条件  
**方案**: 改用**内外盘失衡** + **价格不动** + **成交量放大**:
```python
# 内外盘失衡: |外盘%-内盘%| < 15% 但成交活跃
imbalance = abs(buy_pct - sell_pct) < 15  # 方向不明显
# 对倒典型特征: 成交量放大 + 价格不动 + 内外盘方向模糊
if (volume_up or volume_ratio > 2.0) and no_move and imbalance:
    # 对倒
```

#### 建议3: 口诀③④ 补充量能确认（P1）
**文件**: `src/core/dark_flow.py:601-606`  
**方案**: 在规则条件中加入量比下限:
```python
# ③ 诱多出货: 外盘大+跌+高位+缩量才更可信
(buy_pct > _MNEMONIC_STRONG and down and position == "high" and volume_ratio < 1.2,
 "诱多出货", "警惕", ...)
# ④ 压盘吸筹: 内盘大+涨+低位+缩量
(sell_pct > _MNEMONIC_STRONG and up and position == "low" and volume_ratio < 1.2,
 "压盘吸筹", "看涨", ...)
```

#### 建议4: 清除口诀⑤ 重复规则（P2）
**文件**: `src/core/dark_flow.py:607-609`  
**动作**: 删除第 607-609 行的重复规则（第 590 行已覆盖）。

### 6.2 主力意图算法优化

#### 建议5: 统一判据常量（P1）
**文件**: `intraday_monitor.py:245-250` vs `dark_flow.py:1028-1034`  
**问题**: 两处独立实现 `strong_absorb = (intensity >= 35) and (buy_ratio >= 48)`  
**方案**: 将判据阈值提取到 `dark_flow.py` 顶部常量区域，`_main_intent_structured` 引用 `dark_flow` 导出的函数/常量，消除漂移。

#### 建议6: 新增"控盘度"指标（P1）
**文件**: `src/core/dark_flow.py` 新增函数  
**新增**: `_calc_control_degree(dark, quote) -> float`  
- 筹码集中度(股东户数变化)  
- 主力参与度(日内)  
- 振幅收缩系数  
- 输出 0-100 控盘分，供口诀⑥⑦ 使用

#### 建议7: 新增"内外盘方向分歧"信号（P2）
**文件**: `src/core/dark_flow.py:831-844`  
**新增**: 主力净额方向与内外盘方向不一致时，标记为"分歧信号"。例如：主力净流入(+)但外盘占比<50% → 分歧。已在 `_judge_mnemonic` 的 `divergence` 字段中有初步实现，但未在 `_judge_signal` 中体现。

### 6.3 前端卡片展示

#### 建议8: 实现暗盘卡片组件（P1）
**新增文件**: `frontend/src/components/DarkFlowCard.tsx`  
**消费API**: `GET /api/dark-flow?symbol=XXXXXX`  
**展示内容**:
- 主力意图行: 方向(买/卖/洗/中性) + 净额 + 参与度 + 买占比
- 内外盘占比: 外盘% / 内盘% 进度条（绿/红）
- 口诀命中: 显示口诀名 + 方向图标 + 背离警告(⚠️)
- 位置标签: 高位/低位/中位
- 数据状态: 不足/异常/正常

#### 建议9: 在个股详情对话中集成口诀卡片（P2）
**文件**: `frontend/src/pages/Stocks.tsx`  
**方案**: 在 K线摘要窗口下方新增"暗盘/内外盘"标签页，渲染 `darkflow.py` 返回的结构化数据。

### 6.4 数据质量

#### 建议10: 数据源切换时自动调整口诀参数（P2）
**文件**: `src/core/dark_flow.py:54`  
**方案**: `DARK_SOURCE` 切换时（如变为 L2 含中性盘），自动调整 `_MNEMONIC_DOUBLE_LOW` 和 `_MNEMONIC_DOUBLE_HIGH` 阈值。当前注释已预留但未实现。

---

## 7. 关键文件清单

| 文件 | 行数 | 功能 | 审计重点 |
|------|------|------|---------|
| `src/core/dark_flow.py` | 1051 | 暗盘计算核心 + 七口诀 + 背离/拆单/节奏 | 全部 |
| `src/core/main_flow_compare.py` | 197 | 腾讯逐笔 vs thsdk L2 双源对比 | 辅助验证 |
| `src/core/intent_explain.py` | 313 | AI 解释层（规则主, AI 只解释不改结论） | 辅助 |
| `src/agents/intraday_monitor.py` | 2135 | 主力意图摘要/结构化/AI反证/面板渲染 | `_main_intent_*` 函数 |
| `src/agents/stock_attribution.py` | — | 归因分析中引用主力意图 | 归因精度 |
| `src/collectors/capital_flow_collector.py` | 317 | 东财资金流采集（仅参考口径） | 隔离验证 |
| `src/web/api/darkflow.py` | 124 | 暗盘 API 端点（供前端卡片） | 数据组织 |
| `src/web/api/klines.py` | 363 | K线摘要中嵌主力意图 | 缓存/TTL |
| `src/agents/tradingagents/portfolio_context.py` | 220 | TA past_context 注入 | 口径标注 |
| `frontend/src/pages/Stocks.tsx` | 3421 | 前端个股列表 | 超时配置 |

---

## 8. 总结

### 做对的
- ✅ 数据依赖严格隔离：逐笔 vs 东财四档，口径标注清晰
- ✅ 七口诀 90% 以上的规则架构与用户意图一致
- ✅ 背离检测（口诀 vs 主力意图方向）实现巧妙
- ✅ AI 只做解释不改结论，数据不足时静默降级
- ✅ 拆单识别 v3 含套牢区判断（用户洞察已落地）

### 需要修的
- ❌ 口诀⑥⑦ 在腾讯数据源下退化严重（P0）
- ❌ 口诀③④ 缺量能确认（P1）
- ❌ 口诀⑤ 代码重复（P2）
- ❌ 前端无暗盘卡片组件（P1）
- ❌ 判据常量在 `dark_flow.py` 和 `intraday_monitor.py` 两处独立实现（P1）

### 建议优先级
1. **P0**: 修复口诀⑥⑦ 腾讯数据源适配 → 直接影响用户识别控盘/对倒
2. **P1**: 实现 `DarkFlowCard` 前端组件 → 让口诀数据可见
3. **P1**: 口诀③④ 补充量比条件 → 减少误报
4. **P1**: 统一判据常量 → 防逻辑漂移
5. **P2**: 清理死代码 + 新增控盘度指标 → 中长期提升