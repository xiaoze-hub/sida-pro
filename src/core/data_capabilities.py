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
    h = health.get(row.get("provider") or "") or {}
    return {
        "provider": row.get("provider"),
        "name": row.get("name"),
        "enabled": bool(row.get("enabled")),
        "priority": row.get("priority"),
        "success_rate": h.get("success_rate"),
        "samples": h.get("count"),
        "ewma_latency_ms": h.get("ewma_latency_ms"),
        "last_error": h.get("last_error") or "",
    }


def classify(sources: list[dict]) -> tuple[str, str]:
    """由该数据集的源列表 + 健康指标定状态。返回 (status, reason)。"""
    enabled = [s for s in sources if s["enabled"]]
    if not enabled:
        return "unavailable", "没有启用的数据源"
    measured = [s for s in enabled if (s["samples"] or 0) >= MIN_SAMPLES and s["success_rate"] is not None]
    if not measured:
        return "unknown", f"{len(enabled)} 个源尚无足够样本(需 ≥{MIN_SAMPLES} 次调用)"
    best = max(measured, key=lambda s: s["success_rate"])
    if best["success_rate"] >= OK_SUCCESS_RATE:
        return "ok", f"最佳源 {best['provider']} 成功率 {round(best['success_rate'] * 100)}%"
    worst = ", ".join(f"{s['provider']} {round(s['success_rate'] * 100)}%" for s in measured)
    return "degraded", f"全部源成功率低于 {round(OK_SUCCESS_RATE * 100)}%({worst})"


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


def build_capabilities(rows: list[dict], health: dict, freshness: dict | None = None,
                       today: str | None = None) -> dict:
    """rows: DataSource 记录(dict); health: vendor→指标; freshness: 表名→最新日期字符串。"""
    freshness = freshness or {}
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        t = r.get("type") or ""
        if t:
            by_type.setdefault(t, []).append(r)

    items = []
    for t in sorted(by_type):
        sources = sorted((_source_view(r, health) for r in by_type[t]),
                         key=lambda s: (not s["enabled"], s["priority"] if s["priority"] is not None else 99))
        status, reason = classify(sources)
        table, _col = FRESHNESS_PROBES.get(t, (None, None))
        latest = str(freshness[table]) if (table and freshness.get(table) is not None) else None
        items.append({
            "type": t,
            "label": DATASET_LABELS.get(t, t),
            "status": status,
            "status_label": STATUS_LABELS[status],
            "reason": reason,
            "sources": sources,
            "enabled_count": sum(1 for s in sources if s["enabled"]),
            "latest_date": latest,
            "age_days": _age_days(latest, today),
        })

    def _count(st):
        return sum(1 for i in items if i["status"] == st)

    return {
        "items": items,
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
