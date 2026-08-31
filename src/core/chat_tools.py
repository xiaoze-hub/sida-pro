# -*- coding: utf-8 -*-
"""SIDA-Pro AI 助手 10 工具骨架 (P3, 2026-08-31, 升级到 v2)
=====================================================
按 Hermes 派活规格 (msg_6xmrM17fpdAH..., msg_H2b5wm9pOFGq5wK...) 实现,
结合 7/31 手册 (thsdk 接入/决策先锋 8 问 8 答) 校准字段与口径。

工具清单 (10 个, 每个独立可调, 返回真实数据, 不编造):
  1. get_forecast            4 模型预测 + 到期对照
  2. get_opportunities       今日候选池
  3. get_strategy_signals    今日策略信号
  4. get_notifications       今日提醒
  5. get_dragon_tiger        龙虎榜 + 公告
  6. get_dark_flow_precise   .tck 委托号级暗盘 (Hermes 0831 口径: 主动侧 a28/a32 1:1, 被动侧 maker 未落盘 → 仅做主笔级还原)
  7. get_order_book_queue    .img 盘口队列 / 托压单 (字段 64: 每笔挂单量, 无委托号 → 仅形态识别)
  8. get_market_scan         formula 全市场扫描
  9. get_l2_flow             thsdk 实时大单方向 (1主买/2买/-1主卖/-2卖, 手册 §4.2 实测 253×6, 单笔 ≥30万阈值)
  10. get_stock_screen       wencai 一句话选股 (16+ 模板见手册 §11.1/§11.2)

硬约束 (Hermes 红线 + 手册 §10):
  - 金额 = 元, 成交量 = 股 (统一口径, 见 §3.2)
  - 缺失数据显式标 "无数据" (None + note), 禁止推测编造 (§10 风险 4)
  - 多用户隔离 (admin / 黄磊 / 娟姐 / demo, §5.3), 所有查询自动注入 user_id 过滤
  - 不持久化调用结果到主 DB (只写 chat_*.json 缓存 + 工具调用日志)
  - thsdk 走云端 L2 通道 (需 THS_USERNAME/THS_PASSWORD, 已实测登录成功 §3.2)
  - 不主张"我们的 AI 方向预测" (§8.2 风险), thsdk 直出是数据, 明盘+暗盘是同花顺官方口径, 转述时标清
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """统一工具返回: data + meta + error (缺失时 data=None, error 显式标)。"""
    tool: str
    params: Dict[str, Any]
    data: Optional[Any] = None
    error: Optional[str] = None
    note: Optional[str] = None
    units: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "params": self.params,
            "data": self.data,
            "error": self.error,
            "note": self.note,
            "units": self.units,
        }


def _err(tool: str, params: dict, msg: str, note: str = "无数据") -> ToolResult:
    return ToolResult(tool=tool, params=params, data=None, error=msg, note=note)


def _ok(tool: str, params: dict, data: Any, units: Dict[str, str], note: Optional[str] = None) -> ToolResult:
    return ToolResult(tool=tool, params=params, data=data, error=None, note=note, units=units)


# ===== wencai_nlp 模板库 (手册 §11, 已实测可用) =====
WENCAI_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "板块资金流向":    {"q": "今日行业板块主力资金净流入排名前20", "src": "wencai_nlp"},
    "概念板块流向":   {"q": "今日概念板块主力资金净流入排名前20", "src": "wencai_nlp"},
    "板块涨幅":       {"q": "今日行业板块涨幅排名前20", "src": "wencai_nlp"},
    "概念涨幅":       {"q": "今日概念板块涨幅排名前20", "src": "wencai_nlp"},
    "北向持股":       {"q": "北向资金持股比例最高的20只个股", "src": "wencai_nlp",
                       "note": "实时净买入已停披露 (2024-08 起)"},
    "龙虎榜":         {"q": "昨日龙虎榜个股, 显示上榜原因", "src": "wencai_nlp"},
    "龙虎榜机构":     {"q": "昨日龙虎榜买入席位包含机构专用的个股", "src": "wencai_nlp"},
    "涨停归因":       {"q": "今日涨停个股及涨停原因", "src": "wencai_nlp"},
    "连板":           {"q": "今日连板个股, 显示连板数", "src": "wencai_nlp"},
    "创新高":         {"q": "今日创历史新高的个股", "src": "wencai_nlp"},
    "强势股":         {"q": "今日涨幅超过7%的个股, 非ST", "src": "wencai_nlp"},
    "主力净流入":     {"q": "今日主力资金净流入排名前20的个股", "src": "wencai_nlp"},
    "主力连续流入":   {"q": "连续3日主力资金净流入的个股, 非ST", "src": "wencai_nlp"},
    "融资融券":       {"q": "融资余额排名前20的个股", "src": "wencai_nlp"},
    "技术形态_MACD":  {"q": "今日MACD金叉的个股, 非ST", "src": "wencai_nlp"},
    "技术形态_MA":    {"q": "均线多头排列", "src": "wencai_nlp"},
    "尾盘异动":       {"q": "今日尾盘拉升的个股", "src": "wencai_nlp"},
    "解禁日历":       {"q": "未来一周解禁的个股", "src": "wencai_nlp"},
    "大宗交易":       {"q": "今日发生大宗交易的个股", "src": "wencai_nlp"},
    "破净股":         {"q": "市净率小于1的个股, 非ST", "src": "wencai_nlp"},
    "高股息":         {"q": "股息率大于5%的个股", "src": "wencai_nlp"},
    "业绩预增":       {"q": "中报净利润同比增长超过100%, 非ST", "src": "wencai_nlp"},
}


def _wencai_query(query: str, code: str = None, sleep_ms: int = 350) -> Dict[str, Any]:
    """wencai_nlp 真实接入骨架 (手册 §4.5, 限频 250ms/次, 默认 sleep 350ms)。

    Returns: {ok, rows: [...], error?: str}
    """
    import os
    os.environ.setdefault("PYTHONUTF8", "1")  # 手册 §2 防 gbk
    try:
        from thsdk import THS  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"thsdk 未安装: {e}"}
    import time
    user = os.environ.get("THS_USERNAME")
    pwd = os.environ.get("THS_PASSWORD")
    if not (user and pwd):
        return {"ok": False, "error": "THS_USERNAME/THS_PASSWORD 未设置 (.env.sida-pro.local)"}
    full_q = f"{query} 代码={code}" if code else query
    try:
        with THS({"username": user, "password": pwd, "mac": ""}) as ths:
            resp = ths.wencai_nlp(full_q)
            time.sleep(sleep_ms / 1000)
            rows = list(resp.data) if hasattr(resp, "data") and resp.data else []
            return {"ok": True, "rows": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _thsdk_call(method_name: str, **kwargs) -> Dict[str, Any]:
    """thsdk 真实接入骨架 (手册 §4, 限频 50ms/次)。"""
    import os
    import time
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from thsdk import THS  # type: ignore
    except Exception as e:
        return {"ok": False, "error": f"thsdk 未安装: {e}"}
    user = os.environ.get("THS_USERNAME")
    pwd = os.environ.get("THS_PASSWORD")
    if not (user and pwd):
        return {"ok": False, "error": "THS_USERNAME/THS_PASSWORD 未设置"}
    try:
        with THS({"username": user, "password": pwd, "mac": ""}) as ths:
            method = getattr(ths, method_name, None)
            if method is None:
                return {"ok": False, "error": f"thsdk 无方法 {method_name}"}
            time.sleep(0.05)
            resp = method(**kwargs)
            data = resp.data if hasattr(resp, "data") else resp
            return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ===== 10 工具函数 =====

# ===== P1+P2 (2026-09-01): 用户鉴权 + user_id 前缀缓存 =====
# P2 隔离红线 (Hermes msg_3d5HE4A8 派活):
#   1. 每个工具签名带 user_id, 解析 users 表, 不存在/停用 → 明示"用户 X 无权访问"
#   2. 缓存 key 一律 user_id 前缀, 防跨账号读脏数据
#   3. notifications 按 user_id 行级过滤 (+ user_id=NULL 全局通知, 与 chat.py S5 同模式)

import os
import threading
import time as _time

_CACHE_LOCK = threading.Lock()
_CACHE_TTL = {
    "get_forecast": 300.0,
    "get_opportunities": 30.0,      # 与 stock_pool._CACHE 同 30s
    "get_strategy_signals": 60.0,
    "get_notifications": 60.0,
}


def _cache_path() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "chat_tools_cache.json")


def _cache_key(tool: str, user_id: str, **parts) -> str:
    """P2: user_id 前缀, 跨账号物理隔离缓存条目。"""
    suffix = ":".join(str(parts[k]) for k in sorted(parts) if parts[k])
    return f"{user_id}:{tool}:{suffix}"


def _cache_get(tool: str, user_id: str, **parts):
    import json
    key = _cache_key(tool, user_id, **parts)
    ttl = _CACHE_TTL.get(tool, 60.0)
    with _CACHE_LOCK:
        try:
            with open(_cache_path(), encoding="utf-8") as f:
                store = json.load(f)
        except (OSError, ValueError):
            return None
    hit = store.get(key)
    if hit and _time.time() - hit.get("ts", 0) < ttl:
        return hit.get("data")
    return None


def _cache_set(tool: str, user_id: str, data, **parts) -> None:
    import json
    key = _cache_key(tool, user_id, **parts)
    with _CACHE_LOCK:
        try:
            with open(_cache_path(), encoding="utf-8") as f:
                store = json.load(f)
        except (OSError, ValueError):
            store = {}
        store[key] = {"ts": _time.time(), "data": data}
        now = _time.time()
        store = {k: v for k, v in store.items() if now - v.get("ts", 0) < 900}
        path = _cache_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass


def _auth_user(user_id: str):
    """解析 user_id (UUID 或用户名) → users 表活跃用户。

    返回 (user, None); 失败返回 (None, "用户 X 不存在或无权访问") (P2 错误明示)。
    """
    if not user_id:
        return None, "user_id 为空, 拒绝访问"
    try:
        from src.web.database import SessionLocal
        from src.web.models import User
    except Exception as e:  # noqa: BLE001
        return None, f"数据库不可用: {e}"
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == str(user_id)).first()
        if user is None:
            user = db.query(User).filter(User.username == str(user_id)).first()
        if user is None or not user.is_active:
            return None, f"用户 {user_id} 不存在或无权访问"
        return user, None
    finally:
        db.close()


def _jsonable(v):
    """ORM 行值转 JSON 安全类型 (datetime→ISO 字符串)。"""
    from datetime import datetime, date as _date
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, _date):
        return v.isoformat()
    return v


def get_forecast(date_: str = None, code: str = None, user_id: str = "admin") -> ToolResult:
    """(1) 4 模型预测 + 到期对照。
    来源: 预测库 (~/.panwatch_forecast.db forecasts 表, forecast_server.py 4 模型
    加权: Kronos/Chronos-Bolt/XGBoost/线性回归, models 字段存各模型明细)。
    单位: 价格=元 (手册 §10)。到期对照: target_date 未到 → pending。
    """
    params = {"date": date_ or date.today().isoformat(), "code": code, "user_id": user_id}
    user, err = _auth_user(user_id)
    if err:
        return _err("get_forecast", params, err)
    cached = _cache_get("get_forecast", user.id, date=params["date"], code=code)
    if cached is not None:
        return _ok("get_forecast", params, cached, units={"price": "元"}, note="缓存")
    import json as _json
    import sqlite3
    db_path = os.path.abspath(os.path.expanduser(
        os.environ.get("FORECAST_DB_PATH", "~/.panwatch_forecast.db")))
    if not os.path.exists(db_path):
        return _err("get_forecast", params,
                    "预测库不存在 (预测引擎 forecast_server 未部署, 无历史预测)", note="无数据")
    q = "SELECT * FROM forecasts WHERE date(created_at) = ?"
    args: List = [params["date"]]
    if code:
        q += " AND symbol = ?"
        args.append(code)
    q += " ORDER BY id DESC LIMIT 20"
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return _err("get_forecast", params, f"预测库读取失败: {e}", note="无数据")
    if not rows:
        return _err("get_forecast", params, f"{params['date']} 无预测记录 (预测库为空或该日未跑)", note="无数据")
    out = []
    for r in rows:
        for k in ("prediction", "models"):
            if isinstance(r.get(k), str):
                try:
                    r[k] = _json.loads(r[k])
                except ValueError:
                    pass
        r["outcome_status"] = "pending" if str(r.get("target_date") or "") >= params["date"] else "待行情对照"
        out.append({k: _jsonable(v) for k, v in r.items()})
    _cache_set("get_forecast", user.id, out, date=params["date"], code=code)
    return _ok("get_forecast", params, out, units={"price": "元"})


def get_opportunities(date_: str = None, code: str = None, user_id: str = "admin",
                      scan: bool = True) -> ToolResult:
    """(2) 今日候选池 (entry_candidates) + 决策先锋三指标共振扫描 (stock_pool)。

    entry_candidates 为全局市场快照 (表无 user_id 列), 隔离体现在鉴权+缓存前缀。
    scan=True 时对候选 (前 20 只) 跑三指标共振 (GS趋势/机构活跃度/L2主力净流入),
    单只失败降级"无"不阻塞 (stock_pool._screen 语义)。
    """
    params = {"date": date_ or date.today().isoformat(), "code": code, "user_id": user_id, "scan": scan}
    user, err = _auth_user(user_id)
    if err:
        return _err("get_opportunities", params, err)
    cached = _cache_get("get_opportunities", user.id, date=params["date"], code=code, scan=scan)
    if cached is not None:
        return _ok("get_opportunities", params, cached, units={"price": "元"}, note="缓存")
    try:
        from sqlalchemy import or_  # noqa: F401
        from src.web.database import SessionLocal
        from src.web.models import EntryCandidate
    except Exception as e:  # noqa: BLE001
        return _err("get_opportunities", params, f"数据库不可用: {e}")
    db = SessionLocal()
    try:
        query = (db.query(EntryCandidate)
                 .filter(EntryCandidate.snapshot_date == params["date"],
                         EntryCandidate.status == "active"))
        if code:
            query = query.filter(EntryCandidate.stock_symbol == code)
        rows = query.order_by(EntryCandidate.score.desc()).limit(50).all()
        out = []
        for c in rows:
            out.append({
                "stock_symbol": c.stock_symbol, "stock_name": c.stock_name,
                "action": c.action, "action_label": c.action_label,
                "score": c.score, "confidence": c.confidence,
                "signal": c.signal, "reason": c.reason,
                "strategy_tags": c.strategy_tags, "plan_quality": c.plan_quality,
                "entry_low": c.entry_low, "entry_high": c.entry_high,
                "stop_loss": c.stop_loss, "target_price": c.target_price,
                "candidate_source": c.candidate_source,
            })
    finally:
        db.close()
    if not out:
        return _err("get_opportunities", params,
                    f"{params['date']} 无活跃候选 (entry_candidates 为空)", note="无数据")
    if scan:
        try:
            from src.web.api.stock_pool import _screen
            symbols = [r["stock_symbol"] for r in out[:20]
                       if r["stock_symbol"] and r["stock_symbol"].isdigit()]
            if symbols:
                res = _screen(symbols)
                res_map = {r["symbol"]: r for r in res.get("rows", [])}
                for r in out:
                    s = res_map.get(r["stock_symbol"])
                    r["resonance"] = s.get("resonance") if s else "无"
                    r["resonance_score"] = s.get("score") if s else 0
                    r["activity"] = s.get("activity") if s else None
                    r["gs_state"] = s.get("gs_state") if s else None
                    r["l2_net_wan"] = s.get("l2_net") if s else None  # 万元 (stock_pool 口径)
        except Exception as e:  # noqa: BLE001
            for r in out:
                r.setdefault("resonance", None)
            params["scan_note"] = f"共振扫描失败降级: {e}"
        order = {"强": 0, "弱": 1, "无": 2, None: 3}
        out.sort(key=lambda r: (order.get(r.get("resonance"), 3),
                                -(r.get("resonance_score") or 0), -(r.get("score") or 0)))
    _cache_set("get_opportunities", user.id, out, date=params["date"], code=code, scan=scan)
    return _ok("get_opportunities", params, out,
               units={"price": "元", "l2_net_wan": "万元"},
               note="l2_net_wan 沿用 stock_pool 万元口径, 其余金额=元")


def get_strategy_signals(date_: str = None, code: str = None, user_id: str = "admin") -> ToolResult:
    """(3) 今日策略信号 (strategy_signal_runs 活跃信号, 按 rank_score 排序)。

    该表为全局策略执行快照 (无 user_id 列), 隔离体现在鉴权+缓存前缀。
    """
    params = {"date": date_ or date.today().isoformat(), "code": code, "user_id": user_id}
    user, err = _auth_user(user_id)
    if err:
        return _err("get_strategy_signals", params, err)
    cached = _cache_get("get_strategy_signals", user.id, date=params["date"], code=code)
    if cached is not None:
        return _ok("get_strategy_signals", params, cached, units={"price": "元"}, note="缓存")
    try:
        from src.web.database import SessionLocal
        from src.web.models import StrategySignalRun
    except Exception as e:  # noqa: BLE001
        return _err("get_strategy_signals", params, f"数据库不可用: {e}")
    db = SessionLocal()
    try:
        query = (db.query(StrategySignalRun)
                 .filter(StrategySignalRun.snapshot_date == params["date"],
                         StrategySignalRun.status == "active"))
        if code:
            query = query.filter(StrategySignalRun.stock_symbol == code)
        rows = query.order_by(StrategySignalRun.rank_score.desc()).limit(50).all()
        out = []
        for s in rows:
            out.append({
                "stock_symbol": s.stock_symbol, "stock_name": s.stock_name,
                "strategy_code": s.strategy_code, "strategy_name": s.strategy_name,
                "score": s.score, "rank_score": s.rank_score, "confidence": s.confidence,
                "action": s.action, "action_label": s.action_label,
                "signal": s.signal, "reason": s.reason, "holding_days": s.holding_days,
                "entry_low": s.entry_low, "entry_high": s.entry_high,
                "stop_loss": s.stop_loss, "target_price": s.target_price,
                "plan_quality": s.plan_quality, "source_pool": s.source_pool,
            })
    finally:
        db.close()
    if not out:
        return _err("get_strategy_signals", params,
                    f"{params['date']} 无活跃策略信号 (strategy_signal_runs 为空)", note="无数据")
    _cache_set("get_strategy_signals", user.id, out, date=params["date"], code=code)
    return _ok("get_strategy_signals", params, out, units={"price": "元"})


def get_notifications(date_: str = None, user_id: str = "admin") -> ToolResult:
    """(4) 今日提醒 (notifications 表, P2 行级隔离: 本人通知 + user_id=NULL 全局通知,
    与 src/web/api/chat.py S5 同模式)。未读优先。"""
    params = {"date": date_ or date.today().isoformat(), "user_id": user_id}
    user, err = _auth_user(user_id)
    if err:
        return _err("get_notifications", params, err)
    cached = _cache_get("get_notifications", user.id, date=params["date"])
    if cached is not None:
        return _ok("get_notifications", params, cached, units={"count": "条"}, note="缓存")
    try:
        from datetime import datetime as _dt, timedelta as _td
        from sqlalchemy import or_
        from src.web.database import SessionLocal
        from src.web.models import Notification
    except Exception as e:  # noqa: BLE001
        return _err("get_notifications", params, f"数据库不可用: {e}")
    try:
        day_start = _dt.fromisoformat(params["date"])
    except ValueError:
        return _err("get_notifications", params, f"日期格式错误: {params['date']} (需 YYYY-MM-DD)")
    day_end = day_start + _td(days=1)
    db = SessionLocal()
    try:
        rows = (db.query(Notification)
                .filter(or_(Notification.user_id == user.id, Notification.user_id.is_(None)),
                        Notification.created_at >= day_start,
                        Notification.created_at < day_end)
                .limit(200).all())
        # 未读优先 + 时间倒序 (Python 侧排序, 避免布尔表达式 ORDER BY 的方言差异)
        rows.sort(key=lambda n: (n.read_at is not None,
                                 -(n.created_at.timestamp() if n.created_at else 0)))
        rows = rows[:50]
        out = [{
            "id": n.id, "category": n.category, "level": n.level,
            "title": n.title, "body": n.body, "link": n.link,
            "source": n.source, "push_status": n.push_status,
            "unread": n.read_at is None,
            "created_at": _jsonable(n.created_at),
        } for n in rows]
    finally:
        db.close()
    if not out:
        return _err("get_notifications", params,
                    f"用户 {user.username} 在 {params['date']} 无提醒", note="无数据")
    _cache_set("get_notifications", user.id, out, date=params["date"])
    return _ok("get_notifications", params, out, units={"count": "条"})


def get_dragon_tiger(date_: str = None, code: str = None, user_id: str = "admin") -> ToolResult:
    """(5) 龙虎榜 + 公告 (wencai_nlp 一句话查)。
    典型 query: '昨日龙虎榜个股, 显示上榜原因' (手册 §11.1 实测 660×23 列)。
    """
    params = {"date": date_ or date.today().isoformat(), "code": code, "user_id": user_id}
    q = "昨日龙虎榜个股, 显示上榜原因"
    if code:
        q = f"{q} 代码={code}"
    resp = _wencai_query(q)
    if not resp["ok"]:
        return _err("get_dragon_tiger", params, resp.get("error", "wencai 调用失败"), note="无数据")
    return _ok("get_dragon_tiger", params, resp["rows"], units={"amount": "元", "count": "条"})


def get_dark_flow_precise(code: str, date_: str = None, user_id: str = "admin") -> ToolResult:
    """(6) .tck 委托号级暗盘还原 (Hermes 0831 口径: 主动侧 a28/a32 1:1; 被动侧 maker 未落盘 → 仅做主笔级还原)。
    单位: 净额=元 (手册 §10)。
    """
    params = {"code": code, "date": date_, "user_id": user_id}
    try:
        from src.core.tdx_tick_parser import parse_tck
        # TODO: 用 parse_tck 重算暗盘 (Hermes 0831 口径: 逐笔成交分档 = 净+898 万)
        return _err("get_dark_flow_precise", params, ".tck 解析已就绪, 主笔级暗盘复算待 P0-3 真实接入", note="无数据")
    except Exception as e:
        return _err("get_dark_flow_precise", params, str(e))


def get_order_book_queue(code: str, user_id: str = "admin") -> ToolResult:
    """(7) .img 盘口队列 / 托压单 (字段 64 仅每笔挂单量, 无委托号 → 仅形态识别, 手册 §4.2 §5.2)。"""
    params = {"code": code, "user_id": user_id}
    try:
        from src.core.tdx_img_parser import parse_img  # noqa: F401  (Hermes 审码修正: parse_img 在 tdx_img_parser)
        return _err("get_order_book_queue", params, ".img 解析已就绪, 托压单形态识别待 P3 真实实现", note="无数据")
    except Exception as e:
        return _err("get_order_book_queue", params, str(e))


def get_market_scan(date_: str = None, formula: str = "MACD金叉", user_id: str = "admin") -> ToolResult:
    """(8) formula 全市场扫描。
    来源: thsdk.formula_process_mul_zb (盘中有效, 降级 wencai 模板如 '今日MACD金叉的个股, 非ST')。
    """
    params = {"date": date_ or date.today().isoformat(), "formula": formula, "user_id": user_id}
    # 优先 thsdk, 降级 wencai (Hermes 审码修正: 去掉写死的 if False, 让 thsdk 真正走通)
    resp = _thsdk_call("formula_process_mul_zb", code=None, formula=formula)
    if resp and resp["ok"]:
        return _ok("get_market_scan", params, resp["data"], units={"count": "只"})
    q = WENCAI_TEMPLATES.get(f"{formula}", {}).get("q") or f"今日{formula}的个股"
    resp = _wencai_query(q)
    if resp["ok"]:
        return _ok("get_market_scan", params, resp["rows"], units={"count": "只"}, note="thsdk 未跑通, 用 wencai 降级")
    return _err("get_market_scan", params, resp.get("error", "formula 全市场扫描失败"), note="无数据")


def get_l2_flow(code: str, date_: str = None, big_threshold_wan: float = 30.0, user_id: str = "admin") -> ToolResult:
    """(9) thsdk 实时大单方向 (1主买/2买/-1主卖/-2卖, 单笔 >= 30 万阈值, 手册 §3.2 §4.2)。
    仅盘中有效; 盘后方向无效 (Hermes 0831 实测)。
    单位: 净额=元 (§10)。
    """
    params = {"code": code, "date": date_, "big_threshold_wan": big_threshold_wan, "user_id": user_id}
    resp = _thsdk_call("big_order_flow", code=code)
    if not resp or not resp.get("ok"):  # Hermes 审码修正: resp 空值检查
        err = (resp or {}).get("error", "thsdk.big_order_flow 调用失败")
        return _err("get_l2_flow", params, err, note="无数据 (凭据未注入或非盘中)")
    return _ok("get_l2_flow", params, resp["data"], units={"amount": "元", "vol": "股"})


def get_stock_screen(template: str = "强势股", code: str = None, user_id: str = "admin") -> ToolResult:
    """(10) wencai 一句话选股。
    template 从 WENCAI_TEMPLATES 取, 也可自定义 query 字符串 (限频 250ms/次, 手册 §4.5)。
    常用模板: 强势股/连板/创新高/涨停归因/主力连续流入/板块资金/技术形态_MACD 等 21 个。
    """
    params = {"template": template, "code": code, "user_id": user_id}
    if template in WENCAI_TEMPLATES:
        q = WENCAI_TEMPLATES[template]["q"]
        note = WENCAI_TEMPLATES[template].get("note")
    elif "SELECT" in template.upper() or "WHERE" in template.upper():
        # 防 SQL 注入: 自定义 query 只允许 wencai 模板前缀
        return _err("get_stock_screen", params, "自定义 query 不允许 (仅限模板)", note="无数据")
    else:
        q = template
        note = None
    if code:
        q = f"{q} 代码={code}"
    resp = _wencai_query(q)
    if not resp["ok"]:
        return _err("get_stock_screen", params, resp.get("error", "wencai 调用失败"), note=note or "无数据")
    return _ok("get_stock_screen", params, resp["rows"], units={"count": "只"}, note=note)


# ===== 工具注册表 =====
TOOL_REGISTRY = {
    "get_forecast":           (get_forecast,           {"date": "str(YYYYMMDD)", "code": "str or None"}),
    "get_opportunities":      (get_opportunities,      {"date": "str(YYYYMMDD)", "code": "str or None"}),
    "get_strategy_signals":   (get_strategy_signals,   {"date": "str(YYYYMMDD)", "code": "str or None"}),
    "get_notifications":      (get_notifications,      {"date": "str(YYYYMMDD)"}),
    "get_dragon_tiger":       (get_dragon_tiger,       {"date": "str(YYYYMMDD)", "code": "str or None"}),
    "get_dark_flow_precise":  (get_dark_flow_precise,  {"code": "str", "date": "str or None"}),
    "get_order_book_queue":   (get_order_book_queue,   {"code": "str"}),
    "get_market_scan":        (get_market_scan,        {"date": "str or None", "formula": "str or None"}),
    "get_l2_flow":            (get_l2_flow,            {"code": "str", "date": "str or None"}),
    "get_stock_screen":       (get_stock_screen,       {"template": "str", "code": "str or None"}),
}


def call_tool(name: str, **kwargs) -> ToolResult:
    if name not in TOOL_REGISTRY:
        return _err(name, kwargs, f"未知工具 '{name}' (注册表大小 {len(TOOL_REGISTRY)})")
    func, _schema = TOOL_REGISTRY[name]
    return func(**kwargs)


def list_wencai_templates() -> List[str]:
    """返回所有可用 wencai 模板名 (供 LLM agent 选)。"""
    return list(WENCAI_TEMPLATES.keys())
