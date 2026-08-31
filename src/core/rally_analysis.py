"""盘中顺势拉升段分析: 判断"放量上涨(真拉升)"还是"拉高出货(假拉升)"。

2026-08-12 神剑实证沉淀(用户需求): 基于逐单明细 + 分钟级价格/量能,
识别盘中顺势拉升段, 逐段拆解主力/散户买卖结构, 输出判别结论。

判别特征(来自 8/12 神剑案例的 5 个维度):
  1. 拉升段主动买占比: >60% = 真买; ~50% 或更低 = 对倒/出货
  2. 拉升段主力净额: 大额正 = 真; 负/小额 = 借拉出货
  3. 拉升段散户方向: 散户净卖/不追 = 主力自己买(真); 散户追涨接盘 = 出货
  4. 拉升后 5-10 分钟: 回落浅 + 主力仍净买 = 真; 快速回落 + 主力净卖 = 出货
  5. 量价配合: 放量 + 价涨 + 买占高 = 真; 放量但买占低 = 出货

数据源: 腾讯逐笔明细(dark_flow._fetch_all_ticks, 含 30s 增量缓存)。
"""
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 主力单阈值(腾讯官方口径): 金额≥20万 或 量≥600手
MAIN_AMT = 20e4
MAIN_VOL = 600
# 拉升识别参数
RALLY_VOL_MULT = 1.5      # 拉升分钟量能 ≥ 前5分钟均额 x1.5
RALLY_UP_MIN = 0.005      # 拉升分钟价格涨幅下限(元)
RALLY_DOWN_MIN = 0.005    # 下探分钟价格跌幅下限(元)
POST_WINDOW_MIN = 5       # 拉升后观察窗口(分钟)
# 判别阈值
BUY_RATIO_TRUE = 60.0     # 主动买占比>60% = 真买
MAIN_NET_TRUE = 0.0       # 拉升段主力净额>0 = 真买


def _build_minutes(ticks: list[dict]) -> tuple[OrderedDict, list[str]]:
    """逐笔 → 分钟聚合。返回 (分钟dict, 时间列表)。"""
    minutes: OrderedDict = OrderedDict()
    for t in ticks:
        if t["t"] < "09:30":
            continue  # 竞价单剔除
        hm = t["t"][:5]
        m = minutes.setdefault(hm, {
            "c": 0.0, "amt": 0.0, "n": 0,
            "buy": 0.0, "sell": 0.0, "mid": 0.0,
            "bb": 0.0, "bs": 0.0, "sb": 0.0, "ss": 0.0,
        })
        p, amt = t["price"], t["amt"]
        m["c"], m["amt"], m["n"] = p, m["amt"] + amt, m["n"] + 1
        if t["d"] == "B":
            m["buy"] += amt
            if amt >= MAIN_AMT or t["vol"] >= MAIN_VOL:
                m["bb"] += amt
            else:
                m["sb"] += amt
        elif t["d"] == "S":
            m["sell"] += amt
            if amt >= MAIN_AMT or t["vol"] >= MAIN_VOL:
                m["bs"] += amt
            else:
                m["ss"] += amt
        else:
            m["mid"] += amt
    return minutes, list(minutes.keys())


def _find_rallies(minutes: OrderedDict, times: list[str]) -> list[dict]:
    """识别顺势拉升分钟: 收盘价比前1分钟高 + 量能≥前5分钟均额x1.5。

    返回 [{t, c, prev_c, amt, avg, up}]。
    """
    rallies = []
    for i in range(1, len(times)):
        cur, prev = minutes[times[i]], minutes[times[i - 1]]
        if cur["c"] <= prev["c"] or cur["amt"] <= 0:
            continue
        w_times = times[max(0, i - 5):i]
        avg = sum(minutes[hm]["amt"] for hm in w_times) / max(len(w_times), 1)
        if avg > 0 and cur["amt"] >= RALLY_VOL_MULT * avg and (cur["c"] - prev["c"]) >= RALLY_UP_MIN:
            rallies.append({"t": times[i], "c": cur["c"], "prev_c": prev["c"],
                            "amt": cur["amt"], "avg": avg, "up": cur["c"] - prev["c"]})
    return rallies


