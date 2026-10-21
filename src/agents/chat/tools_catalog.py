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



# ──────────────────────────── P1 六域的渲染（同样：缺数据写「未取到」，禁补 0） ────────────────────────────

#: 指数显示名（库内 symbol 是腾讯原始符号 sh000001；index_daily 用裸码 000001）
_IDX_LABELS: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
    "000688": "科创50",
}

_REGIME_LABELS = {"bullish": "偏多", "neutral": "中性", "bearish": "偏空"}


def _f(v: Any) -> float | None:
    """松散转 float；None/空/非数 → None（调用方据此说「未披露」，不补 0）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct_of(price: Any, prev: Any) -> float | None:
    """由 (现价, 昨收) 反算涨跌幅（%）；缺任一侧或昨收为 0 → None。"""
    p, q = _f(price), _f(prev)
    if p is None or q is None or q == 0:
        return None
    return (p - q) / q * 100.0


def _json_list(raw: Any) -> list[Any]:
    """JSON 文本/已是 list → list；解析失败 → []（不猜）。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return obj if isinstance(obj, list) else []
    return []


def render_market_index(
    *,
    as_of: str,
    quotes: list[dict[str, Any]] | None = None,
    source: str = "quote_snapshots（market=IDX）/ index_daily",
    missing: list[str] | None = None,
) -> str:
    """大盘指数与量能：指数点位/涨跌幅 + 两市成交额。空结果 → 「未取到」。"""
    items = [q for q in (quotes or []) if isinstance(q, dict)]
    miss = list(missing or [])
    sections: list[str] = []
    if items:
        parts: list[str] = []
        amounts: dict[str, float] = {}
        for q in items[:8]:
            code = str(q.get("code") or "")
            label = str(q.get("name") or _IDX_LABELS.get(code) or code or "?")
            bits = [_num(q.get("price"), 2), _pct(q.get("pct"))]
            amt = _f(q.get("amount"))
            if amt is None:
                bits.append("成交额 未披露")
            else:
                amounts[code] = amt
                bits.append(f"成交额 {_amt_plain(amt)}")
            parts.append(" ".join([f"{label}（{code}）"] + bits))
        sections.append("- " + "；".join(parts))
        sh, sz = amounts.get("000001"), amounts.get("399001")
        if sh is not None and sz is not None:
            sections.append(
                f"- 两市成交额 {_amt_plain(sh + sz)}（沪 {_amt_plain(sh)} + 深 {_amt_plain(sz)}；"
                f"口径=上证指数与深证成指成交额字段相加）"
            )
        else:
            miss.append("两市成交额：缺上证或深证成指成交额（不补 0）")
    return render_domain_sections(
        "market.index", as_of=as_of, source=source, sections=sections, missing=miss
    )


def render_market_breadth(
    *,
    as_of: str,
    series_text: str = "",
    dist: dict[str, Any] | None = None,
    regime: dict[str, Any] | None = None,
    source: str = "market_breadth_daily（复用 market_breadth_series）/ breadth_distribution_snapshot / market_regime_snapshots",
    missing: list[str] | None = None,
) -> str:
    """市场广度：涨跌家数与 ADL 分位 + 涨跌幅九档分布 + 市场状态。空 → 「未取到」。"""
    miss = list(missing or [])
    sections: list[str] = []
    if series_text.strip():
        sections.append(series_text.strip())

    d = dist or {}
    valid, total = _f(d.get("valid")), _f(d.get("total"))
    buckets = [
        b for b in _json_list(d.get("items")) if isinstance(b, dict) and b.get("bucket") is not None
    ]
    if buckets and (valid or 0) > 0:
        parts = [f"{b.get('bucket')} {int(_f(b.get('count')) or 0)}" for b in buckets]
        sections.append(
            f"- 涨跌幅九档分布（有效 {int(valid or 0)} / 样本 {int(total or 0)}，"
            f"采集 {d.get('trade_date') or '未标注'}，源 {d.get('source') or '未标注'}）："
            + "｜".join(parts)
        )
    elif d:
        miss.append(
            f"breadth_distribution_snapshot 最近一行有效样本为 {int(valid or 0)}"
            f"（源不可用，不补 0）"
        )

    r = regime or {}
    if r:
        score, conf = _f(r.get("regime_score")), _f(r.get("confidence"))
        bits = [
            f"状态 {_REGIME_LABELS.get(str(r.get('regime') or ''), r.get('regime') or '未标注')}",
            f"打分 {_num(score, 2, signed=True)}",
            f"置信 {_num(conf, 2)}",
        ]
        for key, label in (
            ("breadth_up_pct", "上涨占比"),
            ("avg_change_pct", "平均涨幅"),
            ("volatility_pct", "波动率"),
        ):
            v = _f(r.get(key))
            bits.append(f"{label} {_pct(v) if v is not None else '未披露'}")
        active = _f(r.get("active_ratio"))
        bits.append(f"活跃度 {_num(active, 2)}")
        sample = _f(r.get("sample_size"))
        bits.append(f"样本 {int(sample) if sample is not None else '未披露'}")
        sections.append(f"- 市场状态（{r.get('snapshot_date') or '未标注'}）：" + "｜".join(bits))
    return render_domain_sections(
        "market.breadth", as_of=as_of, source=source, sections=sections, missing=miss
    )


