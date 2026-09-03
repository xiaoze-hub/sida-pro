# 数据源与算法接口设计：TDX × THS 互补（v1.0）

> 依据：`通达信客户端研究结果汇总-20260830.md`（TDX落盘+TQ网关）/
> `决策先锋8问8答_解析.md`（官方口径）/ `同花顺决策先锋复刻研究手册.md`（thsdk实测）。
> 原则：**盘中用THS云端L2（唯一盘中真值），盘后用TDX落盘（委托号级精确），免费源只做兜底**；
> 缺失一律显式"无数据"，禁LLM推测（用户幻觉红线）。

## 一、互补矩阵（指标 × 数据源，主/备/口径）

| 指标 | 官方口径 | 主源 | 备源 | 口径差异/诚实标注 |
|---|---|---|---|---|
| GS趋势 | G=启动/S=结束，非函数次日不消失 | 任一K线（Engine→东财→新浪/腾讯，见`fetch_bars`） | — | 自研版形态过滤近似，需截图校准；G/S去重（次日不消失） |
| 明盘资金 | 单笔≥30万大单净额 | 盘中：**腾讯逐笔成交明细**（免费，`get_main_intent`已用）按30万过滤；thsdk `big_order_flow`互验 | 盘后：`.tck`逐笔聚合；TQ `Zjl/Zjl_HB`成品（实时快照） | 逐笔明细腾讯已兜底，不指望TQ补；TQ价值在撤单量+分档+涨停人气 |
| 暗盘资金 | AI识别拆单/对倒/量化单，代表真意图 | 盘中：腾讯逐笔**近似拆单**（时间/价格/手数聚类，无订单ID会误判） | 盘后：`.tck`委托号聚簇（主动侧100%精确，`dark_flow_fusion`）；盘中增强：thsdk逐笔拆单特征 | 精确拆单需订单ID（TDX客户端"逐笔还原"有但TQ接不出）；被动maker侧23.2%缺口，两源都看不到，必须标注confidence |
| AI活跃度 | max(7因子)×1.2；线1.56/3/6 | 任一K线（`ai_activity.eval_activity`官方阈值已对） | — | 纯K线，无源差异 |
| 共振 | 7行状态表→四态+回测基准 | `resonance.evaluate_state`（已落地） | — | 缺：暗盘1/3/5日SERIES+0轴CROSS（PR-2待选A/B/C） |
| L2盘口 | — | 盘中：thsdk `order_book` 20档 | 盘后：`.img`十档3s快照；实时兜底：TQ五档快照 | `.img`队列无委托号，只能看形态 |
| 选股/资讯 | 三指标共振选股 | `wencai_nlp`（涨停归因/连板/概念涨幅/北向持股比） | TDX `formula_process_mul_xg`批量选股 | 北向实时净买入已停披露，只能拿持股比 |
| 主力净额实时 | — | TQ `get_more_info`（Zjl等104字段，~28ms） | thsdk `market_data_cn`扩展1 | 口径不同（公式成品 vs 逐笔聚合），展示须标source |

## 二、接口清单（现有 → 缺口）

现有（`src/core/`，已有fallback链）：
- `decision_pioneer.fetch_bars`：K线多源（Engine→东财→新浪→腾讯）+ `fetch_tq_l2`（TQ实时）
- `dark_flow_fusion.compute_dark_fusion`：`.tck`主动精确 + thsdk被动估计，confidence标注
- `ai_activity.eval_activity`：官方阈值1.56/3/6 + streak_days（连续多日强势线，选股用）
- `resonance.evaluate_state`：7行表→四态+回测基准对照
- `dark_l2.fetch_l2_ticks`：thsdk盘中逐笔（待盘中验证tick_super_level1真值）
- `wencai`：问财选股/龙虎榜/涨停归因（限频250ms，sleep 0.5）

缺口（按优先级）：
1. **P0 — 暗盘1/3/5日SERIES + 0轴CROSS字段化**（=PR-2，3方案A/B/C待选）：资金"由绿转红上穿0轴=看多"是官方核心判定，当前只有单日净额，无滚动序列。选型后接入`resonance`（fund_net_1/3/5 + cross_dir）。
2. **P1 — thsdk L2盘中验证**（手册待办）：盘中跑`tick_super_level1`见1/2/-1/-2真值即打通；验证前`dark_l2`输出必须带`verified:false`。
3. **P2 — GS截图校准**：自研版vs原版截图调参（BB0 3/7/13/27 + A0加权 + CROSS去重），输出校准报告。
4. **P3 — TDX formula批量选股**（可选）：`formula_process_mul_xg`全市场共振选股，与wencai选股互补（wencai吃问法，formula吃模板）。

## 三、Fallback链与标注铁律

1. 盘中链：thsdk L2逐笔 → TQ快照(Zjl) → 无数据（标注，不降级编造）。
2. 盘后链：`.tck`委托号聚簇 → K线分摊对照（标注"近似，妖股日误差可达23倍"）→ 无数据。
3. TDX独有增量（腾讯给不了）：`BCancel/SCancel`撤单量 + `L2AMO`分档资金 + `get_gpjy_value`涨停人气/封单——TQ接入优先这三样。
4. 每个资金数字必带：`source`（tck/thsdk/tq/kline_est）+ `confidence`（exact/approx/none）+ 周期（1/3/5日）。
5. 凭据：THS账号走`.env`（mx_8lj4le6qd），禁提交仓库；TQ网关仅小主机本地。

## 四、决策需求

- [ ] PR-2三方案A/B/C选型（SERIES+CROSS实现路径）
- [ ] P2校准是否立项（需原版截图输入）
