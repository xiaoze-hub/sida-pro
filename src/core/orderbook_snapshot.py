# -*- coding: utf-8 -*-
"""十档盘口快照后台采集 + 请求端读快照(2026-10-14, 盘口移出用户请求路径)。

审计定性
--------
`/api/orderbook-ob`(`src/web/api/orderbook.py`)与 klines summary 的
`_build_orderbook`(`src/web/api/klines.py`)在**请求期**直接打 thsdk 十档
(`order_book_bid/ask`): orderbook-ob 硬超时 12s, _build_orderbook 硬超时 8s; 上游单次
实测可卡 30s。卡顿/超时**直接压在用户请求上**(反代读超时 → 502 / 首屏慢)。

本模块(仿 `board_flow_sampler` / `l2_fund_snapshot` 成熟模式)
--------------------------------------------------------------
- 采集: 交易时段每 15~30s(`ORDERBOOK_SNAPSHOT_INTERVAL_SEC`, 默认 20)对**活跃票**
  (自选 ∪ 启用账户持仓 ∪ 当日异动, 带 universe 上限)逐个拉十档快照, 追加进该票
  **滚动窗**(默认 5 帧) 落 `orderbook_snapshots`(一行一票 upsert)。节流 + 单轮时间
  预算 + 逐票小睡, 防 thsdk 过载(thsdk 单并发/熔断见 `src/core/thsdk_breaker.py`)。
- 读: `read_snapshot()` 返回最新滚动窗 + **`snapshot_age_sec` 显式陈旧度**;
  `fresh_snapshot()` 仅当年龄 <= `ORDERBOOK_SNAPSHOT_STALE_SEC`(默认 60)时返回
  —— 调用方据此『够新读快照、超龄穿透实时、无快照走实时/显式降级』。
- 缺数据显式(不编造): 空盘口(无真实买卖价)不落库; 无行/表未迁移/读库失败 → None;
  采集取数失败只记日志不抛。

不碰请求期 thsdk 调用的既有语义(available:false 显式降级), 只是在它前面加一层快照读。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "orderbook_snapshots"

# 滚动窗帧数: 与旧 `/api/orderbook-ob` 请求期采集的 5 帧对齐, 供 OB 序列/演变/幽灵单算法。
_WINDOW = 5
# 每帧保留的档位数(十档): thsdk order_book 给 20 档, 只留前 10 档(OB 计算口径即前 10 档)。
_LEVELS = 10

# 采集节流(env 可配, 非法/越界回落默认)
_INTERVAL_ENV = "ORDERBOOK_SNAPSHOT_INTERVAL_SEC"
_INTERVAL_DEFAULT = 20        # 15~30s 区间取中
_INTERVAL_MIN, _INTERVAL_MAX = 5, 600
# 读侧陈旧阈值(秒): 年龄超过它 → 读路径穿透实时(env 可配)
_STALE_ENV = "ORDERBOOK_SNAPSHOT_STALE_SEC"
_STALE_DEFAULT = 60
# universe 上限: 单轮最多采多少只(thsdk 单并发, 防过载)
_MAX_SYMBOLS_ENV = "ORDERBOOK_SNAPSHOT_MAX_SYMBOLS"
_MAX_SYMBOLS_DEFAULT = 40
# 单轮时间预算(秒): 超预算立即结束本轮, 不拖到下一轮(周期 15~30s)
_CYCLE_BUDGET_S = 12.0
# 单票取数十档的硬超时(秒): 上游挂住时放弃该票, 不阻塞整轮
_PER_SYMBOL_TIMEOUT_S = 6.0
# 逐票小睡(秒): 给 thsdk 客户端喘息, 防瞬时过载
_INTER_SYMBOL_SLEEP_S = 0.05

_cycle_lock = threading.Lock()
_last_cycle_start: float | None = None


# ── env 配置解析 ─────────────────────────────────────────────────────────────

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            v = int(float(raw))
            if lo <= v <= hi:
                return v
        except (TypeError, ValueError):
            logger.debug("%s 非法(%r), 用默认 %s", name, raw, default)
    return default


def snapshot_interval_sec() -> int:
    """采集周期(秒), 默认 20(15~30s 区间)。"""
    return _env_int(_INTERVAL_ENV, _INTERVAL_DEFAULT, _INTERVAL_MIN, _INTERVAL_MAX)


def stale_threshold_sec() -> int:
    """读侧陈旧阈值(秒), 默认 60; 年龄超过它 → 读路径穿透实时。"""
    return _env_int(_STALE_ENV, _STALE_DEFAULT, 1, 86400)


def max_symbols() -> int:
    """单轮 universe 上限, 默认 40。"""
    return _env_int(_MAX_SYMBOLS_ENV, _MAX_SYMBOLS_DEFAULT, 1, 5000)


def _ths_key(symbol: str) -> str:
    """6 位代码 / THS 代码 → THS 代码(入库键)。无法识别时原样返回(不猜)。"""
    try:
        from src.core import orderbook_engine as obe

        code = obe.to_ths_code(symbol)
        if code:
            return code
    except Exception as e:  # noqa: BLE001
        logger.debug("THS 代码归一失败 %s: %s", symbol, e)
    return (symbol or "").strip().upper()


def _norm6(symbol) -> str | None:
    """只接受 6 位 A 股代码(其它市场/格式忽略, 不猜)。"""
    s = str(symbol or "").strip()
    return s if (len(s) == 6 and s.isdigit()) else None


# ── 帧构建(与 orderbook_engine.fetch_snapshot 同构, 但可 JSON 往返) ────────────

def trim_frame(snap: dict | None) -> dict | None:
    """实时快照 → 落库帧(裁剪到前十档, 字段可 JSON 往返)。

    无任一真实买卖价 → None(空盘口不落, 0 不冒充)。
    """
    if not snap:
        return None
    bid_levels = list(snap.get("bid_levels") or [])[: _LEVELS]
    ask_levels = list(snap.get("ask_levels") or [])[: _LEVELS]
    if not bid_levels and not ask_levels:
        return None

    def _lv(lv: dict) -> dict | None:
        try:
            price = float(lv.get("price"))
        except (TypeError, ValueError):
            return None
        hands: list[int] = []
        for h in lv.get("ordersque") or []:
            try:
                hands.append(int(h))
            except (TypeError, ValueError):
                continue
        return {
            "orderlevel": int(lv.get("orderlevel") or 0),
            "price": price,
            "ordersque": hands,
        }

    bl = [x for x in (_lv(lv) for lv in bid_levels) if x]
    al = [x for x in (_lv(lv) for lv in ask_levels) if x]
    if not bl and not al:
        return None
    try:
        ts = float(snap.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    qs = snap.get("queue_shares")
    frame = {
        "ts": ts,
        "dt": snap.get("dt"),
        "bid_levels": bl,
        "ask_levels": al,
        "queue_shares": list(qs) if qs else None,
        "source": snap.get("source") or "thsdk",
    }
    return frame


def rebuild_snapshot(frame: dict | None) -> dict | None:
    """落库帧 → 与 `fetch_snapshot()` 同构的快照 dict(重建 float-key bid/ask map)。

    JSON 往返会把 dict 键变字符串, 而 `order_book_evolution`/`order_book_imbalance`/
    `ghost_order` 依赖 float 价格键做 `.get(price)` 与 `min/max(keys)` —— 必须在此重建,
    否则价格键比较退化成字符串比较(静默错)。无可用档位 → None(不编造)。
    """
    if not frame:
        return None

    def _levels(ls) -> list[dict]:
        out: list[dict] = []
        for lv in ls or []:
            try:
                price = float(lv.get("price"))
            except (TypeError, ValueError):
                continue
            hands = []
            for h in lv.get("ordersque") or []:
                try:
                    hands.append(int(h))
                except (TypeError, ValueError):
                    continue
            out.append({
                "orderlevel": int(lv.get("orderlevel") or 0),
                "price": price,
                "ordersque": hands,
            })
        return out

    bl = _levels(frame.get("bid_levels"))
    al = _levels(frame.get("ask_levels"))
    if not bl and not al:
        return None
    bid = {lv["price"]: int(sum(lv["ordersque"])) for lv in bl}
    ask = {lv["price"]: int(sum(lv["ordersque"])) for lv in al}
    qs = frame.get("queue_shares")
    try:
        ts = float(frame.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return {
        "ts": ts,
        "dt": frame.get("dt"),
        "bid_levels": bl,
        "ask_levels": al,
        "bid": bid,
        "ask": ask,
        "queue_shares": list(qs) if qs else None,
        "source": frame.get("source") or "thsdk",
    }


# ── 写 ───────────────────────────────────────────────────────────────────────

def append_frame(symbol: str, frame: dict, *, now: float | None = None) -> bool:
    """把一帧追加进该票滚动窗(旧→新, 截断到 `_WINDOW`)并 upsert。失败静默返回 False。"""
    if not frame:
        return False
    key = _ths_key(symbol)
    if not key:
        return False
    try:
        from sqlalchemy import text

        from src.db.dialect import upsert_sql
        from src.db.session import engine
    except Exception as e:  # noqa: BLE001
        logger.debug("盘口快照写库依赖不可用: %s", e)
        return False

    cur = read_snapshot(key)
    frames = list(cur["frames"]) if cur else []
    frames.append(frame)
    frames = frames[-_WINDOW:]
    cols = ["symbol", "market", "collected_at", "latest_ts", "frames", "payload", "source"]
    updates = ["collected_at", "latest_ts", "frames", "payload", "source"]
    try:
        stmt = text(upsert_sql(_TABLE, cols, ["symbol"], updates))
        with engine.begin() as conn:
            conn.execute(stmt, {
                "symbol": key,
                "market": "CN",
                "collected_at": float(time.time() if now is None else now),
                "latest_ts": frame.get("dt"),
                "frames": len(frames),
                "payload": json.dumps(frames, ensure_ascii=False, default=str),
                "source": frame.get("source") or "thsdk",
            })
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("盘口快照写库失败(静默) %s: %s", key, e)
        return False


# ── 读 ───────────────────────────────────────────────────────────────────────

def read_snapshot(symbol: str) -> dict | None:
    """读最新滚动窗(含陈旧度)。无行/表未迁移/读库失败/payload 非法 → None(不猜)。"""
    key = _ths_key(symbol)
    if not key:
        return None
    try:
        from sqlalchemy import text

        from src.db.session import engine

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT collected_at, latest_ts, frames, payload, source "
                    f"FROM {_TABLE} WHERE symbol = :s LIMIT 1"
                ),
                {"s": key},
            ).fetchone()
    except Exception as e:  # noqa: BLE001  表未迁移/库不可达 → 无快照
        logger.debug("盘口快照读库失败 %s: %s", key, e)
        return None
    if row is None:
        return None
    raw = row[3]
    try:
        frames = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(frames, list) or not frames:
        return None
    try:
        collected = float(row[0]) if row[0] is not None else None
    except (TypeError, ValueError):
        collected = None
    age = None if collected is None else int(max(0, round(time.time() - collected)))
    return {
        "symbol": key,
        "frames": frames,
        "frame_count": len(frames),
        "latest_ts": row[1],
        "collected_at": collected,
        "snapshot_age_sec": age,
        "source": row[4] or "thsdk",
    }


def fresh_snapshot(symbol: str) -> dict | None:
    """最新滚动窗(仅当年龄 <= 陈旧阈值)。过旧/算不出年龄 → None(调用方穿透实时)。"""
    snap = read_snapshot(symbol)
    if snap is None:
        return None
    age = snap.get("snapshot_age_sec")
    if age is None or age > stale_threshold_sec():
        return None
    return snap


# ── universe ─────────────────────────────────────────────────────────────────

def _add_symbol(out: list[str], seen: set[str], sym) -> None:
    code = _norm6(sym)
    if code and code not in seen:
        seen.add(code)
        out.append(code)


def active_symbols(limit: int | None = None) -> list[str]:
    """活跃票(6 位代码): 自选 ∪ 启用账户持仓 ∪ 当日异动(涨停/封板样本), 去重 + 上限。

    - 自选∪持仓: 复用 `quote_snapshots.collect_market_symbols`(公共市场数据, 与快照落库同源);
    - 当日异动: `seal_quality_samples` 当日样本(涨停/封板, 当日最强异动), 表缺失/读失败则忽略;
    - 任一来源失败只忽略该来源, 不抛。
    """
    limit = limit or max_symbols()
    out: list[str] = []
    seen: set[str] = set()
    try:
        from src.core.quote_snapshots import collect_market_symbols

        for _mkt, syms in (collect_market_symbols() or {}).items():
            for s in syms or []:
                _add_symbol(out, seen, s)
    except Exception as e:  # noqa: BLE001
        logger.debug("盘口快照 universe(自选∪持仓)收集失败: %s", e)
    try:
        from sqlalchemy import text

        from src.db.session import engine

        today = datetime.now(_CST).strftime("%Y-%m-%d")
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT symbol FROM seal_quality_samples "
                    "WHERE ts >= :d LIMIT :n"
                ),
                {"d": today, "n": limit * 2},
            ).fetchall()
        for r in rows:
            _add_symbol(out, seen, r[0])
    except Exception as e:  # noqa: BLE001  表缺失/读失败 → 忽略该来源
        logger.debug("盘口快照 universe(当日异动)收集失败: %s", e)
    return out[:limit]


# ── 采集 ─────────────────────────────────────────────────────────────────────

def collect_symbol(symbol: str, *, now: float | None = None) -> dict:
    """拉一只十档快照并追加进滚动窗。永不抛。"""
    key = _ths_key(symbol)
    if not key:
        return {"symbol": symbol, "written": False, "reason": "无有效代码"}
    try:
        from src.core import orderbook_engine as obe
    except Exception as e:  # noqa: BLE001
        return {"symbol": key, "written": False, "reason": f"引擎不可用: {e}"}
    if obe.THS is None:
        return {"symbol": key, "written": False, "reason": "thsdk 未安装"}
    try:
        from src.core.thsdk_breaker import call_with_hard_timeout

        snap = call_with_hard_timeout(
            lambda: obe.fetch_snapshot(key),
            default=None,
            timeout_s=_PER_SYMBOL_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001  护栏自身异常也不外泄
        logger.debug("盘口快照取数护栏异常 %s: %s", key, e)
        snap = None
    if snap is None:
        return {"symbol": key, "written": False, "reason": "取数失败/超时"}
    frame = trim_frame(snap)
    if frame is None:
        return {"symbol": key, "written": False, "reason": "空盘口(无真实买卖价, 不落)"}
    ok = append_frame(key, frame, now=now)
    return {"symbol": key, "written": ok, "reason": None if ok else "写库失败"}


def _throttled() -> bool:
    with _cycle_lock:
        last = _last_cycle_start
    if last is None:
        return False
    return (time.monotonic() - last) < snapshot_interval_sec()


def collect_once(now: datetime | None = None, *, force: bool = False) -> dict:
    """采集一轮(scheduler 入口): 交易时段守卫 → 节流 → 活跃票逐个拉快照(有界预算)。永不抛。

    - 非交易时段: 跳过(不打源);
    - 节流窗口内(距上轮 < `snapshot_interval_sec`, 且非 force): 跳过;
    - `force=True`: 绕过节流(测试/手动)。
    """
    global _last_cycle_start
    try:
        now = now or datetime.now(_CST)
        from src.core.quote_snapshots import in_trading_window

        if not in_trading_window(now):
            return {"ok": True, "skipped": True, "reason": "非交易时段"}
        if not force and _throttled():
            return {"ok": True, "skipped": True, "reason": "节流窗口内"}
        symbols = active_symbols()
        if not symbols:
            return {"ok": True, "skipped": True, "reason": "无活跃标的"}
        with _cycle_lock:
            _last_cycle_start = time.monotonic()
        deadline = time.monotonic() + _CYCLE_BUDGET_S
        written = failed = skipped = 0
        for i, sym in enumerate(symbols):
            if time.monotonic() > deadline:
                skipped = len(symbols) - i
                break
            r = collect_symbol(sym)
            if r.get("written"):
                written += 1
            elif r.get("reason") in ("取数失败/超时", "写库失败", "引擎不可用", "thsdk 未安装"):
                failed += 1
            else:
                skipped += 1
            if _INTER_SYMBOL_SLEEP_S > 0:
                time.sleep(_INTER_SYMBOL_SLEEP_S)
        return {
            "ok": True,
            "skipped": False,
            "universe": len(symbols),
            "written": written,
            "failed": failed,
            "skipped_symbols": skipped,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("盘口快照采集异常: %s", e)
        return {"ok": False, "skipped": False, "error": str(e)}


def _reset_state_for_tests() -> None:
    """测试用: 清空进程内节流状态。"""
    global _last_cycle_start
    with _cycle_lock:
        _last_cycle_start = None
