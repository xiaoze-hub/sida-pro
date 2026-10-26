#!/usr/bin/env python3
"""从通达信官方 `PYPlugins/user/tqcenter.py` 静态提取本地 RPC 契约。

为什么这么做：`PYPlugins/` 不是黑盒 —— 官方把高层 API 以**明文 Python 源码**随客户端发布
(`user/tqcenter.py` 4.1k 行 / `user/tqserver.py` 958 行)。方法名、参数、默认值、返回结构
全在源码里，用 AST 就能 100% 还原，不需要盲猜参数，也不需要逆向。

用法:
    python scripts/extract_tdx_contract.py /tmp/tqcenter.py docs/research/tdx_local_contract
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

# 网关默认禁用: 交易/资金/外部执行类, 必须在清单里显式标注出来
RISK = {
    "order_stock": "下单",
    "cancel_order_stock": "撤单",
    "stock_account": "资金账户登录",
    "query_stock_asset": "资金查询",
    "query_stock_orders": "委托查询",
    "query_stock_positions": "持仓查询",
    "exec_to_tdx": "客户端内执行 URL(可被滥用)",
}

GROUPS = {
    "行情": ("get_market_data", "get_market_snapshot", "get_more_info", "get_minute", "get_pricevol",
             "get_real", "get_his", "get_exday", "get_zdt", "refresh_kline", "refresh_cache"),
    "F10/基本面": ("get_stock_info", "get_financial_data", "get_gb_info", "get_divid_factors",
                   "get_kzz_info", "get_ipo_info", "get_trackzs_etf_info", "get_relation"),
    "板块": ("get_sector", "get_stock_list_in_sector", "create_sector", "delete_sector",
             "rename_sector", "clear_sector", "get_user_sector"),
    "公式": ("formula", "tdx_formula"),
    "专业序列": ("get_gpjy", "get_bkjy", "get_scjy", "get_gp_one_data"),
    "订阅/推送": ("subscribe", "unsubscribe", "send_", "print_to_tdx"),
    "工具": ("get_trading_calendar", "get_trading_dates", "get_stock_list", "get_match_stkinfo",
             "download_file", "get_python_version", "initialize", "close"),
}


def classify(name: str) -> str:
    for group, keys in GROUPS.items():
        if any(k in name for k in keys):
            return group
    return "其他"


def extract(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        args = [a.arg for a in node.args.args if a.arg not in ("cls", "self")]
        defaults = {}
        offset = len(node.args.args) - len(node.args.defaults)
        for i, d in enumerate(node.args.defaults):
            arg = node.args.args[offset + i].arg
            try:
                defaults[arg] = ast.literal_eval(d)
            except Exception:
                defaults[arg] = {"$expr": ast.unparse(d)}
        doc = (ast.get_docstring(node) or "").strip().splitlines()
        out[node.name] = {
            "line": node.lineno,
            "args": args,
            "defaults": defaults,
            "group": classify(node.name),
            "risk": RISK.get(node.name),
            "doc": doc[0] if doc else "",
            "decorators": [ast.unparse(d) for d in node.decorator_list],
        }
    return out


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tqcenter.py")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/research/tdx_local_contract")
    contract = extract(src)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "contract.json").write_text(
        json.dumps({"source": str(src), "count": len(contract), "methods": contract},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    by_group: dict[str, list[str]] = {}
    for name, meta in contract.items():
        by_group.setdefault(meta["group"], []).append(name)

    lines = ["# 通达信本地 API 契约（静态提取，非逆向）", "",
             f"- 来源: `{src}`  ({sum(1 for _ in src.open(encoding='utf-8', errors='replace'))} 行)",
             f"- 公开方法: **{len(contract)}**", "",
             "| 分组 | 方法数 | 方法 |", "|---|---|---|"]
    for group, names in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"| {group} | {len(names)} | {', '.join(sorted(names))} |")
    risky = [n for n, m in contract.items() if m["risk"]]
    lines += ["", "## 网关必须默认禁用的方法", "", "| 方法 | 风险 |", "|---|---|"]
    lines += [f"| `{n}` | {contract[n]['risk']} |" for n in sorted(risky)]
    (dst / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"methods={len(contract)} groups={len(by_group)} risky={len(risky)} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
