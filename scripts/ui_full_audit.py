#!/usr/bin/env python3
"""SIDA-Pro 全界面走查探针（2026-10-18 UI 审计配套，可复跑）。

用途：登录态逐页 / 逐 Tab 截图 + 导出 DOM 文本 + 统计 `--` 占位与版面指标，
并与同目录 JSON 报告一起，作为「字段缺失 / 页面高度 / 描边密度」三类审计的证据。

用法（密码只从环境读，不落盘）：
    SIDA_SHOT_PW=<admin 密码> python3 scripts/ui_full_audit.py
    SIDA_SHOT_PW=... python3 scripts/ui_full_audit.py --pages /,/theme-mood,/stocks/002361?tab=l2
    SIDA_TOKEN=<jwt> python3 scripts/ui_full_audit.py        # 已有 token 时免登录

产物：
    docs/research/shots_ui_audit/<slug>.png|.txt
    docs/research/shots_ui_audit/audit.json

判据（与本项目审计报告 §10 对应）：
    placeholders  —— 页面内 `--`/`—`/`NaN` 等占位文本出现次数（按上下文去重）
    stats.scrollHeight —— 全页高度（>4000 视为需要分页/折叠，R15）
    stats.bordered —— 有可见边框的元素数（>400 视为 hairline 过密，R14 辅助）
    http_errors    —— 4xx/5xx（用于区分「后端没数据」与「前端没读」）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

BASE = os.getenv("SIDA_BASE_URL", "https://www.sida.hengsheng-elec.com").rstrip("/")
OUT = Path(__file__).resolve().parent.parent / "docs" / "research" / "shots_ui_audit"

DEFAULT_PAGES = [
    "/",
    "/theme-mood",
    "/heatmap",
    "/opportunities",
    "/auction",
    "/ai-copilot",
    "/stocks/002361?tab=l2",
    "/stocks/002361?tab=suggest",
    "/stocks/002361?tab=fundamental",
    "/stocks/002361?tab=news",
    "/stocks/002361?tab=research",
    "/stocks/002361?tab=forecast",
    "/index/000001",
    "/dark-fund-top",
    "/war-report",
    "/decision-ledger",
    "/caliber-compare",
    "/portfolio",
]

SCAN_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const out = { placeholders: [], stats: {}, text: '', tabs: [], loading: [] };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  let n;
  while ((n = walker.nextNode())) {
    const t = norm(n.nodeValue);
    if (!t) continue;
    const el = n.parentElement;
    if (!el) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    let kind = null;
    if (t.length <= 12 && ['--', '—', '-', 'N/A', '暂无', '暂无数据', '未获取', 'undefined', 'null'].includes(t)) kind = 'exact';
    else if (/^--+$/.test(t) || /\bNaN\b/.test(t) || /undefined/.test(t)) kind = 'bad';
    if (kind) {
      let ctx = '';
      let p = el;
      for (let i = 0; i < 5 && p; i++) {
        const txt = norm(p.innerText || '');
        if (txt && txt.length > t.length) { ctx = txt.slice(0, 140); break; }
        p = p.parentElement;
      }
      const key = ctx + '|' + t;
      if (!seen.has(key)) { seen.add(key); out.placeholders.push({ text: t, kind, ctx }); }
    }
    if (/加载中|计算中|读取中|Loading|请稍候/.test(t) && t.length < 60) out.loading.push(t);
  }
  const all = Array.from(document.querySelectorAll('body *'));
  let bordered = 0, tinyFont = 0, shadows = 0;
  for (const el of all) {
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (parseFloat(st.fontSize) <= 10.5) tinyFont++;
    const bs = parseFloat(st.borderTopWidth) + parseFloat(st.borderBottomWidth)
             + parseFloat(st.borderLeftWidth) + parseFloat(st.borderRightWidth);
    if (bs > 0 && st.borderTopColor !== 'rgba(0, 0, 0, 0)') bordered++;
    if (st.boxShadow && st.boxShadow !== 'none') shadows++;
  }
  out.stats = {
    scrollHeight: document.documentElement.scrollHeight,
    hOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    bordered, tinyFont, shadows,
    tableCount: document.querySelectorAll('table').length,
    canvasCount: document.querySelectorAll('canvas').length,
  };
  out.text = document.body.innerText;
  out.tabs = Array.from(document.querySelectorAll('[role=tab]')).map(e => norm(e.innerText));
  return out;
}
"""


def login() -> tuple[str, str]:
    """密码只从环境读；返回 (token, expires_at)。"""
    token = os.getenv("SIDA_TOKEN", "").strip()
    if token:
        return token, ""
    pw = os.getenv("SIDA_SHOT_PW", "")
    if not pw:
        raise SystemExit("缺少 SIDA_SHOT_PW（只从环境读，不落盘）或 SIDA_TOKEN")
    body = json.dumps({"username": os.getenv("AUTH_USERNAME", "admin"), "password": pw}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)["data"]
    return data["token"], data["expires_at"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default="", help="逗号分隔路径；缺省跑 DEFAULT_PAGES")
    ap.add_argument("--wait", type=int, default=14000, help="每页沉降毫秒（重页面建议 20000+）")
    args = ap.parse_args()

    pages = [p.strip() for p in args.pages.split(",") if p.strip()] or DEFAULT_PAGES
    token, expires = login()

    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width": 1680, "height": 1050},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.new_page()
        net: list = []
        cons: list = []
        page.on("response", lambda r: net.append({"s": r.status, "u": r.url}) if r.status >= 400 else None)
        page.on("console", lambda m: cons.append(f"{m.type}: {m.text[:200]}") if m.type == "error" else None)

        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
        page.evaluate(
            "([t, e]) => { localStorage.setItem('token', t); if (e) localStorage.setItem('token_expires', e); }",
            [token, expires],
        )

        for i, path in enumerate(pages, 1):
            name = f"{i:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', path).strip('_') or 'root'}"
            net.clear()
            cons.clear()
            try:
                page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(args.wait)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                data = page.evaluate(SCAN_JS)
                (OUT / f"{name}.txt").write_text(data["text"], encoding="utf-8")
                page.screenshot(path=str(OUT / f"{name}.png"), full_page=True, timeout=40000)
                codes: dict = {}
                for r in net:
                    k = f'{r["s"]} {r["u"].replace(BASE, "")[:120]}'
                    codes[k] = codes.get(k, 0) + 1
                results[path] = {
                    "name": name,
                    "stats": data["stats"],
                    "placeholders": data["placeholders"],
                    "loading": data["loading"],
                    "tabs": data["tabs"],
                    "http_errors": sorted(codes.items(), key=lambda kv: -kv[1])[:25],
                    "console_errors": cons[:20],
                }
                print(
                    f"[ok] {path} h={data['stats']['scrollHeight']} "
                    f"ph={len(data['placeholders'])} err={len(codes)}",
                    flush=True,
                )
            except Exception as e:  # noqa: BLE001 — 单页失败不阻断全量走查
                results[path] = {"error": str(e)[:300]}
                print(f"[ERR] {path}: {e}", flush=True)

        browser.close()

    (OUT / "audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v.get("placeholders", [])) for v in results.values())
    print(f"\n{len(results)} 页 / `--` 合计 {total} / 报告 {OUT / 'audit.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
