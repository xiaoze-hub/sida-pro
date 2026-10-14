"""工作台日级图层派生块预落库(2026-10-14 首屏 P0: 26s → 秒开)。

## 为什么需要
个股工作台首屏 `/api/klines/{symbol}/summary` 的**日级派生块** ——
gs_signals(全量 GS 交叉序列) / fund_flow / activity_series / events(涨停跌停 +
.tck 拆单撤单 + wencai 龙虎榜/公告) / unlock_levels / chips(标准筹码接口) /
resonance(三指标共振) —— 此前**每次请求都在 `src/web/api/klines.py::_build_layer_data`
现算**: 其中 chips(腾讯/新浪分价表)、events 的 wencai 两条查询(硬超时 10s)、
resonance 的 compute_pool_flow(明+暗主力净额) 都是**网络调用**, 冷启动实测 ~26s,
撞前端 fetchAPI 的 20s abort。此前只有 300s TTL 兜底(summary_cache / biz_cache) ——
进程重启 / 换 worker / 隔夜首访必然重吃一遍。

## 口径(日级块 = 已定型交易日的不变量)
日级派生块由**收盘定型的日 K** 决定: 收盘(15:05)后当日块不再变化, 直到下一个交易日
盘中才产生新的"当日块"。故:

- **收盘批算**: 工作日 `RUN_HOUR:RUN_MINUTE`(15:40, 与 K 线入库/TQ 类调度器错开)对
  高频票(全库自选 ∪ 持仓 ∪ 最近访问)批算一次落 `workbench_layer_daily`, `settled=1`。
- **请求期**: `_build_layer_data` 先读库 —— 命中(该行 `settled=1` 且覆盖**最近已定型
  交易日**)则日级块**零现算**; 未命中(盘中当日块未定型 / 无行 / 行过旧 / 读库失败)
  ⇒ **只现算当日实时块**(现行 `_compute_daily_blocks` 全路径, 逐字段口径不变),
  算完若该日已定型就落库, 下次直接命中。
- **不冒充实时**: 盘中(交易日 09:15–15:05, A 股数据仍在变)一律**不读库** ——
  拿上一个定型日的 chips/资金/共振当当日状态展示, 就是"用旧数据冒充实时"(§5 诚实口径)。
  该窗口内仍走 summary_cache(300s)/biz_cache 双层 TTL(本改造不动它们)。
- **失败不阻塞**: 读库失败 / 算库失败 / 缺表 / 日期解析失败 / 交易日历缺年 —— 一律
  best-effort 回现算或返回原降级结果, 绝不因预落库把 summary 打挂, 也不编造数据。

## 与决策预落库的关系
与 `src/core/decision_precompute.py` 同一成熟模式(预热集 + 收盘批算 + 读时命中),
两个差别: ①本表按 (symbol, market, **asof 交易日**) 定稿 —— 日级块是**按日**的不变量,
保留历史行便于事后核对/回放; ②读侧有"最近已定型交易日"新鲜度门禁(行落后于最新定型日
= 当年块没算完/批算漏了该票 → 回现算, 不返回缺日的序列)。

## 依赖方向(为什么现算入口是"注册"进来的)
日级块的现算实现(`_compute_daily_blocks`)住在 web 层(`src/web/api/klines.py`)——
它依赖 `_build_events`/`_build_resonance`/`_to_tencent` 等一堆 web 层件, 且这些符号
被既有测试直接引用/打桩。而 `src/core` **禁止**反向 import `src/web`
(B4.1/KI-039 棘轮门禁 `tests/test_w41_core_web_dependency.py`)。故本模块用
**依赖倒置**: web 层 import 期调 `register_daily_block_builder(build_layer_daily_blocks)`,
本模块只按回调调用; 未注册时批算显式记 skipped 并告警(**不猜不编造**)。

schema 变更唯一入口是 `src/web/migrations.py` `_m190_workbench_layer_daily_table`,
本模块不建表(AGENTS 铁律)。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: 落库/读库的日级派生块键(与 `klines._compute_daily_blocks` 返回键一一对应)。
#: orderbook 是**实时块**(盘口队列), 不落库、每次现算。
DAILY_BLOCK_KEYS: tuple[str, ...] = (
    "gs_signals",
    "fund_flow",
    "activity_series",
    "events",
    "unlock_levels",
    "chips",
    "resonance",
)

#: 写入来源标记(可观测/排查用): 收盘批算 vs 请求期算完落库。
SOURCE_PRECOMPUTE = "precompute"
SOURCE_REQUEST = "request"

#: payload 上限(与 summary_cache 同口径): 超限宁可不落库, 也不存残缺载荷。
PAYLOAD_MAX = 1_000_000
#: 行保留天数(读/写时顺带清掉更老的行, 防表膨胀; summary_cache 同法)。
RETENTION_DAYS = 14
#: "最近访问"回溯天数: 近 N 天内算过的票并入收盘预热集(用户高频票)。
RECENT_DAYS = 5

#: 当日块"定型"时刻(北京时间): 收盘 15:00 + 集合竞价, 15:05 后当日日 K 不再变化
#: (与 theme_mood 收盘定型同口径: `_m182` settled_at / dark_flow 的 15:05 判据)。
SETTLE_AFTER = time(15, 5)
#: 盘中窗口起点(集合竞价开始): 此刻起当日块开始"未定型", 一律回现算。
LIVE_WINDOW_START = time(9, 15)

#: 收盘批算时刻(北京时间): 15:40 —— 收盘后、K 线入库之后, 与 decision_precompute(15:45)
#: / TQ 类调度器(15:35, 15:50)错开。
RUN_HOUR = 15
RUN_MINUTE = 40

_CST = None  # 惰性构造, 避免 zoneinfo 在无 tzdata 环境 import 期炸
_global_scheduler = None

#: 日级块**现算入口**(依赖倒置): 现算实现住在 web 层(`src/web/api/klines.py`,
#: 依赖 `_build_events`/`_build_resonance` 等一堆 web 层件), 而 `src/core` **禁止**
#: 反向 import `src/web`(B4.1/KI-039 棘轮门禁 `tests/test_w41_core_web_dependency.py`)。
#: 故由 web 层在 import 期把入口注册进来, 本模块只按回调调用, 不认识 web。
#: 签名: (symbol: str, market_code, days: int) -> (asof: str|None, blocks: dict)
_daily_block_builder = None


def register_daily_block_builder(builder) -> None:
    """注册日级块现算入口(由 `src/web/api/klines.py` import 期调用, 幂等)。"""
    global _daily_block_builder
    _daily_block_builder = builder


def get_daily_block_builder():
    """当前的日级块现算入口; 未注册 → None(批算会显式记 skipped, 不编造)。"""
    return _daily_block_builder


def _cst():
    global _CST
    if _CST is None:
        from zoneinfo import ZoneInfo

        _CST = ZoneInfo("Asia/Shanghai")
    return _CST


def now_cst(now: datetime | None = None) -> datetime:
    """归一到 Asia/Shanghai; naive 视为上海墙上时间(便于单测注入)。"""
    if now is None:
        return datetime.now(_cst())
    if now.tzinfo is None:
        return now.replace(tzinfo=_cst())
    return now.astimezone(_cst())


def _to_iso_date(value) -> str | None:
    """日期规范化 'YYYY-MM-DD'(8 位数字 / 带时间串截断); 空 → None。"""
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def is_settled(asof, now: datetime | None = None) -> bool:
    """该交易日是否已**定型**(日 K 不再变化 ⇒ 日级块可当定稿)。

    历史日(< 今日)恒已定型; 今日须过 `SETTLE_AFTER`(15:05); 未来日一律未定型
    (宁可不落库, 也不把未定型数据钉成定稿)。
    """
    d = _to_iso_date(asof)
    if not d:
        return False
    try:
        dd = date.fromisoformat(d)
    except ValueError:
        return False
    n = now_cst(now)
    if dd < n.date():
        return True
    if dd > n.date():
        return False
    return n.time() >= SETTLE_AFTER


def in_live_window(now: datetime | None = None) -> bool:
    """是否处于"A 股当日块还在变"的窗口(交易日 09:15–15:05)。

    该窗口内当日 chips/资金/共振/事件仍在变, 读库里上一个定型日的行 = 拿旧数据
    冒充当日状态 ⇒ 一律回现算。交易日历缺年/异常时**保守**按周一~周五判(宁多重算)。
    """
    n = now_cst(now)
    if not (LIVE_WINDOW_START <= n.time() < SETTLE_AFTER):
        return False
    try:
        from src.core.trading_calendar import is_trading_day

        return bool(is_trading_day(n.date()))
    except Exception:  # noqa: BLE001 — 日历未覆盖该年等: 保守按工作日
        return n.weekday() < 5


def _as_bool(value) -> bool:
    """DB 里的 settled(SQLite 可能回 str) → bool。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none")
    return bool(value)


