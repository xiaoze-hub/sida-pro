"""因子权重 API(M5):只读列表 + 手动覆盖(pin / 设权重 / 开关自动标定) + 只读标定建议。

权限(2026-11-12 独立审计):
- **写** `POST /weights/{factor_code}/{market}` 会改**全局**(所有用户共用)的因子权重,
  因此必须基于**服务端认证身份**判 `manage_settings`(与 /api/settings 同一门槛),
  不能靠路径前缀中间件、也不能信客户端自报的角色/身份。owner 与管理白名单可写,
  member / pro / demo(guest) 一律 403。判权与审计身份都来自 `require_settings_write`。
- **读** 保持登录即可(登录依赖由 app 层 `protected` 提供)。

标定建议不落库、不可从此 API 生效:改权重只有本模块的手工覆盖路径(manual 审计)。

响应由 ResponseWrapperMiddleware 统一包成 {code,data,message},路由直接返回原始数据。
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.factor_calibration import calibrate_factor_weights
from src.core.factor_weights import (
    CALIBRATABLE_FACTORS,
    MARKETS,
    get_all_factor_weights,
    set_factor_weight,
)
from src.core.timezone import utc_now
from src.web.api.settings import SettingsActor, require_settings_write
from src.web.database import get_db

router = APIRouter()

# ── 只读标定的进程内缓存 ────────────────────────────────────────────────
# 一轮标定要扫最近 N 天快照 × 持有期回填, 是个不轻的只读查询; 页面/外部脚本反复
# 拉同一个市场会把 DB 打满。按「参数组合」缓存短 TTL, 并回报 cached/computed_at,
# 调用方仍能看到实际计算时间, 不会把旧结果当新结果。
_CALIB_TTL_SECONDS = 60
#: 两次**实际计算**之间的最小间隔(秒). 窗口参数可以变, 缓存键也跟着变, 所以
#: 光靠 TTL 缓存挡不住「换参数扫一轮」; 这里做全局节流, 命中缓存不受影响。
_CALIB_MIN_INTERVAL_SECONDS = 3
_CALIB_CACHE: dict[tuple, dict] = {}
_CALIB_LOCK = threading.Lock()
_CALIB_LAST_COMPUTE = 0.0


def _reset_calibration_cache() -> None:
    """清空缓存与节流状态(测试/运维热重启用; 不加 HTTP 入口)。"""
    global _CALIB_LAST_COMPUTE
    with _CALIB_LOCK:
        _CALIB_CACHE.clear()
        _CALIB_LAST_COMPUTE = 0.0


class FactorWeightUpdate(BaseModel):
    """手动覆盖入参,均可选(只传要改的字段)。

    故意**不收** actor/by 之类身份字段: 身份只认服务端认证结果, 请求体里塞什么
    都不影响审计。
    """

    weight: float | None = None
    is_pinned: bool | None = None
    auto_calibrate: bool | None = None


@router.get("/weights")
def list_weights(db: Session = Depends(get_db)):
    """列出所有市场 × 因子的权重 + 最近记录到的 IC/IR 观测(只读)。

    `last_ic/last_ir` 是 meta 里**最后一次记录的观测**, 不是实时值 —— 响应里
    `last_observation_at / last_observation_stale / observation_note` 显式说明
    观测时间与是否过期; 要当前口径请看 `GET /calibration`。
    """
    return {"items": get_all_factor_weights(db=db)}


@router.get("/calibration")
def calibration(
    market: str = Query("CN", description="市场(CN/HK/US)"),
    days: int = Query(90, ge=7, le=365, description="回看快照天数"),
    horizon: int = Query(5, ge=1, le=60, description="持有期(交易日)"),
    min_samples: int = Query(20, ge=3, le=500, description="出建议的最小样本量"),
    db: Session = Depends(get_db),
):
    """实时计算一轮**只读**因子标定建议(零写入、不 lazy seed)。

    返回里的可消费信息:
    - `computed_at`: 本次实际计算时间(缓存命中时仍是首次计算时间)
    - `ok` / `error`: 计算是否成功; 失败的原因原样带出, 不伪装成「无建议」
    - `rows_present` / `no_rows`: 库里已有多少权重行(本端点不会替你建行)
    - 每因子 `sample_size` + `status`: `skipped_no_data`(无已走完持有期的样本) /
      `skipped_low_sample`(有样本但不够) / `skipped_ic_unavailable`(期数不够算 IC) /
      `proposed` 等, 前三种含义不同, 不要混读
    - `changed` 恒为 0、`write_back` 恒为 False: 建议**不会**生效, 改权重仍须走
      受 manage_settings 保护的手工覆盖端点。
    """
    global _CALIB_LAST_COMPUTE

    market = (market or "CN").strip().upper()
    if market not in MARKETS:
        raise HTTPException(400, detail=f"未知市场: {market}")

    key = (market, int(days), int(horizon), int(min_samples))
    now = utc_now()
    with _CALIB_LOCK:
        cached = _CALIB_CACHE.get(key)
        if cached and (now - cached["_at"]).total_seconds() < _CALIB_TTL_SECONDS:
            out = dict(cached["payload"])
            out["cached"] = True
            out["cache_age_seconds"] = round((now - cached["_at"]).total_seconds(), 3)
            return out
        started = time.monotonic()
        if started - _CALIB_LAST_COMPUTE < _CALIB_MIN_INTERVAL_SECONDS:
            raise HTTPException(
                429,
                detail={
                    "message": "标定计算过于频繁, 请稍后再试",
                    "retry_after_seconds": round(
                        _CALIB_MIN_INTERVAL_SECONDS - (started - _CALIB_LAST_COMPUTE),
                        1,
                    ),
                },
            )
        # 先在锁内占位, 再出锁计算: 并发请求不会在同一瞬间全部穿透去扫库。
        _CALIB_LAST_COMPUTE = started

    result = calibrate_factor_weights(
        market, days=int(days), horizon=int(horizon),
        min_samples=int(min_samples), db=db,
    )
    payload = dict(result)
    payload["cached"] = False
    payload["cache_age_seconds"] = 0.0
    payload["cache_ttl_seconds"] = _CALIB_TTL_SECONDS

    with _CALIB_LOCK:
        _CALIB_LAST_COMPUTE = time.monotonic()
        _CALIB_CACHE[key] = {"_at": now, "payload": payload}
    return payload


@router.post("/weights/{factor_code}/{market}")
def update_weight(
    factor_code: str,
    market: str,
    payload: FactorWeightUpdate,
    db: Session = Depends(get_db),
    actor: SettingsActor = Depends(require_settings_write),
):
    """手动覆盖某因子权重 / pin / 开关自动标定(权重变化写 manual 审计)。

    权限 = `manage_settings`(owner 或经白名单授权的成员), 由
    `require_settings_write` 实时从**写库**读角色判定; 审计身份用该依赖返回的
    服务端身份, 请求体里的任何身份字段都不采信。
    """
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
            actor=getattr(actor, "username", None) or None,
            via="api",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
