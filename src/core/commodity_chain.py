"""商品品种 → A 股行业通道 (`src/core/commodity_chain.py`)。

为什么需要它：盘前快讯里的商品行情（生产库真实形态如『商品期货收盘涨跌不一 钯涨超4%…
纸浆涨超1』）只说明「某商品涨了」，不含任何 A 股标的；而
`event_catalyst_engine` 的方向是「已知个股找事件」，反过来问不了。这里补的就是
**商品 → 行业 → 成分股** 这条正向通道。

口径（2026-11-11）：
- 行业名只用**已核实的真实 TQ 二级行业名**（128 个目录里的；如 造纸 881167.SH、
  包装印刷 881171.SH），不自编板块名，也不做同花顺↔通达信代码映射（两边撞号不同义）。
- `direction` 是**商品价格上涨**时该行业的传导方向：
  * `涨价受益` — 行业本身就是该商品的产出方（纯碱→化学原料、生猪→养殖业）；
  * `成本承压` — 该商品是行业的主要成本项（纸浆→造纸/包装印刷、棉花→纺织制造）。
  两个方向都保留行业本身：涨价既可能是行情催化，也可能压毛利，判断留给下游模型。
- TQ 取数失败一律 `None`，**绝不返回 `[]`** —— 空列表会被上游当成「这个板块没有成分股」，
  把「没接上/没取到」伪装成「市场事实」。
- 不写文件、不调 LLM、不推通知；网络调用全部经 `src.core.tdx_boards` 的
  `sector_items()` / `constituents()`，测试直接打桩这两个函数，不连真网关。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 商品涨价对行业的传导方向（见模块 docstring）
DIRECTION_BENEFIT = "涨价受益"
DIRECTION_COST = "成本承压"
DIRECTIONS = (DIRECTION_BENEFIT, DIRECTION_COST)

# 品种 → 真实 TQ 行业名 + 方向 + 命中关键词
# 关键词同时供 `premarket_evidence._NEWS_THEME_KEYWORDS`（大宗/涨价）复用，避免两处漂移。
COMMODITY_CHAINS: dict[str, dict[str, Any]] = {
    # 浆纸链：纸浆/木浆/各类成品纸同属一条成本链
    "纸浆": {
        "keywords": ("纸浆", "木浆", "浆价", "原纸", "瓦楞纸", "箱板纸", "白卡纸", "双胶纸"),
        "industries": ("造纸", "包装印刷"),
        "direction": DIRECTION_COST,
    },
    "纯碱": {"keywords": ("纯碱",), "industries": ("化学原料",), "direction": DIRECTION_BENEFIT},
    "尿素": {"keywords": ("尿素",), "industries": ("农用化工",), "direction": DIRECTION_BENEFIT},
    "磷肥": {"keywords": ("磷肥",), "industries": ("农用化工",), "direction": DIRECTION_BENEFIT},
    "钾肥": {"keywords": ("钾肥",), "industries": ("农用化工",), "direction": DIRECTION_BENEFIT},
    "甲醇": {"keywords": ("甲醇",), "industries": ("化学制品",), "direction": DIRECTION_BENEFIT},
    "PTA": {"keywords": ("PTA",), "industries": ("化纤",), "direction": DIRECTION_BENEFIT},
    "涤纶": {"keywords": ("涤纶",), "industries": ("化纤",), "direction": DIRECTION_BENEFIT},
    "乙二醇": {"keywords": ("乙二醇",), "industries": ("化纤",), "direction": DIRECTION_BENEFIT},
    "螺纹钢": {"keywords": ("螺纹钢",), "industries": ("普钢",), "direction": DIRECTION_BENEFIT},
    "铁矿石": {"keywords": ("铁矿石",), "industries": ("冶钢原料",), "direction": DIRECTION_BENEFIT},
    "焦煤": {"keywords": ("焦煤",), "industries": ("煤炭开采",), "direction": DIRECTION_BENEFIT},
    "动力煤": {"keywords": ("动力煤",), "industries": ("煤炭开采",), "direction": DIRECTION_BENEFIT},
    "锂": {
        "keywords": ("锂", "碳酸锂", "氢氧化锂", "锂精矿", "锂矿"),
        "industries": ("能源金属",),
        "direction": DIRECTION_BENEFIT,
    },
    "钴": {"keywords": ("钴", "硫酸钴", "氯化钴"), "industries": ("能源金属",), "direction": DIRECTION_BENEFIT},
    "镍": {"keywords": ("镍", "硫酸镍", "镍矿", "沪镍"), "industries": ("工业金属",), "direction": DIRECTION_BENEFIT},
    "稀土": {"keywords": ("稀土",), "industries": ("稀有金属",), "direction": DIRECTION_BENEFIT},
    "生猪": {"keywords": ("生猪",), "industries": ("养殖业",), "direction": DIRECTION_BENEFIT},
    "白糖": {"keywords": ("白糖",), "industries": ("农产品加工",), "direction": DIRECTION_BENEFIT},
    "棉花": {"keywords": ("棉花", "棉价"), "industries": ("纺织制造",), "direction": DIRECTION_COST},
    "豆粕": {"keywords": ("豆粕",), "industries": ("农产品加工",), "direction": DIRECTION_BENEFIT},
    "棕榈油": {"keywords": ("棕榈油",), "industries": ("农产品加工",), "direction": DIRECTION_BENEFIT},
    "天然橡胶": {"keywords": ("天然橡胶",), "industries": ("橡胶",), "direction": DIRECTION_COST},
}

# 「锂电」是新能源题材，不是碳酸锂商品行情：命中该词的快讯不算「锂」品种，
# 否则每条电池装机量快讯都会被贴成大宗品种。
_NEGATIVE_CONTEXT: dict[str, tuple[str, ...]] = {"锂": ("锂电",)}

# 供 `_NEWS_THEME_KEYWORDS` 复用的品种词表（保持 COMMODITY_CHAINS 顺序、去重）
VARIETY_KEYWORDS: tuple[str, ...] = tuple(
    dict.fromkeys(kw for chain in COMMODITY_CHAINS.values() for kw in chain["keywords"])
)

_MEMBER_TTL_S = 300.0
_members_cache: dict[str, tuple[float, list[str]]] = {}


def _clear_cache() -> None:
    """测试隔离/手动清理。"""
    _members_cache.clear()


def text_hits(text: str, keyword: str) -> bool:
    """关键词命中判定：中文原样包含，ASCII 词（PTA 等）大小写不敏感。"""
    if not text or not keyword:
        return False
    if keyword in text:
        return True
    return keyword.isascii() and keyword.upper() in text.upper()


def _chain_hits(text: str, commodity: str, chain: dict[str, Any]) -> bool:
    if not any(text_hits(text, kw) for kw in chain["keywords"]):
        return False
    return not any(neg in text for neg in _NEGATIVE_CONTEXT.get(commodity, ()))


def match_commodity_news(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出命中商品品种的快讯（只返回命中项，保持输入顺序）。

    返回每项：
        {"index": 原始下标, "commodity": 首个命中品种, "commodities": [品种…],
         "industries": [真实 TQ 行业名…], "direction": 首个品种方向, "directions": [方向…]}

    一条快讯可命中多个品种（『碳酸锂与纯碱价格齐涨』），行业按 COMMODITY_CHAINS 顺序去重。
    """
    hits: list[dict[str, Any]] = []
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        text = f"{item.get('title') or ''} {item.get('content') or ''}"
        matched = [name for name, chain in COMMODITY_CHAINS.items() if _chain_hits(text, name, chain)]
        if not matched:
            continue
        industries: list[str] = []
        for name in matched:
            for ind in COMMODITY_CHAINS[name]["industries"]:
                if ind not in industries:
                    industries.append(ind)
        hits.append(
            {
                "index": idx,
                "commodity": matched[0],
                "commodities": matched,
                "industries": industries,
                "direction": COMMODITY_CHAINS[matched[0]]["direction"],
                "directions": [COMMODITY_CHAINS[name]["direction"] for name in matched],
            }
        )
    return hits


