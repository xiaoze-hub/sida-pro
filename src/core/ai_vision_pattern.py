# -*- coding: utf-8 -*-
"""AI 看图形态 **影子证据层**(2026-10-14 胜率创新 · 主线一并行)。

立项启发(QuantHarness)
----------------------
多 Agent LLM 量化框架的核心启发是: **视觉 LLM 直接"看图认形态"比纯数值规则更自然** ——
`src/core/kline_patterns` 是纯规则 10 形态(严格数学定义), 纯规则写不全
(头肩顶/头肩底/旗形/楔形/三角形等经典形态难以穷举)。本模块做 **AI 看图形态影子层**:

    K线渲染 → 截图 → 视觉模型认形态(形态名/置信度/依据) → 与规则形态同日同票对照落库

产出落 `ai_pattern_shadow` 一张**只读用**新表(与 `kline_patterns` 规则结果并排), 供
「分桶验证」(一致 agree / 分歧 conflict / 仅AI / 仅规则 四组 T+1/T+3 命中差) 使用。
**这不是独立报告**, 而是与规则形态对照的影子证据。

影子运行(不干扰生产)
--------------------
每日盘后对 **自选池 ∪ 前一日有标记的票**(成本可控, 见 ``shadow_universe``)逐票识别,
只写 `ai_pattern_shadow` —— **不改任何现有信号/决策/落库**。表建在
`src/web/migrations.py::_m195`。

视觉模型默认绑定(2026-10-15 用户裁决)
--------------------------------------
`scene='vision'` 默认绑定 sida-pro 自带的 **`agens-3.0-flash`**(多模态)。绑定在
`src.core.ai_client.SCENE_DEFAULT_MODEL_NAMES`(按**模型名**回落, 不锁 model_id);
设置页「场景分配」可改成模型池里的任意模型。未显式绑定且模型池无该默认模型 →
`resolve_vision_model` 返回 None → 显式降级(语义不变)。

复用既有能力(禁重造)
--------------------
- 标的池: `shadow_universe` = 自选池(stocks 表, CN) ∪ 前一日影子表出过形态的票;
- K 线: `KlineCollector.get_klines`(PG 优先);
- 规则形态: `kline_patterns.detect_pattern_marks`;
- 视觉模型: 统一 LLM 配置中心 `get_model_for_scene(db, "vision")`(设置页场景分配);
- 渲染: 仓库内**无** K 线→PNG 现成件(`screenshot_collector` 截的是外部东财/雪球页面,
  不可控窗口且需外网), 故本模块提供一个**仅依赖 Pillow 的本地蜡烛图渲染器**(无网络,
  确定性), 而非新建浏览器截图基建。

缺数据显式(不编造)
------------------
- 无视觉模型(scene='vision' 未绑定 / 绑定模型无 vision 能力) → ``status='degraded'``, 不出形态;
- 视觉调用失败 / 返回不可解析 → ``status='degraded'``, 不出形态;
- K 线不足 / 取不到 → ``status='no_data'``;
- 视觉模型可用但判**无形态** → ``status='ok'`` + 空形态(与"降级"严格区分)。
信号是证据非建议; 形态名只客观描述, 不出"买入/卖出"字样。
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")
_TABLE = "ai_pattern_shadow"
_SCENE = "vision"
#: 该场景默认模型名(与 src.core.ai_client.SCENE_DEFAULT_MODEL_NAMES['vision'] 同源)。
#: 未显式绑定时, 解析到模型池里此名的模型即视为视觉模型(能力由探针实测, 见模块头)。
DEFAULT_VISION_MODEL = "agens-3.0-flash"

#: 默认窗口: 取最近多少根 K 线渲染/识别
DEFAULT_WINDOW = 60
#: 规则侧对照窗口: 只取确认根落在最近 N 根的规则形态(与 AI "看图认当前形态" 同窗口口径)
RULE_LOOKBACK = 5
#: 依据文本截断上限
_BASIS_MAX = 600
_SUMMARY_MAX = 300
#: 渲染尺寸
RENDER_WIDTH = 720
RENDER_HEIGHT = 420

#: 落库列(除主键 trade_date/symbol 外), 与 _m195 表结构一一对应。
FIELDS: tuple[str, ...] = (
    "name", "ai_pattern", "ai_confidence", "ai_basis", "ai_all",
    "model_name", "window_n", "rule_primary", "rule_patterns",
    "compare_status", "status", "source",
)

# 对照结论(四组分桶口径 + 两个非对照态)
COMPARE_AGREE = "agree"            # AI 与规则都出形态且有交集
COMPARE_CONFLICT = "conflict"      # 两侧都出形态但无交集(分歧)
COMPARE_AI_ONLY = "ai_only"        # 仅 AI 出形态
COMPARE_RULES_ONLY = "rules_only"  # 仅规则出形态
COMPARE_NONE = "none"              # 两侧皆判无形态(不计入四组)
COMPARE_DEGRADED = "degraded"      # AI 侧降级(无模型/失败), 无法对照

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _as_num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    return None


def _get(bar, key):
    """兼容 dict 与对象两种 bar 表示(dict / KlineData dataclass / 测试 FakeBar)。"""
    if isinstance(bar, dict):
        return bar.get(key)
    return getattr(bar, key, None)


def _trade_date(now: datetime | None = None) -> str:
    return (now or datetime.now(_CST)).strftime("%Y%m%d")


def _ymd(d) -> str:
    """任意日期 → `YYYYMMDD`(影子表主键口径)。"""
    return str(d or "").strip().replace("-", "")


def _usable_candles(bars) -> list[tuple]:
    """归一化可用的 OHLC(V) 元组列表; 缺任一 OHLC 的根剔除。"""
    out: list[tuple] = []
    for b in bars or []:
        o = _as_num(_get(b, "open"))
        h = _as_num(_get(b, "high"))
        lo = _as_num(_get(b, "low"))
        c = _as_num(_get(b, "close"))
        if None in (o, h, lo, c):
            continue
        out.append((o, h, lo, c, _as_num(_get(b, "volume"))))
    return out


# ── 渲染: K 线 → 蜡烛图 PNG(本地, 无网络) ──────────────────────────────────

def render_kline_png(
    bars,
    out_path: str,
    *,
    width: int = RENDER_WIDTH,
    height: int = RENDER_HEIGHT,
) -> str | None:
    """K 线序列(升序, 末根最新)→ 蜡烛图 PNG。返回落盘路径; 不足 2 根 / 依赖缺失 → None。

    红涨绿跌(买红卖绿口径), 下方带成交量副图。字体只用位图 ASCII(数字/符号),
    避免中文字体缺失渲染成豆腐块。**纯本地, 不发网络请求。**
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: Pillow 不可用, 无法渲染: %s", e)
        return None

    cs = _usable_candles(bars)
    if len(cs) < 2:
        return None

    n = len(cs)
    left, right, top = 8, 54, 12
    has_vol = any((c[4] or 0) > 0 for c in cs)
    vol_h = int((height - top) * 0.22) if has_vol else 0
    price_bottom = height - 16 - vol_h
    plot_w = width - left - right
    step = plot_w / n
    body_w = max(1.0, step * 0.62)

    lo = min(c[2] for c in cs)
    hi = max(c[1] for c in cs)
    if hi - lo < 1e-9:
        hi = lo + max(abs(lo) * 0.01, 1.0)
    pad = (hi - lo) * 0.04
    lo -= pad
    hi += pad

    def y(price: float) -> float:
        return top + (hi - price) / (hi - lo) * (price_bottom - top)

    img = Image.new("RGB", (width, height), (17, 20, 26))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:  # noqa: BLE001
        font = None

    # 价格轴刻度(右侧)
    for k in range(5):
        price = lo + (hi - lo) * k / 4
        yy = y(price)
        d.line([(left, yy), (width - right, yy)], fill=(40, 45, 55))
        d.text((width - right + 3, yy - 4), f"{price:.2f}", fill=(150, 158, 170), font=font)

    up_col = (232, 78, 78)     # 涨=红
    dn_col = (52, 199, 123)    # 跌=绿
    for i, (o, h, lo_, c, _v) in enumerate(cs):
        x = left + i * step + step / 2
        col = up_col if c >= o else dn_col
        d.line([(x, y(h)), (x, y(lo_))], fill=col, width=1)
        y_open, y_close = y(max(o, c)), y(min(o, c))
        d.rectangle([x - body_w / 2, y_open, x + body_w / 2, max(y_close, y_open + 1)],
                    fill=col)

    if has_vol:
        vmax = max((c[4] or 0) for c in cs) or 1.0
        vtop = price_bottom + 8
        vbot = height - 6
        for i, (o, h, lo_, c, v) in enumerate(cs):
            x = left + i * step + step / 2
            bh = (v or 0) / vmax * (vbot - vtop)
            col = up_col if c >= o else dn_col
            d.rectangle([x - body_w / 2, vbot - bh, x + body_w / 2, vbot], fill=col)

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


