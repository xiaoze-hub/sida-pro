"""竞价复盘降级回归(2026-09-10 修)。

问题: `auction_review` 9:26 触发, 正落在采集器判定的悟道限流窗口(9:15-10:30)内 →
`fetch_auction_raw()` 五字段全空; 而 agent `collect` 无条件写 `client_ok=True` →
`build_prompt` 的"数据不可用→降级"分支永不触发, 送空 prompt → LLM 回报
"关键数据缺失提醒…请补充数据源"(或误判"日期在未来")。

覆盖:
  - 限流窗口/悟道全空时, `fetch_auction_raw` 用腾讯降级榜填充 opening_snapshot
  - agent 如实置 client_ok / degraded
  - build_prompt: 全空走降级分支; 仅腾讯降级带"数据口径"说明
"""
from __future__ import annotations

import asyncio

from src.agents.auction_review import AuctionReviewAgent
from src.collectors import auction_collector as ac

BOARD = "【竞价高开榜(腾讯降级)】(候选池24只, 竞价涨停3只, 按竞价涨幅排序)\n- 002361 神剑股份: 10.12 (+9.99%) 🔴涨停"


def test_raw_uses_tencent_fallback_in_limit_window(monkeypatch):
    monkeypatch.setattr(ac, "_wudao_available", lambda: False)
    monkeypatch.setattr(ac, "_fetch_tencent_gainer_board", lambda *a, **k: BOARD)
    out = ac.fetch_auction_raw()
    assert out["limited"] is True
    assert out["opening_snapshot"]["text"] == BOARD
    assert out["opening_snapshot"]["source"] == "tencent_fallback"


def test_raw_falls_back_when_wudao_returns_empty(monkeypatch):
    monkeypatch.setattr(ac, "_wudao_available", lambda: True)
    monkeypatch.setattr(ac, "_fetch_tencent_gainer_board", lambda *a, **k: BOARD)

    class _EmptyClient:
        def _initialize(self):
            pass

        def __getattr__(self, _name):
            return lambda *a, **k: {}

    monkeypatch.setattr(
        "src.collectors.wudao_mcp_client.WudaoMCPClient", lambda: _EmptyClient()
    )
    out = ac.fetch_auction_raw()
    assert out["opening_snapshot"]["source"] == "tencent_fallback"
    assert out["limited"] is False


def test_collect_marks_degraded(monkeypatch):
    monkeypatch.setattr(
        ac,
        "fetch_auction_raw",
        lambda: {
            "opening_snapshot": {"text": BOARD, "source": "tencent_fallback"},
            "theme_strength": {},
            "market_scan": {},
            "weak_to_strong": {},
            "limitup_feedback": {},
            "limited": True,
            "error": "悟道限流窗口",
        },
    )
    data = asyncio.run(AuctionReviewAgent().collect(None))  # type: ignore[arg-type]
    ad = data["auction_data"]
    assert ad["client_ok"] is True
    assert ad["degraded"] is True
    assert ad["source"] == "tencent_fallback"


def test_collect_marks_unavailable_when_all_empty(monkeypatch):
    monkeypatch.setattr(
        ac,
        "fetch_auction_raw",
        lambda: {
            "opening_snapshot": {},
            "theme_strength": {},
            "market_scan": {},
            "weak_to_strong": {},
            "limitup_feedback": {},
            "limited": True,
            "error": "悟道限流窗口",
        },
    )
    data = asyncio.run(AuctionReviewAgent().collect(None))  # type: ignore[arg-type]
    assert data["auction_data"]["client_ok"] is False


def test_to_ths_code():
    assert ac._to_ths_code("002361") == "USZA002361"
    assert ac._to_ths_code("600519") == "USHA600519"
    assert ac._to_ths_code("688981") == "USHA688981"
    assert ac._to_ths_code("830799") == "USTM830799"
    assert ac._to_ths_code("USZA002361") == "USZA002361"
    assert ac._to_ths_code("AAPL") == ""


