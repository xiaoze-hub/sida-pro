"""盘前股性因子凭证的写入/读取（追加式，2026-11-12 复审修正）。

存在的理由（真实落库链的两个缺口）：

1. `analysis_history` 同键是 upsert。盘后手工重跑会把当天**盘前**写入的
   `raw_data.demon_factors` / `demon_factors_frozen_at` 一起覆盖掉 —— 正文里还写着
   「股性分 88」，账本却再也证明不了它在盘前被读到过。本模块把凭证写进
   `premarket_factor_evidence`，**只 INSERT、从不 UPDATE/DELETE**，重跑只会多一条。
2. 采样时点必须带时区、且必须是**真实完成采样**时才记。检测到 `datetime.now()` 之类
   naive 时间一律不落凭证（宁缺勿假）：naive 08:20 与 16:20 在账本上无法区分。

**不要把它读成「报告不可变」**：
- 报告正文仍在 `analysis_history`，同一天盘前内的重跑依然会覆盖正文（已记入风险）。
- 本表只保证「每一版被真实采样的因子快照与采样时点都留着」，不保证正文历史版本。
- `snapshot_sha256` 只是一致性校验（检测存储内容被改动/串行），不证明来源、
  不证明采样时点、更不证明数据完整。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import or_

from src.core import premarket_ambush as pa
from src.core.timezone import beijing_now

logger = logging.getLogger(__name__)

AGENT_NAME = "premarket_outlook"
TABLE = "premarket_factor_evidence"

# 来源描述：账本要能回答「这快照是从哪儿按什么条件读的」
SOURCE_TOP_N = "demon_factors(每股一行当前快照) SELECT top50 BY total; 过滤 factor_date<=报告日"


def now_iso() -> str:
    """真实采样完成时刻（带时区）。"""
    return beijing_now().isoformat(timespec="seconds")


def normalize_generated_at(value: Any) -> str | None:
    """把带时区的采样时点规范成北京时区 ISO8601；naive/非法 → None（不猜时区）。"""
    dt = pa._parse_dt_aware(value)
    if dt is None:
        return None
    return dt.isoformat(timespec="seconds")


def preopen_flag(generated_at: Any, report_day: str) -> bool | None:
    """由「带时区采样时点 + 报告日」推导是否盘前；任一不可解析 → None(unknown)。"""
    dt = pa._parse_dt_aware(generated_at)
    day = pa._norm_day(report_day)
    if dt is None or day is None:
        return None
    return dt.date().isoformat() == day and dt.time() < pa._PREOPEN


def build_snapshot_evidence(
    *,
    snapshot: dict | None,
    frozen_at: Any,
    report_day: str,
    dropped: list[str] | None = None,
    asof: str | None = None,
    source: str | None = None,
) -> dict | None:
    """生成侧构造待落库凭证；采样时点不可核验 → None（不落假凭证）。

    只有真实完成采样（拿到快照）并且采样时点带时区时才返回 dict。返回里的
    `preopen` 由时点推导，不接受调用方自称；`snapshot_sha256` 仅一致性校验。
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    generated_at = normalize_generated_at(frozen_at)
    if generated_at is None:
        logger.warning("盘前凭证未落库: 采样时点不带时区或无法解析(%r)", frozen_at)
        return None
    preopen = preopen_flag(generated_at, report_day)
    if preopen is None:
        logger.warning("盘前凭证未落库: 报告日无法解析(%r)", report_day)
        return None
    return {
        "agent_name": AGENT_NAME,
        "stock_symbol": "*",
        "analysis_date": pa._norm_day(report_day),
        "generated_at": generated_at,
        "preopen": preopen,
        "source": source or SOURCE_TOP_N,
        "snapshot": snapshot,
        "dropped": list(dropped or []),
        "asof": asof,
        "snapshot_sha256": pa.factor_snapshot_fingerprint(snapshot),
        "note": (
            "生成时冻结的股性因子快照; 快照日只说明上次计算日, 不证明数据已截至报告日前一交易日"
        ),
    }