# ── 视觉提示词 / 解析 ────────────────────────────────────────────────────────

def build_vision_prompt(rule_names: list[str] | None = None) -> tuple[str, str]:
    """构造 (system, user) 提示词。rule_names 缺省取 kline_patterns 目录里的标准形态名。"""
    names = [str(x) for x in (rule_names or []) if str(x).strip()]
    if not names:
        try:
            from src.core.kline_patterns import pattern_catalog

            names = [p["name"] for p in pattern_catalog()]
        except Exception:  # noqa: BLE001
            names = []

    hint = ("优先使用这些标准形态名: " + "、".join(names) + "; ") if names else ""
    system = (
        "你是 K 线形态识别助手。只依据图中可见的 K 线(价格/影线/实体/量能)判断形态, "
        "不做涨跌预测, 不出买卖建议。"
    )
    user = (
        "这是一张日 K 线蜡烛图(红涨绿跌, 下方为成交量), 窗口内末根为最新一根。"
        "请识别图中**最近 K 线组合**的经典形态。\n"
        f"{hint}"
        "图中若无对应标准形态, 可用其他经典形态名(如头肩顶/头肩底/旗形/楔形/"
        "对称三角形/上升三角形/下降三角形/矩形整理/缺口), 但必须真实可辨。\n"
        "严格只输出**一个 JSON 对象**, 不要任何多余文字, 格式:\n"
        '{"patterns":[{"name":"形态名","confidence":0.0到1.0,"basis":"判读依据, '
        '基于图上可见的影线/实体/量能特征"}],"summary":"一句话"}\n'
        "识别不出任何形态时输出 {\"patterns\":[]}。confidence 是你的判读把握, 不是涨跌概率。"
    )
    return system, user


