"""图表说明(看图决策产品件, 2026-10-14): 逐标记「为什么」——人可读 / AI 可引。

## 为什么
产品口径(用户 2026-10-14 明示): **看图决策 = 提供图表说明供用户和 AI 共同决策**。
图表标注是**人机共用的证据语言** —— 用户看图拍板、AI 读图备料, 两者必须看到
**同一套**标注(单一事实源, 逐字一致)。

本模块把 K 线图层上的**每个标记**从「画了什么」升格为「**为什么在那**」, 每个标记带:

- **触发条件(triggers)**   —— 由规则/数值**确定性**拼出, 不由 LLM 编造;
- **数据时点(as_of)**      —— 缺失显式「时点缺失」, 非今日显式标基准日(**不默认成今日**);
- **失效条件(invalidation)** —— **必填**(缺失回领域兜底并标 `invalidation_defaulted`),
  反例检查防单边叙事;
- **置信度(confidence)**    —— 挂钩规则依据数 / 决策账本实测(缺样本显式「未校准」, 不给数字)。

三字段**复用** `src/core/evidence_chain.build_evidence_chain`(与 AI 建议出口**同一套
字段/口径**, 不另立标准) —— 图文同源。

## 单一事实源(禁第二套口径)
标记**列表**不由本模块重算 —— 由调用方喂入**与图层渲染同一份计算结果**:

| 标记类型 | 单一事实源 |
|---|---|
| GS 买卖点 | `_build_layer_data().gs_signals`(即 `gs_strategy.compute_gs_signals`) |
| 涨停/跌停/龙虎榜/公告/拆单簇/撤单 | `_build_layer_data().events`(即 `klines._build_events`) |
| K 线形态 | `detect_pattern_marks`(与 `/klines/{symbol}/patterns` 同函数) |
| 趋势操盘线买卖点 | `fetch_trend_line`(与 `/indicators/trend-line` 同函数) |
| 牛熊线金叉/死叉 | `fetch_niuxiong`(与 `/indicators/niuxiong` 同函数) |
| GS 区间/待确认 | `gs_state.resolve_gs_state`(与 K 线图层色带同源) |

本模块**只做「标记 → 说明」的映射**(纯函数, 无 IO), 绝不另算一条标记序列 ——
`tests/test_chart_notes.py::test_same_source_contract` 逐条钉死「chart-notes 条目 ⇔
图层标记」一一对应。

## 出口
- `GET /api/klines/{symbol}/chart-notes`(见 `src/web/api/klines.py`);
- AI 备料: `notes` 结构稳定可引用; `notes_text` 逐条人可读单行;
- 推送/战报文案取句统一走 `note_sentence(note)` / `chart_notes_sentence(...)`,
  **不再各写各的**(同源收口)。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from src.core.evidence_chain import build_evidence_chain

#: 标记类型 → 中文短类名(与前端图层/图例词表一致)
MARKER_KIND_LABELS: dict[str, str] = {
    "gs": "GS买卖点",
    "pattern": "K线形态",
    "trend_buy": "操盘线买点",
    "trend_sell": "操盘线卖点",
    "niuxiong": "牛熊线交叉",
    "limit_up": "涨停",
    "limit_down": "跌停",
    "dragon_tiger": "龙虎榜",
    "announcement": "公告",
    "split_cluster": "拆单簇",
    "cancel_anomaly": "撤单异常",
}

#: 图例词表(供 UI 分组/排序稳定)
_KIND_ORDER = list(MARKER_KIND_LABELS.keys())


# ────────────────────────────────────────────────────────────────────────────
# 单条说明
# ────────────────────────────────────────────────────────────────────────────
def _confidence_rule(note: str, basis: Sequence[str] | None = None) -> dict[str, Any]:
    """规则证据的置信度(确定性, 不挂钩账本 —— 显式标注, 绝不冒领校准数)。"""
    out: dict[str, Any] = {"calibrated": False, "value": None, "note": note}
    if basis:
        out["basis"] = list(basis)
        out["n_basis"] = len(basis)
    return out


def build_note(
    *,
    kind: str,
    label: str,
    date: Any = None,
    price: Any = None,
    side: str | None = None,
    index: Any = None,
    triggers: Any = None,
    as_of: Any = None,
    invalidation: Any = None,
    default_invalidation: Any = None,
    confidence: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一个图层标记 → 一条「为什么」说明(**三字段走 evidence_chain, 与 AI 建议同源**)。

    `triggers`/`invalidation` 由调用方(规则/数值)拼出, 不由 LLM 编造; 缺失时
    `build_evidence_chain` 保证 `invalidation` 非空(回兜底并标 defaulted)。

    `kind` 非法字符串 → 保留原值(不猜、不 500); `label` 缺省回 `MARKER_KIND_LABELS`。
    """
    k = str(kind or "").strip() or "unknown"
    ev = build_evidence_chain(
        triggers=triggers,
        as_of=as_of if as_of is not None else date,
        invalidation=invalidation,
        default_invalidation=default_invalidation,
    )
    note: dict[str, Any] = {
        "kind": k,
        "marker_type": MARKER_KIND_LABELS.get(k, k),
        "label": str(label or "").strip() or MARKER_KIND_LABELS.get(k, k),
        "date": _norm_day(date),
        "price": _num(price),
        "side": (str(side).strip() or None) if side is not None else None,
        "index": index if isinstance(index, int) else None,
        # 三字段(与 AI 建议证据链同一套字段/口径)
        "trigger": list(ev["triggers"]),
        "as_of": _norm_day(ev["as_of"]),
        "as_of_is_today": ev["as_of_is_today"],
        "as_of_note": ev["as_of_note"],
        "invalidation": list(ev["invalidation"]),
        "invalidation_defaulted": ev["invalidation_defaulted"],
        "confidence": confidence
        or _confidence_rule("规则证据, 未挂钩决策账本(不给校准数)"),
    }
    note["id"] = f"{k}:{note['date'] or '-'}:{note['index'] if note['index'] is not None else '-'}"
    if extra:
        note["extra"] = dict(extra)
    return note


