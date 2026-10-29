import asyncio
import logging
import threading
import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from pydantic import BaseModel

from src.web.database import get_db
from src.web.api._scope import scoped
from src.web.api.auth import get_current_user
from src.web.models import (
    Stock,
    StockAgent,
    AgentConfig,
    Position,
    PriceAlertRule,
    PriceAlertHit,
    User,
)
from src.web.stock_list import search_stocks, refresh_stock_list
from src.core.marketdata_client import md_quote_rows
from src.models.market import MarketCode, MARKETS
from src.core.agent_catalog import AGENT_KIND_WORKFLOW, infer_agent_kind

logger = logging.getLogger(__name__)
router = APIRouter()


class StockCreate(BaseModel):
    symbol: str
    name: str
    market: str = "CN"


class StockUpdate(BaseModel):
    name: str | None = None


class StockAgentInfo(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockResponse(BaseModel):
    id: int
    symbol: str
    name: str
    market: str
    sort_order: int
    agents: list[StockAgentInfo] = []

    class Config:
        from_attributes = True


class StockAgentItem(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockAgentUpdate(BaseModel):
    agents: list[StockAgentItem]


class StockReorderItem(BaseModel):
    id: int
    sort_order: int


class StockReorderRequest(BaseModel):
    items: list[StockReorderItem]


def _stock_to_response(stock: Stock) -> dict:
    return {
        "id": stock.id,
        "symbol": stock.symbol,
        "name": stock.name,
        "market": stock.market,
        "sort_order": stock.sort_order or 0,
        "agents": [
            {
                "agent_name": sa.agent_name,
                "schedule": sa.schedule or "",
                "ai_model_id": sa.ai_model_id,
                "notify_channel_ids": sa.notify_channel_ids or [],
            }
            for sa in stock.agents
            if infer_agent_kind(sa.agent_name) == AGENT_KIND_WORKFLOW
        ],
    }


@router.get("/markets/status")
def get_market_status():
    """获取各市场的交易状态"""
    from datetime import datetime

    from src.core.trading_calendar import is_trading_day

    result = []
    for market_code, market_def in MARKETS.items():
        try:
            now = datetime.now(market_def.get_tz())
            is_trading = market_def.is_trading_time()

            # 获取交易时段描述
            sessions_desc = []
            for session in market_def.sessions:
                sessions_desc.append(f"{session.start.strftime('%H:%M')}-{session.end.strftime('%H:%M')}")

            # 判断状态
            weekday = now.weekday()
            current_time = now.time()

            if weekday >= 5:
                status = "closed"
                status_text = "休市（周末）"
            elif is_trading:
                status = "trading"
                status_text = "交易中"
            elif market_code == MarketCode.CN and not is_trading_day(now.date()):
                # W2.6(B6): A股法定节假日(工作日)白天, 旧逻辑会误标"盘前/已收盘"
                status = "closed"
                status_text = "休市（节假日）"
            else:
                # 判断是盘前还是盘后
                first_session = market_def.sessions[0]
                last_session = market_def.sessions[-1]
                if current_time < first_session.start:
                    status = "pre_market"
                    status_text = "盘前"
                elif current_time > last_session.end:
                    status = "after_hours"
                    status_text = "已收盘"
                else:
                    status = "break"
                    status_text = "午间休市"

            result.append({
                "code": market_code.value,
                "name": market_def.name,
                "status": status,
                "status_text": status_text,
                "is_trading": is_trading,
                "sessions": sessions_desc,
                "local_time": now.strftime("%H:%M"),
                "timezone": market_def.timezone,
            })
        except Exception as e:
            # 单个市场获取失败不影响其他市场
            logger.error(f"获取 {market_code.value} 市场状态失败: {e}")
            result.append({
                "code": market_code.value,
                "name": market_def.name,
                "status": "unknown",
                "status_text": "未知",
                "is_trading": False,
                "sessions": [],
                "local_time": "--:--",
                "timezone": market_def.timezone,
                "error": str(e),
            })

    return result


@router.get("/search")
def search(q: str = Query("", min_length=1), market: str = Query("")):
    """模糊搜索股票(代码/名称)"""
    return search_stocks(q, market)


#: 批量取名字的单词上限 —— 账本一页 500 行也就 500 个符号, 200 足够页面用, 且防滥用。
STOCK_NAMES_MAX_SYMBOLS = 200


@router.get("/names")
def get_stock_names(
    symbols: str = Query("", description="逗号分隔代码, 最多 200 个(可带 sz/002050.SZ 等前缀后缀)"),
    market: str = Query("CN", description="市场: CN(默认)/HK/US"),
    _: User = Depends(get_current_user),
):
    """代码 → 中文名批量解析(2026-10-21)。

    页面只拿到代码时用它一次补齐名字, 免得每个页面前端各自维护一份代码表。
    **缺失语义**: 取不到的名字不出现在 `names` 里、改为列在 `missing`(前端显示 `--`);
    绝不拿代码冒充名字, 也不返回空串 —— 空串会被当成"名字是空字符串", 而"名字未知"是缺键。
    """
    from src.core.stock_names import names_for

    raw = [s.strip() for s in (symbols or "").split(",") if s.strip()]
    uniq = list(dict.fromkeys(raw))  # 去重保序, 不改变调用方语义
    if len(uniq) > STOCK_NAMES_MAX_SYMBOLS:
        # 显式 400: 静默截断会让 `missing` 骗人(被丢掉的符号既不在 names 也不在 missing)
        raise HTTPException(
            status_code=400,
            detail=f"symbols 最多 {STOCK_NAMES_MAX_SYMBOLS} 个, 收到 {len(uniq)} 个",
        )
    mk = (market or "CN").strip().upper() or "CN"
    names = names_for(uniq, market=mk)
    return {"names": names, "missing": [s for s in uniq if s not in names]}


@router.get("/{symbol}/l2")
def get_stock_l2(symbol: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """单股 L2 字段: 通达信 snapshot + more_info。

    权限(2026-09-15): view_l2 — member 3次/天试用, pro/owner 全开。
    调用日志(2026-09-15 C.1): 每次调用落 high_value_api_logs。
    """
    from src.core.permissions import PERM_VIEW_L2, enforce_perm

    enforce_perm(user, PERM_VIEW_L2, db)
    from src.core.hv_api_log import log_high_value_call

    log_high_value_call(db, user, "l2", symbol)
    from src.core.stock_l2 import fetch_stock_l2_ex

    # 铁律『数据缺失显式降级、永不 500』(2026-10-10 P1): 上游(TQ 网关)不可用 /
    # 非交易日无数据 / RPC 抛异常 → 200 + available:false + note(真实原因),
    # 不裸 500。契约与同族 /api/stocks/{symbol}/dark-flow-tq 一致。
    data, reason = fetch_stock_l2_ex(symbol)
    if not data:
        return {"symbol": symbol, "available": False, "snapshot": {}, "more": {},
                "as_of": None, "note": reason or "无L2数据(通达信源不可用)"}
    # 部分源缺失(如更多信息源异常)时把真实原因带到 note; 两源皆正常 → reason None
    return {**data, "available": True, "note": reason}


@router.get("/{symbol}/blocks")
def get_stock_blocks(symbol: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """个股→所属板块反查(P1, get_relation): [{code,name,type}]; 源不可用 → 空列表+note(不编)。

    权限(P2, audit-20260915): view_quotes — 与行情接口一致(板块归属属行情面数据)。
    """
    from src.core.permissions import PERM_VIEW_QUOTE, enforce_perm

    enforce_perm(user, PERM_VIEW_QUOTE, db)
    from src.core.tdx_boards import stock_blocks

    blocks = stock_blocks(symbol)
    if blocks is None:
        return {"symbol": symbol, "blocks": [], "note": "板块反查不可用(通达信源)"}
    return {"symbol": symbol, "blocks": blocks, "note": None}


@router.get("/{symbol}/fundamental")
def get_stock_fundamental(symbol: str, user=Depends(get_current_user)):
    """个股基本面/股本/次新(P2): 股本(get_gb_info) + 上市信息(get_stock_info) + 次新判定。

    PE/PB/股息 走 /l2 的 more(不新增 RPC); 研报 get_report_data MCP 不支持 → 不做。
    源不可用 → 对应字段 None + note(不编)。
    """
    from src.core.tdx_fundamental import fetch_gb, fetch_listing, is_sub_new

    gb = fetch_gb(symbol)
    listing = fetch_listing(symbol)
    sub_new = is_sub_new(listing.get("listing_date") or "") if listing else None
    note = None
    if gb is None and listing is None:
        note = "基本面/股本不可用(通达信源)"
    return {"symbol": symbol, "gb": gb, "listing": listing,
            "sub_new": sub_new, "note": note}


@router.post("/refresh-list")
def refresh_list():
    """刷新股票列表缓存"""
    stocks = refresh_stock_list()
    return {"count": len(stocks)}


@router.get("", response_model=list[StockResponse])
def list_stocks(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stocks = db.query(Stock).filter(
        or_(Stock.user_id == user.id, Stock.user_id.is_(None))  # 自己的 + 全局
    ).order_by(Stock.sort_order.asc(), Stock.id.asc()).all()
    return [_stock_to_response(s) for s in stocks]


@router.get("/quotes")
def get_quotes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取所有自选股的实时行情。

    KI-043(2026-09-18): 逐市场失败不再静默吞掉 —— 响应带 `degraded_markets`
    `[{market, error}]`, 成功市场的报价仍在 `quotes` 映射里。
    响应形状: `{quotes: {symbol: {...}}, degraded_markets: [...]}`。
    """
    stocks = db.query(Stock).filter(
        or_(Stock.user_id == user.id, Stock.user_id.is_(None))
    ).all()
    if not stocks:
        return {"quotes": {}, "degraded_markets": []}

    # 按市场分组
    market_stocks: dict[str, list[Stock]] = {}
    for s in stocks:
        market_stocks.setdefault(s.market, []).append(s)

    quotes = {}
    degraded_markets: list[dict] = []
    for market, stock_list in market_stocks.items():
        try:
            MarketCode(market)  # 校验市场合法
        except ValueError:
            degraded_markets.append({"market": market, "error": f"未知市场 {market}"})
            continue

        symbols = [s.symbol for s in stock_list]   # 原始代码,md 内部按市场格式化
        try:
            items = md_quote_rows(symbols, market)
            for item in items:
                quotes[item["symbol"]] = {
                    "current_price": item["current_price"],
                    "change_pct": item["change_pct"],
                    "change_amount": item["change_amount"],
                    "prev_close": item["prev_close"],
                }
        except Exception as e:
            logger.error(f"获取 {market} 行情失败: {e}")
            degraded_markets.append({"market": market, "error": str(e)[:200]})

    return {"quotes": quotes, "degraded_markets": degraded_markets}


@router.post("", response_model=StockResponse)
def create_stock(stock: StockCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # demo 账号: 自选数量上限(演示体验; 防公开账号堆积垃圾数据)
    # B3(2026-09-29): 上限从「免费档」面板的 guest_strategy.watchlist_limit 读, 未配置 → 1
    if str(getattr(user, "username", "")) == "demo" or str(getattr(user, "role", "")) == "guest":
        from src.core import free_tier

        watch_limit = int(free_tier.guest_strategy(db).get("watchlist_limit", 1))
        if watch_limit > 0:
            own_count = db.query(Stock).filter(Stock.user_id == user.id).count()
            if own_count >= watch_limit:
                raise HTTPException(403, f"演示账号仅可添加 {watch_limit} 只自选股。请先删除当前自选,再添加其他股票体验。")
    # 权限体系 2026-09-15: 普通账号自选≤10, pro/owner 无限
    from src.core.permissions import check_watchlist_quota

    check_watchlist_quota(db, user)

    existing = db.query(Stock).filter(
        Stock.symbol == stock.symbol, Stock.market == stock.market,
        or_(Stock.user_id == user.id, Stock.user_id.is_(None)),
    ).first()
    if existing:
        raise HTTPException(400, f"股票 {stock.symbol} 已存在")

    max_order = db.query(func.max(Stock.sort_order)).scalar() or 0
    db_stock = Stock(**stock.model_dump(), sort_order=int(max_order) + 1, user_id=user.id)
    db.add(db_stock)
    db.commit()
    db.refresh(db_stock)

    # 2026-08-17 加股快速 backfill(60s 内): 用户加股后立即拉 K线, 不必等 18:00 cron
    # 60s 延迟是为了合并 1 分钟内多次 add(用户连续点不会重复拉)
    try:
        # 复用 server.py lifespan 已启动的 scheduler(单例), 不要新建
        # 因为新建会创建第二个 AsyncIOScheduler, schedule_one_off 的 job
        # 跟 18:00 cron 不在同一线程, 启动后没人 start() 会死锁
        import src.core.kline_backfill_scheduler as _kbs_mod

        if _kbs_mod._global_scheduler is None:
            # 单例不存在(测试环境 / server 未启动) — 走 18:00 cron 兜底
            logger.warning(
                "K线入库调度器未启动, 加股 backfill 跳过(18:00 cron 兜底)"
            )
        else:
            market_str = (
                db_stock.market.value
                if hasattr(db_stock.market, "value")
                else str(db_stock.market)
            )
            symbol_str = (
                db_stock.symbol.value
                if hasattr(db_stock.symbol, "value")
                else str(db_stock.symbol)
            )
            _kbs_mod._global_scheduler.schedule_one_off(
                symbol=symbol_str,
                market=market_str,
            )
            logger.info(
                f"已为新加自选 {db_stock.symbol}.{db_stock.market} 调度 60s 后 backfill"
            )
    except Exception as e:
        logger.warning(f"调度新加股 backfill 失败: {e}")

    return _stock_to_response(db_stock)


@router.put("/reorder")
def reorder_stocks(body: StockReorderRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not body.items:
        return {"updated": 0}
    ids = [int(x.id) for x in body.items]
    rows = db.query(Stock).filter(Stock.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in body.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        # 归属校验(P1-2 2026-09-05 28号审计): 只能排自己的; 共享默认项仅 owner
        if row.user_id is not None and row.user_id != user.id:
            continue
        if row.user_id is None and user.role != "owner":
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


@router.put("/{stock_id}", response_model=StockResponse)
def update_stock(stock_id: int, stock: StockUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    # 归属校验(P1-2 2026-09-05 28号审计, 对齐 delete_stock)
    if db_stock.user_id is not None and db_stock.user_id != user.id:
        raise HTTPException(403, "无权修改该自选(非本人创建)")
    if db_stock.user_id is None and user.role != "owner":
        raise HTTPException(403, "无权修改共享默认自选")

    for key, value in stock.model_dump(exclude_unset=True).items():
        setattr(db_stock, key, value)

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.delete("/{stock_id}")
def delete_stock(stock_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    # 归属校验(2026-08-15 安全加固): 只能删自己的自选; 共享默认项(user_id NULL)仅 owner 可删
    if db_stock.user_id is not None and db_stock.user_id != user.id:
        raise HTTPException(403, "无权删除该自选(非本人创建)")
    if db_stock.user_id is None and user.role != "owner":
        raise HTTPException(403, "无权删除共享默认自选")

    # 删除股票前，要求先清理持仓，避免误删资产数据。
    # 2026-10-29 三审: 旧实现一条 `filter(Position.stock_id == stock_id)` 查**全库**
    # 持仓 —— ① 别人在我这一行上的历史错位持仓也算数; ② 有人拿我的 stock_id 建过仓
    # (旧 create_position 不校验标的归属), 我删自选会把**他的**持仓级联删掉。
    # 现在分两档, 都不泄露归属:
    #   - 自己可见的持仓还在 → 400, 提示先清理(信息对他本人真实且必要);
    #   - 只有他人持仓引用 → 409 通用文案(不报数量/账户名/归属人), 拒绝删除,
    #     因为拒绝是唯一不破坏他人数据的选择; 残留的"存在引用"信号无法完全消除,
    #     这是任何安全删除设计的固有代价(详见 docs 三审记录)。
    from src.core.portfolio_scope import (
        account_user_scope_clause,
        position_user_scope_clause,
    )
    from src.web.models import Account

    uid = getattr(user, "id", None)
    has_position = (
        db.query(Position.id)
        .join(Account, Account.id == Position.account_id)
        .filter(
            Position.stock_id == stock_id,
            account_user_scope_clause(uid),
            position_user_scope_clause(uid),
        )
        .first()
    )
    if has_position:
        raise HTTPException(400, "该股票存在持仓，请先删除持仓后再删除股票")
    foreign_ref = db.query(Position.id).filter(Position.stock_id == stock_id).first()
    if foreign_ref:
        logger.warning(
            "拒绝删除自选: stock_id=%s 仍被不可见持仓引用(不泄露归属) user=%s",
            stock_id,
            getattr(user, "id", None),
        )
        raise HTTPException(
            409,
            "该自选仍被其它数据引用，暂无法删除，请联系管理员",
        )

    # ── 提醒规则 / 命中历史: 只允许本人销毁本人的, 其余一律整单拒绝 ──
    #
    # 2026-10-29 五审的「安全拆离」有跨账号副作用, 六审回退为**保守拒绝**:
    #   1) 给他人复制 Stock(symbol) + StockAgent 绑定 = 替别人建自选/订阅, 调度器
    #      随后会真的跑他的 Agent(LLM 调用 + 按绑定渠道外发), 未经其同意;
    #   2) 只改 rule.stock_id 不改 PriceAlertHit.stock_id → 删原行时
    #      `price_alert_hits.stock_id ON DELETE CASCADE` 仍会把他人的命中历史删掉;
    #   3) NULL 归属规则被 `~own_filter`(SQL 三值逻辑)滤掉 → 既不算他人的,
    #      也不在本人删除集合里, 最后被数据库级联删掉, 历史同样丢失。
    #
    # 现在的口径(不猜、不代劳、不丢历史):
    #   - 本人规则(user_id == 我) → 允许随自选一起删(用户自己的显式操作);
    #   - 任何非本人规则(含 user_id IS NULL 的历史规则) → 通用 409, 整单不删;
    #   - 任何本人不可销毁规则上的命中, 或 stock_id 指向本行的孤儿命中 → 同样 409;
    #   - 共享票/规则/绑定/命中全部原样保留, 文案不报归属人、不报数量。
    # 用户可操作的替代路径: `POST /api/price-alerts/{rule_id}/adopt` 由规则本人在
    # 「价格提醒」页显式迁到自己的私有自选(带同意、不复制 Agent、不触发 LLM/外发),
    # 迁空后本端点即可正常删除共享行。
    own_rule_ids = [
        row[0]
        for row in db.query(PriceAlertRule.id)
        .filter(PriceAlertRule.stock_id == stock_id, PriceAlertRule.user_id == uid)
        .all()
    ]
    has_unowned_rule = (
        db.query(PriceAlertRule.id)
        .filter(
            PriceAlertRule.stock_id == stock_id,
            or_(
                PriceAlertRule.user_id.is_(None),
                PriceAlertRule.user_id != uid,
            ),
        )
        .first()
    )
    # 命中记录没有 user_id, 只能经 rule_id 判定: 规则缺失/非本人 → 不能证明可删。
    foreign_hit = (
        db.query(PriceAlertHit.id)
        .outerjoin(PriceAlertRule, PriceAlertRule.id == PriceAlertHit.rule_id)
        .filter(
            PriceAlertHit.stock_id == stock_id,
            or_(
                PriceAlertRule.id.is_(None),
                PriceAlertRule.user_id.is_(None),
                PriceAlertRule.user_id != uid,
            ),
        )
        .first()
    )
    if has_unowned_rule or foreign_hit:
        logger.warning(
            "拒绝删除自选: stock_id=%s 仍被非本人规则/命中引用(不泄露归属) user=%s",
            stock_id,
            uid,
        )
        raise HTTPException(
            409,
            "该自选仍被其它数据引用，暂无法删除（删除会丢失历史记录）。"
            "如提醒规则属于你本人，请在「价格提醒」页将其迁移到自己的私有自选后再试；"
            "仍未解决请联系管理员。",
        )

    try:
        # 先删本人规则命中, 再删规则, 最后删 Agent 绑定/自选行 —— 显式清理不依赖
        # 数据库 FK 级联(SQLite 默认可能未启用), 任何一步失败整单回滚。
        if own_rule_ids:
            db.query(PriceAlertHit).filter(
                PriceAlertHit.rule_id.in_(own_rule_ids)
            ).delete(synchronize_session=False)
            db.query(PriceAlertRule).filter(
                PriceAlertRule.id.in_(own_rule_ids)
            ).delete(synchronize_session=False)
        db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete(
            synchronize_session=False
        )
        # 用 bulk DELETE 的 rowcount 判定"这次是否真的删到了":
        # 并发双删时另一个事务已删, 这里 0 行 → 幂等 404, 不报 200 也不 500。
        deleted = (
            db.query(Stock)
            .filter(Stock.id == stock_id)
            .delete(synchronize_session=False)
        )
        if not deleted:
            db.rollback()
            raise HTTPException(404, "股票不存在")
        db.commit()
    except StaleDataError:
        # 并发双删: 另一请求已删掉本行 → 幂等 404, 不冒 500。
        db.rollback()
        if db.query(Stock.id).filter(Stock.id == stock_id).first() is None:
            raise HTTPException(404, "股票不存在")
        raise HTTPException(409, "该自选正在被其他操作修改，请刷新后重试")
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("删除自选失败(整单回滚, 未删除): stock=%s err=%s", stock_id, e)
        raise HTTPException(409, "该自选暂无法删除，请稍后重试")

    return {"ok": True}


@router.put("/{stock_id}/agents", response_model=StockResponse)
def update_stock_agents(stock_id: int, body: StockAgentUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新股票关联的 Agent 列表（含调度配置和 AI/通知覆盖）"""
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    # 归属校验(P1-2 2026-09-05 28号审计, 对齐 delete_stock)
    if db_stock.user_id is not None and db_stock.user_id != user.id:
        raise HTTPException(403, "无权修改该自选(非本人创建)")
    if db_stock.user_id is None and user.role != "owner":
        raise HTTPException(403, "无权修改共享默认自选")

    for item in body.agents:
        agent = db.query(AgentConfig).filter(AgentConfig.name == item.agent_name).first()
        if not agent:
            raise HTTPException(400, f"Agent {item.agent_name} 不存在")
        agent_kind = (agent.kind or "").strip() or infer_agent_kind(agent.name)
        if agent_kind != AGENT_KIND_WORKFLOW:
            raise HTTPException(400, f"Agent {item.agent_name} 为内部能力，不支持绑定到股票")

    # 清除旧关联，重建
    db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete()
    for item in body.agents:
        db.add(StockAgent(
            stock_id=stock_id,
            agent_name=item.agent_name,
            schedule=item.schedule,
            ai_model_id=item.ai_model_id,
            notify_channel_ids=item.notify_channel_ids,
        ))

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.post("/{stock_id}/agents/{agent_name}/trigger")
async def trigger_stock_agent(
    stock_id: int,
    agent_name: str,
    bypass_throttle: bool = False,
    bypass_market_hours: bool = False,
    allow_unbound: bool = False,
    wait: bool = False,
    force_refresh: bool = False,
    symbol: str = Query(""),
    market: str = Query("CN"),
    name: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发单只股票 Agent。

    - 正常模式：传有效 stock_id
    - 无绑定模式：stock_id<=0 且传 symbol/market（需 allow_unbound=true）
    - 无绑定模式默认禁用通知（仅生成建议）
    - 默认异步执行（立即返回），传 wait=true 可同步等待结果
    """
    sa = None
    trigger_stock = None
    suppress_notify = stock_id <= 0

    if stock_id > 0:
        # C3(2026-09-09): 股票按归属过滤 —— 不能借 stock_id 触发他人自选股的 Agent
        db_stock = scoped(db.query(Stock), user).filter(Stock.id == stock_id).first()
        if not db_stock:
            raise HTTPException(404, "股票不存在")

        sa = db.query(StockAgent).filter(
            StockAgent.stock_id == stock_id, StockAgent.agent_name == agent_name
        ).first()
        if not sa and not allow_unbound:
            raise HTTPException(400, f"股票未关联 Agent {agent_name}")
        if not sa and allow_unbound:
            # 允许无绑定触发时，至少确保 Agent 存在。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
        trigger_stock = db_stock
    else:
        symbol = (symbol or "").strip()
        if not symbol:
            raise HTTPException(400, "当 stock_id<=0 时，symbol 不能为空")
        if not allow_unbound:
            raise HTTPException(400, "当 stock_id<=0 时，需设置 allow_unbound=true")

        market = (market or "CN").strip().upper() or "CN"
        name = (name or "").strip() or symbol
        # C3(2026-09-09): 同上按归属过滤; 非本人自选 → 走不落库的一次性分析,
        # 不再复用他人 Stock 行/其 Agent 绑定。
        db_stock = scoped(db.query(Stock), user).filter(
            Stock.symbol == symbol, Stock.market == market
        ).first()
        if db_stock:
            sa = db.query(StockAgent).filter(
                StockAgent.stock_id == db_stock.id, StockAgent.agent_name == agent_name
            ).first()
            trigger_stock = db_stock
        else:
            # 不落库：用于详情弹窗未持仓且未关注股票的一次性分析。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
            trigger_stock = SimpleNamespace(
                id=0,
                symbol=symbol,
                name=name,
                market=market,
            )

    logger.info(
        f"手动触发 Agent {agent_name} - {trigger_stock.name}({trigger_stock.symbol})"
    )

    from server import trigger_agent_for_stock
    import time as _time

    # 幂等性兜底:TradingAgents 单次 3-5 分钟,前端误操作/双击可能并发触发同一标的。
    # 后端先查"该 symbol 是否有真正在跑的 TA 任务",有则返回现有 trace_id(不启新任务)。
    # force_refresh=true 时跳过去重,允许用户主动强制重跑(老任务自然终止,新 trace_id)。
    if agent_name == "tradingagents" and not force_refresh:
        from src.web.api.agents import find_active_tradingagents_trace
        existing_trace = find_active_tradingagents_trace(db, trigger_stock.symbol)
        if existing_trace:
            logger.info(
                f"[trigger 幂等] {trigger_stock.symbol} 已有在跑任务 trace={existing_trace},"
                f"复用而非启新任务"
            )
            return {
                "queued": False,
                "trace_id": existing_trace,
                "message": "已有正在执行的深度分析,返回现有任务进度",
                "deduplicated": True,
            }

    # 预生成 trace_id,返回给前端用于轮询进度
    trace_id = f"man-{agent_name}-{trigger_stock.symbol}-{int(_time.time() * 1000)}"

    # 立刻写一条"任务已触发"进度日志,保证前端 polling 第一拍就能看到 running。
    # 否则 trigger_agent_for_stock 内部要先 await agent.collect()(美股拉 yfinance 数据
    # 可能 30s+),期间没有任何 ta_progress 日志 → 前端 progress 接口返回 not_found
    # → 60s grace 过后前端 reset 到 idle,看起来像"进度卡死自动退回"。
    if agent_name == "tradingagents":
        try:
            from src.core.log_context import log_context
            with log_context(
                trace_id=trace_id,
                agent_name="tradingagents",
                event="ta_progress",
                tags={"stage": "task_triggered", "action": "triggered"},
            ):
                logger.info(
                    f"[TA] 任务已触发 - {trigger_stock.symbol} (trace={trace_id})"
                )
        except Exception as e:
            logger.warning(f"[TA] 写触发日志失败,不影响主流程: {e}")

    if not wait:
        # 异步模式：后台执行，立即返回
        sa_id = sa.id if sa else None
        _sym = trigger_stock.symbol
        _nm = getattr(trigger_stock, "name", "") or _sym
        _mkt = getattr(trigger_stock, "market", "CN")
        # 2026-10-29 三审(R4): 线程里不能碰请求级 ORM 对象(会话已关/跨线程懒加载),
        # 且通知必须归**触发它的那个人**。旧 `_notify` 不传 user_id → notify_task_done
        # 兜底推给 owner, member 手动触发的分析结果进了 owner 的站内信/渠道。这里在起
        # 线程**之前**把标量 user_id 抓出来, 线程内只用这个字符串。
        _uid = str(user.id) if getattr(user, "id", None) else None
        # 四审: 完成通知的渠道集合同样按 agent/stock_agent 配置 + 归属解析一次, 最终
        # 外发在通知中心按"归属 ∩ 选择集"再校验 —— 勾了 1 个渠道就只发 1 个。
        _notify_channel_ids: list[int] | None
        try:
            from src.bootstrap.runtime import resolve_notify_channels_ex

            _res = resolve_notify_channels_ex(
                agent_name, sa.id if sa is not None else None, user_id=_uid
            )
            _notify_channel_ids = [int(c.id) for c in _res.channels]
        except Exception as _e:  # noqa: BLE001
            logger.warning("解析通知渠道选择集失败(本轮不外发): %s", _e)
            _notify_channel_ids = []

        def _notify(ok: bool, detail: str, started: float, status: str = "") -> None:
            try:
                from src.core.notify_center import notify_task_done

                notify_task_done(
                    f"{_nm}({_sym}) {agent_name}",
                    ok=ok,
                    detail=detail or ("分析已完成，可在个股详情查看。" if ok else ""),
                    category="agent_run",
                    source=agent_name,
                    trace_id=trace_id,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    link=f"/portfolio?symbol={_sym}&market={_mkt}",
                    status=status,
                    user_id=_uid,
                    channel_ids=_notify_channel_ids,
                )
            except Exception:
                logger.exception("写入站内通知失败: %s %s", agent_name, _sym)

        def _runner():
            _started = time.monotonic()
            try:
                outcome = asyncio.run(trigger_agent_for_stock(
                    agent_name,
                    trigger_stock,
                    stock_agent_id=sa_id,
                    bypass_throttle=bypass_throttle,
                    bypass_market_hours=bypass_market_hours,
                    suppress_notify=suppress_notify,
                    trace_id=trace_id,
                    force_refresh=force_refresh,
                    user_id=_uid,
                ))
                if outcome.get("skipped"):
                    detail = str(outcome.get("content") or outcome.get("message") or "本次任务已跳过")
                    logger.info(f"Agent {agent_name} 后台执行已跳过 - {_sym}: {detail}")
                    _notify(True, detail, _started, status="skipped")
                else:
                    logger.info(f"Agent {agent_name} 后台执行完成 - {_sym}")
                    _notify(True, "", _started)
            except Exception as exc:
                logger.exception(f"Agent {agent_name} 后台执行失败 - {_sym}")
                _notify(False, str(exc)[:500], _started)

        t = threading.Thread(
            target=_runner,
            name=f"stock-trigger-{agent_name}-{trigger_stock.symbol}",
            daemon=True,
        )
        t.start()
        return {"queued": True, "trace_id": trace_id, "message": "已提交后台执行"}

    # 同步模式：等待结果返回
    try:
        result = await trigger_agent_for_stock(
            agent_name,
            trigger_stock,
            stock_agent_id=sa.id if sa else None,
            bypass_throttle=bypass_throttle,
            bypass_market_hours=bypass_market_hours,
            suppress_notify=suppress_notify,
            trace_id=trace_id,
            force_refresh=force_refresh,
            user_id=user.id,
        )
        logger.info(f"Agent {agent_name} 执行完成 - {trigger_stock.symbol}")
        return {
            "result": result,
            "trace_id": trace_id,
            "code": int(result.get("code", 0)),
            "success": bool(result.get("success", True)),
            "message": result.get("message", "ok"),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Agent {agent_name} 执行失败 - {trigger_stock.symbol}: {e}")
        raise HTTPException(500, f"Agent 执行失败: {e}")
