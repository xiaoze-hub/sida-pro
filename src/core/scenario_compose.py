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

from typing import Any


def owner_cost(db: Any, user: Any, symbol: str, market: str = "CN") -> tuple[float | None, float | None]:
    """取本人的 (成本价, 纪律止损价)。没有持仓/没设止损就返回 None —— 不编默认止损。

    实盘读 `positions.cost_price`（经 stocks 表按 symbol 关联）；模拟盘读
    `PaperTradingPosition.entry_price` / `stop_loss`（它自带止损列，正好是「纪律位」）。
    实盘优先：同一只票两边都有时，用户心理账户锚在实盘成本上。
    """
    sym = str(symbol or "").strip().upper()
    if db is None or not sym:
        return None, None
    uid = getattr(user, "id", None)

    try:
        from sqlalchemy import or_

        from src.db.models import Position, Stock

        rows = db.query(Position, Stock).join(Stock, Position.stock_id == Stock.id).filter(
            Stock.symbol == sym
        )
        if uid is not None:
            rows = rows.filter(or_(Position.user_id == uid, Position.user_id.is_(None)))
        row = rows.first()
        if row is not None:
            pos = row[0] if isinstance(row, tuple) else row
            cost = getattr(pos, "cost_price", None)
            if cost:
                return float(cost), None      # 实盘没有纪律止损列 → 不猜
    except Exception:  # noqa: BLE001 - 持仓读不到就当没有，不影响矩阵
        pass

    try:
        from sqlalchemy import or_

        from src.db.models import PaperTradingPosition

        q = db.query(PaperTradingPosition).filter(
            PaperTradingPosition.stock_symbol == sym,
            PaperTradingPosition.status == "open",
        )
        if uid is not None:
            q = q.filter(
                or_(
                    PaperTradingPosition.user_id == uid,
                    PaperTradingPosition.user_id.is_(None),
                )
            )
        pp = q.first()
        if pp is not None:
            cost = getattr(pp, "entry_price", None)
            stop = getattr(pp, "stop_loss", None)
            return (float(cost) if cost else None, float(stop) if stop else None)
    except Exception:  # noqa: BLE001
        pass
    return None, None


def build_for_tool(db: Any, symbol: str, market: str = "CN", *, user: Any = None) -> str:
    """工具入口：矩阵正文 + （有持仓时）成本锚定 + 相似形态频次。"""
    from src.core import analog_finder, scenario_cost_anchor, scenario_engine

    sym = str(symbol or "").strip().upper()
    if not sym:
        return "请提供股票代码(symbol)，如 002050。"

    matrix = scenario_engine.build_matrix(db, sym, market)
    text = scenario_engine.render(matrix)

    if matrix and matrix.get("available"):
        cost, stop = owner_cost(db, user, sym, market)
        if cost:
            try:
                enriched = scenario_cost_anchor.enrich_with_cost(
                    matrix, cost_price=cost, stop_price=stop
                )
                # enrich_with_cost 返回「矩阵 + cost_anchor 子块」；渲染取的是子块本身
                anchor = (enriched or {}).get("cost_anchor") or {}
                extra = scenario_cost_anchor.render_cost_anchor(anchor)
                if extra:
                    text = f"{text}\n\n{extra}"
            except Exception:  # noqa: BLE001 - 附加段失败不影响矩阵本身
                pass

    try:
        analog = analog_finder.build(sym, market)
        if analog:
            text = f"{text}\n\n{analog}"
    except Exception:  # noqa: BLE001
        pass
    return text
