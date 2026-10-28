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

2026-10-28 安全复审驳回（第二轮整改，§11-§13）：
  - 市场从"字符串相等"改为**可证明的同一标的**（`provable_markets`）：SH/SZ/BJ 提议必须能从
    `?market=CN` 的页面读到，但指数/个股同号（000001）不得互证、矛盾市场写入即拒；
    整链用例走 真实 propose 工具 → HTTP 确认 → HTTP 读取；
    市场不符的播报不许冒充成"周期不符"；
  - ts-only：确认了也画不出来（图上没有时间图层）⇒ 明确 400 拒绝，不落死数据；
  - 唯一索引冲突的诊断分支 SQLite/PG 都认（SQLite 原文不含索引名，旧判断必然 500），
    并用原始 HTTP 断言 400 与可执行文案；
  - 悬空链接不再全局跨用户 UPDATE 清理：PG 序列不复用 ⇒ 不读不写（真冲突默认拒绝并报告）；
    只有会复用主键的方言上、只在真撞号时、只清**本人**可证明悬空的行。

真实 PG 验证（CI 暂无 PG service，**默认 skip**，本地/隔离库手动跑）::

    SIDA_TEST_PG_URL="postgresql+psycopg2://user:pw@127.0.0.1:5432/<隔离库>" \\
      python -m pytest tests/test_copilot_annotation_contract.py -q

  该库会被本文件 drop/create `copilot_actions` / `kline_annotations` /
  `schema_migrations` 三张表 —— 必须指向**隔离库**，绝不能指生产库。
