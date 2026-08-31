"""问财 NLP 自然语言选股接口。

端点:
    GET /api/wencai?query=<URL编码的问财条件>

核心纯函数:
    run_wencai(query: str) -> dict
        -> {"available": bool, "rows": [{"symbol", "name", ...关键指标列}], "note": str}

行为约定:
- 调 data_source.thsdk_l2.get_wencai_nlp(query)(懒加载,避免 import 时依赖 thsdk)。
- 股票代码 300033.SZ → USZA300033 / 600519.SH → USHA600519(thsdk 行情前缀)。
- thsdk 未安装 / 数据源不可用 → available:false + note,绝不伪造数据。
- 查询成功但空结果 → available:true + 空 rows + note。
- 单次 rows 截断上限 300 行(raw 可能上千)。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 单次返回行数上限(raw 可能上千, 前端面板只展示 50)
MAX_ROWS = 300

# 问财返回代码后缀 → thsdk 行情前缀(见 data_source/thsdk_l2.py 常量)
_SUFFIX_PREFIX = {
    "SZ": "USZA",  # 深 A
    "SH": "USHA",  # 沪 A
    "BJ": "USTM",  # 北交所
    "HK": "UHKG",  # 港股
    "US": "UNQQ",  # 美股
}

# 中文指标列名里的日期尾巴, 如 "主力资金流向[20260819]" → "主力资金流向"
_TS_SUFFIX_RE = re.compile(r"\[[0-9]{8}\]$")

# 涨跌幅类列: 问财返回值已是百分数(如涨停≈9.996), 仅做舍入, 不换算
_PCT_COL_RE = re.compile(r"涨跌幅|涨幅|跌幅")

# 数字样字符串(thsdk 问财返回的指标值实际是 str, 如 '-489459.4000000004')
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def convert_symbol(raw: Any) -> str:
    """问财代码 → thsdk 行情代码: 300033.SZ → USZA300033, 600519.SH → USHA600519。

    已带 US/UR/UH 前缀或无法识别的代码原样返回(不猜测、不伪造)。
    """
    code = str(raw or "").strip()
    if not code:
        return ""
    upper = code.upper()
    if upper.startswith(("US", "UR", "UH")):
        return upper
    parts = upper.split(".")
    if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
        prefix = _SUFFIX_PREFIX.get(parts[1])
        if prefix:
            return prefix + parts[0]
    return upper


def _clean_col(name: Any) -> str:
    """清洗列名: '涨跌幅:前复权[20260819]' → '涨跌幅:前复权'。"""
    return _TS_SUFFIX_RE.sub("", str(name or "")).strip()


def _norm_value(v: Any, col: str) -> Any:
    """数值归一化(JSON 安全 + 可读): 数字样字符串转数值, numpy 标量转原生, 百分比列 *100, float 取 2 位。"""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s and _NUM_RE.match(s):
            v = float(s)
        else:
            return v
    if isinstance(v, int):
        return v
    if hasattr(v, "item"):  # numpy 标量 → 原生类型
        try:
            v = v.item()
        except Exception:
            return str(v)
    if isinstance(v, float):
        if _PCT_COL_RE.search(col):
            return round(v, 2)  # 问财涨跌幅已是百分数(如涨停股=9.996, 展示 10.0%), 不再 *100
        return round(v, 2)
    return str(v)


def _df_to_rows(df: Any) -> List[Dict[str, Any]]:
    """DataFrame → [{symbol, name, ...指标列}], 空/异常返回 []。"""
    if df is None:
        return []
    try:
        records = df.to_dict("records")
    except Exception as e:
        logger.warning(f"[wencai] DataFrame 转 records 失败: {e}")
        return []

    rows: List[Dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        row: Dict[str, Any] = {
            "symbol": convert_symbol(rec.get("股票代码", rec.get("代码"))),
            "name": str(rec.get("股票简称", rec.get("名称", "")) or "").strip(),
        }
        seen = {"symbol", "name"}
        for k, v in rec.items():
            ck = _clean_col(k)
            if not ck or ck in ("股票代码", "代码", "股票简称", "名称"):
                continue
            base, i = ck, 2
            while ck in seen:  # 去重: 同名指标列(如多个日期列)追加序号
                ck = f"{base}_{i}"
                i += 1
            seen.add(ck)
            row[ck] = _norm_value(v, ck)
        rows.append(row)
    return rows


def run_wencai(query: str) -> dict:
    """问财选股纯函数: 真实调 thsdk, 数据源不可用/失败时诚实降级, 绝不伪造。"""
    q = (query or "").strip()
    if not q:
        return {"available": False, "rows": [], "note": "问财条件为空, 请填写选股条件"}

    # 懒加载: thsdk 未安装时模块导入即抛异常, 这里转成 available:false
    try:
        from data_source.thsdk_l2 import get_wencai_nlp
    except Exception as e:
        logger.warning(f"[wencai] thsdk 数据源不可用: {e}")
        return {
            "available": False,
            "rows": [],
            "note": "L2 问财数据源未接入(thsdk 不可用), 无法执行问财选股",
        }

    try:
        df = get_wencai_nlp(q)
    except Exception as e:
        logger.warning(f"[wencai] 问财查询失败 query={q!r}: {e}")
        return {"available": False, "rows": [], "note": f"L2 问财查询失败: {e}"}

    rows = _df_to_rows(df)
    total = len(rows)
    if total == 0:
        return {"available": True, "rows": [], "note": "问财查询成功, 未命中任何股票"}

    truncated = total > MAX_ROWS
    rows = rows[:MAX_ROWS]
    note = f"问财命中 {total} 只"
    if truncated:
        note += f", 仅返回前 {MAX_ROWS} 只"

    # 查询结果入池(P1 整合): 供当日候选池共振计分, 静默失败不影响查询
    try:
        from src.core.entry_candidates import record_manual_query_candidates

        items = []
        for r in rows:
            sym = str(r.get("代码") or r.get("股票代码") or r.get("symbol") or "").strip()
            if not sym:
                continue
            sym = sym.split(".")[0].replace("USZA", "").replace("USHA", "")
            items.append({
                "symbol": sym,
                "market": "CN",
                "name": str(r.get("名称") or r.get("股票简称") or r.get("name") or ""),
            })
        record_manual_query_candidates(kind="wencai", query_text=q, items=items)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"问财结果入池跳过: {e}")

    return {"available": True, "rows": rows, "note": note}


# ── FastAPI 路由(fastapi 缺失时模块仍可导入, 纯函数不受影响) ──
try:
    from fastapi import APIRouter, Query

    router = APIRouter(tags=["wencai"])

    @router.get("")
    def api_wencai(query: str = Query(..., description="问财自然语言选股条件(URL 编码)")) -> dict:
        return run_wencai(query)

except Exception as e:  # pragma: no cover - fastapi 不在环境时仅跳过路由
    logger.warning(f"[wencai] fastapi 不可用, 跳过路由注册: {e}")
    router = None
