"""交割单格式适配器(移植自 HKUDS/Vibe-Trading, MIT)。

每个 parser 把一种券商导出格式归一化为 TradeRecord 列表。支持:
同花顺 / 东方财富 / 富途 / generic CSV。

编码回退顺序(CSV): utf-8-sig → utf-8 → utf-16 → gbk → gb2312。
Excel (.xlsx/.xls) 经 openpyxl/xlrd 以 utf-8 内部读取。
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

# 券商 CSV/Excel 单元格常带货币代码或符号(Schwab/IBKR "$1,234.56"、CN "¥1000")。
# 逗号已剥离;不剥离这些 token 时 float() 失败并静默存 0.0。
_CURRENCY_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z])(?:USDT|USDC|USD|EUR|GBP|JPY|CNY|HKD)(?![A-Za-z])|[$€£¥￥]"
)

FormatName = str  # "tonghuashun" | "eastmoney" | "futu" | "generic" | "unknown"

_A_SHARE_EXCHANGE_MAP = {
    # 前缀 → 后缀:沪主板+科创板 / 深主板+中小板+创业板 / 北交所
    ("6",): ".SH",
    ("0", "3"): ".SZ",
    ("4", "8"): ".BJ",
}

_BUY_TOKENS = {
    "buy", "b", "purchase", "buy to cover", "buy-to-cover", "buy_to_cover",
    "买入", "证券买入", "融资买入", "做多", "long",
}
_SELL_TOKENS = {
    "sell", "s", "sell short", "sell-short", "sell_short",
    "卖出", "证券卖出", "融券卖出", "做空", "short",
}


@dataclass(frozen=True)
class TradeRecord:
    """标准化交易记录(不可变)。

    Attributes:
        datetime: ISO8601 时间戳,如 "2026-01-15 09:35:00"。
        symbol: 带交易所后缀的代码,如 "600519.SH" / "AAPL" / "00700.HK"。
        name: 可读证券名。
        side: "buy" 或 "sell"。
        quantity: 成交数量。
        price: 成交价。
        amount: 成交金额(quantity * price,未扣费)。
        fee: 总费用(佣金+印花税+过户费)。
        market: "china_a" / "us" / "hk" / "other"。
    """

    datetime: str
    symbol: str
    name: str
    side: str
    quantity: float
    price: float
    amount: float
    fee: float
    market: str


# ---------------- 文件加载 ----------------

_HEADER_KEYWORDS = ("证券代码", "证券名称", "股票代码", "发生日期", "成交日期", "成交时间", "成交均价", "业务名称", "操作")


def _find_header_row(raw: pd.DataFrame) -> int | None:
    """在 Excel 全表(header=None)中定位表头行。

    标准券商交割单前几行是营业部/股东/资金账户信息,表头行特征是
    同时包含多个关键列名(如 证券代码 + 证券名称 + 成交均价)。返回
    0-based 行号;找不到返回 None(调用方回退默认 header=0)。
    """
    for idx, row in raw.iterrows():
        cells = [str(v).strip() for v in row.tolist() if v is not None and str(v).strip()]  # type: ignore[arg-type]
        if not cells:
            continue
        hits = sum(1 for kw in _HEADER_KEYWORDS if any(c == kw for c in cells))
        if hits >= 2:
            return int(idx)
    return None


def _load_text_table(p: Path, ext: str) -> pd.DataFrame:
    """Excel/PDF 交割单 → DataFrame(统一文本行解析)。

    券商交割单(国投/同花顺等)Excel 有合并单元格展开、PDF 是空格对齐
    文本表格,两者列布局都可能错位。统一策略:
      1. 把每个数据行转成一行文本(Excel 逐单元格 join / PDF 逐行提取)
      2. 去掉 nan 占位与相邻重复(合并单元格展开)
      3. 按 10 列(表头顺序)对齐
    """
    col_names = ["发生日期", "证券代码", "证券名称", "业务名称", "成交均价", "成交数量", "成交金额", "股份余额", "净手续费", "印花税"]
    lines: list[str] = []

    if ext in {".xlsx", ".xls"}:
        raw = pd.read_excel(p, header=None, dtype=str)
        for _, r in raw.iterrows():
            cells = [str(v).strip() for v in r.tolist() if v is not None and str(v).strip()]  # type: ignore[union-attr]
            if cells:
                lines.append(" ".join(cells))
    else:  # pdf
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(str(p))
        for page in reader.pages:
            text = page.extract_text() or ""
            for ln in text.splitlines():
                s = ln.strip()
                if s:
                    lines.append(s)

    # 定位表头行
    header_idx = None
    for i, ln in enumerate(lines):
        if "发生日期" in ln and "证券代码" in ln:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("交割单未找到表头行(发生日期/证券代码)")

    rows: list[dict[str, str]] = []
    side_keywords = ("证券买入", "证券卖出", "融资买入", "融券卖出", "担保品买入", "担保品卖出")
    for ln in lines[header_idx + 1:]:
        if not ln.strip():
            continue
        if "营业部名" in ln or "股东姓名" in ln or "资金帐户" in ln or "制表日期" in ln:
            continue
        if "第" in ln and "页" in ln:
            continue
        if not ln[0].isdigit():
            continue  # 数据行必须以日期数字开头
        tokens = [x for x in ln.split() if x.lower() != "nan"]
        # 合并单元格展开会产生相邻重复(如 1700 1700),去重(保留第一个)
        deduped: list[str] = []
        for x in tokens:
            if deduped and x == deduped[-1]:
                continue
            deduped.append(x)
        if len(deduped) < 9:
            continue
        # 用业务名称关键词定位:它前面是 [日期, 代码, 名称(可变token数)],
        # 后面固定 6 个数字列(均价/数量/金额/余额/手续费/印花税)
        side_idx = -1
        for i, x in enumerate(deduped):
            if x in side_keywords:
                side_idx = i
                break
        if side_idx < 0 or side_idx < 2:
            continue
        tail = deduped[side_idx + 1:]
        if len(tail) < 6:
            continue
        row: dict[str, str] = {
            "发生日期": deduped[0],
            "证券代码": deduped[1],
            "证券名称": "".join(deduped[2:side_idx]),
            "业务名称": deduped[side_idx],
            "成交均价": tail[0],
            "成交数量": tail[1],
            "成交金额": tail[2],
            "股份余额": tail[3],
            "净手续费": tail[4],
            "印花税": tail[5],
        }
        rows.append(row)

    if not rows:
        raise ValueError("交割单未提取到数据行")
    return pd.DataFrame(rows)


def load_dataframe(path: str | Path) -> pd.DataFrame:
    """加载 CSV/Excel 为 DataFrame,带编码回退。

    Args:
        path: 文件路径(.csv/.xlsx/.xls)。

    Returns:
        保留原始列名的 DataFrame(未归一化)。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 不支持的扩展名或所有编码都失败。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")

    ext = p.suffix.lower()
    if ext in {".xlsx", ".xls", ".pdf"}:
        # 统一走"文本行 → 去 nan 占位 → 10 列对齐"策略(券商交割单兼容:
        # Excel 合并单元格展开/PDF 文本表格, 列布局都可能错位)。
        return _load_text_table(p, ext)
    if ext != ".csv":
        raise ValueError(f"Unsupported extension: {ext}")

    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gbk", "gb2312"):
        try:
            return pd.read_csv(p, dtype=str, encoding=enc)
        except UnicodeDecodeError as exc:
            last_err = exc
    raise ValueError(f"Failed to decode CSV with utf-8/utf-16/gbk/gb2312: {last_err}")


