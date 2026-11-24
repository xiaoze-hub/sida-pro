"""Dispatch explicitly supported read-only questions before model routing."""
from __future__ import annotations

import logging
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User
    from src.harness.runtime import AgentRuntime


logger = logging.getLogger(__name__)


async def answer(db: Session, text: str, bound: str | None, user: User | None,
                 runtime: AgentRuntime | None, *, stock_question: bool,
                 allow_specialized: bool) -> str | None:
    readers = ("quick_answer",) if stock_question else ()
    if allow_specialized:
        readers += ("portfolio_risk", "institution_holdings")
    for name in readers:
        try:
            # Load each optional reader independently. A broken import must not skip its siblings.
            reader = import_module(f"src.harness.{name}").answer
            if result := await reader(db, text, bound, user, runtime):
                return result
        except Exception as exc:  # noqa: BLE001 — best-effort path; cancellation still propagates
            # Never log exception text: it can contain private holdings or connection details.
            logger.warning("[copilot.direct_answer] reader=%s failed (%s)", name, type(exc).__name__)
    return None
