"""盘前分支真实落库链 —— 冻结证据/采样时点/盘后重跑/报告级盘前门槛（2026-11-12 P0）。

审计指出的问题, 本文件全部走**真实 `save_analysis` 路径**验证(不复制白名单):

① `save_analysis` 的 raw_data 白名单漏了 `demon_factors` / `demon_factors_frozen_at`
   → 正文按冻结快照写了「股性分 88」, 账本却一律 unknown;
② 同日 upsert 会销毁盘前凭证 → 凭证改为追加式表 `premarket_factor_evidence`,
   并按 `run_trace_id` + 相同 user scope 绑定到具体某一版报告;
③ 采样时点用 `raw_data.timestamp` 兜底 → 那是整份报告采集结束时刻, 不能证明快照采样时点;
   且不带时区的 naive 时间无法核对 09:30 边界, 一律 unknown;
④ 盘后重跑被当成当日盘前成绩 → 盘前判定改为「整份报告完成时刻 + 采集结束时刻都在 09:30 前」,
   非盘前生成(preopen=false)或时点不可证明(preopen=null)的期次只展示真实时刻,
   当日/T+1/T+3 一律留空;
⑤ 证据写失败会把整份报告回滚掉 → 现在正文先提交, 凭证单独一个事务, 失败时
   `SaveResult.evidence_saved=False` 显式告知调用方(报告不丢, 也不冒充完整)。

测试库纪律: 每个用例 new 一个 `tmp_path` 下的独立 SQLite, 只用它; 不碰生产库,
不做全表清理, 也不依赖 conftest 的共享测试库(该库可能承载别的用户数据)。
"""

from __future__ import annotations

import os
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
SNAP_V2 = {
    "600183": {
        "total": 91.0,
        "grade": "极妖",
        "n_sealed": 13,
        "max_streak": 5,
        "participation": "高度参与",
        "factor_date": "20261110",
    }
}


class _User:
    id = "u-p0"
    role = "owner"


