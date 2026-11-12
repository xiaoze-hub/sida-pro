"""因子权重 API(M5):只读列表 + 手动覆盖(pin / 设权重 / 开关自动标定) + 只读标定建议。

权限(2026-11-12 独立审计):
- **写** `POST /weights/{factor_code}/{market}` 会改**全局**(所有用户共用)的因子权重,
  因此必须基于**服务端认证身份**判 `manage_settings`(与 /api/settings 同一门槛),
  不能靠路径前缀中间件、也不能信客户端自报的角色/身份。owner 与管理白名单可写,
  member / pro / demo(guest) 一律 403。判权与审计身份都来自 `require_settings_write`。
- **读** `GET /weights` 保持登录即可(登录依赖由 app 层 `protected` 提供)。
- **只读标定** `GET /calibration` 每轮要扫最近 N 天快照 × 持有期回填, 是不轻的
  查询, 因此收敛到同一 `manage_settings` 门槛(受限管理员), 并加参数上限 +
  墙钟预算; 普通成员拿到 403 而不是替全站触发一次重扫。

标定建议不落库、不可从此 API 生效:改权重只有本模块的手工覆盖路径(manual 审计)。

读库/写库(2026-11-12 复审): 本模块三个端点都用 `get_write_db`(专用写会话):
- 写端点必须先读旧值再写新值, 用路由会话会把事务绑到读副本(读到滞后旧值/写副本报错);
- 列表会补默认行(写), 标定要读「刚改过的权重」当 current_weight;
- 正确性优先于把这点读流量摊给副本。

响应由 ResponseWrapperMiddleware 统一包成 {code,data,message},路由直接返回原始数据。
"""

from __future__ import annotations

import copy
import logging
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.db_safe import safe_rollback
from src.core.error_report import log_failure, new_error_ref, public_error
from src.core.factor_calibration import calibrate_factor_weights
from src.core.factor_weights import (
    CALIBRATABLE_FACTORS,
    MARKETS,
    get_all_factor_weights,
    set_factor_weight,
)
from src.core.timezone import utc_now
from src.db.models import FactorWeight
from src.web.api.settings import SettingsActor, require_settings_write
from src.web.database import get_write_db

router = APIRouter()

logger = logging.getLogger(__name__)

# ── 只读标定的进程内缓存 ────────────────────────────────────────────────
# 一轮标定要扫最近 N 天快照 × 持有期回填, 是个不轻的只读查询; 页面/外部脚本反复
# 拉同一个市场会把 DB 打满。按「参数组合」缓存短 TTL, 并回报 cached/computed_at,
# 调用方仍能看到实际计算时间, 不会把旧结果当新结果。
_CALIB_TTL_SECONDS = 60
#: 两次**实际计算**之间的最小间隔(秒). 窗口参数可以变, 缓存键也跟着变, 所以
#: 光靠 TTL 缓存挡不住「换参数扫一轮」; 这里做**进程内**节流(不是全局), 命中缓存不受影响。
_CALIB_MIN_INTERVAL_SECONDS = 3
#: 缓存条目上限。参数本身已被 Query 有界(market×days×horizon×min_samples),
#: 但仍显式限长: 没有上限的 dict 就是一个内存增长点。
_CALIB_CACHE_MAX_ENTRIES = 32
#: 同参数并发请求等待首个结果的最长时间(秒)。只等这么久, 之后直接 429 ——
#: 无限等待会占满 FastAPI 的同步线程池。
_CALIB_WAIT_SECONDS = 1.0
_CALIB_CACHE: dict[tuple, dict] = {}
#: 正在计算的 key → Event。**进程内最多一轮实际计算**: 并发不同参数也不并行,
#: 否则一个脚本换参数就能把线程池/DB 一起打满。
_CALIB_INFLIGHT: dict[tuple, threading.Event] = {}
_CALIB_LOCK = threading.Lock()
_CALIB_LAST_COMPUTE = 0.0
#: 一轮计算的墙钟预算(秒): 只在**因子之间**检查, 超了就中止剩下的因子并如实
#: 报「超时/未写入」。它**不能中断**已经开始的那一次 `evaluate_factor_ic`
#: (Python 线程无法安全打断), 所以单次 IC 评估本身挂住时预算不生效 ——
#: 这是「不让一轮按因子个数无限拖」的粗粒度上限, **不是防挂死机制**。
#: 真正兜住耗时的仍然是参数上限(days<=180 / horizon<=30 / min_samples<=200)。
_CALIB_TIME_BUDGET_SECONDS = 20.0

