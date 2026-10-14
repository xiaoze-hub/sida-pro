"""市场主线识别 API(v0.3.0, 2026-08-24)。

端点(挂在 /api/market 前缀下, 与现有 market.py 同前缀不冲突):
    GET /api/market/mainline         → Top20 主线排名 + 各主线成分股(含 rank_change)

设计:
  - 数据源: MarketSentimentCollector().get_limit_up_pool()(wudao 优先, 东财兜底)
  - 聚合: src.core.market_mainline.aggregate_mainline
  - 缓存: biz_cache(L1 内存 + L2 Redis, 跨 worker/重启共享) + mainline_daily_snapshot
    日频快照落库; 读侧"够新读缓存/读库、过旧回源+stale 标注"(SWR, 不阻塞用户请求)。
    2026-10-14 缓存收口前是模块内 60s 进程 dict(per spec), 现已按 AGENTS 收口。
  - v0.4.7: ranked_groups 每项加 rank + rank_change(rank_change = 昨日rank - 今日rank,
    正数=上升, 首次无昨日快照时为 null)。当日快照 upsert 到 mainline_rank_daily。

与其他 market.* 接口解耦: 仅本路由聚合涨停池, 不动现有 indices/sparkline 缓存。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date

from fastapi import APIRouter
from sqlalchemy import text

from src.core.market_mainline import aggregate_mainline
from src.web.cache.biz_cache import biz_cache

logger = logging.getLogger(__name__)

router = APIRouter()

# ──────────── Task 3: 主线日榜快照(双方言兼容, v0.4.7) ────────────
# 主线榜每次计算成功后 upsert 当日 (date, name, rank, score),
# 跨日对比出 rank_change。PRIMARY KEY(date, name) 保证每日每线只留最新一条。
# 建表由 B 层版本化迁移 src/web/migrations.py _m139 负责(W1.5/A5 收编)。


def _load_yesterday_ranks(today_iso: str) -> dict[str, int]:
    """读最近一日 < today 的快照, 返回 {name: rank}。

    没有历史快照时返回空 dict, 上层据此输出 rank_change=null。
    """
    try:
        from src.web.database import engine
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT MAX(date) AS latest_date
                    FROM mainline_rank_daily
                    WHERE date < :today
                    """
                ),
                {"today": today_iso},
            ).first()
            latest = row[0] if row else None
            if not latest:
                return {}
            rows = conn.execute(
                text(
                    """
                    SELECT name, rank
                    FROM mainline_rank_daily
                    WHERE date = :d
                    """
                ),
                {"d": str(latest)},
            ).fetchall()
            return {str(r[0]): int(r[1]) for r in rows if r[0] is not None}
    except Exception as e:
        logger.debug(f"mainline_rank_daily 读取昨日失败(静默): {e}")
        return {}


def _upsert_today_snapshot(today_iso: str, ranked_groups: list[dict]) -> None:
    """当日快照 upsert: PRIMARY KEY(date, name) 冲突时覆盖 rank + score。
    失败静默(主流程不应被落库异常阻断)。
    """
    if not ranked_groups:
        return
    try:
        from src.db.dialect import upsert_sql
        from src.web.database import engine

        # W3.1(D2): 方言分叉收编 src/db/dialect.upsert_sql
        # (两后端均支持 ON CONFLICT ... DO UPDATE SET x = excluded.x)
        stmt = upsert_sql(
            "mainline_rank_daily",
            ["date", "name", "rank", "score"],
            ["date", "name"],
            ["rank", "score"],
        )
        with engine.begin() as conn:
            for g in ranked_groups:
                name = g.get("name")
                if not name:
                    continue
                conn.execute(
                    text(stmt),
                    {
                        "date": today_iso,
                        "name": str(name),
                        "rank": int(g.get("rank") or 0),
                        "score": g.get("score"),
                    },
                )
    except Exception as e:
        logger.debug(f"mainline_rank_daily 写入当日快照失败(静默): {e}")


