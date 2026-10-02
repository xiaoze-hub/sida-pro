"""TQ 公式**按需批量执行引擎**测试(`src/collectors/tq_formula_batch.py` + API 接线)。

CI 无 TQ 网关, 全部 monkeypatch 假源(同 `test_tq_formula_scan.py` 惯例)。

覆盖会导致**静默错误**的行为:
- 分片失败被当成 0 命中(而不是显式 incomplete + 缺哪些片)
- 公式不存在被退化成「扫描不完整」而非显式错误态
- ISO 日期未归一 → 网关静默返回 0 命中
- 非交易日仍打网关空跑
"""

from __future__ import annotations

from marketdata.vendors import tq as tqmod
from src.collectors import tq_formula_batch as batch

DAY = "20260924"          # 已知 A 股交易日(2026 秋)
HOLIDAY = "2026-10-01"    # 国庆法定休市


def _hit_res(code: str, *, value: str = "1", date: str = DAY) -> dict:
    """单只命中响应(与真实 `formula_process_mul_xg` 同形: {代码: {OUTPUT1: [{Date,Value}]}})。"""
    return {code: {"OUTPUT1": [{"Date": date, "Value": value}]}}


def _pool(n: int) -> list[str]:
    return [f"{i:06d}.SZ" for i in range(1, n + 1)]


def _full_market(monkeypatch, codes: list[str]):
    """让 `stock_list` 返回全市场池(list[dict] 形态, 同真实网关)。"""
    monkeypatch.setattr(tqmod, "stock_list", lambda *a, **k: [{"Code": c} for c in codes])


# ─────────────────────────────────────────────────────────────────────────────
# 多公式批量命中
# ─────────────────────────────────────────────────────────────────────────────
class Test多公式批量:
    def test_两个公式各自命中并汇总(self, monkeypatch):
        pool = _pool(4)
        seen_formulas: list[str] = []

        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            seen_formulas.append(formula)
            return {"ErrorId": "0", **_hit_res(part[0])}  # 每片首只命中

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入", "KDJ买入"], trade_date=DAY, codes=pool)

        assert out["ok"] is True and out["complete"] is True
        assert out["trade_date"] == DAY and out["is_trading_day"] is True
        assert [f["formula"] for f in out["formulas"]] == ["MACD买入", "KDJ买入"]
        assert out["total_hits"] == 2
        assert all(f["hit_count"] == 1 and f["complete"] for f in out["formulas"])
        assert seen_formulas == ["MACD买入", "KDJ买入"]

    def test_全市场池由stock_list解析(self, monkeypatch):
        _full_market(monkeypatch, _pool(3))
        monkeypatch.setattr(tqmod, "formula_xg_mul",
                            lambda *a, **k: {"ErrorId": "0", **_hit_res(a[1][0])})
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY)
        assert out["pool_source"] == "full_market" and out["pool_size"] == 3
        assert out["total_hits"] == 1

    def test_公式参数与去重(self, monkeypatch):
        seen_args: list[str] = []

        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            seen_args.append(formula_arg)
            return {"ErrorId": "0", **_hit_res(part[0])}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        # 同一 (公式, 参数) 重复 → 只执行一次
        out = batch.run_formula_batch(
            [("UPN", "连涨3天", "3"), {"code": "UPN", "arg": "3"}, "DOWNN"],
            trade_date=DAY, codes=_pool(2),
        )
        assert [f["formula"] for f in out["formulas"]] == ["UPN", "DOWNN"]
        assert seen_args == ["3", ""]


# ─────────────────────────────────────────────────────────────────────────────
# 分片边界
# ─────────────────────────────────────────────────────────────────────────────
class Test分片边界:
    def test_默认分片500(self, monkeypatch):
        seen: list[int] = []

        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            seen.append(len(part))
            return {"ErrorId": "0"}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(1001))
        assert seen == [500, 500, 1], "1001 只 / 500 = 3 片"
        assert out["chunk"] == tqmod.TQ_SCAN_CHUNK == 500
        assert out["chunks_total"] == 3
        assert out["formulas"][0]["chunks_total"] == 3

    def test_指定分片50(self, monkeypatch):
        seen: list[int] = []

        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            seen.append(len(part))
            return {"ErrorId": "0"}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(120), chunk=50)
        assert seen == [50, 50, 20]
        assert out["chunks_total"] == 3

    def test_命中跨片合并(self, monkeypatch):
        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            return {"ErrorId": "0", **_hit_res(part[0])}  # 每片首只命中

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(7), chunk=3)
        f = out["formulas"][0]
        assert f["hit_count"] == 3 and f["scanned"] == 7
        assert sorted(h["symbol"] for h in f["hits"]) == ["000001.SZ", "000004.SZ", "000007.SZ"]


