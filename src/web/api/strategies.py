"""策略库 API: 列出/查看/应用借鉴 alphasift 的 YAML 策略到单只股票。

设计原则:
- 策略只用到可拿到的字段(实时或盘后)
- 单只股票评分(快速) + 全市场扫描(慢, 盘后)
- 字段缺失时显式标注, 不静默跳过
"""
import logging
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.web.api.auth import get_current_user
from src.web.models import User

from src.core.strategy_library import (  # noqa: F401  (W4.2: 实现下沉 core, 此处再导出)
    _evaluate_strategy,
    _quote_to_dict,
    effective_config,
    rounding_safe,
    strategy_params,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# 策略 YAML 路径(借鉴 alphasift 的格式, 翻译为 PanWatch 可用字段子集)
STRATEGIES_FILE = Path(__file__).parent.parent.parent.parent / "strategies" / "panwatch_strategies.yaml"


def _load_strategies() -> dict:
    if not STRATEGIES_FILE.exists():
        raise HTTPException(503, f"策略文件不存在: {STRATEGIES_FILE}")
    try:
        data = yaml.safe_load(STRATEGIES_FILE.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        raise HTTPException(500, f"策略文件解析失败: {e}")


@router.get("/list")
async def list_strategies():
    """列出所有可用策略。"""
    data = _load_strategies()
    completeness = data.get("data_completeness", {})

    items = []
    for key, cfg in data.items():
        if key == "data_completeness" or not isinstance(cfg, dict):
            continue
        strategy_data_status = completeness.get("strategy_data_status", {}).get(key, {})
        items.append({
            "id": key,
            "display_name": cfg.get("display_name", key),
            "description": cfg.get("description", ""),
            "category": cfg.get("category", "other"),
            "tags": cfg.get("tags", []),
            "ui_badge": cfg.get("ui_badge", ""),
            "source": cfg.get("source", ""),
            "filter": cfg.get("filter", {}),
            "eod_fields": list(_eod_fields(cfg)),  # 需要的盘后字段
            "params": strategy_params(cfg),         # 可编辑参数(前端据此自动渲染表单)
            "data_window": strategy_data_status.get("available_in", "realtime"),
            "available_now": strategy_data_status.get("available_in", "realtime") == "realtime",
        })
    return {"items": items, "total": len(items)}


@router.get("/{strategy_id}")
async def get_strategy(strategy_id: str):
    """查看单个策略详情。"""
    data = _load_strategies()
    if strategy_id not in data:
        raise HTTPException(404, f"策略不存在: {strategy_id}")
    cfg = data[strategy_id]
    completeness = data.get("data_completeness", {}).get("strategy_data_status", {}).get(strategy_id, {})
    return {
        "id": strategy_id,
        "display_name": cfg.get("display_name", strategy_id),
        "description": cfg.get("description", ""),
        "category": cfg.get("category", "other"),
        "tags": cfg.get("tags", []),
        "filter": cfg.get("filter", {}),
        "ranking_factors": cfg.get("ranking_factors", {}),
        "eod_only_fields": list(_eod_fields(cfg)),
        "params": strategy_params(cfg),
        "ui_badge": cfg.get("ui_badge", ""),
        "source": cfg.get("source", ""),
        "data_window": completeness.get("available_in", "realtime"),
    }


class ApplyRequest(BaseModel):
    strategy_id: str
    symbol: str
    market: str = "CN"
    # 参数覆盖(键须是该策略 params 声明过的阈值), 未声明的键会被静默丢弃
    overrides: dict[str, float] = Field(default_factory=dict)


class ScanRequest(BaseModel):
    strategy_id: str
    market: str = "CN"
    limit: int = 50          # 返回 top N
    universe: str = "all"    # all=全市场, watchlist=自选+种子池
    min_score: float = 0.0   # 最低分过滤
    symbol_limit: int = 0    # 0=不限, 否则限制扫描股票数(调试用)
    # 自定义股票池(2026-08-22 共振查询): 传入则优先于 universe,
    # 只扫这几只(如 问小达+问财 合并后的候选), 上限 100 防滥用
    symbols: list[str] = Field(default_factory=list)
    # 参数覆盖(与 apply 同规则): 只认该策略 params 声明过的阈值键
    overrides: dict[str, float] = Field(default_factory=dict)


@router.post("/scan")
async def scan_strategy(
    req: ScanRequest,
    user: User = Depends(get_current_user),
):
    """批量选股: 用策略硬过滤扫描全市场/候选池, 返回通过名单(按分数排序)。

    - universe=all: 全市场 A 股(优先缓存列表, 东财/akshare 兜底)
    - universe=watchlist: 自选 + 内置种子池(快, ~200 只)
    - symbols 非空: 只扫传入的自定义股票池(共振查询精筛用, ≤100 只)
    - 行情走腾讯批量接口(免费, 100 只/批, 盘中含 PE/PB/市值全字段)
    """
    from src.web.stock_list import get_stock_list
    from marketdata.vendors.tencent import TencentQuoteVendor
    from marketdata import Symbol

    data = _load_strategies()
    if req.strategy_id not in data:
        raise HTTPException(404, f"策略不存在: {req.strategy_id}")
    cfg = effective_config(data[req.strategy_id], req.overrides)

    # 1. 确定扫描股票池
    mkt = (req.market or "CN").strip().upper()
    if req.symbols:
        # 自定义股票池(共振查询): 只保留 6 位数字码, 去重, 截断 100
        symbols = list(dict.fromkeys(s.strip() for s in req.symbols if str(s).strip().isdigit()))
        symbols = [s for s in symbols if s.startswith(("60", "00", "30", "68"))][:100]
    elif req.universe == "watchlist":
        from src.web.database import SessionLocal
        from src.web.models import Stock
        # C3(2026-09-09): 自选池按归属过滤(自己的 + 全局), 此前拉全库所有用户自选
        db = SessionLocal()
        try:
            rows = (
                db.query(Stock)
                .filter(
                    (Stock.user_id == user.id) | (Stock.user_id.is_(None))
                )
                .all()
            )
        finally:
            db.close()
        symbols = [str(s.symbol).strip() for s in rows if str(s.market) == mkt]
        # 内置种子池补充
        from src.core.entry_candidates import MARKET_SCAN_SEED_SYMBOLS
        symbols += [s for s in MARKET_SCAN_SEED_SYMBOLS.get(mkt, []) if s not in symbols]
    else:
        all_stocks = get_stock_list()
        symbols = []
        for s in all_stocks:
            code = str(s.get("symbol") or s.get("code") or "").strip()
            market = str(s.get("market") or s.get("market_code") or "").strip().upper()
            if not code or not code.isdigit():
                continue
            if market != mkt:
                # 兼容缓存里 market 是中文或缺失: A股默认 CN
                if mkt == "CN" and market in ("", "A股", "CN", "SH", "SZ"):
                    pass
                else:
                    continue
            # 只要沪深主板+创业板 6位代码(排除北交所 4/8 开头)
            if mkt == "CN" and not code.startswith(("60", "00", "30", "68")):
                continue
            symbols.append(code)
        symbols = list(dict.fromkeys(symbols))

    if req.symbol_limit and req.symbol_limit > 0:
        symbols = symbols[: req.symbol_limit]
    if not symbols:
        return {"items": [], "total": 0, "scanned": 0, "message": "股票池为空"}

    # 2. 腾讯批量行情(100只/批)
    vendor = TencentQuoteVendor()
    quote_map: dict[str, dict] = {}
    batch_size = 100
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        try:
            syms = [Symbol.parse(c, mkt) for c in batch]
            quotes = vendor.fetch(syms, {})
            for q in quotes:
                d = _quote_to_dict(q)
                quote_map[str(q.symbol)] = d
        except Exception as e:
            logger.warning(f"[scan] 批量行情失败 {i}..{i+batch_size}: {e}")

    # 3. 逐只评估
    results = []
    for code in symbols:
        q = quote_map.get(code)
        if not q or not q.get("current_price"):
            continue
        r = _evaluate_strategy(cfg, q, req.strategy_id, code, mkt)
        if r["passed"] and r["score"] >= req.min_score:
            results.append(r)

    # 4. 按分数排序, 取 top N
    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[: req.limit]
    return {
        "items": [
            {
                "symbol": r["symbol"],
                "name": quote_map.get(r["symbol"], {}).get("name", ""),
                "market": r["market"],
                "score": r["score"],
                "score_breakdown": r["score_breakdown"],
                "current_data": r["current_data"],
                "missing_fields": r["missing_fields"],
            }
            for r in top
        ],
        "total": len(results),
        "scanned": len(symbols),
        "quoted": len(quote_map),
    }


@router.post("/apply")
async def apply_strategy(req: ApplyRequest):
    """应用策略到单只股票: 硬过滤 + 因子打分。

    用现有 quotes 拿实时字段; 盘后字段如果有则用, 没有则跳过对应过滤项。
    """
    from src.core.marketdata_client import get_market_data

    data = _load_strategies()
    if req.strategy_id not in data:
        raise HTTPException(404, f"策略不存在: {req.strategy_id}")
    cfg = effective_config(data[req.strategy_id], req.overrides)

    # 拉取股票行情(用 quotes, 通用接口)
    try:
        quotes = get_market_data().quotes([req.symbol], market=req.market)
    except Exception as e:
        logger.warning(f"拉取行情失败 {req.symbol}: {e}")
        quotes = []

    q = _quote_to_dict(quotes[0]) if quotes else None
    if not q or not q.get("current_price"):
        raise HTTPException(404, f"未找到行情: {req.symbol} ({req.market})")

    return _evaluate_strategy(cfg, q, req.strategy_id, req.symbol, req.market)


def _eod_fields(cfg: dict) -> set:
    """从 filter + 顶层 字段里识别哪些是盘后字段。"""
    eod_keys = {"pe_ttm_max", "pe_ttm_min", "pb_max", "pb_min", "market_cap_min", "market_cap_max"}
    f = cfg.get("filter", {})
    found = {k for k in f if k in eod_keys}
    # 顶层也可能有(dual_low 把 pe_ttm_max 放在 cfg 顶层)
    for k in cfg:
        if k in eod_keys:
            found.add(k)
    return found