def parse_vision_result(text: str) -> dict | None:
    """视觉模型文本 → ``{"patterns":[{name,confidence,basis}], "summary":str}``。

    容错: 去 code fence / 取首个 JSON 对象; 无法解析 → None(调用方按降级处理, 不编造)。
    """
    if not text:
        return None
    t = _FENCE_RE.sub("", str(text).strip()).strip()
    m = _JSON_OBJ_RE.search(t)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None

    raw = obj.get("patterns")
    if raw is None:
        raw = [obj] if obj.get("name") else []
    if not isinstance(raw, list):
        return None

    patterns: list[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        conf = _as_num(p.get("confidence"))
        if conf is None:
            conf = _as_num(p.get("conf"))
        if conf is not None:
            conf = max(0.0, min(1.0, conf))
        basis = str(p.get("basis") or p.get("evidence") or "").strip()[:_BASIS_MAX]
        patterns.append({"name": name, "confidence": conf, "basis": basis})
    return {
        "patterns": patterns,
        "summary": str(obj.get("summary") or "").strip()[:_SUMMARY_MAX],
    }


# ── 与规则形态对照 ──────────────────────────────────────────────────────────

def compare_with_rules(ai_names, rule_names) -> str:
    """AI 形态名集合 vs 规则形态名集合 → 对照结论(四组分桶口径之一)。"""
    ai = {str(x).strip() for x in (ai_names or []) if str(x).strip()}
    ru = {str(x).strip() for x in (rule_names or []) if str(x).strip()}
    if ai and ru:
        return COMPARE_AGREE if (ai & ru) else COMPARE_CONFLICT
    if ai:
        return COMPARE_AI_ONLY
    if ru:
        return COMPARE_RULES_ONLY
    return COMPARE_NONE


# ── 纯逻辑: 原始记录 → 影子行 ────────────────────────────────────────────────

def build_rows(records: list[dict]) -> list[dict]:
    """原始记录 → 影子行(含对照结论)。纯函数, 无 I/O。

    每条 record: ``{symbol, name?, ai_patterns?:[{name,confidence,basis}],
    rule_patterns?:[name...], model_name?, window?, status, source?}``。
    symbol 为空的行剔除(无法作主键)。status: ok / degraded / no_data。
    """
    out: list[dict] = []
    for rec in records or []:
        symbol = str(rec.get("symbol") or "").strip()
        if not symbol:
            continue
        status = str(rec.get("status") or "no_data")
        ai_patterns = [p for p in (rec.get("ai_patterns") or []) if isinstance(p, dict)]
        rule_patterns = [str(x) for x in (rec.get("rule_patterns") or []) if str(x).strip()]

        primary = None
        if ai_patterns:
            primary = max(ai_patterns, key=lambda p: (p.get("confidence") or 0.0))

        if status == "ok":
            cmp_status = compare_with_rules(
                [p.get("name") for p in ai_patterns], rule_patterns
            )
        elif status == "degraded":
            cmp_status = COMPARE_DEGRADED
        else:
            cmp_status = None

        out.append(
            {
                "symbol": symbol,
                "name": rec.get("name") or "",
                "ai_pattern": (primary or {}).get("name"),
                "ai_confidence": (primary or {}).get("confidence"),
                "ai_basis": (primary or {}).get("basis") or None,
                "ai_all": (
                    json.dumps(ai_patterns, ensure_ascii=False) if ai_patterns else None
                ),
                "model_name": rec.get("model_name") or None,
                "window_n": rec.get("window"),
                "rule_primary": rule_patterns[0] if rule_patterns else None,
                "rule_patterns": (
                    json.dumps(rule_patterns, ensure_ascii=False) if rule_patterns else None
                ),
                "compare_status": cmp_status,
                "status": status,
                "source": rec.get("source") or "ai_vision",
            }
        )
    return out


# ── 单票识别编排(渲染 → 视觉 → 解析) ────────────────────────────────────────

def recognize_from_bars(
    bars,
    *,
    vision_provider=None,
    render_provider=None,
    window: int = DEFAULT_WINDOW,
    model_name: str | None = None,
    keep_image: bool = False,
) -> dict:
    """K 线序列 → 渲染 PNG → 视觉模型认形态。

    返回 ``{status, ai_patterns, model_name, window, reason?}``。
    - vision_provider 为 None(无视觉模型) → status='degraded';
    - 渲染失败 / 调用失败 / 输出不可解析 → 'degraded'(不编造形态);
    - 视觉可用但判无形态 → 'ok' + 空 patterns;
    - K 线不足(<3 根) → 'no_data'。
    """
    raw_bars = list(bars or [])
    if window and window > 0:
        raw_bars = raw_bars[-int(window):]
    n = len(_usable_candles(raw_bars))
    base = {"ai_patterns": [], "model_name": model_name, "window": n}
    if n < 3:
        return {**base, "status": "no_data", "reason": "K线不足(<3根)"}
    if vision_provider is None:
        return {**base, "status": "degraded",
                "reason": "无视觉模型(scene='vision' 未绑定 / 绑定模型无 vision 能力)"}

    render_provider = render_provider or render_kline_png
    tmp_dir = Path(tempfile.gettempdir()) / "ai_vision_pattern"
    png = tmp_dir / f"{datetime.now(_CST).strftime('%Y%m%d_%H%M%S_%f')}.png"
    try:
        rendered = render_provider(raw_bars, str(png))
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "degraded", "reason": f"渲染异常: {e}"}
    if not rendered:
        return {**base, "status": "degraded", "reason": "渲染失败"}

    system, user = build_vision_prompt()
    try:
        raw = vision_provider(str(rendered), system, user)
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "degraded", "reason": f"视觉调用失败: {e}"}
    finally:
        if not keep_image:
            try:
                Path(rendered).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    if not raw:
        return {**base, "status": "degraded", "reason": "视觉模型返回空"}
    parsed = parse_vision_result(raw)
    if parsed is None:
        return {**base, "status": "degraded", "reason": "视觉输出不可解析"}
    return {
        "status": "ok",
        "ai_patterns": parsed["patterns"],
        "summary": parsed.get("summary"),
        "model_name": model_name,
        "window": n,
    }


