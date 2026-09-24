---
name: sida-market-data
display_name: SIDA 数智分析市场数据
display_name_en: SIDA Market Data API
description: 查询 A 股市场数据的 HTTP 接口，覆盖实时行情、技术面、资金流向、主力意图、市场新闻、竞价数据、情绪周期、北向资金、K 线形态、基本面等。适用于：查个股实时价格/涨跌幅/成交量、技术指标、资金动向、板块异动、热门股、北向资金、机会榜单、K 线形态、市场新闻、竞价数据、情绪周期、双源主力比对等各类取数场景。触发词：SIDA、A股行情、实时行情、查股价、主力意图、资金流向、北向资金、热门股、情绪周期、竞价数据、机会榜、K线形态、市场异动、双源主力、A-share quote、market data。
description_zh: 接入 SIDA 市场数据接口，用一条 HTTP 请求取回 A 股的实时行情、技术指标、资金动向、市场情绪与机会榜单等数据，并附带数据口径说明，便于判断数据能不能用于下一步分析。只提供数据查询，不含交易执行与投资建议。
description_en: Query China A-share market data over plain HTTP — real-time quotes, technical indicators, capital flows, main-force intent, market sentiment and opportunity rankings, each returned with a clear caliber label. Data retrieval only; no trade execution or investment advice.
category: 数据分析
version: 1.2.0
author: SIDA
allowed-tools: Bash, Read, Write
---

# SIDA 数智分析市场数据

面向开发者的 **A 股市场数据查询接口**。一条 HTTP 请求即可取回行情、技术面、资金动向、
市场情绪等数据，并附带数据口径说明。

> **定位**：本接口**只做数据查询** —— 不提供交易执行与下单通道，也不提供投资建议。
> 返回的是原始数据与统计口径说明，「怎么用这些数据」由调用方自行判断。
> 接口内部的服务名称为 *SIDA Skill Gateway*（见下方项目源头）。

**提供 HTTP 接口，不提供 skill 源码与 prompt。**

## 项目源头

| 用途 | 地址 |
|---|---|
| 开源仓库 | https://github.com/xiaoze-hub/sida-pro |
| 官网（国内） | https://www.sida.hengsheng-elec.com |
| 官网（海外） | https://sida.win |
| API 基址 | `https://www.sida.hengsheng-elec.com` |
| 开发者文档页 | `{API 基址}/developers` |

- 客户端脚本：`scripts/sida_client.py`（仅标准库，Python 3.9+ 可直接跑）
- 包装脚本：`scripts/sida.sh`（自动载入凭据，推荐日常用）
- 接口明细与调用示例：`@references/sida-gateway-api.md`
- 数据口径详解：`@references/caliber-guide.md`
- 全部 skill 的参数与返回格式：`@references/skill-reference.md`
- 排障手册：`@references/troubleshooting.md`

**开放 skill 共 23 个**：free 档 15 个、pro 档 8 个。

---

## 一、拿到 AppKey

所有业务接口都要 `X-API-Key: sk_...`。**先领 key，再调用**（无 key 匿名调用一律 403）。

最简单：游客自动领一把 free 档 key（无需任何信息，100 次/天）：

```bash
curl -X POST https://www.sida.hengsheng-elec.com/api/guest-key
```

也可以带标识领 key（`trial: true` 领 10 天试用档 500 次/天；不传则 free 档）：

```bash
curl -X POST https://www.sida.hengsheng-elec.com/api/keys \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "你的手机或微信标识", "trial": true}'
```

返回里的 `api_key` 即为凭据，**明文只返回一次，请立即保存**。不传 `trial: true` 时签发免费档（free）。

拿到后设置环境变量：

```bash
export SIDA_KEY=sk_xxxxxxxxxxxxxxxx
export SIDA_BASE=https://www.sida.hengsheng-elec.com
```

