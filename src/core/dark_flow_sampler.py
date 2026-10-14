"""暗盘逐笔后台增量采集器(2026-10-14, 冷启动移出请求路径)。

## 审计定性(为何要这个模块)

`src/core/dark_flow.py` 的逐笔缓存此前只有两种路径: ① 90s TTL 内命中(零翻页);
② 过期/无缓存时, **在用户请求路径上**从游标翻页增量续拉 —— 首次请求(无缓存)
即整轮全量翻页(冷启动; 分钟接口 swings 场景实测 ~15s), 直接把用户请求挂在
网络翻页上。前端轮询期间靠 TTL 兜住, 但 TTL 一过又轮到某个倒霉的请求去翻页。

本模块仿 `board_flow_sampler` / `market_flow_sampler` 的成熟先例, 把"推进游标"
这件事**搬到后台**: 交易时段内每 60s 对标的池跑一次增量续拉(只拉缓存末游标之后
的页), 使请求端常态命中缓存(90s TTL) → **零翻页**。标的 = ① 热码(最近被请求过,
用户正在看) + ② **自选∪持仓池**(轮转切片; 用户还没点开先预热 —— 否则"当天第一次
点开"仍会冷启动在请求路径上)。

## 语义边界(不破既有契约)

- **TTL 不破**: `dark_flow._TICKS_TTL` 仍是 90s, 采集间隔(默认 60s)必须 < TTL,
  否则请求会看到过期缓存又回到请求期翻页 —— 见 `sampler_interval_s()` 与单测。
- **请求期兜底保留**: 缓存空/跨日/序号断裂时, 请求端照旧自己拉一次(冷启动一次),
  采集器只是"锦上添花", 不是请求路径的硬依赖。
- **不编造**: 上游零新页且在交易时段 → 明确标 `degraded`(返回旧快照 + 原因),
  不补 0、不假装成功。
- **不落库**: 逐笔快照的持久化仍走 `dark_flow` 既有的 `DiskCache`(按交易日分 key,
  进程重启自动从磁盘回血)。本模块不新增表, 避免与既有磁盘层重复存一份逐笔。

## 标的池

- **热码表**: 请求路径每次拉逐笔都会 `register_code(code)`(见
  `dark_flow._fetch_all_ticks_inner`), LRU 上限 300; 30 分钟无请求即退出采集
  (避免盘后长期空转)。用户正在看的票始终排在轮转池之前。
- **自选∪持仓池**: `quote_snapshots.collect_market_symbols()` 的 CN 部分(复用既有
  口径, 不另建一套), 池子缓存 300s, 上限 300; 按轮转切片刷新(每轮 `_BATCH_PER_RUN`
  只), 轮转步长 = 本轮实际取走的池子标的数(防跳号)。库不可用/`DARK_FLOW_UNIVERSE_DISABLED`
  置位 → 静默退回"只刷热码"。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime

logger = logging.getLogger(__name__)

#: 热码表上限(LRU): 超过即淘汰最久未请求的。
_MAX_TRACKED = 300
#: 热码活跃窗口(秒): 最近一次请求早于此的票不再被后台采集。
_HOT_TTL_S = 1800.0
#: 单轮最多刷新多少只(控制单轮耗时; 其余下一轮再拉)。
_BATCH_PER_RUN = 40
#: 默认采集间隔(秒): 必须 < dark_flow._TICKS_TTL(90s), 否则请求端会看到过期缓存。
_DEFAULT_INTERVAL_S = 60.0
#: 间隔环境变量(仅用于运维调参做减法; 大于 TTL 会被 `sampler_interval_s` 钳回默认)。
_INTERVAL_ENV = "DARK_FLOW_SAMPLER_INTERVAL_SEC"

# ── 采集标的池 ────────────────────────────────────────────────────────────
# ① 热码表: 最近被请求过的股票(请求路径自动登记) —— 用户当前正在看的票, 优先刷新;
# ② 自选∪持仓池: 用户大概率会点开的票 —— **这池子就是"免冷启动"的意义所在**, 否则
#    某只票当天第一次被点开时, 冷启动仍然发生在请求路径上(本改造要消除的正是这个)。
#    池子按轮转切片刷新(每轮 `_BATCH_PER_RUN` 只), 避免固定只刷前 N 只把后面的饿死。
_UNIVERSE_TTL_S = 300.0     # 池子缓存(DB 查询 5min 一次, 不必每轮打库)
_UNIVERSE_MAX = 300         # 池子上限(绑定单轮工作量上界)
_UNIVERSE_DISABLE_ENV = "DARK_FLOW_UNIVERSE_DISABLED"

_lock = threading.Lock()
_hot: "OrderedDict[str, float]" = OrderedDict()
_state: dict = {"rotation": 0, "universe": None}   # universe: (ts, [code])
_stats: dict = {
    "runs": 0,
    "last_run_ts": None,
    "last_refreshed": 0,
    "last_failed": 0,
    "last_degraded": [],
}


def register_code(code: str, now: float | None = None) -> None:
    """登记热码(请求/采集路径调用): 后台据此预热; LRU 上限 `_MAX_TRACKED`。"""
    if not code:
        return
    ts = time.time() if now is None else now
    with _lock:
        _hot[code] = ts
        _hot.move_to_end(code)
        while len(_hot) > _MAX_TRACKED:
            _hot.popitem(last=False)


def tracked_codes(limit: int | None = None, now: float | None = None) -> list[str]:
    """当前活跃热码(最近 `_HOT_TTL_S` 内有请求), 最近使用的在前。"""
    ts_now = time.time() if now is None else now
    with _lock:
        codes = [c for c, ts in reversed(list(_hot.items())) if ts_now - ts <= _HOT_TTL_S]
    return codes[:limit] if limit else codes


def clear_tracking() -> int:
    """清空热码表 + 标的池缓存/轮转(测试/运维), 返回清掉的条数。"""
    with _lock:
        n = len(_hot)
        _hot.clear()
        _state["universe"] = None
        _state["rotation"] = 0
    return n


def universe_codes(now: float | None = None) -> list[str]:
    """自选∪持仓池的腾讯代码(缓存 `_UNIVERSE_TTL_S`; 库不可用/被禁用 → [], 不抛)。

    只取 CN(暗盘逐笔是 A 股口径); 转换失败的标的跳过(不猜)。环境变量
    `DARK_FLOW_UNIVERSE_DISABLED` 置任何非空值即退回"只刷热码"。
    """
    if os.environ.get(_UNIVERSE_DISABLE_ENV):
        return []
    ts_now = time.time() if now is None else now
    with _lock:
        cached = _state.get("universe")
        if cached and ts_now - cached[0] < _UNIVERSE_TTL_S:
            return list(cached[1])
    codes: list[str] = []
    try:
        from marketdata.symbol import Symbol

        from src.core.dark_flow import _tencent_code
        from src.core.quote_snapshots import collect_market_symbols

        for sym in (collect_market_symbols().get("CN") or [])[:_UNIVERSE_MAX]:
            try:
                tcode = _tencent_code(Symbol.parse(sym, "CN"))
            except Exception:  # noqa: BLE001 — 非法标的跳过
                tcode = None
            if tcode and tcode not in codes:
                codes.append(tcode)
    except Exception as e:  # noqa: BLE001 — 池子拿不到就只刷热码
        logger.debug("自选/持仓池获取失败(本轮只刷热码): %s", e)
        codes = []
    with _lock:
        _state["universe"] = (ts_now, list(codes))
    return codes


def _select_targets(now: float | None = None) -> list[str]:
    """本轮要刷新的标的: 热码(最近请求, 优先) + 池子轮转切片, 去重后截到 `_BATCH_PER_RUN`。

    轮转按"本轮**实际取走**的池子标的数"推进 —— 按批大小推进会在热码占位时跳过中间的票
    (永远刷不到), 这是轮转最容易踩的坑。
    """
    targets = tracked_codes()[:_BATCH_PER_RUN]
    remaining = _BATCH_PER_RUN - len(targets)
    if remaining <= 0:
        return targets
    seen = set(targets)
    extra = [c for c in universe_codes(now) if c not in seen]
    if not extra:
        return targets
    n = len(extra)
    with _lock:
        start = int(_state.get("rotation") or 0) % n
        order = extra[start:] + extra[:start]
        take = order[:remaining]
        _state["rotation"] = (start + len(take)) % n
    return targets + take


def sampler_interval_s() -> float:
    """采集间隔(秒): 环境变量覆盖; 非法/非正/**≥TTL** 一律回落默认(保护 TTL 语义)。"""
    from src.core.dark_flow import _TICKS_TTL

    raw = os.environ.get(_INTERVAL_ENV)
    if raw:
        try:
            v = float(raw)
            if 0 < v < _TICKS_TTL:
                return v
            logger.warning(
                "%s=%r 不在 (0, %.0fs) 区间, 回落默认 %ss(防请求端读到过期缓存)",
                _INTERVAL_ENV, raw, _TICKS_TTL, _DEFAULT_INTERVAL_S,
            )
        except (TypeError, ValueError):
            logger.debug("%s 非法(%r), 用默认 %ss", _INTERVAL_ENV, raw, _DEFAULT_INTERVAL_S)
    return _DEFAULT_INTERVAL_S


def sampler_stats() -> dict:
    """采集器状态快照(tracked=热码数, universe_* = 自选∪持仓池)。"""
    with _lock:
        out = {**_stats, "tracked": len(_hot)}
        uni = _state.get("universe")
    out["universe"] = len(uni[1]) if uni else 0
    out["universe_age_sec"] = int(max(0, time.time() - uni[0])) if uni else None
    return out


def collect_once(now: datetime | None = None) -> dict:
    """跑一轮增量采集: 非交易时段跳过; 对标的池逐只增量续拉(失败静默, 永不抛)。

    标的 = 热码(最近请求过, 优先) + 自选∪持仓池(轮转切片)。时段守卫内建
    (`in_trading_window`: 交易日 ∩ 09:30-11:30/13:00-15:00), 故调度上挂 interval
    即可 —— 非交易时段每轮直接 skip, 不做网络请求。
    """
    from src.core.quote_snapshots import in_trading_window

    try:
        if not in_trading_window(now):
            return {"ok": True, "skipped": True, "reason": "非交易时段"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": f"交易时段判定不可用: {e}"}

    codes = _select_targets(time.time() if now is None else now.timestamp())
    from src.core.dark_flow import refresh_ticks

    refreshed = 0
    failed = 0
    degraded: list[str] = []
    for code in codes:
        try:
            out = refresh_ticks(code)
        except Exception as e:  # noqa: BLE001 — 单只失败不拖垮整轮
            failed += 1
            logger.debug("逐笔采集[%s]异常: %s", code, e)
            continue
        if out.get("ok"):
            refreshed += 1
            if out.get("degraded"):
                degraded.append(code)
        else:
            failed += 1
            logger.debug("逐笔采集[%s]失败: %s", code, out.get("error"))

    result = {
        "ok": True,
        "skipped": False,
        "tracked": len(tracked_codes()),
        "targets": len(codes),
        "refreshed": refreshed,
        "failed": failed,
        "degraded": degraded,
    }
    with _lock:
        _stats["runs"] += 1
        _stats["last_run_ts"] = time.time()
        _stats["last_refreshed"] = refreshed
        _stats["last_failed"] = failed
        _stats["last_degraded"] = degraded
    return result
