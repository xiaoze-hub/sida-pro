# 资金类指标口径矩阵（docs/_frozen · B3/3.4 · 2026-09-09）

> **冻结文档**：本表是资金类指标"口径 × 数据源"的权威对照，供 3.4(B3) 口径治理与
> 3.6(D6) 暗盘口径矩阵复用。改数据源/口径时必须同步更新本表并走评审。
> 红线：**主力意图/方向性判定只能用 `tick`（逐笔主动买卖）口径**；`eastmoney4`/`ths`
> 仅作资金面参考（AGENTS.md "SIDA 业务硬约束" 口径条目；契约代码 `src/core/caliber.py`）。
>
> 口径类型：`tick`=逐笔主动买卖方向（腾讯逐笔）｜`eastmoney4`=按单金额四档归类
> （东财 push2 / 腾讯四档 / Engine 四档，方向位与逐笔可能相反）｜`ths`=同花顺
> DDE 大单口径｜`unknown`=未标注（不得用于方向判定）。

| 指标 | 数据源（vendor/接口） | 口径类型 | 方向语义 | 单位 | 可用于方向判定 |
|---|---|---|---|---|---|
| 主力净额（个股·今日） | 东财 push2delay 直连/网关（`capital_flow_collector._fetch_direct_flow/_fetch_cn_gateway_flow`） | eastmoney4 | 按单金额四档归类净额（超大+大单净额），非逐笔方向，与逐笔口径可能相反 | 元 | **禁止**（仅资金面参考） |
| 主力净额（个股·今日，兜底） | 腾讯四档 `marketdata/vendors/tencent_fundflow.py`；Engine 四档（`marketdata.capital_flow`，腾讯/东财） | eastmoney4 | 同上（按单金额归类；腾讯四档与东财四档同类口径） | 元 | **禁止**（仅资金面参考） |
| 主力意图/暗盘主动买卖 | 腾讯逐笔明细 `dark_flow.compute_dark_flow`（`get_main_intent` 底层，`src/core/dark_flow.py:1052`） | **tick** | 逐笔主动买−主动卖（含主力/超大单/参与度/买占比），唯一具备方向语义的口径 | 元 | **可以**（唯一口径） |
| L2 主力净流入 | 通达信 TQ `marketdata/vendors/tq.py`（本机网关 127.0.0.1:5100，`get_decision_pioneer` L2 字段） | ths*（TQ 扩展口径，非逐笔、非东财四档） | TQ 口径大单归类净流入，与逐笔/东财四档均不同 | 元 | **禁止**（资金面参考；与逐笔冲突时说明差异） |
| DDE 大单（暗盘资金 TOP） | 同花顺 thsdk DDE（`data_source/thsdk_l2.py`，`compute_dark_flow_l2(source="thsdk")`） | ths | 同花顺 DDE 大单口径（逐单大单归类），与逐笔/东财四档均不同 | 万元（main_net_wan） | **禁止**（仅资金面参考） |
| 大盘/两市主力净流入 | 国内网关 `115.190.177.213:8100/cn/market-overview`（东财两市超大+大单汇总，`/api/market-data/market-capital-flow`） | eastmoney4 | 两市按单金额四档归类汇总 | 亿元 | **禁止**（仅资金面参考） |
| 板块/行业资金 | 同花顺 `marketdata/vendors/ths_flow.py`（data.10jqka.com.cn/funds/hyzjl|gnzjl） | ths | 同花顺行业资金净额（流入/流出榜），口径独立 | 亿元 | **禁止**（参考） |
| 北向资金 | 同花顺 hexin `marketdata/vendors/northbound.py`（东财 kamt 自 2024-08 断供） | ths | hexin 当日分钟累计净买入（市场级） | 元 | 参考（市场级情绪，无个股方向语义） |
| 龙虎榜 | 东财 datacenter（`marketdata` dragon_tiger，datasources 健康检查含此类型） | eastmoney4 | 榜单营业部净买入归类（日频 T-1），非实时方向语义 | 元 | **禁止**（参考） |

## 使用规则（与 AGENTS.md 同步）

1. **方向性判定出口**一律调 `src/core/caliber.py::require_directional(tag, usage)`
   —— 非 tick 口径抛 `CaliberViolationError`。
2. 逐笔 vs eastmoney4/ths 同屏时用 `reconcile_direction()`：冲突必须说明口径差异
   （逐笔主动买卖 vs 按单金额归类）并**优先采信逐笔**。
3. 数据出口带标签：`CapitalFlow.caliber`/`get_capital_flow_summary()` dict、
   signal_pack `capital_flow`、`/api/market-data/market-capital-flow` 响应、
   forecast push `payload.capital_flow` 均已携带 caliber + label（B3/3.4）。
4. 新增资金类数据源时：先在本表登记口径类型与方向语义，再写代码；拿不准按
   `unknown` 处理（不得用于方向判定）。

## 实测依据

- 腾讯四档与东财四档同为"按单金额归类"口径：`capital_flow_collector.py` 取数优先级
  注释（2026-08-11）+ 腾讯 vendor 字段一一对应（super/big/mid/small 四档）。
- 东财 kamt 北向断供：`northbound.py:3-4`（2024-08 起返回 NaN/0，改同花顺 hexin）。
- TQ 网关链路与延迟：`tq.py:1-8`（frps/frpc→通达信 TQ HTTP，27-48ms 实测）。
- 同花顺 ths_flow 端点与单位：`ths_flow.py:1-8`（hyzjl/gnzjl，单位亿）。
- DDE 单位万元：`frontend/src/pages/DarkFundTop.tsx:7`（main_net_wan 口径注释）。
