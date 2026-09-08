"""SIDA-Pro 数据接口 thin-client(Hermes skill 用)。

只做三件事: 拼 URL、带认证头、打印 JSON。不写业务逻辑。
认证:
  - 读口(quotes/klines/health): SIDA_SERVICE_TOKEN(服务 token,只进只读口)
  - 写口/用户口(decision/trust/accuracy/dark-flow): SIDA_USER_TOKEN(用户 JWT,/api/auth/login 拿)
  - 地址: SIDA_BASE_URL(默认 http://100.91.30.35:8000, 生产小主机 Tailscale 内网)

用量示例:
  SIDA_BASE_URL=http://100.91.30.35:8000 SIDA_SERVICE_TOKEN=xxx python sida.py quote 002361
  SIDA_BASE_URL=... SIDA_USER_TOKEN=yyy python sida.py decision 002361
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("SIDA_BASE_URL", "http://100.91.30.35:8000").rstrip("/")
SVC = os.environ.get("SIDA_SERVICE_TOKEN", "")
USER = os.environ.get("SIDA_USER_TOKEN", "")

# 端点表: (方法, 路径模板, 认证级). 认证级: svc=服务token可进, user=需用户JWT, open=免认证
ENDPOINTS = {
    "health": ("GET", "/api/health", "open"),
    "quote": ("GET", "/api/quotes/{s}", "svc"),
    "quotes": ("GET", "/api/quotes?symbols={s}", "svc"),
    "klines": ("GET", "/api/klines/{s}?period={p}", "svc"),
    "minute": ("GET", "/api/quotes/minute/{s}", "svc"),
    "moreinfo": ("GET", "/api/quotes/{s}/more-info", "svc"),
    "decision": ("GET", "/api/decision/{s}", "user"),
    "trust": ("GET", "/api/datasources/trust", "user"),
    "accuracy": ("GET", "/api/stats/accuracy?days={p}", "user"),
    "darkflow": ("GET", "/api/dark-flow/{s}", "user"),
    "company": ("GET", "/api/quotes/{s}/company", "svc"),
}

UNIT_NOTE = "单位铁律: 金额=元, 成交量=股; 缺数显式无数据, 禁止推测编造。"


def call(name: str, s: str = "", p: str = "") -> dict:
    if name not in ENDPOINTS:
        raise SystemExit(f"未知端点 {name}, 可选: {','.join(sorted(ENDPOINTS))}")
    method, tpl, level = ENDPOINTS[name]
    token = SVC if level == "svc" else (USER if level == "user" else "")
    if level in ("svc", "user") and not token:
        need = "SIDA_SERVICE_TOKEN" if level == "svc" else "SIDA_USER_TOKEN"
        raise SystemExit(f"{name} 需认证级 {level}, 请设置环境变量 {need}")
    url = BASE + tpl.format(s=urllib.parse.quote(s), p=urllib.parse.quote(p or _default_p(name)))
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise SystemExit(f"请求失败 {url}: {e}")


def _default_p(name: str) -> str:
    return {"klines": "1d", "accuracy": "30"}.get(name, "")


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[1] in ("-h", "--help", "list"):
        print("用法: sida.py <端点> [symbol] [period]")
        print("端点: " + ", ".join(sorted(ENDPOINTS)))
        print(UNIT_NOTE)
        return
    name, s, p = argv[1], (argv[2] if len(argv) > 2 else ""), (argv[3] if len(argv) > 3 else "")
    print(json.dumps(call(name, s, p), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv)