def payload_is_empty(blocks: dict) -> bool:
    """日级块是否**整体为空**(数据源全挂的降级态)。

    这种行不落库: 一旦钉成"该日定稿", 之后所有请求都会命中一个空载荷(比现算更坏)。
    """
    if not isinstance(blocks, dict):
        return True
    for key in DAILY_BLOCK_KEYS:
        if key == "resonance":
            continue
        if blocks.get(key):
            return False
    res = blocks.get("resonance")
    return not (isinstance(res, dict) and res.get("available"))


def _engine():
    from src.db.session import engine

    return engine


def get_layer_daily(symbol: str, market: str) -> dict | None:
    """读该票**最新一行**日级派生块。

    返回 `{asof, settled, source, computed_ts, blocks}`; 无行 / 读库失败 /
    载荷损坏 → None(调用方回现算, 绝不抛)。是否可用由 `usable()` 判定 ——
    本函数只负责把行原样读出来。
    """
    try:
        with _engine().begin() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT asof, settled, source, computed_ts, payload
                        FROM workbench_layer_daily
                        WHERE symbol = :symbol AND market = :market
                        ORDER BY asof DESC
                        LIMIT 1
                        """
                    ),
                    {"symbol": symbol, "market": market},
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        blocks = json.loads(row["payload"] or "{}")
        if not isinstance(blocks, dict):
            return None
        return {
            "asof": _to_iso_date(row["asof"]),
            "settled": _as_bool(row["settled"]),
            "source": row["source"] or "",
            "computed_ts": row["computed_ts"],
            # 只回日级块键(载荷里多余字段不冒充契约)
            "blocks": {k: blocks.get(k) for k in DAILY_BLOCK_KEYS},
        }
    except Exception as e:  # noqa: BLE001 — 读库失败 = 未命中, 回现算
        logger.debug("[layer-precompute] get %s.%s failed: %s", symbol, market, e)
        return None


def usable(
    row: dict | None, latest_settled_asof: str | None, now: datetime | None = None
) -> bool:
    """该行能否**直接作为当前日级块**返回(即: 请求期零现算)。

    三个条件缺一不可:
      ① 行存在且 `settled=1`(该日已定型, 不是盘中写下的当日行);
      ② 当前不在盘中窗口(09:15–15:05 交易日) —— 当日块未定型, 一律现算;
      ③ 行覆盖**最近已定型交易日**(`asof >= latest_settled_asof`) —— 否则说明该票的
         日级块落后于最新定型日(批算漏票/当日没算完), 回现算补齐序列, 不返回缺日的块。
    """
    if not row or not row.get("settled"):
        return False
    if in_live_window(now):
        return False
    asof = _to_iso_date(row.get("asof"))
    cut = _to_iso_date(latest_settled_asof)
    return bool(asof and cut and asof >= cut)


def put_layer_daily(
    symbol: str,
    market: str,
    asof,
    blocks: dict,
    *,
    source: str = SOURCE_REQUEST,
    settled: bool = False,
    now: datetime | None = None,
) -> bool:
    """落库一行日级派生块(upsert)。返回是否写入成功; 失败永不抛。

    - `asof` 为交易日(定稿维度, 联合主键的一部分);
    - `settled` 由调用方声明, 但**必须**过了 `is_settled(asof)` 才真置 1 ——
      未定型的当日行只作"最近访问"痕迹, 读侧永远不用(`usable`)。
    - 全空载荷不落库(见 `payload_is_empty`); 超限不落库(不存残缺载荷)。
    """
    a = _to_iso_date(asof)
    if not a or not isinstance(blocks, dict):
        return False
    if payload_is_empty(blocks):
        logger.debug("[layer-precompute] %s.%s asof=%s 载荷为空, 跳过落库", symbol, market, a)
        return False
    flag = bool(settled) and is_settled(a, now)
    try:
        body = json.dumps(blocks, ensure_ascii=False, default=str)
        if len(body) > PAYLOAD_MAX:
            logger.warning(
                "[layer-precompute] %s.%s payload %dB 超 %dB 上限, 跳过落库(不截断)",
                symbol, market, len(body), PAYLOAD_MAX,
            )
            return False
        from src.db.dialect import upsert_sql

        stmt = upsert_sql(
            "workbench_layer_daily",
            ["symbol", "market", "asof", "settled", "source", "computed_ts", "payload"],
            ["symbol", "market", "asof"],
            ["settled", "source", "computed_ts", "payload"],
        )
        ts = now_cst(now).timestamp()
        with _engine().begin() as conn:
            conn.execute(
                text(stmt),
                {
                    "symbol": symbol,
                    "market": market,
                    "asof": a,
                    "settled": 1 if flag else 0,
                    "source": source,
                    "computed_ts": ts,
                    "payload": body,
                },
            )
            conn.execute(
                text("DELETE FROM workbench_layer_daily WHERE computed_ts < :cut"),
                {"cut": ts - RETENTION_DAYS * 86400},
            )
        return True
    except Exception as e:  # noqa: BLE001 — 落库失败不影响响应
        logger.debug("[layer-precompute] put %s.%s failed: %s", symbol, market, e)
        return False


def clear_layer_daily(symbol: str | None = None) -> int:
    """清行: symbol=None 全清; 否则删该票。返回删除行数(供测试隔离/运维强刷)。"""
    try:
        with _engine().begin() as conn:
            if symbol:
                r = conn.execute(
                    text("DELETE FROM workbench_layer_daily WHERE symbol = :symbol"),
                    {"symbol": symbol},
                )
            else:
                r = conn.execute(text("DELETE FROM workbench_layer_daily"))
            return int(r.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("[layer-precompute] clear %s failed: %s", symbol, e)
        return 0


def recent_targets(db, days: int = RECENT_DAYS) -> list[tuple[str, str]]:
    """最近访问集: 本表近 N 天内写过行的 (symbol, market)(= 被访问/被算过的票)。

    表不存在/读失败 → 空列表(预热集退化为"自选 ∪ 持仓", 不影响正确性)。
    """
    cut = now_cst().timestamp() - max(int(days), 1) * 86400
    try:
        rows = db.execute(
            text(
                """
                SELECT DISTINCT symbol, market FROM workbench_layer_daily
                WHERE computed_ts >= :cut
                """
            ),
            {"cut": cut},
        ).all()
    except Exception as e:  # noqa: BLE001
        logger.debug("[layer-precompute] recent_targets 读取失败: %s", e)
        return []
    out = []
    for symbol, market in rows:
        code = str(symbol or "").strip()
        if code:
            out.append((code, str(market or "CN").upper()))
    return out


def collect_targets(db, recent_days: int = RECENT_DAYS) -> list[tuple[str, str]]:
    """收盘预热集 = **全库自选 ∪ 持仓 ∪ 最近访问** 去重的 (symbol, market)。

    自选/持仓是全局基底(与 user_id 无关 —— 不泄露任何用户维度, 同
    `decision_precompute.collect_targets` 的语义与理由); 任一路读失败不拖垮其余。
    """
    seen: set[tuple[str, str]] = set()

    def _add(rows) -> None:
        for symbol, market in rows or []:
            code = str(symbol or "").strip()
            if code:
                seen.add((code, str(market or "CN").upper()))

    try:
        from src.db.models import Position, Stock

        _add(db.query(Stock.symbol, Stock.market).all())
        # 持仓(经 Stock 关联拿 symbol/market)
        _add(
            db.query(Stock.symbol, Stock.market)
            .join(Position, Position.stock_id == Stock.id)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[layer-precompute] 自选/持仓集读取失败: %s", e)

    _add(recent_targets(db, recent_days))
    return sorted(seen)


def precompute(
    *,
    db=None,
    symbols: list[tuple[str, str]] | None = None,
    days: int = 120,
    limit: int | None = None,
    now: datetime | None = None,
    builder=None,
) -> dict:
    """批算并落库日级派生块。返回统计 {total, computed, skipped, failed, errors}。

    `symbols` 显式给出则不查库(测试/运维用); 否则从 Stock/Position 表取预热集。
    `builder` 是日级块现算入口(签名 `(symbol, market_code, days) -> (asof, blocks)`),
    缺省用 `get_daily_block_builder()`(web 层 import 期注册的 `build_layer_daily_blocks`);
    未注册 → 全部计 skipped 并告警(**绝不编造**)。单票失败只记录不抛(继续下一只);
    非 CN(港股/美股无日级块)与取不到 bars 的票计入 skipped。落库 `settled` 由
    `is_settled(asof, now)` 决定 —— 盘中手动跑批也不会把未定型的当日块钉成定稿。
    """
    build = builder or get_daily_block_builder()
    owns_db = False
    if symbols is None:
        if db is None:
            from src.db.session import SessionLocal

            db = SessionLocal()
            owns_db = True
        try:
            symbols = collect_targets(db)
        finally:
            if owns_db:
                db.close()

    targets = list(symbols or [])
    if limit is not None and limit >= 0:
        targets = targets[:limit]

    out = {"total": len(targets), "computed": 0, "skipped": 0, "failed": 0,
           "errors": [], "asof": None}
    if build is None:
        # 依赖倒置的入口没注册(web 层未加载): 不猜不编造, 全计 skipped 并告警。
        out["skipped"] = len(targets)
        out["errors"].append("no_builder")
        logger.warning("[layer-precompute] 日级块现算入口未注册(web 层未加载?), 跳过批算")
        return out
    for symbol, market in targets:
        if str(market or "").upper() != "CN":
            out["skipped"] += 1
            continue
        try:
            from src.models.market import MarketCode

            asof, blocks = build(symbol, MarketCode.CN, days)
            if not asof:
                out["skipped"] += 1
                continue
            if put_layer_daily(
                symbol, "CN", asof, blocks,
                source=SOURCE_PRECOMPUTE, settled=True, now=now,
            ):
                out["computed"] += 1
            else:
                out["skipped"] += 1
            if out["asof"] is None:
                out["asof"] = asof
        except Exception as e:  # noqa: BLE001 — 单票失败不拖垮整批
            out["failed"] += 1
            if len(out["errors"]) < 20:
                out["errors"].append(f"{symbol}:{type(e).__name__}")
            logger.debug("[layer-precompute] %s 批算失败: %s", symbol, e)
    logger.info(
        "[layer-precompute] 完成: %s",
        {k: out[k] for k in ("total", "computed", "skipped", "failed", "asof")},
    )
    return out


class LayerPrecomputeScheduler:
    """每工作日 15:40 盘后批算工作台日级图层块(后台线程调度)。"""

    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        from apscheduler.schedulers.background import BackgroundScheduler

        self.scheduler = BackgroundScheduler(timezone=timezone)

    def _job(self) -> None:
        try:
            from src.db.session import SessionLocal

            db = SessionLocal()
            try:
                precompute(db=db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[layer-precompute] 批算任务失败: %s", exc)

    def start(self) -> None:
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job, "cron", day_of_week="mon-fri",
            hour=RUN_HOUR, minute=RUN_MINUTE,
            id="workbench_layer_precompute", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        try:
            from src.core.scheduler_registry import register

            register("workbench_layer_precompute", self.scheduler)
        except Exception:  # noqa: BLE001 — CLI 无调度场景优雅跳过
            pass
        self.scheduler.start()
        logger.info("工作台图层预落库调度器已启动(工作日 %02d:%02d)", RUN_HOUR, RUN_MINUTE)

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


def run_precompute_job() -> None:
    """一次性批算入口(供运维/手动补跑调用)。失败静默(只记日志)。"""
    try:
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            precompute(db=db)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[layer-precompute] 手动触发批算失败: %s", exc)