def annotate_news_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """为命中条目补 `commodities` / `industries`（未命中条目原样保留，不加空字段）。"""
    rows = [dict(it) if isinstance(it, dict) else it for it in (items or [])]
    for hit in match_commodity_news(rows):
        row = rows[hit["index"]]
        row["commodities"] = list(hit["commodities"])
        row["industries"] = list(hit["industries"])
    return rows


def industry_members(industry: str) -> list[str] | None:
    """真实 TQ 行业名 → 成分股代码（如 造纸 → ['600103.SH', …]）。

    取数失败 / 目录里没有该行业名 / 成分返回空 → `None`（不返回 `[]` 冒充「板块无成分」）。
    """
    name = str(industry or "").strip()
    if not name:
        return None
    now = time.time()
    cached = _members_cache.get(name)
    if cached and now - cached[0] < _MEMBER_TTL_S:
        return list(cached[1])

    from src.core import tdx_boards

    try:
        sectors = tdx_boards.sector_items() or []
    except Exception as e:  # noqa: BLE001
        logger.warning("[商品链] TQ 板块目录不可用: %s", e)
        return None
    code = next(
        (str(s.get("code") or "").strip().upper() for s in sectors
         if isinstance(s, dict) and str(s.get("name") or "").strip() == name),
        None,
    )
    if not code:
        logger.warning("[商品链] TQ 目录无此行业名: %s", name)
        return None
    try:
        raw = tdx_boards.constituents(code)
    except Exception as e:  # noqa: BLE001
        logger.warning("[商品链] TQ 成分股不可用 %s(%s): %s", name, code, e)
        return None
    if not raw:
        logger.warning("[商品链] TQ 成分股为空, 按取数失败处理 %s(%s)", name, code)
        return None
    members = sorted({str(m).strip().upper() for m in raw if str(m).strip()})
    if not members:
        return None
    _members_cache[name] = (now, members)
    return list(members)


