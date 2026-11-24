"""Dispatch explicitly supported read-only questions before model routing."""
from __future__ import annotations

import logging
from importlib import import_module
from inspect import iscoroutinefunction
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
    reader_failures: list[tuple[str, str]] = []
    for name in readers:
        try:
            # Load each optional reader independently. A broken import must not skip its siblings.
            reader = import_module(f"src.harness.{name}").answer
            if not iscoroutinefunction(reader):
                raise TypeError("reader entry point is not async")
        except Exception as exc:  # noqa: BLE001 — module initialization/export is a configuration failure
            reader_failures.append((name, "reader_configuration_error"))
            logger.error("[copilot.direct_answer] reader=%s configuration failed (%s)", name, type(exc).__name__)
            continue
        try:
            if result := await reader(db, text, bound, user, runtime):
                return result
        except Exception as exc:  # noqa: BLE001 — best-effort path; cancellation still propagates
            # Never log exception text: it can contain private holdings or connection details.
            reader_failures.append((name, "reader_execution_error"))
            logger.warning("[copilot.direct_answer] reader=%s failed (%s)", name, type(exc).__name__)
    # No reader recovered. Persist the failure signal so a model fallback without
    # evidence cannot leave the run recorded as a clean success. Keep routing/cancellation unchanged.
    trace = getattr(runtime, "trace", None)
    if trace is not None:
        for name, error in reader_failures:
            try:
                trace.step(name, ok=False, error=error)
            except Exception as exc:  # noqa: BLE001 — telemetry must not block fallback
                logger.warning("[copilot.direct_answer] reader=%s trace failed (%s)", name, type(exc).__name__)
    return None
