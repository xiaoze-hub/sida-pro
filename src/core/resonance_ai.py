"""三指标共振 AI 判定(2026-09-11, 老板"要接入ai分析, 给出是否共振; 现在没办法知道到底有没有共振")。

输入(全部来自我们自己的实现, 口径唯一):
- 趋势: GS(gs_strategy.eval_gs/trend_label) → G区/G信号/…
- 强度: AI机构活跃度(ai_activity) → 数值/档位/连强天数
- 资金: 主力净流入(通达信 SUPAMO, 元; 缺则显式 None)
- 序列: 近 N 日活跃度(供 AI 判断趋势变化)

输出: 结构化判定(强共振/弱共振/未共振/无法判定) + 置信度 + 依据/风险/关注点 + 一句话结论。
纪律: 缺数据必须说"无法判定"并列缺项, 禁止编造; 提示词统一挂合规护栏(with_compliance)。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

VERDICT_STRONG = "强共振"
VERDICT_WEAK = "弱共振"
VERDICT_NONE = "未共振"
VERDICT_UNKNOWN = "无法判定"

SYSTEM_PROMPT = (
    "你是 SIDA(数智分析)的「三指标共振」分析员。三个指标定义:\n"
    "1) 趋势(GS策略): G区/G信号=趋势向上; S区/S信号=趋势向下; 无数据=不可用。\n"
    "2) 强度(AI机构活跃度): 数值, 档位 弱(<1.56)/生命(1.56~3)/强势(3~6)/大牛(≥6); 阈值 1.56/3/6。\n"
    "3) 资金(主力净流入): 正=净流入, 负=净流出, 单位元; 缺失=不可得。\n"
    "判定口径: 趋势在 G 区 + 活跃度≥3(强势线) + 资金净流入 = **强共振**; 满足两项 = **弱共振**; "
    "否则 **未共振**; 关键项缺失导致无法判断时, 必须输出 **无法判定** 并列出缺失项。\n"
    "要求: 只依据给出的数据推理, 禁止脑补或引用外部信息; 语言精炼; 必须输出 JSON。\n"
    '输出 JSON(不要多余文字): {"resonance":"强共振|弱共振|未共振|无法判定","confidence":0~1,'
    '"summary":"一句话结论(≤40字)","reasons":["依据1","依据2"],"risks":["风险/背离1"],'
    '"watch":["下一步该看什么1"],"missing":["缺失项(如有)"]}'
)


def build_user_content(
    symbol: str,
    name: str,
    detail: dict,
    series: list[dict] | None = None,
) -> str:
    """把三指标数据拼成给 LLM 的用户消息(纯函数, 便于测试)。"""

    def fmt_fund(v) -> str:
        if v is None:
            return "无数据"
        try:
            yi = float(v) / 1e8
        except (TypeError, ValueError):
            return "无数据"
        return f"{yi:+.2f}亿"

    lines = [
        f"标的: {name or ''}({symbol})",
        f"数据日期: {detail.get('trade_date') or '未知'}",
        "三指标现状:",
        f"- 趋势(GS): {detail.get('trend') or '无数据'}",
        f"- 强度(AI机构活跃度): {detail.get('activity') if detail.get('activity') is not None else '无数据'}"
        f" (档位 {detail.get('level') or '未知'})",
        f"- 资金(主力净流入): {fmt_fund(detail.get('fund_net'))}",
        f"规则引擎判定(供参考, 可修正): {detail.get('level3') or '无'}",
    ]
    if series:
        recent = series[-10:]
        seq = ", ".join(
            f"{str(i.get('date'))[-4:]}:{i.get('activity') if i.get('activity') is not None else '-'}" for i in recent
        )
        lines.append(f"近10日活跃度序列(MMDD:值): {seq}")
    lines.append("请按口径给出共振判定与依据; 数据缺失的项目必须列入 missing, 不得猜测。")
    return "\n".join(lines)


def parse_ai_verdict(content: str | None) -> dict:
    """解析 LLM 输出的 JSON(容错: 提取首个 {...} 块); 失败 → 无法判定(不编造)。"""
    if not content:
        return {
            "resonance": VERDICT_UNKNOWN,
            "confidence": None,
            "summary": "AI 未返回内容",
            "reasons": [],
            "risks": [],
            "watch": [],
            "missing": [],
            "parse_error": True,
        }
    raw = content.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict):
        return {
            "resonance": VERDICT_UNKNOWN,
            "confidence": None,
            "summary": raw[:80],
            "reasons": [],
            "risks": [],
            "watch": [],
            "missing": [],
            "parse_error": True,
        }
    verdict = str(data.get("resonance") or "").strip()
    if verdict not in (VERDICT_STRONG, VERDICT_WEAK, VERDICT_NONE, VERDICT_UNKNOWN):
        verdict = VERDICT_UNKNOWN

    def _list(key: str) -> list[str]:
        v = data.get(key)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()][:6]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    conf = data.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf))) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    return {
        "resonance": verdict,
        "confidence": conf,
        "summary": str(data.get("summary") or "").strip()[:120],
        "reasons": _list("reasons"),
        "risks": _list("risks"),
        "watch": _list("watch"),
        "missing": _list("missing"),
        "parse_error": False,
    }


# ── 盘后批量判定(2026-09-11 老板"可以": 当日共振标的批量跑, 首页行内显示) ──────────
BATCH_CHUNK = 12  # 单次 LLM 覆盖的标的数(控制调用量与输出长度)
BATCH_SYSTEM_PROMPT = (
    "你是 SIDA(数智分析)的「三指标共振」分析员。三指标: 1) 趋势(GS策略): G区/G信号=向上; "
    "2) 强度(AI机构活跃度): 阈值 1.56/3/6, 大于等于3 为强势; 3) 资金(主力净流入, 元): 正=流入。\n"
    "判定口径: 趋势G区 + 活跃度大于等于3 + 资金净流入 = 强共振; 满足两项 = 弱共振; 否则 未共振; "
    "关键项缺失且影响判断 = 无法判定。\n"
    "只依据给定数据, 禁止脑补。输出 JSON 数组, 每只一行, 不要多余文字:\n"
    '[{"symbol":"600519","verdict":"强共振|弱共振|未共振|无法判定","confidence":0~1,'
    '"summary":"一句话(<=30字)","risk":"主要风险(<=20字, 无则空串)"}]'
)


def build_batch_content(rows: list[dict]) -> str:
    """多标的数据拼装(纯函数): 每行 代码|名称|趋势|活跃度(档位)|资金(亿)|规则判定。"""
    lines = ["标的列表(逐行判定, 输出 JSON 数组):"]
    for r in rows:
        fund = r.get("fund_net")
        fund_s = "无数据" if fund is None else f"{float(fund) / 1e8:+.2f}亿"
        act = r.get("activity")
        lines.append(
            f"{r.get('symbol')}|{r.get('name') or ''}|{r.get('trend') or '无数据'}|"
            f"{'无数据' if act is None else round(float(act), 2)}({r.get('level') or '未知'})|{fund_s}|{r.get('level3') or '无'}"
        )
    return "\n".join(lines)


def parse_batch_verdicts(content: str | None, symbols: list[str]) -> list[dict]:
    """解析批量 JSON 数组; 缺失/解析失败的标的**不落库**(调用方跳过), 不编造。"""
    if not content:
        return []
    raw = content.strip()
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    wanted = {str(s) for s in symbols}
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").strip()
        if sym not in wanted:
            continue
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in (VERDICT_STRONG, VERDICT_WEAK, VERDICT_NONE, VERDICT_UNKNOWN):
            verdict = VERDICT_UNKNOWN
        conf = item.get("confidence")
        try:
            conf = max(0.0, min(1.0, float(conf))) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        out.append(
            {
                "symbol": sym,
                "verdict": verdict,
                "confidence": conf,
                "summary": str(item.get("summary") or "").strip()[:80],
                "risk": str(item.get("risk") or "").strip()[:60],
            }
        )
    return out


def _upsert_verdicts(trade_date: str, rows: list[dict], model: str | None) -> int:
    if not rows:
        return 0
    from sqlalchemy import text as _text

    from src.db.session import engine

    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                _text(
                    """
                    INSERT INTO resonance_ai_verdicts
                        (trade_date, symbol, verdict, confidence, summary, reasons, risks, watch, missing, model)
                    VALUES (:trade_date, :symbol, :verdict, :confidence, :summary, :reasons, :risks, :watch, :missing, :model)
                    ON CONFLICT(trade_date, symbol) DO UPDATE SET
                        verdict=excluded.verdict, confidence=excluded.confidence, summary=excluded.summary,
                        reasons=excluded.reasons, risks=excluded.risks, watch=excluded.watch,
                        missing=excluded.missing, model=excluded.model, created_at=CURRENT_TIMESTAMP
                    """
                ),
                {
                    "trade_date": trade_date,
                    "symbol": r["symbol"],
                    "verdict": r["verdict"],
                    "confidence": r["confidence"],
                    "summary": r["summary"],
                    "reasons": json.dumps([], ensure_ascii=False),
                    "risks": json.dumps([r.get("risk")] if r.get("risk") else [], ensure_ascii=False),
                    "watch": json.dumps([], ensure_ascii=False),
                    "missing": json.dumps([], ensure_ascii=False),
                    "model": model or "",
                },
            )
    return len(rows)


def _build_batch_client(db=None):
    """批量判定 LLM 客户端(core 内自建, 不走 web 层 `_get_ai_client`):
    场景绑定(chat) → Settings(AI_BASE_URL/AI_API_KEY/AI_MODEL) 兜底。"""
    from src.core.ai_client import AIClient, get_model_for_scene

    if db is not None:
        try:
            from src.db.models import AIService

            m = get_model_for_scene(db, "chat")
            if m is not None:
                s = db.query(AIService).filter(AIService.id == m.service_id).first()
                if s is not None and s.base_url and s.api_key:
                    return AIClient(base_url=s.base_url, api_key=s.api_key, model=m.model, scene="resonance_ai")
        except Exception as e:  # noqa: BLE001
            logger.debug("批量判定场景绑定不可用(回落 Settings): %s", e)
    from src.config import Settings

    st = Settings()
    return AIClient(base_url=st.ai_base_url, api_key=st.ai_api_key, model=st.ai_model, scene="resonance_ai")


async def run_daily_verdicts(limit: int = 24, symbol_filter: list[str] | None = None) -> dict:
    """盘后批量判定: 取当日共振标的 top N → 分组调 LLM → 落库。永不抛异常。

    单组失败跳过(其余组照常); 解析缺失的标的不落库(宁缺勿编)。
    """
    out = {"ok": True, "verdicts": 0, "calls": 0, "errors": 0}
    try:
        from sqlalchemy import text as _text

        from src.db.session import engine

        with engine.begin() as conn:
            day = conn.execute(_text("SELECT MAX(trade_date) FROM resonance_scan")).scalar()
            if not day:
                out.update(ok=False, reason="无扫描数据")
                return out
            rows = conn.execute(
                _text(
                    "SELECT symbol, name, trend, activity, level, fund_net, hits FROM resonance_scan"
                    " WHERE trade_date = :d AND resonance = TRUE"
                    " ORDER BY activity DESC NULLS LAST LIMIT :lim"
                ),
                {"d": day, "lim": max(1, min(int(limit), 100))},
            ).fetchall()
        # level3 由 hits 派生(与 resonance_level 同口径; 表无该列, 避免仅为展示加迁移)
        pool = []
        for r in rows:
            d = dict(r._mapping)
            d["level3"] = {3: "强", 2: "弱"}.get(int(d.get("hits") or 0), "无")
            pool.append(d)
        if symbol_filter:
            wanted = {str(x) for x in symbol_filter}
            pool = [p for p in pool if str(p.get("symbol")) in wanted]
        if not pool:
            out.update(ok=True, note="当日无共振标的")
            return out

        from src.core.ai_client import with_compliance
        from src.db.session import SessionLocal

        for i in range(0, len(pool), BATCH_CHUNK):
            chunk = pool[i : i + BATCH_CHUNK]
            syms = [str(c.get("symbol")) for c in chunk]
            try:
                db = SessionLocal()
                try:
                    client = _build_batch_client(db)
                finally:
                    db.close()  # 长 LLM 调用期间不占连接
                content = await client.chat(
                    with_compliance(BATCH_SYSTEM_PROMPT), build_batch_content(chunk), temperature=0.2
                )
                out["calls"] += 1
                parsed = parse_batch_verdicts(content, syms)
                if parsed:
                    out["verdicts"] += _upsert_verdicts(str(day), parsed, None)
            except Exception as e:  # noqa: BLE001
                out["errors"] += 1
                logger.warning("批量共振判定失败(组 %s): %s", int(i / BATCH_CHUNK), e)
        out.update(ok=out["errors"] == 0, trade_date=str(day), pool=len(pool))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("批量共振判定异常: %s", e)
        return {"ok": False, "verdicts": 0, "calls": 0, "errors": 1, "reason": str(e)}
