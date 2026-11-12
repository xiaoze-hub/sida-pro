"""盘前账本「按日混配」P0 回归 —— 证据必须绑定到具体某一版报告（2026-11-12）。

审计复现的真实故障（本文件逐条钉住）：

1. **系统盘前版 + owner 盘后版按日混配**: 账本按 `analysis_date` 取「最早一条可验证盘前」
   的凭证, 却展示当天最新一版正文 → owner 的 16:00 盘后重跑正文被挂上 08:20 的盘前凭证,
   `preopen=true`, 当日/T+1/T+3 涨跌被记成「盘前候选成绩」。
2. **同日不同重跑正文与因子错配**: 08:00 版冻结 88 分、08:40 版冻结 91 分, 正文被第二版
   覆盖后账本仍显示 88 分(第一版凭证)。
3. **raw_data 缺 run_trace_id 时按日期回退**: 只要同日有一条凭证能和报告日对上就会被借用。
4. **整份报告 09:30 后才完成却算盘前**: 只看因子采样时点(08:20), 不管报告 10:05 才写完。
5. **证据写失败连正文一起丢**: 凭证写失败 → 整个事务回滚, 报告消失只留一句「保存失败」。
6. **成功恢复报告被旧失败记录挡住**: 当日已有 degraded 盘前记录时, 后一次成功报告被拒。
7. **并发系统 NULL 归属重复**: 同一天出现两条 NULL 归属报告时, 收敛规则必须确定、可复核,
   且绝不把两条的正文/凭证混在一起。

测试库纪律: 每个用例一套 `tmp_path` 下的独立 SQLite; 起测前断言「连的是临时库」并清掉
所有 DB 环境变量; 不碰生产库、不做全表清理、不依赖 conftest 的共享测试库。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

REPORT = """
事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""

DAY = "2026-11-11"


def _snap(total: float) -> dict:
    return {
        "600183": {
            "total": total,
            "grade": "妖",
            "n_sealed": 12,
            "max_streak": 4,
            "participation": "活跃",
            "factor_date": "20261110",
        }
    }


class _User:
    id = "u-owner"
    role = "owner"


class _Member:
    # 与 owner 同 id 但角色是 member: 用来验证「member 只看本人, 不看 NULL 系统行」
    id = "u-owner"
    role = "member"


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
    from src.db.models import AnalysisHistory, PremarketFactorEvidence, User
    from src.db.session import Base

    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "bind.db"
    url = f"sqlite:///{path}"
    assert str(tmp_path) in url and not os.path.exists(path), "必须使用本次用例的临时库"
    eng = create_engine(url, future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)
    Base.metadata.create_all(
        eng,
        tables=[User.__table__, AnalysisHistory.__table__, PremarketFactorEvidence.__table__],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.analysis_history.SessionLocal", Session)
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: eng)

    from src.core import decision_log

    # 报告日 11-11 之前: 11-09=10.0, 11-10=10.5; 11-11 收盘 12.0 只能用于事后表现
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


def _seed_report(
    db,
    *,
    run_trace_id: str | None,
    snapshot: dict | None,
    sampled_at: str | None,
    collected_at: str,
    completed_at: str | None,
    user_id: str | None = None,
    status: str = "success",
    date: str = DAY,
    content: str = REPORT,
    stored_preopen: bool | None = None,
    created_at: datetime | None = None,
) -> int:
    """按「真实落库的形状」直接写一行报告（含/不含各类时点字段）。"""
    from src.core.premarket_ambush import factor_snapshot_fingerprint
    from src.db.models import AnalysisHistory

    raw: dict = {"timestamp": collected_at}
    if run_trace_id is not None:
        raw["run_trace_id"] = run_trace_id
    if completed_at is not None:
        raw["report_completed_at"] = completed_at
    if sampled_at is not None:
        raw["demon_factors"] = snapshot or {}
        raw["demon_factors_frozen_at"] = sampled_at
        raw["demon_factors_preopen"] = True
        raw["demon_factors_sha256"] = factor_snapshot_fingerprint(snapshot or {})
    if stored_preopen is not None:
        raw["premarket_report_preopen"] = stored_preopen
    with db.Session() as s:
        rec = AnalysisHistory(
            user_id=user_id,
            agent_name="premarket_outlook",
            stock_symbol="*",
            analysis_date=date,
            title="【盘前分析】测试",
            content=content,
            raw_data=raw,
            status=status,
        )
        if created_at is not None:
            rec.created_at = created_at
        s.add(rec)
        s.commit()
        return rec.id


