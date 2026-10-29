"""账户和持仓管理 API"""
import logging
import math
import threading
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from datetime import datetime, timedelta, timezone

from src.web.database import get_db
from src.web.api.auth import get_current_user
from src.web.models import Account, PriceAlertRule, Position, Stock, User
from src.core.marketdata_client import md_quote_rows
from src.core.portfolio_scope import (
    account_user_scope_clause,
    account_user_scope_writable,
    position_user_scope_visible,
    position_user_scope_clause,
    position_user_scope_writable,
    user_scope_accounts,
    user_scope_positions,
)
from src.core.quote_period import classify_quote_period, summarize_daily_pnl_period
from src.models.market import MarketCode

logger = logging.getLogger(__name__)
router = APIRouter()

# 汇率缓存
# 2026-10-29 复审: 内置默认值(0.92/7.25)只是「从未成功拉取」时的兜底, 不是事实。
# 缓存里额外记录来源(last_ok_ts/source/as_of), 对外用 exchange_rate_meta 标明
# source=live|remote_cache|default 与 stale, 消费方不得把默认值当当日汇率。
_hkd_rate_cache: dict = {
    "rate": 0.92, "ts": 0, "last_ok_ts": 0.0, "source": "default", "as_of": None,
}
_usd_rate_cache: dict = {
    "rate": 7.25, "ts": 0, "last_ok_ts": 0.0, "source": "default", "as_of": None,
}
EXCHANGE_RATE_TTL = 3600  # 成功缓存 1 小时
# 2026-08-12: 海外节点新浪超时(每次冷启动白等 3s×2)。失败缓存短 TTL(5分钟)防反复撞墙;
# 成功后回到 1 小时长缓存。另用线程锁保证 HKD/USD 并发拉取不重复等待。
EXCHANGE_RATE_FAIL_TTL = 300
_fx_lock = threading.Lock()


def _parse_fx_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _fx_meta(cache: dict, name: str, rate: float | None = None) -> dict:
    """汇率来源声明: 无法证明新鲜(默认值/无时间戳)时 stale=True, 不冒充实时汇率。"""
    now = time.time()
    try:
        last_ok = float(cache.get("last_ok_ts") or 0.0)
    except (TypeError, ValueError):
        last_ok = 0.0
    if not math.isfinite(last_ok):
        last_ok = 0.0
    as_of = cache.get("as_of")
    age = int(now - last_ok) if last_ok > 0 else None
    stale = True if last_ok <= 0 else (now - last_ok > EXCHANGE_RATE_TTL)
    return {
        "name": name,
        "rate": rate if rate is not None else cache.get("rate"),
        "source": cache.get("source") or "default",
        "as_of": as_of,
        "age_seconds": age,
        "stale": stale,
    }


def _fetch_fx_rate(
    url: str,
    cache: dict,
    name: str,
    redis_key: str,
    fail_ttl: int = EXCHANGE_RATE_FAIL_TTL,
) -> float:
    """拉取单个汇率(带失败短缓存 + Redis 跨进程缓存)。返回当前生效汇率。"""
    if time.time() - cache["ts"] < EXCHANGE_RATE_TTL:
        return cache["rate"]
    # 2026-08-22: 内存 miss 时查 Redis(跨进程/重启兜底), 命中则回填内存。
    from src.web.cache.biz_cache import biz_cache
    cached = biz_cache.get_json(redis_key)
    if cached is not None:
        try:
            if isinstance(cached, dict):
                rate = float(cached["rate"])
                as_of = cached.get("as_of")
                ok_ts = _parse_fx_ts(as_of) or 0.0
            else:  # 旧格式只有裸 float: 来源可证, 时间不可证 → 交给 stale=True
                rate = float(cached)
                as_of, ok_ts = None, 0.0
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError(f"异常汇率: {rate}")
            cache.update(
                rate=rate, ts=time.time(), last_ok_ts=ok_ts,
                source="remote_cache", as_of=as_of,
            )
            return cache["rate"]
        except (TypeError, ValueError, KeyError):
            pass
    try:
        # 2026-08-12: 数据源从新浪(hq.sinajs.cn)换成腾讯 qt.gtimg.cn——
        # 海外节点新浪超时(冷启动白等 3s×2), 腾讯 0.1s 秒回且支持外汇。
        # 腾讯格式: v_whUSDCNY="310~美元人民币~USDCNY~6.7461~0~时间~..."; GBK 编码
        resp = httpx.get(url, timeout=3, headers={
            "User-Agent": "Mozilla/5.0",
        })
        text = resp.content.decode("gbk", "replace")
        if "=" in text and "~" in text:
            val = text.split("=", 1)[1].strip().strip(";").strip('"')
            parts = val.split("~")
            if len(parts) > 3:
                rate = float(parts[3])  # 第4字段 = 现价
                if not math.isfinite(rate) or rate <= 0:
                    raise ValueError(f"异常汇率: {rate}")
                as_of = datetime.now(timezone.utc).isoformat()
                cache.update(
                    rate=rate, ts=time.time(), last_ok_ts=time.time(),
                    source="live", as_of=as_of,
                )  # 成功: 长 TTL
                biz_cache.set_json(
                    redis_key, {"rate": rate, "as_of": as_of}, ttl=EXCHANGE_RATE_TTL
                )
                logger.info(f"更新{name}汇率: {rate}")
                return rate
    except Exception as e:
        # 海外节点超时(2026-08-12): 失败也刷新 ts(短TTL), 防每次请求都撞超时
        logger.warning(f"获取{name}汇率失败，使用缓存: {e}")
    if time.time() - cache["ts"] >= fail_ttl:
        cache["ts"] = time.time()  # 失败: 短 TTL 后重试
    return cache["rate"]


def get_hkd_cny_rate() -> float:
    """获取港币兑人民币汇率(腾讯 qt.gtimg.cn, 海外可达)"""
    global _hkd_rate_cache
    with _fx_lock:
        return _fetch_fx_rate(
            "https://qt.gtimg.cn/q=whHKDCNY", _hkd_rate_cache, "港币", "fx:rate:hkd"
        )


def get_usd_cny_rate() -> float:
    """获取美元兑人民币汇率(腾讯 qt.gtimg.cn, 海外可达)"""
    global _usd_rate_cache
    with _fx_lock:
        return _fetch_fx_rate(
            "https://qt.gtimg.cn/q=whUSDCNY", _usd_rate_cache, "美元", "fx:rate:usd"
        )


# ========== Pydantic Models ==========

class AccountCreate(BaseModel):
    name: str
    # 非有限数(NaN/Inf)不是金额: 从入参就拦掉, 不许写进库。
    available_funds: float = Field(default=0, allow_inf_nan=False)


class AccountUpdate(BaseModel):
    name: str | None = None
    available_funds: float | None = Field(default=None, allow_inf_nan=False)
    enabled: bool | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    # NULL = 该账户从未记过可用资金(老数据) —— 是"未知", 不是 0。
    available_funds: float | None = None
    enabled: bool

    class Config:
        from_attributes = True


class PositionCreate(BaseModel):
    account_id: int
    stock_id: int
    cost_price: float = Field(allow_inf_nan=False)
    quantity: int
    invested_amount: float | None = Field(default=None, allow_inf_nan=False)
    trading_style: str | None = None  # short: 短线, swing: 波段, long: 长线
    # 防守线(绝对价): 建仓时若用户已经想好纪律位可一并带上, 省略即"未设置"。
    # 非有限数(NaN/Inf)一律拒收 —— 存进去既不是价格, 又会让判定式静默失效。
    defense_price: float | None = Field(default=None, allow_inf_nan=False)


