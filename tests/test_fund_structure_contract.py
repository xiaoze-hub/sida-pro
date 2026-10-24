"""四档资金结构端点契约（2026-10-24 新增）。

为什么用静态契约而不是 TestClient：
  该端点只读 `exday_daily`（TQ 每日 15:45 落库），起 TestClient 要拖起整个 DB 夹具；
  这里要锁的是**口径与诚实性契约**，不是 SQL 语法 —— 那几点在源码里就是可直接断言的。
  （口径错误是本项目最贵的 bug：`DarkFundTop` 曾把 `thsdk_dde` 标成 `eastmoney4`。）
"""
import pathlib
import re

SRC = pathlib.Path("src/web/api/market_data.py").read_text(encoding="utf-8")


def _block() -> str:
    i = SRC.find('@router.get("/fund-structure/{symbol}")')
    assert i > 0, "四档资金结构端点不存在"
    return SRC[i : i + 6000]


def test_route_registered():
    assert '@router.get("/fund-structure/{symbol}")' in SRC


def test_caliber_is_tdx_exday4_not_eastmoney4():
    b = _block()
    assert '"caliber": "tdx_exday4"' in b, "必须声明 tdx_exday4 口径"
    # 口径红线：不得退化成东财四档（两者不可换算）
    assert '"caliber": "eastmoney4"' not in b
    assert '"caliber": "ths"' not in b


def test_thresholds_flagged_as_inferred():
    """TQ 不返回档位阈值 —— 界面/接口都必须标成推断值，不能写成精确分档。"""
    b = _block()
    assert "tier_thresholds_inferred" in b, "接口必须显式声明阈值是推断值"
    assert '"thresholds_hint": EXDAY4_THRESHOLDS_HINT' in b
    # 文案常量在路由定义之前，故对全文断言
    assert "推断值" in SRC


def test_honest_when_table_empty():
    """表空时要如实说「无记录 + 为什么」，不许返回假数据或静默 0。"""
    b = _block()
    assert "hint" in b
    assert "历史不可回补" in b, "必须说清 TQ 只返当日 1 行、历史补不了"
    assert '"days": 0' in b


def test_reads_table_not_live_rpc():
    """必须读 exday_daily 表（历史序列），不能退回 TQ 当日实时 RPC（那只有 1 行）。"""
    b = _block()
    assert "FROM exday_daily" in b
    assert "exday_latest" not in b, "当日 RPC 只有 1 行，回答不了趋势问题"


def test_refuses_cross_caliber_math():
    b = _block()
    assert "EXDAY4_CALIBER_NOTE" in b, "必须把口径说明挂进返回体"
    assert "不可换算" in SRC
    assert re.search(r"禁止跨口径", SRC), "必须写明禁止跨口径相加"


def test_collector_targets_full_market():
    """采集器股票池必须是全市场日线覆盖，不能退化成只有自选股。"""
    c = pathlib.Path("src/collectors/exday_daily.py").read_text(encoding="utf-8")
    assert "FROM klines WHERE period='1d'" in c
    assert "get_stock_list" in c
