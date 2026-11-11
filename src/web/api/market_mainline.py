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
#
# 2026-11-11 遗留行清理: 同一数据日会被重算多次(盘中每次回源都算一遍), 只 upsert
# 不删 ⇒ 某次入榜、之后掉出榜单的组会留在表里(生产实测 2026-11-11 的"造纸 rank=10"
# 就是盘中遗留, 现算只有 9 组入榜), `_enrich_with_rank_change` 于是拿过期名次算
# rank_change。现在 upsert 与 DELETE 同事务: 本次名单之外的同日行一律清掉。


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


def _delete_sql_other_rows(n: int) -> str:
    """清掉同一数据日、但不在本次名单里的行(双方言同一条 SQL)。

    命名参数固定 n0..n{n-1}(不用 IN (...) 拼字面量, 名单里有引号/中文都不怕)。
    """
    placeholders = ", ".join(f":n{i}" for i in range(n))
    return f"DELETE FROM mainline_rank_daily WHERE date = :date AND name NOT IN ({placeholders})"


def _upsert_today_snapshot(today_iso: str, ranked_groups: list[dict]) -> None:
    """当日快照: 同一事务内 upsert 本次全部行, 再删同日不在本次名单的行。

    PRIMARY KEY(date, name) 冲突时覆盖 rank + score; 同日重算掉出榜单的组会被
    删掉, 免得 rank_change 拿过期名次做对比(见模块内 2026-11-11 备注)。

    保守约定: 本次 payload 为空(或全无 name) ⇒ **一行也不删** —— 分不清"还没算"
    和"今天真的没主线", 误删会把历史痕迹抹干净。失败静默(主流程不被落库异常阻断),
    upsert 与 DELETE 同事务 ⇒ 任一失败整批回滚, 不会留下"删了没补"的半截快照。
    """
    if not ranked_groups:
        return
    # 先过滤出本次真正会写的行; 全无 name 时视为无效载荷, 不删(否则会把当日清空)
    rows: list[tuple[str, int, object]] = []
    for g in ranked_groups:
        name = g.get("name")
        if not name:
            continue
        rows.append((str(name), int(g.get("rank") or 0), g.get("score")))
    if not rows:
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
        names = sorted({r[0] for r in rows})
        delete_stmt = _delete_sql_other_rows(len(names))
        # engine.begin() = 单事务: upsert 与 DELETE 要么都生效, 要么都回滚
        with engine.begin() as conn:
            for name, rank, score in rows:
                conn.execute(
                    text(stmt),
                    {"date": today_iso, "name": name, "rank": rank, "score": score},
                )
            conn.execute(
                text(delete_stmt),
                {"date": today_iso, **{f"n{i}": n for i, n in enumerate(names)}},
            )
    except Exception as e:
        logger.debug(f"mainline_rank_daily 写入当日快照失败(静默): {e}")


