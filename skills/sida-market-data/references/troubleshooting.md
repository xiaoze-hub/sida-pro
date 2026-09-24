# 排障手册

面向调用方的常见问题与处方。服务端内部问题不在本文范围。

---

## 一、先确认凭据有效

最快的探针是 `GET /api/usage`：

```bash
curl -s -H "X-API-Key: $SIDA_KEY" https://www.sida.hengsheng-elec.com/api/usage
```

| 结果 | 含义 |
|---|---|
| `200` + 返回 `tier` / 当日用量 / 剩余额度 | key 有效，可直接开始调用 |
| `401` | key 缺失、错误或已失效 —— 重新领取或核对环境变量 |

> 如果不想用 key 只想看有什么能力，`GET /api/skills/catalog` 是公开端点，无需凭据。

---

## 二、限流（HTTP 429）

### 三重限制

同时生效，任一超限都返回 `429`：

| 维度 | 含义 |
|---|---|
| 日配额 | 每自然日重置（free 100 / trial 500 / pro 5000） |
| 突发上限 | 短时间内的并发上限（free 30 / trial 50 / pro 100） |
| 匀速 | 按恒定速率补充令牌（free 15 / trial 20 / pro 60 次每分） |

**实际瓶颈通常是匀速**。批量任务跑一段时间后开始零星 429，就是这个原因。

### 处方一：最简单 —— 加延时

```bash
for sym in 600519 000001 300750; do
  curl -s -X POST "https://www.sida.hengsheng-elec.com/api/skills/get_stock_quote/run" \
    -H "X-API-Key: $SIDA_KEY" -H "Content-Type: application/json" \
    -d "{\"args\": {\"symbol\": \"$sym\", \"market\": \"CN\"}}"
  sleep 3.5
done
```

### 处方二：令牌桶限流器（并发场景）

需要并发拉取时，用令牌桶把发出速率压在匀速限制以下，并对 429 做退避重试：

```python
import json, os, random, threading, time, urllib.error, urllib.request

BASE = os.environ.get("SIDA_BASE", "https://www.sida.hengsheng-elec.com")
KEY = os.environ["SIDA_KEY"]


class RateLimiter:
    """按恒定速率放行调用，把并发压到匀速限制以下。"""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if sleep_for:
            time.sleep(sleep_for)


LIMITER = RateLimiter(per_minute=15)   # free 档取 15；trial 取 20，pro 取 60


def call_skill(name: str, args: dict, attempts: int = 4) -> dict | None:
    """调用单个 skill。内建限速 + 429 退避 + null 重试。"""
    payload = json.dumps({"args": args}).encode()
    for i in range(attempts):
        LIMITER.wait()
        req = urllib.request.Request(
            f"{BASE}/api/skills/{name}/run", data=payload, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode())
            data = out.get("data") if isinstance(out, dict) else None
            if data is None:
                # 上游瞬时故障会返回字面量 null —— 重试一次通常就好，别当成 key 失效
                time.sleep(4)
                continue
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = float(e.headers.get("Retry-After") or 0) or (2 ** i) * 5
                time.sleep(retry + random.random())
                continue
            if e.code in (401, 403, 404):     # 凭据/权限/名称问题，重试无意义
                raise
            time.sleep((2 ** i) * 2)          # 5xx 退避
        except urllib.error.URLError:
            time.sleep((2 ** i) * 2)
    return None
```

**三个要点**：

- **限速优先于重试**。重试不解决限流，只会让情况更糟。
- **401 / 403 / 404 重试无意义**，直接失败快。
- **`null` 返回要单独重试一次**，它代表上游瞬时故障，不是凭据问题。
  把 `null` 误判成 key 失效会导致无谓地重新领 key。

---

## 三、超时

| 场景 | 建议超时 |
|---|---|
| free 档普通查询（行情、技术面、资金流） | 30s |
| pro 档耗时接口（预测、盘口、Delta、事件、解读、IC 报告） | 60s |

`get_forecast` / `get_delta_series` / `get_orderbook` / `get_event_catalyst` /
`get_intent_explain` / `get_factor_ic_report` 这 6 个在目录里标记为 `slow`，
默认 30s 超时容易误判成失败。用 `GET /api/skills/catalog` 可以看到每个 skill 的 `slow` 标记。

---

## 四、网络与代理

- **运行环境配置了 `http_proxy` / `https_proxy`** 时，`curl` 测试可能连不通目标。
  加 `--noproxy '*'`，或临时 `unset http_proxy https_proxy`。
- Python 脚本默认走系统代理。如遇连接失败，可显式传 `ProxyHandler({})` 关闭。
- 域名请以 API 基址为准，不要凭记忆拼写 —— 拼错的域名通常表现为连接超时，
  看起来像网络故障，实际是域名不存在。

---

## 五、常见现象对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP 200` 但取不到数据 | 多数是漏了 `market` 参数 | 补 `"market":"CN"` |
| 返回值是字面量 `null` | 上游瞬时故障 | sleep 4s 后重试一次 |
| 一直 401 | key 未载入 / 已失效 | 用 `/api/usage` 复核，必要时重新领取 |
| 一直 403 且报文写 `需要 pro 档位` | 档位不足 | 申请 pro，或改调 free 档 skill |
| 批量任务中途大量 429 | 触到匀速限制 | 降低并发，或加令牌桶 |
| 单个请求耗时十几秒 | 命中了 `slow` 接口 | 超时放宽到 60s |
| 返回里有「降级」「暂缺」 | 主数据源不可用，已切备用源 | **在结论里如实说明**，不要当正常数据 |
| 返回文案被改写且 `risk` 里有命中词 | 服务端合规改写 | 正常行为，不是异常 |

---

## 六、批量任务的产物纪律

如果拿这些数据生成报告或供人决策，**建议固定以下口径**（这能避免大部分"数据看起来不对"的困惑）：

1. **记录抓取时刻**：产物里写明本次数据的抓取时间，不要沿用旧数据。
2. **失败要显式**：某条拿不到就标注缺失原因，**不要留空、不要用旧值填充**。
3. **口径随数走**：把 `caliber` 一并写入产物。
4. **风险提示随数走**：保留返回里的 `risk` 文案。

一套"字段为空 + 逐条失败原因标注"的产物，比一套"看起来完整但混了不同时刻/不同口径"
的数据有用得多。