def _merge_segments(rallies: list[dict], times: list[str], kind: str = "rally") -> list[dict]:
    """连续拉升/下探分钟合并为段。返回 [{start, end, start_price, end_price, amt, price_up|price_down, mins}]。"""
    key = "price_up" if kind == "rally" else "price_down"
    segments = []
    t_idx = {t: i for i, t in enumerate(times)}
    for r in rallies:
        if segments and t_idx.get(r["t"], -1) == t_idx.get(segments[-1]["end"], -2) + 1:
            seg = segments[-1]
            seg["end"] = r["t"]
            seg["end_price"] = r["c"]
            seg["amt"] += r["amt"]
            seg[key] = seg["end_price"] - seg["start_price"]
            seg["mins"] += 1
        else:
            segments.append({"start": r["t"], "end": r["t"],
                             "start_price": r["c"], "end_price": r["c"],
                             "amt": r["amt"], key: 0.0, "mins": 1})
    return segments


def _segment_stats(ticks: list[dict], start: str, end: str) -> dict:
    """段内逐笔统计: 主力/散户 买卖净额 + 主动买占比。

    分钟粒度段(start/end 为 "HH:MM")与逐笔时间("HH:MM:SS")比较须用前缀。
    """
    seg = [t for t in ticks if start <= t["t"][:5] <= end]
    bb = sum(t["amt"] for t in seg if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
    bs = sum(t["amt"] for t in seg if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")
    sb = sum(t["amt"] for t in seg if not (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
    ss = sum(t["amt"] for t in seg if not (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")
    buy = sum(t["amt"] for t in seg if t["d"] == "B")
    sell = sum(t["amt"] for t in seg if t["d"] == "S")
    total = sum(t["amt"] for t in seg)
    return {
        "main_net": round(bb - bs),
        "main_buy": round(bb), "main_sell": round(bs),
        "retail_net": round(sb - ss),
        "retail_buy": round(sb), "retail_sell": round(ss),
        "buy_ratio": round(buy / (buy + sell) * 100, 1) if (buy + sell) else None,
        "amt": round(total),
        "ticks": len(seg),
    }


def _judge(stats: dict, post: dict | None, price_up: float) -> dict:
    """判别拉升段性质: 放量上涨(真) / 拉高出货(假) / 对倒诱多 / 中性。"""
    buy_ratio = stats["buy_ratio"] or 0
    main_net = stats["main_net"]
    retail_net = stats["retail_net"]
    signals = []
    verdict = "中性"

    # 1. 主动买占比
    if buy_ratio >= BUY_RATIO_TRUE:
        signals.append(f"主动买占{buy_ratio:.0f}%>60%(真买)")
    elif buy_ratio <= 50:
        signals.append(f"主动买占{buy_ratio:.0f}%≤50%(对倒/卖压)")
    else:
        signals.append(f"主动买占{buy_ratio:.0f}%(中性)")

    # 2. 主力净额
    if main_net > MAIN_NET_TRUE:
        signals.append(f"主力净{main_net/1e4:+.0f}万(真金白银)")
    else:
        signals.append(f"主力净{main_net/1e4:+.0f}万(拉高借机卖)")

    # 3. 散户方向
    if retail_net < -30e4:
        signals.append("散户净卖(无人抬轿)")
    elif retail_net > 30e4:
        signals.append(f"散户净买{retail_net/1e4:+.0f}万(散户追涨⚠️)")
    else:
        signals.append("散户中性")

    # 4. 拉升后验证
    post_note = "无后续数据"
    if post:
        post_main = post["main_net"]
        post_drop = post["price_change"]
        if post_main < -200e4 and post_drop < 0:
            signals.append(f"拉升后主力净{post_main/1e4:+.0f}万+回落{post_drop:+.2f}元(出货⚠️)")
            post_note = "拉升后主力离场+回落"
        elif post_main > 0 and post_drop >= -0.02:
            signals.append(f"拉升后主力仍净买{post_main/1e4:+.0f}万+价格稳(真)")
            post_note = "拉升后主力留守"
        else:
            signals.append(f"拉升后主力净{post_main/1e4:+.0f}万/价{post_drop:+.2f}元")
            post_note = "拉升后中性"

    # 综合判定
    score = 0
    if buy_ratio >= BUY_RATIO_TRUE: score += 2
    elif buy_ratio <= 50: score -= 2
    if main_net > 300e4: score += 2
    elif main_net < -100e4: score -= 2
    if retail_net < -30e4: score += 1  # 散户不追 = 主力自己买
    elif retail_net > 30e4: score -= 1  # 散户追涨 = 出货风险
    if post:
        if post["main_net"] > 0 and post["price_change"] >= -0.02: score += 1
        elif post["main_net"] < -200e4 and post["price_change"] < 0: score -= 1

    if score >= 4:
        verdict = "放量上涨(真拉升)"
    elif score <= -3:
        verdict = "拉高出货(假拉升)"
    elif score <= -1:
        verdict = "疑似出货(对倒诱多)"
    elif score >= 2:
        verdict = "疑似真拉升"

    return {
        "verdict": verdict,
        "score": score,
        "signals": signals,
        "post_note": post_note,
        "price_up": round(price_up, 2),
    }


def analyze_rallies(symbol: str) -> dict | None:
    """分析股票当日盘中顺势拉升段。

    Args:
        symbol: A股代码, 如 "002361"

    Returns:
        {
          "symbol", "date", "current_price",
          "rallies": [{start, end, price_up, amt, main_net, retail_net,
                        buy_ratio, verdict, score, signals, post}],
          "summary": {"n_rallies", "true_count", "distribute_count", "main_net_total"},
        }
    """
    try:
        from marketdata import Symbol as MDSymbol
        from src.core.dark_flow import _fetch_all_ticks, _tencent_code, _TICKS_CACHE

        mdsym = MDSymbol.parse(symbol, "CN")
        code = _tencent_code(mdsym)
        if not code:
            return None
        _TICKS_CACHE.clear()  # 强制刷新, 保证分析的是最新数据
        ticks = _fetch_all_ticks(code)
        if not ticks:
            return None

        minutes, times = _build_minutes(ticks)
        if not times:
            return None

        rallies = _find_rallies(minutes, times)
        segments = _merge_segments(rallies, times)

        out_rallies = []
        for seg in segments:
            stats = _segment_stats(ticks, seg["start"], seg["end"])
            # 拉升后窗口
            end_idx = times.index(seg["end"]) if seg["end"] in times else -1
            post = None
            if end_idx >= 0 and end_idx + 1 < len(times):
                post_times = times[end_idx + 1:min(end_idx + 1 + POST_WINDOW_MIN, len(times))]
                post_ticks = [t for t in ticks if post_times[0] <= t["t"][:5] <= post_times[-1]]
                pbb = sum(t["amt"] for t in post_ticks if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
                pbs = sum(t["amt"] for t in post_ticks if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")
                post = {
                    "main_net": round(pbb - pbs),
                    "price_change": round(minutes[post_times[-1]]["c"] - seg["end_price"], 2),
                }
            judge = _judge(stats, post, seg["price_up"])
            out_rallies.append({
                "start": seg["start"], "end": seg["end"],
                "price_up": round(seg["price_up"], 2),
                "amt": stats["amt"], "ticks": stats["ticks"],
                "main_net": stats["main_net"], "main_buy": stats["main_buy"],
                "main_sell": stats["main_sell"],
                "retail_net": stats["retail_net"],
                "retail_buy": stats["retail_buy"], "retail_sell": stats["retail_sell"],
                "buy_ratio": stats["buy_ratio"],
                "verdict": judge["verdict"], "score": judge["score"],
                "signals": judge["signals"],
                "post": post,
            })

        # 全天汇总
        all_non_auc = [t for t in ticks if t["t"] >= "09:30"]
        all_bb = sum(t["amt"] for t in all_non_auc if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
        all_bs = sum(t["amt"] for t in all_non_auc if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")
        last_price = ticks[-1]["price"]

        summary = {
            "n_rallies": len(out_rallies),
            "true_count": sum(1 for r in out_rallies if r["verdict"].startswith("放量上涨")),
            "distribute_count": sum(1 for r in out_rallies if "出货" in r["verdict"]),
            "main_net_total": round(all_bb - all_bs),
        }

        return {
            "symbol": symbol,
            "date": ticks[-1]["t"][:5] and __import__("datetime").date.today().isoformat(),
            "current_price": last_price,
            "rallies": out_rallies,
            "summary": summary,
        }
    except Exception as e:
        logger.warning(f"拉升段分析失败 {symbol}: {e}")
        return None


def _find_dips(minutes: OrderedDict, times: list[str]) -> list[dict]:
    """识别瞬时下探分钟: 收盘价比前1分钟低 + 量能≥前5分钟均额x1.5。

    返回 [{t, c, prev_c, amt, avg, down}]。
    """
    dips = []
    for i in range(1, len(times)):
        cur, prev = minutes[times[i]], minutes[times[i - 1]]
        if cur["c"] >= prev["c"] or cur["amt"] <= 0:
            continue
        w_times = times[max(0, i - 5):i]
        avg = sum(minutes[hm]["amt"] for hm in w_times) / max(len(w_times), 1)
        if avg > 0 and cur["amt"] >= RALLY_VOL_MULT * avg and (prev["c"] - cur["c"]) >= RALLY_DOWN_MIN:
            dips.append({"t": times[i], "c": cur["c"], "prev_c": prev["c"],
                         "amt": cur["amt"], "avg": avg, "down": prev["c"] - cur["c"]})
    return dips


def _judge_dip(stats: dict, post: dict | None, price_down: float) -> dict:
    """判别下探段性质: 放量下杀(真出货) / 诱空吸筹(假跌) / 中性。

    镜像于 _judge: 主动卖占比>60% = 真砸; 主力大额净卖+下探后仍流出 = 出货;
    主力净买+下探后回补 = 诱空吸筹(主力借跌吸筹)。
    """
    sell_ratio = 100 - (stats["buy_ratio"] or 0)  # 主动卖占比
    main_net = stats["main_net"]
    retail_net = stats["retail_net"]
    signals = []
    verdict = "中性"
    score = 0

    if sell_ratio >= 60:
        signals.append(f"主动卖占{sell_ratio:.0f}%>60%(真砸)")
        score -= 2
    elif sell_ratio <= 50:
        signals.append(f"主动卖占{sell_ratio:.0f}%≤50%(对倒/承接)")
        score += 2
    else:
        signals.append(f"主动卖占{sell_ratio:.0f}%(中性)")

    if main_net < -300e4:
        signals.append(f"主力净{main_net/1e4:+.0f}万(真出货)")
        score -= 2
    elif main_net > 100e4:
        signals.append(f"主力净{main_net/1e4:+.0f}万(逆势吸筹)")
        score += 2
    else:
        signals.append(f"主力净{main_net/1e4:+.0f}万")

    if retail_net < -30e4:
        signals.append("散户割肉(恐慌盘)")
    elif retail_net > 30e4:
        signals.append(f"散户净买{retail_net/1e4:+.0f}万(散户接盘⚠️)")
        score += 1  # 散户接盘 = 主力在卖
    else:
        signals.append("散户中性")

    post_note = "无后续数据"
    if post:
        post_main = post["main_net"]
        post_chg = post["price_change"]
        if post_main > 200e4 and post_chg > 0:
            signals.append(f"下探后主力回补{post_main/1e4:+.0f}万+反弹{post_chg:+.2f}元(诱空⚠️)")
            score += 2
            post_note = "下探后主力回补(诱空吸筹)"
        elif post_main < -200e4 and post_chg <= 0:
            signals.append(f"下探后主力仍流出{post_main/1e4:+.0f}万+续跌(真出货⚠️)")
            score -= 2
            post_note = "下探后主力持续流出"
        else:
            signals.append(f"下探后主力净{post_main/1e4:+.0f}万/价{post_chg:+.2f}元")
            post_note = "下探后中性"

    if score >= 4:
        verdict = "诱空吸筹(假跌)"
    elif score <= -3:
        verdict = "放量下杀(真出货)"
    elif score <= -1:
        verdict = "疑似出货"
    elif score >= 2:
        verdict = "疑似诱空"

    return {
        "verdict": verdict,
        "score": score,
        "signals": signals,
        "post_note": post_note,
        "price_down": round(price_down, 2),
    }


# 横盘段识别参数(2026-08-12 用户反馈: 尾盘放量横盘没标注)
FLAT_MIN_MINS = 5           # 最少连续分钟数
FLAT_MAX_SPREAD = 0.003     # 段内收盘价最大波幅(0.3%)
FLAT_VOL_MULT = 3.0         # 段累计额 ≥ 前10分钟均额 x3(放量)
FLAT_MIN_MAIN = 200e4       # 主力净额绝对值 ≥200万 才值得标注


def _find_flat_segments(minutes: OrderedDict, times: list[str]) -> list[dict]:
    """识别放量横盘段: 连续N分钟价格波动<0.3% 且 累计成交额显著放大。

    尾盘"放量横盘"常是托盘出货/压盘吸筹(价格不动但量巨大)——拉升/下探
    识别(要求价格剧烈变动)会漏掉这类主力行为。

    返回 [{start, end, hi, lo, spread, amt, avg}]。
    """
    segs: list[dict] = []
    cur: dict | None = None
    for i, t in enumerate(times):
        m = minutes[t]
        if cur is None:
            cur = {"start": t, "end": t, "hi": m["c"], "lo": m["c"], "amt": m["amt"], "n": 1}
            continue
        # 扩展当前段: 新分钟收盘仍在 [lo, hi] 波幅内 → 继续横盘
        new_hi = max(cur["hi"], m["c"])
        new_lo = min(cur["lo"], m["c"])
        spread = (new_hi - new_lo) / max(new_lo, 1e-9)
        if spread <= FLAT_MAX_SPREAD:
            cur["end"] = t
            cur["hi"], cur["lo"] = new_hi, new_lo
            cur["amt"] += m["amt"]
            cur["n"] += 1
        else:
            if cur["n"] >= FLAT_MIN_MINS:
                segs.append(cur)
            cur = {"start": t, "end": t, "hi": m["c"], "lo": m["c"], "amt": m["amt"], "n": 1}
    if cur and cur["n"] >= FLAT_MIN_MINS:
        segs.append(cur)

    # 放量过滤: 段累计额 ≥ 段前10分钟均额 x3
    out = []
    for seg in segs:
        end_idx = times.index(seg["end"]) if seg["end"] in times else -1
        if end_idx < 0:
            continue
        w = times[max(0, end_idx - 10):end_idx + 1]
        avg = sum(minutes[hm]["amt"] for hm in w) / max(len(w), 1)
        if avg > 0 and seg["amt"] >= FLAT_VOL_MULT * avg:
            seg["avg"] = avg
            seg["spread"] = round((seg["hi"] - seg["lo"]) / max(seg["lo"], 1e-9), 4)
            out.append(seg)
    return out


def _judge_flat(stats: dict, seg: dict) -> dict:
    """判别放量横盘性质: 托盘出货 / 压盘吸筹 / 对倒换手 / 中性。

    托盘出货(危险): 主力净卖 + 主动买占<45%(大单托价, 小单出货)
    压盘吸筹: 主力净买 + 主动买占>55%(压价收筹)
    对倒换手: 主力净额≈0 但 主力成交占比高(自买自卖造量)
    """
    buy_ratio = stats["buy_ratio"] or 0
    main_net = stats["main_net"]
    main_turnover = stats.get("main_buy", 0) + stats.get("main_sell", 0)
    signals = []
    score = 0
    verdict = "中性"

    if buy_ratio < 45:
        signals.append(f"主动买占{buy_ratio:.0f}%<45%(卖压重)")
        score -= 2
    elif buy_ratio > 55:
        signals.append(f"主动买占{buy_ratio:.0f}%>55%(买盘强)")
        score += 2
    else:
        signals.append(f"主动买占{buy_ratio:.0f}%(均衡)")

    if main_net < -FLAT_MIN_MAIN:
        signals.append(f"主力净{main_net/1e4:+.0f}万(大单托盘出货)")
        score -= 2
    elif main_net > FLAT_MIN_MAIN:
        signals.append(f"主力净{main_net/1e4:+.0f}万(压盘吸筹)")
        score += 2
    else:
        if main_turnover > 500e4:
            signals.append(f"主力双向{main_turnover/1e4:.0f}万净额≈0(对倒嫌疑)")
            score -= 1
        else:
            signals.append(f"主力净{main_net/1e4:+.0f}万(无主力参与)")

    if score <= -2:
        verdict = "托盘出货"
    elif score >= 2:
        verdict = "压盘吸筹"
    elif score == -1:
        verdict = "对倒换手"
    return {"verdict": verdict, "score": score, "signals": signals}


def analyze_swings(symbol: str) -> dict | None:
    """分析当日盘中顺势拉升段 + 瞬时下探段(供分时K线标记)。

    与 analyze_rallies 共享逐笔缓存(30s TTL), 一次性计算两类段。

    Returns:
        {
          "symbol", "current_price",
          "rallies": [...同 analyze_rallies...],
          "dips": [{start, end, price_down, amt, main_net, retail_net,
                     sell_ratio, verdict, score, post}],
          "summary": {...},
        }
    """
    try:
        from marketdata import Symbol as MDSymbol
        from src.core.dark_flow import _fetch_all_ticks, _tencent_code

        mdsym = MDSymbol.parse(symbol, "CN")
        code = _tencent_code(mdsym)
        if not code:
            logger.warning(f"analyze_swings {symbol}: 无代码")
            return None
        ticks = _fetch_all_ticks(code)
        if not ticks:
            logger.warning(f"analyze_swings {symbol}: 无逐笔数据(code={code})")
            return None

        minutes, times = _build_minutes(ticks)
        if not times:
            return None

        # 拉升段
        rallies = _merge_segments(_find_rallies(minutes, times), times)
        # 下探段
        dips_raw = _find_dips(minutes, times)
        dips = _merge_segments(dips_raw, times, kind="dip")

        def _post_window(end: str):
            end_idx = times.index(end) if end in times else -1
            if end_idx < 0 or end_idx + 1 >= len(times):
                return None
            post_times = times[end_idx + 1:min(end_idx + 1 + POST_WINDOW_MIN, len(times))]
            post_ticks = [t for t in ticks if post_times[0] <= t["t"][:5] <= post_times[-1]]
            pbb = sum(t["amt"] for t in post_ticks if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
            pbs = sum(t["amt"] for t in post_ticks if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")
            return {
                "main_net": round(pbb - pbs),
                "price_change": round(minutes[post_times[-1]]["c"] - minutes[end]["c"], 2),
            }

        out_rallies = []
        for seg in rallies:
            stats = _segment_stats(ticks, seg["start"], seg["end"])
            post = _post_window(seg["end"])
            judge = _judge(stats, post, seg["price_up"])
            out_rallies.append({
                "start": seg["start"], "end": seg["end"],
                "price_up": round(seg["price_up"], 2),
                "amt": stats["amt"], "ticks": stats["ticks"],
                "main_net": stats["main_net"], "main_buy": stats["main_buy"],
                "main_sell": stats["main_sell"],
                "retail_net": stats["retail_net"],
                "retail_buy": stats["retail_buy"], "retail_sell": stats["retail_sell"],
                "buy_ratio": stats["buy_ratio"],
                "verdict": judge["verdict"], "score": judge["score"],
                "signals": judge["signals"],
                "post": post,
            })

        out_dips = []
        for seg in dips:
            stats = _segment_stats(ticks, seg["start"], seg["end"])
            post = _post_window(seg["end"])
            judge = _judge_dip(stats, post, seg.get("price_down", 0))
            out_dips.append({
                "start": seg["start"], "end": seg["end"],
                "price_down": round(seg.get("price_down", 0), 2),
                "amt": stats["amt"], "ticks": stats["ticks"],
                "main_net": stats["main_net"], "main_buy": stats["main_buy"],
                "main_sell": stats["main_sell"],
                "retail_net": stats["retail_net"],
                "sell_ratio": round(100 - (stats["buy_ratio"] or 0), 1),
                "verdict": judge["verdict"], "score": judge["score"],
                "signals": judge["signals"],
                "post": post,
            })

        # 放量横盘段(2026-08-12): 托盘出货/压盘吸筹/对倒换手
        out_flats = []
        for seg in _find_flat_segments(minutes, times):
            stats = _segment_stats(ticks, seg["start"], seg["end"])
            judge = _judge_flat(stats, seg)
            out_flats.append({
                "start": seg["start"], "end": seg["end"],
                "spread": seg.get("spread", 0), "amt": stats["amt"], "ticks": stats["ticks"],
                "main_net": stats["main_net"], "main_buy": stats["main_buy"],
                "main_sell": stats["main_sell"],
                "retail_net": stats["retail_net"],
                "retail_buy": stats["retail_buy"], "retail_sell": stats["retail_sell"],
                "buy_ratio": stats["buy_ratio"],
                "verdict": judge["verdict"], "score": judge["score"],
                "signals": judge["signals"],
            })

        all_non_auc = [t for t in ticks if t["t"] >= "09:30"]
        all_bb = sum(t["amt"] for t in all_non_auc if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "B")
        all_bs = sum(t["amt"] for t in all_non_auc if (t["amt"] >= MAIN_AMT or t["vol"] >= MAIN_VOL) and t["d"] == "S")

        return {
            "symbol": symbol,
            "current_price": ticks[-1]["price"],
            "rallies": out_rallies,
            "dips": out_dips,
            "flats": out_flats,
            "summary": {
                "n_rallies": len(out_rallies),
                "n_dips": len(out_dips),
                "n_flats": len(out_flats),
                "true_rallies": sum(1 for r in out_rallies if "放量上涨" in r["verdict"] or "疑似真拉升" in r["verdict"]),
                "true_dips": sum(1 for d in out_dips if "诱空" in d["verdict"] or "疑似诱空" in d["verdict"]),
                "flat_verdicts": [f["verdict"] for f in out_flats],
                "main_net_total": round(all_bb - all_bs),
            },
        }
    except Exception as e:
        logger.warning(f"拉升/下探段分析失败 {symbol}: {e}", exc_info=True)
        return None


def format_rally_report(result: dict) -> str:
    """结构化摘要(供 AI 助手/推送展示, 不依赖 LLM 复述)。"""
    if not result:
        return ""
    lines = [f"{result['symbol']} 拉升段分析(逐单明细): 现价{result['current_price']}"]
    s = result["summary"]
    lines.append(f"识别{s['n_rallies']}段顺势拉升: 真拉升{s['true_count']}段 / 疑似出货{s['distribute_count']}段 / 全天主力净{s['main_net_total']/1e4:+.0f}万")
    for r in result["rallies"]:
        up = r.get("price_up") or 0.0
        br = r.get("buy_ratio") or 0.0
        lines.append(
            f"[{r['start']}-{r['end']}] 涨{up:+.2f}元 额{r['amt']/1e4:.0f}万 | "
            f"{r['verdict']}(评分{r['score']}) | 主力净{r['main_net']/1e4:+.0f}万 买占{br:.0f}% | "
            f"{'；'.join(r['signals'])}"
        )
        if r.get("post"):
            lines.append(f"  拉升后: 主力净{r['post']['main_net']/1e4:+.0f}万, 价{r['post']['price_change']:+.2f}元")
    return "\n".join(lines)
