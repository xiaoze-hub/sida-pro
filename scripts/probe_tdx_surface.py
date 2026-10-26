#!/usr/bin/env python3
"""P1: 用提取出的请求体 schema 打一遍真实 17709，标注每个方法的可用状态。

输出 `surface_report.json` + `SURFACE.md`：
  ok        ErrorId=0
  param     ErrorId=10（参数缺少/结构不符，附服务端原话）
  denied    网关默认禁用（下单/撤单/资金/客户端执行 URL），不探
  skip      重接口（全市场公式扫描），不探

用法:
    python scripts/probe_tdx_surface.py --base http://172.27.16.1:17709/ \
        --schema docs/research/tdx_local_contract/http_schema.json \
        --out docs/research/tdx_local_contract
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# 按参数名构造最小合法测试值（错误码本身会告诉我们还缺什么）
TEST = {
    "stock_code": "600111.SH",
    "code": "600111.SH",
    "zs_code": "000300.SH",
    "stock_list": ["600111.SH"],
    "codes": ["600111.SH"],
    "field_list": ["FN1"],
    "table_list": ["FN1"],
    "market": "5",
    "list_type": 0,
    "period": "1d",
    "stock_period": "1d",
    "formula_name": "MACD",
    "formula_list": [12, 26, 9],
    "formula_arg": "12,26,9",
    "formula_type": 0,
    "xsflag": 6,
    "return_count": 2,
    "return_date": False,
    "count": 2,
    "dividend_type": 0,
    "start_time": "",
    "end_time": "",
    "report_type": "report_time",
    "key_word": "北方稀土",
    "url": "about:blank",
}

SKIP = {"formula_process_mul", "formula_process_mul_xg", "formula_process_mul_zb",
        "formula_process_mul_exp", "formula_get_all", "get_stock_list", "download_file",
        "create_sector", "delete_sector", "rename_sector", "clear_sector",
        "refresh_kline", "refresh_cache", "subscribe_hq", "subscribe_quote",
        "unsubscribe_hq", "send_message", "send_file", "send_warn", "send_bt_data",
        "send_user_block", "print_to_tdx", "send_source", "get_user_sector"}

# 保守测试值：get_stock_list 的 market 要合法但返回大列表，故单独限流
HEAVY_TIMEOUT = 20


def build_body(body: dict) -> dict:
    out: dict = {}
    for key, spec in body.items():
        if spec == "$run_id":
            out[key] = 1
        elif isinstance(spec, str) and spec.startswith("$"):
            param = spec[1:]
            # formula_arg 在请求体里是数字列表
            if key == "formula_arg":
                out[key] = [12, 26, 9]
            else:
                out[key] = TEST.get(param, TEST.get(key, ""))
        else:
            out[key] = spec
    return out


def call(base: str, method: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        base, data=json.dumps({"id": 1, "method": method, "params": body}).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())
    except Exception as e:
        return {"status": "exc", "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}"[:120]}
    ms = int((time.time() - t0) * 1000)
    if isinstance(raw, dict) and "error" in raw:
        return {"status": "rpc_error", "ms": ms, "error": str(raw["error"])[:120]}
    res = raw.get("result", raw)
    eid = str(res.get("ErrorId", "?")) if isinstance(res, dict) else "?"
    err = (res.get("Error") or "") if isinstance(res, dict) else ""
    if eid == "0":
        status = "ok"
    elif eid == "10":
        status = "param"
    else:
        status = f"err{eid}"
    return {"status": status, "ms": ms, "error_id": eid, "error": err[:120]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://172.27.16.1:17709/")
    ap.add_argument("--schema", default="docs/research/tdx_local_contract/http_schema.json")
    ap.add_argument("--out", default="docs/research/tdx_local_contract")
    ap.add_argument("--only", default="", help="逗号分隔的方法名，只探这些")
    ap.add_argument("--sleep", type=float, default=1.2, help="每次调用之间的间隔秒（客户端单线程，别调小）")
    args = ap.parse_args()

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    rows = {}
    only = {x for x in args.only.split(",") if x}
    for name, meta in sorted(schema["methods"].items()):
        if only and name not in only:
            continue
        if meta.get("denied"):
            rows[name] = {"status": "denied", "note": "网关禁用（下单/撤单/资金/exec_to_tdx）"}
            continue
        if name in SKIP:
            rows[name] = {"status": "skip", "note": "重接口/副作用，需单独授权后测"}
            continue
        body = build_body(meta["body"])
        if args.sleep:
            time.sleep(args.sleep)
        rows[name] = call(args.base, name, body, HEAVY_TIMEOUT)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "surface_report.json").write_text(
        json.dumps({"base": args.base, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "methods": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for r in rows.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = ["# 17709 实际可用面（探针实测）", "",
             f"- base `{args.base}`",
             f"- 统计: " + ", ".join(f"**{k}** {v}" for k, v in sorted(counts.items())), "",
             "| 方法 | 状态 | 耗时 | 服务端原话 |", "|---|---|---|---|"]
    for name, r in sorted(rows.items()):
        lines.append(f"| `{name}` | {r['status']} | {r.get('ms', '-')}ms | {r.get('error') or r.get('note') or ''} |")
    (out / "SURFACE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
