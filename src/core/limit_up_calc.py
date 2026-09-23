"""涨停判定与连板数计算(纯函数, 无 I/O —— 便于逐条钉口径)。

## 为什么单开这个模块

线上「涨停池」此前靠外部源(东财/wudao)直接给 `days`(连板数), 而东财那条路**读错了字段**
(`item.get("days")`, 实际字段是 `lbc`) ⇒ 连板数恒为 1: 实测 8 只连板股全塌成首板
(新华文轩 601811、大亚圣象 000910 真 4 板 → 报 1 板), 主线的"高度"维度与情绪周期同源失真。

改成**自己从日线收盘价算**(通达信 K 线, 不复权): 判据可解释、可单测、不依赖第三方字段命名。

## 口径

- 涨停 = `close / prev_close - 1 >= 阈值 - 容差`(阈值按板块: 主板 10% / 创业科创 20% / 北交所 30%)
- **连板数** = 从当日往回数, 连续涨停的天数(当日封板才算, 开板不回封不算 —— 与"涨停池"口径一致)
- 数据不足(新股/停牌/序列过短) ⇒ 0, **不猜**
- ⚠️ **已知缺口(诚实声明)**: ST 股涨停是 5%, 本模块没有 ST 名单 ⇒ 按板块阈值判**会漏掉 ST 涨停股**。
  兜底: 池子由「通达信 + 东财」**并集**构成, 东财那份含 ST ⇒ 不会整体丢失, 只是该股连板数可能来自
  东财(带 `source` 标记)。彻底解决需接入 ST 名单(客户端 `get_stock_info.IsSTGP`)。
"""
from __future__ import annotations

#: 各板块涨停幅度。键 = 代码前缀, 值 = 涨幅阈值。
_BOARD_LIMITS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("688", "689"), 0.20),  # 科创板
    (("300", "301", "302"), 0.20),  # 创业板(含 301/302 新号段)
    (("43", "83", "87", "92"), 0.30),  # 北交所(43x/83x/87x/92x)
    (("60", "00"), 0.10),  # 主板(沪 60x / 深 00x)
)
_DEFAULT_LIMIT = 0.10

#: 判停容差: 涨停价按分位取整(如 3.87 → 4.26 实为 10.08%), 也可能略低于名义值 ⇒ 留 0.5 个点。
_TOLERANCE = 0.005


def raw_code(code: str) -> str:
    """`002361.SZ` / `SZ.002361` → `002361`。"""
    return (code or "").strip().split(".")[0]


def limit_up_ratio(code: str) -> float:
    """按证券代码返回涨停幅度阈值(0.10 / 0.20 / 0.30)。"""
    c = raw_code(code)
    for prefixes, ratio in _BOARD_LIMITS:
        if c.startswith(prefixes):
            return ratio
    return _DEFAULT_LIMIT


def consecutive_boards(closes: list[float], code: str) -> int:
    """连板数: 从末根往前数连续涨停的天数(0 = 当日未封板)。

    例: [..., 10.0, 11.0, 12.1] 两段都涨停 ⇒ 2 板。
    """
    seq = [float(x) for x in (closes or []) if x is not None]
    if len(seq) < 2:
        return 0
    ratio = limit_up_ratio(code) - _TOLERANCE
    boards = 0
    for i in range(len(seq) - 1, 0, -1):
        prev_close, close = seq[i - 1], seq[i]
        if prev_close <= 0 or (close / prev_close - 1) < ratio:
            break
        boards += 1
    return boards


def is_limit_up(closes: list[float], code: str) -> bool:
    """序列末根是否为涨停(至少需 2 根收盘价)。"""
    return consecutive_boards(closes, code) >= 1


def _to_float_list(v) -> list[float]:
    if not isinstance(v, list):
        return []
    out: list[float] = []
    for x in v:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            out.append(None)  # type: ignore[arg-type]
    return out


def build_pool_items(
    bars: dict[str, dict],
    names: dict[str, str] | None = None,
) -> list[dict]:
    """通达信日线返回 `{code: {"Close": [...], "Amount": [...]}}` → 涨停池条目。

    只保留**当日封板**的; 字段与既有池子结构对齐; 算不出的(流通市值/封单额/首次涨停时间)
    **留空或 0**, 不编造 —— 由调用方按需补齐或标注缺失。
    """
    names = names or {}
    out: list[dict] = []
    for code, payload in (bars or {}).items():
        if not isinstance(payload, dict):
            continue
        closes = _to_float_list(payload.get("Close") or payload.get("close"))
        boards = consecutive_boards(closes, code)
        if boards <= 0:
            continue
        seq = [x for x in closes if x is not None]
        close, prev = seq[-1], seq[-2]
        amounts = _to_float_list(payload.get("Amount") or payload.get("amount"))
        amount = amounts[-1] if amounts and amounts[-1] is not None else 0.0
        pure = raw_code(code)
        out.append(
            {
                "code": pure,
                "name": names.get(code) or names.get(pure) or "",
                "price": round(close, 3),
                "pct": round((close / prev - 1) * 100, 3) if prev else 0.0,
                # 万元 → 元(与东财/wudao 口径统一: amount 为元)
                "amount": amount * 1e4,
                "ltsz": 0.0,
                "first_time": "",
                "last_time": "",
                "days": boards,
                "sector": "",
                "theme": "",
                "reason": "",
                "turnover_rate": 0.0,
                "order_amount": 0.0,
                "source": "tq",
            }
        )
    return out
