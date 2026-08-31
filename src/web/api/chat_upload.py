"""AI 对话助手附件上传/解析 API。

POST /api/chat/upload
- multipart/form-data, 字段 file(最大 20MB)
- 按扩展名解析: 图片 → pytesseract OCR(chi_sim) / Excel → pandas 前 50 行 / PDF → pypdf 前 5 页 / txt,md → 直接读
- 解析失败或类型不支持也返回 200: {text: "", filename, error: "提示"}, 由前端拼进消息内容
"""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat-upload"])

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20MB

# 附件落盘目录(容器内 /app/data/wechat_media; 本地开发回退 data/wechat_media)
_UPLOAD_DIR = (
    Path("/app/data/wechat_media") if Path("/app/data").exists() else Path("data/wechat_media")
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_EXCEL_SUFFIXES = {".xlsx", ".xls", ".csv"}
_PDF_SUFFIXES = {".pdf"}
_TEXT_SUFFIXES = {".txt", ".md"}

# 单文件解析输出上限, 避免超大附件把对话上下文撑爆
_MAX_TEXT_CHARS = 100_000


def _read_text_with_fallback(path: Path) -> str:
    """UTF-8 优先, 失败回退 GBK(国内 Excel/CSV/文本常见编码)。"""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, UnicodeError):
        return path.read_text(encoding="gbk", errors="replace")


def _parse_image(path: Path) -> tuple[str, str | None]:
    """图片 OCR(pytesseract, 中文 chi_sim)。返回 (text, error)。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return "", f"服务器未安装 OCR 依赖({exc.name}), 暂时无法解析图片"
    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang="chi_sim")
        text = (text or "").strip()
        if not text:
            return "", "图片 OCR 未识别到文字(可能为空白图或字体特殊)"
        return text[:_MAX_TEXT_CHARS], None
    except Exception as exc:  # noqa: BLE001 - tesseract 二进制缺失/语言包缺失等统一转提示
        logger.warning("chat upload OCR failed for %s: %s", path.name, exc)
        return "", f"图片 OCR 失败: {exc}"


def _parse_excel(path: Path, suffix: str) -> tuple[str, str | None]:
    """Excel/CSV → pandas 读前 50 行转文本。返回 (text, error)。"""
    try:
        import pandas as pd
    except ImportError:
        return "", "服务器未安装 pandas, 暂时无法解析表格文件"
    try:
        if suffix == ".csv":
            # 编码探测: utf-8 优先, 失败回退 gbk
            try:
                df = pd.read_csv(path, nrows=50, encoding="utf-8")
            except (UnicodeDecodeError, UnicodeError):
                df = pd.read_csv(path, nrows=50, encoding="gbk")
        else:
            df = pd.read_excel(path, nrows=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat upload excel parse failed for %s: %s", path.name, exc)
        return "", f"表格解析失败: {exc}"
    if df is None or df.empty:
        return "", "表格内容为空"
    text = df.to_string(index=False, max_colwidth=40)
    return text[:_MAX_TEXT_CHARS], None


def _parse_pdf(path: Path) -> tuple[str, str | None]:
    """PDF → pypdf 提取前 5 页文本。返回 (text, error)。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "服务器未安装 pypdf, 暂时无法解析 PDF"
    try:
        reader = PdfReader(path)
        parts = []
        for page in reader.pages[:5]:
            parts.append(page.extract_text() or "")
        text = "\n".join(parts).strip()
        if not text:
            return "", "PDF 未提取到文字(可能为扫描件, 暂不支持扫描件 OCR)"
        if len(reader.pages) > 5:
            text += f"\n\n[PDF 共 {len(reader.pages)} 页, 仅提取前 5 页]"
        return text[:_MAX_TEXT_CHARS], None
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat upload pdf parse failed for %s: %s", path.name, exc)
        return "", f"PDF 解析失败: {exc}"


def _to_data_url(path: Path) -> str | None:
    """图片 → base64 data URL(多模态直连模型看图)。"""
    try:
        import base64 as _b64
        import mimetypes

        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{mime};base64,{_b64.b64encode(raw).decode('ascii')}"
    except Exception:
        return None


@router.post("/upload")
def upload_attachment(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传并解析对话附件(图片 OCR / Excel / PDF / 文本)。"""
    del user, db  # 登录保护 + 路由级依赖已足够, 暂无按用户落库需求
    filename = Path(file.filename or "").name or "attachment"
    suffix = Path(filename).suffix.lower()

    if suffix not in _IMAGE_SUFFIXES | _EXCEL_SUFFIXES | _PDF_SUFFIXES | _TEXT_SUFFIXES:
        return {
            "text": "",
            "filename": filename,
            "error": f"不支持的文件类型 {suffix or '(无扩展名)'}, 仅支持图片(png/jpg/webp)、Excel(xlsx/xls/csv)、PDF、txt/md",
        }

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 时间戳 + uuid 前缀防止重名/路径穿越
    dest = _UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"

    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "文件超过 20MB 限制")
            f.write(chunk)

    try:
        if suffix in _IMAGE_SUFFIXES:
            text, error = _parse_image(dest)
            # 多模态直连: 图片同时返回 base64 data URL, 供模型直接看图(OCR 文本作为兜底说明)
            image_data = _to_data_url(dest)
        elif suffix in _EXCEL_SUFFIXES:
            text, error = _parse_excel(dest, suffix)
            image_data = None
        elif suffix in _PDF_SUFFIXES:
            text, error = _parse_pdf(dest)
            image_data = None
        else:
            try:
                text = _read_text_with_fallback(dest)
                if not text.strip():
                    text, error = "", "文件内容为空"
                else:
                    error = None
                    if len(text) > _MAX_TEXT_CHARS:
                        text = text[:_MAX_TEXT_CHARS] + "\n\n[内容过长, 已截断]"
            except Exception as exc:  # noqa: BLE001
                text, error = "", f"文本读取失败: {exc}"
            image_data = None
    finally:
        # 解析完成即清理, 附件内容已随 text 返回, 无需长期留存
        dest.unlink(missing_ok=True)

    result: dict = {"text": text, "filename": filename}
    if image_data:
        result["image_data"] = image_data
    if error:
        result["error"] = error
    return result