# ---------------- 格式检测 ----------------

def detect_format(df: pd.DataFrame) -> FormatName:
    """按列名签名检测券商格式。

    Returns:
        格式标识;"unknown" 表示无匹配(调用方可试 GenericCSVParser)。
    """
    cols = set(df.columns.astype(str))

    if {"成交时间", "证券代码", "操作"}.issubset(cols):
        return "tonghuashun"
    if {"发生日期", "证券代码", "业务名称"}.issubset(cols):
        return "guotou"
    if {"买卖标志", "股票代码"}.issubset(cols) or {"买卖标志", "成交均价"}.issubset(cols):
        return "eastmoney"
    if {"Date", "Symbol", "Side"}.issubset(cols) or {"Date", "Symbol", "Direction"}.issubset(cols):
        return "futu"

    # Generic: 含时间/代码提示列即可
    lowered = {c.lower() for c in cols}
    if any(c in lowered for c in ("datetime", "time", "date")) and any(
        c in lowered for c in ("symbol", "ticker", "code")
    ):
        return "generic"
    return "unknown"


# ---------------- Parsers ----------------

def _normalize_side(raw: Any) -> str:
    """返回 ``buy`` 或 ``sell``(支持方向别名)。

    Raises:
        ValueError: 方向缺失或不受支持。
    """
    if raw is None or pd.isna(raw):
        raise ValueError("Trade side is required")
    s = str(raw).strip().lower()
    if not s:
        raise ValueError("Trade side is required")
    if s in _BUY_TOKENS:
        return "buy"
    if s in _SELL_TOKENS:
        return "sell"
    raise ValueError(f"Unsupported trade side: {raw!r}")


