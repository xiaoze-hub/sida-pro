# -*- coding: utf-8 -*-
"""mainline_rank_daily 当日快照清理(2026-11-11)。

缺陷(生产实测): `_upsert_today_snapshot` 只 upsert 从不删除 —— 同日盘中某次计算
入榜的组, 后续重算掉出榜单后**行还在**。实测 2026-11-11 该表 23 行, 其中
`造纸 rank=10` 是盘中遗留(现算只有 1 只涨停、9 组入榜, 造纸不在榜单), 该行仍在
⇒ `_enrich_with_rank_change` 会拿过期名次算 rank_change。

修法: 同一事务内「先 upsert 本次全部行, 再 DELETE date=:d AND name NOT IN (本次名单)」。
本次 payload 为空(或全部无 name) ⇒ **不删任何行**(保守: 分不清"没算"和"全没了")。

本文件钉(隔离 SQLite, 不碰真库/不联网):
  ① 旧行 + 少一组的 payload → 旧行被删, 其余行 rank/score 被覆盖;
  ② payload 为空(及列表里全无 name) → 一行都不删;
  ③ 幂等: 同 payload 连写两次 → 行数/内容不变;
  ④ 不同日期互不影响(只清 date=本次数据日);
  ⑤ upsert 与 DELETE 同一事务: DELETE 失败 → upsert 一并回滚, 且异常静默不抛出。
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text

from src.web.api import market_mainline as mm

D_TODAY = "2026-11-11"
D_PREV = "2026-11-10"


# 真引擎在 import 期抓一份: ⑤ 的用例会把 src.web.database.engine 换成"DELETE 必炸"的
# 包装对象, 断言/播种必须仍走真引擎, 否则 helper 一起去炸(实测踩过)。
from src.web.database import engine as _REAL_ENGINE  # noqa: E402


def _engine():
    return _REAL_ENGINE


def _clear() -> None:
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("DELETE FROM mainline_rank_daily WHERE date IN (:a, :b)"),
                {"a": D_TODAY, "b": D_PREV},
            )
    except Exception:  # noqa: BLE001 — 表缺失等不该让用例 setup 炸
        pass


def _seed(rows: list[tuple[str, str, int, float]]) -> None:
    with _engine().begin() as conn:
        for d, name, rank, score in rows:
            conn.execute(
                text(
                    "INSERT INTO mainline_rank_daily (date, name, rank, score)"
                    " VALUES (:d, :n, :r, :s)"
                ),
                {"d": d, "n": name, "r": rank, "s": score},
            )


def _rows(d: str) -> dict[str, tuple[int, float]]:
    with _engine().connect() as conn:
        rs = conn.execute(
            text("SELECT name, rank, score FROM mainline_rank_daily WHERE date = :d"),
            {"d": d},
        ).fetchall()
    return {str(r[0]): (int(r[1]), float(r[2])) for r in rs}


@pytest.fixture(autouse=True)
def _isolate():
    _clear()
    yield
    _clear()


# ──────────── ① 遗留行被清理 ────────────
def test_stale_row_from_earlier_intraday_run_is_deleted():
    """盘中某次入榜、后续掉出榜单的组 → 该行必须被删, 不能留过期名次。"""
    _seed(
        [
            (D_TODAY, "AI算力", 1, 90.0),
            (D_TODAY, "商业航天", 2, 80.0),
            (D_TODAY, "造纸", 10, 5.0),  # 盘中的遗留行
        ]
    )
    # 现算: 只有 2 组入榜, 造纸掉出
    mm._upsert_today_snapshot(
        D_TODAY,
        [
            {"name": "AI算力", "rank": 1, "score": 95.0},
            {"name": "商业航天", "rank": 2, "score": 82.0},
        ],
    )

    rows = _rows(D_TODAY)
    assert "造纸" not in rows, f"过期遗留行没被删: {rows}"
    assert rows == {"AI算力": (1, 95.0), "商业航天": (2, 82.0)}


def test_rank_swap_overwrites_without_leaving_duplicates():
    """同日重算名次互换 → 覆盖为新值, 行数不增。"""
    _seed([(D_TODAY, "AI算力", 1, 90.0), (D_TODAY, "商业航天", 2, 80.0)])
    mm._upsert_today_snapshot(
        D_TODAY,
        [
            {"name": "商业航天", "rank": 1, "score": 88.0},
            {"name": "AI算力", "rank": 2, "score": 70.0},
        ],
    )
    assert _rows(D_TODAY) == {"商业航天": (1, 88.0), "AI算力": (2, 70.0)}


# ──────────── ② 空 payload 保守不删 ────────────
def test_empty_payload_deletes_nothing():
    """本次没算出榜单 ≠ 今日榜单全没了 —— 空 payload 一律不删。"""
    _seed([(D_TODAY, "AI算力", 1, 90.0), (D_TODAY, "造纸", 10, 5.0)])
    mm._upsert_today_snapshot(D_TODAY, [])
    assert _rows(D_TODAY) == {"AI算力": (1, 90.0), "造纸": (10, 5.0)}


def test_payload_without_any_name_deletes_nothing():
    """列表非空但全无 name(脏载荷) → 也不能当成"全没了"去清表。"""
    _seed([(D_TODAY, "AI算力", 1, 90.0)])
    mm._upsert_today_snapshot(D_TODAY, [{"rank": 1, "score": 1.0}, {"name": "", "rank": 2}])
    assert _rows(D_TODAY) == {"AI算力": (1, 90.0)}


# ──────────── ③ 幂等 ────────────
def test_repeated_write_is_idempotent():
    payload = [
        {"name": "AI算力", "rank": 1, "score": 90.0},
        {"name": "商业航天", "rank": 2, "score": 80.0},
    ]
    mm._upsert_today_snapshot(D_TODAY, payload)
    first = _rows(D_TODAY)
    mm._upsert_today_snapshot(D_TODAY, payload)
    assert _rows(D_TODAY) == first == {"AI算力": (1, 90.0), "商业航天": (2, 80.0)}


# ──────────── ④ 不同日期互不影响 ────────────
def test_other_dates_are_untouched():
    _seed([(D_PREV, "AI算力", 3, 60.0), (D_PREV, "造纸", 4, 50.0)])
    _seed([(D_TODAY, "AI算力", 1, 90.0), (D_TODAY, "造纸", 9, 5.0)])

    mm._upsert_today_snapshot(D_TODAY, [{"name": "AI算力", "rank": 1, "score": 91.0}])

    assert _rows(D_TODAY) == {"AI算力": (1, 91.0)}
    assert _rows(D_PREV) == {"AI算力": (3, 60.0), "造纸": (4, 50.0)}, "不得动其它日期的历史快照"


# ──────────── ⑤ 同一事务 + 失败静默 ────────────
class _FailingDeleteConn:
    """代理连接: DELETE 语句炸, 其余原样透传(用来验事务原子性)。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, stmt, params=None):
        if "DELETE" in str(stmt).upper():
            raise RuntimeError("boom delete")
        return self._conn.execute(stmt, params or {})


class _EngineWithFailingDelete:
    def __init__(self, eng):
        self._eng = eng

    @contextmanager
    def begin(self):
        with self._eng.begin() as conn:
            yield _FailingDeleteConn(conn)


def test_delete_failure_rolls_back_upsert_and_stays_silent(monkeypatch):
    """DELETE 与 upsert 必须同一事务: 删除失败 ⇒ 本次 upsert 一并回滚, 且异常不外抛。"""
    from src.web import database as db_mod

    _seed([(D_TODAY, "AI算力", 5, 10.0), (D_TODAY, "造纸", 10, 5.0)])
    monkeypatch.setattr(db_mod, "engine", _EngineWithFailingDelete(_engine()))

    mm._upsert_today_snapshot(
        D_TODAY,
        [{"name": "AI算力", "rank": 1, "score": 99.0}, {"name": "商业航天", "rank": 2, "score": 80.0}],
    )  # 不得抛异常

    assert _rows(D_TODAY) == {"AI算力": (5, 10.0), "造纸": (10, 5.0)}, (
        "事务未回滚: 删除失败时 upsert 应一并撤销"
    )
