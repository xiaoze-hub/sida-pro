"""生产验收: 行情页返回入口(2026-09-20 v0.10.57)。

在**生产**上跑: 机会页点「洞察」→ 行情页 → 断言出现「← 返回机会」→ 点击 → 必须回到机会页;
再单开一页直接访问 /stocks/002361(无来路) → 断言**不渲染**返回按钮。

数据来源: 机会页对 admin 有真实候选(持仓页对 admin 是空态, 持仓在黄磊/娟姐账号)。
输出给 postdeploy_verify 用: 末尾打印 RESULT=PASS/FAIL。
"""
import os
import sys
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SIDA_BASE", "https://www.sida.hengsheng-elec.com")
PW = os.environ["SIDA_SHOT_PW"]

JS_BACK = """() => {
  const b = Array.from(document.querySelectorAll('button')).find(x => (x.getAttribute('aria-label')||'').startsWith('返回'))
  return b ? { label: (b.textContent||'').trim(), aria: b.getAttribute('aria-label') } : null
}"""

fails = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg, flush=True)
    if not cond:
        fails.append(msg)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    pg.set_default_timeout(25000)
    try:
        pg.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("form button[type=submit]", timeout=25000)
        pg.fill("input[type=text]", "admin")
        pg.fill("input[type=password]", PW)
        pg.locator("form button[type=submit]").first.click()
        pg.wait_for_timeout(2500)

        pg.goto(f"{BASE}/opportunities", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(12000)
        clicked = pg.evaluate("""() => {
          const btns = Array.from(document.querySelectorAll('button,a')).filter(x => (x.textContent||'').trim() === '洞察')
          if (!btns.length) return null
          btns[0].click(); return '洞察'
        }""")
        pg.wait_for_timeout(6000)
        url = pg.url.replace(BASE, "")
        check(bool(clicked) and "/stocks/" in url, f"机会页点「洞察」进入行情页 (URL={url})")
        back = pg.evaluate(JS_BACK)
        check(bool(back), f"行情页出现返回按钮 ({back})")
        check(bool(back) and "返回机会" in back["label"], f"按钮文案带来源页 ({back and back['label']})")
        if back:
            pg.evaluate("""() => { const b = Array.from(document.querySelectorAll('button')).find(x => (x.getAttribute('aria-label')||'').startsWith('返回')); if (b) b.click() }""")
            pg.wait_for_timeout(3500)
            check("/opportunities" in pg.url, f"点击返回回到机会页 (URL={pg.url.replace(BASE, '')})")

        # ② 无来路 → 不渲染
        pg2 = b.new_page(viewport={"width": 1400, "height": 900})
        pg2.set_default_timeout(25000)
        pg2.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
        try:
            pg2.wait_for_selector("form button[type=submit]", timeout=10000)
            pg2.fill("input[type=text]", "admin")
            pg2.fill("input[type=password]", PW)
            pg2.locator("form button[type=submit]").first.click()
            pg2.wait_for_timeout(2500)
        except Exception:
            pass
        pg2.goto(f"{BASE}/stocks/002361", wait_until="domcontentloaded", timeout=60000)
        pg2.wait_for_timeout(7000)
        nb = pg2.evaluate(JS_BACK)
        check(nb is None, f"直接打开行情页不渲染返回按钮 (got={nb})")
    finally:
        b.close()

print("RESULT=" + ("PASS" if not fails else "FAIL"), flush=True)
sys.exit(0 if not fails else 1)
