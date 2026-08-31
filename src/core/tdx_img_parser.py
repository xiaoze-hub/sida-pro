# -*- coding: utf-8 -*-
"""通达信 .img 十档盘口 + 委托队列解析器。

数据来源: 通达信客户端 L2 落盘 `.img`(十档盘口 TLV, 约 3 秒一帧快照)。
产出与 `src/core/tdx_tick_parser.py`(.tck)同构, 供盘口队列展示 / 托压单识别使用。

字段表(来源: SIDA 交接文档 2026-08-31「已解码」章节)
----------------------------------------------------
| 标签  | 含义                                   | 单位        |
|-------|----------------------------------------|-------------|
| 0T    | 时间                                   | HHMMSSmmm   |
| 04/05 | 买一价 / 买一量                        | 元 / 股     |
| 20-29 | 买 1-10 档价                           | 元          |
| 30-39 | 买 1-10 档量                           | 股          |
| 40-49 | 卖 1-10 档价                           | 元          |
| 50-59 | 卖 1-10 档量                           | 股          |
| 62/63 | 买 / 卖 委托笔数                       | 笔          |
| 64    | 委托队列(每笔挂单量, 被动形态, 无委托号)| 股          |

⚠️ 待校准(必须拿到一份真实 .img 样本后确认, 未校准前不得用于生产)
------------------------------------------------------------------
1. TLV 帧结构: 当前按 `1 字节 tag + 1 字节 len + payload` 扫描, 未确认
   是否带帧同步头 / 是否整帧定长 / 长度字段是否含 tag+len 自身字节。
2. 价格标度 `PRICE_SCALE`: 当前按 1e3 定点还原, 未确认(也可能是 1e2 或 f32/f64)。
3. 时间字段编码: 当前同时兼容 u32 HHMMSSmmm 与 epoch 秒两种, 按数值区间自动判定。
4. 委托队列(标签 64)的 payload 切分粒度: 当前按 u32 数组解析, 未确认是否为变长项。

上述假设全部集中在 `ImgFormat` / 模块常量里, 校准时只改这一处, 下游逻辑不动。
在拿到样本前, 本模块只保证**纯函数层**(TLV 扫描 / 十档装配 / 指标计算)正确,
`parse_img()` 对真实文件的解析结果必须显式标注"未校准"。

单位口径(项目硬约束): 价格=元, 成交量=股, 委托笔数=笔。
缺失一律返回 None, 由上层显式标注"无数据", 禁止推断或编造。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

# ---------------------------------------------------------------------------
# 格式假设(校准点集中区)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImgFormat:
    """.img TLV 帧格式参数。拿到真实样本后只改这里的字段。"""

    tag_size: int = 1          # 标签字节宽度
    len_size: int = 1          # 长度字段字节宽度
    endian: str = "<"          # 字节序(小端)
    len_includes_header: bool = False  # 长度字段是否把 tag+len 自身算进去
    sync: bytes = b""          # 帧同步头(未知, 空串表示不做同步)


# 价格定点标度(待校准)
PRICE_SCALE = 1000

# 标签定义
TAG_TIME = 0x0C        # 交接文档记作 "0T", 非合法 hex, 暂按 0x0C 兜底(待校准)
TAG_BID1_PRICE = 0x04
TAG_BID1_VOL = 0x05
BID_PRICE_TAGS: tuple[int, ...] = tuple(range(0x20, 0x2A))   # 20-29 买1-10 价
BID_VOL_TAGS: tuple[int, ...] = tuple(range(0x30, 0x3A))     # 30-39 买1-10 量
ASK_PRICE_TAGS: tuple[int, ...] = tuple(range(0x40, 0x4A))   # 40-49 卖1-10 价
ASK_VOL_TAGS: tuple[int, ...] = tuple(range(0x50, 0x5A))     # 50-59 卖1-10 量
TAG_BID_ORDERS = 0x62  # 买委托笔数
TAG_ASK_ORDERS = 0x63  # 卖委托笔数
TAG_QUEUE = 0x64       # 委托队列(每笔挂单量)

DEPTH = 10             # 十档

# epoch 秒下限: 真实 epoch 约 1.7e9(2023-12 起); u32 HHMMSSmmm 最大 235959999
EPOCH_FLOOR = 1_600_000_000

# 缺失值统一占位(上层渲染为"无数据")
MISSING = "无数据"


class ImgParseError(ValueError):
    """.img 解析失败(结构损坏 / 假设不匹配)。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ImgSnapshot:
    """.img 单帧十档盘口快照。

    价格单位=元, 量单位=股, 笔数单位=笔。任何字段缺失为 None。
    """

    t: Optional[str] = None                       # "HH:MM:SS"
    bid_prices: list[Optional[float]] = field(default_factory=list)   # 元, 买1→买10
    bid_vols: list[Optional[int]] = field(default_factory=list)       # 股
    ask_prices: list[Optional[float]] = field(default_factory=list)   # 元, 卖1→卖10
    ask_vols: list[Optional[int]] = field(default_factory=list)       # 股
    bid_orders: Optional[int] = None              # 买委托笔数
    ask_orders: Optional[int] = None              # 卖委托笔数
    queue: Optional[list[int]] = None             # 委托队列, 每笔挂单量(股)

    # ---- 派生指标(全部返回 None 表示无数据, 不编造) ----

    def best_bid(self) -> Optional[float]:
        return _first_valid(self.bid_prices)

    def best_ask(self) -> Optional[float]:
        return _first_valid(self.ask_prices)

    def spread(self) -> Optional[float]:
        """买卖价差(元)。任一档缺失 → None。"""
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return round(a - b, 6)

    def bid_pressure(self) -> Optional[float]:
        """买盘力量占比 = 买十档总量 / (买十档总量 + 卖十档总量), 0~1。

        总量为 0(无挂单) → None, 不返回 0.0 冒充"均衡"。
        """
        bid = sum(v for v in self.bid_vols if v is not None)
        ask = sum(v for v in self.ask_vols if v is not None)
        total = bid + ask
        if total <= 0:
            return None
        return round(bid / total, 6)

    def queue_imbalance(self) -> Optional[int]:
        """委托队列净挂单量(股) = 队列总量 - 卖一量。

        用于识别托单/压单: 队列里有大量挂单但卖一量很小 → 疑似压单。
        缺失队列或卖一时返回 None。
        """
        if not self.queue:
            return None
        ask1 = _first_valid(self.ask_vols)
        if ask1 is None:
            return None
        return int(sum(self.queue) - ask1)


