#!/usr/bin/env python3
"""公开面(未登录)部署后实证 —— P2 阶段的验收脚本, 2026-09-18。

## 为什么需要它
P2-4 的档位页在生产上"接口 200 但页面空白"—— 根因是前端把路径拼成了 `/api/api/tiers`(404)。
**后端测试查不出这类问题**(接口本身是好的), 只有浏览器看**真实发出的请求路径**才行。
所以这个脚本专门盯:
  1. 公开路由是否真的公开(不被弹回登录);
  2. 页面关键文案是否渲染(证明数据取到了, 而不是停在"取不到"分支);
  3. **实际请求的 API 路径**(不是代码里写了什么);
  4. 公开面纪律: 字阶档数、无外部 CDN、无价格字段。

用法:
    SIDA_SHOT_PW=... python3 scripts/anon_probe.py [base_url]

退出码: 0 = 全通过; 1 = 有失败项(逐条打印)。
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

DEFAULT_BASE = "https://www.sida.hengsheng-elec.com"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    fails: list[str] = []
    notes: list[str] = []

    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        pg = ctx.new_page()

        # ── /tiers: 公开档位页(未登录) ──────────────────────────────
        api_calls: list[str] = []
        pg.on("response", lambda r: api_calls.append(f"{r.status} {r.url}") if "/api/" in r.url else None)
        pg.goto(f"{base}/tiers", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(3500)
        t = pg.evaluate("() => document.body.innerText || ''")
        fonts = pg.evaluate("""() => new Set([...document.querySelectorAll('body *')]
            .filter(e => e.offsetParent !== null)
            .filter(e => [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()))
            .map(e => getComputedStyle(e).fontSize)).size""")

        if "/login" in pg.url:
            fails.append("/tiers 未登录被弹回登录页(档位页应是公开面)")
        if "内测期不收费" not in t:
            fails.append("/tiers 没渲染出「内测期不收费」—— 多半是接口没取到(检查请求路径)")
        if "自选股：" not in t:
            fails.append("/tiers 没渲染出免费档实时上限 —— 同上, 数据未到位")
        if "免费注册" not in t:
            fails.append("/tiers 未登录时缺少「免费注册」入口(转化路径断了)")
        if any(x in t for x in ("￥", "¥", "USD", "元/月")):
            fails.append("/tiers 出现了价格字样 —— 内测期不收费, 不该有价格区")
        if fonts > 7:
            fails.append(f"/tiers 字号 {fonts} 档 > 公开面上限 7 档")

        bad_paths = [c for c in api_calls if " 200 " not in c and "/api/version" not in c]
        for c in bad_paths:
            fails.append(f"/tiers 发起了失败请求: {c}")
        if any("/api/api/" in c for c in api_calls):
            fails.append("/tiers 请求出现双前缀 /api/api/ —— fetchAPI 的 baseURL 已含 /api")
        if not any(c.endswith("/api/tiers") and c.startswith("200") for c in api_calls):
            fails.append("/tiers 没有成功拉到 /api/tiers")
        notes.append(f"/tiers API 请求: {[c.split(' ')[1] for c in api_calls if '/api/' in c][:4]}")

        # ── /developers: 公开文档页必须带可见可交互的调试台 ──────────
        pg.goto(f"{base}/developers", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2500)
        dev = pg.evaluate("""() => {
          const play = document.querySelector('#sec-playground');
          const vis = e => !!e && e.offsetParent !== null;
          return {
            redirected: location.pathname.includes('login'),
            hasPlayground: vis(play),
            inputs: play ? play.querySelectorAll('input, button[role=combobox]').length : 0,
            runBtn: play ? [...play.querySelectorAll('button')].filter(b => /运行|发送|执行/.test(b.innerText)).length : 0,
          };
        }""")
        if dev["redirected"]:
            fails.append("/developers 未登录被弹回登录页(应是公开面)")
        if not dev["hasPlayground"]:
            fails.append("/developers 缺在线调试台(#sec-playground 不可见)")
        if dev["runBtn"] == 0:
            fails.append("/developers 调试台没有可点的运行按钮")
        notes.append(f"/developers 调试台控件数={dev['inputs']} 运行按钮={dev['runBtn']}")

        # ── 落地页: 首屏必须答"给谁用", 且不引外部 CDN ───────────────
        pg.goto(f"{base}/", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(2000)
        home = pg.evaluate("""() => ({
          text: (document.body.innerText || '').slice(0, 1200),
          extDeps: [...document.querySelectorAll('script[src], link[href]')]
            .map(e => e.getAttribute('src') || e.getAttribute('href') || '')
            .filter(u => /^https?:\\/\\//.test(u) && !u.includes(location.host)),
        })""")
        if not any(k in home["text"] for k in ("给谁用", "开发者", "操盘手")):
            fails.append("落地页首屏没答「给谁用」")
        if home["extDeps"]:
            fails.append(f"落地页引了外部依赖(禁外部 CDN): {home['extDeps'][:2]}")
        notes.append(f"落地页外部依赖={len(home['extDeps'])}")

        b.close()

    print("=== 公开面部署后实证 ===")
    for n in notes:
        print(f"  · {n}")
    if fails:
        print("\n失败项:")
        for f in fails:
            print(f"  ✗ {f}")
        print(f"\n结果: {len(fails)} 项不通过")
        return 1
    print("\n结果: 全部通过 ✅")
    return 0


if __name__ == "__main__":
    if not os.environ.get("SIDA_SHOT_PW"):
        print("缺少环境变量 SIDA_SHOT_PW(只从环境读, 不落盘)")   # 仅提示, 匿名页面不需要密码
    sys.exit(main())
