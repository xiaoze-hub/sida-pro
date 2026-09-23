"""涨停池口径钉子(2026-09-23 换通达信主源时立的)。

四条硬口径(改了就是 bug):
1. **连板数必须真的算出来** —— 此前东财路读错字段(`item.get("days")`, 实际是 `lbc`)导致
   连板股全塌成首板(实测 8 只: 新华文轩/大亚圣象真 4 板报 1 板), 主线"高度"维度与情绪周期
   同源失真。这里用纯函数从收盘价自算, 逐条钉死。
2. **单次 TQ 请求 ≤50 只** —— 事故铁律: 2026-09-23 向 17709 发单次 5576 只/269 板块请求
   把客户端压进假死态(整条链路 ConnectionReset, 生产 more-info 报"TQ 未连接"、l2 500、
   行情降级腾讯), 恢复只能整机重启。这条测试就是防它回归。
3. **东财按 tc 取全, 不截断** —— 09-21 实测 tc=103 只取 60 且按封板时间排序 ⇒ 系统丢尾盘涨停。
4. **算不出的字段不编造** —— 池子缺 sector/封单额时留空, 不填假值。
"""
from __future__ import annotations

from src.collectors.market_sentiment_collector import (
    _merge_ztpool,
    _parse_ztpool,
    MarketSentimentCollector,
)
from src.core.limit_up_calc import (
    build_pool_items,
    consecutive_boards,
    limit_up_ratio,
    raw_code,
)


class TestLimitUpRatio:
    def test_主板_10个点(self):
        assert limit_up_ratio("600519.SH") == 0.10
        assert limit_up_ratio("000001.SZ") == 0.10
        assert limit_up_ratio("002361.SZ") == 0.10

    def test_创业板科创板_20个点(self):
        assert limit_up_ratio("300750.SZ") == 0.20
        assert limit_up_ratio("301059.SZ") == 0.20  # 301 新号段
        assert limit_up_ratio("688318.SH") == 0.20
        assert limit_up_ratio("689009.SH") == 0.20

    def test_北交所_30个点(self):
        assert limit_up_ratio("430047.BJ") == 0.30
        assert limit_up_ratio("831010.BJ") == 0.30
        assert limit_up_ratio("920001.BJ") == 0.30

    def test_带后缀与纯代码都认(self):
        assert raw_code("002361.SZ") == "002361"
        assert raw_code("002361") == "002361"
        assert limit_up_ratio("002361") == 0.10


class TestConsecutiveBoards:
    """连板数 = 从末根往前数连续涨停的天数。"""

    def test_首板(self):
        # 10.00 → 11.00 (+10%) = 1 板
        assert consecutive_boards([9.5, 10.0, 11.0], "002361.SZ") == 1

    def test_三板(self):
        # 10.00 → 11.00 → 12.10 → 13.31 (每段约 +10%)
        assert consecutive_boards([10.0, 11.0, 12.1, 13.31], "002361.SZ") == 3

    def test_中间断板只算最近一段(self):
        # 首段涨停后被一根阴线打断, 末端两段涨停 ⇒ 2 板(不是 3)
        seq = [10.0, 11.0, 10.5, 11.55, 12.71]
        assert consecutive_boards(seq, "002361.SZ") == 2

    def test_未涨停_0(self):
        assert consecutive_boards([10.0, 10.2], "002361.SZ") == 0

    def test_创业板要_20个点(self):
        # +10% 在创业板不算涨停
        assert consecutive_boards([10.0, 11.0], "300750.SZ") == 0
        assert consecutive_boards([10.0, 12.0], "300750.SZ") == 1

    def test_数据不足不猜(self):
        assert consecutive_boards([], "002361.SZ") == 0
        assert consecutive_boards([10.0], "002361.SZ") == 0

    def test_含_None_不崩且跳过(self):
        assert consecutive_boards([10.0, None, 11.0], "002361.SZ") == 1

    def test_涨停价取整容差_不误判(self):
        # 3.87 的 10% 涨停价 = 4.26 ⇒ 4.26/3.87-1 = 10.08% ✅ 必须判到
        assert consecutive_boards([3.80, 3.87, 4.26], "000518.SZ") == 1


