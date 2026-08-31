# -*- coding: utf-8 -*-
"""
秒级 Delta 序列引擎(2026-08-19)
================================

输入: dark_l2 同构逐笔列表(腾讯逐笔或 thsdk L2 均可):
    [{"d": "B"/"S"/"M", "amt": 金额(元), "vol": 手数, "price": 价格, "t": "HH:MM:SS"}]

输出: 结构化 dict:
    {
      "ticks": int,            # 输入逐笔条数
      "first_t": str,          # 首条时间
      "last_t": str,           # 末条时间
      "seconds": [...],        # 按秒升序, 每秒:
          {"t", "sec", "net", "buy", "sell", "delta30", "cum_net", "price",
           "hi", "lo", "vol"}
      "signals": [...],        # 背离信号:
          {"type": "顶背离"/"底背离", "t", "price", "delta30", "cum_net",
           "since": "触发新高的时间", "streak": 持续秒数}
      "stats": {...}           # 统计汇总
    }

计算规则:
- 按秒聚合: net = 该秒内 主动买额(B) - 主动卖额(S), M(中性)不计
- 前滚 30 秒平滑: delta30[t] = Σ net 在 [t-29, t] 窗口内
  (窗口内无成交的秒自然缺失, 午休 11:30-13:00 的缺口不会跨段累计)
- 全天累计 Delta: cum_net[t] = Σ net 从首秒到 t
- 顶背离: 价格创日内新高后, delta30 转负且持续 >= 120 秒(2 分钟)不转正
- 底背离: 价格创日内新低后, delta30 转正且持续 >= 120 秒不转负

口径说明: delta 金额单位为【元】; vol 单位为手(仅统计, 不参与 Delta 计算)。
"""

from __future__ import annotations

import re
from typing import Optional

