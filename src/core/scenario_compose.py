"""情景矩阵的工具级组装（2026-10-23）：矩阵 + 本人成本锚定 + 相似形态频次。

为什么要这一层：`scenario_engine` 只算「市场对所有人一样的支撑/阻力」，而用户真正关心的是
「跌破这条线对我的仓位意味着什么」。同时「这个形态历史上出现过多少次、之后怎样」是
纯确定性统计，比模型复述指标有用得多。两者都需要 DB 与 user，只有工具层有 —— 所以组装
落在这里，让 `registry.py` 里的 handler 保持两行（它所在文件在 core 段中间，改动越少越好）。

三条纪律：
1. **成本锚定只在真有该股持仓时出现**：没有持仓就不打印「无持仓成本」这句噪音 ——
   诊断陌生票时多一行无用文案，用户会以为系统在凑字数。
2. **相似形态永远按统计口径说话**：`n` 不足就只说样本不足，绝不把 5 个近邻当赔率。
3. 任何一段失败只影响自己：矩阵算不出来时整段照旧返回「不硬编情景」；附加段失败就跳过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.numutil import finite_float


@dataclass(frozen=True)
class CostAnchor:
    """成本锚定的**证据**: 不只给一个数, 还给"这个数是怎么来的"。

    2026-10-29 三审(R6): 旧 `owner_cost` 用 `db.query(...).first()` 在多账户场景下
    **随便挑一行**当成本, 且 `if cost:` 判定放过 NaN(`bool(nan) is True`) —— 于是
    "14.28 的成本"可能来自任意一个账户, 甚至是个 NaN。现在:

    - 只采纳**有限且 > 0** 的成本; 脏值单独计数(`unpriced`), 不参与计算也不当 0;
    - 多个账户成本相同(容差内) → method=`single`(就是这一个成本, 无歧义);
    - 多个账户成本不同 → method=`weighted`, 按**持仓量加权**, 并保留 `accounts`
      逐账户明细供文案"逐账户"展示 —— 用户能看见差异, 系统不替他挑一个;
    - 完全没有可用成本但有持仓行 → method=`unknown`, 不做成本锚定(不编)。
    """

    cost: float | None = None
    stop: float | None = None
    method: str = "none"  # single | weighted | paper | unknown | none
    accounts: list[dict[str, Any]] = field(default_factory=list)
    unpriced: int = 0

    @property
    def has_cost(self) -> bool:
        return self.cost is not None


def owner_cost_detail(
    db: Any, user: Any, symbol: str, market: str = "CN"
) -> CostAnchor:
    """取本人的 (成本价, 纪律止损价) 及来源证据。没有持仓 → CostAnchor()(不编默认止损)。

    实盘读 `positions.cost_price`（经 stocks 表按 symbol 关联); 模拟盘读
    `PaperTradingPosition.entry_price` / `stop_loss`。实盘优先: 同一只票两边都有时,
    用户心理账户锚在实盘成本上。

    作用域: **账户可见 AND 持仓可见**(与其它持仓口径同源), 不看别人的成本。
    """
    sym = str(symbol or "").strip().upper()
    mkt = str(market or "CN").strip().upper() or "CN"
    if db is None or not sym:
        return CostAnchor()
    uid = getattr(user, "id", None)

    try:
        from src.core.portfolio_scope import (
            account_user_scope_clause,
            position_user_scope_clause,
        )
        from src.db.models import Account, Position, Stock

        # 只取 Position 实体: `db.query(Position, Stock).first()` 返回的是 Row,
        # SQLAlchemy 2.0 的 Row 不是 tuple, 旧的 `isinstance(row, tuple)` 判定会落到
        # "整行当持仓"分支, 于是真实库上成本锚定一直是 None(2026-10-29 实测)。
        # 注: 不加 .order_by() —— 顺序在 Python 侧定, 也便于最小 DB 替身复用。
        rows = (
            db.query(Position)
            .join(Stock, Position.stock_id == Stock.id)
            .join(Account, Account.id == Position.account_id)
            .filter(
                Stock.symbol == sym,
                # 2026-10-29 四审: 必须同一市场 —— 同 symbol 的 CN/HK/US 行是不同币种,
                # 混在一起加权等于把不同货币的成本平均(数学上无意义, 业务上误导)。
                Stock.market == mkt,
                account_user_scope_clause(uid),
                position_user_scope_clause(uid),
            )
            .all()
        )
        priced: list[dict[str, Any]] = []
        unpriced = 0
        for row in rows or []:
            # 只采纳**有效正持仓**: quantity 必须有限且 > 0。已清仓(qty=0)/脏数量
            # 的行不是当前持仓, 不能拿它的成本去锚定(旧实现 `qty<=0 → 权重 1`
            # 会把已清仓账户的成本混进加权, 直接算错)。
            qty = finite_float(getattr(row, "quantity", None))
            if qty is None or qty <= 0:
                continue
            raw_cost = getattr(row, "cost_price", None)
            cost = finite_float(raw_cost)
            if cost is None or cost <= 0:
                # NaN/Inf/0/负数: 脏数据, 当"成本未知", 不参与也不当 0
                unpriced += 1
                continue
            priced.append(
                {
                    "position_id": getattr(row, "id", None),
                    "account_id": getattr(row, "account_id", None),
                    "cost": round(cost, 6),
                    # 到这里 qty 一定是有限正数(上面已筛掉), 直接当权重。
                    "quantity": qty,
                }
            )
        if priced:
            distinct = {round(p["cost"], 6) for p in priced}
            if len(distinct) == 1:
                return CostAnchor(
                    cost=priced[0]["cost"],
                    stop=None,  # 实盘没有纪律止损列 → 不猜
                    method="single",
                    accounts=priced,
                    unpriced=unpriced,
                )
            total_qty = sum(p["quantity"] for p in priced)
            if total_qty <= 0:
                # 数量异常(理论上不会到这里) → 不能加权, 报 unknown(不随手挑一个)
                return CostAnchor(
                    cost=None, stop=None, method="unknown", accounts=priced, unpriced=unpriced
                )
            weighted = sum(p["cost"] * p["quantity"] for p in priced) / total_qty
            w = finite_float(weighted)
            if w is None:
                return CostAnchor(
                    cost=None, stop=None, method="unknown", accounts=priced, unpriced=unpriced
                )
            return CostAnchor(
                cost=round(w, 6),
                stop=None,
                method="weighted",
                accounts=priced,
                unpriced=unpriced,
            )
        if unpriced:
            # 有该股**有效持仓**(qty>0)但成本全是脏值 → 明确"未知", 不回落模拟盘
            return CostAnchor(cost=None, stop=None, method="unknown", unpriced=unpriced)
    except Exception:  # noqa: BLE001 - 持仓读不到就当没有，不影响矩阵
        pass

    try:
        from sqlalchemy import or_

        from src.db.models import PaperTradingPosition

        # 2026-10-30 复审(OCR security): 没有会话(user=None)就不读任何人的模拟盘 ——
        # 旧写法不筛归属, `q.first()` 会挑到任意用户的开仓, 把别人的成本当成"该股成本"。
        # 有会话时只认本人持仓(NULL 归属 = 归属未知, 不自动授予可见)。
        if uid is None:
            return CostAnchor()
        rows = (
            db.query(PaperTradingPosition)
            .filter(
                PaperTradingPosition.stock_symbol == sym,
                # 同 symbol 不同市场的模拟盘是不同标的, 不能互相锚定。
                PaperTradingPosition.stock_market == mkt,
                PaperTradingPosition.status == "open",
                PaperTradingPosition.quantity > 0,
                or_(
                    PaperTradingPosition.user_id == uid,
                    PaperTradingPosition.user_id.is_(None),
                ),
            )
            .all()
        )
        # 同标的多笔开仓是常态(无唯一约束): 数量可证时按数量加权,
        # 不能再"随便挑一行"导致与组合平均成本不一致。
        lots: list[tuple[float, float | None]] = []
        for r in rows:
            c = finite_float(getattr(r, "entry_price", None))
            if c is None or c <= 0:
                continue
            lots.append((c, finite_float(getattr(r, "quantity", None))))
        if lots:
            weighted: float | None = None
            if all(q is not None and q > 0 for _, q in lots):
                total_qty = sum(q for _, q in lots if q is not None)
                if total_qty > 0:
                    weighted = finite_float(
                        sum(c * q for c, q in lots if q is not None) / total_qty
                    )
            elif len(lots) == 1:
                weighted = lots[0][0]  # 数量缺失的老数据: 只有一条时成本仍可证
            if weighted is not None:
                stop = (
                    finite_float(getattr(rows[0], "stop_loss", None)) if len(rows) == 1 else None
                )
                return CostAnchor(
                    cost=round(weighted, 6),
                    stop=stop if (stop is not None and stop > 0) else None,
                    method="paper",
                    unpriced=0,
                )
        # 有开仓但入场价全是脏值 → 明确"未知", 与实盘分支同口径, 不静默当没持仓。
        if rows:
            return CostAnchor(cost=None, stop=None, method="unknown", unpriced=len(rows))
    except Exception:  # noqa: BLE001
        pass
    return CostAnchor()


def owner_cost(db: Any, user: Any, symbol: str, market: str = "CN") -> tuple[float | None, float | None]:
    """兼容视图: (成本价, 纪律止损价)。证据(逐账户/加权/未知)见 `owner_cost_detail`。"""
    a = owner_cost_detail(db, user, symbol, market)
    return a.cost, a.stop



def _cost_evidence_note(a: CostAnchor) -> str:
    """多账户/加权时的口径说明(单账户/模拟盘返回空串, 不制造噪音)。"""
    if a.method != "weighted":
        return ""
    per = "、".join(
        f"账户#{p.get('account_id')} {p.get('cost'):.2f}"
        for p in a.accounts
        if p.get("cost") is not None
    )
    return (
        f"> 成本口径：你在这只票上有 {len(a.accounts)} 个账户、成本不一致，"
        f"下方锚点按**持仓量加权**得到 {a.cost:.2f}（逐账户：{per}）—— 不是某一个账户的原始成本。\n"
    )


def build_for_tool(db: Any, symbol: str, market: str = "CN", *, user: Any = None) -> str:
    """工具入口：矩阵正文 + （有持仓时）成本锚定 + 相似形态频次。"""
    from src.core import analog_finder, scenario_cost_anchor, scenario_engine

    sym = str(symbol or "").strip().upper()
    if not sym:
        return "请提供股票代码(symbol)，如 002050。"

    matrix = scenario_engine.build_matrix(db, sym, market)
    text = scenario_engine.render(matrix)

    if matrix and matrix.get("available"):
        anchor_ev = owner_cost_detail(db, user, sym, market)
        cost, stop = anchor_ev.cost, anchor_ev.stop
        if cost:
            try:
                enriched = scenario_cost_anchor.enrich_with_cost(
                    matrix, cost_price=cost, stop_price=stop
                )
                # enrich_with_cost 返回「矩阵 + cost_anchor 子块」；渲染取的是子块本身
                anchor = (enriched or {}).get("cost_anchor") or {}
                extra = scenario_cost_anchor.render_cost_anchor(anchor)
                if extra:
                    # 多账户成本不一致时必须说清"这个锚是怎么算的", 否则用户会以为
                    # 系统读的是某个账户的真实成本(2026-10-29 三审 R6)。
                    note = _cost_evidence_note(anchor_ev)
                    text = f"{text}\n\n{note}{extra}" if note else f"{text}\n\n{extra}"
            except Exception:  # noqa: BLE001 - 附加段失败不影响矩阵本身
                pass
        elif anchor_ev.method == "unknown":
            # 有持仓但成本无效 → 不锚定, 但要如实说一句(否则用户以为系统漏了)。
            text = (
                f"{text}\n\n> 该股有持仓记录, 但成本价无效或缺失"
                f"（{anchor_ev.unpriced} 条），本轮不做成本锚定。"
            )

    try:
        analog = analog_finder.build(sym, market)
        if analog:
            text = f"{text}\n\n{analog}"
    except Exception:  # noqa: BLE001
        pass
    return text
