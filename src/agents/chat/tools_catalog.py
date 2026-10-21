# -*- coding: utf-8 -*-
"""数据域目录 + 统一查询入口（2026-10-21，数据域架构 P0）。

## 为什么有这一层

平台有 132 张表 / 443 路由，但 chat 只有 62 个工具、可触达约 14%。继续堆工具数会把
模型的选择成本推到不可用。这里改为「**先给目录，再查数据**」：

- `list_data_domains()`  —— 一句话 + 26 个域的一行摘要（≤2500 字），模型据此知道有什么可查；
- `query_data_domain(domain, params_json)` —— 封闭白名单的统一取数入口。

域目录的唯一权威是 `src/harness/data_catalog.yaml`（**只读，本模块不改它**）。

## 三条铁律

1. **有现成工具的域一律转发到原 handler**（`CHAT_TOOL_REGISTRY`），不在这里重写取数逻辑
   —— 保证与页面/既有工具**同一口径**，不产生第二套数字。
   P0 八个域里，`limit.pool`（涨停池/连板梯队/封板强度）、`lhb.daily`（龙虎榜/席位）与
   `theme.mood`（题材五维评分）没有现成 chat 工具，由本模块写只读 SQL（连板梯队复用
   `src.core.limit_ladder` 的算法，与题材情绪页同源）。
2. **只读**：只发 SELECT，必带 LIMIT（禁止全表扫），一律 `sqlalchemy.text()` + 绑定参数，
   禁止字符串拼接。本模块不依赖 Web 层（不 import src/web），只用 `src.db.session.SessionLocal`。
3. **说人话 + 不编**：首行「域 / 时点 / 来源表」，正文分条；取不到就写「未取到（原因）」，
   **绝不补 0**；单域渲染 ≤1200 字（超出截断并标注）。

注册方式与 `tools_thsdk.py` 一致：import 时登记（`registry.py` 末尾调用 `register_catalog_tools()`），
两个工具排在导出清单最末，不破坏 core 工具的连续前缀。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User

logger = logging.getLogger(__name__)

#: 目录长度上限（字）。模型上下文预算：目录一屏能读完。
MAX_CATALOG_CHARS = 2500
#: 单域渲染上限（字）。超出截断并标注，不静默丢失。
MAX_DOMAIN_CHARS = 1200
#: 「未取到」统一措辞 —— 下游据此判断是空还是 0。
MISSING_TAG = "未取到"

_CST = ZoneInfo("Asia/Shanghai")
_CATALOG_FILE = Path(__file__).resolve().parents[2] / "harness" / "data_catalog.yaml"


# ──────────────────────────── 目录（data_catalog.yaml 只读） ────────────────────────────


def catalog_path() -> Path:
    """目录文件路径（测试与运维用来定位，不做写入）。"""
    return _CATALOG_FILE


def load_domains() -> list[dict[str, Any]]:
    """读 data_catalog.yaml 的 domains（返回规整后的 dict 列表）。

    文件缺失/解析失败不抛：返回空列表，工具层如实说「目录不可用」——
    宁可什么都不查，也不凭空造域和表名。
    """
    try:
        import yaml

        raw = yaml.safe_load(_CATALOG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据域目录读取失败 %s: %s", _CATALOG_FILE, exc)
        return []
    out: list[dict[str, Any]] = []
    for d in raw.get("domains") or []:
        if not isinstance(d, dict) or not str(d.get("id") or "").strip():
            continue
        params = d.get("params")
        out.append(
            {
                "id": str(d["id"]).strip(),
                "label": str(d.get("label") or "").strip(),
                "level": str(d.get("level") or "").strip(),
                "scope": str(d.get("scope") or "").strip(),
                "params": params if isinstance(params, dict) else {},
                "source": str(d.get("source") or "").strip(),
                "desc": str(d.get("desc") or "").strip(),
            }
        )
    return out


DOMAINS: tuple[dict[str, Any], ...] = tuple(load_domains())
#: 封闭白名单 —— 与 yaml 的 id 集合恒等（测试锁定，防漂移）。
DOMAIN_IDS: frozenset[str] = frozenset(d["id"] for d in DOMAINS)
_DOMAIN_BY_ID: dict[str, dict[str, Any]] = {d["id"]: d for d in DOMAINS}


def required_params(domain: str) -> tuple[str, ...]:
    """该域的必填参数（yaml 里取值以「必填」开头的键）。"""
    params = (_DOMAIN_BY_ID.get(domain) or {}).get("params") or {}
    return tuple(k for k, v in params.items() if str(v).strip().startswith("必填"))


def _cap(text: str, limit: int) -> str:
    """截断到 limit 字，并显式标注被截断（不静默丢内容）。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 24, 0)].rstrip() + f"\n…（已截断，原文 {len(text)} 字）"


