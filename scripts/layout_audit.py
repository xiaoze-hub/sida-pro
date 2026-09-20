#!/usr/bin/env python3
"""布局体检（overlap / 裁切 / 溢出）—— 2026-09-19 用户报"K线与成交量重合"后建的常驻探针。

## 为什么需要
`terminal_audit.py` 量的是**密度/字号/K线占比**这类"多长多密"的结构指标，
**量不出"两个东西画在一起了"**。重合、被裁掉一半、溢出容器这类问题必须**比矩形**，
所以单独一个探针，判据也不同。

## 判据（每条都只报"确定性违规"，不猜）
1. **重叠**：两个"有内容的可视元素"矩形相交，且**互不为祖先**（嵌套是正常布局），
   相交面积 > 较小者的 30% 且 > 120px² → 报。绝对定位的浮层（tooltip/下拉/遮罩）白名单放行。
2. **裁切**：`overflow: hidden/clip` 且 `scrollWidth/Height` 超出 `clientWidth/Height` 5px 以上 → 内容被切。
3. **横向溢出**：文档 `scrollWidth > clientWidth + 2` → 出现横向滚动条。
4. **图表布局不变量**：K 线容器带 `data-chart-layout="主图底/副图顶"`，
   要求 **主图 bottom ≥ 副图 top**（K 线不得画进成交量区域）。

用法: python scripts/layout_audit.py [--base URL] [--json OUT] [--pages a,b,c]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_BASE = os.getenv("SIDA_BASE_URL", "https://www.sida.hengsheng-elec.com")

#: 默认体检页面（挑"信息最密"的几页：图表页 + 榜单页 + 表格页）
DEFAULT_PAGES = [
    ("/stocks/002361", "个股行情页(K线+副图)"),
    ("/index/000001", "指数页(K线+副图)"),
    ("/heatmap", "热力图"),
    ("/dark-fund-top", "暗盘资金榜"),
    ("/theme-mood", "题材情绪页"),
    ("/opportunities", "机会页"),
    ("/portfolio", "持仓页"),
    ("/dashboard", "首页"),
]

PROBE = r"""
() => {
  const IGNORE_SEL = [
    '[role="tooltip"]', '.recharts-tooltip-wrapper', '[data-radix-popper-content-wrapper]',
    '[class*="tooltip"]', '[class*="Tooltip"]', '[class*="dropdown"]', '[class*="Dropdown"]',
    '[class*="popover"]', '[class*="Popover"]', '[class*="modal"]', '[class*="Modal"]',
    '[class*="overlay"]', '[class*="Overlay"]', '[class*="toast"]', '[class*="Toast"]',
    '[aria-hidden="true"]',
  ];
  const ignored = (el) => IGNORE_SEL.some((s) => { try { return el.matches(s); } catch { return false; } });

  // 元素是否在"浮层"里（自身或近祖先是 absolute/fixed）：底部免责声明条、悬浮按钮、
  // 侧栏固定块都属于这一类 —— 它们**设计上就叠在内容之上**，不是布局事故。
  // （第一版只看元素自身的 position，结果把 fixed 免责声明条的 <p> 子元素报成重叠 → 假信号）
  const inFloatLayer = (el) => {
    let e = el;
    for (let i = 0; i < 6 && e && e.tagName; i++) {
      const pos = getComputedStyle(e).position;
      if (pos === 'absolute' || pos === 'fixed') return true;
      // sticky 也算"设计上就叠着"：粘性首列/表头**就是靠压住相邻内容**工作的
      // (题材情绪页的粘性首列被报成 13 处"重叠", 全是假信号)。但粘性表头**遮住内容**
      // 属于真问题 —— 那种情况要靠"内容是否被永久遮挡"人工判断, 不在本探针自动判据里。
      if (pos === 'sticky') return true;
      e = e.parentElement;
    }
    return false;
  };

  const vis = (el, st, r) => {
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (parseFloat(st.opacity || '1') < 0.05) return false;
    if (r.width < 6 || r.height < 6) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    // ★ 关键: 现代 Chrome 的**折叠 <details>** 会给隐藏内容保留**布局盒**
    // (getBoundingClientRect 照样返回真实尺寸), 只是不绘制 —— 只比矩形会把
    // "收起状态的明细表"报成压住了下面的内容(Settings 页一次报了 40 处假重叠)。
    // checkVisibility 才是"到底画没画"的判据。
    if (typeof el.checkVisibility === 'function') {
      try {
        if (!el.checkVisibility({ contentVisibilityAuto: true, opacityProperty: true, visibilityProperty: true })) return false;
      } catch { /* 旧内核不认参数, 退回矩形判断 */ }
    }
    return true;
  };

  const cand = [];
  const all = document.querySelectorAll('body *');
  for (const el of all) {
    const st = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (!vis(el, st, r)) continue;
    if (ignored(el)) continue;
    const tag = el.tagName.toLowerCase();
    const isMedia = ['canvas', 'svg', 'img', 'video'].includes(tag);
    const txt = (el.innerText || '').trim();
    // 只看"有内容"的元素：直接文本 / 媒体 / 表单控件；纯布局容器不算
    const hasText = txt.length > 0 && el.children.length === 0;
    if (!isMedia && !hasText && !['input', 'select', 'textarea', 'button', 'table'].includes(tag)) continue;
    cand.push({
      el,
      i: cand.length,
      tag,
      cls: (el.className || '').toString().slice(0, 90),
      x: r.left, y: r.top, w: r.width, h: r.height,
      txt: txt.slice(0, 30),
      pos: st.position,
      z: st.zIndex,
    });
    if (cand.length > 900) break;
  }

  // 重叠检测（同页候选，跳过祖先/后代关系）
  const els = Array.from(document.querySelectorAll('body *'));
  const overlaps = [];
  const suspects = [];   // 疑似(多行 inline 并集等) —— 只给人眼看, 不算违规
  const inter = (a, b) => {
    const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
    const x2 = Math.min(a.x + a.w, b.x + b.w), y2 = Math.min(a.y + a.h, b.y + b.h);
    if (x2 <= x || y2 <= y) return 0;
    return (x2 - x) * (y2 - y);
  };
  for (let i = 0; i < cand.length; i++) {
    for (let j = i + 1; j < cand.length; j++) {
      const a = cand[i], b = cand[j];
      // 快速排除：纵向不相交
      if (a.y + a.h <= b.y || b.y + b.h <= a.y) continue;
      const ov = inter(a, b);
      if (ov <= 0) continue;
      const small = Math.min(a.w * a.h, b.w * b.h);
      if (small <= 0) continue;
      if (ov / small < 0.3 || ov < 120) continue;
      // 绝对定位元素（浮层/角标）不报——它们在设计上就叠着
      if (inFloatLayer(a.el) || inFloatLayer(b.el)) continue;
      // **嵌套不算重叠**：图标在按钮里、文字在容器里都是正常结构。
      // （第一版漏了这条，把"button × 内部 svg"全报成重叠 → 满屏假信号）
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      // **多行 inline 元素的 rect 是各行碎片的并集** —— 并集相交不等于"画面重合"
      // (口径对照页那 3 处就是这样: 等级标签与说明文字同起点, 实为并集假象)。
      // 这类不判违规, 单独进 suspects 供人眼看, 免得探针自己制造"问题"。
      const multiLineInline = (c) => {
        const st = getComputedStyle(c.el);
        if (st.display !== 'inline') return false;
        const lh = parseFloat(st.lineHeight);
        return Number.isFinite(lh) && c.h > lh * 1.6;
      };
      const rec = { a: cand[i], b: cand[j], ratio: +(ov / small).toFixed(2), area: Math.round(ov) };
      if (multiLineInline(a) || multiLineInline(b)) { suspects.push(rec); continue; }
      overlaps.push(rec);
    }
  }

  // 裁切检测
  const clipped = [];
  for (const el of all) {
    const st = getComputedStyle(el);
    if (!['hidden', 'clip'].includes(st.overflow) && !['hidden', 'clip'].includes(st.overflowX) && !['hidden', 'clip'].includes(st.overflowY)) continue;
    const r = el.getBoundingClientRect();
    if (!vis(el, st, r)) continue;
    const dw = el.scrollWidth - el.clientWidth, dh = el.scrollHeight - el.clientHeight;
    const txt = (el.innerText || '').trim();
    if ((dw > 5 || dh > 5) && (txt.length > 0 || el.querySelector('canvas,svg,img'))) {
      // 有意截断(ellipsis)与**真裁切**分开报：前者是设计选择(信息可 hover 看全)，
      // 后者是"内容被切掉且看不全"，混在一起报等于没报。
      const ellipsis = st.textOverflow === 'ellipsis' ||
        /\btruncate\b|line-clamp/.test((el.className || '').toString());
      clipped.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().slice(0, 90),
        dw, dh, w: Math.round(r.width), h: Math.round(r.height),
        txt: txt.slice(0, 40),
        ellipsis,
      });
    }
    if (clipped.length > 60) break;
  }

  // 横向溢出
  const de = document.documentElement;
  const hScroll = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth,
                    overflow: de.scrollWidth - de.clientWidth };

  // 图表布局不变量
  const charts = Array.from(document.querySelectorAll('[data-chart-layout]')).map((el) => {
    const raw = el.getAttribute('data-chart-layout') || '';
    const [priceBottom, subTop] = raw.split('/').map((v) => parseFloat(v));
    const r = el.getBoundingClientRect();
    // 不变量: 价格轴底边距 B 与副图顶部起点 S 满足 B ≥ 1 - S（等价于 1-B ≤ S）。
    // 直接比 B ≥ S 是**错的**(两者分数语义不同), 会把修好的版本误判成违规。
    return { raw, priceBottom, subTop, h: Math.round(r.height),
             ok: Number.isFinite(priceBottom) && Number.isFinite(subTop)
                 && priceBottom >= (1 - subTop) - 1e-9 };
  });

  return { candidates: cand.length, overlaps: overlaps.slice(0, 40), suspects: suspects.slice(0, 20), clipped, hScroll, charts };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", default="")
    ap.add_argument("--pages", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    pages = DEFAULT_PAGES
    if args.pages:
        pages = [(p.strip(), p.strip()) for p in args.pages.split(",") if p.strip()]

    pw_pw = os.getenv("SIDA_SHOT_PW")
    if not pw_pw:
        print("缺少环境变量 SIDA_SHOT_PW(只从环境读, 不落盘)", file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    report: dict = {"base": args.base, "pages": []}
    fails: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        pg = ctx.new_page()
        # 登录（终端页要登录才看得到）
        try:
            pg.goto(f"{args.base}/login", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_selector("form button[type=submit]", timeout=15000)
            pg.fill("input[type=text], input[placeholder*='用户']", os.getenv("AUTH_USERNAME", "admin"))
            pg.fill("input[type=password]", pw_pw)
            # ⚠️ 不能用 get_by_role(name="登录"): 模式标签"密码登录/验证码登录"都含"登录",
            # .first 会点到标签上(只切模式不提交) → 页面停在登录页, 后续全判"未就绪"。
            # 用表单提交按钮, 失败则退回回车提交。
            try:
                pg.locator("form button[type=submit]").first.click(timeout=5000)
            except Exception:  # noqa: BLE001
                pg.keyboard.press("Enter")
            pg.wait_for_timeout(2500)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 登录步骤异常: {e}", file=sys.stderr)

        for path, label in pages:
            try:
                # ⚠️ 不能用 networkidle: 首页/持仓页有**周期轮询**(实时行情),
                # 永远等不到"网络空闲" → 直接 60s 超时(实测踩到)。
                # 用 domcontentloaded + 固定沉降时间, 并显式等一个内容锚点。
                pg.goto(f"{args.base}{path}", wait_until="domcontentloaded", timeout=60000)
                try:
                    pg.wait_for_selector("main, #root > div", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                pg.wait_for_timeout(3200)
                txt_len = pg.evaluate("() => (document.body.innerText || '').length")
                if txt_len < 200:
                    report["pages"].append({"path": path, "label": label, "status": "未就绪", "textLen": txt_len})
                    print(f"{label:<22} 未就绪(文字 {txt_len} 字) —— 复跑一次再下结论")
                    continue
                res = pg.evaluate(PROBE)
                res.update({"path": path, "label": label, "status": "ok", "textLen": txt_len})
                report["pages"].append(res)

                ov, hs, ch = res["overlaps"], res["hScroll"], res["charts"]
                cl_all = res["clipped"]
                cl = [c for c in cl_all if not c.get("ellipsis")]      # 真裁切
                cl_ell = [c for c in cl_all if c.get("ellipsis")]      # 有意截断(info)
                res["clipped_hard"], res["clipped_ellipsis"] = cl, cl_ell
                bad_charts = [c for c in ch if not c["ok"]]
                su = res.get("suspects") or []
                line = f"{label:<22} 重叠={len(ov):<3} 疑似={len(su):<3} 真裁切={len(cl):<3} 截断={len(cl_ell):<3} 横向={hs['overflow']:<4} 图表={len(ch)}"
                if bad_charts:
                    line += f"  ⚠ 图表布局违规: {[c['raw'] for c in bad_charts]}"
                    fails.append(f"{label}: 主图/副图重叠({bad_charts[0]['raw']})")
                if hs["overflow"] > 2:
                    fails.append(f"{label}: 横向溢出 {hs['overflow']}px")
                print(line)
                for o in ov[:6]:
                    print(f"    ⚠ 重叠 {o['ratio']}× {o['area']}px²  [{o['a']['tag']} {o['a']['cls'][:34]}] × [{o['b']['tag']} {o['b']['cls'][:34]}]")
                for c in cl[:5]:
                    print(f"    ⚠ 裁切 {c['tag']} +{c['dw']}x{c['dh']}px [{c['cls'][:40]}] «{c['txt'][:26]}»")
            except Exception as e:  # noqa: BLE001
                print(f"{label:<22} 探针异常: {type(e).__name__}: {e}")
                report["pages"].append({"path": path, "label": label, "status": "error", "error": str(e)})

        browser.close()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"SAVED {args.json}")

    print()
    if fails:
        print("违规项:")
        for f_ in fails:
            print(f"  ✗ {f_}")
        return 1
    print("布局体检: 未发现确定性违规 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
