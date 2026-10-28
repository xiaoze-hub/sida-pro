"""服务端确认标注上图的后端契约（2026-10-28, P2 可追溯契约）。

问题：AI 提议的标注（`copilot_actions.kind == "annotation"`）点确认后确实落了
`kline_annotations`，但**确认响应里没有那条标注的 id**，`copilot_actions` 也没有指向
它的列 —— 于是三件事做不到：

1. 前端确认后无法把「图上这条线」和「刚才那次动作」对上（点第二次、刷新、回滚都失去锚点）；
2. 事后排障只能靠 symbol/label/价格猜，两条同名标注就分不清是哪次确认画的；
3. 越权/重复确认没有服务端可判定的边界（谁能确认、能确认几次、画到谁名下）。

本文件钉住最小契约：
  - 一次 action 至多关联一条 annotation（`copilot_actions.annotation_id`，唯一索引），
    确认响应用**真实** `annotation_id` 回传，并回传实际落库的 symbol/market/period；
  - 归属一律取自身份（action 的 user_id），payload 里的 `user_id`/`annotation_id` 一律拒绝；
  - 越权（他人 action）默认拒绝且不产生任何标注行；
  - 历史数据（迁移前已 applied、annotation_id 为 NULL）照样能读，**不猜测**补 id；
  - 周期/价位做写入端校验，避免再出现「确认成功但图上查不到」。

2026-10-28 合并阻断返工（§6-§10）：
  - v231 的唯一索引建不起来必须**失败**：不许吞异常记 success=1（SQLite 会静默丢约束），
    也不许让 PG 事务留在 aborted 把真实原因换成 `InFailedSqlTransaction`；
  - 写入端把 market 归一成大写、`1M` 认成月线（不是 1 分钟）、price/ts 至少一个、
    ts 超长或格式非法直接 400 并整笔回滚；
  - 弱引用悬空（标注被删后 SQLite rowid 复用同一个 id）不许让确认**永远**失败；
  - annotation 不可回滚就如实说，不许先给 `can_rollback=true` 再报"没有快照"。

真实 PG 验证（CI 暂无 PG service，**默认 skip**，本地/隔离库手动跑）::

    SIDA_TEST_PG_URL="postgresql+psycopg2://user:pw@127.0.0.1:5432/<隔离库>" \\
      python -m pytest tests/test_copilot_annotation_contract.py -q

  该库会被本文件 drop/create `copilot_actions` / `kline_annotations` /
  `schema_migrations` 三张表 —— 必须指向**隔离库**，绝不能指生产库。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.db.models import CopilotAction, KlineAnnotation, Stock, User
from src.db.session import SessionLocal
from src.harness import actions as A

#: 36 字符（UUID 形状）—— 必须与线上 `users.id VARCHAR(36)` 同宽。
#: 2026-10-28 复审: 旧值是 37 字符，SQLite 照收，PG 直接
#: StringDataRightTruncation → 这组契约在真实库上一条都跑不起来。
UID = "a7c2e5d1-33b8-4f6a-9d21-anncontract0"
OTHER_UID = "b8d3f6e2-44c9-4a7b-8e32-anncontract1"
SYMBOL = "600519"

assert len(UID) == 36 and len(OTHER_UID) == 36, "ids 必须落在 users.id VARCHAR(36) 之内"


def _db():
    return SessionLocal()


def _cleanup(db) -> None:
    db.rollback()
    for uid in (UID, OTHER_UID):
        db.query(CopilotAction).filter(CopilotAction.user_id == uid).delete()
        db.query(KlineAnnotation).filter(KlineAnnotation.user_id == uid).delete()
        db.query(Stock).filter(Stock.user_id == uid).delete()
        db.query(User).filter(User.id == uid).delete()
    db.commit()


def _ensure_users(db) -> None:
    """stocks.user_id 有外键 → 归属用例里的账号必须先存在。"""
    for uid, name in ((UID, "ann-contract-owner"), (OTHER_UID, "ann-contract-other")):
        if db.query(User).filter(User.id == uid).first() is None:
            db.add(User(id=uid, username=name, password_hash="x", role="member"))
    db.commit()


@pytest.fixture()
def db():
    session = _db()
    _cleanup(session)
    _ensure_users(session)
    try:
        yield session
    finally:
        _cleanup(session)
        session.close()


def _propose(db, payload: dict, user_id: str = UID) -> dict:
    return A.propose(db, user_id=user_id, kind="annotation", payload=payload)


# ── 1. 确认响应带回真实 annotation_id，并与 action 双向可追溯 ────────────────

def test_confirm_returns_real_annotation_id_and_links_action(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "缩量假阳", "price": 35.23,
                         "note": "量 23.94 万手/额 8.42 亿"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["status"] == "applied"
    assert isinstance(out["annotation_id"], int) and out["annotation_id"] > 0

    row = db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).one()
    assert row.id == out["annotation_id"], "响应里的 id 必须就是落库那条标注"
    assert row.symbol == SYMBOL and row.price == 35.23 and row.label == "缩量假阳"

    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.annotation_id == row.id, "action → annotation 的关联必须落库"

    # 明细块只回传事实，不额外编字段
    detail = out["annotation"]
    assert detail["id"] == row.id
    assert detail["symbol"] == SYMBOL and detail["period"] == "1d"
    assert detail["source"] == "ai"


def test_confirm_echoes_symbol_and_period_and_is_visible_on_chart(db):
    """确认时写的 symbol/market/period 必须就是图上按这些参数能查到的那条。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "30 分钟放量", "price": 35.2,
                         "market": "SH", "period": "30m", "ts": "2026-10-28 10:00"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["annotation"]["market"] == "SH"
    assert out["annotation"]["period"] == "30m"
    assert out["annotation"]["ts"] == "2026-10-28 10:00"

    from src.web.api.copilot import get_copilot_annotations

    class _U:
        id = UID

    # 页面用 CN 查（A 股一组等价）也必须命中，且 id 与确认响应一致
    rows = get_copilot_annotations(symbol=SYMBOL, market="CN", period="30m", db=db, user=_U())
    assert [r["id"] for r in rows] == [out["annotation_id"]]


