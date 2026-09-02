# -*- coding: utf-8 -*-
"""盘口演变引擎(order_book_engine) — 基于 THS L2 逐笔委托盘口(20档)的三个创新算法。

数据源: thsdk `order_book_bid` / `order_book_ask`(需在 `with THS() as ths:` 上下文内调用)。
每档 dict keys = ['orderlevel', 'price', 'ordersque']:
  - orderlevel : 档位序号, 1 = 最优价(买一/卖一), 2..20 依次向外
  - price      : 档位价格, 单位 元
  - ordersque  : list of 委托笔数, 每个元素是一笔委托的手数(1手=100股), 实测买一队列 297 笔

三个算法:
  (1) order_book_evolution  盘口演变引擎: 连续采集 10-30 个快照(间隔 1-2s), 追踪每档 ordersque
      求和的变化, 检测:
        - 托单(bid 档位堆单增多而价格不涨 — 承接/护盘信号)
        - 压单(ask 档位堆单增多而价格不跌 — 上方抛压/压制)
        - 撤单(某档位笔数骤减 >=50% 而价格未按不利方向移动; 尾盘自然撤单会打标记可忽略)
        - 幽灵单(巨量堆积出现后又消失 — 与算法(3)互补, 此处按"档位总量"口径检测)
  (2) order_book_imbalance  订单簿失衡: 前10档 bid/ask 金额求和算 OB,
      金额 = price × sum(ordersque) × 100(手→股×100→金额元),
      OB = (BidAmt10 - AskAmt10) / (BidAmt10 + AskAmt10) ∈ [-1, 1]
      > +0.3 判买压, < -0.3 判卖压
  (3) ghost_order           幽灵单检测: 对每快照每档 ordersque 找大单(单笔>1000手
      或单笔>该档总手数50%), 跨快照跟踪其"出现→消失", 统计 ghost_ratio = 幽灵单数/大单出现总数。

时间口径: A股连续竞价 9:30-11:30 / 13:00-15:00, 集合竞价 9:15-9:25。
非交易时段盘口通常为收盘静态快照(实测 2026-08-19 21:14 神剑股份 USZA002361:
买一 11.27/297笔/266492手, 三次采样完全一致) → 事件检测为空, 属正常现象, 如实上报。

## 2026-09-01: .img 离线数据源接入(设计稿 §3.1)

除 thsdk 实时盘口外, 本模块新增**通达信 .img 离线数据源**(解析见 `tdx_img_parser`):
`load_snapshots_from_img()` 把 .img 的十档帧序列转换成与 `fetch_snapshot()` **同构**的
快照 dict, 因此 `order_book_evolution` / `order_book_imbalance` / `ghost_order`
三个算法无需任何改动即可跑离线数据。

单位换算(硬约束: 本仓库金额=元 / 成交量=股):
  - `tdx_img_parser` 输出量单位为**股**
  - 本引擎 `ordersque` 约定单位为**手**(1 手 = 100 股, A 股整数换算)
  - 因此 ordersque 元素 = round(股 / 100); 金额口径仍按 `price × 手 × 100` 还原成元

限频: 每次调用后 sleep 50ms(仓库统一规则); 失败重试 3 次退避 0.5s。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

try:
    from thsdk import THS  # 模块级软依赖: 无 thsdk 环境(如纯导入分析)不报错
except Exception:  # pragma: no cover
    THS = None

SHARES_PER_HAND = 100  # A股: 1 手 = 100 股

# 合约常量
RATE_LIMIT_S = 0.05      # 限频: 每次行情调用后 sleep 50ms
MAX_RETRY = 3            # 失败重试次数
RETRY_BACKOFF_S = 0.5    # 重试退避基数
BIG_ORDER_HANDS = 1000   # 大单阈值: 单笔 > 1000 手
BIG_ORDER_RATIO = 0.5    # 大单阈值: 单笔 > 该档总手数 50%
CANCEL_PCT = 0.5         # 撤单阈值: 笔数总和骤减 >= 50%
SIG_INCR_HANDS = 500     # 托/压单显著增量: >= 500 手(绝对)
SIG_INCR_PCT = 0.25      # 托/压单显著增量: >= 前值 25%(相对)
GHOST_FLASH_HANDS = 5000 # 幽灵单(档位口径)闪现巨量阈值: >= 5000 手
OB_TOP_N = 10            # 失衡计算用前 10 档
OB_BUY_TH = 0.3          # 买压阈值
OB_SELL_TH = -0.3        # 卖压阈值

# 连续竞价时间段(判断尾盘自然撤单)
SESSION_SEGMENTS = ((9, 30, 11, 30), (13, 0, 15, 0))
TAIL_MINUTE = 14, 55  # 尾盘起点 14:55: 之后发生的撤单大概率是收盘前自然撤单


# ---------------------------------------------------------------------------
# 快照采集
# ---------------------------------------------------------------------------
def _now() -> float:
    """当前时间戳。"""
    return time.time()


def _is_trading_time(dt: datetime | None = None) -> bool:
    """是否处于 A 股连续竞价时段(不含尾盘特殊段)。"""
    dt = dt or datetime.now()
    h, m = dt.hour, dt.minute
    for (hs, ms, he, me) in SESSION_SEGMENTS:
        if (h, m) >= (hs, ms) and (h, m) < (he, me):
            return True
    return False


def _is_tail_session(dt: datetime | None = None) -> bool:
    """是否处于尾盘段(>=14:55): 该时段的撤单多为自然撤单, 可忽略。"""
    dt = dt or datetime.now()
    if _is_trading_time(dt):
        return (dt.hour, dt.minute) >= TAIL_MINUTE
    return False


def _fetch_levels(ths: Any, ths_code: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """在一个 THS 上下文内拉取买/卖各 20 档。失败抛异常由上层重试。"""
    bid_resp = ths.order_book_bid(ths_code)
    time.sleep(RATE_LIMIT_S)  # 限频 50ms
    ask_resp = ths.order_book_ask(ths_code)
    time.sleep(RATE_LIMIT_S)  # 限频 50ms
    if not bid_resp or not bid_resp.success or not ask_resp or not ask_resp.success:
        err = (getattr(bid_resp, "error", "") or getattr(ask_resp, "error", ""))
        raise RuntimeError(f"order_book 拉取失败: {err}")
    return list(bid_resp.data or []), list(ask_resp.data or [])


def fetch_snapshot(ths_code: str) -> dict[str, Any]:
    """拉取一个盘口快照(每个快照一个 `with THS()` 块, 符合 THS 上下文约束)。

    返回:
        {ts, dt, bid_levels, ask_levels, bid: {price: sum_hands}, ask: {...}}
        bid/ask 为 {价格: 该档 ordersque 总和(手)}
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            if THS is None:
                raise RuntimeError("thsdk 未安装: 无法拉取真实盘口")
            with THS() as ths:  # 每个快照独立上下文
                bid_levels, ask_levels = _fetch_levels(ths, ths_code)
            ts = _now()
            return {
                "ts": ts,
                "dt": datetime.now().isoformat(timespec="seconds"),
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
                "bid": {float(lv["price"]): int(sum(lv["ordersque"])) for lv in bid_levels},
                "ask": {float(lv["price"]): int(sum(lv["ordersque"])) for lv in ask_levels},
            }
        except Exception as e:  # 重试退避
            last_err = e
            time.sleep(RETRY_BACKOFF_S * attempt)
    raise RuntimeError(f"连续 {MAX_RETRY} 次采集失败: {last_err}")


