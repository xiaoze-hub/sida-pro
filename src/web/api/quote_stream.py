"""行情实时推送(2026-08-12): 自选股行情 WebSocket 推送, 免手动刷新/轮询。

实现: 后台线程每 5s 批量拉取(腾讯批量接口)自选股行情 → 广播给所有 WebSocket 订阅者。
前端 WebSocket 连 /api/quotes/ws, 收到 JSON 后更新持仓/自选列表现价。

⚠️ 2026-08-12 踩坑记录: 曾用 HTTP SSE(StreamingResponse + while True 生成器),
   在 uvicorn 0.52 + FastAPI 0.141 下无限循环生成器挂起(连接无首字节, 有限循环正常)。
   原因疑似 Starlette 流式响应与永续 await 的交互 bug。改用 WebSocket 绕开, 稳定。
"""
import asyncio
import json
import logging
import threading
import time
from collections import defaultdict

from fastapi import WebSocketDisconnect

logger = logging.getLogger(__name__)

# 订阅者集合: {id: asyncio.Queue}(同一 event loop 内使用)
_subscribers: dict[int, asyncio.Queue] = {}
_subscribers_lock = threading.Lock()
_next_id = 0

# 聚合器状态
_agg_running = False
_agg_lock = threading.Lock()  # 2026-08-27 fix: check-then-set 原子化, 防并发首连起双聚合器线程
_agg_interval_s = 5.0
_last_snapshot: dict = {}


def subscribe(user_id: str | None = None) -> tuple[int, asyncio.Queue]:
    """注册订阅者, 返回 (id, queue)。必须在 event loop 线程内调用。
    2026-09-08 T8: user_id 绑定订阅, 广播时按用户过滤(不向 A 推仅属于 B 的 symbol)。"""
    global _next_id
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    with _subscribers_lock:
        _next_id += 1
        sid = _next_id
        _subscribers[sid] = (q, user_id)
    _ensure_aggregator()
    # 新订阅者立即收到当前快照(P2: 同样走 envelope, topic=quote.snapshot)
    # 2026-09-08 T8: 快照同样按用户过滤(新用户先刷一次关注集合缓存再过滤)
    if _last_snapshot:
        try:
            from src.web.realtime.envelope import pack

            if user_id is None:
                snap = _last_snapshot
            else:
                _collect_watchlist_symbols()  # 刷新 per-user 缓存(WS 连接低频, 代价可接受)
                allowed = _allowed_symbols(user_id) or set()
                snap = {k: v for k, v in _last_snapshot.items() if _sym_key(k) in allowed}
            if snap:
                q.put_nowait(pack("quote.snapshot", user_id, {"type": "snapshot", "data": snap}))
        except Exception:
            pass
    return sid, q


def unsubscribe(sid: int):
    with _subscribers_lock:
        _subscribers.pop(sid, None)


def _broadcast(payload: dict):
    """按订阅者归属过滤后推送(跨线程: 聚合器线程 → 订阅者队列)。

    2026-09-07 P2: 下行统一 envelope(quote.tick), 断线重放靠 ?last_seq=。
    2026-09-08 T8: 行情数据本身公开, 但"谁在关注什么"不是 —— 原实现收集
    所有用户持仓并向全体广播, 任一账号可推断他人的持仓/自选集合。现按
    订阅者的 symbol 集过滤: 只推本人(及历史遗留共享账户)关注的标的。
    """
    from src.web.realtime.envelope import pack

    data: dict = payload.get("data") or {}
    with _subscribers_lock:
        subs = list(_subscribers.items())
    for sid, (q, user_id) in subs:
        allowed = _allowed_symbols(user_id)
        if allowed is not None:
            filtered = {k: v for k, v in data.items() if _sym_key(k) in allowed}
            if not filtered:
                continue  # 该用户当前无关注标的, 不推
            frame = pack("quote.tick", user_id, {**payload, "data": filtered})
        else:
            frame = pack("quote.tick", None, payload)  # 无绑定(兼容路径): 全量
        try:
            if q.full():
                try:
                    q.get_nowait()
                except Exception:
                    pass
            q.put_nowait(frame)  # asyncio.Queue put_nowait 底层原子, 跨线程可用
        except Exception:
            pass


def _sym_key(symbol: str) -> str:
    return str(symbol)


def _allowed_symbols(user_id: str | None) -> set[str] | None:
    """返回该用户可接收的 symbol 集合(含历史遗留共享账户); None=不过滤。
    简化实现: 每次广播时现查代价高, 改由聚合器缓存的 per-user 集合。"""
    with _symbols_lock:
        cached = _user_symbols_cache.get(user_id)
    # user_id 不在缓存里(新订阅/无持仓) → 空集合(不推); None 只出现在兼容路径
    if user_id is None:
        return None
    return cached if cached is not None else set()


