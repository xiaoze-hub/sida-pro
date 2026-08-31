"""决策先锋选股池 API(盘中实时, 2026-08-30)。

POST /api/stock-pool/screen
请求体: {"symbols": ["002361", "600519", ...]}
→ 对每只股票批量算决策先锋三指标(GS + 机构活跃度 + L2主力净流入),
  按"共振强度"排序返回。

共振判定(买入方向):
  趋势分: gs.state == "G区" 或 gs.signal == "G"
  强度分: institution_activity.level in ("大牛","强势") 或 activity >= 3
  资金分: l2.available 且 zjl_hb > 0
  三项全满足="强", 满足两项="弱", 否则="无"。

进程内 30s 缓存; 单只失败标"无"不阻塞整批; 数据缺失显式 None 不编造。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_SYMBOLS = 50
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 30.0


class StockPoolScreenRequest(BaseModel):
    symbols: list[str] = Field(..., description="6位A股代码列表, 最多50只")


def _valid_symbols(raw: list[str]) -> list[str]:
    out: list[str] = []
    for s in raw:
        code = (s or "").strip()
        if code.isdigit() and len(code) == 6 and code not in out:
            out.append(code)
    return out[:_MAX_SYMBOLS]


def _resonance(gs, act, l2) -> tuple[str, int]:
    """返回 (共振级别, 得分 0-2)。

    2026-08-30 调整: GS 只做"趋势过滤"不做"买卖触发"——日线均线交叉的
    signal(G/S点)滞后一天(知乎第三方实测"套都被套死了才提示"), 实战无价值;
    只认 state(G区/S区)作方向过滤, S区/无GS 直接不共振。买卖强度看活跃度+L2资金。
    """
    # GS 趋势过滤: 只认 G区(趋势向上), S区/无GS 直接过滤
    if not (gs and gs.get("state") == "G区"):
        return "无", 0
    score = 0
    # 强度分: AI机构活跃度 ≥ 强势线3
    if act:
        level = act.get("level")
        activity = act.get("activity")
        if level in ("大牛", "强势") or (isinstance(activity, (int, float)) and activity >= 3):
            score += 1
    # 资金分: L2 主力净流入 > 0
    if l2 and l2.get("available") and isinstance(l2.get("zjl_hb"), (int, float)) and l2["zjl_hb"] > 0:
        score += 1
    if score >= 2:
        return "强", 2
    if score == 1:
        return "弱", 1
    return "无", 0


def _screen(symbols: list[str]) -> dict:
    from src.core.decision_pioneer import fetch_decision_pioneer

    rows: list[dict] = []
    truncated = False
    if len(symbols) > _MAX_SYMBOLS:
        truncated = True
        symbols = symbols[:_MAX_SYMBOLS]

    for sym in symbols:
        try:
            d = fetch_decision_pioneer(sym, "CN")
        except Exception as e:  # noqa: BLE001
            logger.warning("stock-pool screen %s failed: %s", sym, e)
            d = None
        if not d:
            rows.append({
                "symbol": sym,
                "activity": None,
                "activity_level": None,
                "gs_state": None,
                "gs_signal": None,
                "l2_net": None,
                "resonance": "无",
                "score": 0,
            })
            continue
        act = d.get("institution_activity")
        gs = d.get("gs")
        l2 = d.get("l2") or {}
        level, score = _resonance(gs, act, l2)
        zjl = l2.get("zjl_hb")
        rows.append({
            "symbol": sym,
            "activity": round(act["activity"], 2) if act and isinstance(act.get("activity"), (int, float)) else None,
            "activity_level": act.get("level") if act else None,
            "gs_state": gs.get("state") if gs else None,
            "gs_signal": gs.get("signal") if gs else None,
            "l2_net": round(zjl / 1e4, 0) if isinstance(zjl, (int, float)) else None,  # 万元
            "resonance": level,
            "score": score,
        })

    # 排序: resonance(强>弱>无) → score 降序 → symbol 升序
    order = {"强": 0, "弱": 1, "无": 2}
    rows.sort(key=lambda r: (order.get(r["resonance"], 2), -r["score"], r["symbol"]))
    return {"rows": rows, "total": len(rows), "truncated": truncated}


@router.post("/screen")
def screen_stock_pool(req: StockPoolScreenRequest):
    """决策先锋选股池共振扫描(批量, 盘中实时)。"""
    symbols = _valid_symbols(req.symbols)
    if not symbols:
        return {"rows": [], "total": 0, "truncated": False}
    key = ",".join(symbols)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = _screen(symbols)
    _CACHE[key] = (now, data)
    return data
