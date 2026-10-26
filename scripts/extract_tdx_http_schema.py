#!/usr/bin/env python3
"""从 `tqcenter.py` 静态提取**本地 HTTP/17709 请求体契约**（P1）。

背景（2026-10-19 实测）：`PYPlugins/user/tqcenter.py` 每个方法体里都构造一个
`code_json` / `batch_json`，其中包含三样"隐藏参数"：
  - `id`      客户端会话 run_id（裸调 HTTP 必须自己给，否则报参数错误）
  - `type`    方法判别码（快照 3 / 股票列表 0 / 财务 1 / 交易数据 …）
  - `table_list` 财务/GP/SC 类专业序列字段清单 —— 漏了就是 `ErrorId=10:
              json has no table_list`，长期被误判成"要下载盘后专业数据包"

本脚本把每个公开方法的请求体反解成机器可读 schema：
    method -> {"body": {key: "<字面量>" | "<参数名>"}, "via": "GetTdxDataStr|GetProDataInStr|..."}

用法:
    python scripts/extract_tdx_http_schema.py /tmp/tqcenter.py docs/research/tdx_local_contract
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

# 网关必须默认拒之门外的调用（真实资金账户 + 客户端内执行 URL）
DENY = {
    "order_stock", "cancel_order_stock", "exec_to_tdx",
    "stock_account", "query_stock_asset", "query_stock_orders", "query_stock_positions",
}


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except Exception:
        return {"$expr": ast.unparse(node)}


def _body_from(func: ast.FunctionDef) -> dict | None:
    """找到方法体里构造请求体的 dict（变量名含 json / request）。"""
    best: dict | None = None
    for node in ast.walk(func):
        if not isinstance(node, ast.Dict):
            continue
        keys = []
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.append(k.value)
        if "type" not in keys and "id" not in keys:
            continue
        body = {}
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            if isinstance(v, ast.Name):
                body[k.value] = f"${v.id}"          # 来自方法参数
            elif isinstance(v, ast.Attribute) and v.attr in ("_get_run_id",):
                body[k.value] = "$run_id"
            elif isinstance(v, ast.Call) and "run_id" in ast.unparse(v):
                body[k.value] = "$run_id"
            else:
                body[k.value] = _literal(v)
        # 取字段最多的那个 dict（方法里可能有多个小 dict）
        if best is None or len(body) > len(best):
            best = body
    return best


def _transport(func: ast.FunctionDef) -> str:
    text = ast.get_source_segment(SOURCE, func) or ""
    for fn in ("GetProDataInStr", "GetTdxDataStr", "TdxFuncMain", "GetOrderStr"):
        if fn in text:
            return fn
    if "_get_pro_data_json" in text:
        return "GetProDataInStr"
    return "?"


SOURCE: str = ""


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tqcenter.py")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/research/tdx_local_contract")
    text = src.read_text(encoding="utf-8", errors="replace")
    global SOURCE
    SOURCE = text
    tree = ast.parse(text)

    out: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        body = _body_from(node)
        if not body:
            continue
        defaults = {}
        offset = len(node.args.args) - len(node.args.defaults)
        for i, d in enumerate(node.args.defaults):
            arg = node.args.args[offset + i].arg
            try:
                defaults[arg] = ast.literal_eval(d)
            except Exception:
                continue

        out[node.name] = {
            "line": node.lineno,
            "transport": _transport(node),
            "body": body,
            "defaults": defaults,
            "denied": node.name in DENY,
        }

    dst.mkdir(parents=True, exist_ok=True)
    (dst / "http_schema.json").write_text(
        json.dumps({"source": str(src), "count": len(out), "methods": out},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # 人读表
    lines = ["# 通达信本地 HTTP 请求体契约", "",
             f"- 来源: `{src}`；方法数 **{len(out)}**（含 `id`/`type`/`table_list` 等隐藏字段）", "",
             "| 方法 | 传输 | 请求体字段 | 网关 |", "|---|---|---|---|"]
    for name in sorted(out):
        meta = out[name]
        fields = ", ".join(f"`{k}`" + ("" if isinstance(v, (int, float, str)) else "") for k, v in meta["body"].items())
        lines.append(f"| `{name}` | {meta['transport']} | {fields} | {'⛔ 禁用' if meta['denied'] else '✅'} |")
    (dst / "HTTP_SCHEMA.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"methods with body={len(out)} denied={sum(1 for m in out.values() if m['denied'])} -> {dst/'http_schema.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