# ────────────────────────────────────────────────────────────────────────────
# 各标记类型 → 触发/失效/置信(确定性, 全部来自数据, 不编造)
# ────────────────────────────────────────────────────────────────────────────
def _gs_note(sig: dict, *, as_of: Any, gs_zone: dict | None) -> dict[str, Any]:
    side = sig.get("side")
    confirmed = bool(sig.get("confirmed"))
    is_g = side == "G"
    price = sig.get("price")
    trig = [
        f"GS 交叉: 快线 A0 {'上穿' if is_g else '下穿'} 慢线 BB0"
        f" → {'G 买入' if is_g else 'S 卖出'}",
        "该交叉{}".format("已确认(收盘定死, 图表实心 ▲/▼)" if confirmed
                          else "待确认(末根盘中价, 收盘才定死, 图表空心 ○)"),
    ]
    if price is not None:
        trig.append(f"信号时点收盘价 {price}")
    if gs_zone and gs_zone.get("zone"):
        trig.append(f"当前区间 {gs_zone['zone']}(已确认锚点 {gs_zone.get('signal_date')})")
    if is_g:
        default_inval = ["若出现反向 GS 交叉(S) / 快线 A0 重新跌回慢线 BB0 下方, 则该 G 区间结束"]
    else:
        default_inval = ["若出现反向 GS 交叉(G) / 快线 A0 重新升回慢线 BB0 上方, 则该 S 区间结束"]
    conf = _confidence_rule(
        "规则证据(均线交叉), 未校准" if not confirmed else "规则证据(均线交叉, 已确认)",
        basis=["A0 快线交叉", "confirmed" if confirmed else "pending"],
    )
    return build_note(
        kind="gs", label=f"{side} {'买入' if is_g else '卖出'}", date=sig.get("date"),
        price=price, side=side, triggers=trig, as_of=as_of,
        default_invalidation=default_inval, confidence=conf,
        extra={"confirmed": confirmed},
    )