def test_period_url_alias_is_normalized_to_chart_interval(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "周线级别", "period": "w1", "price": 35.2})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])
    assert out["annotation"]["period"] == "1w"


def test_unknown_period_is_rejected_without_partial_write(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "三小时", "period": "3h"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])
    assert "周期" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    # 认领失败必须回滚，动作继续可确认（不能留下 applied 却没有标注）
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "pending"


def test_invalid_price_is_rejected_instead_of_written_as_garbage(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "错价", "price": "abc"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])
    assert "价位" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0


# ── 2. 越权默认拒绝 ──────────────────────────────────────────────────────

def test_apply_other_users_action_is_denied_and_writes_nothing(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "别人的线", "price": 35.0}, user_id=OTHER_UID)

    with pytest.raises(A.ActionError):
        A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == OTHER_UID).count() == 0
    owner_action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert owner_action.status == "pending", "越权尝试不能改变他人动作状态"


def test_payload_cannot_inject_annotation_id_or_owner(db):
    """归属和 annotation_id 只能由服务端定；payload 里带了就等于试图改别人的图。"""
    victim = KlineAnnotation(user_id=OTHER_UID, symbol=SYMBOL, market="CN", period="1d",
                             price=99.0, label="他人标注", source="user")
    db.add(victim)
    db.commit()
    db.refresh(victim)

    for bad in ({"annotation_id": victim.id}, {"user_id": OTHER_UID}):
        item = _propose(db, {"symbol": SYMBOL, "label": "伪造", "price": 35.0, **bad})
        with pytest.raises(A.ActionError):
            A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(KlineAnnotation).filter(KlineAnnotation.id == victim.id).one().user_id == OTHER_UID


def test_apply_is_one_shot_per_action(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "只画一次", "price": 35.0})
    first = A.apply_action(db, user_id=UID, action_id=item["id"])

    with pytest.raises(A.ActionError):
        A.apply_action(db, user_id=UID, action_id=item["id"])

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.annotation_id == first["annotation_id"]


def test_reject_cannot_overwrite_an_applied_action(db):
    """已 applied 的动作被 reject 掉会让「有标注但动作显示被拒绝」——必须拒绝。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "已确认", "price": 35.0})
    applied = A.apply_action(db, user_id=UID, action_id=item["id"])

    with pytest.raises(A.ActionError):
        A.reject_action(db, user_id=UID, action_id=item["id"])

    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "applied"
    assert action.annotation_id == applied["annotation_id"]


def test_concurrent_second_confirmer_loses_and_draws_nothing(db):
    """两个会话都以为还是 pending（连点/双击/两个标签页）→ 只有一个能画上线。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "并发确认", "price": 35.0})
    other = _db()
    try:
        stale = other.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
        assert stale.status == "pending"

        won = A.apply_action(db, user_id=UID, action_id=item["id"])
        assert won["annotation_id"]

        with pytest.raises(A.ActionError):
            A.apply_action(other, user_id=UID, action_id=item["id"])
        assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1
    finally:
        other.close()


# ── 3. 历史数据兼容（迁移前 applied、无 annotation_id）────────────────────