#: 并发保护**范围**: 只有本进程内的 single-flight。生产 `WEB_WORKERS` 默认 4,
#: 进程内状态跨不过进程边界, 所以这里如实写 process / cross_worker_exclusive=False,
#: 不宣称任何「全局独占」—— 之前那层 Redis 租约已被移除(它没有续租、失败时
#: 静默降级, 既不能保证全链路互斥, 又让响应看起来像"已全局串行")。
_CONCURRENCY_SCOPE = {
    "scope": "process",
    "cross_worker_exclusive": False,
    "cache_scope": "process",
    "note": (
        "只有本进程内的 single-flight: 同一 worker 同一时刻最多一轮实际计算; "
        "多 worker 部署下每个 worker 可能各有 1 轮, 结果缓存也是每 worker 一份。"
        "没有跨 worker/全局互斥(未使用 Redis 租约)。"
    ),
}


def _reset_calibration_cache() -> None:
    """清空缓存、single-flight 与节流状态(测试/运维热重启用; 不加 HTTP 入口)。"""
    global _CALIB_LAST_COMPUTE
    with _CALIB_LOCK:
        _CALIB_CACHE.clear()
        for ev in _CALIB_INFLIGHT.values():
            ev.set()
        _CALIB_INFLIGHT.clear()
        _CALIB_LAST_COMPUTE = 0.0


def _weights_fingerprint(db: Session, market: str) -> tuple:
    """当前权重/开关的指纹: 命中缓存前比对, 防止回放**陈旧 current_weight**。"""
    rows = (
        db.query(
            FactorWeight.factor_code,
            FactorWeight.weight,
            FactorWeight.is_pinned,
            FactorWeight.auto_calibrate,
        )
        .filter(FactorWeight.market == market)
        .all()
    )
    return tuple(
        sorted(
            (str(r[0]), round(float(r[1]), 6), bool(r[2]), bool(r[3]))
            for r in rows
        )
    )


def _cache_get(key: tuple) -> dict | None:
    """取缓存(深拷贝)。返回值与缓存对象不共享任何可变结构。"""
    entry = _CALIB_CACHE.get(key)
    if entry is None:
        return None
    return copy.deepcopy(entry["payload"])


def _cache_put(key: tuple, payload: dict, fingerprint: tuple, now) -> None:
    """写入成功结果并做 LRU 式限长; 失败结果**绝不**入缓存。"""
    entry = {
        "_at": now,
        "fingerprint": fingerprint,
        "payload": copy.deepcopy(payload),
    }
    _CALIB_CACHE.pop(key, None)
    _CALIB_CACHE[key] = entry
    while len(_CALIB_CACHE) > _CALIB_CACHE_MAX_ENTRIES:
        _CALIB_CACHE.pop(next(iter(_CALIB_CACHE)))


def _request_ip(request: Request | None) -> str:
    """审计用客户端 IP; 取不到就留空(不影响判权/写入)。"""
    if request is None:
        return ""
    try:
        from src.web.middleware import _get_client_ip

        return _get_client_ip(request) or ""
    except Exception:  # noqa: BLE001
        return ""


def _concurrency_block() -> dict:
    """把并发保护范围翻译成**可展示的事实**(不美化: 只有进程内就说只有进程内)。"""
    return dict(_CONCURRENCY_SCOPE)


class FactorWeightUpdate(BaseModel):
    """手动覆盖入参,均可选(只传要改的字段)。

    故意**不收** actor/by 之类身份字段: 身份只认服务端认证结果, 请求体里塞什么
    都不影响审计。
    """

    weight: float | None = None
    is_pinned: bool | None = None
    auto_calibrate: bool | None = None


@router.get("/weights")
def list_weights(db: Session = Depends(get_write_db)):
    """列出所有市场 × 因子的权重 + 最近记录到的 IC/IR 观测(只读展示)。

    `last_ic/last_ir` 是 meta 里**最后一次记录的观测**, 不是实时值 —— 响应里
    `last_observation_at / last_observation_stale / observation_note` 显式说明
    观测时间与是否过期; 要当前口径请看 `GET /calibration`。

    首次打开面板会补齐默认权重行, 这是写操作, 所以走写库会话。
    """
    return {"items": get_all_factor_weights(db=db)}