> **凭据纪律**：`SIDA_KEY` 只放环境变量或权限 600 的配置文件，
> **不要写进代码、不要提交进仓库、不要贴进日志或对话**。
>
> 建议存放在 `~/.sida.env`（`chmod 600`），用 `set -a; . ~/.sida.env; set +a` 载入。

需要更高额度时走 `POST /api/pro/apply`（人工审核）。

> **遇到问题？** 使用说明或申请 Key 卡住时，欢迎加入微信群交流（二维码见开源仓库
> `README`）；群二维码失效后，请添加微信 `winter1920977` 联系。

---

## 二、调用方式

统一入口：`POST {API 基址}/api/skills/{skill_name}/run`

### 用包装脚本（推荐）

```bash
scripts/sida.sh skills                                      # 列可用 skill 与档位
scripts/sida.sh run get_stock_quote '{"symbol":"600519"}'   # 调用
scripts/sida.sh usage                                       # 查当日用量与剩余额度
```

### 用 curl

```bash
curl -s -X POST https://www.sida.hengsheng-elec.com/api/skills/get_stock_quote/run \
  -H "X-API-Key: $SIDA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"args": {"symbol": "600519", "market": "CN"}}'
```

### 用 Python（标准库，无第三方依赖）

```python
import json, os, urllib.request

BASE = os.environ.get("SIDA_BASE", "https://www.sida.hengsheng-elec.com")
req = urllib.request.Request(
    f"{BASE}/api/skills/get_stock_quote/run",
    data=json.dumps({"args": {"symbol": "600519", "market": "CN"}}).encode(),
    headers={"Content-Type": "application/json", "X-API-Key": os.environ["SIDA_KEY"]},
    method="POST",
)
print(json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]["result"])
```

**两个必知要点**：

1. **参数要包一层 `args`**：`{"args": {"symbol": "600519"}}`。包装脚本会自动包，裸 curl 必须自己写。
2. **两种返回信封**：裸 HTTP 拿到的是 `{code, success, data:{...}}`，正文在 `data.result`；
   `sida_client.py` 已自动解包，顶层直接是 `{skill, result, caliber, risk, duration_ms}`。
   **别把文档里的 `data.result` 直接套在客户端输出上** —— 会静默取到 `None`。

**不要猜参数名。** `GET /api/skills` 返回每个 skill 的完整 `schema.parameters`（JSON Schema，含
`required` 与 `default`），照抄即可。实测参数只有 `symbol` / `market` / `limit` / `period` /
`scene` / `briefing_type` 六种，且多数可选。

---

## 三、开放 skill 清单

**free 档（15 个）** —— 免费与试用档可用：

| skill | 必填 | 说明 |
|---|---|---|
| `get_stock_quote` | `symbol` | 实时行情（价 / 涨跌幅 / 成交量） |
| `get_technical_analysis` | `symbol` | 技术面（趋势 / MACD / RSI / 支撑压力） |
| `get_main_intent` | `symbol` | 主力意图（**逐笔口径**） |
| `get_rally_analysis` | `symbol` | 拉升段分析 |
| `get_capital_flow` | `symbol` | 资金流向（**东财四档口径**） |
| `get_kline_patterns` | `symbol` | K 线形态 |
| `get_main_flow_compare` | `symbol` | 双源主力比对 |
| `get_fundamentals_detail` | `symbol` | 基本面 |
| `get_market_news` | — | 市场新闻（可带 `limit` / `briefing_type`） |
| `get_sentiment_cycle` | — | 情绪周期 |
| `get_market_anomalies` | — | 市场异动（可带 `limit`） |
| `get_northbound` | — | 北向资金（**估算口径**） |
| `get_hot_stocks` | — | 热门股票（可带 `period` / `limit`） |
| `get_opportunities` | — | 机会榜单 |
| `get_strategy_signals` | — | 策略信号（可带 `limit`） |

