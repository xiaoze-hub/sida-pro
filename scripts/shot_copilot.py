"""生产参谋页截图（2026-10-26）。

用途：改完 UI 必须自己看一眼真实渲染，而不是"测试全绿就说好了"。
做法：用 JWT 注入 cookie（不输密码），Playwright 无头 Chromium 截图。

用法: python3 scripts/shot_copilot.py <输出路径> [宽度]
"""

from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "https://SIDA.hengsheng-elec.com"
TOKEN_FILE = pathlib.Path("/tmp/sida_tok.txt")


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/copilot.png")
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1600
    route = sys.argv[3] if len(sys.argv) > 3 else "/ai-copilot"
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 1000
    token = TOKEN_FILE.read_text().strip()
    if not token:
        print("缺少 token：先写 /tmp/sida_tok.txt")
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": width, "height": height})
        # 域名不带点会与重定向后的主机名不匹配 -> 用宽域 cookie
        ctx.add_cookies(
            [{"name": "sida_token", "value": token, "domain": ".hengsheng-elec.com",
              "path": "/", "secure": True, "sameSite": "Lax"}]
        )
        page = ctx.new_page()
        # 前端 UI 角色读 localStorage.token（后端鉴权走 cookie），两个都补上
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page.evaluate("t => localStorage.setItem('token', t)", token)
        page.goto(f"{BASE}{route}", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        # 登录页说明 cookie 没生效
        if "/login" in page.url:
            print(f"被弹回登录页: {page.url}")
            browser.close()
            return 3
        page.screenshot(path=str(out), full_page=False)
        print(f"OK {out} url={page.url}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