# ── 视觉模型解析(统一 LLM 配置中心) + 默认真实调用 ───────────────────────────

def _has_vision_capability(caps: str) -> bool:
    tags = {t.strip().lower() for t in str(caps or "").split(",") if t.strip()}
    return "vision" in tags


def resolve_vision_model(db=None) -> tuple[str, str, str] | None:
    """解析 scene='vision' 的视觉模型 → ``(base_url, api_key, model_name)``。

    无绑定 / 无 vision 能力 / 配置不全 → None(调用方显式降级, 禁编造)。平台模型
    要求 capabilities 含 'vision'; 用户 BYOK(scene='vision')按配置信任。
    """
    from src.core.ai_client import get_model_for_scene

    own = False
    if db is None:
        from src.db.session import SessionLocal

        db = SessionLocal()
        own = True
    try:
        try:
            model = get_model_for_scene(db, _SCENE, user=None)
        except Exception as e:  # noqa: BLE001
            logger.warning("AI看图: 解析 vision 模型失败: %s", e)
            return None
        if model is None:
            return None
        if isinstance(model, dict):  # BYOK
            bu = str(model.get("base_url") or "").strip()
            key = str(model.get("api_key") or "").strip()
            mn = str(model.get("model") or model.get("name") or "").strip()
            return (bu, key, mn) if (bu and key and mn) else None
        # 平台 AIModel: 显式声明 vision 能力, 或即本场景默认模型(名称匹配 DEFAULT_VISION_MODEL)。
        # 默认模型按用户裁决绑定, 能力由探针实测(不因存量 capabilities 未标注就误降级)。
        mn = str(getattr(model, "model", "") or "").strip()
        if not _has_vision_capability(getattr(model, "capabilities", "")) and mn != DEFAULT_VISION_MODEL:
            logger.warning(
                "AI看图: scene='vision' 绑定模型 '%s' 无 vision 能力 → 降级", mn,
            )
            return None
        from src.db.models import AIService

        svc = db.query(AIService).filter(AIService.id == model.service_id).first()
        if svc is None:
            return None
        bu = str(svc.base_url or "").strip()
        key = str(svc.api_key or "").strip()
        return (bu, key, mn) if (bu and key and mn) else None
    finally:
        if own:
            db.close()


