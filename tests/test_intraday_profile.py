"""分时画像：只算事实，不推断（2026-10-21）。"""
from src.core.intraday_profile import compute, parse_qt, render

QT_RAW = (
    'v_sz002050="51~三花智控~002050~32.92~33.03~33.03~195698~104787~90877~'
    '32.90~115~32.89~45~32.88~376~32.87~214~32.86~970~'
    '32.92~96~32.93~76~32.94~13~32.95~29~32.96~62~~20261021103403~'
    '-0.11~-0.33~33.33~32.66~32.92/195698/645782388~195698~64578~0.53~34.66~~'
    '33.33~32.66~2.03~1215.09~1385.28~4.32~36.33~29.73~1.54~1444~33.00~33.89~34.10~~~'
    '1.19~64578.2388~0.0000~0~   A~GP-A~-39.92~5.21~1.21~12.30~7.87~60.37~30.12~-7.81~-2.26~-13.32~'
    '3691044992~4208013935~72.34~-43.40~3691044992~~~-29.07~0.06~~CNY~0~~32.85~1129~";'
)


def test_parse_qt_fields():
    qt = parse_qt(QT_RAW)
    assert qt["name"] == "三花智控"
    assert qt["prev_close"] == 33.03
    assert qt["outer"] == 104787 and qt["inner"] == 90877
    assert qt["bids"][0] == (32.90, 115.0)
    assert qt["asks"][0] == (32.92, 96.0)
    assert qt["vol_ratio"] == 1.54


def _pts():
    # 09:30-09:59 放量下杀，10:00-10:29 缩量横盘
    pts = []
    for i in range(30):
        pts.append({"t": f"09{30+i:02d}" if 30 + i < 60 else f"10{30+i-60:02d}",
                    "price": 33.0 - i * 0.01, "avg": 33.0 - i * 0.005, "volume": 2000 if i < 10 else 300})
    return pts


def test_compute_metrics():
    m = compute(_pts(), parse_qt(QT_RAW))
    assert m["available"] is True
    assert m["open"] == 33.0
    assert m["high"] == 33.0 and m["low"] == 32.71
    assert m["buckets"]["开盘 30 分钟"] > 60, "开盘半小时应当量能占大头"
    assert m["tail"]["reached"] is False, "没到 14:30 不许给出尾盘数字"
    assert m["active_buy_pct"] is not None


def test_render_marks_missing_instead_of_guessing():
    m = compute(_pts(), {})  # 没有 qt：内外盘/五档缺失
    text = render(m)
    assert "内外盘：未取到" in text and "五档：未取到" in text
    assert "尚未到该时段" in text


def test_no_data_returns_explicit_reason():
    m = compute([], {})
    assert m["available"] is False
    assert "不做推断" in render(m)
