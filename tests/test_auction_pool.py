"""竞价异动池测试(2026-08-24 v0.3.2 字段口径二次修正)。

覆盖:
- fetch_auction_anomaly: 市场映射 CN->USHA / SZ->USZA, DataFrame 转 dict, 代码归一化
- 30s 进程内缓存命中 / 过期
- 数据源不可用(未安装/抛异常) -> [] 容错
- sync_auction_to_db / get_anomaly_history: 用独立内存 SQLite 引擎验证 DB 读写
- register_cron: 复用现有调度器(不新开), job 成功注册 / 传入 None 不崩
- 字段口径(2026-08-24 v0.3.2):
  * 实测 thsdk 仅返回 6 列, "价格" 列不是价格而是异动幅度小数比例。
  * gap_pct / withdraw_rate 在 _to_records 内基于 异动类型 + 价格列 直接推导。
  * volume_ratio 数据源不提供, 固定 None。
  * 详细的"急速/大幅异动 → gap_pct / 涨停撤单 → withdraw_rate / 试盘 → None"
    推导逻辑见 tests/test_auction_gap.py。
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, Mock

import pytest

_FX = os.path.join(os.path.dirname(__file__), "fixtures")
if _FX not in sys.path:
    sys.path.insert(0, _FX)
import mock_main_flow as mmf  # noqa: E402

from src.core import auction_pool  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pool_cache():
    auction_pool.clear_cache()
    yield
    auction_pool.clear_cache()


@pytest.fixture
def mock_thsdk_l2(monkeypatch):
    """注入 data_source.thsdk_l2 内存桩(get_auction_anomaly 返回 fake DF)。"""
    mod = mmf.fake_thsdk_l2_module()
    mod.get_auction_anomaly = MagicMock(return_value=mmf.fake_auction_df())
    monkeypatch.setitem(sys.modules, "data_source.thsdk_l2", mod)
    return mod


def test_fetch_cn_maps_to_usha(mock_thsdk_l2):
    """默认 market=CN -> 映射 USHA, 且 DataFrame 正确转 list[dict]。

    2026-08-24 v0.3.2 口径: fake_auction_df 用真实口径,
      002361 大幅高开 + 价格=0.0335 -> gap_pct = 3.35
      600000 大幅低开 + 价格=-0.0178 -> gap_pct = -1.78
    withdraw_rate / volume_ratio 字段:
      volume_ratio 固定 None(数据源不提供)
      withdraw_rate 仅"涨停撤单/跌停撤单"类型填入(本 fixture 不含此类 -> None)
    """
    recs = auction_pool.fetch_auction_anomaly("CN")
    mock_thsdk_l2.get_auction_anomaly.assert_called_once_with("USHA")
    assert len(recs) == 2
    first = recs[0]
    assert first["symbol"] == "002361"
    assert first["code"] == "002361"
    assert first["name"] == "神剑股份"
    # 2026-08-24 v0.3.2: gap_pct 由异动类型 + 价格列直接推导, 无需 klines
    assert first["gap_pct"] == pytest.approx(3.35, abs=0.01)
    assert first["withdraw_rate"] is None
    assert first["volume_ratio"] is None
    # 内部字段(供调试 / 测试断言可见)
    assert first["price_raw"] == pytest.approx(0.0335)
    assert first["anomaly_type"] == "大幅高开"


def test_fetch_sz_maps_to_usza(mock_thsdk_l2):
    """market=SZ -> USZA。"""
    auction_pool.fetch_auction_anomaly("SZ")
    mock_thsdk_l2.get_auction_anomaly.assert_called_once_with("USZA")


def test_fetch_symbol_normalize(mock_thsdk_l2):
    """thsdk 前缀(USZA002361)与交易所后缀(002361.SZ)都归一化到 6 位代码。"""
    import pandas as pd

    df = pd.DataFrame(
        [
            {"时间": "09:25", "价格": 10.0, "总金额": 100.0,
             "代码": "USZA000001", "名称": "平安银行", "异动类型1": "高开"},
            {"时间": "09:25", "价格": 20.0, "总金额": 200.0,
             "代码": "002361.SZ", "名称": "神剑", "异动类型1": "高开"},
        ]
    )
    mock_thsdk_l2.get_auction_anomaly.return_value = df
    recs = auction_pool.fetch_auction_anomaly("CN")
    symbols = {r["symbol"] for r in recs}
    assert symbols == {"000001", "002361"}


def test_fetch_cache_hit(mock_thsdk_l2):
    """30s 内二次调用命中缓存, get_auction_anomaly 只调一次。"""
    auction_pool.fetch_auction_anomaly("CN")
    auction_pool.fetch_auction_anomaly("CN")
    assert mock_thsdk_l2.get_auction_anomaly.call_count == 1


def test_fetch_cache_expired(mock_thsdk_l2, monkeypatch):
    """超过 30s TTL 后重新拉取。

    2026-08-24 v0.3.2: fetch_auction_anomaly 不再调 SQLAlchemy/连接池, fake_time 仍难精准控制。
    改为直接预置 _cache(写入一个"很旧时间戳"), 第 2 次 fetch 时 now - cached[0] 必然 > 30s,
    走刷新分支。
    """
    # 预置一个 100s 前的缓存(模拟"已经过期 30s 缓存")
    auction_pool._cache["CN"] = (time.time() - 100.0, [{"symbol": "000001", "name": "stale"}])
    auction_pool.fetch_auction_anomaly("CN")
    # 缓存过期 -> 重新拉取, 应再调一次 get_auction_anomaly
    assert mock_thsdk_l2.get_auction_anomaly.call_count == 1


def test_fetch_thsdk_unavailable_fallback(mock_thsdk_l2):
    """数据源抛异常 -> 返回 [], 不崩。"""
    mock_thsdk_l2.get_auction_anomaly.side_effect = Exception("thsdk 连接失败")
    assert auction_pool.fetch_auction_anomaly("CN") == []


def _thsdk_actually_importable() -> bool:
    """2026-08-20 辅助: 检测 thsdk 在当前环境下能否真正 import。"""
    try:
        import data_source.thsdk_l2  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    _thsdk_actually_importable(),
    reason="thsdk 实际可 import (本环境已装 thsdk); 这个用例只验证 ImportError 路径",
)
def test_fetch_thsdk_module_missing(monkeypatch):
    """thsdk 模块未安装(ImportError) -> 返回 [] 容错。

    2026-08-20 修复: 同 test_main_flow_compare.test_thsdk_module_missing
    — thsdk 实际安装时 delitem 后仍可重导入, 触发不到 ImportError 分支。
    """
    if "data_source.thsdk_l2" in sys.modules:
        monkeypatch.delitem(sys.modules, "data_source.thsdk_l2")
    assert auction_pool.fetch_auction_anomaly("CN") == []


# ── DB 读写: 用独立内存 SQLite 引擎, 避免污染真实 data/panwatch.db ──────────
@pytest.fixture
def in_mem_db(monkeypatch):
    """注入内存 SQLite 引擎 + 建 auction_anomaly_records 表, 全程隔离真实 DB。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.web import models
    from src.web import database as _db

    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(_db, "engine", engine)
    monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(_db, "IS_PG", False)
    # sync 里走 acquire_write -> 其上引用 _db.IS_PG(False 则取 sqlite 信号量, OK)
    return models


