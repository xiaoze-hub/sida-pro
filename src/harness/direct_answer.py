"""Dispatch explicitly supported read-only questions before model routing."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User
    from src.harness.runtime import AgentRuntime


async def answer(db: Session, text: str, bound: str | None, user: User | None,
                 runtime: AgentRuntime | None, *, stock_question: bool,
                 allow_specialized: bool) -> str | None:
    if stock_question:
        from src.harness.quick_answer import answer as quote_answer

        if result := await quote_answer(db, text, bound, user, runtime):
            return result
    if not allow_specialized:
        return None

    from src.harness import institution_holdings, portfolio_risk

    for reader in (portfolio_risk.answer, institution_holdings.answer):
        if result := await reader(db, text, bound, user, runtime):
            return result
    return None
