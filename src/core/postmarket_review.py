# -*- coding: utf-8 -*-
"""盘后复盘(设计稿 §6.2 / 复刻手册 §5.2 §8) —— .tck 委托号级精确暗盘复盘。

## 定位

设计稿 §6.2「.tck 精确暗盘复盘: 委托号级暗盘, 与同花顺对齐」。与盘中实时
(`l4_events.split_clusters`, 成交级) 不同, 本模块用 .tck 的**委托申报记录**
(tag `00`, 含委托号 seq + 反向指针 a28/a32) 做**委托号级**拆单识别, 精确度更高。

## 暗盘口径(复刻手册 §5.2, 对齐同花顺)

- **明盘** = 单笔 > 30 万的大单(市场可见, 可被拆单伪装, 不能代表真主力)
- **暗盘** = 机构游资对倒拆单、大单拆小单(代表真正意图)
- 显示: 明盘 + 暗盘净额 > 0 = 流入(红) / < 0 = 流出(绿)

## 委托号级拆单识别(核心算法)

一笔大单被拆成多笔 <30 万的小委托挂单, 以躲避明盘阈值。这些委托在 .tck 里的特征:

    1. 委托号连续(seq 相邻或 gap 很小)
    2. 同价堆积(价格相同)
    3. 时间密集(相邻 30s 内)
    4. 每笔金额 < 30 万(单看是小单)
    5. 簇总额 >= 30 万(合起来是一笔大单)

五条同时满足 → 拆单簇 → 计入暗盘。委托号连续性 + 反向指针 a28/a32 是
**比盘中成交级聚簇更强的证据**(能区分拆单方向: 被主动买扫 = 拆卖单, 被主动卖砸 = 拆买单)。

## ⚠️ 诚实口径(红线)

- 无 .tck 文件 / 解析失败 / 无委托记录 → `available=False`, 不编造
- 撤单率 / 主动买卖比 分母为 0 → 返回 None(不除零, 不伪装)
- 对倒(自买自卖)需要账户信息, .tck 无账户字段 → 无法识别, 结果里显式说明
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 复用 dark_split 的明盘阈值(单一事实来源)
from src.core.dark_split import MING_THRESHOLD_30W  # noqa: E402

# --- 拆单簇阈值(与 l4_events 的成交级口径一致, 委托号级额外加 seq 连续性) ---
CLUSTER_MIN_TRADES = 5            # 一簇最少笔数
CLUSTER_WINDOW_MS = 30_000        # 相邻委托时间窗口 30s
CLUSTER_PRICE_TICKS = 1           # 价格容差 ±1 价位(0.01 元)
CLUSTER_MAX_SEQ_GAP = 3           # 委托号 gap <= 3(中间允许夹杂撤单等)
PRICE_TICK = 0.01


# ---------------------------------------------------------------------------
# ① 委托号级拆单识别
# ---------------------------------------------------------------------------
def split_clusters_from_orders(orders: Iterable[dict], date_: str) -> list[dict]:
    """从委托申报记录(tag `00`)识别拆单簇。

    只取**被成交的委托**(a28 或 a32 非零); 未成交的裸委托不参与。
    方向: a28 非零 = 被主动买扫掉 = 挂卖(拆卖单); a32 非零 = 被主动卖砸掉 = 挂买(拆买单)。

    Returns:
        [{price, side, count, shares, amount, time_range}]; 无簇 → []
    """
    rows = []
    for o in (orders or []):
        if not isinstance(o, dict):
            continue
        a28 = o.get("a28")
        a32 = o.get("a32")
        if not a28 and not a32:
            continue  # 未被成交的委托, 不参与拆单识别
        # 方向: 优先 a28(被主动买吃掉 → 挂卖), 否则 a32(挂买)
        side = "卖" if a28 else "买"
        rows.append({
            "seq": int(o.get("seq") or 0),
            "t": int(o.get("t") or 0),
            "price": float(o.get("price") or 0),
            "vol": int(o.get("vol") or 0),
            "amt": float(o.get("amt") or 0),
            "side": side,
        })
    if not rows:
        return []
    rows.sort(key=lambda x: (x["price"], x["t"], x["seq"]))

    events: list[dict] = []
    cluster: list[dict] = [rows[0]]

    def _close(cur: list[dict]):
        if len(cur) < CLUSTER_MIN_TRADES:
            return
        total_amt = sum(r["amt"] for r in cur)
        if total_amt < MING_THRESHOLD_30W:
            return
        if any(r["amt"] >= MING_THRESHOLD_30W for r in cur):
            return  # 簇里出现明摆着的大单, 不算拆单
        total_vol = sum(r["vol"] for r in cur)
        events.append({
            "date": date_,
            "price": round(cur[0]["price"], 4),
            "side": cur[0]["side"],
            "count": len(cur),
            "shares": total_vol,
            "amount": round(total_amt, 2),
            "time_range": _ms_to_hms(cur[0]["t"]) + "~" + _ms_to_hms(cur[-1]["t"]),
        })

    for prev, cur in zip(rows, rows[1:]):
        same_side = prev["side"] == cur["side"]
        near_price = abs(cur["price"] - prev["price"]) <= CLUSTER_PRICE_TICKS * PRICE_TICK + 1e-9
        in_window = (cur["t"] - prev["t"]) <= CLUSTER_WINDOW_MS
        seq_contiguous = (cur["seq"] - prev["seq"]) <= CLUSTER_MAX_SEQ_GAP and cur["seq"] > prev["seq"]
        if same_side and near_price and in_window and seq_contiguous:
            cluster.append(cur)
        else:
            _close(cluster)
            cluster = [cur]
    _close(cluster)
    return events


# ---------------------------------------------------------------------------
# ② 撤单率 / 主动买卖比(同源 .tck)
# ---------------------------------------------------------------------------
def cancel_rate(cancels: Iterable[dict], trades: Iterable[dict]) -> float | None:
    """撤单率 = 撤单量 / (撤单量 + 成交量)。

    分母为 0(无撤单也无成交) → None(不除零)。
    单位: 股(项目硬约束, 成交/撤单的 vol 均为股)。
    """
    cancel_vol = sum(int(c.get("vol") or 0) for c in (cancels or []) if isinstance(c, dict))
    trade_vol = sum(int(t.get("vol") or 0) for t in (trades or []) if isinstance(t, dict))
    denom = cancel_vol + trade_vol
    if denom <= 0:
        return None
    return round(cancel_vol / denom, 6)


def active_passive_ratio(trades: Iterable[dict]) -> float | None:
    """主动买卖比 = 主动买额 / 主动卖额。

    方向取 .tck trades 的 dir('B'='2B'主买 / 'S'='2S'主卖)。
    主动卖额为 0 → None(除零, 不伪装成无穷大)。
    """
    buy = sell = 0.0
    for t in (trades or []):
        if not isinstance(t, dict):
            continue
        amt = float(t.get("amt") or 0.0)
        d = str(t.get("dir") or "").upper()
        if d.startswith("B"):
            buy += amt
        elif d.startswith("S"):
            sell += amt
    if sell <= 0:
        return None
    return round(buy / sell, 4)


# ---------------------------------------------------------------------------
# ③ 暗盘复盘主入口
# ---------------------------------------------------------------------------
def dark_review_from_tck(symbol: str, date_: str | None = None,
                         tck_path: str | None = None) -> dict[str, Any]:
    """.tck 委托号级暗盘复盘(盘后)。

    输出对齐同花顺口径: 明盘 + 暗盘 + 主力净额(明+暗), 附撤单率/主动买卖比。

    Returns:
        {
          symbol, date, available,
          ming: {net, buy, sell, count},         # 明盘(单笔>30万)
          dark: {net, buy, sell, count, clusters},  # 暗盘(拆单簇)
          main_net,                               # 明+暗
          cancel_rate, active_passive_ratio,
          clusters, note
        }
        无数据 → available=False, 其余字段 None(不编造)。
    """
    base = {
        "symbol": symbol, "date": date_, "available": False,
        "ming": None, "dark": None, "main_net": None,
        "cancel_rate": None, "active_passive_ratio": None,
        "clusters": [], "note": None,
    }
    try:
        from src.core.dark_split import find_tck_file
        from src.core.tdx_tick_parser import parse_tck

        path = tck_path or find_tck_file(symbol, date_) or find_tck_file(symbol)
        if not path:
            base["note"] = "无 .tck 文件(需配置 PANWATCH_TCK_DIR)"
            return base
        trades, orders, cancels = parse_tck(path)
        if not trades:
            base["note"] = ".tck 无成交记录"
            return base
    except Exception as e:  # noqa: BLE001
        base["note"] = f".tck 解析失败: {e}"
        return base

    d = date_ or ""
    # 明盘: 单笔 > 30万 的成交
    ming_buy = ming_sell = ming_count = 0.0
    ming_count = 0
    for t in trades:
        amt = float(t.get("amt") or 0.0)
        if amt < MING_THRESHOLD_30W:
            continue
        ming_count += 1
        if str(t.get("dir") or "").upper().startswith("B"):
            ming_buy += amt
        else:
            ming_sell += amt

    # 暗盘: 委托号级拆单簇
    clusters = split_clusters_from_orders(orders, d)
    dark_buy = dark_sell = 0.0
    dark_count = 0
    for c in clusters:
        dark_count += c["count"]
        if c["side"] == "买":
            dark_buy += c["amount"]
        else:
            dark_sell += c["amount"]

    base["available"] = True
    base["ming"] = {
        "net": round(ming_buy - ming_sell, 2),
        "buy": round(ming_buy, 2), "sell": round(ming_sell, 2), "count": ming_count,
    }
    base["dark"] = {
        "net": round(dark_buy - dark_sell, 2),
        "buy": round(dark_buy, 2), "sell": round(dark_sell, 2), "count": dark_count,
    }
    base["main_net"] = round((ming_buy - ming_sell) + (dark_buy - dark_sell), 2)
    base["cancel_rate"] = cancel_rate(cancels, trades)
    base["active_passive_ratio"] = active_passive_ratio(trades)
    base["clusters"] = clusters
    base["note"] = "对倒(自买自卖)需账户信息, .tck 无账户字段 → 无法识别, 暗盘仅含拆单簇"
    return base


def _ms_to_hms(t: Any) -> str:
    """u32 时间(HHMMSSmmm) → 'HH:MM:SS'; 无法解析 → '--'。"""
    try:
        v = int(t or 0)
        if v <= 0:
            return "--"
        return f"{v // 10_000_000:02d}:{(v // 100_000) % 100:02d}:{(v // 1_000) % 100:02d}"
    except (TypeError, ValueError):
        return "--"
