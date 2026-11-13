"""个人中心 API(2026-08-15 SIDA 完整度评估 P1): 个人资料 + 我的数据。

- GET /api/profile        → 当前用户信息(nickname/avatar/username/role/created_at)
- PUT /api/profile        → 更新昵称/头像(昵称 1-32 字; 头像 base64 data URL <200KB 或空串清空)
- GET /api/profile/stats  → 我的数据: 预测命中率 / 自选数 / 持仓数 / 影子账户画像有无
- 修改密码复用 /api/auth/change-password(前端安全区直接调用)

头像存储策略(与 /settings/avatar 一致): DB(users.avatar, String(255))只存文件名,
图片本体落盘到 data/avatars/, GET 时转 data URL 返回。避免 200KB 级 base64 塞进
users 行(导出/审计等并行子任务也在读 users 表, 保持行内轻量)。
文件名唯一(avatar_u<uid>-<uuid>.<ext>): 换头像写新文件 → 提交 DB → 成功后才删旧文件,
失败只清新文件(详见下方"一致性约定")。

预测命中率说明(2026-11-12 P0 更正): agent_prediction_outcomes 原来**没有 user_id**,
两个账号同标的同日的预测落同一个键 —— 后跑的会把别人的行改写, 统计也只能"全平台"混算。
现在加了 user_id(历史行保持 NULL = 归属不可证明), 因此:

- 本接口**只统计当前账号自己的记录**(scope=user), 不再给"全平台命中率";
- 历史无归属行不纳入成绩, 条数在 `legacy_unscoped_rows` 里如实报出并标 `legacy_untrusted`,
  不把"历史已清洗/已修好"当结论(没清洗, 也不会去动生产既有数据);
- system 桶(无会话的系统调度运行, user_id 恒空)独立计数在 `system_scoped_rows`, 绝不并入
  任何账号 —— 也不去猜它属于谁;
- 命中定义不变: outcome_status='evaluated' 且有收益的记录中, buy/add 且收益>0、
  reduce/sell/avoid 且收益<0 记命中; watch/hold/alert 等中性动作不计入分母。
"""
import base64
import logging
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import AgentPredictionOutcome, AppSettings, Position, Stock, User

logger = logging.getLogger(__name__)

router = APIRouter()

NICKNAME_MAX_LEN = 32
AVATAR_MAX_BYTES = 200 * 1024  # 200KB(解码后)

# 方向性动作 → 命中方向; 中性动作(watch/hold/alert/未知)不计入命中率
_BULLISH_ACTIONS = {"buy", "add"}
_BEARISH_ACTIONS = {"reduce", "sell", "avoid"}


# ── 头像落盘/读取(与 settings.py 的 ui_avatar 同套机制, 按用户隔离文件名) ──────────
#
# 一致性约定(2026-11-07 复审): 文件系统不受 DB 事务保护, 所以顺序必须是
#   ① 校验 → ② 写**唯一名**新文件 → ③ 提交 DB → ④ 提交成功后才删旧文件。
# 历史实现是 ①② 之间先删旧文件(固定名 avatar_u<id>.<ext> 直接覆盖), commit 失败时
# DB 回滚了但磁盘已经变了 —— 新头像"没保存"却生效, 清空"失败"却真没了。现在:
#   - 唯一文件名(带 uuid): 并发写/切扩展名都不会互相覆盖, 也不会覆盖未被替换的旧头像;
#   - commit 失败 → 只清掉这张没被登记的新文件, 旧文件与 DB 原值保持一致;
#   - 删旧文件前查一次 DB, 若仍有其他用户行引用同一文件名(历史共享)则保留, 防误删;
#   - 清空头像同样先提交 DB, 成功后才删文件。