def append_evidence(db, evidence: dict, *, user_id: str | None = None) -> bool:
    """追加一条凭证（只 INSERT）。失败返回 False，由调用方决定是否算整体保存失败。

    `preopen` 在这里由 `generated_at` + `analysis_date` **重新推导**，不采信调用方传入的
    布尔值 —— 否则一个手滑的 True 就能把收盘后采样标成盘前。推导不出来(时点/日期不可解析)
    直接拒绝写入，不落半真半假的凭证。
    """
    from src.core.json_safe import to_jsonable
    from src.db.models import PremarketFactorEvidence

    if not isinstance(evidence, dict):
        return False
    try:
        analysis_date = str(evidence["analysis_date"])
        generated_at = normalize_generated_at(evidence["generated_at"])
        preopen = preopen_flag(generated_at, analysis_date)
        if generated_at is None or preopen is None:
            logger.error(
                "盘前因子凭证拒绝写入: 采样时点/报告日不可核验(generated_at=%r, date=%r)",
                evidence.get("generated_at"),
                evidence.get("analysis_date"),
            )
            return False
        db.add(
            PremarketFactorEvidence(
                user_id=user_id,
                agent_name=evidence.get("agent_name") or AGENT_NAME,
                stock_symbol=evidence.get("stock_symbol") or "*",
                analysis_date=analysis_date,
                generated_at=generated_at,
                preopen=bool(preopen),
                source=str(evidence.get("source") or ""),
                snapshot=to_jsonable(evidence.get("snapshot") or {}),
                snapshot_sha256=evidence.get("snapshot_sha256"),
                note=str(evidence.get("note") or ""),
            )
        )
        db.flush()
        return True
    except Exception as e:  # noqa: BLE001 — 由调用方回滚并报告
        logger.error("盘前因子凭证写入失败: %s", e)
        return False


def _scope_clause(user_id: str | None, role: str | None):
    from src.db.models import PremarketFactorEvidence

    uid = str(user_id) if user_id else ""
    if not uid:
        return None
    if str(role or "").strip().lower() == "owner":
        return or_(
            PremarketFactorEvidence.user_id == uid,
            PremarketFactorEvidence.user_id.is_(None),
        )
    return PremarketFactorEvidence.user_id == uid


def load_evidence_map(
    db,
    dates: list[str],
    *,
    user_id: str | None,
    role: str | None = None,
    agent_name: str = AGENT_NAME,
) -> dict[str, dict]:
    """按报告日取凭证并核对，返回 `{date: view}`（无凭证的日期不出现在结果里）。

    `view` 字段：`row` / `verified` / `preopen` / `generated_at` / `reason` /
    `kept` / `notes` / `dropped` / `asof` / `source`。

    选取规则：优先「最早一条能验证为盘前」的凭证（那是当天第一版真实盘前采样）；
    没有任何可验证盘前凭证时，取最新一条仅用于展示真实生成时刻/非盘前标志 ——
    这条**不能**当盘前成绩（verified=False）。
    """
    from src.db.models import PremarketFactorEvidence

    wanted = [d for d in (pa._norm_day(x) for x in dates or []) if d]
    scope = _scope_clause(user_id, role)
    if not wanted or scope is None:
        return {}
    try:
        rows = (
            db.query(PremarketFactorEvidence)
            .filter(
                PremarketFactorEvidence.agent_name == agent_name,
                PremarketFactorEvidence.analysis_date.in_(wanted),
                scope,
            )
            .order_by(
                PremarketFactorEvidence.analysis_date.asc(),
                PremarketFactorEvidence.generated_at.asc(),
                PremarketFactorEvidence.id.asc(),
            )
            .all()
        )
    except Exception as e:  # noqa: BLE001 — 表缺失(未迁移) → 全部 unknown
        logger.warning("盘前因子凭证查询失败(按无凭证处理): %s", e)
        return {}

    by_date: dict[str, list] = {}
    for row in rows:
        by_date.setdefault(str(row.analysis_date), []).append(row)

    out: dict[str, dict] = {}
    for day, items in by_date.items():
        verified_preopen: list[tuple[Any, dict]] = []
        latest: tuple[Any, dict] | None = None
        for row in items:
            result = pa.demon_evidence_verified(
                row.snapshot,
                row.generated_at,
                day,
                source=row.source,
                declared_preopen=bool(row.preopen),
                fingerprint=row.snapshot_sha256,
            )
            if result.get("verified") and result.get("preopen"):
                verified_preopen.append((row, result))
            latest = (row, result)
        if verified_preopen:
            row, result = verified_preopen[0]
        elif latest is not None:
            row, result = latest
        else:  # pragma: no cover — items 非空时不会走到
            continue
        out[day] = {
            "row": row,
            "verified": bool(result.get("verified")),
            "preopen": bool(result.get("preopen")),
            "generated_at": result.get("frozen_at") or normalize_generated_at(row.generated_at),
            "reason": result.get("reason"),
            "kept": result.get("kept") or {},
            "notes": result.get("notes") or {},
            "dropped": result.get("dropped") or [],
            "asof": result.get("asof"),
            "source": row.source,
            "evidence_rows": len(items),
        }
    return out
