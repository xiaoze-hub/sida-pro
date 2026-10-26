"""腾讯排行（个股榜 / 板块榜）—— 免配额、零 key（2026-10-25 实测）。

来源：`proxy.finance.qq.com/cgi/cgi-bin/rank/{hs/getBoardRankList|pt/getRank}`

**为什么要它**：通达信问小达配额用尽（`MCP error -32603: Your usage quota has been reached`），
而"今日主力净流入前10""今日涨幅前10的概念板块"这类市场级排行是参谋的高频问法。
腾讯这个接口免费、实时、板块和个股都覆盖，字段比问小达还全。

**三条实测边界（别踩）**：
1. `count` 上限 **100**，传 500 直接 KeyError。
2. `sort_type` 是**白名单**，只有 `PriceRatio`(涨幅) / `turnover`(成交额) / `speed`(涨速) / `pn` 可用；
   传 `zdf`/`hsl`/`lb`/`zljlr`/`zsz` 会 KeyError 'data'（响应体里没有 data，不是 HTTP 错）。
3. 分页越界返回**空列表且不报错** → 终止条件必须是"这页不满 100 条"。

**主力净流入排序**：服务端不支持按 `zljlr` 排，但每条记录都带该字段 →
拉全量后本地排序（805 个概念 = 9 次请求）。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
_PAGE = 100
_MAX_PAGES = 12  # 概念 805 个 / 12 页 = 1200，留余量且防止无限翻页

# scope → (path, 固定参数)
_SCOPES: Dict[str, tuple] = {
    "stock": ("hs/getBoardRankList", {"board_code": "aStock"}),
    "industry": ("pt/getRank", {"board_type": "hy"}),
    "concept": ("pt/getRank", {"board_type": "gn"}),
    "region": ("pt/getRank", {"board_type": "dy"}),
}

# metric → 服务端 sort_type（None = 服务端不支持，必须本地排）
_SERVER_SORT = {"change": "PriceRatio", "amount": "turnover", "speed": "speed"}

SCOPE_LABEL = {"stock": "个股", "industry": "行业板块", "concept": "概念板块", "region": "地区板块"}
METRIC_LABEL = {"change": "涨跌幅", "amount": "成交额", "speed": "涨速", "main_inflow": "主力净流入"}


def _f(v: Any) -> Optional[float]:
    """腾讯所有数值都是字符串，且可能是 '-' → 统一成 float，坏值给 None。"""
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _fetch_page(path: str, params: Dict[str, str], sort_type: str, offset: int) -> List[dict]:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = (
        f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/{path}"
        f"?{q}&sort_type={sort_type}&direct=down&offset={offset}&count={_PAGE}"
    )
    req = urllib.request.Request(url, headers=_UA)
    raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    j = json.loads(raw)
    data = j.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"腾讯返回无 data（sort_type={sort_type} 可能在白名单外）")
    return list(data.get("rank_list") or [])


def _normalize(r: dict, *, is_board: bool) -> dict:
    leader = r.get("lzg") or {}
    return {
        "code": r.get("code"),
        "name": r.get("name"),
        "price": _f(r.get("zxj")),
        "change_pct": _f(r.get("zdf")),
        "change_amount": _f(r.get("zd")),
        "amplitude": _f(r.get("zf")),
        "turnover_rate": _f(r.get("hsl")),
        "volume_ratio": _f(r.get("lb")),
        "speed": _f(r.get("speed")),
        "amount": _f(r.get("turnover")),
        "main_inflow": _f(r.get("zljlr")),
        "circ_mv": _f(r.get("ltsz")),
        "total_mv": _f(r.get("zsz")),
        "change_5d": _f(r.get("zdf_d5")),
        "change_20d": _f(r.get("zdf_d20")),
        # 板块专属：涨/跌家数（**不是涨停家数**）与领涨股
        "up_down": r.get("zgb") if is_board else None,
        "leader": (
            {"name": leader.get("name"), "change_pct": _f(leader.get("zdf"))}
            if is_board and isinstance(leader, dict)
            else None
        ),
    }


def fetch_board_rank(scope: str = "stock", metric: str = "change", limit: int = 10) -> dict:
    """拉市场级排行。

    返回 `{available, reason, rows, caliber, metric, scope, count}`。
    失败**不抛**，返回 `available=False` + `reason`（参谋据此如实说"取不到"，不编 0 条）。
    """
    scope = (scope or "stock").strip().lower()
    metric = (metric or "change").strip().lower()
    caliber = "腾讯排行 · 实时快照 · 非复权"

    if scope not in _SCOPES:
        return {"available": False, "reason": f"不支持的 scope={scope}（可选 {'/'.join(_SCOPES)}）",
                "rows": [], "caliber": caliber, "scope": scope, "metric": metric, "count": 0}
    if metric not in METRIC_LABEL:
        return {"available": False, "reason": f"不支持的 metric={metric}（可选 {'/'.join(METRIC_LABEL)}）",
                "rows": [], "caliber": caliber, "scope": scope, "metric": metric, "count": 0}

    try:
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit = 10

    path, params = _SCOPES[scope]
    is_board = scope != "stock"
    server_sort = _SERVER_SORT.get(metric)

    try:
        if server_sort:
            # 服务端能排：只拉够 limit 的页
            rows: List[dict] = []
            offset = 0
            while len(rows) < limit and offset // _PAGE < _MAX_PAGES:
                page = _fetch_page(path, params, server_sort, offset)
                if not page:
                    break
                rows.extend(page)
                if len(page) < _PAGE:
                    break
                offset += _PAGE
            rows = rows[:limit]
        else:
            # 主力净流入：服务端不支持排序 → 拉全量本地排
            rows = []
            offset = 0
            while offset // _PAGE < _MAX_PAGES:
                page = _fetch_page(path, params, "PriceRatio", offset)
                if not page:
                    break
                rows.extend(page)
                if len(page) < _PAGE:
                    break
                offset += _PAGE
            rows.sort(key=lambda x: (_f(x.get("zljlr")) if _f(x.get("zljlr")) is not None else float("-inf")),
                      reverse=True)
            rows = rows[:limit]
    except Exception as e:  # 网络/解析失败：如实报错，绝不返回 0 条冒充"今天没有"
        logger.warning("腾讯排行拉取失败 scope=%s metric=%s: %s", scope, metric, e)
        return {"available": False, "reason": f"腾讯排行拉取失败: {type(e).__name__}: {e}",
                "rows": [], "caliber": caliber, "scope": scope, "metric": metric, "count": 0}

    if not rows:
        return {"available": False, "reason": "腾讯排行返回空列表（可能非交易时段或参数越界）",
                "rows": [], "caliber": caliber, "scope": scope, "metric": metric, "count": 0}

    out = [_normalize(r, is_board=is_board) for r in rows]
    return {
        "available": True,
        "reason": "",
        "rows": out,
        "caliber": caliber,
        "scope": scope,
        "metric": metric,
        "count": len(out),
        "scope_label": SCOPE_LABEL.get(scope, scope),
        "metric_label": METRIC_LABEL.get(metric, metric),
    }