# 时间串校验: HH:MM:SS
_T_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def _t2sec(t: str) -> int:
    """'HH:MM:SS' → 当日秒数。"""
    if not _T_RE.match(t):
        raise ValueError(f"非法时间串: {t!r}, 应为 HH:MM:SS")
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _sec2t(sec: int) -> str:
    """当日秒数 → 'HH:MM:SS'。"""
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def compute_delta_series(
    ticks: list[dict],
    smooth_sec: int = 30,
    divergence_min_sec: int = 120,
) -> dict:
    """从逐笔列表计算秒级 Delta 序列 + 背离信号。

    Args:
        ticks: dark_l2 同构逐笔 [{d, amt, vol, price, t}]
        smooth_sec: 前滚平滑窗口(秒), 默认 30
        divergence_min_sec: 背离持续性门槛(秒), 默认 120(2 分钟)

    Returns:
        结构化 dict, 见模块 docstring。输入为空时报 ValueError。
    """
    if not ticks:
        raise ValueError("ticks 为空, 无法计算 Delta 序列")

    # 1) 按秒聚合
    #    sec -> {buy, sell, price(取该秒最后一笔), vol}
    agg: dict[int, dict] = {}
    for tk in ticks:
        if not isinstance(tk, dict) or "d" not in tk or "t" not in tk:
            raise ValueError(f"非法 tick 行: {tk!r}, 需要 d/amt/vol/price/t 字段")
        sec = _t2sec(tk["t"])
        a = agg.setdefault(sec, {"buy": 0.0, "sell": 0.0, "price": 0.0, "vol": 0.0})
        # 同一秒内多笔: 价格取最后一笔
        a["price"] = float(tk.get("price") or 0.0)
        a["vol"] += float(tk.get("vol") or 0.0)
        if tk["d"] == "B":
            a["buy"] += float(tk.get("amt") or 0.0)
        elif tk["d"] == "S":
            a["sell"] += float(tk.get("amt") or 0.0)

    secs = sorted(agg.keys())
    n = len(secs)

    # 2) 每秒 net / cum_net / 平滑 delta30(前缀和 O(1) 窗口求和)
    net_arr = [agg[s]["buy"] - agg[s]["sell"] for s in secs]
    prefix = [0.0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + net_arr[i]

    def window_sum(i: int) -> float:
        """前滚 smooth_sec 窗口和: [secs[i]-smooth_sec+1, secs[i]] 内所有有成交秒。"""
        lo = secs[i] - smooth_sec + 1
        j = 0
        while j <= i and secs[j] < lo:
            j += 1
        return prefix[i + 1] - prefix[j]

    seconds_out = []
    cum = 0.0
    hi, lo = float("-inf"), float("inf")
    for i, s in enumerate(secs):
        cum += net_arr[i]
        price = agg[s]["price"]
        if price > hi:
            hi = price
        if price < lo:
            lo = price
        seconds_out.append(
            {
                "t": _sec2t(s),
                "sec": s,
                "net": round(net_arr[i], 2),
                "buy": round(agg[s]["buy"], 2),
                "sell": round(agg[s]["sell"], 2),
                "delta30": round(window_sum(i), 2),
                "cum_net": round(cum, 2),
                "price": price,
                "hi": hi,
                "lo": lo,
                "vol": round(agg[s]["vol"], 1),
            }
        )

    # 3) 背离信号
    signals: list[dict] = []
    top_cand_sec: Optional[int] = None   # 最近一次价格新高的秒
    bot_cand_sec: Optional[int] = None   # 最近一次价格新低的秒
    neg_streak, pos_streak = 0, 0
    run_hi, run_lo = float("-inf"), float("inf")
    for row in seconds_out:
        price, delta30 = row["price"], row["delta30"]
        # 候选更新: 仅在严格超越前高/前低时刷新(等值停留不刷新)
        if price >= run_hi:
            if price > run_hi or top_cand_sec is None:
                top_cand_sec = row["sec"]
                neg_streak = 0  # 新高后重新累计负时长
            run_hi = price
        if price <= run_lo:
            if price < run_lo or bot_cand_sec is None:
                bot_cand_sec = row["sec"]
                pos_streak = 0
            run_lo = price

        # 顶背离: 新高后 delta30 转负并持续
        if top_cand_sec is not None:
            if delta30 < 0:
                neg_streak += 1
                if neg_streak >= divergence_min_sec:
                    signals.append(
                        {
                            "type": "顶背离",
                            "t": row["t"],
                            "price": price,
                            "delta30": round(delta30, 2),
                            "cum_net": round(cum, 2),
                            "since": _sec2t(top_cand_sec),
                            "streak": neg_streak,
                        }
                    )
                    top_cand_sec = None  # 已触发, 待下一次新高
                    neg_streak = 0
            else:
                neg_streak = 0  # 转正中断, 重新累计

        # 底背离: 新低后 delta30 转正并持续
        if bot_cand_sec is not None:
            if delta30 > 0:
                pos_streak += 1
                if pos_streak >= divergence_min_sec:
                    signals.append(
                        {
                            "type": "底背离",
                            "t": row["t"],
                            "price": price,
                            "delta30": round(delta30, 2),
                            "cum_net": round(cum, 2),
                            "since": _sec2t(bot_cand_sec),
                            "streak": pos_streak,
                        }
                    )
                    bot_cand_sec = None
                    pos_streak = 0
            else:
                pos_streak = 0

    total_buy = sum(r["buy"] for r in seconds_out)
    total_sell = sum(r["sell"] for r in seconds_out)
    stats = {
        "seconds": n,
        "total_buy_yuan": round(total_buy, 2),
        "total_sell_yuan": round(total_sell, 2),
        "total_neutral_yuan": round(
            sum(t["amt"] for t in ticks if t["d"] == "M"), 2
        ),
        "net_yuan": round(total_buy - total_sell, 2),
        "cum_net_last": round(cum, 2),
        "peak_delta30": max(r["delta30"] for r in seconds_out),
        "trough_delta30": min(r["delta30"] for r in seconds_out),
        "hi_price": hi,
        "lo_price": lo,
        "signals": len(signals),
    }

    return {
        "ticks": len(ticks),
        "first_t": seconds_out[0]["t"],
        "last_t": seconds_out[-1]["t"],
        "seconds": seconds_out,
        "signals": signals,
        "stats": stats,
    }


if __name__ == "__main__":
    import sys

    sys.path.insert(0, "/home/ubuntu/sida-src")

    print("=" * 70)
    print("delta_engine 自测: 真实 thsdk 逐笔 → 秒级 Delta 序列 + 背离信号")
    print("=" * 70)

    from src.core.dark_l2 import fetch_l2_ticks

    for demo_code, demo_name in [("sz002361", "神剑股份"), ("sh600519", "贵州茅台")]:
        print(f"\n--- {demo_name} {demo_code} ---")
        ticks = fetch_l2_ticks(demo_code, "thsdk")
        print(f"  输入逐笔: {len(ticks)} 条")

        res = compute_delta_series(ticks, smooth_sec=30, divergence_min_sec=120)
        st = res["stats"]
        print(f"  秒数={st['seconds']}  首秒={res['first_t']}  末秒={res['last_t']}")
        print(f"  主动买={st['total_buy_yuan']:,.0f}元  主动卖={st['total_sell_yuan']:,.0f}元  "
              f"净额={st['net_yuan']:,.0f}元")
        print(f"  累计Delta(末)={st['cum_net_last']:,.0f}元  价格区间={st['lo_price']}~{st['hi_price']}")
        print(f"  delta30 峰值={st['peak_delta30']:,.0f}  谷值={st['trough_delta30']:,.0f}")
        print(f"  信号数={st['signals']}")

        print("  前 3 秒样例:")
        for r in res["seconds"][:3]:
            print(f"    {r['t']} net={r['net']:,.0f} delta30={r['delta30']:,.0f} "
                  f"cum={r['cum_net']:,.0f} price={r['price']} hi={r['hi']} lo={r['lo']}")
        print("  信号样例:")
        for s in res["signals"][:3]:
            print(f"    {s['type']} @ {s['t']} price={s['price']} delta30={s['delta30']:,.0f} "
                  f"cum={s['cum_net']:,.0f} since={s['since']} streak={s['streak']}s")

        # 一致性断言
        assert res["ticks"] == len(ticks)
        assert st["seconds"] > 0
        assert abs(st["net_yuan"] - st["cum_net_last"]) < 0.01, "净额应等于累计Delta末值"
        print(f"  ✅ {demo_name} Delta 序列一致性断言通过")

    print("\n" + "=" * 70)
    print("✅ delta_engine 自测完成")