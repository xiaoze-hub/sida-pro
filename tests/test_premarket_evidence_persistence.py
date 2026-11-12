"""盘前分支真实落库链 —— 冻结证据/采样时点/盘后重跑(2026-11-12 复审修正)。

审计指出的四个问题, 本文件全部走**真实 `save_analysis` 路径**验证(不复制白名单):

① `save_analysis` 的 raw_data 白名单漏了 `demon_factors` / `demon_factors_frozen_at`
   → 正文按冻结快照写了「股性分 88」, 账本却一律 unknown;
② 同日 upsert 会销毁盘前凭证 → 凭证改为追加式表 `premarket_factor_evidence`;
③ 采样时点用 `raw_data.timestamp` 兜底 → 那是整份报告采集结束时刻, 不能证明快照采样时点;
   且不带时区的 naive 时间无法核对 09:30 边界, 一律 unknown;
④ 盘后重跑被当成当日盘前成绩 → 非盘前生成(preopen=false)或时点不可证明(preopen=null)
   的期次只展示真实生成时刻, 当日/T+1/T+3 一律留空。

测试库纪律: 每个用例 new 一个 `tmp_path` 下的独立 SQLite, 只用它; 不碰生产库,
不做全表清理, 也不依赖 conftest 的共享测试库(该库可能承载别的用户数据)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

REPORT = """
0. 事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""

DAY = "2026-11-11"
SNAP = {
    "600183": {
        "total": 88.0,
        "grade": "妖",
        "n_sealed": 12,
        "max_streak": 4,
        "participation": "活跃",
        "factor_date": "20261110",
    }
}


