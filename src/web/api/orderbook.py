# -*- coding: utf-8 -*-
"""OB 失衡条 轻接口(分时卡片用, 2026-08-20)。

GET /api/orderbook-ob?symbol=USZA002361
GET /api/orderbook-ob/USZA002361
→ 响应包装后 {code:0, success:true, data: {available, ob_series, events, ghost_ratio, note}}

- available   : bool 数据是否可用(thsdk 缺失/取数失败/数据为空 → False, 不伪造)
- ob_series   : 盘口失衡序列(每快照一个), 每项含 {ob, label, bid_amt10, ask_amt10, ts, dt}
- events      : 盘口演变事件(托单/压单/撤单/幽灵单)
- ghost_ratio : 幽灵单比率
- note        : 真实说明(时段/数据状态), 取数失败时给出原因
- source      : 数据来源标注('thsdk' 实时 / 'img' 离线快照兜底); 成功路径不新增字段
- as_of       : 数据时间(降级/快照兜底时标注, 不得拿陈旧值冒充实时)

依赖: src.core.orderbook_engine.run()(THS L2 真实盘口, 20 档)。
thsdk 未安装时引擎模块级软依赖自动置 None, 本接口返回 available:false + 真实原因。

## 2026-10-01: 上游瞬时抖动治理(B1 P2 间歇 502)

现场: `/stocks/002361` 命中过一次 502 Bad Gateway, 复现时 4s 内返回 200 —— 上游间歇故障。

根因: 本接口此前对 `orderbook_engine.run()` **没有硬超时**。`run()` 内每个快照走
`fetch_snapshot`(内部 3 轮退避, 单次可卡 30s, 实测 thsdk -6 超时), 5 个快照叠加
总耗时可达 90s+(既有 KI-030「/api/orderbook-ob 93s 慢响应」), 拖垮反代读超时 ⇒ 502。
抛异常的分支本就被 `except` 兜住返回 200, 真正漏网的是**挂住不返回**。

对症修(对齐 klines.py `_build_orderbook` 的既有姿势):
  1. 硬超时护栏: `call_with_hard_timeout` 包裹 `run()`, 单次上限 `_HARD_TIMEOUT_S`,
     挂死也不阻塞反代(挂死线程仍占 thsdk 并发槽, 见 thsdk_breaker);
  2. 有限次重试 + 退避: 上游瞬时失败时重试 `_RETRY_ATTEMPTS` 次, 退避基数 `_RETRY_BACKOFF_S`;
  3. 显式降级: 重试仍失败 → 返回 200 + `available:false` + 真实 `note` + `source`/`as_of`,
     **绝不**把上游抖动直接变成页面 502;
  4. 快照兜底: 有本地 `.img` 离线快照时优先用最新一帧兜底, 并在 `source`/`as_of`/`note`
     里**显式标注**来源与时间(离线快照 ≠ 实时, 不冒充)。

成功路径返回值与修改前逐字段一致(见 tests/test_orderbook_ob_endpoint.py)。
"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request

from src.core import orderbook_engine
from src.core.img_orderbook_store import SHARED_SCOPE

logger = logging.getLogger(__name__)

router = APIRouter()

# A股盘口时间口径统一 Asia/Shanghai(UTC 宿主 naive now 会错 8h)
_CST = ZoneInfo("Asia/Shanghai")

# 轻接口快照参数: 卡片每 30s 刷新, 采集 5 个快照(间隔 0.3s) ≈ 2-3s 完成, 避免阻塞卡片
_N_SNAPSHOTS = 5
_INTERVAL_S = 0.3

# 上游瞬时抖动治理(B1 P2 间歇 502)
# 单次 run() 硬超时: 正常 5 快照 ≈ 3s, 留足余量; 超时即弃用直接走降级, 不阻塞反代
_HARD_TIMEOUT_S = 12.0
# 有限次重试(首次 + 1 次重试)与退避基数: 上游瞬时失败重试一次即可, 不做长退避
_RETRY_ATTEMPTS = 2
_RETRY_BACKOFF_S = 0.5


def _now_iso() -> str:
    """当前上海时间(标注降级/快照兜底的数据时间用)。"""
    return datetime.now(_CST).isoformat(timespec="seconds")


def _normalize_symbol(raw: str) -> str:
    """兼容 THS 代码(USZA002361)与裸 6 位代码(002361 → USZA/USSH)。"""
    code = (raw or "").strip()
    if not code:
        return code
    if code.isdigit() and len(code) == 6:
        return f"USSH{code}" if code[0] == "6" else f"USZA{code}"
    return code


def _degraded(
    note: str,
    *,
    source: str = "thsdk",
    as_of: str | None = None,
    events: list | None = None,
    ghost_ratio: float = 0.0,
) -> dict:
    """显式降级响应(available:false): 带真实原因 + 来源/时间标注, 绝不伪造盘口数据。"""
    return {
        "available": False,
        "ob_series": [],
        "events": events or [],
        "ghost_ratio": ghost_ratio,
        "note": note,
        "source": source,
        "as_of": as_of or _now_iso(),
    }


def _run_guarded(
    ths_code: str,
    n_snapshots: int,
    interval: float,
) -> tuple[dict | None, int]:
    """带硬超时 + 有限次重试/退避地调 `orderbook_engine.run`。

    Returns:
        (result | None, attempts): result 为 None 表示重试耗尽仍未拿到数据。
    """
    from src.core.thsdk_breaker import call_with_hard_timeout

    attempts = 0
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        attempts = attempt
        try:
            res = call_with_hard_timeout(
                lambda: orderbook_engine.run(
                    ths_code, n_snapshots=n_snapshots, interval=interval),
                default=None,
                timeout_s=_HARD_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001  护栏自身异常也不外泄成 5xx
            logger.warning("orderbook-ob 取数护栏异常 %s(第 %d 次): %s", ths_code, attempt, e)
            res = None
        if res is not None:
            return res, attempts
        if attempt < _RETRY_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None, attempts


def _img_snapshot_fallback(symbol: str) -> dict | None:
    """本地 .img 离线快照兜底: 有则用最新一帧跑同一套算法, 显式标注 source/as_of。

    离线快照 ≠ 实时, 仅在实时取数重试耗尽后兜底, 且 note/source/as_of 全部标注来源与时间。
    """
    try:
        img_path = orderbook_engine.find_img_file(symbol, "CN")
        if not img_path:
            return None
        snaps = orderbook_engine.load_snapshots_from_img(img_path)
        if not snaps:
            return None
        ob_series = orderbook_engine.order_book_imbalance(snaps)
        if not ob_series:
            return None
        events = orderbook_engine.order_book_evolution(snaps)
        _ghosts, ghost_ratio = orderbook_engine.ghost_order(snaps)
        as_of = snaps[-1].get("dt")
        return {
            "available": True,
            "ob_series": ob_series,
            "events": events,
            "ghost_ratio": ghost_ratio,
            "note": (
                f"实时盘口不可用, 已回退本地 .img 离线快照({img_path}) 兜底; "
                f"该数据为离线快照非实时, 数据时间 {as_of or '未知'}"
            ),
            "source": "img",
            "as_of": as_of or _now_iso(),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("orderbook-ob .img 快照兜底失败 %s: %s", symbol, e)
        return None


def get_orderbook_ob(
    symbol: str,
    n_snapshots: int = _N_SNAPSHOTS,
    interval: float = _INTERVAL_S,
) -> dict:
    """OB 失衡条纯函数: 调 orderbook_engine.run 取真实 THS L2 盘口, 组装卡片数据。

    任何失败(thsdk 缺失 / 取数失败 / 数据为空 / 上游超时)都返回 available:false + 真实 note,
    绝不伪造盘口数据; 上游瞬时抖动做有限次重试 + 退避 + 硬超时, 重试仍失败显式降级(200)。
    """
    ths_code = _normalize_symbol(symbol)
    if not ths_code:
        return _degraded("symbol 为空, 无法取盘口数据")

    # thsdk 缺失: 引擎模块级软依赖 THS=None, 这里显式预检给出明确原因
    if orderbook_engine.THS is None:
        return _degraded("thsdk 未安装, 无法拉取真实 L2 盘口(OB 失衡条不可用)")

    result, attempts = _run_guarded(ths_code, n_snapshots, interval)

    if result is None:
        # 实时取数失败(异常 / 硬超时): 先用本地 .img 快照兜底, 否则显式降级
        fallback = _img_snapshot_fallback(ths_code)
        if fallback is not None:
            return fallback
        return _degraded(
            f"盘口取数失败(上游 thsdk 连续 {attempts} 次不可用或超时), 本次无数据",
            source="thsdk",
        )

    ob_series = result.get("ob_series") or []
    if not ob_series:
        return _degraded(
            "盘口无数据(非交易时段或无成交), 未产出 OB 序列",
            events=result.get("events") or [],
            ghost_ratio=result.get("ghost_ratio", 0.0),
        )

    # 成功路径: 字段与修改前逐字段一致(不新增 source/as_of, 避免影响既有消费方)
    return {
        "available": True,
        "ob_series": ob_series,
        "events": result.get("events") or [],
        "ghost_ratio": result.get("ghost_ratio", 0.0),
        "note": result.get("summary") or "OK",
    }


# ─────────────────────────────────────────────────────────────────────────
# .img 十档盘口(已校准解析 + 入库回放) —— 2026-10-02
# ─────────────────────────────────────────────────────────────────────────
# 与 /api/orderbook-ob 同一套显式错误态语义: available/source/as_of/note 全标注;
# 缺数据一律「无数据」, **不补 0**(0 是有意义的挂单量)。
# 多用户隔离: 读路径按当前登录用户 user_id 过滤; 未登录回退共享 scope(公共市场数据)。


def _scope_user_id(request: Request | None) -> str:
    """尽力解析登录用户作数据隔离 scope; 无/无效 token → 共享 scope。

    只做签名/有效期校验(不查库、不因未登录而 401) —— 读路径始终按解析出的
    user_id 过滤, 未登录态落共享 scope, 不会串到别的用户数据。
    """
    try:
        from src.core.auth_tokens import decode_token
        from src.web.api.auth import extract_token_from_request

        raw = extract_token_from_request(request, None) if request is not None else None
        if raw:
            payload = decode_token(raw) or {}
            uid = payload.get("sub")
            if uid:
                return str(uid)
    except Exception as e:  # noqa: BLE001  解析失败只影响 scope, 不阻断读
        logger.debug("解析 .img 盘口用户 scope 失败, 回退 shared: %s", e)
    return SHARED_SCOPE


def _to_code(raw: str) -> str:
    """THS 代码(USZA002361)/裸 6 位 → 6 位代码(入库键)。"""
    s = (raw or "").strip().upper()
    for prefix in ("USZA", "USHA", "USBJ", "USTM"):
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            return s[len(prefix):]
    return s


def _row_to_img_snapshot(row: dict):
    """入库行 → `tdx_img_parser.ImgSnapshot`(缺失保持 None, 不补 0)。"""
    from src.core.tdx_img_parser import ImgSnapshot

    as_of = row.get("as_of") or ""
    t = as_of.split("T", 1)[1][:8] if "T" in as_of else (row.get("as_of") or None)
    return ImgSnapshot(
        t=t,
        bid_prices=row.get("bid_prices") or [],
        bid_vols=row.get("bid_vols") or [],
        ask_prices=row.get("ask_prices") or [],
        ask_vols=row.get("ask_vols") or [],
        bid_orders=row.get("bid_orders"),
        ask_orders=row.get("ask_orders"),
        queue=row.get("ask_queue"),
        bid_queue=row.get("bid_queue"),
    )


def _img_degraded(symbol: str, note: str, *, source: str = "img",
                  as_of: str | None = None) -> dict:
    """`.img` 盘口显式降级(available:false): 真实原因 + 来源/时间, 缺数据不补 0。"""
    from src.core.tdx_img_parser import MISSING

    return {
        "available": False,
        "source": source,
        "as_of": as_of or _now_iso(),
        "note": note,
        "symbol": symbol,
        "trade_date": None,
        "frames": 0,
        "origin": None,
        "shape": None,
        "best_bid": None,
        "best_ask": None,
        "bid_pressure": None,
        "spread": None,
        "book": {"bid": [], "ask": []},
        "bid_queue": MISSING,
        "ask_queue": MISSING,
        "bid_orders": MISSING,
        "ask_orders": MISSING,
        "queue_imbalance": MISSING,
    }


def _img_response(symbol: str, row: dict, total: int, origin: str, note: str) -> dict:
    """一行入库帧 → 端点响应(十档 + 队列 + 派生; 缺失处「无数据」)。"""
    from src.core import tdx_img_parser as tip

    snap = _row_to_img_snapshot(row)
    frame = tip.snapshots_to_frames([snap])[0]
    ob = orderbook_engine.order_book_queue(
        orderbook_engine.img_frame_to_snapshot(snap, ts=0.0, dt_iso=row.get("as_of"))
    )
    return {
        "available": True,
        "source": "img",
        "as_of": row.get("as_of"),
        "note": note,
        "symbol": symbol,
        "trade_date": row.get("trade_date"),
        "frames": total,
        "origin": origin,
        "shape": ob.get("shape"),
        "best_bid": ob.get("best_bid"),
        "best_ask": ob.get("best_ask"),
        "bid_pressure": ob.get("bid_pressure"),
        "spread": ob.get("spread"),
        "book": {"bid": frame["bid"], "ask": frame["ask"]},
        "bid_queue": snap.bid_queue if snap.bid_queue else tip.MISSING,
        "ask_queue": snap.queue if snap.queue else tip.MISSING,
        "bid_orders": frame["bid_orders"],
        "ask_orders": frame["ask_orders"],
        "queue_imbalance": frame["queue_imb"],
    }


def _pick_row(rows: list[dict], as_of: str | None) -> dict | None:
    """取 as_of(含)之前最近一帧; 未给 as_of → 最新一帧。"""
    if not as_of:
        return rows[-1]
    eligible = [r for r in rows if (r.get("as_of") or "") <= as_of]
    return eligible[-1] if eligible else None


def get_img_orderbook(
    symbol: str,
    *,
    user_id: str = SHARED_SCOPE,
    as_of: str | None = None,
    limit: int | None = None,
) -> dict:
    """`.img` 十档盘口(已入库优先, 本地 .img 兜底)纯函数, 显式错误态。

    顺序: 入库帧(user_id 隔离) → 本地 `PANWATCH_IMG_DIR` 下 .img → 显式「无数据」。
    任一环节失败不抛, 返回 available:false + 真实 note; 缺档不补 0。
    """
    from src.core import img_orderbook_store as store

    code = _to_code(symbol)
    if not code:
        return _img_degraded(symbol, "symbol 为空, 无法取 .img 盘口")

    rows = store.load_frames(code, user_id=user_id, limit=limit)
    if rows:
        row = _pick_row(rows, as_of)
        if row is not None:
            return _img_response(
                symbol, row, len(rows), "db",
                f"通达信 .img 离线盘口(已入库, {len(rows)} 帧可选, user={user_id}); 非实时",
            )
        return _img_degraded(
            symbol,
            f"已入库 {len(rows)} 帧, 但无 as_of <= {as_of} 的帧",
            as_of=as_of,
        )

    # 未入库 → 本地 .img 文件兜底(显式标注来源与时间为文件内帧时间)
    try:
        from src.core import tdx_img_parser as tip

        img_path = orderbook_engine.find_img_file(code, "CN")
        if img_path:
            snaps = tip.parse_img(img_path)
            if snaps:
                snap = snaps[-1]  # 最新一帧
                frame = tip.snapshots_to_frames([snap])[0]
                return {
                    "available": True,
                    "source": "img-file",
                    "as_of": snap.t,
                    "note": (
                        f".img 未入库, 回退本地文件 {img_path}(离线快照, 非实时); "
                        f"数据时间 {snap.t or '未知'}"
                    ),
                    "symbol": symbol,
                    "trade_date": store.trade_date_from_path(img_path),
                    "frames": len(snaps),
                    "origin": "file",
                    "shape": orderbook_engine.order_book_queue(
                        orderbook_engine.img_frame_to_snapshot(snap, ts=0.0, dt_iso=snap.t)
                    ).get("shape"),
                    "best_bid": frame["bid"][0]["price"],
                    "best_ask": frame["ask"][0]["price"],
                    "bid_pressure": frame["bid_pressure"],
                    "spread": frame["spread"],
                    "book": {"bid": frame["bid"], "ask": frame["ask"]},
                    "bid_queue": snap.bid_queue if snap.bid_queue else tip.MISSING,
                    "ask_queue": snap.queue if snap.queue else tip.MISSING,
                    "bid_orders": frame["bid_orders"],
                    "ask_orders": frame["ask_orders"],
                    "queue_imbalance": frame["queue_imb"],
                }
    except Exception as e:  # noqa: BLE001  .img 兜底失败不外泄成 5xx
        logger.warning(".img 盘口文件兜底失败 %s: %s", code, e)

    return _img_degraded(
        symbol,
        f"无 .img 盘口数据(user={user_id} 无入库帧, 且本地 .img 不可用/解析失败); "
        f"未编造任何盘口数字",
    )


@router.get("")
def orderbook_ob(symbol: str = Query(..., description="THS 代码(如 USZA002361)或 6 位代码(如 002361)")):
    """OB 失衡条(轻接口, 分时卡片用): 买|卖压比例 + 盘口演变事件 + 幽灵单比率。"""
    return get_orderbook_ob(symbol)


@router.get("/img")
def orderbook_img(
    request: Request,
    symbol: str = Query(..., description="6 位代码(如 002361)或 THS 代码(如 USZA002361)"),
    as_of: str | None = Query(None, description="取该时间点(含)之前最近一帧, ISO 如 2026-08-27T09:30:00"),
    limit: int | None = Query(None, ge=1, le=5000, description="最多回看多少已入库帧"),
):
    """`.img` 十档盘口 + 委托队列(已校准解析; 已入库优先, 本地文件兜底)。

    显式错误态: available / source / as_of / note 全标注; 缺档一律「无数据」, 不补 0。
    读路径按当前登录用户隔离, 未登录回退共享 scope。
    """
    return get_img_orderbook(
        symbol, user_id=_scope_user_id(request), as_of=as_of, limit=limit
    )


@router.get("/{symbol}")
def orderbook_ob_path(symbol: str):
    """路径式别名: /api/orderbook-ob/USZA002361。"""
    return get_orderbook_ob(symbol)