def _seed_evidence(
    db,
    *,
    run_trace_id: str | None,
    sampled_at: str,
    snapshot: dict,
    user_id: str | None = None,
    date: str = DAY,
) -> None:
    from src.core.premarket_ambush import factor_snapshot_fingerprint
    from src.db.models import PremarketFactorEvidence

    with db.Session() as s:
        s.add(
            PremarketFactorEvidence(
                user_id=user_id,
                agent_name="premarket_outlook",
                stock_symbol="*",
                analysis_date=date,
                generated_at=sampled_at,
                preopen=True,
                source="test",
                snapshot=snapshot,
                snapshot_sha256=factor_snapshot_fingerprint(snapshot),
                run_trace_id=run_trace_id,
                note="",
            )
        )
        s.commit()


def _ledger(user: Any | None = None) -> dict:
    from src.web.api import decisions as dec

    return dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, user or _User()))


def _only_item(user: Any | None = None) -> dict:
    out = _ledger(user)
    assert out["available"] is True
    assert len(out["items"]) == 1, f"应只收敛成 1 期, 实际 {len(out['items'])}"
    return out["items"][0]


# ── 1. 系统盘前版 + owner 盘后版: 绝不混配 ────────────────────────────────

SYSTEM_RUN = "run-sys-0820"
OWNER_RUN = "run-owner-1600"


def _seed_mixed(db) -> None:
    _seed_report(
        db,
        run_trace_id=SYSTEM_RUN,
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:22:00+08:00",
        user_id=None,  # 系统调度
        stored_preopen=True,
    )
    _seed_evidence(db, run_trace_id=SYSTEM_RUN, sampled_at=f"{DAY}T08:20:00+08:00",
                   snapshot=_snap(88.0), user_id=None)
    _seed_report(
        db,
        run_trace_id=OWNER_RUN,
        snapshot=_snap(70.0),
        sampled_at=f"{DAY}T16:00:00+08:00",
        collected_at=f"{DAY}T16:00:00+08:00",
        completed_at=f"{DAY}T16:05:00+08:00",
        user_id=_User.id,  # owner 盘后手动重跑
        stored_preopen=False,
    )
    _seed_evidence(db, run_trace_id=OWNER_RUN, sampled_at=f"{DAY}T16:00:00+08:00",
                   snapshot=_snap(70.0), user_id=_User.id)


def test_system_premarket_report_wins_and_owner_post_close_is_not_mixed_in(db):
    _seed_mixed(db)

    item = _only_item()
    # 收敛到「能证明盘前的那一版」= 系统 08:20 版
    assert item["run_trace_id"] == SYSTEM_RUN
    assert item["record_scope"] == "system"
    assert item["preopen"] is True and item["premarket_eligible"] is True
    assert item["report_completed_at"] == f"{DAY}T08:22:00+08:00"
    # 因子必须来自同一版(88), 不是 owner 盘后那版的 70
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["pit"]["evidence_matched"] == 1
    assert item["pit"]["evidence_source_kind"] == "appended"
    # 两版都要能被复核(不静默丢弃), 但只有被选中的一版参与对账
    cands = {c["run_trace_id"]: c for c in item["pit"]["candidates"]}
    assert set(cands) == {SYSTEM_RUN, OWNER_RUN}
    assert cands[OWNER_RUN]["preopen"] is False
    assert cands[OWNER_RUN]["scope"] == "user"


