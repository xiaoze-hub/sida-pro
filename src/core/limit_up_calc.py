"""涨停判定与连板数计算(纯函数, 无 I/O —— 便于逐条钉口径)。

## 为什么单开这个模块

线上「涨停池」此前靠外部源(东财/wudao)直接给 `days`(连板数), 而东财那条路**读错了字段**
(`item.get("days")`, 实际字段是 `lbc`) ⇒ 连板数恒为 1: 实测 8 只连板股全塌成首板
(新华文轩 601811、大亚圣象 000910 真 4 板 → 报 1 板), 主线的"高度"维度与情绪周期同源失真。

改成**自己从日线收盘价算**(通达信 K 线, 不复权): 判据可解释、可单测、不依赖第三方字段命名。

## 口径

- 涨停 = `close / prev_close - 1 >= 阈值 - 容差` **或** 收盘价 == 该前收对应的涨停价
  (阈值按板块: 主板 10% / 创业科创 20% / 北交所 30%)
- **连板数** = 从当日往回数, 连续涨停的天数(当日封板才算, 开板不回封不算 —— 与"涨停池"口径一致)
- 数据不足(新股/停牌/序列过短) ⇒ 0, **不猜**
- **ST 按真实名称 + 真实交易日期判**(2026-11-09 核对规则后改): 沪深主板风险警示股
  (名称前缀 `ST`/`*ST`/`SST`/`S*ST`)的涨跌幅限制 **自 2026-07-06 起由 ±5% 改为 ±10%**
  (沪深北交易所 2026-04 公告, 7/6 实施; 创业板/科创板 ST 仍 20%、北交所 ST 仍 30%)。
  所以:
    · 2026-07-06 及以后 —— 主板 ST 与普通主板同 10%, 名称不再改变阈值;
    · 2026-07-06 之前 —— 主板 ST 必须按 5% 判(拿 10% 判会漏掉当年真实的 ST 涨停);
    · 日期未知 —— 按**现行**规则(10%), 不假设 ST 降档。
  名称缺失时按板块名义阈值判(不猜 ST), 由调用方负责传真实名称与 K 线日期。

⚠️ 与 `src/core/limit_rules.py` 的关系: 那份仍写着"主板 ST 5%"的旧口径(2026-09-06 落下,
早于新规核对), 服务于 K 线校验/回测等另一条链路, 本次不改(爆炸半径大), 需另开单对齐。
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: 各板块涨停幅度。键 = 代码前缀, 值 = 涨幅阈值。
_BOARD_LIMITS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("688", "689"), 0.20),  # 科创板
    (("300", "301", "302"), 0.20),  # 创业板(含 301/302 新号段)
    (("43", "83", "87", "92"), 0.30),  # 北交所(43x/83x/87x/92x)
    (("60", "00"), 0.10),  # 主板(沪 60x / 深 00x)
)
_DEFAULT_LIMIT = 0.10
#: 主板风险警示股(ST)涨跌幅: 2026-07-06 前 5%, 之后 10%(与主板普通股一致)
_ST_MAIN_LIMIT_OLD = 0.05
_ST_MAIN_10PCT_SINCE = "20260706"

#: 判停容差: 涨停价按分位取整(如 3.87 → 4.26 实为 10.08%), 也可能略低于名义值 ⇒ 留 0.5 个点。
_TOLERANCE = 0.005

#: ST 名称前缀: ST / *ST / SST / S*ST(大小写不敏感, 允许中间空格)。
_ST_PREFIX = re.compile(r"^S?\*?ST", re.IGNORECASE)


def raw_code(code: str) -> str:
    """`002361.SZ` / `SZ.002361` → `002361`。"""
    return (code or "").strip().split(".")[0]


def is_st_name(name: str) -> bool:
    """名称是否 ST 族(只看**前缀**, "海ST重工"这种名字里的 ST 不算)。"""
    n = re.sub(r"[\s\u3000]", "", str(name or "")).upper()
    return bool(_ST_PREFIX.match(n))


def _norm_ymd(value) -> str:
    """`2026-07-06` / `YYYYMMDD` / int → YYYYMMDD; 解析不了 → ""(不猜)。"""
    s = str(value if value is not None else "").strip()
    digits = "".join(ch for ch in s[:10] if ch.isdigit())
    if len(digits) < 8:
        return ""
    ymd = digits[:8]
    if not (ymd[:4].isdigit() and "1990" <= ymd[:4] <= "2100"):
        return ""
    if not (1 <= int(ymd[4:6]) <= 12 and 1 <= int(ymd[6:8]) <= 31):
        return ""
    return ymd


def limit_up_ratio(code: str, name: str = "", as_of=None) -> float:
    """涨停幅度阈值: 主板 0.10 / 创业科创 0.20 / 北交所 0.30; 主板 ST 按日期分档。

    `name` 传真实证券名称, `as_of` 传该根 K 线的交易日:
      · 主板 ST 在 2026-07-06 之前是 0.05, 之后与普通主板同为 0.10;
      · 创业/科创 ST 始终 0.20, 北交所 ST 始终 0.30(不受该新规影响);
      · `as_of` 缺失 → 按现行规则(0.10), 不假设 ST 降档。
    只看代码不算 ST(名称是唯一依据); 名称缺失时按板块名义阈值判。
    """
    c = raw_code(code)
    base = _DEFAULT_LIMIT
    for prefixes, ratio in _BOARD_LIMITS:
        if c.startswith(prefixes):
            base = ratio
            break
    if base == _DEFAULT_LIMIT and is_st_name(name):
        ymd = _norm_ymd(as_of)
        if ymd and ymd < _ST_MAIN_10PCT_SINCE:
            return _ST_MAIN_LIMIT_OLD
    return base


def limit_up_price(prev_close: float, ratio: float) -> float:
    """涨停价 = 前收 ×(1+幅度), **四舍五入到分**(A 股真实报价规则)。"""
    try:
        raw = Decimal(str(prev_close)) * (Decimal("1") + Decimal(str(ratio)))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    try:
        return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return 0.0


def _is_limit_hit(prev_close: float, close: float, ratio: float) -> bool:
    """单根涨幅是否涨停: 涨幅阈值(带容差) 或 收盘价恰为该前收的涨停价。"""
    if prev_close <= 0 or close is None:
        return False
    if (close / prev_close - 1) >= ratio - _TOLERANCE:
        return True
    target = limit_up_price(prev_close, ratio)
    return target > 0 and abs(close - target) <= 0.001


def consecutive_boards(
    closes: list[float],
    code: str,
    name: str = "",
    as_of=None,
    dates: list | None = None,
) -> int:
    """连板数: 从末根往前数连续涨停的天数(0 = 当日未封板)。

    例: [..., 10.0, 11.0, 12.1] 两段都涨停 ⇒ 2 板。
    `name` 参与 ST 判定; `dates`(与 closes 对齐的交易日)让每段用**当日**的阈值判:
    主板 ST 在 2026-07-06 前后是 5% / 10%, 跨新规的连板窗口必须逐段取规则。
    `dates` 缺失时退化为 `as_of`(整段同一规则), 再缺则按现行规则。
    """
    seq = [float(x) for x in (closes or []) if x is not None]
    if len(seq) < 2:
        return 0
    dseq = list(dates) if isinstance(dates, (list, tuple)) else []
    boards = 0
    for i in range(len(seq) - 1, 0, -1):
        prev_close, close = seq[i - 1], seq[i]
        day = dseq[i] if i < len(dseq) else as_of
        ratio = limit_up_ratio(code, name, as_of=day)
        if not _is_limit_hit(prev_close, close, ratio):
            break
        boards += 1
    return boards


def is_limit_up(
    closes: list[float],
    code: str,
    name: str = "",
    as_of=None,
    dates: list | None = None,
) -> bool:
    """序列末根是否为涨停(至少需 2 根收盘价)。"""
    return consecutive_boards(closes, code, name, as_of=as_of, dates=dates) >= 1


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
        pure = raw_code(code)
        name = names.get(code) or names.get(pure) or ""
        raw_dates = payload.get("Date") or payload.get("date")
        dates = list(raw_dates) if isinstance(raw_dates, (list, tuple)) else []
        last_day = dates[-1] if dates else None
        boards = consecutive_boards(closes, code, name, as_of=last_day, dates=dates)
        if boards <= 0:
            continue
        seq = [x for x in closes if x is not None]
        close, prev = seq[-1], seq[-2]
        amounts = _to_float_list(payload.get("Amount") or payload.get("amount"))
        amount = amounts[-1] if amounts and amounts[-1] is not None else 0.0
        out.append(
            {
                "code": pure,
                "name": name,
                "price": round(close, 3),
                "pct": round((close / prev - 1) * 100, 3) if prev else 0.0,
                # 万元 → 元(与东财/wudao 口径统一: amount 为元)
                "amount": amount * 1e4,
                "ltsz": 0.0,
                "first_time": "",
                "last_time": "",
                "days": boards,
                "limit_pct": round(limit_up_ratio(code, name, as_of=last_day) * 100, 2),
                "sector": "",
                "theme": "",
                "reason": "",
                "turnover_rate": 0.0,
                "order_amount": 0.0,
                "source": "tq",
            }
        )
    return out