"""

from __future__ import annotations

import json
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


def test_read_non_strict_keeps_legacy_history_for_ambiguous_000xxx(db):
    """非严格读取 = 既有行为，不许因为 000xxx 收口被打断（历史标注回看/报表/诊断调用方）。

    000001 同时是上证指数(SH)与平安银行(SZ)：严格模式证明不了标的类型 ⇒ 画图链路不给行。
    但 README 之外的既有调用方（不带 market_strict/mine_only/action_linked/ids）走的是
    「A 股族归并 + 本人或全局」，历史上就是这么读的 —— 这次收口只能拦画图，不能把
    通用读取一并变成空数组，否则库里已有的 000xxx 标注直接读不出来。
    """
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SH", period="1d",
                           price=3200.0, label="指数线", source="ai"))
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                           price=12.5, label="个股线", source="ai"))
    # 历史行：没有确认动作（action_id 必须回显 null，但行本身要读得出来）
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="CN", period="1d",
                           price=1.0, label="族语义线", source="ai"))
    # 全局行（user_id IS NULL）在非严格模式下同样可见 —— 与 000001 之前的语义一致。
    # 用 1w 周期 + 集合断言：全局行 fixture 不清（没有属主），不许污染/依赖同库其它用例。
    db.add(KlineAnnotation(user_id=None, symbol="000002", market="SZ", period="1w",
                           price=9.9, label="全局行", source="manual"))
    db.commit()

    def labels(**kw) -> list[str]:
        return sorted(r["label"] for r in _read(db, **kw))

    assert labels(symbol="000001", market="CN") == ["个股线", "指数线", "族语义线"], (
        "非严格 CN 读取必须保持族归并（既有调用方语义），不得因为 000xxx 变成空数组"
    )
    assert labels(symbol="000001", market="SH") == ["个股线", "指数线", "族语义线"]
    assert labels(symbol="000001", market="SZ") == ["个股线", "指数线", "族语义线"]
    assert labels(symbol="000001", market="BJ") == ["个股线", "指数线", "族语义线"]

    # 回显字段照旧：属主 + 标的 + 无动作行 action_id 为 null（历史行不编造 action）
    rows = {r["label"]: r for r in _read(db, symbol="000001", market="CN")}
    assert rows["指数线"]["user_id"] == UID and rows["指数线"]["action_id"] is None
    assert rows["指数线"]["symbol"] == "000001" and rows["指数线"]["period"] == "1d"

    assert set(labels(symbol="000002", market="CN", period="1w")) == {"全局行"}, (
        "非严格读取仍含全局行"
    )
    # 非 A 股市场不受族归并影响（HK 只认自己）
    assert _read(db, symbol="000001", market="HK") == []


def test_read_strict_and_chart_modes_refuse_ambiguous_000xxx(db):
    """严格/画图读取：000xxx 一律不给行 —— 前端那三条参数就是画图链路。

    `market_strict` / `mine_only` / `action_linked` 任一为真（前端画图固定三条全带），
    或带 `ids` 精确读取，都属于「这条行证明得了标的类型我才画」的请求：标注行没有
    instrument 列，000xxx 整段证明不了指数还是个股 ⇒ 空数组（不是给一条错的线）。
    """
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SH", period="1d",
                           price=3200.0, label="指数线", source="ai"))
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                           price=12.5, label="个股线", source="ai"))
    db.add(KlineAnnotation(user_id=UID, symbol="000002", market="SZ", period="1d",
                           price=9.9, label="万科线", source="ai"))
    db.commit()
    ambiguous_id = db.query(KlineAnnotation).filter(
        KlineAnnotation.label == "个股线", KlineAnnotation.user_id == UID
    ).one().id

    for sym in ("000001", "000002"):
        for market in ("CN", "SH", "SZ", "BJ"):
            for kw in (
                {"market_strict": True},
                {"mine_only": True},
                {"action_linked": True},
                {"market_strict": True, "mine_only": True, "action_linked": True},
                {"ids": str(ambiguous_id)},
            ):
                assert _read(db, symbol=sym, market=market, **kw) == [], (
                    f"{sym}/{market} 严格读取 {kw} 不许给行（标的类型不可证）"
                )

    # 对照：可证代码在同样严格参数下照旧读得到（安全边界不许外溢）
    db.add(KlineAnnotation(user_id=UID, symbol=SYMBOL, market="SH", period="1d",
                           price=35.2, label="沪市线", source="ai"))
    db.commit()
    assert [r["label"] for r in _read(db, symbol=SYMBOL, market="CN", market_strict=True)] == ["沪市线"]
    assert [r["label"] for r in _read(db, symbol=SYMBOL, market="SH")] == ["沪市线"]


def test_read_ambiguous_000xxx_never_leaks_across_users(db):
    """不同用户隔离：000xxx 收口不以放宽属主过滤为代价（严格与非严格都不许看到他人行）。

    非严格读取是「本人 + 全局」，严格读取是「本人」；两条路径下他人的 000001 标注
    都必须看不见 —— 否则「恢复非严格历史读取」就成了越权通道。
    """
    db.add(KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                           price=12.5, label="我的", source="ai"))
    db.add(KlineAnnotation(user_id=OTHER_UID, symbol="000001", market="SZ", period="1d",
                           price=12.6, label="他人的", source="ai"))
    db.add(KlineAnnotation(user_id=OTHER_UID, symbol="000002", market="SZ", period="1d",
                           price=9.9, label="他人000002", source="ai"))
    db.commit()

    def labels(user_id: str, **kw) -> list[str]:
        from src.web.api.copilot import get_copilot_annotations

        params = {"symbol": "000001", "market": "CN", "period": "1d", "db": db,
                  "user": _U(user_id)}
        params.update(kw)
        return sorted(r["label"] for r in get_copilot_annotations(**params))

    assert labels(UID) == ["我的"], "非严格读取只给本人 + 全局，绝不给同标的的他人行"
    assert labels(OTHER_UID) == ["他人的"]
    assert labels(UID, mine_only=True) == [], "严格读取下 000xxx 一律不给行"
    assert labels(OTHER_UID, mine_only=True) == []
    # 精确 id 也不行：他人的行即使 id 点名也读不到
    from src.web.api.copilot import get_copilot_annotations

    theirs = db.query(KlineAnnotation).filter(KlineAnnotation.label == "他人的").one().id
    assert get_copilot_annotations(symbol="000001", market="CN", period="1d",
                                   ids=str(theirs), db=db, user=_U()) == []


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

    bad = _propose(db, {"symbol": SYMBOL, "label": "银河系", "price": 35.2, "market": "银河系",
                        "instrument": "stock"})
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

    2026-10-28 复审驳回 ④ 之后：只清**本人**可证明悬空的链接（不跨用户改写历史）。
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


def test_dangling_link_of_another_user_is_reported_not_rewritten(db):
    """别人的悬空链接占住下一条标注号：**默认拒绝 + 报告**，一行历史都不许改。

    复审驳回 ④：旧实现为了让自己能写进去，把全库所有悬空链接（含他人的）UPDATE 成 NULL
    —— 那是替别人改写审计记录，也是全表写放大。现在：只读定位，明确拒绝并说清是谁占的。
    """
    other_item = _propose(db, {"symbol": SYMBOL, "label": "他人线", "price": 21.0},
                          user_id=OTHER_UID)
    other = A.apply_action(db, user_id=OTHER_UID, action_id=other_item["id"])
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": other["annotation_id"]})
    db.commit()

    mine_item = _propose(db, {"symbol": SYMBOL, "label": "我的线", "price": 22.0})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=mine_item["id"])
    msg = str(ei.value)
    assert f"#{other_item['id']}" in msg, "报告必须点名占住编号的那条动作"
    assert "不跨账号" in msg, "要说清为什么不修：不跨账号改写历史关联"

    # 他人的历史关联原样保留（悬空也不替别人改）
    theirs = db.query(CopilotAction).filter(CopilotAction.id == other_item["id"]).one()
    assert theirs.annotation_id == other["annotation_id"] and theirs.status == "applied"
    # 我这次没落库，动作仍可重试
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(CopilotAction).filter(CopilotAction.id == mine_item["id"]).one().status == "pending"


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
    # 复审阻断 ④：不许指一条不存在的路（图上没有删除入口，服务端也没有删除接口）
    assert "在图上删除" not in msg and "图上删除" not in msg
    assert "删除入口" in msg and "无法撤下" in msg

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
    msg = str(ei.value)
    assert "不支持回滚" in msg
    # 复审阻断 ④：文案不许再指"图上删除"这条不存在的路
    assert "在图上删除" not in msg
    assert "删除入口" in msg and "无法撤下" in msg


def test_pg_ambiguous_target_is_refused_before_any_pending_row(pg_db):
    """真实 PG：000xxx 在提议阶段就拒绝，库里连 pending 行都不出现（复审阻断 ②）。"""
    for sym in ("000001", "000002", "399001"):
        with pytest.raises(A.ActionError) as ei:
            A.propose(pg_db, user_id=UID, kind="annotation",
                      payload={"symbol": sym, "label": f"{sym} 同号", "price": 12.3, "market": "SZ"})
        assert sym in str(ei.value) and "指数" in str(ei.value)
    assert pg_db.query(CopilotAction).count() == 0, "提议被拒绝 ⇒ 一条 pending 都不许落库"

    ok = A.propose(pg_db, user_id=UID, kind="annotation",
                   payload={"symbol": SYMBOL, "label": "可证个股", "price": 35.2, "market": "SH"})
    assert ok["status"] == "pending", "可证个股照旧能提议（闸门不误伤）"


def test_pg_dangling_link_is_left_alone_and_does_not_block(pg_db):
    """PG 语义（生产）：序列不复用 ⇒ 悬空链接不可能撞新 id，因此**一行历史都不改**。

    复审驳回 ④：旧实现在每次确认前做一次全库 UPDATE 清理，跨用户改写审计关联。
    现在 PG 上既不读也不写（`_guard_annotation_id` 直接返回），新确认照常成功，
    别人的悬空链接原样留给他自己/运维。
    """
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
    assert stale.annotation_id == first["annotation_id"], "PG 上不清理历史关联（不跨用户/不猜测修复）"
    assert stale.status == "applied"


# ── 11. 市场映射：SH/SZ/BJ 提议必须能从 CN 页读到，但指数/个股同号不得误画 ────────
#
# 复审驳回 ①：`propose_action(market="SH")` 落库 SH，而 K 线页只有 `?market=CN`，
# 严格等值读取永远为空 —— "确认成功、图上没有线"。反过来简单做 A 股族归并，
# 000001 的上证指数线就会画到 000001（平安银行）的个股图上。
# 现在两侧共用一条**可证明**规则：由代码推导交易所 → 只与能证实的市场互证。


def _http_for(session, uid: str = UID):
    """真实 HTTP 行为：只挂 copilot 路由 + 覆盖鉴权/会话依赖（不经全局中间件）。

    测的是「路由 → 异常映射 → 状态码/JSON」这条真链路，而不是直接调函数。
    `uid` 可换身份：越权/隔离用例必须真的换一个登录态，不能只换 payload。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import copilot as copilot_api
    from src.web.api.auth import get_current_user
    from src.web.database import get_db

    app = FastAPI()
    app.include_router(copilot_api.router)

    def _db():
        yield session

    async def _user():
        class _Who:
            id = uid

        return _Who()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def http(db):
    return _http_for(db)


