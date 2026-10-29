"""报告中心 API: 列出/读取 Hermes cron 输出报告。

数据源: ~/.hermes/cron/output/<job_id>/*.md

性能(2026-10-11): /list 曾被首页每 30s 轮询, 原实现对全目录**每个** .md 都
f.stat() + 逐行读标题(**先排序截断在后**, limit 不减扫描量), 无缓存, 生产实测
30-45s 渲染。修法: 先 stat 全量→按 mtime 倒序→只读命中前 limit 项的标题;
外加 mtime 签名 + TTL 缓存(新报告落盘即失效);保留期清理见
scripts/cleanup_cron_outputs.py。

可见性(2026-10-29 三审, 跨租户泄漏收口): 报告是**共享目录里的裸文件**, 没有租户列。
历史上一份盘前/盘后报告里写着全库所有人的成本价/持仓量, 而本路由只要登录就能读 ——
任何 member 都能看到别人的持仓。现在每份报告旁边有 `<name>.md.meta.json`
(见 `src/core/report_scope.py`), 本路由按它判可见性:

- 无 meta(历史存量 / 外部 cron 直写) → **仅 owner 可读**(来源不明 = fail-closed);
- `scope=shared` 且不含私有持仓 → 所有登录用户;
- `scope=private` → 仅 `owner_user_id` 本人(owner 角色不自动越权);
- 不可读时 `/content` 统一 404、`/list` 直接不出现(不泄露"存在但你不能看")。

缓存键必须带**查看者作用域**, 否则 A 的 list 结果会命中给 B(越权缓存命中)。
"""
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.core import report_scope
from src.core.paths import CRON_OUTPUT_DIR, HERMES_HOME  # noqa: F401  (KI-039 第二阶段: 路径解析下沉 core)
from src.web.api.auth import get_current_user
from src.web.models import User

router = APIRouter()
logger = logging.getLogger(__name__)

# 文件名/目录名只允许这些字符(报告文件名由生成器拼成; job_id 来自 jobs.json)。
# 旧实现只校验 file 的 ".."/分隔符, job_id 可以传 "../.." 直接逃出报告根目录。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _safe_component(name: str, what: str) -> str:
    """校验路径分量(job_id / file): 只允许单层安全名字, 拒绝空/点开头/分隔符。"""
    s = (name or "").strip()
    if not s or s in (".", "..") or not _SAFE_NAME_RE.match(s) or s.startswith(".."):
        raise HTTPException(400, f"非法{what}")
    return s


def _viewer_scope(user: User | None) -> tuple[str | None, str | None]:
    """(user_id, role)。取不到 → (None, None) = 只读"来源不明"报告的 owner 通道之外全部拒绝。"""
    if user is None:
        return None, None
    return getattr(user, "id", None), getattr(user, "role", None)


def _visible(meta: dict | None, viewer: tuple[str | None, str | None]) -> bool:
    uid, role = viewer
    return report_scope.report_visible(meta, user_id=uid, role=role)


def _is_owner(viewer: tuple[str | None, str | None]) -> bool:
    return str(viewer[1] or "").lower() == "owner"


def _read_head(report_path, limit: int) -> str:
    """只读文件头 `limit` 字节(解码为 utf-8, 坏字节忽略)。

    报告正文可能几 MB, 但标题/私有持仓绊线都只看开头: `read_text()` 会整篇载入,
    在 /list 的循环里等于每文件一次全量分配(OCR performance, 2026-10-30)。
    """
    with open(report_path, "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="ignore")


def _report_visible(report_path, viewer: tuple[str | None, str | None]) -> bool:
    """owner 直接可见(**不读 meta**) —— 既符合"数据责任人可见全部"的口径, 又保住
    /list 的"只读命中 limit 项标题"性能护栏(旧实现 30-45s 就是被逐文件 IO 拖的)。

    四审(2026-10-29): member 侧在 meta 判定通过后再做一次**内容绊线** —— meta 可能
    是旧生成器/外部 cron 写错的(自称 shared+不含私有, 正文却是「500股 成本 12.34」)。
    只扫共享报告的头 64KB, 命中即按含私有数据拒绝(member 侧 fail-closed)。
    """
    if _is_owner(viewer):
        return True
    meta = report_scope.read_report_meta(report_path)
    if not _visible(meta, viewer):
        return False
    if str((meta or {}).get("scope") or "").strip().lower() != report_scope.SCOPE_SHARED:
        return True
    try:
        # 只读前缀: 旧写法 read_text() 整篇载入再切片, 多 MB 报告会整篇读进内存。
        head = _read_head(report_path, report_scope.SUSPICIOUS_CONTENT_SCAN_BYTES)
    except OSError as e:
        # 读不了就无法证明它是纯市场 → 不展示给 member。
        logger.warning("[报告] 共享报告内容校验失败(按不可见处理) %s: %s", report_path, e)
        return False
    if report_scope.content_looks_private_holdings(head):
        logger.warning(
            "[报告] 共享报告正文含持仓成本, 拒绝 member 读取(meta 与实际内容不符): %s %s",
            getattr(report_path, "name", report_path),
            viewer[0],
        )
        return False
    return True


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
    user: User = Depends(get_current_user),
):
    """列出**当前用户可见**的 cron 报告。

    按 job_id 分组,每组按 mtime 倒序(最新在前)。
    返回: [{ job_id, job_name, file, size, mtime, title_preview }, ...]

    性能: 先只 stat 全量 → mtime 倒序 → **只读命中前 limit 项**的标题
    (旧实现对全部文件逐个读标题, limit 不减扫描量); 结果按 (job_id, limit, 查看者)
    缓存, TTL 内且 job 目录 mtime 签名未变直接命中。

    可见性(2026-10-29 三审): 不可读的报告**先于截断**被剔除, 所以 member 的 limit
    语义仍是"最多 N 条我能看的"; 缓存键含查看者, 不会把 A 的列表命中给 B。
    """
    return _list_reports_for(job_id=job_id, limit=limit, viewer=_viewer_scope(user))