# ─────────────────────────────────────────────────────────────────────────────
# 分片失败注入
# ─────────────────────────────────────────────────────────────────────────────
class Test分片失败:
    def test_失败片显式不完整并列出缺失片(self, monkeypatch):
        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            if part[0] == "000004.SZ":       # 第 2 片(chunk=3, 起始下标 3)炸
                raise RuntimeError("网关炸了")
            return {"ErrorId": "0", **_hit_res(part[0])}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(9), chunk=3)

        assert out["complete"] is False
        assert out["incomplete_formulas"] == ["MACD买入"]
        f = out["formulas"][0]
        assert f["complete"] is False and f["chunks_failed"] == 1
        assert f["chunks_total"] == 3
        # 缺失片**列出来**, 不是只有一个计数
        assert f["missing_chunks"] == [{
            "index": 1, "start_index": 3, "count": 3, "error": "网关炸了",
        }]
        # 失败片不计入已扫描, 但其命中也不被静默补 0
        assert f["scanned"] == 6
        assert f["hit_count"] == 2, "成功片的命中照常计入"
        assert all(h["symbol"] != "000004.SZ" for h in f["hits"])

    def test_一个公式失败不拖垮另一个(self, monkeypatch):
        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            if formula == "坏公式":
                raise RuntimeError("炸")
            return {"ErrorId": "0", **_hit_res(part[0])}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["坏公式", "好公式"], trade_date=DAY, codes=_pool(3))
        by = {f["formula"]: f for f in out["formulas"]}
        assert by["坏公式"]["complete"] is False
        assert by["好公式"]["complete"] is True and by["好公式"]["hit_count"] == 1
        # 顶层因任一不完整而为 False, 且列出具体公式
        assert out["complete"] is False
        assert out["incomplete_formulas"] == ["坏公式"]

    def test_全片失败仍不是0命中而是不完整(self, monkeypatch):
        monkeypatch.setattr(tqmod, "formula_xg_mul",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("隧道断")))
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(4), chunk=2)
        f = out["formulas"][0]
        assert f["scanned"] == 0 and f["hit_count"] == 0
        assert f["complete"] is False and f["chunks_failed"] == 2
        assert out["complete"] is False and "不是全市场口径" in out["note"]


