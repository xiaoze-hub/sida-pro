#!/usr/bin/env python3
"""SIDA 发版冒烟测试(2026-08-21)。

用法:
  # 密码登录(自动拿 token)
  python smoke_test.py --base https://www.sida.hengsheng-elec.com --user admin --password xxx
  # 直接给 token
  SMOKE_TOKEN=xxx python smoke_test.py --base http://127.0.0.1:8000
  # 无 auth 模式(只测公开端点)
  python smoke_test.py --base http://127.0.0.1:8000 --no-auth

退出码: 全 PASS=0, 任一 FAIL=1。
"""
import argparse
import os
import sys
import time

import requests

RESULTS: list[tuple[str, bool, float, str]] = []


def _record(name: str, ok: bool, dt: float, note: str = "") -> None:
    RESULTS.append((name, ok, dt, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} {dt*1000:7.0f}ms  {note[:80]}")


def _get(base: str, path: str, token: str | None, timeout: float = 15.0):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    t0 = time.time()
    r = requests.get(f"{base}{path}", headers=headers, timeout=timeout)
    return r, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--token", default=os.environ.get("SMOKE_TOKEN", ""))
    ap.add_argument("--no-auth", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    token = args.token
    if not args.no_auth and not token:
        t0 = time.time()
        try:
            r = requests.post(
                f"{base}/api/auth/login",
                json={"username": args.user, "password": args.password},
                timeout=10,
            )
            j = r.json()
            token = (j.get("data") or {}).get("token") or ""
            ok = r.status_code == 200 and bool(token)
            _record("POST /api/auth/login", ok, time.time() - t0,
                    "" if ok else f"status={r.status_code}")
            if not ok:
                print("!! 登录失败, 后续 auth 端点将 FAIL。检查密码或用 --token/--no-auth")
        except Exception as e:  # noqa: BLE001
            _record("POST /api/auth/login", False, time.time() - t0, str(e)[:80])

    def auth() -> str | None:
        return None if args.no_auth else (token or None)

    # ── 公开端点 ──
    try:
        r, dt = _get(base, "/api/health", None)
        j = r.json() if r.status_code == 200 else {}
        db_ok = ((j.get("data") or {}).get("components") or {}).get("database", {}).get("status") == "ok"
        _record("GET /api/health", r.status_code == 200 and db_ok, dt,
                f"db_ok={db_ok}")
    except Exception as e:  # noqa: BLE001
        _record("GET /api/health", False, 0.0, str(e)[:80])

    # ── 需要登录的端点 ──
    checks = [
        ("/api/stocks", lambda j: isinstance(j.get("data"), list) and len(j["data"]) > 0),
        ("/api/datasources", lambda j: j.get("success") is True),
        ("/api/settings", lambda j: j.get("success") is True),
        ("/api/notifications", lambda j: j.get("success") is True),
        ("/api/agents", lambda j: j.get("success") is True),
    ]
    for path, validator in checks:
        try:
            r, dt = _get(base, path, auth())
            j = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and validator(j)
            _record(f"GET {path}", ok, dt, "" if ok else f"status={r.status_code}")
        except Exception as e:  # noqa: BLE001
            _record(f"GET {path}", False, 0.0, str(e)[:80])

    # 主力意图轻接口(数据链路核心)
    try:
        r, dt = _get(base, "/api/dark-flow?symbol=002361", auth())
        j = r.json() if r.status_code == 200 else {}
        mi = ((j.get("data") or {}).get("main_intent")) or {}
        ok = r.status_code == 200 and "main_net" in mi
        _record("GET /api/dark-flow?symbol=002361", ok, dt,
                f"main_net={mi.get('main_net')}" if ok else f"status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        _record("GET /api/dark-flow", False, 0.0, str(e)[:80])

    try:
        r, dt = _get(base, "/api/main-flow/compare/002361", auth(), timeout=40)
        ok = r.status_code == 200
        _record("GET /api/main-flow/compare/002361", ok, dt,
                "" if ok else f"status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        _record("GET /api/main-flow/compare", False, 0.0, str(e)[:80])

    try:
        r, dt = _get(base, "/api/klines/002361/summary?market=CN", auth(), timeout=45)
        ok = r.status_code == 200
        _record("GET /api/klines/.../summary", ok, dt,
                "" if ok else f"status={r.status_code}")
    except Exception as e:  # noqa: BLE001
        _record("GET /api/klines summary", False, 0.0, str(e)[:80])

    passed = sum(1 for _, ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n===== 冒烟结果: {passed}/{total} passed, 总耗时 "
          f"{sum(dt for _, _, dt, _ in RESULTS):.1f}s =====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
