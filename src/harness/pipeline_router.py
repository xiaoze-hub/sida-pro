"""管线路由器（2026-10-23）：按配方 `scope` 选符号管线或全市场管线。

为什么要有这一层：`chat.py` 有行数护栏（当前 2307 / 上限 2310），只够改 **import 一行**。
但两条管线的入口签名不同（全市场管线不吃 `bound`、要 `watchlist`），把这些分歧塞进
chat.py 就会破护栏。所以路由逻辑落在这里，chat.py 的调用点一个字都不用动。

判据只有一条：`recipe.scope == "market"` → 走 `pipeline_market`；其余走符号管线。
`scope` 非法/缺失时 `recipes._parse_scope` 已经退回 `"symbol"`，所以这里不需要再兜底。
"""
from __future__ import annotations

from src.harness import pipeline as _sym
from src.harness import pipeline_market as _mkt

# watchlist 展开上限：与 pipeline_market.MAX_SYMBOLS 同源语义（那边单标的 8 只封顶）。
MAX_WATCHLIST = 8


# 上下文透传口径（2026-11-07 修缺口）：`chat.py` 对两条管线是同一次调用，会递
# `context` / `fact_pack` / `evidence_sink` / `view_state` / `db` / `user`。路由**不许**
# 因为分支不同就静默丢参数 —— 全市场分支此前只转 `watchlist`/`context`，另外两个
# 参数掉了，于是全市场结论缺证据，委派时缺少父 run 冻结时点的提示。
# 新增的时间约束仍是提示词，并不校验工具数据是否晚于该时点。现在按各分支
# 真的会消费的集合分发：
#   - 两条都消费：`context` / `fact_pack` / `evidence_sink` / `trace` / `market`。
#   - 只有符号管线消费：`db` / `user`（`propose_levels` 写关键位）与 `view_state`
#     （单标的页面锚点）。`db`/`user` 在全市场分支由 `resolve_watchlist` 消费完，
#     不再下传；`view_state` 在全市场分支没有那一个标的，下传就是死形参。


def is_market_scope(recipe) -> bool:
    """这份配方是不是全市场/无标的口径。"""
    return (getattr(recipe, "scope", "symbol") or "symbol").lower() == "market"


def resolve_watchlist(db, user) -> list[str]:
    """本轮可用的自选代码（用户级 + 全局，去重、上限 8）。

    全市场管线用它把「需要 symbol 的工具」展开成每票一次。取不到就是空表 ——
    空表是有意义的信号（pipeline_market 会记 `no_symbol_scope` 并让结论写「未取到」），
    绝不能拿持仓或指数顶替（那等于替用户编一个观察对象）。
    """
    if db is None:
        return []
    try:
        from sqlalchemy import or_

        from src.db.models import Stock

        q = db.query(Stock)
        uid = getattr(user, "id", None)
        if uid is not None:
            q = q.filter(or_(Stock.user_id == uid, Stock.user_id.is_(None)))
        out: list[str] = []
        for row in q.order_by(Stock.sort_order.asc()).limit(200).all():
            sym = str(getattr(row, "symbol", "") or "").strip()
            if sym and sym not in out:
                out.append(sym)
            if len(out) >= MAX_WATCHLIST:
                break
        return out
    except Exception:  # noqa: BLE001 - 自选读不到就当没有，不许因此打断作答
        return []


async def answer(ai_client, recipe, question: str, bound, executor, *,
                 market: str = "CN", trace=None, context: str = "",
                 db=None, user=None, view_state: dict | None = None,
                 evidence_sink: list | None = None,
                 fact_pack: dict | None = None):
    """非流式入口，签名与 `pipeline.answer` 完全一致（调用方无需知道有两条管线）。"""
    if is_market_scope(recipe):
        return await _mkt.answer_market(
            ai_client, recipe, executor, question, market=market, trace=trace,
            watchlist=resolve_watchlist(db, user), context=context,
            fact_pack=fact_pack, evidence_sink=evidence_sink,
        )
    return await _sym.answer(
        ai_client, recipe, question, bound, executor, market=market, trace=trace,
        context=context, db=db, user=user, view_state=view_state,
        evidence_sink=evidence_sink,
        fact_pack=fact_pack,
    )


async def answer_stream(ai_client, recipe, question: str, bound, executor, *,
                        market: str = "CN", trace=None, context: str = "",
                        db=None, user=None, view_state: dict | None = None,
                        evidence_sink: list | None = None,
                        fact_pack: dict | None = None):
    """流式入口：全市场管线没取到任何维度时它自己 return（调用方回落多轮 loop）。"""
    if is_market_scope(recipe):
        async for ev in _mkt.answer_market_stream(
            ai_client, recipe, executor, question, market=market, trace=trace,
            watchlist=resolve_watchlist(db, user), context=context,
            fact_pack=fact_pack, evidence_sink=evidence_sink,
        ):
            yield ev
        return
    async for ev in _sym.answer_stream(
        ai_client, recipe, question, bound, executor, market=market, trace=trace,
        context=context, db=db, user=user, view_state=view_state,
        evidence_sink=evidence_sink,
        fact_pack=fact_pack,
    ):
        yield ev