def _pattern_note(mark: Any, *, as_of: Any) -> dict[str, Any]:
    d = mark.to_dict() if hasattr(mark, "to_dict") else dict(mark or {})
    name = d.get("name")
    direction = d.get("direction")
    basis = list(d.get("basis") or [])
    trig = [f"{name} 匹配: " + " / ".join(basis)] if basis else [f"{name} 规则匹配"]
    trig.append(f"确认根 {d.get('date')}, 位置 {d.get('position')}")
    if d.get("definition"):
        trig.append(d["definition"])
    default_inval = [
        f"若 {d.get('date')} 确认根低点被收盘价跌破 / 出现反向形态, 则「{name}」形态失效"
    ]
    conf = _confidence_rule(
        f"规则严格匹配 {len(basis)} 条依据(形态库口径, 未挂钩决策账本)", basis=basis,
    )
    return build_note(
        kind="pattern", label=f"{name} {direction}", date=d.get("date"),
        side=direction, index=d.get("index"), triggers=trig, as_of=as_of,
        default_invalidation=default_inval, confidence=conf,
        extra={"category": d.get("category"), "span": d.get("span"),
               "definition": d.get("definition")},
    )


def _trend_note(pt: dict, *, as_of: Any, side: str) -> dict[str, Any]:
    trig = [pt.get("trigger") or "操盘线买卖点规则命中"]
    is_buy = side == "buy"
    default_inval = (
        ["若收盘跌破绿线 / 反弹触绿线无力突破(卖点), 则该买点作废"]
        if is_buy
        else ["若收盘重新站上绿线 / 回踩多方带收阳(买点), 则该卖点作废"]
    )
    conf = _confidence_rule(
        "规则证据(操盘线三线), 参数逆向近似待校准",
        basis=[str(pt.get("rule") or "")],
    )
    return build_note(
        kind="trend_buy" if is_buy else "trend_sell",
        label=f"操盘线{'买' if is_buy else '卖'}点",
        date=pt.get("time"), price=pt.get("price"), side=side,
        triggers=trig, as_of=as_of, default_invalidation=default_inval, confidence=conf,
        extra={"rule": pt.get("rule")},
    )


def _niuxiong_note(nx: dict, *, as_of: Any) -> dict[str, Any] | None:
    sig = nx.get("signal")
    if sig not in ("B", "S"):
        return None
    cross = nx.get("cross") or {}
    trig = [
        "牛线(加权){}{}买卖线".format("上穿" if sig == "B" else "下穿",
                                    " → 金叉 B(买)" if sig == "B" else " → 死叉 S(卖)"),
    ]
    if cross.get("bars_ago") is not None:
        trig.append(f"交叉发生在 {cross['bars_ago']} 根之前")
    if nx.get("state"):
        trig.append(f"现态 {nx['state']}")
    default_inval = [
        "若牛线反向穿越买卖线(金叉↔死叉), 则本信号翻转"
    ]
    conf = _confidence_rule("规则证据(牛熊线), 买卖线口径逆向近似待校准")
    return build_note(
        kind="niuxiong", label=f"牛熊线{'金叉 B(买)' if sig == 'B' else '死叉 S(卖)'}",
        date=cross.get("time") or nx.get("bar_time"), price=nx.get("close"),
        side=sig, triggers=trig, as_of=as_of, default_invalidation=default_inval,
        confidence=conf, extra={"cross_type": cross.get("type")},
    )