def _list_reports_for(
    *,
    job_id: str | None,
    limit: int,
    viewer: tuple[str | None, str | None],
) -> dict:
    """`/list` 的实现(纯函数式, 便于单测直接调; viewer 决定可见性)。"""
    if not CRON_OUTPUT_DIR.exists():
        return {"items": [], "total": 0, "jobs": []}

    key = (job_id or "*", limit, str(viewer[0] or ""), str(viewer[1] or ""))
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

    # 2026-10-30 复审(OCR error): 先按 mtime 排序, 再逐条判可见性, 凑够 limit 立即停。
    # 旧顺序是"对**每个** .md 先判可见性"——member 侧每条都要读 meta(共享报告还读正文头),
    # 等于把模块注释里说要避免的 O(N) 文件 IO 重新引回 async 端点(457 个文件时 30-45s)。
    # 语义不变: limit 仍然只数"我能看的", 只是从最旧的文件开始不再读。
    found.sort(key=lambda x: x[0], reverse=True)
    visible: list[tuple] = []
    for item in found:
        if not _report_visible(item[2], viewer):
            continue
        visible.append(item)
        if len(visible) >= limit:
            break
    found = visible

    items = []
    for mtime, size, f, jid in found:
        title_preview = ""
        try:
            # 标题只在头部: 有界读取, 不为一行标题读整篇报告。
            title_preview = _extract_title(_read_head(f, 8192))
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
    # 2026-10-30 复审: 写缓存前先清过期项 —— 旧实现只在"同一个 key 再来读"时判定过期,
    # 已过期条目会一直驻留; 而 key 里有未校验的 job_id, 任意登录用户可用不同字符串
    # 把 entries 无限撑大(memory 随用户数线性增长)。
    now = time.monotonic()
    for stale_key in [k for k, v in _CACHE.items() if v[0] <= now]:
        _CACHE.pop(stale_key, None)
    _CACHE[key] = (now + _CACHE_TTL_SECONDS, sig, payload)
    return payload


@router.get("/content")
async def get_report_content(
    job_id: str = Query(...),
    file: str = Query(...),
    user: User = Depends(get_current_user),
):
    """读取单个报告完整 markdown。

    读取 cron 原始输出并自动去噪(_strip_meta), 保证 Dialog 不展示元信息噪音。
    可见性: 不可读 → 404「报告不存在」(与不存在同一文案, 不给存在性信号)。
    """
    return _report_content_for(
        job_id=job_id, file=file, viewer=_viewer_scope(user)
    )


def _report_content_for(
    *, job_id: str, file: str, viewer: tuple[str | None, str | None]
) -> dict:
    """`/content` 的实现(纯函数式, 便于单测直接调)。"""
    jid = _safe_component(job_id, "job_id")
    fname = _safe_component(file, "文件名")
    if not fname.endswith(".md"):
        raise HTTPException(400, "非法文件名")

    f = CRON_OUTPUT_DIR / jid / fname
    if not f.is_file():
        raise HTTPException(404, f"报告不存在: {jid}/{fname}")
    if not _report_visible(f, viewer):
        uid, role = viewer
        logger.info(
            "[报告] 拒绝访问(不可见): %s/%s viewer=%s reason=%s",
            jid,
            fname,
            uid,
            report_scope.report_visibility_reason(
                report_scope.read_report_meta(f), user_id=uid, role=role
            ),
        )
        # 统一 404: 让"存在但你不能看"与"不存在"不可区分。
        raise HTTPException(404, f"报告不存在: {jid}/{fname}")
    try:
        raw = f.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        raise HTTPException(500, f"读取失败: {e}")

    content = _strip_meta(raw)
    return {"job_id": jid, "file": fname, "content": content}


@router.get("/broker/{symbol}")
def get_broker_research_reports(symbol: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50)):
    """获取东方财富机构券商研报列表。"""
    from src.collectors.eastmoney_research import fetch_eastmoney_research_reports

    data = fetch_eastmoney_research_reports(symbol, page=page, page_size=page_size)
    return {"code": 0, "success": True, "data": data}


# 2026-08-18: 根路径 alias (前端 fetch /api/reports 直接转发到 /list)
@router.get("")
async def get_reports_root(user: User = Depends(get_current_user)):
    return await list_reports(job_id=None, limit=200, user=user)
