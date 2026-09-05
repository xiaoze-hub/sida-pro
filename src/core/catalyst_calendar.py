"""未来催化日历(2026-09-05, v0.5.6 CKPT1)。

聚合三类未来催化, 统一输出 [{date, type, symbol, title, detail}],
按 date 升序。分层降级: 任一源失败只记日志, 不阻塞其他源。

源:
1. 解禁(未来 N 天): 东财 push2 clist(字段 f12 代码/f14 名称/f26 解禁日期?
   以生产实测为准, 解析失败即整源降级) → 降级: 悟道 MCP unlock_events
   (有 token 才试) → 再降级: 空列表。
2. 除权除息(未来 N 天): 东财 datainterface(302/解析失败即降级为空)。
3. 静态窗口: data/catalyst_windows.json(宏观/会议/财报季, 手工维护)。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

CALENDAR_DAYS_DEFAULT = 30
_WINDOWS_FILE = os.path.join(os.path.dirname(__file__), "catalyst_windows.json")


def _load_static_windows(days: int, today: date) -> list[dict]:
    """静态政策/会议/财报季窗口。文件缺失/非法 → 空列表。"""
    try:
        with open(_WINDOWS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug(f"静态催化窗口加载失败: {e}")
        return []
    out = []
    end = today + timedelta(days=days)
    for w in data.get("windows", []):
        try:
            d = datetime.strptime(w["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if today <= d <= end:
            out.append({
                "date": d.isoformat(),
                "type": "宏观窗口",
                "symbol": "",
                "title": w.get("title", ""),
                "detail": f"级别{w.get('level', '')};" + ",".join(w.get("sectors", []) or ["全市场"]),
            })
    return out


def _fetch_unlock_eastmoney(days: int, today: date) -> list[dict]:
    """东财解禁(直连, 本机/容器网络不通或解析失败 → 抛异常由上游降级)。"""
    from src.collectors.market_http import market_get

    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?fid=f26&po=1&pz=100&pn=1&np=1&fltt=2&invt=2"
        "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        "&fields=f1,f2,f3,f12,f13,f14,f26,f38,f62"
    )
    headers = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    data = market_get(url, host_key="push2.eastmoney.com", headers=headers, timeout=12, parse="json")
    diff = (data or {}).get("data", {}).get("diff", [])
    out = []
    end = today + timedelta(days=days)
    for row in diff:
        code = str(row.get("f12", ""))
        name = str(row.get("f13", row.get("f14", "")))
        raw_date = str(row.get("f26", ""))  # 候选: 解禁日期字段, 生产实测校准
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= end and code:
            out.append({
                "date": d.isoformat(),
                "type": "解禁",
                "symbol": code,
                "title": f"{name}({code})解禁",
                "detail": f"解禁日期{d.isoformat()}",
            })
    return out


def _fetch_unlock_wudao(days: int, today: date) -> list[dict]:
    """悟道 MCP 解禁(有 token 才试, 无 token/失败 → 空列表)。"""
    try:
        from src.collectors.wudao_mcp_client import WudaoMCPClient

        cli = WudaoMCPClient()
        res = cli.call_tool("unlock_events", {"days": days})
        items = (res or {}).get("data", res.get("items", [])) if isinstance(res, dict) else []
    except Exception as e:
        logger.debug(f"悟道解禁获取失败: {e}")
        return []
    out = []
    end = today + timedelta(days=days)
    for it in items or []:
        try:
            d = datetime.strptime(str(it.get("date", ""))[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= end:
            out.append({
                "date": d.isoformat(),
                "type": "解禁",
                "symbol": str(it.get("symbol", "")),
                "title": str(it.get("title", "解禁")),
                "detail": str(it.get("detail", "")),
            })
    return out


def _fetch_exrights_eastmoney(days: int, today: date) -> list[dict]:
    """东财除权除息(直连, 失败 → 抛异常由上游降级)。"""
    from src.collectors.market_http import market_get

    url = (
        "https://datainterface.eastmoney.com/EM_DataCenter/JS.aspx"
        "?type=NS&sty=NSST&st=1&sr=-1&p=1&ps=100&jsm=&js={pages:(tp),data:[(x)]}"
    )
    headers = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    _ = market_get(url, host_key="datainterface.eastmoney.com", headers=headers, timeout=12, parse="text")
    # datainterface 常 302/变结构: 本期只探通, 解析放到生产实测后补
    logger.debug("除权接口探通待解析, 本期降级为空")
    return []


def get_calendar(days: int = CALENDAR_DAYS_DEFAULT, today: date | None = None) -> list[dict]:
    """未来催化日历主入口。永远返回列表(可为空), 不抛异常。"""
    today = today or date.today()
    out: list[dict] = []
    try:
        out.extend(_fetch_unlock_eastmoney(days, today))
    except Exception as e:
        logger.debug(f"东财解禁降级: {e}")
        out.extend(_fetch_unlock_wudao(days, today))
    try:
        out.extend(_fetch_exrights_eastmoney(days, today))
    except Exception as e:
        logger.debug(f"东财除权降级: {e}")
    out.extend(_load_static_windows(days, today))
    out.sort(key=lambda x: x["date"])
    return out
