"""只读缠论分型端点 + 与图表响应绑定的内容摘要(2026-11-17)。

目标(为什么需要这个模块)
------------------------
图表 GET /api/klines/{symbol} 与独立来源的 `GET .../chan-structure` 是两次
HTTP 请求: 中间可能跨 worker、跨 60s 缓存、跨一次数据源漂移。若结构端点只凭
symbol/market/instrument 取数计算, 它算出的分型可能来自**另一批 K 线**, 与用户
屏幕上的图不一致 —— 那就不是"这张图的缠论结构", 却会被当成同一结论使用。

做法: 图表响应里带一个**内容摘要**(``chan_snapshot.digest``); 结构端点必须回传
该摘要, 再用**同一个 get_klines 的返回**重算同一摘要, 不一致 → 409。摘要绑定
symbol/market/instrument/interval + 每根 bar 的日期与完整 OHLC + 数据顺序, 并绑定
**会改变结构/归属的元信息**: 每根 bar 的 ``partial`` 标记、每根 bar 的 ``source``,
以及响应级 ``source`` / ``today_bar``, 所以:
  * 少一根/多一根/换一根/顺序变化 → 摘要不同;
  * 价格只差第 5 位小数 → 摘要不同(不用 %.4f 之类定点截断, 见 ``_number_bytes``);
  * 同样 OHLC 但末根从"已收盘"变成"盘中未定型"(partial) → 摘要不同;
  * 同样 OHLC 但来源变了(pg_klines_hypertable → 实时源) → 摘要不同;
  * ``partial`` 不是布尔值(如字符串 "true") → 摘要层直接拒绝(ValueError → 409),
    绝不猜成 False 把未定型 bar 当已收盘;
  * 摘要用完整 sha256 十六进制(64 字符), 不截短;
  * 不同 worker 缓存内容不一致 → 摘要不同 → 409, 而不是静默各算一套。
  不含 ``as_of``/``amount``/``volume``: 它们不进入分型计算, 绑上去只会让无关噪声
  变成 409(诚实 ≠ 把不相关的变化也算成结构变化)。

身份诚实性
----------
* 端点把 ``instrument`` 设为**必填** Query 参数(缺参 422), 不再默认 ``stock``:
  "忘了传"不等于"我说了是股票", 默认值会把声明伪装成结论。
* 调用方声明的 instrument **不是**已验证的源身份: 端点强制把 instrument 显式传给
  ``get_klines``(绝不用 ``auto``), 再用响应的 ``is_index`` 反查, 不一致即 409。
  A 股指数与个股存在同码(000001), 不校验就会互证。
* ``is_index`` 必须是布尔: 字符串 / 数字 / None 一律拒绝(409), 不静默当 stock。
  键**缺失**是历史 stock 响应契约 —— 此时身份依据是"用显式 stock 调用路径取数",
  不是独立验证, 故响应里如实标 ``instrument_basis='declared_path'``; 只有当响应
  自带布尔 ``is_index`` 时才标 ``response_flag``。
* 只接受 CN / 1d / 显式 stock|index; 其他市场、其他周期一律 422, 不做猜测。
* ``source`` 只回传响应自述的来源, 缺省 ``unknown``; ``adjustment`` 恒为
  ``unknown``(本适配器无法证明复权口径), 不冒充。
* 只输出合并K线**分型**(blocked/candidate/confirmed、发生日、确认日、规则版本)、
  在**同一份**分型快照上算出的**笔**、以及在同一份笔快照上算出的**线段**与
  **操作口径中枢**(operational centre)。不含买卖点、严格中枢、走势类型, 响应里
  ``not_implemented`` 明写, 不允许被读成"中枢/买卖点都有了"。
* 线段来自 ``chan_segment.segment_from_pens``(内核 ``chan-segment-v2``), 输入就是产出
  ``pens`` 的那个 ``PenSnapshot``; 只把 **confirmed 前缀**当几何对外, 尾部 candidate
  单列并标 ``driftable``(终点仍可漂移), ``pending``/``blocked`` 单列并保留内核原因。
  ``blocked`` **不提供价格几何**(``geometry_withheld``), 否则会被画成一条线并喂成中枢。
  证据笔(特征序列)与几何跨度分开给出: 证据笔通常落在终点之后, 两套集合分别计数。
* 操作口径中枢来自 ``chan_centre_adapter.centre_from_segment_snapshot``: 原样透传
  ``basis='segment_a0'`` / ``strictness='operational_not_strict'`` / ``status`` /
  ``chain_complete`` / ``issues`` / ``limitations``。它**不是**「中枢」, 更**不是**
  「严格中枢」(第18课定义未实现); ``not_enough_confirmed_segments`` 只表示"还没凑够",
  绝不显示成「无中枢」; ``no_centre`` 才是实测无交集。
* 线段/中枢与分型/笔同属一次 ``get_klines`` 的同一份快照: 摘要仍只算一次, 不二次取数、
  不另写摘要。任一环节(内核/适配器/自洽性/来源)不符 → 409 整份失败, 不返回半份结果。
* 笔由 ``chan_pen.pen_from_snapshot`` 计算, 输入就是产出 ``fractals`` 的那个
  ``ChanSnapshot`` 对象: 一次 ``get_klines``、一次 ``build_chan_structure``, 绝不为笔
  二次取数或按参数重算。笔的每个端点都必须能对回本响应的已确认分型(同合并索引/同日/
  同价/同类型), 任何不符 → 409 整份失败(不返回"分型是这份数据、笔是那份数据"的混合结果)。
* 笔的 ``confirmed_on`` 就是终点被冻结那天(= 反向分型右邻合并K线的封口日), 必 ≤
  ``as_of_day``; 最后一笔恒为 ``candidate``(端点仍可漂移), 之前的笔 ``confirmed``。
  笔候选与已确认都只是**规则几何**, 不是交易信号, 也不代表数据已最终定型。
* 当日 bar 不参与计算(沿用 ``chan_kline_adapter`` 的保守口径), 但仍在摘要里 ——
  摘要描述的是"图表看到的那份响应", 不是"我们要用的那几根"。
* 没有任何已完成的K线 → 422: 不许用"200 + 空分型/空笔"冒充"这张图没有结构"。
* 未来日期 bar → 422(时间不倒退); 重复/乱序日期 → 422(不自动去重)。
* 鉴权: 未登录 401, 账号禁用/注销 403; 上游异常只回固定文案(细节不进响应)。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Sequence
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from src.core.chan_centre_adapter import (
    ADAPTER_VERSION as CENTRE_ADAPTER_VERSION,
    BASIS as OPERATIONAL_CENTRE_BASIS,
    LIMITATIONS as OPERATIONAL_CENTRE_LIMITATIONS,
    STRICTNESS as OPERATIONAL_CENTRE_STRICTNESS,
    CentreAdapterResult,
    centre_from_segment_snapshot,
)
from src.core.chan_kline_adapter import adapt_chart_bars
from src.core.chan_pen import (
    MIN_INDEX_GAP,
    PEN_KERNEL_VERSION,
    PenEndpoint,
    PenSnapshot,
    pen_from_snapshot,
)
from src.core.chan_segment import (
    MIN_PENS,
    SEGMENT_KERNEL_VERSION,
    Segment,
    SegmentBinding,
    SegmentSnapshot,
    segment_from_pens,
    segment_input_digest,
)
from src.core.chan_structure import ChanSnapshot, build_chan_structure
from src.db.models import User
from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

#: 摘要编码方案。任何会改变字节流的改动都必须换版本号, 否则新旧进程摘要互不相等
#: (宁可 409 让前端刷新, 也不能让两种编码碰撞成"看似匹配")。
#: v2(2026-11-17): 在 v1 的 OHLC 之上补绑 partial/source/today_bar —— 同价不同
#: 含义(未定型/另一个来源)不再共用摘要, 故必须换号。
DIGEST_SCHEMA = "sida-chan-snapshot-v2"
#: 分型规则版本(与 chan_structure v1 合并/分型规则同源, 文档固定值)。
#: `inclusive-sealed` = 包含式合并(等高等低也算包含) + 右邻封口才确认。
#: 改规则必须改版本号: 同号换语义会让历史结论被当成新规则的结果。
CHAN_FRACTAL_RULES_VERSION = "chan-fractal-inclusive-sealed-v1"
#: 笔规则版本: 直接取内核常量, 不在这里另写字面量 —— 两处字符串一旦漂移, 客户端
#: 就会把两套规则当成同一套。间距(MIN_INDEX_GAP)=4 根合并K线 + 顶包络高于底包络
#: + 反向分型封口才冻结, 详见 `src/core/chan_pen.py`。
CHAN_PEN_RULES_VERSION = PEN_KERNEL_VERSION
#: 线段规则版本: 直接取内核常量(同 `CHAN_PEN_RULES_VERSION` 的理由 —— 两处字面量
#: 一旦漂移, 客户端就会把两套规则当成同一套)。
CHAN_SEGMENT_RULES_VERSION = SEGMENT_KERNEL_VERSION
#: 线段块/操作口径中枢块的 schema 版本号。新增字段属于**可选**扩展, 但版本号必须跟着
#: 语义走: 客户端可以据此判断"这份响应里的 segments/operational_centre 是什么口径"。
SEGMENTS_SCHEMA = "sida-chan-segments-v1"
OPERATIONAL_CENTRE_SCHEMA = "sida-chan-operational-centre-v1"
#: 只有完整 64 位小写十六进制才接受; 截短/大写/非十六进制一律 422(不"修正")。
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
#: 身份依据: 响应布尔 is_index / 显式 stock 取数路径；均非独立证券主数据核验。
IdentityBasis = Literal["response_flag", "declared_path"]

_NOT_IMPLEMENTED: tuple[str, ...] = (
    # 线段只实现第67课**第一种情况**(无缺口); 以下仍是明确未实现, 不许被读成"都有了"
    "segment_case2_gap",
    "segment_destruction_repartition",
    # 操作口径中枢不是第18课的严格中枢定义, 也不做延伸/多中枢/级别升级/走势类型
    "strict_centre_18th_lesson_definition",
    "centre_extension_multi_centre_level_upgrade",
    "buy_sell_point",  # 买卖点
    "multi_level_recursion",
)

_SUPPORTED_INSTRUMENTS: frozenset[str] = frozenset({"stock", "index"})
_APP_TZ = "Asia/Shanghai"

#: 适配器/内核可能抛出的 ValueError 文案 → 对外安全的说明。不把异常原文回给调用方。
_SAFE_BAR_ERRORS: dict[str, tuple[int, str]] = {
    "bar dates must be strictly increasing": (422, "K线日期重复或乱序, 拒绝计算(不自动去重)"),
    "future bar": (422, "K线含未来日期, 拒绝计算"),
    "historical bar is partial": (422, "历史K线被标记为未完成, 拒绝计算"),
    "invalid bar": (422, "K线内容不完整, 拒绝计算"),
    "invalid OHLC": (422, "K线OHLC不合法, 拒绝计算"),
    "missing klines": (422, "响应缺少K线序列, 拒绝计算"),
    "instrument identity mismatch": (409, "图表快照身份与请求不一致, 请刷新图表"),
    "instrument response mismatch": (409, "图表快照身份与请求不一致, 请刷新图表"),
    "only CN daily bars supported": (422, "仅支持 CN 日线"),
}

router = APIRouter()


# ── 内容摘要 ────────────────────────────────────────────────────────────


def _feed(frame: bytearray, payload: bytes) -> None:
    """长度前缀编码: 消除 "a"+"bc" 与 "ab"+"c" 之类拼接歧义。"""
    frame += b"\x01" + len(payload).to_bytes(8, "big") + payload


def _number_bytes(value: Any) -> bytes:
    """价格 → 可复现字节串。

    必须用 ``repr(float)``(最短往返表示, 17 位有效数字以内无损)而不是
    ``%.4f``: 定点截断会把 10.00001 与 10.00002 压成同一个摘要, 也就把"换了一根
    K线"压成"快照未变"。int/float/Decimal 统一走 float 后再编码 —— 10 与 10.0
    必须同摘要, 否则同一笔数据经 PG(Decimal) 与 JSON(float) 两条路会互不相等。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("non-numeric bar value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite bar value")
    return repr(number).encode("ascii")


