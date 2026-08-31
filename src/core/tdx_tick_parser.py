"""通达信 .tck 逐笔解析器 — 36 字节委托号级(官方方向 2B/2S + 主动侧委托号 + 撤单)。

从 tdx_tools/dark_fund.py 搬进 SIDA, 产出与 dark_flow 同构的 tick 列表。
数据源: 通达信客户端"超盘回放"落盘 `T0002/zst_cache/{市场}{代码}_{yyyymmdd}.tck`。
官方方向比腾讯逐笔的自解析方向准(2B主买/2S主卖是交易所级标记), 用于修正暗盘方向。

局限(2026-08-30 实测, 见 tdx-client-l2-extraction skill):
- 仅**主动侧**委托号(被动 maker 未落盘, 委托量==成交量 1:1, 0 条"大单拆小单"实体)。
- **盘后数据**(超盘回放落盘), 非盘中实时; 盘中实时用腾讯逐笔兜底。
"""
import struct
import zlib

REC, HDR = 36, 24
CONT = 93_000_000  # 连续竞价 9:30:00.000 (u32 HHMMSSmmm)


def parse_tck(path: str) -> tuple[list[dict], list[dict], list[dict]]:
    """解析 .tck → (trades, orders, cancels)。

    .tck 结构: 24 字节头 + zlib(78 9C) 压缩的 36 字节定长记录。
    记录字段(小端): [0]=类型(0成交), [2:6]=u32时间HHMMSSmmm, [6:14]=f64价格,
    [14:18]=u32量(股), [22:26]=u32成交序号, [26:28]=tag("2B"主买/"2S"主卖/"00"申报/"0C"撤单),
    [28:32]=a28(→主动买成交seq), [32:36]=a32(→主动卖成交seq)。
    """
    raw = open(path, "rb").read()
    comp = struct.unpack("<I", raw[8:12])[0]
    raw_size = struct.unpack("<I", raw[16:20])[0]
    dec = zlib.decompress(raw[HDR:HDR + comp])
    assert len(dec) == raw_size and len(dec) % REC == 0, (
        f".tck 解压尺寸不符: dec={len(dec)} raw_size={raw_size}"
    )

    trades: list[dict] = []
    orders: list[dict] = []
    cancels: list[dict] = []
    for i in range(len(dec) // REC):
        r = dec[i * REC:(i + 1) * REC]
        t = struct.unpack("<I", r[2:6])[0]
        price = struct.unpack("<d", r[6:14])[0]
        vol = struct.unpack("<I", r[14:18])[0]
        seq = struct.unpack("<I", r[22:26])[0]
        tag = r[26:28]
        a28 = struct.unpack("<I", r[28:32])[0]
        a32 = struct.unpack("<I", r[32:36])[0]
        if r[0] == 0:
            d = tag.decode("ascii", "replace")
            trades.append({
                "seq": seq, "t": t, "price": price, "vol": vol,
                "dir": "B" if d.endswith("B") else "S", "amt": price * vol,
            })
        elif tag == b"00":
            orders.append({
                "seq": seq, "t": t, "price": price, "vol": vol,
                "amt": price * vol, "a28": a28, "a32": a32,
            })
        elif tag == b"0C":
            cancels.append({"seq": seq, "t": t, "vol": vol, "target": a28 or a32})
    return trades, orders, cancels


def _tck_t_to_hms(t: int) -> str:
    """u32 HHMMSSmmm → 'HH:MM:SS'。"""
    h = t // 10_000_000
    m = (t // 100_000) % 100
    s = (t // 1_000) % 100
    return f"{h:02d}:{m:02d}:{s:02d}"


def ticks_from_tck(trades: list[dict], min_price: float = 5.0,
                   cont_only: bool = True) -> list[dict]:
    """把 .tck trades 转成 dark_flow 同构 tick 列表 [{d, amt, vol, price, t}]。

    官方方向: '2B'主买→'B', '2S'主卖→'S'。
    过滤(对齐 dark_fund.ohlcv_from_trades):
      ① 连续竞价(t >= 9:30)剔除集合竞价虚拟撮合(方向不可信)
      ② price >= min_price 剔除修正行(price=0)和噪声行(price=1.0)
    """
    ticks: list[dict] = []
    for t in trades:
        if t["price"] < min_price:
            continue
        if cont_only and t["t"] < CONT:
            continue
        ticks.append({
            "d": t["dir"],
            "amt": t["amt"],
            "vol": t["vol"],
            "price": t["price"],
            "t": _tck_t_to_hms(t["t"]),
            "seq": t["seq"],
        })
    return ticks