# ─────────────────────────────────────────────────────────────────────────────
# 公式不存在错误态
# ─────────────────────────────────────────────────────────────────────────────
class Test公式不存在:
    def test_首片报公式不存在即显式错误且不再空扫(self, monkeypatch):
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            raise RuntimeError("TQ formula_process_mul_xg ErrorId=9: 获取公式失败或公式不存在")

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["不存在的公式"], trade_date=DAY, codes=_pool(9), chunk=3)
        f = out["formulas"][0]
        assert f["error"] == "formula_not_found"
        assert f["complete"] is False
        assert calls["n"] == 1, "已判定公式不存在, 不应继续空扫其余片"
        assert out["errored_formulas"] == ["不存在的公式"]

    def test_非公式类失败不退化成公式不存在(self, monkeypatch):
        def fake(*a, **k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(2))
        f = out["formulas"][0]
        assert f["error"] is None
        assert f["complete"] is False and f["chunks_failed"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 非交易日
# ─────────────────────────────────────────────────────────────────────────────
class Test非交易日:
    def test_节假日不扫且不打网关(self, monkeypatch):
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return {"ErrorId": "0"}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        _full_market(monkeypatch, _pool(3))  # 即便有池也不该走到取数
        out = batch.run_formula_batch(["MACD买入"], trade_date=HOLIDAY)

        assert out["ok"] is False and out["skipped"] is True
        assert out["is_trading_day"] is False
        assert out["trade_date"] == "20261001", "ISO 日期应先归一"
        assert out["formulas"] == []
        assert "不是 A 股交易日" in out["note"]
        assert calls["n"] == 0, "非交易日绝不打网关"

    def test_周末不扫(self, monkeypatch):
        monkeypatch.setattr(tqmod, "formula_xg_mul",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该被调用")))
        out = batch.run_formula_batch(["MACD买入"], trade_date="2026-09-26")  # 周六
        assert out["skipped"] is True and out["is_trading_day"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 日期归一 / 无数据 / 空输入
# ─────────────────────────────────────────────────────────────────────────────
class Test日期与边界:
    def test_ISO日期归一到紧凑格式(self, monkeypatch):
        seen: dict = {}

        def fake(formula, part, *, formula_arg="", start_time="", end_time=""):
            seen["start"] = start_time
            seen["end"] = end_time
            return {"ErrorId": "0", **_hit_res(part[0])}

        monkeypatch.setattr(tqmod, "formula_xg_mul", fake)
        out = batch.run_formula_batch(["MACD买入"], trade_date="2026-09-24", codes=["000001.SZ"])
        assert seen["end"] == "20260924", "网关只认紧凑格式, ISO 会静默 0 命中"
        assert len(seen["start"]) == 8 and seen["start"].isdigit()
        assert out["trade_date"] == "20260924"

    def test_无数据行时date_has_data为false(self, monkeypatch):
        # 网关返回成功但没有目标日的数据行 → 不是「0 家触发」, 而是「该日无数据」
        monkeypatch.setattr(tqmod, "formula_xg_mul", lambda *a, **k: {"ErrorId": "0"})
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY, codes=_pool(2))
        f = out["formulas"][0]
        assert f["complete"] is True and f["hit_count"] == 0
        assert f["date_rows"] == 0 and f["date_has_data"] is False

    def test_空公式集(self):
        out = batch.run_formula_batch([], trade_date=DAY)
        assert out["ok"] is False and out["error"] == "no_formulas"

    def test_空标的池(self, monkeypatch):
        _full_market(monkeypatch, [])
        out = batch.run_formula_batch(["MACD买入"], trade_date=DAY)
        assert out["ok"] is False and out["error"] == "empty_pool"

    def test_超公式数上限(self, monkeypatch):
        monkeypatch.setattr(tqmod, "formula_xg_mul", lambda *a, **k: {"ErrorId": "0"})
        formulas = [f"F{i}" for i in range(batch.MAX_BATCH_FORMULAS + 1)]
        out = batch.run_formula_batch(formulas, trade_date=DAY, codes=_pool(2))
        assert out["ok"] is False and out["error"] == "too_many_formulas"


# ─────────────────────────────────────────────────────────────────────────────
# API 接线
# ─────────────────────────────────────────────────────────────────────────────
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import formula_signals as fs

    app = FastAPI()
    app.include_router(fs.router, prefix="/api/formula-signals")
    return TestClient(app)


class TestBatchAPI:
    def test_batch透传并截断展示(self, monkeypatch):
        canned = {
            "ok": True, "trade_date": DAY, "complete": True,
            "formulas": [{"formula": "MACD买入", "complete": True, "hit_count": 3,
                          "hits": [{"symbol": "1"}, {"symbol": "2"}, {"symbol": "3"}]}],
        }
        monkeypatch.setattr(batch, "run_formula_batch", lambda *a, **k: dict(canned, formulas=[
            dict(f) for f in canned["formulas"]
        ]))
        r = _client().get("/api/formula-signals/batch", params={"formulas": "MACD买入:3", "limit": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["formulas"][0]["hits"] == [{"symbol": "1"}, {"symbol": "2"}]
        assert body["formulas"][0]["hits_truncated"] is True
        assert body["formulas"][0]["hit_count"] == 3, "截断只影响展示, 不改计数"

    def test_batch公式数超限400(self):
        formulas = ",".join(f"F{i}" for i in range(batch.MAX_BATCH_FORMULAS + 1))
        r = _client().get("/api/formula-signals/batch", params={"formulas": formulas})
        assert r.status_code == 400

    def test_batch空公式400(self):
        r = _client().get("/api/formula-signals/batch", params={"formulas": ""})
        assert r.status_code == 400