def test_legacy_applied_action_without_link_reads_as_null_not_guessed(db):
    legacy = CopilotAction(
        user_id=UID, kind="annotation", status="applied", result="已标注 600519 @ 35.0：旧线",
        payload='{"symbol": "600519", "label": "旧线", "price": 35.0}',
    )
    db.add(legacy)
    db.commit()
    db.refresh(legacy)

    out = A._to_dict(legacy)
    assert out["annotation_id"] is None, "历史记录没有关联就是没有，不许猜一个"
    assert out["annotation"] is None
    assert out["kind"] == "annotation" and out["status"] == "applied"


def test_non_annotation_actions_keep_null_link(db):
    item = A.propose(db, user_id=UID, kind="watchlist", payload={"symbol": SYMBOL})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])
    assert out["annotation_id"] is None and out["annotation"] is None


# ── 3.5 HTTP 层：确认响应与越权状态码 ─────────────────────────────────────

def test_api_confirm_returns_annotation_id_and_cross_user_is_400(db):
    from fastapi import HTTPException

    from src.web.api.copilot import apply_copilot_action

    class _U:
        def __init__(self, uid: str):
            self.id = uid

    item = _propose(db, {"symbol": SYMBOL, "label": "API 确认", "price": 35.0})
    out = apply_copilot_action(action_id=item["id"], db=db, user=_U(UID))
    assert out["annotation_id"] == out["annotation"]["id"]

    other = _propose(db, {"symbol": SYMBOL, "label": "他人动作", "price": 35.0}, user_id=OTHER_UID)
    with pytest.raises(HTTPException) as ei:
        apply_copilot_action(action_id=other["id"], db=db, user=_U(UID))
    assert ei.value.status_code == 400
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1


def test_scenario_proposal_confirms_into_traceable_annotation(db):
    """真实链路：情景矩阵提议 → 用户确认 → 图上按页面参数能查到同一个 id。"""
    matrix = {"available": True, "levels": {"support": 95.5, "resist": 108.25}}
    proposed = A.propose_scenario_annotations(
        db, user_id=UID, symbol=SYMBOL, market="SH", matrix=matrix,
    )
    support = next(p for p in proposed if p["payload"]["label"] == "支撑位")

    out = A.apply_action(db, user_id=UID, action_id=support["id"])
    assert out["annotation_id"] and out["annotation"]["price"] == 95.5
    assert out["annotation"]["market"] == "SH" and out["annotation"]["period"] == "1d"

    from src.web.api.copilot import get_copilot_annotations

    class _U:
        id = UID

    rows = get_copilot_annotations(symbol=SYMBOL, market="SH", period="1d", db=db, user=_U())
    assert out["annotation_id"] in [r["id"] for r in rows]


# ── 4. 迁移：加列 + 唯一索引，可重跑 ──────────────────────────────────────

def _has_column(conn, table: str, column: str) -> bool:
    return column in _columns(conn, table)


def _has_index(conn, table: str, name: str) -> bool:
    return any(n == name for n, _unique in _indexes(conn, table))


def test_migration_adds_column_and_unique_link_index_idempotently(db):
    from src.web.migrations import MIGRATIONS, _m231_copilot_action_annotation_link

    assert 231 in [m.version for m in MIGRATIONS], "v231 必须在 MIGRATIONS 元组里注册"

    with db.get_bind().begin() as conn:
        _m231_copilot_action_annotation_link(conn)
        _m231_copilot_action_annotation_link(conn)  # 幂等：重跑不炸
        assert _has_column(conn, "copilot_actions", "annotation_id")
        assert _has_index(conn, "copilot_actions", "uq_copilot_actions_annotation")

    # 唯一索引兜住「一条标注被两次确认占用」：非空值重复必须失败
    a1 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending")
    a2 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending")
    db.add_all([a1, a2])
    db.commit()
    a1.annotation_id = 987654321
    db.commit()
    a2.annotation_id = 987654321
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_migration_alters_a_legacy_table_that_lacks_the_column(dialect, mig_engine):
    """存量库路径：老 copilot_actions 没有 annotation_id 时, v231 必须真把它加上。"""
    from src.web.migrations import _m231_copilot_action_annotation_link

    engine = mig_engine(dialect)
    _reset_actions_table(engine, with_column=False)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO copilot_actions (user_id, kind, payload, status)"
            " VALUES ('legacy', 'annotation', '{}', 'applied')"
        ))
        assert "annotation_id" not in _columns(conn, "copilot_actions")

        _m231_copilot_action_annotation_link(conn)
        assert "annotation_id" in _columns(conn, "copilot_actions")
        # 历史行不被回填, 也不会因为唯一索引失败
        assert conn.execute(text(
            "SELECT annotation_id FROM copilot_actions WHERE user_id='legacy'"
        )).scalar() is None
        _m231_copilot_action_annotation_link(conn)  # 幂等
        assert ("uq_copilot_actions_annotation", True) in _indexes(conn, "copilot_actions")


