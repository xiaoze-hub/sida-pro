# -*- coding: utf-8 -*-
"""交易模式画像(2026-10-22)验收: 只讲证据、不讲奉承、样本不足必须显式标注。

## 为什么要有这组测试
本模块最大的风险不是"算错", 是**编造**:
1. 把系统信号台账(decision_log)当成主人自己的战绩 → 必须结构上隔离;
2. 拿 3 个样本算 67% → 必须 n<8 不出结论;
3. 本人只有聚合画像, 却硬编"止损纪律差" → 必须宁缺不编。

## 覆盖
- n 统计(未回填的行不进分母)
- confidence 分档(low/mid/high)
- n<8 不产出结论(只进 notes)
- render 含"样本不足"、不含买卖指令措辞、不含台账胜率
- block 异常返回空串(熔断)
- 台账口径声明逐字出现在 notes
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import trader_playbook as tp  # noqa: E402


USER_ID = "u-playbook-001"

#: 已占用的 (kind, symbol, trade_date) —— 唯一键防撞(每个用例重置)
_USED_DAYS: set[tuple[str, str, str]] = set()


@pytest.fixture(autouse=True)
def _reset_used_days():
    _USED_DAYS.clear()
    yield
    _USED_DAYS.clear()


# ────────────────────────────── 夹具 ──────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """临时 SQLite 库 + 真实建表/迁移(拿到真实 decision_log/users 结构)。"""
    from src.db.models import Base
    from src.web.migrations import run_versioned_migrations

    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    # users 表由 ORM create_all 建(迁移只加列), decision_log 由迁移建
    import src.web.models  # noqa: F401  (触发全部 ORM 类注册到 Base.metadata)

    Base.metadata.create_all(bind=eng)
    run_versioned_migrations(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


def _add_user(db, *, uid=USER_ID, profile=None):
    """插一个用户(用 ORM, 走 Python 侧默认值; shadow_profile_json 存 JSON 文本)。"""
    from src.db.models import User

    u = User(
        id=uid,
        username=f"user-{uid}",
        password_hash="x",
        role="member",
        shadow_profile_json=json.dumps(profile) if profile is not None else None,
    )
    db.add(u)
    db.commit()


def _add_ledger(
    db,
    *,
    kind="resonance3",
    n=0,
    win=0,
    regime="repair",
    symbol="600000",
    date0="20260101",
    fill=True,
    ret=0.01,
):
    """往 decision_log 塞 n 行(默认全为命中, ret 可指定)。

    唯一键是 (signal_kind, symbol, trade_date) → 逐行递增 trade_date 避开撞键
    (同一测试里可能对同一 kind 调多次, 故从"全局已用日"往后找空位)。
    """
    from datetime import date, timedelta

    base = date(int(date0[:4]), int(date0[4:6]), int(date0[6:8]))
    for i in range(n):
        hit = 1 if i < win else 0
        day = base
        for _ in range(400):  # 找一个没被占用的 (kind, symbol, day)
            key = (kind, symbol, day.strftime("%Y%m%d"))
            if key not in _USED_DAYS:
                break
            day = day + timedelta(days=1)
        _USED_DAYS.add((kind, symbol, day.strftime("%Y%m%d")))
        db.execute(
            text(
                "INSERT INTO decision_log "
                "(signal_kind, symbol, trade_date, price_at_signal, context_json, source, "
                " ret_t1, ret_t3, ret_t5, hit_t1, hit_t3, hit_t5, regime) "
                "VALUES (:k, :s, :d, 10.0, '{}', 'test', "
                " :r1, :r3, :r5, :h1, :h3, :h5, :reg)"
            ),
            {
                "k": kind,
                "s": symbol,
                "d": day,
                "r1": ret if fill else None,
                "r3": ret if fill else None,
                "r5": ret if fill else None,
                "h1": hit if fill else None,
                "h3": hit if fill else None,
                "h5": hit if fill else None,
                "reg": regime,
            },
        )
    db.commit()


_PROFILE_457 = {
    "shadow_id": "shadow_88f3d0d6",
    "profitable_roundtrips": 249,
    "total_roundtrips": 457,
    "date_range": ["2024-01-24 00:00:00", "2026-08-11 00:00:00"],
    "profile_text": "你 457 笔已平仓回合中 249 笔盈利。",
    "rules": [
        {
            "rule_id": "R1",
            "human_text": "A 股于 0 点买入, 持有 1-34 天",
            "holding_days_range": [1, 34],
            "support_count": 248,
            "coverage_rate": 0.996,
        }
    ],
    "preferred_markets": ["china_a"],
    "typical_holding_days": [5.0, 21.0],
}


# ────────────────────────────── 样本量与分母 ──────────────────────────────


def test_sample_counts_and_denominator(db):
    """n 只统计真实行数; 胜率分母 = 已回填行(未回填不进分母)。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=10, win=6, fill=True)   # 10 行全部回填, 6 命中
    _add_ledger(db, n=5, win=0, fill=False)   # 5 行未回填(hit/ret 全 NULL)

    pb = tp.build(db, USER_ID)

    assert pb["sample"]["roundtrips"] == 457
    assert pb["sample"]["decisions"] == 15          # 台账总行数
    assert pb["signal_ledger"]["n"] == 15
    assert pb["signal_ledger"]["n_t1"] == 10        # 分母只算已回填
    assert pb["signal_ledger"]["win_t1"] == pytest.approx(0.6)


