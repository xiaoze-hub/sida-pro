"""报告中心 API: 列出/读取 Hermes cron 输出报告。

数据源: ~/.hermes/cron/output/<job_id>/*.md

性能(2026-10-11): /list 曾被首页每 30s 轮询, 原实现对全目录**每个** .md 都
f.stat() + 逐行读标题(**先排序截断在后**, limit 不减扫描量), 无缓存, 生产实测
30-45s 渲染。修法: 先 stat 全量→按 mtime 倒序→只读命中前 limit 项的标题;
外加 mtime 签名 + TTL 缓存(新报告落盘即失效);保留期清理见
scripts/cleanup_cron_outputs.py。
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from src.core.paths import CRON_OUTPUT_DIR, HERMES_HOME  # noqa: F401  (KI-039 第二阶段: 路径解析下沉 core)

router = APIRouter()
logger = logging.getLogger(__name__)


def _strip_meta(content: str) -> str:
    """剥掉 cron 原始报告的元信息噪音, 只留正文。

    噪音构成(开头):
      # Cron Job: <任务名>
      **Job ID:** ...
      **Run Time:** ...
      **Schedule:** ...
      ## Prompt
      <整段 skill 定义 / Prompt 内容>
      ...
      ## Response            <- cron 系统注入的"正文开始"标记
      # 📈 <真正的报告标题>   <- 正文起点

    正文起点锚定: 找到 '## Response' 之后出现的第一个一级标题 '# x'(非 '## ')。
    找不到 Response 则退回到 '## Prompt' 之后第一个 '# '; 再找不到则原样返回(不误删)。
    """
    lines = content.splitlines()

    def _first_h1_after(start: int) -> int | None:
        for j in range(start + 1, len(lines)):
            s = lines[j].strip()
            if s.startswith("# ") and not s.startswith("## "):
                return j
        return None

    # 优先锚点: ## Response
    anchor = None
    for i, ln in enumerate(lines):
        if ln.strip() == "## Response":
            anchor = i
            break
    # 次选锚点: ## Prompt
    if anchor is None:
        for i, ln in enumerate(lines):
            if ln.strip().startswith("## Prompt"):
                anchor = i
                break

    if anchor is None:
        return content

    body_start = _first_h1_after(anchor)
    if body_start is None:
        # 没找到一级标题, 退回到 anchor 之后第一行非空
        for j in range(anchor + 1, len(lines)):
            if lines[j].strip():
                body_start = j
                break
    if body_start is None:
        return content
    return "\n".join(lines[body_start:])


def _extract_title(raw: str) -> str:
    """从 cron 原始报告取**真标题**(剥掉元信息噪音后的第一个一级标题)。

    旧实现直接取原始首行 → 446/457 文件恒显 `# Cron Job: 任务名` 噪音(首行是
    元信息头而非正文标题)。这里复用 _strip_meta 先剥噪, 再取正文首个 `# x`;
    仍撞到 `# Cron Job:` 头则跳过继续找(兼容无 `## Response` 锚点的旧格式)。
    """
    body = _strip_meta(raw)
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            title = s[2:].strip()
            if title.lower().startswith("cron job"):
                continue  # 元信息噪音头, 继续找真标题
            return title[:80]
    return ""


# ── /list 缓存(2026-10-11) ────────────────────────────────────────────
# 首页每 30s 轮询 /api/reports; 全量扫 457 文件的开销不该每轮重付。
# 缓存键=(job_id, limit); 命中条件=未过 TTL **且** job 目录 mtime 签名未变。
_CACHE_TTL_SECONDS = 30.0
_CACHE: dict[tuple, tuple[float, tuple, dict]] = {}


def _dir_signature() -> tuple:
    """job 目录集 + 各自 mtime_ns 的轻量签名(只 stat 目录, 不碰文件)。

    新报告落盘会改所在 job 目录的 mtime → 签名变化 → 缓存立即失效,
    不必等 TTL 过期; 无新增时 TTL 内直接命中(避免重扫)。
    """
    try:
        sig = []
        for d in CRON_OUTPUT_DIR.iterdir():
            if not d.is_dir():
                continue
            try:
                sig.append((d.name, d.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(sorted(sig))
    except OSError:
        return ()


def _clear_cache() -> None:
    """清空 /list 缓存(测试隔离用)。"""
    _CACHE.clear()


def _job_name_map() -> dict:
    """job_id → 人类可读任务名(从 jobs.json 读)。"""
    jobs_file = HERMES_HOME / "cron" / "jobs.json"
    if not jobs_file.exists():
        return {}
    try:
        data = json.loads(jobs_file.read_text())
        return {j["id"]: j.get("name", j["id"]) for j in data.get("jobs", [])}
    except Exception as e:
        logger.warning(f"读 jobs.json 失败: {e}")
        return {}


@router.get("/list")
async def list_reports(
    job_id: Optional[str] = Query(None, description="按 job_id 过滤"),
    limit: int = Query(200, ge=1, le=1000, description="最多返回多少个文件"),
):
    """列出所有 cron 报告。

    按 job_id 分组,每组按 mtime 倒序(最新在前)。
    返回: [{ job_id, job_name, file, size, mtime, title_preview }, ...]

    性能: 先只 stat 全量 → mtime 倒序 → **只读命中前 limit 项**的标题
    (旧实现对全部文件逐个读标题, limit 不减扫描量); 结果按 (job_id, limit)
    缓存, TTL 内且 job 目录 mtime 签名未变直接命中。
    """
    if not CRON_OUTPUT_DIR.exists():
        return {"items": [], "total": 0, "jobs": []}

    key = (job_id or "*", limit)
    sig = _dir_signature()
    cached = _CACHE.get(key)
    if cached is not None and cached[0] > time.monotonic() and cached[1] == sig:
        return cached[2]

    name_map = _job_name_map()
    found: list[tuple] = []  # (mtime, size, path, jid)
    jobs_seen = set()

    for job_dir in sorted(CRON_OUTPUT_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        jid = job_dir.name
        if job_id and jid != job_id:
            continue
        jobs_seen.add(jid)

        for f in job_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            try:
                stat = f.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, f, jid))

    # 先排序取前 limit, 再只读命中项标题(limit 直接决定读取量)
    found.sort(key=lambda x: x[0], reverse=True)
    found = found[:limit]

    items = []
    for mtime, size, f, jid in found:
        title_preview = ""
        try:
            raw = f.read_text(encoding="utf-8", errors="ignore")
            title_preview = _extract_title(raw)
        except Exception:
            pass
        items.append({
            "job_id": jid,
            "job_name": name_map.get(jid, jid),
            "file": f.name,
            "size": size,
            "mtime": int(mtime),
            "mtime_iso": datetime.fromtimestamp(mtime).isoformat(),
            "title_preview": title_preview,
        })

    payload = {
        "items": items,
        "total": len(items),
        "jobs": [
            {"job_id": j, "job_name": name_map.get(j, j)}
            for j in sorted(jobs_seen)
        ],
    }
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, sig, payload)
    return payload


@router.get("/content")
async def get_report_content(
    job_id: str = Query(...),
    file: str = Query(...),
):
    """读取单个报告完整 markdown。

    读取 cron 原始输出并自动去噪(_strip_meta), 保证 Dialog 不展示元信息噪音。
    """
    # 防止路径穿越
    if ".." in file or "/" in file or "\\" in file:
        raise HTTPException(400, "非法文件名")

    f = CRON_OUTPUT_DIR / job_id / file
    if not f.exists() or not f.is_file():
        raise HTTPException(404, f"报告不存在: {job_id}/{file}")
    try:
        raw = f.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(500, f"读取失败: {e}")

    content = _strip_meta(raw)
    return {"job_id": job_id, "file": file, "content": content}


# 2026-08-18: 根路径 alias (前端 fetch /api/reports 直接转发到 /list)
@router.get("")
async def get_reports_root():
    return await list_reports(limit=200)
