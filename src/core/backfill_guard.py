# -*- coding: utf-8 -*-
"""回填防污染契约(v0.5.80 B3)。

## 为什么有这个东西
2026-09-12 的 v0.5.73 事故: 从 `limit_up_events` 回填市场情绪周期, 跑出 2522 天 / 181 段,
数字看着都对, 唯一的问题是**早年库里只有零星几条事件**(日均 1~9 条 vs 2025 年起 107 条)。
稀疏日的"首板数=0.3"被当成事实, 再经 EMA 平滑 + 2 日确认**跨日传播**, 把阈值标定、
段数、平均时长、去向概率一起带偏 —— 面板会自信地输出错结论。

修法当时是就地写的(`market_phase.eligible_dates` + `_purge_uncovered`)。本模块把它提成
**所有回填路径共用的两条硬规则**, 免得下一个回填(龙虎榜/快照/事件类)再踩一遍:

1. **覆盖不足的日子不写**: 每个日期先数"库里当天有多少条事件", 低于门槛直接排除,
   且被排除的日期要如实返回(不能静默);
2. **已经写进去的要清掉**: 只保留可信日期集, 其余删除 —— 光"以后不写"不够,
   历史脏行会继续参与标定。

## 安全
表名/列名不能走参数绑定, 所以先过标识符白名单校验(只允许 `[A-Za-z_][A-Za-z0-9_]*`),
不合法直接抛错, 不拼进 SQL。删除按"先查实际存在的日期, 再分片 DELETE ... IN (...)"做,
避免 `NOT IN (几千个参数)` 撞上 SQLite 变量数上限, 也避免删到不存在的行空转。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DELETE_CHUNK = 400


def check_ident(name: str, kind: str = "标识符") -> str:
    """表名/列名白名单校验(SQL 里唯一不能参数绑定的地方)。"""
    s = str(name or "")
    if not _IDENT.match(s):
        raise ValueError(f"非法{kind}: {name!r}(只允许 [A-Za-z_][A-Za-z0-9_]*)")
    return s


def coverage_gate(counts: dict[str, int], min_events: int) -> tuple[list[str], list[str]]:
    """纯函数: 日期→事件数 → (可信日期升序, 被排除日期升序)。

    `min_events <= 0` 视为"不设门槛"(调用方显式决定, 例如测试), 但会把全部日期列为可信,
    所以排除项仍如实返回空列表而不是 None —— 上层要能区分"没有脏日"与"没查"。
    """
    floor = int(min_events)
    ok = sorted(d for d, n in counts.items() if n >= floor)
    dropped = sorted(d for d, n in counts.items() if n < floor)
    return ok, dropped


def count_by_field(rows: Iterable[dict], field: str) -> dict[str, int]:
    """按某字段(如 trade_date)计数; 字段缺失/为空的行不计入(不猜日期)。"""
    out: dict[str, int] = {}
    for r in rows:
        key = str((r.get(field) if isinstance(r, dict) else None) or "").strip()
        if not key:
            continue
        out[key] = out.get(key, 0) + 1
    return out


def purge_rows_outside(
    keep: set[str],
    *,
    table: str,
    date_col: str,
    normalize: bool = True,
    engine: Any = None,
) -> int:
    """删掉 `table.date_col` 不在 `keep` 里的行, 返回删除的日期数。

    `keep` 与库内日期都先去掉 `-` 再比(库里可能是 'YYYY-MM-DD' 而事件表是 'YYYYMMDD');
    `normalize=False` 时按原样字符串比。`keep` 为空 = 什么都不保留 —— 这种"清库"级别的
    操作必须显式调用, 所以这里**拒绝执行并返回 0**, 防止门槛配错把整张表删空。
    """
    t = check_ident(table, "表名")
    c = check_ident(date_col, "列名")
    if not keep:
        logger.warning("purge_rows_outside(%s.%s) 被跳过: keep 为空集(疑似门槛配置错误)", t, c)
        return 0

    from sqlalchemy import text

    if engine is None:
        from src.db.session import engine as engine  # noqa: F811

    def _norm(v: Any) -> str:
        s = str(v)
        return s.replace("-", "") if normalize else s

    keep_set = {_norm(k) for k in keep}
    removed = 0
    with engine.begin() as conn:
        present = conn.execute(text(f"SELECT DISTINCT {c} FROM {t}")).fetchall()  # noqa: S608
        victims = [r[0] for r in present if _norm(r[0]) not in keep_set]
        for i in range(0, len(victims), DELETE_CHUNK):
            part = victims[i:i + DELETE_CHUNK]
            params = {f"v{n}": v for n, v in enumerate(part)}
            placeholders = ", ".join(f":v{n}" for n in range(len(part)))
            conn.execute(
                text(f"DELETE FROM {t} WHERE {c} IN ({placeholders})"),  # noqa: S608
                params,
            )
            removed += len(part)
    if removed:
        logger.info("%s.%s 清掉不可信日期 %s 个", t, c, removed)
    return removed