def _partial_bytes(bar: dict) -> bytes:
    """每根 bar 的"是否未定型"标记 → 一个字节。

    只接受布尔: ``partial="true"``(字符串)在 adapter 里是 ``is True`` 判假 → 历史
    未定型 bar 会被当成已收盘参与确认。这种畸形值不能"猜成 False", 必须抛错让端点
    409(拒绝计算), 否则响应里的一句谎言会变成分型结论。
    """
    if "partial" not in bar:
        return b"0"
    flag = bar["partial"]
    if flag is True:
        return b"1"
    if flag is False:
        return b"0"
    raise ValueError("bar partial flag must be boolean")


def _source_bytes(value: Any) -> bytes:
    """来源标记 → 字节串(缺省 = 空: "没说" 不冒充任何来源)。"""
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValueError("source must be a string")
    return value.strip().encode("utf-8")


def compute_snapshot_digest(
    *,
    symbol: str,
    market: str,
    instrument: Literal["stock", "index"] | str,
    interval: str,
    bars: Sequence[dict],
    source: str | None = None,
    today_bar: str | None = None,
) -> str:
    """对"图表响应的日线内容"求 sha256。

    绑定: 摘要方案、symbol、market、instrument、interval、响应级 source/today_bar、
    bar 数量、以及**按响应原顺序**逐根的 date/open/high/low/close + partial + source。
    不排序、不去重、不补 bar —— 顺序与重复本身就是被校验的内容(端点对重复/乱序报
    422, 而不是悄悄修好)。

    为什么连 partial/source 一起绑: 同样的 OHLC 可能是"已收盘"也可能是"盘中未定型",
    可能来自 PG 定型库也可能来自实时源。这两种差别会改变分型能否确认、结论归谁,
    只绑价格就会把两种不同含义的数据压成同一摘要 —— 于是结构端点敢拿 A 的数据算 B 的图。

    不绑 ``as_of``/``amount``/``volume``: 分型内核只吃 OHLC, 这些字段变了不改变结构,
    绑上去等于把展示层噪声升级成 409。
    """
    if instrument not in _SUPPORTED_INSTRUMENTS:
        raise ValueError("instrument must be explicit stock|index")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol required")
    if not isinstance(market, str) or not market:
        raise ValueError("market required")
    if not isinstance(interval, str) or not interval:
        raise ValueError("interval required")
    if not isinstance(bars, (list, tuple)):
        raise ValueError("bars must be a sequence")

    frame = bytearray()
    # market 统一大写: 绑的是"市场身份", 不是某个数据源顺手写的大小写。
    for text in (DIGEST_SCHEMA, symbol, market.strip().upper(), instrument, interval):
        _feed(frame, text.encode("utf-8"))
    # 响应级归属: 整份响应的来源 / 今日 bar 状态(定型? 合成? 缺失?)
    _feed(frame, _source_bytes(source))
    _feed(frame, _source_bytes(today_bar))
    frame += len(bars).to_bytes(8, "big")
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("bar must be an object")
        day = bar.get("date")
        if not isinstance(day, str) or not day:
            raise ValueError("bar date required")
        _feed(frame, day.encode("utf-8"))
        for field in ("open", "high", "low", "close"):
            _feed(frame, _number_bytes(bar.get(field)))
        _feed(frame, _partial_bytes(bar))
        _feed(frame, _source_bytes(bar.get("source")))
    return hashlib.sha256(bytes(frame)).hexdigest()


