"""TQ 条件选股(formula_process_mul_xg) 的分页/解析/扫描测试。

CI 无 TQ 网关, 全部 monkeypatch 假源。
覆盖两处**会导致静默错误**的行为:
- 服务端分页不续取 → 大列表结果残缺(旧实现即如此, 肉眼看不出来)
- 分片失败不标记 → 把残缺结果当成"全市场命中数"(等于编造)
"""

from __future__ import annotations

import pytest

from marketdata.vendors import tq as tqmod


def _page(codes: dict, *, more: bool, nxt: int, err: str = "0") -> dict:
    """构造一页网关响应(含批次元数据, 与真实响应同形)。"""
    out = dict(codes)
    out.update({
        "ErrorId": err,
        "BatchFormulaPaged": True,
        "batch_start_index": 0,
        "batch_end_index": len(codes),
        "batch_stock_count": len(codes),
        "stock_total": len(codes),
        "next_stock_index": nxt,
        "has_more_batch": more,
    })
    return out


def _series(*vals: str, start_day: int = 22) -> dict:
    """{信号名: [{Date, Value}]}，按日递增。"""
    return {"OUTPUT1": [{"Date": f"202609{start_day + i:02d}", "Value": v}
                        for i, v in enumerate(vals)]}


class Test翻页:
    def test_两页合并且剔除元数据(self, monkeypatch):
        calls: list[int] = []

        def fake(method, params, timeout=None, **kw):
            calls.append(params["batch_start_index"])
            assert kw.get("full") is True, "必须 full=True 自行判 ErrorId"
            if params["batch_start_index"] == 0:
                return _page({"000001.SZ": _series("1")}, more=True, nxt=1)
            return _page({"600519.SH": _series("0", "1")}, more=False, nxt=2)

        monkeypatch.setattr(tqmod, "_rpc", fake)
        res = tqmod.formula_xg_mul("MACD买入", ["000001.SZ", "600519.SH"])
        assert calls == [0, 1], "必须按游标续取第二页"
        assert set(res) == {"000001.SZ", "600519.SH"}
        for meta in ("BatchFormulaPaged", "next_stock_index", "has_more_batch", "ErrorId"):
            assert meta not in res, f"元数据 {meta} 不该混进结果"

    def test_数据过大ErrorId19只警告不抛(self, monkeypatch):
        monkeypatch.setattr(
            tqmod, "_rpc",
            lambda *a, **kw: _page({"002361.SZ": _series("1")}, more=False, nxt=1, err="19"),
        )
        res = tqmod.formula_xg_mul("MACD买入", ["002361.SZ"])
        assert "002361.SZ" in res, "部分返回也必须交回已取到的数据"

    def test_其他ErrorId抛异常(self, monkeypatch):
        monkeypatch.setattr(
            tqmod, "_rpc",
            lambda *a, **kw: _page({}, more=False, nxt=0, err="9"),
        )
        with pytest.raises(RuntimeError, match="ErrorId=9"):
            tqmod.formula_xg_mul("不存在的公式", ["002361.SZ"])

    def test_游标不前进不死循环(self, monkeypatch):
        n = {"c": 0}

        def fake(*a, **kw):
            n["c"] += 1
            return _page({"000001.SZ": _series("1")}, more=True, nxt=0)  # 游标恒 0

        monkeypatch.setattr(tqmod, "_rpc", fake)
        tqmod.formula_xg_mul("MACD买入", ["000001.SZ"])
        assert n["c"] == 1, "游标未前进时必须立刻停手, 不能反复打网关"

    def test_无分页标记时单次返回(self, monkeypatch):
        monkeypatch.setattr(
            tqmod, "_rpc",
            lambda *a, **kw: {"000001.SZ": _series("1"), "ErrorId": "0"},
        )
        assert set(tqmod.formula_xg_mul("KDJ买入", ["000001.SZ"])) == {"000001.SZ"}


class Test命中解析:
    def test_只认非零值(self):
        res = {
            "000001.SZ": _series("0"),
            "600519.SH": _series("1"),
            "002415.SZ": {"OUTPUT1": [{"Date": "20260922", "Value": None}]},
        }
        hits, _ = tqmod._formula_hits(res)
        assert [h["symbol"] for h in hits] == ["600519.SH"]
        assert hits[0]["value"] == "1"

    def test_指定日期取那一行(self):
        res = {"002361.SZ": _series("0", "1", "0")}  # 22/23/24
        hits, used = tqmod._formula_hits(res, date="20260923")
        assert used == "20260923"
        assert len(hits) == 1 and hits[0]["date"] == "20260923"

    def test_指定日期无该行则不命中(self):
        res = {"002361.SZ": _series("1")}  # 只有 20260922
        hits, _ = tqmod._formula_hits(res, date="20260924")
        assert hits == []

    def test_信号名随公式变化也认(self):
        res = {"002361.SZ": {"UP3": [{"Date": "20260922", "Value": "1"}]}}
        hits, _ = tqmod._formula_hits(res)
        assert hits and hits[0]["signal"] == "UP3"


class Test全市场扫描:
    def test_分片调用且合并命中(self, monkeypatch):
        seen: list[list[str]] = []

        def fake(method, params, timeout=None, **kw):
            seen.append(list(params["stock_list"]))
            # 每片里第一只命中
            first = params["stock_list"][0]
            return {"ErrorId": "0", first: _series("1")}

        monkeypatch.setattr(tqmod, "_rpc", fake)
        codes = [f"{i:06d}.SZ" for i in range(7)]
        out = tqmod.formula_scan("MACD买入", codes=codes, chunk=3)
        assert len(seen) == 3, "7 只 / 每片 3 只 = 3 片"
        assert out["scanned"] == 7
        assert out["hit_count"] == 3
        assert out["complete"] is True and out["chunks_failed"] == 0

    def test_分片失败必须标记不完整(self, monkeypatch):
        def fake(method, params, timeout=None, **kw):
            if params["stock_list"][0] == "000003.SZ":
                raise RuntimeError("网关炸了")
            return {"ErrorId": "0"}

        monkeypatch.setattr(tqmod, "_rpc", fake)
        codes = ["000000.SZ", "000001.SZ", "000002.SZ",
                 "000003.SZ", "000004.SZ", "000005.SZ"]
        out = tqmod.formula_scan("MACD买入", codes=codes, chunk=3)
        assert out["chunks_failed"] == 1
        assert out["complete"] is False, "有失败片绝不能说成完整"
        assert out["scanned"] == 3, "失败片不计入已扫描数"

    def test_代码池为空时complete为False(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", lambda *a, **kw: [])
        out = tqmod.formula_scan("MACD买入", codes=None)
        assert out["complete"] is False
        assert out["scanned"] == 0 and out["hits"] == []
        assert "error" in out

    def test_按日期扫描(self, monkeypatch):
        def fake(method, params, timeout=None, **kw):
            return {"ErrorId": "0", params["stock_list"][0]: _series("1", "0")}

        monkeypatch.setattr(tqmod, "_rpc", fake)
        out = tqmod.formula_scan("MACD买入", codes=["002361.SZ"], date="20260923")
        assert out["date"] == "20260923"
        assert out["hit_count"] == 0, "20260923 那行是 0, 不该命中"
