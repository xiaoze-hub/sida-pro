# -*- coding: utf-8 -*-
"""通达信 .img 十档盘口 + 委托队列解析器(**真实样本校准版**, 2026-10-02)。

数据来源: 通达信客户端 L2 落盘 `.img`(十档盘口, 约 3 秒一帧快照)。
产出与 `src/core/tdx_tick_parser.py`(.tck)同构, 供盘口队列展示 / 托压单识别使用。

=====================================================================
校准说明(基于真实样本逐条实测, 非人工结论)
=====================================================================
样本: `sz002361_20260827.img`(368511B) / `sz002361_20260828.img`(374898B)。
校准入口: `calibrate_from_sample(path)` 直接跑样本输出逐条证据, 测试随附。

原模块顶部 4 条「待校准」假设, 实测结论 —— **原假设全部不成立**:

① 帧结构: **不是二进制 TLV**。
   真实结构 = 24 字节文件头 + **zlib 压缩流**; 解压后是 **ASCII 文本协议**:
     - 帧: `0x03` 起, `0x04` 止(样本 0x03 与 0x04 各 5066 次, 一一配对)
     - 帧内字段: `0x02` 分隔; 每字段 = 2 字符 ASCII 标签 + 十进制文本值
   证据: 头 24B = 4B magic(随文件变: `d1eb0b0b` / `6888f00a`, 经比对**不是**
     crc32/adler32, 视为不透明 magic) + 4B 零 + u64LE 压缩长度 + u64LE 解压长度;
     `压缩长度 == 文件长-24`, zlib 解开长度 `== 头声明解压长度`(两份样本精确相等)。

② 价格标度: **不是定点 1e3**。值是十进制文本, 直接按元解析(scale=1)。
   证据: 字段 `20`=「10.410000」, 与 `1E`=11.34 / `1F`=9.28 满足 last=10.31 →
     10.31×1.1 / ×0.9 的涨跌停口径。

③ 时间编码: **不是 u32 HHMMSSmmm / epoch**。标签 `0T`, 值 = 十进制「HHMMSS.mmm」。
   证据: 首帧「83627.000」= 08:36:27(盘前), 竞价帧「91500.000」= 09:15:00,
     尾帧「155927.000」= 15:59:27; 相邻帧间隔众数 3s(4757/5065 帧)。

④ 委托队列(标签 64): **不是 u32 数组**。是**每笔挂单占一个字段**、同帧内连续出现,
   且分**两段**: 第一段 = 买一委托队列, 第二段 = 卖一委托队列。
   切分依据: 卖一区块由标签 `61` 引导, 故 `61` 之前的 64 段 = 买, 之后的 = 卖。
   证据: 两段各自求和 与 买一量(`30`)/卖一量(`50`) 在字段齐全帧上精确相等。

额外实测(原交接文档未记):
  - 档位标签与交接文档一致: `20-29` 买1-10 价 / `30-39` 买1-10 量 /
    `40-49` 卖1-10 价 / `50-59` 卖1-10 量; `04/05` = 买一价/量(盘前初始化帧)。
  - **档位是增量更新**: 缺失标签 = 沿用上一帧(**不是**无数据)。
    证据: 首帧一次性给出全部 40 个档位标签(盘前全 `0`), 之后单帧只带变动档位
    (全天 326 帧为全量帧); 且档位消失时 TDX 显式送 `0`(首帧 `20`=0)而非省略
    ⇒ 省略必然表示「未变」。`ImgFormat.delta=True` 时按此沿用; 要单帧原样传 `delta=False`。
    **队列不做沿用**(缺失即无队列): 盘前无挂单帧(样本 160 帧)确实无队列。
  - 委托笔数: 标签 `62`(每帧 2 个, 前=买 / 后=卖; 可 > 队列条数),
    标签 `63` = 队列条数(上限 50), `60/61` 恒为 `10`(语义未定, 不映射)。

单位口径(项目硬约束): 价格=元, 成交量=股, 委托笔数=笔。
缺失一律返回 None, 由上层显式标注「无数据」, 禁止推断或编造。
"""

