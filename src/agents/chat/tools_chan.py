# -*- coding: utf-8 -*-
"""数智参谋 · 缠论结构只读工具(2026-11-17)。

为什么必须绑定"某一轮的那一份 K 线"
----------------------------------
缠论分型/笔是"某一份 K 线响应"的函数, 不是"某个代码"的函数。用户在图表上看到的
是 ``GET /api/klines/{symbol}?interval=1d`` 的那一次响应; 如果参谋自己按
symbol/market 再取一次数, 它算出的结构可能来自另一批 bar(跨 worker 缓存、数据源漂移、
当日 bar 盘中变化), 却会被当成"这张图的缠论结构"引用 —— 那是伪造同图证据。

所以本工具**只有一条取数路径**: 把**本轮消息随附**的 ``snapshot_digest`` 原样交给既有
只读端点 ``GET /api/klines/{symbol}/chan-structure``, 由端点用**同一个** ``get_klines``
返回重算摘要并逐字节比对(不符 → 409)。分型/笔全部由端点内部同一份 ``ChanSnapshot``
产出, 本工具**不另算**任何分型/笔, 也不自行取数、不缓存、不猜摘要。

摘要从哪来(诚实边界, 不可绕过)
-------------------------------
首选来源是**本轮消息**随附的 ``chart_snapshot``: 消息入口(``SendMessageBody.chart_snapshot``)
用严格 schema 收下, 再调**同一个**只读端点复核"摘要与服务器此刻这份 K 线一致", 通过后只放进
本轮请求上下文(``harness/context.py::REQ_CHAN_SNAPSHOT``), 不落库、不进会话 initial_context、
不跨轮复用。本工具**只**取它: 模型自带的 ``snapshot_digest`` 不再被接受(2026-11-17 审计缺口 3:
否则上一轮的摘要能在下一轮无声明时被"复用"成同图证据), ``instrument``/``days`` 一律以本轮
复核结果为准; 模型请求**别的 symbol**、或服务端判定的本轮请求目标(含"正文只写名字"解析出的
代码, ``REQ_BOUND``)与快照不是同一只时, 直接拒绝, 不跨标的套用。
即便如此, 这份声明仍来自客户端: 服务端只证明了"摘要与这份 K 线内容一致", **没有、也不能
证明用户屏幕上真的显示了这张图** —— 渲染时不写"已核对屏幕"。

那一轮没带 ``chart_snapshot``(或带了但与本轮请求目标不一致、判不出唯一目标、无法复核)时,
工具退回**严格缺失态**(要求先打开/刷新该标的日线图后重新提问), 绝不自行取一张新快照
冒充"用户那张图", 也不接受调用方(模型)自带摘要。

摘要不进库
----------
完整摘要是"本轮凭据": 轨迹(``copilot_steps.args/result_digest``)与工具日志都由
``harness/redaction.py`` 收缩成前 8 位, 渲染文本也只展示前 8 位 —— 于是它不会被本功能
持久化成下一轮可复用的凭据。用户自己在消息正文里贴的摘要属于用户内容, 系统不改写。

权限与错误
----------
* 无用户身份(定时任务/系统通道/service token) → 拒绝: 结构结论会被当成"这张图的
  缠论结构"引用, 不能由无用户身份代签;
* 有身份但无行情权限(guest 等) → 走 ``core.permissions.enforce_perm`` 同一口径拒绝;
* 端点返回 401/403/409/422/503 一律**如实转述固定文案**, 不打印异常原文/DSN/token,
  也不在失败时返回半份结构。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User

logger = logging.getLogger(__name__)

#: 端点 Query 的上下界(与 schema 保持一致)。
_MIN_DAYS, _MAX_DAYS = 1, 2000

#: 文本渲染上限: 结构是"最近发生了什么", 超长列表对 LLM 只是噪声。
#: 截断时显式写出总条数, 不让读者以为看到的就是全部。
_MAX_FRACTALS = 40
_MAX_PENS = 20

_MISSING_DIGEST = (
    "缺少图表快照摘要: 本轮消息没有随附经服务端复核的图表快照, 未读取任何结构数据。"
    "2026-11-17 起本工具**只**使用本条消息随附的快照(客户端把 `chan_snapshot.digest` "
    "随消息发出, 消息入口用同一取数链重算摘要复核); 模型自带的 snapshot_digest 一律不接受 —— "
    "它可能来自上一轮、别的标的或编造, 无法证明是用户当前这张图。"
    "本工具也不会自行取一张新快照冒充同图 —— 请让用户在图表页打开/刷新该标的日线图后重新提问; "
    "如果拿不到快照, 请如实告诉用户「需要当前图表快照才能对齐缠论结构」, 不要猜、不要编。"
)

def _req_bound_symbol() -> str | None:
    """本轮请求目标(服务端判定, 含名字解析)。取不到就当没有 —— 旁路不阻断。"""
    try:
        from src.harness.context import REQ_BOUND

        value = REQ_BOUND.get()
    except Exception:  # noqa: BLE001 - 上下文缺失等价于"没有判定结果"
        return None
    return str(value or "").strip() or None


def _turn_chart_snapshot() -> dict | None:
    """本轮消息随附、且已由消息入口复核过的图表快照(没有 = 走原始严格缺失态)。

    只读请求上下文: 这份值不落库、不进会话 initial_context, 也不跨轮复用 ——
    下一轮客户端没带, 这里就是 None, 绝不"沿用上一次的图"。
    """
    try:
        from src.harness.context import REQ_CHAN_SNAPSHOT

        ref = REQ_CHAN_SNAPSHOT.get()
    except Exception:  # noqa: BLE001 - 上下文缺失等价于"本轮没带", 不影响原链路
        return None
    if not isinstance(ref, dict):
        return None
    for key in ("symbol", "market", "instrument", "interval", "days", "digest"):
        if ref.get(key) in (None, ""):
            return None
    return ref


def _detail_of(exc) -> str:
    """HTTPException.detail → 安全文本(只取字符串 message, 不序列化整个异常)。"""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        detail = detail.get("message")
    return str(detail or "").strip()


def _status_message(status: int, detail: str) -> str:
    """端点状态码 → 给用户/模型看的固定文案(附端点自己的安全 detail)。"""
    if status == 401:
        base = "未登录或登录已失效, 未读取任何缠论结构。"
    elif status == 403:
        base = "当前账号无权读取该K线/结构数据, 已拒绝, 未返回任何结构。"
    elif status == 409:
        base = (
            "图表快照校验未通过(内容已变化、标的不一致或跨进程缓存不一致), "
            "未返回任何结构, 也不会退回自行取数。请刷新图表后重新获取摘要。"
        )
    elif status == 422:
        base = "参数或数据不满足分型计算前提, 未返回任何结构。"
    elif status == 503:
        base = "K线数据源暂不可用, 未返回任何结构。"
    else:
        base = f"缠论结构接口返回 {status}, 未返回任何结构。"
    return f"{base}({detail})" if detail else base


def _fmt_fractal(f: dict) -> str:
    confirmed = f.get("confirmed_on") or "未确认"
    price = f.get("position_price")
    return (
        f"- {f.get('position_day')} {f.get('kind')} 价格 {price} "
        f"状态 {f.get('status')} 确认日 {confirmed}"
    )


def _fmt_pen(p: dict) -> str:
    start = p.get("start") or {}
    end = p.get("end") or {}
    confirmed = p.get("confirmed_on") or "未确认"
    line = (
        f"- {p.get('direction')} {p.get('status')} "
        f"{start.get('position_day')}@{start.get('price')} → "
        f"{end.get('position_day')}@{end.get('price')} "
        f"间距 {p.get('index_gap')} 确认日 {confirmed} 内核 {p.get('kernel_version')}"
    )
    # 端点漂移必须可见: 否则"挪过一次的端点"看起来和原装端点一样。
    drift = []
    for side, endpoint in (("起点", start), ("终点", end)):
        revisions = endpoint.get("revisions")
        if isinstance(revisions, int) and revisions > 0:
            days = ",".join(str(d) for d in (endpoint.get("drift_days") or []))
            drift.append(f"{side}修订 {revisions} 次(放弃日 {days or '未记录'})")
    if drift:
        line += " " + "; ".join(drift)
    return line


def render_chan_structure(payload: dict, origin: str | None = None) -> str:
    """把端点返回的**统一结果**渲染成人话(字段逐项保留, 不做二次解释)。

    只做三件事: 排序原样、截断时标注总数、把口径/未实现/免责声明放在固定位置。
    不新增任何方向判断, 不把分型/笔叫成中枢或买卖点。

    ``origin`` 是快照归属说明(本轮消息携带/缺失), 只解释"这份结构算的是哪份数据",
    不追加任何方向结论; 措辞里也不许写"已验证用户屏幕"。

    摘要只渲染前 8 位: 完整摘要属于"本轮凭据", 出现在工具结果里就会被轨迹/模型回答
    二次持久化(2026-11-17 审计缺口 2), 而读者只需要能对上"是不是同一份"。
    """
    from src.harness.redaction import short_digest

    fractals = list(payload.get("fractals") or [])
    pens = list(payload.get("pens") or [])
    counts = payload.get("counts") or {}
    pen_counts = payload.get("pen_counts") or {}

    lines: list[str] = [
        "[数据源: 图表日线快照(chan_snapshot.digest 校验) → 只读分型/笔; "
        f"source={payload.get('source') or 'unknown'}; "
        f"adjustment={payload.get('adjustment') or 'unknown'}; "
        f"finality={payload.get('finality') or 'unverified'}]",
        (
            f"【缠论结构·只读观测】{payload.get('symbol')} {payload.get('market')} "
            f"{payload.get('instrument')} (身份依据: {payload.get('instrument_basis')}) "
            f"{payload.get('interval')}"
        ),
        (
            f"截止日 {payload.get('as_of_day')} · 摘要 {short_digest(payload.get('snapshot_digest'))} · "
            f"分型规则 {payload.get('rules_version')} · 笔规则 {payload.get('pen_rules_version')}"
        ),
        (
            f"分型 {counts.get('total', len(fractals))} 个"
            f"(confirmed {counts.get('confirmed', 0)} / candidate {counts.get('candidate', 0)} / "
            f"blocked {counts.get('blocked', 0)}):"
        ),
    ]
    if origin:
        # 归属只解释"这份结构算的是哪份数据", 不给方向结论, 也不写"已验证用户屏幕"。
        lines.insert(2, f"[快照归属: {origin}]")
    shown = fractals[-_MAX_FRACTALS:]
    if len(fractals) > len(shown):
        lines.append(f"(共 {len(fractals)} 条, 仅列最近 {len(shown)} 条)")
    lines.extend(_fmt_fractal(f) for f in shown)
    if not shown:
        lines.append("- 本份快照没有分型(空列表, 未用 0 冒充其他结论)")

    lines.append(
        f"笔 {pen_counts.get('total', len(pens))} 笔"
        f"(confirmed {pen_counts.get('confirmed', 0)} / candidate {pen_counts.get('candidate', 0)} / "
        f"blocked {pen_counts.get('blocked', 0)}):"
    )
    shown_pens = pens[-_MAX_PENS:]
    if len(pens) > len(shown_pens):
        lines.append(f"(共 {len(pens)} 条, 仅列最近 {len(shown_pens)} 条)")
    lines.extend(_fmt_pen(p) for p in shown_pens)
    if not shown_pens:
        lines.append("- 本份快照没有笔(空列表)")

    not_implemented = payload.get("not_implemented") or []
    lines.append(
        "未实现(本工具不提供): " + ", ".join(str(x) for x in not_implemented)
        if not_implemented else "未实现清单缺失, 不得据此推断存在中枢/线段/买卖点"
    )
    if payload.get("omitted_latest_reason"):
        lines.append(f"当日 bar: {payload['omitted_latest_reason']} (不参与计算, 但仍在摘要内)")
    if payload.get("disclaimer"):
        lines.append(f"声明: {payload['disclaimer']}")
    if payload.get("pen_disclaimer"):
        lines.append(f"笔声明: {payload['pen_disclaimer']}")
    return "\n".join(lines)


async def _call_endpoint(*, symbol: str, digest: str, instrument: str, days: int, user) -> dict:
    """调用**同一个**只读端点的内部业务逻辑(不复制分型/笔实现)。

    为什么直接调路由函数: 分型/笔/摘要校验都在 `src/web/api/chan_snapshot.py` 的
    ``get_chan_structure`` 里, 复制一份或另写一个"轻量版"就会产生两套口径。这里只
    负责把已通过本地前置校验的参数原样传进去, 并在同步网络调用外做 ``to_thread``。
    """
    from src.web.api.chan_snapshot import get_chan_structure as endpoint

    return await asyncio.to_thread(
        endpoint,
        symbol=symbol,
        snapshot=digest,
        instrument=instrument,
        market="CN",
        interval="1d",
        days=days,
        _=user,
    )


async def _tool_get_chan_structure(db: Session, args: dict, user: User | None = None) -> str:
    """只读缠论分型/笔: 必须回传真实图表快照摘要, 缺失即严格缺失态。"""
    from src.core.permissions import PERM_VIEW_QUOTE

    # ① 身份: 无用户/服务令牌一律拒绝 —— 结构结论不能由无身份通道代签。
    if user is None or not getattr(user, "id", None) or getattr(user, "is_service", False):
        return "该工具需要已登录的用户身份; 当前会话没有可核验的用户, 未读取任何结构数据。"
    if getattr(user, "is_active", True) is False:
        return "当前账号已禁用或注销, 无权读取缠论结构数据。"

    # ② 权限: 与 HTTP API / 其他聊天工具同源(core.permissions.enforce_perm)。
    from src.agents.chat.registry import _perm_denied

    denied = _perm_denied(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied

    # ③ 信源：**只**接受本轮消息随附、且已由消息入口复核过的快照。
    #    2026-11-17 审计缺口 3: 旧实现允许模型自带 snapshot_digest 当替代 —— 那可能是
    #    上一轮(甚至别的票)的摘要, 与"用户这张图"没有任何可核验关系, 于是"同图"承诺失效。
    #    现在没有本轮快照就是严格缺失态, 模型参数只用来"对照"（symbol 必须是同一只）。
    turned = _turn_chart_snapshot()
    origin: str | None = None
    symbol = str(args.get("symbol") or "").strip().upper()

    if not turned:
        if args.get("snapshot_digest") not in (None, ""):
            # 只记事实, 不记摘要内容 —— 轨迹/日志里也不许留完整摘要(见 harness/redaction.py)。
            logger.info(
                "get_chan_structure 拒绝模型自带 digest(本轮无复核快照) user=%s symbol=%s",
                getattr(user, "id", None), symbol or "-",
            )
            return _MISSING_DIGEST
        logger.info(
            "get_chan_structure 缺少本轮图表快照 user=%s symbol=%s", getattr(user, "id", None), symbol or "-",
        )
        return _MISSING_DIGEST

    if symbol and symbol != turned["symbol"]:
        logger.info(
            "get_chan_structure 拒绝跨标的套用快照 user=%s turn=%s asked=%s",
            getattr(user, "id", None), turned["symbol"], symbol,
        )
        return (
            f"本轮的图表快照绑定的是 {turned['symbol']}"
            f"（{turned['instrument']}, days={turned['days']}）, 与你请求的 {symbol} 不一致; "
            "不跨标的套用别人的快照, 未读取任何结构数据。"
            "请针对当前图表标的提问, 或先让用户把图表切到该标的。"
        )

    # ③-b 第二层归属核对: 本轮请求目标(服务端判定, 含"正文只写名字"解析出的代码)
    #      必须与快照是同一只。消息入口已按同一口径丢弃过一次；这里是工具层的兜底 ——
    #      上下文装配路径变化(或另一轮并发改绑)时也不许出现"REQ_BOUND=600111 却按
    #      000002 算结构"。
    bound = _req_bound_symbol()
    if bound and bound != turned["symbol"]:
        logger.info(
            "get_chan_structure 拒绝: 本轮请求标的与快照不一致 user=%s bound=%s turn=%s",
            getattr(user, "id", None), bound, turned["symbol"],
        )
        return (
            f"本轮请求标的是 {bound}, 而随附的图表快照绑定 {turned['symbol']}, 两者不一致: "
            "不跨标的套用快照, 未读取任何结构数据。请针对当前图表标的提问。"
        )

    _model_digest = args.get("snapshot_digest")
    if _model_digest not in (None, "") and str(_model_digest).strip() != turned["digest"]:
        # 覆盖是确定性行为, 不依赖模型"听话"; 只记事实, 不记摘要内容。
        logger.info(
            "get_chan_structure 以本轮图表快照为准, 忽略模型自带 digest user=%s symbol=%s",
            getattr(user, "id", None), turned["symbol"],
        )
    symbol = turned["symbol"]
    market = turned["market"]
    instrument = turned["instrument"]
    days = turned["days"]
    digest = turned["digest"]
    origin = (
        "本轮消息随附的图表快照(消息入口已用同一取数链重算摘要复核一致; "
        "仅证明与本轮这份K线一致, 未验证用户屏幕)"
    )

    # ④ 取数: 只走既有只读端点的内部业务逻辑; 任何异常都不外抛、不回显原文。
    from fastapi import HTTPException

    try:
        payload = await _call_endpoint(
            symbol=symbol, digest=digest, instrument=instrument, days=days, user=user,
        )
    except HTTPException as exc:
        logger.info(
            "get_chan_structure 端点拒绝 user=%s symbol=%s status=%s",
            getattr(user, "id", None), symbol, exc.status_code,
        )
        return _status_message(int(exc.status_code), _detail_of(exc))
    except Exception as exc:  # noqa: BLE001 - 详情只进服务端日志, 不回给模型/用户
        logger.warning(
            "get_chan_structure 调用失败 user=%s symbol=%s error=%s",
            getattr(user, "id", None), symbol, type(exc).__name__,
        )
        return "缠论结构读取失败(上游异常), 未返回任何结构数据; 详情已记录在服务端日志。请稍后重试。"

    if not isinstance(payload, dict):
        return "缠论结构接口返回了非结构化结果, 未返回任何结构数据。"
    logger.info(
        "get_chan_structure ok user=%s symbol=%s instrument=%s as_of=%s",
        getattr(user, "id", None), symbol, instrument, payload.get("as_of_day"),
    )
    return render_chan_structure(payload, origin=origin)


_CHAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_chan_structure",
        "description": (
            "【只读缠论结构·只用本轮图表快照】返回某份日线快照上的合并K线分型"
            "(blocked/candidate/confirmed + 发生日/确认日/价格)与笔(端点/状态/确认日/规则版本)。"
            "**不接受**你传入的摘要: 快照由客户端随本轮消息附上(`chan_snapshot.digest`), "
            "消息入口已用同一取数链重算摘要复核; 你只需按 system 里的"
            "【本轮图表快照（服务端已复核）】给出 symbol/instrument/days(与服务端复核结果一致)。"
            "本轮没有随附快照时(或你请求的 symbol 与本轮快照标的不是同一只)本工具直接拒绝, "
            "不得沿用上一轮摘要、不得用用户口述摘要替代、不得自己取一张新快照。"
            "严禁编造摘要或把「没拿到快照」写成「这张图没有结构」。"
            "本工具只描述规则几何, 不含线段/中枢/买卖点, 不构成交易信号; "
            "失败/无数据/409 一律如实转述, 不得用猜测补齐。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码, 如 600000"},
                "market": {
                    "type": "string",
                    "description": "市场, 仅 CN(其他市场不提供)",
                    "enum": ["CN"],
                    "default": "CN",
                },
                "instrument": {
                    "type": "string",
                    "description": "标的类型, 按用户当前图表显式声明: stock=个股 / index=指数(必填, 不猜)",
                    "enum": ["stock", "index"],
                },
                "days": {
                    "type": "integer",
                    "description": "必须与用户图表请求的 days 完全一致(如 60/120), 否则摘要对不上",
                    "minimum": _MIN_DAYS,
                    "maximum": _MAX_DAYS,
                },
            },
            # 摘要不再由模型提供(旧客户端/旧提示词多传的 snapshot_digest 会被服务端忽略)。
            "required": ["symbol", "instrument", "days"],
        },
    },
}

CHAN_TOOLS: tuple[tuple[str, dict, str, Any], ...] = (
    (
        "get_chan_structure",
        _CHAN_SCHEMA,
        (
            "图表日线快照(chan_snapshot.digest 绑定 bar 内容) → 合并K线分型/笔; "
            "只认本轮消息随附且经服务端复核的快照(模型自带 digest 不接受); "
            "adjustment=unknown, finality=unverified; 不含中枢/线段/买卖点"
        ),
        _tool_get_chan_structure,
    ),
)


def register_chan_tools() -> None:
    """注册缠论只读工具(只读、无写库; 需要显式快照摘要, 缺失时严格缺失态)。"""
    from src.agents.chat.registry import register_chat_tool

    for name, schema, caliber, handler in CHAN_TOOLS:
        register_chat_tool(name, schema=schema, caliber=caliber, requires="")(handler)


__all__ = [
    "CHAN_TOOLS",
    "register_chan_tools",
    "render_chan_structure",
    "_tool_get_chan_structure",
]
