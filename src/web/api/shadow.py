"""Shadow Account API — 上传交割单 → 行为画像 + 影子策略 + 归因报告。

端点:
    POST /api/shadow/analyze   上传交割单 CSV/Excel → 画像 + 行为 + 规则
    GET  /api/shadow/trades    本人交割单明细(按标的) —— 供 §6.2「交割单标 K 线」复盘
    GET  /api/shadow/report/{shadow_id}  → HTML 报告
    GET  /api/shadow/report/{shadow_id}/pdf → PDF 报告(weasyprint 可用时)
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from src.core.shadow_account import (
    compute_behavior,
    compute_profile,
    extract_shadow_profile,
    render_shadow_report,
    run_shadow_attribution,
    summarize_result,
)
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["shadow"])

# 交割单落盘目录(容器内;生产建议挂载卷)
_UPLOAD_DIR = Path("/app/data/shadow_uploads") if Path("/app/data").exists() else Path("data/shadow_uploads")
_REPORT_DIR = Path("/app/data/shadow_reports") if Path("/app/data").exists() else Path("data/shadow_reports")

_ALLOWED_SUFFIX = {".csv", ".xlsx", ".xls", ".pdf"}

# §6.2「交割单标 K 线」: 分析时把成交明细**一并落库**(存进 users.shadow_profile_json 的
# `trades` 键, 无需新建表/迁移)。只保留**最近** MAX_STORED_TRADES 笔 —— 上千笔 PDF 若全量
# 塞 JSON 列会明显撑大行; 复盘只看近期够用, 且响应里带 `capped` 明示"被截断", 不假装是全量。
MAX_STORED_TRADES = 400
#: 单笔明细只留复盘必需字段(时间/标的/方向/价/量/额), 不带费用明细等冗余
_TRADE_KEYS = ("datetime", "symbol", "name", "side", "quantity", "price", "amount", "market")


@router.post("/analyze")
def analyze_journal(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传交割单 → 交易画像 + 4 项行为诊断 + 影子规则提取 + 归因。

    登录用户分析成功后, 画像(profile.to_dict())落库到 users.shadow_profile_json,
    AI 对话助手可据此给出更贴合用户交易风格的建议。
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIX:
        raise HTTPException(400, f"不支持的文件类型 {suffix or '(无扩展名)'},仅支持 {sorted(_ALLOWED_SUFFIX)}")

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 2026-09-08 C1: 落盘名用 uuid 与用户输入彻底解耦(原实现 file.filename 直接拼
    # 路径, "../../" 可写出上传目录); 后缀保留供解析器识别格式。
    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        profile = extract_shadow_profile(dest)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        logger.exception("shadow analyze failed")
        raise HTTPException(500, f"交割单解析失败: {exc}")

    behavior = None
    profile_stats = None
    records = None  # §6.2: 复用同一次解析结果做成交明细落库, 不再二次 parse
    try:
        from src.core.shadow_account.parsers import parse_file, records_to_dataframe

        _, records = parse_file(dest)
        df = records_to_dataframe(records)
        profile_stats = compute_profile(df)
        behavior = compute_behavior(df)
    except Exception as exc:
        logger.warning("behavior/profile compute failed: %s", exc)

    result = None
    try:
        attribution, shadow_pnl, real_pnl = run_shadow_attribution(profile, dest)
        result = summarize_result(profile, attribution, shadow_pnl, real_pnl)
    except Exception as exc:
        logger.warning("attribution failed: %s", exc)

    # 报告
    html_path, pdf_path = render_shadow_report(
        profile, result, behavior, out_dir=_REPORT_DIR,
    )

    # 画像落库(A 方案): 存当前登录用户的 shadow_profile_json, 供 AI 对话助手使用
    saved = False
    try:
        payload = profile.to_dict()
        # §6.2: 顺便把成交明细落库(同一列, 免迁移) —— 供 /shadow/trades 做「交割单标 K 线」复盘
        try:
            _records = records
            if _records is None:  # 上面那次解析失败(behavior 计算异常)时才重试一次
                from src.core.shadow_account.parsers import parse_file as _parse_file

                _, _records = _parse_file(dest)
            compact, capped = _compact_trades(_records)
            payload["trades"] = compact
            payload["trades_capped"] = capped
        except Exception as _exc:  # 明细落库失败不该影响画像落库/分析结果
            logger.warning("shadow trades 落库失败(画像仍保存): %s", _exc)
        user.shadow_profile_json = payload
        db.add(user)
        db.commit()
        saved = True
    except Exception as exc:
        db.rollback()
        logger.warning("shadow profile 落库失败(分析仍成功): %s", exc)

    return {
        "shadow_id": profile.shadow_id,
        "profile": profile.to_dict(),
        "behavior": behavior,
        "stats": profile_stats,
        "attribution": result.to_dict() if result else None,
        "report_html": f"/api/shadow/report/{profile.shadow_id}",
        "report_pdf": f"/api/shadow/report/{profile.shadow_id}/pdf" if pdf_path else None,
        "saved": saved,
    }


def _compact_trades(records) -> tuple[list[dict], bool]:
    """TradeRecord 列表 → 落库用紧凑字典列表(最近 MAX_STORED_TRADES 笔)。

    返回 (trades, capped)。`capped=True` 表示原明细更多、此处只留了尾部 —— 前端据此**显式标注**
    "仅展示最近 N 笔", 不让人误以为交割单只有这么点。
    """
    recs = list(records or [])
    capped = len(recs) > MAX_STORED_TRADES
    tail = recs[-MAX_STORED_TRADES:]
    out: list[dict] = []
    for r in tail:
        item = {k: getattr(r, k, None) for k in _TRADE_KEYS}
        # 数值统一成 float/int(JSON 可序列化), 缺失保持 None(前端显示 `--`, 不补 0)
        for k in ("quantity", "price", "amount"):
            v = item.get(k)
            item[k] = float(v) if isinstance(v, (int, float)) else None
        out.append(item)
    return out, capped


@router.get("/trades")
def get_my_trades(symbol: str | None = None, user: User = Depends(get_current_user)):
    """取**本人**交割单成交明细(设计稿 §6.2「交割单标 K 线」的数据源)。

    - 数据来自 `users.shadow_profile_json.trades`(上传分析时一并落库), 无上传 → 空表 + `saved=False`,
      **不编造**任何记录。
    - `?symbol=600519.SH` 可按标的过滤; 不传 = 全量(受 MAX_STORED_TRADES 截断, 见 `capped`)。
    - 归属天然隔离: 只读调用者自己那一列, 不存在越权读他人交割单的路径。
    """
    payload = user.shadow_profile_json or {}
    raw = payload.get("trades") or []
    trades = [t for t in raw if isinstance(t, dict)]
    symbols: list[str] = []
    for t in trades:
        sym = t.get("symbol")
        if isinstance(sym, str) and sym and sym not in symbols:
            symbols.append(sym)
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]
    return {
        "saved": bool(raw) or bool(payload.get("shadow_id")),
        "symbols": symbols,
        "trades": trades,
        "total": len(trades),
        "capped": bool(payload.get("trades_capped")),
        "note": "" if raw else "尚未上传交割单, 或该次分析早于成交明细落库(重新上传一次即可)",
    }


@router.get("/profile")
def get_my_profile(user: User = Depends(get_current_user)):
    """取当前登录用户的影子画像(落库版)。

    未上传过交割单时 profile=None。前端可展示"我的画像", AI 对话也可参考。
    """
    if not user.shadow_profile_json:
        return {"profile": None, "saved": False}
    return {"profile": user.shadow_profile_json, "saved": True}


def _resolve_report(shadow_id: str, ext: str, user: User) -> Path:
    """报告路径解析三重校验(2026-09-08 C1)。

    原实现 `_REPORT_DIR / f"{shadow_id}.html"` 无格式校验/无路径包含校验/无归属
    校验, 任意登录者可枚举读他人报告。现: ID 格式白名单 → resolve 后必须仍在
    报告目录内 → 落库画像的 shadow_id 必须与请求者匹配。三关统一 404(不泄露存在性)。
    """
    if not re.fullmatch(r"shadow_[0-9a-f]{8}", shadow_id or ""):
        raise HTTPException(404, "报告不存在")
    path = (_REPORT_DIR / f"{shadow_id}{ext}").resolve()
    if not path.is_relative_to(_REPORT_DIR.resolve()):
        raise HTTPException(404, "报告不存在")
    if not path.exists():
        raise HTTPException(404, "报告不存在")
    stored = (user.shadow_profile_json or {}).get("shadow_id")
    if stored != shadow_id:
        raise HTTPException(404, "报告不存在")
    return path


@router.get("/report/{shadow_id}", response_class=HTMLResponse)
def get_report(shadow_id: str, user: User = Depends(get_current_user)):
    """取 HTML 报告(仅本人)。"""
    path = _resolve_report(shadow_id, ".html", user)
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/report/{shadow_id}/pdf")
def get_report_pdf(shadow_id: str, user: User = Depends(get_current_user)):
    """取 PDF 报告(仅本人)。"""
    path = _resolve_report(shadow_id, ".pdf", user)
    return FileResponse(path, media_type="application/pdf", filename=f"{shadow_id}.pdf")
