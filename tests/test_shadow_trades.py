"""§6.2「交割单标 K 线」数据面回归 —— /api/shadow/trades + 落库紧凑化。

设计: 分析时把成交明细一并塞进 `users.shadow_profile_json.trades`(不建新表/不迁移),
`/shadow/trades` 只读调用者自己那一列 → 归属天然隔离。
本文件钉四件事:
  ① 没上传过 → 空表 + saved=False + 一句可执行的 note(**不编造**记录);
  ② 按标的过滤正确, symbols 去重且保持出现顺序;
  ③ 用户之间不串(读他人明细的路径根本不存在);
  ④ 超过上限只留**最近** MAX_STORED_TRADES 笔, 并置 capped=True(**明示被截断**, 不装全量)。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.web.api import shadow as shadow_api


def _user(trades: list[dict] | None = None, shadow_id: str | None = "shadow_abc12345"):
    payload = None
    if trades is not None:
        payload = {"shadow_id": shadow_id, "trades": trades}
    return SimpleNamespace(id="u1", role="member", shadow_profile_json=payload)


def _trade(symbol: str, dt: str, side: str = "buy", price: float = 10.0) -> dict:
    return {
        "datetime": dt,
        "symbol": symbol,
        "name": f"名{symbol}",
        "side": side,
        "quantity": 100.0,
        "price": price,
        "amount": price * 100,
        "market": "china_a",
    }


# ── ① 无上传 ─────────────────────────────────────────────────────


def test_trades_empty_without_upload():
    """没上传过交割单 → 空表 + saved=False + 可执行提示, 不返回任何编造记录。"""
    res = shadow_api.get_my_trades(user=_user(None))
    assert res["trades"] == []
    assert res["symbols"] == []
    assert res["total"] == 0
    assert res["saved"] is False
    assert "重新上传" in res["note"]


def test_trades_empty_when_profile_has_no_trades_key():
    """老版本落库的画像(只有 shadow_id, 无 trades) → 同样空表, 但 saved=True(画像是在的)。"""
    user = SimpleNamespace(
        id="u1", role="member", shadow_profile_json={"shadow_id": "shadow_abc12345"}
    )
    res = shadow_api.get_my_trades(user=user)
    assert res["trades"] == []
    assert res["saved"] is True
    assert res["note"]  # 仍给出"重新上传一次即可"的说明


# ── ② 过滤 + symbols 去重保序 ─────────────────────────────────────


def test_trades_filter_by_symbol_and_symbols_dedup_keeps_order():
    trades = [
        _trade("600519.SH", "2026-01-05 09:35:00"),
        _trade("002361.SZ", "2026-01-06 10:00:00", side="sell"),
        _trade("600519.SH", "2026-01-07 14:20:00", side="sell"),
    ]
    user = _user(trades)

    all_res = shadow_api.get_my_trades(user=user)
    assert all_res["total"] == 3
    assert all_res["symbols"] == ["600519.SH", "002361.SZ"]  # 去重且保持首次出现顺序

    one = shadow_api.get_my_trades(symbol="600519.SH", user=user)
    assert one["total"] == 2
    assert {t["symbol"] for t in one["trades"]} == {"600519.SH"}
    # 过滤只影响 trades, symbols 仍是全量标的清单(前端下拉要能看到所有标的)
    assert one["symbols"] == ["600519.SH", "002361.SZ"]

    none = shadow_api.get_my_trades(symbol="000001.SZ", user=user)
    assert none["total"] == 0
    assert none["trades"] == []


# ── ③ 用户隔离 ───────────────────────────────────────────────────


def test_trades_are_isolated_between_users():
    a = _user([_trade("600519.SH", "2026-01-05 09:35:00")])
    b = _user([_trade("002361.SZ", "2026-01-06 10:00:00")])

    ra = shadow_api.get_my_trades(user=a)
    rb = shadow_api.get_my_trades(user=b)

    assert {t["symbol"] for t in ra["trades"]} == {"600519.SH"}
    assert {t["symbol"] for t in rb["trades"]} == {"002361.SZ"}
    # 端点签名里没有"指定 user_id"的参数 —— 越权读他人交割单的入口不存在
    import inspect

    assert "user_id" not in inspect.signature(shadow_api.get_my_trades).parameters


# ── ④ 截断: 只留最近 N 笔 + capped 明示 ───────────────────────────


def test_compact_trades_caps_keeps_most_recent_and_flags():
    n = shadow_api.MAX_STORED_TRADES
    records = [
        SimpleNamespace(
            datetime=f"2026-01-{i % 28 + 1:02d} 09:30:00",
            symbol="600519.SH",
            name="贵州茅台",
            side="buy",
            quantity=100,
            price=float(100 + i),
            amount=float((100 + i) * 100),
            market="china_a",
            fee=1.5,  # 冗余字段: 不应被落库(只留 _TRADE_KEYS)
        )
        for i in range(n + 25)
    ]
    trades, capped = shadow_api._compact_trades(records)

    assert capped is True
    assert len(trades) == n
    # 留的是**尾部**(最近), 不是头部
    assert trades[-1]["price"] == float(100 + n + 24)
    assert trades[0]["price"] == float(100 + 25)
    # 只留复盘必需字段
    assert set(trades[0].keys()) == set(shadow_api._TRADE_KEYS)
    assert "fee" not in trades[0]


def test_compact_trades_no_cap_when_under_limit():
    trades, capped = shadow_api._compact_trades(
        [SimpleNamespace(**{k: None for k in shadow_api._TRADE_KEYS})]
    )
    assert capped is False
    assert len(trades) == 1
    # 缺失数值保持 None(前端显示 `--`), 不补 0
    assert trades[0]["price"] is None
    assert trades[0]["quantity"] is None
