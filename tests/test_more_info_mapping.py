"""md_more_info 字段映射回归 (2026-09-09).

背景: 前端「扩展指标 · TQ实时」的 主力净流入/主买净额 与 L2 页「主力净额/主买净额」
一直显示 "--" —— 后端 `md_more_info` 漏把 vendor 的 zjl / zjl_hb 透传进 payload,
前端即使有值也读不到(见 KI-041)。
"""
import src.core.marketdata_client as mc


class _MD:
    def more_info(self, symbols, *, market="CN"):
        from marketdata.types import MoreInfo

        return [
            MoreInfo(
                symbol=s,
                market="CN",
                zjl=-7086.62,
                zjl_hb=-3762.99,
                l2_tick_num=120327,
                l2_order_num=224629,
                total_buy_vol=37842.0,
                total_sell_vol=96825.0,
            )
            for s in symbols
        ]


def test_md_more_info_passes_zjl_fields(monkeypatch):
    monkeypatch.setattr(mc, "get_market_data", lambda: _MD())
    rows = mc.md_more_info(["002361"], "CN")
    assert len(rows) == 1
    r = rows[0]
    # 资金两字段必须透传(此前缺失 → 前端恒显 "--")
    assert r["zjl"] == -7086.62
    assert r["zjl_hb"] == -3762.99
    # 既有字段不回归
    assert r["l2_tick_num"] == 120327
    assert r["total_buy_vol"] == 37842.0
    assert r["symbol"] == "002361"