def _enrich_with_rank_change(ranked_groups: list[dict]) -> list[dict]:
    """给 ranked_groups 每项加 rank(1-indexed)+ rank_change(昨日-今日, 正=上升)。

    首次无昨日快照 → rank_change=null(数据缺失诚实标注, 不编造)。
    """
    if not ranked_groups:
        return ranked_groups
    today_iso = date.today().isoformat()
    yesterday = _load_yesterday_ranks(today_iso)

    enriched: list[dict] = []
    for idx, g in enumerate(ranked_groups, start=1):
        item = dict(g)
        item["rank"] = idx
        name = g.get("name")
        if name in yesterday:
            # rank_change 正数=上升(昨日排名 - 今日排名)
            item["rank_change"] = int(yesterday[name]) - idx
        else:
            item["rank_change"] = None
        enriched.append(item)

    # 落当日快照(失败静默, 不影响响应)
    _upsert_today_snapshot(today_iso, enriched)
    return enriched


# ──────────── 缓存: biz_cache(L1 内存 + L2 Redis) + 日频快照落库 ────────────
# key 固定为 "mainline:top20", 共享一份 Top20 排名(全市场视角)。
# 拉涨停池耗 5-15s, 缓存压住前端 30s 轮询的并发翻页成本。
#
# 2026-10-14 缓存收口(审计: 放着已有缓存设施不用):
#   原实现 = 模块内进程 dict(多 worker 各一份 + 重启即丢, 且违反 AGENTS
#   『缓存一律走 src/web/cache/biz_cache.py, 禁止业务代码自建』)。现三层:
#     ① biz_cache(L1 内存 + L2 Redis) —— 跨 worker / 多容器 / 重启共享;
#     ② mainline_daily_snapshot 日频快照(每次成功聚合 upsert 当日一行 —— 收盘后那次
#        即当日定稿; 先例: market_data._try_write_snapshot_async 写 market_flow_snapshots);
#     ③ 回源: MarketSentimentCollector().get_limit_up_pool() 聚合(前两层都缺时)。
#   读侧口径(SWR 不变): 够新 → 读缓存/读库直接返(毫秒级, 不阻塞); 过旧 → 仍先返
#   旧值 + 标注 stale, 由后台单飞线程回源刷新 —— 不把用户请求卡在 16~37s 上。
_CACHE_KEY = "mainline:top20"
# 30s TTL + 同步回源 = 每半分钟就有一个倒霉的请求被卡住, 页面永远在转圈。
# 故: 过期先返回旧值(毫秒级) + 后台刷新; 只有"完全没数据"(进程刚起且库里也没有)
# 才同步取一次。
_CACHE_TTL_S = 180.0     # 主线排名是涨停池聚合, 3 分钟内变化有限; 30s 太短(命中率低→频繁回源)
_STALE_MAX_S = 1800.0    # 陈旧上限: 超过 30 分钟没成功刷新过也仍可用(标注 stale)
_refresh_lock = threading.Lock()
_refreshing = False      # 单飞: 防止过期瞬间 N 个请求同时回源(惊群)

# 日频快照落库节流(同 market_data 的 30s 节流): 前端高频轮询不会撑爆表。
_SNAPSHOT_INTERVAL_S = 30.0
_snapshot_lock = threading.Lock()
_snapshot_last_write_ts: float = 0.0


def _now_ts() -> float:
    return time.time()


def _payload_age(payload: dict) -> float | None:
    """载荷的数据年龄(秒)。取 cache_ts(_fetch_mainline 写入), 缺失 → None(不猜)。"""
    ts = payload.get("cache_ts") if isinstance(payload, dict) else None
    try:
        if ts:
            return max(0.0, _now_ts() - float(ts))
    except (TypeError, ValueError):
        pass
    return None


