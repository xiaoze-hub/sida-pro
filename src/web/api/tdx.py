"""通达信问小达 API。

数据源: 通达信 MCP 问小达(tdx_wenda_quotes), 经 TDX MCP endpoint 直连。
覆盖: 个股行情 / 智能选股 / 板块排行 / 财务 / 技术 / 资金流向(自然语言问答)。

鉴权: 环境变量 TDX_API_KEY(Bearer token), 由容器 env 注入, 不写进代码。
数据源注册在 data_sources 表(type=wenda, provider=tdx)。
本接口直接调用 marketdata 的 tdx vendor, 不进 MarketData Engine(问小达是问答型,
返回表格, 与个股 symbol 模型不同)。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query


logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ask")
async def tdx_ask(
    q: str = Query(..., description="自然语言查询, 如 '近5日主力净流入前10的半导体'"),
    max_rows: int = Query(50, description="返回最大行数, 0=不截断"),
):
    """通达信问小达自然语言投研问答。"""
    if not q or not q.strip():
        raise HTTPException(400, "q 不能为空")

    try:
        from marketdata.vendors.tdx import ask_wenda

        # 热修 2026-08-14: 同步网络调用包 to_thread, 防阻塞 asyncio 事件循环(登录超时根因)
        result = await asyncio.to_thread(ask_wenda, q.strip(), config=None)
    except Exception as e:
        logger.warning(f"通达信问小达调用失败: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")

    if not result:
        raise HTTPException(502, "通达信问小达无返回(可能 key 无效或服务不可用)")

    meta = result.get("meta") or {}
    if meta.get("code") not in (0, None):
        raise HTTPException(502, f"通达信问小达返回错误码: {meta.get('code')}")

    headers = result.get("headers") or []
    data = result.get("data") or []
    total = meta.get("total") or len(data)

    if max_rows and max_rows > 0 and len(data) > max_rows:
        data = data[:max_rows]

    # 查询结果入池(P1 整合): 供当日候选池共振计分, 静默失败不影响查询
    try:
        from src.core.entry_candidates import record_manual_query_candidates

        items = []
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = ""
            for key in ("代码", "股票代码", "symbol", "code"):
                raw = str(row.get(key) or "").strip()
                if raw:
                    sym = raw.split(".")[0].replace("USZA", "").replace("USHA", "")
                    break
            if not sym or not sym.isdigit() or len(sym) != 6:
                continue
            name = ""
            for key in ("名称", "股票简称", "name"):
                if row.get(key):
                    name = str(row[key])
                    break
            items.append({"symbol": sym, "market": "CN", "name": name})
        record_manual_query_candidates(kind="tdx", query_text=q, items=items)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"tdx 查询结果入池跳过: {e}")

    return {
        "query": q,
        "total": total,
        "returned": len(data),
        "headers": headers,
        "rows": data,
    }
