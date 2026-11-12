"""盘前重跑完整性 —— 成功正文不被降级覆盖 / 盘后重跑不写统计表 / 历史数值不消失（2026-11-12 复审）。

审计复现的真实故障（本文件逐条钉住）：

1. **成功盘前正文被降级版顶掉**: 08:20 已生成成功盘前报告, 08:40 LLM 限流又跑出「事实快报」
   (status=degraded), 旧保护只看「新报告是不是盘前」—— 降级快报也是盘前, 于是把成功正文覆盖掉。
2. **盘后/unknown 重跑先写统计表**: `analyze()` 在正文落库**之前**就无条件写
   `stock_suggestions` / `agent_prediction_outcomes`。正文被按规则保留(没覆盖), 统计表却
   多出一批「盘前建议」; 同日重跑一次还会再插一套后验行, 命中率分母被重复灌水。
3. **生成完成时刻被保存时刻冒名**: `save_analysis(report_completed_at=None)` 曾用「落库那一刻」
   当报告完成时刻。落库慢/补跑都会把「生成时点」写错, 盘前门槛随之失真。现在两个时刻分开记,
   缺生成时刻 → unknown(不拿保存时刻兜底)。
4. **历史数值凭空消失**: 旧报告自己 `raw_data` 里有冻结因子快照, 但缺带时区生成时刻 →
   盘前不可证明。此时数值不能当成绩, 也不该整段消失: 走「历史未验证 · 不纳入成绩」展示通道。
5. **告警必须如实**: 正文没落库 / 正文按规则保留 / 凭证没写进, 三种状态分别告警, 不互相冒充。

测试库纪律: 每个用例一套 `tmp_path` 下的独立 SQLite; 起测前清掉所有 DB / DATA_DIR / PYTHONPATH
环境变量并断言「连的就是这次用例的临时库」; 不碰生产库、不做全表清理、不依赖 conftest 的共享库。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

REPORT = """
事件驱动前瞻（双轨制 · 潜伏表）
| 事件 | 受益板块 | 代码 | 名称 | 受益逻辑 | 强度 | 埋伏方式 | 分批计划 | 止损 | 目标 | 证伪条件 |
|---|---|---|---|---|---|---|---|---|---|---|
| PCB涨价 | 覆铜板 | 600183 | 生益科技 | 龙头 | A | 回调埋伏 | 1/3 | 35.60 | +10% | 提价落空 |
"""

DAY = "2026-11-11"
PRE_AT = f"{DAY}T08:20:00+08:00"
PRE_DONE = f"{DAY}T08:22:00+08:00"
POST_AT = f"{DAY}T16:00:00+08:00"
POST_DONE = f"{DAY}T16:05:00+08:00"

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
SNAP_V2 = {**SNAP, "600183": {**SNAP["600183"], "total": 91.0}}


class _Owner:
    id = "u-owner"
    role = "owner"


_DB_ENV = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_READONLY",
    "SIDA_DB",
    "SIDA_DB_URL",
    "SIDA_DATA_DIR",
    "SIDA_CACHE_DIR",
    "DATA_DIR",
    "POSTGRES_DSN",
    "PYTHONPATH",
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """每个用例一套全新 SQLite(文件在 tmp_path), 并接到真实落库路径。"""
    from src.db.models import (
        AgentContextRun,
        AgentPredictionOutcome,
        AnalysisHistory,
        PremarketFactorEvidence,
        StockSuggestion,
        User,
    )
    from src.db.session import Base

    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    db_path = tmp_path / "rerun.db"
    url = f"sqlite:///{db_path}"
    # 起测断言: 库文件必须落在本用例的临时目录, 且此刻还不存在(绝不连既有库)
    assert str(tmp_path) in url and not os.path.exists(db_path)
    eng = create_engine(url, future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)

    Base.metadata.create_all(
        eng,
        tables=[
            User.__table__,
            AnalysisHistory.__table__,
            PremarketFactorEvidence.__table__,
            StockSuggestion.__table__,
            AgentPredictionOutcome.__table__,
            AgentContextRun.__table__,
        ],
    )
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, future=True)
    # analysis_history / context_store / suggestion_pool 都是模块级 `from ... import SessionLocal`
    monkeypatch.setattr("src.db.session.SessionLocal", Session)
    monkeypatch.setattr("src.core.analysis_history.SessionLocal", Session)
    monkeypatch.setattr("src.core.context_store.SessionLocal", Session)
    monkeypatch.setattr("src.core.suggestion_pool.SessionLocal", Session)
    monkeypatch.setattr("src.db.session.get_read_engine", lambda: eng)

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
    return SimpleNamespace(engine=eng, Session=Session, path=db_path)


def _data(
    *,
    at: str,
    run: str,
    snapshot: dict | None = SNAP,
    frozen_at: str | None = None,
    completed_at: str | None = None,
    with_evidence: bool = True,
) -> dict:
    """模拟 collect() 的输出(冻结快照 + 带时区采样时点 + 追加式凭证)。"""
    from src.core.premarket_factor_evidence import SOURCE_TOP_N, build_snapshot_evidence

    data: dict = {
        "timestamp": at,
        "run_trace_id": run,
        "demon_factors": snapshot,
        "demon_factors_asof": "2026-11-10",
        "demon_factors_dropped": [],
        "demon_factors_preopen": True,
        "demon_factors_frozen_at": frozen_at if frozen_at is not None else at,
        "demon_factors_source": SOURCE_TOP_N,
        "report_completed_at": completed_at,
    }
    ev = build_snapshot_evidence(
        snapshot=snapshot,
        frozen_at=data["demon_factors_frozen_at"],
        report_day=DAY,
        dropped=[],
        asof="2026-11-10",
        source=SOURCE_TOP_N,
        run_trace_id=run,
    )
    if ev and with_evidence:
        data["demon_factors_preopen"] = ev["preopen"]
        data["demon_factors_sha256"] = ev["snapshot_sha256"]
        data["_demon_evidence"] = ev
    return data


def _persist(
    data: dict,
    content: str = REPORT,
    *,
    completed: str | None = None,
    status: str | None = None,
    error: str | None = None,
    user: Any | None = None,
):
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    return agent._persist_report(
        result=AnalysisResult(agent_name=agent.name, title="【盘前分析】测试", content=content),
        data=data,
        context=cast(Any, SimpleNamespace(user=user)),
        status=status,
        error=error,
        report_completed_at=completed if completed is not None else data.get("report_completed_at"),
    )


def _reports(db) -> list:
    from src.db.models import AnalysisHistory

    with db.Session() as s:
        return s.execute(select(AnalysisHistory).order_by(AnalysisHistory.id)).scalars().all()


def _outcomes(db) -> list:
    from src.db.models import AgentPredictionOutcome

    with db.Session() as s:
        return (
            s.execute(select(AgentPredictionOutcome).order_by(AgentPredictionOutcome.id))
            .scalars()
            .all()
        )


def _suggestions(db) -> list:
    from src.db.models import StockSuggestion

    with db.Session() as s:
        return (
            s.execute(select(StockSuggestion).order_by(StockSuggestion.id)).scalars().all()
        )


def _ledger(user: Any | None = None) -> dict:
    from src.web.api import decisions as dec

    return dec.decisions_premarket(days=5, max_rows=30, user=cast(Any, user or _Owner()))


def _only_item(user: Any | None = None) -> dict:
    out = _ledger(user)
    assert out["available"] is True, out
    assert len(out["items"]) == 1, f"应只有 1 期, 实际 {len(out['items'])}"
    return out["items"][0]


# ── 1. 成功盘前正文: 降级版不许覆盖 ──────────────────────────────────────


def test_degraded_fallback_cannot_overwrite_successful_premarket_body(db):
    assert _persist(_data(at=PRE_AT, run="run-ok", completed_at=PRE_DONE)).body_saved is True

    saved = _persist(
        _data(at=f"{DAY}T08:40:00+08:00", run="run-degraded", completed_at=f"{DAY}T08:41:00+08:00"),
        content="【盘前事实快报】AI 解读不可用",
        completed=f"{DAY}T08:41:00+08:00",
        status="degraded",
        error="AI 限流",
    )
    # 降级正文也在 09:30 前生成, 但**不是**成功报告 → 不许顶掉成功盘前正文
    assert saved.body_saved is False
    assert saved.content_skipped is True
    assert saved.body_status == "degraded"
    assert "degraded" in (saved.reason or "") and "不覆盖正文" in (saved.reason or "")
    assert bool(saved) is True, "按规则保留不是失败"

    rows = _reports(db)
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert "事实快报" not in (rows[0].content or "")
    assert rows[0].raw_data["run_trace_id"] == "run-ok"


def test_successful_premarket_body_can_still_replace_an_old_degraded_row(db):
    from src.agents.base import AnalysisResult
    from src.agents.premarket_outlook import PremarketOutlookAgent

    agent = PremarketOutlookAgent()
    # 旧行是 degraded(盘前采样但 AI 失败) —— 不能挡住后来的成功报告
    old = agent._persist_report(
        result=AnalysisResult(
            agent_name=agent.name, title="t", content="【盘前事实快报】AI 解读不可用"
        ),
        data=_data(at=PRE_AT, run="run-degraded-old", completed_at=PRE_DONE),
        context=cast(Any, SimpleNamespace(user=None)),
        status="degraded",
        error="AI 解读不可用",
        report_completed_at=PRE_DONE,
    )
    assert old.body_saved is True

    saved = _persist(
        _data(at=f"{DAY}T09:00:00+08:00", run="run-ok-2", completed_at=f"{DAY}T09:01:00+08:00"),
        completed=f"{DAY}T09:01:00+08:00",
    )
    assert saved.body_saved is True, "成功恢复报告被旧 degraded 记录挡住了"
    assert saved.content_skipped is False

    rows = _reports(db)
    assert len(rows) == 1 and rows[0].status == "success"
    assert rows[0].raw_data["run_trace_id"] == "run-ok-2"


# ── 2. 盘后/unknown 重跑: 不写 stock_suggestions / agent_prediction_outcomes ──


def _analyze_ctx(user: Any | None = None):
    from src.agents.base import AgentContext
    from src.config import AppConfig, Settings, StockConfig
    from src.models.market import MarketCode

    return AgentContext(
        ai_client=cast(Any, MagicMock()),
        notifier=cast(Any, MagicMock()),
        config=AppConfig(
            settings=Settings(),
            watchlist=[StockConfig(symbol="600183", name="生益科技", market=MarketCode.CN)],
        ),
        # P0(2026-11-12): 后验记录必须有归属用户; 真实路径由调度注入 context.user。
        # 不注入(= 系统调度/无会话)时存储层会拒写, 见 test_unscoped_run_does_not_write_outcomes。
        user=user,
    )


def _stub_llm(monkeypatch, agent):
    payload = {
        "suggestions": [
            {
                "symbol": "600183",
                "action": "buy",
                "action_label": "准备建仓",
                "reason": "覆铜板提价",
                "signal": "PCB涨价",
            }
        ]
    }
    content = (
        "事件驱动前瞻（双轨制 · 潜伏表）\n"
        "<!--SIDA_JSON-->\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n<!--/SIDA_JSON-->"
    )

    monkeypatch.setattr(agent, "_build_analysis_prompt", lambda *_a, **_k: ("sys", "user"))

    async def _fake_call(*_a, **_k):
        return content

    monkeypatch.setattr(agent, "_call_llm_analysis", _fake_call)


def _freeze_now(monkeypatch, iso: str):
    """把 agent 侧的「生成完成时刻」固定在指定时区时刻(生产是真的现在)。"""
    fixed = datetime.fromisoformat(iso)
    monkeypatch.setattr("src.agents.premarket_outlook.beijing_now", lambda: fixed)


def _quiet_notify(monkeypatch):
    monkeypatch.setattr(
        "src.core.notify_center.push_notification", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        "src.core.notify_dedupe.check_and_mark_notify", lambda *a, **k: True, raising=False
    )


@pytest.mark.asyncio
async def test_post_close_rerun_writes_no_suggestions_or_outcomes(db, monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent

    _quiet_notify(monkeypatch)
    agent = PremarketOutlookAgent()
    _stub_llm(monkeypatch, agent)
    _freeze_now(monkeypatch, POST_DONE)

    data = _data(at=POST_AT, run="run-post", completed_at=POST_DONE)
    await agent.analyze(_analyze_ctx(user=SimpleNamespace(id='u-owner')), data)

    rows = _reports(db)
    assert len(rows) == 1 and rows[0].status == "success", "正文本身是真成功, 应落库"
    assert rows[0].raw_data["premarket_report_preopen"] is False
    # 盘后生成 → 不是盘前候选: 建议池与预测后验都必须为空(不许伪装盘前)
    assert _suggestions(db) == []
    assert _outcomes(db) == []


@pytest.mark.asyncio
async def test_unknown_generation_time_writes_no_suggestions_or_outcomes(db, monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent

    _quiet_notify(monkeypatch)
    agent = PremarketOutlookAgent()
    _stub_llm(monkeypatch, agent)
    # 生成完成时刻不可证明(naive) → 门槛 unknown → 不许写统计表
    _freeze_now(monkeypatch, f"{DAY}T08:22:00")

    data = _data(at=PRE_AT, run="run-unknown", completed_at=None)
    await agent.analyze(_analyze_ctx(user=SimpleNamespace(id='u-owner')), data)

    assert _reports(db)[0].raw_data["premarket_report_preopen"] is None
    assert _suggestions(db) == []
    assert _outcomes(db) == []


@pytest.mark.asyncio
async def test_premarket_run_writes_once_and_rerun_does_not_duplicate(db, monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent

    _quiet_notify(monkeypatch)
    agent = PremarketOutlookAgent()
    _stub_llm(monkeypatch, agent)
    ctx = _analyze_ctx(user=SimpleNamespace(id='u-owner'))

    _freeze_now(monkeypatch, PRE_DONE)
    await agent.analyze(ctx, _data(at=PRE_AT, run="run-pre-1", completed_at=PRE_DONE))

    sug = _suggestions(db)
    out = _outcomes(db)
    assert len(sug) == 1 and sug[0].action == "buy"
    assert sorted((o.horizon_days for o in out)) == [1, 5]
    assert sug[0].meta["analysis_date"] == DAY and sug[0].meta["run_trace_id"] == "run-pre-1"

    # 同日盘前二次重跑(更晚但仍盘前): 正文是最新一版, 统计表**不新增行**
    _freeze_now(monkeypatch, f"{DAY}T08:41:00+08:00")
    await agent.analyze(ctx, _data(at=f"{DAY}T08:40:00+08:00", run="run-pre-2",
                                  snapshot=SNAP_V2, completed_at=f"{DAY}T08:41:00+08:00"))

    sug2 = _suggestions(db)
    out2 = _outcomes(db)
    assert len(sug2) == 1, "同日重跑在建议池里多插了一行(看起来像两次建议)"
    assert int((sug2[0].meta or {}).get("revision") or 0) >= 1
    scored = [o for o in out2 if o.outcome_status != "superseded"]
    assert len(scored) == 2, f"重跑把计分行重复插入了: {len(scored)} 条计分行"
    assert sorted(o.horizon_days for o in scored) == [1, 5]
    assert all(o.outcome_status == "pending" for o in scored)
    assert all(o.run_trace_id == "run-pre-1" for o in scored), "计分行仍应是第一版"
    # 2026-11-12 二次修正: 新版不覆盖、也不是丢掉 —— 以 superseded 审计版本留档, 不参与计分
    superseded = [o for o in out2 if o.outcome_status == "superseded"]
    assert len(superseded) == 2 and all(o.run_trace_id == "run-pre-2" for o in superseded)
    assert _reports(db)[0].raw_data["run_trace_id"] == "run-pre-2"


@pytest.mark.asyncio
async def test_post_close_rerun_after_premarket_keeps_tables_untouched(db, monkeypatch):
    from src.agents.premarket_outlook import PremarketOutlookAgent

    _quiet_notify(monkeypatch)
    agent = PremarketOutlookAgent()
    _stub_llm(monkeypatch, agent)
    ctx = _analyze_ctx(user=SimpleNamespace(id='u-owner'))

    _freeze_now(monkeypatch, PRE_DONE)
    await agent.analyze(ctx, _data(at=PRE_AT, run="run-pre", completed_at=PRE_DONE))
    sug_before = len(_suggestions(db))
    out_before = len(_outcomes(db))
    assert sug_before == 1 and out_before == 2

    _freeze_now(monkeypatch, POST_DONE)
    await agent.analyze(ctx, _data(at=POST_AT, run="run-post", snapshot=SNAP_V2,
                                  completed_at=POST_DONE))

    # 盘后重跑: 正文按规则保留, 统计表也不许多出「盘前建议」
    assert len(_suggestions(db)) == sug_before
    assert len(_outcomes(db)) == out_before
    rows = _reports(db)
    assert len(rows) == 1 and rows[0].raw_data["run_trace_id"] == "run-pre"


# ── 3. 生成完成时刻 vs 保存时刻: 不许互相冒名 ────────────────────────────


def test_missing_completion_time_is_unknown_and_save_time_is_recorded_separately(db):
    saved = _persist(_data(at=PRE_AT, run="run-nocomp", completed_at=None))
    assert saved.body_saved is True
    assert saved.report_completed_at is None, "缺生成完成时刻不许拿保存时刻补"
    assert saved.report_saved_at and saved.report_saved_at.endswith("+08:00")
    assert saved.preopen is None, "缺生成完成时刻只能是 unknown"

    raw = _reports(db)[0].raw_data
    assert raw["report_completed_at"] is None
    assert raw["report_saved_at"] and raw["report_saved_at"] != raw.get("report_completed_at")
    assert raw["premarket_report_preopen"] is None
    assert raw["premarket_gate_reason"]

    item = _only_item()
    assert item["preopen"] is None
    assert item["premarket_eligible"] is False
    assert item["report_completed_at"] is None
    assert item["pit"]["demon_evidence"] is False
    assert item["pit"]["demon_unverified"] == 1


# ── 4. 历史未验证: 数值不消失, 但绝不冒充盘前/不纳入成绩 ─────────────────


def test_history_values_stay_visible_but_marked_unverified(db):
    # 旧报告: 有冻结快照, 没有可核验的生成完成时刻(迁移前的真实形状)
    _persist(_data(at=PRE_AT, run="run-legacy", completed_at=None))

    item = _only_item()
    assert item["preopen"] is None and item["premarket_eligible"] is False
    row = item["rows"][0]
    # 可验证通道为空 —— 不许把它当盘前信号
    assert row["signals"]["demon"] is None
    # 历史数值不消失, 走「历史未验证」通道, 并写明不纳入成绩
    hist = row["signals"]["demon_historical"]
    assert hist is not None and hist["score"] == 88.0 and hist["factor_date"] == "20261110"
    assert "不纳入" in (row["signals"]["demon_note"] or "")
    assert item["pit"]["demon_unverified"] == 1
    assert "历史未验证" in (item["pit"]["demon_unverified_reason"] or "")
    # 事后成绩一律留空, summary 不计分
    assert row["outcome"]["t0"] is None and row["outcome"]["t1"] is None
    assert item["summary"] == {
        "rows": 1,
        "t0_scored": 0,
        "t0_up": 0,
        "t1_scored": 0,
        "t1_up": 0,
    }


def test_post_close_report_history_values_are_labelled_not_premarket(db):
    _persist(_data(at=POST_AT, run="run-post", completed_at=POST_DONE))

    item = _only_item()
    assert item["preopen"] is False
    row = item["rows"][0]
    assert row["signals"]["demon"] is None
    assert row["signals"]["demon_historical"]["score"] == 88.0
    assert "非盘前" in (row["signals"]["demon_note"] or "")
    assert row["outcome"]["t0"] is None
    assert item["pit"]["demon_unverified"] == 1


# ── 5. 告警如实: 三种状态分别告警, 不互相冒充 ───────────────────────────


def test_persist_alerts_distinguish_body_failure_skip_and_evidence_failure(db, monkeypatch, caplog):
    from src.agents.premarket_outlook import PremarketOutlookAgent
    from src.core.analysis_history import SaveResult

    agent = PremarketOutlookAgent()

    with caplog.at_level("WARNING", logger="src.agents.premarket_outlook"):
        agent._log_persist_result(
            SaveResult(body_saved=False, content_skipped=True, reason="当日已有盘前成功版"),
            trace_id="t-skip",
            label="盘前报告",
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("未覆盖" in m for m in msgs), msgs
    assert not any(r.levelname == "ERROR" and "落库失败" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level("ERROR", logger="src.agents.premarket_outlook"):
        agent._log_persist_result(
            SaveResult(body_saved=False, content_skipped=False, reason="磁盘满"),
            trace_id="t-fail",
            label="盘前报告",
        )
    assert any("正文落库失败" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level("ERROR", logger="src.agents.premarket_outlook"):
        agent._log_persist_result(
            SaveResult(body_saved=True, evidence_saved=False, reason="凭证写入失败"),
            trace_id="t-ev",
            label="盘前报告",
        )
    assert any("凭证未写入" in r.getMessage() for r in caplog.records)


def test_evidence_write_failure_keeps_body_and_says_so(db, monkeypatch):
    from src.core.premarket_factor_evidence import append_evidence

    def boom(*_a, **_k):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(
        "src.core.premarket_factor_evidence.append_evidence", boom, raising=True
    )
    assert append_evidence is not None

    saved = _persist(_data(at=PRE_AT, run="run-evfail", completed_at=PRE_DONE))
    assert saved.body_saved is True
    assert saved.evidence_saved is False
    assert saved.ok is False
    assert "凭证" in (saved.reason or "")
    assert len(_reports(db)) == 1
    # 账本回落到同一行 raw_data 的快照(仍是这一版自己的), 但不冒充「凭证已写入」
    item = _only_item()
    assert item["preopen"] is True
    assert item["pit"]["evidence_source_kind"] == "raw_data"
    assert item["rows"][0]["signals"]["demon"]["score"] == 88.0


# ── 6. 双用户隔离 + 同一 run 重复保存的幂等 ─────────────────────────────


def test_two_users_same_day_are_isolated_in_both_directions(db):
    class _U:
        def __init__(self, uid: str, role: str = "owner"):
            self.id = uid
            self.role = role

    ua, ub = _U("u-a"), _U("u-b")
    assert _persist(
        _data(at=PRE_AT, run="run-a", snapshot=SNAP, completed_at=PRE_DONE), user=ua
    ).body_saved is True
    assert _persist(
        _data(at=PRE_AT, run="run-b", snapshot=SNAP_V2, completed_at=PRE_DONE), user=ub
    ).body_saved is True

    ledger_a = _ledger(ua)
    ledger_b = _ledger(ub)
    assert len(ledger_a["items"]) == 1 and len(ledger_b["items"]) == 1
    item_a, item_b = ledger_a["items"][0], ledger_b["items"][0]
    assert item_a["run_trace_id"] == "run-a" and item_b["run_trace_id"] == "run-b"
    # 各看各的因子: 不能串到对方的凭证
    assert item_a["rows"][0]["signals"]["demon"]["score"] == 88.0
    assert item_b["rows"][0]["signals"]["demon"]["score"] == 91.0
    assert item_a["record_scope"] == "user" and item_b["record_scope"] == "user"
    # member 角色也只能看到本人那一行
    member_a = _ledger(_U("u-a", "member"))
    assert len(member_a["items"]) == 1
    assert member_a["items"][0]["run_trace_id"] == "run-a"


def test_saving_the_same_run_twice_appends_evidence_once(db):
    data = _data(at=PRE_AT, run="run-twice", completed_at=PRE_DONE)
    assert _persist(data).body_saved is True
    # 同一次运行的重复保存: 正文可以再写一次(内容相同), 但凭证必须只有一行
    assert _persist(data).body_saved is True

    from src.db.models import PremarketFactorEvidence

    with db.Session() as s:
        rows = s.execute(select(PremarketFactorEvidence)).scalars().all()
    assert len(rows) == 1, "同一次运行重复保存不许把凭证写成两行(假证据)"
    assert len(_reports(db)) == 1


# ── 7. 迁移 247/248 不动, 新列由 249 负责; 历史版本号如实核对 ─────────────
#
# 事实核对(2026-11-12 复审, 用 git 逐版本查过):
#   基线 5f79714a 的 MIGRATIONS 最高是 **246** —— 基线**没有** 247;
#   247 首次出现在 8c972f63, 248 出现在 f97514d7。
# 所以此前"m247 是基线里已发布的迁移、函数体被改动会重跑"的说法不准确。真正成立的约束只有:
# 迁移函数体一旦进过任何库, 改一个字符(含注释/文档串)都会让 inspect.getsource 的 checksum
# 变化并被重新执行 —— 因此新列必须放新版本(249), 而不是改 247。

def test_m247_and_m248_bodies_untouched_and_m249_adds_the_user_scope(tmp_path, monkeypatch):
    from src.web import migrations as mig

    # 独立空库: 不能用 db fixture 的库(Base.metadata 已按当前模型建表, 会把 m247 的 DDL 骗过去)
    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "mig.db"
    assert not os.path.exists(path)
    eng = create_engine(f"sqlite:///{path}", future=True)
    assert str(tmp_path) in str(eng.url)

    with eng.begin() as conn:
        mig._m247_premarket_factor_evidence(conn)
        cols = {c["name"] for c in inspect(conn).get_columns("premarket_factor_evidence")}
        assert "run_trace_id" not in cols, (
            "m247 的函数体必须与首次落库时逐字一致(checksum 不能变); 加列必须在新的 m248/m249 里做"
        )

        mig._m248_premarket_evidence_run_binding(conn)
        cols_after = {c["name"] for c in inspect(conn).get_columns("premarket_factor_evidence")}
        assert "run_trace_id" in cols_after

        # 幂等: 再跑一次不报错, 不改数据
        mig._m248_premarket_evidence_run_binding(conn)
        n = conn.execute(text("SELECT COUNT(*) FROM premarket_factor_evidence")).scalar()
        assert n == 0

    versions = [m.version for m in mig.MIGRATIONS]
    assert versions == sorted(versions) and len(versions) == len(set(versions))
    assert versions[-1] == 250, (
        "新增列/索引必须排在新版本号上; 历史版本(246/247/248/249)号段不得跳号或复用"
    )


def test_m249_adds_user_column_and_partial_unique_key_without_touching_legacy_rows(
    tmp_path, monkeypatch
):
    """老库(存量行 user_id 全空、甚至已有同键重复)也要能升级, 且存量行一条不动。

    2026-11-12 三次修正: 老版本 m249 自己建 `ux_prediction_outcome_user_key`(无状态唯一键),
    它在"同键多版本行"的库上重跑会因重复键建索引失败 → 启动迁移中断。现在 m249 只加列 +
    普通索引, 计分行唯一性由 m250 的部分唯一索引承担 —— 所以这里先验 m249(不许出现那条
    索引), 再跑 m250 验唯一性。
    """
    from src.web import migrations as mig

    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "mig249.db"
    assert not os.path.exists(path)
    eng = create_engine(f"sqlite:///{path}", future=True)

    with eng.begin() as conn:
        # 模拟加列之前的老库: 没有 user_id, 而且同一键已有两行历史重复
        conn.execute(text(
            """
