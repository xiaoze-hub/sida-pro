"""终端度巡检 + 公开面巡检 (设计稿 v3.0 §九 验收线的可执行版)。

产品铁律里只有一条是带数字的 —— **"K 线是绝对主角(≥80% 屏宽)"** —— 本脚本把铁律连同
"不堆数据/字阶纪律/不卡片化"一起变成 DOM 指标, 让"设计有没有达标"可以复测而不是靠印象。

八个指标(全部只读 DOM, 不碰任何数据):
  klineShare  最宽 canvas / 视口宽          —— K 线主角度(目标 ≥0.80)
  cards       圆角≥8+边框/阴影, >200x80 的块 —— 卡片化(终端页目标 0)
  hairline    1px solid 边框元素数           —— 分层是否用 hairline(表格墙指标, ≤80)
  fontCount   可见文本用到的字号种类          —— 字阶纪律(目标 ≤6: 10/11/12/13/16/20)
  dash        页面里 "--"(如实标缺数)        —— 平铺缺数(个股页目标 ≤12, 其余聚合)
  density     可见字符 / 每千像素高度         —— 目标 1500~3500(低于=空白浪费, 高于=堆砌)
  overflowX   横向溢出                       —— 必须 false
  zeroish     "0.00" 形式出现次数             —— **只提示不判定**: 可能是真 0, 也可能是
                                                "用 0 冒充无数据"(诚实口径红线), 需人工核对

**指标分三类(2026-09-18 定稿)**: ① **结构指标**(随时可比): klineShare / hairline / cards / fontCount;
② **结构判据**(布尔, 比统计型稳): 首页是否含"结论行"; ③ **数据/状态指标**(须固定时点 + 只对表格页判):
dash / zeroish / densityNoChart(**剔除图表占高**, 且不判图表页与混合看板)。

**测量基准(2026-09-18 实测教训)**: `dash`(缺数)与 `zeroish`(0.00 类)是**数据状态依赖**的 ——
同一天 18:50 测个股页 `dash=46`(部分数据源未补齐), 19:30 复测 **0**。所以:
  · 这两项**必须在固定时点比**(建议盘后 16:00 数据回流之后), 否则前后两次数字会互相矛盾;
  · 比数字时把"数据状态"一起说(是否有源不可用), 不要当成"代码改坏了/改好了"。
`klineShare` / `hairline` / `cards` / `fontCount` 是**结构指标**, 与数据无关, 可以随时比。

用法:
  SIDA_SHOT_PW=*** python scripts/terminal_audit.py      # 默认打生产 + 全部页面(必须给密码)
  python scripts/terminal_audit.py --base http://localhost:8000
  python scripts/terminal_audit.py --json /tmp/audit.json
  SIDA_SHOT_PW=*** python scripts/terminal_audit.py      # 登录密码只从环境变量读(仓库不留凭据)

退出码: 0 = 全部达标; 1 = 有指标越线(打印越线明细)。**发版前跑一次, 数据回填到设计稿**。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 目标阈值(设计稿 v3.0 §九)。None = 该页不适用。
TARGETS: dict[str, dict[str, object]] = {
    "klineShare": {"min": 0.80, "pages": {"个股页", "指数页"}},
    # 卡片化只判"图表为主的行情终端页"(设计稿 v3.0 §五 范式 A); 列表页/工作台页允许**一个**汇总面板,
    # 否则会把"页头汇总条"也算成卡片墙 —— 这是口径, 不是放宽: 个股/指数页仍是 0。
    "cards": {"max": 0, "pages": {"个股页", "指数页", "热力图"}},
    "hairline": {"max": 80, "pages": None},
    "fontCount": {"max": 6, "pages": None},
    "dash": {"max": 12, "pages": {"个股页"}},
    # 密度**只报告、不做目标**(2026-09-18 定稿): 它随数据状态剧烈变化 —— 同一天首页 765/1183/1344,
    # 空态页(持仓/机会)文字少自然也低。真正的问题是"表格墙"(题材页 9680) → 修完 1882 ✅。
    # 拿一个"状态依赖"的数字当全站硬指标, 只会制造噪声(这条已写进设计稿方法论)。
    # 首页改用**结构性判据**: 首屏必须有"结论行"(统计型密度对看板不稳, 结构型可判定)
    "conclusionRow": {"must": True, "pages": {"首页"}},
}

# (标签, 路由, 稳定等待秒)
ROUTES: list[tuple[str, str, int]] = [
    ("首页", "/", 5),
    ("个股页", "/stocks/002361", 9),
    ("指数页", "/stocks/000001?type=index", 6),
    ("机会页", "/opportunities", 7),
    ("暗盘榜", "/dark-fund-top", 7),
    ("题材页", "/theme-mood", 7),
    ("热力图", "/heatmap", 7),
    ("持仓页", "/portfolio", 6),
]

# 公开面(匿名可达): 设计稿 v3.0 §三。营销页允许比终端宽的字阶, 上限 7 档。
ROUTES_ANON: list[tuple[str, str, int]] = [
    ("落地页", "/", 6),
    ("登录页", "/login", 4),
    ("注册页", "/login?mode=register", 5),
    ("开发者文档", "/developers", 6),
]

TARGETS_ANON: dict[str, dict[str, object]] = {
    "fontCount": {"max": 7, "pages": None},
    "cdnRefs": {"max": 0, "pages": None},
}

PROBE_ANON_JS = r"""() => {
  const vis = e => e.offsetParent !== null && e.getBoundingClientRect().width > 0;
  const sizes = {};
  for (const e of [...document.querySelectorAll('body *')].filter(vis)) {
    const hasText = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (hasText) { const fs = Math.round(parseFloat(getComputedStyle(e).fontSize)); sizes[fs] = (sizes[fs] || 0) + 1; }
  }
  // 只找**真依赖**(script/link 指向外部域名); 注释里的字样不算, 避免像上一版那样误报。
  let cdnRefs = 0;
  document.querySelectorAll('script[src], link[href], img[src]').forEach(el => {
    const u = el.getAttribute('src') || el.getAttribute('href') || '';
    if (/^(https?:)?\/\//.test(u) && !u.includes(location.host)) cdnRefs++;
  });
  // G/S 颜色验收(2026-09-19): canvas 画不进 DOM, KlineChart 把**解析后的颜色**挂在容器上,
  // 这里读出来在 Python 侧判色相 —— 生产上真拦住"token 被改成绿/红互换"。
  const gsEl = document.querySelector('[data-gs-go]');
  const txt = document.body.innerText || '';
  return {
    fontSizes: Object.keys(sizes).map(Number).sort((a, b) => a - b),
    fontCount: Object.keys(sizes).length,
    cdnRefs,
    docH: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight),
    textLen: txt.length,
    hasPricing: /档位|定价|免费|价格/.test(txt),
    ctaCount: [...document.querySelectorAll('a,button')].filter(vis)
        .filter(e => /注册|开始|快速体验|免费/.test(e.innerText || '')).length,
  };
}"""

PROBE_JS = r"""() => {
  const vis = e => e.offsetParent !== null && e.getBoundingClientRect().width > 0;
  const vw = window.innerWidth;
  let maxCanvas = 0;
  document.querySelectorAll('canvas').forEach(c => {
    const w = c.getBoundingClientRect().width; if (w > maxCanvas) maxCanvas = w;
  });
  let cards = 0, hairline = 0;
  const sizes = {};
  const all = [...document.querySelectorAll('body *')].filter(vis);
  for (const e of all) {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    const rad = parseFloat(cs.borderTopLeftRadius || '0');
    const bw = parseFloat(cs.borderTopWidth || '0');
    const shadow = cs.boxShadow && cs.boxShadow !== 'none';
    if (rad >= 8 && (bw > 0 || shadow) && r.width > 200 && r.height > 80) cards++;
    if (bw > 0 && bw <= 1.2 && cs.borderTopStyle === 'solid') hairline++;
    const hasText = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (hasText) { const fs = Math.round(parseFloat(cs.fontSize)); sizes[fs] = (sizes[fs] || 0) + 1; }
  }
  const docH = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  // 2026-09-18: 图表占高用"区间并集"量 —— 直接累加会把卡片里的多个小 canvas(迷你走势图)
  // 重复计入, 把整页高度算穿(实测 首页 出现 canvasH > docH)。并集才是"图表真正占掉的高度"。
  // 用 **canvas 自身的盒子**量图表占高(最无歧义): 向上找祖先会把"图表+其他内容"的大容器
  // 也算进去(实测把整页算成图表)。canvas 自己就是图表本体, 容器只多一层内边距。
  const spans = [];
  document.querySelectorAll('canvas').forEach(c => {
    const r = c.getBoundingClientRect();
    if (r.height > 8 && r.width > 8) spans.push([r.top + window.scrollY, r.bottom + window.scrollY]);
  });
  spans.sort((x, y) => x[0] - y[0]);
  let canvasH = 0, curS = -1, curE = -1;
  for (const [st, en] of spans) {
    if (st > curE) { if (curE > curS) canvasH += curE - curS; curS = st; curE = en; }
    else curE = Math.max(curE, en);
  }
  if (curE > curS) canvasH += curE - curS;
  canvasH = Math.min(canvasH, docH * 0.9);   // 兜底: 不允许超过页高 90%
  const txt = document.body.innerText || '';
  return {
    fontSizes: Object.keys(sizes).map(Number).sort((a, b) => a - b),
    fontCount: Object.keys(sizes).length,
    cdnRefs,
    docH: Math.max(document.documentElement.scrollHeight, document.body.scrollHeight),
    textLen: txt.length,
    hasPricing: /档位|定价|免费|价格/.test(txt),
    ctaCount: [...document.querySelectorAll('a,button')].filter(vis)
        .filter(e => /注册|开始|快速体验|免费/.test(e.innerText || '')).length,
  };
}"""

PROBE_JS = r"""() => {
  const vis = e => e.offsetParent !== null && e.getBoundingClientRect().width > 0;
  const vw = window.innerWidth;
  let maxCanvas = 0;
  document.querySelectorAll('canvas').forEach(c => {
    const w = c.getBoundingClientRect().width; if (w > maxCanvas) maxCanvas = w;
  });
  let cards = 0, hairline = 0;
  const sizes = {};
  const all = [...document.querySelectorAll('body *')].filter(vis);
  for (const e of all) {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    const rad = parseFloat(cs.borderTopLeftRadius || '0');
    const bw = parseFloat(cs.borderTopWidth || '0');
    const shadow = cs.boxShadow && cs.boxShadow !== 'none';
    if (rad >= 8 && (bw > 0 || shadow) && r.width > 200 && r.height > 80) cards++;
    if (bw > 0 && bw <= 1.2 && cs.borderTopStyle === 'solid') hairline++;
    const hasText = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (hasText) { const fs = Math.round(parseFloat(cs.fontSize)); sizes[fs] = (sizes[fs] || 0) + 1; }
  }
  const docH = Math.max(document.documentElement.scrollHeight, document.body.scrollHeight);
  // 2026-09-18: 图表画布高度单独量出来 —— 图表占高而文字少, 不剔除它 "密度" 会系统性冤枉图表页。
  // 用 **canvas 自身盒子** + **区间并集**量图表占高:
  //  · 向上找祖先会把"图表+其他内容"的大容器也算进去(实测把整页算成图表);
  //  · 直接累加会把 lightweight-charts 的多个重叠 canvas(主图/价格轴/量副图)重复计入。
  const spans = [];
  document.querySelectorAll('canvas').forEach(c => {
    const r = c.getBoundingClientRect();
    if (r.height > 8 && r.width > 8) spans.push([r.top + window.scrollY, r.bottom + window.scrollY]);
  });
  spans.sort((x, y) => x[0] - y[0]);
  let canvasH = 0, curS = -1, curE = -1;
  for (const [st, en] of spans) {
    if (st > curE) { if (curE > curS) canvasH += curE - curS; curS = st; curE = en; }
    else curE = Math.max(curE, en);
  }
  if (curE > curS) canvasH += curE - curS;
  canvasH = Math.min(canvasH, docH * 0.9);
  const txt = document.body.innerText || '';
  return {
    canvasH: Math.round(canvasH),
    // 结构性判据(比统计型指标稳): 首页首屏的"结论行"在不在
    conclusionRow: !!document.querySelector('[data-testid=\"market-conclusion\"]'),
    klineShare: maxCanvas ? +(maxCanvas / vw).toFixed(3) : 0,
    cards, hairline,
    fontSizes: Object.keys(sizes).map(Number).sort((a, b) => a - b),
    fontCount: Object.keys(sizes).length,
    dash: (txt.match(/--/g) || []).length,
    zeroish: (txt.match(/\b0\.0+\b/g) || []).length,
    textLen: txt.length,   // 页面是否"渲染上来了"的粗判据(见 judge 的未就绪短路)
    gsGo: gsEl ? gsEl.getAttribute('data-gs-go') : null,
    gsStop: gsEl ? gsEl.getAttribute('data-gs-stop') : null,
    docH,
    density: docH ? +(txt.length / (docH / 1000)).toFixed(0) : 0,
    densityNoChart: docH - Math.round(canvasH) > 200
      ? +(txt.length / ((docH - Math.round(canvasH)) / 1000)).toFixed(0) : 0,
    overflowX: document.documentElement.scrollWidth > vw + 2,
  };
}"""


def _login(base: str, user: str, pw: str) -> str:
    import urllib.request

    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"username": user, "password": pw}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    return (body.get("data") or {}).get("token") or ""


def _hue(color: str | None) -> float | None:
    """#rrggbb / #rgb → 色相(0-360); 解析不了返回 None(如实说"量不出", 不猜)。"""
    if not color:
        return None
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        return None
    try:
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return (h * 60) % 360


