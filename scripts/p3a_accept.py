#!/usr/bin/env python3
"""P3-A 四账号生产验收 (It's working if: 全部行 PASS, 零 FAIL)。

覆盖: admin 4 项读 + P2-C datasources 回归(5 健康列, 无 500)
 + 临时 member 隔离(看不见 admin 数据/写自己数据/管理区 403/禁用后 403)
 + demo 只读(读 200/写 403/管理区 403) + 清理(临时号删除)。
黄磊/娟姐: 密码未知, 只验存在+active, 直接登录测不了(报告如实标注)。

用法: python3 /tmp/p3a_accept.py [demo_password]
退出码: 0=全 PASS(黄磊/娟姐只读存在性检查不计入), 1=有 FAIL。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "https://www.sida.hengsheng-elec.com"
ADMIN = ("admin", "xz.170530")
TMP_USER = "p3a_tmp"
TMP_PASS = "p3a-Temp-0815"
RESULTS = []


def req(method, path, token=None, body=None):
    r = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(r, timeout=25) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() or "")[:200]


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)[:160]))
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}"[:170] if detail else ""))


def login(u, p):
    s, b = req("POST", "/api/auth/login", body={"username": u, "password": p})
    if s == 200:
        return b["data"]["token"]
    return None


def main():
    demo_pass = sys.argv[1] if len(sys.argv) > 1 else None

    s, b = req("GET", "/api/health")
    ver = (b.get("data") or {}).get("version", "?")
    check("health 200 + version", s == 200 and (b.get("data") or {}).get("status") == "ok", ver)

    atok = login(*ADMIN)
    check("admin 登录", bool(atok), "")
    if not atok:
        return finish()

    # admin 读 + P2-C 回归(累计统计口径: v0.4.51 缺列 500 的同类端点)
    s, b = req("GET", "/api/datasources/health/data-sources", atok)
    d = (b.get("data") or {}) if s == 200 else {}
    items = d.get("items", []) if isinstance(d, dict) else []
    need = {"success_count", "error_count", "last_status", "last_used_at", "last_error_at"}
    cols_ok = bool(items) and all(need <= set(it) for it in items)
    check("admin health累计 200 + 5 健康列(P2-C)", s == 200 and cols_ok,
          f"n={len(items)}" if s == 200 else b)
    s, b = req("GET", "/api/datasources", atok)
    check("admin datasources 列表 200(无 500)", s == 200,
          f"n={len(b.get('data', []))}" if s == 200 else b)
    s, b = req("GET", "/api/positions", atok)
    admin_pos = b.get("data", []) if s == 200 else []
    check("admin positions 200", s == 200, f"n={len(admin_pos)}")
    s, b = req("GET", "/api/stocks", atok)
    admin_stocks = b.get("data", []) if s == 200 else []
    check("admin 自选 200", s == 200, f"n={len(admin_stocks)}")
    s, b = req("GET", "/api/notifications", atok)
    check("admin notifications 200", s == 200, "")

    # 4 账号存在性
    s, b = req("GET", "/api/auth/users", atok)
    udata = b.get("data", []) if s == 200 else []
    users = udata.get("users", udata) if isinstance(udata, dict) else udata
    umap = {u["username"]: u for u in users if isinstance(u, dict)}
    for name in ("admin", "黄磊", "娟姐", "demo"):
        u = umap.get(name)
        check(f"账号存在+active: {name}", bool(u) and u.get("is_active") is True,
              (u or {}).get("role", "缺失"))

    # 临时 member 隔离实测
    s, b = req("POST", "/api/auth/users", atok,
               body={"username": TMP_USER, "password": TMP_PASS, "role": "member"})
    tmp_id = ((b.get("data") or {}).get("user") or {}).get("id") if s == 200 else None
    check("临时 member 创建", s == 200 and bool(tmp_id), b if s != 200 else tmp_id)
    ttok = login(TMP_USER, TMP_PASS)
    check("临时 member 登录", bool(ttok), "")
    if ttok:
        s, b = req("POST", "/api/accounts", ttok,
                   body={"name": "P3A验收户", "available_funds": 100000})
        _tmp_acct = (b.get("data") or {}).get("id") if s == 200 else None
        check("member 可建自己账户", s == 200 and isinstance(_tmp_acct, int), b if s != 200 else _tmp_acct)
        s, b = req("GET", "/api/positions", ttok)
        tpos = b.get("data", []) if s == 200 else None
        check("隔离: 看不见 admin 持仓", s == 200 and tpos == [],
              f"tmp_n={len(tpos) if isinstance(tpos, list) else '?'} admin_n={len(admin_pos)}")
        s, b = req("GET", "/api/stocks", ttok)
        check("隔离: 自选独立(空)", s == 200 and b.get("data") == [], str(b)[:80])
        s, b = req("POST", "/api/stocks", ttok,
                   body={"symbol": "600519", "market": "SH", "name": "贵州茅台"})
        check("member 可写自己自选", s == 200, b if s != 200 else "ok")
        s, b = req("GET", "/api/auth/users", ttok)
        check("member 用户列表 403", s in (401, 403), s)
        s, b = req("GET", "/api/stocks", ttok)
        tstocks = b.get("data", []) if s == 200 else []
        _sid = next((x["id"] for x in tstocks if x.get("symbol") == "600519"), None)
        s, b = req("POST", "/api/positions", ttok,
                   body={"account_id": _tmp_acct, "stock_id": _sid,
                         "cost_price": 1400, "quantity": 100})
        # member 持仓写允许(自己数据)则 200
        check("member 持仓写自己数据", s == 200, b if s != 200 else "ok")
    # 清理: 删除临时号(连带其自选/持仓由外键/归属清理, 数量极少)
    if tmp_id:
        s, b = req("DELETE", f"/api/auth/users/{tmp_id}", atok)
        check("临时号删除", s == 200, b if s != 200 else "ok")
        check("删除后登录被拒", login(TMP_USER, TMP_PASS) is None, "")

    # demo 只读
    if demo_pass:
        dtok = login("demo", demo_pass)
        check("demo 登录", bool(dtok), "")
        if dtok:
            s, _ = req("GET", "/api/datasources", dtok)
            check("demo 读 200", s == 200, s)
            s, _ = req("POST", "/api/positions", dtok,
                       body={"symbol": "600519", "market": "SH", "quantity": 1, "price": 1})
            check("demo 持仓写 403", s in (401, 403), s)
            s, _ = req("GET", "/api/auth/users", dtok)
            check("demo 用户列表 403", s in (401, 403), s)
    else:
        print("SKIP demo 直接登录(密码未知, 未测)")

    return finish()


def finish():
    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== P3-A: {len(RESULTS)-len(fails)}/{len(RESULTS)} PASS ===")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