def _is_empty_code(raw: Any) -> bool:
    """None/NaN/空白证券代码 → True。"""
    if raw is None:
        return True
    try:
        if pd.isna(raw):
            return True
    except (TypeError, ValueError):
        pass
    return not str(raw).strip()


def _qualify_a_share(code: str) -> str:
    """给裸 A 股代码补 .SH/.SZ/.BJ 后缀。"""
    if _is_empty_code(code):
        raise ValueError("empty securities code")
    code = str(code).strip()
    # Excel/CSV 数字单元格会字符串化为 "600519.0"/科学计数 —— 不是交易所后缀。
    try:
        as_float = float(code)
        if as_float.is_integer() and abs(as_float) < 10_000_000:
            code = str(int(as_float))
    except (ValueError, OverflowError):
        pass
    code = code.zfill(6)
    if "." in code:
        return code.upper()
    first = code[0]
    for prefixes, suffix in _A_SHARE_EXCHANGE_MAP.items():
        if first in prefixes:
            return code + suffix
    return code


def _to_float(val: Any, default: float = 0.0) -> float:
    """安全转 float;失败返回 default。"""
    if val is None:
        return default
    try:
        s = str(val).strip().replace("\u2212", "-")
        s = _CURRENCY_TOKEN_RE.sub("", s).replace(",", "").strip()
        if not s:
            return default
        parsed = float(s)
        return parsed if math.isfinite(parsed) else default
    except (ValueError, TypeError):
        return default


def parse_guotou(df: pd.DataFrame) -> list[TradeRecord]:
    """解析国投证券(及同类标准券商)交割单。

    期望列: 发生日期, 证券代码, 证券名称, 业务名称, 成交均价,
    成交数量, 成交金额, 股份余额, 净手续费, 印花税。
    业务名称: 证券买入/证券卖出/融资买入/融券卖出。
    """
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        raw_code = row.get("证券代码", "")
        if _is_empty_code(raw_code):
            continue
        side_raw = str(row.get("业务名称", "")).strip()
        # 跳过非买卖操作行: 分页表头("业务名称"自身)、红股派息、指定交易等
        if side_raw not in _BUY_TOKENS and side_raw not in _SELL_TOKENS:
            continue
        qty = _to_float(row.get("成交数量"))
        price = _to_float(row.get("成交均价"))
        amount = _to_float(row.get("成交金额")) or qty * price
        fee = _to_float(row.get("净手续费")) + _to_float(row.get("印花税"))
        records.append(TradeRecord(
            datetime=_ths_datetime(row.get("发生日期", "")),
            symbol=_qualify_a_share(str(raw_code)),
            name=str(row.get("证券名称", "")).strip(),
            side=_normalize_side(side_raw),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        ))
    return records


