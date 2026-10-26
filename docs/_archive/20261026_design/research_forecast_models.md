# 时序预测模型精品调研（补齐版）

> 日期: 2026-08-25 | 模型: DeepSeek v4 Pro + 人工补齐
> 背景: SIDA 现有 Kronos(主, 6维MC30) + Chronos-Bolt(快, 1维) + XGBoost + 线性回归，Lag-Llama 待定

## 现有架构诊断
- Kronos-small (NeoQuasar) 需 ~20-30s CPU，6维OHLCV+amount，MC采样抗噪优秀但慢
- Chronos-Bolt 0.06s 快但仅 close 一维，浪费量价
- XGBoost 每次重训 30s+，无持久化，20%验证集未用
- 线性回归在震荡市无意义，权重已压到 0.1

## 候选模型评估

| 模型 | 论文/仓库 | 输入 | 速度 | 开源 | 与Kronos互补 | 结论 |
|------|-----------|------|------|------|--------------|------|
| **TimesFM (Google)** | google-research/timesfm Apache2 | 单变量块 | 快 | ✅ | 强(互补预训练) | ✅ 推荐替代Lag-Llama，单变量但预训练最强，BigQuery已内置 |
| **Moirai (Salesforce)** | Salesforce/moirai Apache2 | 任意变量 | 中 | ✅ | 强(多变量) | ✅ 推荐，支持任意变量数+分位数，比Chronos-Bolt更强 |
| **Lag-Llama** | time-series-foundation-models/Lag-Llama | 滞后特征 | 中 | ✅ | 弱(单变量) | ❌ 建议去掉，已被TimesFM/Moirai全面超越，2024后无更新 |
| **Mamba/TimeMachine** | Event-AHU/Mamba | 多变量 | 快 | ✅ | 中(长序列效率) | △ 观望，SSM在长序列效率高但金融短序列优势不明显 |
| **PatchTST** | yuqingw405/patchtst | 多变量 | 快 | ✅ | 中 | △ 可作为轻量baseline替代线性回归 |
| **StockFormer/GNN** | 多篇2024 | 股票图 | 慢 | △ | 弱 | ❌ 需图构建，A股全市场图成本高，不适合日频预测 |

## Lag-Llama 去留
**去掉**。理由：单变量滞后特征范式已被 TimesFM/Moirai 的 patch-based 预训练超越；社区活跃度低；SIDA已有更快的Chronos-Bolt覆盖同生态位。

## 推荐替换方案
1. **P0 去掉 Lag-Llama**，依赖从 forecast_requirements.txt 移除
2. **P1 引入 TimesFM 或 Moirai 二选一**：TimesFM更轻(Google预训练强)，Moirai更全(多变量+概率)。建议先试 Moirai-small (与Kronos互补：Kronos捕6维量价非线性，Moirai补多尺度概率)
3. **P1 线性回归 → PatchTST 或 轻量ARIMA**：保留极快baseline但换更有解释性的

## 缝合成本
- TimesFM: pip install timesfm + 单变量close接入，1天
- Moirai: pip install moirai + 多变量接入，2天
- 均需在 forecast_models.py 加新分支，复用现有动态权重投票框架