# per-user 关注标的缓存 {user_id(或 "" = 历史遗留共享账户): {"600519", ...}}(裸 symbol)
_user_symbols_cache: dict[str | None, set[str]] = {}
_symbols_lock = threading.Lock()


def _ensure_aggregator():
    """确保聚合器线程已启动(幂等)。

    2026-08-27 fix (5+1 评审 B 轨): 原 check-then-set 非原子, 并发首连可启动
    两个聚合器线程重复拉行情+广播; 加锁包住检查与置位。
    """
    global _agg_running
    with _agg_lock:
        if _agg_running:
            return
        _agg_running = True
    t = threading.Thread(target=_aggregator_loop, name="quote-stream-agg", daemon=True)
    t.start()


def _aggregator_loop():
    """聚合器主循环: 每 5s 拉一次自选股行情, 广播。

    2026-09-09 KI-025: 无订阅者时跳过拉取 —— 自选纳入推送集后, 无脑每 5s 拉全量
    自选会在无人看盘时也持续打行情源(1.5 CPU 生产限额下无谓占用/风控风险)。
    有订阅者才拉, 新订阅者最多等一个周期(≤5s)拿到首帧; 快照改由该周期顺带填充。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            with _subscribers_lock:
                has_subscribers = bool(_subscribers)
            if has_subscribers:
                symbols = _collect_watchlist_symbols()
                if symbols:
                    data = loop.run_until_complete(_fetch_batch_quotes(symbols))
                    if data:
                        _last_snapshot.update(data)
                        _broadcast({"type": "quotes", "data": data, "ts": time.time()})
        except Exception as e:
            logger.warning(f"行情聚合器异常: {e}")
        time.sleep(_agg_interval_s)


def _collect_watchlist_symbols() -> dict[str, list[str]]:
    """从 DB 收集「自选 + 启用账户持仓」(按市场分组), 并刷新 per-user 关注集合缓存。

    2026-09-08 T8: 返回值是全体 union(供聚合器一次批量拉行情), 用户的可见
    集合落在 `_user_symbols_cache`(含 "" = 历史遗留共享账户, 人人可见)。
    2026-09-09 KI-025: 原实现只 join `positions`, 纯自选(未持仓)标的不进推送集 →
    有自选无持仓时 WS 全程静默(与模块 docstring「自选股行情推送」不符)。
    现补上 `stocks` 表(user_id 归属), 自选与持仓合并推送。
    """
    try:
        from src.web.database import SessionLocal
        from src.web.models import Position, Stock, Account

        db = SessionLocal()
        try:
            per_user: dict[str | None, set[str]] = {}
            groups: dict[str, list[str]] = defaultdict(list)

            def _add(sym: str | None, mkt: str | None, owner: str | None) -> None:
                if not sym:
                    return
                mkt = mkt or "CN"
                groups.setdefault(mkt, [])
                if sym not in groups[mkt]:
                    groups[mkt].append(sym)
                key = owner if owner else ""  # "" = 历史遗留共享账户
                # 2026-09-09 KI-025: 缓存键必须是**裸 symbol** —— 下行 data 与快照
                # 均以裸 symbol 为键(见 _fetch_batch_quotes), 而 _broadcast/subscribe
                # 用 _sym_key(k) 比对; 原存 "CN:600519" 与裸键永不相等 → 任何用户都
                # 收不到帧(含快照)。故此处存 sym, 不拼市场前缀。
                per_user.setdefault(key, set()).add(sym)

            # 1) 自选(stocks.user_id 归属)
            for sym, mkt, uid in db.query(
                Stock.symbol, Stock.market, Stock.user_id
            ).all():
                _add(sym, mkt, uid)
            # 2) 启用账户持仓(account.user_id 归属; 可能含未加自选的标的)
            for sym, mkt, uid in (
                db.query(Stock.symbol, Stock.market, Account.user_id)
                .join(Position, Position.stock_id == Stock.id)
                .join(Account, Account.id == Position.account_id)
                .filter(Account.enabled == True)  # noqa: E712
                .all()
            ):
                _add(sym, mkt, uid)

            with _symbols_lock:
                _user_symbols_cache.clear()
                _user_symbols_cache.update(per_user)
                _user_symbols_cache.setdefault(None, set())  # 占位, 不参与过滤语义
            return dict(groups)
        finally:
            db.close()
    except Exception:
        return {}


async def _fetch_batch_quotes(groups: dict[str, list[str]]) -> dict:
    """按市场分组批量拉行情, 返回 {symbol: {price, change_pct, prev_close, name}}。"""
    from src.core.marketdata_client import md_quote_rows

    out: dict = {}
    for market, symbols in groups.items():
        try:
            rows = await asyncio.to_thread(md_quote_rows, symbols, market)
            for item in rows:
                sym = item.get("symbol")
                if not sym:
                    continue
                out[sym] = {
                    "price": item.get("current_price"),
                    "change_pct": item.get("change_pct"),
                    "prev_close": item.get("prev_close"),
                    "name": item.get("name"),
                }
        except Exception as e:
            logger.debug(f"批量行情 {market} 失败: {e}")
    return out


async def _extract_ws_token(websocket) -> tuple[str, str]:
    """从 Sec-WebSocket-Protocol 头或 ?token= query 提取 JWT。

    P1-11 (2026-08-23 审计): 优先级 Sec-WebSocket-Protocol > ?token=。
    返回 (token, swp_subprotocol_or_empty); swp 非空时需在 accept() 回声子协议。
    """
    # 1) Sec-WebSocket-Protocol 头(推荐, 不进 URL/access log)
    swp = websocket.headers.get("sec-websocket-protocol", "")
    if swp:
        parts = [p.strip() for p in swp.split(",") if p.strip()]
        for i, p in enumerate(parts):
            if p == "panwatch.auth.bearer" and i + 1 < len(parts):
                return parts[i + 1], ("panwatch.auth.bearer" if "panwatch.auth.bearer" in parts else parts[0])
        # 没有标记 → 整串当 token
        if parts:
            return parts[-1], parts[0]
    # 2) ?token= query(向后兼容桌面 App)
    qtoken = websocket.query_params.get("token", "")
    return qtoken, ""


async def websocket_quote_handler(websocket):
    """WebSocket 端点实现: 连接后持续推送行情帧。

    2026-08-12 auth: 路由级 HTTPBearer 对 WS 握手 500, 这里从 query 取 token 自校验。
    前端连 ws://host/api/quotes/ws?token=<jwt>。

    P1-11 (2026-08-23 审计): 保留 ?token= 向后兼容(桌面 App 依赖), 同时推荐
    `Sec-WebSocket-Protocol: panwatch.auth.bearer, <jwt>` 方式携带 token
    (避免 nginx/access log 记录 JWT)。优先级: Sec-WebSocket-Protocol > ?token=。
    客户端示例:
        new WebSocket(url, ["panwatch.auth.bearer", "<jwt>"])  // 推荐
        new WebSocket(`${url}?token=${jwt}`)                    // 兼容旧客户端
    """
    token = ""
    swp_sub = ""
    user_id: str | None = None
    try:
        token, swp_sub = await _extract_ws_token(websocket)
        from src.web.api.auth import decode_token, verify_ws_token_payload
        from src.web.database import SessionLocal

        payload = decode_token(token) if token else None
        # 2026-09-08 T8: 不止验签, 还对齐 HTTP 层口径(is_active + token_version) —
        # 禁用账号/改密踢人后, 旧 JWT 在剩余有效期内不得再连 WS。
        db = SessionLocal()
        try:
            user_id = verify_ws_token_payload(db, payload)
        finally:
            db.close()
        if not user_id:
            await websocket.close(code=4401, reason="unauthorized")
            return
    except Exception:
        try:
            await websocket.close(code=4401, reason="unauthorized")
        except Exception:
            pass
        return

    if swp_sub:
        await websocket.accept(subprotocol=swp_sub)
    else:
        await websocket.accept()
    sid, q = subscribe(user_id)
    # P2: 断线重放 (?last_seq=上次最大seq, 只补本进程 ring 内 missed 帧; 定向帧按用户过滤)
    try:
        from src.web.realtime.envelope import replay_since

        for frame in replay_since(websocket.query_params.get("last_seq"), user_id=user_id):
            await websocket.send_text(json.dumps(frame, ensure_ascii=False))
    except Exception:
        pass
    try:
        while True:
            payload = await q.get()
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            except WebSocketDisconnect:
                break
            except Exception as e:
                # 2026-08-27 fix (5+1 评审 B 轨): 区分断开与发送异常, 不再全部静默吞
                logger.debug(f"[quote_stream] send failed: {e}")
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[quote_stream] 推送循环异常: {e}")
    finally:
        # 2026-08-27 fix: 删除下方重复的 accept+subscribe 块(复制粘贴残留:
        # 已 accept 的连接二次 accept 必抛, 且首次 subscribe 的队列永不退订);
        # 无论何种退出路径都退订, 保证断连不泄漏订阅队列。
        unsubscribe(sid)


# 2026-08-12: 模块 import 即启动聚合器(不等第一个订阅者), 保证快照始终有数据
_ensure_aggregator()