def test_null_returns_excluded_from_averages(db):
    """NULL 收益不进均值(不补 0, 0 是真实收益)。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=4, win=4, fill=True, ret=0.05)

    pb = tp.build(db, USER_ID)
    assert pb["signal_ledger"]["avg_ret_t1"] == pytest.approx(0.05)
    assert pb["signal_ledger"]["n_ret_t1"] == 4


# ────────────────────────────── 置信度分档 ──────────────────────────────


def test_confidence_buckets(db):
    """n<30 → low; 30~100 → mid; >100 → high。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, kind="resonance3", n=120, win=60)
    _add_ledger(db, kind="demon_pool", n=40, win=20)
    _add_ledger(db, kind="bdqk_confirm", n=10, win=5)

    pb = tp.build(db, USER_ID)
    conf = {row["signal_kind"]: row["confidence"] for row in pb["by_kind"]}
    assert conf["resonance3"] == "high"
    assert conf["demon_pool"] == "mid"
    assert conf["bdqk_confirm"] == "low"

    # 本人画像 457 回合 → high
    assert all(s["confidence"] == "high" for s in pb["strengths"])


def test_every_conclusion_carries_n(db):
    """每条长项/短板都必须带 n 与置信度。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    pb = tp.build(db, USER_ID)
    for bucket in ("strengths", "weaknesses"):
        for item in pb[bucket]:
            assert isinstance(item["n"], int) and item["n"] > 0
            assert item["confidence"] in ("low", "mid", "high")
            assert item["text"]
            assert set(item) >= {"key", "text", "metric", "n", "confidence"}


# ────────────────────────────── 样本不足不出结论 ──────────────────────────────


def test_small_sample_group_produces_no_conclusion(db):
    """n<8 的分组不进 by_kind, 只进 notes。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, kind="tiny_kind", n=7, win=3)
    _add_ledger(db, kind="big_kind", n=20, win=10)

    pb = tp.build(db, USER_ID)
    kinds = {row["signal_kind"] for row in pb["by_kind"]}
    assert "tiny_kind" not in kinds
    assert "big_kind" in kinds
    assert any("tiny_kind" in n and "样本" in n for n in pb["notes"])


def test_small_profile_produces_no_strengths(db):
    """本人回合数 < 8 → 不产出长项/短板(宁缺不编)。"""
    _add_user(db, profile={**_PROFILE_457, "total_roundtrips": 5, "profitable_roundtrips": 3})

    pb = tp.build(db, USER_ID)
    assert pb["strengths"] == []
    assert pb["weaknesses"] == []
    assert "样本不足" in tp.render(pb)


# ────────────────────────────── 口径隔离(核心) ──────────────────────────────