def response_instrument(response: dict) -> Literal["stock", "index"]:
    """从**响应**判定标的类型, 不信调用方声明。

    · ``is_index is True`` → index; ``is_index is False`` → stock。标记可能由
      同一取数分支生成，只验证内部一致性，不证明证券主数据或上游身份。
    · 键**缺失** → stock: 这是历史 stock 响应契约。此分支的身份依据是"用显式 stock
      调用路径取的数", **不是**对响应内容的独立验证 —— 见 :func:`instrument_identity_basis`,
      调用方必须把这一点如实标出来, 不许当成已验证;
    · 其它取值(字符串 "true"、数字 1、None …) → ``ValueError``。畸形值不能被猜成任一类:
      猜成 stock 就会把指数当个股算, 猜成 index 则反向; 唯一诚实的处理是拒绝。
    """
    if "is_index" not in response:
        return "stock"
    flag = response["is_index"]
    if flag is True:
        return "index"
    if flag is False:
        return "stock"
    raise ValueError("is_index must be boolean")


def instrument_identity_basis(response: dict) -> IdentityBasis:
    """这份响应的身份是怎么来的: 自带布尔 flag(``response_flag``) vs 仅靠调用路径。

    只在 ``is_index`` **键缺失**时返回 ``declared_path`` —— 那是"我们是用显式 stock
    取数路径拿到它的", 不是"它在响应里自称 stock"。
    """
    return "declared_path" if "is_index" not in response else "response_flag"


def _snapshot_block(
    *,
    instrument: Literal["stock", "index"] | None,
    basis: IdentityBasis | None,
    reason: str | None = None,
    bars: int | None = None,
    as_of_day: str | None = None,
    digest: str | None = None,
) -> dict:
    block = {
        "schema": DIGEST_SCHEMA,
        "rules_version": CHAN_FRACTAL_RULES_VERSION,
        "instrument": instrument,
        "instrument_basis": basis,
        "interval": "1d",
        "digest": digest,
    }
    if reason is not None:
        block["unavailable_reason"] = reason
    else:
        block["bars"] = bars
        block["as_of_day"] = as_of_day
    return block


def attach_snapshot_digest(response: dict) -> dict:
    """给既有 GET klines 日线响应补 ``chan_snapshot``, 原地返回同一对象。

    只在 ``interval == '1d'`` 时挂: 周/月线不是分型输入, 挂了只会诱导误用。
    摘要覆盖**响应里的每一根**(含当日未定型 bar), 因为接口契约是"这张图看到的那份
    数据", 而不是"结构计算实际用了哪几根"。当日 bar 在盘中会变 → 摘要随之变 →
    结构端点 409 让前端刷新, 这是诚实结果, 不静默用旧结构。

    身份标记畸形(``is_index="true"`` 之类)时**不产出摘要**: 连标的类型都不确定,
    算出来的 hex 只会被当成"可校验", 那比没有摘要更危险。
    """
    if not isinstance(response, dict):
        return response
    response.pop("chan_snapshot", None)
    if str(response.get("interval") or "").strip().lower() != "1d":
        return response

    try:
        instrument: Literal["stock", "index"] | None = response_instrument(response)
        basis: IdentityBasis | None = instrument_identity_basis(response)
    except ValueError:
        response["chan_snapshot"] = _snapshot_block(
            instrument=None, basis=None, reason="instrument_flag_unverified"
        )
        return response

    symbol = response.get("symbol")
    market = response.get("market")
    bars = response.get("klines")
    if (
        not isinstance(symbol, str)
        or not symbol
        or not isinstance(market, str)
        or not market
        or not isinstance(bars, list)
        or not all(isinstance(bar, dict) for bar in bars)
    ):
        response["chan_snapshot"] = _snapshot_block(
            instrument=instrument, basis=basis, reason="bars_not_digestible"
        )
        return response
    try:
        digest = compute_snapshot_digest(
            symbol=symbol,
            market=market,
            instrument=instrument,
            interval="1d",
            bars=bars,
            source=response.get("source"),
            today_bar=response.get("today_bar"),
        )
    except ValueError as exc:
        # 响应本身不可摘要(序列缺失/OHLC 非数值/partial 或 source 畸形) → 如实说
        # "没有可校验摘要", 不编一个 hex 让调用方以为能校验。畸形元信息单独给一个
        # reason: 那是"这份响应的字段类型不对", 与"根本没有 K 线"不是一回事。
        reason = (
            "bar_metadata_invalid"
            if str(exc) in ("bar partial flag must be boolean", "source must be a string")
            else "bars_not_digestible"
        )
        response["chan_snapshot"] = _snapshot_block(
            instrument=instrument, basis=basis, reason=reason
        )
        return response

    response["chan_snapshot"] = _snapshot_block(
        instrument=instrument,
        basis=basis,
        bars=len(bars),
        as_of_day=str(bars[-1].get("date")) if bars else None,
        digest=digest,
    )
    return response


# ── 笔(pen) ─────────────────────────────────────────────────────────────


def _endpoint_payload(endpoint: PenEndpoint) -> dict:
    """一个笔端点 → 响应字段(位置/价格/状态/确认日都逐项给出, 不合并成"一个点")。"""
    return {
        "kind": endpoint.kind,
        "merged_index": endpoint.merged_index,
        "position_day": endpoint.position_day,
        "price": endpoint.price,
        "bar_high": endpoint.bar_high,
        "bar_low": endpoint.bar_low,
        # 端点漂移必须可见: 候选端点挪过一次就带 revisions>0 + 被放弃的发生日,
        # 否则一个"动过的端点"看起来和原装端点一样。
        "revisions": endpoint.revisions,
        "drift_days": list(endpoint.drift_days),
        "status": endpoint.status,
        "confirmed_on": endpoint.confirmed_on,
    }


def _pen_payload(pen) -> dict:
    return {
        "direction": pen.direction,
        "status": pen.status,
        "confirmed_on": pen.confirmed_on,
        "index_gap": pen.index_gap,
        "kernel_version": pen.kernel_version,
        "start": _endpoint_payload(pen.start),
        "end": _endpoint_payload(pen.end),
    }