**pro 档（8 个）** —— 需申请；其中 6 个为耗时接口（`slow`），客户端超时请放宽到 60s：

| skill | 必填 | 说明 | 耗时 |
|---|---|---|---|
| `get_decision_pioneer` | `symbol` | 数智决策三指标（机构活跃度 + GS + L2 主力净流入） | — |
| `get_auction_data` | — | 集合竞价数据（可带 `scene` / `limit`） | — |
| `get_forecast` | `symbol` | K 线形态预测 | ✅ |
| `get_delta_series` | `symbol` | 秒级 Delta 序列 | ✅ |
| `get_orderbook` | `symbol` | 盘口演变（托压单） | ✅ |
| `get_event_catalyst` | `symbol` | 事件催化与预期差 | ✅ |
| `get_intent_explain` | `symbol` | 主力意图解读 | ✅ |
| `get_factor_ic_report` | — | 因子 IC 报告 | ✅ |

**不开放的能力**：持仓、自选、通知、个股建议、网页抓取、问财问答、IR 问答
—— 涉及个人数据、外部抓取面或第三方账号依赖。

`market` 参数支持 `CN` / `HK` / `US`，默认 `CN`。

---

## 四、档位与限流

| 档位 | 日限 | 突发上限 | 匀速（次/分） | 说明 |
|---|---|---|---|---|
| free | 100 | 30 | 15 | 领 key 即可（`/api/guest-key` 自动发放，或 `/api/keys`） |
| trial | 500 | 50 | 20 | 10 天有效期，到期自动降为 free |
| pro | 5000 | 100 | 60 | 需申请，人工审核 |

> **没有「无 key 匿名调用」通道**：必须先领 key 再调用。游客 = 通过 `/api/guest-key`
> 自动领 free 档 key 的用户。

限流是**三重**的：日配额 + 突发上限 + 匀速（恒定速率补充）。
返回 `429` 时读 `Retry-After` 头，按指数退避重试。

> **批量拉数据必须限速**。匀速 15～20 次/分会成为实际瓶颈，
> 建议调用之间 `sleep 3.5`，或采用 `@references/troubleshooting.md` 里的限流器写法。

---

## 五、错误码

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | 缺少或无效的 `X-API-Key` | 检查 `SIDA_KEY` 是否已载入环境变量 |
| 403 | 档位不足（该 skill 需要更高档位），或 key 被禁用 | 报文会写明 `需要 pro 档位`；按要求升级或改调 free 档 skill |
| 404 | 未知 skill 名 | 用 `scripts/sida.sh skills` 核对名称 |
| 429 | 超出日配额 / 突发 / 匀速限制 | 读 `Retry-After`，退避后重试；长期不够则升级档位 |
| 500 | 该次执行失败（多为上游数据源瞬时问题） | 重试一次；持续失败换个 `symbol` 验证 |

---

## 六、数据口径（**使用前必读**，否则结论会错）

这是本接口与普通行情 API 最不同的地方：**同一件事可能有多个口径的数字，用错口径会得出反向结论。**

### 6.1 「资金流向」与「主力意图」是两套口径，不能混用

- `get_main_intent` —— 逐笔主动买卖口径。**判断主力方向只用它。**
- `get_capital_flow` —— 按单金额四档归类口径（超大单 / 大单 / 中单 / 小单）。
  服务端返回值中明确标注：**方向位与逐笔口径相反，禁止据此判定主力意图或吸筹派发**。

两者对同一标的的数字**必然不同**，且**方向可能相反**。同屏展示时必须并列标注口径来源；
冲突时**以逐笔口径为准**。

每个返回都带 `caliber` 字段说明数据口径，引用数据时一并带上，便于追溯。

### 6.2 各接口返回是**自然语言字符串**，不是结构化数据

`result` 字段是文本。要批量算指标得自己解析。已实测的稳定格式：