def test_ledger_is_not_reported_as_own_win_rate(db):
    """台账数字绝不能进 strengths/weaknesses, 也不能出现在 render 正文里。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=100, win=10)  # 台账 T+1 胜率 10% —— 远低于本人 54.5%

    pb = tp.build(db, USER_ID)
    text = tp.render(pb)

    # 本人胜率只引用 457 回合
    assert "54.5%" in text
    assert "10.0%" not in text
    # 短板/长项里不许出现台账口径的胜率
    for item in pb["strengths"] + pb["weaknesses"]:
        assert "10.0%" not in item["text"]
        assert "30.1%" not in item["text"]
    # 台账单独成段
    assert pb["signal_ledger"]["win_t1"] == pytest.approx(0.1)
    assert pb["signal_ledger"]["note"] == tp.LEDGER_NOTE


def test_ledger_note_verbatim_in_notes(db):
    """口径声明必须逐字出现, 防止下游把它当本人业绩。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=20, win=10)

    pb = tp.build(db, USER_ID)
    assert tp.LEDGER_NOTE in pb["notes"]
    assert tp.LEDGER_NOTE in pb["signal_ledger"]["note"]


def test_no_tick_note_present_when_only_aggregate(db):
    """本人只有聚合画像 → 显式写明逐笔未落库, 短板不硬编。"""
    _add_user(db, profile=_PROFILE_457)

    pb = tp.build(db, USER_ID)
    assert tp.NO_TICK_NOTE in pb["notes"]
    assert "止损纪律" in tp.NO_TICK_NOTE


def test_missing_profile_says_insufficient(db):
    """没有交割单画像 → 样本不足, 不产出结论, 不编数字。"""
    _add_user(db, profile=None)
    _add_ledger(db, n=50, win=25)

    pb = tp.build(db, USER_ID)
    assert pb["strengths"] == []
    assert pb["weaknesses"] == []
    assert pb["baseline"]["win_rate"] is None
    text = tp.render(pb)
    assert "样本不足" in text
    assert "%" not in text.split("长项")[0]  # 头部没有任何百分比战绩


# ────────────────────────────── render ──────────────────────────────


def test_render_contains_required_segments(db):
    """render 形如: 胜率…（n 回合）｜持仓中位… 长项… 短板… 样本不足未结论…"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    text = tp.render(tp.build(db, USER_ID))
    assert "主人交易模式" in text
    assert "54.5%" in text
    assert "457 回合" in text
    assert "持仓中位 5 天" in text
    assert "长项：" in text
    assert "短板：" in text
    assert "样本不足未结论：" in text
    assert len(text) <= tp.RENDER_MAX_CHARS


def test_render_insufficient_section_leads_with_real_gap(db):
    """"样本不足未结论"段必须先说真正的证据缺口, 不是口径说明。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    text = tp.render(tp.build(db, USER_ID))
    seg = text.split("样本不足未结论：", 1)[1]
    assert tp.NO_TICK_NOTE in seg, "逐笔未落库的缺口必须出现在样本不足段"
    assert "胜率口径" not in seg.split("；")[0], "口径说明不该占样本不足段第一条"


def test_render_has_no_trading_directive_wording(db):
    """只讲行为特征, 不出现买卖指令措辞。

    注: NO_TICK_NOTE 是**必写的口径声明**(逐字要求: "盈亏比/加仓行为/止损纪律无法计算"),
    里面的"加仓/止损"是"算不出"的意思, 不是指令 → 先把该声明摘掉再查禁用词。
    """
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    text = tp.render(tp.build(db, USER_ID))
    body = text.replace(tp.NO_TICK_NOTE, "")
    for banned in ("建议买", "建议卖", "可以买入", "应该买", "该买", "该卖", "加仓", "减仓"):
        assert banned not in body, f"render 出现买卖指令措辞: {banned}"