def _pen_consistency_error(pens: PenSnapshot, structure: ChanSnapshot) -> str | None:
    """笔结果与本快照是否自洽; ``None`` = 自洽, 否则返回**仅进日志**的诊断。

    ``pen_from_snapshot`` 自己已经复核传入的快照, 但"内核被喂了别人的快照"这件事它
    看不出来(两份快照各自自洽)。这里补上跨对象对账, 也是本端点唯一的兜底:

    · 截止日/规则版本必须与本次分型快照同源;
    · 每个端点必须能对回本快照里**同一个**合并索引的已确认分型(同日/同价/同类型) ——
      端点价格被改过、或整批笔来自另一批K线, 都在这里被挡住;
    · 笔必须是连续链(上一笔终点 == 下一笔起点), 不许断裂或重叠;
    · 已确认笔必须有 ``confirmed_on``, 且晚于起点发生日、不越过 ``as_of_day``;
      候选/受阻笔必须没有 ``confirmed_on``;
    · 端点发生日不得越过 ``as_of_day``(不许描述快照之后的走势)。
    """
    if pens.as_of_day != structure.as_of_day:
        return f"pen as_of_day {pens.as_of_day!r} != structure {structure.as_of_day!r}"
    if pens.kernel_version != CHAN_PEN_RULES_VERSION or pens.rules.kernel_version != CHAN_PEN_RULES_VERSION:
        return f"pen kernel version {pens.kernel_version!r} is not {CHAN_PEN_RULES_VERSION!r}"

    as_of_day = structure.as_of_day
    merged_total = len(structure.merged_bars)
    fractals = {f.position_merged_index: f for f in structure.fractals}
    previous_end: PenEndpoint | None = None

    for position, pen in enumerate(pens.pens):
        for side, endpoint in (("start", pen.start), ("end", pen.end)):
            if not 0 <= endpoint.merged_index < merged_total:
                return f"pen {position} {side} merged index {endpoint.merged_index} out of range"
            fractal = fractals.get(endpoint.merged_index)
            if fractal is None or (
                fractal.kind,
                fractal.position_day,
                fractal.position_price,
                fractal.status,
            ) != (endpoint.kind, endpoint.position_day, endpoint.price, "confirmed"):
                return (
                    f"pen {position} {side} endpoint "
                    f"{endpoint.merged_index}/{endpoint.position_day}/{endpoint.price} "
                    "does not belong to this snapshot's confirmed fractals"
                )
            if endpoint.status not in ("candidate", "confirmed"):
                return f"pen {position} {side} has unknown endpoint status {endpoint.status!r}"
            if endpoint.status == "confirmed" and endpoint.confirmed_on is None:
                return f"pen {position} {side} is confirmed without a confirmation day"
            if as_of_day is not None and endpoint.position_day > as_of_day:
                return f"pen {position} {side} position day {endpoint.position_day} is past as_of_day"

        if pen.start.kind == pen.end.kind:
            return f"pen {position} joins two {pen.start.kind} fractals"
        expected_direction = "up" if pen.start.kind == "bottom" else "down"
        if pen.direction != expected_direction:
            return f"pen {position} direction {pen.direction!r} != {expected_direction!r}"
        if pen.index_gap < MIN_INDEX_GAP:
            return f"pen {position} index gap {pen.index_gap} < {MIN_INDEX_GAP}"

        if pen.status == "confirmed":
            if pen.confirmed_on is None:
                return f"pen {position} is confirmed without a confirmation day"
            if as_of_day is None or pen.confirmed_on > as_of_day:
                return f"pen {position} confirmed on {pen.confirmed_on} after as_of_day"
            if pen.confirmed_on <= pen.start.position_day:
                return f"pen {position} confirmed on {pen.confirmed_on}, not after its own start"
            if pen.end.status != "confirmed":
                return f"pen {position} is confirmed although its end is {pen.end.status!r}"
        elif pen.status == "candidate":
            if pen.confirmed_on is not None:
                return f"pen {position} is a candidate with a confirmation day"
            if pen.end.status != "candidate":
                return f"pen {position} is a candidate although its end is {pen.end.status!r}"
        elif pen.status == "blocked":
            if pen.confirmed_on is not None:
                return f"pen {position} is blocked with a confirmation day"
            if not any(pen.start.merged_index <= t <= pen.end.merged_index for t in pens.taint_indices):
                return f"pen {position} is blocked without a tainted window in its span"
        else:
            return f"pen {position} has unknown status {pen.status!r}"

        if previous_end is not None and (
            pen.start.merged_index,
            pen.start.position_day,
            pen.start.price,
        ) != (previous_end.merged_index, previous_end.position_day, previous_end.price):
            return f"pen {position} does not continue the chain from the previous pen"
        previous_end = pen.end
    return None


# ── 线段(segment) + 操作口径中枢(operational centre) ──────────────────────

#: 每个状态的对外说明: 措辞本身是契约的一部分 —— 「不够」不许说成「无」, 「操作口径」
#: 不许说成「中枢」/「严格中枢」。
_STATUS_NOTES: dict[str, str] = {
    "centre": (
        "已由前 3 条已确认线段测出操作口径区间(zd/zg); 这是 basis=segment_a0 / "
        "strictness=operational_not_strict 的几何, 不是「中枢」, 也不是「严格中枢」"
    ),
    "no_centre": (
        "实测结果: 前 3 条已确认线段的区间没有严格正宽度交集(measured absence), "
        "不是缺少数据, 也不是等一等就会出现"
    ),
    "not_enough_confirmed_segments": (
        "已确认线段不足 3 条, 尚不能给出任何中枢结论; 这**不等于**「无中枢」, "
        "也不等于「中枢未形成」—— 等更多线段确认是唯一出路"
    ),
    "blocked_before_centre": (
        "线段内核在凑够 3 条已确认线段之前就拒绝出版(blocked), 数据不完整; "
        "既不说有中枢, 也不说无中枢"
    ),
}

_CENTRE_LABEL_NOTE = (
    "字段名 operational_centre 指 basis=segment_a0 / strictness=operational_not_strict "
    "的操作口径几何(前 3 条已确认线段区间之交); 不是「中枢」, 不是「严格中枢」"
    "(第18课定义未实现), 不是买卖点, 不构成交易信号"
)

_SEGMENT_NOTE = (
    "线段=chan-segment-v2 第67课第一种情况(无缺口)的规则几何; 只有 confirmed 前缀可当"
    "几何使用, 尾部 candidate 终点仍可漂移, blocked/pending 是内核拒绝出版, 不提供价格几何; "
    "证据笔(特征序列)与几何跨度是两套集合, 分别计数"
)


def _segment_endpoint_payload(endpoint) -> dict:
    return {
        "kind": endpoint.kind,
        "pen_index": endpoint.pen_index,
        "merged_index": endpoint.merged_index,
        "position_day": endpoint.position_day,
        "price": endpoint.price,
        "status": endpoint.status,
        "confirmed_on": endpoint.confirmed_on,
        "revisions": endpoint.revisions,
        "drift_days": list(endpoint.drift_days),
    }


def _segment_geometry_payload(segment: Segment) -> dict:
    """只放**几何跨度**: 笔区间 + 两端点。证据笔不在这里(见 evidence)。"""
    return {
        "direction": segment.direction,
        "pen_start_index": segment.pen_start_index,
        "pen_end_index": segment.pen_end_index,
        "pens_used": segment.pens_used,
        "start": _segment_endpoint_payload(segment.start),
        "end": _segment_endpoint_payload(segment.end),
    }