_STRICT = {"market_strict": "1", "mine_only": "1", "action_linked": "1"}


def test_full_chain_market_proposal_sh_reads_back_on_cn_page(db, http):
    """整链：chat 工具 propose_action(market=SH) → HTTP 确认 → HTTP 读取（页面参数 CN）。"""
    import asyncio

    from src.agents.chat.registry import _tool_propose_action

    class _Who:
        id = UID

    msg = asyncio.run(_tool_propose_action(
        db,
        {"kind": "annotation", "symbol": SYMBOL, "label": "支撑位", "price": 35.5, "market": "SH"},
        _Who(),
    ))
    assert "提议" in msg, f"propose_action 必须真的落 pending：{msg}"

    pending = [a for a in A.list_pending(db, UID) if a["payload"].get("label") == "支撑位"]
    assert len(pending) == 1
    action_id = pending[0]["id"]

    applied = http.post(f"/copilot/actions/{action_id}/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["annotation"]["market"] == "SH", "落库市场就是提议里的交易所，不被偷偷改写"

    resp = http.get("/copilot/annotations", params={
        "symbol": SYMBOL, "market": "CN", "period": "1d", **_STRICT,
    })
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [body["annotation_id"]], "CN 页必须读到 SH 行（可证明同一标的）"
    assert rows[0]["market"] == "SH" and rows[0]["action_id"] == action_id
    assert rows[0]["user_id"] == UID


def test_sz_and_bj_proposals_are_readable_from_the_cn_page(db, http):
    """深市 / 北交所代码同样必须能从 CN 页读到（三个交易所都是同一套页面参数）。"""
    for symbol, market in (("002636", "SZ"), ("830799", "BJ")):
        item = _propose(db, {"symbol": symbol, "label": f"{market} 标注", "price": 12.3,
                             "market": market})
        applied = http.post(f"/copilot/actions/{item['id']}/apply")
        assert applied.status_code == 200, applied.text
        rows = http.get("/copilot/annotations", params={
            "symbol": symbol, "market": "CN", "period": "1d", **_STRICT,
        }).json()
        assert [r["id"] for r in rows] == [applied.json()["annotation_id"]], f"{market} 行读不到"


def test_provable_markets_never_cross_index_and_stock_with_the_same_code(db):
    """000001 = 上证指数(CN) / 平安银行(SZ)：类型不同 ⇒ 市场集合不同，永不互证。"""
    assert A.exchange_of("600519") == "SH"
    assert A.exchange_of("002636") == "SZ"
    assert A.exchange_of("830799") == "BJ"
    assert A.exchange_of("399001", A.INSTRUMENT_STOCK) is None, "指数代码不推导交易所"
    assert A.exchange_of("000001", A.INSTRUMENT_INDEX) is None, "指数不做交易所推导"
    assert A.exchange_of("000001", A.INSTRUMENT_STOCK) == "SZ", "000001 个股段是深市（平安银行）"
    assert A.exchange_of("ABCDEF") is None

    assert A.provable_markets("600519", "CN") == ["CN", "SH"]
    assert A.provable_markets("600519", "SH") == ["SH", "CN"]
    assert A.provable_markets("600519", "SZ") == ["SZ"], "矛盾的市场不互证（写入端已拒绝）"
    # 同号代码：类型决定口径 —— 个股可证 SZ，指数只认 CN，两者交集**只有 CN 这一个视图口径**
    # （视图市场不是身份：真正的隔离靠 instrument 列，见 §14）
    assert A.provable_markets("000001", "CN", A.INSTRUMENT_STOCK) == ["CN", "SZ"]
    assert A.provable_markets("000001", "CN", A.INSTRUMENT_INDEX) == ["CN"]
    assert A.provable_markets("000001", "SZ", A.INSTRUMENT_STOCK) == ["SZ", "CN"]
    assert A.provable_markets("00700", "HK") == ["HK"], "非 A 股市场只认自己"


def test_same_code_index_row_is_not_read_back_on_the_stock_page(db):
    """读端闸门（严格/画图读取）：000001 的行（SH 指数 / SZ 个股）在任何 market 下都不给。

    非严格通用读取仍按既有语义返回（见 `test_read_non_strict_keeps_legacy_history_for_ambiguous_000xxx`）：
    那条路径不画图，前端 `verifyRow` 也会拒画。
    """
    db.add_all([
        KlineAnnotation(user_id=UID, symbol="000001", market="SH", period="1d",
                        price=3200.0, label="指数线", source="ai"),
        KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                        price=12.5, label="个股线", source="ai"),
    ])
    db.commit()

    def labels(**kw):
        rows = http_read(db, symbol="000001", market_strict=True, **kw)
        return [r["label"] for r in rows]

    assert labels(market="SZ") == [], "即便指名 SZ，000001 也无法证明是个股（默认拒画）"
    assert labels(market="SH") == [], "即便指名 SH，000001 也无法证明是指数（默认拒画）"
    assert labels(market="CN") == [], "CN 页无法证明是股票还是指数 ⇒ 一条都不给（默认拒画）"


