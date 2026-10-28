"""src/web/api/copilot.py

AI 数智参谋（Copilot）API 路由。
提供 /api/copilot/brief/{symbol} 与 /api/copilot/presets。
契约源: docs/research/AI数智参谋全链路落地设计方案.md §4
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_

from src.core.copilot_brief import build_copilot_brief
from src.core.trade_phase import phase_meta
from src.web.api.auth import get_current_user
from src.web.database import get_db as _get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot"])

_TTL = {"intraday": 20, "closed": 300, "non_trading": 1800}

# 简易进程内缓存与单飞锁
_cache: dict[str, tuple[float, int, dict]] = {}
_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_flight_lock(key: str) -> threading.Lock:
    with _global_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


@router.get("/brief/{symbol}")
def get_copilot_brief(
    symbol: str,
    market: str = Query("CN", pattern="^(CN|HK|US)$"),
    refresh: bool = Query(False, description="true=跳过进程缓存"),
    user: Any = Depends(get_current_user),
) -> dict:
    meta = phase_meta()
    phase = meta.get("trade_phase", "closed")
    is_live = bool(meta.get("is_live", False))

    ttl = _TTL["intraday"] if is_live else (_TTL["closed"] if phase == "closed" else _TTL["non_trading"])
    cache_key = f"{symbol}:{market}:{phase}"

    if not refresh and cache_key in _cache:
        ts, hit_ttl, payload = _cache[cache_key]
        if time.time() - ts < hit_ttl:
            res = dict(payload)
            res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
            return res

    lock = _get_flight_lock(cache_key)
    with lock:
        # 双重检查
        if not refresh and cache_key in _cache:
            ts, hit_ttl, payload = _cache[cache_key]
            if time.time() - ts < hit_ttl:
                res = dict(payload)
                res["cache"] = {"hit": True, "ttl_s": hit_ttl, "key": cache_key}
                return res

        brief = build_copilot_brief(symbol, market=market)
        # 若不是全降级，写缓存
        evidence_used = brief.get("verdict", {}).get("evidence_used", [])
        if len(evidence_used) >= 1:
            _cache[cache_key] = (time.time(), ttl, brief)

        res = dict(brief)
        res["cache"] = {"hit": False, "ttl_s": ttl, "key": cache_key}
        return res


@router.get("/persona/{symbol}")
def get_persona(symbol: str, market: str = "CN",
                user: Any = Depends(get_current_user)) -> dict:
    """标的画像（妖股 / 左侧 / 题材龙头 / 趋势 / 常规），供工作台打徽标。

    与参谋回答里用的是同一套判定（`src/core/stock_persona.py`），
    数据不足时返回 `kind="normal"` 且在 reasons 里说明 —— 不猜。
    """
    from src.core.stock_persona import detect as _detect
    from src.db.session import SessionLocal

    _db = SessionLocal()
    try:
        p = _detect(_db, symbol.split(".")[0].strip(), market)
    except Exception as exc:  # noqa: BLE001  画像失败不该让页面白屏
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}
    finally:
        _db.close()
    return {
        "available": True,
        "kind": p.kind,
        "label": p.label,
        "score": p.score,
        "grade": p.grade,
        "reasons": list(p.reasons),
        "recipe": p.recipe,
    }


@router.get("/presets")
def get_copilot_presets(
    user: Any = Depends(get_current_user),
) -> list[dict]:
    return [
        {
            "id": "full_dossier",
            "icon": "🧬",
            "label": "全景数智深度诊断(多维Harness)",
            "prompt": "请调用标的全景档案工具，对我当前查看的股票进行全面深度分析：整合实时量价、K线形态、腾讯逐笔主力意图、120日底仓筹码沉淀、数智决策GS多空色带、东方财富机构研报、近期公司公告与利好/利空消息面，给出明确结论与实战作战预案。",
            "group": "dossier",
            "requires_perm": None,
        },
        {
            "id": "auction_strong",
            "icon": "🔥",
            "label": "扫描今日 9:25 爆量抢筹",
            "prompt": "扫描今天集合竞价里爆量抢筹(竞价金额>2000万且量比>2.5)的股票，按竞价金额降序给出前 10 只，并标注每只的五态判定与竞价撤单率。",
            "group": "auction",
            "requires_perm": "view_auction",
        },
        {
            "id": "chip_cost",
            "icon": "🎯",
            "label": "诊断持仓股底仓成本与获利盘",
            "prompt": "用我的持仓列表，逐只给出底仓成本(COST50)、70%筹码区间与当前获利盘比例，标出已跌破 70% 筹码下沿的票。",
            "group": "chip",
            "requires_perm": None,
        },
        {
            "id": "resonance_first",
            "icon": "🛡️",
            "label": "筛三指标首次共振股",
            "prompt": "筛出今天三指标首次共振(🔥首次共振·机会挖掘)的股票，并按机构活跃度降序排列，说明每只的 GS 区间与 L2 主力净额。",
            "group": "resonance",
            "requires_perm": "view_forecast",
        },
        {
            "id": "demon_mainline",
            "icon": "👑",
            "label": "检索主线中的妖股候选",
            "prompt": "找出当前主线 Top5 板块里、同时进入妖股因子库 Top50 的个股，给出因子分、分级与连板身位。",
            "group": "demon",
            "requires_perm": "view_opportunities",
        },
        {
            "id": "defense_check",
            "icon": "🧯",
            "label": "检查持仓的防守位是否破位",
            "prompt": "对每只持仓，给出第一防守位与核心生命线(70%筹码下沿)，并明确哪些已经破位、哪些还在上方。",
            "group": "defense",
            "requires_perm": None,
        },
    ]


# ─────────────────────────────── 内置智能体（Harness 配方） ───────────────────────────────
@router.get("/agents")
def get_copilot_agents(user: Any = Depends(get_current_user)) -> list[dict]:
    """内置智能体元信息（前端 @ 面板用）。配方正文不下发。"""
    from src.harness.recipes import list_recipes

    return [r.public() for r in list_recipes()]


@router.get("/mentions")
def get_copilot_mentions(
    q: str = Query("", description="代码 / 名称 / 拼音首字母"),
    limit: int = Query(8, ge=1, le=30),
    user: Any = Depends(get_current_user),
) -> dict:
    """@ 面板候选：自选股（含全局）+ 内置智能体。

    自选股按当前用户隔离（与 get_watchlist 同口径：本人 + user_id IS NULL 的全局自选）。
    """
    from src.db.models import Stock
    from src.harness.recipes import list_recipes
    from src.web.database import SessionLocal

    kw = (q or "").strip().lower()
    stocks: list[dict] = []
    db = SessionLocal()
    try:
        query = db.query(Stock)
        if getattr(user, "id", None) is not None:
            query = query.filter(or_(Stock.user_id == user.id, Stock.user_id.is_(None)))
        rows = query.order_by(Stock.sort_order.asc()).limit(200).all()
        for s in rows:
            name = s.name or ""
            py = _pinyin_initials(name)
            hay = f"{s.symbol} {name} {py}".lower()
            if kw and kw not in hay:
                continue
            stocks.append({
                "type": "stock",
                "symbol": s.symbol,
                "name": name,
                "market": s.market or "CN",
                "py": py,
            })
            if len(stocks) >= limit:
                break
    except Exception as e:  # 自选表异常不应打断 @ 面板
        logger.warning("mentions 自选股查询失败: %s", e)
    finally:
        db.close()

    agents = []
    for r in list_recipes():
        hay = f"{r.id} {r.label} {r.desc} {' '.join(r.when)}"
        if kw and kw not in hay.lower():
            continue
        item = r.public()
        item["type"] = "agent"
        agents.append(item)
        if len(agents) >= limit:
            break
    return {"stocks": stocks, "agents": agents}


def _pinyin_initials(name: str) -> str:
    """名称 → 拼音首字母（"北方稀土" → "bfxt"）。pypinyin 缺失时退化为空串。"""
    if not name:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        return "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore")).lower()
    except Exception:
        return ""


# ──────────────── P1: 页面即上下文 / 动作回写 / P3: 轨迹只读 ────────────────

from fastapi import Body, HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.harness import actions as actions_mod  # noqa: E402
from src.harness.viewstate import ViewState, load_view_state, save_view_state  # noqa: E402
from src.web.database import get_db  # noqa: E402


@router.post("/context")
def post_copilot_context(
    payload: dict = Body(...),
    user: Any = Depends(get_current_user),
) -> dict:
    """前端上报当前视图（K 线页的标的/周期/圈选区）。

    刻意用裸 dict 而不是直接绑定 ViewState：前端字段演进时不该 422，
    未知字段一律忽略，缺字段一律可选。
    """
    conversation_id = payload.get("conversation_id")
    try:
        state = ViewState(**{k: v for k, v in payload.items() if k in ViewState.model_fields})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"视图状态不合法：{exc}") from exc
    ok = save_view_state(user.id, conversation_id, state)
    return {"saved": ok, "ttl_seconds": 1800}


@router.get("/context")
def get_copilot_context(
    conversation_id: int | None = Query(default=None),
    user: Any = Depends(get_current_user),
) -> dict:
    return {"state": load_view_state(user.id, conversation_id)}


@router.get("/actions")
def get_copilot_actions(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """待确认动作列表（AI 提议、等用户点确认）。"""
    return actions_mod.list_pending(db, user.id)


@router.post("/actions/{action_id}/apply")
def apply_copilot_action(
    action_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    try:
        return actions_mod.apply_action(db, user_id=user.id, action_id=action_id)
    except actions_mod.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/actions/{action_id}/reject")
def reject_copilot_action(
    action_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    try:
        return actions_mod.reject_action(db, user_id=user.id, action_id=action_id)
    except actions_mod.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/actions/{action_id}/rollback")
def rollback_copilot_action(
    action_id: int,
    reason: str = Query(default=""),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """把一次已 apply 的动作退回（P1-1.3）。回滚本身也写审计。"""
    try:
        return actions_mod.rollback_action(db, user_id=user.id, action_id=action_id, reason=reason)
    except actions_mod.ActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def get_copilot_runs(
    limit: int = Query(default=20, le=100),
    conversation_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """最近若干轮问答的骨架（P3 治理/回放入口）。"""
    from src.db.models import CopilotRun

    q = db.query(CopilotRun).filter(CopilotRun.user_id == str(user.id))
    if conversation_id is not None:
        q = q.filter(CopilotRun.conversation_id == conversation_id)
    rows = q.order_by(CopilotRun.id.desc()).limit(limit).all()
    run_ids = [r.run_id for r in rows]
    tokens: dict[str, int] = {}
    if run_ids:
        from sqlalchemy import func

        from src.db.models import LLMUsage

        for rid, tot in (
            db.query(LLMUsage.run_id,
                     func.coalesce(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens), 0))
            .filter(LLMUsage.run_id.in_(run_ids))
            .group_by(LLMUsage.run_id)
            .all()
        ):
            tokens[rid] = int(tot or 0)
    return [{
        "run_id": r.run_id, "conversation_id": r.conversation_id, "recipe": r.recipe,
        "status": r.status, "tool_calls": r.tool_calls, "elapsed_ms": r.elapsed_ms,
        "tokens": tokens.get(r.run_id),
        "question": r.question, "created_at": str(r.created_at or ""),
    } for r in rows]


@router.get("/runs/{run_id}")
def get_copilot_run_detail(
    run_id: str,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """单轮回放：骨架 + 每步工具调用（含摘要与耗时）。"""
    from src.db.models import CopilotRun, CopilotStep

    run = (
        db.query(CopilotRun)
        .filter(CopilotRun.run_id == run_id, CopilotRun.user_id == str(user.id))
        .one_or_none()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="轨迹不存在")
    steps = (
        db.query(CopilotStep).filter(CopilotStep.run_id == run_id)
        .order_by(CopilotStep.seq).all()
    )
    return {
        "run": {
            "run_id": run.run_id, "status": run.status, "recipe": run.recipe,
            "question": run.question, "tool_calls": run.tool_calls,
            # 2026-10-25: 后台执行完成后前端要据此重拉会话拿正文; 之前没这个字段,
            # 前端只能拿到 null 并提示"没拿到会话 id"（生产实测）
            "conversation_id": run.conversation_id,
            "elapsed_ms": run.elapsed_ms, "created_at": str(run.created_at or ""),
        },
        "steps": [{
            "seq": s.seq, "tool": s.tool, "ok": s.ok, "elapsed_ms": s.elapsed_ms,
            "args": s.args, "result_digest": s.result_digest,
        } for s in steps],
    }


@router.get("/metrics")
def get_copilot_metrics(
    days: int = Query(default=7, le=30),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """成本/质量看板数据：运行数、成功率、平均工具数、P95 耗时、按配方分组。

    P3（2026-10-22）补：原来 `by_recipe` 只给条数，看不出**哪份配方在坏** ——
    同一份配方跑 10 次全 degraded 与跑 10 次全 ok 在旧口径里长得一模一样。
    现在每行带 ok_rate / degraded_runs / error_runs / zero_tool_runs / avg_elapsed_ms
    （旧字段 recipe/runs 保留，前端老代码不受影响；算不出的均值写 null，不写 0）。
    """
    from datetime import timedelta

    from sqlalchemy import func

    from src.db.models import CopilotRun

    since = __import__("datetime").datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(CopilotRun)
        .filter(CopilotRun.user_id == str(user.id), CopilotRun.created_at >= since)
        .all()
    )
    if not rows:
        return {"days": days, "runs": 0, "ok_rate": None, "avg_tools": None,
                "p95_elapsed_ms": None, "by_recipe": [],
                "degraded_rate": None, "zero_tool_runs": 0}
    elapsed = sorted([r.elapsed_ms for r in rows if r.elapsed_ms])
    p95 = elapsed[int(len(elapsed) * 0.95) - 1] if elapsed else None

    # 按配方聚合：计数与耗时同一次遍历算完，避免再查一遍库。
    agg: dict[str, dict] = {}
    for r in rows:
        key = r.recipe or "(默认)"
        item = agg.get(key)
        if item is None:
            item = agg[key] = {
                "runs": 0, "ok": 0, "degraded": 0, "error": 0, "zero_tool": 0, "elapsed": [],
            }
        item["runs"] += 1
        st = (r.status or "").lower()
        if st == "ok":
            item["ok"] += 1
        elif st == "degraded":
            item["degraded"] += 1
        elif st == "error":
            item["error"] += 1
        if (r.tool_calls or 0) == 0:
            item["zero_tool"] += 1
        if r.elapsed_ms:
            item["elapsed"].append(int(r.elapsed_ms))

    by_recipe = []
    for key, item in agg.items():
        n = item["runs"]
        avg_ms = sum(item["elapsed"]) / len(item["elapsed"]) if item["elapsed"] else None
        by_recipe.append({
            "recipe": key,
            "runs": n,
            "ok_rate": round(item["ok"] / n, 3),
            "degraded_runs": item["degraded"],
            "error_runs": item["error"],
            "zero_tool_runs": item["zero_tool"],
            "avg_elapsed_ms": int(avg_ms) if avg_ms is not None else None,
        })
    by_recipe.sort(key=lambda x: (-x["runs"], x["recipe"]))

    return {
        "days": days,
        "runs": len(rows),
        "ok_rate": round(sum(1 for r in rows if r.status == "ok") / len(rows), 3),
        "avg_tools": round(sum(r.tool_calls or 0 for r in rows) / len(rows), 1),
        "p95_elapsed_ms": p95,
        "degraded_rate": round(sum(1 for r in rows if r.status == "degraded") / len(rows), 3),
        "zero_tool_runs": sum(1 for r in rows if (r.tool_calls or 0) == 0),
        "by_recipe": by_recipe,
    }


@router.get("/annotations")
def get_copilot_annotations(
    symbol: str = Query(...),
    market: str = Query(default="CN"),
    period: str = Query(default="1d"),
    ids: str = Query(default="", description="只读这些标注 id（逗号分隔；精确身份读取）"),
    market_strict: bool = Query(default=False, description="True = 严格市场等值（不做 A 股族归并）"),
    mine_only: bool = Query(default=False, description="True = 只返回本人标注（不含全局行）"),
    action_linked: bool = Query(default=False, description="True = 只返回能对回本人确认动作的行"),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """K 线图层标注（P1 收尾，2026-10-20；2026-10-28 P2 补齐可核验身份）。

    只返回本人 + 全局(user_id IS NULL)的标注；AI 与用户画的都在这张表里，
    用 `source` 区分。前端按 symbol/period 取，直接画到图上。

    2026-10-28 P2 三次收口的安全复审要求「标注读取必须精确身份与目标」：
    旧响应只有 id/price/ts/label/note/color/source/created_at —— 前端拿不到属主、
    标的、周期，也无从判断这一行是不是自己确认动作画的，只能靠本地会话信封+
    客户端标记自证。现在响应逐行回显 `symbol/market/period/user_id/action_id`：

    - `user_id`：行属主；`null` = 全局行（**不是**我确认的，前端默认不上图）
    - `action_id`：`copilot_actions.annotation_id` 反查出来的确认动作（本人），
      没有确认动作的行 = `null`（历史/人工/他人），前端据此拒绝上图
    - `symbol/market/period`：目标回显，前端必须与当前 K 线逐字比对

    `ids` / `market_strict` / `mine_only` / `action_linked` 是给「精确读取」用的严格模式；
    默认值保持旧行为（A 股市场族归并 + 本人或全局），不改变既有调用方语义。
    """
    from sqlalchemy import and_, or_

    from src.db.models import CopilotAction, KlineAnnotation

    wanted: list[int] = []
    # 直接调用(单测/内部)时未传的 Query 默认值不是 str/bool —— 按"没给"处理，
    # 不让 FastAPI 的默认对象渗进业务判断。
    raw_ids = ids if isinstance(ids, str) else ""
    strict = market_strict is True
    only_mine = mine_only is True
    linked_only = action_linked is True
    if raw_ids.strip():
        try:
            wanted = [int(x) for x in raw_ids.split(",") if x.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"ids 不是合法的标注 id 列表：{raw_ids}") from exc
        if not wanted or any(i <= 0 for i in wanted):
            raise HTTPException(status_code=400, detail=f"ids 不是合法的标注 id 列表：{raw_ids}")
        if len(wanted) > 200:
            raise HTTPException(status_code=400, detail="ids 一次最多 200 个")

    # 2026-10-20 线上实测: AI 提议里 market 默认写 "CN"，而 K 线页传 "SH"/"SZ"，
    # 严格等值匹配会导致"标注确认成功但图上查不到"。A 股市场代码统一按一组处理。
    # 但市场族归并就抹掉了「同一代码的指数与个股」这一层（000001 上证指数 / 平安银行）：
    # 需要严格区分的调用方传 market_strict=true，此时只做等值匹配。
    _A_SHARE = {"CN", "SH", "SZ", "BJ"}
    mk = (market or "").upper()
    if strict:
        markets = [mk]
    else:
        markets = sorted(_A_SHARE) if mk in _A_SHARE else [market]

    uid = str(user.id)
    query = db.query(KlineAnnotation, CopilotAction.id.label("action_id")).outerjoin(
        CopilotAction,
        and_(CopilotAction.annotation_id == KlineAnnotation.id, CopilotAction.user_id == uid),
    )
    query = query.filter(
        KlineAnnotation.symbol == symbol,
        KlineAnnotation.market.in_(markets),
        KlineAnnotation.period == period,
    )
    if only_mine:
        query = query.filter(KlineAnnotation.user_id == uid)
    else:
        query = query.filter(or_(KlineAnnotation.user_id == uid, KlineAnnotation.user_id.is_(None)))
    if wanted:
        query = query.filter(KlineAnnotation.id.in_(wanted))
    if linked_only:
        query = query.filter(CopilotAction.id.isnot(None))
    rows = query.order_by(KlineAnnotation.created_at.desc()).limit(200).all()
    return [{
        "id": r.id,
        # 目标与属主回显：前端必须逐字核对，不再靠「请求—响应配对」猜
        "symbol": r.symbol,
        "market": r.market,
        "period": r.period,
        "user_id": r.user_id,
        # 本人确认动作（`copilot_actions.annotation_id` 反查）；null = 无确认动作
        "action_id": int(action_id) if action_id is not None else None,
        "price": r.price, "ts": r.ts, "label": r.label,
        "note": r.note, "color": r.color, "source": r.source,
        "created_at": str(r.created_at or ""),
    } for r, action_id in rows]


@router.get("/governance/cost")
def get_copilot_cost(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """成本看板：按日 / 按模型 / 按用户聚合 token 与估算费用。

    未配置单价的模型 cost 返回 null 并列进 `unpriced_models` —— 不编造金额。
    普通用户只能看自己那份（by_user 只剩自己），owner/admin 看全量。
    """
    from src.core.copilot_quota import cost_summary

    if _is_admin(user):
        data = cost_summary(db, days=days, price_per_mtoken=_llm_prices())
        data["scope"] = "all"
    else:
        uid = str(getattr(user, "id", ""))
        data = cost_summary(db, days=days, price_per_mtoken=_llm_prices(), user_id=uid)
        data["by_user"] = [r for r in data["by_user"] if r.get("user_id") == uid]
        data["scope"] = "self"
    return data


@router.get("/governance/quota")
def get_copilot_quota(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """配额列表 + 各自今日用量。普通用户只看自己。"""
    from src.core.copilot_quota import get_quota, list_all, usage_today

    if _is_admin(user):
        return {"scope": "all", "items": list_all(db)}
    uid = str(getattr(user, "id", ""))
    row = get_quota(db, uid)
    return {
        "scope": "self",
        "items": [{
            "user_id": uid,
            "daily_runs": getattr(row, "daily_runs", None),
            "daily_tokens": getattr(row, "daily_tokens", None),
            "enabled": bool(getattr(row, "enabled", True)) if row else True,
            "note": getattr(row, "note", None),
            "used_runs": usage_today(db, uid)["runs"],
            "used_tokens": usage_today(db, uid)["tokens"],
            "updated_at": str(getattr(row, "updated_at", "") or ""),
        }],
    }


@router.put("/governance/quota/{user_id}")
def put_copilot_quota(
    user_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """设置某人配额（仅 owner/admin）。body: {daily_runs, daily_tokens, enabled, note}。

    `null` = 该维度不限；两个都 null 等于不限。
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="只有管理员可以修改配额")
    from src.core.copilot_quota import set_quota

    def _int_or_none(v):
        if v is None or v == "":
            return None
        n = int(v)
        if n < 0:
            raise HTTPException(status_code=400, detail="配额不能为负数")
        return n

    row = set_quota(
        db, user_id,
        daily_runs=_int_or_none(body.get("daily_runs")),
        daily_tokens=_int_or_none(body.get("daily_tokens")),
        enabled=bool(body.get("enabled", True)),
        note=(body.get("note") or None),
        updated_by=getattr(user, "id", None),
    )
    return {
        "user_id": row.user_id, "daily_runs": row.daily_runs,
        "daily_tokens": row.daily_tokens, "enabled": bool(row.enabled),
        "note": row.note, "updated_at": str(row.updated_at or ""),
    }