def test_sync_and_history_then_clear(in_mem_db):
    """同步落库 + 查询历史, 均走内存库。"""
    recs = [
        {"symbol": "002361", "name": "神剑股份", "gap_pct": 3.38,
         "withdraw_rate": 0.243, "volume_ratio": 2.5},
        {"symbol": "600000", "name": "浦发银行", "gap_pct": -1.2,
         "withdraw_rate": 0.1, "volume_ratio": 0.8},
    ]
    n = auction_pool.sync_auction_to_db(recs)
    assert n == 2

    hist = auction_pool.get_anomaly_history("002361", days=5)
    assert len(hist) == 1
    assert hist[0]["symbol"] == "002361"
    assert hist[0]["gap_pct"] == pytest.approx(3.38)

    # 其他代码查不到
    assert auction_pool.get_anomaly_history("999999", days=5) == []


def test_sync_empty_returns_zero(in_mem_db):
    """空 records 不写库, 返回 0。"""
    assert auction_pool.sync_auction_to_db([]) == 0


# ── register_cron: 复用现有 APScheduler, 不新开 ────────────────────────────
def test_register_cron_reuses_scheduler():
    """把 job 加到传入的现有调度器上, 且 id 唯一、触发时刻正确。"""
    sched = Mock()
    assert auction_pool.register_cron(sched) is True
    sched.add_job.assert_called_once()
    args = sched.add_job.call_args[0]
    kw = sched.add_job.call_args.kwargs if hasattr(sched.add_job.call_args, "kwargs") else sched.add_job.call_args[1]
    assert args[1] == "cron"          # trigger 以位置参数传入
    assert kw["day_of_week"] == "mon-fri"
    assert kw["hour"] == 9
    assert kw["minute"] == 25
    assert kw["id"] == "auction_anomaly_daily_sync"
    assert kw.get("replace_existing") is True


def test_register_cron_none_safe():
    """传入 None / 无 add_job 对象 -> 返回 False, 不崩。"""
    assert auction_pool.register_cron(None) is False
    assert auction_pool.register_cron(object()) is False