def _fmt_board_row(r: dict[str, Any]) -> str:
    name = str(r.get("name") or r.get("block_code") or "?")
    btype = str(r.get("board_type") or "")
    tag = {"industry": "行业", "concept": "概念"}.get(btype, btype or "未分类")
    bits = [f"{name}（{tag}）", _pct(r.get("change_pct")), f"净流入 {_amt(r.get('fund_net'))}"]
    vol = _f(r.get("volume"))
    if vol is not None:
        bits.append(f"成交额 {_amt_plain(vol)}")
    return " ".join(bits)


def render_board_daily(
    *,
    as_of: str,
    inflow: list[dict[str, Any]] | None = None,
    outflow: list[dict[str, Any]] | None = None,
    flow: dict[str, Any] | None = None,
    top_n: int = 6,
    missing: list[str] | None = None,
) -> str:
    """板块行情与资金：净流入/净流出排行 + 盘中板块资金快照。空 → 「未取到」。"""
    miss = list(missing or [])
    sections: list[str] = []
    ins = [r for r in (inflow or []) if isinstance(r, dict)]
    outs = [r for r in (outflow or []) if isinstance(r, dict)]
    if ins:
        sections.append(f"- 资金净流入前列（{as_of}）：" + "；".join(_fmt_board_row(r) for r in ins[:top_n]))
    if outs:
        sections.append(
            f"- 资金净流出前列（仅列净流出）："
            + "；".join(_fmt_board_row(r) for r in [x for x in outs if (_f(x.get("fund_net")) or 0) < 0][:top_n])
        )
    f = flow or {}
    f_items = [i for i in _json_list(f.get("payload")) if isinstance(i, dict)]
    if f_items:
        ranked = sorted(
            f_items, key=lambda i: (_f(i.get("net_inflow")) is None, -(_f(i.get("net_inflow")) or 0))
        )[:3]
        parts = []
        for i in ranked:
            leader = str(i.get("leader_name") or "").strip()
            parts.append(
                f"{i.get('board_name') or '?'} {_pct(i.get('change_pct'))} 净流入 "
                f"{_amt((_f(i.get('net_inflow')) or 0) * 1e8 if _f(i.get('net_inflow')) is not None else None)}"
                + (f" 龙头 {leader} {_pct(i.get('leader_change_pct'))}" if leader else "")
            )
        sections.append(
            f"- 盘中板块资金快照（{f.get('ts') or '未标注'}，{f.get('item_count') or len(f_items)} 个板块）："
            + "；".join(parts)
        )
    elif flow:
        miss.append("board_flow_snapshots 有行但 payload 为空/解析失败")
    return render_domain_sections(
        "board.daily",
        as_of=as_of,
        source="board_daily + boards / board_flow_snapshots",
        sections=sections,
        missing=miss,
    )


