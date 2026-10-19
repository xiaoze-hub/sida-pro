# TDX Local Gateway

通达信客户端本地标准网关。把 `TdxW.exe` 自带的 17709 JSON-RPC 包一层：**契约补参 + 串行排队 + 熔断 + 缓存 + 指标**。

## 为什么需要它（都是实测出来的）

| 问题 | 实测证据 | 网关怎么解决 |
|---|---|---|
| 隐藏参数导致 `ErrorId=10` | `get_financial_data` 裸调报 `json has no table_list`；`get_stock_list` 报 `参数缺少:market/list_type` | 按 `PYPlugins/user/tqcenter.py` 反解出的 schema 自动补 `id`/`type`/`table_list`，调用方只传业务参数 |
| 客户端单线程，并发会崩 | 2026-10-19 连打 30 个快请求后 **TdxW.exe 整个进程消失**，17709 全断 | 并发闸门默认 **1** + 队列上限 + single-flight 合并同参请求 |
| 地址/端口不稳 | 只有 `0.0.0.0:5100 → 127.0.0.1:17709` 的 portproxy，17709 未转发；重启后 portproxy 丢失 | 网关只认一个上游 URL，portproxy 由部署脚本检查 |
| 交易接口混在同一个 RPC 里 | `order_stock` / `cancel_order_stock` / `exec_to_tdx` 就在同一张方法表 | 硬编码拒绝清单，返回 403，任何配置都不放开 |
| 出问题查不到原因 | 之前只能看客户端行为 | `/metrics` 给出方法级 ok/err/缓存命中/平均延迟 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/` | JSON-RPC 直通，形状与 17709 完全一致（SIDA 现有 vendor 可零改动指向这里） |
| POST | `/rpc/{method}` | REST 便捷口，body 只传业务参数 |
| GET | `/api/quote/{symbol}` | 单只快照 |
| POST | `/api/quotes` | 批量快照（串行，上限 50） |
| GET | `/health` | 上游可达性 + 延迟 + 熔断状态（失败返回 503） |
| GET | `/metrics` | Prometheus 文本指标 |

```bash
# 只传业务参数，table_list/id/type 由网关补齐
curl -s -X POST localhost:17710/rpc/get_financial_data \
  -H 'Content-Type: application/json' \
  -d '{"stock_list":["600111.SH"],"field_list":["FN1"]}'

# 交易类：403
curl -s -X POST localhost:17710/rpc/order_stock -d '{}'   # {"detail":"order_stock ... 网关不开放"}
```

## 部署（小主机 WSL）

```bash
bash tdx_gateway/deploy.sh            # 构建 + 起容器（幂等）
docker logs -f tdx-gateway            # 日志
curl -s localhost:17710/health        # 自检
```

容器 `tdx-gateway` 挂在 `panwatch-net` 上，SIDA 容器用 `http://tdx-gateway:8000/` 直连；
也可对外发布 `127.0.0.1:17710`。

## 契约从哪来（可重跑）

```bash
# 1) 从客户端源码提取方法签名与请求体
python scripts/extract_tdx_contract.py   <tqcenter.py> docs/research/tdx_local_contract
python scripts/extract_tdx_http_schema.py <tqcenter.py> docs/research/tdx_local_contract
# 2) 探针实测（务必慢！默认 1.2s 间隔，客户端经不起突发）
python scripts/probe_tdx_surface.py --base http://172.27.16.1:17709/ \
       --schema docs/research/tdx_local_contract/http_schema.json --sleep 1.2
cp docs/research/tdx_local_contract/http_schema.json tdx_gateway/http_schema.json
```

## 测试

```bash
env -u PYTHONPATH .venv/bin/python -m pytest tdx_gateway/tests/test_gateway.py -q
```

覆盖：契约补参（`table_list` 映射）、判别码不可被调用方覆盖、缺参人话报错、串行闸门、
single-flight + 缓存、队列满拒绝、禁用清单。