def beneficiaries(news_items: Iterable[dict[str, Any]]) -> dict[str, list[str]] | None:
    """快讯 → 受影响行业的真实成分股：`{"造纸": ["600103.SH", …], …}`。

    两步组合：`match_commodity_news`（品种）+ `industry_members`（TQ 成分）。
    - 没有任何商品命中 → `{}`（确实没有可传导的品种）；
    - 任一行业取数失败 → `None`（整条链路按不可用处理，不给半份名单）。
    """
    hits = match_commodity_news(news_items)
    if not hits:
        return {}
    industries: list[str] = []
    for hit in hits:
        for ind in hit["industries"]:
            if ind not in industries:
                industries.append(ind)
    out: dict[str, list[str]] = {}
    for ind in industries:
        members = industry_members(ind)
        if members is None:
            return None
        out[ind] = members
    return out


def news_commodity_tag(title: str, content: str = "") -> str:
    """新闻行商品注释：`' （商品：纸浆 → 造纸/包装印刷）'`；未命中返回空串。

    只依赖静态映射（不碰 TQ），因此 TQ 不可用时新闻行照样能标出品种→行业。
    """
    hits = match_commodity_news([{"title": title, "content": content}])
    if not hits:
        return ""
    commodities: list[str] = []
    industries: list[str] = []
    for hit in hits:
        for c in hit["commodities"]:
            if c not in commodities:
                commodities.append(c)
        for ind in hit["industries"]:
            if ind not in industries:
                industries.append(ind)
    return f" （商品：{'/'.join(commodities)} → {'/'.join(industries)}）"