def test_fetch_snapshots_thsdk_filters_and_scopes(monkeypatch):
    from src.core import thsdk_alert as ta

    class _FakeTHS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ta, "THS", _FakeTHS)
    monkeypatch.setattr(ta, "_fetch_prev_close", lambda ths, code: 10.0)

    def _snap(ticks, prev):
        code = getattr(_snap, "last_code", "")
        return {"direction": "无数据" if "000001" in code else "高开", "auction_price": 10.1}

    def _fetch(ths, code, date=None):
        _snap.last_code = code
        return [1]

    monkeypatch.setattr(ta, "_fetch_tick_super_level1", _fetch)
    monkeypatch.setattr(ta, "auction_snapshot", _snap)

    out = ac.fetch_auction_snapshots_thsdk(["600519", "000001"], limit=10)
    assert set(out) == {"600519"}  # "无数据" 的被过滤, 不冒充
    assert out["600519"]["direction"] == "高开"

    # limit 生效
    out2 = ac.fetch_auction_snapshots_thsdk(["600519", "000001", "300750"], limit=1)
    assert set(out2) == {"600519"}


def test_collect_ok_when_only_thsdk_snapshots(monkeypatch):
    """腾讯降级失败但 thsdk 快照有数据 → client_ok=True 且 source=thsdk。"""
    monkeypatch.setattr(
        ac,
        "fetch_auction_raw",
        lambda: {
            "opening_snapshot": {},
            "theme_strength": {},
            "market_scan": {},
            "weak_to_strong": {},
            "limitup_feedback": {},
            "limited": True,
            "error": "悟道限流窗口",
        },
    )
    monkeypatch.setattr(
        ac,
        "fetch_auction_snapshots_thsdk",
        lambda symbols, limit=10: {"002361": {"direction": "低开", "gap_pct": -0.2}},
    )

    class _Ctx:
        class _S:
            symbol = "002361"

        watchlist = [_S()]

    data = asyncio.run(AuctionReviewAgent().collect(_Ctx()))  # type: ignore[arg-type]
    ad = data["auction_data"]
    assert ad["client_ok"] is True
    assert ad["degraded"] is True
    assert ad["source"] == "thsdk"
    assert "002361" in ad["auction_snapshots"]


def test_build_prompt_renders_thsdk_snapshots():
    _, uc = AuctionReviewAgent().build_prompt(
        {
            "timestamp": "2026-09-10T09:26:00",
            "auction_data": {
                "client_ok": True,
                "degraded": True,
                "client_error": "悟道限流窗口",
                "opening_snapshot": {"text": BOARD, "source": "tencent_fallback"},
                "auction_snapshots": {
                    "002361": {
                        "direction": "低开",
                        "auction_price": 10.1,
                        "auction_high": 10.12,
                        "auction_low": 10.1,
                        "gap_pct": -0.198,
                        "withdraw_rate_pre0920": 0.17,
                    }
                },
            },
        },
        None,  # type: ignore[arg-type]
    )
    assert "自选竞价快照(同花顺超级盘口, 逐票)" in uc
    assert "002361: 低开" in uc
    assert "撤单率近似17.0%" in uc


def test_build_prompt_degrade_branches():
    agent = AuctionReviewAgent()
    # 全空 → 明确降级, 且不索要数据
    sp, uc = agent.build_prompt(
        {
            "timestamp": "2026-09-10T09:26:00",
            "auction_data": {"client_ok": False, "client_error": "悟道限流窗口"},
        },
        None,  # type: ignore[arg-type]
    )
    assert "竞价数据全部不可用" in uc
    assert "不要向用户索要数据" in uc

    # 仅腾讯降级 → 带"数据口径"说明, 并给出可分析的数据
    _, uc2 = agent.build_prompt(
        {
            "timestamp": "2026-09-10T09:26:00",
            "auction_data": {
                "client_ok": True,
                "degraded": True,
                "client_error": "悟道限流窗口",
                "opening_snapshot": {"text": BOARD, "source": "tencent_fallback"},
            },
        },
        None,  # type: ignore[arg-type]
    )
    assert "数据口径" in uc2
    assert BOARD in uc2
    assert "严禁编造具体数字" not in uc2  # 有数据时不走全空分支