def gs_color_violation(go: str | None, stop: str | None) -> str | None:
    """G/S 颜色验收线: **G(机会)=红系, S(风险)=绿系**(A 股惯例)。

    注意判**色相**而不是字符串相等 —— 换个同色系的红/绿都算过, 只有"红绿互换/变成灰蓝"才判越线。
    量不到(元素不存在/颜色解析失败)返回 None 表示**不判**(该页没画 K 线是正常的)。
    """
    hgo, hstop = _hue(go), _hue(stop)
    if hgo is None or hstop is None:
        return None
    go_red = hgo <= 25 or hgo >= 330
    stop_green = 90 < hstop < 170
    if not go_red or not stop_green:
        return (f"GS 颜色不符(G 应红系 got {go}/hue {hgo:.0f}; "
                f"S 应绿系 got {stop}/hue {hstop:.0f})")
    return None


def judge(row: dict) -> list[str]:
    """按阈值判定单页越线项。只报**适用该页**的指标, 避免误伤。

    2026-09-19: 加"页面未就绪"短路 —— 文本量过小(默认 <200 字)说明数据还没渲染上来,
    此时判 `conclusionRow`/`cards`/字号 只会产出**假越线**(实测踩过: 同一版本两次跑结论相反)。
    未就绪一律不判, 由调用方打印"未就绪"提示, 避免拿"没加载完"当"页面改坏了"。
    """
    bad: list[str] = []
    label = row["name"]
    if int(row.get("textLen") or 0) < 200:
        return []
    viol = gs_color_violation(row.get("gsGo"), row.get("gsStop"))
    if viol:
        bad.append(viol)
    for key, spec in TARGETS.items():
        pages = spec.get("pages")
        if pages is not None and label not in pages:  # type: ignore[operator]
            continue
        if key == "klineShare" and row.get(key, 0) == 0:
            continue  # 该页没有 K 线(列表/工作台页), 不判
        v = row.get(key)
        if v is None:
            continue
        if spec.get("must") is True:
            if not v:
                bad.append(f"{key}=false(结构性判据未满足)")
            continue
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:  # type: ignore[operator]
            bad.append(f"{key}={v} < {lo}")
        if hi is not None and v > hi:  # type: ignore[operator]
            bad.append(f"{key}={v} > {hi}")
    if row.get("overflowX"):
        bad.append("overflowX=true")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://www.sida.hengsheng-elec.com")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口(调试用)")
    ap.add_argument("--anon", action="store_true",
                    help="公开面模式: 不带凭据访问落地页/登录/注册/文档(设计稿 v3.0 §三)")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    # 密码只从环境变量读 —— **仓库里不留任何凭据字面量**(tests/test_auth_no_default_password.py 门禁)。
    pw_env = os.environ.get("SIDA_SHOT_PW") or ""
    if not pw_env:
        print("需要 SIDA_SHOT_PW 环境变量(登录密码不入库): SIDA_SHOT_PW=*** python scripts/terminal_audit.py",
              file=sys.stderr)
        return 2
    rows: list[dict] = []
    if args.anon:  # 公开面: 一律不带凭据, 且要验"匿名到底能不能看到"
        anon_rows: list[dict] = []
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=not args.headed,
                                   args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            pg = b.new_context(viewport={"width": 1600, "height": 900}).new_page()
            for name, route, settle in ROUTES_ANON:
                rec: dict = {"name": name, "route": route}
                try:
                    pg.goto(args.base + route, timeout=60000, wait_until="domcontentloaded")
                    pg.wait_for_timeout(settle * 1000)
                    rec.update(pg.evaluate(PROBE_ANON_JS))
                    rec["landed"] = pg.url.replace(args.base, "")
                except Exception as exc:  # noqa: BLE001
                    rec["error"] = f"{type(exc).__name__}: {exc}"[:160]
                anon_rows.append(rec)
            b.close()
        print(f"{'页面':<10}{'匿名落点':<26}{'字号':>5}{'外部依赖':>9}{'页高':>7}  判定")
        print("-" * 92)
        bad_n = 0
        for r in anon_rows:
            if "error" in r:
                print(f"{r['name']:<10}{'探针失败':<26}{'—':>5}{'—':>9}{'—':>7}  {r['error']}")
                bad_n += 1
                continue
            viol: list[str] = []
            for k, spec in TARGETS_ANON.items():
                v = r.get(k)
                hi = spec.get("max")
                if v is not None and hi is not None and v > hi:  # type: ignore[operator]
                    viol.append(f"{k}={v} > {hi}")
            bad_n += 1 if viol else 0
            landed = str(r.get("landed", ""))
            # 注册页本身就在 /login?mode=register 上, 别把它误判成"弹回登录"
            bounced = ("→弹回登录" if "/login" in landed and "mode=register" not in landed
                       and r["route"] not in ("/login", "/login?mode=register") else "")
            print(f"{r['name']:<10}{landed[:24]:<26}{r['fontCount']:>5}{r['cdnRefs']:>9}{r['docH']:>7}"
                  f"  {('OK' if not viol else '越线: ' + '; '.join(viol))}{bounced}")
        print(f"\n公开面 {len(anon_rows)} 页, 越线 {bad_n}")
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump({"base": args.base, "at": time.strftime("%F %T"), "anon": True, "rows": anon_rows},
                          fh, ensure_ascii=False, indent=1)
            print("SAVED", args.json_out)
        return 1 if bad_n else 0

    with sync_playwright() as pw:
        b = pw.chromium.launch(
            headless=not args.headed,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = b.new_context(viewport={"width": 1600, "height": 900})
        ctx.add_init_script(
            f"try{{localStorage.setItem('token',{json.dumps(_login(args.base, args.user, pw_env))})}}catch(e){{}}"
        )
        page = ctx.new_page()
        for name, route, settle in ROUTES:
            rec: dict = {"name": name, "route": route}
            try:
                page.goto(args.base + route, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(settle * 1000)
                # 2026-09-19: 首页的**结构性判据**(结论行)依赖数据到位 —— 固定等待会偶发
                # "页面还空着就量", 报出 conclusionRow=false 的**假越线**(实测: 同一版本两次跑,
                # 一次 hairline=16/fonts=6, 一次 hairline=2/fonts=5 且判越线)。所以额外等一等,
                # 最多 8s; 等不到就交给下面的 unsettled 分支, 不硬判。
                if route == "/":
                    try:
                        page.wait_for_selector("[data-testid=market-conclusion]", timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass
                rec.update(page.evaluate(PROBE_JS))
            except Exception as exc:  # noqa: BLE001
                rec["error"] = f"{type(exc).__name__}: {exc}"[:160]
            rec["violations"] = judge(rec) if "error" not in rec else ["probe-failed"]
            # 未就绪(文本量过小)→ 明确标注, 而不是显示成 OK(否则等于把"没加载完"当成"合格")
            if "error" not in rec and int(rec.get("textLen") or 0) < 200:
                rec["unsettled"] = True
            rows.append(rec)
        b.close()

    hdr = f"{'页面':<8}{'K线占屏':>9}{'卡片':>6}{'hairline':>10}{'字号':>6}{'缺数':>6}{'密度':>7}  判定"
    print(hdr)
    print("-" * len(hdr) * 2)
    n_bad = 0
    for r in rows:
        if "error" in r:
            print(f"{r['name']:<8}{'—':>9}{'—':>6}{'—':>10}{'—':>6}{'—':>6}{'—':>7}  探针失败: {r['error']}")
            n_bad += 1
            continue
        ks = r.get("klineShare") or 0
        dnc = r.get("densityNoChart")
        viol = r["violations"]
        n_bad += 1 if viol else 0
        if r.get("unsettled") and not viol:
            mark = "未就绪(文本<200字, 已跳过判定) —— 复跑一次再下结论"
        else:
            mark = "OK" if not viol else "越线: " + "; ".join(viol)
        print(
            f"{r['name']:<8}{ks:>9.3f}{r['cards']:>6}{r['hairline']:>10}{r['fontCount']:>6}"
            f"{r['dash']:>6}{dnc if dnc is not None else r['density']:>7}  {mark}"
        )
        if r.get("zeroish"):
            print(f"{'':<8}  ⚠ 出现 {r['zeroish']} 处 0.00 形式数字 —— 需人工确认是真 0 还是用 0 冒充无数据")

    print()
    print(f"共 {len(rows)} 页, 达标 {len(rows) - n_bad}, 越线/失败 {n_bad}  ({time.strftime('%F %T')})")
    print("说明: 越线不阻断发版, 但需书面说明(设计稿 v3.0 §十一); 逐页明细见 --json 输出。")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"base": args.base, "at": time.strftime("%F %T"), "rows": rows}, fh,
                      ensure_ascii=False, indent=1)
        print("SAVED", args.json_out)
    return 1 if n_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