class PositionUpdate(BaseModel):
    cost_price: float | None = Field(default=None, allow_inf_nan=False)
    quantity: int | None = None
    invested_amount: float | None = Field(default=None, allow_inf_nan=False)
    trading_style: str | None = None


class DefenseLineUpdate(BaseModel):
    """防守线设置载荷(方案 B)。

    `null` 即**清除** —— 本端点只做"设置/清除"一件事, 故不区分"字段未传"与"传 null"。
    比例式(-8%)由前端按成本价折算成绝对价再提交, 后端只存用户拍下的那个价。
    `allow_inf_nan=False`: NaN/Inf 不是价格, 在 422 就挡掉, 不落到 DB
    (`value <= 0` 对 NaN 恒为 False, 旧校验会把它当合法价存进去)。
    """

    defense_price: float | None = Field(default=None, allow_inf_nan=False)


class PositionResponse(BaseModel):
    id: int
    account_id: int
    stock_id: int
    # 历史脏成本(NaN/Inf)在出参处已有限化: 未知即 null, 不用 0 冒充。
    cost_price: float | None
    quantity: int
    invested_amount: float | None
    sort_order: int
    trading_style: str | None
    # 用户自设防守线(绝对价); None = 未设置, 前端据此只提示不告警。
    defense_price: float | None = None
    # 关联信息
    account_name: str | None = None
    stock_symbol: str | None = None
    stock_name: str | None = None

    class Config:
        from_attributes = True


class PositionReorderItem(BaseModel):
    id: int
    sort_order: int


class PositionReorderRequest(BaseModel):
    items: list[PositionReorderItem]


# ========== 归属门禁(2026-10-29 跨租户越权修复) ==========
# 读: 账户可见 **且** 持仓可见(src.core.portfolio_scope 唯一口径)。
# 写: 在可读之上再区分 本人 / 共享(NULL, 仅 owner); 他人私有项一律 404(不泄露存在性)。


def _visible_account_or_404(db: Session, account_id: int, user: User) -> Account:
    account = user_scope_accounts(db, user.id).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")
    return account


def _writable_account_or_error(db: Session, account_id: int, user: User) -> Account:
    account = _visible_account_or_404(db, account_id, user)
    if not account_user_scope_writable(account, getattr(user, "id", None), getattr(user, "role", None)):
        # 全局共享账户(account.user_id=NULL)删/改会波及所有人的持仓 → 仅 owner。
        raise HTTPException(403, "共享账户仅 owner 可修改")
    return account


def _writable_position_or_404(db: Session, position_id: int, user: User) -> Position:
    position = db.query(Position).filter(Position.id == position_id).first()
    account = position.account if position is not None else None
    uid, role = getattr(user, "id", None), getattr(user, "role", None)
    if not position_user_scope_visible(position, account, uid):
        # 父账户不可见 / 持仓归他人 / 账户与持仓归属错位: 统一 404。
        raise HTTPException(404, "持仓不存在")
    if not position_user_scope_writable(position, account, uid, role):
        raise HTTPException(403, "共享持仓仅 owner 可修改")
    assert position is not None  # 可读即非空, 仅为类型收窄
    return position


# ========== Account Endpoints ==========

@router.get("/accounts", response_model=list[AccountResponse])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取当前用户的账户(含全局共享)"""
    return user_scope_accounts(db, user.id).order_by(Account.id).all()


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取单个账户"""
    return _visible_account_or_404(db, account_id, user)


