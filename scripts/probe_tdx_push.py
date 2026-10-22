#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通达信「订阅推送」只读探针（人工运行，脚本不会自动执行）。

目的
----
验证 `TPythClient.dll` 的 `Register_DataTransferFunc` / `SubscribeGPData` 这条
**进程内**回调链到底能不能收到推送数据。

结论口径见 docs/research/tdx_local_contract/subscription_push.md：
  * 这两个符号是原生 DLL 导出，**经 17709 HTTP 拿不到回调**；
  * 要接只能**自己起一个进程**加载 `TPythClient.dll`（本脚本干的事），
    或者在客户端进程内挂策略插件。

安全边界（硬红线，改代码前先读）
------------------------------
  * **只读**：只调 `InitConnect` / `Register_DataTransferFunc` / `SubscribeGPData`
    / `CloseConnect`。**绝不**碰 `GetOrderStr`（下单/撤单/资金）系列。
  * **不写文件**：除 stdout 外不落盘。
  * **不改客户端**：不改 `PYPlugins/`、不改 `py_strategy.cfg`、不重启 `TdxW.exe`。
  * **自动收尾**：默认 90 秒后自己 `CloseConnect` 退出，不留常驻进程。
  * **单实例**：若 `InitConnect` 返回 `run_id < 0`（同名策略已在跑），**立即放弃**，
    不重试、不轰炸客户端。

已知风险
--------
`TdxW.exe` 在 ~4000 req/min 的既有负载下会偶发 `TPyth.dll` 访问冲突崩溃
（2026-10-16 / 10-19 / 10-20 / 10-22 均出现过）。本探针请求量极低（1 次订阅），
但**仍应避开交易高峰**运行。

用法
----
在**小主机 Windows** 上，用已装的 Python 3.11 跑：

    "C:\\Program Files\\Python311\\python.exe" scripts\\probe_tdx_push.py
    "C:\\Program Files\\Python311\\python.exe" scripts\\probe_tdx_push.py --seconds 120
    "C:\\Program Files\\Python311\\python.exe" scripts\\probe_tdx_push.py --codes 600111.SH,000001.SZ

判定标准
--------
  * 收到 >= 1 条 `{"Code":..., "ErrorId":"0"}`  -> 推送链**通**
  * 60+ 秒一条都没有                            -> 订阅**未生效**（看 ErrorId）
  * `InitConnect` 返回 `run_id < 0`             -> 客户端已有同名策略，**放弃**
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

# 客户端 PYPlugins 目录（Windows 侧）。可用 --dll-dir 覆盖。
DEFAULT_DLL_DIR = Path(r"C:\new_tdx64\PYPlugins")

# 这些符号是交易/资金类，本探针**永不**触碰（双保险）。
FORBIDDEN = (
    "GetOrderStr", "order_stock", "cancel_order_stock",
    "stock_account", "query_stock_asset", "query_stock_orders", "query_stock_positions",
)

# 回调签名：void (*)(char*)  —— 与 tqcenter.py:2516 的
#   ctypes.CFUNCTYPE(None, ctypes.c_char_p) 完全一致。
CALLBACK_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_run_id(ptr) -> int:
    """InitConnect 返回的是 JSON 字符串，里面才有真正的 run_id。"""
    if not ptr:
        return -1
    try:
        obj = json.loads(ptr.decode("utf-8"))
    except Exception as exc:                       # noqa: BLE001
        _log(f"InitConnect 返回不是 JSON: {exc}; raw={ptr[:120]!r}")
        return -1
    _log(f"InitConnect -> {json.dumps(obj, ensure_ascii=False)[:300]}")
    # ErrorId=0 表示正常；12 表示"已存在同名策略"，run_id 仍可能有值
    return int(obj.get("run_id", -1))