def _avatar_dir() -> str:
    d = os.path.join(os.environ.get("DATA_DIR", "./data"), "avatars")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_avatar_fname(fname: str | None) -> str:
    """P2(audit-20260915): DB 中的 fname 须经 basename 白名单, 防路径穿越; 非法返回空串。"""
    if not fname:
        return ""
    safe = os.path.basename(fname)
    if not safe or safe != fname or "/" in fname or "\\" in fname:
        logger.warning("头像文件名非法, 跳过: %r", fname)
        return ""
    return safe


def _remove_avatar_file(fname: str | None) -> None:
    """尽力删除头像文件; 失败只记日志(不得因清理失败影响已提交的结果)。"""
    safe = _safe_avatar_fname(fname)
    if not safe:
        return
    try:
        os.remove(os.path.join(_avatar_dir(), safe))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("头像文件删除失败(忽略): %s err=%r", safe, exc)


def _read_avatar_data_url(fname: str | None) -> str:
    """从文件名读回 data URL; 无头像/文件丢失返回空串。"""
    safe = _safe_avatar_fname(fname)
    if not safe:
        return ""
    path = os.path.join(_avatar_dir(), safe)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    mime = "image/png" if safe.lower().endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _avatar_shared_by_other_user(db: Session | None, fname: str, user_id) -> bool:
    """fname 是否仍被别的用户行引用 → 不能删。

    历史数据/手工改库可能出现两个用户指向同一文件名; 只按"当前用户换了头像"就删,
    会把别人的头像一起删掉。查询失败时保守返回 True(宁可留垃圾文件, 不可误删在用的图)。
    """
    if db is None or not fname:
        return False
    try:
        rows = db.query(User.id).filter(User.avatar == fname).all()
        global_avatar = db.query(AppSettings).filter(AppSettings.key == 'ui_avatar', AppSettings.value == fname).first()
        if global_avatar:
            return True
    except Exception as exc:  # noqa: BLE001 — 清理是尽力而为, 不得倒灌到接口
        logger.warning("头像引用检查失败, 保守保留文件 %s: %r", fname, exc)
        return True
    # 提交后只要仍有任意用户引用就保留，包括并发请求使当前用户重新引用旧文件。
    return bool(rows)


def _cleanup_replaced_avatar(
    db: Session | None, old_fname: str | None, user_id, new_fname: str | None
) -> None:
    """DB 提交成功后再清旧文件(尽力而为): 同名/仍被引用则跳过。"""
    safe = _safe_avatar_fname(old_fname)
    if not safe or safe == new_fname:
        return
    if _avatar_shared_by_other_user(db, safe, user_id):
        logger.info("旧头像仍被其他用户引用, 跳过删除: %s", safe)
        return
    _remove_avatar_file(safe)


def _decode_avatar_data_url(value: str) -> tuple[bytes, str]:
    """校验 base64 data URL 并解码, 返回 (raw, 扩展名)。只校验, 不落盘(400 无副作用)。"""
    if not (value.startswith("data:") and "," in value):
        raise HTTPException(400, "头像需为 data URL")
    header, b64 = value.split(",", 1)
    if "image/" not in header:
        raise HTTPException(400, "头像需为图片 data URL")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "头像数据无效")
    if not raw:
        raise HTTPException(400, "头像数据无效")
    if len(raw) > AVATAR_MAX_BYTES:
        raise HTTPException(400, "头像不能超过 200KB")
    return raw, ("png" if "image/png" in header else "jpg")


def _write_avatar_file(raw: bytes, user_id, ext: str) -> str:
    """写唯一文件名并返回; 先 .part 暂存再 os.replace, 绝不覆盖已有头像。"""
    avatar_dir = _avatar_dir()
    # user_id 来自 DB, 仍净化后再拼文件名(防路径分隔符/超长)
    uid = re.sub(r"[^A-Za-z0-9_-]", "", str(user_id))[:32] or "anon"
    fname = f"avatar_u{uid}-{uuid.uuid4().hex}.{ext}"
    staged = os.path.join(avatar_dir, f".staged-{uuid.uuid4().hex}.part")
    try:
        with open(staged, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, os.path.join(avatar_dir, fname))
    except OSError as exc:
        # 半成品/已落盘的新文件都清掉: DB 还没登记它, 不能留孤儿
        _remove_avatar_file(os.path.basename(staged))
        _remove_avatar_file(fname)
        logger.error("头像文件写入失败: %r", exc)
        raise HTTPException(503, "头像未保存: 文件写入失败, 请重试") from exc
    return fname


