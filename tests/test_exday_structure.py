"""四档资金结构（exday）解析与因子：用真网关实测值当 fixture 钉死口径。

fixture 来源：2026-10-23 生产容器内对 `172.27.16.1:17709` 的实盘取数（盘中），
三条恒等式已交叉验证（买=卖=成交额、主动买+主动卖=成交额、Σ主动净额=get_more_info.Zjl）。
"""
from src.core.exday_structure import (
    CALIBER,
    TIERS,
    compute,
    parse_matrix,
    to_row,
    _conclusions,
)

# ── 600519 贵州茅台 2026-10-23（成交额 76.77 亿，Zjl=61,740.19 万）────────────
MAOTAI_AMO = [
    [1505502080.0, 2032773248.0, 878273472.0, 986522304.0],
    [1498899584.0, 1146054784.0, 812249472.0, 652102720.0],
    [4672200192.0, 4497159680.0, 2456402176.0, 1890591232.0],
    [153544.33, 921265.94, 0.0, 307088.66],
]
MAOTAI_VOL = [
    [11322.59, 15284.46, 6599.27, 7421.87],
    [11261.45, 8607.89, 6105.48, 4897.54],
    [35097.27, 33783.19, 18451.42, 14202.28],
    [1.15, 6.92, 0.0, 2.31],
]
MAOTAI_ORDER = dict(b_order=86001.93, s_order=88623.93, total_b_order=1777.0,
                    total_s_order=7652.0, b_cancel=26539.0, s_cancel=23286.0, cjbs=45237.0)

# ── 002361 神剑股份 2026-10-23（成交额 5.48 亿，Zjl=452.68 万）───────────────
SHENJIAN_AMO = [
    [13394046.0, 15871177.0, 10620975.0, 10489445.0],
    [59253856.0, 66959268.0, 39645064.0, 34493504.0],
    [163622192.0, 182288352.0, 89593240.0, 95413400.0],
    [311745888.0, 282897152.0, 136395680.0, 131331792.0],
]
SHENJIAN_VOL = [
    [15541.39, 18475.42, 12299.86, 12133.30],
    [68738.28, 78501.30, 46047.61, 40448.61],
    [191314.08, 213479.47, 104356.63, 112120.91],
    [364998.38, 330135.94, 159167.89, 153978.88],
]
SHENJIAN_ORDER = dict(b_order=1005818.75, s_order=990056.75, total_b_order=27903.0,
                      total_s_order=49328.0, b_cancel=337298.0, s_cancel=300111.0, cjbs=77398.0)


def _maotai(**kw):
    return compute(MAOTAI_AMO, MAOTAI_VOL, **{**MAOTAI_ORDER, **kw})


def _shenjian(**kw):
    return compute(SHENJIAN_AMO, SHENJIAN_VOL, **{**SHENJIAN_ORDER, **kw})


# ──────────────────────── 1. 矩阵标定（三条恒等式） ────────────────────────

def test_buy_equals_sell_equals_turnover():
    """Σ买入额 == Σ卖出额（每笔成交同时计买卖双方）—— 口径错位时这条先红。"""
    for amo, expected_yi in ((MAOTAI_AMO, 76.77), (SHENJIAN_AMO, 5.48)):
        buy = sum(r[0] for r in amo)
        sell = sum(r[1] for r in amo)
        assert abs(buy - sell) / buy < 0.001, "买卖合计必须相等"
        assert abs(buy / 1e8 - expected_yi) < 0.05


def test_active_plus_passive_equals_turnover():
    """Σ主动买 + Σ主动卖 == 成交额（列语义：0/1=买/卖额，2/3=主动买/卖额）。"""
    for amo in (MAOTAI_AMO, SHENJIAN_AMO):
        amount = sum(r[0] for r in amo)
        act = sum(r[2] for r in amo) + sum(r[3] for r in amo)
        assert abs(act - amount) / amount < 0.001


def test_active_net_equals_zjl():
    """Σ(主动买−主动卖) == get_more_info.Zjl（外盘−内盘）—— 列序的终审。"""
    maotai = _maotai()
    shenjian = _shenjian()
    assert abs(maotai["active_buy_amt"] - maotai["active_sell_amt"] - 617401900) / 617401900 < 0.01
    assert abs(shenjian["active_buy_amt"] - shenjian["active_sell_amt"] - 4526800) / 4526800 < 0.01


def test_tier_order_is_xl_first():
    """行序 = 特大→小：茅台的"小单"结构上为 0（1 手 = 13.35 万 > 小单阈值）。"""
    maotai = _maotai()
    shenjian = _shenjian()
    assert maotai["factors"]["retail_ratio"] < 0.1, "茅台不可能有小单"
    assert shenjian["factors"]["retail_ratio"] > 50, "神剑是小单主导的妖股"
    assert maotai["tiers"]["xl"]["buy_share"] > shenjian["tiers"]["xl"]["buy_share"]


# ──────────────────────── 2. 结构因子 ────────────────────────

def test_main_active_net_only_counts_xl_and_lg():
    m = _maotai()
    expect = ((878273472.0 - 986522304.0) + (812249472.0 - 652102720.0))
    assert abs(m["factors"]["main_active_net"] - expect) < 1.0
    # 全档主动净额（=Zjl）必须大于主力口径（中单也是正的）
    assert m["active_buy_amt"] - m["active_sell_amt"] > m["factors"]["main_active_net"]