class TestBuildPoolItems:
    def test_组装与字段(self):
        bars = {
            "001234.SZ": {"Close": ["5.0", "5.5", "6.05"], "Amount": ["1000", "2000", "3000"]},
            "600519.SH": {"Close": ["1500", "1505", "1510"], "Amount": ["1", "2", "3"]},
        }
        pool = build_pool_items(bars, names={"001234.SZ": "泰慕士"})
        assert len(pool) == 1  # 茅台没涨停
        row = pool[0]
        assert row["code"] == "001234"
        assert row["name"] == "泰慕士"
        assert row["days"] == 2  # 5.0→5.5→6.05 两段
        assert row["source"] == "tq"
        assert row["amount"] == 3000 * 1e4  # 万元 → 元

    def test_算不出的字段不编造(self):
        bars = {"002361.SZ": {"Close": ["10.0", "11.0"]}}
        row = build_pool_items(bars)[0]
        for k in ("ltsz", "turnover_rate", "order_amount"):
            assert row[k] == 0.0
        assert row["sector"] == "" and row["first_time"] == ""


class TestTqChunkGuard:
    """事故铁律: 任何单次 TQ 请求 ≤50 只。"""

    def test_分片常量是_50(self):
        assert MarketSentimentCollector._TQ_CHUNK == 50

    def test_全A扫描的每次请求都不超过_50(self, monkeypatch):
        seen: list[int] = []

        codes = [f"{600000 + i}.SH" for i in range(137)]  # 137 只 → 必须切成 3 批(50/50/37)

        def fake_rpc(method, params, timeout=None):
            if method == "get_stock_list":
                return [{"Code": c, "Name": f"S{i}"} for i, c in enumerate(codes)]
            if method == "get_market_data":
                part = params.get("stock_list") or []
                seen.append(len(part))
                return {c: {"Close": ["10.0", "11.0"], "Amount": ["1", "1"]} for c in part}
            return {}

        import marketdata.vendors.tq as tqmod

        monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
        monkeypatch.setattr("src.core.tdx_boards.stock_blocks", lambda sym: [], raising=False)

        pool = MarketSentimentCollector()._limit_up_pool_tq()
        assert seen == [50, 50, 37]  # 分片正确
        assert max(seen) <= 50  # 铁律
        assert len(pool) == 137  # 全部涨停(序列两根都是 +10%)

    def test_连续两批失败即中止(self, monkeypatch):
        """绝不"压测式重试" —— 那正是把客户端压死的做法。"""
        calls = {"n": 0}

        def fake_rpc(method, params, timeout=None):
            if method == "get_stock_list":
                return [{"Code": f"{600000 + i}.SH", "Name": "S"} for i in range(200)]
            calls["n"] += 1
            raise RuntimeError("TQ down")

        import marketdata.vendors.tq as tqmod

        monkeypatch.setattr(tqmod, "tq_rpc", fake_rpc, raising=True)
        pool = MarketSentimentCollector()._limit_up_pool_tq()
        assert pool == []
        assert calls["n"] == 2  # 只试两批就放弃


class TestEastmoneyPagination:
    """东财兜底: 按 tc 取全 + lbc 映射正确。"""

    def test_连板数读_lbc_不是_days(self):
        payload = {"data": {"tc": 2, "pool": [
            {"c": "001234", "n": "泰慕士", "p": 6050, "zdp": 10.0, "lbc": 3, "amount": 1, "ltsz": 1, "hybk": "纺织服装"},
            {"c": "002238", "n": "天威视讯", "p": 7660, "zdp": 10.05, "lbc": 2, "amount": 1, "ltsz": 1, "hybk": "传媒"},
        ]}}
        rows, tc = _parse_ztpool(payload)
        assert tc == 2
        assert [r["days"] for r in rows] == [3, 2]  # 修 bug 前这里会是 [1, 1]
        assert rows[0]["source"] == "eastmoney"
        assert rows[0]["price"] == 6.05  # 分价 → 元

    def test_缺_lbc_时退化为_1_而不是崩(self):
        rows, _ = _parse_ztpool({"data": {"tc": 1, "pool": [{"c": "000001", "n": "X"}]}})
        assert rows[0]["days"] == 1

    def test_坏响应不抛错(self):
        assert _parse_ztpool(None) == ([], 0)
        assert _parse_ztpool({"data": {}}) == ([], 0)

    def test_翻页合并去重且保序(self):
        p1 = [{"code": "a"}, {"code": "b"}]
        p2 = [{"code": "b"}, {"code": "c"}]
        assert [r["code"] for r in _merge_ztpool(p1, p2)] == ["a", "b", "c"]
