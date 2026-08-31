"""媒体内容文本化工具: 图片 OCR + 文件文本提取。

供微信 worker(收图片/文件消息)与网页上传共用。
依赖(容器内已装): pytesseract + tesseract-ocr-chi-sim, Pillow, pypdf, pandas。

图片会留档保存到 <DATA_DIR>/wechat_media/(容器内默认 /app/data/wechat_media),
返回 (识别文本, 保存路径); 文件按扩展名解析, 返回文本摘要。
"""
import logging
import os
import time
import uuid
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

# 图片/文件留档目录。容器内 DATA_DIR=/app/data → /app/data/wechat_media;
# 本地可用 DATA_DIR 环境变量覆盖。
MEDIA_DIR = Path(
    os.getenv("WECHAT_MEDIA_DIR")
    or (Path(os.getenv("DATA_DIR", "/app/data")) / "wechat_media")
)

# 文本摘要上限(避免超大文件撑爆上下文)
MAX_TEXT_LEN = 10_000
# 表格/CSV 读取行数与展示行数
FILE_MAX_ROWS_READ = 200
FILE_MAX_ROWS_SHOW = 50
FILE_MAX_COLS_SHOW = 20
# PDF 提取页数
PDF_MAX_PAGES = 5

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
_TABLE_EXTS = {".xlsx", ".xls", ".csv"}
_TEXT_EXTS = {".txt", ".md", ".log", ".json", ".csv"}


def _ensure_media_dir() -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR


def _safe_filename(filename: str) -> str:
    """清洗文件名, 只保留安全字符, 防止路径穿越。"""
    name = Path(filename or "media").name.strip()
    name = "".join(ch for ch in name if ch not in "/\\\x00" and ord(ch) >= 32)
    return name or "media"


def save_bytes(data: bytes, filename: str) -> str:
    """把原始媒体字节存到留档目录, 返回保存路径(供文件解析/留档共用)。"""
    _ensure_media_dir()
    safe = _safe_filename(filename)
    stem = Path(safe).stem or "media"
    ext = Path(safe).suffix.lower() or ""
    path = MEDIA_DIR / f"wx_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{stem}{ext}"
    path.write_bytes(data)
    return str(path)


def _open_image(path_or_bytes):
    from PIL import Image

    if isinstance(path_or_bytes, (bytes, bytearray)):
        return Image.open(BytesIO(path_or_bytes))
    return Image.open(path_or_bytes)


def image_to_text(path_or_bytes) -> tuple[str, str | None]:
    """OCR 提取图片文字(中文 chi_sim)。返回 (文本, 留档保存路径)。

    图片会保存到 MEDIA_DIR 便于留档; OCR 失败或不可用返回 ("", None)。
    """
    save_path: str | None = None
    try:
        import pytesseract

        img = _open_image(path_or_bytes)
        # 留档保存(JPEG 去 alpha, 其他格式按原格式)
        fmt = (img.format or "JPEG").upper()
        ext = ".jpg" if fmt in ("JPEG", "JPG") else f".{fmt.lower()}" if fmt in ("PNG", "BMP", "WEBP", "TIFF") else ".png"
        if ext == ".jpg" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        path = MEDIA_DIR / f"wx_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{ext}"
        _ensure_media_dir()
        img.save(path, format="JPEG" if ext == ".jpg" else fmt)
        save_path = str(path)

        text = pytesseract.image_to_string(img, lang="chi_sim")
        return text.strip(), save_path
    except Exception as exc:
        logger.warning(f"图片 OCR 失败: {exc}")
        return "", save_path


def _df_to_text(df, max_rows: int = FILE_MAX_ROWS_SHOW, max_cols: int = FILE_MAX_COLS_SHOW) -> str:
    """DataFrame → 紧凑文本(前若干行/列)。"""
    if df is None or df.empty:
        return ""
    df = df.head(max_rows)
    if df.shape[1] > max_cols:
        df = df.iloc[:, :max_cols]
    return df.to_string(index=False, max_rows=max_rows, max_cols=max_cols)


def file_to_text(path) -> str:
    """按扩展名提取文件文本摘要; 不支持的扩展名或解析失败返回空字符串。

    - .xlsx/.xls/.csv: pandas 读前若干行转文本
    - .pdf: pypdf 提取前若干页
    - .txt/.md 等文本: 直接读(截断)
    """
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in _TABLE_EXTS:
            import pandas as pd

            if ext == ".csv":
                df = pd.read_csv(path, nrows=FILE_MAX_ROWS_READ, dtype=str)
            else:
                df = pd.read_excel(path, nrows=FILE_MAX_ROWS_READ, dtype=str)
            return _df_to_text(df)
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            pages = []
            for page in reader.pages[:PDF_MAX_PAGES]:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t.strip():
                    pages.append(t.strip())
            return "\n\n".join(pages)
        if ext in _TEXT_EXTS:
            data = p.read_text(encoding="utf-8", errors="replace")
            return data[:MAX_TEXT_LEN]
    except Exception as exc:
        logger.warning(f"文件解析失败 {p.name}: {exc}")
    return ""