def test_owner_ledger_does_not_borrow_owner_evidence_for_system_report(db):
    """反过来: 选中的是系统版时, 也绝不能借用 owner 那版(哪怕更晚/更"新")的凭证。"""
    _seed_mixed(db)
    item = _only_item()
    assert item["run_trace_id"] == SYSTEM_RUN
    # 88 只能来自 system scope 的那条凭证; 若串了 scope 会拿到 70
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["record_scope"] == "system"


def test_cross_scope_evidence_with_same_run_id_is_rejected(db):
    """同一 run_trace_id 但归属不同 → 不是同一份证据, 不许借用(严格等于)。"""
    _seed_report(
        db,
        run_trace_id=OWNER_RUN,
        snapshot=None,  # 这一版自己的 raw_data 没有冻结快照
        sampled_at=None,
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:25:00+08:00",
        user_id=_User.id,
        stored_preopen=True,
    )
    # 凭证存在但归属 NULL(系统), run_trace_id 却写成了 owner 那次 —— 不能算作这一版的
    _seed_evidence(db, run_trace_id=OWNER_RUN, sampled_at=f"{DAY}T08:20:00+08:00",
                   snapshot=_snap(88.0), user_id=None)

    item = _only_item()
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["pit"]["demon_evidence"] is False
    assert item["pit"]["evidence_matched"] == 0
    assert "不作数" in (item["pit"]["binding_reason"] or "")


def test_report_without_run_trace_id_never_falls_back_to_date(db):
    """raw_data 没有 run_trace_id(旧记录) → 不许按日期借同日的凭证。"""
    _seed_report(
        db,
        run_trace_id=None,
        snapshot=None,
        sampled_at=None,
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:22:00+08:00",
        user_id=None,
        stored_preopen=True,
    )
    # 同日同 scope 有一条"看起来完全匹配"的盘前凭证, 但没有 run_trace_id 可对
    _seed_evidence(db, run_trace_id="run-other", sampled_at=f"{DAY}T08:10:00+08:00",
                   snapshot=_snap(88.0), user_id=None)

    item = _only_item()
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["pit"]["evidence_rows"] == 1  # 凭证确实在同一天, 但不可对账
    assert item["pit"]["evidence_matched"] == 0
    assert "run_trace_id" in (item["pit"]["binding_reason"] or "")


def test_report_without_run_trace_id_still_uses_its_own_raw_data_snapshot(db):
    """旧记录只认**它自己** raw_data 里的冻结快照(同一行), 不是按日期借。"""
    _seed_report(
        db,
        run_trace_id=None,
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:22:00+08:00",
        user_id=None,
        stored_preopen=True,
    )
    item = _only_item()
    assert item["preopen"] is True
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["pit"]["evidence_source_kind"] == "raw_data"


# ── 2. 同日两次重跑: 正文是哪一版, 因子就必须是哪一版 ────────────────────

V1 = "run-v1-0800"
V2 = "run-v2-0840"


def test_rerun_body_and_factor_snapshot_always_share_one_run(db):
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    agent = PremarketOutlookAgent()

    def persist(total: float, at: str, run: str, content: str):
        snap = _snap(total)
        data = {
            "timestamp": at,
            "run_trace_id": run,
            "demon_factors": snap,
            "demon_factors_frozen_at": at,
            "demon_factors_preopen": True,
            "demon_factors_source": SOURCE_TOP_N,
            "demon_factors_dropped": [],
            "demon_factors_asof": "2026-11-10",
        }
        data["_demon_evidence"] = build_snapshot_evidence(
            snapshot=snap, frozen_at=at, report_day=DAY, asof="2026-11-10",
            source=SOURCE_TOP_N, run_trace_id=run,
        )
        return agent._persist_report(
            result=AnalysisResult(agent_name=agent.name, title="t", content=content),
            data=data,
            context=cast(Any, SimpleNamespace(user=None)),
            report_completed_at=at,
        )

    assert persist(88.0, f"{DAY}T08:00:00+08:00", V1, REPORT).body_saved is True
    assert persist(91.0, f"{DAY}T08:40:00+08:00", V2, REPORT + "\n第二版\n").body_saved is True

    item = _only_item()
    assert item["run_trace_id"] == V2
    assert item["rows"][0]["signals"]["demon"]["score"] == 91.0, "正文第二版却挂了第一版因子"
    assert item["generated_at"] == f"{DAY}T08:40:00+08:00"
    assert item["pit"]["evidence_rows"] == 2 and item["pit"]["evidence_matched"] == 1


