# 四档资金结构消费（exday_daily · tdx_exday4）

规格事实源。对应实现：`src/core/exday_structure.py`（`query_series` 表取数）、
`src/web/api/fund_structure.py`（日序列端点）、
`frontend/src/components/FundStructurePanel.tsx`（展示组件, 挂载于工作台「盘口资金」标签）。

**基准日**：2026-10-24
**覆盖范围**：`exday_daily` 表（Migration 220）的消费链路 —— API 出参契约 + 前端稀疏数据展示。
**不覆盖**：落库管线（`src/collectors/exday_daily.py`，已有 `tests/test_exday_collector.py`）、
矩阵解析与因子（已有 `tests/test_exday_structure.py`）。

> 背景一句话：`get_exday_data` 只返当日 1 行，四档结构历史无法回填，只能靠每日 15:45
> 落库累积 —— 表 **2026-10-23 起才有数据**。消费端把"只有 2 天"画成"长期趋势"、
> 或把缺日期补 0，都是口径造假。

## 验收标准

- **EXDAY-001** 四档资金结构 API 响应必须携带 `caliber: "tdx_exday4"` 与 `direction_semantics`（按单笔成交金额分四档，与腾讯逐笔/东财四档不可换算，禁止用于主力意图判定）
- **EXDAY-002** 日期序列只返回库中实际落库行（字段按表结构原样出），缺日期不得补 0 / 补平成连续交易日轴
- **EXDAY-003** 响应必须带 `data_since`（全表累积起点）与 `count`（该股实际行数），供前端显式标注「数据自 2026-10-23 起累积，N 个交易日」
- **EXDAY-004** 无数据（未落库/缺表/查询失败/db 为 None）必须显式 `available=false` + 原因，禁止以 0 或空数组冒充有数；best-effort 吞异常前必须先 `safe_rollback(db)`
- **EXDAY-005** 前端：`count==1` 只展示当日结构不画趋势序列，`count>=2` 才画每日序列；金额走 toAmount（元→亿/万）、数字 tabular-nums、买入/净流入红卖出/净流出绿，同屏其他口径资金数据各带口径标签，无数据显式「无数据」

## 维护方式

表结构变更（新列）先改 `src/web/migrations.py`（Migration 224+），再同步
`src/db/models.py` 的 `ExdayDaily` 与本文件；消费端新字段原样透传即可。