def _segment_evidence_payload(segment: Segment) -> dict:
    """特征序列(证据笔): 与几何跨度分开, 右元素通常落在终点之后。"""
    return {
        "pen_indices": list(segment.evidence_pen_indices),
        "confirmed_on": segment.evidence_confirmed_on,
        "fractal_elements": [
            {
                "role": element.role,
                "pen_indices": list(element.pen_indices),
                "high": element.high,
                "low": element.low,
                "confirmed_on": element.confirmed_on,
            }
            for element in segment.evidence
        ],
        "note": (
            "证据笔=支撑该线段成立的特征序列笔, 不等于几何跨度内的笔"
            "(右元素在终点之后); 二者分别计数, 不许合并"
        ),
    }


def _confirmed_segment_payload(index: int, segment: Segment) -> dict:
    return {
        "index": index,
        "status": "confirmed",
        "reason": segment.reason,
        "confirmed_on": segment.confirmed_on,
        "kernel_version": segment.kernel_version,
        "characteristic_elements": segment.characteristic_elements,
        "geometry": _segment_geometry_payload(segment),
        "evidence": _segment_evidence_payload(segment),
    }


def _candidate_segment_payload(index: int, segment: Segment) -> dict:
    return {
        "index": index,
        "status": "candidate",
        "reason": segment.reason,
        "confirmed_on": None,
        # 尾部候选的终点仍可漂移: 明确标注, 免得被当成已确认几何
        "driftable": True,
        "note": "尾部候选线段: 终点价格仍可漂移, 不是已确认几何, 不得当作中枢输入",
        "kernel_version": segment.kernel_version,
        "characteristic_elements": segment.characteristic_elements,
        "geometry": _segment_geometry_payload(segment),
        "evidence": _segment_evidence_payload(segment),
    }


def _blocked_segment_payload(index: int, segment: Segment) -> dict:
    """blocked = 内核拒绝出版线段。**不提供价格几何**: 一旦给区间就会被画成线。"""
    return {
        "index": index,
        "status": "blocked",
        "reason": segment.reason,
        "direction": segment.direction,
        "pen_start_index": segment.pen_start_index,
        "pen_end_index": segment.pen_end_index,
        "pens_used": segment.pens_used,
        "geometry_withheld": True,
        "note": "内核拒绝出版该线段(blocked): 不提供价格区间, 不得作为几何/中枢输入",
    }


def _pending_payload(pending) -> dict:
    return {
        "status": pending.status,
        "reason": pending.reason,
        "direction": pending.direction,
        "pen_start_index": pending.pen_start_index,
        # 只有"对某个跨度下判断"的拒绝才有 would-be 终点; 决策笔拒绝时恒为 None
        "end_pen_index": pending.end_pen_index,
        "blocked_at_pen_index": pending.blocked_at_pen_index,
        "pens_used": pending.pens_used,
        "confirmed_pens": pending.confirmed_pens,
        # 审计用: 这条链起点当时是什么, 不是一条线段的端点几何
        "audit": {
            "start_position_day": pending.start_position_day,
            "start_price": pending.start_price,
        },
        "detail": pending.detail,
        "note": "pending 不是线段: 没有几何可画, 只报告内核拒绝的原因与位置",
    }


def build_segments_payload(segment_snapshot: SegmentSnapshot) -> dict:
    """线段快照 → 对外块: confirmed 前缀(几何) + 候选尾(可漂移) + pending/blocked(原因)。

    ``blocked`` 不携带价格几何; ``evidence`` 与 ``geometry`` 分开。任何"状态与位置
    自相矛盾"的快照(confirmed 出现在非 confirmed 之后、confirmed 却带着拒绝原因、
    候选不在尾部)都抛 ``ValueError``, 由端点整份 409 —— 不返回"看起来完整"的混合结果。
    """
    if not isinstance(segment_snapshot, SegmentSnapshot):
        raise ValueError("segment_snapshot must be a SegmentSnapshot")
    if segment_snapshot.kernel_version != SEGMENT_KERNEL_VERSION:
        raise ValueError(
            f"segment snapshot kernel {segment_snapshot.kernel_version!r} is not "
            f"{SEGMENT_KERNEL_VERSION!r}"
        )

    confirmed: list[dict] = []
    blocked: list[dict] = []
    candidate_tail: dict | None = None
    seen_non_confirmed = False
    for index, segment in enumerate(segment_snapshot.segments):
        if segment.status == "confirmed":
            if seen_non_confirmed:
                raise ValueError(
                    f"confirmed segment at index {index} appears after a non-confirmed one; "
                    "the confirmed prefix must be the leading run"
                )
            if segment.reason != "first_case_no_gap":
                raise ValueError(
                    f"segment {index} claims confirmed with reason {segment.reason!r}; "
                    "the kernel never emits that, so this snapshot is doctored"
                )
            confirmed.append(_confirmed_segment_payload(index, segment))
            continue
        seen_non_confirmed = True
        if segment.status == "candidate":
            if index != len(segment_snapshot.segments) - 1:
                raise ValueError(f"candidate segment {index} is not the tail")
            candidate_tail = _candidate_segment_payload(index, segment)
        elif segment.status == "blocked":
            blocked.append(_blocked_segment_payload(index, segment))
        else:  # pragma: no cover - the kernel's Literal already forbids this
            raise ValueError(f"segment {index} has unknown status {segment.status!r}")

    prefix = segment_snapshot.confirmed_segments
    if len(prefix) != len(confirmed) or any(
        left is not right for left, right in zip(prefix, segment_snapshot.segments[: len(prefix)])
    ):
        # 前缀必须是**对象相同**的前导连续段: 数一下长度不够, 还要位置一致。
        raise ValueError(
            "confirmed_segments is not the leading run of this snapshot's segments; "
            "refusing to publish a prefix that skips a refusal"
        )

    counts = {
        "confirmed": len(confirmed),
        "candidate": 0 if candidate_tail is None else 1,
        "blocked": len(blocked),
        "pending": len(segment_snapshot.pending),
        "total": len(segment_snapshot.segments),
    }
    if counts["confirmed"] + counts["candidate"] + counts["blocked"] != counts["total"]:
        raise ValueError("segment counts do not add up; the snapshot is inconsistent")

    return {
        "schema": SEGMENTS_SCHEMA,
        "kernel_version": segment_snapshot.kernel_version,
        "rules_version": segment_snapshot.kernel_version,
        "source_pen_kernel_version": segment_snapshot.source_pen_kernel_version,
        "as_of_day": segment_snapshot.as_of_day,
        "input_digest": segment_snapshot.input_digest,
        "min_pens": MIN_PENS,
        # 几何: 只给 confirmed 前缀
        "confirmed": confirmed,
        # 尾部候选: 明确 driftable, 不是几何
        "candidate_tail": candidate_tail,
        # 拒绝/未决: 原因 + 位置, blocked 无价格几何
        "pending": [_pending_payload(p) for p in segment_snapshot.pending],
        "blocked": blocked,
        "rejections": [
            {
                "pen_index": rejection.pen_index,
                "position_day": rejection.position_day,
                "price": rejection.price,
                "reason": rejection.reason,
                "detail": rejection.detail,
                "audit_only": True,
            }
            for rejection in segment_snapshot.rejections
        ],
        "counts": counts,
        "note": _SEGMENT_NOTE,
    }


