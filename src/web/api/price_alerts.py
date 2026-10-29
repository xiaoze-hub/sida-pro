import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from src.config import Settings
from src.core.price_alert_engine import ENGINE
from src.web.api._db_errors import (
    bound_lock_wait,
    is_fk_violation,
    is_lock_conflict,
    is_unique_violation,
)
from src.web.api._scope import scoped
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import PriceAlertHit, PriceAlertRule, Stock, User

logger = logging.getLogger(__name__)
router = APIRouter()


def _my_stock(db: Session, uid: str, symbol: str, market: str) -> Stock | None:
    """本人在某标的上的私有自选(可能不存在)。"""
    return (
        db.query(Stock)
        .filter(
            Stock.user_id == uid,
            Stock.symbol == symbol,
            Stock.market == market,
        )
        .first()
    )


def _format_datetime(dt) -> str:
    """格式化时间为当前时区的 ISO 格式（naive datetime 视为 app 时区）。"""
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


class AlertConditionItem(BaseModel):
    type: str = Field(..., description="price/change_pct/turnover/volume/volume_ratio")
    op: str = Field(..., description=">=/<=/>/</==/between")
    value: float | list[float] = Field(..., description="阈值")


class AlertConditionGroup(BaseModel):
    op: str = Field(default="and", description="and/or")
    items: list[AlertConditionItem] = Field(default_factory=list)


class PriceAlertCreate(BaseModel):
    stock_id: int
    name: str = ""
    enabled: bool = True
    condition_group: AlertConditionGroup
    market_hours_mode: str = "trading_only"
    cooldown_minutes: int = 30
    max_triggers_per_day: int = 3
    repeat_mode: str = "repeat"
    expire_at: str | None = None
    notify_channel_ids: list[int] = []


class PriceAlertUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    condition_group: AlertConditionGroup | None = None
    market_hours_mode: str | None = None
    cooldown_minutes: int | None = None
    max_triggers_per_day: int | None = None
    repeat_mode: str | None = None
    expire_at: str | None = None
    notify_channel_ids: list[int] | None = None


class ToggleBody(BaseModel):
    enabled: bool


def _validate_condition_group(group: AlertConditionGroup):
    if group.op not in ("and", "or"):
        raise HTTPException(400, "condition_group.op 仅支持 and/or")
    if not group.items:
        raise HTTPException(400, "condition_group.items 不能为空")
    allowed_types = {"price", "change_pct", "turnover", "volume", "volume_ratio"}
    allowed_ops = {">=", "<=", ">", "<", "==", "=", "!=", "<>", "between", "in"}
    for it in group.items:
        if it.type not in allowed_types:
            raise HTTPException(400, f"不支持的条件类型: {it.type}")
        if it.op not in allowed_ops:
            raise HTTPException(400, f"不支持的运算符: {it.op}")
        if it.op in ("between", "in"):
            if not isinstance(it.value, list) or len(it.value) != 2:
                raise HTTPException(400, f"{it.type} 的 {it.op} 需要两个值")


def _to_response(rule: PriceAlertRule) -> dict:
    stock = rule.stock
    return {
        "id": rule.id,
        "stock_id": rule.stock_id,
        "stock_symbol": stock.symbol if stock else "",
        "stock_name": stock.name if stock else "",
        "market": stock.market if stock else "",
        # 该规则挂在共享(NULL)自选上时, UI 提供「迁到我的自选」入口(六审)。
        "stock_shared": bool(stock is not None and stock.user_id is None),
        "name": rule.name,
        "enabled": rule.enabled,
        "condition_group": rule.condition_group or {},
        "market_hours_mode": rule.market_hours_mode,
        "cooldown_minutes": rule.cooldown_minutes,
        "max_triggers_per_day": rule.max_triggers_per_day,
        "repeat_mode": rule.repeat_mode,
        "expire_at": _format_datetime(rule.expire_at) or None,
        "notify_channel_ids": rule.notify_channel_ids or [],
        "last_trigger_at": _format_datetime(rule.last_trigger_at) or None,
        "last_trigger_price": rule.last_trigger_price,
        "trigger_count_today": rule.trigger_count_today or 0,
        "trigger_date": rule.trigger_date or "",
        "created_at": _format_datetime(rule.created_at),
        "updated_at": _format_datetime(rule.updated_at),
    }