def _read_daily_snapshot() -> dict | None:
    """读 mainline_daily_snapshot 里 date ≤ 今日的最近一行, 返回 payload 字典。

    失败/无表静默返回 None(读不到就回源, 不影响功能)。cache_ts 缺失时用列
    computed_ts 回填, 保证下游能算出数据年龄(诚实标注"这份数据多旧")。
    """
    try:
        from src.web.database import engine

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload, computed_ts
                    FROM mainline_daily_snapshot
                    WHERE date <= :today
                    ORDER BY date DESC
                    LIMIT 1
                    """
                ),
                {"today": date.today().isoformat()},
            ).first()
        if not row:
            return None
        raw = row[0]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(payload, dict) or not payload:
            return None
        if payload.get("cache_ts") is None and row[1] is not None:
            payload = dict(payload)
            payload["cache_ts"] = float(row[1])
        return payload
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mainline_daily_snapshot 读取失败(静默): {e}")
        return None


def _upsert_daily_snapshot(payload: dict) -> bool:
    """upsert 当日主线快照(date 主键 → 同日覆盖为最新)。失败静默返回 False。"""
    if not payload:
        return False
    try:
        from src.db.dialect import upsert_sql
        from src.web.database import engine

        body = json.dumps(payload, default=str, ensure_ascii=False)
        stmt = upsert_sql(
            "mainline_daily_snapshot",
            ["date", "payload", "computed_ts"],
            ["date"],
            ["payload", "computed_ts"],
        )
        with engine.begin() as conn:
            conn.execute(
                text(stmt),
                {
                    "date": date.today().isoformat(),
                    "payload": body,
                    "computed_ts": payload.get("cache_ts"),
                },
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug(f"mainline_daily_snapshot 写入失败(静默): {e}")
        return False


def _try_write_daily_snapshot_async(payload: dict) -> None:
    """后台线程 upsert 当日快照(30s 节流, 失败静默不阻断主流程)。

    收盘后最后一次成功聚合即为当日定稿; 重启/换 worker 由此读库, 不再冷启动回源。
    """
    if not payload:
        return
    now = time.monotonic()
    with _snapshot_lock:
        global _snapshot_last_write_ts
        if now - _snapshot_last_write_ts < _SNAPSHOT_INTERVAL_S:
            return  # 节流窗口内, 跳过
        _snapshot_last_write_ts = now
    threading.Thread(
        target=_upsert_daily_snapshot,
        args=(dict(payload),),
        name="mainline-snapshot-writer",
        daemon=True,
    ).start()


def _warm_biz_cache(payload: dict) -> None:
    """回填 biz_cache(L1+L2), TTL 取陈旧上限 —— SWR 要旧值活到能返给用户。"""
    try:
        biz_cache.set_json(_CACHE_KEY, payload, ttl=int(_STALE_MAX_S))
    except Exception as e:  # noqa: BLE001
        logger.debug("market_mainline: biz_cache 写入失败(静默): %s", e)


def _store_mainline(payload: dict) -> dict:
    """成功聚合后回填两层: biz_cache(L1+L2) + 异步日频快照落库。"""
    _warm_biz_cache(payload)
    _try_write_daily_snapshot_async(payload)
    return payload


def _cached_response(payload: dict) -> dict:
    """按数据年龄给出 SWR 响应(**永不阻塞**)。

    够新(<_CACHE_TTL_S) → 直接返, stale=False;
    过旧/无龄 → 仍返旧值, 但标 stale=True + 给 age_s, 并触发后台单飞回源刷新 ——
    "接口契约要一致": 消费方永远能拿到"这份数据多旧", 不必写两套判断。
    """
    age = _payload_age(payload)
    if age is None:
        # 无龄信息: 不能声称"新", 按陈旧处理 + 回源(诚实标注, 不假装是新的)
        _refresh_in_background()
        return {**payload, "stale": True, "age_s": None}
    if age <= _CACHE_TTL_S:
        return {**payload, "stale": False, "age_s": round(age, 1)}
    _refresh_in_background()
    return {**payload, "stale": True, "age_s": round(age, 1)}


def _refresh_in_background() -> None:
    """后台刷新(单飞)。失败只记日志 —— 旧值继续用, 不要把异常抛给请求线程。"""
    global _refreshing
    with _refresh_lock:
        if _refreshing:
            return
        _refreshing = True

    def _work() -> None:
        global _refreshing
        try:
            payload = _fetch_mainline()
            _store_mainline(payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("market_mainline: 后台刷新失败(沿用旧值): %s", e)
        finally:
            with _refresh_lock:
                _refreshing = False

    threading.Thread(target=_work, name="mainline-refresh", daemon=True).start()


def warm_market_mainline_cache() -> None:
    """预热(供 APScheduler 定时调用): 让第一个用户请求几乎总能命中。"""
    _refresh_in_background()


def _fetch_mainline() -> dict:
    """拉涨停池 + 聚合。失败容错: 返回 aggregate_mainline([]) 空结构 + note。

    v0.4.7: 聚合后 enrich ranked_groups(加 rank + rank_change), 并 upsert 当日快照。
    enrichment 失败静默, 主流程返回结构不变(只是少 rank_change 字段)。
    """
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        collector = MarketSentimentCollector()
        pool = collector.get_limit_up_pool()
    except Exception as e:
        logger.warning("market_mainline: 涨停池拉取失败(%s), 返回空数据", e)
        pool = []

    result = aggregate_mainline(pool)
    # 增加一个 cache_ts(给前端读"最近一次拉取时间")
    result = dict(result)
    result["cache_ts"] = time.time()
    # v0.4.7: 排名变动(对比昨日快照, 失败静默)
    try:
        ranked = result.get("ranked_groups") or []
        if ranked:
            result["ranked_groups"] = _enrich_with_rank_change(ranked)
    except Exception as e:
        logger.debug("market_mainline: rank_change 增强失败(静默): %s", e)
    return result


@router.get("/mainline")
def get_market_mainline() -> dict:
    """市场主线 Top20 排名 + 成分股列表。

    返回结构见 src.core.market_mainline.aggregate_mainline 文档:
      {
        "total_groups", "ranked_groups": [...], "unranked": [...],
        "filter_stats": {broad_filtered, below_min, ranked},
        "note", "cache_ts", "stale", "age_s"
      }

    缓存三层(2026-10-14 缓存收口): biz_cache(L1 内存 + L2 Redis, 跨 worker/重启
    共享) → mainline_daily_snapshot 日频快照读库 → 回源拉涨停池聚合。
    读侧口径: 够新读缓存/读库直接返; 过旧先返旧值 + stale 标注(后台单飞刷新)。
    clear_market_mainline_cache() 可强制刷新缓存(运维用)。
    """
    # ① biz_cache(内存 + Redis): 跨 worker / 重启共享 —— 够新直接返, 过旧先返旧值+标 stale
    hit = biz_cache.get_json(_CACHE_KEY)
    if isinstance(hit, dict) and hit:
        return _cached_response(hit)

    # ② 日频快照读库(重启/换 worker 兜底): 够新直接给; 过旧仍先给旧值 + stale 标注,
    #    不阻塞用户(库里的就是历史数据, 同步回源也无新意 —— 交给后台单飞去刷)
    snap = _read_daily_snapshot()
    if snap:
        _warm_biz_cache(snap)
        return _cached_response(snap)

    # ③ 完全没数据(全新部署 / 库里也没有) → 同步取一次(这一次用户要等, 但只此一次)
    payload = _fetch_mainline()
    _store_mainline(payload)
    return {**payload, "stale": False, "age_s": 0.0}


def clear_market_mainline_cache() -> None:
    """清空主线响应缓存(biz_cache 里的 mainline:top20)。运维/测试用。

    只清响应缓存; 日频快照(mainline_daily_snapshot)是历史数据, 不随缓存清理丢弃。
    """
    try:
        biz_cache.delete(_CACHE_KEY)
    except Exception as e:  # noqa: BLE001
        logger.debug("market_mainline: 清缓存失败(静默): %s", e)
