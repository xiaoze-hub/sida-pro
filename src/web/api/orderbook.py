# -*- coding: utf-8 -*-
"""OB 失衡条 轻接口(分时卡片用, 2026-08-20)。

GET /api/orderbook-ob?symbol=USZA002361
GET /api/orderbook-ob/USZA002361
→ 响应包装后 {code:0, success:true, data: {available, ob_series, events, ghost_ratio, note}}

- available   : bool 数据是否可用(thsdk 缺失/取数失败/数据为空 → False, 不伪造)
- ob_series   : 盘口失衡序列(每快照一个), 每项含 {ob, label, bid_amt10, ask_amt10, ts, dt}
- events      : 盘口演变事件(托单/压单/撤单/幽灵单)
- ghost_ratio : 幽灵单比率
- note        : 真实说明(时段/数据状态), 取数失败时给出原因

依赖: src.core.orderbook_engine.run()(THS L2 真实盘口, 20 档)。
thsdk 未安装时引擎模块级软依赖自动置 None, 本接口返回 available:false + 真实原因。
"""
import logging

from fastapi import APIRouter, Query

from src.core import orderbook_engine

logger = logging.getLogger(__name__)

router = APIRouter()

# 轻接口快照参数: 卡片每 30s 刷新, 采集 5 个快照(间隔 0.3s) ≈ 2-3s 完成, 避免阻塞卡片
_N_SNAPSHOTS = 5
_INTERVAL_S = 0.3


def _normalize_symbol(raw: str) -> str:
    """兼容 THS 代码(USZA002361)与裸 6 位代码(002361 → USZA/USSH)。"""
    code = (raw or "").strip()
    if not code:
        return code
    if code.isdigit() and len(code) == 6:
        return f"USSH{code}" if code[0] == "6" else f"USZA{code}"
    return code


def get_orderbook_ob(
    symbol: str,
    n_snapshots: int = _N_SNAPSHOTS,
    interval: float = _INTERVAL_S,
) -> dict:
    """OB 失衡条纯函数: 调 orderbook_engine.run 取真实 THS L2 盘口, 组装卡片数据。

    任何失败(thsdk 缺失 / 取数失败 / 数据为空)都返回 available:false + 真实 note,
    绝不伪造盘口数据。
    """
    ths_code = _normalize_symbol(symbol)
    if not ths_code:
        return {
            "available": False,
            "ob_series": [],
            "events": [],
            "ghost_ratio": 0.0,
            "note": "symbol 为空, 无法取盘口数据",
        }

    # thsdk 缺失: 引擎模块级软依赖 THS=None, 这里显式预检给出明确原因
    if orderbook_engine.THS is None:
        return {
            "available": False,
            "ob_series": [],
            "events": [],
            "ghost_ratio": 0.0,
            "note": "thsdk 未安装, 无法拉取真实 L2 盘口(OB 失衡条不可用)",
        }

    try:
        result = orderbook_engine.run(ths_code, n_snapshots=n_snapshots, interval=interval)
    except ImportError as e:
        # thsdk 运行时导入失败(部分环境半安装): 不伪造, 如实上报
        return {
            "available": False,
            "ob_series": [],
            "events": [],
            "ghost_ratio": 0.0,
            "note": f"thsdk 导入失败, 无法取盘口: {e}",
        }
    except Exception as e:
        logger.warning(f"orderbook-ob 取数异常 {ths_code}: {e}")
        return {
            "available": False,
            "ob_series": [],
            "events": [],
            "ghost_ratio": 0.0,
            "note": f"盘口取数失败: {e}",
        }

    ob_series = result.get("ob_series") or []
    if not ob_series:
        return {
            "available": False,
            "ob_series": [],
            "events": result.get("events") or [],
            "ghost_ratio": result.get("ghost_ratio", 0.0),
            "note": "盘口无数据(非交易时段或无成交), 未产出 OB 序列",
        }

    return {
        "available": True,
        "ob_series": ob_series,
        "events": result.get("events") or [],
        "ghost_ratio": result.get("ghost_ratio", 0.0),
        "note": result.get("summary") or "OK",
    }


@router.get("")
def orderbook_ob(symbol: str = Query(..., description="THS 代码(如 USZA002361)或 6 位代码(如 002361)")):
    """OB 失衡条(轻接口, 分时卡片用): 买|卖压比例 + 盘口演变事件 + 幽灵单比率。"""
    return get_orderbook_ob(symbol)


@router.get("/{symbol}")
def orderbook_ob_path(symbol: str):
    """路径式别名: /api/orderbook-ob/USZA002361。"""
    return get_orderbook_ob(symbol)