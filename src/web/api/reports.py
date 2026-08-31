"""报告中心 API: 列出/读取 Hermes cron 输出报告。

数据源: ~/.hermes/cron/output/<job_id>/*.md
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)

# Hermes cron 输出根目录
# 容器内通过 HERMES_HOME 环境变量指定(挂载主机 ~/.hermes 到 /hermes, HERMES_HOME=/hermes)
# 则 CRON_OUTPUT_DIR = /hermes/cron/output
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME")
    or os.environ.get("CRON_OUTPUT_DIR")
    or "/hermes"  # 推荐挂载点
)
CRON_OUTPUT_DIR = HERMES_HOME / "cron" / "output"


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
    """
    if not CRON_OUTPUT_DIR.exists():
        return {"items": [], "total": 0, "jobs": []}

    name_map = _job_name_map()
    items = []
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
            # 提取 md 标题(第一行非空 # 开头)
            title_preview = ""
            try:
                with f.open("r", encoding="utf-8", errors="ignore") as fp:
                    for line in fp:
                        line = line.strip()
                        if line.startswith("# "):
                            title_preview = line[2:].strip()[:80]
                            break
            except Exception:
                pass
            items.append({
                "job_id": jid,
                "job_name": name_map.get(jid, jid),
                "file": f.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "title_preview": title_preview,
            })

    # 按 mtime 倒序
    items.sort(key=lambda x: x["mtime"], reverse=True)
    items = items[:limit]

    return {
        "items": items,
        "total": len(items),
        "jobs": [
            {"job_id": j, "job_name": name_map.get(j, j)}
            for j in sorted(jobs_seen)
        ],
    }


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
