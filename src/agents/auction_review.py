"""竞价复盘 Agent(9:30 触发):wudao 竞价数据 + 竞价博弈方法论。

方法论移植自 wudao-auction-review skill:
1. 情绪定性(sentimentSignal: cooling/neutral/warming)
2. 主线定向(consistency 一致性,不是总额最大)
3. 盯盘名单(bidStrength 排序,3-5 只)

数据源:wudao MCP(HTTP 直连,含 consistency/bidStrength/弱转强等独家字段)。
"""
from __future__ import annotations

from src.bootstrap.agents import register_agent
import logging
from datetime import datetime

from src.agents.base import AgentContext, BaseAgent

logger = logging.getLogger(__name__)


@register_agent("auction_review")
class AuctionReviewAgent(BaseAgent):
    """竞价复盘:9:30 后解读当日竞价(情绪/主线/盯盘名单)。"""

    name = "auction_review"
    display_name = "竞价复盘"
    description = "9:30后解读当日集合竞价:情绪定性/主线定向/盯盘名单"

    async def collect(self, context: AgentContext) -> dict:
        """采集竞价数据(auction_collector: 悟道优先, 限流窗口快速失败)。"""
        trace_id = datetime.now().strftime("%m%d%H%M%S%f")[-10:]
        start_ts = __import__("time").monotonic()

        data: dict = {}

        try:
            from src.collectors.auction_collector import fetch_auction_raw

            raw = fetch_auction_raw()
            data["opening_snapshot"] = raw.get("opening_snapshot") or {}
            data["theme_strength"] = raw.get("theme_strength") or {}
            data["market_scan"] = raw.get("market_scan") or {}
            data["weak_to_strong"] = raw.get("weak_to_strong") or {}
            data["limitup_feedback"] = raw.get("limitup_feedback") or {}
            # 2026-09-10 修: 原先无条件 client_ok=True, 即使五个字段全空 → build_prompt 的
            # "数据不可用→降级"分支永不触发, 送空 prompt 给 LLM → 模型回报"数据缺失/日期在未来"。
            _sections = (
                "opening_snapshot",
                "theme_strength",
                "market_scan",
                "weak_to_strong",
                "limitup_feedback",
            )
            _has_data = any(bool(data.get(k)) for k in _sections)
            # 仅腾讯降级(无悟道独家字段) → 可用但降级, 需在 prompt 里标口径
            _has_wudao_only = any(
                bool(data.get(k))
                for k in ("theme_strength", "market_scan", "weak_to_strong", "limitup_feedback")
            )
            # 悟道独家字段不可得(免费档 9:15-10:30 被服务端屏蔽)时, 补**逐票**竞价快照:
            # ① 通达信 TQ(老板建议, 已接通, ~30ms/票): 开盘涨幅/竞价成交额/一字买量/买一卖一;
            # ② 同花顺超级盘口(游客账户): 竞价方向/高低 + 09:20 前撤单率近似(需帧序列, TQ 给不了)。
            if not _has_wudao_only:
                _wl = (
                    [s.symbol for s in context.watchlist] if context is not None else []
                )
                _snaps: dict = {}
                _snap_srcs: list[str] = []
                # ① 通达信 TQ(快 ~30ms/票, 覆盖多): 开盘涨幅/竞价成交额/一字买量/买一卖一
                try:
                    from src.collectors.auction_collector import (
                        fetch_auction_snapshots_tq,
                    )

                    _rows = fetch_auction_snapshots_tq(_wl, limit=20)
                    if _rows:
                        _snap_srcs.append("tq")
                        for _sym, _row in _rows.items():
                            _snaps[_sym] = dict(_row)
                except Exception as e:  # noqa: BLE001 - 补充源失败不拖垮主流程
                    logger.debug("[%s] TQ 竞价快照补充失败: %s", trace_id, e)
                # ② 同花顺(慢, 覆盖少): 补 竞价方向/高低 + 09:20 前撤单率近似(需帧序列)
                try:
                    from src.collectors.auction_collector import (
                        fetch_auction_snapshots_thsdk,
                    )

                    _rows2 = fetch_auction_snapshots_thsdk(_wl, limit=10)
                    if _rows2:
                        _snap_srcs.append("thsdk")
                        for _sym, _row in _rows2.items():
                            _snaps.setdefault(_sym, {}).update(_row)
                except Exception as e:  # noqa: BLE001
                    logger.debug("[%s] thsdk 竞价快照补充失败: %s", trace_id, e)
                if _snaps:
                    data["auction_snapshots"] = _snaps
                    data["snapshot_sources"] = _snap_srcs
            _has_snaps = bool(data.get("auction_snapshots"))
            data["client_ok"] = _has_data or _has_snaps
            data["degraded"] = bool(data["client_ok"]) and not _has_wudao_only
            data["source"] = (data.get("opening_snapshot") or {}).get("source") or (
                "wudao"
                if _has_wudao_only
                else "+".join(data.get("snapshot_sources") or [])
            )
            if raw.get("limited"):
                data["limited"] = True
                data["client_error"] = raw.get("error", "悟道限流窗口")
                logger.info(
                    "[%s] 悟道限流窗口, 竞价降级(has_data=%s): %s",
                    trace_id,
                    _has_data,
                    raw.get("error"),
                )
            elif raw.get("error"):
                data["client_error"] = raw.get("error")
                logger.warning("[%s] 悟道竞价采集失败: %s", trace_id, raw.get("error"))
            else:
                logger.info("[%s] 竞价采集完成(悟道)", trace_id)

        except Exception as e:
            logger.error("[%s] 竞价采集异常: %s", trace_id, e)
            data["client_ok"] = False
            data["client_error"] = str(e)

        logger.info(
            "[%s] 竞价采集完成: elapsed_ms=%s",
            trace_id,
            int((__import__("time").monotonic() - start_ts) * 1000),
        )

        return {
            "auction_data": data,
            "timestamp": datetime.now().isoformat(),
            "run_trace_id": trace_id,
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        """构建竞价复盘 prompt(先情绪后主线再名单)。"""
        system_prompt = """你是一个A股短线竞价分析员。你的任务:解读当日集合竞价,回答短线选手开盘前最关心的三件事:
1) 今天资金的态度变了吗?(情绪修复/中性/退潮)
2) 今天主线在哪?(哪个题材有一致性,不只是总额最大)
3) 9:25 之后该盯哪 3-5 只票?(强龙头、反包候选、风险标的)

【判断标准】
- consistency ≥ 0.5 = 题材有一致性,真主线
- consistency ≤ 0.2 = 少数票抱团,不算主线(即使总额大)
- bidStrength > 50 = 强; > 100 = 很强(市值归一化,跨市值可比)
- bidAmountPercentile ≥ 90 = 自身历史强
- breakRate ≥ 40% 或 highBoardBreakCount ≥ 3 = 退潮
- breakRate ≤ 15% 且 hotOpenCount ≥ 30% = 修复

【输出格式】
1. 情绪定性:修复/中性/退潮(一句话+证据)
2. 主线定向:哪 2-3 个题材有真一致性(consistency 值),哪些是假强(总额大但一致性低)
3. 盯盘名单:3-5 只票,每只标注「为什么入选」
4. 弱转强/被核:昨炸板谁反包强,昨高标谁被核
5. 风险提示

【数据与时效】
- 用户消息里已给出**当日真实数据与日期**; 严禁以"日期在未来/超出知识库截止/需要你提供数据"为由拒答, 也不要向用户索要数据。
- 数据缺字段(如悟道独家字段在 9:15-10:30 限流窗口不可得)就明确标注"该字段不可得", 并**基于已有数据**给出结论。

【硬约束】
- 先结论后证据
- 不给买卖建议,不预测后续涨跌,只做竞价截面解释
- 数据缺字段就明说,不要编"""

        user_content = []
        ad = data.get("auction_data", {}) or {}
        user_content.append(f"## 日期:{data.get('timestamp', datetime.now().isoformat())[:10]} 竞价复盘\n")

        if not ad.get("client_ok"):
            user_content.append(f"⚠️ 竞价数据全部不可用:{ad.get('client_error', '未知错误')}")
            user_content.append(
                "(悟道独家字段与腾讯降级榜均未取到 —— 无法给出竞价截面解读。"
                "请直接说明'今日竞价数据暂不可得(数据源不可用)', 并仅给中性提示; "
                "严禁编造具体数字, 也不要向用户索要数据。)"
            )
            return system_prompt, "\n".join(user_content)

        # 数据口径(降级时): 说明缺哪些字段及其原因, 避免模型把"缺失"当异常或反过来索要数据
        if ad.get("degraded"):
            user_content.append(
                f"> 数据口径: {ad.get('client_error') or '悟道数据不可用'}; "
                "「竞价全景」为腾讯批量行情降级(竞价高开榜), 并附**通达信+同花顺逐票竞价快照**"
                "(开盘涨幅/竞价成交额/一字买量/竞价方向/撤单率近似); **题材一致性(consistency)/竞价强度榜(bidStrength)/"
                "弱转强/被核反馈为悟道独家字段, 本时段不可得**。请基于已有高开榜+逐票快照做截面解读, "
                "缺的字段标注'不可得'即可, 不要索要数据。\n"
            )

        # 自选竞价快照(逐票): 通达信 TQ + 同花顺超级盘口(悟道独家字段不可得时的替代源)
        snaps = ad.get("auction_snapshots") or {}
        if snaps:
            user_content.append("## 自选竞价快照(逐票)")
            for sym, s in snaps.items():
                parts: list[str] = []
                if s.get("open") is not None:
                    _pct = s.get("open_pct")
                    _pct_s = f"({_pct:+.2f}%)" if isinstance(_pct, (int, float)) else ""
                    parts.append(f"开盘{s.get('open')}{_pct_s}")
                _amt = s.get("open_amount")
                if isinstance(_amt, (int, float)):
                    _amt_s = (
                        f"{_amt / 1e8:.2f}亿" if abs(_amt) >= 1e8 else f"{_amt / 1e4:.0f}万"
                    )
                    parts.append(f"竞价额{_amt_s}")
                _ztb = s.get("open_limit_buy")
                if isinstance(_ztb, (int, float)) and _ztb:
                    parts.append(f"开盘一字买量{_ztb:.0f}")
                if s.get("direction") and s.get("direction") != "无数据":
                    _g = s.get("gap_pct")
                    _g_s = f"{_g:+.2f}%" if isinstance(_g, (int, float)) else "-"
                    parts.append(f"同花顺{s.get('direction')}(偏离{_g_s})")
                _wr = s.get("withdraw_rate_pre0920")
                if isinstance(_wr, (int, float)):
                    parts.append(f"09:20前撤单率近似{_wr * 100:.1f}%")
                _b1, _b1v = s.get("buy1"), s.get("buy1_vol")
                if _b1:
                    parts.append(f"买一{_b1}×{_b1v or '-'}")
                if parts:
                    user_content.append(f"- {sym}: " + " | ".join(parts))
            user_content.append(
                "> 口径: 开盘/竞价额/一字买量来自**通达信**, 竞价方向/撤单率来自**同花顺超级盘口**近似; "
                "均与悟道 consistency/bidStrength 口径不同, 不可互相换算。\n"
            )

        # 竞价全景
        snap = ad.get("opening_snapshot", {}) or {}
        if snap.get("text"):
            user_content.append(f"## 竞价全景\n{snap['text']}\n")
        elif snap:
            user_content.append(f"## 竞价全景\n{json_dumps(snap)[:500]}\n")

        # 题材一致性
        themes = ad.get("theme_strength", {}) or {}
        theme_list = themes.get("themes") or themes.get("data", {}).get("themes") or []
        if theme_list:
            user_content.append("## 题材竞价一致性(consistency)")
            for t in theme_list[:8]:
                user_content.append(
                    f"- {t.get('name')}: 总额{t.get('totalBidAmountText','-')} "
                    f"一致性{t.get('consistencyText','-')} "
                    f"高开{t.get('hotOpenCount','-')}家 涨停开{t.get('limitUpOpenCount','-')}家"
                )
            user_content.append("")

        # 竞价强度榜
        scan = ad.get("market_scan", {}) or {}
        scan_list = (
            scan.get("rows")
            or scan.get("stocks")
            or scan.get("data", {}).get("rows")
            or scan.get("data", {}).get("stocks")
            or scan.get("list")
            or []
        )
        if scan_list:
            user_content.append("## 竞价强度榜(bidStrength)")
            for s in scan_list[:10]:
                user_content.append(
                    f"- {s.get('name')}({s.get('code','')}): 强度{s.get('bidStrength','-')} "
                    f"涨幅{s.get('changeRate','-')}% "
                    f"竞价额{s.get('bidAmountText', str(s.get('bidAmount','-'))) if 'bidAmountText' in s else s.get('bidAmount','-')}"
                )
            user_content.append("")

        # 弱转强
        wts = ad.get("weak_to_strong", {}) or {}
        wts_list = (
            wts.get("rows")
            or wts.get("stocks")
            or wts.get("data", {}).get("rows")
            or wts.get("data", {}).get("stocks")
            or wts.get("list")
            or []
        )
        if wts_list:
            user_content.append("## 弱转强候选(昨炸板反包)")
            for s in wts_list[:8]:
                user_content.append(
                    f"- {s.get('name')}({s.get('code','')}): 强度{s.get('wtsScore','-')}"
                )
            user_content.append("")

        # 昨涨停反馈(被核?)
        fb = ad.get("limitup_feedback", {}) or {}
        if fb.get("text"):
            user_content.append(f"## 昨涨停反馈\n{fb['text'][:400]}\n")
        elif fb:
            summary = fb.get("summary") or fb.get("data", {}).get("summary") or {}
            if summary:
                user_content.append(f"## 昨涨停反馈\n{json_dumps(summary)[:400]}\n")

        return system_prompt, "\n".join(user_content)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)