def make_vision_provider(model: tuple, *, timeout: float = 60.0):
    """由 ``(base_url, api_key, model_name)`` 构造 sync 视觉调用闭包。

    签名 ``(image_path, system_prompt, user_prompt) -> str``(与 recognize_from_bars
    的 vision_provider 契约一致), 便于测试注入。真实实现走 OpenAI 兼容
    ``/chat/completions`` + base64 内嵌 image_url。
    """
    base_url, api_key, model_name = (str(x) for x in model)
    model_name = model_name or ""

    def _call(image_path: str, system_prompt: str, user_prompt: str) -> str:
        import base64

        import httpx

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                },
            ],
            "max_tokens": 600,
            "temperature": 0.0,
        }
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            return str(r.json()["choices"][0]["message"]["content"] or "").strip()

    return _call


# ── 默认标的池(成本可控) ─────────────────────────────────────────────────────

def _bare(sym) -> str:
    """归一化代码: 去市场后缀(600000.SH → 600000), 与影子表主键口径一致。"""
    return str(sym or "").split(".", 1)[0].strip()


def _default_watchlist() -> list[str]:
    """自选池(CN)代码 —— stocks 表(与 quote_snapshots 的\"自选\"口径一致, **不含持仓**)。

    保留 .SH/.SZ 后缀(取 K 线 provider 需要), 去重保序。
    """
    try:
        from src.db.models import Stock
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            out: list[str] = []
            for sym, mkt in db.query(Stock.symbol, Stock.market).all():
                if (mkt or "CN") != "CN" or not sym:
                    continue
                s = str(sym)
                if s not in out:
                    out.append(s)
            return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取自选池失败: %s", e)
        return []