def _first_valid(seq: Sequence[Optional[float]]) -> Optional[float]:
    """取序列里第一个非 None 值, 全空返回 None。"""
    for v in seq:
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# TLV 扫描
# ---------------------------------------------------------------------------


def iter_tlv(data: bytes, fmt: ImgFormat = ImgFormat()) -> Iterator[tuple[int, bytes]]:
    """按 `tag + len + payload` 顺序扫描 TLV 记录。

    Args:
        data: 一帧 .img 的原始字节
        fmt: 帧格式参数(见 ImgFormat)

    Yields:
        (tag: int, payload: bytes)

    Raises:
        ImgParseError: 长度字段越界(结构不匹配当前假设)
    """
    pos = 0
    n = len(data)
    header = fmt.tag_size + fmt.len_size
    # struct 用 '<'/'>', int.from_bytes 用 'little'/'big'
    byteorder = "little" if fmt.endian in ("<", "=") else "big"
    while pos + header <= n:
        tag_bytes = data[pos:pos + fmt.tag_size]
        len_bytes = data[pos + fmt.tag_size:pos + header]
        tag = int.from_bytes(tag_bytes, "big")
        size = int.from_bytes(len_bytes, byteorder)
        start = pos + header
        if fmt.len_includes_header:
            size -= header
        if size < 0:
            raise ImgParseError(f"TLV 长度字段异常: tag=0x{tag:02X} size={size}")
        end = start + size
        if end > n:
            raise ImgParseError(
                f"TLV 长度越界: tag=0x{tag:02X} size={size} 剩余={n - start}"
            )
        yield tag, data[start:end]
        pos = end


def _u32(payload: bytes, endian: str = "<") -> Optional[int]:
    """payload 按 u32 解析, 长度不足返回 None。"""
    if len(payload) < 4:
        return None
    return struct.unpack(endian + "I", payload[:4])[0]


def _i32(payload: bytes, endian: str = "<") -> Optional[int]:
    if len(payload) < 4:
        return None
    return struct.unpack(endian + "i", payload[:4])[0]


def _price(payload: bytes, endian: str = "<", scale: int = PRICE_SCALE) -> Optional[float]:
    """定点价格 → 元。payload 按 u32 解析后除以 scale。"""
    raw = _u32(payload, endian)
    if raw is None:
        return None
    return round(raw / scale, 6)


def _u32_array(payload: bytes, endian: str = "<") -> list[int]:
    """payload 按 u32 数组解析(不足 4 字节的尾巴丢弃)。"""
    count = len(payload) // 4
    if count == 0:
        return []
    return list(struct.unpack(endian + f"{count}I", payload[:count * 4]))


