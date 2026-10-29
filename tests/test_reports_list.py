"""P1(2026-10-11) /api/reports/list 性能+正确性回归。

修的两个缺陷:
  1) 性能: 原实现对全目录**每个** .md 都 f.stat()+逐行读标题, 排序截断在后
     → limit 不减扫描量; 无缓存; 被首页每 30s 轮询。
  2) 正确性: title_preview 取原始首行 → 恒显 `# Cron Job: 任务名` 噪音,
     而非正文真标题(446/457 文件受影响)。

覆盖: 真标题提取 / limit 语义(只读命中项) / mtime 倒序 / 缓存命中与 mtime
签名失效 / 非 md 忽略。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import src.web.api.reports as reports

# 本文件只验证 list 的性能/排序/缓存语义, 用 owner 视角(可见全部)保持语义不变;
# 跨租户可见性在 tests/test_reports_scope.py 单独验证。
_OWNER = ("test-owner-uid", "owner")


NOISY_TEMPLATE = """# Cron Job: {job}
**Job ID:** {job}
**Run Time:** 2026-10-10 08:30:00
**Schedule:** 0 30 8 * * *
## Prompt
<一整段 skill 定义/提示词>
## Response
# {real_title}
正文内容...
"""


def _write_report(path: Path, job: str, real_title: str, mtime: float) -> None:
    path.write_text(NOISY_TEMPLATE.format(job=job, real_title=real_title), encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture(autouse=True)
def _clear_reports_cache():
    reports._clear_cache()
    yield
    reports._clear_cache()


def _setup(tmp_path, monkeypatch):
    root = tmp_path / "cron" / "output"
    root.mkdir(parents=True)
    # HERMES_HOME 指向 tmp_path, 让 _job_name_map 读不到 jobs.json(返回 {})
    monkeypatch.setattr(reports, "CRON_OUTPUT_DIR", root)
    monkeypatch.setattr(reports, "HERMES_HOME", tmp_path)
    return root


def test_title_preview_is_real_title_not_cron_job_noise(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    jd = root / "premarket-daily"
    jd.mkdir()
    _write_report(jd / "a.md", "premarket-daily", "📈 A股盘前报告 2026-10-10", 1000)

    import asyncio

    out = reports._list_reports_for(job_id=None, limit=10, viewer=_OWNER)
    assert out["total"] == 1
    assert out["items"][0]["title_preview"] == "📈 A股盘前报告 2026-10-10"
    assert "Cron Job" not in out["items"][0]["title_preview"]


def test_no_response_anchor_still_skips_cron_job_header(tmp_path, monkeypatch):
    """旧格式无 `## Response` 锚点: 也要跳过 `# Cron Job:` 噪音头。"""
    root = _setup(tmp_path, monkeypatch)
    jd = root / "weekly"
    jd.mkdir()
    (jd / "w.md").write_text(
        "# Cron Job: weekly\n**Run Time:** x\n# 周报标题\nbody\n", encoding="utf-8"
    )

    import asyncio

    out = reports._list_reports_for(job_id=None, limit=10, viewer=_OWNER)
    assert out["items"][0]["title_preview"] == "周报标题"


def test_limit_returns_newest_and_only_reads_hits(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    jd = root / "jobA"
    jd.mkdir()
    for i in range(6):  # mtime 递增
        _write_report(jd / f"r{i}.md", "jobA", f"报告{i}", 1000 + i)

    # 统计标题读取次数: 只有命中前 limit 项应被 read_text
    real_read_text = Path.read_text
    reads: list[str] = []

    def _counting_read_text(self, *a, **k):
        reads.append(str(self))
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)

    import asyncio

    out = reports._list_reports_for(job_id=None, limit=2, viewer=_OWNER)
    assert out["total"] == 2
    # mtime 倒序: 最新 r5,r4
    assert [it["file"] for it in out["items"]] == ["r5.md", "r4.md"]
    assert [it["title_preview"] for it in out["items"]] == ["报告5", "报告4"]
    # 关键性能断言: 只读了命中项的标题, 不是全部 6 个
    assert len(reads) == 2, f"应只读 2 个标题, 实际读了 {reads}"


def test_cache_hit_skips_all_reads(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    jd = root / "jobB"
    jd.mkdir()
    _write_report(jd / "x.md", "jobB", "缓存标题", 2000)

    import asyncio

    first = reports._list_reports_for(job_id=None, limit=5, viewer=_OWNER)

    reads: list[str] = []
    real_read_text = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (reads.append(str(self)), real_read_text(self, *a, **k))[1],
    )
    second = reports._list_reports_for(job_id=None, limit=5, viewer=_OWNER)
    assert second == first
    assert reads == [], "TTL 内且 mtime 未变应命中缓存, 零文件读取"


def test_new_file_invalidates_cache_via_dir_mtime(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    jd = root / "jobC"
    jd.mkdir()
    _write_report(jd / "old.md", "jobC", "旧报告", 3000)

    import asyncio

    out1 = reports._list_reports_for(job_id=None, limit=5, viewer=_OWNER)
    assert out1["total"] == 1

    # 新报告落盘(改 job 目录 mtime) → 缓存立即失效, 不必等 TTL。
    # 显式 bump 目录 mtime: 避免依赖文件系统目录 mtime 的更新粒度(某些 FS 秒级,
    # 同秒内新增文件不改变 st_mtime_ns → 偶发假红)。
    import time

    _write_report(jd / "new.md", "jobC", "新报告", time.time())
    bumped = jd.stat().st_mtime + 10
    os.utime(jd, (bumped, bumped))
    out2 = reports._list_reports_for(job_id=None, limit=5, viewer=_OWNER)
    assert out2["total"] == 2
    assert out2["items"][0]["title_preview"] == "新报告"


def test_non_md_and_job_filter(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    for jid in ("j1", "j2"):
        d = root / jid
        d.mkdir()
        _write_report(d / "a.md", jid, f"标题-{jid}", 1000)
        (d / "ignore.txt").write_text("not a report", encoding="utf-8")

    import asyncio

    out = reports._list_reports_for(job_id=None, limit=10, viewer=_OWNER)
    assert out["total"] == 2
    assert {it["job_id"] for it in out["items"]} == {"j1", "j2"}

    reports._clear_cache()
    only = reports._list_reports_for(job_id="j1", limit=10, viewer=_OWNER)
    assert only["total"] == 1
    assert only["items"][0]["job_id"] == "j1"