class _User:
    id = "u-p0"
    role = "owner"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一套全新 SQLite(文件在 tmp_path), 并把它接进真实落库路径。"""
    from src.db.models import AnalysisHistory, PremarketFactorEvidence, User
    from src.db.session import Base

    eng = create_engine(f"sqlite:///{tmp_path / 'p0.db'}", future=True)
    Base.metadata.create_all(
        eng,
        tables=[User.__table__, AnalysisHistory.__table__, PremarketFactorEvidence.__table__],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    # analysis_history / decisions / context_store 都是模块级 `from ... import SessionLocal`
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.analysis_history.SessionLocal", Session)
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: eng)

    # 日线：只用报告日之前的收盘做盘前位置/连板
    from src.core import decision_log

    monkeypatch.setattr(
        decision_log,
        "_close_series_pg",
        lambda _eng, _sym, _start: [
            ("2026-11-09", 10.0),
            ("2026-11-10", 10.5),
            ("2026-11-11", 12.0),
        ],
    )
    return SimpleNamespace(engine=eng, Session=Session)


def _data(
    *,
    gen: str = f"{DAY}T08:20:00+08:00",
    snapshot: dict | None = None,
    frozen: bool = True,
    declared_preopen: bool = True,
) -> dict:
    """模拟 collect() 的输出: 冻结快照 + 带时区的采样时点 + 追加式凭证。"""
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    snap = snapshot if snapshot is not None else SNAP
    data = {
        "timestamp": gen,
        "demon_factors": snap,
        "demon_factors_asof": "2026-11-10",
        "demon_factors_dropped": [],
        "demon_factors_preopen": declared_preopen,
        "demon_factors_frozen_at": gen if frozen else None,
        "demon_factors_source": SOURCE_TOP_N,
    }
    ev = build_snapshot_evidence(
        snapshot=snap,
        frozen_at=data["demon_factors_frozen_at"],
        report_day=DAY,
        dropped=[],
        asof="2026-11-10",
        source=SOURCE_TOP_N,
    )
    if ev:
        data["demon_factors_preopen"] = ev["preopen"]
        data["demon_factors_sha256"] = ev["snapshot_sha256"]
        data["_demon_evidence"] = ev
    return data


def _persist(content: str, data: dict) -> bool:
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    return agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="【盘前分析】测试", content=content),
        data=data,
        context=cast(Any, SimpleNamespace(user=None)),
    )


def _ledger():
    from src.web.api import decisions as dec

    return dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, _User()))


def _reports(db) -> list:
    from src.db.models import AnalysisHistory

    with db.Session() as s:
        return s.execute(select(AnalysisHistory).order_by(AnalysisHistory.id)).scalars().all()


def _evidence(db) -> list:
    from src.db.models import PremarketFactorEvidence

    with db.Session() as s:
        return (
            s.execute(
                select(PremarketFactorEvidence).order_by(PremarketFactorEvidence.id)
            )
            .scalars()
            .all()
        )


# ── ① 真实保存路径: 白名单带上冻结凭证, 账本与正文同源 ─────────────────────


def test_real_save_path_keeps_frozen_snapshot_and_ledger_uses_it(db):
    data = _data()
    assert _persist(REPORT, data) is True

    reports = _reports(db)
    assert len(reports) == 1
    raw = reports[0].raw_data or {}
    # 白名单必须包含凭证(曾经漏掉 → 正文/账本矛盾)
    assert raw["demon_factors"]["600183"]["total"] == 88.0
    assert raw["demon_factors_frozen_at"] == f"{DAY}T08:20:00+08:00"
    assert raw["demon_factors_preopen"] is True
    assert raw["demon_factors_source"]

    rows = _evidence(db)
    assert len(rows) == 1 and rows[0].preopen is True
    assert rows[0].generated_at == f"{DAY}T08:20:00+08:00"
    assert rows[0].snapshot_sha256  # 一致性指纹

    out = _ledger()
    item = out["items"][0]
    assert item["preopen"] is True and item["premarket_eligible"] is True
    assert item["generated_at"] == f"{DAY}T08:20:00+08:00"
    row = item["rows"][0]
    assert row["signals"]["demon"]["score"] == 88.0, "账本没读到真实落库的冻结快照"
    assert row["signals"]["price"]["close"] == 10.5
    assert item["pit"]["demon_source"]
    assert row["outcome"]["t0"] == pytest.approx((12.0 / 10.5 - 1) * 100, abs=0.05)


# ── ② 追加式: 同日盘前重跑不销毁凭证, 但正文仍是覆盖(不声称不可变) ─────────


def test_evidence_is_append_only_while_report_row_still_upserts(db):
    first = _data(gen=f"{DAY}T08:00:00+08:00")
    second = _data(
        gen=f"{DAY}T08:40:00+08:00",
        snapshot={
            "600183": {
                "total": 91.0,
                "grade": "极妖",
                "n_sealed": 13,
                "max_streak": 5,
                "participation": "高度参与",
                "factor_date": "20261110",
            }
        },
    )
    assert _persist(REPORT, first) is True
    assert _persist(REPORT + "\n第二版\n", second) is True

    reports = _reports(db)
    assert len(reports) == 1, "同日报告行是 upsert —— 这里如实体现覆盖风险"
    assert "第二版" in reports[0].content
    ev = _evidence(db)
    assert len(ev) == 2, "两次真实采样都要留在追加式凭证里"
    assert {e.preopen for e in ev} == {True}

    out = _ledger()
    row = out["items"][0]["rows"][0]
    # 对账用「最早一条可验证盘前」的真实采样, 而不是被后来重跑写进去的正文
    assert row["signals"]["demon"]["score"] == 88.0
    assert out["items"][0]["pit"]["evidence_rows"] == 2


# ── ③ 跨时区: 同一时刻不同偏移等价; 开盘后等同的 UTC 时间必须 unknown ──


def test_utc_offsets_are_normalized_before_judging_preopen(db):
    # 00:20 UTC == 08:20 北京 → 盘前
    assert _persist(REPORT, _data(gen=f"{DAY}T00:20:00+00:00")) is True
    ev = _evidence(db)
    assert ev[0].generated_at == f"{DAY}T08:20:00+08:00"
    assert ev[0].preopen is True
    assert _ledger()["items"][0]["rows"][0]["signals"]["demon"]["score"] == 88.0


def test_utc_time_after_open_is_not_premarket(db):
    # 02:00 UTC == 10:00 北京 → 开盘后, 不能当盘前
    assert _persist(REPORT, _data(gen=f"{DAY}T02:00:00+00:00")) is True
    ev = _evidence(db)
    assert ev[0].preopen is False
    item = _ledger()["items"][0]
    assert item["preopen"] is False and item["premarket_eligible"] is False
    assert item["generated_at"] == f"{DAY}T10:00:00+08:00"
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["rows"][0]["outcome"]["t0"] is None
    assert "非盘前" in (item["rows"][0]["outcome"]["note"] or "")


# ── ④ 盘后重跑: 不覆盖盘前记录, 也不被当成盘前成绩 ─────────────────────────


def test_post_close_rerun_cannot_overwrite_premarket_record(db):
    preopen = _data()
    assert _persist(REPORT, preopen) is True

    after_close = _data(gen=f"{DAY}T20:10:00+08:00")
    assert _persist(REPORT + "\n收盘后重跑\n", after_close) is False

    reports = _reports(db)
    assert len(reports) == 1
    assert "收盘后重跑" not in reports[0].content, "盘后重跑顶掉了当日盘前正文"
    ev = _evidence(db)
    assert len(ev) == 1 and ev[0].preopen is True, "盘后重跑销毁了盘前凭证"

    item = _ledger()["items"][0]
    assert item["preopen"] is True and item["premarket_eligible"] is True
    assert item["generated_at"] == f"{DAY}T08:20:00+08:00"


def test_post_close_rerun_without_fresh_sampling_also_cannot_overwrite(db):
    """盘后重跑连因子都没采到(无凭证)时, 同样不能顶掉盘前记录。"""
    assert _persist(REPORT, _data()) is True
    assert _persist(REPORT + "\n盘后无采样重跑\n", _data(gen=f"{DAY}T20:10:00+08:00", frozen=False)) is False
    reports = _reports(db)
    assert len(reports) == 1 and "盘后无采样重跑" not in reports[0].content
    assert _ledger()["items"][0]["preopen"] is True


def test_post_close_only_run_is_flagged_and_never_scored_as_premarket(db):
    assert _persist(REPORT, _data(gen=f"{DAY}T20:10:00+08:00")) is True

    out = _ledger()
    item = out["items"][0]
    assert item["preopen"] is False
    assert item["premarket_eligible"] is False
    assert item["generated_at"] == f"{DAY}T20:10:00+08:00"
    assert item["summary"] == {"rows": 1, "t0_scored": 0, "t0_up": 0, "t1_scored": 0, "t1_up": 0}
    row = item["rows"][0]
    assert row["outcome"]["t0"] is None and row["outcome"]["t1"] is None
    assert row["signals"]["demon"] is None
    assert "非盘前" in (row["outcome"]["note"] or "")


# ── ⑤ 缺凭证 / 冲突 / 指纹: 一律 unknown, 不拿 timestamp 或 hash 冒充证明 ──


def test_missing_frozen_at_with_only_timestamp_is_unknown(db):
    data = _data(gen=f"{DAY}T08:20:00+08:00", frozen=False)
    assert "_demon_evidence" not in data
    assert _persist(REPORT, data) is True

    assert _evidence(db) == [], "采样时点不可核验时不得落凭证"
    item = _ledger()["items"][0]
    assert item["preopen"] is None, "只有整份报告 timestamp → 时点不可证明, 不是盘前"
    assert item["premarket_eligible"] is False
    assert item["generated_at"] is None
    row = item["rows"][0]
    assert row["signals"]["demon"] is None
    assert "timestamp" in (item["pit"]["demon_reason"] or "")
    assert "不可证明" in (row["outcome"]["note"] or "")


def test_naive_sampling_time_is_unknown_even_if_locally_preopen(db):
    """naive 08:20 在时区未知时可以是任何时刻 —— 不猜, 按 unknown。"""
    from src.core import premarket_ambush as pa

    data = _data()
    data["demon_factors_frozen_at"] = f"{DAY}T08:20:00"  # 去掉时区
    data["_demon_evidence"] = {
        **_data()["_demon_evidence"],
        "generated_at": f"{DAY}T08:20:00",
    }
    # save_analysis 的凭证写入会拒绝不可核验时点 → 整体保存失败(不落半真半假记录)
    assert _persist(REPORT, data) is False
    assert _evidence(db) == []
    assert _reports(db) == []

    out = pa.demon_evidence_from_record(
        {"demon_factors_frozen_at": f"{DAY}T08:20:00", "demon_factors": SNAP}, DAY
    )
    assert out["verified"] is False
    assert "时区" in (out["reason"] or "")


def test_conflicting_preopen_flag_and_time_is_unknown(db):
    """raw_data 自称 preopen=True, 但采样时点在收盘后 → 凭证自相矛盾, unknown。"""
    raw = {
        "timestamp": f"{DAY}T20:10:00+08:00",
        "demon_factors": SNAP,
        "demon_factors_frozen_at": f"{DAY}T20:10:00+08:00",
        "demon_factors_preopen": True,
    }
    from src.db.models import AnalysisHistory

    with db.Session() as s:
        s.add(
            AnalysisHistory(
                user_id=None,
                agent_name="premarket_outlook",
                stock_symbol="*",
                analysis_date=DAY,
                title="旧记录(无追加式凭证)",
                content=REPORT,
                raw_data=raw,
                status="success",
            )
        )
        s.commit()

    item = _ledger()["items"][0]
    assert item["rows"][0]["signals"]["demon"] is None
    assert "矛盾" in (item["pit"]["demon_reason"] or "")
    assert item["preopen"] is None


def test_caller_cannot_force_post_close_sample_to_preopen(db):
    """凭证的 preopen 由时点推导, 调用方传 True 也不会被采信。"""
    data = _data(gen=f"{DAY}T20:10:00+08:00", declared_preopen=True)
    assert data["_demon_evidence"]["preopen"] is False  # build 阶段已按时间纠正
    assert _persist(REPORT, data) is True
    ev = _evidence(db)
    assert ev[0].preopen is False
    assert _ledger()["items"][0]["preopen"] is False


def test_snapshot_fingerprint_mismatch_is_unknown_not_provenance(db):
    """指纹只做一致性校验: 存储被改动 → unknown; 对得上也不证明来源/时点。"""
    assert _persist(REPORT, _data()) is True
    from src.db.models import PremarketFactorEvidence

    with db.Session() as s:
        row = s.execute(select(PremarketFactorEvidence)).scalars().one()
        row.snapshot = {"600183": {"total": 1.0, "factor_date": "20261110"}}
        s.commit()

    item = _ledger()["items"][0]
    assert item["rows"][0]["signals"]["demon"] is None
    assert "一致性" in (item["pit"]["demon_reason"] or "")


def test_migration_creates_append_only_evidence_table(tmp_path):
    """生产迁移与模型要能对上: 新库跑一次 247 就有凭证表(只增不改)。"""
    from sqlalchemy import inspect

    from src.web.migrations import _m247_premarket_factor_evidence

    eng = create_engine(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    with eng.begin() as conn:
        _m247_premarket_factor_evidence(conn)
    cols = {c["name"] for c in inspect(eng).get_columns("premarket_factor_evidence")}
    assert {
        "analysis_date",
        "generated_at",
        "preopen",
        "source",
        "snapshot",
        "snapshot_sha256",
    } <= cols
    # 幂等: 再跑一次不报错
    with eng.begin() as conn:
        _m247_premarket_factor_evidence(conn)


def test_legacy_record_without_evidence_row_is_unknown_not_premarket(db):
    """迁移前的旧记录: 没有带时区采样时点 → 不能证明盘前, 也不计成绩。"""
    from src.db.models import AnalysisHistory

    with db.Session() as s:
        s.add(
            AnalysisHistory(
                user_id=None,
                agent_name="premarket_outlook",
                stock_symbol="*",
                analysis_date=DAY,
                title="旧记录",
                content=REPORT,
                raw_data={
                    "timestamp": f"{DAY}T08:20:00",
                    "demon_factors": SNAP,
                    "demon_factors_frozen_at": f"{DAY}T08:20:00",
                },
                status="success",
            )
        )
        s.commit()

    item = _ledger()["items"][0]
    assert item["preopen"] is None
    assert item["premarket_eligible"] is False
    assert item["pit"]["demon_evidence"] is False
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["rows"][0]["outcome"]["t0"] is None
    assert item["summary"]["t0_scored"] == 0
