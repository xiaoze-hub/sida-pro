"""受益名 → 交易代码落地(2026-09-05, v0.5.6 CKPT4)。

把 LLM 输出的受益池名字(个股名/产业链名/题材名)落到可交易 symbol。
三级匹配, 置信度递减; fuzzy 低分只标注不进榜(宁可漏, 防幻觉下单依据污染)。

- exact: 名单精确命中(含代码直给) → 置信度 高
- sector: 题材/板块名 → 成分股(取前 10, 需上游再过滤) → 置信度 中
- fuzzy: difflib 名字近似(≥0.6) → 置信度 低, 标待确认
"""

from __future__ import annotations

import difflib
import logging
import re

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"\b([0368]\d{5})\b")
_FUZZY_CUTOFF = 0.6
_SECTOR_TOPN = 10


def _stock_index() -> dict[str, str]:
    """name → symbol(失败 → 空 dict, 上游降级)。"""
    try:
        from src.web.stock_list import get_stock_list

        return {s["name"]: s["symbol"] for s in get_stock_list() if s.get("name") and s.get("symbol")}
    except Exception as e:
        logger.debug(f"股票名单加载失败: {e}")
        return {}


def resolve_beneficiaries(names: list[str]) -> list[dict]:
    """受益名落代码。返回 [{name, symbol, via, confidence}]。

    via ∈ {code, exact, sector, fuzzy}。fuzzy 仅保留 ≥0.6 的首个, confidence=低。
    sector 展开取前 10 只成分, confidence=中, name 保留原始题材名。
    """
    out: list[dict] = []
    index = _stock_index()
    all_names = list(index)
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        m = _CODE_RE.search(name)
        if m:
            out.append({"name": name, "symbol": m.group(1), "via": "code", "confidence": "高"})
            continue
        if name in index:
            out.append({"name": name, "symbol": index[name], "via": "exact", "confidence": "高"})
            continue
        try:
            from src.core.sector_filter import resolve_sector_codes

            codes = resolve_sector_codes(name)[:_SECTOR_TOPN]
        except Exception:
            codes = []
        if codes:
            for c in codes:
                out.append({"name": name, "symbol": c, "via": "sector", "confidence": "中"})
            continue
        hit = difflib.get_close_matches(name, all_names, n=1, cutoff=_FUZZY_CUTOFF)
        if hit:
            out.append({"name": name, "symbol": index[hit[0]], "via": "fuzzy", "confidence": "低"})
        else:
            logger.debug(f"受益名无法落地: {name}")
    return out
