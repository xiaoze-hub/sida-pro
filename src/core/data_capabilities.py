"""数据集能力视图(2026-09-12, 借鉴 tick-stock-panel 的能力路由矩阵)。

我们和它的差别: 它按"数据集 × 源能力"做**路由**(可切生效源); 我们的源是运行期按
priority 兜底的, 不需要再做一个路由器。真正缺的是**视图** —— 用户(老板)现在看不出
"哪类数据此刻是好的、哪类在降级、哪类根本没测过", 只能看到一串源名字。

口径:
- 状态四档, **不合并"未测量"到"正常"**(对齐 0.3「降级不伪装」):
  ok 正常 / degraded 降级 / unknown 未测量 / unavailable 无可用源
- degraded 判据: 该数据集所有启用源里, 有足够样本(≥MIN_SAMPLES)且成功率 < OK_SUCCESS_RATE。
- unknown: 有启用源但没有任何可信样本(从未调用/样本太少) → 前端标灰, 不显示绿色。
- 新鲜度只对"我们确实落库"的数据集探测(FRESHNESS_PROBES), 其余给 None —— 不编造。
"""
from __future__ import annotations

# 数据集 → 中文名(与 web/api/datasources.TYPE_LABELS 对齐, 并补齐 DB 里存在但未登记的类型)
DATASET_LABELS = {
    "kline": "K线数据",
    "quote": "实时行情",
    "capital_flow": "资金流向",
    "board_capital_flow": "板块资金",
    "market_capital_flow": "大盘资金",
    "flash_news": "快讯",
    "news": "新闻资讯",
    "events": "事件日历",
    "macro_calendar": "财经日历",
    "fundamentals": "基本面",
    "dragon_tiger": "龙虎榜",
    "margin": "融资融券",
    "shareholders": "股东户数",
    "dividend": "分红",
    "northbound": "北向资金",
    "chart": "K线截图",
    "more_info": "扩展指标",
    "wenda": "通达信问答",
}

# 数据集 → (落库表, 日期列): 只对确实入库的类型探新鲜度
FRESHNESS_PROBES = {
    "kline": ("klines", "ts"),
    "quote": ("quote_snapshots", "ts"),
    "dragon_tiger": ("dragon_tiger_events", "trade_date"),
    "board_capital_flow": ("board_daily", "date"),
}

OK_SUCCESS_RATE = 0.8       # 成功率≥此值算正常
MIN_SAMPLES = 10            # 样本不足不下"降级"结论(避免冷启动误判)

STATUS_LABELS = {"ok": "正常", "degraded": "降级", "unknown": "未测量", "unavailable": "无可用源"}


def _source_view(row: dict, health: dict) -> dict:
    """单个源的健康读数。

    **优先用 vendor 滚动 EWMA; 但它只在进程内存里 —— 每次重启后归零, 18 类会全变
    "未测量"(诚实却没用)。所以 EWMA 样本不足时退回该源自己的 DB 累计计数**
    (`data_sources.success_count/error_count`, 跨重启存活), 并记下判断依据 basis。
    两者都不足 → 仍返回 None 让上层判 unknown, 不猜。
    """
    prov = row.get("provider") or ""
    h = health.get(prov) or {}
    sr, n, basis = h.get("success_rate"), (h.get("count") or 0), ("ewma" if h else "none")
    ds, de = int(row.get("success_count") or 0), int(row.get("error_count") or 0)
    if (sr is None or n < MIN_SAMPLES) and (ds + de) > n:
        tot = ds + de
        sr, n, basis = (ds / tot if tot else None), tot, "db"
    return {
        "provider": row.get("provider"),
        "name": row.get("name"),
        "enabled": bool(row.get("enabled")),
        "priority": row.get("priority"),
        "success_rate": round(sr, 4) if sr is not None else None,
        "samples": n,
        "basis": basis,
        "ewma_latency_ms": h.get("ewma_latency_ms"),
        "last_error": h.get("last_error") or "",
    }


def _measured(sources: list[dict]) -> list[dict]:
    """够格参与判定的源: 启用 + 样本足 + 有成功率。判定口径只在这里定义一次。"""
    return [s for s in sources
            if s["enabled"] and (s["samples"] or 0) >= MIN_SAMPLES and s["success_rate"] is not None]


def verdict_source(sources: list[dict]) -> dict | None:
    """**驱动本次结论的那个源**(与 classify 同口径: 够格里成功率最高者)。
    判不出(未测量/无启用源)→ None。UI 必须显示这个源的读数, 而不是按 priority 猜。"""
    measured = _measured(sources)
    return max(measured, key=lambda s: s["success_rate"]) if measured else None


def classify(sources: list[dict]) -> tuple[str, str]:
    """由该数据集的源列表 + 健康指标定状态。返回 (status, reason)。"""
    enabled = [s for s in sources if s["enabled"]]
    if not enabled:
        return "unavailable", "没有启用的数据源"
    best = verdict_source(sources)
    if best is None:
        return "unknown", f"{len(enabled)} 个源尚无足够样本(需 ≥{MIN_SAMPLES} 次调用)"
    measured = _measured(sources)
    basis_label = {"ewma": "滚动EWMA", "db": "累计统计"}.get(best.get("basis") or "")
    suffix = f"({basis_label})" if basis_label else ""
    if best["success_rate"] >= OK_SUCCESS_RATE:
        return "ok", f"最佳源 {best['provider']} 成功率 {round(best['success_rate'] * 100)}%{suffix}"
    worst = ", ".join(f"{s['provider']} {round(s['success_rate'] * 100)}%" for s in measured)
    return "degraded", f"全部源成功率低于 {round(OK_SUCCESS_RATE * 100)}%({worst}{suffix})"


