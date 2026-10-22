"""新分析维度提案（2026-10-22, P0 采集层）：**只记录，不生效**。

背景：数智参谋的分析维度写死在手写 YAML 配方里（`src/harness/recipes/*.yaml`，11 份）。
用户在对话里提出的新维度（"股东户数变化""龙虎榜席位风格"）没有落点 —— 助手只能拿
现有工具凑合答，或者老实说"这个数据平台没有"。两件事都不对：该补的能力没人补，
用户每次都要重新解释一遍。

P0 只做**采集**：
- 提案落 `harness_proposals` 表，带理由/证据/标的/会话/当时的配方；
- 同一用户同一维度反复提 → 只加 `seen_count`，不插重复行（哪条最该补，看次数就知道）；
- 人工审核（accept/reject/snooze）之后，**是否进配方由人决定**，本模块不碰任何 YAML。

红线：采集层绝不影响任何一次问答的行为。写了不等于做了 —— 配方一个字都不改。

去重键用 `normalized_dimension`（去空白 + 大小写折叠），唯一约束
`(user_id, normalized_dimension, status)` 里**故意带 status**：同一维度被拒之后
再次出现，可以重新提（pending 的位置是空的），而不是被历史行永久压住。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# 维度名长度：2~40 字。太短多半是乱码，太长多半是把问题本身塞进来了。
MIN_DIMENSION_LEN = 2
MAX_DIMENSION_LEN = 40

MAX_REASON = 500
MAX_EVIDENCE = 4000

DEFAULT_LIMIT = 50
STATS_TOP = 10

STATUS_PENDING = "pending"
# decide 的动作 = 落库的 status（accept/reject/snooze 都表示"已处理，不再是 pending"）
ACTIONS = ("accept", "reject", "snooze")

_WS = re.compile(r"\s+")


def normalize_dimension(dimension: str) -> str:
    """归一化维度名（去所有空白 + 大小写折叠）—— 去重键。

    "股东户数变化" / "股东 户数 变化" / "股东户数变化 " 必须算同一个维度，
    否则用户换个说法就多一行提案，统计就废了。
    """
    return _WS.sub("", (dimension or "").strip()).casefold()


def _clean_dimension(dimension: str) -> str:
    d = (dimension or "").strip()
    if len(d) < MIN_DIMENSION_LEN:
        raise ValueError(f"维度太短（至少 {MIN_DIMENSION_LEN} 个字）")
    if len(d) > MAX_DIMENSION_LEN:
        raise ValueError(f"维度太长（最多 {MAX_DIMENSION_LEN} 个字）")
    return d


def _clip(text: str | None, limit: int) -> str:
    """长文本截断（Text 列：超长时加省略号）。"""
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _trunc(text: str | None, limit: int) -> str:
    """定宽列截断（VARCHAR(n)：**不加省略号**）。

    加省略号会变成 n+1 字 → PG 直接报 "value too long for type character varying(n)"。
    """
    s = (text or "").strip()
    return s[:limit] if len(s) > limit else s


def propose(
    db,
    user_id,
    *,
    dimension: str,
    reason: str,
    evidence: str = "",
    conversation_id: int | None = None,
    symbol: str | None = None,
    recipe: str | None = None,
) -> dict:
    """记一条"用户想要但配方里没有"的分析维度。**只记录，不生效。**

    去重：同一 user + 同一 normalized_dimension 已有 `status='pending'` 的行 →
    不插新行，只 `seen_count += 1` 并刷新 `last_seen_at`，返回既有行
    （返回 dict 带 `created=False`；新建时 `created=True`）。

    维度名去空白后必须 2~40 字，否则 `ValueError`（空/超长）。
    """
    from sqlalchemy.exc import IntegrityError

    from src.db.models import HarnessProposal

    uid = str(user_id or "")
    if not uid:
        raise ValueError("缺少 user_id")
    dim = _clean_dimension(dimension)
    norm = normalize_dimension(dim)
    if not norm:
        raise ValueError("维度不能为空")
    # casefold() 可能把字符变长(如 ß → ss) → 定宽列会被 PG 拒绝 → 兜底截断
    norm = _trunc(norm, MAX_DIMENSION_LEN)

    now = datetime.utcnow()
    existing = (
        db.query(HarnessProposal)
        .filter(
            HarnessProposal.user_id == uid,
            HarnessProposal.normalized_dimension == norm,
            HarnessProposal.status == STATUS_PENDING,
        )
        .order_by(HarnessProposal.id.desc())
        .first()
    )
    if existing is not None:
        return _bump(db, existing, now)

    row = HarnessProposal(
        user_id=uid,
        dimension=dim,
        normalized_dimension=norm,
        reason=_clip(reason, MAX_REASON),
        evidence=_clip(evidence, MAX_EVIDENCE),
        conversation_id=int(conversation_id) if conversation_id else None,
        symbol=_trunc(symbol, 16) or None,
        recipe=_trunc(recipe, 64) or None,
        status=STATUS_PENDING,
        seen_count=1,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发提交同一维度 → 退回"又一次"语义（谁先提交谁建行，另一方加计数）
        db.rollback()
        again = (
            db.query(HarnessProposal)
            .filter(
                HarnessProposal.user_id == uid,
                HarnessProposal.normalized_dimension == norm,
                HarnessProposal.status == STATUS_PENDING,
            )
            .order_by(HarnessProposal.id.desc())
            .first()
        )
        if again is None:
            raise
        return _bump(db, again, now)
    db.refresh(row)
    out = _row_dict(row)
    out["created"] = True
    return out


def _bump(db, row, now: datetime) -> dict:
    """重复提案：只加计数 + 刷新最后出现时间，不动原始理由。"""
    row.seen_count = int(row.seen_count or 1) + 1
    row.last_seen_at = now
    db.commit()
    db.refresh(row)
    out = _row_dict(row)
    out["created"] = False
    return out


def list_proposals(db, user_id, *, status: str | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """列出某人的提案（严格按 user_id 过滤）。status 可选（pending/accept/reject/snooze）。"""
    from src.db.models import HarnessProposal

    uid = str(user_id or "")
    if not uid:
        return []
    q = db.query(HarnessProposal).filter(HarnessProposal.user_id == uid)
    if status:
        q = q.filter(HarnessProposal.status == status)
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    rows = q.order_by(HarnessProposal.last_seen_at.desc(), HarnessProposal.id.desc()).limit(n).all()
    return [_row_dict(r) for r in rows]


def decide(db, user_id, proposal_id: int, *, action: str) -> dict | None:
    """人工审核一条提案：accept / reject / snooze。

    写 `status` + `decided_at`。不属于本人（或不存在）→ 返回 None（调用方应转 404）。
    action 非法 → `ValueError`（调用方应转 400）。
    """
    from src.db.models import HarnessProposal

    act = (action or "").strip().lower()
    if act not in ACTIONS:
        raise ValueError(f"action 只能是 {' / '.join(ACTIONS)}")

    uid = str(user_id or "")
    row = (
        db.query(HarnessProposal)
        .filter(HarnessProposal.id == proposal_id, HarnessProposal.user_id == uid)
        .one_or_none()
    )
    if row is None:
        return None

    # 唯一约束是 (user_id, normalized_dimension, status)：同一维度被处理过两次时，
    # 目标状态可能已存在旧行 → 合并计数并删旧行（保留最新一次处理结果）。
    conflict = (
        db.query(HarnessProposal)
        .filter(
            HarnessProposal.user_id == row.user_id,
            HarnessProposal.normalized_dimension == row.normalized_dimension,
            HarnessProposal.status == act,
            HarnessProposal.id != row.id,
        )
        .order_by(HarnessProposal.id.asc())
        .first()
    )
    if conflict is not None:
        row.seen_count = int(row.seen_count or 1) + int(conflict.seen_count or 1)
        if conflict.first_seen_at and (row.first_seen_at is None or conflict.first_seen_at < row.first_seen_at):
            row.first_seen_at = conflict.first_seen_at
        db.delete(conflict)
        # 必须先让 DELETE 落地: 同一次 flush 里 SQLAlchemy 先 UPDATE 后 DELETE,
        # UPDATE 会先撞唯一约束(user, norm, status) → IntegrityError。
        db.flush()

    row.status = act
    row.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def stats(db, user_id) -> list[dict]:
    """「哪些维度反复出现」= 最该补的能力。按维度聚合 seen_count，取前 10。

    同一维度可能有多行（pending / rejected / snoozed 各一行），这里按
    normalized_dimension 聚合成一行，seen_count 求和 —— 这样"被拒了但用户还在提"
    依然排在最前面（它确实是反复出现）。
    """
    from src.db.models import HarnessProposal

    uid = str(user_id or "")
    if not uid:
        return []
    rows = (
        db.query(HarnessProposal)
        .filter(HarnessProposal.user_id == uid)
        .order_by(HarnessProposal.seen_count.desc(), HarnessProposal.id.desc())
        .all()
    )
    agg: dict[str, dict] = {}
    for r in rows:
        key = r.normalized_dimension or normalize_dimension(r.dimension or "")
        item = agg.get(key)
        if item is None:
            agg[key] = {
                "dimension": r.dimension or "",
                "normalized_dimension": key,
                "seen_count": int(r.seen_count or 1),
                "status": r.status or STATUS_PENDING,
                "rows": 1,
                "first_seen_at": str(r.first_seen_at or ""),
                "last_seen_at": str(r.last_seen_at or ""),
            }
            continue
        item["seen_count"] += int(r.seen_count or 1)
        item["rows"] += 1
        if r.status == STATUS_PENDING:
            item["status"] = STATUS_PENDING
        if str(r.first_seen_at or "") and (not item["first_seen_at"] or str(r.first_seen_at) < item["first_seen_at"]):
            item["first_seen_at"] = str(r.first_seen_at or "")
        if str(r.last_seen_at or "") > item["last_seen_at"]:
            item["last_seen_at"] = str(r.last_seen_at or "")
    items = sorted(
        agg.values(),
        key=lambda it: (-it["seen_count"], it["last_seen_at"], it["dimension"]),
    )
    return items[:STATS_TOP]


def summary(db, user_id) -> dict:
    """按状态的数量汇总（面板顶部那几个数）。"""
    from src.db.models import HarnessProposal

    uid = str(user_id or "")
    out = {"total": 0, "pending": 0, "accept": 0, "reject": 0, "snooze": 0}
    if not uid:
        return out
    rows = db.query(HarnessProposal).filter(HarnessProposal.user_id == uid).all()
    out["total"] = len(rows)
    for r in rows:
        st = (r.status or "").lower()
        if st in out:
            out[st] += 1
    return out


def _row_dict(r) -> dict:
    return {
        "id": r.id,
        "user_id": r.user_id,
        "dimension": r.dimension,
        "normalized_dimension": r.normalized_dimension,
        "reason": r.reason or "",
        "evidence": r.evidence or "",
        "conversation_id": r.conversation_id,
        "symbol": r.symbol,
        "recipe": r.recipe,
        "status": r.status or STATUS_PENDING,
        "seen_count": int(r.seen_count or 0),
        "first_seen_at": str(r.first_seen_at or ""),
        "last_seen_at": str(r.last_seen_at or ""),
        "decided_at": str(r.decided_at or ""),
    }
