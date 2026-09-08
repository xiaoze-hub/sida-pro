---
name: sida-pro-data
displayName: SIDA-Pro 数据接口
version: 1.0.0
description: Use when 需要 A 股实时行情/K线/主力意图/决策信号/数据源质量分/命中榜等结构化数据时, 直调 SIDA-Pro 生产接口, 不经过第三方聚合。覆盖 quotes/klines/decision/trust/accuracy/dark-flow。
trigger_keywords:
  - SIDA-Pro 数据
  - 神剑股份实时行情
  - 主力意图查询
  - 决策信号查询
  - 数据源质量分
  - 命中榜
allowed-tools:
  - execute_command
---

# SIDA-Pro 数据接口(Hermes 直调生产)

## 概述

SIDA-Pro 是自研 A 股行情终端(生产跑在小主机 Tailscale 内网,
`SIDA_BASE_URL=http://100.91.30.35:8000`)。本 skill 让 Hermes 绕过第三方聚合,
直调生产接口拿一手结构化数据:实时行情、日K/分时、决策合成(三信号→动手/看看/别碰)、
vendor 质量分、分 agent 命中榜、暗盘资金。

OpenAPI 快照(291 paths): 仓内 `docs/_frozen/openapi.p1.json`。

## 认证(两级, 与 P1 双轨对齐)

| 级别 | 环境变量 | 能进 | 怎么拿 |
|---|---|---|---|
| svc | `SIDA_SERVICE_TOKEN` | quotes/klines/minute/more-info/company(只读口) | 小主机生产容器 env(与 Hermes 本机环境变量同名直读) |
| user | `SIDA_USER_TOKEN` | decision/trust/accuracy/dark-flow(用户口) | `POST /api/auth/login`(admin 账密)→响应的 token |
| open | 无 | health | — |

服务 token 进写口一律 403(提权面为零);提权需求走用户 JWT,不放宽。

## 工作流程

```
用户要数据 → 选端点(下表) → 置环境变量 → python scripts/sida.py <端点> [symbol] [period] → 解析JSON → 按单位铁律输出 → 结束
```

## 端点表

| 端点 | 说明 | 认证 |
|---|---|---|
| `health` | 服务/DB/Redis 存活+版本号 | open |
| `quote 002361` | 单股实时行情(含 source/source_latency_ms 来源透传) | svc |
| `quotes 002361,300750` | 批量行情(逗号分隔) | svc |
| `klines 002361 1d` | 日K(PG hypertable 优先, asof 显式标注基准日) | svc |
| `minute 002361` | 当日分时 | svc |
| `moreinfo 002361` | 更多信息(资金/席位等) | svc |
| `company 002361` | 公司信息 | svc |
| `decision 002361` | 决策合成: verdict 动手/看看/别碰 + 一行理由 | user |
| `trust` | vendor 质量分(成功率−延迟档, 未调用过 null 不冒充) | user |
| `accuracy 30` | 分 agent 命中榜(days 参数, 默认 30) | user |
| `darkflow 002361` | 暗盘/主力意图(逐笔口径, 对齐同花顺) | user |

## 硬约束(违反等于幻觉)

1. **单位**: 金额=元, 成交量=股; `vol × price == amt` 对不上先怀疑单位, 不改数。
2. **缺数显式标"无数据"**, 禁止 LLM 推测编造数字; K 线 asof 滞后必须标注基准日, 不拿昨日推断今日。
3. **主力意图只认 darkflow(逐笔口径)**, 禁用东财资金流方向(会反)。
4. asof < today 且厚度 < 30 天 → 接口已自动 failover 走联网, 引用时标注来源。

## 示例

```bash
export SIDA_BASE_URL=http://100.91.30.35:8000
export SIDA_SERVICE_TOKEN=xxx SIDA_USER_TOKEN=yyy
python scripts/sida.py quote 002361
python scripts/sida.py klines 002361 1d
python scripts/sida.py decision 002361
python scripts/sida.py trust
```