def render_domain_catalog() -> str:
    """26 个域的一行摘要（`域id | 标签 | 必填参数 | 覆盖什么`），总长 ≤2500 字。"""
    if not DOMAINS:
        return (
            "数据域目录不可用：src/harness/data_catalog.yaml 读不到或为空。"
            "本次不提供域列表，也不要凭印象猜表名。"
        )
    head = (
        f"平台可查数据域（共 {len(DOMAINS)} 个）。先在这里选域，再用 "
        f"query_data_domain(domain, params_json) 取数；参数列写「必填」的必须给。"
        f"标注「P1 待接」的域尚未接入实现，调了会明确说未接入。"
    )
    lines = [head]
    for d in DOMAINS:
        req = "、".join(required_params(d["id"])) or "—"
        lines.append(f"{d['id']} | {d['label']} | {req} | {d['desc']}")
    return _cap("\n".join(lines), MAX_CATALOG_CHARS)


# ──────────────────────────── 渲染（人话 Markdown，缺数据说「未取到」） ────────────────────────────


def render_domain_sections(
    domain: str,
    *,
    as_of: str,
    source: str,
    sections: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    """把某域的分条正文拼成 Markdown。

    首行固定「域 / 时点 / 来源表」；`sections` 全空 → 明确写「未取到」，
    **不写 0、不写「持平/正常」这类看起来有值的措辞**。
    """
    label = (_DOMAIN_BY_ID.get(domain) or {}).get("label") or ""
    title = f"**{domain}{(' · ' + label) if label else ''}** ｜ 时点 {as_of or '未知'}"
    title += f" ｜ 来源 {source or '未标注'}"
    body = [s for s in (sections or []) if s and str(s).strip()]
    miss = [str(m) for m in (missing or []) if str(m).strip()]
    if not body:
        miss = miss or ["该域本次没有取到任何记录（数据源无行或尚未采集）"]
    out = [title, *body]
    if miss:
        out.append(f"{MISSING_TAG}：" + "；".join(miss))
    return _cap("\n".join(out), MAX_DOMAIN_CHARS)


def _num(v: Any, digits: int = 2, signed: bool = False) -> str:
    """数值格式化；None/非数 → 「未披露」（禁补 0）。"""
    if v is None:
        return "未披露"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "未披露"
    return f"{f:+.{digits}f}" if signed else f"{f:.{digits}f}"


def _amt(v: Any) -> str:
    """金额（元）→ 亿/万；None → 「未披露」。"""
    if v is None:
        return "未披露"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "未披露"
    if abs(f) >= 1e8:
        return f"{f / 1e8:+.2f}亿"
    if abs(f) >= 1e4:
        return f"{f / 1e4:+.0f}万"
    return f"{f:+.0f}元"


def _pct(v: Any, digits: int = 2) -> str:
    """已是百分数的字段（涨幅/换手）→ 「+1.23%」；None → 「未披露」。"""
    if v is None:
        return "未披露"
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return "未披露"


def _ymd(v: Any) -> str:
    """日期归一为紧凑 YYYYMMDD（事件表 trade_date 口径）；取不到 → 空串。"""
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def render_limit_pool(
    *,
    as_of: str,
    counts: dict[str, Any] | None = None,
    ladder_rows: list[dict[str, Any]] | None = None,
    strength_rows: list[dict[str, Any]] | None = None,
    blown: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    """涨停池 / 连板梯队 / 封板强度（三表合成一屏）。空结果 → 「未取到」。"""
    sections: list[str] = []
    total = (counts or {}).get("total")
    if counts and total:
        n_total = int(total)
        n_sealed = int(counts.get("sealed") or 0)
        rate = f"{n_sealed / n_total * 100:.1f}%" if n_total else "—"
        sections.append(
            f"- 触板 {n_total} 家：收盘封住 {n_sealed} 家，炸板 {n_total - n_sealed} 家（封板率 {rate}）"
        )
    if ladder_rows:
        parts = []
        for row in ladder_rows[:6]:
            boards = int(row.get("boards") or 0)
            tag = "首板" if boards <= 1 else f"{boards}板"
            names = [str(x) for x in (row.get("names") or [])]
            shown = "、".join(names[:8]) or "—"
            more = f" 等 {len(names)} 只" if len(names) > 8 else ""
            parts.append(f"{tag} {len(names)} 只（{shown}{more}）")
        sections.append("- 连板梯队：" + "；".join(parts))
    if blown:
        shown = "、".join(str(x) for x in blown[:10])
        more = f" 等 {len(blown)} 只" if len(blown) > 10 else ""
        sections.append(f"- 炸板（触板未封）：{shown}{more}")
    if strength_rows:
        items = []
        for r in strength_rows[:5]:
            sym = str(r.get("symbol") or "")
            name = str(r.get("name") or "")
            label = r.get("strength_label") or r.get("seal_ratio_grade") or "未分级"
            ratio = r.get("seal_ratio")
            ratio_txt = (
                f"封成比 {float(ratio) * 100:.2f}%" if isinstance(ratio, (int, float)) else "封成比 未披露"
            )
            bits = [f"{sym} {name}".strip(), f"强度 {label}", ratio_txt]
            if r.get("first_time"):
                bits.append(f"首封 {r['first_time']}")
            if r.get("open_times") is not None:
                bits.append(f"开板 {r['open_times']} 次")
            items.append("，".join(bits))
        sections.append("- 封板强度（按封成比降序）：" + "；".join(items))
    return render_domain_sections(
        "limit.pool",
        as_of=as_of,
        source="limit_up_events / limit_strength_shadow / ladder_daily_snapshot",
        sections=sections,
        missing=missing,
    )


def render_lhb(
    *,
    as_of: str,
    events: list[dict[str, Any]] | None = None,
    seats: list[dict[str, Any]] | None = None,
    missing: list[str] | None = None,
) -> str:
    """龙虎榜榜单 + 席位明细。空结果 → 「未取到」。"""
    sections: list[str] = []
    rows = list(events or [])
    if rows:
        uniq = {str(r.get("symbol") or "") for r in rows}
        sections.append(f"- 上榜记录 {len(rows)} 条（涉及 {len(uniq)} 只个股）")
        ordered = sorted(rows, key=lambda r: (r.get("net_buy") is None, -(r.get("net_buy") or 0)))
        buys = [r for r in ordered if (r.get("net_buy") or 0) > 0][:5]
        sells = [r for r in ordered if (r.get("net_buy") or 0) < 0][-5:]
        if buys:
            sections.append("- 净买入前列：" + "；".join(_fmt_lhb_row(r) for r in buys))
        if sells:
            sections.append("- 净卖出前列：" + "；".join(_fmt_lhb_row(r) for r in sells))
        if not buys and not sells:
            sections.append("- 净额均为 0 或未披露，不排序（不编方向）")
    if seats:
        for side, cn in (("buy", "买入席位"), ("sell", "卖出席位")):
            group = [r for r in seats if str(r.get("side") or "").lower() == side][:5]
            if not group:
                continue
            items = []
            for r in group:
                dept = str(r.get("operate_dept_name") or "未知营业部")
                items.append(f"{dept} {_amt_plain(r.get('accum_amount'))}")
            sections.append(f"- {cn}：" + "；".join(items))
    return render_domain_sections(
        "lhb.daily",
        as_of=as_of,
        source="dragon_tiger_events / lhb_seat_details",
        sections=sections,
        missing=missing,
    )


def _amt_plain(v: Any) -> str:
    """金额（元）→ 亿/万，**不带符号**（买/卖额是量级，不是方向）；None → 「未披露」。"""
    txt = _amt(v)
    return txt[1:] if txt.startswith("+") else txt


def _core_stocks(raw: Any) -> list[str]:
    """core_stocks 列（JSON 文本）→ ["002361 闽东电力(3板)", ...]；解析失败 → []。"""
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        sym, name = str(c.get("symbol") or ""), str(c.get("name") or "")
        boards = c.get("boards")
        tag = f"({boards}板)" if boards else ""
        out.append(f"{sym} {name}{tag}".strip())
    return out


def _fmt_theme_row(r: dict[str, Any], fund: dict[str, Any] | None) -> str:
    name = r.get("block_name") or r.get("block_code") or "?"
    core_flag = "★核心 " if r.get("core") else ""
    line = (
        f"- {core_flag}{name}：总分 {_num(r.get('score'), 1)}"
        f"（结构 {_num(r.get('s1'), 0)}/扩散 {_num(r.get('s2'), 0)}/核心 {_num(r.get('s3'), 0)}"
        f"/接力 {_num(r.get('s4'), 0)}/持续 {_num(r.get('s5'), 0)}；置信 {_num(r.get('confidence'), 0)}）"
        f"｜涨停 {_num(r.get('limit_up_cnt'), 0)} 触板 {_num(r.get('touched_cnt'), 0)}"
        f" 最高 {_num(r.get('max_boards'), 0)} 板 二板+ {_num(r.get('ge2_cnt'), 0)}"
    )
    subs = []
    leaders = _core_stocks(r.get("core_stocks"))[:3]
    if leaders:
        subs.append("龙头 " + "、".join(leaders))
    if fund:
        subs.append(
            f"板块资金 涨跌 {_pct(fund.get('change_pct'))} 净流入 {_amt(fund.get('fund_net'))}"
        )
    if subs:
        line += "\n  " + " ｜ ".join(subs)
    return line


def render_theme_mood(
    *,
    as_of: str,
    rows: list[dict[str, Any]] | None = None,
    funds: dict[str, dict[str, Any]] | None = None,
    top_n: int = 20,
    missing: list[str] | None = None,
) -> str:
    """题材情绪与生命周期（五维评分 + 板内结构 + 板块资金）。空结果 → 「未取到」。"""
    items = [r for r in (rows or []) if isinstance(r, dict)]
    items.sort(key=lambda r: (r.get("score") is None, -(r.get("score") or 0)))
    items = items[: max(int(top_n or 20), 1)]
    sections: list[str] = []
    if items:
        sections.append(f"- 当日有评分题材 {len(items)} 个（按总分降序，最多 {top_n} 个）")
        for r in items[:8]:
            sections.append(_fmt_theme_row(r, (funds or {}).get(str(r.get("block_code")))))
        if len(items) > 8:
            sections.append(f"- 其余 {len(items) - 8} 个未展开（提高 top_n 可看到更多，单域上限 1200 字）")
        sections.append(
            "口径：score/s1-s5 来自 theme_mood_daily（结构=板内涨停结构、扩散=涨幅广度、"
            "核心=核心票强度、接力=昨封今晋级、持续=近 3 日强度延续）；平台无权威「生命周期阶段」"
            "分类器，故只给五维读数——接力/持续两维偏低即延续性弱，别替它贴阶段标签。"
        )
    return render_domain_sections(
        "theme.mood",
        as_of=as_of,
        source="theme_mood_daily / board_daily",
        sections=sections,
        missing=missing,
    )


def _fmt_lhb_row(r: dict[str, Any]) -> str:
    bits = [f"{r.get('symbol') or '?'} {r.get('name') or ''}".strip()]
    bits.append(f"净买 {_amt(r.get('net_buy'))}")
    buy, sell = r.get("buy_amt"), r.get("sell_amt")
    if buy is not None or sell is not None:
        bits.append(f"买 {_amt_plain(buy)} / 卖 {_amt_plain(sell)}")
    bits.append(f"涨幅 {_pct(r.get('change_pct'))}")
    if r.get("turnover_pct") is not None:
        bits.append(f"换手 {_num(r.get('turnover_pct'))}%")
    reason = str(r.get("reason") or "").strip()
    if reason:
        bits.append(f"原因「{reason[:24]}」")
    return f"{bits[0]}（{'，'.join(bits[1:])}）"



# ──────────────────────────── 只读取数底座（SELECT + LIMIT + 绑定参数） ────────────────────────────

#: 全部只读语句集中在此（测试静态校验「必须有 LIMIT」）。命名参数一律 `:name`，双方言通用。
#: 排序统一 `ORDER BY symbol`（不在 SQL 里按可空列排序：SQLite/PG 的 NULL 次序相反）。
_SQL: dict[str, str] = {
    "limit_date": "SELECT MAX(trade_date) AS d FROM limit_up_events LIMIT 1",
    "limit_counts": (
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN is_sealed_close = 1 THEN 1 ELSE 0 END) AS sealed "
        "FROM limit_up_events WHERE trade_date = :d LIMIT 1"
    ),
    "limit_events": (
        "SELECT symbol, market, name, prev_close, limit_price, close_price, touched, is_sealed_close "
        "FROM limit_up_events WHERE trade_date = :d ORDER BY symbol LIMIT :n"
    ),
    "limit_dates": "SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT :n",
    "limit_history": (
        "SELECT trade_date, symbol, name, is_sealed_close FROM limit_up_events "
        "WHERE trade_date >= :a ORDER BY trade_date, symbol LIMIT :n"
    ),
    "limit_strength": (
        "SELECT symbol, name, first_time, seal_amount, circulating_mktcap, seal_ratio, "
        "seal_ratio_grade, open_times, strength_label, status FROM limit_strength_shadow "
        "WHERE trade_date = :d ORDER BY symbol LIMIT :n"
    ),
    "ladder_snapshot": (
        "SELECT payload, date FROM ladder_daily_snapshot "
        "WHERE window_days = :w AND mode = :m ORDER BY date DESC LIMIT 1"
    ),
    "theme_date": "SELECT MAX(trade_date) AS d FROM theme_mood_daily LIMIT 1",
    "theme_rows": (
        "SELECT trade_date, block_code, block_name, block_type, score, s1, s2, s3, s4, s5, "
        "confidence, core, limit_up_cnt, touched_cnt, max_boards, ge2_cnt, core_stocks, breadth "
        "FROM theme_mood_daily WHERE trade_date = :d ORDER BY block_code LIMIT :n"
    ),
    "theme_funds": (
        "SELECT block_code, date, change_pct, fund_net, volume FROM board_daily "
        "WHERE date = :d ORDER BY block_code LIMIT :n"
    ),
    "lhb_date": "SELECT MAX(trade_date) AS d FROM dragon_tiger_events LIMIT 1",
    "lhb_events": (
        "SELECT symbol, market, name, reason, close, change_pct, net_buy, buy_amt, sell_amt, "
        "deal_amt, turnover_pct FROM dragon_tiger_events WHERE trade_date = :d "
        "ORDER BY symbol LIMIT :n"
    ),
    "lhb_events_symbol": (
        "SELECT symbol, market, name, reason, close, change_pct, net_buy, buy_amt, sell_amt, "
        "deal_amt, turnover_pct FROM dragon_tiger_events WHERE trade_date = :d AND symbol = :s "
        "ORDER BY symbol LIMIT :n"
    ),
    "lhb_seats": (
        "SELECT side, symbol, name, operate_dept_name, close, change_pct, accum_amount, accum_volume "
        "FROM lhb_seat_details WHERE trade_date = :d AND symbol = :s "
        "ORDER BY side, symbol LIMIT :n"
    ),
    "lhb_seats_top": (
        "SELECT side, symbol, name, operate_dept_name, close, change_pct, accum_amount, accum_volume "
        "FROM lhb_seat_details WHERE trade_date = :d ORDER BY symbol LIMIT :n"
    ),
}

#: 客户端传入 db 为 None 时自开/自关会话（测试与定时任务路径）。

def _session(db: Any) -> tuple[Any, bool]:
    if db is not None:
        return db, False
    from src.db.session import SessionLocal

    return SessionLocal(), True


def _read(db: Any, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """执行一条只读语句，返回 dict 行。缺 LIMIT 直接拒绝（编程期错误，不发给库）。"""
    sql = _SQL.get(key)
    if not sql:
        raise KeyError(f"未登记只读语句: {key}")
    if "LIMIT" not in sql.upper():
        raise ValueError(f"只读查询必须带 LIMIT（禁止全表扫）: {key}")
    from sqlalchemy import text

    session, own = _session(db)
    try:
        rows = session.execute(text(sql), params or {}).mappings().all()
        return [dict(r) for r in rows]
    finally:
        if own:
            session.close()


def _read_one(db: Any, key: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = _read(db, key, params)
    return rows[0] if rows else None


# ──────────────────────────── 各域取数 ────────────────────────────


def _now_cn() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M")


async def _delegate(name: str, db: Any, args: dict[str, Any], user: Any) -> str:
    """转发到注册表里既有工具的原 handler（同一口径）。

    失败/未注册一律返回空串，由调用方记「未取到（原因）」——不在这里编数据。
    """
    try:
        from src.agents.chat.registry import CHAT_TOOL_REGISTRY

        tool = CHAT_TOOL_REGISTRY.get(name)
        if tool is None:
            logger.warning("数据域转发失败: 工具 %s 未注册", name)
            return ""
        out = await tool.handler(db, dict(args or {}), user)
        return out if isinstance(out, str) else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("数据域转发工具 %s 失败: %s", name, exc)
        return ""


async def _query_market_sentiment(db: Any, params: dict[str, Any], user: Any) -> str:
    text_out = await _delegate("get_sentiment_cycle", db, {}, user)
    sections = [text_out] if text_out.strip() else []
    return render_domain_sections(
        "market.sentiment",
        as_of=_now_cn(),
        source="复用 get_sentiment_cycle（涨停池/连板高度情绪周期模型 + 市场广度）",
        sections=sections,
        missing=[] if sections else ["get_sentiment_cycle 未返回内容（涨停池可能不可用）"],
    )


async def _query_stock_chip(db: Any, params: dict[str, Any], user: Any) -> str:
    symbol = str(params.get("symbol") or "").strip().upper()
    text_out = await _delegate("get_chip_accumulation", db, {"symbol": symbol, "market": "CN"}, user)
    sections = [text_out] if text_out.strip() else []
    return render_domain_sections(
        "stock.chip",
        as_of=_now_cn(),
        source="复用 get_chip_accumulation（自算 CYQ · chip_distribution）",
        sections=sections,
        missing=[] if sections else ["筹码模型未返回内容（日K 不足或源不可用）"],
    )


async def _query_stock_flow(db: Any, params: dict[str, Any], user: Any) -> str:
    symbol = str(params.get("symbol") or "").strip().upper()
    args = {"symbol": symbol, "market": "CN"}
    intent = await _delegate("get_main_intent", db, args, user)
    cancel = await _delegate("get_cancel_order_intent", db, args, user)
    sections = [s for s in (intent, cancel) if s and s.strip()]
    missing: list[str] = []
    if not (intent and intent.strip()):
        missing.append("主力意图（腾讯逐笔）未返回内容")
    if not (cancel and cancel.strip()):
        missing.append("撤单意图（TQ 会话/收盘快照）未返回内容")
    return render_domain_sections(
        "stock.flow",
        as_of=_now_cn(),
        source="复用 get_main_intent（腾讯逐笔）/ get_cancel_order_intent（TQ 撤单）",
        sections=sections,
        missing=missing,
    )


async def _query_stock_intraday(db: Any, params: dict[str, Any], user: Any) -> str:
    symbol = str(params.get("symbol") or "").strip().upper()
    text_out = await _delegate("get_intraday_profile", db, {"symbol": symbol, "market": "CN"}, user)
    sections = [text_out] if text_out.strip() else []
    return render_domain_sections(
        "stock.intraday",
        as_of=_now_cn(),
        source="复用 get_intraday_profile（src.core.intraday_profile.build）",
        sections=sections,
        missing=[] if sections else ["分时画像未返回内容（腾讯分时源不可用？）"],
    )


async def _query_stock_factor(db: Any, params: dict[str, Any], user: Any) -> str:
    symbol = str(params.get("symbol") or "").strip().upper()
    factor_txt = await _delegate("get_stock_factors", db, {"symbol": symbol, "market": "CN"}, user)
    sections = [factor_txt] if factor_txt.strip() else []
    missing: list[str] = []
    if not sections:
        missing.append("个股因子（stock_factor_daily）未返回内容")
    # IC/IR 是全市场 × 60 日的重算（load_qfq_bars 逐票），默认不跑；显式要了才跑。
    if params.get("with_ic"):
        try:
            from src.core import factor_ic as _factor_ic

            metrics = await asyncio.to_thread(_factor_ic.compute, db)
            sections.append(_factor_ic.render(metrics))
        except Exception as exc:  # noqa: BLE001
            logger.warning("因子 IC 计算失败: %s", exc)
            missing.append(f"因子有效性 IC/IR 计算失败（{type(exc).__name__}）")
    else:
        sections.append("（因子有效性 IC/IR 需 params_json 里加 \"with_ic\": true，全市场重算较慢，默认不跑）")
    return render_domain_sections(
        "stock.factor",
        as_of=_now_cn(),
        source="复用 get_stock_factors（stock_factor_daily）+ src.core.factor_ic",
        sections=sections,
        missing=missing,
    )


def _ladder_from_snapshot(db: Any, date: str) -> tuple[list[dict[str, Any]], str]:
    """兜底：从页面预渲染的 ladder_daily_snapshot 取某日梯队（与题材情绪页同源）。

    返回 (rows, 说明)。读不到 → ([], 原因)。
    """
    row = _read_one(db, "ladder_snapshot", {"w": 20, "m": "auto"})
    if not row:
        return [], "ladder_daily_snapshot 无快照行"
    try:
        payload = json.loads(row["payload"]) if isinstance(row.get("payload"), str) else row.get("payload")
    except Exception as exc:  # noqa: BLE001
        return [], f"ladder_daily_snapshot payload 解析失败（{type(exc).__name__}）"
    if not isinstance(payload, dict):
        return [], "ladder_daily_snapshot payload 非对象"
    days = payload.get("ladder") or []
    target = next((d for d in days if _ymd(d.get("date")) == date), None)
    if not target:
        return [], f"ladder_daily_snapshot 快照日 {payload.get('dates')} 不含 {date}"
    rows = [
        {"boards": r.get("boards"), "codes": r.get("codes") or [], "names": r.get("names") or []}
        for r in (target.get("rows") or [])
    ]
    return rows, f"梯队取自 ladder_daily_snapshot（快照日 {target.get('date')}）"


def _limit_pool_payload(db: Any, date: str, min_streak: int) -> dict[str, Any]:
    """limit.pool 取数（同步，调用方放 to_thread 之外也行 —— 语句全部带 LIMIT，毫秒级）。"""
    d = date or (_ymd((_read_one(db, "limit_date") or {}).get("d")))
    if not d:
        return {"as_of": "未知", "missing": ["limit_up_events 为空（尚未回填涨停事件）"]}

    counts = _read_one(db, "limit_counts", {"d": d}) or {}
    events = _read(db, "limit_events", {"d": d, "n": 500})
    strength = _read(db, "limit_strength", {"d": d, "n": 60})
    missing: list[str] = []
    if not events:
        missing.append(f"limit_up_events 在 {d} 无记录")
    if not strength:
        missing.append(f"limit_strength_shadow 在 {d} 无记录（影子表未跑或未迁移）")

    # 连板梯队：与题材情绪页同一算法（src.core.limit_ladder），连板数不信任 limit_days 列
    ladder_rows: list[dict[str, Any]] = []
    dates = sorted(_ymd(r.get("trade_date")) for r in _read(db, "limit_dates", {"n": 60}))
    dates = [x for x in dates if x]
    if d in dates and len(dates) >= 1:
        history = _read(db, "limit_history", {"a": dates[0], "n": 12000})
        try:
            from src.core.limit_ladder import ladder_window

            names = {str(e.get("symbol")): str(e.get("name") or "") for e in history if e.get("name")}
            window = ladder_window(dates, history, names)
            day = next((x for x in window if x.get("date") == d), None)
            ladder_rows = list((day or {}).get("rows") or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("连板梯队计算失败 %s: %s", d, exc)
            missing.append(f"连板梯队计算失败（{type(exc).__name__}）")
    else:
        ladder_rows, note = _ladder_from_snapshot(db, d)
        if not ladder_rows:
            missing.append(note)

    if min_streak > 1:
        ladder_rows = [r for r in ladder_rows if int(r.get("boards") or 0) >= min_streak]

    blown = [
        f"{e.get('symbol')} {e.get('name') or ''}".strip()
        for e in events
        if not e.get("is_sealed_close")
    ]
    strength_sorted = sorted(
        [r for r in strength if r.get("strength_label") or r.get("seal_ratio_grade")],
        key=lambda r: (r.get("seal_ratio") is None, -(r.get("seal_ratio") or 0)),
    )
    return {
        "as_of": d,
        "counts": counts,
        "ladder_rows": ladder_rows,
        "strength_rows": strength_sorted,
        "blown": blown,
        "missing": missing,
    }


def _theme_payload(db: Any, date: str, top_n: int) -> dict[str, Any]:
    """theme.mood 取数：theme_mood_daily 五维评分 + board_daily 板块资金（同板块代码对齐）。"""
    d = date or _ymd((_read_one(db, "theme_date") or {}).get("d"))
    if not d:
        return {"as_of": "未知", "missing": ["theme_mood_daily 为空（题材评分尚未跑或未迁移）"]}
    rows = _read(db, "theme_rows", {"d": d, "n": max(int(top_n or 20) * 2, 40)})
    missing: list[str] = []
    if not rows:
        missing.append(f"theme_mood_daily 在 {d} 无评分行")
        return {"as_of": d, "rows": [], "funds": {}, "missing": missing}
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    try:
        funds = {str(r.get("block_code")): r for r in _read(db, "theme_funds", {"d": iso, "n": 800})}
    except Exception as exc:  # noqa: BLE001 — 资金是附加块，缺了不影响评分主块
        logger.warning("board_daily 板块资金读取失败 %s: %s", d, exc)
        funds = {}
        missing.append(f"board_daily 板块资金读取失败（{type(exc).__name__}）")
    if not funds and not any("board_daily" in m for m in missing):
        missing.append(f"board_daily 在 {iso} 无板块资金行")
    return {"as_of": d, "rows": rows, "funds": funds, "missing": missing}


def _lhb_payload(db: Any, date: str, symbol: str) -> dict[str, Any]:
    d = date or (_ymd((_read_one(db, "lhb_date") or {}).get("d")))
    if not d:
        return {"as_of": "未知", "missing": ["dragon_tiger_events 为空（尚未回填龙虎榜）"]}
    if symbol:
        events = _read(db, "lhb_events_symbol", {"d": d, "s": symbol, "n": 30})
        seats = _read(db, "lhb_seats", {"d": d, "s": symbol, "n": 40})
    else:
        events = _read(db, "lhb_events", {"d": d, "n": 60})
        seats = _read(db, "lhb_seats_top", {"d": d, "n": 40})
    missing: list[str] = []
    if not events:
        missing.append(f"dragon_tiger_events 在 {d} 无记录" + (f"（symbol={symbol}）" if symbol else ""))
    if not seats:
        missing.append(f"lhb_seat_details 在 {d} 无席位明细" + (f"（symbol={symbol}）" if symbol else ""))
    return {"as_of": d, "events": events, "seats": seats, "missing": missing}


# ──────────────────────────── 统一入口 ────────────────────────────


def _parse_params(raw: Any) -> tuple[dict[str, Any], str]:
    """params_json → dict。坏 JSON/非对象一律拒绝（不猜模型想传什么）。"""
    if raw is None or raw == "" or raw == {}:
        return {}, ""
    if isinstance(raw, dict):
        return raw, ""
    try:
        obj = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}, "params_json 不是合法 JSON"
    if not isinstance(obj, dict):
        return {}, "params_json 必须是 JSON 对象"
    return obj, ""


async def query_data_domain(
    db: Session | None,
    args: dict[str, Any],
    user: User | None = None,
) -> str:
    """统一取数入口：按域白名单转发到既有工具 / 只读查询。"""

    domain = str((args or {}).get("domain") or "").strip()
    params, perr = _parse_params((args or {}).get("params_json"))
    if not domain:
        return (
            f"请提供数据域 id（如 stock.chip）。先调用 list_data_domains 看有哪些域，"
            f"当前共 {len(DOMAINS)} 个。"
        )
    if domain not in DOMAIN_IDS:
        return (
            f"未知数据域「{domain}」，请先调用 list_data_domains 从目录里选域"
            f"（共 {len(DOMAINS)} 个，不接受目录外的域，也不做 raw SQL）。"
        )
    if perr:
        return f"参数格式不对：{perr}。请传 JSON 对象字符串，如 \"{{\\\"symbol\\\": \\\"002361\\\"}}\"。"

    need = required_params(domain)
    absent = [k for k in need if not str(params.get(k) or "").strip()]
    if absent:
        return (
            f"数据域「{domain}」缺少必填参数 {('、'.join(absent))}，本次不取数。"
            f"请补到 params_json 里，如 \"{{\\\"{absent[0]}\\\": \\\"002361\\\"}}\"。"
        )

    handlers = {
        "market.sentiment": _query_market_sentiment,
        "limit.pool": None,  # 见下（自带只读 SQL）
        "lhb.daily": None,   # 见下（自带只读 SQL）
        "theme.mood": None,  # 见下（自带只读 SQL）
        "stock.chip": _query_stock_chip,
        "stock.flow": _query_stock_flow,
        "stock.intraday": _query_stock_intraday,
        "stock.factor": _query_stock_factor,
    }
    if domain not in handlers:
        return (
            f"数据域「{domain}」已在目录登记但尚未接入实现（P1 待接）—— 现在取不到，"
            f"不要用别的域或编数字顶替。可先查已接入的域："
            f"{'、'.join(sorted(k for k in handlers))}。"
        )

    try:
        if domain == "limit.pool":
            try:
                min_streak = max(int(params.get("min_streak") or 1), 1)
            except (TypeError, ValueError):
                min_streak = 1
            payload = _limit_pool_payload(db, _ymd(params.get("date")), min_streak)
            return render_limit_pool(
                as_of=payload.get("as_of") or "未知",
                counts=payload.get("counts"),
                ladder_rows=payload.get("ladder_rows"),
                strength_rows=payload.get("strength_rows"),
                blown=payload.get("blown"),
                missing=payload.get("missing"),
            )
        if domain == "lhb.daily":
            payload = _lhb_payload(db, _ymd(params.get("date")), str(params.get("symbol") or "").strip().upper())
            return render_lhb(
                as_of=payload.get("as_of") or "未知",
                events=payload.get("events"),
                seats=payload.get("seats"),
                missing=payload.get("missing"),
            )
        if domain == "theme.mood":
            try:
                top_n = min(max(int(params.get("top_n") or 20), 1), 40)
            except (TypeError, ValueError):
                top_n = 20
            payload = _theme_payload(db, _ymd(params.get("date")), top_n)
            return render_theme_mood(
                as_of=payload.get("as_of") or "未知",
                rows=payload.get("rows"),
                funds=payload.get("funds"),
                top_n=top_n,
                missing=payload.get("missing"),
            )
        return await handlers[domain](db, params, user)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 — 单域故障不能炸掉整轮对话
        logger.warning("数据域 %s 取数失败: %s", domain, exc)
        return render_domain_sections(
            domain,
            as_of="未知",
            source=(_DOMAIN_BY_ID.get(domain) or {}).get("source") or "未标注",
            sections=[],
            missing=[f"取数异常（{type(exc).__name__}: {str(exc)[:80]}）"],
        )


# ──────────────────────────── 注册（import 时登记，同 tools_thsdk） ────────────────────────────

_CATALOG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_data_domains",
        "description": (
            "列出平台可查询的**数据域目录**（26 个域：域id | 标签 | 必填参数 | 覆盖什么）。"
            "任何『平台有哪些数据可查 / 这个数据从哪来』的问题，或使用 query_data_domain 之前，"
            "都先调本工具拿目录，不要凭印象猜表名或域 id。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_data_domain",
        "description": (
            "**数据域统一取数入口**：传域 id + 参数，返回人话 Markdown（首行「域 / 时点 / 来源表」，"
            "末尾缺数据会写「未取到（原因）」）。"
            "已接入的域：market.sentiment（情绪周期）、limit.pool（涨停池/连板梯队/封板强度，可选 date=YYYYMMDD、min_streak）、"
            "lhb.daily（龙虎榜/席位，可选 symbol、date）、theme.mood（题材五维评分+板块资金，可选 top_n、date）、"
            "stock.chip（筹码/集中度，必填 symbol）、"
            "stock.flow（主力意图+撤单，必填 symbol）、stock.intraday（分时结构/盘口，必填 symbol）、"
            "stock.factor（因子分位，必填 symbol，可选 with_ic=true 追因子 IC）。"
            "其余域为 P1 待接，调用会明确提示未接入。域 id 一律先用 list_data_domains 查。"
            "返回的是客观读数与口径，不含买卖建议。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "域 id，如 stock.chip / limit.pool / lhb.daily（必须来自 list_data_domains）",
                },
                "params_json": {
                    "type": "string",
                    "description": "参数 JSON 对象字符串，如 {\"symbol\": \"002361\"} 或 {\"date\": \"20261020\", \"min_streak\": 2}；无参数留空",
                    "default": "",
                },
            },
            "required": ["domain"],
        },
    },
}