# ── 5. 读取路径：精确身份与目标（2026-10-28 P2 三次收口复审整改）───────────────
#
# 复审红线：旧读取响应只有 id/price/ts/label/note/color/source —— 前端拿不到属主、
# 标的、周期，也无法把「图上这条线」对回「哪次确认」，只能靠本地会话信封 + 客户端
# 标记自证（可伪造）。这一组钉住服务端把身份与目标**回显出来**，且可用严格参数
# 精确读取（ids / market_strict / mine_only / action_linked）。


#: 精确读取用例专用标的：避开同文件其它用例/遗留全局行（全局行没有属主，fixture 不清）。
SYM_ISO = "600521"
SYM_ISO2 = "600522"


class _U:
    def __init__(self, uid: str = UID):
        self.id = uid


def _read(db, **kw):
    from src.web.api.copilot import get_copilot_annotations

    params = {"symbol": SYMBOL, "market": "CN", "period": "1d", "db": db, "user": _U()}
    params.update(kw)
    return get_copilot_annotations(**params)


def test_read_echoes_owner_target_and_action_link(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "可追溯", "price": 35.23})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    rows = _read(db, ids=str(out["annotation_id"]))
    assert len(rows) == 1, "按 id 精确读取必须只回这一条"
    row = rows[0]
    assert row["id"] == out["annotation_id"]
    # 目标回显：前端据此逐字核对当前 K 线，而不是靠「请求—响应配对」猜
    assert row["symbol"] == SYMBOL and row["market"] == "CN" and row["period"] == "1d"
    # 属主回显：全局行 user_id 为 null，前端默认不上图
    assert row["user_id"] == UID
    # 动作回显：这一行是哪次确认画的
    assert row["action_id"] == item["id"]


def test_read_marks_unconfirmed_rows_without_action(db):
    """历史/人工/他人留下的行没有确认动作 → action_id=null，前端据此拒画。"""
    orphan = KlineAnnotation(user_id=UID, symbol=SYM_ISO, market="CN", period="1d",
                             price=33.3, label="无动作行", source="ai")
    global_row = KlineAnnotation(user_id=None, symbol=SYM_ISO, market="CN", period="1d",
                                 price=44.4, label="全局行", source="ai")
    db.add_all([orphan, global_row])
    db.commit()

    rows = _read(db, symbol=SYM_ISO)
    by_label = {r["label"]: r for r in rows}
    assert by_label["无动作行"]["action_id"] is None
    assert by_label["无动作行"]["user_id"] == UID
    assert by_label["全局行"]["user_id"] is None, "全局行的属主必须如实是 null"
    assert by_label["全局行"]["action_id"] is None


def test_read_action_linked_filters_out_rows_without_confirmation(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "已确认", "price": 35.23})
    confirmed = A.apply_action(db, user_id=UID, action_id=item["id"])
    db.add(KlineAnnotation(user_id=UID, symbol=SYMBOL, market="CN", period="1d",
                           price=31.1, label="未确认", source="ai"))
    db.commit()

    rows = _read(db, action_linked=True)
    assert [r["id"] for r in rows] == [confirmed["annotation_id"]]


def test_read_mine_only_excludes_global_rows(db):
    db.add(KlineAnnotation(user_id=UID, symbol=SYM_ISO2, market="CN", period="1d",
                           price=31.1, label="我的", source="ai"))
    db.add(KlineAnnotation(user_id=None, symbol=SYM_ISO2, market="CN", period="1d",
                           price=32.2, label="全局", source="ai"))
    db.commit()

    assert sorted(r["label"] for r in _read(db, symbol=SYM_ISO2)) == ["全局", "我的"]
    assert [r["label"] for r in _read(db, symbol=SYM_ISO2, mine_only=True)] == ["我的"]


def test_read_ids_is_exact_and_cannot_see_other_users_rows(db):
    mine = KlineAnnotation(user_id=UID, symbol=SYMBOL, market="CN", period="1d",
                           price=31.1, label="我的", source="ai")
    theirs = KlineAnnotation(user_id=OTHER_UID, symbol=SYMBOL, market="CN", period="1d",
                             price=32.2, label="他人的", source="ai")
    db.add_all([mine, theirs])
    db.commit()

    rows = _read(db, ids=f"{mine.id},{theirs.id}")
    assert [r["id"] for r in rows] == [mine.id], "他人的行即使 id 精确指定也不可见"


def test_read_rejects_malformed_ids(db):
    from fastapi import HTTPException

    for bad in ("abc", "1,x", "0", "-3"):
        with pytest.raises(HTTPException) as ei:
            _read(db, ids=bad)
        assert ei.value.status_code == 400