def http_read(db, **kw):
    """直接调读端（与 HTTP 查询参数同语义，省一层序列化）。"""
    from src.web.api.copilot import get_copilot_annotations

    params = {"symbol": SYMBOL, "market": "CN", "period": "1d", "db": db, "user": _U()}
    params.update(kw)
    return get_copilot_annotations(**params)


# ── 11b. 000002/000003 …：整段不可证，不是"名单里没有就放行" ────────────────────
#
# 000001 之外，000002（A股指数 / 万科A）、000003（B股指数）等同样是指数与个股撞号。
# 旧实现只列了"已知撞号"的 7 个代码，名单外的就被当成深市个股推导成 SZ
# → 上证指数系列的线可以画到个股图上。服务端标注行没有 instrument 列，
# 也没有任何字段能证明标的类型，所以整段 000xxx 默认不可证：不推导、不互证、
# 严格/画图读取不给行、不落库；**非严格通用读取保持既有行为**（历史标注不许读不出来）。

_AMBIGUOUS_SAMPLES = (
    "000001",  # 上证指数 / 平安银行
    "000002",  # A股指数 / 万科A
    "000003",  # B股指数
    "000004", "000005", "000009", "000010", "000016",
    "000300", "000688", "000852", "000905", "000985",
    "000999", "000777",
)


def test_every_000_xxx_code_is_unprovable_not_just_a_curated_list():
    """000xxx 整段**仅凭代码**不可证 —— 但类型是显式字段，不是"整段禁用"。

    可证的只有一件：这一段同时是深市个股段（000858=五粮液、000001=平安银行），
    声明 stock 合法；声明 index 则要求代码出现在服务端指数表里（000002/000003 不在表内 ⇒ 拒）。
    """
    from src.core.index_codes import INDEX_CODES

    for sym in _AMBIGUOUS_SAMPLES:
        assert A.instrument_ambiguous(sym) is True, sym
        assert A.provable_instrument(sym) is None, f"{sym} 不许仅凭代码推导类型"
        assert A.exchange_of(sym, A.INSTRUMENT_INDEX) is None, f"{sym} 指数不推导交易所"
        assert A.stock_exchange_of(sym) == "SZ", f"{sym} 的个股段是深市"
        # 声明 stock：合法（深市个股段）
        assert A.resolve_instrument(sym, "CN", A.INSTRUMENT_STOCK) == A.INSTRUMENT_STOCK
        # 声明 index：只有指数表里那几个才合法（000001/000016/000300/000688/000852/000905/000985…）
        if sym in INDEX_CODES:
            assert A.resolve_instrument(sym, "CN", A.INSTRUMENT_INDEX) == A.INSTRUMENT_INDEX, sym
        else:
            with pytest.raises(A.ActionError):
                A.resolve_instrument(sym, "CN", A.INSTRUMENT_INDEX)
    # 带后缀不许：行里只存代码字符串，后缀证明不了当初指的是哪一类标的
    assert A.instrument_ambiguous("000002.SZ") is True
    assert A.provable_instrument("000002.SZ") is None