from __future__ import annotations

import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

# ---------------------------------------------------------------------------
# 格式假设(校准点集中区) —— 已按真实样本校准, 见模块顶部
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImgFormat:
    """`.img` 容器 / 帧 / 字段格式参数(校准后)。"""

    # —— 容器头: magic + reserved + u64LE 压缩长度 + u64LE 解压长度 ——
    header_size: int = 24          # 头字节数
    comp_len_offset: int = 8       # u64LE 压缩长度偏移
    decomp_len_offset: int = 16    # u64LE 解压长度偏移
    endian: str = "<"              # 头内整数字节序(小端)
    magic_len: int = 4             # magic 字节数(值随文件变, 按不透明处理, 不校验)

    # —— 帧/字段分隔(解压后的 ASCII 文本协议) ——
    record_start: int = 0x03       # 帧起始
    record_end: int = 0x04         # 帧结束
    field_sep: int = 0x02          # 帧内字段分隔
    tag_width: int = 2             # 标签宽度(ASCII 字符数)
    encoding: str = "ascii"        # 值编码

    # —— 价格 / 增量语义 ——
    price_scale: int = 1           # 十进制文本直接解析(校准: 非定点)
    delta: bool = True             # 档位缺失 = 沿用上一帧(校准: 增量更新)

    def with_delta(self, delta: bool) -> "ImgFormat":
        """返回一份仅改 `delta` 的副本(dataclass 冻结, 用 replace 语义)。"""
        from dataclasses import replace

        return replace(self, delta=delta)


# 价格标度(校准后 = 1: 直接按元解析十进制文本)
PRICE_SCALE = 1

# 标签(2 字符 ASCII; 与交接文档字段表对齐, 已实测确认)
TAG_TIME = "0T"        # 时间, 值 = HHMMSS.mmm(十进制文本)
TAG_BID1_PRICE = "04"  # 买一价(盘前初始化帧)
TAG_BID1_VOL = "05"    # 买一量(盘前初始化帧)
TAG_LAST = "08"        # 最新价(增量)
TAG_BID_ORDERS = "62"  # 委托笔数(每帧 2 个: 前=买 / 后=卖)
TAG_QUEUE_LEN = "63"   # 队列条数(上限 QUEUE_MAX_ENTRIES)
TAG_BID_SECTION = "60"  # 买一区块引导(恒 10, 不映射)
TAG_ASK_SECTION = "61"  # 卖一区块引导(队列切分锚点)
TAG_QUEUE = "64"       # 委托队列(每笔挂单一个字段)


def _tag_range(start: int, n: int) -> tuple[str, ...]:
    """十六进制 2 字符标签区间, 如 (0x20,10) → ("20"..."29")。"""
    return tuple(f"{start + i:02X}" for i in range(n))


BID_PRICE_TAGS = _tag_range(0x20, 10)   # 买1-10 价(元)
BID_VOL_TAGS = _tag_range(0x30, 10)     # 买1-10 量(股)
ASK_PRICE_TAGS = _tag_range(0x40, 10)   # 卖1-10 价(元)
ASK_VOL_TAGS = _tag_range(0x50, 10)     # 卖1-10 量(股)

DEPTH = 10                  # 十档
QUEUE_MAX_ENTRIES = 50      # 单侧队列条数上限(标签 63 实测封顶 50)
TIME_MAX = 235_959          # HHMMSS 上界

# 缺失值统一占位(上层渲染为「无数据」)
MISSING = "无数据"


