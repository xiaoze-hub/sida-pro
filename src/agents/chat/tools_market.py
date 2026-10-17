# -*- coding: utf-8 -*-
"""数智参谋（Copilot）专属 chat 工具（2026-10-18）。

定位：补齐现有 31 个 core 工具的**能力盲区**，而不是重做已有工具（见
`docs/research/AI数智参谋全链路落地设计方案.md` §1.2 的纠偏表）：

| 新工具 | 补的盲区 | 与邻居的分工 |
| :--- | :--- | :--- |
| `get_chip_accumulation` | 自算 CYQ 的 120 日波段底仓模型 | `get_stock_chips` = 客户端**公式**口径（SSRP/CYC/CYW），数字不可互算 |
| `get_cancel_order_intent` | TQ 会话级买卖双向撤单率 + 意图定性 | `get_orderbook` = 十档静态挂单，无撤单行为 |
| `get_wyckoff_structure` | 日 K 量价结构 + Phase A–E | `get_kline_patterns` = 29 种 K 线形态，非量价结构 |
| `get_demon_factor` | 妖股六维因子分 + 分级 + 身位 | `get_hot_stocks` / `get_opportunities` = 当日热度榜 |
| `scan_auction_anomaly` | 全市场竞价五态扫描 + 金额阈值过滤 | `get_auction_data` = 竞价池固定 scene |

统一约定（5 个工具全部遵守）：
- handler 签名 `async def _tool_x(db, args, user=None) -> str`，**永远返回人话字符串**
  （chat 的 `_execute_tool` 直接把返回值喂给 LLM）；
- 同步网络/DB 调用一律 `asyncio.to_thread`，不阻塞事件循环；
- 任何异常**不上抛**，`logger.warning` 打全 + 返回 `"...获取失败: {截断 100 字}"`；
- 首行固定 `[数据源: <口径>]`；无数据一律说「不猜」，**绝不返回 0 冒充真实值**。

注册顺序由 `register_market_tools()` 决定，**必须**在 `registry.py` 的 thsdk 段之前调用
（core 工具是导出清单的连续前缀，否则 `EXPECTED_CORE_ORDER` 的 index 断言会红）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User

logger = logging.getLogger(__name__)

# 注册顺序即导出顺序（registry 里按此顺序调用 register_chat_tool）。
MARKET_TOOL_ORDER: tuple[str, ...] = (
    "get_chip_accumulation",
    "get_cancel_order_intent",
    "get_wyckoff_structure",
    "get_demon_factor",
    "scan_auction_anomaly",
)


def _deny(user, db, perm: str) -> str | None:
    """权限收口（调用期懒 import，避免 tools_market ↔ registry 的模块级循环依赖）。"""
    from src.agents.chat.registry import _perm_denied

    return _perm_denied(user, db, perm)


def _symbol_of(args: dict) -> str:
    return str(args.get("symbol") or args.get("code") or "").strip().upper()


def _market_of(args: dict) -> str:
    return str(args.get("market") or "CN").strip().upper()


# ──────────────────────────── 1. 底仓筹码沉淀 ────────────────────────────

async def _tool_get_chip_accumulation(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _deny(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied

    symbol = _symbol_of(args)
    if not symbol:
        return "请提供股票代码(symbol)，如 600111 或 600111.SH。"
    market = _market_of(args)
    if market != "CN":
        return "底仓筹码沉淀模型仅支持 A 股(CN)。"
    try:
        lookback = max(60, min(int(args.get("lookback_days") or 120), 250))
    except (TypeError, ValueError):
        lookback = 120

    try:
        from src.core.chip_distribution import compute_main_accumulation_chips
        from src.core.decision_pioneer import fetch_bars

        # 多取 40 根：算法内部再截 lookback 根，前置窗口用于波段定位
        bars = await asyncio.to_thread(fetch_bars, symbol, market, lookback + 40)
        if not bars:
            return f"[底仓筹码] {symbol} 取不到日K（多源均失败），无法建模 —— 不猜数据。"
        if len(bars) < 30:
            return f"[底仓筹码] {symbol} 日K 仅 {len(bars)} 根（<30），样本不足，不做建模。"

        # compute_* 用属性访问读 bar；dict bar 的适配已落在函数内部（P0 修复），此处不再包一层
        r = await asyncio.to_thread(compute_main_accumulation_chips, bars, None, lookback)
        if not r:
            return f"[底仓筹码] {symbol} 筹码模型返回空（价格网格异常/成交量为 0），本次不给数值。"

        close = float(r.get("last_close") or 0.0)
        conc = r.get("concentration")
        conc_txt = f"{float(conc):.3f}" if conc is not None else "--"
        return "\n".join([
            f"[数据源: 自算CYQ·{r.get('lookback_days')}日底仓沉淀] {symbol} 现价 {close:.2f}",
            f"  底仓成本(COST50)：{r.get('main_cost'):.2f} 元 ｜ 筹码峰：{r.get('peak_price'):.2f} 元"
            f"（占比 {r.get('peak_ratio'):.1f}%）",
            f"  70% 筹码带：{r.get('cost_70_low'):.2f} – {r.get('cost_70_high'):.2f} 元 ｜ "
            f"90% 筹码带：{r.get('cost_90_low'):.2f} – {r.get('cost_90_high'):.2f} 元",
            f"  获利盘：{r.get('profit_ratio'):.1f}%（{r.get('profit_state')}） ｜ "
            f"集中度：{conc_txt}（{r.get('pattern')} / {r.get('status_label')}）",
            f"  现价相对底仓成本：{r.get('departure_pct'):+.2f}%",
            f"  模型结论：{r.get('action_advice')}",
            "  ⚠️ 口径：自算 CYQ（三角分布 + 换手率衰减），与 get_stock_chips 的客户端公式口径不同，"
            "数字不可互相校验。",
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_chip_accumulation 工具失败 [%s]: %s", symbol, e)
        return f"底仓筹码获取失败: {str(e)[:100]}"


# ──────────────────────────── 2. 盘口撤单意图 ────────────────────────────

# md_more_info / 收盘快照的归一化小写键 → calc_cancel_order_intent 吃的 TQ 原始键名。
# 这一步最容易漏：漏了就是"永远无数据"，而指标算法本身完全正常。
_CANCEL_KEY_MAP = (
    ("BCancel", "cancel_buy"),
    ("SCancel", "cancel_sell"),
    ("TotalBVol", "total_buy_vol"),
    ("TotalSVol", "total_sell_vol"),
    ("L2OrderNum", "l2_order_num"),
    ("L2TicNum", "l2_tick_num"),
)


async def _tool_get_cancel_order_intent(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_L2

    denied = _deny(user, db, PERM_VIEW_L2)
    if denied:
        return denied

    symbol = _symbol_of(args)
    if not symbol:
        return "请提供股票代码(symbol)，如 600111。"
    market = _market_of(args)
    if market != "CN":
        return "盘口撤单意图仅支持 A 股(CN)。"

    try:
        from src.core.cancel_order_intent import calc_cancel_order_intent
        from src.core.l2_fund_snapshot import FIELDS, has_live_values, latest_snapshot
        from src.core.marketdata_client import md_more_info

        rows = await asyncio.to_thread(md_more_info, [symbol], market)
        row = (rows[0] if rows else None) or {}
        source = "盘中实时会话值"

        # 会话值全 0（收盘/周末/停牌）→ 回退最近收盘快照，绝不把 0 当真实值
        live = {f: row.get(f) for f in FIELDS}
        if not row or not has_live_values(live):
            snap = await asyncio.to_thread(latest_snapshot, symbol, market)
            if not snap:
                return (
                    f"[盘口撤单] {symbol} 当前无会话值（非交易时段）且无收盘快照 —— "
                    "无数据，不伪造。"
                )
            row = snap
            source = f"收盘快照 {snap.get('trade_date') or '未知日期'}"

        mi = {tq_key: row.get(low_key) for tq_key, low_key in _CANCEL_KEY_MAP}
        r = calc_cancel_order_intent(mi, pct_chg=row.get("change_pct"))
        if not r.get("available"):
            return f"[盘口撤单] {symbol} 当日无有效撤单记录（{r.get('intent_desc')}）。"

        # 撤单率源数据是 0~1 小数，展示层 ×100，且只乘一次
        b = float(r["b_cancel_ratio"]) * 100
        s = float(r["s_cancel_ratio"]) * 100
        bias = float(r["cancel_bias"])
        return "\n".join([
            f"[数据源: TQ 撤单意图 · {source}] {symbol}",
            f"  买盘撤单率：{b:.1f}% ｜ 卖盘撤单率：{s:.1f}% ｜ 撤单偏向：{bias:+.2f}"
            f"（{'买方撤多' if bias > 0 else '卖方撤多'}）",
            f"  撤单活跃度：{float(r['activity_score']):.2f} ｜ 意图判定：{r['intent_tag']}"
            f"（{r['warning_level']}）",
            f"  说明：{r['intent_desc']}",
            "  ⚠️ 口径：撤单量 / (委托量 + 撤单量)，TQ 会话累计值；收盘后为最近快照，不代表次日盘口。",
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_cancel_order_intent 工具失败 [%s]: %s", symbol, e)
        return f"盘口撤单意图获取失败: {str(e)[:100]}"


# ──────────────────────────── 3. 威科夫量价结构 ────────────────────────────

async def _tool_get_wyckoff_structure(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _deny(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied

    symbol = _symbol_of(args)
    if not symbol:
        return "请提供股票代码(symbol)，如 600111。"
    market = _market_of(args)
    if market != "CN":
        return "威科夫量价结构仅支持 A 股(CN)。"
    try:
        lookback = max(30, min(int(args.get("lookback") or 60), 180))
    except (TypeError, ValueError):
        lookback = 60

    try:
        from src.core.decision_pioneer import fetch_bars
        from src.core.wyckoff import analyze_wyckoff

        bars = await asyncio.to_thread(fetch_bars, symbol, market, lookback + 30)
        if not bars:
            return f"[量价结构] {symbol} 取不到日K，无法识别结构（不猜）。"

        # analyze_wyckoff 是鸭子类型（_get 同时支持 dict / 对象），不需要 _BarView 适配
        res = await asyncio.to_thread(analyze_wyckoff, bars, lookback)
        v = res.get("verdict") or {}
        marks = res.get("markers") or []

        if v.get("stage") == "unknown" and not marks:
            return (
                f"[数据源: 日K量价结构] {symbol}：未识别到有效结构（{v.get('stage_label')}）。"
                f"{v.get('note') or ''}"
            )

        lines = [
            f"[数据源: 日K量价结构·威科夫] {symbol}",
            f"  阶段：{v.get('stage')} · {v.get('stage_label')}"
            f" ｜ 努力-结果：{v.get('effort_result')} ｜ 标记数：{v.get('markers_count')}",
        ]
        tail = marks[-5:]
        if tail:
            lines.append("  最近结构标记（新→旧）:")
            for m in reversed(tail):
                basis = "、".join(str(x) for x in (m.get("basis") or [])[:3])
                lines.append(
                    f"    {m.get('date')} {m.get('name')}（{m.get('direction')}）依据: {basis}"
                )
        lines.append(
            f"  说明：{v.get('note') or '仅日K量价结构描述，不含逐笔数据，不得作为买卖结论。'}"
        )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("get_wyckoff_structure 工具失败 [%s]: %s", symbol, e)
        return f"量价结构获取失败: {str(e)[:100]}"


# ──────────────────────────── 4. 妖股六维因子 ────────────────────────────

async def _tool_get_demon_factor(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_OPPORTUNITIES

    denied = _deny(user, db, PERM_VIEW_OPPORTUNITIES)
    if denied:
        return denied

    symbol = _symbol_of(args)
    if not symbol:
        return "请提供股票代码(symbol)，如 002361。"
    # 因子表按 6 位代码存储，接受 600111.SH 形式
    symbol = symbol.split(".")[0]

    try:
        from src.core.demon_factors import load_factor_pool

        # 只读 200 行因子表（毫秒级），比 demon_detail 现算（拉事件+龙虎榜，1~3s）快两个量级
        pool = await asyncio.to_thread(load_factor_pool, 200, 0)
        if not pool:
            return (
                "[妖股因子] 因子库为空（尚未跑过回填/重算）。"
                "请先执行妖股因子管线，本次不给数值 —— 不猜。"
            )
        idx = next((i for i, p in enumerate(pool) if str(p.get("symbol")) == symbol), None)
        if idx is None:
            return (
                f"[妖股因子] {symbol} 未进入因子库 Top200（近 260 日涨停事件不足或从未触发）。"
                "这不等于『不会妖』，只表示当前无统计样本。"
            )
        p = pool[idx]
        dims = p.get("dims") or {}
        dim_txt = " ｜ ".join(f"{k} {v}" for k, v in list(dims.items())[:6]) or "无分项"
        flags = "、".join(p.get("flags") or []) or "无"
        return "\n".join([
            f"[数据源: 妖股六维因子库·{p.get('factor_date') or '最新'}] {symbol} {p.get('name') or ''}",
            f"  综合分：{p.get('total')} ｜ 分级：{p.get('grade') or '未分级'} ｜ "
            f"池内排名：第 {idx + 1}/200（{'Top50 内' if idx < 50 else 'Top50 外'}）",
            f"  连板身位：最高 {p.get('max_streak') or 0} 连板 ｜ 涨停事件：{p.get('n_events') or 0} 次"
            f"（封住 {p.get('n_sealed') or 0} 次） ｜ 资金参与度：{p.get('participation')}",
            f"  分项：{dim_txt}",
            f"  标签：{flags}",
            "  ⚠️ 口径：涨停事件统计画像（近 260 日），非当日热度、非推荐。",
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("get_demon_factor 工具失败 [%s]: %s", symbol, e)
        return f"妖股因子获取失败: {str(e)[:100]}"


# ──────────────────────────── 5. 全市场竞价异动 ────────────────────────────

_AUCTION_LEVELS = ("strong_buy", "buy", "warning", "risk", "neutral", "all")


def _fmt_auction_row(i: int, r: dict) -> list[str]:
    """单条竞价异动 → 1~2 行文本（缺字段就不写，绝不补 0）。"""
    amt_raw = r.get("open_amount")
    amt_txt = "竞价额 --" if amt_raw is None else f"竞价额 {float(amt_raw) / 1e4:,.0f} 万"
    gap = r.get("gap_pct")
    head = (
        f"  {i}. {r.get('symbol')} {r.get('name') or ''} ｜ {r.get('verdict_tag')} "
        f"｜ {amt_txt}"
    )
    if gap is not None:
        head += f" ｜ 竞价涨幅 {float(gap):+.2f}%"
    lines = [head]

    extra = []
    vr = r.get("volume_ratio")
    wr = r.get("withdraw_rate")
    if vr is not None:
        extra.append(f"量比 {float(vr):.2f}")
    if wr is not None:
        extra.append(f"撤单率 {float(wr):.1f}%")
    if r.get("open_zt_buy"):
        extra.append(f"涨停委买 {(r['open_zt_buy']) / 1e4:,.0f} 万")
    if extra:
        lines.append(f"      {' ｜ '.join(extra)}")
    return lines


async def _tool_scan_auction_anomaly(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_AUCTION

    denied = _deny(user, db, PERM_VIEW_AUCTION)
    if denied:
        return denied

    level = str(args.get("level") or "buy")
    if level not in _AUCTION_LEVELS:
        level = "buy"
    try:
        min_amt = float(args.get("min_open_amount") or 0)
    except (TypeError, ValueError):
        min_amt = 0.0
    try:
        limit = max(1, min(int(args.get("limit") or 10), 30))
    except (TypeError, ValueError):
        limit = 10

    try:
        from src.core.auction_pool import fetch_auction_anomaly

        # 自带 30s 进程缓存 + TQ 逐票增强；全市场冷启动约 20~60s
        rows = await asyncio.to_thread(fetch_auction_anomaly, "CN")
        if not rows:
            return (
                "[竞价异动] 当前无竞价异动记录：可能非 9:15–9:25 时段、非交易日，"
                "或 thsdk 竞价源未就绪。无数据，不猜。"
            )
        picked = [
            r for r in rows
            if (level == "all" or r.get("verdict_level") == level)
            and (r.get("open_amount") or 0) >= min_amt
        ]
        picked.sort(key=lambda r: (r.get("open_amount") or 0, r.get("gap_pct") or -99), reverse=True)
        picked = picked[:limit]

        if not picked:
            return (
                f"[竞价异动] 无满足条件（level={level}, 竞价额≥{min_amt / 1e4:.0f}万）的标的；"
                f"全池 {len(rows)} 条。可放宽阈值或换 level=all。"
            )

        lines = [
            f"[数据源: 9:25 竞价异动池] 命中 {len(picked)} 只（level={level}，全池 {len(rows)} 条）:"
        ]
        for i, r in enumerate(picked, 1):
            lines.extend(_fmt_auction_row(i, r))
        lines.append(
            "  ⚠️ 口径：竞价成交额=TQ OpenAmo(元)；量比=OpenVolPre1(相对前1日量)；"
            "撤单率=BCancel/(TotalBVol+BCancel)。竞价强弱≠日内走势。"
        )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("scan_auction_anomaly 工具失败: %s", e)
        return f"竞价异动扫描失败: {str(e)[:100]}"


# ──────────────────────────── 注册 ────────────────────────────

_CHIP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_chip_accumulation",
        "description": (
            "个股【底仓筹码沉淀模型】(自算 CYQ, 120 日窗口): 底仓成本 COST(50)、筹码峰价位与占比、"
            "70%/90% 筹码带、获利盘比例、集中度、形态(单峰密集/缓步沉淀/筹码发散)、现价相对底仓成本的脱离度。"
            "用户问「主力底仓在什么位置」「底仓成本多少」「筹码集中不集中」「获利盘多少」「跌到哪算破位」时调用。"
            "⚠️ 本工具是**自算 CYQ 口径**，与 get_stock_chips 的客户端公式口径(SSRP/CYC/CYW)不同，"
            "两者数字不可互相校验；同一问题只用一个口径回答并标注。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "6 位股票代码，如 600111 或 600111.SH"},
                "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
                "lookback_days": {
                    "type": "integer",
                    "description": "回溯交易日数，默认 120（可 60~250）",
                    "default": 120,
                },
            },
            "required": ["symbol"],
        },
    },
}

_CANCEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_cancel_order_intent",
        "description": (
            "个股【盘口撤单意图】: 买盘撤单率、卖盘撤单率、撤单偏向(-1~1)、撤单活跃度，"
            "并按广发金工口径定性为「⚠️托单诱多 / 🛡️压盘吸筹 / 买单虚脱 / 抛压减退 / 频繁试盘 / 多空均衡」。"
            "用户问「买方是不是在挂假单/托单出货」「卖压重不重」「盘口撤单多不多」「挂单是真的吗」时调用。"
            "收盘后会话值归零 → 自动回退最近收盘快照并标注日期。"
            "⚠️ 与 get_orderbook(十档挂单队列)不同: 本工具看的是**撤单行为**，不是静态挂单。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "6 位股票代码，如 600111"},
                "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
            },
            "required": ["symbol"],
        },
    },
}

_WYCKOFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_wyckoff_structure",
        "description": (
            "个股【威科夫量价结构】: 阶段判定(A 止跌构建 / B 筹码沉淀 / C 终极测试 / D-E 突破趋势 / unknown 区间)"
            "与四类结构标记——放量滞涨(effort_divergence)、弹簧试探(spring_probe)、缩量沉淀(dry_up)、"
            "放量突破(markup_break)，含每条的触发依据与日期。用户问「现在是洗盘还是出货」「有没有弹簧针/终极洗盘」"
            "「放量滞涨了吗」「缩量沉淀到位没」「突破有效吗」时调用。"
            "⚠️ 只描述日K量价结构，**不是**方向结论；方向请配合 get_main_intent / get_decision_pioneer。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "6 位股票代码，如 600111"},
                "market": {"type": "string", "description": "市场，仅 CN", "default": "CN"},
                "lookback": {"type": "integer", "description": "回看 bar 数，默认 60", "default": 60},
            },
            "required": ["symbol"],
        },
    },
}

_DEMON_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_demon_factor",
        "description": (
            "个股【妖股因子】: 六维综合分(total)、分级(极妖/妖/活跃)、连板身位(max_streak)、"
            "近 260 日涨停事件数/封板数、资金参与度、各维度分项(dims)与标签(flags)，"
            "以及是否在妖股先锋 Top50 内及其排名。"
            "用户问「这票妖不妖」「妖股基因怎么样」「连板空间/身位」「在不在妖股池里」「为什么它能成妖」时调用。"
            "⚠️ 因子来自历史涨停事件统计，是对『妖性』的画像，**不是**推荐。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "6 位股票代码，如 002361"},
            },
            "required": ["symbol"],
        },
    },
}

_AUCTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "scan_auction_anomaly",
        "description": (
            "【全市场集合竞价异动扫描】: 按五态筛选 9:25 竞价异动股 —— "
            "strong_buy(👑涨停抢筹) / buy(🔥爆量抢筹、🛡️弱转强) / warning(⚠️诱多试盘) / "
            "risk(⚡恐慌砸盘) / neutral(常态试盘)，并给出竞价真实成交额(元)、竞价量比、竞价撤单率、涨停委买额。"
            "用户问「今天竞价谁在爆量抢筹」「有哪些票竞价异动」「竞价撤单率高的票」「谁被核了」时调用。"
            "⚠️ 与 get_auction_data(竞价池固定场景)不同: 本工具可按五态与金额阈值过滤并排序。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "description": "strong_buy / buy / warning / risk / neutral / all(默认 buy)",
                    "default": "buy",
                },
                "min_open_amount": {
                    "type": "number",
                    "description": "竞价成交额下限(元)，默认 0；常见阈值 1e7=1000万、2e7=2000万",
                    "default": 0,
                },
                "limit": {"type": "integer", "description": "返回条数，默认 10，最多 30", "default": 10},
            },
            "required": [],
        },
    },
}

# (name, schema, caliber, handler) —— 顺序即注册/导出顺序
MARKET_TOOLS: tuple[tuple[str, dict, str, Any], ...] = (
    (
        "get_chip_accumulation",
        _CHIP_SCHEMA,
        "自算CYQ(120日·三角分布+换手衰减)，底仓成本=COST(50)；与客户端公式口径(SSRP/CYC)不可混算",
        _tool_get_chip_accumulation,
    ),
    (
        "get_cancel_order_intent",
        _CANCEL_SCHEMA,
        "TQ会话撤单量 BCancel/(TotalBVol+BCancel)，盘中实时；收盘回退最近快照",
        _tool_get_cancel_order_intent,
    ),
    (
        "get_wyckoff_structure",
        _WYCKOFF_SCHEMA,
        "日K量价结构(放量1.8×/突破1.5×/缩量0.6×/弹簧回收≥1%)，不含逐笔，非方向结论",
        _tool_get_wyckoff_structure,
    ),
    (
        "get_demon_factor",
        _DEMON_SCHEMA,
        "妖股六维因子库(涨停事件驱动: 连板/封板率/参与度/龙虎榜/流通盘/题材)，非推荐",
        _tool_get_demon_factor,
    ),
    (
        "scan_auction_anomaly",
        _AUCTION_SCHEMA,
        "9:25 竞价异动池 + TQ OpenAmo/OpenVolPre1/BCancel(真实撤单率)",
        _tool_scan_auction_anomaly,
    ),
)


def register_market_tools() -> None:
    """把 5 个数智参谋工具按固定顺序登记进 registry（幂等由 registry 的重复注册报错保证）。

    在 `registry.py` 的 thsdk 段**之前**调用 —— core 工具必须是导出清单的连续前缀。
    """
    from src.agents.chat.registry import register_chat_tool

    for name, schema, caliber, handler in MARKET_TOOLS:
        register_chat_tool(name, schema=schema, caliber=caliber, requires="")(handler)


__all__ = [
    "MARKET_TOOL_ORDER",
    "MARKET_TOOLS",
    "register_market_tools",
    "_tool_get_chip_accumulation",
    "_tool_get_cancel_order_intent",
    "_tool_get_wyckoff_structure",
    "_tool_get_demon_factor",
    "_tool_scan_auction_anomaly",
]