def test_read_market_strict_keeps_index_and_stock_apart(db):
    """000001 同时是上证指数(SH)与平安银行(SZ)：族归并会把两条都吐出来。

    这正是前端「后端无法保证标的类型则默认拒画」的依据 —— 服务端只能靠精确 market
    区分，而 propose 写入的 market 可能是 CN（族语义），此时无法证明是哪一类。
    """
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SH", period="1d",
                           price=3200.0, label="指数线", source="ai"))
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                           price=12.5, label="个股线", source="ai"))
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="CN", period="1d",
                           price=1.0, label="族语义线", source="ai"))
    db.commit()

    merged = _read(db, symbol="000001", market="CN")
    assert {r["label"] for r in merged} == {"指数线", "个股线", "族语义线"}

    strict_sz = _read(db, symbol="000001", market="SZ", market_strict=True)
    assert [r["label"] for r in strict_sz] == ["个股线"]
    strict_cn = _read(db, symbol="000001", market="CN", market_strict=True)
    assert [r["label"] for r in strict_cn] == ["族语义线"], (
        "严格模式下 CN 行不得被当成 SZ/SH —— 它无法证明标的类型"
    )


# ── 6. 迁移 v231：唯一索引建不起来必须失败（不许吞异常 / 不许记成功）───────────
#
# 复审红线：旧实现 `try: CREATE UNIQUE INDEX ... except: logger.warning(不阻断)`。
#   - SQLite: 建索引失败被吞 → schema_migrations 记 success=1，库里**没有**唯一约束，
#     契约静默失效；
#   - PG: 语句失败后事务进入 aborted，吞掉异常再写 success=1 必然
#     `InFailedSqlTransaction`，真正原因（annotation_id 重复）被这句废话掩盖。
# 两条路径都必须：整个迁移事务回滚、明确失败、真实原因落进 schema_migrations.error。
# 同一组用例在 SQLite 与真实 PG 上各跑一遍（PG 由 SIDA_TEST_PG_URL 指向隔离库）。


def _pg_url() -> str:
    return os.environ.get("SIDA_TEST_PG_URL", "").strip()


@pytest.fixture()
def mig_engine():
    """按方言造引擎：sqlite 用内存库，postgresql 用 SIDA_TEST_PG_URL 指向的隔离库。"""
    made: list = []

    def _get(dialect: str):
        if dialect == "sqlite":
            eng = create_engine("sqlite://")
        elif dialect == "postgresql":
            url = _pg_url()
            if not url:
                pytest.skip("SIDA_TEST_PG_URL 未设置（须指向隔离库）：跳过真实 PG 验证")
            eng = create_engine(url)
        else:  # pragma: no cover
            raise AssertionError(dialect)
        made.append(eng)
        return eng

    try:
        yield _get
    finally:
        for eng in made:
            eng.dispose()


def _reset_actions_table(engine, *, with_column: bool = True) -> None:
    """造一张"存量" copilot_actions（默认已有 annotation_id 列、还没有唯一索引）。"""
    pg = engine.dialect.name == "postgresql"
    drop = " CASCADE" if pg else ""
    id_col = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY"
    col = ", annotation_id INTEGER" if with_column else ""
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS copilot_actions{drop}"))
        conn.execute(text(f"DROP TABLE IF EXISTS schema_migrations{drop}"))
        conn.execute(text(
            f"CREATE TABLE copilot_actions (id {id_col}, user_id VARCHAR(36) NOT NULL,"
            " kind VARCHAR(24) NOT NULL, payload TEXT NOT NULL, status VARCHAR(16) NOT NULL"
            f"{col})"
        ))


def _columns(conn, table: str) -> list[str]:
    from sqlalchemy import inspect as sa_inspect

    return [c["name"] for c in sa_inspect(conn).get_columns(table)]


def _indexes(conn, table: str) -> list[tuple[str, bool]]:
    from sqlalchemy import inspect as sa_inspect

    return [(i["name"], bool(i.get("unique"))) for i in sa_inspect(conn).get_indexes(table)]


def _insert_actions(engine, links: list) -> None:
    with engine.begin() as conn:
        for v in links:
            conn.execute(
                text("INSERT INTO copilot_actions (user_id, kind, payload, status, annotation_id)"
                     " VALUES ('u1', 'annotation', '{}', 'applied', :v)"),
                {"v": v},
            )


def _run_m231_only(engine, monkeypatch) -> None:
    import src.web.migrations as mig

    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        (mig.Migration(231, "copilot_action_annotation_link",
                       mig._m231_copilot_action_annotation_link),),
    )
    mig.run_versioned_migrations(engine)