def _data_date(as_of: str | None) -> str | None:
    """数据日校验: 接受 ISO(YYYY-MM-DD) 与紧凑 YYYYMMDD; 其它/缺失 → None(不猜成今天)。"""
    s = str(as_of or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        candidate = s
    elif len(s) >= 8 and s[:8].isdigit():
        candidate = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    else:
        return None
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _enrich_with_rank_change(ranked_groups: list[dict], as_of: str | None = None) -> list[dict]:
    """给 ranked_groups 每项加 rank(1-indexed)+ rank_change(昨日-今日, 正=上升)。

    日期口径(2026-11-09): 一律按**涨停池数据日** as_of, 不用墙钟 today ——
    盘前跑的是上一交易日数据, 按 today 落快照/找"昨日"就是把两天的名次错配。
    as_of 未知 → 不落快照、rank_change=null(不假装有对比)。
    首次无昨日快照 → rank_change=null(数据缺失诚实标注, 不编造)。
    """
    if not ranked_groups:
        return ranked_groups
    data_date = _data_date(as_of)
    yesterday = _load_yesterday_ranks(data_date) if data_date else {}

    enriched: list[dict] = []
    for idx, g in enumerate(ranked_groups, start=1):
        item = dict(g)
        item["rank"] = idx
        name = g.get("name")
        if data_date and name in yesterday:
            # rank_change 正数=上升(昨日排名 - 今日排名)
            item["rank_change"] = int(yesterday[name]) - idx
        else:
            item["rank_change"] = None
        enriched.append(item)

    # 落当日快照(失败静默, 不影响响应); 时点未知则不落(落"今日"即错标)
    if data_date:
        _upsert_today_snapshot(data_date, enriched)
    return enriched


def _normalize_payload(payload: dict) -> dict:
    """旧版本缓存/快照载荷补齐新键(**不臆测**, 并显式说明为什么是空)。

    2026-11-09 概念维度上线前的载荷没有 concept_* 键 —— 直接透传会让前端拿到
    `undefined`, 看起来像"今日没有概念题材"。这里补空结构 + legacy_payload 标记,
    旧字段原样保留; as_of 不从载荷里推导(它本来就没带)。
    """
    if not isinstance(payload, dict) or not payload:
        return payload
    out = dict(payload)
    out.setdefault("dimension", "industry")
    legacy = "concept_groups" not in payload
    if legacy:
        out.setdefault("concept_groups", [])
        out.setdefault("concept_unranked", [])
        stats = dict(payload.get("concept_stats") or {})
        stats.update(
            {
                "distinct_stocks": stats.get("distinct_stocks", 0),
                "tagged_stocks": stats.get("tagged_stocks", 0),
                "untagged_stocks": stats.get("untagged_stocks", 0),
                "multi_concept_stocks": stats.get("multi_concept_stocks", 0),
                "member_count_sum": stats.get("member_count_sum", 0),
                "overlap_excess": stats.get("overlap_excess", 0),
                "overlap_ratio": stats.get("overlap_ratio", 0.0),
                "summable": False,
                "legacy_payload": True,
                "note": "旧版本缓存载荷未包含概念维度（未重算）；空概念不代表今日无概念题材。",
            }
        )
        out["concept_stats"] = stats
        out["concept_status"] = "unavailable"
        out["concept_note"] = "旧版本缓存载荷未包含概念维度，需等下次回源重算；空概念不代表今日无概念题材。"
        meta = dict(payload.get("data_meta") or {})
        meta.setdefault("as_of", None)
        meta["concept_source"] = meta.get("concept_source")
        meta["concept_status"] = "unavailable"
        meta["concept_note"] = out["concept_note"]
        meta["concept_source_verified"] = False
        meta["concept_as_of"] = None
        meta["concept_as_of_note"] = "旧版本载荷未带概念归属日期，归属时点未知（未猜成与池子同日）"
        meta["pool_coverage"] = {
            "status": "unknown",
            "requested_codes": None,
            "fetched_codes": None,
            "batch_failures": None,
            "note": "旧版本载荷未带覆盖率信息，无法确认是否覆盖全市场（未声称完整）",
        }
        meta["coverage_note"] = meta["pool_coverage"]["note"]
        out["data_meta"] = meta
    else:
        out.setdefault("concept_status", "available" if out.get("concept_groups") else "unavailable")
        out.setdefault("concept_note", "")
        meta = dict(payload.get("data_meta") or {})
        meta.setdefault("concept_status", out["concept_status"])
        meta.setdefault("concept_source_verified", bool(meta.get("concept_source")))
        meta.setdefault("concept_as_of", None)
        meta.setdefault("concept_as_of_note", "")
        meta.setdefault(
            "pool_coverage",
            {
                "status": "unknown",
                "requested_codes": None,
                "fetched_codes": None,
                "batch_failures": None,
                "note": "载荷未带覆盖率信息，无法确认是否覆盖全市场（未声称完整）",
            },
        )
        meta.setdefault("coverage_note", (meta["pool_coverage"] or {}).get("note") or "")
        out.setdefault("data_meta", meta)
    return out


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


def _upsert_daily_snapshot(payload: dict, snapshot_date: str | None = None) -> bool:
    """upsert 快照(date 主键 → 同日覆盖为最新)。失败静默返回 False。

    date 只取**可信来源**: 调用方显式快照日, 或载荷里的 data_meta.as_of(数据日)。
    两者都没有 ⇒ **不落库** —— 盘前把上一交易日的榜落成"今日"快照会让 rank_change
    跨日错配, 而且事后无法分辨哪一行是猜的。
    """
    if not payload:
        return False
    row_date = snapshot_date or _data_date((payload.get("data_meta") or {}).get("as_of"))
    if not row_date:
        logger.debug("mainline_daily_snapshot: 数据日未知 → 不落库(不按今日冒充)")
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
                    "date": row_date,
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
    # v0.4.7: 排名变动(对比昨日快照, 失败静默)。日期用池子数据日, 不用墙钟。
    try:
        ranked = result.get("ranked_groups") or []
        if ranked:
            as_of = (result.get("data_meta") or {}).get("as_of")
            result["ranked_groups"] = _enrich_with_rank_change(ranked, as_of=as_of)
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
        "dimension": "industry",
        "concept_groups": [...], "concept_unranked": [...],
        "concept_stats": {..., "summable": false},
        "data_meta": {as_of, computed_at, pool_source, concept_source, as_of_note},
        "note", "cache_ts", "stale", "age_s"
      }
    概念维度(2026-11-09): 一股多概念 ⇒ 各概念家数**独立统计、不可相加**;
    concept leader 仅是本概念内排序第一, 不是该股专属驱动。

    缓存三层(2026-10-14 缓存收口): biz_cache(L1 内存 + L2 Redis, 跨 worker/重启
    共享) → mainline_daily_snapshot 日频快照读库 → 回源拉涨停池聚合。
    读侧口径: 够新读缓存/读库直接返; 过旧先返旧值 + stale 标注(后台单飞刷新)。
    clear_market_mainline_cache() 可强制刷新缓存(运维用)。
    """
    # ① biz_cache(内存 + Redis): 跨 worker / 重启共享 —— 够新直接返, 过旧先返旧值+标 stale
    #    旧版本载荷(概念键上线前写入)先补齐新键, 否则新字段在缓存命中期整体消失
    hit = biz_cache.get_json(_CACHE_KEY)
    if isinstance(hit, dict) and hit:
        return _cached_response(_normalize_payload(hit))

    # ② 日频快照读库(重启/换 worker 兜底): 够新直接给; 过旧仍先给旧值 + stale 标注,
    #    不阻塞用户(库里的就是历史数据, 同步回源也无新意 —— 交给后台单飞去刷)
    snap = _read_daily_snapshot()
    if snap:
        snap = _normalize_payload(snap)
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
