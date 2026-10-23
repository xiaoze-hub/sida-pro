"""防守线定向门禁(方案 B: -8% 硬防线只在用户自己设了才生效)。

裁决口径(2026-10-23 产品):
- `positions.defense_price` 是**绝对价**, NULL = 用户未设置;
- 未设置 → 不告警(系统不再用 cost*0.92 冒充用户纪律位);
- 取价失败 → 显式降级, 不静默消失、不 return None 当作"没击穿";
- 模拟盘的 DEFAULT_STOP_LOSS_PCT=0.92 保留但只是系统执行兜底, 标记 stop_basis。

本文件只跑定向路径, 不含全量回归(见任务硬约束: 禁全量 pytest)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.agents.intraday_monitor import evaluate_defense_breach
from src.core.paper_trading_engine import (
    DEFAULT_STOP_LOSS_PCT,
    STOP_BASIS_SIGNAL,
    STOP_BASIS_SYSTEM_DEFAULT,
    _stop_basis,
)
from src.db.models import Account, Base, Position, Stock, User
from src.web.api.accounts import DefenseLineUpdate, set_position_defense_line
from src.web.migrations import _m223_position_defense_price

REPO_ROOT = Path(__file__).resolve().parent.parent
BANNER = REPO_ROOT / "frontend" / "src" / "components" / "DefenseLineBanner.tsx"


@pytest.fixture()
def db(tmp_path):
    """独立临时库(不与 conftest 的共享库互污染)。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'defense.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db, *, defense_price: float | None = None, username: str = "u1"):
    user = User(
        id=f"user-{username}",
        username=username,
        password_hash="x",
        role="member",
        is_active=True,
        token_version=1,
    )
    db.add(user)
    db.flush()
    account = Account(name=f"账户-{username}", available_funds=10000.0, user_id=user.id)
    db.add(account)
    db.flush()
    stock = Stock(symbol="600519", name="贵州茅台", market="CN")
    db.add(stock)
    db.flush()
    position = Position(
        account_id=account.id,
        stock_id=stock.id,
        cost_price=100.0,
        quantity=100,
        user_id=user.id,
        defense_price=defense_price,
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    return user, position


# ---------- ① 无 defense_price: 不告警、不报错 ----------

def test_missing_defense_price_never_alerts(db):
    """未设置防线 ⇒ 判定函数不产出任何告警(哪怕浮亏已超 -8%)。"""
    _, position = _seed(db)
    assert position.defense_price is None  # 新列默认 NULL, 不回填成本价 *0.92

    verdict = evaluate_defense_breach(
        defense_price=position.defense_price,
        current_price=85.0,  # 浮亏 -15%, 旧口径会告警
        cost_price=position.cost_price,
    )
    assert verdict is None


@pytest.mark.parametrize("bad", [None, 0, 0.0, -1.0, "abc"])
def test_defense_price_invalid_is_treated_as_unset(bad):
    """0/负数/非数字都不是"纪律位", 一律按未设置处理(不抛异常)。"""
    assert evaluate_defense_breach(defense_price=bad, current_price=10.0, cost_price=12.0) is None


# ---------- ② 有 defense_price: 按绝对价判击穿 ----------

def test_breach_when_price_crosses_user_line():
    verdict = evaluate_defense_breach(defense_price=92.0, current_price=91.5, cost_price=100.0)
    assert verdict is not None
    assert verdict["breached"] is True
    assert verdict["defense_price"] == 92.0
    assert verdict["loss_pct"] == pytest.approx(-8.5)


def test_no_breach_above_user_line():
    verdict = evaluate_defense_breach(defense_price=92.0, current_price=95.0, cost_price=100.0)
    assert verdict is not None
    assert verdict["breached"] is False


def test_breach_is_inclusive_at_exact_line():
    """恰好等于防线价即算击穿(用户写下的价就是他要守住的那条)。"""
    verdict = evaluate_defense_breach(defense_price=92.0, current_price=92.0, cost_price=100.0)
    assert verdict is not None and verdict["breached"] is True


def test_user_line_differs_from_system_default():
    """用户设 95(比 -8% 更严)时, 现价 93 属于击穿 —— 系统 -8% 口径会漏掉这一击穿。"""
    verdict = evaluate_defense_breach(defense_price=95.0, current_price=93.0, cost_price=100.0)
    assert verdict is not None and verdict["breached"] is True
    # 同一价格下旧的 -8% 口径(92)不会告警, 证明纪律位确实来自用户
    legacy = evaluate_defense_breach(defense_price=92.0, current_price=93.0, cost_price=100.0)
    assert legacy is not None and legacy["breached"] is False


# ---------- ③ defense_price=null 可清除 ----------

def test_endpoint_sets_and_clears_defense_price(db):
    user, position = _seed(db, defense_price=None)

    set_position_defense_line(
        position.id, DefenseLineUpdate(defense_price=91.2), db=db, user=user
    )
    db.refresh(position)
    assert position.defense_price == pytest.approx(91.2)

    cleared = set_position_defense_line(
        position.id, DefenseLineUpdate(defense_price=None), db=db, user=user
    )
    db.refresh(position)
    assert position.defense_price is None
    # 响应体也必须是 null, 否则前端会继续按旧防线告警
    assert cleared["defense_price"] is None
    assert cleared["cost_price"] == pytest.approx(100.0)


@pytest.mark.parametrize("bad", [0, -3.5])
def test_endpoint_rejects_non_positive_price(db, bad):
    """0/负数不是价格, 也不是"清除"的写法 —— 400, 免得存下永远触发的防线。"""
    user, position = _seed(db)
    with pytest.raises(HTTPException) as exc:
        set_position_defense_line(position.id, DefenseLineUpdate(defense_price=bad), db=db, user=user)
    assert exc.value.status_code == 400


def test_endpoint_is_user_isolated(db):
    """别人的持仓一律 404(与其它持仓端点同口径, 防账号探测)。"""
    _, position = _seed(db, username="owner")
    other, _ = _seed(db, username="intruder")
    with pytest.raises(HTTPException) as exc:
        set_position_defense_line(
            position.id, DefenseLineUpdate(defense_price=90.0), db=db, user=other
        )
    assert exc.value.status_code == 404
    db.refresh(position)
    assert position.defense_price is None  # 未被越权改写


# ---------- ④ 取价失败: 显式降级, 不猜 ----------

def test_missing_quote_returns_none_instead_of_guessing():
    """缺现价时判定函数返回 None —— 它不得把"取不到价"当成"没击穿"或"已击穿"。"""
    assert evaluate_defense_breach(defense_price=92.0, current_price=None, cost_price=100.0) is None


def test_banner_renders_degraded_state_instead_of_returning_null():
    """UI 契约: 缺现价时必须渲染明确文案, 且组件内不得再有 cost*0.92 默认防线。"""
    src = BANNER.read_text(encoding="utf-8")
    assert "防守线无法计算（缺现价）" in src
    assert "defaultDefense" not in src
    assert "0.92" not in src
    assert "未设置防守线" in src


# ---------- 盘中监控取数口径 ----------

def test_load_defense_lines_only_returns_user_set_lines(db, monkeypatch):
    """只回用户显式设过的持仓: NULL 的持仓不进入告警候选(不退回 -8%)。"""
    from src.agents import intraday_monitor as im

    user, pos_with = _seed(db, defense_price=91.0, username="setter")
    _, pos_without = _seed(db, defense_price=None, username="unset")
    monkeypatch.setattr("src.db.session.SessionLocal", lambda: db)

    rows = im.load_defense_lines("600519", user_id=user.id)
    assert [r["position_id"] for r in rows] == [pos_with.id]
    assert rows[0]["defense_price"] == pytest.approx(91.0)
    assert pos_without.id not in [r["position_id"] for r in rows]


# ---------- 迁移 223 ----------

def test_migration_223_adds_column_idempotently(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE positions (id INTEGER PRIMARY KEY, cost_price FLOAT)"))
        _m223_position_defense_price(conn)
        _m223_position_defense_price(conn)  # 重跑不报错
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(positions)"))]
    engine.dispose()
    assert "defense_price" in cols
    assert cols.count("defense_price") == 1


def test_migration_223_skips_missing_table(tmp_path):
    """表不存在时静默跳过(迁移链不得因此中断)。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as conn:
        _m223_position_defense_price(conn)  # 不抛异常即通过
    engine.dispose()


# ---------- 纸上交易: 数值不变, 只标注来源 ----------

def test_paper_trading_default_stop_kept_but_marked_system_default():
    assert DEFAULT_STOP_LOSS_PCT == 0.92  # 数值不动, 避免存量模拟盘行为突变
    pos = SimpleNamespace(entry_price=10.0, stop_loss=9.2)
    assert _stop_basis(pos) == STOP_BASIS_SYSTEM_DEFAULT
    assert _stop_basis(SimpleNamespace(entry_price=10.0, stop_loss=9.5)) == STOP_BASIS_SIGNAL
    # 无止损(0/None)不能反过来被当作"信号纪律位"
    assert _stop_basis(SimpleNamespace(entry_price=10.0, stop_loss=None)) == STOP_BASIS_SYSTEM_DEFAULT
