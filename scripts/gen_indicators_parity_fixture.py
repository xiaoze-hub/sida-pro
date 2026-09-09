"""生成前端指标 parity 夹具 (KI-037 收口)。

用后端 `src/core/indicators.py` 的口径, 对一组确定性 OHLC 序列算出
sma/ema/macd/rsi 期望值, 写进 `frontend/tests/fixtures/indicators_parity.json`,
供 `frontend/tests/lib/indicators-parity.test.ts` 逐值比对(容差 1e-9)。

重跑: `PYTHONUTF8=1 python scripts/gen_indicators_parity_fixture.py`
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import indicators as ind  # noqa: E402

OUT = ROOT / "frontend" / "tests" / "fixtures" / "indicators_parity.json"


def main() -> None:
    rng = random.Random(20260909)
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    price = 10.0
    for _ in range(120):
        price = max(1.0, price * (1 + rng.uniform(-0.03, 0.03)))
        closes.append(round(price, 4))
        highs.append(round(price * (1 + rng.uniform(0.0, 0.02)), 4))
        lows.append(round(price * (1 - rng.uniform(0.0, 0.02)), 4))
    bars = [(highs[i], lows[i], closes[i]) for i in range(len(closes))]

    def sma_series(values: list[float], period: int) -> list[float | None]:
        return [ind.sma(values[: i + 1], period) for i in range(len(values))]

    def rsi_series(values: list[float], period: int) -> list[float | None]:
        return [ind.rsi(values[: i + 1], period) for i in range(len(values))]

    macd = ind.macd(closes, 12, 26, 9)
    assert macd is not None
    dif, dea, hist = macd

    fixture = {
        "meta": {
            "generated_by": "scripts/gen_indicators_parity_fixture.py",
            "source": "src/core/indicators.py",
            "seed": 20260909,
            "tolerance": 1e-9,
            "caliber": {
                "sma": "最近 period 个的简单均值",
                "ema": "data[0] 播种, k=2/(period+1)",
                "macd": "DIF=EMA(12)-EMA(26); DEA=EMA(DIF,9); HIST=(DIF-DEA)*2",
                "rsi": "Cutler 简单均值(非 Wilder)",
            },
        },
        "closes": closes,
        "bars": [{"high": h, "low": l, "close": c} for h, l, c in bars],
        "expected": {
            "sma": {
                "5": sma_series(closes, 5),
                "10": sma_series(closes, 10),
                "20": sma_series(closes, 20),
                "60": sma_series(closes, 60),
            },
            "ema": {
                "12": ind.ema_series(closes, 12),
                "26": ind.ema_series(closes, 26),
            },
            "macd": {"dif": dif, "dea": dea, "hist": hist},
            "rsi6": rsi_series(closes, 6),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