#: L4 事件 kind → (中文类名, 触发条件, 失效/作废条件)
_EVENT_RULES: dict[str, tuple[str, list[str], list[str]]] = {
    "limit_up": (
        "涨停",
        ["K线自算: 当日涨幅 ≥ +9.8%(阈值与前端 InteractiveKline 同口径)"],
        ["次日无法维持封板 / 涨幅回落即该涨停标记不再代表封板"],
    ),
    "limit_down": (
        "跌停",
        ["K线自算: 当日涨幅 ≤ -9.8%(阈值与前端 InteractiveKline 同口径)"],
        ["次日回封 / 涨幅回升即该跌停标记不再代表封死"],
    ),
    "dragon_tiger": (
        "龙虎榜",
        ["wencai(thsdk) 检索到该交易日龙虎榜披露"],
        ["仅代表当日上榜事实, 不代表后续走势(披露数据, 非建议)"],
    ),
    "announcement": (
        "公告",
        ["wencai(thsdk) 检索到该交易日公告披露"],
        ["仅代表当日有公告披露, 不代表后续走势(披露数据, 非建议)"],
    ),
    "split_cluster": (
        "拆单簇",
        ["逐笔(.tck) 连续同向小单聚集(拆单簇)"],
        ["仅代表当日拆单行为, 方向不预设(可能吸筹/派发, 需多维判断)"],
    ),
    "cancel_anomaly": (
        "撤单异常",
        ["逐笔(.tck) 大额/集中撤单记录"],
        ["仅代表当日撤单行为, 不代表后续走势"],
    ),
}


def _event_note(ev: dict, *, as_of: Any) -> dict[str, Any] | None:
    kind = str(ev.get("kind") or "").strip()
    rule = _EVENT_RULES.get(kind)
    if not rule:
        return None  # 未知事件类不产出(不猜)
    label, trig, inval = rule
    return build_note(
        kind=kind, label=ev.get("label") or label, date=ev.get("date"),
        price=ev.get("price"), triggers=trig, as_of=as_of,
        default_invalidation=inval,
        confidence=_confidence_rule("事实披露/规则自算, 非建议, 未挂钩决策账本"),
    )


# ────────────────────────────────────────────────────────────────────────────
# 装配: 图层标记 → chart-notes
# ────────────────────────────────────────────────────────────────────────────
def build_chart_notes(
    *,
    symbol: str,
    as_of: Any = None,
    gs_signals: Iterable[dict] | None = None,
    events: Iterable[dict] | None = None,
    patterns: Iterable[Any] | None = None,
    trend: dict | None = None,
    niuxiong: dict | None = None,
    gs_state: dict | None = None,
) -> dict[str, Any]:
    """把**图层同一份标记**逐条转成「为什么」说明(纯函数, 无 IO, 不重算标记)。

    顺序与图层渲染一致: GS → 形态 → 操盘线买/卖 → 牛熊线 → 事件。每条说明的
    `(kind, date, index)` 与源标记一一对应(同源契约)。

    非 A 股 / 无任何标记 → `notes=[]`, `count=0`, 附 `note`(不编造)。
    """
    zone = gs_state if isinstance(gs_state, dict) else None
    notes: list[dict[str, Any]] = []

    # ① GS 买卖点(与图层 gs_signals 逐条对应; 含待确认)
    for sig in (gs_signals or []):
        if not isinstance(sig, dict) or sig.get("side") not in ("G", "S"):
            continue
        notes.append(_gs_note(sig, as_of=as_of or sig.get("date"), gs_zone=zone))

    # ② K 线形态(与 /patterns 同函数输出)
    for mark in (patterns or []):
        notes.append(_pattern_note(mark, as_of=as_of))

    # ③ 趋势操盘线买卖点(与 /indicators/trend-line 同函数输出)
    t = trend if isinstance(trend, dict) else {}
    if t.get("available"):
        for pt in (t.get("buy_points") or []):
            if isinstance(pt, dict):
                notes.append(_trend_note(pt, as_of=as_of, side="buy"))
        for pt in (t.get("sell_points") or []):
            if isinstance(pt, dict):
                notes.append(_trend_note(pt, as_of=as_of, side="sell"))

    # ④ 牛熊线金叉/死叉(与 /indicators/niuxiong 同函数输出)
    nx = niuxiong if isinstance(niuxiong, dict) else {}
    if nx.get("available"):
        n = _niuxiong_note(nx, as_of=as_of)
        if n:
            notes.append(n)

    # ⑤ L4 事件(与图层 events 逐条对应)
    for ev in (events or []):
        if isinstance(ev, dict):
            n = _event_note(ev, as_of=as_of or ev.get("date"))
            if n:
                notes.append(n)

    # 稳定排序: 先按类(图层顺序), 再按日期, 再按 index —— 保证可复现/可对拍
    notes.sort(key=lambda n: (
        _KIND_ORDER.index(n["kind"]) if n["kind"] in _KIND_ORDER else len(_KIND_ORDER),
        str(n.get("date") or ""),
        n.get("index") if n.get("index") is not None else -1,
    ))

    counts: dict[str, int] = {}
    for n in notes:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1

    out: dict[str, Any] = {
        "symbol": symbol,
        "asof": _norm_day(as_of),
        "available": bool(notes),
        "count": len(notes),
        "counts": counts,
        "notes": notes,
        "notes_text": [note_sentence(n) for n in notes],
    }
    if not notes:
        out["note"] = "本窗口内无可标注的图标记(无数据不编造)"
    return out


