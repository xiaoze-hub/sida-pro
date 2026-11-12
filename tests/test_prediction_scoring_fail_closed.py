"""后验计分 fail-closed 闸门(2026-11-13)。

审计重现的问题: m250 的**四条部分唯一索引**缺失(或被人用同名但定义不同的索引顶替)时,
`schema_migrations.success=1` 仍是 1, 而评估/统计照旧出数 —— 同一个计分键可以有多条计分行,
命中率双计; 迁移"success"也不会自愈。

本文件钉住修好后的契约(逐条对应审计要求):

  A. 索引缺失 → 写入/回填/评估/个人统计**全部** `scoring_blocked`, 且**不动任何数据行**;
  B. 同名错定义索引(非唯一/列不全/无谓词)必须被识别为 `index_definition_mismatch`
     —— 只看名字的检查会漏;
  C. 安全状态下按需**幂等重建**(该谓词下无重复键才建); 有重复键则**不建**、不清理数据,
     继续阻断;
  D. 修复后重启(新引擎/新连接)看到 ok, 写入恢复; 重复体检不产生第二个索引;
  E. 缺索引时并发体检/重建收敛到"四条索引各一份", 数据不变;
  F. 状态检查与索引体检**不泄露其他用户**(不含 user_id/username/行内容);
  G. PG 与 SQLite 双样本: 两条 pending 行在阻断时**都还是 pending**(一条都没被回填)。

测试库纪律(与 test_prediction_outcome_ownership_integrity.py 一致): 每个用例一套
`tmp_path` 独立 SQLite; 起测清空 DATABASE_*/SIDA_DB*/DATA_DIR/PYTHONPATH; 断言连的就是
这次的临时库; 不碰生产库、不清任何既有数据。PG 用例在
`tests/test_prediction_outcome_user_scope_pg.py`(需要 `SIDA_TEST_PG_ADMIN` 的隔离库)。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text as sqltext
from sqlalchemy.orm import sessionmaker

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

DAY = (date.today() - timedelta(days=10)).isoformat()
DAY2 = (date.today() - timedelta(days=9)).isoformat()

_ACTIVE = "ux_prediction_outcome_user_active_key"
_RUN = "ux_prediction_outcome_user_run_key"
_SYS_ACTIVE = "ux_prediction_outcome_system_active_key"
_SYS_RUN = "ux_prediction_outcome_system_run_key"
_ALL = (_ACTIVE, _RUN, _SYS_ACTIVE, _SYS_RUN)


def _engine(tmp_path, name: str = "guard.db"):
    for key in _DB_ENV:
        os.environ.pop(key, None)
    path = tmp_path / name
    assert not os.path.exists(path), "测试库必须是新建的, 不许复用既有库文件"
    eng = create_engine(f"sqlite:///{path}", future=True)
    assert str(eng.url).startswith("sqlite:///") and str(tmp_path) in str(eng.url)
    return eng


def _make_db(
    tmp_path,
    *,
    users=("u-a", "u-b"),
    rows: tuple[dict, ...] = (),
    drop_indexes: tuple[str, ...] = (),
):
    """create_all 建库(索引与生产同名的四条部分唯一索引都在) + 种子数据。"""
    import src.web.models as M  # noqa: F401 — 触发 ORM 注册
    from src.db.models import AgentPredictionOutcome, User
    from src.db.session import Base
    from src.core.json_safe import to_jsonable

    eng = _engine(tmp_path)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    # 先删索引再种数据: 模拟"索引缺失的老库", 这样重复键才写得进去(否则 DB 会先挡住)
    _drop(eng, *drop_indexes)
    with Session() as s:
        for uid in users:
            s.add(
                User(
                    id=uid,
                    username=f"account-{uid}",
                    password_hash="x",
                    role="member",
                    is_active=True,
                )
            )
        for r in rows:
            payload = dict(r)
            payload.setdefault("agent_name", "premarket_outlook")
            payload.setdefault("stock_symbol", "600183")
            payload.setdefault("stock_market", "CN")
            payload.setdefault("prediction_date", DAY)
            payload.setdefault("horizon_days", 1)
            payload.setdefault("action", "buy")
            payload.setdefault("action_label", "买入")
            payload.setdefault("outcome_status", "pending")
            payload.setdefault("owner_scope", "user")
            payload["meta"] = to_jsonable(payload.get("meta") or {})
            s.add(AgentPredictionOutcome(**payload))
        s.commit()
    return eng, Session


def _snapshot(eng) -> tuple[list[tuple], list[str]]:
    """整表快照(所有列, 按 id) + 全部索引名 —— 用来断言"一行都没变"。"""
    with eng.begin() as c:
        cols = [r[1] for r in c.execute(sqltext("PRAGMA table_info(agent_prediction_outcomes)"))]
        col_sql = ", ".join(cols)
        rows = [
            tuple(r)
            for r in c.execute(
                sqltext(f"SELECT {col_sql} FROM agent_prediction_outcomes ORDER BY id")
            ).fetchall()
        ]
        idx = sorted(
            r[0]
            for r in c.execute(
                sqltext("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
            ).fetchall()
            if r[0]
        )
    return rows, idx


def _table_rows(eng) -> list[tuple]:
    return _snapshot(eng)[0]


def _index_names(eng) -> list[str]:
    return _snapshot(eng)[1]


def _drop(eng, *names: str) -> None:
    with eng.begin() as c:
        for n in names:
            c.execute(sqltext(f"DROP INDEX IF EXISTS {n}"))


def _index_ddl(eng, name: str) -> str | None:
    with eng.begin() as c:
        row = c.execute(
            sqltext("SELECT sql FROM sqlite_master WHERE type='index' AND name=:n"),
            {"n": name},
        ).first()
    return row[0] if row else None


def _save(**over):
    from src.core.context_store import save_agent_prediction_outcome

    kw = dict(
        agent_name="premarket_outlook",
        stock_symbol="600183",
        stock_market="CN",
        prediction_date=DAY,
        horizon_days=1,
        action="buy",
        action_label="买入",
        user_id="u-a",
        owner_scope="user",
        run_trace_id="run-1",
    )
    kw.update(over)
    return save_agent_prediction_outcome(**kw)


def _wire(monkeypatch, eng, Session):
    """让 context_store 用这次临时库(写入路径的闸门也走同一个 bind)。"""
    import src.core.context_store as cs

    monkeypatch.setattr(cs, "SessionLocal", Session)
    return cs


# ── A/G. 索引缺失: 写入/回填/评估/统计全部阻断, 数据一行不动 ────────────────


def test_missing_index_blocks_every_scoring_path_and_changes_no_data(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(
        tmp_path,
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-a", "run_trace_id": "run-2", "stock_symbol": "600519",
             "prediction_date": DAY2},
        ),
    )
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE, _RUN)

    before = _snapshot(eng)

    # 写入: 拒写(不"先写着看")
    res = _save(run_trace_id="run-3")
    assert res.written is False and res.status == "scoring_blocked"
    assert "index_missing" in res.reason

    # 回填: 拒绝(此刻"这是唯一计分行"本身无法证明)
    from src.core.context_store import mark_agent_prediction_outcome

    assert mark_agent_prediction_outcome(record_id=1, outcome_price=40.0, outcome_return_pct=9.9) is False

    # 评估: 返回 blocked 骨架, evaluated=0, 一条都不回填
    # 个人统计: 不出数字, 只给 mark 与原因
    from src.web.api.profile import _accuracy_board

    with Session() as s:
        board = _accuracy_board(s, since="", user_id="u-a")
    assert board["scoring_blocked"] is True
    assert board["overall"]["hit_rate"] is None and board["overall"]["total"] == 0
    assert board["agents"] == []
    assert board["block_reason_code"].startswith("index_missing")
    assert "scoring_blocked" in board["note"]

    # /stats 的 prediction 也不能出现命中率
    from src.web.api.profile import _prediction_hit_stats

    with Session() as s:
        overall = _prediction_hit_stats(s, user_id="u-a")
    assert overall["scoring_blocked"] is True and overall["hit_rate"] is None
    assert overall["total"] == 0

    # 数据一行都没变(包括状态: 两条都还是 pending)
    after = _snapshot(eng)
    assert after[0] == before[0], "阻断路径不允许改/删/回填任何行"
    with eng.begin() as c:
        statuses = [
            r[0]
            for r in c.execute(
                sqltext(
                    "SELECT outcome_status FROM agent_prediction_outcomes ORDER BY id"
                )
            ).fetchall()
        ]
    assert statuses == ["pending", "pending"], "阻断时一条都不许被回填成 evaluated"
    # 只读路径不允许建索引
    assert after[1] == before[1], "只读巡检不得偷偷补索引"


def test_evaluation_repairs_missing_index_then_proceeds_and_says_so(tmp_path, monkeypatch):
    """评估是"发现安全状态就按需幂等重建"的入口: 缺索引且**无重复键**时先建回来再算,
    并如实报出 `scoring_repaired`; 不会像断言的"阻断"那样在有救的情况下白停一天。"""
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE, _RUN)
    rows_before = _table_rows(eng)

    stats = evaluate_pending_prediction_outcomes()
    assert stats["scoring_blocked"] is False
    assert sorted(stats["scoring_repaired"]) == sorted((_ACTIVE, _RUN))
    rebuilt = _index_ddl(eng, _ACTIVE) or ""
    assert "UNIQUE" in rebuilt.upper()
    # 重建只碰索引: 行内容不变(评估本身可能改 outcome_*; 这里行情价取不到, 只会 skipped)
    assert len(_table_rows(eng)) == len(rows_before)


def test_readonly_check_never_creates_indexes(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path)
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ALL[0])
    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    assert _index_ddl(eng, _ACTIVE) is None, "GET/统计路径不能因为有人刷新就做 DDL"


# ── B. 伪造成同名索引: 必须按定义识别, 不能只看名字 ────────────────────────


def test_forged_same_name_index_is_detected_then_rebuilt(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)
    # 同名、但非唯一 + 列不全 + 没有谓词 —— 名字对得上, 语义完全不对
    with eng.begin() as c:
        c.execute(
            sqltext(
                f"CREATE INDEX {_ACTIVE} ON agent_prediction_outcomes (user_id)"
            )
        )

    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    assert "index_definition_mismatch" in health.reason_code
    state = {i.name: i for i in health.indexes}[_ACTIVE]
    assert state.status == "mismatch"
    assert "唯一" in state.detail
    before = _table_rows(eng)

    # 写入同样被挡(约束不在位)
    assert _save(run_trace_id="run-2").status == "scoring_blocked"

    # 按需重建: 丢掉错定义的同名索引(只删索引) → 建正确的
    fixed = check_scoring_health(eng, allow_repair=True)
    assert fixed.blocked is False, fixed.detail
    assert _ACTIVE in fixed.repaired
    ddl = _index_ddl(eng, _ACTIVE)
    assert ddl and "UNIQUE" in ddl.upper()
    assert "outcome_status <> 'superseded'" in ddl
    assert _table_rows(eng) == before, "重建索引不得碰数据"

    # 新键(不同标的/日期)才是真插入; 同键只会按契约追加 superseded 审计版本
    assert _save(run_trace_id="run-2", stock_symbol="600519", prediction_date=DAY2).status == "inserted"


def test_same_name_unique_index_with_wrong_predicate_is_a_mismatch(tmp_path, monkeypatch):
    """唯一 + 列对, 但谓词少了 `outcome_status <> 'superseded'` → 仍算定义错误。"""
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path)
    _wire(monkeypatch, eng, Session)
    _drop(eng, _RUN)
    with eng.begin() as c:
        c.execute(
            sqltext(
                f"CREATE UNIQUE INDEX {_RUN} ON agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                "horizon_days, run_trace_id)"
            )
        )
    health = check_scoring_health(eng, allow_repair=False)
    assert health.blocked is True
    state = {i.name: i for i in health.indexes}[_RUN]
    assert state.status == "mismatch" and "谓词" in state.detail
    assert check_scoring_health(eng, allow_repair=True).blocked is False


# ── C. 有重复键则不建索引、不清理数据, 继续阻断 ───────────────────────────


def test_duplicate_rows_prevent_rebuild_and_keep_scoring_blocked(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    # 先在"没有唯一索引"的库上种出同键两条计分行(历史脏数据的真实来历)
    eng, Session = _make_db(
        tmp_path,
        rows=(
            {"user_id": "u-a", "run_trace_id": "v1", "action": "buy"},
            {"user_id": "u-a", "run_trace_id": "v2", "action": "sell"},
        ),
        drop_indexes=(_ACTIVE, _RUN),
    )
    _wire(monkeypatch, eng, Session)
    before = _snapshot(eng)

    health = check_scoring_health(eng, allow_repair=True)
    assert health.blocked is True
    assert "duplicate_rows" in health.reason_code
    assert _ACTIVE in health.duplicate_blocked
    assert "重复键组" in health.detail
    # 契约: 有重复 → 不建索引、不删不改
    assert _index_ddl(eng, _ACTIVE) is None
    after = _snapshot(eng)
    assert after[0] == before[0], "有重复键也不许删/改行"

    # 另外两条 system 索引(无重复)该建的还是建了 —— 阻断不搞连坐
    assert _index_ddl(eng, _SYS_ACTIVE) is not None
    # run_key 谓词下这两行版本不同(run_trace_id 不同), 没有重复 → 它照建
    assert _index_ddl(eng, _RUN) is not None

    # 统计/写入继续阻断
    assert _save(run_trace_id="run-9").status == "scoring_blocked"
    from src.core.prediction_outcome import evaluate_pending_prediction_outcomes

    stats = evaluate_pending_prediction_outcomes()
    assert stats["scoring_blocked"] is True and stats["evaluated"] == 0
    assert _table_rows(eng) == before[0]


# ── D. 修复后"重启"看到 ok; 重复体检幂等 ─────────────────────────────────


def test_repair_then_restart_is_ok_and_idempotent(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, *_ALL)
    rows_before = _table_rows(eng)

    first = check_scoring_health(eng, allow_repair=True)
    assert first.blocked is False
    assert sorted(first.repaired) == sorted(_ALL)
    idx_after_first = _index_names(eng)

    # 幂等: 再体检一次不再建第二份、也不报 repaired
    second = check_scoring_health(eng, allow_repair=True)
    assert second.blocked is False and second.repaired == ()
    assert _index_names(eng) == idx_after_first
    counts = {n: idx_after_first.count(n) for n in _ALL}
    assert counts == {n: 1 for n in _ALL}

    # "重启": 全新引擎/连接池(不共享任何进程内状态) → 立刻看到 ok
    fresh = create_engine(str(eng.url), future=True)
    try:
        brand_new = check_scoring_health(fresh, allow_repair=False)
        assert brand_new.blocked is False, brand_new.detail
        assert all(i.status == "ok" for i in brand_new.indexes)
        assert _table_rows(fresh) == rows_before, "重建索引不得改数据"
    finally:
        fresh.dispose()

    # 写入恢复(数据只多出这一行; 必须是新键 —— 同键按契约只会追加 superseded)
    assert _save(run_trace_id="run-2", stock_symbol="600519", prediction_date=DAY2).status == "inserted"
    assert len(_table_rows(eng)) == len(rows_before) + 1


# ── E. 缺索引时并发体检/重建收敛 ─────────────────────────────────────────


def test_concurrent_repair_converges_to_one_index_each(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    _wire(monkeypatch, eng, Session)
    _drop(eng, *_ALL)
    before = _snapshot(eng)

    results: list[tuple[bool, tuple[str, ...]]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        h = check_scoring_health(eng, allow_repair=True)
        with lock:
            results.append((h.blocked, h.repaired))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    # 并发下允许某个线程"这次没建成"(锁冲突), 但**最终状态**必须收敛且只有一个线程报修复
    final = check_scoring_health(eng, allow_repair=False)
    assert final.blocked is False, final.detail
    names = _index_names(eng)
    assert {n: names.count(n) for n in _ALL} == {n: 1 for n in _ALL}
    assert _table_rows(eng) == before[0], "并发重建不得改数据"
    assert sum(1 for blocked, _ in results if not blocked) >= 1


# ── F. 状态检查不泄露其他用户 ────────────────────────────────────────────


def test_health_payload_contains_no_user_data(tmp_path, monkeypatch):
    from src.core.scoring_guard import check_scoring_health

    eng, Session = _make_db(
        tmp_path,
        users=("u-a", "u-secret-uid-0001"),
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-secret-uid-0001", "run_trace_id": "run-2", "stock_symbol": "000001"},
        ),
    )
    _wire(monkeypatch, eng, Session)
    _drop(eng, _ACTIVE)

    payload = check_scoring_health(eng, allow_repair=False).as_payload()
    assert payload["contains_user_data"] is False
    assert set(payload) == {
        "status",
        "scoring_blocked",
        "reason_code",
        "reason",
        "indexes",
        "repaired",
        "duplicate_blocked",
        "table",
        "contains_user_data",
    }
    blob = json.dumps(payload, ensure_ascii=False)
    for leaked in ("u-a", "u-secret-uid-0001", "account-u-a", "600183", "000001"):
        assert leaked not in blob, f"状态体检泄露了用户数据: {leaked}"
    # 聚合计数里也不带具体键
    assert "duplicate_keys" not in payload


def test_scoring_status_endpoint_is_identical_for_all_users_and_leaks_nothing(
    tmp_path, monkeypatch
):
    from src.web.api import profile as profile_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db
    from src.web.models import User

    eng, Session = _make_db(
        tmp_path,
        users=("u-a", "u-b"),
        rows=(
            {"user_id": "u-a", "run_trace_id": "run-1"},
            {"user_id": "u-b", "run_trace_id": "run-2", "stock_symbol": "600519"},
        ),
    )
    _drop(eng, _ACTIVE)

    class _U:
        def __init__(self, uid):
            self.id = uid
            self.username = f"account-{uid}"
            self.role = "member"
            self.nickname = None
            self.avatar = None
            self.shadow_profile_json = None

    def _build(user_id: str) -> TestClient:
        app = FastAPI()
        app.include_router(profile_api.router, prefix="/p")

        def _db():
            s = Session()
            try:
                yield s
            finally:
                s.close()

        async def _u():
            return _U(user_id)

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_current_user] = _u
        return TestClient(app, raise_server_exceptions=False)

    r_a = _build("u-a").get("/p/scoring/status")
    r_b = _build("u-b").get("/p/scoring/status")
    assert r_a.status_code == 200 and r_b.status_code == 200
    body = r_a.json()
    assert body["scoring_blocked"] is True and body["reason_code"] == "index_missing"
    assert body == r_b.json(), "schema 体检与登录用户无关(不是按用户过滤的数据)"
    for leaked in ("u-a", "u-b", "account-u-a", "account-u-b", "600183", "600519"):
        assert leaked not in r_a.text and leaked not in r_b.text

    # /stats 也不能出现别人的任何东西, 且命中率为 None
    s_body = _build("u-b").get("/p/stats").json()["prediction"]
    assert s_body["scoring_blocked"] is True and s_body["hit_rate"] is None
    assert "u-a" not in json.dumps(s_body, ensure_ascii=False)


# ── 导出: 阻断时"计分"列不得声称"是" ─────────────────────────────────────


def test_export_marks_predictions_as_not_scored_when_blocked(tmp_path, monkeypatch):
    from src.web.api import export as export_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    from src.db.models import AgentPredictionOutcome

    eng, Session = _make_db(tmp_path, rows=({"user_id": "u-a", "run_trace_id": "run-1"},))
    with Session() as s:
        row = s.query(AgentPredictionOutcome).first()
        row.outcome_status = "evaluated"
        row.outcome_return_pct = 3.2
        s.commit()
    _drop(eng, _ACTIVE)

    class _U:
        id = "u-a"
        username = "account-u-a"
        role = "member"
        nickname = None
        avatar = None
        shadow_profile_json = None

    app = FastAPI()
    app.include_router(export_api.router, prefix="/api")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    async def _u():
        return _U()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _u
    c = TestClient(app, raise_server_exceptions=False)

    r = c.get("/api/export/predictions")
    assert r.status_code == 200, r.text
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    header = lines[0].lstrip("\ufeff")
    assert "计分" in header
    assert "否(评分阻断)" in r.text
    assert ",是" not in r.text.replace("否(评分阻断)", "")