class ImgParseError(ValueError):
    """`.img` 解析失败(容器损坏 / 压缩流损坏 / 结构不匹配当前假设)。"""


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
    # 委托队列(每笔挂单量, 股)。校准: 单帧带两段, 分别对应买一 / 卖一。
    # `queue` 保持历史语义 = **卖一委托队列**(与 order_book_queue 的卖一比较口径一致)。
    queue: Optional[list[int]] = None             # 卖一委托队列
    bid_queue: Optional[list[int]] = None         # 买一委托队列(校准新增)

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

        总量为 0(无挂单) → None, 不返回 0.0 冒充「均衡」。
        """
        bid = sum(v for v in self.bid_vols if v is not None)
        ask = sum(v for v in self.ask_vols if v is not None)
        total = bid + ask
        if total <= 0:
            return None
        return round(bid / total, 6)

    def queue_imbalance(self) -> Optional[int]:
        """委托队列净挂单量(股) = 队列总量 - 卖一量。

        历史口径保留(卖一队列 vs 卖一量)。注: 校准后 `queue` 即卖一委托队列,
        其总量与卖一量同源, 该指标真实值通常≈0 — 保留仅为兼容既有消费方。
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
# 容器解码(头 + zlib)
# ---------------------------------------------------------------------------


def decode_container(data: bytes, fmt: ImgFormat = ImgFormat()) -> bytes:
    """`.img` 文件字节 → 解压后的帧流字节。

    Raises:
        ImgParseError: 头长度不足 / 压缩长度越界 / zlib 解压失败 /
            解压长度与头声明不一致(样本漂移要显式报错, 不静默)。
    """
    if len(data) < fmt.header_size:
        raise ImgParseError(
            f".img 头长度不足: {len(data)} < {fmt.header_size}(非 .img 容器)"
        )
    raw_len = int.from_bytes(
        data[fmt.comp_len_offset:fmt.comp_len_offset + 8], "little"
    )
    out_len = int.from_bytes(
        data[fmt.decomp_len_offset:fmt.decomp_len_offset + 8], "little"
    )
    body = data[fmt.header_size:]
    if raw_len and raw_len > len(body):
        raise ImgParseError(f"压缩长度越界: 声明 {raw_len} > 实际余量 {len(body)}")
    payload = body[:raw_len] if raw_len else body
    try:
        raw = zlib.decompress(payload)
    except zlib.error as e:
        raise ImgParseError(f"zlib 解压失败(容器损坏或非本格式): {e}") from e
    if out_len and out_len != len(raw):
        raise ImgParseError(
            f"解压长度与头声明不一致: 实得 {len(raw)} != 声明 {out_len}"
        )
    return raw


# ---------------------------------------------------------------------------
# 帧 / 字段扫描(校准后的文本协议)
# ---------------------------------------------------------------------------


def iter_records(raw: bytes, fmt: ImgFormat = ImgFormat()) -> Iterator[bytes]:
    """解压后的流 → 逐帧字段字节串(`0x03`..`0x04` 之间, 不含分隔符)。

    尾部不完整帧(无收尾 `0x04`)直接丢弃 —— 不补不猜。
    """
    sep_s = fmt.record_start.to_bytes(1, "big")
    sep_e = fmt.record_end.to_bytes(1, "big")
    pos = raw.find(sep_s)
    while pos != -1:
        end = raw.find(sep_e, pos + 1)
        if end == -1:
            return
        yield raw[pos + 1:end]
        pos = raw.find(sep_s, end + 1)


def iter_fields(frame: bytes, fmt: ImgFormat = ImgFormat()) -> Iterator[tuple[str, bytes]]:
    """一帧 → (标签, 值字节)。

    Yields:
        (tag: 2 字符 ASCII, value: bytes)。
        长度不足 tag_width 的残片跳过(不编造)。
    """
    for chunk in frame.split(fmt.field_sep.to_bytes(1, "big")):
        if len(chunk) < fmt.tag_width:
            continue
        yield chunk[:fmt.tag_width].decode(fmt.encoding, "replace"), chunk[fmt.tag_width:]


# ---------------------------------------------------------------------------
# 值解析
# ---------------------------------------------------------------------------


def _text(payload: bytes, encoding: str = "ascii") -> str:
    return payload.decode(encoding, "ignore").strip()