# ── 3. 整份报告 09:30 后才完成 → 不算盘前(哪怕因子 08:20 采样) ───────────

LATE = "run-late-1005"


def test_report_completed_after_open_is_not_premarket_even_if_sampling_was_preopen(db):
    _seed_report(
        db,
        run_trace_id=LATE,
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",       # 因子采样确实在盘前
        collected_at=f"{DAY}T08:22:00+08:00",     # 采集也确实在盘前结束
        completed_at=f"{DAY}T10:05:00+08:00",     # 但整份报告 10:05 才写完
        user_id=None,
        stored_preopen=None,
    )
    _seed_evidence(db, run_trace_id=LATE, sampled_at=f"{DAY}T08:20:00+08:00",
                   snapshot=_snap(88.0), user_id=None)

    item = _only_item()
    assert item["preopen"] is False
    assert item["premarket_eligible"] is False
    assert item["report_completed_at"] == f"{DAY}T10:05:00+08:00"
    assert item["rows"][0]["outcome"]["t0"] is None
    assert "09:30" in (item["pit"]["gate_reason"] or "")
    assert item["rows"][0]["signals"]["demon"] is None, "非盘前报告不许展示盘前因子"


def test_missing_report_completion_time_is_unknown_not_premarket(db):
    _seed_report(
        db,
        run_trace_id="run-nocomp",
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=None,  # 旧记录没有 report_completed_at
        user_id=None,
        stored_preopen=None,
    )
    item = _only_item()
    assert item["preopen"] is None, "缺完成时刻只能 unknown, 不许按日期/采样时点回退"
    assert item["premarket_eligible"] is False
    assert item["rows"][0]["outcome"]["t0"] is None
    assert "生成完成时刻" in (item["pit"]["gate_reason"] or "")


def test_stored_and_recomputed_gate_conflict_is_unknown(db):
    _seed_report(
        db,
        run_trace_id="run-conflict",
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T20:00:00+08:00",
        completed_at=f"{DAY}T20:05:00+08:00",
        user_id=None,
        stored_preopen=True,  # 存档说盘前, 重算是 False → 自相矛盾
    )
    item = _only_item()
    assert item["preopen"] is None
    assert "矛盾" in (item["pit"]["gate_reason"] or "")
    assert item["rows"][0]["outcome"]["t0"] is None


def test_preopen_gate_boundaries():
    from src.core.premarket_factor_evidence import report_preopen_state

    def state(done, coll=DAY + "T08:00:00+08:00"):
        return report_preopen_state(
            report_completed_at=done, collection_finished_at=coll, report_day=DAY
        )

    assert state(f"{DAY}T09:29:59+08:00")["preopen"] is True
    assert state(f"{DAY}T09:30:00+08:00")["preopen"] is False  # 开盘瞬间不算盘前
    assert state(f"{DAY}T00:59:59+08:00")["preopen"] is True
    assert state(f"{DAY}T01:00:00+08:00")["preopen"] is True  # 9:30 之前都算(含凌晨)
    assert state(f"{DAY}T20:00:00+08:00")["preopen"] is False
    assert state(f"{DAY}T08:00:00")["preopen"] is None  # naive → unknown
    assert state(None)["preopen"] is None
    assert state(f"{DAY}T08:00:00+08:00", coll="2026-11-10T23:00:00+08:00")["preopen"] is False
    # 采集结束在 09:31 → 不算盘前(即使报告完成时刻在 09:00)
    assert (
        state(f"{DAY}T09:00:00+08:00", coll=f"{DAY}T09:31:00+08:00")["preopen"] is False
    )


