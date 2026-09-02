# 时间口径约定

> 本项目涉及 3 个时间源，每个口径不同。部署/排障/对账时必须显式换算。

## 三种时间口径

| 来源 | 时区/格式 | 示例 |
|---|---|---|
| **容器日志** `docker logs panwatch` | **北京时间 UTC+8**（容器 `TZ=Asia/Shanghai`）| `2026-09-02 13:44:19` |
| **邮件 `created_at`** (AgentMail / QQ Mail) | **UTC** | `2026-09-02T05:44:19Z` |
| **通达信 TQ `HqDate`** | **交易日 `YYYYMMDD`**（无时区概念）| `20260827` |
| 业务侧日期字段（事件 date / K 线 date） | `YYYY-MM-DD`（v0.4.57 起统一）| `2026-08-27` |

## 换算规则

| 你看到 | 换算 | 实际 |
|---|---|---|
| `13:44` 在容器日志 / xiaoze 信里 | **已经是 UTC+8** | 北京时间 |
| `05:44` 在邮件 created_at | **UTC** | 北京时间 = **+8h** = `13:44` |
| `20260827` 在 TQ 数据 | **交易日**（无时区） | 等同 `2026-08-27`（业务日）|

## 跨源日期规范化

**v0.4.57 起**：业务侧的 date 字段（`fund_flow[*].date`、`events[*].date`、`chips.date`）**统一为 `YYYY-MM-DD`**。TQ 给 `20260827` / 东财给 `2026-08-27` / .tck 文件名 `20260827` → 内部统一为 `2026-08-27`。

实现：`src/web/api/klines.py:_norm_date(d)`。

**v0.4.58 待修**：`src/agents/gs_strategy.py:compute_gs_signals` 内的 date 字段**未过 `_norm_date`**——TQ 降级东财时会出现日期样式突变。

## 邮件 + 部署时间对齐约定

xiaoze6096（盘后 Agent 同事）以后邮件中时刻统一标注 `HH:mm (UTC+8)`。Hermes 这边解读时同步换算：

- 邮件 `created_at` 是 UTC → **+8h** = 北京时间
- 容器日志 / cron / 部署脚本日志 → **已是 UTC+8** → 直接对照

## 排障常用

```bash
# 容器时间
docker exec panwatch date                # 容器内部时间（UTC+8）
docker logs panwatch --since "2026-09-02 13:30:00" --until "2026-09-02 14:00:00"
# 注：docker logs 时间参数是 UTC+8（容器本地时区）

# cron 同步时间（cron 默认 UTC，Hermes 部署时已显式 TZ=Asia/Shanghai）
crontab -l | grep -E "TZ|45d3592196a4"
```