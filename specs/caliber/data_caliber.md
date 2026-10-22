# 数智参谋 · 数据口径契约

规格事实源。对应实现：`src/core/caliber.py`（口径类型与方向性校验）、
`src/agents/chat/registry.py`（每个工具挂的 `caliber`）、`src/web/api/klines.py`（K 线时点标注）。

**基准日**：2026-10-22
**覆盖范围**：口径标签的存在性与方向性校验出口；K 线 `as_of` 标注契约。
**不覆盖**：第三方数据本身的准确性（那是数据源的事）。

> 这三条都是**出过事**才写的：把东财四档的净流入当成"主力在买"，
> 方向位与逐笔相反，结论会整个反过来。

## 验收标准

- **DATA-001** `get_capital_flow` 的 `caliber` 必须显式包含「禁止据此判定主力意图」类红线 —— 它是按单金额四档归类，不是逐笔方向
- **DATA-002** 方向性判定只能走逐笔口径：`require_directional()` 对四档口径必须抛 `CaliberViolationError`，而 `get_main_intent` 的 caliber 必须是逐笔
- **DATA-003** K 线读数必须带时点标注 `as_of`（盘中合成的今日 bar 另标 `partial`）—— 缺了它，昨日数据会被当成今日行情

## 维护方式

新增任何资金类工具，先在这里补一条口径标准，再在注册表里挂 `caliber`。
**没有 caliber 的资金类工具不得上线** —— 下游会误读成事实。
