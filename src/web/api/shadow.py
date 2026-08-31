"""Shadow Account API — 上传交割单 → 行为画像 + 影子策略 + 归因报告。

端点:
    POST /api/shadow/analyze   上传交割单 CSV/Excel → 画像 + 行为 + 规则
    GET  /api/shadow/report/{shadow_id}  → HTML 报告
    GET  /api/shadow/report/{shadow_id}/pdf → PDF 报告(weasyprint 可用时)
"""

from __future__ import annotations

import logging
import shutil
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
    dest = _UPLOAD_DIR / f"{file.filename or 'journal'}{suffix}"
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
        user.shadow_profile_json = profile.to_dict()
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


@router.get("/profile")
def get_my_profile(user: User = Depends(get_current_user)):
    """取当前登录用户的影子画像(落库版)。

    未上传过交割单时 profile=None。前端可展示"我的画像", AI 对话也可参考。
    """
    if not user.shadow_profile_json:
        return {"profile": None, "saved": False}
    return {"profile": user.shadow_profile_json, "saved": True}


@router.get("/report/{shadow_id}", response_class=HTMLResponse)
def get_report(shadow_id: str):
    """取 HTML 报告。"""
    path = _REPORT_DIR / f"{shadow_id}.html"
    if not path.exists():
        raise HTTPException(404, f"报告不存在: {shadow_id}")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/report/{shadow_id}/pdf")
def get_report_pdf(shadow_id: str):
    """取 PDF 报告。"""
    path = _REPORT_DIR / f"{shadow_id}.pdf"
    if not path.exists():
        raise HTTPException(404, f"PDF 报告不存在: {shadow_id}")
    return FileResponse(path, media_type="application/pdf", filename=f"{shadow_id}.pdf")