def test_render_truncates_overlong(db):
    """超长输出截断(不炸 prompt)。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    pb = tp.build(db, USER_ID)
    pb["strengths"] = [
        {"key": f"k{i}", "text": "很长的一句话" * 40, "metric": {}, "n": 100, "confidence": "high"}
        for i in range(5)
    ]
    text = tp.render(pb)
    assert len(text) <= tp.RENDER_MAX_CHARS


# ────────────────────────────── block 熔断 ──────────────────────────────


def test_block_returns_empty_on_exception(db, monkeypatch):
    """异常一律返回空串(熔断), 绝不拖垮对话。"""
    def _boom(*_a, **_kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(tp, "build", _boom)
    assert tp.block(db, USER_ID) == ""


def test_block_returns_empty_on_broken_db():
    """db 本身坏掉 → 仍返回空串。"""
    class _Broken:
        def execute(self, *a, **kw):
            raise RuntimeError("no such table: users")

    assert tp.block(_Broken(), USER_ID) == ""


def test_block_normal_path(db):
    """正常路径: 返回可注入的文本块。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    text = tp.block(db, USER_ID)
    assert "主人交易模式" in text
    assert len(text) <= tp.RENDER_MAX_CHARS


# ────────────────────────────── 其它 ──────────────────────────────


def test_by_regime_present(db):
    """regime 分桶产出且带中文标签。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, regime="repair", n=30, win=18)
    _add_ledger(db, regime="ebb", n=20, win=4)

    pb = tp.build(db, USER_ID)
    regs = {r["regime"]: r for r in pb["by_regime"]}
    assert regs["repair"]["win_t1"] == pytest.approx(0.6)
    assert regs["repair"]["label"] == "修复"
    assert regs["ebb"]["n"] == 20


def test_build_is_readonly(db):
    """只读: build 之后账本行数与内容不变。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    before = db.execute(text("SELECT count(*) FROM decision_log")).scalar()
    tp.build(db, USER_ID)
    after = db.execute(text("SELECT count(*) FROM decision_log")).scalar()
    assert before == after


def test_build_accepts_engine_not_only_session(db):
    """Engine / Connection 也吃(批处理与生产验证传的是引擎, 不是 ORM 会话)。"""
    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)

    eng = db.get_bind()
    pb = tp.build(eng, USER_ID)
    assert pb["sample"]["roundtrips"] == 457
    assert pb["signal_ledger"]["n"] == 30
    assert not pb.get("fatal")

    with eng.connect() as conn:
        pb2 = tp.build(conn, USER_ID)
    assert pb2["sample"]["roundtrips"] == 457


def test_unknown_user_returns_empty_evidence(db):
    """查无此人 → 不编数据(全部空 + 样本不足)。"""
    pb = tp.build(db, "no-such-user")
    assert pb["baseline"]["win_rate"] is None
    assert pb["strengths"] == []
    assert "样本不足" in tp.render(pb)


def test_system_prompt_injects_playbook_once(db):
    """接线回归：传过交割单的用户，其 system prompt 必须带上「交易模式画像（证据）」。"""
    from src.core.ai_client import build_system_prompt
    from src.db.models import User

    _add_user(db, profile=_PROFILE_457)
    _add_ledger(db, n=30, win=15)
    user = db.query(User).filter(User.id == USER_ID).one()   # _add_user 不返回对象

    out = build_system_prompt(db, "copilot", "你是数智参谋。", user)
    assert "交易模式画像（证据）" in out, "画像没注入 —— 参谋不知道用户是谁"
    assert "54.5%" in out or "0.545" in out, "注入块里应带真实胜率证据"
    assert out.count("交易模式画像（证据）") == 1, "不能重复注入"
    assert out.startswith("你是数智参谋。"), "原始提示词必须保留在最前"


def test_system_prompt_skips_playbook_for_user_without_journal(db):
    """没传过交割单的用户：不注入噪声（连'样本不足'都不该塞进系统提示）。"""
    from src.core.ai_client import build_system_prompt
    from src.db.models import User

    _add_user(db, profile=None)
    user = db.query(User).filter(User.id == USER_ID).one()
    out = build_system_prompt(db, "copilot", "你是数智参谋。", user)
    assert "交易模式画像" not in out
