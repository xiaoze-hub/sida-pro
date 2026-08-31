"""题材启动识别 Agent(9:45 触发)。

目标:开盘后识别"刚启动"的新题材+首板候选,提前潜伏而非追高。
数据源:market_sentiment_collector(东财涨停池 + 涨停板块分布 + 指数)。
方法论移植自 a-share-expert 场景8「题材刚启动识别」框架。
"""
from __future__ import annotations

import logging
from datetime import datetime

from src.agents.base import AgentContext, BaseAgent

logger = logging.getLogger(__name__)


class ThemeLaunchDetectorAgent(BaseAgent):
    """题材启动识别:扫描新题材+首板候选,输出潜伏池。"""

    name = "theme_launch_detector"
    display_name = "题材启动识别"
    description = "开盘后扫描今日刚启动的新题材与首板候选,提前识别潜伏机会"

    async def collect(self, context: AgentContext) -> dict:
        """采集:涨停池 + 板块分布 + 指数(全部走东财,免 key)。"""
        trace_id = datetime.now().strftime("%m%d%H%M%S%f")[-10:]
        start_ts = __import__("time").monotonic()

        sentiment_data = {}
        top_sectors = []  # 采集失败时为空, 商品轮动检测跳过
        try:
            from src.collectors.market_sentiment_collector import (
                MarketSentimentCollector,
            )

            senti = MarketSentimentCollector()
            summary = senti.get_sentiment_summary()
            pool = senti.get_limit_up_pool()
            indices = senti.get_index_snapshot()

            # 首板候选(days=1 且板块集中度高)
            first_boards = [p for p in pool if p.get("days", 1) == 1]

            # 板块分布
            sector_dist = {}
            for p in pool:
                sector = p.get("sector", "") or "其他"
                sector_dist[sector] = sector_dist.get(sector, 0) + 1
            top_sectors = sorted(
                sector_dist.items(), key=lambda x: x[1], reverse=True
            )[:6]

            sentiment_data = {
                "sentiment": summary,
                "pool": pool,
                "first_boards": first_boards,
                "top_sectors": [
                    {"name": k, "count": v} for k, v in top_sectors
                ],
                "indices": indices,
            }
            logger.info(
                "[%s] 题材启动采集: pool=%s first_boards=%s sectors=%s",
                trace_id,
                len(pool),
                len(first_boards),
                len(top_sectors),
            )
        except Exception as e:
            logger.warning("[%s] 题材启动采集失败: %s", trace_id, e)
            sentiment_data = {}

        # 商品轮动前瞻 + 地缘冲突检测(2026-08-10 接入)
        # 用涨停板块分布 + 近48h事件流识别轮动阶段/冲突,联动题材启动判断
        rotation = {}
        try:
            from src.core.commodity_rotation import detect_rotation_stage
            # 事件输入: 板块分布(领涨行业/概念) 作为商品相关信号
            events = []
            for s in top_sectors:
                events.append(f"{s[0]}板块涨停{s[1]}家")
            # 冲突关键词直接检测(板块名含 军工/黄金/石油 等)
            sector_names = [s[0] for s in top_sectors]
            conflict_kws = ["军工", "国防", "石油", "油气", "黄金"]
            if any(k in " ".join(sector_names) for k in conflict_kws):
                events.append("地缘冲突避险板块活跃(军工/石油/黄金)")
            rotation = detect_rotation_stage(events)
            logger.info(
                "[%s] 商品轮动检测: stage=%s", trace_id, rotation.get("stage")
            )
        except Exception as e:
            logger.warning("[%s] 商品轮动检测失败: %s", trace_id, e)
            rotation = {}

        sentiment_data["rotation"] = rotation

        logger.info(
            "[%s] 题材启动采集完成: elapsed_ms=%s",
            trace_id,
            int((__import__("time").monotonic() - start_ts) * 1000),
        )

        return {
            "sentiment_data": sentiment_data,
            "timestamp": datetime.now().isoformat(),
            "run_trace_id": trace_id,
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        """构建题材启动识别 prompt。"""
        system_prompt = """你是一个A股题材博弈分析师。你的核心任务:在开盘后识别**今天刚启动的新题材 + 首板候选**,输出潜伏候选池。

目标:提前识别"刚启动"的题材股(潜伏期/启动首日),而不是涨完的高位票。

【硬约束】
- 只能推荐:沪市主板(600/601/603/605) + 深市主板(000/002)
- ❌ 禁止:创业板(300/301)、科创板(688)、北交所(830/836/870/920)
- ❌ ST/*ST 股直接排除
- 报告输出前扫描所有 6 位代码,出现 300/688/830/920 或 ST 立即删除替换

【输出格式】
标题:🚀 题材启动识别扫描 | YYYY-MM-DD

1. 今日新题材(板块 + 涨停家数 + 为什么新)
2. 首板候选池表:| 代码 | 名称 | 题材 | 涨停时间 | 逻辑 | 买点 | 止损 |
3. 风险提示

【判断标准】
- 新题材 = 从前期无名突然冲进涨停榜前列(新钱进场)
- 首板 = 今日第一次涨停(days=1),非连板
- 涨停集中度高(单一板块涨停≥3家)= 主线明确
- 板块分散 = 无主线,谨慎
- 最高连板若仅 1 板 = 情绪冰点,轻仓试错"""

        user_content = []
        sd = data.get("sentiment_data", {}) or {}
        senti = sd.get("sentiment", {}) or {}

        user_content.append(f"## 日期:{data.get('timestamp', datetime.now().isoformat())[:10]} 盘后扫描\n")

        if senti and not senti.get("error"):
            user_content.append("## 市场情绪")
            user_content.append(
                f"- 涨停家数:{senti.get('limit_up_count', '-')} 最高连板:{senti.get('max_streak', '-')}板"
            )
            top_stocks = senti.get("top_stocks", [])
            if top_stocks:
                user_content.append(f"- 最高板:{'、'.join(top_stocks)}")
            user_content.append("")

        top_sectors = sd.get("top_sectors", [])
        if top_sectors:
            user_content.append("## 涨停板块分布")
            sector_str = "、".join(
                "{}×{}".format(s.get("name"), s.get("count")) for s in top_sectors
            )
            user_content.append(f"- {sector_str}")
            user_content.append("")

        # 商品轮动前瞻 + 地缘冲突(2026-08-10 接入, 联动题材启动判断)
        rotation = sd.get("rotation") or {}
        if rotation and rotation.get("stage"):
            user_content.append("## 商品轮动前瞻(联动涨价题材)")
            user_content.append(f"- 当前阶段:{rotation.get('stage')}")
            rot_sectors = rotation.get("sectors") or []
            if rot_sectors:
                user_content.append(f"- 关联板块:{'、'.join(rot_sectors)}")
            if rotation.get("next_stage") and rotation.get("next_sectors"):
                user_content.append(
                    f"- 下一幕预判:{rotation.get('next_stage')}(关注 {'、'.join(rotation.get('next_sectors'))})"
                )
            if rotation.get("conflict"):
                user_content.append(f"- ⚠️ 地缘冲突模式:{rotation.get('conflict')}")
            user_content.append(
                "> 规则: 商品轮动/冲突阶段决定题材方向——能源涨→关注石油煤炭, 金属涨→有色钢铁, 冲突→军工黄金避险。题材启动需与轮动方向一致。"
            )
            user_content.append("")

        first_boards = sd.get("first_boards", [])
        if first_boards:
            user_content.append("## 首板候选(今日首次涨停)")
            for p in first_boards[:20]:
                user_content.append(
                    f"- {p.get('name')}({p.get('code')}) 题材:{p.get('sector', '-')} "
                    f"涨停时间:{str(p.get('first_time', ''))} 现价:{p.get('price', '-')}"
                )
            user_content.append("")

        return system_prompt, "\n".join(user_content)
