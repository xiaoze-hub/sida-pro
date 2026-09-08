"""AI 裁判层: 模型预测交给 PanWatch 对话助手(8000)评估, 可改最终方向(B方案)。

流程:
- 预测引擎(8010) 4 模型加权投票后(2026-08-13 起独立 LLM 情绪打分已停用,
  AI 裁判接管消息面/情绪判断), 把预测结果打包成评估数据
- 经 8000 对话助手 API 建会话(绑定股票, initial_context 放评估数据快照)
  → 发消息让 AI 用其工具(主力意图 get_main_intent / 资金流 get_capital_flow /
    技术面 get_technical_analysis / 形态 get_rally_analysis 等)核实后再评估
- 解析 AI 回复中的 JSON 裁判结论:
    {"verdict": "confirm"|"adjust"|"abstain", "direction": "up"|"down"|null, "reason": "..."}
  - verdict=confirm: 认可模型方向(direction 可为 null)
  - verdict=adjust : 不认可, direction 给建议方向(强势 B 方案: 直接覆盖最终方向)
  - verdict=abstain: 盘面证据不足/工具拉取失败 —— 不认可也不否决, 维持模型方向,
    绝不在证据不足时强行 confirm(2026-09-08 风险方案 0.3/0.0)
- 失败降级(0.0 fail-open→abstain): 任何异常/超时/解析失败都返回
    {"verdict": "abstain", "direction": null, "reason": "裁判不可用: <原因>"}
  保证裁判故障不阻断预测主流程, 也绝不伪造"裁判确认过"的结论。

认证/寻址: 复用 forecast_lib/panwatch_client.py 的 get_panwatch_url() +
auth_headers()(X-Service-Token 只读服务令牌, 0.0 起不再伪造 owner JWT)。
对话助手侧(chat.py): 建会话/发消息两端口 0.0 起接受 X-Service-Token,
服务令牌建的会话 user_id=NULL(系统会话, 与用户会话互不可见)。

性能: 对话助手是外部 API(agnes) + 多轮工具调用, 可能 20-45s;
建会话超时 15s, 发消息超时 45s, 全程 try/except。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

try:
    from .panwatch_client import get_panwatch_url, auth_headers
except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
    from panwatch_client import get_panwatch_url, auth_headers

try:
    from .forecast_traces import record_referee_eval
except ImportError:
    from forecast_traces import record_referee_eval

import requests

logger = logging.getLogger(__name__)

# 建会话 / 发消息 超时(秒)。发消息含 AI 工具调用, 最耗时。
_CONVERSATION_TIMEOUT = 15.0
_MESSAGE_TIMEOUT = 45.0


# ── 统一 LLM 配置中心(2026-08-13): 裁判模型解析 ─────────────────────────────

class RefereeConfigUnavailable(RuntimeError):
    """取不到 referee 场景模型配置。禁止回落硬编码模型(风险方案 0.0)。"""


try:
    from forecast_lib import panwatch_client
except ImportError:  # forecast_server.py 将 forecast_lib 直接加入 sys.path
    import panwatch_client


def resolve_referee_model_cfg() -> dict:
    """解析 AI 裁判模型配置(统一 LLM 配置中心)。

    2026-09-08 风险方案 0.0: 唯一通道是主服务 HTTP
    (/api/service/forecast-config, X-Service-Token 服务令牌鉴权)。
    原 sqlite 直读主库的三候选路径(旧环境变量 → docker volume → /app/data)
    已整体删除 —— 切 PG 后读到的是冻结旧值且不报错, 含明文 api_key, 属旁路。

    Returns:
        命中 referee 场景绑定时带 ai_model_id/base_url/api_key;
        未绑定但配置了 forecast_llm_* 时返回该配置(显式用户配置, 非硬编码)。

    Raises:
        RefereeConfigUnavailable: API 不可达 / 无 referee 绑定 / 无任何可用
        api_key。调用方(forecast_server/evaluate_prediction)按 abstain 处理,
        绝不回落硬编码 agnes。
    """
    try:
        data = panwatch_client.request_json("/api/service/forecast-config", timeout=10)
    except Exception as exc:
        raise RefereeConfigUnavailable(f"主服务 API 不可达(/api/service/forecast-config): {exc}") from exc
    if not isinstance(data, dict):
        raise RefereeConfigUnavailable(
            "主服务 API 响应异常(/api/service/forecast-config)"
        )

    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    ref = (payload or {}).get("referee")
    if isinstance(ref, dict) and ref.get("base_url"):
        logger.info(
            "AI 裁判模型: 主服务 API referee 场景绑定 ai_model_id=%s (%s)",
            ref.get("ai_model_id"), ref.get("model"),
        )
        return {
            "ai_model_id": ref.get("ai_model_id"),
            "model": ref.get("model") or "",
            "name": ref.get("name") or "",
            "base_url": ref.get("base_url") or "",
            "api_key": ref.get("api_key") or "",
        }

    # 无 referee 绑定: 退而用设置页显式配置的 forecast_llm_*(仍经 API 下发)
    llm = (payload or {}).get("llm") or {}
    if isinstance(llm, dict) and llm.get("api_key") and llm.get("base_url"):
        logger.warning(
            "AI 裁判模型: referee 场景未绑定, 回落设置页 forecast_llm_* (%s) —— "
            "请在设置页为 referee 场景绑定模型",
            llm.get("model"),
        )
        return {
            "base_url": llm.get("base_url") or "",
            "api_key": llm.get("api_key") or "",
            "model": llm.get("model") or "",
        }

    raise RefereeConfigUnavailable(
        "referee 场景未绑定模型且未配置 forecast_llm_* —— "
        "拒绝回落硬编码模型, 请在设置页配置"
    )


def _infer_market(symbol: str) -> str:
    """6 位代码推断市场: 沪市 6 开头, 其余按深市。"""
    return "sh" if str(symbol).startswith("6") else "sz"


def _summarize_models(prediction_result: dict) -> str:
    """把 4 模型预测结果压成紧凑摘要(供 prompt 使用)。

    prediction_result 结构见 forecast_server.predict():
      {"kronos": {...含 median 数组...}, "chronos": {...}, "xgboost": [...], "linreg": [...]}
    各模型关键值取: 首个/中位数/末位预测价, 以及方向。
    """
    lines: list[str] = []
    for name in ("kronos", "chronos", "xgboost", "linreg"):
        raw = prediction_result.get(name) if isinstance(prediction_result, dict) else None
        if raw is None:
            lines.append(f"- {name}: 不可用/无数据")
            continue
        # 模型返回可能是 dict(带 median/mean) 或纯数组
        if isinstance(raw, dict):
            series = (
                raw.get("median")
                or raw.get("mean")
                or raw.get("predictions")
                or raw.get("values")
                or []
            )
            extra = {k: v for k, v in raw.items() if k not in ("median", "mean", "predictions", "values", "dates")}
        else:
            series, extra = raw, {}
        if not series:
            lines.append(f"- {name}: 无有效序列{(' ' + json.dumps(extra, ensure_ascii=False)[:100]) if extra else ''}")
            continue
        try:
            series = [float(x) for x in series]
        except (TypeError, ValueError):
            lines.append(f"- {name}: 序列无法数值化")
            continue
        first, last = series[0], series[-1]
        mid = series[len(series) // 2]
        pct = (last / series[0] - 1) * 100 if series[0] else 0.0
        lines.append(
            f"- {name}: 首日预测 {first:.2f} → 末日 {last:.2f} (中位 {mid:.2f}), "
            f"区间涨跌 {pct:+.2f}%"
        )
    return "\n".join(lines)


def _build_eval_message(
    symbol: str,
    stock_name: str,
    last_close: float,
    prediction_result: dict,
    direction: str,
    expected_pct: float,
    profile_block: str = "",
) -> str:
    """组装发给对话助手的评估消息。

    明确要求: 用工具核实 → 评估可信度 → 只输出 JSON(便于程序解析)。
    profile_block: 用户交易风格画像段(可选)。有画像时注入, 只影响建议贴合度,
    不改 verdict/direction 判断。
    """
    model_summary = _summarize_models(prediction_result)
    profile_section = f"\n{profile_block}\n" if profile_block else ""
    return (
        f"【AI 裁判任务】请对模型预测结果做裁判评估(标的 {symbol} {stock_name}, 最新收盘 {last_close})。\n\n"
        f"=== 4 模型加权投票预测(8010 预测引擎产出) ===\n"
        f"{model_summary}\n"
        f"- 加权投票方向: {direction}\n"
        f"- 预期涨幅: {expected_pct:+.2f}%\n"
        f"{profile_section}"
        f"=== 你的职责 ===\n"
        f"1. 先用工具核实真实盘面: get_main_intent(主力意图, 逐笔口径, 判断吸筹/派发), "
        f"get_capital_flow(资金流向, 东财口径), get_technical_analysis(技术面/支撑压力), "
        f"get_rally_analysis(涨停/形态) 等, 需要哪个调哪个, 不要凭记忆。\n"
        f"2. 结合工具结果评估: 模型预测的方向和幅度是否可信? 主力行为/资金流/技术形态是支持还是反对?\n"
        f"3. 只输出一个 JSON 对象, 不要输出任何其他文字/解释/代码块标记:\n"
        f'{{"verdict": "confirm" 或 "adjust" 或 "abstain", "direction": "up" 或 "down" 或 null, "reason": "中文理由(≤80字, 引用关键工具数据)"}}\n'
        f"   - verdict=confirm: 认可模型方向(此时 direction 可给 null 或维持原方向)\n"
        f"   - verdict=adjust : 不认可模型方向, direction 必须给出你建议的方向(up/down), reason 说明依据\n"
        f"   - verdict=abstain: 盘面证据不足/工具拉取失败时输出 abstain 并在 reason 说明缺哪项证据; 禁止在证据不足时强行 confirm\n"
        f"口径裁决规则(必须遵守):\n"
        f"   - 严禁用 get_capital_flow 的数据直接下『主力派发/吸筹』结论(东财口径无逐笔方向性);\n"
        f"   - 若 get_main_intent 与 get_capital_flow 方向冲突, 说明口径差异(逐笔 vs 东财)并优先采信 get_main_intent。\n"
        f"注意: 只输出 JSON, 严格用英文双引号, 不要用 markdown 代码块。"
    )


# 画像注入(B方案, 2026-08-13): profile_text 截断 + rules 只取前 N 条, 控制 token
_SHADOW_PROFILE_TEXT_MAX = 300
_SHADOW_PROFILE_RULES_MAX = 3


def _build_profile_block(user_profile: dict | None) -> str:
    """从 users.shadow_profile_json 构建裁判 prompt 的画像段(无画像返回空串)。

    画像只影响"建议表达方式"(如短线风格强调进出场节奏/仓位管理,
    潜伏风格强调耐心持有), 不改 verdict/direction —— 方向判断仍由盘面工具数据决定。
    """
    if not user_profile or not isinstance(user_profile, dict):
        return ""
    parts: list[str] = []

    profile_text = (user_profile.get("profile_text") or "").strip()
    if profile_text:
        if len(profile_text) > _SHADOW_PROFILE_TEXT_MAX:
            profile_text = profile_text[:_SHADOW_PROFILE_TEXT_MAX] + "…"
        parts.append(f"画像: {profile_text}")

    rules = user_profile.get("rules") or []
    if rules:
        rule_lines = []
        for rule in rules[:_SHADOW_PROFILE_RULES_MAX]:
            if isinstance(rule, dict) and rule.get("human_text"):
                rule_lines.append(f"- {rule['human_text']}")
        if rule_lines:
            parts.append("交易规则:\n" + "\n".join(rule_lines))

    preferred_markets = user_profile.get("preferred_markets") or []
    if preferred_markets:
        parts.append("偏好市场: " + ", ".join(str(m) for m in preferred_markets))

    holding_days = user_profile.get("typical_holding_days")
    if holding_days:
        if isinstance(holding_days, (list, tuple)) and len(holding_days) == 2:
            parts.append(f"典型持仓天数: 中位 {holding_days[0]} 天 / P75 {holding_days[1]} 天")
        else:
            parts.append(f"典型持仓天数: {holding_days} 天")

    if not parts:
        return ""
    return (
        "=== 用户交易风格参考(仅用于建议贴合度, 不改预测) ===\n"
        "以下为用户交易风格画像。你的裁判结论(verdict/direction)必须仍由盘面工具数据决定, "
        "画像只影响建议表达方式: 如用户是短线/高换手风格就强调进出场节奏与仓位管理, "
        "潜伏/长持风格就强调耐心与止损纪律。\n"
        + "\n".join(parts)
    )


def _parse_verdict(text: str) -> Optional[dict]:
    """从 AI 回复里宽松提取 JSON 裁判结论。

    容忍: 代码块围栏、前后缀文字、多余字段; 校验 verdict/direction 枚举。
    0.0: verdict 支持 abstain(证据不足时不许强行 confirm)。
    解析失败返回 None(上层按 abstain 降级)。
    """
    if not text or not isinstance(text, str):
        return None
    # 去掉 markdown 代码块围栏
    cleaned = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    # 找第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("confirm", "adjust", "abstain"):
        return None
    direction = data.get("direction")
    if direction is not None:
        direction = str(direction).strip().lower()
        if direction not in ("up", "down"):
            direction = None
    return {
        "verdict": verdict,
        "direction": direction,
        "reason": str(data.get("reason", "")).strip() or "",
    }


def evaluate_prediction(
    symbol: str,
    stock_name: str,
    last_close: float,
    prediction_result: dict,
    direction: str,
    expected_pct: float,
    run_id: int = 0,
    user_profile: dict | None = None,
    model_cfg: dict | None = None,
) -> dict:
    """把模型预测交给 PanWatch 对话助手评估, 返回裁判结论。

    Args:
        symbol: 6 位 A 股代码
        stock_name: 股票名称(可空)
        last_close: 最新收盘价
        prediction_result: predict() 的 models dict(4 模型原始输出)
        direction: 模型加权投票方向 "up"/"down"/"flat"
        expected_pct: 加权预期涨跌幅(%)
        run_id: prediction_runs.id(可选)。>0 时裁判结论落 prediction_referee_evals 表;
                不传则由落库层按 symbol 自动补最新 run。
        user_profile: 用户影子画像 dict(users.shadow_profile_json, 可选)。
                有画像时注入评估 prompt 的"用户交易风格参考"段: 只影响建议表达
                贴合度(短线/潜伏/持仓节奏), 不影响 verdict/direction 判断。
                无画像(None)行为与旧版完全一致。
        model_cfg: 裁判模型配置(统一 LLM 配置中心, 2026-08-13)。None 时内部调用
                resolve_referee_model_cfg() 自解析(referee 场景绑定 > 旧
                forecast_llm_*; 均不可用则按 abstain, 不回落硬编码 agnes)。
                命中场景绑定(带 ai_model_id)时,
                建会话 body 带 ai_model_id, 对话助手 send_message 优先用它。

    Returns:
        {"verdict": "confirm"|"adjust"|"abstain", "direction": "up"|"down"|None,
         "reason": str, "conv_id": int|None, "elapsed_ms": int}
        0.0: 任何异常/配置缺失都按 abstain 返回(不抛异常), 绝不伪造成
        verdict=confirm"裁判确认过"。
    """
    import time

    t0 = time.monotonic()
    conv_id: Optional[int] = None
    try:
        base_url = get_panwatch_url()
        headers = {"Content-Type": "application/json", **auth_headers()}
        if "X-Service-Token" not in headers and "Authorization" not in headers:
            raise RuntimeError(
                "无可用凭据(未配置 PANWATCH_SERVICE_TOKEN/SIDA_SERVICE_TOKEN/"
                "PANWATCH_TOKEN/账号密码)"
            )

        # 1) 建会话: 绑定股票, initial_context 放评估数据快照(会注入 system prompt)
        market = _infer_market(symbol)
        initial_context = (
            f"[预测引擎 AI 裁判] 股票 {symbol} {stock_name or ''}, 市场 {market}, "
            f"最新收盘 {last_close}, 模型加权方向 {direction}, 预期 {expected_pct:+.2f}%。"
        )
        conv_body: dict = {
            "stock_symbol": symbol,
            "stock_market": market,
            "initial_context": initial_context,
        }
        # 统一 LLM 配置中心(2026-08-13): referee 场景绑定 → 会话指定 ai_model_id,
        # 对话助手 send_message 的 _get_ai_client 优先用它(显式模型 > chat 场景绑定)。
        if model_cfg is None:
            model_cfg = resolve_referee_model_cfg()
        conv_model_id = model_cfg.get("ai_model_id") if isinstance(model_cfg, dict) else None
        if conv_model_id:
            conv_body["ai_model_id"] = conv_model_id
        conv_resp = requests.post(
            f"{base_url}/api/chat/conversations",
            json=conv_body,
            headers=headers,
            timeout=_CONVERSATION_TIMEOUT,
        )
        conv_resp.raise_for_status()
        conv_data = conv_resp.json()
        # PanWatch API 统一包装: {"code":0,"success":true,"data":{...},"message":""}
        conv_payload = conv_data.get("data") if isinstance(conv_data, dict) and isinstance(conv_data.get("data"), dict) else conv_data
        conv_id = conv_payload.get("id")
        if not conv_id:
            raise RuntimeError(f"建会话返回无 id: {conv_data}")

        # 2) 发评估消息(对话助手自动调工具核实, 最耗时)
        # 画像注入(B方案): 有画像时 prompt 带"用户交易风格参考"段, 只影响建议贴合度
        profile_block = _build_profile_block(user_profile)
        if profile_block:
            logger.info(
                "AI 裁判已注入用户画像(仅影响建议贴合度, 不改方向): %s %s",
                symbol, stock_name,
            )
        msg_resp = requests.post(
            f"{base_url}/api/chat/conversations/{conv_id}/messages",
            json={
                "content": _build_eval_message(
                    symbol, stock_name, last_close,
                    prediction_result, direction, expected_pct,
                    profile_block=profile_block,
                )
            },
            headers=headers,
            timeout=_MESSAGE_TIMEOUT,
        )
        msg_resp.raise_for_status()
        msg_data = msg_resp.json()
        msg_payload = msg_data.get("data") if isinstance(msg_data, dict) and isinstance(msg_data.get("data"), dict) else msg_data
        ai_text = (msg_payload.get("content") or "").strip()

        verdict = _parse_verdict(ai_text)
        if verdict is None:
            raise RuntimeError(f"AI 回复未含可解析的 JSON 裁判结论: {ai_text[:200]!r}")
        result = {
            "verdict": verdict["verdict"],
            "direction": verdict["direction"],
            "reason": verdict["reason"] or "AI 未给出理由",
            "conv_id": conv_id,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
        logger.info(
            "AI 裁判 %s %s: verdict=%s direction=%s conv=%s",
            symbol, stock_name, result["verdict"], result["direction"], conv_id,
        )
        # 裁判结论落库 (独立表 prediction_referee_evals, 供 referee_impact_stats
        # 统计介入前后命中率对比; 仅 confirm/adjust 真实结论入库, abstain/降级不入)。
        if result["verdict"] in ("confirm", "adjust"):
            try:
                record_referee_eval(
                    run_id, symbol, result["verdict"], result["direction"],
                    result["reason"], conv_id,
                )
            except Exception as exc:
                logger.warning("AI 裁判落库失败 %s %s: %s", symbol, stock_name, exc)
        return result
    except Exception as exc:
        # 0.0 fail-open→abstain: 裁判不可用绝不伪造成"裁判确认过"(confirm),
        # 也不覆盖预测方向 —— 维持模型方向并显式标注 abstain。
        reason = f"裁判不可用: {exc}"
        logger.warning("AI 裁判降级 %s %s: %s", symbol, stock_name, exc)
        return {
            "verdict": "abstain",
            "direction": None,
            "reason": reason,
            "conv_id": conv_id,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    finally:
        pass  # base 占位保留(兼容未来扩展)


# ── 裁判效果统计 ────────────────────────────────────────────────────────

def referee_impact_stats(symbol: str = "") -> dict:
    """AI 裁判介入前后命中率对比 (验证裁判是帮忙还是添乱)。

    口径 (与 forecast_server._do_backtest 一致):
    - 样本: prediction_referee_evals 有记录的 run(真实裁判介入过),
      且满足: 模型输出可推断介入前方向 + target_date 在 K 线索引内。
      降级 confirm(裁判不可用)不落库, 天然被排除。
    - 介入前方向(模型投票): 各模型 model_pred_close 的中位数 vs last_close
      (≥2 个有效预测价用中位数; 否则用模型方向 up/down 多数票)。
      不用 final_direction —— 它已被裁判改过, 不是"介入前"。
      也不用 model_weight 加权 —— finalize_run 回算的权重是"与最终方向一致性",
      用它会镜像裁判结果, 污染基线。
    - 介入后方向: verdict=confirm → 维持介入前方向; verdict=adjust → 裁判 direction。
    - 实际方向: target_date 收盘 vs last_close (K线 date->close 索引)。
    - hit: 方向与实际一致 且 实际非 flat (同 _do_backtest)。

    Returns:
        {"total", "symbol", "confirm_count", "adjust_count",
         "baseline_hit", "baseline_accuracy",
         "referee_confirm_hit", "confirm_accuracy",
         "referee_adjust_hit", "adjust_accuracy",
         "referee_accuracy", "direction_changed", "samples"}
        confirm_accuracy/adjust_accuracy 样本为 0 时为 None。
    """
    try:
        from .forecast_traces import (
            get_run, list_model_outputs, list_referee_evals,
        )
    except ImportError:
        from forecast_traces import get_run, list_model_outputs, list_referee_evals

    evals = list_referee_evals()
    if symbol:
        evals = [e for e in evals if e.get("symbol") == symbol]
    empty = {
        "total": 0, "symbol": symbol or "all",
        "message": "暂无裁判记录(裁判层尚未介入过预测, 或 evaluate_prediction 未调用)",
    }
    if not evals:
        return empty

    # 按 run_id 聚合 (run_id=0 且关联不到 run 的跳过)
    by_run: dict[int, list[dict]] = {}
    for e in evals:
        rid = e.get("run_id")
        if rid:
            by_run.setdefault(rid, []).append(e)

    # 延迟加载 K 线 (只在真跑统计时拉数据, 不拖累 evaluate_prediction 主路径)
    try:
        from .forecast_models import load_kline
    except ImportError:
        from forecast_models import load_kline

    _price_idx_cache: dict[str, dict] = {}

    def _price_idx(sym: str) -> dict:
        if sym not in _price_idx_cache:
            try:
                df = load_kline(sym, days=400)
                _price_idx_cache[sym] = {
                    str(df["timestamp"].iloc[i].date()): float(df["close"].iloc[i])
                    for i in range(len(df))
                }
            except Exception:
                _price_idx_cache[sym] = {}
        return _price_idx_cache[sym]

    def _pre_referee_direction(mos: list[dict], last_close: float) -> str | None:
        """介入前方向: 模型预测价中位数 vs last_close; 不足 2 个价则多数票。"""
        closes = [float(m["model_pred_close"]) for m in mos
                  if m.get("model_pred_close") is not None]
        if len(closes) >= 2:
            median = sorted(closes)[len(closes) // 2]
            return "up" if median > last_close else "down" if median < last_close else None
        ups = sum(1 for m in mos if m.get("model_pred_direction") == "up")
        downs = sum(1 for m in mos if m.get("model_pred_direction") == "down")
        if ups == downs:
            return None
        return "up" if ups > downs else "down"

    stats = {
        "total": 0,
        "symbol": symbol or "all",
        "confirm_count": 0,
        "adjust_count": 0,
        "baseline_hit": 0,
        "referee_confirm_hit": 0,
        "referee_adjust_hit": 0,
        "referee_hit": 0,
        "direction_changed": 0,
        "samples": [],
    }

    for rid, evs in by_run.items():
        run = get_run(rid)
        if not run:
            continue
        sym = run.get("symbol") or ""
        if symbol and sym != symbol:
            continue
        last_close = run.get("last_close")
        target_date = run.get("target_date")
        if not last_close or not target_date:
            continue
        price_idx = _price_idx(sym)
        if target_date not in price_idx:
            continue
        mos = list_model_outputs(rid)
        pre_dir = _pre_referee_direction(mos, float(last_close))
        if not pre_dir:
            continue

        ev = evs[-1]  # 每 run 一条, 取最新
        verdict = ev.get("verdict") or ""
        ref_dir = ev.get("direction")
        if ref_dir not in ("up", "down"):
            ref_dir = None
        # 介入后方向: confirm 维持模型方向; adjust 用裁判方向
        post_dir = ref_dir if (verdict == "adjust" and ref_dir) else pre_dir

        actual_close = price_idx[target_date]
        base_close = float(last_close)
        actual_dir = ("up" if actual_close > base_close
                      else "down" if actual_close < base_close else "flat")
        is_hit = actual_dir != "flat"
        baseline_hit = is_hit and (pre_dir == actual_dir)
        referee_hit = is_hit and (post_dir == actual_dir)

        stats["total"] += 1
        stats["baseline_hit"] += 1 if baseline_hit else 0
        stats["referee_hit"] += 1 if referee_hit else 0
        if verdict == "adjust":
            stats["adjust_count"] += 1
            stats["direction_changed"] += 1
            stats["referee_adjust_hit"] += 1 if (ref_dir and is_hit and ref_dir == actual_dir) else 0
        else:
            stats["confirm_count"] += 1
            # confirm 时介入后 == 介入前, 命中与 baseline 同
            stats["referee_confirm_hit"] += 1 if baseline_hit else 0

        if len(stats["samples"]) < 50:
            stats["samples"].append({
                "run_id": rid, "symbol": sym, "target_date": target_date,
                "verdict": verdict, "referee_direction": ref_dir,
                "pre_direction": pre_dir, "post_direction": post_dir,
                "actual_direction": actual_dir, "actual_pct": round(
                    (actual_close / base_close - 1) * 100, 2),
                "baseline_hit": baseline_hit, "referee_hit": referee_hit,
            })

    total = stats["total"]
    stats["baseline_accuracy"] = round(stats["baseline_hit"] / total * 100, 1) if total else 0.0
    stats["referee_accuracy"] = round(stats["referee_hit"] / total * 100, 1) if total else 0.0
    stats["confirm_accuracy"] = (
        round(stats["referee_confirm_hit"] / stats["confirm_count"] * 100, 1)
        if stats["confirm_count"] else None
    )
    stats["adjust_accuracy"] = (
        round(stats["referee_adjust_hit"] / stats["adjust_count"] * 100, 1)
        if stats["adjust_count"] else None
    )
    stats["delta_accuracy_pct"] = (
        round(stats["referee_accuracy"] - stats["baseline_accuracy"], 1)
        if total else 0.0
    )
    return stats