def build_operational_centre_payload(result: CentreAdapterResult) -> dict:
    """适配器结果 → 对外块。``basis``/``strictness``/``status``/``issues`` 原样透传。

    ``zone is None`` 有四种含义, 由 ``status`` + ``status_note`` 区分: 实测无交集
    (``no_centre``)、还没凑够线段(``not_enough_confirmed_segments``)、内核在该点之前
    就拒绝出版(``blocked_before_centre``)。任何一种都不许被写成「中枢」。
    """
    if not isinstance(result, CentreAdapterResult):
        raise ValueError("result must be a CentreAdapterResult")
    if result.basis != OPERATIONAL_CENTRE_BASIS or result.strictness != OPERATIONAL_CENTRE_STRICTNESS:
        raise ValueError(
            f"centre adapter labels {result.basis!r}/{result.strictness!r} are not "
            f"{OPERATIONAL_CENTRE_BASIS!r}/{OPERATIONAL_CENTRE_STRICTNESS!r}; refusing to republish them"
        )
    if result.status not in _STATUS_NOTES:  # pragma: no cover - Literal-guarded upstream
        raise ValueError(f"unknown operational centre status {result.status!r}")
    if tuple(result.limitations) != OPERATIONAL_CENTRE_LIMITATIONS:
        # 限制清单是内核契约的一部分: 漂移了就不许再声称"原样透传"。
        raise ValueError("centre adapter limitations drifted from the kernel contract")

    centre = result.centre
    if (result.status == "centre") != (centre is not None):
        raise ValueError(
            f"status {result.status!r} disagrees with whether a centre exists; refusing to publish"
        )

    zone = None
    if centre is not None:
        zone = {"zd": centre.zd, "zg": centre.zg, "width": centre.zg - centre.zd}

    source = result.source
    return {
        "schema": OPERATIONAL_CENTRE_SCHEMA,
        "adapter_version": result.adapter_version,
        "input_kind": result.input_kind,
        "centre_input_kind": result.centre_input_kind,
        "segments_engine": result.segments_engine,
        # ↓↓↓ 以下五项必须原样透传, 任何改名都会被读成更强的结论
        "basis": result.basis,
        "strictness": result.strictness,
        "status": result.status,
        "chain_complete": result.chain_complete,
        "limitations": list(result.limitations),
        "as_of_day": source.as_of_day,
        "absence_reason": result.absence_reason,
        "confirmed_prefix_length": result.confirmed_prefix_length,
        "total_segments": result.total_segments,
        "zone": zone,
        "segment_indices": None if centre is None else list(centre.segment_indices),
        "structure_start_day": None if centre is None else centre.structure_start_day,
        "structure_start_price": None if centre is None else centre.structure_start_price,
        "confirmed_on": None if centre is None else centre.confirmed_on,
        "visible_on": result.visible_on,
        "lifecycle_status": result.lifecycle_status,
        "issues": [
            {
                "reason": issue.reason,
                "detail": issue.detail,
                "blocking": issue.blocking,
                "segment_index": issue.segment_index,
                "pen_index": issue.pen_index,
            }
            for issue in result.issues
        ],
        "source": {
            "symbol": source.symbol,
            "adjust": source.adjust,
            "pen_digest": source.pen_digest,
            "segment_kernel": source.segment_kernel,
            "pen_kernel": source.pen_kernel,
        },
        "status_note": _STATUS_NOTES[result.status],
        "label_note": _CENTRE_LABEL_NOTE,
    }


def build_segment_snapshot(pen_snapshot: PenSnapshot, *, symbol: str) -> SegmentSnapshot:
    """在**同一个**笔快照上算线段: 不取数、不重算分型/笔。

    ``adjust='unknown'`` 与响应里的 ``adjustment='unknown'`` 同源: 本适配器无法证明
    复权口径, 于是声明未知, 绝不冒充 none/qfq/hfq。``as_of_day`` 直接取笔快照的截止日
    (内核会拒绝不一致的绑定)。
    """
    binding = SegmentBinding(
        symbol=symbol,
        adjust="unknown",
        as_of_day=pen_snapshot.as_of_day,
    )
    return segment_from_pens(pen_snapshot, binding=binding)


def _segment_consistency_error(
    segment_snapshot: SegmentSnapshot,
    pen_snapshot: PenSnapshot,
    structure: ChanSnapshot,
    *,
    symbol: str,
) -> str | None:
    """线段块是否与本次分型/笔快照同源且自洽; ``None`` = 自洽, 否则返回诊断(仅进日志)。"""
    if segment_snapshot.kernel_version != CHAN_SEGMENT_RULES_VERSION:
        return f"segment kernel {segment_snapshot.kernel_version!r}"
    if segment_snapshot.source_pen_kernel_version != pen_snapshot.kernel_version:
        return "segment snapshot was not derived from this pen kernel"
    if segment_snapshot.as_of_day != structure.as_of_day or pen_snapshot.as_of_day != structure.as_of_day:
        return (
            f"as_of mismatch: segments {segment_snapshot.as_of_day!r}, pens "
            f"{pen_snapshot.as_of_day!r}, structure {structure.as_of_day!r}"
        )
    if segment_snapshot.input_digest != segment_input_digest(pen_snapshot):
        return "segment input_digest is not this pen series"
    if segment_snapshot.binding.symbol != symbol or segment_snapshot.binding.adjust != "unknown":
        return (
            f"segment binding {segment_snapshot.binding.symbol!r}/"
            f"{segment_snapshot.binding.adjust!r} is not {symbol!r}/unknown"
        )

    pens = pen_snapshot.pens
    expected_start = 0
    for position, segment in enumerate(segment_snapshot.confirmed_segments):
        if segment.pen_start_index != expected_start:
            return (
                f"confirmed segment {position} starts at pen {segment.pen_start_index}, "
                f"expected the contiguous chain start {expected_start}"
            )
        if not (0 <= segment.pen_start_index < len(pens) and segment.pen_end_index < len(pens)):
            return f"confirmed segment {position} points outside the pen series"
        start_pen = pens[segment.pen_start_index]
        end_pen = pens[segment.pen_end_index]
        if start_pen.status != "confirmed" or end_pen.status != "confirmed":
            return f"confirmed segment {position} spans a pen that is not confirmed"
        if (
            start_pen.start.merged_index,
            start_pen.start.position_day,
            start_pen.start.price,
            start_pen.start.kind,
        ) != (
            segment.start.merged_index,
            segment.start.position_day,
            segment.start.price,
            segment.start.kind,
        ):
            return f"confirmed segment {position} start does not match its own start pen"
        if (
            end_pen.end.merged_index,
            end_pen.end.position_day,
            end_pen.end.price,
            end_pen.end.kind,
        ) != (
            segment.end.merged_index,
            segment.end.position_day,
            segment.end.price,
            segment.end.kind,
        ):
            return f"confirmed segment {position} end does not match its own end pen"
        if segment.confirmed_on is None:
            return f"confirmed segment {position} has no confirmation day"
        if segment.confirmed_on <= segment.end.position_day:
            return f"confirmed segment {position} was confirmed on its own end day"
        if structure.as_of_day is not None and segment.confirmed_on > structure.as_of_day:
            return f"confirmed segment {position} is confirmed after as_of_day"
        expected_start = segment.pen_end_index + 1
    return None


