"""隔离 PostgreSQL 上真跑回测日线的首末两端（默认跳过）。

monkeypatch 只能证明传给仓储的 kwargs，证明不了 `:ce IS NULL` / `ts < :ce` 的绑定与
`Asia/Shanghai` 日期标签在真实 PG 上是否成立 —— 而 `load_qfq_bars` 会吞掉异常返回空，
参数类型推断失败在生产里只会表现为「库里查不到、退到联网兜底」。

    SIDA_BT_TEST_PG_URL='postgresql+psycopg2:///bt_pg_verify?host=/var/run/postgresql' \
      python -m pytest tests/test_backtest_klines_window_pg.py -q

安全闸: 只连库名以 `bt_pg_` 开头的隔离库，且不当成生产库名。
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.skipif(
    not os.environ.get("SIDA_BT_TEST_PG_URL"),
    reason="未提供 SIDA_BT_TEST_PG_URL(隔离临时 PG 库), 跳过真实 PG 验证",
)

SYMBOL = "600029"


@pytest.fixture()
def pg(monkeypatch):
    from src.db import klines_repo, session

    url = make_url(os.environ["SIDA_BT_TEST_PG_URL"])
    assert url.get_backend_name() == 'postgresql' and (url.database or '').startswith('bt_pg_')
    assert url.host is None and str(url.query.get('host', '')).startswith('/'), '仅允许隔离 Unix socket'
    engine = create_engine(url, future=True)
    with engine.begin() as c:
        assert c.execute(text('SELECT inet_server_addr() IS NULL')).scalar_one()
        c.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        c.execute(text("""
            CREATE TEMP TABLE klines (
                ts timestamptz NOT NULL, symbol varchar(16) NOT NULL, market varchar(8) NOT NULL,
                period varchar(8) NOT NULL, source varchar(16), open numeric, high numeric,
                low numeric, close numeric, volume numeric, quality_flag int, adjust varchar(8)
            ) ON COMMIT DROP"""))
        # 上一个上海日 23:59:59 与当日零点各一根, 外加一根未来柱。
        c.execute(text("""
            INSERT INTO klines VALUES
              ('2026-11-26 15:59:59+00',:s,'CN','1d','tq',1,1,1,11,10,1,'qfq'),
              ('2026-11-26 16:00:00+00',:s,'CN','1d','tq',1,1,1,10,100,1,'qfq'),
              ('2026-11-30 16:00:00+00',:s,'CN','1d','tq',1,1,1,99,100,1,'qfq'),
              ('2026-11-26 16:00:00+00','00700','HK','1d','tencent',1,1,1,10,100,1,'qfq'),
              ('2026-11-26 16:00:00+00','AAPL','US','1d','tencent',1,1,1,10,100,1,'qfq')
        """), {"s": SYMBOL})
        monkeypatch.setattr(session, "engine", SimpleNamespace(connect=lambda: nullcontext(c)))
        yield klines_repo.load_qfq_bars
    engine.dispose()


@pytest.mark.parametrize("ceiling,expected", [
    (date(2026, 11, 27), ["2026-11-27"]),          # 含当日, 排除次日
    (date(2026, 12, 1), ["2026-11-27", "2026-12-01"]),
    (None, ["2026-11-27", "2026-12-01"]),          # 无上界的路径也必须正常绑定
])
def test_window_bounds_execute_on_real_pg(pg, ceiling, expected):
    rows = pg(SYMBOL, "CN", 30, date_cutoff=date(2026, 11, 27), date_ceiling=ceiling)
    assert [row["date"] for row in rows] == expected


def test_ceiling_none_keeps_the_cutoff_bar(pg):
    rows = pg(SYMBOL, "CN", 30, date_cutoff=date(2026, 11, 27))
    assert rows and rows[0]["close"] == 10.0


@pytest.mark.parametrize('symbol,market', [('00700', 'HK'), ('AAPL', 'US')])
def test_non_cn_storage_labels_agree_with_window_in_utc_session(pg, symbol, market):
    day = date(2026, 11, 27)
    rows = pg(symbol, market, date_cutoff=day, date_ceiling=day)
    assert [row['date'] for row in rows] == [day.isoformat()]
