#!/usr/bin/env python3
"""SIDA 数据能力 · MCP server（stdio，2026-10-25）。

把 SIDA 的**只读数据能力**包成 MCP server，让 Claude Code / Codex / Cursor 这类
原生 MCP 客户端能直接调用，而不是只能 `curl | bash` 装个 skill。

设计取舍（为什么不新写数据接口）：
  - 数据出口已经存在且带配额/鉴权/口径标注（`/api/skills/catalog`、`/api/skills/{name}/run`，
    `X-API-Key`）。这个 server 只是**协议适配层**：MCP JSON-RPC → HTTP，不重复实现取数，
    配额与限流仍由既有网关管，不另起炉灶。
  - 工具只有两个元工具（列能力 / 调能力），具体能力由目录动态展开 —— 网关加 skill，
    这边零改动就能用；硬编码工具清单必然和网关漂移。

安全边界：
  - **只读**。调用前过白名单检查：名字里带 trade/order/buy/sell/position-alter 的一律拒绝，
    本 server 不含任何下单能力（agent-kernel 非目标：不做自动交易）。
  - 不落盘、不缓存响应内容、不打印 key。

用法：
  export SIDA_API_KEY=sk_xxx            # https://<站点>/api-keys 领取
  export SIDA_BASE_URL=https://SIDA.hengsheng-elec.com   # 可选
  python3 scripts/sida_mcp_server.py

MCP 客户端配置（Claude Code / Codex 的 mcpServers）：
  {"sida": {"command": "python3", "args": ["<repo>/scripts/sida_mcp_server.py"],
            "env": {"SIDA_API_KEY": "sk_xxx"}}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "sida-data"
SERVER_VERSION = "1.0.0"

DEFAULT_BASE_URL = "https://SIDA.hengsheng-elec.com"

#: 交易相关一律不出现在这个出口（只读数据 server 的安全底线）
FORBIDDEN_NAME_PARTS = ("trade", "order", "buy", "sell", "place_", "cancel_", "withdraw")


class ToolError(Exception):
    """工具层错误：以 MCP `isError` 返回给客户端，不终止 server。"""


def is_forbidden(name: str) -> bool:
    low = str(name or "").lower()
    return any(part in low for part in FORBIDDEN_NAME_PARTS)


def _http(method: str, url: str, *, api_key: str, body: dict | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 固定 base_url
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise ToolError(f"网关返回 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"连不上 SIDA 网关({url}): {exc.reason}") from exc


def _unwrap(payload: dict) -> Any:
    """网关统一包了 {code, success, data}；取 data，失败抛 ToolError 带原文。"""
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            raise ToolError(str(payload.get("message") or payload.get("detail") or "网关返回失败"))
        return payload.get("data")
    return payload


TOOLS: list[dict] = [
    {
        "name": "sida_list_skills",
        "description": (
            "列出 SIDA 数据能力目录（名称/说明/口径/参数 schema/是否慢查询）。"
            "实施任何取数前先调它，按 name 再调 sida_run_skill。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sida_run_skill",
        "description": (
            "执行一个 SIDA 数据能力，返回 handler 输出的文本 + 口径标签。"
            "口径不可换算：返回里的 caliber 必须原样带给用户，不得省略或聚合。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "能力名（来自 sida_list_skills）"},
                "params": {
                    "type": "object",
                    "description": "能力参数（键值见目录里的 params）",
                    "additionalProperties": True,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]


def make_handlers(
    *, api_key: str, base_url: str, transport: Callable[..., dict] | None = None
) -> dict[str, Callable[[dict], dict]]:
    """构造元工具实现。`transport` 可注入（测试用，避免真网络）。"""
    http = transport or (lambda method, url, **kw: _http(method, url, api_key=api_key, **kw))
    base = base_url.rstrip("/")

    def list_skills(_args: dict) -> dict:
        data = _unwrap(http("GET", f"{base}/api/skills/catalog"))
        skills = data.get("skills") if isinstance(data, dict) else data
        if not isinstance(skills, list):
            raise ToolError("目录响应格式不认识（缺 skills 数组）")
        items = [
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "caliber": s.get("caliber"),
                "slow": s.get("slow"),
                "params": (s.get("params") or {}),
                "required": s.get("required") or [],
            }
            for s in skills
            if isinstance(s, dict) and s.get("name") and not is_forbidden(str(s.get("name")))
        ]
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"count": len(items), "skills": items}, ensure_ascii=False),
                }
            ]
        }

    def run_skill(args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        params = args.get("params") or {}
        if not name:
            raise ToolError("缺少 name")
        if is_forbidden(name):
            raise ToolError(f"拒绝执行 {name}：该出口只读，不含任何交易能力")
        if not isinstance(params, dict):
            raise ToolError("params 必须是对象")
        data = _unwrap(
            http("POST", f"{base}/api/skills/{name}/run", body={"params": params})
        )
        text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
        return {"content": [{"type": "text", "text": text}]}

    return {"sida_list_skills": list_skills, "sida_run_skill": run_skill}


def handle(payload: dict, handlers: dict[str, Callable[[dict], dict]]) -> dict | None:
    """处理一条 JSON-RPC 请求；通知（无 id）返回 None。"""
    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        fn = handlers.get(name)
        if fn is None:
            return err(-32602, f"未知工具 {name}")
        try:
            return ok(fn(params.get("arguments") or {}))
        except ToolError as exc:
            # 工具层失败以 result.isError 返回：客户端能看到原文，且会话不中断
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if method == "ping":
        return ok({})
    return err(-32601, f"不支持的方法 {method}")


def main() -> int:  # pragma: no cover — 进程入口
    api_key = os.environ.get("SIDA_API_KEY", "").strip()
    if not api_key:
        print("SIDA_API_KEY 未设置（https://<站点>/api-keys 领取）", file=sys.stderr)
        return 2
    base = os.environ.get("SIDA_BASE_URL", DEFAULT_BASE_URL)
    handlers = make_handlers(api_key=api_key, base_url=base)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "解析失败"}}
        else:
            out = handle(payload, handlers)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