def test_retail_and_xl_share_are_percentages():
    m = _shenjian()
    assert 0 <= m["factors"]["retail_ratio"] <= 100
    assert 0 <= m["factors"]["xl_share"] <= 100
    assert m["factors"]["retail_ratio"] + m["factors"]["xl_share"] <= 100


def test_chip_migration_sign():
    m = _shenjian()
    f = m["factors"]
    assert abs(f["chip_migration"] - (f["main_active_net"] - f["sm_active_net"])) < 1.0


# ──────────────────────── 3. 委托粒度（拆单代理） ────────────────────────

def test_granularity_matches_measured_values():
    """实测（茅台）：买侧 48.4 手/笔、卖侧 11.6 手/笔、加权 18.5 手/笔；
    笔均成交 1.28 手 → 加权隐匿度 14.5×、买侧 37.9×、笔数比 0.21。"""
    g = _maotai()["granularity"]
    assert g["available"] is True
    assert abs(g["avg_order_lots_buy"] - 48.4) < 0.5
    assert abs(g["avg_order_lots_sell"] - 11.6) < 0.5
    assert abs(g["avg_order_lots"] - 18.5) < 0.5
    assert abs(g["gran_hiding"] - 14.5) < 0.6
    assert g["gran_hiding_buy"] > g["gran_hiding_sell"], "买侧拆得更细 → 买侧隐匿度更高"
    assert abs(g["order_fill_ratio"] - 0.209) < 0.01
    assert abs(g["cancel_buy_rate"] - 30.9) < 0.5


def test_granularity_absent_without_order_fields():
    g = compute(MAOTAI_AMO, MAOTAI_VOL)["granularity"]
    assert g == {"available": False}


def test_passive_split_tag_on_maotai():
    """委托/成交笔数比 0.21 + 隐匿度 38 → 必须命中"被动拆单结构"。"""
    tags = {t["code"] for t in _maotai()["tags"]}
    assert "passive_split" in tags


def test_conclusions_are_derivable_from_numbers():
    """每条判语都要能从数字复算，不许凭空下结论。"""
    m = _maotai()
    for tag in m["tags"]:
        assert tag["basis"], f"{tag['code']} 没有依据"
    # 人工把撤单率调高、主动买调低 → 应命中虚假托盘
    fake = dict(MAOTAI_ORDER)
    fake.update(b_cancel=60000.0, s_cancel=1000.0)
    amo = [list(r) for r in MAOTAI_AMO]
    for r in amo:  # 把主动买压到 30% 成交额
        r[2] = r[0] * 0.3
        r[3] = r[0] * 0.7
    tags = {t["code"] for t in _conclusions(compute(amo, MAOTAI_VOL, **fake))}
    assert "fake_bid" in tags


# ──────────────────────── 4. 质量判定与降级 ────────────────────────

def test_quality_ok_when_identities_hold():
    assert _maotai()["quality"] == "ok"


def test_quality_suspect_when_buy_sell_skew():
    amo = [list(r) for r in MAOTAI_AMO]
    for r in amo:
        r[1] = r[1] * 0.8  # 卖出行被篡改 20%
    m = compute(amo, MAOTAI_VOL)
    assert m["quality"] == "suspect"
    assert "买卖合计不等" in m["reason"]


def test_quality_suspect_when_turnover_mismatch():
    m = compute(MAOTAI_AMO, MAOTAI_VOL, kline_amount=50e8)  # 日线说 50 亿，矩阵说 76.77 亿
    assert m["quality"] == "suspect"
    assert "偏差" in m["reason"]


def test_bad_matrix_shape_is_rejected_not_guessed():
    assert parse_matrix([[1, 2, 3]]) is None
    assert parse_matrix([[1, 2, 3, 4]] * 3) is None
    assert parse_matrix([[1, 2, 3, 4], ["x", 1, 2, 3], [0, 0, 0, 0], [0, 0, 0, 0]]) is None
    assert compute(None)["available"] is False
    assert compute([[0, 0, 0, 0]] * 4)["available"] is False


# ──────────────────────── 5. 落库行 ────────────────────────

def test_to_row_flattens_and_keeps_none():
    row = to_row(_maotai(), symbol="600519", trade_date="2026-10-23")
    assert row["trade_date"] == "20261023"
    assert row["symbol"] == "600519"
    assert row["amount"] > 7e9
    assert row["xl_buy_amt"] == MAOTAI_AMO[0][0]
    assert row["sm_active_net"] == 0.0 or row["sm_active_net"] == -307088.66
    assert row["b_order"] == 86001.93
    assert row["gran_hiding"] and row["gran_hiding"] > 14
    assert row["quality"] == "ok"
    # 缺委托字段时这些列必须是 None，不能补 0
    row2 = to_row(compute(MAOTAI_AMO, MAOTAI_VOL), symbol="600519", trade_date="20261023")
    assert row2["b_order"] is None and row2["gran_hiding"] is None


def test_caliber_constant_is_documented_value():
    assert CALIBER == "tdx_exday4"
    assert TIERS == ("xl", "lg", "md", "sm")
