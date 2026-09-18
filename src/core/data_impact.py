"""数据集「影响面」映射(P2-3, 2026-09-18)。

## 为什么需要
现有能力视图已经能说"哪类数据在降级 / 没测过"(见 `data_capabilities.py`), 但用户看到
「资金流向 · 降级」之后还得自己猜:**这会影响我哪个页面? 我看到的数是缺的还是有替代?**
本模块把"影响面"补上 —— 数据集 → 受影响页面 + 降级时的**实际表现**。

## 三条口径
1. **降级表现写实**: 只写**代码里真实会发生的事**(空态 + 原因 / 多源兜底 / 显式「无数据」),
   不写"可能会受影响"这种没信息的话;
2. **未登记 ≠ 无影响**: 没登记的数据集返回 `pages=[]` + `effect="影响面未登记"` ——
   明确说"我们还没梳理", 不冒充"没有影响";
3. **不发散**: 只映射**已登记在 `DATASET_LABELS` 里的数据集**, 新增数据集必须同步补这里
   (有测试钉住: 每个数据集都要有影响面条目)。
"""

from __future__ import annotations

from typing import Any

#: 数据集 → {pages: 受影响页面, effect: 降级时的实际表现, fallback: 是否有替代源}
DATA_IMPACT: dict[str, dict[str, Any]] = {
    "kline": {
        "pages": ["个股/指数工作台", "热力图", "题材页", "预测标签", "决策账本(回填)"],
        "effect": "K 线区显示空态 + 原因(不是空白页); 收盘后由定时任务补数",
        "fallback": "有(tencent / eastmoney / sina 三源 + PG hypertable 缓存)",
    },
    "quote": {
        "pages": ["首页", "持仓", "自选", "榜单", "工作台顶部信息带"],
        "effect": "现价/涨跌幅显 `--`; 不拿昨收冒充现价",
        "fallback": "有(源按 priority 运行期兜底)",
    },
    "capital_flow": {
        "pages": ["盘口资金标签", "首页主力净流入", "口径对照页"],
        "effect": "资金数字显 `--` 并带口径标签; 与逐笔口径方向冲突时以逐笔为准",
        "fallback": "有(东财四档 ↔ 逐笔口径, **口径不同不互相校准**)",
    },
    "board_capital_flow": {
        "pages": ["热力图", "题材页", "板块详情"],
        "effect": "板块色块缺资金维度, 只按涨跌幅着色 + 显式说明",
        "fallback": "部分(板块资金另有东财派生)",
    },
    "market_capital_flow": {
        "pages": ["首页结论行", "大盘资金带"],
        "effect": "结论行该字段显 `--`, 颜色走中性(不给缺数上「涨红」)",
        "fallback": "无(大盘级只有这一路)",
    },
    "flash_news": {
        "pages": ["首页快讯", "通知中心"],
        "effect": "快讯区空态 + 原因; 不显示旧快讯冒充实时",
        "fallback": "无",
    },
    "news": {
        "pages": ["个股「消息」标签", "首页今日必读"],
        "effect": "公告/新闻段显「暂无」并注明来源未返回",
        "fallback": "部分(东财 ↔ 同花顺各自独立)",
    },
    "events": {
        "pages": ["K 线事件图标(§12)"],
        "effect": "事件图标**灰显** + 悬停显示原因(不隐藏也不伪装有数据)",
        "fallback": "无",
    },
    "macro_calendar": {
        "pages": ["首页财经日历", "盘前分析"],
        "effect": "日历区空态; 盘前报告显式标注「日历不可用」",
        "fallback": "无",
    },
    "fundamentals": {
        "pages": ["个股「基本面」标签", "工作台快照行(PE/PB)"],
        "effect": "字段显 `--`; 不进「缺数折叠」的隐藏位(基本面属决策关键位)",
        "fallback": "部分(同花顺 ↔ 东财)",
    },
    "dragon_tiger": {
        "pages": ["个股「盘口资金」标签", "龙虎榜相关卡片"],
        "effect": "进「缺数折叠」聚合展示(非关键位), 原因逐项可见",
        "fallback": "无",
    },
    "margin": {
        "pages": ["个股「基本面」标签"],
        "effect": "字段显 `--`",
        "fallback": "无",
    },
    "shareholders": {
        "pages": ["个股「基本面」标签"],
        "effect": "字段显 `--`",
        "fallback": "无",
    },
    "dividend": {
        "pages": ["个股「基本面」标签", "分红相关卡片"],
        "effect": "字段显 `--`",
        "fallback": "无",
    },
    "northbound": {
        "pages": ["首页北向卡片", "个股北向持仓"],
        "effect": "显式「无数据」+ 原因(该口径自政策调整后常无公开数据) —— 属**已知缺口**, 不是故障",
        "fallback": "无",
    },
    "chart": {
        "pages": ["个股页 K 线截图", "分享图"],
        "effect": "截图入口禁用 + 说明; 不影响主 K 线(自托管渲染)",
        "fallback": "无",
    },
    "more_info": {
        "pages": ["个股「盘口资金」标签(TQ 扩展指标)", "主力意图"],
        "effect": "扩展指标(撤单率/封单成色)显 `--` + 原因; 主力意图走逐笔口径不受影响",
        "fallback": "无",
    },
    "wenda": {
        "pages": ["AI 助手(问小达工具)"],
        "effect": "该工具返回「暂不可用」并说明原因, 不编造答案",
        "fallback": "无",
    },
}


def impact_of(dataset: str) -> dict[str, Any]:
    """取某数据集的影响面; **未登记时如实说"未登记", 不冒充"没影响"**。"""
    hit = DATA_IMPACT.get(dataset)
    if hit is None:
        return {"pages": [], "effect": "影响面未登记(尚未梳理, 不代表没影响)", "fallback": "未知"}
    return {
        "pages": list(hit.get("pages") or []),
        "effect": str(hit.get("effect") or ""),
        "fallback": str(hit.get("fallback") or "未知"),
    }