def _default_prev_marked(trade_date: str) -> list[str]:
    """前一个有打标日的影子表里**出过形态**(规则或 AI)的裸代码 —— 与自选合并控成本。

    取影子表里 < trade_date 的最大 trade_date(前一个已打标交易日); 其 rule_primary 或
    ai_pattern 非空即算\"有标记\"。表未迁移/查询失败 → [](上层按仅自选处理, 不编造)。
    """
    try:
        from sqlalchemy import text

        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = db.execute(
                text(f"SELECT MAX(trade_date) AS d FROM {_TABLE} WHERE trade_date < :td"),
                {"td": trade_date},
            ).first()
            prev = str(row[0]) if row and row[0] else None
            if not prev:
                return []
            rows = db.execute(
                text(
                    f"SELECT symbol FROM {_TABLE} WHERE trade_date = :d "
                    f"AND (rule_primary IS NOT NULL OR ai_pattern IS NOT NULL)"
                ),
                {"d": prev},
            ).fetchall()
            return [_bare(r[0]) for r in rows if r and r[0]]
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取前一日标记票失败(忽略): %s", e)
        return []


def shadow_universe(
    trade_date: str,
    *,
    watchlist_provider=None,
    marks_provider=None,
) -> list[str]:
    """影子任务标的圈定(成本可控): **自选池 ∪ 前一日有标记的票**。

    视觉 LLM 逐票调用有成本, 故不放全市场 / 全部持仓, 只跑自选池 + 前一日影子表里
    出过形态的票(自扩张但有界)。返回**裸代码**(去 .SH/.SZ, 与影子表主键一致), 去重保序。
    watchlist_provider / marks_provider 可注入(测试禁真网络/真库)。
    """
    wp = watchlist_provider or _default_watchlist
    mp = marks_provider or (lambda: _default_prev_marked(trade_date))
    try:
        watch = [_bare(s) for s in (wp() or [])]
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取自选池异常(继续): %s", e)
        watch = []
    try:
        marked = [_bare(s) for s in (mp() or [])]
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取前一日标记异常(继续): %s", e)
        marked = []

    out: list[str] = []
    for s in [*watch, *marked]:
        if s and s not in out:
            out.append(s)
    return out