@router.post("/accounts", response_model=AccountResponse)
def create_account(data: AccountCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """创建账户"""
    account = Account(name=data.name, available_funds=data.available_funds, user_id=user.id)
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(f"创建账户: {account.name}")
    return account


@router.put("/accounts/{account_id}", response_model=AccountResponse)
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新账户(共享账户仅 owner)"""
    account = _writable_account_or_error(db, account_id, user)

    if data.name is not None:
        account.name = data.name
    if data.available_funds is not None:
        account.available_funds = data.available_funds
    if data.enabled is not None:
        account.enabled = data.enabled

    db.commit()
    db.refresh(account)
    logger.info(f"更新账户: {account.name}")
    return account


@router.delete("/accounts/{account_id}")
def delete_account(
    account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """删除账户（会同时删除该账户的所有持仓）

    S5(2026-08-26): 归属校验 —— 他人账户返回 404 防账号探测。
    2026-10-29: 共享账户(account.user_id=NULL)跨用户, 删除会带走他人持仓 → 仅 owner 可删(403)。
    """
    account = _writable_account_or_error(db, account_id, user)
    account_name = account.name

    db.delete(account)
    db.commit()
    logger.info(f"删除账户: {account_name}")
    return {"success": True}


# ========== Position Endpoints ==========

def _defense_line_out(value) -> float | None:
    """防守线对外口径: 只有正数且有限才是"用户设过的价"。

    历史脏数据(NaN/Inf/0/负数)一律按未设置返回 None —— 否则整份响应不是合法 JSON,
    前端还会拿着永远不成立的防线继续告警。
    """
    px = _finite_float(value)
    return px if px is not None and px > 0 else None


def _position_payload(pos: Position) -> dict:
    """持仓响应体(四处端点共用一份口径)。

    为什么收敛成一处: `defense_price` 若只加在部分端点, 前端在"刚设完再拉列表"和
    "建仓返回"两条路径上会拿到互斥的字段集, 一侧永远看不到用户自己设的防线。
    """
    return {
        "id": pos.id,
        "account_id": pos.account_id,
        "stock_id": pos.stock_id,
        # 四审(2026-10-29): 历史脏成本 `NaN`/`Inf` 直接出参 → 响应不是合法 JSON
        # (/api/positions 500)。这里统一有限化, 取不到就给 None(成本未知, 不用 0 冒充)。
        "cost_price": _finite_float(pos.cost_price),
        "quantity": pos.quantity,
        "invested_amount": _finite_float(pos.invested_amount),
        "sort_order": pos.sort_order or 0,
        "trading_style": pos.trading_style,
        "defense_price": _defense_line_out(getattr(pos, "defense_price", None)),
        "account_name": pos.account.name if pos.account else None,
        "stock_symbol": pos.stock.symbol if pos.stock else None,
        "stock_name": pos.stock.name if pos.stock else None,
    }


@router.get("/positions", response_model=list[PositionResponse])
def list_positions(
    account_id: int | None = None,
    stock_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取持仓列表，可按账户或股票筛选(仅当前用户的)"""
    query = user_scope_positions(db, user.id, account_id=account_id, stock_id=stock_id)

    positions = query.order_by(Position.account_id.asc(), Position.sort_order.asc(), Position.id.asc()).all()
    return [_position_payload(pos) for pos in positions]


def _is_unique_conflict(exc: IntegrityError) -> bool:
    """区分"唯一约束冲突"(可解释为 409) 与其它完整性错误(外键等, 不该吞)。

    PG 用 SQLSTATE 23505; SQLite 的原文是 "UNIQUE constraint failed: ..."。
    不能一律 409: 账户在预检与写入之间被删掉会抛外键错误, 那是真故障。
    """
    orig = getattr(exc, "orig", None) or exc
    if getattr(orig, "pgcode", None) == "23505":
        return True
    text = str(orig).lower()
    return "unique constraint failed" in text or "duplicate key value" in text


@router.post("/positions", response_model=PositionResponse)
def create_position(data: PositionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """创建持仓。

    归属口径与 position/defense/delete 统一(2026-10-29 复审):

    - 账户**可写**才建仓: 共享账户(user_id=NULL)仅 owner, 他人私有账户 404;
      此前只校验"可读", member 能把持仓写进共享账户, 违背"共享仅 owner 写";
    - 标的同样要**可见**: 他人私有自选行(user_id=别人)一律按"股票不存在"拒,
      不能把持仓挂到别人的 Stock 行上(对方删自选会级联删掉我的持仓);
    - 去重与 sort_order 只看**本租户可见**的持仓: 他人(共享账户里别人建的 /
      数据错位的行)同票持仓既不参与判断, 也不把"账户 X 已有 Y"这种带名字的
      文案回显出去 —— 那等于告诉 A "B 也持有这只票";
    - 真正的唯一约束冲突(并发, 或唯一行对当前租户不可见)返回**通用 409**,
      回滚后不留下半条记录, 文案不带账户名/票名/归属人。
    """
    account = _writable_account_or_error(db, data.account_id, user)

    # 标的也必须在本租户可见范围(2026-10-29 三审): 旧写法只按主键取 Stock,
    # 于是 A 可以传 B 的私有自选 stock_id 建仓 —— 持仓挂到 B 的自选行上, B 删自选
    # 时级联把 A 的持仓带走; 同时 "股票不存在" 与 404 的差异也成了存在性探针。
    from src.core.portfolio_scope import stock_user_scope_clause

    stock = (
        db.query(Stock)
        .filter(Stock.id == data.stock_id, stock_user_scope_clause(user.id))
        .first()
    )
    if not stock:
        # 他人私有标的与不存在统一 404/400 同文案, 不给存在性信号。
        raise HTTPException(400, "股票不存在")

    # 预检收敛到本租户可见范围: 自己的持仓才提示"编辑现有持仓"。
    existing = user_scope_positions(
        db, user.id, account_id=account.id, stock_id=stock.id
    ).first()
    if existing:
        raise HTTPException(400, f"账户 {account.name} 已有 {stock.name} 的持仓，请编辑现有持仓")

    max_order = (
        user_scope_positions(db, user.id, account_id=account.id)
        .with_entities(func.max(Position.sort_order))
        .scalar()
    ) or 0

    position = Position(
        account_id=data.account_id,
        stock_id=data.stock_id,
        cost_price=data.cost_price,
        quantity=data.quantity,
        invested_amount=data.invested_amount,
        sort_order=int(max_order) + 1,
        trading_style=data.trading_style,
        user_id=user.id,
        defense_price=_defense_line_out(data.defense_price),
    )
    db.add(position)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_unique_conflict(exc):
            raise
        # uq_account_stock: 并发建同一 (account, stock), 或该唯一行对我不可见。
        # 一律通用文案 —— 说明"存在"已经足够, 再带名字就是越权泄漏。
        logger.info(
            "建仓唯一约束冲突(不泄露归属): account_id=%s stock_id=%s user=%s",
            data.account_id,
            data.stock_id,
            user.id,
        )
        raise HTTPException(
            409,
            "该账户下该标的的持仓已存在或正在创建，请刷新后重试",
        ) from None
    db.refresh(position)

    logger.info(f"创建持仓: {account.name} - {stock.name}")
    return _position_payload(position)


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, data: PositionUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新持仓"""
    position = _writable_position_or_404(db, position_id, user)

    if data.cost_price is not None:
        position.cost_price = data.cost_price
    if data.quantity is not None:
        position.quantity = data.quantity
    if data.invested_amount is not None:
        position.invested_amount = data.invested_amount
    if data.trading_style is not None:
        # 空字符串表示清空，设为 None
        position.trading_style = data.trading_style if data.trading_style else None

    db.commit()
    db.refresh(position)

    logger.info(f"更新持仓: {position.account.name} - {position.stock.name}")
    return _position_payload(position)


@router.put("/positions/{position_id}/defense", response_model=PositionResponse)
def set_position_defense_line(
    position_id: int,
    data: DefenseLineUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """设置/清空用户自设防守线(方案 B)。

    `defense_price=null` 即清除。清除后该持仓**不再有**告警基准 —— 系统不会退回
    默认 -8%, 因为默认 -8% 已按产品裁决降级为"仅作参考展示"。
    归属口径: 自己的持仓可写; 历史共享(NULL)持仓仅 owner; 他人私有持仓 404。
    """
    position = _writable_position_or_404(db, position_id, user)

    value = data.defense_price
    if value is not None:
        # 0/负数/非有限数都不是价格, 也不是"清除"的写法 —— 直接拒, 免得存下永远触发的防线。
        px = _finite_float(value)
        if px is None or px <= 0:
            raise HTTPException(400, "防守线必须是正数价格")
        position.defense_price = px
    else:
        position.defense_price = None

    db.commit()
    db.refresh(position)
    logger.info(
        "更新防守线: %s - %s -> %s",
        position.account.name if position.account else position.account_id,
        position.stock.name if position.stock else position.stock_id,
        position.defense_price,
    )
    return _position_payload(position)


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """删除持仓"""
    position = _writable_position_or_404(db, position_id, user)
    label = (
        position.account.name if position.account else position.account_id,
        position.stock.name if position.stock else position.stock_id,
    )

    db.delete(position)
    db.commit()
    logger.info("删除持仓: %s - %s", *label)
    return {"success": True}


@router.put("/positions/reorder/batch")
def reorder_positions(data: PositionReorderRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量更新持仓排序(只改自己可写的; 他人私有/共享 NULL 持仓一律跳过)"""
    if not data.items:
        return {"updated": 0}
    ids = [int(x.id) for x in data.items]
    uid, role = getattr(user, "id", None), getattr(user, "role", None)
    rows = user_scope_positions(db, uid).filter(Position.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in data.items:
        row = row_map.get(int(item.id))
        if not row or not position_user_scope_writable(row, row.account, uid, role):
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


# ========== 行情可用性(2026-10-29 缺行情伪造收益缺陷) ==========
# 原则: 没有可用的现价就没有市值 —— 绝不用成本顶替、绝不记 0、绝不出 -100%。
# 股票行情里 0 / 负数 / NaN 不是有效成交价(停牌、坏数据、字段缺失), 一律视为无效报价;
# 原始值保留在 raw_current_price 供排查, 但绝不参与市值/盈亏计算。

QUOTE_OK = "ok"                  # 有可用现价, 参与计价
QUOTE_MISSING = "missing"        # 行情源没有这只票的现价
QUOTE_INVALID = "invalid_price"  # 有现价字段但不可用(<=0 / 非数值 / NaN)
QUOTE_NOT_REQUESTED = "not_requested"  # 调用方显式不要行情(include_quotes=false)
QUOTE_PARTIAL = "partial"        # 多账户同标的中部分仓位有价(仅合并层出现)


def _finite_float(value) -> float | None:
    """数值化; None/非数值/NaN/Inf 一律视为「不可用」。0 是有效数值, 不动它。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _valid_price(value) -> float | None:
    """把行情里的价格字段转成可用现价; 不可用返回 None(0 不是合法股票现价)。"""
    px = _finite_float(value)
    return px if px is not None and px > 0 else None


def _safe_amount(value) -> float:
    """金额/数量兜底: NULL / NaN / Inf / 非数值 → 0.0(可用于加法, 不污染 JSON)。"""
    f = _finite_float(value)
    return f if f is not None else 0.0


def _resolve_quote_price(quote: dict | None, requested: bool = True) -> tuple[float | None, str, float | None]:
    """解析一条行情 → (可用现价, 状态, 原始现价)。

    原始现价只在**有限数值**时回传: NaN/Inf 会被标准 json 输出成非法 token,
    直接破坏整份响应, 故坏值一律 raw=None(原因已由 invalid_price 表达)。
    """
    if not requested:
        return None, QUOTE_NOT_REQUESTED, None
    if not quote:
        return None, QUOTE_MISSING, None
    raw = quote.get("current_price")
    px = _valid_price(raw)
    if px is None:
        if raw is None:
            return None, QUOTE_MISSING, None
        return None, QUOTE_INVALID, _finite_float(raw)
    return px, QUOTE_OK, px


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    """四舍五入; None/NaN/Inf 保持 None(真实的 0 是有效数值)。"""
    f = _finite_float(value)
    return round(f, digits) if f is not None else None


def _quotes_coverage(
    *,
    requested: bool,
    positions_total: int,
    positions_priced: int,
    priced_cost: float,
    total_cost: float,
) -> dict:
    """覆盖度声明: 让消费方一眼看出市值/盈亏算的是「全部持仓」还是「已定价子集」。

    三种「完整」互斥且自洽:
      - no_positions: 本来就没有持仓, 0/0 是完整事实(不是缺行情);
      - all_positions: 有持仓且全部有价;
      - priced_positions_only: 部分缺价, 市值/盈亏只覆盖已定价子集。
    not_requested 时缺失数是「不适用」(None), 不能拿持仓总数冒充缺失数。
    """
    total = max(0, int(positions_total or 0))
    if not requested:
        return {
            "requested": False,
            "positions_total": total,
            "positions_priced": None,
            "positions_missing": None,
            "cost_coverage_pct": None,
            "pricing_basis": "not_requested",
            "complete": False,
        }
    priced = max(0, min(int(positions_priced or 0), total))
    missing = total - priced
    if total == 0:
        basis, complete = "no_positions", True
    elif missing == 0:
        basis, complete = "all_positions", True
    else:
        basis, complete = "priced_positions_only", False
    cost_pct = round(priced_cost / total_cost * 100, 2) if total_cost > 0 else None
    return {
        "requested": True,
        "positions_total": total,
        "positions_priced": priced,
        "positions_missing": missing,
        "cost_coverage_pct": cost_pct,
        "pricing_basis": basis,
        "complete": complete,
    }


def _daily_pnl_coverage(
    *, requested: bool, positions_total: int, positions_observed: int
) -> dict:
    """当日盈亏覆盖度: 缺现价或缺昨日收盘的仓位都不该被当成「0 涨跌」。"""
    total = max(0, int(positions_total or 0))
    if not requested:
        return {
            "requested": False,
            "positions_total": total,
            "positions_observed": None,
            "complete": False,
        }
    observed = max(0, min(int(positions_observed or 0), total))
    return {
        "requested": True,
        "positions_total": total,
        "positions_observed": observed,
        "complete": observed == total,
    }


def _accumulate_holding(
    seen: dict,
    statuses: dict[tuple[str, str], set[str]],
    key: tuple[str, str],
    *,
    symbol: str,
    market: str,
    name: str,
    quantity: int | float,
    fx: float,
    cost_cny: float,
    price: float | None,
    quote_status: str,
    strategy_code: str,
    fx_source: str = "default",
    fx_stale: bool = True,
) -> dict:
    """把一笔持仓并入按 (market, symbol) 聚合的持仓篮子, 并返回该篮子。

    数量与已定价市值必须同篮子: `quantity` 是全量, 只有 `priced_quantity` 乘现价进市值;
    合并状态由各腿的状态聚合得出(与遍历顺序无关), 部分腿缺价时是 partial, 不是 ok。
    """
    qty = _safe_amount(quantity)
    h = seen.get(key)
    if h is None:
        h = {
            "symbol": symbol,
            "market": market,
            "name": name,
            "quantity": 0.0,
            "priced_quantity": 0.0,
            "unpriced_quantity": 0.0,
            "fx": fx,
            # None 表示「现价不可用」, 消费方不得按 0 或成本理解。
            "market_value": None,
            "unrealized_pnl": None,
            "cost_cny": 0.0,
            "priced_cost_cny": 0.0,
            "quote_status": quote_status,
            "priced_position_count": 0,
            "missing_quote_count": 0,
            "position_count": 0,
            "fx_source": fx_source,
            "fx_stale": fx_stale,
            "strategy_code": strategy_code,
        }
        seen[key] = h
    h["quantity"] += qty
    h["cost_cny"] += cost_cny
    h["position_count"] += 1
    statuses.setdefault(key, set()).add(quote_status)
    if price is not None:
        mv_cny = price * qty * fx
        h["market_value"] = (h["market_value"] or 0.0) + mv_cny
        h["unrealized_pnl"] = (h["unrealized_pnl"] or 0.0) + (mv_cny - cost_cny)
        h["priced_quantity"] += qty
        h["priced_cost_cny"] += cost_cny
        h["priced_position_count"] += 1
    else:
        h["unpriced_quantity"] += qty
        h["missing_quote_count"] += 1
    h["fx_stale"] = bool(h["fx_stale"] or fx_stale)
    # 状态聚合: 全有价 ok / 全缺价取最具体原因 / 混合 partial。
    if h["priced_position_count"] == 0:
        h["quote_status"] = (
            QUOTE_INVALID if QUOTE_INVALID in statuses[key] else QUOTE_MISSING
        )
    elif h["priced_position_count"] == h["position_count"]:
        h["quote_status"] = QUOTE_OK
    else:
        h["quote_status"] = QUOTE_PARTIAL
    return h


# ========== Portfolio Summary ==========

def _portfolio_positions(
    db: Session, user: User | None, *, account_id: int | None = None
) -> list[Position]:
    """SQL 层归属隔离: 账户可见(自己的或历史 NULL) **且** 持仓可见(自己的或历史 NULL)。

    为什么不能直接遍历 `acc.positions`: 共享/遗留账户(account.user_id IS NULL)下可能
    混入其他用户建的 Position(Position.user_id 有值), 关系遍历会把别人的持仓算进
    当前用户的成本/市值/诊断。这里把与 list_positions 同款的门禁下沉到 SQL,
    绝不放宽成"账户可见即全部持仓可见"。
    无用户会话(user=None)只对应历史共享桶(NULL/NULL), 不会退化成全表。
    """
    return (
        user_scope_positions(
            db,
            getattr(user, "id", None),
            account_id=account_id,
            enabled_accounts_only=True,
        )
        .all()
    )


@router.get("/portfolio/summary")
def get_portfolio_summary(
    account_id: int | None = None,
    include_quotes: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    获取持仓汇总信息

    Args:
        account_id: 可选，指定账户ID。不指定则汇总所有账户

    Returns:
        accounts: 账户列表及各账户持仓明细
        total: 所有账户汇总

    字段契约(2026-10-29 缺行情修复 + 复审收紧, 消费方必须按此解释):
      - 现价不可用(缺行情 / 0 / 负数 / NaN / Inf)时: current_price、market_value、
        market_value_cny、pnl、pnl_pct 全为 None; quote_status 说明原因(missing /
        invalid_price / not_requested / partial); raw_current_price 只回传有限数值
        (NaN/Inf 传 None, 否则整份响应不是合法 JSON)。
      - total_market_value / total_pnl / total_pnl_pct 只在**已定价子集**上计算:
        None = 一只都没价(未知), 不是 0, 更不是 -100%。
      - total_cost 是全持仓成本(已知事实); priced_cost / unpriced_cost 把已定价与未定价拆开,
        与 total_market_value 同一篮子。
      - total_assets 是"总资产"语义: 只有全部持仓有价(或本来没持仓)才给数字且
        total_assets_complete=True; 部分/全部缺价时为 None, basis 写明口径,
        子集口径另给 total_assets_priced_subset, 不得把子集当总资产展示。
      - total_daily_pnl 只在"有现价且有昨日收盘"的仓位上累加; 一只都没观测到时为 None。
        daily_pnl_coverage 给出 positions_total / positions_observed / complete,
        部分覆盖时数字只是已观测部分。
      - quotes_coverage 给出持仓总数 / 已定价数 / 缺失数 / 成本覆盖率 / pricing_basis:
        no_positions(0/0 完整) / all_positions / priced_positions_only / not_requested。
      - include_quotes=false 是显式"不计价"模式(前端用本地行情重算), 保持旧数值 0 语义,
        以 quotes_coverage.requested=false 区分; 该模式下 positions_priced / positions_missing
        为 None(不适用), 不要当成"缺了 N 只行情"。
      - exchange_rate_meta 标明 HKD_CNY / USD_CNY 的来源(live / remote_cache / default)、
        as_of、age_seconds、stale; source=default 或 stale=True 时港美市值换算不可当实时事实。
    """
    # 获取账户(仅当前用户的)
    accounts = user_scope_accounts(db, user.id, enabled_only=True).filter(
        *([Account.id == account_id] if account_id else [])
    ).all()

    if not accounts:
        return {
            "accounts": [],
            "total": {
                "total_market_value": 0,
                "total_cost": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "total_daily_pnl": 0,  # 没有账户/持仓: 0 是真实事实
                "daily_pnl_period": "unknown",
                "daily_pnl_label": "当日盈亏",
                "daily_pnl_date": None,
                "available_funds": 0,
                # 没有任何账户: 现金合计是空集之和(真实的 0), 不涉及 NULL 未知。
                "available_funds_known": True,
                "total_assets": 0,
                "total_assets_complete": True,
                "total_assets_basis": "no_positions",
                "total_assets_priced_subset": 0,
                "priced_cost": 0,
                "unpriced_cost": 0,
                "quotes_coverage": _quotes_coverage(
                    requested=include_quotes,
                    positions_total=0,
                    positions_priced=0,
                    priced_cost=0.0,
                    total_cost=0.0,
                ),
                "daily_pnl_coverage": _daily_pnl_coverage(
                    requested=include_quotes, positions_total=0, positions_observed=0
                ),
            },
        }

    # 持仓在 SQL 层按归属过滤(共享账户里别人的 Position 不得混入), 再按账户分组。
    positions = _portfolio_positions(db, user, account_id=account_id)
    positions_by_account: dict[int, list[Position]] = {}
    for pos in positions:
        positions_by_account.setdefault(pos.account_id, []).append(pos)

    all_stock_ids = {pos.stock_id for pos in positions}
    stocks = db.query(Stock).filter(Stock.id.in_(all_stock_ids)).all() if all_stock_ids else []
    stock_map = {s.id: s for s in stocks}

    # 获取实时行情（可选）
    quotes = _fetch_quotes_for_stocks(stocks) if include_quotes else {}

    # 获取汇率 + 来源声明(默认值/过期值必须显式标出, 不得冒充当日汇率)
    hkd_rate = get_hkd_cny_rate()
    usd_rate = get_usd_cny_rate()
    hkd_meta = _fx_meta(_hkd_rate_cache, "HKD_CNY", hkd_rate)
    usd_meta = _fx_meta(_usd_rate_cache, "USD_CNY", usd_rate)

    # 计算各账户持仓
    account_summaries = []
    grand_total_market_value = 0
    grand_total_cost = 0
    grand_priced_cost = 0
    grand_unpriced_cost = 0
    grand_positions_count = 0
    grand_priced_count = 0
    grand_available_funds = 0
    grand_funds_known = True  # 任一账户 available_funds 为 NULL → 汇总现金是"未知", 不是 0
    grand_daily_pnl = 0
    grand_daily_pnl_observed = 0
    grand_daily_pnl_periods: list[tuple[str, str | None]] = []

    for acc in accounts:
        positions_data = []
        acc_market_value = 0
        acc_cost = 0
        acc_priced_cost = 0
        acc_unpriced_cost = 0
        acc_priced_count = 0
        acc_positions_count = 0
        acc_daily_pnl = 0
        acc_daily_pnl_observed = 0
        acc_daily_pnl_periods: list[tuple[str, str | None]] = []
        # available_funds 可为 NULL(老数据): NULL 是"没记过现金", 不是 0 —— 输出 None,
        # 并且下游 total_assets 不得把它当 0 加进总资产(见 acc_assets_* 分支)。
        funds = _finite_float(acc.available_funds)
        funds_known = funds is not None
        funds_value = funds if funds_known else 0.0

        positions_sorted = sorted(
            positions_by_account.get(acc.id, []),
            key=lambda p: (int(getattr(p, "sort_order", 0) or 0), int(p.id)),
        )
        for pos in positions_sorted:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue

            qty = _safe_amount(pos.quantity)
            acc_positions_count += 1
            quote = quotes.get(stock.symbol)
            # 没有可用现价就没有市值(不填 0、不用成本顶替), 见文件顶部"行情可用性"说明。
            current_price, quote_status, raw_price = _resolve_quote_price(quote, requested=include_quotes)
            priced = current_price is not None
            # 昨日收盘同属行情字段: 坏值(NaN/Inf/<=0)等于没有, 不得参与当日盈亏。
            prev_close = _valid_price(quote.get("prev_close")) if quote else None
            change_pct = _finite_float(quote.get("change_pct")) if (quote and priced) else None
            quote_time = quote.get("quote_time") if quote else None
            quote_date = quote.get("quote_date") if quote else None
            daily_pnl_period = classify_quote_period(quote_date, stock.market)

            # 根据市场确定汇率
            is_foreign = stock.market in ("HK", "US")
            fx_meta = None
            if stock.market == "HK":
                rate, fx_meta = hkd_rate, hkd_meta
            elif stock.market == "US":
                rate, fx_meta = usd_rate, usd_meta
            else:
                rate = 1.0

            market_value = None
            market_value_cny = None
            pnl = None
            pnl_pct = None
            daily_pnl = None
            daily_pnl_pct = None

            if priced and prev_close is not None:
                daily_pnl = (current_price - prev_close) * qty * rate
                daily_pnl_pct = (current_price - prev_close) / prev_close * 100
                acc_daily_pnl += daily_pnl
                acc_daily_pnl_observed += 1
                grand_daily_pnl_observed += 1
                observation = (daily_pnl_period, quote_date)
                acc_daily_pnl_periods.append(observation)
                grand_daily_pnl_periods.append(observation)

            cost = _safe_amount(pos.cost_price) * qty
            cost_cny = cost * rate  # 假设成本价也是原币种
            acc_cost += cost_cny

            if priced:
                market_value = current_price * qty  # 原币种市值
                market_value_cny = market_value * rate  # 人民币市值
                pnl = market_value_cny - cost_cny
                pnl_pct = (pnl / cost_cny * 100) if cost_cny > 0 else None

                acc_market_value += market_value_cny
                acc_priced_cost += cost_cny
                acc_priced_count += 1
            else:
                acc_unpriced_cost += cost_cny

            positions_data.append({
                "id": pos.id,
                "stock_id": pos.stock_id,
                "symbol": stock.symbol,
                "name": stock.name,
                "market": stock.market,
                "cost_price": _finite_float(pos.cost_price),
                "quantity": pos.quantity,
                "invested_amount": _finite_float(pos.invested_amount),
                "sort_order": pos.sort_order or 0,
                "trading_style": pos.trading_style,
                # 用户自设防守线(绝对价, 原币种): 前端据此判定"有没有纪律位", None 即未设置。
                "defense_price": _defense_line_out(getattr(pos, "defense_price", None)),
                "current_price": current_price,
                # 行情源给的原始现价(仅有限数值; 0 保留用于区分"坏值"与"缺字段")。
                "raw_current_price": raw_price,
                "quote_status": quote_status,
                "priced": priced,
                "current_price_cny": _round_or_none(current_price * rate) if priced else None,
                "change_pct": change_pct,
                "market_value": _round_or_none(market_value),
                "market_value_cny": _round_or_none(market_value_cny),
                "pnl": _round_or_none(pnl),
                "pnl_pct": _round_or_none(pnl_pct),
                "daily_pnl": _round_or_none(daily_pnl),
                "daily_pnl_pct": _round_or_none(daily_pnl_pct),
                "daily_pnl_period": daily_pnl_period,
                "quote_time": quote_time,
                "quote_date": quote_date,
                "exchange_rate": _round_or_none(rate, 6) if is_foreign else None,
                # 港美换算用的汇率是否可信: default/stale 时市值不是当日事实。
                "exchange_rate_source": fx_meta["source"] if fx_meta else None,
                "exchange_rate_stale": fx_meta["stale"] if fx_meta else None,
            })

        # 账户口径: 市值/盈亏只在已定价子集上算(分子分母同篮子); 缺价仓位只进未定价成本,
        # 不进市值也不进盈亏 → 不会出现"成本在分母、市值缺席"的假亏损。
        # 现金 NULL 时现金是"未知": 不能当 0 加进总资产, basis 标 available_funds_unknown。
        if not include_quotes:
            acc_pnl = 0
            acc_pnl_pct = 0
            acc_market_value_out: float | None = acc_market_value  # 不计价模式: 保持旧数值 0
            acc_total_assets = funds if funds_known else None
            acc_assets_complete = False
            acc_assets_basis = "not_requested" if funds_known else "available_funds_unknown"
            acc_assets_subset: float | None = None
            acc_daily_pnl_out: float | None = 0
        else:
            acc_assets_subset = (acc_market_value + funds_value) if (acc_priced_count and funds_known) else None
            if acc_positions_count == 0:
                # 空账户: 没有持仓就是真的 0 盈亏/0 市值; 总资产=可用资金(NULL 则未知)。
                acc_pnl, acc_pnl_pct = 0, 0
                acc_market_value_out = 0
                acc_total_assets = funds if funds_known else None
                if funds_known:
                    acc_assets_complete, acc_assets_basis = True, "no_positions"
                else:
                    acc_assets_complete, acc_assets_basis = False, "available_funds_unknown"
                acc_daily_pnl_out = 0
            elif acc_priced_count == 0:
                # 有持仓但一只都没价: 市值/盈亏/总资产都是未知, 不给数字。
                acc_pnl = acc_pnl_pct = None
                acc_market_value_out = None
                acc_total_assets = None
                acc_assets_complete, acc_assets_basis = False, "no_priced_positions"
                acc_daily_pnl_out = None
            else:
                acc_pnl = acc_market_value - acc_priced_cost
                acc_pnl_pct = (acc_pnl / acc_priced_cost * 100) if acc_priced_cost > 0 else None
                acc_market_value_out = acc_market_value
                complete = acc_priced_count == acc_positions_count
                if complete and funds_known:
                    acc_total_assets = acc_market_value + funds_value
                    acc_assets_complete, acc_assets_basis = True, "all_positions"
                elif complete:
                    # 全部持仓有价, 但现金未知 → 总资产仍不可知(不得把 NULL 当 0)。
                    acc_total_assets = None
                    acc_assets_complete, acc_assets_basis = False, "available_funds_unknown"
                else:
                    # 子集市值 + 全部资金 ≠ 总资产, 只给子集口径, 不冒充总额。
                    acc_total_assets = None
                    acc_assets_complete, acc_assets_basis = False, "priced_positions_only"
                acc_daily_pnl_out = round(acc_daily_pnl, 2) if acc_daily_pnl_observed else None

        account_summaries.append({
            "id": acc.id,
            "name": acc.name,
            # NULL = 从未记录可用资金(未知), 不得按 0 展示。
            "available_funds": _round_or_none(funds),
            "available_funds_known": funds_known,
            "total_market_value": _round_or_none(acc_market_value_out),
            "total_cost": round(acc_cost, 2),
            "priced_cost": round(acc_priced_cost, 2),
            "unpriced_cost": round(acc_unpriced_cost, 2),
            "total_pnl": _round_or_none(acc_pnl),
            "total_pnl_pct": _round_or_none(acc_pnl_pct),
            "total_daily_pnl": _round_or_none(acc_daily_pnl_out),
            "total_assets": _round_or_none(acc_total_assets),
            "total_assets_complete": acc_assets_complete,
            "total_assets_basis": acc_assets_basis,
            "total_assets_priced_subset": _round_or_none(acc_assets_subset),
            "quotes_coverage": _quotes_coverage(
                requested=include_quotes,
                positions_total=acc_positions_count,
                positions_priced=acc_priced_count,
                priced_cost=acc_priced_cost,
                total_cost=acc_cost,
            ),
            "daily_pnl_coverage": _daily_pnl_coverage(
                requested=include_quotes,
                positions_total=acc_positions_count,
                positions_observed=acc_daily_pnl_observed,
            ),
            "positions": positions_data,
            **summarize_daily_pnl_period(acc_daily_pnl_periods),
        })

        grand_total_market_value += acc_market_value
        grand_total_cost += acc_cost
        grand_priced_cost += acc_priced_cost
        grand_unpriced_cost += acc_unpriced_cost
        grand_positions_count += acc_positions_count
        grand_priced_count += acc_priced_count
        grand_available_funds += funds_value
        grand_funds_known = grand_funds_known and funds_known
        grand_daily_pnl += acc_daily_pnl

    grand_assets_subset = (
        (grand_total_market_value + grand_available_funds)
        if (grand_priced_count and grand_funds_known)
        else None
    )
    if not include_quotes:
        grand_pnl = 0
        grand_pnl_pct = 0
        grand_market_value_out: float | None = grand_total_market_value
        grand_total_assets = grand_available_funds if grand_funds_known else None
        grand_assets_complete = False
        grand_assets_basis = "not_requested" if grand_funds_known else "available_funds_unknown"
        grand_daily_pnl_out: float | None = 0
    elif grand_positions_count == 0:
        grand_pnl, grand_pnl_pct = 0, 0
        grand_market_value_out = 0
        grand_total_assets = grand_available_funds if grand_funds_known else None
        if grand_funds_known:
            grand_assets_complete, grand_assets_basis = True, "no_positions"
        else:
            grand_assets_complete, grand_assets_basis = False, "available_funds_unknown"
        grand_daily_pnl_out = 0
    elif grand_priced_count == 0:
        # 有持仓但一只都没价: 市值/盈亏/总资产都是未知, 不给数字。
        grand_pnl = grand_pnl_pct = None
        grand_market_value_out = None
        grand_total_assets = None
        grand_assets_complete, grand_assets_basis = False, "no_priced_positions"
        grand_daily_pnl_out = None
    else:
        grand_pnl = grand_total_market_value - grand_priced_cost
        grand_pnl_pct = (grand_pnl / grand_priced_cost * 100) if grand_priced_cost > 0 else None
        grand_market_value_out = grand_total_market_value
        if grand_priced_count == grand_positions_count and grand_funds_known:
            grand_total_assets = grand_total_market_value + grand_available_funds
            grand_assets_complete, grand_assets_basis = True, "all_positions"
        elif grand_priced_count == grand_positions_count:
            # 持仓全有价但现金未知: 总资产仍是"未知", 不得把 NULL 当 0。
            grand_total_assets = None
            grand_assets_complete, grand_assets_basis = False, "available_funds_unknown"
        else:
            grand_total_assets = None
            grand_assets_complete, grand_assets_basis = False, "priced_positions_only"
        grand_daily_pnl_out = round(grand_daily_pnl, 2) if grand_daily_pnl_observed else None

    # 构建 quotes 字典（用于前端股票列表显示; 坏值不得原样透传）
    quotes_dict = {}
    if include_quotes:
        for symbol, quote in quotes.items():
            quotes_dict[symbol] = {
                "current_price": _valid_price(quote.get("current_price")),
                "change_pct": _finite_float(quote.get("change_pct")),
                "quote_time": quote.get("quote_time"),
                "quote_date": quote.get("quote_date"),
            }

    total_daily_pnl_meta = summarize_daily_pnl_period(grand_daily_pnl_periods)
    return {
        "accounts": account_summaries,
        "total": {
            "total_market_value": _round_or_none(grand_market_value_out),
            "total_cost": round(grand_total_cost, 2),
            "priced_cost": round(grand_priced_cost, 2),
            "unpriced_cost": round(grand_unpriced_cost, 2),
            "total_pnl": _round_or_none(grand_pnl),
            "total_pnl_pct": _round_or_none(grand_pnl_pct),
            "total_daily_pnl": _round_or_none(grand_daily_pnl_out),
            "available_funds": _round_or_none(grand_available_funds) if grand_funds_known else None,
            "available_funds_known": grand_funds_known,
            "total_assets": _round_or_none(grand_total_assets),
            "total_assets_complete": grand_assets_complete,
            "total_assets_basis": grand_assets_basis,
            "total_assets_priced_subset": _round_or_none(grand_assets_subset),
            "quotes_coverage": _quotes_coverage(
                requested=include_quotes,
                positions_total=grand_positions_count,
                positions_priced=grand_priced_count,
                priced_cost=grand_priced_cost,
                total_cost=grand_total_cost,
            ),
            "daily_pnl_coverage": _daily_pnl_coverage(
                requested=include_quotes,
                positions_total=grand_positions_count,
                positions_observed=grand_daily_pnl_observed,
            ),
            **total_daily_pnl_meta,
        },
        "exchange_rates": {
            "HKD_CNY": _finite_float(hkd_rate),
            "USD_CNY": _finite_float(usd_rate),
        },
        "exchange_rate_meta": {
            "HKD_CNY": hkd_meta,
            "USD_CNY": usd_meta,
        },
        "quotes": quotes_dict,  # 可选：返回行情数据
    }


def _fetch_quotes_for_stocks(stocks: list[Stock]) -> dict:
    """获取股票列表的实时行情"""
    if not stocks:
        return {}

    # 按市场分组
    market_stocks: dict[str, list[Stock]] = {}
    for s in stocks:
        market_stocks.setdefault(s.market, []).append(s)

    quotes = {}
    for market, stock_list in market_stocks.items():
        try:
            market_code = MarketCode(market)
        except ValueError:
            continue

        symbols = [s.symbol for s in stock_list]
        try:
            items = md_quote_rows(symbols, market_code.value)
            for item in items:
                quotes[item["symbol"]] = item
        except Exception as e:
            logger.error(f"获取 {market} 行情失败: {e}")

    return quotes


# 组合基准/归因结果缓存:重建全持仓 NAV 很贵(逐只拉 K 线),按持仓指纹缓存结果。
# 2026-08-22: 已迁到 biz_cache(L1 内存 + L2 Redis), 跨进程共享 + 重启不丢。
# 持仓变动即失效(指纹变);失败/空结果不缓存,避免把瞬时故障冻住 10 分钟。


def _holdings_signature(db: Session, user: User | None = None) -> str:
    """启用账户持仓的稳定指纹(stock_id + 合并后数量);仅查 DB,不拉行情/K 线。

    归属门禁与 list_positions / _portfolio_positions 同款: 账户可见**且**持仓可见,
    否则别人的持仓会改变缓存指纹(进而复用错误的基准/归因结果)。
    user=None 只对应历史共享桶(NULL/NULL), 不会退化成全表。
    """
    uid = getattr(user, "id", None)
    q = (
        db.query(Position.stock_id, Position.quantity)
        .join(Account, Account.id == Position.account_id)
        .filter(
            Account.enabled.is_(True),
            account_user_scope_clause(uid),
            position_user_scope_clause(uid),
        )
    )
    rows = q.all()
    agg: dict[int, float] = {}
    for sid, qty in rows:
        agg[sid] = agg.get(sid, 0.0) + _safe_amount(qty)
    return ";".join(f"{sid}:{agg[sid]:g}" for sid in sorted(agg))


def _gather_holdings(db: Session, user: User | None = None) -> list[dict]:
    """汇总所有启用账户的真实持仓为统一列表(CNY 市值/浮盈 + fx),多账户同股合并。

    合并口径(2026-10-29 复审): 数量是全部腿之和, 但市值/浮盈只乘「已定价腿」的数量
    (`priced_quantity`), 不会出现「总数量 × 现价」与已定价市值混篮子; 合并状态由各腿
    状态聚合(全有价 ok / 全缺价 missing|invalid_price / 混合 partial), 与遍历顺序无关。
    持仓归属在 SQL 层隔离(共享账户里别人的 Position 不得混入)。
    """
    q = user_scope_accounts(db, getattr(user, "id", None), enabled_only=True)
    accounts = q.all()
    positions = _portfolio_positions(db, user) if accounts else []
    positions_by_account: dict[int, list[Position]] = {}
    for pos in positions:
        positions_by_account.setdefault(pos.account_id, []).append(pos)

    stock_ids = {p.stock_id for p in positions}
    stocks = db.query(Stock).filter(Stock.id.in_(stock_ids)).all() if stock_ids else []
    stock_map = {s.id: s for s in stocks}
    quotes = _fetch_quotes_for_stocks(stocks) if stocks else {}
    hkd, usd = get_hkd_cny_rate(), get_usd_cny_rate()
    hkd_meta = _fx_meta(_hkd_rate_cache, "HKD_CNY", hkd)
    usd_meta = _fx_meta(_usd_rate_cache, "USD_CNY", usd)

    out: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    statuses: dict[tuple[str, str], set[str]] = {}
    for acc in accounts:
        ordered = sorted(
            positions_by_account.get(acc.id, []),
            key=lambda p: (int(getattr(p, "sort_order", 0) or 0), int(p.id)),
        )
        for pos in ordered:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue
            if stock.market == "HK":
                rate, fx_meta = hkd, hkd_meta
            elif stock.market == "US":
                rate, fx_meta = usd, usd_meta
            else:
                rate, fx_meta = 1.0, None
            quote = quotes.get(stock.symbol)
            # 缺/坏现价 → 市值与浮盈未知(None)。绝不用成本回填。
            price, quote_status, _raw = _resolve_quote_price(quote)
            qty = _safe_amount(pos.quantity)
            cost_cny = _safe_amount(pos.cost_price) * qty * rate
            key = (stock.market, stock.symbol)
            is_new = key not in seen
            _accumulate_holding(
                seen,
                statuses,
                key,
                symbol=stock.symbol,
                market=stock.market,
                name=stock.name,
                quantity=qty,
                fx=rate,
                cost_cny=cost_cny,
                price=price,
                quote_status=quote_status,
                strategy_code=pos.trading_style or "",
                fx_source=fx_meta["source"] if fx_meta else "not_applicable",
                fx_stale=fx_meta["stale"] if fx_meta else False,
            )
            if is_new:
                out.append(seen[key])
    return out


@router.get("/portfolio/diagnostics")
def portfolio_diagnostics(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """真实持仓组合诊断:集中度(HHI)/最大单仓/市场分布/风险提示(只读)。"""
    from src.core.portfolio_diagnostics import diagnose_positions

    return diagnose_positions(_gather_holdings(db, user))


@router.get("/portfolio/benchmark")
def portfolio_benchmark(
    days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """真实持仓组合 vs 基准:超额收益/信息比率/相对回撤 + 归一化净值曲线。"""
    from src.core.portfolio_benchmark import (
        DEFAULT_BENCHMARK,
        build_portfolio_benchmark,
    )

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db, user)
    if not sig:
        return {"empty": True, "reason": "no_holdings"}
    ckey = f"portfolio:bench:{days}:{bcode}:{sig}"
    from src.web.cache.biz_cache import biz_cache
    cached = biz_cache.get_json(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db, user)
    if not holdings:
        return {"empty": True, "reason": "no_holdings"}
    res = build_portfolio_benchmark(holdings, days=days, benchmark_code=bcode)
    if not res:
        # 失败/数据不足不缓存,下轮可重试(由 K 线负缓存兜住打爆)
        return {"empty": True, "reason": "insufficient_data"}
    biz_cache.set_json(ckey, res, ttl=600)
    return res


@router.get("/portfolio/todos")
def portfolio_todos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """首页空态待办:持仓但未设提醒 / 提醒即将到期(可行动,盘后也不空)。"""
    todos: list[dict] = []
    # 归属门禁下沉到 SQL: 共享账户里别人的 Position 不得出现在当前用户的待办里。
    held_ids = {p.stock_id for p in _portfolio_positions(db, user)}
    # 提醒规则同样按归属隔离(自己的或历史 NULL), 不能把别人的规则算成"已设提醒"。
    rule_scope = or_(PriceAlertRule.user_id == user.id, PriceAlertRule.user_id.is_(None))
    if held_ids:
        ruled = {
            r.stock_id
            for r in db.query(PriceAlertRule)
            .filter(
                PriceAlertRule.enabled == True,  # noqa: E712
                PriceAlertRule.stock_id.in_(held_ids),
                rule_scope,
            )
            .all()
        }
        for sid in held_ids - ruled:
            stock = db.query(Stock).filter(Stock.id == sid).first()
            if stock:
                todos.append(
                    {
                        "type": "no_alert",
                        "symbol": stock.symbol,
                        "market": stock.market,
                        "message": f"{stock.name} 持仓中,未设价格提醒",
                    }
                )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    soon = now + timedelta(days=3)
    expiring = (
        db.query(PriceAlertRule)
        .filter(
            PriceAlertRule.enabled == True,  # noqa: E712
            PriceAlertRule.expire_at.isnot(None),
            PriceAlertRule.expire_at >= now,
            PriceAlertRule.expire_at <= soon,
            rule_scope,
        )
        .all()
    )
    for r in expiring:
        stock = db.query(Stock).filter(Stock.id == r.stock_id).first()
        todos.append(
            {
                "type": "alert_expiring",
                "symbol": stock.symbol if stock else "",
                "market": stock.market if stock else "CN",
                "message": f"{(r.name or '提醒')} 即将到期",
            }
        )

    return {"todos": todos[:10], "count": len(todos)}


@router.get("/portfolio/attribution")
def portfolio_attribution(days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """近 days 日各持仓对组合收益的贡献(谁拖累/贡献),降序。"""
    from src.core.portfolio_benchmark import DEFAULT_BENCHMARK, build_attribution

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db, user)
    if not sig:
        return {"items": []}
    ckey = f"portfolio:attr:{days}:{bcode}:{sig}"
    from src.web.cache.biz_cache import biz_cache
    cached = biz_cache.get_json(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db, user)
    if not holdings:
        return {"items": []}
    items = build_attribution(holdings, days=days, benchmark_code=bcode)
    result = {"items": items}
    if items:  # 空结果不缓存,下轮可重试
        biz_cache.set_json(ckey, result, ttl=600)
    return result


@router.post("/portfolio/ai-review")
async def portfolio_ai_review(model_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """组合 AI 体检:诊断+基准+归因 → 叙述结论 + 调仓建议(只读,不下单)。"""
    from src.core.portfolio_benchmark import build_attribution, build_portfolio_benchmark
    from src.core.portfolio_diagnostics import diagnose_positions
    from src.web.api.chat import _get_ai_client

    holdings = _gather_holdings(db, user)
    if not holdings:
        return {"empty": True, "reason": "no_holdings"}

    diag = diagnose_positions(holdings)
    bench = build_portfolio_benchmark(holdings, days=60) or {}
    attr = build_attribution(holdings, days=60)
    top = attr[:3]
    worst = list(reversed(attr[-3:])) if len(attr) > 3 else []

    def _amt(v) -> str:
        # 缺行情时市值/浮盈是 None: 明确写"未知", 不要格式化出 0 让 AI 当成真实数字。
        return "未知(缺行情)" if v is None else f"{v:.0f}"

    lines = [
        f"持仓 {diag['position_count']} 只,总市值 {_amt(diag['total_market_value'])},"
        f"浮盈 {_amt(diag['total_unrealized_pnl'])}",
        f"集中度 HHI {diag['hhi']},最大单仓 {diag['max_weight'] * 100:.0f}%",
    ]
    if diag.get("missing_quote_count"):
        lines.append(
            f"行情覆盖: {diag['priced_position_count']}/{diag['position_count']} 只有效现价,"
            f"{diag['missing_quote_count']} 只缺行情(市值与浮盈只统计已定价部分)"
        )
    if bench.get("excess_return") is not None:
        lines.append(
            f"近60日 vs {bench.get('benchmark_label', '基准')}:超额 {bench['excess_return']}%"
            f"(组合 {bench.get('portfolio_return')}% / 基准 {bench.get('benchmark_return')}%),"
            f"相对回撤 {bench.get('relative_drawdown')}%"
        )
    if diag.get("by_market"):
        lines.append("市场分布:" + ", ".join(f"{k} {v:.0f}" for k, v in diag["by_market"].items()))
    if diag.get("alerts"):
        lines.append("风险提示:" + "; ".join(diag["alerts"]))
    if top:
        lines.append("贡献最大:" + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in top))
    if worst:
        lines.append("拖累最大:" + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in worst))

    system_prompt = (
        "你是稳健的组合顾问。基于给定的组合诊断/基准对比/个股归因,给一段简短体检 + 可执行调仓建议,"
        "只读分析、不下单、不承诺收益。严格格式:\n体检: 一句话总评\n建议:\n- (2~3 条具体可执行)\n风险: 一句话最大风险"
    )
    user_content = "组合概况:\n" + "\n".join(lines)
    try:
        content = await _get_ai_client(db, model_id).chat(system_prompt, user_content, temperature=0.3)
    except Exception as e:
        raise HTTPException(502, f"AI 体检失败: {e}")

    return {"content": content, "top": top, "worst": worst, "diagnostics": diag, "benchmark": bench}