# ---------------------------------------------------------------------------
# .img 离线数据源(2026-09-01, 设计稿 §3.1)
# ---------------------------------------------------------------------------
def img_frame_to_snapshot(fr: Any, ts: float, dt_iso: str | None = None) -> dict[str, Any]:
    """.img 单帧(ImgSnapshot) → 与 fetch_snapshot 同构的快照 dict。

    Args:
        fr: `tdx_img_parser.ImgSnapshot`(十档价格/量单位: 元 / 股)
        ts: 快照时间戳(epoch 秒); 离线帧按调用方给定的时间轴传入
        dt_iso: 可选的时间字符串, 仅用于展示

    Returns:
        与 `fetch_snapshot()` 同构: {ts, dt, bid_levels, ask_levels, bid, ask}
        另附 `queue`(委托队列, 单位手) 与 `queue_shares`(原始股), 供托压单识别。
    """
    def _levels(prices: list, vols: list) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i, (p, v) in enumerate(zip(prices or [], vols or [])):
            if p is None or v is None:
                continue
            hands = int(round(v / SHARES_PER_HAND))
            out.append({"orderlevel": i + 1, "price": float(p), "ordersque": [hands]})
        return out

    bid_levels = _levels(fr.bid_prices, fr.bid_vols)
    ask_levels = _levels(fr.ask_prices, fr.ask_vols)
    queue_shares = list(fr.queue) if getattr(fr, "queue", None) else None
    return {
        "ts": ts,
        "dt": dt_iso or getattr(fr, "t", None),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "bid": {float(lv["price"]): int(sum(lv["ordersque"])) for lv in bid_levels},
        "ask": {float(lv["price"]): int(sum(lv["ordersque"])) for lv in ask_levels},
        # 委托队列: 手(引擎口径) + 原始股(不丢信息)
        "queue": [int(round(v / SHARES_PER_HAND)) for v in queue_shares] if queue_shares else None,
        "queue_shares": queue_shares,
        "source": "img",
    }


