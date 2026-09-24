# SIDA Skill Gateway — 接口说明

面向开发者的接入文档。**只提供 HTTP 接口，不提供 skill 源码与 prompt。**

- API 基址：`https://www.sida.hengsheng-elec.com`
- 开源仓库：https://github.com/xiaoze-hub/sida-pro
- 开发者文档页：`{API 基址}/developers`（含在线调试台，可直接发请求验证）

依赖：`python3`（3.9+，仅标准库）或 `curl`。

---

## 一、端点总览

| 方法 | 路径 | 凭据 | 用途 |
|---|---|---|---|
| `POST` | `/api/keys` | 无 | 领取 AppKey（免费 trial / free） |
| `GET` | `/api/skills/catalog` | 无 | 全部开放 skill 目录 + 各档位限流配置 |
| `GET` | `/api/skills` | `X-API-Key` | 当前 key 可用的 skill（含完整参数 Schema） |
| `POST` | `/api/skills/{name}/run` | `X-API-Key` | 执行 skill（**主要入口**） |
| `GET` | `/api/usage` | `X-API-Key` | 当日用量与剩余额度 |
| `POST` | `/api/pro/apply` | 登录态 | 申请 pro 档（人工审核） |

> `GET /api/skills/catalog` 不需要凭据，适合在写代码前先探查有哪些能力、各自参数是什么。

---

## 二、鉴权

业务接口统一使用请求头：

```
X-API-Key: sk_xxxxxxxxxxxxxxxx
```

三种调用者的额度互不相同：

| 调用者 | 凭据 | 额度 | 可调 skill |
|---|---|---|---|
| API Key | `X-API-Key` | 按档位（见下） | 按档位 |
| 游客 | 无 | 按来源 IP，10 次 / 24h | 仅 free 档 |

凭据纪律：`SIDA_KEY` 只放环境变量或权限 600 的配置文件，**不要写进代码、不要提交进仓库、
不要输出到日志或对话**。

---

## 三、领取 AppKey

```bash
curl -X POST https://www.sida.hengsheng-elec.com/api/keys \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "你的手机或微信标识", "trial": true}'
```

返回：

```json
{
  "code": 0,
  "success": true,
  "data": {
    "api_key": "sk_xxxxxxxxxxxxxxxx",
    "key_prefix": "sk_xxxxxxxx",
    "tier": "trial",
    "daily_limit": 500,
    "expires_at": "2026-10-04T10:26:40",
    "note": "请妥善保存 API Key, 服务端不会再次展示明文。",
    "risk": "本结果由算法/数据源生成, 仅供参考, 不构成任何投资建议; 股市有风险, 入市需谨慎。"
  }
}
```

**`api_key` 明文只返回一次，请立即保存。** 服务端只存哈希，无法找回，丢失只能重新领取。

`owner_label` 建议填固定的、可追溯的标识（手机号或微信号），便于对账与后续升级档位。

不传 `trial: true` 时签发 free 档。

---

## 四、调用 skill

```bash
curl -s -X POST https://www.sida.hengsheng-elec.com/api/skills/get_stock_quote/run \
  -H "X-API-Key: $SIDA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"args": {"symbol": "600519", "market": "CN"}}'
```

返回：

```json
{
  "code": 0,
  "success": true,
  "data": {
    "skill": "get_stock_quote",
    "result": "实时行情：（CN:600519）价格 1234.04，涨跌幅 -1.37%，成交量 19383.0",
    "caliber": "多源实时行情聚合",
    "risk": "本结果由算法/数据源生成, 仅供参考, 不构成任何投资建议; 股市有风险, 入市需谨慎。",
    "duration_ms": 85
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `skill` | 回显的 skill 名 |
| `result` | **数据正文，是自然语言字符串**（不是结构化对象）。解析方式见 `caliber-guide.md` |
| `caliber` | 数据口径说明，引用数据时建议一并带上，便于追溯 |
| `risk` | 风险提示；命中合规词时这里会标注命中词 |
| `duration_ms` | 服务端耗时 |

**两个最常见的新手错误：**

1. **参数漏包 `args`** —— 必须是 `{"args": {...}}`，直接平铺参数不会生效。
2. **把 `data.result` 套在客户端脚本输出上** —— `scripts/sida_client.py` 已经解包过，
   它的顶层就是 `data` 的内容。对着客户端输出取 `["data"]["result"]` 会静默得到 `None`。

---

## 五、批量调用示例（含限速）

匀速限制（free 档 15 次/分）是批量任务的实际瓶颈。最简做法：

```bash
for sym in 600519 000001 300750; do
  curl -s -X POST "https://www.sida.hengsheng-elec.com/api/skills/get_stock_quote/run" \
    -H "X-API-Key: $SIDA_KEY" -H "Content-Type: application/json" \
    -d "{\"args\": {\"symbol\": \"$sym\", \"market\": \"CN\"}}"
  echo
  sleep 3.5
done
```

需要并发时请自建令牌桶（见 `troubleshooting.md`），并对 `null` 返回值实现一次重试。

---

## 六、限流与档位

| 档位 | 日限 | 突发上限 | 匀速（次/分） |
|---|---|---|---|
| 游客 | 10（按 IP） | — | — |
| free | 100 | 30 | 15 |
| trial | 500 | 50 | 20 |
| pro | 5000 | 100 | 60 |

三重限制同时生效：**日配额**（每天重置）、**突发上限**（短时并发上限）、
**匀速**（按恒定速率补充令牌）。任一超限都返回 `429`。

`429` 响应带 `Retry-After` 头（秒）。请按指数退避重试，不要立即重打。

---

## 七、错误码

| HTTP | 报文特征 | 含义与处理 |
|---|---|---|
| 401 | `无效的 API Key` | key 缺失、错误或已失效。用 `GET /api/usage` 复核 |
| 403 | `需要 pro 档位(当前 xxx)` | 档位不足。升级档位，或改调 free 档 skill |
| 403 | 其它文案 | key 被禁用或冻结 |
| 404 | `未知 skill` | skill 名拼错。用 `GET /api/skills` 核对 |
| 429 | — | 限流。读 `Retry-After`，退避重试 |
| 500 | — | 该次执行失败，多为上游数据源瞬时问题。重试一次通常可恢复 |

---

## 八、返回值的进一步加工

`result` 是给人读的文本，但格式稳定，可以用正则解析成结构化数据。各 skill 的具体格式
见 `skill-reference.md`，口径差异见 `caliber-guide.md`。

解析前请先确认：

- 拿到的 `caliber` 是什么口径 —— 决定这个数字能用来下什么结论；
- 返回里有没有「降级」「暂缺」等标记 —— 有的话结论要如实说明；
- 是否命中 `null`（上游瞬时故障）—— 应重试一次而不是当作 key 失效。