def _is_admin(user) -> bool:
    return str(getattr(user, "role", "") or "").lower() in ("owner", "admin")


def _llm_prices() -> dict:
    """模型单价表（元 / 百万 token，混合口径）。

    来源：`settings` 表的 `llm_price_per_mtoken`（JSON，可热改），缺省用内置保守值。
    没有配置的模型一律 cost=None —— 宁可显示"—"，不编一个假金额。
    """
    import json

    try:
        from src.db.models import AppSettings
        from src.db.session import SessionLocal

        with SessionLocal() as db:
            row = db.query(AppSettings).filter(AppSettings.key == "llm_price_per_mtoken").one_or_none()
            if row and row.value:
                return json.loads(row.value)
    except Exception:  # noqa: BLE001 - 没有配置表/解析失败都退回内置值
        pass
    return {
        "gemini": 0.0,      # 订阅制，无按量计费
        "deepseek": 2.0,
        "glm": 1.0,
        "kimi": 1.0,
        "gpt-4o-mini": 1.0,
    }


@router.get("/memory")
def get_memory(
    include_disabled: int = Query(default=0),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """长期记忆列表（本人）。带 kind/weight/hits，可逐条停用或删除。"""
    from src.core.copilot_memory import list_all

    return list_all(db, getattr(user, "id", None), include_disabled=bool(include_disabled))


@router.post("/memory")
def post_memory(
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """手工写一条记忆。body: {content, kind?, weight?}。"""
    from src.core.copilot_memory import remember

    content = str(body.get("content") or "").strip()
    if len(content) < 2:
        raise HTTPException(status_code=400, detail="内容太短")
    kind = str(body.get("kind") or "preference")
    weight = int(body.get("weight") or 5)
    row = remember(db, getattr(user, "id", None), kind=kind, content=content,
                   source="user", weight=weight)
    if row is None:
        raise HTTPException(status_code=500, detail="写入失败")
    return row


@router.patch("/memory/{memory_id}")
def patch_memory(
    memory_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    from src.core.copilot_memory import update as _update

    _uid = getattr(user, "id", None)
    _kw: dict = {}
    if "enabled" in body:
        _kw["enabled"] = bool(body.get("enabled"))
    if body.get("content") is not None:
        _kw["content"] = str(body.get("content") or "")
    if body.get("weight") is not None:
        try:
            _kw["weight"] = int(body.get("weight"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="weight 必须是 1~10 的整数") from None
    if not _kw:
        raise HTTPException(status_code=400, detail="没有可更新的字段（content/weight/enabled）")
    try:
        row = _update(db, _uid, memory_id, **_kw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if row is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True, "memory": row}


@router.delete("/memory/{memory_id}")
def del_memory(
    memory_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    from src.core.copilot_memory import delete as _delete

    if not _delete(db, getattr(user, "id", None), memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@router.get("/schedules")
def get_schedules(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """会话定时任务列表（本人）。"""
    from src.core.copilot_schedule import list_all as _list

    return _list(db, getattr(user, "id", None))


@router.post("/schedules")
def post_schedule(
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """直接建一个定时任务（面板手工建；AI 侧走 propose→apply 的 schedule 动作）。"""
    from src.core.copilot_schedule import create as _create

    try:
        return _create(
            db, getattr(user, "id", None),
            kind=str(body.get("kind") or "auction_digest"),
            at_hhmm=str(body.get("at_hhmm") or "09:25"),
            weekdays=str(body.get("weekdays") or "mon-fri"),
            conversation_id=body.get("conversation_id"),
            payload=body.get("payload") or {},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/schedules/{sched_id}")
def patch_schedule(
    sched_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    from src.core.copilot_schedule import set_enabled

    if not set_enabled(db, getattr(user, "id", None), sched_id, bool(body.get("enabled", True))):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@router.delete("/schedules/{sched_id}")
def del_schedule(
    sched_id: int,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    from src.core.copilot_schedule import delete as _delete

    if not _delete(db, getattr(user, "id", None), sched_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


# ──────────────── P0 采集层: 新分析维度提案(只记录, 不生效) ────────────────
# 分析维度写死在手写 YAML 配方里; 用户在对话里提出配方没覆盖的维度时无处落点。
# 这里只做**采集**: 提案落库 + 人工审核状态流转, 绝不改配方、绝不影响任何一次作答。


@router.get("/proposals")
def get_proposals(
    status: str | None = Query(default="pending", description="pending/accept/reject/snooze; 不传=全部"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> list[dict]:
    """列出本人提出的新维度提案。严格按 user_id 过滤(别人的一条都看不到)。"""
    from src.core.harness_proposals import list_proposals

    if status is not None:
        status = status.strip()
        if not status:
            status = None
    return list_proposals(db, getattr(user, "id", None), status=status, limit=limit)


@router.post("/proposals/{proposal_id}/decide")
def decide_proposal(
    proposal_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """人工审核一条提案: body {action: accept|reject|snooze}。

    不属于本人 / 不存在 → 404(不泄露别人的提案是否存在)。
    非法 action → 400。
    """
    from src.core.harness_proposals import decide as _decide

    action = str((body or {}).get("action") or "").strip().lower()
    try:
        row = _decide(db, getattr(user, "id", None), proposal_id, action=action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if row is None:
        raise HTTPException(status_code=404, detail="提案不存在")
    return {"ok": True, "proposal": row}


@router.get("/proposals/stats")
def get_proposals_stats(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """「哪些维度反复出现」= 最该补的能力(按 seen_count 降序取前 10)。

    只统计本人的; 附带按状态的数量汇总。
    """
    from src.core.harness_proposals import stats as _stats
    from src.core.harness_proposals import summary as _summary

    items = list(_stats(db, getattr(user, "id", None)))
    return {
        "items": items,
        "top": items[0] if items else None,
        "summary": _summary(db, getattr(user, "id", None)),
    }

# ──────────────── 经验库（实验台，P2-2.4）────────────────────────────────
# 只读：把 `harness_experiments` 里的事实原样展示出来。
# **不在这里做判断**——verdict 是写入时定的（keep/rollback），这里只读不改。
# 也不能"补 0"：baseline/candidate 缺就是缺，前端显示 —。


@router.get("/experiments")
def list_experiments(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    verdict: str | None = Query(None),
    recipe_id: str | None = Query(None),
    db=Depends(_get_db),
    user=Depends(get_current_user),
):
    """分页列出配方实验（改动 → 指标变化 → 保留/回滚）。"""
    from src.core.harness_experiments import VERDICTS, verdict_stats
    from src.db.models import HarnessExperiment

    if verdict and verdict not in VERDICTS:
        return {"available": False,
                "reason": f"verdict 只能是 {'/'.join(VERDICTS)}，收到 {verdict}",
                "items": [], "total": 0, "stats": {}}

    q = _scope_experiments(db, user)
    if verdict:
        q = q.filter(HarnessExperiment.verdict == verdict)
    if recipe_id:
        q = q.filter(HarnessExperiment.recipe_id == recipe_id)

    total = q.count()
    rows = (
        q.order_by(HarnessExperiment.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "available": True,
        "reason": "",
        "items": [_experiment_row(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "stats": verdict_stats(db, user_id=user.id),
    }


@router.get("/experiments/{exp_id}")
def get_experiment(
    exp_id: int,
    db=Depends(_get_db),
    user=Depends(get_current_user),
):
    """单条实验详情：基线/候选指标对照 + 回滚原因 + 证据文件是否存在。"""
    from pathlib import Path

    from src.db.models import HarnessExperiment

    row = _scope_experiments(db, user).filter(HarnessExperiment.id == exp_id).first()
    if row is None:
        return {"available": False, "reason": "没找到这条实验记录", "item": None}

    item = _experiment_row(row)
    item["baseline"] = row.baseline or {}
    item["candidate"] = row.candidate or {}
    item["reasons"] = list(row.reasons or [])
    item["note"] = row.note
    # 证据文件是落盘路径：**文件不在就说不存在**，不要让前端以为能下载
    ep = (row.evidence_path or "").strip()
    item["evidence_path"] = ep or None
    item["evidence_exists"] = bool(ep) and Path(ep).is_file()
    return {"available": True, "reason": "", "item": item}


def _scope_experiments(db, user):
    """按 user_id 隔离；admin 可看全部（平台级实验事实）。

    `harness_experiments.user_id` 是"谁发起的"，但实验结论是平台知识，
    所以管理员看全量、普通账号只看自己发起的。两边都是**显式**过滤条件。
    """
    from src.db.models import HarnessExperiment

    q = db.query(HarnessExperiment)
    if getattr(user, "is_admin", False) or getattr(user, "role", "") == "admin":
        return q
    return q.filter(HarnessExperiment.user_id == user.id)


def _experiment_row(r) -> dict:
    return {
        "id": r.id,
        "recipe_id": r.recipe_id,
        "change_summary": r.change_summary,
        "verdict": r.verdict,
        "user_id": r.user_id,
        "created_at": str(r.created_at or ""),
    }

# ──────────────── Agent 目录 / 配方版本（P2-2.1）─────────────────────────
# 只读。配方来自 YAML 文件，版本用内容指纹（不是手写版本号），历史惰性落库。


@router.get("/agents")
def list_agents(db: Session = Depends(get_db), user: Any = Depends(get_current_user)) -> dict:
    """全部配方的目录：能干什么、挂哪些工具、当前版本指纹。

    顺带同步版本历史（读到新指纹就记一条，幂等）。
    """
    import asyncio as _asyncio

    from src.core import recipe_versions as rv
    from src.harness import recipes as R

    changed: list[dict] = []
    try:
        changed = rv.sync_versions(db)
    except Exception as exc:  # noqa: BLE001
        # 版本记录失败不该让目录打不开：目录本身是文件系统事实
        logger.warning("配方版本同步失败(不阻断目录): %s", exc)

    items = R.catalog()
    return {
        "available": bool(items),
        "reason": "" if items else "配方目录为空（检查 src/harness/recipes/*.yaml）",
        "items": items,
        "count": len(items),
        "versions_recorded": changed,
    }


@router.get("/agents/{recipe_id}")
def get_agent(
    recipe_id: str,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """单个配方详情 + 版本历史（新→旧）。history 为空说明只观察过一次，不是错误。"""
    from src.core import recipe_versions as rv
    from src.harness import recipes as R

    rec = R.get_recipe(recipe_id)
    if rec is None:
        return {"available": False, "reason": f"没有这个配方：{recipe_id}", "item": None, "history": []}
    try:
        rv.sync_versions(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("配方版本同步失败(不阻断详情): %s", exc)
    return {
        "available": True,
        "reason": "",
        "item": rec.public(),
        "history": rv.history(db, recipe_id),
    }

# ──────────────── 工具目录(只读反射, 插件面板数据源) ────────────────
# 注册表 77 个工具对外没有任何目录入口, 用户看不到"平台到底有哪些能力、各自什么口径、
# 挂在哪些配方里"。这里只做**只读反射**: 不改注册表、不新增/删除工具(77 是门禁钉子)。


@router.get("/tools")
def get_tools_catalog(user: Any = Depends(get_current_user)) -> dict:
    """工具目录: 名称/说明/口径/挂载配方/是否计预算/是否有降级。

    口径必须随每个工具逐条返回 —— 五个数据源口径不可换算, 汇总或省略会误导判断。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    from src.harness.recipes import list_recipes

    # 反向索引: 工具名 -> 挂载它的配方 id 列表
    mounted: dict[str, list[str]] = {}
    for recipe in list_recipes():
        for tool_name in recipe.tools or []:
            mounted.setdefault(tool_name, []).append(recipe.id)

    items = []
    for name, tool in CHAT_TOOL_REGISTRY.items():
        fn = ((tool.schema or {}).get("function") or {})
        desc = str(fn.get("description") or "").strip().split("\n")[0][:120]
        items.append(
            {
                "name": name,
                "description": desc,
                "caliber": tool.caliber or "",
                "requires": tool.requires or "",
                "charges_budget": bool(tool.charges_budget),
                "has_fallback": bool(tool.fallback),
                # 未挂任何配方的工具返回空数组(前端要能看见"这个工具没人用")
                "recipes": sorted(mounted.get(name, [])),
            }
        )
    return {
        "total": len(items),
        "mounted": sum(1 for i in items if i["recipes"]),
        "tools": items,
    }