# ── 数据组装 ──────────────────────────────────────────────────────────

def _profile_to_dict(user: User) -> dict:
    return {
        "username": user.username,
        "nickname": user.nickname or "",
        "avatar": _read_avatar_data_url(user.avatar),
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _legacy_unscoped_evaluated_count(db: Session, since: str = "") -> int:
    """窗口内「历史无归属」的已评估条数 —— 只用于如实标注, 不参与成绩。

    这些行是加 user_id/owner_scope 之前的存量数据: 无法证明属于哪个账号, 也没被清洗/回填。
    统计里必须显式说明它们被排除, 否则"总样本变小"会被误读成"记录少了"。
    """
    q = db.query(func.count(AgentPredictionOutcome.id)).filter(
        AgentPredictionOutcome.user_id.is_(None),
        AgentPredictionOutcome.owner_scope.is_(None),
        AgentPredictionOutcome.outcome_status == "evaluated",
        AgentPredictionOutcome.outcome_return_pct.isnot(None),
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    return int(q.scalar() or 0)


def _system_evaluated_count(db: Session, since: str = "") -> int:
    """窗口内 system 桶的已评估条数 —— **独立口径**, 绝不并入任何账号成绩。

    system 桶 = 无会话的系统调度运行(user_id 恒为空, owner_scope='system')。
    这里只报数, 不猜它属于谁。
    """
    q = db.query(func.count(AgentPredictionOutcome.id)).filter(
        AgentPredictionOutcome.user_id.is_(None),
        AgentPredictionOutcome.owner_scope == "system",
        AgentPredictionOutcome.outcome_status == "evaluated",
        AgentPredictionOutcome.outcome_return_pct.isnot(None),
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    return int(q.scalar() or 0)


def _system_pending_count(db: Session) -> int:
    """system 桶里还没评估的行数 —— 由**独立**的 system 成熟对账处理。

    定位(2026-11-13 更新): system 桶仍不进入任何人的个人命中率, 也不由个人评估路径
    回填(无个人归属); 但成熟行会由 `reconcile_system_outcomes`(调度器每工作日收盘后 /
    管理员手动)按真实交易日历与当日收盘价**持久对账**, 结果写回同一 system 行并带
    数据日期/规则版本/采集时间。对账只做聚合参考, 不据此自动交易、不包装成"平台成绩"。
    超过后验保留期后仍由 `cleanup_context_data` 按保留期删除。"""
    q = db.query(func.count(AgentPredictionOutcome.id)).filter(
        AgentPredictionOutcome.user_id.is_(None),
        AgentPredictionOutcome.owner_scope == "system",
        AgentPredictionOutcome.outcome_status == "pending",
    )
    return int(q.scalar() or 0)


def _ghost_user_evaluated_count(db: Session, since: str = "") -> int:
    """user_id 已不指向任何用户的**已评估**行数(ghost) —— 明确标注不纳入任何成绩。

    迁移库(缺 user_id 外键)在用户被物理清除后会留下这种行。它们原来会被当成"有归属"
    去评估/计数(ghost 用户错误计分); 现在读取与评估都要求用户行仍存在, 这里只如实报数,
    既不删也不回填。
    """
    q = (
        db.query(func.count(AgentPredictionOutcome.id))
        .filter(
            AgentPredictionOutcome.user_id.isnot(None),
            AgentPredictionOutcome.user_id.notin_(db.query(User.id)),
            AgentPredictionOutcome.outcome_status == "evaluated",
            AgentPredictionOutcome.outcome_return_pct.isnot(None),
        )
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    return int(q.scalar() or 0)


def _detached_account_count(db: Session, since: str = "") -> int:
    """账号已注销、行被 SET NULL 的留档条数(user_id 为空 + owner_scope='user')。

    与"加列前的历史行"(双 NULL)分开报: 前者是明确的删除语义结果, 后者是归属未知。
    两者都不计入任何个人成绩。
    """
    q = db.query(func.count(AgentPredictionOutcome.id)).filter(
        AgentPredictionOutcome.user_id.is_(None),
        AgentPredictionOutcome.owner_scope == "user",
        AgentPredictionOutcome.outcome_status == "evaluated",
        AgentPredictionOutcome.outcome_return_pct.isnot(None),
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    return int(q.scalar() or 0)


def _prediction_hit_stats(db: Session, *, user_id: str) -> dict:
    """当前账号的预测命中率(user_id 必传: 本表不提供跨账户口径)。

    返回 `overall` 的原形状(hit_count/total/hit_rate/scope, 旧前端依赖)并附
    `scoring_blocked` —— 评分阻断时 hit_rate 恒为 None, 且这个标记永不省略。
    """
    board = _accuracy_board(db, since="", user_id=user_id)
    overall = dict(board.get("overall") or {})
    overall["scoring_blocked"] = bool(board.get("scoring_blocked", False))
    if overall["scoring_blocked"]:
        overall["block_reason_code"] = board.get("block_reason_code")
        overall["block_reason"] = board.get("block_reason")
        overall["note"] = board.get("note")
        overall["scoring_status"] = board.get("scoring_status")
    return overall


def scoring_health_payload(bind=None, *, allow_repair: bool = False) -> dict:
    """评分闸门的只读快照(不含任何用户数据: 只有索引名/状态类别/聚合计数)。

    `allow_repair=False` 的原因: 这是 GET(个人统计)路径的辅助信息, 不能因为有人刷新页面
    就触发 DDL。按需重建由启动 / 评估 / 维护路径负责。
    """
    from src.core.scoring_guard import check_scoring_health

    return check_scoring_health(bind=bind, allow_repair=allow_repair).as_payload()


def _blocked_accuracy_board(health: dict, *, since: str, user_id: str) -> dict:
    """评分阻断时的命中榜骨架: 数字一律 `None`/0, 原因写清楚, 绝不给出"看起来还行"的比例。"""
    return {
        "since": since or None,
        "scope": "user",
        "user_scoped": True,
        "user_scope": str(user_id or ""),
        "scoring_blocked": True,
        "scoring_status": health,
        "block_reason_code": health.get("reason_code") or "unknown",
        "block_reason": health.get("reason") or "评分可信度检查未通过",
        "legacy_unscoped_rows": 0,
        "legacy_untrusted": False,
        "ghost_user_rows": 0,
        "detached_account_rows": 0,
        "system_scoped_rows": 0,
        "system_pending_rows": 0,
        "note": (
            "评分已阻断(scoring_blocked): "
            + str(health.get("reason") or "")
            + " —— 四条后验计分唯一索引不可信时, 同键可能已有多条计分行, 任何命中率都会"
            "双计。本接口不给命中率、不做回填、不清理数据; 恢复索引只能由启动/迁移角色或"
            "运维显式执行(运行期只检查, 不自动建删索引)"
            "后重新查询即可"
        ),
        "overall": {
            "hit_count": 0,
            "total": 0,
            "hit_rate": None,
            "avg_return_pct": None,
            "scope": "user",
            "legacy_unscoped_rows": 0,
            "scoring_blocked": True,
        },
        "agents": [],
    }


def _accuracy_board(db: Session, since: str = "", min_n: int = 1, *, user_id: str) -> dict:
    """当前账号的分 Agent 命中榜(2026-09-08 方向3; 2026-11-12 P0 改为按用户)。

    口径: buy/add 涨>0 命中、reduce/sell/avoid 跌<0 命中, 中性动作不计分母。
    since=YYYY-MM-DD 过滤 prediction_date(字符串比较)。样本 <min_n 的 agent 标
    qualified=false(不参与排名, 防 1 中 1 刷榜)。

    P0: 只读 `user_id == 当前用户` 的行。历史行(user_id 为空)归属不可证明 → 不纳入,
    但在 `legacy_unscoped_rows` / `legacy_untrusted` / `note` 里如实交代被排除的量,
    不把"没数据"说成"成绩很好", 也不声称历史重复已被修好。
    """
    uid = str(user_id or "").strip()
    if not uid:
        # 没有归属就不出成绩: 宁可空榜, 也不拿全平台数据冒充"我的命中率"
        return {
            "since": since or None,
            "scope": "user",
            "user_scoped": True,
            "scoring_blocked": False,
            "legacy_unscoped_rows": 0,
            "legacy_untrusted": False,
            "ghost_user_rows": 0,
            "detached_account_rows": 0,
            "system_scoped_rows": 0,
            "system_pending_rows": 0,
            "note": "缺少当前用户, 不提供跨账户统计",
            "overall": {
                "hit_count": 0,
                "total": 0,
                "hit_rate": None,
                "avg_return_pct": None,
                "scope": "user",
                "legacy_unscoped_rows": 0,
            },
            "agents": [],
        }

    # 评分闸门(fail-closed, 2026-11-13): 四条部分唯一索引不可信时, 同键可能已有多条计分
    # 行, 任何比例都会双计。此路径**只读巡检**(GET 不触发 DDL), 阻断就不给数字。
    health = scoring_health_payload(getattr(db, "bind", None), allow_repair=False)
    if health.get("scoring_blocked"):
        return _blocked_accuracy_board(health, since=since, user_id=uid)

    q = (
        db.query(
            AgentPredictionOutcome.agent_name,
            AgentPredictionOutcome.action,
            AgentPredictionOutcome.outcome_return_pct,
        )
        .filter(
            AgentPredictionOutcome.user_id == uid,
            AgentPredictionOutcome.outcome_status == "evaluated",
            AgentPredictionOutcome.outcome_return_pct.isnot(None),
        )
    )
    if since:
        q = q.filter(AgentPredictionOutcome.prediction_date >= since)
    rows = q.all()

    legacy = _legacy_unscoped_evaluated_count(db, since=since)
    system_rows = _system_evaluated_count(db, since=since)
    system_pending = _system_pending_count(db)
    ghost_rows = _ghost_user_evaluated_count(db, since=since)
    detached_rows = _detached_account_count(db, since=since)

    per: dict[str, dict] = {}
    ohit = otot = 0
    oret_sum = 0.0
    oret_n = 0
    for agent, action, ret in rows:
        action = (action or "").lower()
        if action in _BULLISH_ACTIONS:
            good = ret > 0
        elif action in _BEARISH_ACTIONS:
            good = ret < 0
        else:
            continue
        otot += 1
        oret_sum += ret
        oret_n += 1
        cell = per.setdefault(agent or "unknown", {"hit": 0, "total": 0, "ret_sum": 0.0})
        cell["total"] += 1
        cell["ret_sum"] += ret
        if good:
            ohit += 1
            cell["hit"] += 1

    agents = []
    for agent, cell in per.items():
        n = cell["total"]
        agents.append({
            "agent": agent,
            "hit": cell["hit"],
            "total": n,
            "hit_rate": round(cell["hit"] / n * 100, 1) if n else None,
            "avg_return_pct": round(cell["ret_sum"] / n, 2) if n else None,
            "qualified": n >= min_n,
        })
    agents.sort(key=lambda a: (not a["qualified"], -(a["hit_rate"] or -1)))
    note = "仅统计归属本账号的预测记录"
    if legacy:
        note += (
            f"; 另有 {legacy} 条历史记录无用户归属(加列前的存量), 归属不可证明, "
            "不纳入成绩、不回填、不清洗 —— 历史重复与混算不可视为已修复"
        )
    if ghost_rows:
        note += (
            f"; 另有 {ghost_rows} 条记录的 user_id 已不对应任何账号(ghost, 旧库缺外键 + "
            "账号被物理清除的产物), 已从评估与成绩中排除, 未删除"
        )
    if detached_rows:
        note += (
            f"; 另有 {detached_rows} 条是账号注销后的留档(user_id 已按删除语义置空), "
            "不归任何现存账号"
        )
    if system_rows or system_pending:
        note += (
            f"; 另有 {system_rows} 条 system 桶记录(无会话的系统调度运行)独立统计, "
            "不并入本账号"
        )
    if system_pending:
        note += (
            f"; 其中 {system_pending} 条尚未成熟/未对账 —— 由独立 system 对账在交易日收盘后 "
            "处理(个人路径不评估, 无个人归属, 不并入本账号, 不据此自动交易); "
            "无当日行情/停牌/超时保持 pending"
        )
    return {
        "since": since or None,
        "scope": "user",
        "user_scoped": True,
        "scoring_blocked": False,
        "scoring_status": health,
        "legacy_unscoped_rows": legacy,
        "legacy_untrusted": bool(legacy),
        "ghost_user_rows": ghost_rows,
        "detached_account_rows": detached_rows,
        "system_scoped_rows": system_rows,
        "system_pending_rows": system_pending,
        "note": note,
        "overall": {
            "hit_count": ohit,
            "total": otot,
            "hit_rate": round(ohit / otot * 100, 1) if otot else None,
            "avg_return_pct": round(oret_sum / oret_n, 2) if oret_n else None,
            "scope": "user",
            "legacy_unscoped_rows": legacy,
            "scoring_blocked": False,
        },
        "agents": agents,
    }


# ── API ───────────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    nickname: str | None = None
    avatar: str | None = None


@router.get("")
def get_profile(user: User = Depends(get_current_user)):
    """当前用户信息(nickname/avatar/username/role/created_at)。"""
    return _profile_to_dict(user)


@router.put("")
def update_profile(
    data: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新昵称/头像。昵称 1-32 字; avatar 为 base64 data URL(<200KB)或空串清空。

    2026-11-07 复审(文件/DB 一致性): 头像走"写唯一名新文件 → 提交 DB → 提交成功后
    删旧文件"; commit 失败只回滚 DB 并清掉刚写的新文件, 旧文件/旧 DB 值原样保留。
    旧实现先覆盖/删除再 commit, commit 失败时磁盘已变 —— 新头像"没保存"却生效、
    清空"失败"却真没了。
    """
    if data.nickname is not None:
        nickname = data.nickname.strip()
        if not nickname:
            raise HTTPException(400, "昵称不能为空")
        if len(nickname) > NICKNAME_MAX_LEN:
            raise HTTPException(400, "昵称最多 32 个字")
        user.nickname = nickname

    old_fname = (user.avatar or "").strip() or None
    avatar_changed = data.avatar is not None
    new_fname: str | None = None

    if avatar_changed:
        value = (data.avatar or "").strip()
        if not value:
            # 清空: 只改 DB, 文件等提交成功后再删(提交失败则文件必须还在)
            user.avatar = None
        else:
            raw, ext = _decode_avatar_data_url(value)  # 400 时无任何落盘副作用
            new_fname = _write_avatar_file(raw, user.id, ext)
            user.avatar = new_fname

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — 任何提交失败都必须回到旧状态
        db.rollback()
        if new_fname:
            # DB 已回滚到旧文件名 → 这张新文件无人引用, 清掉; 旧文件从未被碰过
            _remove_avatar_file(new_fname)
        try:
            db.refresh(user)
        except Exception:  # noqa: BLE001 — 会话异常时至少让内存对象与 DB 值一致
            user.avatar = old_fname
        logger.error("个人资料提交失败, 已回滚(头像/昵称未变更): %r", exc)
        raise HTTPException(503, "保存失败, 请重试") from exc

    # 提交成功: 此刻旧文件才真正无人引用(仍被别的用户引用则跳过, 防历史共享误删)
    if avatar_changed:
        _cleanup_replaced_avatar(db, old_fname, user.id, new_fname)

    db.refresh(user)
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        parts = []
        if data.nickname is not None:
            parts.append("昵称")
        if data.avatar is not None:
            parts.append("头像")
        if parts:
            log_audit(db, user, "update_profile", detail="更新" + "/".join(parts), ip="")
    except Exception as e:
        # P1: 审计写失败属异常路径, 需留痕(业务已成功, 仅审计缺失)
        logger.warning(f"profile 审计写入失败: {e}")
    return _profile_to_dict(user)


@router.get("/scoring/status")
def scoring_status(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """后验计分可信度(索引在位与否) —— 不含任何用户数据, 也不触发 DDL。

    只暴露: 索引名 / 状态类别(ok|missing|mismatch) / 差异类别 / 是否阻断。没有 user_id、
    没有行内容、没有其他用户的任何标识 —— 任何登录用户看到的是同一份 schema 体检结果。
    """
    return scoring_health_payload(getattr(db, "bind", None), allow_repair=False)


@router.get("/stats/accuracy")
def prediction_accuracy_board(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int = Query(30, ge=1, le=365),
    min_n: int = Query(5, ge=1, le=100),
):
    """分 Agent 命中榜(2026-09-08 方向3): 谁准谁不准, 一眼见。

    days=回看窗口(默认30天); min_n=参评最低样本(默认5, 防小样本刷榜)。
    下半场(因子自动降权)另开, 先把"看见"做实。
    """
    from datetime import date, timedelta

    since = (date.today() - timedelta(days=days)).isoformat()
    return _accuracy_board(db, since=since, min_n=min_n, user_id=user.id)


@router.get("/stats")
def profile_stats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的数据: 预测命中率(**仅本账号**)/自选数/持仓数/影子账户画像有无。"""
    watchlist_count = (
        db.query(func.count(Stock.id))
        .filter(or_(Stock.user_id == user.id, Stock.user_id.is_(None)))
        .scalar()
        or 0
    )
    position_count = (
        db.query(func.count(Position.id))
        .filter(or_(Position.user_id == user.id, Position.user_id.is_(None)))
        .scalar()
        or 0
    )
    shadow_profile = user.shadow_profile_json or {}
    return {
        "prediction": _prediction_hit_stats(db, user_id=user.id),
        "watchlist_count": watchlist_count,
        "position_count": position_count,
        "has_shadow_profile": bool(shadow_profile),
    }


class ProApplyBody(BaseModel):
    reason: str = ""


@router.post("/pro-apply")
def apply_pro(
    body: ProApplyBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pro 申请(个人中心入口)。

    2026-10-23 修复: 原实现只写 AuditLog + 站内通知, **不落 ProApplication 表**,
    导致后台 `GET /api/pro/admin/applications` 永远为空 —— 申请与审核断路。
    现与 `POST /api/pro/apply` 共用 `pro_billing.submit_application`: 落库 + 通知 owner
    + 拒重复 pending, 两个入口行为完全等价。
    """
    from src.web.api.pro_billing import _app_to_dict, submit_application

    row = submit_application(db, user, reason=body.reason)
    return {
        "status": "pending",
        "application": _app_to_dict(row),
        "message": "申请已提交, 请等待管理员审核(1-2 个工作日)。",
    }