def test_ambiguity_rule_does_not_overreach_into_provable_stock_prefixes():
    """边界：只有 000xxx 与纯指数前缀不可证，其余可证个股前缀一个都不能误伤。"""
    cases = {
        "600519": "SH", "601318": "SH", "688981": "SH", "900901": "SH",
        "002636": "SZ", "001979": "SZ", "300750": "SZ", "301269": "SZ",
        "830799": "BJ", "430047": "BJ", "871981": "BJ",
    }
    for sym, exchange in cases.items():
        assert A.instrument_ambiguous(sym) is False, sym
        assert A.exchange_of(sym) == exchange, sym
        assert set(A.provable_markets(sym, "CN")) == {"CN", exchange}
    for sym in ("399001", "399006", "880301", "881101", "999999"):
        assert A.instrument_ambiguous(sym) is True, sym
        assert A.exchange_of(sym) is None, sym
    # 形状不合法的不算"歧义"（只是不可证），不许把空串/短码误判成指数段
    for sym in ("", "0002", "0000022", "ABC", "sh600519", "39900"):
        assert A.instrument_ambiguous(sym) is False, sym
        assert A.exchange_of(sym) is None, sym


def _legacy_pending(db, payload: dict, user_id: str = UID) -> CopilotAction:
    """**历史 pending**：迁移前/别的写入方落下的待确认动作（提议端现在已拦，直接造行）。"""
    row = CopilotAction(
        user_id=user_id, kind="annotation",
        payload=json.dumps(payload, ensure_ascii=False), status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_ambiguous_target_without_instrument_is_refused_at_propose(db):
    """000xxx **不声明类型**时在提议阶段就拒绝 —— 不生成"点确认必然 400"的 pending。

    与旧实现的区别：旧实现是"整段拒绝"（连 000858 都画不了）；现在拒绝的唯一理由是
    "没给类型"，给了合法类型就正常提议、正常确认（见 §14）。
    """
    for sym in ("000001", "000002", "000003", "000858"):
        with pytest.raises(A.ActionError) as ei:
            _propose(db, {"symbol": sym, "label": f"{sym} 同号线", "price": 12.3, "market": "CN"})
        msg = str(ei.value)
        assert sym in msg and "instrument" in msg
        assert db.query(CopilotAction).filter(CopilotAction.user_id == UID).count() == 0, (
            "提议被拒绝后不许留下任何 pending（含 action 行）"
        )
    # 纯指数段声明成个股也拒（那几段没有个股）
    with pytest.raises(A.ActionError):
        _propose(db, {"symbol": "399001", "label": "假个股", "price": 12.3,
                      "market": "CN", "instrument": "stock"})
    # 指数表里没有的 000xxx 声明成 index 也拒（不能把 000858 说成指数）
    with pytest.raises(A.ActionError):
        _propose(db, {"symbol": "000858", "label": "假指数", "price": 12.3,
                      "market": "CN", "instrument": "index"})
        assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0

    # 兜底：历史 pending（提议端拦截之前落下的）确认时仍然被拒，且动作回滚回 pending
    legacy = _legacy_pending(db, {"symbol": "000002", "label": "历史同号线", "price": 9.9, "market": "SZ"})
    with pytest.raises(A.ActionError) as ei:
        A.apply_action(db, user_id=UID, action_id=legacy.id)
    assert "000002" in str(ei.value) and "指数" in str(ei.value)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    db.refresh(legacy)
    assert legacy.status == "pending"


def test_scenario_proposals_never_create_pending_for_ambiguous_symbols(db):
    """情景关键位提议同样不落 000xxx 的 pending（同一条闸，不是只拦工具入口）。"""
    matrix = {"available": True, "levels": {"support": 3200.0, "resist": 3400.0}}
    for sym in ("000001", "399001"):
        assert A.propose_scenario_annotations(
            db, user_id=UID, symbol=sym, market="SH", matrix=matrix,
        ) == []
    assert db.query(CopilotAction).filter(CopilotAction.user_id == UID).count() == 0, (
        "情景矩阵对 000xxx 一条 pending 都不许提议"
    )
    # 对照：可证个股照旧提议（别把整条链路一起关掉）
    ok = A.propose_scenario_annotations(
        db, user_id=UID, symbol=SYMBOL, market="SH", matrix=matrix,
    )
    assert {p["payload"]["label"] for p in ok} == {"支撑位", "阻力位"}


def test_propose_tool_reports_the_refusal_instead_of_pending(db):
    """工具层：`propose_action` 直接回绝并说明原因（模型据此换标的，而不是以为已提议）。"""
    from src.agents.chat.registry import _tool_propose_action

    class _U:
        id = UID

    import asyncio

    out = asyncio.run(_tool_propose_action(
        db, {"kind": "annotation", "symbol": "000002", "label": "万科支撑", "price": 9.9}, _U(),
    ))
    assert "动作提议失败" in out and "000002" in out and "指数" in out
    assert "待确认动作" not in out, "被拒绝时不许说已经在待确认列表里生成"
    assert db.query(CopilotAction).filter(CopilotAction.user_id == UID).count() == 0


def test_propose_action_tool_description_states_the_instrument_rule():
    """工具说明同步：模型在**调用前**就知道要给 instrument（不是"000xxx 一律不能用"）。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    spec = CHAT_TOOL_REGISTRY["propose_action"]
    schema = spec.schema
    assert isinstance(schema, dict), "propose_action 必须带 function schema"
    description = schema["function"]["description"]
    assert "instrument" in description and "000xxx" in description
    assert "stock" in description and "index" in description
    # 参数表里必须有这个字段，且取值受约束 —— 否则模型只能靠正文猜
    props = schema["function"]["parameters"]["properties"]
    assert props["instrument"]["enum"] == ["stock", "index"]


def test_http_strict_read_denies_000002_and_000003_but_legacy_read_still_returns(db, http):
    """真实 HTTP：严格参数（画图链路）零行、确认端 400；非严格通用读取照旧给行。

    这两件事必须同时成立 —— 上一版把非严格读取也一起关掉，等于把库里已有的 000xxx
    标注从所有既有调用方（历史回看/报表/诊断）面前删掉。
    """
    db.add_all([
        KlineAnnotation(user_id=UID, symbol="000002", market="SZ", period="1d",
                        price=9.9, label="万科线", source="ai"),
        KlineAnnotation(user_id=UID, symbol="000003", market="SH", period="1d",
                        price=2800.0, label="B股指数线", source="ai"),
    ])
    db.commit()

    for sym in ("000002", "000003"):
        for market in ("CN", "SH", "SZ"):
            # 严格（前端画图会用到的每一种严格参数）：一条都不给。
            # 分别单独传：`action_linked` 本身就会滤掉无确认动作的行，只测三件套会掩盖
            # 「歧义闸门失效」——market_strict/mine_only 单独一条也必须拦得住。
            for strict_kw in ({"market_strict": "1"}, {"mine_only": "1"}, _STRICT):
                resp = http.get("/copilot/annotations", params={
                    "symbol": sym, "market": market, "period": "1d", **strict_kw,
                })
                assert resp.status_code == 200, resp.text
                assert resp.json() == [], f"{sym} 在 {market} 页严格读取 {strict_kw} 不许给行"

            # 非严格（既有通用调用方）：行照旧返回（不许因为收口变成空）
            legacy = http.get("/copilot/annotations", params={
                "symbol": sym, "market": market, "period": "1d",
            })
            assert legacy.status_code == 200, legacy.text
            labels = [r["label"] for r in legacy.json()]
            assert labels == (["万科线"] if sym == "000002" else ["B股指数线"]), (
                f"{sym} 在 {market} 页非严格读取必须保持既有行为：{labels}"
            )

    # 提议端现在就拒绝（复审阻断 ②）；HTTP 确认端对**历史 pending** 仍然是 400
    with pytest.raises(A.ActionError) as pei:
        _propose(db, {"symbol": "000002", "label": "万科支撑", "price": 9.9, "market": "SZ"})
    assert "指数" in str(pei.value)
    legacy = _legacy_pending(db, {"symbol": "000002", "label": "万科支撑", "price": 9.9, "market": "SZ"})
    applied = http.post(f"/copilot/actions/{legacy.id}/apply")
    assert applied.status_code == 400, applied.text
    assert "指数" in applied.json()["detail"]
    assert db.query(KlineAnnotation).filter(
        KlineAnnotation.user_id == UID, KlineAnnotation.label == "万科支撑"
    ).count() == 0


def test_http_ambiguous_read_is_mode_gated_and_user_scoped(db):
    """真实 HTTP：000001 的严格读取零行，但非严格读取按属主隔离（他人的行不许出现）。"""
    db.add_all([
        KlineAnnotation(user_id=UID, symbol="000001", market="SZ", period="1d",
                        price=12.5, label="我的", source="ai"),
        KlineAnnotation(user_id=OTHER_UID, symbol="000001", market="SZ", period="1d",
                        price=12.6, label="他人的", source="ai"),
    ])
    db.commit()

    mine = _http_for(db)
    theirs = _http_for(db, uid=OTHER_UID)

    # 严格参数逐条单测（三件套里的 action_linked 自己就会滤掉无动作行，不能只靠它）
    for market in ("CN", "SZ"):
        for strict_kw in ({"market_strict": "1"}, {"mine_only": "1"}, _STRICT):
            strict = mine.get("/copilot/annotations", params={
                "symbol": "000001", "market": market, "period": "1d", **strict_kw,
            })
            assert strict.status_code == 200, strict.text
            assert strict.json() == [], f"严格读取（{strict_kw} / {market}）一律零行"

    legacy_mine = mine.get("/copilot/annotations", params={
        "symbol": "000001", "market": "CN", "period": "1d",
    })
    assert [r["label"] for r in legacy_mine.json()] == ["我的"], "非严格读取只给本人行"

    legacy_theirs = theirs.get("/copilot/annotations", params={
        "symbol": "000001", "market": "CN", "period": "1d",
    })
    assert [r["label"] for r in legacy_theirs.json()] == ["他人的"], "换登录态读的是他自己的行"


def test_market_contradicting_the_symbol_is_refused_at_write(db):
    """600519 是沪市：提议里写 SZ 的线**不会**出现在任何页面（写入端直接拒绝，不落死数据）。

    2026-10-28 P2：市场一致性并入类型闸（`resolve_instrument`），提议阶段就拒 ——
    不让用户看到一条注定确认失败的动作。确认端同一把闸兜底历史 pending。
    """
    with pytest.raises(A.ActionError) as ei:
        _propose(db, {"symbol": "600519", "label": "错市场", "price": 35.2, "market": "SZ"})
    assert "市场" in str(ei.value) and "SH" in str(ei.value)
    # 历史 pending 走确认端同样拒绝（不落行、不动状态）
    legacy = _legacy_pending(db, {"symbol": "600519", "label": "错市场", "price": 35.2,
                                  "market": "SZ"})
    with pytest.raises(A.ActionError):
        A.apply_action(db, user_id=UID, action_id=legacy.id)
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0

    for market in ("CN", "SH"):
        ok = _propose(db, {"symbol": "600519", "label": f"{market} 行", "price": 35.2,
                           "market": market})
        assert A.apply_action(db, user_id=UID, action_id=ok["id"])["annotation"]["market"] == market


def test_frontend_ambiguity_tables_match_backend():
    """防漂移：前后端"代码段不可证"的两张前缀表必须逐字一致。

    安全边界就是这两条规则（000xxx 整段 + 纯指数前缀），两侧各写一份是最容易漂的地方。
    名单式实现（只列已知撞号）已删除：它覆盖不到 000002/000003，且"名单里没有就放行"
    本身就是把不确定性当成安全。
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "frontend/src/lib/copilot/serverAnnotations.ts"
    text = src.read_text(encoding="utf-8")

    def literal(name: str) -> list[str]:
        block = text.split(f"const {name}", 1)
        assert len(block) == 2, f"前端必须保留 {name} 常量"
        body = block[1].split("=", 1)[1].split("]", 1)[0]
        return re.findall(r"'(\d+)'", body)

    assert literal("AMBIGUOUS_SYMBOL_PREFIXES") == list(A.AMBIGUOUS_SYMBOL_PREFIXES), (
        f"前后端同号代码段漂移：前端 {literal('AMBIGUOUS_SYMBOL_PREFIXES')} / "
        f"后端 {list(A.AMBIGUOUS_SYMBOL_PREFIXES)}"
    )
    assert literal("INDEX_CODE_PREFIXES") == list(A.INDEX_ONLY_PREFIXES), (
        f"前后端指数前缀漂移：前端 {literal('INDEX_CODE_PREFIXES')} / "
        f"后端 {list(A.INDEX_ONLY_PREFIXES)}"
    )
    assert "INDEX_CODE_COLLISIONS" not in text, "名单式实现不得回流（只覆盖已知撞号）"


# ── 12. ts-only：确认了也画不出来 ⇒ 明确拒绝（不制造死数据）────────────────────


def test_ts_only_with_clock_time_is_still_refused_over_http(db, http):
    """带时分的 ts-only：无法证明落在哪一根 K 线（分钟柱时间戳依赖交易所时区）⇒ 仍拒。

    2026-10-28 P2 恢复了 marker 图层（日粒度时间点可以上图，见 §14），但"带时分"这一半
    没有可证的口径：映射错一根就是把标记画到错的 K 线上，比不画更糟。
    """
    item = _propose(db, {"symbol": SYMBOL, "label": "时间点标注", "ts": "2026-10-28 10:00"})
    resp = http.post(f"/copilot/actions/{item['id']}/apply")

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "日粒度" in detail and "DOM" not in detail
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(CopilotAction).filter(CopilotAction.id == item["id"]).one().status == "pending"


def test_price_and_ts_together_is_still_allowed(db):
    """ts 只是附加信息时不影响水平线（有 price 就能画）。"""
    item = _propose(db, {"symbol": SYMBOL, "label": "带时间", "price": 30.0,
                         "ts": "2026-10-28 10:00"})
    out = A.apply_action(db, user_id=UID, action_id=item["id"])
    assert out["annotation"]["price"] == 30.0 and out["annotation"]["ts"] == "2026-10-28 10:00"


# ── 13. 唯一索引冲突：SQLite / PG 都要给 400 + 可执行诊断（原始 HTTP 行为）───────


def test_http_conflict_with_foreign_dangling_link_is_400_with_diagnosis(db, http):
    """别人的悬空链接占号 ⇒ 400 + 点名动作，不 500、不改任何历史、不落标注。"""
    other_item = _propose(db, {"symbol": SYMBOL, "label": "他人线", "price": 21.0},
                          user_id=OTHER_UID)
    other = A.apply_action(db, user_id=OTHER_UID, action_id=other_item["id"])
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": other["annotation_id"]})
    db.commit()

    mine = _propose(db, {"symbol": SYMBOL, "label": "我的线", "price": 22.0})
    resp = http.post(f"/copilot/actions/{mine['id']}/apply")
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert f"#{other_item['id']}" in detail
    assert "UNIQUE constraint" not in detail and "duplicate key" not in detail, "不把数据库原文当文案"

    theirs = db.query(CopilotAction).filter(CopilotAction.id == other_item["id"]).one()
    assert theirs.annotation_id == other["annotation_id"], "不跨账号改写历史关联"
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0


def test_http_conflict_that_escaped_the_guard_is_still_400_not_500(db, http, monkeypatch):
    """竞态：守卫没拦住（另一个连接抢先占了 id）→ 唯一索引兜底也必须是 400 而非 500。

    SQLite 的报错原文只有 `UNIQUE constraint failed: copilot_actions.annotation_id`，
    **不含索引名** —— 旧判断只查索引名，必然漏判成 500。
    """
    monkeypatch.setattr(A, "_guard_annotation_id", lambda db, *, user_id: None)

    other_item = _propose(db, {"symbol": SYMBOL, "label": "他人线", "price": 21.0},
                          user_id=OTHER_UID)
    other = A.apply_action(db, user_id=OTHER_UID, action_id=other_item["id"])
    db.execute(text("DELETE FROM kline_annotations WHERE id = :i"), {"i": other["annotation_id"]})
    db.commit()

    mine = _propose(db, {"symbol": SYMBOL, "label": "我的线", "price": 22.0})
    resp = http.post(f"/copilot/actions/{mine['id']}/apply")
    assert resp.status_code == 400, f"唯一索引冲突必须是 400，实际 {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert "冲突" in detail or "占用" in detail
    assert "uq_copilot_actions_annotation" in detail or "历史动作关联" in detail
    assert db.query(KlineAnnotation).filter(KlineAnnotation.user_id == UID).count() == 0
    assert db.query(CopilotAction).filter(CopilotAction.id == mine["id"]).one().status == "pending"


def test_is_annotation_link_violation_recognizes_both_dialects():
    """诊断分支的判据本身：PG 带索引名 / SQLite 只带列名 / 别的约束不误判。"""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    pg = SAIntegrityError(
        "INSERT ...",
        {},
        Exception('duplicate key value violates unique constraint "uq_copilot_actions_annotation"'),
    )
    sq = SAIntegrityError(
        "INSERT ...", {},
        Exception("UNIQUE constraint failed: copilot_actions.annotation_id"),
    )
    other = SAIntegrityError("INSERT ...", {}, Exception("UNIQUE constraint failed: stocks.symbol"))

    assert A._is_annotation_link_violation(pg) is True
    assert A._is_annotation_link_violation(sq) is True, "SQLite 原文不含索引名，也必须认出来"
    assert A._is_annotation_link_violation(other) is False


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_http_apply_success_and_ts_only_refusal_on_both_dialects(dialect, pg_db):
    """同一组 HTTP 行为在 SQLite 与真实 PG 上一致（SQLite 仅辅助，PG 才是生产）。"""
    if dialect == "postgresql":
        session = pg_db
    else:
        db = SessionLocal()
        _cleanup(db)
        _ensure_users(db)
        session = db
    try:
        http = _http_for(session)
        ok = _propose(session, {"symbol": SYMBOL, "label": "正常行", "price": 33.0, "market": "SH"})
        resp = http.post(f"/copilot/actions/{ok['id']}/apply")
        assert resp.status_code == 200, resp.text
        assert resp.json()["annotation"]["market"] == "SH"

        bad = _propose(session, {"symbol": SYMBOL, "label": "只有时间", "ts": "2026-10-28"})
        bad_resp = http.post(f"/copilot/actions/{bad['id']}/apply")
        assert bad_resp.status_code == 400, bad_resp.text
        assert "ts" in bad_resp.json()["detail"]
    finally:
        if dialect == "sqlite":
            _cleanup(session)
            session.close()


def test_pg_http_unique_conflict_is_400_and_preserves_history(pg_db):
    """PG 生产语义：撞唯一索引 ⇒ 400 + 报告，**不改任何历史行**（默认拒绝，不猜测修复）。

    构造：把序列推到 42，再让另一个账号的历史动作占住 42（PG 正常不会发生，只有手工改库/
    序列重置才会）⇒ 下一次确认的 id=42 必然撞 uq_copilot_actions_annotation。
    """
    http = _http_for(pg_db)
    ghost = CopilotAction(user_id=OTHER_UID, kind="annotation", payload="{}",
                          status="applied", annotation_id=42)
    pg_db.add(ghost)
    pg_db.commit()
    pg_db.execute(text("SELECT setval(pg_get_serial_sequence('kline_annotations','id'), 41)"))
    pg_db.commit()

    mine = _propose(pg_db, {"symbol": SYMBOL, "label": "撞号", "price": 30.0})
    resp = http.post(f"/copilot/actions/{mine['id']}/apply")

    assert resp.status_code == 400, f"PG 唯一索引冲突必须 400，实际 {resp.status_code}: {resp.text}"
    detail = resp.json()["detail"]
    assert "uq_copilot_actions_annotation" in detail or "冲突" in detail
    assert "不跨账号" in detail or "未改动任何历史记录" in detail
    assert pg_db.query(KlineAnnotation).count() == 0
    assert pg_db.query(CopilotAction).filter(CopilotAction.id == mine["id"]).one().status == "pending"
    kept = pg_db.query(CopilotAction).filter(CopilotAction.id == ghost.id).one()
    assert kept.annotation_id == 42, "他人的历史关联原样保留"