# ────────────────────────────────────────────────────────────────────────────
# 人可读单行(AI 备料 / 图下说明条 / 推送文案共用)
# ────────────────────────────────────────────────────────────────────────────
def note_sentence(note: dict[str, Any]) -> str:
    """一条说明 → 人可读单行(结构稳定, 供图下说明条与 AI/推送直接引用)。

    形如: `2026-09-11 · GS买卖点 G 买入 @12.34 · 触发: A0上穿BB0(已确认) · 时点: 2026-09-11(今日) · 失效: ...`
    """
    if not isinstance(note, dict):
        return ""
    date = note.get("date") or "日期缺失"
    mtype = note.get("marker_type") or note.get("kind") or "标记"
    label = note.get("label") or ""
    price = note.get("price")
    head = f"{date} · {mtype} {label}".rstrip()
    if price is not None:
        head += f" @{price}"
    trig = "、".join(note.get("trigger") or []) or "触发条件缺失"
    as_of_note = note.get("as_of_note") or "数据时点缺失"
    inval = "、".join(note.get("invalidation") or []) or "失效条件缺失"
    return f"{head} · 触发: {trig} · 时点: {as_of_note} · 失效: {inval}"


def chart_notes_sentence(notes: Iterable[dict], *, limit: int = 5) -> str:
    """多条说明 → 一段人可读文本(供 AI/推送取句; `limit<=0` 表示不限)。

    空 → 显式「无图标记」(不编造)。
    """
    rows = [note_sentence(n) for n in (notes or []) if isinstance(n, dict)]
    if limit and limit > 0:
        rows = rows[:limit]
    if not rows:
        return "无图标记说明"
    return "；".join(rows)


def pick_text(notes: Iterable[dict], kinds: Iterable[str], *, limit: int = 3) -> str:
    """按标记类型过滤后拼句(推送/战报「引用图上标记」时取句的**统一入口**)。

    AI 文案凡引用图上标记, 一律走本函数取句 —— 保证与 chart-notes / 图层**同源**;
    取不到 → 空串(调用方显式降级, 不编)。
    """
    want = set(kinds or [])
    sel = [n for n in (notes or []) if isinstance(n, dict) and n.get("kind") in want]
    rows = [note_sentence(n) for n in sel]
    if limit and limit > 0:
        rows = rows[:limit]
    return "；".join(rows)


# ────────────────────────────────────────────────────────────────────────────
# GS 标记(当前区间/信号)—— 推送/战报/insights 引用图上 GS 标记的**同源取句入口**
# ────────────────────────────────────────────────────────────────────────────
#: 推送文案里的信号短名(v0.13.67 起的既有词表; 集中在此, 免两处各写各的)
_GS_SIG_PIONEER = {"G": "G买", "S": "S卖"}
_GS_SIG_TREND = {"G": "G买(上穿)", "S": "S卖(下穿)"}