def _migration_record(engine, version: int = 231):
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT success, error FROM schema_migrations WHERE version = :v"),
            {"v": version},
        ).first()


def _link_index(engine) -> list[tuple[str, bool]]:
    with engine.connect() as conn:
        return [i for i in _indexes(conn, "copilot_actions")
                if i[0] == "uq_copilot_actions_annotation"]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_m231_duplicate_links_fail_loudly_and_atomically(dialect, mig_engine, monkeypatch):
    """存量库里已有重复 annotation_id → 迁移必须失败并说清原因，不许记成功。"""
    engine = mig_engine(dialect)
    _reset_actions_table(engine)
    _insert_actions(engine, [7, 7])  # 两条动作认领同一条标注（历史脏数据）

    with pytest.raises(Exception) as ei:  # noqa: B017 - 方言异常类型不同, 只要"失败"
        _run_m231_only(engine, monkeypatch)

    assert "annotation_id" in str(ei.value), "报错必须点出是哪一列/什么事"

    rec = _migration_record(engine)
    assert rec is not None, "失败也要留痕"
    assert int(rec[0]) == 0, "建索引失败绝不能被记成 success=1"
    assert "aborted" not in str(rec[1] or "").lower(), (
        "PG 事务中毒的废话（current transaction is aborted）不能顶替真实原因"
    )
    assert "annotation_id" in str(rec[1] or "")
    assert _link_index(engine) == [], "失败就不该留下（非唯一的）索引"

    # 原事务已回滚、连接可继续用（PG 事务中毒的反面）
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1

    # 运维修掉重复后重跑 → success=1 且索引真的是唯一的
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE copilot_actions SET annotation_id = NULL"
            " WHERE id = (SELECT MAX(id) FROM copilot_actions)"
        ))
    _run_m231_only(engine, monkeypatch)

    rec = _migration_record(engine)
    assert int(rec[0]) == 1 and str(rec[1] or "") == ""
    assert _link_index(engine) == [("uq_copilot_actions_annotation", True)]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_m231_rejects_same_name_non_unique_index(dialect, mig_engine, monkeypatch):
    """同名但非唯一的索引已存在时，`IF NOT EXISTS` 会静默跳过 —— 必须自己证明唯一性。"""
    engine = mig_engine(dialect)
    _reset_actions_table(engine)
    _insert_actions(engine, [None, None])
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX uq_copilot_actions_annotation ON copilot_actions (annotation_id)"
        ))

    with pytest.raises(Exception) as ei:  # noqa: B017
        _run_m231_only(engine, monkeypatch)

    assert "唯一" in str(ei.value)
    rec = _migration_record(engine)
    assert int(rec[0]) == 0, "没有唯一约束就不算迁移成功"


# ── 7. 写入端：market 大小写 / 周期 1M / 位置与时间 / ts 长度 ────────────────


def test_lowercase_market_is_normalized_to_the_chart_code(db):
    """`market="cn"` 落库后按页面参数 CN 永远查不到（PG 大小写敏感）—— 写入端必须归一。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "小写市场", "price": 35.2, "market": "cn"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["annotation"]["market"] == "CN"
    assert [r["id"] for r in _read(db)] == [out["annotation_id"]]


def test_market_is_trimmed_and_unknown_code_is_rejected(db):
    ok = _propose(db, {"symbol": SYMBOL, "label": "带空格", "price": 35.2, "market": " sh "})
    out = A.apply_action(db, user_id=UID, action_id=ok["id"])
    assert out["annotation"]["market"] == "SH"

    bad = _propose(db, {"symbol": SYMBOL, "label": "银河系", "price": 35.2, "market": "银河系"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=bad["id"])
    assert "市场" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1


def test_uppercase_month_period_is_monthly_not_a_minute(db):
    """`1M` 是月线（国内惯例），旧实现 lower() 后变 `1m` = 1 分钟 —— 直接落错周期。"""
    for raw, want in (("1M", "1mth"), ("M", "1mth"), ("month", "1mth"),
                      ("MONTH", "1mth"), ("mn", "1mth"), ("1m", "1m"), ("1min", "1m")):
        item = _propose(db, {"symbol": SYMBOL, "label": f"周期-{raw}",
                             "price": 35.2, "period": raw})
        out = A.apply_action(db, user_id=UID, action_id=item["id"])
        assert out["annotation"]["period"] == want, f"{raw} 应归一为 {want}"


def test_annotation_without_price_and_ts_has_no_position_and_is_rejected(db):
    """价位和时间都缺 → 图上放不了这条线，不许"确认成功但什么都没有"。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "无位置"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])

    assert "价位" in str(ei.value) or "时间" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "pending", "拒绝后必须回滚，动作仍可修正后确认"