async def _tool_list_data_domains(db: Session | None, args: dict | None = None, user: User | None = None) -> str:
    return render_domain_catalog()


async def _tool_query_data_domain(db: Session | None, args: dict | None = None, user: User | None = None) -> str:
    return await query_data_domain(db, dict(args or {}), user)


def register_catalog_tools() -> None:
    """把两个数据域工具登记进 registry（import 时调用一次，重复注册由 registry 报错保证幂等）。"""
    from src.agents.chat.registry import register_chat_tool

    register_chat_tool(
        "list_data_domains",
        schema=_CATALOG_SCHEMA,
        caliber=f"只读目录：src/harness/data_catalog.yaml（{len(DOMAINS)} 域，封闭集合）",
        requires="",
    )(_tool_list_data_domains)
    register_chat_tool(
        "query_data_domain",
        schema=_QUERY_SCHEMA,
        caliber="数据域转发口径：优先复用既有工具 handler（与页面同源）；无工具域为只读 SELECT+LIMIT，缺数据写「未取到」不补 0",
        requires="",
    )(_tool_query_data_domain)


__all__ = [
    "DOMAINS",
    "DOMAIN_IDS",
    "MAX_CATALOG_CHARS",
    "MAX_DOMAIN_CHARS",
    "catalog_path",
    "load_domains",
    "query_data_domain",
    "register_catalog_tools",
    "render_domain_catalog",
    "render_domain_sections",
    "render_limit_pool",
    "render_lhb",
    "required_params",
]
