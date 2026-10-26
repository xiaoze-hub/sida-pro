"""决策合成缓存层(2026-10-10 冗余设计): `/api/decision/{symbol}` 预落库 + 读时个性化。

## 动机(为什么)
`decide()` 是**实时计算**入口 —— 每次现拉 120 天 K 线(可能联网补数)+ 明/暗盘资金
(`compute_pool_flow`: 腾讯明盘 + thsdk L1 暗盘近似)现算三信号, 属慢接口(前端 SDK 显式
把超时放宽到 30s, 见 `packages/api/src/insight.ts`)。首屏/工作台反复请求会重复算同一标的。
本模块把**全局基底**(不含任何用户维度)预落库 + 加短 TTL, 命中直返, 免重复现算。

## 架构: 全局基底 + 读时个性化叠加
- 落库的 payload **只有全局基底**(`decide(symbol, market, days, user_context=None)`);
- 自选/持仓/风险偏好等**用户维度在读取时**用 `apply_user_overlay` 轻量合成 —— 避免按用户
  维度爆炸(4 账号并存), 且 user_id 隔离红线不被破坏(基底与用户无关)。
- `apply_user_overlay` **深拷贝**基底后再调 `_personalize`(该函数原地改 dict)⇒ 绝不污染
  全局缓存行(回归钉死: 见 tests/test_decision_cache.py)。

## 诚实语义(never fabricate)
- 命中/未命中由 API 层注入 `cached` + `computed_at`(计算时刻)—— **不用缓存冒充实时**;
- 缓存过期(age > ttl_s)一律视为 miss → 重算, 不返回陈旧结论;
- 时间戳口径与 `summary_cache` 一致: 列 `timestamp` 存 **naive UTC**, 读侧按 UTC 判龄
  (PG 会话时区 Asia/Shanghai 会把 aware 时间转墙上时间 → 必须统一, 见 summary_cache 注释);
- 缺数据不编造: `last_close` 取不到写 NULL; `decide` 的降级文案原样透传。

schema 变更唯一入口是 `src/web/migrations.py` `_m183_decision_cache`, 本模块不建表。
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: 端上请求(非预热标的)的短 TTL: 盘中同标的最多 5 分钟内不重复现算。
DECISION_TTL_S = 300
#: 缓存来源标记(供可观测 / 排查): 盘后批算 vs 端上即时算。
SOURCE_PRECOMPUTE = "precompute"
SOURCE_TTL = "ttl"
#: payload 上限(与 summary_cache 同口径): 超限宁可不落库, 也不存残缺载荷。
PAYLOAD_MAX = 1_000_000
#: 行保留天数: 过老的行在读/写时顺带清掉, 防表膨胀(与 summary_cache 一致)。
RETENTION_DAYS = 14

_CST = None  # 惰性构造, 避免 zoneinfo 在无 tzdata 环境 import 期炸


def _cst_zone():
    global _CST
    if _CST is None:
        from zoneinfo import ZoneInfo

        _CST = ZoneInfo("Asia/Shanghai")
    return _CST


def _engine():
    from src.db.session import engine

    return engine


def now_utc_naive() -> datetime:
    """当前 naive UTC(写库口径, 两侧统一)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime | None) -> str | None:
    """naive/aware datetime → ISO8601(带 Z), 供响应 `computed_at`。None → None。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(computed) -> datetime | None:
    """把 DB 里的 computed_at 解析成 aware UTC; 失败 → None(视为过期)。"""
    if isinstance(computed, str):
        try:
            computed = datetime.fromisoformat(computed.replace("Z", "+00:00"))
        except Exception:
            return None
    if not isinstance(computed, datetime):
        return None
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    return computed


def _next_open_cst(now_cst: datetime) -> datetime:
    """下一个工作日 09:30(Asia/Shanghai), 严格 > now(周末跳过)。仅用星期几,
    不查交易日历 —— 遇节假日会**提前**到期(→ 重算, 诚实, 只是少省一次)。"""
    cand = now_cst.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_cst >= cand:
        cand = cand + timedelta(days=1)
    while cand.weekday() >= 5:  # 5=周六 6=周日
        cand = cand + timedelta(days=1)
    return cand


def precompute_ttl_s(now: datetime | None = None) -> int:
    """盘后批算的 TTL: 有效到**下一个工作日开盘 09:30**(CST), 夹在 [300s, 96h]。

    盘后(如 15:45)批算 → 有效穿夜到次日 09:30 开盘; 开盘后该行过期 → 端上重算,
    不会用隔夜基底冒充盘中结论(诚实语义)。"""
    now = now or datetime.now(_cst_zone())
    if now.tzinfo is None:
        now = now.replace(tzinfo=_cst_zone())
    secs = int((_next_open_cst(now) - now).total_seconds())
    return max(DECISION_TTL_S, min(secs, 96 * 3600))


# ─────────────────────────── 计算(全局基底) ───────────────────────────
def compute_base(symbol: str, market: str = "CN", days: int = 120) -> dict:
    """算**全局基底**(无用户上下文)。委托 `decide`(惰性 import → monkeypatch 友好)。"""
    from src.core.decision import decide

    return decide(symbol, (market or "CN").upper(), days, None)


# ─────────────────────────── 读时个性化叠加 ───────────────────────────
def apply_user_overlay(base: dict, user_context: dict | None = None) -> dict:
    """在全局基底之上叠加**当前用户**个性化 → 返回新 dict(绝不改 base)。

    无上下文 → 返回基底的**深拷贝**(逐字段与不缓存路径一致, 向后兼容零破坏)。
    `last_close` 取自基底(预落库时一并存), 供持仓浮盈计算。
    """
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    if not user_context:
        return out
    ctx = dict(user_context)
    if ctx.get("last_close") is None and isinstance(base, dict):
        ctx["last_close"] = base.get("last_close")
    from src.core.decision import personalize

    return personalize(out, ctx)


# ─────────────────────────── DB 读写 ───────────────────────────
def get_cached_decision(symbol: str, market: str = "CN") -> dict | None:
    """读 decision_cache: 命中且未过期 → {payload, computed_at, source, ttl_s, age_s}; 否则 None。

    **过期即 miss**(不返回陈旧结论)。任何异常都不抛(退化为无缓存, 由调用方重算)。
    """
    code = (symbol or "").strip()
    mkt = (market or "CN").upper()
    if not code:
        return None
    try:
        with _engine().begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT computed_at, ttl_s, source, payload FROM decision_cache
                    WHERE symbol = :symbol AND market = :market
                    """
                ),
                {"symbol": code, "market": mkt},
            ).mappings().first()
            if not row:
                return None
            computed = _parse_ts(row["computed_at"])
            if computed is None:
                return None
            ttl = int(row["ttl_s"] or 0)
            age = (datetime.now(timezone.utc) - computed).total_seconds()
            # age < 0: 未来时间戳(时区口径不一致/时钟异常)→ 一律视为过期
            if age < 0 or age > ttl:
                return None
            payload = json.loads(row["payload"])
            return {
                "payload": payload,
                "computed_at": iso_utc(computed),
                "ttl_s": ttl,
                "source": row["source"] or "",
                "age_s": round(age, 1),
            }
    except Exception as e:  # noqa: BLE001
        logger.debug("get_cached_decision %s.%s failed: %s", code, mkt, e)
        return None


