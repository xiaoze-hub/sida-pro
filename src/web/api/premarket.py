"""盘前分析 Agent API 接线 (B 支线, 2026-10-23): /today 读底稿 + /run 触发 + /evidence 证据层。

- GET  /api/premarket/today: 该用户(或全局共享)最新一条 premarket_outlook success 记录;
  无记录 → {"date": 今日, "empty": true, "last": null};
  有记录 → 结构化解析 raw_data(兜底从正文解析), 非当日记录 is_stale=true, 绝不冒充今日。
- POST /api/premarket/run: 复用 agents.py 的 _spawn_async_run + server.trigger_agent 异步排队;
  每用户 5 分钟进程内限流(_LAST_RUN_TS), 超限 429; reset_rate_limit() 供测试清理。
- GET  /api/premarket/evidence: 调 src/core/premarket_evidence.collect(),
  六块(overnight/ladder/news/sentiment/watchlist/portfolio)原样透传 ——
  missing/suspect 块**不屏蔽不猜测**, 质量状态交给前端与 LLM 自行处置。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.core.timezone import beijing_now
from src.db.models import AnalysisHistory
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["premarket"])

AGENT_NAME = "premarket_outlook"

# 每用户触发冷却(秒): 盘前分析是重 LLM 任务, 防手抖连点白烧配额。
RUN_COOLDOWN_SEC = 300.0
_LAST_RUN_TS: dict[str, float] = {}


def reset_rate_limit() -> None:
    """清空进程内限流状态(测试隔离用; 生产进程重启自然清零)。"""
    _LAST_RUN_TS.clear()


def _today_str() -> str:
    return beijing_now().strftime("%Y-%m-%d")


def _trigger_run(agent_name: str, user_id: str) -> None:
    """异步排队触发(与 agents.py /trigger 端点同源; 懒 import, 测试可 monkeypatch 本函数)。"""
    from src.web.api.agents import _spawn_async_run
    from server import trigger_agent

    _spawn_async_run(
        trigger_agent,
        agent_name,
        user_id,
        name=f"trigger_agent:{agent_name}",
        notify_label="盘前分析",
        # 2026-10-30 复审: 少了归属, 通知会落 NULL 共享桶(成员看不到也收不到外发)。
        notify_user_id=user_id,
    )


# ──────────────── 结构化解析(只读, 绝不改库) ────────────────


def _norm_gate(v: Any) -> dict | None:
    if not isinstance(v, dict):
        return None
    return {"score": v.get("score"), "position_cap": v.get("position_cap")}


def _norm_mainlines(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for m in v:
        if isinstance(m, dict):
            out.append({"name": m.get("name") or m.get("theme") or "", "strength": m.get("strength")})
    return out


def _norm_plans(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for p in v:
        if isinstance(p, dict):
            out.append(
                {
                    "symbol": p.get("symbol") or p.get("code") or "",
                    "name": p.get("name") or "",
                    "action": p.get("action") or "",
                    "trigger": p.get("trigger"),
                    "stop": p.get("stop") or p.get("stop_loss"),
                }
            )
    return out


def _norm_key_levels(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for k in v:
        if isinstance(k, dict):
            out.append({"symbol": k.get("symbol") or "", "kind": k.get("kind") or "", "price": k.get("price")})
    return out


def _parse_embedded_json(content: str) -> dict | None:
    """从正文里救结构化字段: 先试 fenced ```json 块, 再试内嵌单行 JSON。取不到返回 None(不猜)。"""
    if not content:
        return None
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    m = re.search(r"\{[^{}]*\"(?:gate|mainlines|plans|key_levels)\"[^{}]*\}", content)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def extract_structured(record: AnalysisHistory) -> dict:
    """从 raw_data(JSON 列)取 gate/mainlines/plans/key_levels; raw_data 没有则从正文兜底解析。"""
    raw = record.raw_data if isinstance(getattr(record, "raw_data", None), dict) else {}
    data = raw if any(raw.get(k) for k in ("gate", "mainlines", "plans", "key_levels")) else (
        _parse_embedded_json(record.content or "") or {}
    )
    return {
        "gate": _norm_gate(data.get("gate")),
        "mainlines": _norm_mainlines(data.get("mainlines")),
        "plans": _norm_plans(data.get("plans")),
        "key_levels": _norm_key_levels(data.get("key_levels")),
    }


def _record_quality(record: AnalysisHistory) -> str:
    """有正文 = ok; 记录在但正文空(靠 raw_data 撑着) = suspect —— 前端据此降级展示。"""
    return "ok" if (record.content or "").strip() else "suspect"


def latest_record(
    db: Session,
    user_id: str | None,
    date: str | None = None,
    *,
    role: str | None = None,
) -> AnalysisHistory | None:
    """该用户最新一条 premarket_outlook success 记录。

    2026-10-29 五审: `user_id IS NULL` **不再"人人可见"**(旧行可能是别人的持仓正文)。
    member 只读本人; owner 额外可读 NULL 历史; 无 user_id → 不读(fail-closed)。
    """
    from src.core.analysis_history import analysis_history_scope_clause

    q = db.query(AnalysisHistory).filter(
        AnalysisHistory.agent_name == AGENT_NAME,
        AnalysisHistory.status == "success",
        analysis_history_scope_clause(user_id, role),
    )
    if date:
        q = q.filter(AnalysisHistory.analysis_date == date)
    return (
        q.order_by(AnalysisHistory.analysis_date.desc(), AnalysisHistory.created_at.desc(), AnalysisHistory.id.desc())
        .first()
    )


def render_brief(db: Session, user: User, date: str | None = None) -> str:
    """把最新(或指定日期)盘前底稿渲染成聊天工具文本 —— 当日版 vs 显式标注「非今日」的历史版。"""
    rec = latest_record(db, user.id, date, role=getattr(user, "role", None))
    if rec is None:
        if date:
            return f"{date} 没有盘前分析底稿。"
        return "暂无盘前分析底稿(可在侧栏「盘前分析」手动触发, 或等盘前定时任务生成)。"
    today = _today_str()
    d = str(rec.analysis_date)
    content = (rec.content or "").strip()
    s = extract_structured(rec)
    if d == today:
        head = f"【今日盘前分析底稿（{d}）】"
    else:
        head = f"【历史盘前分析底稿（基准日 {d}，非今日，仅供对照）】"
    lines = [head]
    if content:
        lines.append(content)
    gate = s["gate"]
    if gate:
        lines.append(f"闸门: 得分 {gate.get('score')} / 仓位上限 {gate.get('position_cap')}")
    if s["mainlines"]:
        lines.append("主线: " + "; ".join(f"{m['name']}({m['strength']})" for m in s["mainlines"] if m.get("name")))
    if s["plans"]:
        lines.append(
            "观察计划: "
            + "; ".join(
                f"{p['symbol'] or p['name']} {p['action']} 触发 {p['trigger']} 止损 {p['stop']}"
                for p in s["plans"]
            )
        )
    if s["key_levels"]:
        lines.append("关键位: " + "; ".join(f"{k['symbol']} {k['kind']} {k['price']}" for k in s["key_levels"]))
    if d != today:
        lines.append(f"（注: 该底稿基准日为 {d}，非今日，只作历史对照，勿当作今日盘前结论。）")
    return "\n".join(lines)


# ──────────────── 端点 ────────────────


@router.get("/today")
def get_today(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """该用户最新盘前底稿; 无记录返回 empty=true, 历史记录 is_stale=true(绝不冒充今日)。"""
    today = _today_str()
    rec = latest_record(db, user.id, role=getattr(user, "role", None))
    if rec is None:
        return {"date": today, "empty": True, "last": None}
    structured = extract_structured(rec)
    as_of_dt = rec.updated_at or rec.created_at
    return {
        "date": str(rec.analysis_date),
        "as_of": as_of_dt.isoformat() if as_of_dt else "",
        "is_stale": str(rec.analysis_date) != today,
        "empty": False,
        "last": {"date": str(rec.analysis_date)},
        "title": rec.title or "",
        "summary": (rec.content or "").strip()[:280],
        "content": rec.content or "",
        "structured": {k: structured[k] for k in ("gate", "mainlines", "plans")},
        "key_levels": structured["key_levels"],
        "quality": _record_quality(rec),
    }


@router.post("/run")
async def run(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """异步触发盘前分析(后台跑, 站内通知回执); 每用户 5 分钟限流。"""
    key = str(user.id)
    now = time.monotonic()
    last = _LAST_RUN_TS.get(key)
    if last is not None and now - last < RUN_COOLDOWN_SEC:
        remain = max(1, int(RUN_COOLDOWN_SEC - (now - last)))
        raise HTTPException(status_code=429, detail=f"盘前分析触发过于频繁，请 {remain} 秒后再试")
    _LAST_RUN_TS[key] = now
    try:
        _trigger_run(AGENT_NAME, key)
    except Exception as e:  # noqa: BLE001 - 失败不占限流, 回滚时间戳让用户可立即重试
        _LAST_RUN_TS.pop(key, None)
        raise HTTPException(status_code=500, detail=f"盘前分析触发失败: {e}")
    return {"queued": True, "message": "已提交后台执行"}


@router.get("/evidence")
def get_evidence(
    date: str | None = Query(default=None, description="日期 YYYY-MM-DD 或 YYYYMMDD, 默认今日"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """盘前结构化证据(六块); missing/suspect 块原样透传, 取数层已完成失真判定。"""
    from src.core import premarket_evidence

    try:
        ev = premarket_evidence.collect(date=date, db=db, user_id=user.id)
    except Exception as e:  # noqa: BLE001 - collect 本身承诺不抛, 这里只是保险丝
        logger.warning("[盘前证据] collect 异常: %s", e)
        raise HTTPException(status_code=500, detail=f"盘前证据采集失败: {e}")
    blocks = [
        {"name": k, **ev[k]}
        for k in ("overnight", "ladder", "news", "sentiment", "watchlist", "portfolio")
    ]
    return {"available": True, "date": ev["date"], "as_of": ev["as_of"], "blocks": blocks}
