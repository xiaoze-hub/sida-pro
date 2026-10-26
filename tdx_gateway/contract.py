"""契约层：把 tqcenter.py 反解出来的请求体 schema 变成本地网关的"自动补参"能力。

为什么需要：17709 的裸 HTTP 请求体和 `PYPlugins/user/tqcenter.py` 的方法签名**不是一回事** ——
`id` / `type` / `table_list` 这些字段是 Python 层帮你填的，裸调时不填就报
`ErrorId=10: json has no table_list`，长期被误判成"要下载盘后专业数据包"。
网关在这里按 schema 补齐，调用方只传业务参数。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 真实资金账户 + 客户端内执行 URL：网关默认硬拒绝，任何配置都不能放开
DENY = {
    "order_stock", "cancel_order_stock", "exec_to_tdx", "send_source",
    "stock_account", "query_stock_asset", "query_stock_orders", "query_stock_positions",
}

# 每类方法的并发/超时策略（TdxW 的 TQ 服务是单线程的，并发打会直接把客户端打崩 —— 2026-10-19 实测）
PROFILES = {
    "snapshot": {"timeout": 8, "cache_ttl": 3},
    "kline": {"timeout": 15, "cache_ttl": 30},
    "formula": {"timeout": 90, "cache_ttl": 60},
    "pro_data": {"timeout": 30, "cache_ttl": 300},
    "default": {"timeout": 15, "cache_ttl": 0},
}


def load_schema(path: str | Path) -> dict[str, dict]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("methods", {})


def profile_for(method: str) -> dict:
    if method in ("get_market_snapshot", "get_more_info", "get_pricevol", "get_zdt_data"):
        return PROFILES["snapshot"]
    if method in ("get_market_data", "get_exday_data", "get_minute_data"):
        return PROFILES["kline"]
    if "formula" in method or method in ("tdx_formula",):
        return PROFILES["formula"]
    if method.startswith(("get_financial", "get_gpjy", "get_scjy", "get_bkjy", "get_gb_")):
        return PROFILES["pro_data"]
    return PROFILES["default"]


class ContractError(ValueError):
    """参数无法按契约补齐（缺业务必填项）。"""


def build_body(method: str, params: dict[str, Any], schema: dict[str, dict],
               run_id: int = 1) -> dict[str, Any]:
    """按 schema 把业务参数展开成 17709 认识的真实请求体。

    - `$run_id` → 会话 id（网关固定 1，客户端只要求是整数）
    - `$<param>` → 调用方传的同名参数（缺失时用 schema 里的字面量默认）
    - 字面量（如 `type: 3`）→ 原样保留，调用方不能覆盖，避免串道
    """
    meta = schema.get(method)
    if not meta:
        # 没有 schema 的方法原样透传（保持兼容），只补 id
        body = dict(params)
        body.setdefault("id", run_id)
        return body

    defaults = meta.get("defaults", {})
    body: dict[str, Any] = {}
    missing: list[str] = []
    for key, spec in meta.get("body", {}).items():
        if spec == "$run_id":
            body[key] = run_id
            continue
        if isinstance(spec, str) and spec.startswith("$"):
            param = spec[1:]
            if param in params:
                body[key] = params[param]
            elif key in params:
                body[key] = params[key]
            elif param in defaults:
                body[key] = defaults[param]
            elif param in ("start_time", "end_time"):
                body[key] = ""
            elif param == "formula_list":
                body[key] = []
            else:
                missing.append(param)
        else:
            body[key] = spec

    # 允许调用方追加 schema 未覆盖但服务端需要的字段（如 stock_page_index）
    for k, v in params.items():
        body.setdefault(k, v)

    if missing:
        raise ContractError(
            f"{method} 缺少必填参数: {', '.join(sorted(set(missing)))}"
            f"（契约要求: {sorted(set(missing))}）"
        )
    return body