def main() -> int:
    ap = argparse.ArgumentParser(description="通达信订阅推送只读探针")
    ap.add_argument("--dll-dir", default=str(DEFAULT_DLL_DIR),
                    help="TPythClient.dll 所在目录（默认 C:\\new_tdx64\\PYPlugins）")
    ap.add_argument("--codes", default="600111.SH",
                    help="要订阅的证券代码，逗号分隔（建议只放 1~2 只做验证）")
    ap.add_argument("--seconds", type=int, default=90,
                    help="监听时长秒数（默认 90，结束后自动 CloseConnect 退出）")
    ap.add_argument("--period", default="1d", help="订阅周期（默认 1d）")
    args = ap.parse_args()

    dll_path = Path(args.dll_dir) / "TPythClient.dll"
    if not dll_path.exists():
        _log(f"❌ 找不到 {dll_path}")
        _log("   请在**小主机 Windows** 上运行本脚本（不是 Linux 容器），"
             "或用 --dll-dir 指定实际路径。")
        return 2

    # Windows 下让同目录的依赖 DLL（tdxrpcx64.dll 等）能被加载
    _add_dll_dir = getattr(ctypes, "add_dll_directory", None)
    if _add_dll_dir is not None:
        _add_dll_dir(str(dll_path.parent))

    dll = ctypes.CDLL(str(dll_path))
    dll.InitConnect.restype = ctypes.c_char_p
    dll.Register_DataTransferFunc.restype = None
    # SubscribeGPData 签名（对齐 tqcenter.py:2527）：
    # (run_id, code, start_time, end_time, period, dividend, count, timeout_ms)
    dll.SubscribeGPData.restype = ctypes.c_char_p
    dll.CloseConnect.restype = None

    for name in FORBIDDEN:
        if hasattr(dll, name):
            _log(f"⚠️  DLL 导出了 {name}，本探针**不会**调用它（只读边界）")

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    _log(f"目标 DLL : {dll_path}")
    _log(f"订阅代码 : {codes}")

    # ---- 1) 建立连接，拿进程内 run_id -------------------------------------
    _log("调用 InitConnect ...")
    ptr = dll.InitConnect(
        str(Path(__file__).resolve()).encode("utf-8"),   # file_name（策略脚本路径）
        str(dll_path).encode("utf-8"),                  # dll_path
        ctypes.c_int(0),                                # run_mode
        ctypes.c_int(311),                              # python 版本号 (3.11)
        ctypes.c_bool(False),                           # reInitialized
    )
    run_id = _read_run_id(ptr)
    if run_id < 0:
        _log("❌ InitConnect 没拿到 run_id —— 客户端可能已有同名策略在跑。")
        _log("   **放弃，不重试**（避免打扰客户端）。")
        return 1
    _log(f"✅ run_id = {run_id}")

    # ---- 2) 注册进程内回调 -------------------------------------------------
    stats = {"count": 0, "first_at": None, "codes": set()}
    lock_free = True  # ctypes 回调在同一线程内触发，简单计数即可

    def _on_data(data_ptr) -> None:
        try:
            text = data_ptr.decode("utf-8", errors="replace") if data_ptr else ""
        except Exception:                              # noqa: BLE001
            text = repr(data_ptr)
        stats["count"] += 1
        if stats["first_at"] is None:
            stats["first_at"] = time.time()
        try:
            code = json.loads(text).get("Code")
        except Exception:                              # noqa: BLE001
            code = None
        if code:
            stats["codes"].add(code)
        _log(f"📨 推送 #{stats['count']}  {text[:300]}")

    cb = CALLBACK_FUNC_TYPE(_on_data)
    dll.Register_DataTransferFunc(run_id, cb)
    _log("✅ Register_DataTransferFunc 已注册（进程内 C 函数指针）")

    # ---- 3) 订阅 ----------------------------------------------------------
    for code in codes:
        _log(f"调用 SubscribeGPData({code}) ...")
        res = dll.SubscribeGPData(
            run_id,
            code.encode("utf-8"),
            b"",                       # start_time
            b"",                       # end_time
            args.period.encode("utf-8"),
            ctypes.c_int(0),           # dividend_type: none
            ctypes.c_int(0),           # count
            ctypes.c_int(5000),        # timeout_ms
        )
        if res:
            _log(f"   返回: {res.decode('utf-8', errors='replace')[:300]}")
        else:
            _log("   返回空指针（订阅可能未生效）")

    # ---- 4) 监听 ----------------------------------------------------------
    _log(f"监听 {args.seconds}s ...（推送会通过上面的回调打印）")
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(1)

    # ---- 5) 收尾 ----------------------------------------------------------
    try:
        dll.CloseConnect(run_id, ctypes.c_int(0))
        _log("✅ CloseConnect 已调用")
    except Exception as exc:                           # noqa: BLE001
        _log(f"⚠️ CloseConnect 异常: {exc}")

    elapsed = (stats["first_at"] - (deadline - args.seconds)) if stats["first_at"] else None
    _log("=" * 60)
    _log(f"收到推送条数 : {stats['count']}")
    _log(f"涉及代码     : {sorted(stats['codes']) or '(无)'}")
    if stats["first_at"]:
        _log(f"首条延迟     : {elapsed:.1f}s")
    verdict = "✅ 推送链【通】——可以评估接入" if stats["count"] else "❌ 推送链【不通】——订阅未生效"
    _log(verdict)
    _log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
