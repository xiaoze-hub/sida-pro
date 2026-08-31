"""8010 接入 PanWatch 市场数据源(龙虎榜) — 经 8000 HTTP 代理端点。

设计原则(符合"所有 key 在设置→接口Key 维护"的要求):
- 不直接连 marketdata 包/读 DB 副本(那样 key 不实时)
- 改走 8000 的 /api/market-data/dragon-tiger/{date} 端点
  该端点内部用 marketdata ftshare vendor, config=None → 自动从容器 DB 的
  data_sources 表读「设置→接口Key」配置的 key(改了立即生效, 无需重启)
- 这样 8010 永远拿到 UI 维护的最新 key, 国内外服务器只要 UI 配好源即可

认证: 8000 端点带 protected 依赖(需 Bearer token)，Compose 模式使用
PanWatch 共享数据库签发的短时 Token，不保存用户密码。
"""
from __future__ import annotations

import logging

try:
    from .panwatch_client import request_json
except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
    from panwatch_client import request_json

logger = logging.getLogger(__name__)

# 进程内缓存(龙虎榜日频, 避免重复请求)
_DT_CACHE: dict = {}


def _http_get(path: str) -> dict | None:
    return request_json(path, timeout=30)


def get_dragon_tiger(date: str | None = None, symbol: str | None = None) -> list:
    """获取龙虎榜(经 8000 /api/market-data/dragon-tiger, key 来自 UI 配置)。

    date: YYYYMMDD, 不传则用最近一个交易日(周五回退)
    返回 list of dict:
      [{trade_date, symbol, name, close, change_pct, net_buy, buy_amt, sell_amt, on_list(bool)}]
    """
    if date is None:
        date = _latest_trade_date()
    if date in _DT_CACHE:
        items = _DT_CACHE[date]
    else:
        try:
            resp = _http_get(f"/api/market-data/dragon-tiger/{date}")
            raw = resp.get("data", resp) if resp else {}
            items = raw.get("items", []) if isinstance(raw, dict) else []
            _DT_CACHE[date] = items
            logger.info(f"龙虎榜({date})经8000获取 {len(items)} 条")
        except Exception as e:
            logger.warning(f"龙虎榜经8000获取失败 [{date}]: {e}")
            return []

    if symbol:
        sym_norm = symbol.replace(".SZ", "").replace(".SH", "")
        hit = [r for r in items if r.get("symbol", "").replace(".SZ", "").replace(".SH", "") == sym_norm]
        return hit
    return items


def _latest_trade_date() -> str:
    """最近交易日(周末回退到周五, 简化版)。"""
    from datetime import datetime, timedelta
    d = datetime.now()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