# ── 4. 证据写失败: 报告不丢 + 显式告知 + 回落到同一行的 raw_data ─────────

FAIL_RUN = "run-evidence-fail"


def test_evidence_write_failure_keeps_report_and_falls_back_to_raw_data(db, monkeypatch):
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent
    from src.core import analysis_history as ah
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    def boom(db_, evidence, *, user_id=None):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(
        "src.core.premarket_factor_evidence.append_evidence", boom, raising=True
    )
    assert ah is not None  # 确保模块已导入

    snap = _snap(88.0)
    data = {
        "timestamp": f"{DAY}T08:20:00+08:00",
        "run_trace_id": FAIL_RUN,
        "demon_factors": snap,
        "demon_factors_frozen_at": f"{DAY}T08:20:00+08:00",
        "demon_factors_preopen": True,
        "demon_factors_source": SOURCE_TOP_N,
        "demon_factors_dropped": [],
        "demon_factors_asof": "2026-11-10",
    }
    data["_demon_evidence"] = build_snapshot_evidence(
        snapshot=snap, frozen_at=data["demon_factors_frozen_at"], report_day=DAY,
        asof="2026-11-10", source=SOURCE_TOP_N, run_trace_id=FAIL_RUN,
    )
    agent = PremarketOutlookAgent()
    saved = agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="t", content=REPORT),
        data=data,
        context=cast(Any, SimpleNamespace(user=None)),
        report_completed_at=f"{DAY}T08:22:00+08:00",
    )
    # 报告正文必须留下, 同时明确报告"证据没写进去"(不冒充完整)
    assert saved.body_saved is True
    assert saved.evidence_saved is False
    assert saved.ok is False
    assert "凭证" in (saved.reason or "")

    from src.db.models import AnalysisHistory

    with db.Session() as s:
        rows = s.execute(select(AnalysisHistory)).scalars().all()
    assert len(rows) == 1 and rows[0].content == REPORT

    # 账本回落到同一行的 raw_data 凭证(仍是这一版自己的), 不是借用别人的
    item = _only_item()
    assert item["preopen"] is True
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item["pit"]["evidence_source_kind"] == "raw_data"


# ── 5. 成功恢复报告不被旧 degraded 记录挡住 ──────────────────────────────

RECOVER_RUN = "run-recover-1600"


def test_successful_rerun_overwrites_degraded_row_instead_of_being_refused(db):
    """旧记录是 degraded(盘前采样但 AI 失败) → 后一次成功报告必须能覆盖它。"""
    degraded_id = _seed_report(
        db,
        run_trace_id="run-degraded",
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:22:00+08:00",
        user_id=None,
        status="degraded",
        stored_preopen=True,
        content="【盘前事实快报】AI 解读不可用",
    )

    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    data = {
        "timestamp": f"{DAY}T16:00:00+08:00",
        "run_trace_id": RECOVER_RUN,
        "demon_factors": _snap(70.0),
        "demon_factors_frozen_at": f"{DAY}T16:00:00+08:00",
        "demon_factors_preopen": False,
    }
    saved = agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="t", content=REPORT),
        data=data,
        context=cast(Any, SimpleNamespace(user=None)),
        report_completed_at=f"{DAY}T16:05:00+08:00",
    )
    assert saved.body_saved is True, "成功恢复报告被旧 degraded 记录挡住了"
    assert saved.content_skipped is False

    from src.db.models import AnalysisHistory

    with db.Session() as s:
        row = s.execute(select(AnalysisHistory)).scalars().one()
    assert row.id == degraded_id
    assert row.status == "success"
    assert row.raw_data["run_trace_id"] == RECOVER_RUN

    item = _only_item()
    # 正文是 16:05 完成的成功版 → 明确非盘前, 不计盘前成绩
    assert item["run_trace_id"] == RECOVER_RUN
    assert item["preopen"] is False
    assert item["rows"][0]["outcome"]["t0"] is None