def gs_state_note(gs: dict[str, Any] | None, *, as_of: Any = None) -> dict[str, Any] | None:
    """当前 GS 标记(区间/最近已确认信号 + 末根待确认) → 一条 chart-note(**与 K 线图 GS 图例同源**)。

    `gs` = `gs_state.resolve_gs_state(bars)` 的输出(单一事实源)。无区间且无待确认 → None。
    三字段(触发/时点/失效)与其它标记**同一套口径**(build_evidence_chain), 供 AI 备料引用。
    """
    if not isinstance(gs, dict):
        return None
    zone = gs.get("zone") or gs.get("state")
    pending = bool(gs.get("pending"))
    if not zone and not pending:
        return None
    sig = _GS_SIG_PIONEER.get(str(gs.get("signal") or ""), "无")
    trig: list[str] = []
    if zone:
        trig.append(f"最近已确认信号 {sig}" + (f"({gs.get('signal_date')})" if gs.get("signal_date") else ""))
        trig.append(f"快线 A0 {gs.get('a0')} / 慢线 BB0 {gs.get('bb0')}(展示项, 不参与区间判定)")
    if pending:
        trig.append(f"末根疑似转 {gs.get('pending_side')}(待确认, 图表空心 ○, 不接管区间)")
    if not trig:
        trig = ["GS 状态(无可确认区间)"]
    if zone == "G区":
        default_inval = ["若出现已确认反向 GS 交叉(S), 则 G 区间结束"]
    elif zone == "S区":
        default_inval = ["若出现已确认反向 GS 交叉(G), 则 S 区间结束"]
    else:
        default_inval = ["尚无已确认区间, 末根疑似交叉未定型; 下一根 bar 到来方可确认"]
    return build_note(
        kind="gs", label=(f"{zone}区间" if zone else "待确认"),
        date=gs.get("signal_date") or gs.get("pending_date"),
        price=gs.get("signal_price"), side=gs.get("signal"),
        as_of=as_of or gs.get("as_of"), triggers=trig,
        default_invalidation=default_inval,
        confidence=_confidence_rule("规则证据(GS 区间/信号), 未挂钩决策账本"),
        extra={"zone": zone, "pending": pending, "pending_side": gs.get("pending_side"),
               "pending_label": gs.get("pending_label")},
    )


def gs_state_text(gs: dict[str, Any] | None, *, style: str = "pioneer") -> str:
    """当前 GS 状态 → 推送/战报文案(逐字取自 v0.13.67 既有写法, 现**单点收口**)。

    盘中推送/战报/insights 凡引用图上 GS 标记, 一律走本函数取句 —— 与 chart-notes /
    K 线图 G 区色带**同源**(同一份 `resolve_gs_state` 输出), 不再各写各的。

    `style="pioneer"`: 决策面板/战报一段式(decision_pioneer 口径);
    `style="trend"`  : 盘中推送三指标段(intraday_monitor 口径)。
    """
    if not isinstance(gs, dict):
        return ""
    zone = gs.get("zone") or gs.get("state")
    pending = bool(gs.get("pending"))
    if not (zone or pending):
        return ""
    if style == "trend":
        sig = _GS_SIG_TREND.get(str(gs.get("signal") or ""), "无")
        txt = f"{zone or '待确认'}"
        if pending:
            txt += f"(末根疑似{gs.get('pending_side')}待确认)"
        return f"{txt}(方向过滤, 买卖点滞后仅参考, 最近已确认{sig})"
    # pioneer 口径(默认)
    sig = _GS_SIG_PIONEER.get(str(gs.get("signal") or ""), "无")
    if zone:
        txt = f"当前{zone}(已确认), 最近已确认信号{sig}"
        if gs.get("signal_date"):
            txt += f"({gs['signal_date']})"
        if pending:
            txt += f"; {gs.get('pending_date') or '末根'}疑似转{gs.get('pending_side')}待确认"
    else:
        txt = f"待确认(尚无已确认区间), 末根疑似{gs.get('pending_side')}"
    return txt


# ────────────────────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────────────────────
def _norm_day(d: Any) -> str | None:
    """日期 → 'YYYY-MM-DD'(无法识别回 None, 不猜)。"""
    s = str(d or "").strip()
    if not s:
        return None
    digits = s.replace("-", "").replace("/", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return s[:10]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f