`get_main_intent`：
```
主力净{流入|流出}{±X亿}(超大单{±X亿}/大单{±X万|亿}) | 参与度{N}%买占{N}% |
阶段[{阶段文案}] | 竞价{±X万|亿} | 筹码峰{X} 获利{N}% 成本带{A-B}
```
> 阶段文案里若同时出现「吸筹」和「派发」（例如 `吸筹后转派发(...)`）
> ＝ 该标的已在获利了结，不要只看到「吸筹」二字就下结论。

`get_capital_flow`：
```
- 主力净{流入|流出} {±X亿}（占比{±N%}）
- 超大单{±X} | 大单{±X} | 中单{±X} | 小单{±X}
```
> 第 2 行与第 5 行是口径说明，不是数据。

`get_technical_analysis`：
```
技术面：趋势 X，MACD X(n日)，RSI X，支撑位 X，压力位 X，K线形态 X
```
> 去掉前缀后按 `，` 切分，每段再按第一个空格拆 key/value。

### 6.3 会明确标注的降级与估算

- **北向资金为估算口径**：交易所自 2024-08 起停止披露实时净买入，该接口返回估算值并**明确标注**，
  不可作为权威数据引用。
- **可能返回降级标记**（如 `(备用源)`）：表示主数据源不可用、已切备用源。
  看到「降级」「暂缺」字样，**要在结论里如实说明**，不要当作正常数据。
- **部分返回自带合规改写**：命中合规词时服务端会改写文案并在 `risk` 字段标注命中词。
  这是服务端行为，不是异常。
- **接口偶发返回字面量 `null`**（非 JSON 对象），属上游瞬时故障。
  **批量任务应实现「单次 null 就重试一次」**，而不是当成 key 失效。

### 6.4 数据缺失的处理

拿不到数据时，**如实说明缺什么、为什么缺，不要用其它数据拼凑替代**。
所有返回自带风险提示，转述时保留风险口径，不要加工成买卖建议。

---

## 七、使用红线

- **凭据**：`SIDA_KEY` 不进代码、不进日志、不进对话回显。
- **主力方向**只用 `get_main_intent`（逐笔）；`get_capital_flow` 引用时必须并列标注口径。
- 本服务**不提供 skill 源码与 prompt**。
- 所有返回**不构成投资建议**；第三方服务，数据准确性与可用性不承诺 SLA。
  关键决策请使用第二个数据源交叉验证。

---

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| `HTTP 200` 但 `result` 为空或提示取不到数据 | 多数是**漏了 `market` 参数**（如只发 `{"symbol":"..."}`）。补上 `"market":"CN"` 即可 —— `200` 不代表数据源有问题 |
| 调用很慢（十几秒） | 命中了 pro 档的耗时接口（预测 / 盘口 / Delta 等），把客户端超时放宽到 60s |
| 批量跑一半开始 429 | 触到匀速限制。调用间加 `sleep 3.5`，或采用参考实现里的限流器 |
| 一直 401 | key 未载入或已过期。用 `scripts/sida.sh usage` 确认（200 且返回额度即为有效） |
| 一直 403 | 看报文：写 `需要 pro 档位` 是档位问题；否则可能是 key 被禁用 |
| 连接失败 / 超时 | 运行环境若配置了 `http_proxy`，测试时加 `--noproxy '*'`，或临时 `unset http_proxy https_proxy` |

更多排障细节（含限流器与重试模板）见 `@references/troubleshooting.md`。

---

## 参考资料

| 文件 | 内容 |
|---|---|
| `@references/sida-gateway-api.md` | 接口清单、请求/响应示例、Python 与 curl 用法 |
| `@references/skill-reference.md` | 23 个 skill 的参数、返回格式与适用场景 |
| `@references/caliber-guide.md` | 数据口径指南：逐笔 / 四档 / DDE / 估算口径的差异与选用 |
| `@references/troubleshooting.md` | 排障手册：限流、重试、代理、常见报错 |