@router.get("/calibration")
def calibration(
    market: str = Query("CN", description="市场(CN/HK/US)"),
    days: int = Query(90, ge=7, le=180, description="回看快照天数(上限收敛: 越大越慢)"),
    horizon: int = Query(5, ge=1, le=30, description="持有期(交易日)"),
    min_samples: int = Query(20, ge=3, le=200, description="出建议的最小样本量"),
    db: Session = Depends(get_write_db),
    _actor: SettingsActor = Depends(require_settings_write),
):
    """实时计算一轮**只读**因子标定建议(零写入、不 lazy seed)。

    权限(2026-11-12 复审): 收口到 `manage_settings`。这不是改数据的操作, 但每轮
    都要扫快照 + 回填持有期, 让任意登录用户反复触发等于把只读重查询开放给所有人;
    受限管理员 + 参数上限 + 墙钟预算才是有界的面。

    返回里的可消费信息:
    - `computed_at`: 本次实际计算时间(缓存命中时仍是首次计算时间)
    - `ok` / `error` / `error_ref`: 失败时 `error` 是**安全文案 + 关联号**,
      不含异常文本/SQL/连接串; 具体原因在服务端日志 `[err:<ref>]`
    - `rows_present` / `no_rows`: 库里已有多少权重行(本端点不会替你建行)
    - 每因子 `sample_size` + `status`: `skipped_no_data`(无已走完持有期的样本) /
      `skipped_low_sample`(有样本但不够) / `skipped_ic_unavailable`(期数不够算 IC) /
      `proposed` 等, 前三种含义不同, 不要混读
    - `changed` 恒为 0、`write_back` 恒为 False: 建议**不会**生效, 改权重仍须走
      受 manage_settings 保护的手工覆盖端点。
    - `concurrency`: 这次计算实际受到的并发保护**范围**(见下)。缓存命中时描述的
      是产生这份结果的那一轮。

    并发与缓存(2026-11-12 复审后):
    - **只有进程内 single-flight**: 本进程最多一轮实际计算; 同参数并发请求有界等待
      (最多 `_CALIB_WAIT_SECONDS`)复用结果, 其余立即 429。
    - **不宣称跨 worker / 全局互斥**: 多 worker 部署下每个 worker 各自最多一轮,
      响应里恒为 `concurrency.scope="process"` / `cross_worker_exclusive=false`。
      之前那层 Redis 租约(`readonly_gate`)已移除 —— 它没有续租、Redis 抖动时静默
      降级, 既给不出全链路独占, 又容易被误读成"已全局串行"。
    - 缓存条目有上限(`_CALIB_CACHE_MAX_ENTRIES`), TTL 60s; **失败结果不入缓存**。
      缓存是**每 worker 一份**(进程内), 不从 Redis 共享。
    - 命中缓存前比对当前权重/pin/开关指纹(读写库): 人工改过权重就作废旧缓存,
      绝不回放陈旧的 `current_weight`。
    - 墙钟预算 `_CALIB_TIME_BUDGET_SECONDS`(20s): 粒度是**因子之间**。
      超预算会中止后续因子并返回 `ok=False`(`timeout=true`), 但**不具备中断
      能力**: 单次 `evaluate_factor_ic` 已经开始就打断不了, 它在内部挂住时
      预算不会生效。因此这里**不承诺「防挂死」**, 只承诺「不按因子个数叠加
      下去」; 耗时上限主要靠参数上限(days/horizon/min_samples)与进程内节流。
    - 缓存按调用方**深拷贝**返回, 一个请求改返回值不会污染其他人的建议。
    """
    global _CALIB_LAST_COMPUTE

    market = (market or "CN").strip().upper()
    if market not in MARKETS:
        raise HTTPException(400, detail=f"未知市场: {market}")

    key = (market, int(days), int(horizon), int(min_samples))
    now = utc_now()
    fingerprint = _weights_fingerprint(db, market)
    waiter: threading.Event | None = None
    owner: threading.Event | None = None

    with _CALIB_LOCK:
        hit = _CALIB_CACHE.get(key)
        hit_at = hit["_at"] if hit is not None else None
        fingerprint_changed = bool(
            hit is not None and hit["fingerprint"] != fingerprint
        )
        if (
            hit_at is not None
            and not fingerprint_changed
            and (now - hit_at).total_seconds() < _CALIB_TTL_SECONDS
        ):
            out = _cache_get(key)
            out["cached"] = True
            out["cache_age_seconds"] = round(
                (now - hit_at).total_seconds(), 3
            )
            return out
        if fingerprint_changed:
            # 权重/pin/开关被人工改过: 旧建议里的 current_weight 已经不对,
            # 直接丢弃这条缓存(而不是把旧值当「当前」发出去)。
            _CALIB_CACHE.pop(key, None)

        if key in _CALIB_INFLIGHT:
            # 同参数已有请求在算: 有界等待它的结果, 不再起第二轮。
            waiter = _CALIB_INFLIGHT[key]
        elif _CALIB_INFLIGHT:
            # 别的参数正在算: 本进程只允许一轮实际计算, 直接拒绝。
            raise HTTPException(
                429,
                detail={
                    "message": "已有标定计算进行中, 请稍后再试",
                    "scope": "process",
                    "retry_after_seconds": _CALIB_MIN_INTERVAL_SECONDS,
                },
            )
        else:
            started = time.monotonic()
            if not fingerprint_changed and (
                started - _CALIB_LAST_COMPUTE < _CALIB_MIN_INTERVAL_SECONDS
            ):
                # 新参数/新计算之间的进程内节流(指纹变化 = 人工改权重, 放行一次重算)。
                raise HTTPException(
                    429,
                    detail={
                        "message": "标定计算过于频繁, 请稍后再试",
                        "scope": "process",
                        "retry_after_seconds": round(
                            _CALIB_MIN_INTERVAL_SECONDS
                            - (started - _CALIB_LAST_COMPUTE),
                            1,
                        ),
                    },
                )
            # 锁内占位: 同一瞬间的并发请求只有这一轮真正去扫库。
            owner = threading.Event()
            _CALIB_INFLIGHT[key] = owner
            _CALIB_LAST_COMPUTE = started

    if waiter is not None:
        # 等别的请求算完(有界等待)。等不到就走 429, 不能无限占用线程。
        if waiter.wait(_CALIB_WAIT_SECONDS):
            with _CALIB_LOCK:
                ready = _CALIB_CACHE.get(key)
                if ready is not None:
                    out = _cache_get(key)
                    out["cached"] = True
                    out["cache_age_seconds"] = round(
                        (utc_now() - ready["_at"]).total_seconds(), 3
                    )
                    return out
        raise HTTPException(
            429,
            detail={
                "message": "标定计算进行中, 请稍后再试",
                "scope": "process",
                "retry_after_seconds": round(_CALIB_WAIT_SECONDS, 1),
            },
        )

    assert owner is not None  # 走到这里一定是本轮的实际计算者

    try:
        result = calibrate_factor_weights(
            market, days=int(days), horizon=int(horizon),
            min_samples=int(min_samples), db=db,
            time_budget_seconds=_CALIB_TIME_BUDGET_SECONDS,
        )
        payload = dict(result)
        payload["cached"] = False
        payload["cache_age_seconds"] = 0.0
        payload["cache_ttl_seconds"] = _CALIB_TTL_SECONDS
        payload["concurrency"] = _concurrency_block()

        with _CALIB_LOCK:
            _CALIB_LAST_COMPUTE = time.monotonic()
            # 失败的 payload 不入缓存: 否则一次 DB 抖动会被复读一整分钟。
            if payload.get("ok") is not False:
                _cache_put(key, payload, fingerprint, now)
        return payload
    finally:
        with _CALIB_LOCK:
            _CALIB_INFLIGHT.pop(key, None)
            owner.set()