def parse_tonghuashun(df: pd.DataFrame) -> list[TradeRecord]:
    """解析同花顺导出。

    期望列: 成交时间, 证券代码, 证券名称, 操作, 成交数量, 成交价格,
    成交金额, 手续费, 印花税, 过户费。
    """
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        raw_code = row.get("证券代码", "")
        if _is_empty_code(raw_code):
            continue
        qty = _to_float(row.get("成交数量"))
        price = _to_float(row.get("成交价格"))
        amount = _to_float(row.get("成交金额")) or qty * price
        fee = _to_float(row.get("手续费")) + _to_float(row.get("印花税")) + _to_float(row.get("过户费"))
        records.append(TradeRecord(
            datetime=_ths_datetime(row.get("成交时间", "")),
            symbol=_qualify_a_share(raw_code),
            name=str(row.get("证券名称", "")).strip(),
            side=_normalize_side(row.get("操作")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        ))
    return records


def _ths_datetime(val: Any) -> str:
    """归一化 成交时间;Excel 序列浮点 → ISO datetime。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if pd.api.types.is_number(val) and not isinstance(val, (bool,)):
        ts = pd.to_datetime(float(val), unit="D", origin="1899-12-30", errors="coerce")
        if pd.notna(ts):
            return ts.strftime("%Y-%m-%d %H:%M:%S")
    text = str(val).strip()
    if text and not any(ch in text for ch in "/-:"):
        try:
            serial = float(text)
        except ValueError:
            serial = None
        else:
            if 1.0 <= serial < 100_000.0:
                ts = pd.to_datetime(serial, unit="D", origin="1899-12-30", errors="coerce")
                if pd.notna(ts):
                    return ts.strftime("%Y-%m-%d %H:%M:%S")
    ts = pd.to_datetime(val, errors="coerce")
    if pd.notna(ts):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return text


def parse_eastmoney(df: pd.DataFrame) -> list[TradeRecord]:
    """解析东方财富导出。

    期望列: 成交日期 (YYYYMMDD), 成交时间 (HH:MM:SS), 股票代码,
    股票名称, 买卖标志 (B/S), 成交数量, 成交均价, 成交金额, 佣金, 印花税。
    """
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        raw_code = row.get("股票代码", "")
        if _is_empty_code(raw_code):
            continue
        raw_date = str(row.get("成交日期", "")).strip()
        try:
            as_float = float(raw_date)
            if as_float.is_integer() and 19_000_001 <= int(as_float) <= 21_001_231:
                raw_date = f"{int(as_float):08d}"
            elif 1.0 <= as_float < 100_000.0:
                ts = pd.to_datetime(as_float, unit="D", origin="1899-12-30", errors="coerce")
                if pd.notna(ts):
                    raw_date = ts.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
        raw_time = str(row.get("成交时间", "")).strip()
        if len(raw_date) == 8 and raw_date.isdigit():
            iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            iso_date = raw_date
        dt = f"{iso_date} {raw_time}".strip()
        qty = _to_float(row.get("成交数量"))
        price = _to_float(row.get("成交均价"))
        amount = _to_float(row.get("成交金额")) or qty * price
        fee = _to_float(row.get("佣金")) + _to_float(row.get("印花税"))
        records.append(TradeRecord(
            datetime=dt,
            symbol=_qualify_a_share(raw_code),
            name=str(row.get("股票名称", "")).strip(),
            side=_normalize_side(row.get("买卖标志")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market="china_a",
        ))
    return records


def _futu_market(symbol: str, market_hint: str) -> str:
    """从 symbol/市场列推断市场。"""
    hint = market_hint.strip().lower()
    if hint in {"hk", "us", "cn"}:
        return {"hk": "hk", "us": "us", "cn": "china_a"}[hint]
    if symbol.endswith(".HK"):
        return "hk"
    if symbol.isalpha() or "." not in symbol:
        return "us"
    return "other"


def _futu_datetime(date_val: Any, time_val: Any) -> str:
    """合并富途 Date+Time 单元格;Excel 序列浮点 → ISO datetime。"""
    if pd.api.types.is_number(date_val) and not isinstance(date_val, (bool,)):
        if not (isinstance(date_val, float) and pd.isna(date_val)):
            serial = float(date_val)
            frac = 0.0
            time_is_frac = False
            if pd.api.types.is_number(time_val) and not isinstance(time_val, (bool,)):
                if not (isinstance(time_val, float) and pd.isna(time_val)):
                    candidate = float(time_val)
                    if 0.0 <= candidate < 1.0:
                        frac = candidate
                        time_is_frac = True
            ts = pd.to_datetime(serial + frac, unit="D", origin="1899-12-30", errors="coerce")
            if pd.notna(ts):
                if time_is_frac or time_val is None or (
                    isinstance(time_val, float) and pd.isna(time_val)
                ):
                    return ts.strftime("%Y-%m-%d %H:%M:%S")
                return f"{ts.strftime('%Y-%m-%d')} {str(time_val).strip()}".strip()
    date_text = (
        ""
        if date_val is None or (isinstance(date_val, float) and pd.isna(date_val))
        else str(date_val).strip()
    )
    if date_text and not any(ch in date_text for ch in "/-:"):
        try:
            serial = float(date_text)
        except ValueError:
            serial = None
        else:
            if 1.0 <= serial < 100_000.0:
                frac = 0.0
                time_is_frac = False
                time_text = (
                    ""
                    if time_val is None or (isinstance(time_val, float) and pd.isna(time_val))
                    else str(time_val).strip()
                )
                if time_text and not any(ch in time_text for ch in "/-:"):
                    try:
                        candidate = float(time_text)
                    except ValueError:
                        candidate = None
                    else:
                        if 0.0 <= candidate < 1.0:
                            frac = candidate
                            time_is_frac = True
                ts = pd.to_datetime(serial + frac, unit="D", origin="1899-12-30", errors="coerce")
                if pd.notna(ts):
                    if time_is_frac or not time_text:
                        return ts.strftime("%Y-%m-%d %H:%M:%S")
                    return f"{ts.strftime('%Y-%m-%d')} {time_text}".strip()
    date = date_text
    time = "" if time_val is None or (isinstance(time_val, float) and pd.isna(time_val)) else str(time_val).strip()
    return f"{date} {time}".strip()


def parse_futu(df: pd.DataFrame) -> list[TradeRecord]:
    """解析富途导出(英文表头,HK+US 混合)。

    期望列: Date, Time, Symbol, Name, Side, Quantity, Price,
    Amount, Commission, Platform Fee, Market (可选)。
    """
    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        raw_symbol = row.get("Symbol", "")
        if _is_empty_code(raw_symbol):
            continue
        dt = _futu_datetime(row.get("Date", ""), row.get("Time", ""))
        symbol = str(raw_symbol).strip().upper()
        qty = _to_float(row.get("Quantity"))
        price = _to_float(row.get("Price"))
        amount = _to_float(row.get("Amount")) or qty * price
        fee = _to_float(row.get("Commission")) + _to_float(row.get("Platform Fee"))
        records.append(TradeRecord(
            datetime=dt,
            symbol=symbol,
            name=str(row.get("Name", "")).strip(),
            side=_normalize_side(row.get("Side") if "Side" in df.columns else row.get("Direction")),
            quantity=qty,
            price=price,
            amount=amount,
            fee=fee,
            market=_futu_market(symbol, str(row.get("Market", ""))),
        ))
    return records


def parse_generic(df: pd.DataFrame) -> list[TradeRecord]:
    """解析小写英文表头的通用 CSV。

    列名大小写不敏感匹配。期望(datetime (time/date+time), symbol
    (ticker/code), name, side (direction), quantity (qty/size), price,
    amount (value/notional), fee (commission)。
    """
    colmap: dict[str, str] = {}
    for col in df.columns:
        key = str(col).strip().lower()
        colmap[key] = col

    def pick(*names: str) -> str | None:
        for n in names:
            if n in colmap:
                return colmap[n]
        return None

    dt_col = pick("datetime", "time")
    date_col = pick("date")
    sym_col = pick("symbol", "ticker", "code")
    name_col = pick("name", "instrument")
    side_col = pick("side", "direction", "action")
    qty_col = pick("quantity", "qty", "size", "volume")
    price_col = pick("price")
    amount_col = pick("amount", "value", "notional")
    fee_col = pick("fee", "commission", "fees")

    if side_col is None:
        raise ValueError(
            "Generic trade journal requires a side, direction, or action column"
        )

    records: list[TradeRecord] = []
    for _, row in df.iterrows():
        if sym_col and _is_empty_code(row.get(sym_col)):
            continue
        if dt_col:
            dt = _generic_datetime_cell(row.get(dt_col, ""))
        elif date_col:
            dt = _generic_datetime_cell(row.get(date_col, ""))
        else:
            dt = ""
        symbol = str(row.get(sym_col, "")).strip() if sym_col else ""
        qty = _to_float(row.get(qty_col)) if qty_col else 0.0
        price = _to_float(row.get(price_col)) if price_col else 0.0
        amount = _to_float(row.get(amount_col)) if amount_col else qty * price
        fee = _to_float(row.get(fee_col)) if fee_col else 0.0
        market = _infer_market_from_symbol(symbol)
        records.append(TradeRecord(
            datetime=dt,
            symbol=symbol.upper(),
            name=str(row.get(name_col, "")).strip() if name_col else "",
            side=_normalize_side(row.get(side_col)),
            quantity=qty,
            price=price,
            amount=amount or qty * price,
            fee=fee,
            market=market,
        ))
    return records


def _generic_datetime_cell(val: Any) -> str:
    """归一化通用 datetime/date 单元格;Excel 序列 → ISO datetime。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if pd.api.types.is_number(val) and not isinstance(val, (bool,)):
        serial = float(val)
        if 1.0 <= serial < 100_000.0:
            ts = pd.to_datetime(serial, unit="D", origin="1899-12-30", errors="coerce")
            if pd.notna(ts):
                return ts.strftime("%Y-%m-%d %H:%M:%S")
    text = str(val).strip()
    if text and not any(ch in text for ch in "/-:"):
        try:
            serial = float(text)
        except ValueError:
            serial = None
        else:
            if 1.0 <= serial < 100_000.0:
                ts = pd.to_datetime(serial, unit="D", origin="1899-12-30", errors="coerce")
                if pd.notna(ts):
                    return ts.strftime("%Y-%m-%d %H:%M:%S")
    ts = pd.to_datetime(val, errors="coerce")
    if pd.notna(ts):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return text


def _infer_market_from_symbol(symbol: str) -> str:
    """尽力从代码串推断市场。"""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "hk"
    if s.endswith(".SH") or s.endswith(".SZ") or s.endswith(".BJ"):
        return "china_a"
    if ("-" in s or "/" in s) and any(quote in s for quote in ("USDT", "USDC", "BTC", "USD")):
        return "other"  # crypto 不在数智分析范围,归 other
    for quote in ("USDT", "USDC", "BUSD"):
        if len(s) > len(quote) and s.endswith(quote):
            base = s[: -len(quote)]
            if base.isalpha() and len(base) >= 2:
                return "other"
    if s.isalpha():
        return "us"
    return "other"


_PARSERS = {
    "tonghuashun": parse_tonghuashun,
    "guotou": parse_guotou,
    "eastmoney": parse_eastmoney,
    "futu": parse_futu,
    "generic": parse_generic,
}


def parse_file(path: str | Path) -> tuple[FormatName, list[TradeRecord]]:
    """端到端:加载文件 → 检测格式 → 解析。

    Returns:
        (format_name, records)。检测 unknown 但列看起来可解析时回退 generic;
        否则抛 ValueError。

    Raises:
        ValueError: 未知格式且无可用列。
    """
    df = load_dataframe(path)
    fmt = detect_format(df)
    if fmt == "unknown":
        try:
            records = parse_generic(df)
            if records and records[0].symbol:
                return "generic", records
        except Exception:
            pass
        raise ValueError(f"Unrecognized trade journal format. Columns: {list(df.columns)}")
    records = _PARSERS[fmt](df)
    # 跨页重复: 同一笔(datetime+symbol+side+qty+price)在 PDF 多页可能重复出现, 去重
    seen: set[tuple] = set()
    deduped: list[TradeRecord] = []
    for r in records:
        k = (r.datetime, r.symbol, r.side, r.quantity, r.price)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return fmt, deduped


def records_to_dataframe(records: list[TradeRecord]) -> pd.DataFrame:
    """记录 → 标准化 DataFrame(datetime 已解析)。"""
    if not records:
        return pd.DataFrame(columns=[f.name for f in TradeRecord.__dataclass_fields__.values()])
    df = pd.DataFrame([asdict(r) for r in records])
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df.sort_values("datetime").reset_index(drop=True)