def _age_days(latest: str | None, today: str | None) -> int | None:
    """最新数据距今天数(兼容 'YYYY-MM-DD' 与 'YYYYMMDD' 两种日期写法)。"""
    if not latest or not today:
        return None
    from datetime import date

    def _parse(v: str) -> date | None:
        s = str(v)[:10]
        try:
            if "-" in s:
                return date.fromisoformat(s)
            if len(s) >= 8 and s.isdigit():
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
        return None

    d1, d2 = _parse(latest), _parse(today)
    return (d2 - d1).days if d1 and d2 else None


# ---------------------------------------------------------------------------
# 新鲜度探测缓存（2026-10-31）
# ---------------------------------------------------------------------------
# 背景：`GET /api/datasources/capabilities` 在**每个页面加载**都会被前端调用，它对每张表跑
# `SELECT max(ts)`。klines 是 1.5GB / 1697 个 chunk 的 hypertable，冷态 max() 实测 **4.6s**
# （热态 0.6s）—— 这就是"点进页面转圈几秒才出内容"的主要来源之一。
# 这些数据一天只变一次，没有任何理由每次页面都重算：这里给 5 分钟 TTL。
# 失败**不缓存**（库刚重启/表还没建时下次仍要重试，而不是把"无数据"钉 5 分钟）。
_FRESHNESS_TTL_S = 300.0
_FRESHNESS_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def clear_freshness_cache() -> None:
    """清空缓存（测试用；生产不需要手动调用）。"""
    _FRESHNESS_CACHE.clear()


def _db_key(db) -> str:
    """按连接串分桶：多库/测试环境不能互相读到对方的新鲜度。"""
    try:
        url = db.get_bind().url
        return f"{url.drivername}://{url.host or ''}/{url.database or ''}"
    except Exception:  # noqa: BLE001 - 取不到就给单桶，别再抛错
        return "unknown"


def freshness_probe(db, *, ttl: float = _FRESHNESS_TTL_S) -> dict[str, object]:
    """各数据集最新日期（表名→值）；TTL 内复用，过期重算，查不到的表不进结果。"""
    import time as _time

    from sqlalchemy import text

    key_prefix = _db_key(db)
    now = _time.monotonic()
    out: dict[str, object] = {}
    for name, (table, col) in FRESHNESS_PROBES.items():
        cache_key = (key_prefix, table)
        hit = _FRESHNESS_CACHE.get(cache_key)
        if hit and (now - hit[0]) < ttl:
            if hit[1] is not None:
                out[table] = hit[1]
            continue
        try:
            v = db.execute(text(f"SELECT max({col}) FROM {table}")).scalar()  # noqa: S608 表/列来自本模块常量
        except Exception:  # noqa: BLE001 表可能尚未创建(新库/未迁移)
            db.rollback()
            continue
        if v is not None:
            out[table] = v
            _FRESHNESS_CACHE[cache_key] = (now, v)
        else:
            # 空表也缓存 None：否则每次都要为"没数据"这张表付一次 max() 的代价
            _FRESHNESS_CACHE[cache_key] = (now, None)
    return out


def build_capabilities(rows: list[dict], health: dict, freshness: dict | None = None,
                       today: str | None = None) -> dict:
    """rows: DataSource 记录(dict, 含 success_count/error_count); health: vendor→EWMA;
    freshness: 表名→最新日期字符串。"""
    freshness = freshness or {}
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        t = r.get("type") or ""
        if t:
            by_type.setdefault(t, []).append(r)

    from src.core.data_impact import impact_of

    items = []
    for t in sorted(by_type):
        sources = sorted((_source_view(r, health) for r in by_type[t]),
                         key=lambda s: (not s["enabled"], s["priority"] if s["priority"] is not None else 99))
        status, reason = classify(sources)
        verdict = verdict_source(sources)
        table, _col = FRESHNESS_PROBES.get(t, (None, None))
        latest = str(freshness[table]) if (table and freshness.get(table) is not None) else None
        items.append({
            "type": t,
            "label": DATASET_LABELS.get(t, t),
            "status": status,
            "status_label": STATUS_LABELS[status],
            "reason": reason,
            "sources": sources,
            "verdict": None if verdict is None else {
                "provider": verdict["provider"],
                "success_rate": verdict["success_rate"],
                "samples": verdict["samples"],
                "basis": verdict.get("basis") or "none",
            },
            # P2-3(2026-09-18): 影响面 —— "降级会影响我哪个页面 / 看到的是缺的还是有替代"
            "impact": impact_of(t),
            "enabled_count": sum(1 for s in sources if s["enabled"]),
            "latest_date": latest,
            "age_days": _age_days(latest, today),
        })

    def _count(st):
        return sum(1 for i in items if i["status"] == st)

    return {
        "items": items,
        "min_samples": MIN_SAMPLES,   # 前端据此显示"样本 n/10", 不自己硬编码阈值
        "summary": {
            "total": len(items),
            "ok": _count("ok"),
            "degraded": _count("degraded"),
            "unknown": _count("unknown"),
            "unavailable": _count("unavailable"),
            "degraded_labels": [i["label"] for i in items if i["status"] == "degraded"],
            "unavailable_labels": [i["label"] for i in items if i["status"] == "unavailable"],
        },
    }