@router.get("")
def list_alert_rules(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅返回当前用户的提醒规则。"""
    rows = (
        db.query(PriceAlertRule)
        .join(Stock)
        .filter(PriceAlertRule.user_id == user.id)
        .order_by(PriceAlertRule.updated_at.desc(), PriceAlertRule.id.desc())
        .all()
    )
    return [_to_response(r) for r in rows]


@router.post("")
def create_alert_rule(
    body: PriceAlertCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 新建规则写入 user_id。

    C3(2026-09-09): 股票按归属过滤 —— 不能借 stock_id 在他人自选上建提醒。
    """
    stock = scoped(db.query(Stock), user).filter(Stock.id == body.stock_id).first()
    if not stock:
        raise HTTPException(404, "股票不存在")
    _validate_condition_group(body.condition_group)

    expire_at = None
    if body.expire_at:
        try:
            expire_at = datetime.fromisoformat(body.expire_at)
        except Exception:
            raise HTTPException(400, "expire_at 格式错误")

    row = PriceAlertRule(
        user_id=user.id,
        stock_id=body.stock_id,
        name=(body.name or "").strip() or f"{stock.name} 提醒",
        enabled=bool(body.enabled),
        condition_group=body.condition_group.model_dump(),
        market_hours_mode=body.market_hours_mode or "trading_only",
        cooldown_minutes=max(0, int(body.cooldown_minutes)),
        max_triggers_per_day=max(0, int(body.max_triggers_per_day)),
        repeat_mode=body.repeat_mode or "repeat",
        expire_at=expire_at,
        notify_channel_ids=body.notify_channel_ids or [],
    )
    try:
        db.add(row)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 并发下自选刚被删除 → 本次 INSERT 拿到外键冲突; 另有规则自身的唯一约束。
        # 两者都是"重来一次可能成功"的冲突 → 409; 其它完整性错误是真故障, 原样抛。
        if not (is_fk_violation(exc) or is_unique_violation(exc)):
            raise
        logger.warning("创建提醒规则冲突(未写入) user=%s", user.id)
        raise HTTPException(409, "该自选已不可用，请刷新后重试") from None
    db.refresh(row)
    return _to_response(row)


@router.put("/{rule_id}")
def update_alert_rule(
    rule_id: int,
    body: PriceAlertUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅允许当前用户修改自己的规则; 否则 404 防账号探测。"""
    row = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "规则不存在")

    updates = body.model_dump(exclude_unset=True)
    if "condition_group" in updates and body.condition_group:
        _validate_condition_group(body.condition_group)
        updates["condition_group"] = body.condition_group.model_dump()
    if "cooldown_minutes" in updates:
        updates["cooldown_minutes"] = max(0, int(updates["cooldown_minutes"]))
    if "max_triggers_per_day" in updates:
        updates["max_triggers_per_day"] = max(0, int(updates["max_triggers_per_day"]))
    if "expire_at" in updates:
        val = updates.get("expire_at")
        if val:
            try:
                updates["expire_at"] = datetime.fromisoformat(val)
            except Exception:
                raise HTTPException(400, "expire_at 格式错误")
        else:
            updates["expire_at"] = None

    for k, v in updates.items():
        setattr(row, k, v)

    # 修改提醒规则后重置当日触发计数
    row.trigger_count_today = 0
    row.trigger_date = ""

    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.post("/{rule_id}/toggle")
def toggle_alert_rule(
    rule_id: int,
    body: ToggleBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅允许当前用户 toggle 自己的规则; 否则 404 防账号探测。"""
    row = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "规则不存在")
    row.enabled = bool(body.enabled)
    db.commit()
    db.refresh(row)
    return _to_response(row)


@router.delete("/{rule_id}")
def delete_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅允许当前用户删除自己的规则; 否则 404 防账号探测。"""
    row = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "规则不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/{rule_id}/adopt")
def adopt_alert_rule_to_private(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """由提醒规则的**本人**显式把它迁到自己的私有自选(同一 symbol/market)。

    背景(2026-10-29 六审): 删除共享自选时, 系统**不再**替其他用户自动复制自选/
    Agent 绑定(那会在未经同意的情况下触发其 LLM 调度与外发), 也不再级联删历史。
    需要腾出共享自选的用户, 用这个入口自己迁:

    - 只操作 `user_id == 本人` 的规则及其命中历史(按 rule_id, 不跨租户);
    - 目标=本人已有同标的私有自选, 没有则建一条(受自选配额约束);
    - **不复制 StockAgent 绑定** → 不替用户订阅、不触发任何 Agent/LLM/通知;
    - 命中记录 `stock_id` 同步指向新自选, 原有触发历史完整保留。
    """
    # fail-closed: 没有 user_id 时 `user_id == None` 在 SQL 里是 `IS NULL`,
    # 会把 NULL 归属的历史规则当成"本人的"认领过来。宁可不认。
    uid = getattr(user, "id", None)
    if not uid:
        logger.warning("adopt 拒绝: 当前账号缺少 user_id(rule_id=%s)", rule_id)
        raise HTTPException(403, "无法确认当前账号身份，已拒绝迁移")

    row = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == uid)
        .first()
    )
    if not row:
        raise HTTPException(404, "规则不存在")

    # 2026-10-29 七审(B2 并发): 对本行取 FOR UPDATE —— 与 DELETE /api/stocks/{id}
    # 的写锁互斥。两个方向都安全: 删除先拿到锁 ⇒ 它要么看到本规则而 409 拒绝,
    # 要么本规则已迁走; 迁移先拿到锁 ⇒ 删除的预检读到的是迁移后的状态。
    # 同时它把"同一共享行的并发 adopt"串行化, 避免同 symbol 私有自选被建两条。
    try:
        bound_lock_wait(db)
        src_stock = (
            db.query(Stock)
            .filter(Stock.id == row.stock_id)
            .with_for_update()
            .first()
        )
    except OperationalError as exc:
        db.rollback()
        if not is_lock_conflict(exc):
            raise
        raise HTTPException(
            409, "该自选正在被其他操作修改，请刷新后重试"
        ) from None
    if not src_stock:
        raise HTTPException(409, "该规则关联的自选已不存在")
    if src_stock.user_id == user.id:
        raise HTTPException(400, "该提醒已在你的私有自选上")
    if src_stock.user_id is not None:
        # 规则指向他人私有自选(历史错位): 不借迁移复制其标的元数据。
        raise HTTPException(409, "该规则关联的自选不可用")

    target = _my_stock(db, uid, src_stock.symbol, src_stock.market)
    created_target = False
    if target is None:
        # 与 POST /api/stocks 同口径的配额/演示账号上限, 不借迁移动绕过限制。
        from src.core.permissions import check_watchlist_quota

        if str(getattr(user, "username", "")) == "demo" or str(
            getattr(user, "role", "")
        ) == "guest":
            try:
                from src.core import free_tier

                watch_limit = int(free_tier.guest_strategy(db).get("watchlist_limit", 1))
            except Exception:  # noqa: BLE001
                watch_limit = 1
            own_count = db.query(Stock).filter(Stock.user_id == uid).count()
            if watch_limit > 0 and own_count >= watch_limit:
                raise HTTPException(
                    403, f"演示账号仅可添加 {watch_limit} 只自选股，无法迁移提醒。"
                )
        check_watchlist_quota(db, user)
        from sqlalchemy import func as _func

        max_order = db.query(_func.max(Stock.sort_order)).scalar() or 0
        target = Stock(
            user_id=uid,
            symbol=src_stock.symbol,
            name=src_stock.name,
            market=src_stock.market,
            sort_order=int(max_order) + 1,
        )
        # 并发迁移同一 symbol 时, 若目标表上存在 (user_id, symbol, market) 唯一约束,
        # 后提交的一方会拿到 IntegrityError —— 不能冒 500, 也不能留半截事务。
        # 用 SAVEPOINT 只回滚这次 INSERT, 然后**重查**对手刚建好的行, 幂等复用。
        try:
            with db.begin_nested():
                db.add(target)
                db.flush()
            created_target = True
        except IntegrityError:
            target = _my_stock(db, uid, src_stock.symbol, src_stock.market)
            if target is None:
                # 不是"同 symbol 唯一冲突"而是别的完整性错误: 原样抛出, 由上层 500。
                logger.exception("adopt 建私有自选失败(非唯一冲突)")
                raise
            created_target = False

    row.stock_id = target.id
    # 命中没有 user_id, 但其 rule_id 已证明属于本人 → 同步标的, 历史条目不丢。
    db.query(PriceAlertHit).filter(PriceAlertHit.rule_id == row.id).update(
        {PriceAlertHit.stock_id: target.id}, synchronize_session=False
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not (is_fk_violation(exc) or is_unique_violation(exc)):
            raise
        # 目标行在提交瞬间被删掉(FK 失败)等罕见竞态: 整单回滚, 不冒 500,
        # 规则与命中仍留在原自选上(数据不丢), 前端按 409 重试即可。
        logger.warning("adopt 提交冲突(整单回滚) rule=%s user=%s", rule_id, uid)
        raise HTTPException(409, "迁移失败：自选刚发生变化，请刷新后重试") from None
    db.refresh(row)
    return {"ok": True, "stock_id": target.id, "created_stock": created_target}


@router.get("/hits/today")
def list_today_hits(
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """今日(本地时区)当前用户的命中, 跨规则聚合 —— 供首页"今日要紧事"。

    C3(2026-09-09): 此前完全无 user 过滤(全表 PriceAlertHit + 全表规则),
    命中历史跨用户可见。price_alert_hits 无 user_id 列, 经 rule_id 关联
    PriceAlertRule.user_id 过滤。
    """
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    local_midnight = datetime.now(tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)

    hits = (
        db.query(PriceAlertHit)
        .join(PriceAlertRule, PriceAlertHit.rule_id == PriceAlertRule.id)
        .filter(
            PriceAlertRule.user_id == user.id,
            PriceAlertHit.trigger_time >= start_utc,
        )
        .order_by(PriceAlertHit.trigger_time.desc(), PriceAlertHit.id.desc())
        .limit(max(1, min(int(limit), 200)))
        .all()
    )
    if not hits:
        return []
    rule_ids = {h.rule_id for h in hits}
    rule_map = {
        r.id: r
        for r in db.query(PriceAlertRule)
        .filter(PriceAlertRule.user_id == user.id, PriceAlertRule.id.in_(rule_ids))
        .all()
    }
    stock_ids = {h.stock_id for h in hits}
    stock_map = {s.id: s for s in db.query(Stock).filter(Stock.id.in_(stock_ids)).all()}  # scoped-check: allow — stock_ids 来自按本人规则过滤的命中
    out = []
    for h in hits:
        stock = stock_map.get(h.stock_id)
        rule = rule_map.get(h.rule_id)
        out.append(
            {
                "rule_id": h.rule_id,
                "rule_name": (rule.name if rule else "") or "提醒",
                "symbol": stock.symbol if stock else "",
                "name": stock.name if stock else "",
                "market": stock.market if stock else "CN",
                "trigger_time": _format_datetime(h.trigger_time),
                "snapshot": h.trigger_snapshot or {},
            }
        )
    return out


@router.get("/{rule_id}/hits")
def list_alert_hits(
    rule_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅允许查看自己的规则触发记录; 否则 404 防账号探测。"""
    _ = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == user.id)
        .first()
    )
    if not _:
        raise HTTPException(404, "规则不存在")
    rows = (
        db.query(PriceAlertHit)
        .filter(PriceAlertHit.rule_id == rule_id)
        .order_by(PriceAlertHit.trigger_time.desc(), PriceAlertHit.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [
        {
            "id": r.id,
            "rule_id": r.rule_id,
            "stock_id": r.stock_id,
            "trigger_time": _format_datetime(r.trigger_time),
            "trigger_snapshot": r.trigger_snapshot or {},
            "notify_success": bool(r.notify_success),
            "notify_error": r.notify_error or "",
        }
        for r in rows
    ]


@router.post("/{rule_id}/test")
async def test_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S3(2026-08-23): 仅允许当前用户测试自己的规则; 否则 404 防账号探测。"""
    row = (
        db.query(PriceAlertRule)
        .filter(PriceAlertRule.id == rule_id, PriceAlertRule.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "规则不存在")
    result = await ENGINE.scan_once(
        only_rule_id=rule_id, dry_run=True, bypass_market_hours=True
    )
    return result


@router.post("/scan")
async def scan_alert_rules(
    dry_run: bool = False,
    bypass_market_hours: bool = True,
    user: User = Depends(get_current_user),
):
    """手动触发提醒扫描。

    C3(2026-09-09): 此前完全无鉴权, 未登录也能触发全量扫描(会真发通知)。
    扫描仍覆盖全部用户的规则(引擎语义如此), 但触发权收回到已登录用户。
    """
    _ = user  # 鉴权即目的
    try:
        from server import price_alert_scheduler

        if price_alert_scheduler:
            # 手动扫描默认绕过交易时段门禁，便于即时验证规则
            if bypass_market_hours:
                return await price_alert_scheduler.trigger_once(dry_run=dry_run)
            return await ENGINE.scan_once(dry_run=dry_run, bypass_market_hours=False)
    except Exception:
        pass
    return await ENGINE.scan_once(
        dry_run=dry_run, bypass_market_hours=bypass_market_hours
    )
