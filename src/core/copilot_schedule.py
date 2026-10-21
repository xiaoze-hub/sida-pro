"""会话里创建的定时任务（2026-10-21, P0）。

场景：「每天 9:25 帮我看竞价异动」「收盘后给我一段小结」。

实现取舍：
- **不做动态 APScheduler 注册**：注册在内存里，重启就丢、删任务还有竞态。
  改成每分钟扫一次 `copilot_schedules` 表，命中 `at_hhmm` 且当天没跑过就执行。
- 只支持两类低风险任务：`auction_digest`（竞价摘要）与 `daily_brief`（收盘小结）。
  下单、改持仓永远不在这里（与 actions 的红线一致）。
- 结果写成一条 `assistant` 消息进会话，用户打开面板就能看到；找不到会话就新建一个。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from src.db.models import ChatConversation, ChatMessage, CopilotSchedule

logger = logging.getLogger(__name__)

KINDS = ("auction_digest", "daily_brief")
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _match_weekday(weekdays: str, today: date) -> bool:
    spec = (weekdays or "mon-fri").lower()
    if spec in ("daily", "everyday", "all"):
        return True
    key = _WEEKDAY_KEYS[today.weekday()]
    if "-" in spec and len(spec.split("-")) == 2:
        a, b = spec.split("-")
        if a in _WEEKDAY_KEYS and b in _WEEKDAY_KEYS:
            i, j = _WEEKDAY_KEYS.index(a), _WEEKDAY_KEYS.index(b)
            return i <= today.weekday() <= j
    return key in [x.strip() for x in spec.split(",")]


def create(db, user_id, *, kind: str, at_hhmm: str,
           weekdays: str = "mon-fri", conversation_id: int | None = None,
           payload: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"不支持的任务类型：{kind}（可选 {', '.join(KINDS)}）")
    hh, _, mm = (at_hhmm or "").partition(":")
    if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
        raise ValueError("时间格式应为 HH:MM，例如 09:25")
    row = CopilotSchedule(
        user_id=str(user_id), kind=kind, at_hhmm=f"{int(hh):02d}:{int(mm):02d}",
        weekdays=weekdays or "mon-fri", conversation_id=conversation_id,
        payload=payload or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def list_all(db, user_id) -> list[dict]:
    rows = (
        db.query(CopilotSchedule)
        .filter(CopilotSchedule.user_id == str(user_id))
        .order_by(CopilotSchedule.id.desc()).limit(50).all()
    )
    return [_row_dict(r) for r in rows]


def set_enabled(db, user_id, sched_id: int, enabled: bool) -> bool:
    row = (db.query(CopilotSchedule)
           .filter(CopilotSchedule.id == sched_id, CopilotSchedule.user_id == str(user_id))
           .one_or_none())
    if row is None:
        return False
    row.enabled = bool(enabled)
    db.commit()
    return True


def delete(db, user_id, sched_id: int) -> bool:
    row = (db.query(CopilotSchedule)
           .filter(CopilotSchedule.id == sched_id, CopilotSchedule.user_id == str(user_id))
           .one_or_none())
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _row_dict(r: CopilotSchedule) -> dict:
    return {
        "id": r.id, "kind": r.kind, "at_hhmm": r.at_hhmm, "weekdays": r.weekdays,
        "conversation_id": r.conversation_id, "enabled": bool(r.enabled),
        "payload": r.payload or {}, "last_run_date": r.last_run_date,
        "created_at": str(r.created_at or ""),
    }


# ── 执行 ────────────────────────────────────────────────────────────────

def run_due(now: datetime | None = None) -> dict:
    """每分钟调一次：跑所有"到点且今天没跑过"的任务。返回统计（供日志/自检）。"""
    from src.db.session import SessionLocal

    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    hhmm = now.strftime("%H:%M")
    stats = {"tick": hhmm, "ran": 0, "failed": 0, "skipped": 0}
    db = SessionLocal()
    try:
        rows = (
            db.query(CopilotSchedule)
            .filter(CopilotSchedule.enabled.is_(True), CopilotSchedule.at_hhmm == hhmm)
            .all()
        )
        for row in rows:
            if row.last_run_date == today or not _match_weekday(row.weekdays, now.date()):
                stats["skipped"] += 1
                continue
            try:
                text = _build_text(row)
                if text is None:      # 非交易日/无数据 → 不写、也不标记已跑
                    stats["skipped"] += 1
                    continue
                _deliver(db, row, text)
                row.last_run_date = today
                db.commit()
                stats["ran"] += 1
            except Exception as e:  # noqa: BLE001 - 单条失败不影响其他任务
                stats["failed"] += 1
                logger.warning("[copilot-sched] #%s %s 执行失败: %s", row.id, row.kind, e)
                db.rollback()
        if stats["ran"] or stats["failed"]:
            logger.info("[copilot-sched] %s", stats)
    except Exception as e:  # noqa: BLE001
        logger.warning("[copilot-sched] 扫描失败: %s", e)
    finally:
        db.close()
    return stats


def _build_text(row: CopilotSchedule) -> str | None:
    """生成任务正文；返回 None 表示"今天不该出内容"（非交易日/取不到数据）。"""
    from src.core.trading_calendar import is_trading_day

    try:
        if not is_trading_day(date.today()):
            return None
    except Exception:  # noqa: BLE001
        pass

    payload = row.payload or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}
    symbols = [str(s) for s in (payload.get("symbols") or []) if s]

    if row.kind == "auction_digest":
        from src.collectors.auction_collector import fetch_auction_symbols

        if not symbols:
            symbols = _watchlist_symbols(row.user_id)
        if not symbols:
            return "【竞价摘要】自选股为空，没有可查的标的。"
        body = fetch_auction_symbols(symbols, limit=int(payload.get("limit") or 10))
        return f"【竞价摘要 · {datetime.now():%m-%d %H:%M}】\n{body}\n\n（客观数据 · 不构成买卖指令）"

    if row.kind == "daily_brief":
        from src.collectors.auction_collector import fetch_auction_symbols  # noqa: F401

        # 收盘小结先只给"今日自选表现"，不做行情结论（避免和盘后报告的结论打架）
        if not symbols:
            symbols = _watchlist_symbols(row.user_id)
        if not symbols:
            return "【收盘小结】自选股为空。"
        lines = [f"【收盘小结 · {datetime.now():%m-%d}】自选 {len(symbols)} 只"]
        try:
            from src.core.marketdata_client import get_market_data

            quotes = get_market_data().quotes(symbols[:20])
            up = sum(1 for q in quotes if (getattr(q, "change_pct", 0) or 0) > 0)
            down = sum(1 for q in quotes if (getattr(q, "change_pct", 0) or 0) < 0)
            lines.append(f"涨 {up} / 跌 {down}（{len(quotes)} 只有报价）")
            for q in quotes[:10]:
                pct = getattr(q, "change_pct", None)
                pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "--"
                lines.append(f"- {getattr(q, 'symbol', '')} {getattr(q, 'name', '')} {pct_s}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"行情读取失败：{e}")
        lines.append("\n（客观数据 · 不构成买卖指令）")
        return "\n".join(lines)

    return None


def _watchlist_symbols(user_id) -> list[str]:
    from src.db.models import Stock

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        # Stock 表没有 is_deleted 列（2026-10-21 核对）—— 别照抄别处的过滤条件
        rows = db.query(Stock).filter(Stock.user_id == str(user_id)).limit(10).all()
        return [r.symbol for r in rows]
    except Exception:  # noqa: BLE001
        return []
    finally:
        db.close()


def _deliver(db, row: CopilotSchedule, text: str) -> None:
    """把结果写成 assistant 消息；没有会话就新建一个（标题标明是定时任务）。"""
    conv_id = row.conversation_id
    if conv_id:
        exists = db.query(ChatConversation).filter(ChatConversation.id == conv_id).one_or_none()
        if exists is None:
            conv_id = None
    if not conv_id:
        conv = ChatConversation(user_id=str(row.user_id), title="定时任务",
                               stock_symbol=None, stock_market=None)
        db.add(conv)
        db.flush()
        conv_id = conv.id
        row.conversation_id = conv_id
    db.add(ChatMessage(conversation_id=conv_id, role="assistant", content=text))
    db.commit()