def _price(payload: bytes, encoding: str = "ascii", scale: int = PRICE_SCALE) -> Optional[float]:
    """十进制文本 → 元。空 / 非数值 → None。"""
    s = _text(payload, encoding)
    if not s:
        return None
    try:
        return round(float(s) / scale, 6)
    except ValueError:
        return None


def _int(payload: bytes, encoding: str = "ascii") -> Optional[int]:
    """十进制文本 → int。空 / 非数值 → None。"""
    s = _text(payload, encoding)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _decode_time(payload: bytes, encoding: str = "ascii") -> Optional[str]:
    """时间字段 → 'HH:MM:SS'。

    校准: 值 = 十进制「HHMMSS.mmm」(无前导零, 如「83627.000」= 08:36:27)。
    非法区间(时>23 / 分秒>59)返回 None, 不猜。
    """
    s = _text(payload, encoding)
    if not s:
        return None
    head = s.split(".", 1)[0]
    if not head.isdigit():
        return None
    v = int(head)
    h, rem = divmod(v, 10_000)
    m, sec = divmod(rem, 100)
    if h > 23 or m > 59 or sec > 59:
        return None
    return f"{h:02d}:{m:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
# 快照装配
# ---------------------------------------------------------------------------


def _split_queue_runs(
    fields: Sequence[tuple[str, bytes]], fmt: ImgFormat
) -> tuple[Optional[list[int]], Optional[list[int]]]:
    """按字段顺序切出买一 / 卖一委托队列。

    规则(校准): 标签 `61` 引导卖一区块, 故其**前**的 `64` 连续段 = 买一, 其**后** = 卖一。
    无 `61` 时退回位置口径(第一段=买一, 第二段=卖一)。
    """
    runs: list[tuple[int, list[int]]] = []
    cur: list[int] = []
    start = 0
    for idx, (tag, payload) in enumerate(fields):
        if tag == TAG_QUEUE:
            v = _int(payload, fmt.encoding)
            if v is None:
                continue
            if not cur:
                start = idx
            cur.append(v)
        elif cur:
            runs.append((start, cur))
            cur = []
    if cur:
        runs.append((start, cur))
    if not runs:
        return None, None

    ask_mark = next(
        (i for i, (tag, _p) in enumerate(fields) if tag == TAG_ASK_SECTION), None
    )
    if ask_mark is None:
        bid = runs[0][1] if runs else None
        ask = runs[1][1] if len(runs) > 1 else None
        return bid, ask
    bid = next((vals for pos, vals in runs if pos < ask_mark), None)
    ask = next((vals for pos, vals in runs if pos > ask_mark), None)
    return bid, ask