def _decode_time(payload: bytes, endian: str = "<") -> Optional[str]:
    """时间字段 → 'HH:MM:SS'。

    兼容两种编码(待校准):
      - u32 HHMMSSmmm(与 .tck 一致, 最大 235959999)
      - epoch 秒(>= 1.6e9)
    """
    raw = _u32(payload, endian)
    if raw is None:
        return None
    if raw >= EPOCH_FLOOR:
        import time as _time

        return _time.strftime("%H:%M:%S", _time.localtime(raw))
    if raw > 235_959_999:
        return None
    h = raw // 10_000_000
    m = (raw // 100_000) % 100
    s = (raw // 1_000) % 100
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 快照装配
# ---------------------------------------------------------------------------


def decode_snapshot(
    tlvs: Sequence[tuple[int, bytes]],
    fmt: ImgFormat = ImgFormat(),
    price_scale: int = PRICE_SCALE,
) -> ImgSnapshot:
    """把一帧的 TLV 序列装配成 ImgSnapshot。

    同一标签重复出现时**取第一次**的值(避免脏帧覆盖), 缺失档位保留 None。
    """
    raw: dict[int, bytes] = {}
    for tag, payload in tlvs:
        raw.setdefault(tag, payload)

    endian = fmt.endian
    snap = ImgSnapshot(t=_decode_time(raw.get(TAG_TIME, b""), endian))

    for tag in BID_PRICE_TAGS:
        snap.bid_prices.append(_price(raw[tag], endian, price_scale) if tag in raw else None)
    for tag in BID_VOL_TAGS:
        snap.bid_vols.append(_u32(raw[tag], endian) if tag in raw else None)
    for tag in ASK_PRICE_TAGS:
        snap.ask_prices.append(_price(raw[tag], endian, price_scale) if tag in raw else None)
    for tag in ASK_VOL_TAGS:
        snap.ask_vols.append(_u32(raw[tag], endian) if tag in raw else None)

    # 04/05(买一价/买一量)是独立标签, 不存在时用 20/30 档的值兜底, 都没有则 None
    if snap.bid_prices and snap.bid_prices[0] is None and TAG_BID1_PRICE in raw:
        snap.bid_prices[0] = _price(raw[TAG_BID1_PRICE], endian, price_scale)
    if snap.bid_vols and snap.bid_vols[0] is None and TAG_BID1_VOL in raw:
        snap.bid_vols[0] = _u32(raw[TAG_BID1_VOL], endian)

    if TAG_BID_ORDERS in raw:
        snap.bid_orders = _u32(raw[TAG_BID_ORDERS], endian)
    if TAG_ASK_ORDERS in raw:
        snap.ask_orders = _u32(raw[TAG_ASK_ORDERS], endian)
    if TAG_QUEUE in raw:
        q = _u32_array(raw[TAG_QUEUE], endian)
        snap.queue = q or None

    return snap


def parse_img(
    path: str | Path,
    fmt: ImgFormat = ImgFormat(),
    price_scale: int = PRICE_SCALE,
) -> list[ImgSnapshot]:
    """解析 .img 文件 → 快照列表。

    ⚠️ 未校准: 整文件未按帧切片(帧边界未知)。当前按"整个文件=一帧 TLV 流"
    处理, 只适用于单帧样本文件。多帧文件的切片规则需拿到真实样本后补充。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f".img 文件不存在: {p}")
    data = p.read_bytes()
    if not data:
        raise ImgParseError(f".img 文件为空: {p}")
    return [decode_snapshot(list(iter_tlv(data, fmt)), fmt, price_scale)]


# ---------------------------------------------------------------------------
# 同构输出(供前端 / API 使用)
# ---------------------------------------------------------------------------


def frames_from_img(
    snapshots: Sequence[ImgSnapshot],
    top_n: int = 10,
) -> list[dict]:
    """快照 → 与 .tck / 腾讯逐笔同构的 dict 列表。

    输出字段:
        t            "HH:MM:SS" 或 "无数据"
        bid/ask      [{price, vol, orders?}] 各 top_n 档, 缺失档 price/vol 为 "无数据"
        spread       买卖价差(元)或 "无数据"
        bid_pressure 买盘占比(0~1)或 "无数据"
        queue        委托队列(股)列表, 无队列时为 "无数据"
        queue_imb    队列净挂单量(股)或 "无数据"

    单位: 价格=元, 量=股, 笔数=笔。
    """
    out: list[dict] = []
    for s in snapshots:
        def _levels(prices: Sequence[Optional[float]],
                    vols: Sequence[Optional[int]]) -> list[dict]:
            levels = []
            for i in range(top_n):
                p = prices[i] if i < len(prices) else None
                v = vols[i] if i < len(vols) else None
                levels.append({
                    "price": p if p is not None else MISSING,
                    "vol": v if v is not None else MISSING,
                })
            return levels

        out.append({
            "t": s.t if s.t is not None else MISSING,
            "bid": _levels(s.bid_prices, s.bid_vols),
            "ask": _levels(s.ask_prices, s.ask_vols),
            "bid_orders": s.bid_orders if s.bid_orders is not None else MISSING,
            "ask_orders": s.ask_orders if s.ask_orders is not None else MISSING,
            "spread": s.spread() if s.spread() is not None else MISSING,
            "bid_pressure": s.bid_pressure() if s.bid_pressure() is not None else MISSING,
            "queue": list(s.queue) if s.queue else MISSING,
            "queue_imb": s.queue_imbalance() if s.queue_imbalance() is not None else MISSING,
        })
    return out