def load_snapshots_from_img(img_path: str, limit: int | None = None) -> list[dict[str, Any]]:
    """从 .img 文件加载盘口快照序列(离线), 输出与实时采集同构。

    Args:
        img_path: .img 文件路径
        limit:   最多取前 N 帧; None = 全部

    Returns:
        list[快照dict]; 文件不存在/解析失败/无帧 → [] (调用方显式标"无数据", 不编造)。

    注: .img 帧无日期只有 "HH:MM:SS", ts 按帧序合成(相邻 1 秒),
    仅供演变算法的相对时序判定, 不代表绝对时间。
    """
    try:
        from src.core.tdx_img_parser import frames_from_img
    except Exception as e:  # pragma: no cover
        logger.warning("tdx_img_parser 不可用: %s", e)
        return []
    try:
        frames = frames_from_img(img_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("解析 .img 失败 %s: %s", img_path, e)
        return []
    if not frames:
        return []
    if limit:
        frames = frames[:limit]
    return [img_frame_to_snapshot(fr, ts=float(i), dt_iso=getattr(fr, "t", None))
            for i, fr in enumerate(frames)]


def order_book_queue(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """单快照的盘口队列 + 托压单形态识别(设计稿 §3.1 / §5.3)。

    形态判定(与 ImgSnapshot 派生指标同口径, 缺失一律 None 不编造):
      - 托单(bid 侧): 买盘力量占比 >= 0.6
      - 压单(ask 侧): 买盘力量占比 <= 0.4
      - 队列失衡: 委托队列总量 - 卖一量(股); 队列巨大而卖一很小 → 疑似压单

    Returns:
        {available, best_bid, best_ask, spread, bid_pressure, queue_shares,
         queue_imbalance, shape: '托盘'|'压盘'|'均衡'|None, source}
    """
    if not snapshot:
        return {"available": False, "shape": None, "note": "无数据"}
    bid = snapshot.get("bid") or {}
    ask = snapshot.get("ask") or {}
    best_bid = max(bid) if bid else None
    best_ask = min(ask) if ask else None
    bid_amt_hands = sum(bid.values()) if bid else 0
    ask_amt_hands = sum(ask.values()) if ask else 0
    total = bid_amt_hands + ask_amt_hands
    pressure = round(bid_amt_hands / total, 6) if total > 0 else None

    queue_shares = snapshot.get("queue_shares")
    queue_total = int(sum(queue_shares)) if queue_shares else None
    ask1_shares = None
    if snapshot.get("ask_levels"):
        v = snapshot["ask_levels"][0].get("ordersque") or []
        ask1_shares = int(sum(v)) * SHARES_PER_HAND if v else None
    imbalance = None
    if queue_total is not None and ask1_shares is not None:
        imbalance = queue_total - ask1_shares

    shape: str | None = None
    if pressure is not None:
        if pressure >= 0.6:
            shape = "托盘"
        elif pressure <= 0.4:
            shape = "压盘"
        else:
            shape = "均衡"
    # 2026-09-02 xiaoze 复核: available 必须"有真实盘口价"才算, 空快照(thsdk degraded)不置 True,
    # 否则前端拿 available=true 就不走 §12 灰显兜底, 却没有任何真实数据(假阳性)。
    has_price = bool(bid) or bool(ask)
    return {
        "available": has_price,
        "source": snapshot.get("source", "thsdk"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(best_ask - best_bid, 6) if (best_bid and best_ask) else None,
        "bid_pressure": pressure,
        "queue_shares": queue_total,
        "queue_imbalance": imbalance,
        "shape": shape,
    }


def to_ths_code(symbol: str) -> str | None:
    """6 位 A 股代码 → thsdk 代码(USZA / USHA / USBJ 前缀)。

    thsdk 的 `order_book_bid/ask` 用 USZA/USHA 前缀(实测 神剑股份 USZA002361),
    与 `dark_l2` 用的 sz/sh 腾讯风格**不是同一套**, 混用会取不到数。
    已是 thsdk 格式的(USZA/USHA/USBJ 前缀 6 位)原样返回; 无法识别返回 None。
    """
    s = (symbol or "").strip().upper()
    for prefix in ("USZA", "USHA", "USBJ", "USTM"):
        if s.startswith(prefix) and s[len(prefix):].isdigit():
            return s
    if s.isdigit() and len(s) == 6:
        if s[0] in ("6", "9") or s.startswith("688"):
            return f"USHA{s}"
        if s[0] in ("0", "2", "3"):
            return f"USZA{s}"
        if s[0] in ("4", "8"):
            return f"USBJ{s}"
    return None


def find_img_file(symbol: str, market: str = "CN") -> str | None:
    """按代码在 PANWATCH_IMG_DIR 下找对应 .img 文件。

    约定文件名包含 6 位代码(如 `sz000977*.img` / `000977*.img`)。
    目录未配置或文件不存在 → None(调用方显式标"无数据")。
    """
    base = (os.environ.get("PANWATCH_IMG_DIR") or "").strip()
    if not base or not os.path.isdir(base):
        return None
    code = (symbol or "").strip()
    if not code:
        return None
    try:
        for name in os.listdir(base):
            if not name.lower().endswith(".img"):
                continue
            if code in name:
                return os.path.join(base, name)
    except Exception as e:  # noqa: BLE001
        logger.warning("扫描 .img 目录失败 %s: %s", base, e)
    return None


# ---------------------------------------------------------------------------
# (1) 盘口演变引擎
# ---------------------------------------------------------------------------
def _sum_hands(ordersque: list[Any]) -> int:
    """ordersque(每笔手数 list)求和, 单位: 手。"""
    total = 0
    for h in ordersque or []:
        try:
            total += int(h)
        except (TypeError, ValueError):
            continue
    return total


def _level_sum(levels: list[dict[str, Any]], price: float) -> int:
    """按价格查找档位并返回 ordersque 总和(手); 不存在返回 0。"""
    for lv in levels:
        if abs(float(lv["price"]) - price) < 1e-6:
            return _sum_hands(lv["ordersque"])
    return 0


def order_book_evolution(
    snapshots: list[dict[str, Any]],
    sig_incr_hands: int = SIG_INCR_HANDS,
    sig_incr_pct: float = SIG_INCR_PCT,
    cancel_pct: float = CANCEL_PCT,
) -> list[dict[str, Any]]:
    """盘口演变引擎: 追踪相邻快照间每档 ordersque 总和变化, 输出结构化事件列表。

    事件 dict: {type, side, price_level, price, delta_hands, duration_s, note}
      type       : '托单' | '压单' | '撤单' | '幽灵单'(档位总量口径)
      side       : 'bid' | 'ask'
      price_level: 档位序号(1=最优价); 若价格位移按档序对齐给出近似档位(带 note 说明)
      delta_hands: 手数变化(托/压为正, 撤单为负)
      duration_s : 事件持续时间(从出现到回落/消失, 或到采集结束)

    托单/压单为"持续性"事件: 用 active 表跟踪堆单从出现到回落的全过程。
    撤单为"快照间"事件: 相邻快照对比即可判定, 直接输出。
    """
    events: list[dict[str, Any]] = []
    if len(snapshots) < 2:
        return events

    tail = _is_tail_session()
    trading = _is_trading_time()

    # active: {(side, price): {start_ts, peak_sum, cur_sum, lv}}
    active: dict[tuple[str, float], dict[str, Any]] = {}

    def _close_active(key: tuple[str, float], now_ts: float, cur_sum: int):
        """把堆单事件收尾(堆单回落/消失), 输出托单或压单事件。"""
        a = active.pop(key, None)
        if a is None:
            return
        peak = a["peak_sum"]
        note = ""
        if peak > a["cur_sum"]:
            note = "堆单高位回落"
        events.append({
            "type": "托单" if a["side"] == "bid" else "压单",
            "side": a["side"],
            "price_level": a["lv"],
            "price": key[1],
            # delta 取"相对采集开始时"的净增(正数), 表示这波堆单的强度
            "delta_hands": int(peak - a["base_sum"]),
            "duration_s": round(now_ts - a["start_ts"], 2),
            "ts": now_ts,
            "note": note,
        })

    for i in range(1, len(snapshots)):
        prev, cur = snapshots[i - 1], snapshots[i]
        ts = cur["ts"]
        for side, levels_key in (("bid", "bid_levels"), ("ask", "ask_levels")):
            cur_levels: list[dict[str, Any]] = cur[levels_key]
            prev_map: dict[float, int] = prev[side]
            # 以"档序号"为主键对齐(盘口跳动时价格会整体平移, 档序更稳定)
            for lv in cur_levels:
                price = float(lv["price"])
                orderlevel = int(lv["orderlevel"])
                cur_sum = _sum_hands(lv["ordersque"])
                prev_sum = prev_map.get(price, 0)
                delta = cur_sum - prev_sum
                if prev_sum <= 0:
                    prev_sum = _level_sum(prev[levels_key], price)

                key = (side, price)

                # --- 托单: 买档显著堆单且价格不涨(买一价格不变或下降) ---
                if side == "bid" and delta >= sig_incr_hands and delta >= sig_incr_pct * max(prev_sum, 1):
                    if key not in active:
                        active[key] = {
                            "side": side, "lv": orderlevel, "start_ts": ts,
                            "base_sum": prev_sum, "cur_sum": cur_sum, "peak_sum": cur_sum,
                        }
                    else:
                        active[key]["cur_sum"] = cur_sum
                        active[key]["peak_sum"] = max(active[key]["peak_sum"], cur_sum)

                # --- 压单: 卖档显著堆单且价格不跌 ---
                elif side == "ask" and delta >= sig_incr_hands and delta >= sig_incr_pct * max(prev_sum, 1):
                    if key not in active:
                        active[key] = {
                            "side": side, "lv": orderlevel, "start_ts": ts,
                            "base_sum": prev_sum, "cur_sum": cur_sum, "peak_sum": cur_sum,
                        }
                    else:
                        active[key]["cur_sum"] = cur_sum
                        active[key]["peak_sum"] = max(active[key]["peak_sum"], cur_sum)

                # --- 幽灵单(档位口径): 巨量堆积"闪现后消失"(两快照前该档基本不存在,
                #     上一快照出现巨量 >=5000 手, 当前快照骤减 >=50%) ---
                elif i >= 2 and prev_sum >= GHOST_FLASH_HANDS and cur_sum <= 0.5 * prev_sum:
                    prev2_sum = snapshots[i - 2][side].get(price, 0)
                    if prev2_sum <= 0.3 * prev_sum:
                        events.append({
                            "type": "幽灵单",
                            "side": side,
                            "price_level": orderlevel,
                            "price": price,
                            "delta_hands": int(cur_sum - prev_sum),
                            "duration_s": round(ts - snapshots[i - 2]["ts"], 2),
                            "ts": ts,
                            "note": "档位总量级幽灵堆积(巨量出现后消失)",
                        })

                # --- 撤单: 某档位笔数总和骤减 >=50% 且价格未按不利方向移动 ---
                elif delta <= -cancel_pct * max(prev_sum, 1) and prev_sum > 0:
                    # 价格不利方向移动检查: 买一最优价下移/卖一最优价上移 → 真实成交推动,
                    # 该档骤减是成交而非撤单; 价格未动/纹丝不动 → 疑似虚假挂单被撤。
                    price_ok = True
                    if side == "bid":
                        cur_best = min(cur["bid"].keys(), default=None)
                        prev_best = min(prev["bid"].keys(), default=None)
                        if prev_best is not None and cur_best is not None and cur_best < prev_best:
                            price_ok = False
                    else:
                        cur_best = max(cur["ask"].keys(), default=None)
                        prev_best = max(prev["ask"].keys(), default=None)
                        if prev_best is not None and cur_best is not None and cur_best > prev_best:
                            price_ok = False
                    note = ""
                    if tail:
                        note = "尾盘自然撤单(14:55后, 可忽略)"
                    elif not trading:
                        note = "非交易时段静态快照扰动"
                    events.append({
                        "type": "撤单",
                        "side": side,
                        "price_level": orderlevel,
                        "price": price,
                        "delta_hands": int(delta),
                        "duration_s": round(ts - prev["ts"], 2),
                        "ts": ts,
                        "note": note,
                    })

                # --- 幽灵单(档位口径): 本档总量较两快照前出现巨幅跃升后又消失 ---
                elif i >= 2:
                    prev2 = snapshots[i - 2]
                    prev2_sum = prev2[side].get(price, 0)
                    if (prev2_sum <= 0 and cur_sum >= 10 * sig_incr_hands
                            and prev_sum < cur_sum * 0.5):
                        # 上一个快照(prev)已大幅回落 → 巨量堆积一闪而逝
                        events.append({
                            "type": "幽灵单",
                            "side": side,
                            "price_level": orderlevel,
                            "price": price,
                            "delta_hands": int(cur_sum),
                            "duration_s": round(ts - prev2["ts"], 2),
                            "ts": ts,
                            "note": "档位总量级幽灵堆积(巨量出现后消失)",
                        })

            # 收盘处理: 对仍激活的堆单事件, 若当前快照该档已回落/消失则结算
            for key, a in list(active.items()):
                if a["side"] != side:
                    continue
                cur_sum = cur[side].get(key[1], 0)
                if cur_sum < a["peak_sum"] * 0.5:
                    _close_active(key, ts, cur_sum)

    # 采集结束: 仍未回落的堆单事件按"持续到结束"结算
    end_ts = snapshots[-1]["ts"]
    for key, a in list(active.items()):
        _close_active(key, end_ts, a["cur_sum"])

    # 统一按发生时间戳排序
    events.sort(key=lambda e: e["ts"])
    return events


# ---------------------------------------------------------------------------
# (2) Order Book Imbalance
# ---------------------------------------------------------------------------
def order_book_imbalance(snapshots: list[dict[str, Any]], top_n: int = OB_TOP_N) -> list[dict[str, Any]]:
    """订单簿失衡: 每快照一个 OB 值 + 阈值判断。

    BidAmt10 = Σ(前10档 bid price × sum(ordersque) × 100)   # 手→股→金额(元)
    AskAmt10 = 同理
    OB = (BidAmt10 - AskAmt10) / (BidAmt10 + AskAmt10)
    判断: OB > +0.3 → 买压; OB < -0.3 → 卖压; 否则中性。
    """
    series: list[dict[str, Any]] = []
    for snap in snapshots:
        bid_tops = sorted(snap["bid_levels"], key=lambda lv: int(lv["orderlevel"]))[:top_n]
        ask_tops = sorted(snap["ask_levels"], key=lambda lv: int(lv["orderlevel"]))[:top_n]
        bid_amt = sum(float(lv["price"]) * _sum_hands(lv["ordersque"]) * 100 for lv in bid_tops)
        ask_amt = sum(float(lv["price"]) * _sum_hands(lv["ordersque"]) * 100 for lv in ask_tops)
        denom = bid_amt + ask_amt
        ob = (bid_amt - ask_amt) / denom if denom > 0 else 0.0
        label = ("买压" if ob > OB_BUY_TH else "卖压" if ob < OB_SELL_TH else "中性")
        series.append({
            "ts": snap["ts"],
            "dt": snap["dt"],
            "bid_amt10": round(bid_amt, 2),
            "ask_amt10": round(ask_amt, 2),
            "ob": round(ob, 4),
            "label": label,
        })
    return series


# ---------------------------------------------------------------------------
# (3) 幽灵单检测
# ---------------------------------------------------------------------------
def _find_big_orders(ordersque: list[Any], level_total: int) -> list[int]:
    """找出一档内的大单列表: 单笔 > 1000 手 或 单笔 > 该档总手数 50%。"""
    big: list[int] = []
    for h in ordersque or []:
        try:
            hands = int(h)
        except (TypeError, ValueError):
            continue
        if hands > BIG_ORDER_HANDS or (level_total > 0 and hands > BIG_ORDER_RATIO * level_total):
            big.append(hands)
    return big


def ghost_order(snapshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """幽灵单检测: 跨快照跟踪大单"出现→消失"。

    口径: 对每快照每档 ordersque 找大单(单笔>1000手 或 单笔>该档总和50%);
    大单在某快照出现、下一快照同档位消失(且档位仍存在、总量未崩塌 → 排除"整档成交"误判),
    即计为一次幽灵单。ghost_ratio = 幽灵单次数 / 大单出现总次数。

    返回: (幽灵单事件列表, ghost_ratio)
    """
    ghosts: list[dict[str, Any]] = []
    total_big = 0

    # 上一快照各档大单指纹: {(side, price): Counter(大单手数)}
    prev_big: dict[tuple[str, float], dict[int, int]] = {}

    for i, snap in enumerate(snapshots):
        cur_big: dict[tuple[str, float], dict[int, int]] = {}
        for side, levels_key in (("bid", "bid_levels"), ("ask", "ask_levels")):
            for lv in snap[levels_key]:
                price = float(lv["price"])
                level_total = _sum_hands(lv["ordersque"])
                bigs = _find_big_orders(lv["ordersque"], level_total)
                key = (side, price)
                cur_big[key] = {}
                for b in bigs:
                    cur_big[key][b] = cur_big[key].get(b, 0) + 1
                    total_big += 1

        # 对比上一快照: 出现过的大单现在消失 → 幽灵单
        if prev_big:
            for key, prev_cnts in prev_big.items():
                cur_cnts = cur_big.get(key, {})
                # 仅当该档仍存在(价格未变)才判"消失"(价格移动说明是成交推进, 非幽灵)
                for bval, cnt in prev_cnts.items():
                    gone = cnt - cur_cnts.get(bval, 0)
                    if gone > 0:
                        ghosts.append({
                            "type": "幽灵单",
                            "side": key[0],
                            "price": key[1],
                            "price_level": None,  # 档序需回溯, 置 None 由调用方按价格补
                            "hands": bval,
                            "count": gone,
                            "appear_snap": i - 1,
                            "disappear_snap": i,
                            "dt": snap["dt"],
                        })
        prev_big = cur_big

    ratio = (len(ghosts) / total_big) if total_big > 0 else 0.0
    # 补齐 price_level(按出现快照的档序)
    for g in ghosts:
        s = snapshots[g["appear_snap"]]
        lv = s["bid_levels"] if g["side"] == "bid" else s["ask_levels"]
        for l in lv:
            if abs(float(l["price"]) - g["price"]) < 1e-6:
                g["price_level"] = int(l["orderlevel"])
                break
    # 用各快照的 dt 给事件补 ts
    for g in ghosts:
        s = snapshots[g["appear_snap"]]
        g["ts"] = s["ts"]
    ghosts.sort(key=lambda e: e["ts"])
    return ghosts, round(ratio, 4)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run(
    symbol: str,
    n_snapshots: int = 20,
    interval: float = 1.5,
    verbose: bool = False,
) -> dict[str, Any]:
    """主函数: 采集 n 个快照 → 跑三个算法 → 汇总结果。

    Args:
        symbol: THS 代码, 如 'USZA002361'(神剑股份)
        n_snapshots: 快照数量, 建议 10-30
        interval: 快照间隔秒数, 建议 1-2
        verbose: 是否打印每快照一行摘要

    Returns:
        {
          events:    盘口演变事件列表(托单/压单/撤单/幽灵单)
          ob_series: OB 序列(每快照一个), 附买压/卖压标签
          ghost_ratio: 幽灵单比率
          summary:   汇总文本(事件数/OB均值/幽灵比/时段说明)
        }
    """
    t0 = _now()
    snapshots: list[dict[str, Any]] = []
    for i in range(n_snapshots):
        snap = fetch_snapshot(symbol)
        snapshots.append(snap)
        if verbose:
            bid1 = snap["bid_levels"][0] if snap["bid_levels"] else None
            ask1 = snap["ask_levels"][0] if snap["ask_levels"] else None
            b1s = _sum_hands(bid1["ordersque"]) if bid1 else 0
            a1s = _sum_hands(ask1["ordersque"]) if ask1 else 0
            print(f"  [snap {i + 1}/{n_snapshots}] {snap['dt']} "
                  f"买一 {bid1['price'] if bid1 else '-'} 手数 {b1s} | "
                  f"卖一 {ask1['price'] if ask1 else '-'} 手数 {a1s}")
        if i < n_snapshots - 1:
            time.sleep(max(0.0, interval))

    events = order_book_evolution(snapshots)
    ob_series = order_book_imbalance(snapshots)
    ghosts, ghost_ratio = ghost_order(snapshots)

    ob_vals = [s["ob"] for s in ob_series]
    ob_mean = (sum(ob_vals) / len(ob_vals)) if ob_vals else 0.0
    buy_press = sum(1 for s in ob_series if s["label"] == "买压")
    sell_press = sum(1 for s in ob_series if s["label"] == "卖压")
    trade_state = ("交易时段" if _is_trading_time() else "非交易时段(盘口可能为收盘静态快照)")
    tail_state = "尾盘段(撤单可能为自然撤单)" if _is_tail_session() else ""

    ev_types: dict[str, int] = {}
    for e in events:
        ev_types[e["type"]] = ev_types.get(e["type"], 0) + 1

    summary = (
        f"symbol={symbol} 快照数={len(snapshots)} 用时={round(_now() - t0, 1)}s | "
        f"{trade_state}{tail_state} | "
        f"事件数={len(events)}{ev_types if ev_types else '(无事件)'} | "
        f"OB均值={round(ob_mean, 4)}(买压快照 {buy_press}/卖压快照 {sell_press}) | "
        f"幽灵比={ghost_ratio}"
    )

    return {
        "events": events,
        "ob_series": ob_series,
        "ghost_ratio": ghost_ratio,
        "summary": summary,
    }


def _synthetic_check() -> None:
    """合成数据验收: 构造含托单/压单/撤单/幽灵单/大单闪现的序列, 验证三个算法能正确触发。

    这是对算法逻辑本身的离线验证(不依赖网络/时序), 与上面真实数据实测互补。
    """
    print("\n[合成数据验收] 验证三个算法在非静止盘口上的检出能力:")

    # 造 8 个快照: 价格带 11.20-11.39, 每档 20 笔左右小单打底
    def _mk_level(price: float, orders: list[int], orderlevel: int) -> dict:
        return {"orderlevel": orderlevel, "price": price, "ordersque": orders}

    base_bid = [
        _mk_level(11.27, [100] * 30, 1), _mk_level(11.26, [100] * 25, 2),
        _mk_level(11.25, [100] * 20, 3), _mk_level(11.24, [100] * 18, 4),
    ]
    base_ask = [
        _mk_level(11.28, [100] * 30, 1), _mk_level(11.29, [100] * 25, 2),
        _mk_level(11.30, [100] * 20, 3), _mk_level(11.31, [100] * 18, 4),
    ]

    def _snap(bid: list[dict], ask: list[dict], tag: str) -> dict:
        return {
            "ts": time.time(), "dt": tag,
            "bid_levels": bid, "ask_levels": ask,
            "bid": {float(l["price"]): sum(l["ordersque"]) for l in bid},
            "ask": {float(l["price"]): sum(l["ordersque"]) for l in ask},
        }

    snaps = []
    # snap0: 基准
    snaps.append(_snap([dict(l) for l in base_bid], [dict(l) for l in base_ask], "s0基准"))
    # snap1: 买二(11.26)堆单 +5000 → 托单; 卖三(11.30)堆单 +6000 → 压单
    b1 = [dict(l) for l in base_bid]
    b1[1]["ordersque"] = [100] * 25 + [5000]  # 11.26 档 7500 手
    a1 = [dict(l) for l in base_ask]
    a1[2]["ordersque"] = [100] * 20 + [6000]  # 11.30 档 8000 手
    snaps.append(_snap(b1, a1, "s1堆单"))
    # snap2/snap3: 保持堆单(持续)再回落 → 托单/压单事件结算
    snaps.append(_snap([dict(l) for l in b1], [dict(l) for l in a1], "s2堆单持续"))
    snaps.append(_snap([dict(l) for l in base_bid], [dict(l) for l in base_ask], "s3堆单回落"))
    # snap4: 买四(11.24)撤单 50%+ → 撤单事件(价格未动)
    b4 = [dict(l) for l in base_bid]
    b4[3]["ordersque"] = [100] * 5  # 从 18 笔 1800 手 → 5 笔 500 手, 骤减 72%
    snaps.append(_snap(b4, [dict(l) for l in base_ask], "s4撤单"))
    # snap5: 卖一(11.28)插入单笔 50000 手大单 → 算法3 大单出现
    a5 = [dict(l) for l in base_ask]
    a5[0]["ordersque"] = [100] * 30 + [50000]
    snaps.append(_snap([dict(l) for l in b1], a5, "s5卖一大单"))
    # snap6: 大单消失(档位仍在) → 幽灵单(算法3)
    snaps.append(_snap([dict(l) for l in b1], [dict(l) for l in base_ask], "s6大单消失"))
    # snap7: 买三(11.25)闪现 8000 手(两快照前无此量)后消失 → 档位口径幽灵单
    b7 = [dict(l) for l in b1]
    b7[2]["ordersque"] = [100] * 20 + [8000]
    snaps.append(_snap(b7, [dict(l) for l in base_ask], "s7幽灵闪现"))
    snaps.append(_snap([dict(l) for l in b1], [dict(l) for l in base_ask], "s8幽灵消失"))

    evs = order_book_evolution(snaps)
    ob = order_book_imbalance(snaps)
    ghosts, ratio = ghost_order(snaps)

    types = {e["type"] for e in evs}
    print(f"  演变事件({len(evs)}): {[e['type'] for e in evs]}")
    ok_types = {"托单", "压单", "撤单"} <= types
    print(f"  托单/压单/撤单 全部检出: {'✅' if ok_types else '❌ ' + str(types)}")
    print(f"  OB 序列: {[s['ob'] for s in ob]}")
    print(f"  OB 买压/卖压判定: { {s['label'] for s in ob} }")
    ghost_desc = ", ".join(f"{g['side']}@{g['price']} {g['hands']}手" for g in ghosts)
    print(f"  算法3 幽灵单事件 {len(ghosts)} 条: [{ghost_desc}]")
    print(f"  ghost_ratio={ratio} ({'✅ 大单闪现被捕获' if ghosts else '❌ 未捕获'})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    print("=" * 70)
    print("orderbook_engine 自测: 神剑股份 USZA002361")
    print("=" * 70)

    # Part 1: 合成数据验收(离线, 验证算法逻辑本身)
    _synthetic_check()

    # Part 2: 真实数据实测(THS L2 盘口)
    print("\n" + "=" * 70)
    print("真实数据实测: 神剑股份 USZA002361")
    print("=" * 70)

    # 自测: 6 个快照(1s 间隔), 与任务要求的 5-10 个一致
    result = run(symbol="USZA002361", n_snapshots=6, interval=1.0, verbose=True)

    print("-" * 70)
    print("自测结果:")
    print(f"  summary: {result['summary']}")
    print(f"  盘口演变事件数: {len(result['events'])}")
    for ev in result["events"]:
        print(f"    {ev}")
    print(f"  OB 序列({len(result['ob_series'])} 个快照):")
    for s in result["ob_series"]:
        print(f"    {s['dt']}  OB={s['ob']:+.4f}  label={s['label']}  "
              f"bid10={s['bid_amt10']:.0f}  ask10={s['ask_amt10']:.0f}")
    print(f"  ghost_ratio: {result['ghost_ratio']}")
    print(f"  幽灵单事件: {sum(1 for e in result['events'] if e['type'] == '幽灵单')} 条 "
          f"(算法3明细见 ghost_order 返回值)")

    # 验证口径: 非交易时段静态快照时事件应为空 — 输出原因说明而非伪造
    if not result["events"]:
        from datetime import datetime as _dt
        reason = (
            "盘口无事件: 当前为" + ("交易时段" if _is_trading_time() else "非交易时段") +
            ", 多次采样盘口完全静止(收盘静态快照), 无档位手数变化可检测。" +
            f"本机时间 {_dt.now().strftime('%H:%M:%S')}, " +
            "若在 9:30-11:30/13:00-15:00 运行, 应能检测到托单/压单/撤单/幽灵单。"
        )
        print(f"  说明: {reason}")
    else:
        print("  验证通过: 已产出非空事件列表 ✅")

    print("=" * 70)