"""会话里创建的定时任务（2026-10-21, P0; 2026-11-03 升级四档时段语义与运行记录）。

实现取舍：
- **不做动态 APScheduler 注册**：注册在内存里，重启就丢、删任务还有竞态。
  改成每分钟扫一次 `copilot_schedules` 表，命中规则且今天/当时段没跑过就执行。
- 只支持两类低风险任务：`auction_digest`（竞价摘要）与 `daily_brief`（收盘小结）。
  下单、改持仓永远不在这里（与 actions 的红线一致）。
- 结果写成一条 `assistant` 消息进会话，用户打开面板就能看到；找不到会话就新建一个。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time

from src.core.trading_calendar import is_trading_day
from src.db.models import ChatConversation, ChatMessage, CopilotSchedule, CopilotScheduleRun

logger = logging.getLogger(__name__)

KINDS = ("auction_digest", "daily_brief")
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 四档时段语义
WINDOW_KINDS = ("premarket", "intraday", "postmarket", "custom")


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


def _parse_hhmm(s: str) -> time | None:
    hh, _, mm = (s or "").partition(":")
    if hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60:
        return time(int(hh), int(mm))
    return None


def create(db, user_id, *, kind: str, at_hhmm: str,
           weekdays: str = "mon-fri", conversation_id: int | None = None,
           payload: dict | None = None,
           window_kind: str = "custom", interval_min: int | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"不支持的任务类型：{kind}（可选 {', '.join(KINDS)}）")
    if window_kind not in WINDOW_KINDS:
        raise ValueError(f"不支持的时段语义：{window_kind}（可选 {', '.join(WINDOW_KINDS)}）")

    if conversation_id is not None:
        owned = db.query(ChatConversation.id).filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == str(user_id),
        ).first()
        if owned is None:
            raise ValueError("会话不存在或无权访问")
    
    t = _parse_hhmm(at_hhmm)
    if t is None:
        raise ValueError("时间格式应为 HH:MM，例如 09:25")
    
    norm_hhmm = f"{t.hour:02d}:{t.minute:02d}"
    
    if window_kind == "intraday" and interval_min is not None:
        if int(interval_min) < 5:
            interval_min = 5

    row = CopilotSchedule(
        user_id=str(user_id), kind=kind, at_hhmm=norm_hhmm,
        weekdays=weekdays or "mon-fri", conversation_id=conversation_id,
        payload=payload or {},
        window_kind=window_kind,
        interval_min=int(interval_min) if interval_min is not None else (20 if window_kind == "intraday" else None),
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


def list_runs(db, user_id: str | int, schedule_id: int, limit: int = 20) -> list[dict]:
    """只读 list_runs(db, user_id, schedule_id, limit=20) 供后续接口用。"""
    rows = (
        db.query(CopilotScheduleRun)
        .filter(
            CopilotScheduleRun.schedule_id == int(schedule_id),
            CopilotScheduleRun.user_id == str(user_id)
        )
        .order_by(CopilotScheduleRun.id.desc())
        .limit(limit)
        .all()
    )
    res = []
    for r in rows:
        res.append({
            "id": r.id,
            "schedule_id": r.schedule_id,
            "user_id": r.user_id,
            "run_id": r.run_id,
            "status": r.status,
            "error": r.error,
            "trigger_at": r.trigger_at,
            "created_at": str(r.created_at or ""),
            "finished_at": str(r.finished_at or ""),
        })
    return res


def _row_dict(r: CopilotSchedule) -> dict:
    return {
        "id": r.id, "kind": r.kind, "at_hhmm": r.at_hhmm, "weekdays": r.weekdays,
        "conversation_id": r.conversation_id, "enabled": bool(r.enabled),
        "payload": r.payload or {}, "last_run_date": r.last_run_date,
        "window_kind": r.window_kind or "custom",
        "interval_min": r.interval_min,
        "created_at": str(r.created_at or ""),
    }


# ── 四档时段判定核心逻辑 ──────────────────────────────────────────────────

def _should_trigger_now(row: CopilotSchedule, now: datetime) -> bool:
    """判定定时任务在当前时间点是否应该触发。

    规则：
    1. 必须是交易日（非交易日一律不触发）。
    2. 必须匹配星期。
    3. 根据 window_kind 分流：
       - premarket：09:00 至 09:30 之间（含 09:00, 09:30），且当天没跑过。
       - intraday：在两个交易时段 09:30–11:30 / 13:00–15:00 内（不含 11:30~13:00 午休），
         且距离上次运行时间 >= interval_min（默认 20 分钟，最小 5 分钟）。
       - postmarket：15:00 至 15:59 之间（含 15:00, 15:59），且当天没跑过。
       - custom：完全保持现有行为（精确匹配 at_hhmm 且当天没跑过）。
    """
    today_date = now.date()
    if not is_trading_day(today_date):
        return False
    if not _match_weekday(row.weekdays, today_date):
        return False

    today_str = today_date.strftime("%Y%m%d")
    wk = row.window_kind or "custom"
    current_time = now.time()

    if wk == "custom":
        if row.last_run_date == today_str:
            return False
        sched_t = _parse_hhmm(row.at_hhmm)
        if sched_t is None:
            return False
        return current_time.hour == sched_t.hour and current_time.minute == sched_t.minute

    if wk == "premarket":
        if row.last_run_date == today_str:
            return False
        return time(9, 0) <= current_time <= time(9, 30)

    if wk == "postmarket":
        if row.last_run_date == today_str:
            return False
        return time(15, 0) <= current_time <= time(15, 59)

    if wk == "intraday":
        in_session = (
            (time(9, 30) <= current_time < time(11, 30)) or
            (time(13, 0) <= current_time < time(15, 0))
        )
        if not in_session:
            return False

        from src.db.session import SessionLocal
        db = SessionLocal()
        try:
            last_run = (
                db.query(CopilotScheduleRun)
                .filter(
                    CopilotScheduleRun.schedule_id == row.id,
                    CopilotScheduleRun.status == "ok"
                )
                .order_by(CopilotScheduleRun.id.desc())
                .first()
            )
            if last_run and last_run.created_at:
                last_dt = last_run.created_at
                # 使用当时记录的时间进行差值比较
                delta = now - last_dt
                interval = row.interval_min if row.interval_min is not None else 20
                if interval < 5:
                    interval = 5
                if delta.total_seconds() < interval * 60:
                    return False
        except Exception:
            pass
        finally:
            db.close()
        return True

    return False


# ── 执行 ────────────────────────────────────────────────────────────────

def run_due(now: datetime | None = None) -> dict:
    """每分钟调一次：跑所有符合时段规则的任务。返回统计（供日志/自检）。"""
    from src.db.session import SessionLocal

    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    hhmm = now.strftime("%H:%M")
    stats = {"tick": hhmm, "ran": 0, "failed": 0, "skipped": 0}
    db = SessionLocal()
    has_runs_table = True
    try:
        db.execute(db.query(CopilotScheduleRun).statement.limit(1))
    except Exception:
        has_runs_table = False
        db.rollback()

    try:
        rows = (
            db.query(CopilotSchedule)
            .filter(CopilotSchedule.enabled.is_(True))
            .all()
        )
        for row in rows:
            if not _should_trigger_now(row, now):
                stats["skipped"] += 1
                continue

            trigger_at_str = now.strftime("%H:%M")
            run_row = None
            if has_runs_table:
                try:
                    run_row = CopilotScheduleRun(
                        schedule_id=row.id,
                        user_id=row.user_id,
                        status="skipped",
                        trigger_at=trigger_at_str,
                        created_at=now,  # 使用传入的 now（便于单测模拟时序）
                    )
                    db.add(run_row)
                    db.commit()
                    db.refresh(run_row)
                except Exception:
                    db.rollback()
                    has_runs_table = False
                    run_row = None

            try:
                text = _build_text(row)
                if text is None:      # 非交易日/无数据 → 不写、也不标记已跑
                    if run_row:
                        try:
                            run_row.status = "skipped"
                            run_row.finished_at = now
                            db.commit()
                        except Exception:
                            db.rollback()
                    stats["skipped"] += 1
                    continue
                
                _deliver(db, row, text)
                row.last_run_date = today
                if run_row:
                    run_row.status = "ok"
                    run_row.finished_at = now
                db.commit()
                stats["ran"] += 1
            except Exception as e:  # noqa: BLE001 - 单条失败不影响其他任务
                stats["failed"] += 1
                err_msg = str(e)[:255]
                logger.warning("[copilot-sched] #%s %s 执行失败: %s", row.id, row.kind, e)
                db.rollback()
                if run_row:
                    try:
                        run_row.status = "failed"
                        run_row.error = err_msg
                        run_row.finished_at = now
                        db.commit()
                    except Exception:
                        db.rollback()
                else:
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
        except Exception:  # noqa: BLE001
            logger.exception("[copilot-sched] 行情读取失败")
            lines.append("行情读取失败，请稍后重试。")
        lines.append("\n（客观数据 · 不构成买卖指令）")
        return "\n".join(lines)

    return None


def _watchlist_symbols(user_id) -> list[str]:
    from src.db.models import Stock

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
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
        exists = db.query(ChatConversation).filter(
            ChatConversation.id == conv_id,
            ChatConversation.user_id == str(row.user_id),
        ).one_or_none()
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
    db.flush()