def render_auction_flow(
    *,
    as_of: str,
    snapshot: dict[str, Any] | None = None,
    ticks: list[dict[str, Any]] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    extra: list[str] | None = None,
    missing: list[str] | None = None,
) -> str:
    """集合竞价：9:25 快照 + 逐帧撮合 + 全市场竞价异动池。空 → 「未取到」。"""
    miss = list(missing or [])
    sections: list[str] = [s for s in (extra or []) if str(s).strip()]
    s = snapshot or {}
    if s:
        bits = [f"竞价价 {_num(s.get('auction_price'), 2)}", f"开盘 {_num(s.get('open_price'), 2)}"]
        gap = _f(s.get("open_pct"))
        if gap is None:
            gap = _f(s.get("gap_pct"))
        bits.append(f"开盘涨幅 {_pct(gap)}")
        amt = _f(s.get("open_amount"))
        bits.append(f"竞价成交额 {_amt_plain(amt) if amt is not None else '未披露'}")
        limit_buy = _f(s.get("open_limit_buy"))
        bits.append(f"一字买量 {_num(limit_buy, 0) if limit_buy is not None else '未披露'}")
        wr = _f(s.get("withdraw_rate_pre0920"))
        bits.append(f"9:20 前撤单率 {_pct(wr * 100 if wr is not None and abs(wr) <= 1 else wr) if wr is not None else '未披露'}")
        direction = str(s.get("direction") or "").strip()
        if direction:
            bits.append(f"方向 {direction}")
        sections.append(
            f"- 9:25 竞价快照（{s.get('trade_date') or '未标注'}，源 {s.get('source') or '未标注'}）："
            + "｜".join(bits)
        )
    tick_rows = [t for t in (ticks or []) if isinstance(t, dict)]
    if tick_rows:
        parts = []
        for t in tick_rows[:3]:
            v = str(t.get("verdict") or "未判定")
            parts.append(
                f"{t.get('tick_ts') or '?'} 虚拟价 {_num(t.get('ref_price'), 2)} "
                f"匹配 {_num(t.get('match_vol'), 0)} 手 未匹配 {_num(t.get('unmatched_vol'), 0)} 手 "
                f"撤单率 {_num(t.get('withdraw_rate'), 3)} → {v}"
            )
        sections.append("- 竞价逐帧（最近 " + str(len(tick_rows[:3])) + " 帧）：" + "；".join(parts))
    anom = [a for a in (anomalies or []) if isinstance(a, dict)]
    if anom:
        parts = []
        for a in anom[:8]:
            bits = [f"{a.get('symbol') or '?'} {a.get('name') or ''}".strip(), f"高开 {_pct(a.get('gap_pct'))}"]
            wr = _f(a.get("withdraw_rate"))
            bits.append(f"撤单率 {_num(wr, 3)}")
            bits.append(f"量比 {_num(a.get('volume_ratio'))}")
            note = str(a.get("note") or "").strip()
            if note:
                bits.append(note[:20])
            parts.append(bits[0] + "（" + "，".join(bits[1:]) + "）")
        sections.append(f"- 竞价异动池（{len(anom)} 条）：" + "；".join(parts))
    return render_domain_sections(
        "auction.flow",
        as_of=as_of,
        source="auction_snapshots / auction_tick_snapshots / auction_anomaly_records（可选转发 get_auction_data）",
        sections=sections,
        missing=miss,
    )


