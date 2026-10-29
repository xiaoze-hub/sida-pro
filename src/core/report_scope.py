"""报告可见性/归属(2026-10-29 三审: 报告中心跨租户泄漏收口)。

## 为什么需要它

报告是**落盘的 markdown 文件**(`CRON_OUTPUT_DIR/<job_id>/*.md`), 没有租户列。
历史上 `report_generator` 生成盘前/盘后报告时 `_collect_positions` 不按用户过滤,
于是**全库所有人的成本价/持仓量/自选**被写进同一个文件, 而 `/api/reports` 只要
登录就能读 —— 任一 member 都能看到别人(乃至所有租户)的持仓成本。

文件本身无法靠 SQL 作用域过滤, 所以改成两条纪律:

1. **写**: 生成报告时按归属采集(见 `src/core/report_generator.py`、
   `src/core/premarket_evidence.py`), 不再把任意人的私有持仓写进共享文件;
2. **读**: 每份报告旁边落一份 `<report>.md.meta.json` 说明 `scope`/`owner_user_id`,
   API 按它判可见性。**没有 meta 的报告(历史存量)= 来源不明 → 仅 owner 可读**
   (fail-closed: 不知道是谁的私有数据, 就不能给任何 member 看)。

## 判定表(唯一实现, 测试见 tests/test_reports_scope.py)

| meta 状态 | 内容 | member(登录) | owner |
|---|---|---|---|
| 无 meta(历史/外部 cron 写入) | 未知 | 不可读(404, list 不出现) | 可读 |
| `scope=shared`, `contains_private_holdings=false` | 纯市场口径 | 可读 | 可读 |
| `scope=shared`, `contains_private_holdings=true` | 含私有持仓(旧写法) | 不可读 | 可读 |
| `scope=private`, `owner_user_id=<uid>` | 某人私有 | 仅该 uid 可读 | 可读 |
| 其它/损坏 | 未知 | 不可读 | 可读 |

**owner 可见全部**(含历史存量与 member 的私有报告)是**明确的产品裁决**, 不是漏判:

- owner 是这台机器的数据责任人/运维, 他必须能自查"到底泄了什么、要补哪几份";
- 他本来就有 DB 与文件系统权限, 前端再挡一层只会制造"看不见但能读到"的错觉;
- 这条也让 owner 路径**不读 meta**(全量可见), 保住 /list "只读命中 limit 项的
  标题" 的性能护栏(2026-10-11 修过的 30-45s 渲染问题)。

## 读侧第二道闸: 内容绊线(2026-10-29 四审)

meta 是**写报告的人**给的, 可能写错(旧生成器把共享桶成本标成 shared+不含私有; 外部 cron
直写; 手工补文件)。所以 member 在 meta 判定通过后还会过 `content_looks_private_holdings()`
—— 正文列表行里出现 `N股 成本 X` / `成本: X` 就按含私有数据拒绝(fail-closed)。这只是
窄特征绊线(不是解析器), 目的是不把"meta 说公共"当事实; 误伤代价是 owner 复核。

对 **member** 的隔离是硬的: 看不到别人的成本/仓位, 也看不到"存在但你不能看"的
报告(统一 404 / list 不出现)。代价要写明: 历史存量报告一律不出现在 member 的
报告中心(它们由旧写法生成, 内容含任意人的私有持仓), 消除残留只能靠重新生成。

本模块只依赖 `src.db.models`(db 层)+ 标准库, 不 import src.web / FastAPI。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "META_SUFFIX",
    "SCOPE_SHARED",
    "SCOPE_PRIVATE",
    "owner_user_id",
    "report_meta_path",
    "read_report_meta",
    "write_report_meta",
    "report_visible",
    "report_visibility_reason",
    "content_looks_private_holdings",
    "SUSPICIOUS_CONTENT_SCAN_BYTES",
]

META_SUFFIX = ".meta.json"
SCOPE_SHARED = "shared"
SCOPE_PRIVATE = "private"
_KNOWN_SCOPES = (SCOPE_SHARED, SCOPE_PRIVATE)

#: 共享报告二次内容校验的读取上限(只扫头部, 不把性能护栏打回 30-45s)。
SUSPICIOUS_CONTENT_SCAN_BYTES = 64 * 1024

#: 持仓行的窄特征(锚在列表行首): `- 贵州茅台(600519) 500股 成本 1680.5` /
#: `- 成本: 12.34`。刻意不用裸 "成本 12" —— 市场解读里的「成本下降 5%」不是持仓。
#: 这是**绊线**(tripwire)而不是解析器: 宁可让 owner 复核, 不可漏放。
_PRIVATE_HOLDING_PATTERNS = (
    re.compile(r"(?m)^\s*[-*]\s*[^\n]{0,80}\d+\s*股\s*成本"),
    re.compile(r"(?m)^\s*[-*]\s*[^\n]{0,80}成本\s*[:：]?\s*-?\d+(?:\.\d+)?"),
    re.compile(r"(?m)^\s*[-*]\s*[^\n]{0,80}持仓成本\s*[:：]?\s*\d"),
)


def content_looks_private_holdings(text: str) -> bool:
    """正文里是否出现了**具体的持仓成本/股数**。

    读侧的第二道闸: meta 自称 `shared` + `contains_private_holdings=false`, 但正文
    却有「500股 成本 12.34」这类行 —— 说明写 meta 的人判错了/在撒谎(历史生成器、
    外部 cron、手工写盘)。此时不能把「meta 说是公共的」当事实, 按含私有数据拒绝
    member(`fail closed`)。
    """
    if not text:
        return False
    head = text[:SUSPICIOUS_CONTENT_SCAN_BYTES]
    return any(p.search(head) for p in _PRIVATE_HOLDING_PATTERNS)


def owner_user_id(db: Any = None) -> str | None:
    """系统级报告/后台任务的归属人 = 最早的启用 owner。

    与 `src/core/notify_center._resolve_owner_user_id` 同一口径(同一查询), 单独放在
    这里是为了让 `report_generator`/`report_scheduler` 能显式传归属, 而不是"猜"。
    解析失败返回 None —— 调用方必须按 fail-closed 处理(共享桶), 不许默认全量。
    """
    own_db = False
    try:
        if db is None:
            from src.db.session import SessionLocal

            db = SessionLocal()
            own_db = True
        from src.db.models import User

        u = (
            db.query(User)
            .filter(User.role == "owner", User.is_active.is_(True))
            .order_by(User.id.asc())
            .first()
        )
        return str(u.id) if u is not None and u.id else None
    except Exception as e:  # noqa: BLE001 —— 归属解析失败不得炸掉报告生成
        logger.warning("[报告归属] 解析 owner 失败: %s", e)
        return None
    finally:
        if own_db and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def report_meta_path(report_path: Path | str) -> Path:
    """报告文件对应的 meta 路径: `a.md` → `a.md.meta.json`(不复用 suffix, 免得撞名)。"""
    p = Path(report_path)
    return p.with_name(p.name + META_SUFFIX)


def read_report_meta(report_path: Path | str) -> dict | None:
    """读 meta。不存在/读失败/JSON 坏/不是 dict → None(=来源不明, 走 fail-closed)。"""
    mp = report_meta_path(report_path)
    try:
        raw = mp.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("[报告归属] 读 meta 失败 %s: %s", mp, e)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("[报告归属] meta 不是合法 JSON %s: %s", mp, e)
        return None
    return data if isinstance(data, dict) else None


def write_report_meta(
    report_path: Path | str,
    *,
    scope: str,
    owner_user_id: str | None = None,
    generated_by: str = "",
    contains_private_holdings: bool = False,
    extra: dict | None = None,
) -> Path:
    """原子写 meta(先写临时文件再 rename, 避免读到半个 JSON 被判成"来源不明")。

    `scope=private` 必须带 `owner_user_id`, 否则报错 —— 无主的私有报告没人能读,
    等于悄悄丢报告, 不如在生成时报出来。
    """
    if scope not in _KNOWN_SCOPES:
        raise ValueError(f"未知报告 scope: {scope!r}")
    if scope == SCOPE_PRIVATE and not owner_user_id:
        raise ValueError("scope=private 必须传 owner_user_id")
    payload: dict[str, Any] = {
        "schema": 1,
        "scope": scope,
        "owner_user_id": str(owner_user_id) if owner_user_id else None,
        "generated_by": generated_by,
        # 内容层面的自述: 这份文件里有没有"某个具体人的成本/仓位"。
        # 读侧据此二次把关(shared + 含私有数据 = 老写法产物, 不给 member 看)。
        "contains_private_holdings": bool(contains_private_holdings),
    }
    if extra:
        payload.update(extra)
    mp = report_meta_path(report_path)
    tmp = mp.with_name(mp.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        os.replace(tmp, mp)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # noqa: BLE001
            pass
    return mp


def report_visible(
    meta: dict | None, *, user_id: str | None, role: str | None
) -> bool:
    """该报告对 (user_id, role) 是否可读。任何歧义一律不可读。

    owner → 恒可见(数据责任人, 见模块头"明确的产品裁决"); 其它角色严格按 meta 判定。
    """
    if str(role or "").lower() == "owner":
        return True
    if meta is None:
        # 来源不明(历史存量 / 外部 cron 直写): member 一律不可读(fail-closed)。
        return False

    scope = str(meta.get("scope") or "").strip().lower()
    if scope not in _KNOWN_SCOPES:
        return False

    if scope == SCOPE_PRIVATE:
        owner = meta.get("owner_user_id")
        if not owner or not user_id:
            return False
        # 私有报告: 只认归属人本人。
        return str(owner) == str(user_id)

    # scope == shared
    if meta.get("contains_private_holdings"):
        # 旧写法产物: 名义共享、内容含私有持仓 → member 不可读。
        return False
    return bool(user_id)


def report_visibility_reason(
    meta: dict | None, *, user_id: str | None, role: str | None
) -> str:
    """判定原因(日志/测试用; 不回给前端, 免得泄露报告存在性)。"""
    if report_visible(meta, user_id=user_id, role=role):
        return "visible-owner" if str(role or "").lower() == "owner" else "visible"
    if meta is None:
        return "legacy-unscoped"
    scope = str(meta.get("scope") or "").strip().lower()
    if scope not in _KNOWN_SCOPES:
        return "unknown-scope"
    if scope == SCOPE_SHARED:
        return "shared-but-contains-private" if meta.get("contains_private_holdings") else "not-authenticated"
    return "private-foreign-owner"
