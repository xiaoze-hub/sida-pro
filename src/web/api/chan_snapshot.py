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
* 只输出合并K线**分型**(blocked/candidate/confirmed、发生日、确认日、规则版本)。
  不含笔/线段/中枢/买卖点, 响应里 ``not_implemented`` 明写, 不允许被读成"中枢"。
* 当日 bar 不参与计算(沿用 ``chan_kline_adapter`` 的保守口径), 但仍在摘要里 ——
  摘要描述的是"图表看到的那份响应", 不是"我们要用的那几根"。
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

from src.core.chan_kline_adapter import adapt_chart_bars
from src.core.chan_structure import build_chan_structure
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
#: 只有完整 64 位小写十六进制才接受; 截短/大写/非十六进制一律 422(不"修正")。
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
#: 身份依据: 响应布尔 is_index / 显式 stock 取数路径；均非独立证券主数据核验。
IdentityBasis = Literal["response_flag", "declared_path"]

_NOT_IMPLEMENTED: tuple[str, ...] = (
    "pen",            # 笔
    "segment",        # 线段
    "centre",         # 中枢
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
    """只读缠论分型(合并K线 + 分型), 与图表快照内容严格绑定。

    ``instrument`` 是**必填**参数而不是默认 ``stock``: 默认值会把"调用方忘了说"
    记成"调用方声明了股票", 而 A 股同码(000001)下这个差别就是两只不同标的。

    鉴权: 只要**真实用户身份**(`get_current_user`)。服务令牌不够 —— 结构结论会
    被当成"这张图的缠论结构"引用, 不能由无用户的批处理通道代签。

    状态即语义:
      401 未登录(含 token 失效); 403 账号禁用/注销;
      422 参数/数据不满足前提(市场、周期、重复或乱序日期、未来日期);
      409 摘要与本次服务端响应不一致(图表已变/跨 worker 不一致/身份不符或畸形)。
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
        "not_implemented": list(_NOT_IMPLEMENTED),
        "disclaimer": "仅合并K线的三根分型; 不含笔/线段/中枢/买卖点, 不构成交易建议",
    }