# ── 端点 ────────────────────────────────────────────────────────────────


def _reject(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _now() -> datetime:
    """当前时刻(带时区)。独立函数只为让测试钉住"今天", 不随运行日期漂移。"""
    return datetime.now(ZoneInfo(_APP_TZ))


def _resolve_daily_interval(interval: str) -> str:
    from src.web.api.klines import resolve_interval

    try:
        resolved = resolve_interval(interval)
    except HTTPException:
        raise _reject(422, "仅支持日线(interval=1d), 分钟/周/月线不提供分型") from None
    if resolved != "1d":
        raise _reject(422, "仅支持日线(interval=1d), 分钟/周/月线不提供分型")
    return resolved


def _sanitized_upstream_error(symbol: str, exc: BaseException) -> HTTPException:
    """上游取数失败 → 只给固定文案。

    取数链的异常里可能带 DSN/ip/内部路径; 图表主接口的历史文案有透传 ``{e}`` 的
    地方, 本端点不复制这种风险: 细节只进服务端日志(且只记异常类型)。
    """
    logger.warning("chan-structure 取数失败(%s): %s", symbol, type(exc).__name__)
    if isinstance(exc, HTTPException) and exc.status_code in (401, 403):
        return HTTPException(exc.status_code, "无权访问该K线数据")
    return HTTPException(503, "K线数据源暂不可用")


@router.get("/{symbol}/chan-structure")
def get_chan_structure(
    symbol: str,
    snapshot: str = Query(..., description="图表 GET klines 响应里的 chan_snapshot.digest"),
    instrument: str = Query(
        ...,
        description="显式 stock | index(必填; 缺参 422, auto 不被接受)",
    ),
    market: str = Query("CN", description="仅支持 CN"),
    interval: str = Query("1d", description="仅支持 1d"),
    days: int = Query(60, ge=1, le=2000, description="必须与图表请求的 days 一致"),
    _: User = Depends(get_current_user),
) -> dict:
    """只读缠论分型 + 笔, 与图表快照内容严格绑定。

    ``instrument`` 是**必填**参数而不是默认 ``stock``: 默认值会把"调用方忘了说"
    记成"调用方声明了股票", 而 A 股同码(000001)下这个差别就是两只不同标的。

    鉴权: 只要**真实用户身份**(`get_current_user`)。服务令牌不够 —— 结构结论会
    被当成"这张图的缠论结构"引用, 不能由无用户的批处理通道代签。

    返回 ``fractals`` 与 ``pens``, 两者来自**同一个** ``ChanSnapshot``(一次取数、
    一次结构构建)。笔带端点位置(合并索引/发生日/价格/包络)、端点状态、起点终点、
    ``confirmed_on`` 与 ``kernel_version``, 顶层 ``pen_rules_version`` 与之同源。
    笔候选与已确认都只是规则几何: ``adjustment``/``finality`` 仍是 unknown/unverified,
    ``pen_disclaimer`` 明写"候选端点仍可漂移、不是交易信号"。

    状态即语义:
      401 未登录(含 token 失效); 403 账号禁用/注销;
      422 参数/数据不满足前提(市场、周期、重复或乱序日期、未来日期、无已完成K线);
      409 摘要与本次服务端响应不一致(图表已变/跨 worker 不一致/身份不符或畸形),
          或笔内核拒绝 / 笔与本次分型快照不自洽 —— 这两种情况整份失败, 不返回半份结果。
    """
    # ① 参数必须先收敛再取数: 不猜测市场, 不接受 auto(那是"让服务端猜")。
    if (market or "").strip().upper() != "CN":
        raise _reject(422, "仅支持 CN 日线分型; 其他市场不提供(不猜测)")
    declared_raw = (instrument or "").strip().lower()
    if declared_raw not in _SUPPORTED_INSTRUMENTS:
        raise _reject(422, "instrument 必须显式为 stock 或 index(auto 不被接受)")
    declared: Literal["stock", "index"] = "index" if declared_raw == "index" else "stock"
    resolved_interval = _resolve_daily_interval(interval)
    if not isinstance(snapshot, str) or not _DIGEST_RE.match(snapshot):
        # 截短摘要/大写/非十六进制: 拒绝而不是"补齐/小写化", 那会掩盖客户端的错误绑定。
        raise _reject(422, "snapshot 必须是图表响应 chan_snapshot.digest 的完整 64 位小写十六进制")

    # ② 取同一份图表响应: 显式传 instrument, 绝不用 auto(否则服务端按代码猜指数/个股,
    #    000001 这种同码会被猜错, 摘要与结构都会指向另一个标的)。
    from src.web.api import klines as klines_api

    try:
        response = klines_api.get_klines(
            symbol=symbol,
            market="CN",
            days=days,
            interval=resolved_interval,
            instrument=declared,
        )
    except HTTPException as exc:
        raise _sanitized_upstream_error(symbol, exc) from None
    except Exception as exc:  # noqa: BLE001 - 取数链任何异常都不许 500 透传
        raise _sanitized_upstream_error(symbol, exc) from None
    if not isinstance(response, dict):
        raise _reject(503, "K线数据源暂不可用")

    # ③ 响应自述身份必须与请求一致。调用方的 instrument 只是"声明", 这里用响应的
    #    is_index 反查; 同码指数/个股(000001)不许互证。畸形标记(字符串/数字/None)
    #    一律拒绝 —— 猜成 stock 就是把指数当个股算, 猜成 index 则反向。
    response_symbol = response.get("symbol")
    response_market = str(response.get("market") or "").strip().upper()
    response_interval = str(response.get("interval") or "").strip().lower()
    response_bars = response.get("klines")
    try:
        response_identity = response_instrument(response)
        identity_basis = instrument_identity_basis(response)
    except ValueError:
        raise _reject(409, "图表快照身份标记畸形(is_index 必须是布尔值), 请刷新图表") from None
    if (
        response_symbol != symbol
        or response_market != "CN"
        or response_interval != resolved_interval
        or response_identity != declared
        or not isinstance(response_bars, list)
        or not all(isinstance(bar, dict) for bar in response_bars)
    ):
        raise _reject(409, "图表快照身份与请求不一致, 请刷新图表")

    # ④ 用同一响应的**精确内容**重算摘要, 再与调用方回传的摘要比对。
    #    摘要含 partial/source/today_bar: 同价不同含义(未定型/换源)也算"图变了"。
    try:
        recomputed = compute_snapshot_digest(
            symbol=symbol,
            market=response_market,
            instrument=declared,
            interval=resolved_interval,
            bars=response_bars,
            source=response.get("source"),
            today_bar=response.get("today_bar"),
        )
    except ValueError:
        raise _reject(409, "图表快照内容不可校验, 请刷新图表") from None
    if not hmac.compare_digest(recomputed, snapshot):
        raise _reject(409, "图表快照已变化(内容摘要不匹配), 请刷新图表")

    # ⑤ 当日 bar 不参与计算(适配器按 Asia/Shanghai 截断, 不猜是否收盘)。
    now = _now()
    try:
        chart = adapt_chart_bars(response, symbol=symbol, instrument=declared, now=now)
    except ValueError as exc:
        status, detail = _SAFE_BAR_ERRORS.get(str(exc), (422, "K线内容不满足分型计算前提, 拒绝计算"))
        raise _reject(status, detail) from None

    # 没有任何已完成的K线: 摘要仍能对上(空序列也有摘要), 于是"200 + 空分型/空笔"会
    # 被读成"这张图没有结构"。那是无数据冒充成功 —— 必须拒绝。
    if not chart.bars:
        raise _reject(422, "没有已完成的K线, 拒绝计算分型/笔(不以空结构冒充成功)")

    try:
        structure = build_chan_structure(chart.bars)
    except ValueError as exc:
        status, detail = _SAFE_BAR_ERRORS.get(str(exc), (422, "K线内容不满足分型计算前提, 拒绝计算"))
        raise _reject(status, detail) from None

    fractals = [
        {
            "kind": fractal.kind,
            "status": fractal.status,
            "position_day": fractal.position_day,
            "position_price": fractal.position_price,
            "confirmed_on": fractal.confirmed_on,
        }
        for fractal in structure.fractals
    ]

    # ⑥ 笔: 输入就是上面那个 structure 对象 —— 一次取数、一次 build, 不为笔二次取数,
    #    也不重算一遍结构(两份快照各自"自洽"却指向不同数据时, 重算会静默混用)。
    try:
        pen_snapshot = pen_from_snapshot(structure)
    except ValueError as exc:
        # 内核复核失败 = 这份快照内部不自洽; 不修、不猜, 整份拒绝(细节只进日志)。
        logger.warning("chan-pen 内核拒绝(%s): %s", symbol, type(exc).__name__)
        raise _reject(409, "结构快照内部不自洽, 笔内核拒绝计算; 请刷新图表") from None

    problem = _pen_consistency_error(pen_snapshot, structure)
    if problem is not None:
        # 分型与笔不是同一份数据(或笔自相矛盾): 返回任何一半都是误导。
        logger.warning("chan-pen 自洽性校验失败(%s): %s", symbol, problem)
        raise _reject(409, "笔与本响应分型快照不自洽, 拒绝返回半份结果; 请刷新图表") from None

    pens = [_pen_payload(pen) for pen in pen_snapshot.pens]

    # ⑦ 线段: 输入就是上面那个 pen_snapshot —— 同一次取数 / 一次结构构建 / 一次笔,
    #    不为线段二次取数, 也不重算分型或笔(重算会静默混用两份"各自自洽"的快照)。
    try:
        segment_snapshot = build_segment_snapshot(pen_snapshot, symbol=symbol)
    except ValueError:
        logger.warning("chan-segment 内核拒绝(%s): %s", symbol, "ValueError")
        raise _reject(409, "线段内核拒绝在本次快照上计算; 请刷新图表") from None

    segment_problem = _segment_consistency_error(
        segment_snapshot, pen_snapshot, structure, symbol=symbol
    )
    if segment_problem is not None:
        logger.warning("chan-segment 自洽性校验失败(%s): %s", symbol, segment_problem)
        raise _reject(409, "线段与本次分型/笔快照不自洽, 拒绝返回半份结果; 请刷新图表") from None

    try:
        segments_block = build_segments_payload(segment_snapshot)
    except ValueError as exc:
        logger.warning("chan-segment payload 拒绝(%s): %s", symbol, type(exc).__name__)
        raise _reject(409, "线段快照状态与位置自相矛盾, 请刷新图表") from None

    # ⑧ 操作口径中枢: 适配器只吃**这一个**线段快照 + 产出它的同一份笔快照
    #    (范围来自笔, 不是端点对); 任一侧不自洽就整份拒绝。
    try:
        centre_result = centre_from_segment_snapshot(
            segment_snapshot, pen_snapshot=pen_snapshot
        )
    except ValueError as exc:
        logger.warning("chan-centre 适配器拒绝(%s): %s", symbol, type(exc).__name__)
        raise _reject(409, "操作口径中枢适配器拒绝本次线段快照, 请刷新图表") from None

    # 适配器自述的来源必须就是这份快照: 标的 / 截止日 / 笔序列摘要 / 两个内核版本。
    if (
        centre_result.source.symbol != symbol
        or centre_result.source.as_of_day != structure.as_of_day
        or centre_result.source.pen_digest != segment_snapshot.input_digest
        or centre_result.source.segment_kernel != CHAN_SEGMENT_RULES_VERSION
        or centre_result.source.pen_kernel != pen_snapshot.kernel_version
    ):
        logger.warning("chan-centre 来源不一致(%s): 适配器结果不属于本次快照", symbol)
        raise _reject(409, "操作口径中枢结果不属于本次快照, 拒绝返回; 请刷新图表") from None

    try:
        centre_block = build_operational_centre_payload(centre_result)
    except ValueError as exc:
        logger.warning("chan-centre payload 拒绝(%s): %s", symbol, type(exc).__name__)
        raise _reject(409, "操作口径中枢标签自相矛盾, 请刷新图表") from None

    return {
        "symbol": symbol,
        "market": "CN",
        "instrument": declared,
        # 身份依据仅表示标记来自响应或调用路径，不代表已独立核验上游证券身份。
        "instrument_basis": identity_basis,
        "interval": resolved_interval,
        "rules_version": CHAN_FRACTAL_RULES_VERSION,
        "snapshot_digest": recomputed,
        "as_of_day": structure.as_of_day,
        "omitted_latest_reason": chart.omitted_latest_reason,
        "source": chart.source,
        # 本适配器无法证明复权口径/来源终局性: 恒为 unknown/unverified。
        "adjustment": "unknown",
        "finality": "unverified",
        "fractals": fractals,
        "counts": {
            "total": len(fractals),
            "confirmed": sum(1 for f in fractals if f["status"] == "confirmed"),
            "candidate": sum(1 for f in fractals if f["status"] == "candidate"),
            "blocked": sum(1 for f in fractals if f["status"] == "blocked"),
        },
        "pens": pens,
        "pen_rules_version": CHAN_PEN_RULES_VERSION,
        "pen_counts": {
            "total": len(pens),
            "confirmed": sum(1 for p in pens if p["status"] == "confirmed"),
            "candidate": sum(1 for p in pens if p["status"] == "candidate"),
            "blocked": sum(1 for p in pens if p["status"] == "blocked"),
        },
        # 线段: confirmed 前缀才是几何, 候选尾可漂移, blocked/pending 只给原因
        "segments": segments_block,
        # 操作口径中枢: basis=segment_a0 / strictness=operational_not_strict
        "operational_centre": centre_block,
        "not_implemented": list(_NOT_IMPLEMENTED),
        "disclaimer": (
            "分型、笔与线段均为合并K线/笔链上的规则几何; operational_centre 只是 "
            "basis=segment_a0 的操作口径几何(不是「中枢」, 不是「严格中枢」), "
            "不含买卖点, 不构成交易建议"
        ),
        "pen_disclaimer": (
            "笔=相邻两个已确认分型的合法连接(规则几何): 已确认笔在反向分型右邻封口当日冻结, "
            "最后一笔为候选(candidate)且端点价格仍可漂移; 数据最终性与复权口径未知, 不是交易信号"
        ),
        "segment_disclaimer": (
            "线段=chan-segment-v2 第67课第一种情况的规则几何: 只有 confirmed 前缀可当几何, "
            "尾部候选终点仍可漂移, blocked/pending 不提供价格几何; "
            "操作口径中枢不是严格中枢, 也不是买卖点/交易信号"
        ),
    }