def test_overlong_ts_is_a_clear_400_and_rolls_back(db):
    """`ts` 列是 VARCHAR(32)：超长在 PG 是 DataError(500)，必须先 400 且不留半截。"""
    from fastapi import HTTPException

    from src.web.api.copilot import apply_copilot_action

    long_ts = "2026-10-28 10:00:00." + "1" * 30
    item = _propose(db, {"symbol": SYMBOL, "label": "长 ts", "ts": long_ts})
    with pytest.raises(HTTPException) as ei:
        apply_copilot_action(action_id=item["id"], db=db, user=_U())
    assert ei.value.status_code == 400
    assert "时间" in str(ei.value.detail)

    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "pending" and action.annotation_id is None


def test_unparseable_ts_is_rejected(db):
    item = _propose(db, {"symbol": SYMBOL, "label": "下周一", "ts": "下周一"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=item["id"])
    assert "时间" in str(ei.value)


# ── 8. 弱引用悬空：删掉标注后不许让之后的确认永远失败 ─────────────────────────


def test_deleting_an_annotation_does_not_brick_the_next_confirmation(db):
    """SQLite 的 rowid 会复用：删掉最高 id 的标注行后，下一条新标注拿到同一个 id。

    旧实现下旧动作还挂着这个 id（弱引用悬空），唯一索引让这次确认 —— 以及**之后
    每一次** —— 都撞 `UNIQUE constraint failed: copilot_actions.annotation_id`。
    """
    first_item = _propose(db, {"symbol": SYMBOL, "label": "第一条", "price": 11.0})
    first = A.apply_action(db, user_id=UID, action_id=first_item["id"])
    ann_id = first["annotation_id"]

    # 用户在图层面板删掉这条线（服务端允许删 / 清理任务也会带走）
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": ann_id})
    db.commit()

    second_item = _propose(db, {"symbol": SYMBOL, "label": "复用同号", "price": 12.0})
    second = A.apply_action(db, user_id=UID, action_id=second_item["id"])

    assert second["annotation_id"] == ann_id, "SQLite 复用了同一个 id（正是撞索引的前提）"
    stale = db.query(CopilotAction).filter(CopilotAction.id == first_item["id"]).one()
    assert stale.annotation_id is None, "指向已删标注的链接必须清成 NULL，不许静默指错行"
    assert stale.status == "applied", "清理链接不能改写历史状态"


def test_dangling_link_of_another_user_does_not_block(db):
    """悬空链接若是别人留下的，也会撞同一个全局唯一索引 —— 一样必须清掉。"""
    other_item = _propose(db, {"symbol": SYMBOL, "label": "他人线", "price": 21.0},
                          user_id=OTHER_UID)
    other = A.apply_action(db, user_id=OTHER_UID, action_id=other_item["id"])
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": other["annotation_id"]})
    db.commit()

    mine_item = _propose(db, {"symbol": SYMBOL, "label": "我的线", "price": 22.0})
    mine = A.apply_action(db, user_id=UID, action_id=mine_item["id"])
    assert mine["annotation_id"] == other["annotation_id"]

    theirs = db.query(CopilotAction).filter(CopilotAction.id == other_item["id"]).one()
    assert theirs.annotation_id is None
    assert theirs.status == "applied"


# ── 9. 不可回滚要如实说（can_rollback 与文案）───────────────────────────────


def test_annotation_action_reports_itself_as_not_rollbackable(db):
    """标注确认后没有快照可退：`can_rollback` 必须是 false，回滚文案不能拿"早于 v0.73"糊弄。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "不可回滚", "price": 35.0})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])

    assert out["can_rollback"] is False, "annotation 不在可回滚集合里（_snapshot 对它返回空）"

    with pytest.raises(A.ActionError) as ei:
        A.rollback_action(db, user_id=UID, action_id=item["id"])
    msg = str(ei.value)
    assert "不支持回滚" in msg
    assert "快照" not in msg and "v0.73" not in msg

    # 回滚没生效：线还在图上，动作仍是 applied
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 1
    action = db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "applied" and action.annotation_id == out["annotation_id"]


# ── 10. 真实 PG：同一批契约必须在 varchar(36)/varchar(32) 下成立 ──────────────


@pytest.fixture()
def pg_db():
    """隔离 PG 库上的会话（须由 SIDA_TEST_PG_URL 指向专用库，本 fixture 会 drop 表）。"""
    url = _pg_url()
    if not url:
        pytest.skip("SIDA_TEST_PG_URL 未设置（须指向隔离库）：跳过真实 PG 契约验证")
    engine = create_engine(url)
    drop = " CASCADE" if engine.dialect.name == "postgresql" else ""
    with engine.begin() as conn:
        for t in ("copilot_actions", "kline_annotations", "schema_migrations"):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}{drop}"))
    CopilotAction.__table__.create(engine, checkfirst=True)
    KlineAnnotation.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_pg_confirm_links_action_and_read_echoes_target(pg_db):
    item = A.propose(pg_db, user_id=UID, kind="annotation",
                     payload={"symbol": SYMBOL, "label": "PG 确认", "price": 35.23, "market": "cn"})
    out = A.apply_action(pg_db, user_id=UID, action_id=item["id"])

    assert out["annotation_id"] and out["annotation"]["market"] == "CN"
    row = pg_db.query(KlineAnnotation).filter(KlineAnnotation.id == out["annotation_id"]).one()
    assert row.user_id == UID and len(row.user_id) <= 36
    action = pg_db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.annotation_id == row.id

    class _P:
        id = UID

    from src.web.api.copilot import get_copilot_annotations

    rows = get_copilot_annotations(symbol=SYMBOL, market="CN", period="1d",
                                   db=pg_db, user=_P())
    assert [r["id"] for r in rows] == [out["annotation_id"]]
    assert rows[0]["action_id"] == item["id"] and rows[0]["user_id"] == UID


def test_pg_overlong_ts_is_a_400_not_a_data_error(pg_db):
    from fastapi import HTTPException

    from src.web.api.copilot import apply_copilot_action

    class _P:
        id = UID

    item = A.propose(pg_db, user_id=UID, kind="annotation",
                     payload={"symbol": SYMBOL, "label": "长 ts",
                              "ts": "2026-10-28 10:00:00." + "1" * 30})
    with pytest.raises(HTTPException) as ei:
        apply_copilot_action(action_id=item["id"], db=pg_db, user=_P())
    assert ei.value.status_code == 400
    assert pg_db.query(KlineAnnotation).count() == 0
    action = pg_db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one()
    assert action.status == "pending"


def test_pg_unique_link_is_enforced_by_the_index(pg_db):
    a1 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending", annotation_id=123)
    a2 = CopilotAction(user_id=UID, kind="annotation", payload="{}", status="pending", annotation_id=123)
    pg_db.add_all([a1, a2])
    with pytest.raises(Exception) as ei:  # noqa: B017 - psycopg2 IntegrityError
        pg_db.commit()
    assert "uq_copilot_actions_annotation" in str(ei.value) or "unique" in str(ei.value).lower()
    pg_db.rollback()


def test_pg_month_period_and_no_position_rules(pg_db):
    monthly = A.propose(pg_db, user_id=UID, kind="annotation",
                        payload={"symbol": SYMBOL, "label": "月线", "price": 35.2, "period": "1M"})
    out = A.apply_action(pg_db, user_id=UID, action_id=monthly["id"])
    assert out["annotation"]["period"] == "1mth"

    empty = A.propose(pg_db, user_id=UID, kind="annotation",
                      payload={"symbol": SYMBOL, "label": "无位置"})
    with pytest.raises(A.ActionError):
        A.apply_action(pg_db, user_id=UID, action_id=empty["id"])
    assert pg_db.query(KlineAnnotation).count() == 1


def test_pg_annotation_cannot_rollback(pg_db):
    item = A.propose(pg_db, user_id=UID, kind="annotation",
                     payload={"symbol": SYMBOL, "label": "PG 不可回滚", "price": 30.0})
    out = A.apply_action(pg_db, user_id=UID, action_id=item["id"])
    assert out["can_rollback"] is False
    with pytest.raises(A.ActionError) as ei:
        A.rollback_action(pg_db, user_id=UID, action_id=item["id"])
    assert "不支持回滚" in str(ei.value)


def test_pg_dangling_link_is_pruned_before_the_next_confirmation(pg_db):
    """PG 不复用序列，但悬空链接本身就是错数据：下一次确认前必须清成 NULL。"""
    first_item = A.propose(pg_db, user_id=UID, kind="annotation",
                           payload={"symbol": SYMBOL, "label": "先删掉的线", "price": 41.0})
    first = A.apply_action(pg_db, user_id=UID, action_id=first_item["id"])
    pg_db.execute(text("DELETE FROM kline_annotations WHERE id = :i"),
                  {"i": first["annotation_id"]})
    pg_db.commit()

    second_item = A.propose(pg_db, user_id=UID, kind="annotation",
                            payload={"symbol": SYMBOL, "label": "新线", "price": 42.0})
    second = A.apply_action(pg_db, user_id=UID, action_id=second_item["id"])

    assert second["annotation_id"] != first["annotation_id"], "PG 序列不复用 id"
    stale = pg_db.query(CopilotAction).filter(CopilotAction.id == first_item["id"]).one()
    assert stale.annotation_id is None and stale.status == "applied"
