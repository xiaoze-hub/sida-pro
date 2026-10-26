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

P2（2026-10-22）补一件事：**提案不等于「能力不存在」**。注册表里有 67 个工具，
而 deep_diagnosis 只挂 11 个 —— 用户提的「股东户数变化」很可能已有工具
（`get_fundamentals_detail`）覆盖，只是没进配方。`match_candidate_tools()` 只做
一次纯文本匹配给出「可挂载现有工具」提示，**不改配方、不改任何一次作答**；
审核人据此判「挂工具」还是「补数据源」，避免把已有能力当新需求排期。
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

# ── P2: 提案 → 现有工具匹配（纯文本，只读注册表） ─────────────────────────

#: 候选工具最多给几个（面板一行只放得下三个名字）。
CANDIDATE_TOOLS = 3
#: 中文没有空格 → 用二字词做最小匹配单位（「股东户数」→ 股东/东户/户数）。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
#: 英文/数字/下划线词（工具名与参数名都是这套写法）。
_LATIN_WORD = re.compile(r"[a-z0-9][a-z0-9_.\-]{1,}")
#: 几乎每份工具说明里都有的二字词：命中它们**不证明**这个维度已有工具，
#: 留着只会让面板出现「可挂载：get_stock_quote」这类噪声，直接把分数打假。
_GENERIC_BIGRAMS = frozenset({
    "数据", "分析", "查询", "获取", "指标", "情况", "口径", "最新", "历史", "变化",
    "信息", "相关", "问题", "个股", "股票", "市场", "支持", "是否", "可以", "需要",
    "用户", "本人", "库内", "以及", "用于", "回答", "包含", "明细", "提供", "一下",
})
#: 采集/记忆类工具不是「可挂载的分析能力」，不该出现在候选里。
_EXCLUDED_TOOL_PREFIXES = ("propose_",)
_EXCLUDED_TOOLS = frozenset({"note_new_dimension", "remember_preference"})
#: 维度侧至少要命中几个词：单个二字词命中太廉价（「交易」「股东」几乎每份说明都有），
#: 要求 2 个才认；维度本身只有 1 个词（如「筹码」）时按 1 个算。
_MIN_DIM_HITS = 2

#: 工具文本 → 词集 的缓存（注册表 import 后静态；50 行提案 × 66 工具不必重算）。
_TOOL_TOKENS: dict[str, tuple[str, set[str]]] = {}


def _tool_description(tool) -> str:
    """取工具的自然语言说明（schema 是 `{type: function, function: {...}}` 两层）。"""
    schema = tool.schema or {}
    fn = schema.get("function") if isinstance(schema, dict) else None
    if isinstance(fn, dict) and fn.get("description"):
        return str(fn["description"])
    if isinstance(schema, dict):
        return str(schema.get("description") or "")
    return ""


def _tokens(text: str) -> set[str]:
    """中文二字词 + 拉丁词 → 词集（不做分词，避免引入 jieba 这类重依赖）。"""
    s = (text or "").casefold()
    out = set(_LATIN_WORD.findall(s))
    for run in _CJK_RUN.findall(s):
        if len(run) == 1:
            out.add(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in _GENERIC_BIGRAMS:
                out.add(bigram)
    return out


def _tool_corpus() -> dict[str, tuple[str, set[str]]]:
    """注册表里可挂载工具的 (原文, 词集)。**函数内 import**：core 不许依赖 web。"""
    if _TOOL_TOKENS:
        return _TOOL_TOKENS
    try:
        from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    except Exception:  # pragma: no cover - 注册表不可用时宁可没有提示
        logger.debug("读取 CHAT_TOOL_REGISTRY 失败，跳过候选工具匹配", exc_info=True)
        return {}
    for name, tool in CHAT_TOOL_REGISTRY.items():
        if name in _EXCLUDED_TOOLS or name.startswith(_EXCLUDED_TOOL_PREFIXES):
            continue
        text = f"{name} {_tool_description(tool)} {tool.caliber or ''}"
        _TOOL_TOKENS[name] = (text.casefold(), _tokens(text))
    return _TOOL_TOKENS


def match_candidate_tools(dimension: str, reason: str = "", limit: int = CANDIDATE_TOOLS) -> list[dict]:
    """「这个维度是不是已经有工具了？」→ 最多 `limit` 个候选工具。

    只做词面重合，不调模型、不查库、不改配方。**维度侧的词是硬门槛**
    （至少命中 `_MIN_DIM_HITS` 个词），理由只参与排序 —— 理由里提的能力不能
    把维度本身没提的工具推到面板上。

    返回 `[{"name": ..., "caliber": ...}]`；没有匹配（或注册表读不到）→ `[]`，
    绝不为了「有东西可显示」硬凑一个。
    """
    dim = (dimension or "").strip()
    if not dim:
        return []
    try:
        n = max(1, min(10, int(limit)))
    except (TypeError, ValueError):
        n = CANDIDATE_TOOLS
    corpus = _tool_corpus()
    if not corpus:
        return []

    dim_tokens = _tokens(dim)
    reason_tokens = _tokens(reason or "") - dim_tokens
    if not dim_tokens:
        return []

    scored: list[tuple[int, str]] = []
    required = _MIN_DIM_HITS if len(dim_tokens) >= _MIN_DIM_HITS else 1
    dim_norm = dim.casefold()
    for name, (text, tokens) in corpus.items():
        dim_hits = len(dim_tokens & tokens)
        if dim_hits < required:
            continue
        # 维度名整体出现在工具说明里 → 几乎可以断定就是它，排最前。
        exact = 6 if dim_norm and dim_norm in text else 0
        score = 2 * dim_hits + len(reason_tokens & tokens) + exact
        scored.append((score, name))
    # 同分按工具名排 → 结果稳定（面板不该每次刷新顺序都变）。
    scored.sort(key=lambda x: (-x[0], x[1]))

    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    return [
        {"name": name, "caliber": CHAT_TOOL_REGISTRY[name].caliber or ""}
        for _, name in scored[:n]
    ]



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
    # 候选工具只是提示（面板一行 muted 文案），匹配挂了也必须能把提案本身显示出来
    # —— 采集层的价值是「提案可审」，不能因为附注失败整行 500。
    try:
        candidates = match_candidate_tools(r.dimension or "", r.reason or "")
    except Exception:
        logger.debug("候选工具匹配失败：dimension=%r", getattr(r, "dimension", ""), exc_info=True)
        candidates = []
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
        "candidate_tools": candidates,
    }