def decode_snapshot(
    fields: Sequence[tuple[str, bytes]],
    fmt: ImgFormat = ImgFormat(),
    price_scale: int = PRICE_SCALE,
    prev: Optional["ImgSnapshot"] = None,
) -> ImgSnapshot:
    """把一帧的 (标签, 值) 序列装配成 ImgSnapshot。

    Args:
        fields: `iter_fields()` 输出, 或等价 (str 标签, bytes 值) 序列。
        fmt: 格式参数。
        price_scale: 价格标度(校准后 = 1)。
        prev: 上一帧快照; `fmt.delta=True` 时, 本帧缺失的档位**沿用** `prev` 的值
            (校准语义: 缺失=未变)。None → 缺失档位保留 None。

    价位同标签重复取第一次; 委托笔数(62)按出现顺序取(前=买 / 后=卖)。
    队列不沿用: 本帧无 `64` 即队列为 None。
    """
    ordered = list(fields)
    first: dict[str, bytes] = {}
    for tag, payload in ordered:
        first.setdefault(tag, payload)
    enc = fmt.encoding

    snap = ImgSnapshot(t=_decode_time(first[TAG_TIME], enc) if TAG_TIME in first else None)

    def _price_prev(tag: str, i: int, prev_seq: Sequence[Optional[float]]) -> Optional[float]:
        """本帧价格; 缺失且在增量模式下 → 沿用 prev 对应档位。"""
        val = _price(first[tag], enc, price_scale) if tag in first else None
        if val is None and fmt.delta and prev is not None and i < len(prev_seq):
            return prev_seq[i]
        return val

    def _int_prev(tag: str, i: int, prev_seq: Sequence[Optional[int]]) -> Optional[int]:
        """本帧量/笔数; 缺失且在增量模式下 → 沿用 prev 对应档位。"""
        val = _int(first[tag], enc) if tag in first else None
        if val is None and fmt.delta and prev is not None and i < len(prev_seq):
            return prev_seq[i]
        return val

    prev_p = prev.bid_prices if prev else []
    prev_bv = prev.bid_vols if prev else []
    prev_ap = prev.ask_prices if prev else []
    prev_av = prev.ask_vols if prev else []

    for i, tag in enumerate(BID_PRICE_TAGS):
        snap.bid_prices.append(_price_prev(tag, i, prev_p))
    for i, tag in enumerate(BID_VOL_TAGS):
        snap.bid_vols.append(_int_prev(tag, i, prev_bv))
    for i, tag in enumerate(ASK_PRICE_TAGS):
        snap.ask_prices.append(_price_prev(tag, i, prev_ap))
    for i, tag in enumerate(ASK_VOL_TAGS):
        snap.ask_vols.append(_int_prev(tag, i, prev_av))

    # 04/05(买一价/量)是盘前初始化帧的独立标签, 档位缺失时兜底
    if snap.bid_prices and snap.bid_prices[0] is None and TAG_BID1_PRICE in first:
        snap.bid_prices[0] = _price(first[TAG_BID1_PRICE], enc, price_scale)
    if snap.bid_vols and snap.bid_vols[0] is None and TAG_BID1_VOL in first:
        snap.bid_vols[0] = _int(first[TAG_BID1_VOL], enc)

    # 委托笔数: 62 每帧 2 个(前=买 / 后=卖)
    o62 = [p for tag, p in ordered if tag == TAG_BID_ORDERS]
    bid_o = _int(o62[0], enc) if o62 else None
    ask_o = _int(o62[1], enc) if len(o62) > 1 else None
    if bid_o is None and fmt.delta and prev is not None:
        bid_o = prev.bid_orders
    if ask_o is None and fmt.delta and prev is not None:
        ask_o = prev.ask_orders
    snap.bid_orders, snap.ask_orders = bid_o, ask_o

    # 委托队列: 不沿用(缺失即无队列, 不拿陈旧队列冒充)
    snap.bid_queue, snap.queue = _split_queue_runs(ordered, fmt)
    return snap


# ---------------------------------------------------------------------------
# 文件层
# ---------------------------------------------------------------------------


