"""从 PanWatch 主后端(8000, 容器内 marketdata/tdx 数据源)获取主力资金数据。

为什么不用 8010 直连:
- 8010 宿主机没有 marketdata 包(在 8000 容器内)
- zhitu MCP 的"主买-主卖"口径和东财主力净流入口径差 3 倍以上(已核实)

方案:
- 复用 8000 已有的 tdx ask API(走容器内 marketdata/tdx, 东财口径, 准确可核对)
- 不改 8000 数据源逻辑, 只 HTTP 读取已有接口
- **用 tdx-main-position skill 模板**(2026-08-09 实测比口语化多拿 2 列:机构持股总量+机构数量),
  见 https://www.tdx.com.cn/skillhub/ §21。skill 模板是问小达经过训练的固定 prompt,
  MCP 层仍走 tdx_screener,只是 message 用更精准的 skill 触发句。

依赖: `PANWATCH_URL` 指向健康可访问的 PanWatch 主后端。
"""
from __future__ import annotations

import logging
from typing import Optional

try:
    from .panwatch_client import request_json
except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
    from panwatch_client import request_json

logger = logging.getLogger(__name__)


def _tdx_ask(query: str) -> Optional[dict]:
    """调 8000 tdx ask, 返回结构化 rows/headers 或 None。"""
    try:
        data = request_json(f"/api/tdx/ask?q={urllib.parse.quote(query)}", timeout=25)
        return data.get("data")
    except Exception as e:
        logger.warning(f"tdx ask 失败 [{query}]: {e}")
        return None


def _parse_main_net(d) -> Optional[float]:
    """从 tdx ask 返回里解析'主力净额'字段(单位元)。

    headers 形如 ['主力净额<br>2026.08.070#', ...], 用 '主力净额' 子串匹配。
    """
    if not d:
        return None
    rows = d.get("rows") or []
    if not rows:
        return None
    headers = d.get("headers") or []
    # 找含'主力净额'的列
    col = None
    for h in headers:
        if "主力净额" in str(h):
            col = h
            break
    if not col:
        return None
    val = rows[0].get(col)
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _parse_extra_columns(d) -> dict:
    """解析 tdx-main-position skill 模板多返回的列(机构持股总量/机构数量)。
    列名都是 '主力净额<br>DATE#' 之外的中文 key,可能缺。
    """
    if not d:
        return {}
    rows = d.get("rows") or []
    if not rows:
        return {}
    headers = d.get("headers") or []
    out = {}
    for target in ("机构持股总量", "机构总量"):
        for h in headers:
            if target in str(h):
                val = rows[0].get(h)
                try:
                    out[target] = float(val)
                except (TypeError, ValueError):
                    out[target] = None
                break
    return out


def fetch_capital_flow(symbol: str, days: int = 5) -> list:
    """获取主力资金数据(经 PanWatch 8000, tdx-main-position skill 模板)。

    返回 list of dict(单元素或聚合), 结构:
      [{
        "date": "近5日" / 当日日期,
        "main_net": float,        # 主力净流入(元)
        "institution_hold": float|None,  # 机构持股总量(股), skill 模板特有
        "institution_count": float|None, # 机构数量(家), skill 模板特有
        "source": "panwatch-tdx",
        "note": "tdx-main-position skill(东财口径主力净额+机构持仓)"
      }]
    """
    out = []
    # 用 tdx-main-position skill 触发模板(SKILL.md §21),实测比口语化多 2 列
    # "002361的主力资金流向和机构持股"
    d_today = _tdx_ask(f"{symbol}的主力资金流向和机构持股")
    today_net = _parse_main_net(d_today)
    if today_net is not None:
        # 尝试从 header 拿日期(格式如 "主力净额<br>2026.08.070#")
        date_label = "当日"
        headers = (d_today or {}).get("headers") or []
        import re
        for h in headers:
            if "主力净额" in str(h) and "20" in str(h):
                m = re.search(r"20\d{2}\.\d{2}\.\d{2}", str(h))
                if m:
                    date_label = m.group(0)
                break
        extras = _parse_extra_columns(d_today)
        item = {
            "date": date_label,
            "main_net": today_net,
            "source": "panwatch-tdx",
            "note": "tdx-main-position skill(东财口径主力净额+机构持仓)",
        }
        if extras:
            item.update(extras)
        out.append(item)
    # 近 N 日合计(趋势) — 仍走原口语化查询(模板没有 5 日聚合版)
    d_5 = _tdx_ask(f"{symbol} 近五日主力净流入")
    net_5 = _parse_main_net(d_5)
    if net_5 is not None:
        out.append({
            "date": f"近{days}日",
            "main_net": net_5,
            "source": "panwatch-tdx",
            "note": "东财口径(近5日合计)",
        })
    return out


import urllib.parse  # noqa: E402  (放末尾避免循环 import 顺序问题)
