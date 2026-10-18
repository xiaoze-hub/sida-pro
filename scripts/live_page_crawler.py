import os
import sys
import json
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "https://SIDA.hengsheng-elec.com"
SHOT_DIR = Path("/home/ubuntu/sida-pro/docs/research/shots_live_audit")
SHOT_DIR.mkdir(parents=True, exist_ok=True)

# 1. 登录拿 token
login_req = urllib.request.Request(
    f"{BASE_URL}/api/auth/login",
    data=json.dumps({"username": "admin", "password": "xz.170530"}).encode(),
    headers={"Content-Type": "application/json"}
)
with urllib.request.urlopen(login_req, timeout=15) as resp:
    res = json.loads(resp.read().decode())
    token = res["data"]["token"]

PAGES = [
    ("/", "01_home"),
    ("/stocks", "02_stocks_portfolio"),
    ("/stocks/002361?tab=l2", "03_workbench_l2"),
    ("/stocks/002361?tab=suggest", "04_workbench_suggest"),
    ("/stocks/002361?tab=fundamentals", "05_workbench_fundamentals"),
    ("/stocks/002361?tab=research", "06_workbench_research"),
    ("/stocks/002361?tab=news", "07_workbench_news"),
    ("/auction", "08_auction"),
    ("/theme-mood", "09_theme_mood"),
    ("/reports", "10_reports"),
    ("/ai-copilot", "11_ai_copilot"),
    ("/shadow-account", "12_shadow_account"),
    ("/decision-log", "13_decision_log"),
    ("/profile", "14_profile"),
    ("/admin", "15_admin"),
    ("/cb-radar", "16_cb_radar"),
]

audit_results = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1600, "height": 900},
        ignore_https_errors=True
    )
    # 注入 token 到 localStorage 与 cookie
    page = context.new_page()
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30000)
    page.evaluate(f"""() => {{
        localStorage.setItem('auth_token', '{token}');
        localStorage.setItem('sida_token', '{token}');
        localStorage.setItem('token', '{token}');
    }}""")

    for path, name in PAGES:
        url = f"{BASE_URL}{path}"
        console_errors = []
        network_errors = []
        
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type in ("error", "warning") else None)
        page.on("response", lambda r: network_errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        
        print(f"正在深度巡检: {path} ...")
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            page.wait_for_timeout(4000)
            
        page.wait_for_timeout(3000)
        
        # 页面可见 text 内容检查是否有 429、不可用、失败、错误
        body_text = page.inner_text("body")
        suspicious = []
        for kw in ["429", "不可用", "失败", "未返回", "错误", "Exception", "Error"]:
            if kw in body_text:
                suspicious.append(kw)

        shot_path = SHOT_DIR / f"{name}.png"
        page.screenshot(path=str(shot_path), full_page=False)
        
        audit_results.append({
            "path": path,
            "name": name,
            "shot": str(shot_path),
            "suspicious_keywords": suspicious,
            "network_errors": network_errors[:5],
            "console_errors": console_errors[:5]
        })

    browser.close()

with open(SHOT_DIR / "audit_report.json", "w", encoding="utf-8") as f:
    json.dump(audit_results, f, ensure_ascii=False, indent=2)

print("AUDIT_COMPLETE")