def parse_img(
    path: str | Path,
    fmt: ImgFormat = ImgFormat(),
    price_scale: int = PRICE_SCALE,
    delta: bool | None = None,
) -> list[ImgSnapshot]:
    """解析 `.img` 文件 → 快照列表(多帧切片)。

    Args:
        path: .img 文件路径。
        fmt: 格式参数。
        price_scale: 价格标度(校准 = 1)。
        delta: 是否对档位做「缺失沿用上一帧」; None = 用 `fmt.delta`。

    Returns:
        逐帧 ImgSnapshot(按文件内帧序)。

    Raises:
        FileNotFoundError: 文件不存在。
        ImgParseError: 空文件 / 容器损坏 / 无可解析帧。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f".img 文件不存在: {p}")
    data = p.read_bytes()
    if not data:
        raise ImgParseError(f".img 文件为空: {p}")
    eff = fmt if delta is None else fmt.with_delta(bool(delta))
    raw = decode_container(data, eff)
    if not raw:
        raise ImgParseError(f".img 解压后为空: {p}")
    snaps: list[ImgSnapshot] = []
    prev: Optional[ImgSnapshot] = None
    for frame in iter_records(raw, eff):
        snap = decode_snapshot(list(iter_fields(frame, eff)), eff, price_scale, prev=prev)
        snaps.append(snap)
        prev = snap
    if not snaps:
        raise ImgParseError(f".img 无可解析帧(0x03/0x04 分隔未命中): {p}")
    return snaps


def frames_from_img(
    path: str | Path,
    fmt: ImgFormat = ImgFormat(),
    price_scale: int = PRICE_SCALE,
    delta: bool | None = None,
) -> list[ImgSnapshot]:
    """`parse_img` 的别名(供 `orderbook_engine.load_snapshots_from_img` 调用)。"""
    return parse_img(path, fmt=fmt, price_scale=price_scale, delta=delta)


# ---------------------------------------------------------------------------
# 同构输出(供前端 / API 使用)
# ---------------------------------------------------------------------------


def snapshots_to_frames(
    snapshots: Sequence[ImgSnapshot],
    top_n: int = 10,
) -> list[dict]:
    """快照 → 与 .tck / 腾讯逐笔同构的 dict 列表。

    输出字段:
        t            "HH:MM:SS" 或 "无数据"
        bid/ask      [{price, vol}] 各 top_n 档, 缺失档 price/vol 为 "无数据"
        spread       买卖价差(元)或 "无数据"
        bid_pressure 买盘占比(0~1)或 "无数据"
        queue        卖一委托队列(股)列表, 无队列时为 "无数据"
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


# ---------------------------------------------------------------------------
# 样本驱动校准(把 4 条假设逐条实测, 结论可复现)
# ---------------------------------------------------------------------------


@dataclass
class CalibrationReport:
    """对一份真实 .img 样本的逐条校准证据(全部为实测量, 不编造)。"""

    path: str
    file_size: int = 0
    magic: str = ""
    reserved_zero: bool = False
    compressed_len: int = 0
    declared_decomp_len: int = 0
    actual_decomp_len: int = 0
    compressed_len_matches: bool = False
    decomp_len_matches: bool = False
    n_frames: int = 0
    frame_with_all_depth: int = 0
    frame_with_queue: int = 0
    tag_histogram: dict[str, int] = field(default_factory=dict)
    order_tag_counts: dict[str, int] = field(default_factory=dict)
    time_first: Optional[str] = None
    time_last: Optional[str] = None
    frame_gap_mode_s: Optional[float] = None
    price_samples: list[float] = field(default_factory=list)
    price_within_plausible_range: bool = False
    queue_frames_compared: int = 0
    queue_two_segments: int = 0       # 同帧带完整两段(买一+卖一)队列的帧数
    queue_full_checked: int = 0       # 非截断帧(委托笔数 == 队列条数)对数
    queue_full_equal: int = 0         # 非截断帧中 sum(队列) == 档位量 的对数
    queue_truncated_frames: int = 0   # 截断帧(委托笔数 > 队列条数 = 上限 50)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _gap_seconds(t_prev: str, t_cur: str) -> float:
    def _s(t: str) -> int:
        h, m, s = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + s

    return float(_s(t_cur) - _s(t_prev))