_DB_ENV = (
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "SIDA_DB_URL",
    "SIDA_DB",
    "DATABASE_URL",
    "POSTGRES_DSN",
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一套全新 SQLite(文件在 tmp_path), 并把它接进真实落库路径。

    先断言「连的确实是临时库」并清掉所有 DB 环境变量, 再 monkeypatch —— 任何把
    生产/共享库带进来的路径都要在测试开始前炸掉, 而不是写坏了才发现。
    """
    from src.db.models import AnalysisHistory, PremarketFactorEvidence, User
    from src.db.session import Base

    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    db_path = tmp_path / "p0.db"
    url = f"sqlite:///{db_path}"
    assert str(tmp_path) in url, "测试库必须是本次用例的临时目录"
    assert not os.path.exists(db_path)

    eng = create_engine(url, future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)

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
    run: str | None = None,
    completed: str | None = None,
) -> dict:
    """模拟 collect() 的输出: 冻结快照 + 带时区的采样时点 + 追加式凭证。

    `completed` 是整份报告生成完成时刻（默认与采样时点相同 = 采样后马上出报告）。
    """
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    snap = snapshot if snapshot is not None else SNAP
    trace = run or f"run-{gen}"
    data = {
        "timestamp": gen,
        "run_trace_id": trace,
        "demon_factors": snap,
        "demon_factors_asof": "2026-11-10",
        "demon_factors_dropped": [],
        "demon_factors_preopen": declared_preopen,
        "demon_factors_frozen_at": gen if frozen else None,
        "demon_factors_source": SOURCE_TOP_N,
        "report_completed_at": completed or gen,
    }
    ev = build_snapshot_evidence(
        snapshot=snap,
        frozen_at=data["demon_factors_frozen_at"],
        report_day=DAY,
        dropped=[],
        asof="2026-11-10",
        source=SOURCE_TOP_N,
        run_trace_id=trace,
    )
    if ev:
        data["demon_factors_preopen"] = ev["preopen"]
        data["demon_factors_sha256"] = ev["snapshot_sha256"]
        data["_demon_evidence"] = ev
    return data


def _persist(content: str, data: dict, *, completed: str | None = None):
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    return agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="【盘前分析】测试", content=content),
        data=data,
        context=cast(Any, SimpleNamespace(user=None)),
        report_completed_at=completed or data.get("report_completed_at"),
    )


def _ledger(user: Any | None = None):
    from src.web.api import decisions as dec

    return dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, user or _User()))


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
    saved = _persist(REPORT, data)
    assert saved.body_saved is True and saved.evidence_saved is True

    reports = _reports(db)
    assert len(reports) == 1
    raw = reports[0].raw_data or {}
    # 白名单必须包含凭证(曾经漏掉 → 正文/账本矛盾)
    assert raw["demon_factors"]["600183"]["total"] == 88.0
    assert raw["demon_factors_frozen_at"] == f"{DAY}T08:20:00+08:00"
    assert raw["demon_factors_preopen"] is True
    assert raw["demon_factors_source"]
    # 版本身份与报告级盘前门槛都要落库
    assert raw["run_trace_id"] == data["run_trace_id"]
    assert raw["report_completed_at"] == f"{DAY}T08:20:00+08:00"
    assert raw["premarket_report_preopen"] is True

    rows = _evidence(db)
    assert len(rows) == 1 and rows[0].preopen is True
    assert rows[0].generated_at == f"{DAY}T08:20:00+08:00"
    assert rows[0].run_trace_id == data["run_trace_id"]
    assert rows[0].snapshot_sha256  # 一致性指纹

    out = _ledger()
    item = out["items"][0]
    assert item["preopen"] is True and item["premarket_eligible"] is True
    assert item["generated_at"] == f"{DAY}T08:20:00+08:00"
    assert item["run_trace_id"] == data["run_trace_id"]
    assert item["record_scope"] == "system"
    assert item["pit"]["evidence_matched"] == 1
    assert item["pit"]["evidence_source_kind"] == "appended"
    row = item["rows"][0]
    assert row["signals"]["demon"]["score"] == 88.0, "账本没读到真实落库的冻结快照"
    assert row["signals"]["price"]["close"] == 10.5
    assert item["pit"]["demon_source"]
    assert row["outcome"]["t0"] == pytest.approx((12.0 / 10.5 - 1) * 100, abs=0.05)


# ── ② 追加式 + 版本绑定: 盘前重跑只多一条凭证, 正文与因子必须同源 ─────────


def test_evidence_is_append_only_and_ledger_uses_the_same_run_as_the_body(db):
    first = _data(gen=f"{DAY}T08:00:00+08:00", run="run-v1")
    second = _data(gen=f"{DAY}T08:40:00+08:00", snapshot=SNAP_V2, run="run-v2")
    assert _persist(REPORT, first).body_saved is True
    assert _persist(REPORT + "\n第二版\n", second).body_saved is True

    reports = _reports(db)
    assert len(reports) == 1, "同日报告行是 upsert —— 这里如实体现覆盖风险"
    assert "第二版" in reports[0].content
    assert reports[0].raw_data["run_trace_id"] == "run-v2"
    ev = _evidence(db)
    assert len(ev) == 2, "两次真实采样都要留在追加式凭证里"
    assert {e.preopen for e in ev} == {True}
    assert {e.run_trace_id for e in ev} == {"run-v1", "run-v2"}

    out = _ledger()
    item = out["items"][0]
    row = item["rows"][0]
    # 正文是第二版 → 因子也必须是第二版的 91, 不能拿第一版的 88 凑(旧 bug: 按日期取最早)
    assert item["run_trace_id"] == "run-v2"
    assert item["pit"]["evidence_matched"] == 1
    assert row["signals"]["demon"]["score"] == 91.0
    assert item["generated_at"] == f"{DAY}T08:40:00+08:00"
    # 同日两版凭证都在(pit.evidence_rows), 但只有正文那一版参与对账(evidence_matched)
    assert item["pit"]["evidence_rows"] == 2
    assert item["pit"]["evidence_matched"] == 1
    # 同一 scope 的正文是 upsert, candidates 只有一行 —— 上一版正文不保留(已知边界)
    assert len(item["pit"]["candidates"]) == 1


# ── ③ 跨时区: 同一时刻不同偏移等价; 开盘后等同的 UTC 时间必须非盘前 ──


def test_utc_offsets_are_normalized_before_judging_preopen(db):
    # 00:20 UTC == 08:20 北京 → 盘前
    assert _persist(REPORT, _data(gen=f"{DAY}T00:20:00+00:00")).body_saved is True
    ev = _evidence(db)
    assert ev[0].generated_at == f"{DAY}T08:20:00+08:00"
    assert ev[0].preopen is True
    assert _ledger()["items"][0]["rows"][0]["signals"]["demon"]["score"] == 88.0


def test_utc_time_after_open_is_not_premarket(db):
    # 02:00 UTC == 10:00 北京 → 开盘后, 不能当盘前
    assert _persist(REPORT, _data(gen=f"{DAY}T02:00:00+00:00")).body_saved is True
    ev = _evidence(db)
    assert ev[0].preopen is False
    item = _ledger()["items"][0]
    assert item["preopen"] is False and item["premarket_eligible"] is False
    assert item["generated_at"] == f"{DAY}T10:00:00+08:00"
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["rows"][0]["outcome"]["t0"] is None
    assert "非盘前" in (item["rows"][0]["outcome"]["note"] or "")


# ── ④ 盘后重跑: 不覆盖盘前记录, 也不被当成盘前成绩, 但采样凭证不丢 ──────


def test_post_close_rerun_cannot_overwrite_premarket_record(db):
    assert _persist(REPORT, _data()).body_saved is True

    after_close = _data(gen=f"{DAY}T20:10:00+08:00", snapshot=SNAP_V2, run="run-after")
    saved = _persist(REPORT + "\n收盘后重跑\n", after_close)
    # 显式告知调用方「正文未覆盖」, 不再是静默 False / 静默丢
    assert saved.body_saved is False and saved.content_skipped is True
    assert saved.reason and "不覆盖正文" in saved.reason
    assert bool(saved) is True, "按规则保留不是失败"

    reports = _reports(db)
    assert len(reports) == 1
    assert "收盘后重跑" not in reports[0].content, "盘后重跑顶掉了当日盘前正文"
    assert reports[0].raw_data["run_trace_id"] == _data()["run_trace_id"]
    ev = _evidence(db)
    assert len(ev) == 2, "盘后这次真实采样也要留档(只是它绑定的 run 不是盘前)"
    assert sorted(e.preopen for e in ev) == [False, True]

    item = _ledger()["items"][0]
    assert item["preopen"] is True and item["premarket_eligible"] is True
    assert item["generated_at"] == f"{DAY}T08:20:00+08:00"
    # 盘后那条凭证不能被挂到盘前正文上
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["pit"]["evidence_matched"] == 1


def test_post_close_rerun_without_fresh_sampling_also_cannot_overwrite(db):
    """盘后重跑连因子都没采到(无凭证)时, 同样不能顶掉盘前记录。"""
    assert _persist(REPORT, _data()).body_saved is True
    saved = _persist(
        REPORT + "\n盘后无采样重跑\n",
        _data(gen=f"{DAY}T20:10:00+08:00", frozen=False, run="run-after2"),
    )
    assert saved.body_saved is False and saved.content_skipped is True
    reports = _reports(db)
    assert len(reports) == 1 and "盘后无采样重跑" not in reports[0].content
    assert _ledger()["items"][0]["preopen"] is True


def test_post_close_only_run_is_flagged_and_never_scored_as_premarket(db):
    assert _persist(REPORT, _data(gen=f"{DAY}T20:10:00+08:00")).body_saved is True

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
    assert _persist(REPORT, data).body_saved is True

    assert _evidence(db) == [], "采样时点不可核验时不得落凭证"
    item = _ledger()["items"][0]
    # 整份报告确实在盘前完成(gate=True) 但没有因子凭证 → 期次可计成绩, 因子必须留空
    assert item["preopen"] is True
    assert item["pit"]["demon_evidence"] is False
    assert item["generated_at"] is None
    row = item["rows"][0]
    assert row["signals"]["demon"] is None
    assert "timestamp" in (item["pit"]["demon_reason"] or "")


def test_naive_sampling_time_is_unknown_even_if_locally_preopen(db):
    """naive 08:20 在时区未知时可以是任何时刻 —— 不猜, 按 unknown 处理。"""
    from src.core import premarket_ambush as pa

    data = _data()
    data["demon_factors_frozen_at"] = f"{DAY}T08:20:00"  # 去掉时区
    data["_demon_evidence"] = {
        **_data()["_demon_evidence"],
        "generated_at": f"{DAY}T08:20:00",
    }
    saved = _persist(REPORT, data)
    # 凭证写不进去, 但报告正文必须留在库里(不再整体回滚), 且显式报告证据失败
    assert saved.body_saved is True
    assert saved.evidence_saved is False
    assert saved.ok is False
    assert "凭证" in (saved.reason or "")
    assert _evidence(db) == []
    assert len(_reports(db)) == 1

    out = pa.demon_evidence_from_record(
        {"demon_factors_frozen_at": f"{DAY}T08:20:00", "demon_factors": SNAP}, DAY
    )
    assert out["verified"] is False
    assert "时区" in (out["reason"] or "")


def test_conflicting_preopen_flag_and_time_is_unknown(db):
    """raw_data 自称 preopen=True, 但采样/完成时点在收盘后 → 凭证自相矛盾, unknown。"""
    raw = {
        "timestamp": f"{DAY}T20:10:00+08:00",
        "report_completed_at": f"{DAY}T20:10:00+08:00",
        "demon_factors": SNAP,
        "demon_factors_frozen_at": f"{DAY}T20:10:00+08:00",
        "demon_factors_preopen": True,
        # 落库时算出的门槛会随正文一起存; 这里刻意写反, 让重算与存档冲突
        "premarket_report_preopen": True,
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
    assert _persist(REPORT, data).body_saved is True
    ev = _evidence(db)
    assert ev[0].preopen is False
    assert _ledger()["items"][0]["preopen"] is False


def test_snapshot_fingerprint_mismatch_is_unknown_not_provenance(db):
    """指纹只做一致性校验: 存储被改动 → unknown; 对得上也不证明来源/时点。"""
    assert _persist(REPORT, _data()).body_saved is True
    from src.db.models import PremarketFactorEvidence

    with db.Session() as s:
        row = s.execute(select(PremarketFactorEvidence)).scalars().one()
        row.snapshot = {"600183": {"total": 1.0, "factor_date": "20261110"}}
        s.commit()

    item = _ledger()["items"][0]
    assert item["rows"][0]["signals"]["demon"] is None
    assert "一致性" in (item["pit"]["demon_reason"] or "")


def test_migration_creates_append_only_evidence_table(tmp_path):
    """生产迁移与模型要能对上: 新库跑一次 247/248 就有凭证表(含 run_trace_id 绑定列)。"""
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import inspect

    from src.web.migrations import (
        _m247_premarket_factor_evidence,
        _m248_premarket_evidence_run_binding,
    )

    eng = _ce(f"sqlite:///{tmp_path / 'mig.db'}", future=True)
    with eng.begin() as conn:
        _m247_premarket_factor_evidence(conn)
        _m248_premarket_evidence_run_binding(conn)
    cols = {c["name"] for c in inspect(eng).get_columns("premarket_factor_evidence")}
    assert {
        "analysis_date",
        "generated_at",
        "preopen",
        "source",
        "snapshot",
        "snapshot_sha256",
        "run_trace_id",
    } <= cols
    # 幂等: 再跑一次不报错
    with eng.begin() as conn:
        _m247_premarket_factor_evidence(conn)
        _m248_premarket_evidence_run_binding(conn)


def test_legacy_record_without_evidence_row_is_unknown_not_premarket(db):
    """迁移前的旧记录: 没有可核验的完成时刻 → 不能证明盘前, 也不计成绩。"""
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