def render_stock_quote(
    *,
    as_of: str,
    quote: dict[str, Any] | None = None,
    age_sec: float | None = None,
    extra: list[str] | None = None,
    source: str = "quote_snapshots（1 分钟桶，market=CN）",
    missing: list[str] | None = None,
) -> str:
    """实时报价：库内最新一行（附行龄）或转发实时报价工具后的文本。空 → 「未取到」。"""
    sections: list[str] = [s for s in (extra or []) if str(s).strip()]
    q = quote or {}
    if q:
        bits = [
            f"{q.get('symbol') or '?'} {q.get('name') or ''}".strip(),
            f"最新 {_num(q.get('last'), 2)}",
            _pct(q.get("change_pct")),
        ]
        for key, label in (("open", "开"), ("high", "高"), ("low", "低"), ("prev_close", "昨收")):
            v = _f(q.get(key))
            if v is not None:
                bits.append(f"{label} {v:.2f}")
        vol = _f(q.get("volume"))
        if vol is not None:
            bits.append(f"量 {vol / 1e4:.0f}万手")
        amt = _f(q.get("turnover"))
        bits.append(f"额 {_amt_plain(amt) if amt is not None else '未披露'}")
        tr = _f(q.get("turnover_rate"))
        bits.append(f"换手 {_pct(tr) if tr is not None else '未披露'}")
        vr = _f(q.get("volume_ratio"))
        bits.append(f"量比 {_num(vr) if vr is not None else '未披露'}")
        if age_sec is not None:
            bits.append(f"数据时点 {q.get('ts') or '未标注'}（{age_sec:.0f}s 前）")
        sections.append("- " + "｜".join(bits))
    return render_domain_sections(
        "stock.quote", as_of=as_of, source=source, sections=sections, missing=missing
    )


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
    # —— market.index（quote_snapshots 的指数桶 market='IDX'，日线表兜底）——
    "idx_snapshot_ts": (
        "SELECT trade_date AS d, ts FROM quote_snapshots WHERE market = :m "
        "ORDER BY trade_date DESC, ts DESC LIMIT 1"
    ),
    "idx_snapshot": (
        "SELECT symbol, name, last, prev_close, change_pct, volume, turnover, source "
        "FROM quote_snapshots WHERE market = :m AND trade_date = :d AND ts = :ts "
        "ORDER BY symbol LIMIT :n"
    ),
    "idx_daily_date": "SELECT MAX(trade_date) AS d FROM index_daily WHERE market = :m LIMIT 1",
    "idx_daily": (
        "SELECT code, name, close, prev_close, volume, amount, source FROM index_daily "
        "WHERE trade_date = :d AND market = :m ORDER BY code LIMIT :n"
    ),
    # —— market.breadth ——
    "breadth_dist": (
        "SELECT trade_date, captured_at, total, valid, items, source FROM breadth_distribution_snapshot "
        "ORDER BY trade_date DESC, captured_at DESC LIMIT 1"
    ),
    "regime": (
        "SELECT snapshot_date, market, regime, regime_score, confidence, breadth_up_pct, "
        "avg_change_pct, volatility_pct, active_ratio, sample_size FROM market_regime_snapshots "
        "WHERE market = :m ORDER BY snapshot_date DESC LIMIT 1"
    ),
    # —— board.daily ——
    "board_date": "SELECT MAX(date) AS d FROM board_daily LIMIT 1",
    "board_inflow": (
        "SELECT d.block_code, d.change_pct, d.fund_net, d.volume, b.name, b.board_type "
        "FROM board_daily d LEFT JOIN boards b ON b.block_code = d.block_code "
        "WHERE d.date = :d AND d.fund_net IS NOT NULL ORDER BY d.fund_net DESC LIMIT :n"
    ),
    "board_outflow": (
        "SELECT d.block_code, d.change_pct, d.fund_net, d.volume, b.name, b.board_type "
        "FROM board_daily d LEFT JOIN boards b ON b.block_code = d.block_code "
        "WHERE d.date = :d AND d.fund_net IS NOT NULL ORDER BY d.fund_net ASC LIMIT :n"
    ),
    "board_flow": (
        "SELECT ts, board_type, item_count, payload FROM board_flow_snapshots "
        "WHERE board_type = :bt ORDER BY ts DESC LIMIT 1"
    ),
    # —— auction.flow ——
    "auction_snapshot": (
        "SELECT trade_date, symbol, market, source, auction_price, open_price, open_pct, open_amount, "
        "open_limit_buy, direction, gap_pct, auction_high, auction_low, withdraw_rate_pre0920 "
        "FROM auction_snapshots WHERE symbol = :s AND market = :m ORDER BY trade_date DESC LIMIT :n"
    ),
    "auction_ticks": (
        "SELECT trade_date, tick_ts, ref_price, match_vol, unmatched_vol, bid1_price, ask1_price, "
        "open_amount, open_pct, cancel_cum, withdraw_rate, verdict FROM auction_tick_snapshots "
        "WHERE symbol = :s AND trade_date = :d ORDER BY tick_ts DESC LIMIT :n"
    ),
    "auction_anomalies": (
        "SELECT symbol, name, gap_pct, withdraw_rate, volume_ratio, note, created_at "
        "FROM auction_anomaly_records ORDER BY created_at DESC LIMIT :n"
    ),
    # —— stock.quote（库优先，过旧才转发实时工具）——
    "quote_latest": (
        "SELECT trade_date, ts, symbol, market, name, last, open, high, low, prev_close, change_pct, "
        "volume, turnover, turnover_rate, volume_ratio, source FROM quote_snapshots "
        "WHERE symbol = :s AND market = :m ORDER BY trade_date DESC, ts DESC LIMIT 1"
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


def _query_market_index(db: Any, params: dict[str, Any], user: Any) -> str:
    """market.index 渲染（纯只读库，无外呼）。"""
    p = _index_payload(db)
    return render_market_index(
        as_of=p.get("as_of") or "未知",
        quotes=p.get("quotes"),
        source=p.get("source") or "quote_snapshots / index_daily",
        missing=p.get("missing"),
    )


def _query_market_breadth(db: Any, params: dict[str, Any], user: Any) -> str:
    """market.breadth 渲染（ADL 序列复用 market_breadth_series，与情绪工具同口径）。"""
    p = _breadth_payload(db)
    return render_market_breadth(
        as_of=p.get("as_of") or "未知",
        series_text=p.get("series_text") or "",
        dist=p.get("dist"),
        regime=p.get("regime"),
        missing=p.get("missing"),
    )


def _query_board_daily(db: Any, params: dict[str, Any], user: Any) -> str:
    """board.daily 渲染（board_daily × boards + board_flow_snapshots 盘中快照）。"""
    try:
        top_n = min(max(int(params.get("top_n") or 6), 1), 15)
    except (TypeError, ValueError):
        top_n = 6
    p = _board_payload(db, top_n)
    return render_board_daily(
        as_of=p.get("as_of") or "未知",
        inflow=p.get("inflow"),
        outflow=p.get("outflow"),
        flow=p.get("flow"),
        top_n=top_n,
        missing=p.get("missing"),
    )


async def _query_stock_fundamental(db: Any, params: dict[str, Any], user: Any) -> str:
    """stock.fundamental：转发 get_fundamentals_detail（与页面同一 handler，不重写）。"""
    symbol = str(params.get("symbol") or "").strip().upper()
    market = str(params.get("market") or "CN").strip().upper() or "CN"
    out = await _delegate("get_fundamentals_detail", db, {"symbol": symbol, "market": market}, user)
    sections = [out] if out.strip() else []
    return render_domain_sections(
        "stock.fundamental",
        as_of=_now_cn(),
        source="复用 get_fundamentals_detail（dragon_tiger_events + marketdata 基本面 vendor：两融/股东户数/分红/事件）",
        sections=sections,
        missing=[] if sections else ["get_fundamentals_detail 未返回内容（两融/分红/股东户数 vendor 不可用？）"],
    )


async def _query_auction_flow(db: Any, params: dict[str, Any], user: Any) -> str:
    """auction.flow：库内 9:25 快照优先；库空才转发竞价池工具现取（避免无谓外呼）。"""
    symbol = str(params.get("symbol") or "").strip().upper()
    try:
        limit = min(max(int(params.get("limit") or 10), 1), 30)
    except (TypeError, ValueError):
        limit = 10
    missing: list[str] = []
    extra: list[str] = []
    snapshot: dict[str, Any] | None = None
    ticks: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    as_of = _now_cn()

    if symbol:
        rows = _read(db, "auction_snapshot", {"s": symbol, "m": "CN", "n": 3})
        snapshot = rows[0] if rows else None
        if snapshot:
            as_of = str(snapshot.get("trade_date") or "") or as_of
            ticks = _read(db, "auction_ticks", {"s": symbol, "d": as_of, "n": 5})
            if not ticks:
                missing.append(f"auction_tick_snapshots 在 {as_of} 无 {symbol} 逐帧记录")
        else:
            out = await _delegate(
                "get_auction_data", db, {"scene": "symbols", "symbols": [symbol], "limit": 1}, user
            )
            if out.strip():
                extra.append(out)
            else:
                missing.append(
                    f"auction_snapshots 无 {symbol} 记录，get_auction_data 也未返回"
                    f"（9:25 前当日竞价未生成？）"
                )
    else:
        anomalies = _read(db, "auction_anomalies", {"n": limit})
        if anomalies:
            as_of = str(anomalies[0].get("created_at") or "")[:16] or as_of
        else:
            out = await _delegate(
                "scan_auction_anomaly", db, {"level": "buy", "limit": limit}, user
            )
            if out.strip():
                extra.append(out)
            else:
                missing.append(
                    "auction_anomaly_records 无记录，scan_auction_anomaly 也未返回（竞价未开始或源不可用）"
                )
    return render_auction_flow(
        as_of=as_of,
        snapshot=snapshot,
        ticks=ticks,
        anomalies=anomalies,
        extra=extra,
        missing=missing,
    )


async def _query_stock_quote(db: Any, params: dict[str, Any], user: Any) -> str:
    """stock.quote：库内最新一行 ≤60s 直读；否则转发现有报价工具（过期快照只作标注，不冒充实时）。"""
    symbol = str(params.get("symbol") or "").strip().upper()
    row = _read_one(db, "quote_latest", {"s": symbol, "m": "CN"})
    age = _quote_age_sec(row)
    if row and age is not None and -5 <= age <= 60:
        return render_stock_quote(
            as_of=str(row.get("ts") or _now_cn()),
            quote=row,
            age_sec=age,
            source="quote_snapshots（market=CN，1 分钟桶；行龄 ≤60s，库优先）",
        )

    out = await _delegate("get_stock_quote", db, {"symbol": symbol, "market": "CN"}, user)
    if out.strip():
        return render_stock_quote(
            as_of=_now_cn(),
            extra=[out],
            source="复用 get_stock_quote（库内最新行 "
            + (f"距今 {age:.0f}s，超过 60s" if age is not None else "缺失/时点不可解析")
            + "，转实时报价）",
        )
    if row:
        return render_stock_quote(
            as_of=str(row.get("ts") or "未知"),
            quote=row,
            age_sec=age,
            source="quote_snapshots（过期快照，实时工具无返回）",
            missing=[
                f"quote_snapshots 最新行距今 {age:.0f}s（>60s 视为不新鲜），get_stock_quote 也未返回内容"
            ]
            if age is not None
            else ["quote_snapshots 行时点无法解析，get_stock_quote 也未返回内容"],
        )
    return render_stock_quote(
        as_of="未知",
        source="quote_snapshots / 转发 get_stock_quote",
        missing=[f"quote_snapshots 无 {symbol} 行，get_stock_quote 也未返回内容"],
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


def _index_payload(db: Any) -> dict[str, Any]:
    """market.index 取数：quote_snapshots 指数桶（同 ts 全量）优先，index_daily 日线兜底。"""
    snap = _read_one(db, "idx_snapshot_ts", {"m": "IDX"})
    if snap and snap.get("d") and snap.get("ts"):
        rows = _read(db, "idx_snapshot", {"m": "IDX", "d": snap["d"], "ts": snap["ts"], "n": 12})
        quotes: list[dict[str, Any]] = []
        for r in rows:
            sym = str(r.get("symbol") or "")
            code = sym[-6:] if sym.lower().startswith(("sh", "sz")) else sym
            pct = _f(r.get("change_pct"))
            if pct is None:
                pct = _pct_of(r.get("last"), r.get("prev_close"))
            quotes.append(
                {
                    "code": code,
                    "name": r.get("name") or _IDX_LABELS.get(code),
                    "price": r.get("last"),
                    "pct": pct,
                    "amount": r.get("turnover"),
                }
            )
        if quotes:
            ts = str(snap.get("ts") or "")
            return {
                "as_of": f"{_ymd(snap.get('d'))} {ts[-8:]}".strip(),
                "quotes": quotes,
                "missing": [],
                "source": "quote_snapshots（market=IDX，1 分钟桶）",
            }

    d = _ymd((_read_one(db, "idx_daily_date", {"m": "CN"}) or {}).get("d"))
    if d:
        rows = _read(db, "idx_daily", {"d": d, "m": "CN", "n": 12})
        quotes = []
        for r in rows:
            code = str(r.get("code") or "")
            pct = _pct_of(r.get("close"), r.get("prev_close"))
            quotes.append(
                {
                    "code": code,
                    "name": r.get("name") or _IDX_LABELS.get(code),
                    "price": r.get("close"),
                    "pct": pct,
                    "amount": r.get("amount"),
                }
            )
        if quotes:
            return {
                "as_of": d,
                "quotes": quotes,
                "missing": ["盘中量能读不到：quote_snapshots 无 IDX 行，以下为 index_daily 日线收盘值"],
                "source": "index_daily（日线兜底，非盘中快照）",
            }
    return {
        "as_of": "未知",
        "quotes": [],
        "missing": ["quote_snapshots 无 market=IDX 行，index_daily 也无指数日线（采集未跑？）"],
        "source": "quote_snapshots / index_daily",
    }


def _breadth_payload(db: Any) -> dict[str, Any]:
    """market.breadth 取数：ADL 序列（复用 market_breadth_series）+ 九档分布 + 市场状态。"""
    missing: list[str] = []
    series_text = ""
    series_date = ""
    try:
        from src.collectors.market_breadth_series import latest_with_percentile, render_text

        _db = db
        if _db is None:  # 工具被无会话调用时自建（测试/脚本路径），生产上永远拿得到 db
            from src.db.session import SessionLocal

            _db = SessionLocal()
        summary = latest_with_percentile(_db)
        if summary.get("ok"):
            series_text = render_text(summary) or ""
            series_date = str(summary.get("trade_date") or "")[:10]
        else:
            missing.append(
                f"market_breadth_daily 无可用序列（{summary.get('reason') or 'no_data'}，"
                f"需先跑 sync_breadth_series）"
            )
    except Exception as exc:  # noqa: BLE001 — 广度序列是主块，但缺了不影响另外两块
        logger.warning("广度序列读取失败: %s", exc)
        missing.append(f"广度序列读取失败（{type(exc).__name__}）")

    dist = _read_one(db, "breadth_dist") or {}
    if not dist:
        missing.append("breadth_distribution_snapshot 无快照行（九档分布未采集）")
    regime = _read_one(db, "regime", {"m": "CN"}) or {}
    if not regime:
        missing.append("market_regime_snapshots 无 CN 行（市场状态未生成）")

    as_of = (
        str(dist.get("trade_date") or "")[:10]
        or series_date
        or str(regime.get("snapshot_date") or "")[:10]
    )
    return {"as_of": as_of or "未知", "series_text": series_text, "dist": dist, "regime": regime,
            "missing": missing}


def _board_payload(db: Any, top_n: int) -> dict[str, Any]:
    """board.daily 取数：board_daily × boards 净流入/净流出 + board_flow_snapshots 盘中快照。"""
    d = _ymd((_read_one(db, "board_date") or {}).get("d"))
    if not d:
        return {
            "as_of": "未知", "inflow": [], "outflow": [], "flow": {},
            "missing": ["board_daily 为空（板块日行情尚未落库）"],
        }
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    missing: list[str] = []
    inflow = _read(db, "board_inflow", {"d": iso, "n": top_n})
    outflow = _read(db, "board_outflow", {"d": iso, "n": top_n})
    if not inflow and not outflow:
        missing.append(f"board_daily 在 {iso} 无带资金额的板块行")
    if inflow and not any(str(r.get("name") or "").strip() for r in inflow):
        missing.append("boards 未同步（板块名缺失，只能用 block_code）")

    flow_row = _read_one(db, "board_flow", {"bt": "industry"}) or {}
    flow: dict[str, Any] = {}
    if flow_row:
        items = _json_list(flow_row.get("payload"))
        flow = {
            "ts": flow_row.get("ts"),
            "board_type": flow_row.get("board_type"),
            "item_count": flow_row.get("item_count"),
            "payload": items,
        }
        if not items:
            missing.append("board_flow_snapshots 最近一行 payload 解析为空")
    else:
        missing.append("board_flow_snapshots 无 industry 快照（盘中板块资金采样未跑）")
    return {"as_of": d, "inflow": inflow, "outflow": outflow, "flow": flow, "missing": missing}


def _quote_age_sec(row: dict[str, Any] | None) -> float | None:
    """quote_snapshots.ts（CST 墙钟）距现在的秒数；解析失败 → None（当作不新鲜）。"""
    ts = str((row or {}).get("ts") or "").strip()
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts, fmt).replace(tzinfo=_CST)
        except ValueError:
            continue
        return (datetime.now(_CST) - dt).total_seconds()
    return None



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
        "market.index": _query_market_index,
        "market.breadth": _query_market_breadth,
        "board.daily": _query_board_daily,
        "stock.fundamental": _query_stock_fundamental,
        "auction.flow": _query_auction_flow,
        "stock.quote": _query_stock_quote,
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
        fn = handlers[domain]
        result = fn(db, params, user)  # type: ignore[misc]
        if asyncio.iscoroutine(result):
            result = await result
        return result
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
            "market.index（大盘指数点位/涨跌幅/两市成交额）、market.breadth（涨跌家数+ADL/ADR/ARMS 分位+九档分布+市场状态）、"
            "board.daily（板块涨跌与资金净流入排行，可选 top_n）、stock.fundamental（财务/股东/两融/分红，必填 symbol）、"
            "auction.flow（集合竞价 9:25 快照/逐帧/异动池，可选 symbol、limit）、"
            "stock.quote（实时报价，必填 symbol，库内行超过 60s 转实时取数）、"
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
    "render_auction_flow",
    "render_board_daily",
    "render_domain_catalog",
    "render_domain_sections",
    "render_limit_pool",
    "render_lhb",
    "render_market_breadth",
    "render_market_index",
    "render_stock_quote",
    "required_params",
]