CREATE TABLE agent_prediction_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name VARCHAR(64) NOT NULL,
    stock_symbol VARCHAR(32) NOT NULL,
    stock_market VARCHAR(16) NOT NULL DEFAULT 'CN',
    prediction_date VARCHAR(10) NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 1,
    action VARCHAR(16) NOT NULL DEFAULT 'watch',
    action_label VARCHAR(32) NOT NULL DEFAULT '观望',
    confidence FLOAT,
    trigger_price FLOAT,
    outcome_price FLOAT,
    outcome_return_pct FLOAT,
    outcome_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    meta TEXT,
    evaluated_at TIMESTAMP,
    created_at TIMESTAMP
)
"""
        ))
        conn.execute(text(
            "INSERT INTO agent_prediction_outcomes "
            "(agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
            "VALUES ('premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
            "       ('premarket_outlook','600183','CN','2026-11-11',1,'sell')"
        ))

        mig._m249_prediction_outcome_user_scope(conn)
        cols = {c["name"] for c in inspect(conn).get_columns("agent_prediction_outcomes")}
        assert "user_id" in cols

        # 存量行原样保留: 行数不变、user_id 全空、动作没被改写
        rows = conn.execute(text(
            "SELECT id, user_id, action FROM agent_prediction_outcomes ORDER BY id"
        )).fetchall()
        assert len(rows) == 2
        assert all(r[1] is None for r in rows)
        assert [r[2] for r in rows] == ["buy", "sell"]

        # 幂等: 再执行一次不报错, 不新增列/不改数据
        mig._m249_prediction_outcome_user_scope(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 2

        # 老的无状态唯一键绝不能再出现(重跑建它会 UNIQUE 冲突 → 服务起不来)
        index_names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            ).fetchall()
        }
        assert "ux_prediction_outcome_user_key" not in index_names
        assert "ix_prediction_outcome_user" in index_names

        # 计分行唯一性由 m250 的部分唯一索引承担
        mig._m250_prediction_outcome_versions_and_system_scope(conn)

        # 部分唯一索引只约束有归属的行: 同用户同键第二次插入必须被 DB 拒绝
        conn.execute(text(
            "INSERT INTO agent_prediction_outcomes "
            "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
            "VALUES ('u-a','premarket_outlook','600183','CN','2026-11-11',1,'buy')"
        ))
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('u-a','premarket_outlook','600183','CN','2026-11-11',1,'sell')"
            ))
        # 另一个用户同键仍可各自一行(账户隔离, 不再撞同一个键)
        conn.execute(text(
            "INSERT INTO agent_prediction_outcomes "
            "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
            "VALUES ('u-b','premarket_outlook','600183','CN','2026-11-11',1,'sell')"
        ))
        assert conn.execute(text(
            "SELECT COUNT(*) FROM agent_prediction_outcomes WHERE user_id IS NOT NULL"
        )).scalar() == 2