def _default_names(symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    try:
        from sqlalchemy import text

        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            placeholders = ",".join(f":s{i}" for i in range(len(symbols)))
            rows = db.execute(
                text(f"SELECT symbol, name FROM stocks WHERE symbol IN ({placeholders})"),
                {f"s{i}": s for i, s in enumerate(symbols)},
            ).fetchall()
            return {str(r[0]): str(r[1] or "") for r in rows if r[0]}
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取名称失败(继续): %s", e)
        return {}


def _default_klines(symbol: str, window: int) -> list:
    """PG 优先取日 K(get_klines); 失败 → [](上层按 no_data 处理)。"""
    try:
        from src.collectors.kline_collector import KlineCollector
        from src.models.market import MarketCode

        kc = KlineCollector(MarketCode.CN)
        return kc.get_klines(symbol, days=max(int(window) + 5, 25)) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取K线失败 %s: %s", symbol, e)
        return []


def _default_rules(bars) -> list[str]:
    """规则形态(same window, 最近 RULE_LOOKBACK 根确认)→ 形态名列表。"""
    try:
        from src.core.kline_patterns import detect_pattern_marks

        marks = detect_pattern_marks(bars, lookback=RULE_LOOKBACK)
        # 同一形态多次确认只取一次(名集合口径, 与 compare_with_rules 对齐)
        seen: list[str] = []
        for m in marks:
            if m.name not in seen:
                seen.append(m.name)
        return seen
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 规则形态识别失败(视为无规则形态): %s", e)
        return []


# ── 写入(幂等 upsert) ───────────────────────────────────────────────────────

def _upsert(trade_date: str, row: dict, now: datetime | None = None) -> None:
    from sqlalchemy import text

    from src.db.dialect import upsert_sql
    from src.db.session import SessionLocal

    cols = ["trade_date", "symbol", *FIELDS, "captured_at"]
    updates = [*FIELDS, "captured_at"]
    stmt = text(upsert_sql(_TABLE, cols, ["trade_date", "symbol"], updates))
    payload = {
        "trade_date": trade_date,
        "symbol": row["symbol"],
        "captured_at": (now or datetime.now(_CST)).replace(tzinfo=None),
    }
    for f in FIELDS:
        payload[f] = row.get(f)
    db = SessionLocal()
    try:
        db.execute(stmt, payload)
        db.commit()
    finally:
        db.close()


def mark_rows(
    trade_date: str, rows: list[dict], *, write: bool = True, now: datetime | None = None
) -> dict:
    """把影子行按 (trade_date, symbol) 幂等落库。返回统计(含对照分桶计数)。"""
    from collections import Counter

    written = 0
    for row in rows:
        if write:
            _upsert(trade_date, row, now=now)
        written += 1

    by_status = Counter(r.get("status") for r in rows)
    by_compare = Counter(r.get("compare_status") for r in rows if r.get("compare_status"))
    ok = int(by_status.get("ok", 0))
    return {
        "trade_date": trade_date,
        "rows": len(rows),
        "written": written,
        "ok": ok,
        "degraded": int(by_status.get("degraded", 0)),
        "no_data": int(by_status.get("no_data", 0)),
        "by_compare": dict(by_compare),
    }


# ── 编排: 自选∪持仓 → 逐票识别 → 对照 → 落库 ────────────────────────────────

def collect_and_mark(
    trade_date: str | None = None,
    *,
    universe_provider=None,
    kline_provider=None,
    rule_provider=None,
    names_provider=None,
    render_provider=None,
    vision_provider=None,
    model_provider=None,
    window: int = DEFAULT_WINDOW,
    write: bool = True,
    now: datetime | None = None,
) -> dict:
    """自选池∪前一日标记票 → 逐票(渲染→AI→规则对照) → 影子落库。provider 可注入(测试禁真网络)。

    返回统计 dict; 池子为空 / 无视觉模型 / 全部无数据 → ``ok=False`` 并显式说明原因。
    """
    td = trade_date or _trade_date(now)
    universe_provider = universe_provider or (lambda: shadow_universe(td))
    kline_provider = kline_provider or (lambda s: _default_klines(s, window))
    rule_provider = rule_provider or _default_rules
    names_provider = names_provider or _default_names
    render_provider = render_provider or render_kline_png

    # 视觉模型解析(可注入 model_provider 供测试; 返回 (base_url,api_key,model) 或 None)
    model_name = None
    vision = vision_provider
    if vision is None:
        resolver = model_provider or resolve_vision_model
        try:
            model = resolver()
        except Exception as e:  # noqa: BLE001
            logger.warning("AI看图: 解析视觉模型异常: %s", e)
            model = None
        if model:
            try:
                base_url, api_key, model_name = (str(x) for x in model)
            except Exception:  # noqa: BLE001
                base_url, api_key, model_name = None, None, None
            if base_url and api_key:
                vision = make_vision_provider((base_url, api_key, model_name))
            else:
                model_name = None

    codes = universe_provider() or []
    if not codes:
        return {"ok": False, "trade_date": td, "rows": 0,
                "reason": "自选∪持仓为空(或取码失败)"}

    try:
        names = names_provider(codes) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("AI看图: 取名称失败(继续): %s", e)
        names = {}

    records: list[dict] = []
    for sym in codes:
        bare = str(sym).split(".", 1)[0].strip()
        if not bare:
            continue
        try:
            bars = kline_provider(sym) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("AI看图: 取数异常 %s: %s", sym, e)
            bars = []
        try:
            rules = [str(x) for x in (rule_provider(bars) or [])] if bars else []
        except Exception as e:  # noqa: BLE001
            logger.warning("AI看图: 规则识别异常 %s: %s", sym, e)
            rules = []
        res = recognize_from_bars(
            bars,
            vision_provider=vision,
            render_provider=render_provider,
            window=window,
            model_name=model_name,
        )
        records.append(
            {
                "symbol": bare,
                "name": names.get(bare, ""),
                "ai_patterns": res.get("ai_patterns") or [],
                "rule_patterns": rules,
                "model_name": res.get("model_name"),
                "window": res.get("window_n"),
                "status": res.get("status"),
                "source": "ai_vision",
            }
        )

    rows = build_rows(records)
    stats = mark_rows(td, rows, write=write, now=now)
    stats["ok"] = stats["ok"] > 0
    if not stats["ok"]:
        stats["reason"] = "全部降级/无数据(无视觉模型 / 调用失败 / K线不足, 不编造形态)"
    return stats


def run_ai_vision_pattern_job(
    now: datetime | None = None, trade_date: str | None = None
) -> dict:
    """cron 入口(交易日收盘后): 逐票识别 + 对照 + 落库, 走作业框架诚实性(ok=False → failed)。

    标的圈定走 ``shadow_universe``(自选池 ∪ 前一日有标记票, 成本可控), 由
    ``collect_and_mark`` 的默认 universe 提供。
    """
    from src.core.jobs import jobs

    job_id, is_new = jobs.create("ai_vision_pattern", "AI 看图形态 影子打标(与规则形态对照)")
    if not is_new:
        return {"ok": True, "skipped": "in_flight", "job_id": job_id}
    try:
        out = collect_and_mark(trade_date=trade_date, now=now)
    except Exception as e:  # noqa: BLE001
        jobs.fail(job_id, str(e))
        logger.exception("AI 看图形态影子打标作业异常: %s", e)
        return {"ok": False, "error": str(e), "job_id": job_id}
    out["job_id"] = job_id
    jobs.finish(job_id, out, context="AI 看图形态影子: ")
    return out


# ── 只读读取(供 API/展示) ──────────────────────────────────────────────────

def latest_date() -> str | None:
    """影子表最近交易日(YYYYMMDD); 无数据 → None。"""
    from sqlalchemy import text

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        r = db.execute(text(f"SELECT MAX(trade_date) AS d FROM {_TABLE}")).first()
        return str(r[0]) if r and r[0] else None
    except Exception as e:  # noqa: BLE001 表未迁移 → 当作无数据
        logger.warning("ai_pattern_shadow 查询失败(视为无数据): %s", e)
        return None
    finally:
        db.close()


def patterns_for_date(trade_date: str | None = None) -> dict:
    """某日 AI 看图形态 + 规则对照结果。

    返回 ``{available, trade_date, rows:[...], counts:{...}, note}``。trade_date 缺省取
    最近一日; 无数据 → available=False + note(绝不编造)。
    """
    td = _ymd(trade_date) if trade_date else latest_date()
    if not td:
        return {"available": False, "trade_date": None, "rows": [], "counts": {},
                "note": "尚无 AI 看图形态影子数据(可能未到首个盘后 cron, 或表未迁移)"}
    from sqlalchemy import text

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                f"SELECT symbol, name, ai_pattern, ai_confidence, ai_basis, ai_all, "
                f"model_name, window_n, rule_primary, rule_patterns, compare_status, "
                f"status, source FROM {_TABLE} WHERE trade_date = :d ORDER BY symbol"
            ),
            {"d": td},
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_pattern_shadow 查询失败: %s", e)
        return {"available": False, "trade_date": td, "rows": [], "counts": {},
                "note": f"影子表查询失败: {e}"}
    finally:
        db.close()

    items: list[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("ai_all", "rule_patterns"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except Exception:  # noqa: BLE001
                    pass
        items.append(d)

    from collections import Counter

    counts = dict(Counter(str(r.get("compare_status") or "none") for r in items))
    return {
        "available": bool(items),
        "trade_date": td,
        "rows": items,
        "counts": counts,
        "note": None if items else f"{td} 无 AI 看图形态影子行",
    }