@router.post("/weights/{factor_code}/{market}")
def update_weight(
    factor_code: str,
    market: str,
    payload: FactorWeightUpdate,
    request: Request,
    db: Session = Depends(get_write_db),
    actor: SettingsActor = Depends(require_settings_write),
):
    """手动覆盖某因子权重 / pin / 开关自动标定(权重变化写 manual 审计)。

    权限 = `manage_settings`(owner 或经白名单授权的成员), 由
    `require_settings_write` 实时从**写库**读角色判定; 审计身份用该依赖返回的
    服务端身份, 请求体里的任何身份字段都不采信。

    这里再兜一道: 依赖理论上必然给出 username, 但万一为空也**不许**落到核心层
    「无身份写全局权重」的老路 —— 直接 403, 库内零变化。

    错误口径(2026-11-12 复审): 参数/身份类错误(ValueError)是给人看的校验文案,
    原样 400; 其余异常**不回显异常文本**(可能带 SQL/表名/DSN), 统一 500 +
    安全文案 + 关联号, 先回滚再返回 —— 不留半截写入。
    """
    actor_name = (getattr(actor, "username", None) or "").strip()
    if not actor_name:
        raise HTTPException(403, "无法确认操作者身份, 拒绝修改全局因子权重")
    if factor_code not in CALIBRATABLE_FACTORS:
        raise HTTPException(400, detail=f"未知因子: {factor_code}")
    market = (market or "").strip().upper()
    if market not in MARKETS:
        raise HTTPException(400, detail=f"未知市场: {market}")
    try:
        return set_factor_weight(
            factor_code, market,
            weight=payload.weight, is_pinned=payload.is_pinned,
            auto_calibrate=payload.auto_calibrate, db=db,
            actor=actor_name,
            actor_id=getattr(actor, "id", None),
            actor_kind="user",
            via="api", ip=_request_ip(request),
        )
    except ValueError as e:
        # 核心层的校验文案(未知因子/市场、非法 actor_kind ...), 不含库内细节。
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 - 内部错误统一脱敏
        ref = new_error_ref()
        log_failure(
            logger, "因子权重写入", e, ref=ref,
            context={
                "factor_code": factor_code,
                "market": market,
                "actor": actor_name,
                "via": "api",
            },
        )
        safe_rollback(db)
        raise HTTPException(status_code=500, detail=public_error("因子权重写入", ref))
