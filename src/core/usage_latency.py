"""Skill 调用**耗时**聚合(B7 内部计量, 2026-09-19)。

## 为什么单独一个纯函数模块
耗时统计最容易出"看起来合理其实在编"的数字: 把 `duration_ms=0`(未记录)当成"很快的调用",
或把没数据的 skill 说成"p50=0ms"。所以把口径写死在这里, 由测试钉住, 端点只做取数。

## 三条口径
1. **只统计真的记了耗时的调用**(`duration_ms > 0`);未记录的**单独报数**, 不混进分位数 ——
   "没记录"和"耗时 0ms"是两件事;
2. **样本为 0 时不给分位数**, 返回 `None` + 说明(而不是 0);
3. **误差与调用数一起给**(errors / calls), 免得只看 p95 忘了错误率。

分位数用**最近秩法**(nearest-rank): 小样本下比插值更不容易编出不存在的数。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Sequence


def percentile(values: Sequence[int], q: float) -> int | None:
    """最近秩分位数; 空样本返回 None(**不返回 0**)。

    >>> percentile([10, 20, 30, 40], 0.5)
    20
    >>> percentile([], 0.5) is None
    True
    """
    if not values:
        return None
    xs = sorted(values)
    # 标准最近秩: 第 ceil(q*n) 个值(q<=0 取最小)。偶数样本取**较小的一侧**,
    # 例如 [10,20,30,40] 的 p50 = 20 —— 不插值, 避免小样本上编出不存在的数。
    idx = max(0, math.ceil(q * len(xs)) - 1)
    return xs[min(idx, len(xs) - 1)]


def aggregate_latency(rows: Iterable[Any], *, days: int, today: str) -> dict[str, Any]:
    """把 `skill_usage` 行聚成"按天 + 按 skill"的耗时/错误视图。

    `rows` 需有 `created_at`(datetime), `skill_name`, `status_code`, `duration_ms` 四个属性
    (ORM 行或任何同形对象均可 —— 便于单测不碰数据库)。
    """
    by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "errors": 0, "durs": []})
    by_skill: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "errors": 0, "durs": [], "users": set()})
    total_calls = total_errors = 0
    total_durs: list[int] = []
    no_duration = 0

    for r in rows:
        dur = int(getattr(r, "duration_ms", 0) or 0)
        status = int(getattr(r, "status_code", 200) or 200)
        skill = str(getattr(r, "skill_name", "") or "(未命名)")
        created = getattr(r, "created_at", None)
        day = created.strftime("%Y-%m-%d") if created is not None else "未知日期"

        total_calls += 1
        if dur > 0:
            total_durs.append(dur)
        else:
            no_duration += 1
        if status >= 400:
            total_errors += 1

        d = by_day[day]
        d["calls"] += 1
        if status >= 400:
            d["errors"] += 1
        if dur > 0:
            d["durs"].append(dur)

        s = by_skill[skill]
        s["calls"] += 1
        if status >= 400:
            s["errors"] += 1
        if dur > 0:
            s["durs"].append(dur)
        uid = getattr(r, "user_id", None)
        if uid:
            s["users"].add(str(uid))

    def _finish(key: str, name: Any, rec: dict[str, Any]) -> dict[str, Any]:
        """把一组调用收口成一行: 分位数只由**已记录耗时**的样本算。"""
        durs: list[int] = rec.pop("durs")
        users = rec.pop("users", None)
        out: dict[str, Any] = {
            key: name,
            "calls": rec["calls"],
            "errors": rec["errors"],
            "p50_ms": percentile(durs, 0.5),
            "p95_ms": percentile(durs, 0.95),
            "latency_samples": len(durs),
        }
        if users is not None:
            out["users"] = len(users)
        return out

    day_rows = [
        _finish("day", k, {"calls": v["calls"], "errors": v["errors"], "durs": v["durs"]})
        for k, v in sorted(by_day.items())
    ]
    skill_rows = sorted(
        (_finish("skill", k, {"calls": v["calls"], "errors": v["errors"],
                              "durs": v["durs"], "users": v["users"]}) for k, v in by_skill.items()),
        key=lambda x: (-x["calls"], str(x["skill"])),
    )

    return {
        "days": days,
        "as_of": today,
        "totals": {
            "calls": total_calls,
            "errors": total_errors,
            "p50_ms": percentile(total_durs, 0.5),
            "p95_ms": percentile(total_durs, 0.95),
        },
        "by_day": day_rows,
        "by_skill": skill_rows,
        "latency_note": (
            f"耗时只统计已记录耗时的调用({len(total_durs)} 次); "
            f"另有 {no_duration} 次未记录耗时, 不参与分位数(未记录 ≠ 耗时 0)"
        ),
    }