def calibrate_from_sample(
    path: str | Path, fmt: ImgFormat = ImgFormat()
) -> CalibrationReport:
    """对一份真实 `.img` 样本逐条实测校准假设, 返回证据报告。

    不猜、不补: 每项都是对样本字节/帧的实测量。文件缺失或损坏 → 抛异常(不返回假报告)。
    """
    p = Path(path)
    data = p.read_bytes()
    rep = CalibrationReport(path=str(p), file_size=len(data))
    rep.magic = data[:fmt.magic_len].hex()
    rep.reserved_zero = data[fmt.magic_len:8] == b"\x00\x00\x00\x00"
    rep.compressed_len = int.from_bytes(
        data[fmt.comp_len_offset:fmt.comp_len_offset + 8], "little"
    )
    rep.declared_decomp_len = int.from_bytes(
        data[fmt.decomp_len_offset:fmt.decomp_len_offset + 8], "little"
    )
    rep.compressed_len_matches = rep.compressed_len == len(data) - fmt.header_size
    raw = decode_container(data, fmt)
    rep.actual_decomp_len = len(raw)
    rep.decomp_len_matches = rep.declared_decomp_len == len(raw)

    tag_hist: Counter[str] = Counter()
    order_hist: Counter[str] = Counter()
    frames = 0
    times: list[str] = []
    prices: list[float] = []
    for frame in iter_records(raw, fmt):
        frames += 1
        flds = list(iter_fields(frame, fmt))
        depth = {t for t, _ in flds} & set(
            BID_PRICE_TAGS + BID_VOL_TAGS + ASK_PRICE_TAGS + ASK_VOL_TAGS
        )
        if len(depth) >= 40:
            rep.frame_with_all_depth += 1
        if any(t == TAG_QUEUE for t, _ in flds):
            rep.frame_with_queue += 1
        for t, _ in flds:
            tag_hist[t] += 1
            if t in (TAG_BID_SECTION, TAG_ASK_SECTION, TAG_BID_ORDERS, TAG_QUEUE_LEN):
                order_hist[t] += 1
        snap = decode_snapshot(flds, fmt)
        if snap.t:
            times.append(snap.t)
        if frames <= 200:
            prices.extend(x for x in snap.bid_prices + snap.ask_prices if x)
        # 队列语义: 两段求和 vs 买一量 / 卖一量; 区分「非截断 / 截断」
        bq, aq = _split_queue_runs(flds, fmt)
        if bq and aq:
            rep.queue_two_segments += 1
            rep.queue_frames_compared += 1
            o62 = [_int(v) for t, v in flds if t == TAG_BID_ORDERS]
            first = {t: v for t, v in flds}

            def _pair(queue: list[int], lvl_tag: str, orders: Optional[int]) -> None:
                if orders is None or lvl_tag not in first:
                    return
                lvl = _int(first[lvl_tag])
                if lvl is None:
                    return
                if orders == len(queue):
                    rep.queue_full_checked += 1
                    if sum(queue) == lvl:
                        rep.queue_full_equal += 1
                elif orders > len(queue):
                    rep.queue_truncated_frames += 1

            _pair(bq, BID_VOL_TAGS[0], o62[0] if o62 else None)
            _pair(aq, ASK_VOL_TAGS[0], o62[1] if len(o62) > 1 else None)

    rep.n_frames = frames
    rep.tag_histogram = dict(tag_hist.most_common())
    rep.order_tag_counts = dict(order_hist)
    rep.time_first = times[0] if times else None
    rep.time_last = times[-1] if times else None
    if len(times) > 1:
        gaps = Counter(
            round(_gap_seconds(times[i], times[i + 1]), 3) for i in range(len(times) - 1)
        )
        rep.frame_gap_mode_s = gaps.most_common(1)[0][0]
    rep.price_samples = prices[:8]
    rep.price_within_plausible_range = bool(prices) and all(
        0 < x <= 10_000 for x in prices
    )
    rep.notes = [
        "容器 = 24B 头 + zlib(压缩长度 == 文件长-24)",
        "帧 = 0x03..0x04; 字段 = 0x02 分隔; 标签 2 字符 ASCII",
        "时间 = 标签 0T 的十进制 HHMMSS.mmm",
        "价格 = 十进制文本(scale=1), 非定点",
        "档位为增量: 缺失=沿用上一帧(沿用后 10 档严格单调); 队列不沿用",
        "队列两段: 买一在标签 61 之前, 卖一在其后",
        "队列非截断(委托笔数 <= 50)时 sum(队列) == 该档总量(精确); 截断(> 50)时 "
        "队列只含前 50 条, sum < 该档总量 —— 取档位总量须用标签 30/50, 不可用队列求和",
    ]
    return rep