def put_cached_decision(
    symbol: str,
    market: str,
    payload: dict,
    ttl_s: int = DECISION_TTL_S,
    source: str = SOURCE_TTL,
) -> str | None:
    """写 decision_cache: upsert + 顺带清过期行。失败永不抛。

    成功 → 返回本次落库的 `computed_at`(ISO8601, 供端点透传, 保证 miss 响应与后续
    命中响应的 `computed_at` 一致); 未落库(超限/异常) → None。

    只应存**全局基底**(绝不含用户维度)。超上限跳过落库(不截断以免返回残缺数据)。
    """
    code = (symbol or "").strip()
    mkt = (market or "CN").upper()
    if not code or not isinstance(payload, dict):
        return None
    try:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body) > PAYLOAD_MAX:
            logger.warning(
                "put_cached_decision %s.%s payload %dB 超上限 %dB, 跳过落库",
                code, mkt, len(body), PAYLOAD_MAX,
            )
            return None
        last_close = payload.get("last_close")
        if not isinstance(last_close, (int, float)):
            last_close = None
        now = now_utc_naive()
        from src.db.dialect import upsert_sql

        stmt = upsert_sql(
            "decision_cache",
            ["symbol", "market", "computed_at", "ttl_s", "last_close", "source", "payload"],
            ["symbol", "market"],
            ["computed_at", "ttl_s", "last_close", "source", "payload"],
        )
        with _engine().begin() as conn:
            conn.execute(
                text(stmt),
                {
                    "symbol": code,
                    "market": mkt,
                    "computed_at": now,
                    "ttl_s": int(ttl_s),
                    "last_close": last_close,
                    "source": source or "",
                    "payload": body,
                },
            )
            conn.execute(
                text("DELETE FROM decision_cache WHERE computed_at < :cut"),
                {"cut": now - timedelta(days=RETENTION_DAYS)},
            )
        return iso_utc(now)
    except Exception as e:  # noqa: BLE001
        logger.debug("put_cached_decision %s.%s failed: %s", code, mkt, e)
        return None


def clear_decision_cache(symbol: str | None = None, market: str | None = None) -> int:
    """清缓存行(测试/运维用): 可按 symbol/market 过滤。失败返回 0, 不抛。"""
    try:
        clauses, params = [], {}
        if symbol:
            clauses.append("symbol = :symbol")
            params["symbol"] = symbol.strip()
        if market:
            clauses.append("market = :market")
            params["market"] = market.upper()
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _engine().begin() as conn:
            r = conn.execute(text(f"DELETE FROM decision_cache{where}"), params)
            return int(r.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("clear_decision_cache failed: %s", e)
        return 0