# ── 6. 并发系统 NULL 归属重复: 收敛确定、可复核、不混配 ──────────────────

DUP_PRE = "run-dup-preopen"
DUP_POST = "run-dup-post"


def _seed_duplicate_system_rows(db, *, premarket_first: bool) -> None:
    """同一天两条 NULL 归属报告(并发调度可能撞出这种行; NULL 在唯一约束下不去重)。"""
    pre = dict(
        run_trace_id=DUP_PRE,
        snapshot=_snap(88.0),
        sampled_at=f"{DAY}T08:20:00+08:00",
        collected_at=f"{DAY}T08:20:00+08:00",
        completed_at=f"{DAY}T08:25:00+08:00",
        user_id=None,
        stored_preopen=True,
    )
    post = dict(
        run_trace_id=DUP_POST,
        snapshot=_snap(70.0),
        sampled_at=f"{DAY}T20:00:00+08:00",
        collected_at=f"{DAY}T20:00:00+08:00",
        completed_at=f"{DAY}T20:05:00+08:00",
        user_id=None,
        stored_preopen=False,
    )
    order = (pre, post) if premarket_first else (post, pre)
    base = datetime(2026, 11, 11, 8, 0, 0)
    for i, kw in enumerate(order):
        _seed_report(db, created_at=base + timedelta(minutes=i), **kw)
        _seed_evidence(
            db,
            run_trace_id=kw["run_trace_id"],
            sampled_at=kw["sampled_at"],
            snapshot=kw["snapshot"],
            user_id=None,
        )


@pytest.mark.parametrize("premarket_first", [True, False])
def test_duplicate_system_rows_converge_to_the_premarket_version(db, premarket_first):
    _seed_duplicate_system_rows(db, premarket_first=premarket_first)
    item = _only_item()
    # 与插入顺序/created_at 无关: 能证明盘前的那一版胜出
    assert item["run_trace_id"] == DUP_PRE
    assert item["preopen"] is True
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert len(item["pit"]["candidates"]) == 2


def test_duplicate_system_rows_without_premarket_version_converge_deterministically(db):
    _seed_report(
        db,
        run_trace_id="run-a",
        snapshot=_snap(70.0),
        sampled_at=f"{DAY}T20:00:00+08:00",
        collected_at=f"{DAY}T20:00:00+08:00",
        completed_at=f"{DAY}T20:05:00+08:00",
        user_id=None,
        stored_preopen=False,
        created_at=datetime(2026, 11, 11, 21, 0, 0),
    )
    _seed_report(
        db,
        run_trace_id="run-b",
        snapshot=_snap(60.0),
        sampled_at=f"{DAY}T21:30:00+08:00",
        collected_at=f"{DAY}T21:30:00+08:00",
        completed_at=f"{DAY}T21:35:00+08:00",
        user_id=None,
        stored_preopen=False,
        created_at=datetime(2026, 11, 11, 22, 0, 0),
    )
    item = _only_item()
    # 没有盘前版 → 取最新那条(而不是随机/按字典序), 且必须整条自洽
    assert item["run_trace_id"] == "run-b"
    assert item["preopen"] is False
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["rows"][0]["outcome"]["t0"] is None


# ── 7. 成员视角: 只能看到自己的报告, 也不会借用系统凭证 ──────────────────


def test_member_scope_never_sees_system_report_or_its_evidence(db):
    _seed_mixed(db)
    item = _only_item(_Member())
    assert item["record_scope"] == "user"
    assert item["run_trace_id"] == OWNER_RUN
    assert item["preopen"] is False
    # 系统那版的 88 分不能漏给成员
    assert item["rows"][0]["signals"]["demon"] is None
    assert item["rows"][0]["outcome"]["t0"] is None
    assert len(item["pit"]["candidates"]) == 1
