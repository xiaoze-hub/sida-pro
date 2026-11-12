"""盘前股性因子凭证的写入/读取 + 「这一期报告能不能算盘前」的唯一判定（2026-11-12 P0 复审）。

存在的理由（真实落库链的三个缺口）：

1. `analysis_history` 同键是 upsert。盘后手工重跑会把当天**盘前**写入的
   `raw_data.demon_factors` / `demon_factors_frozen_at` 一起覆盖掉 —— 正文里还写着
   「股性分 88」，账本却再也证明不了它在盘前被读到过。本模块把凭证写进
   `premarket_factor_evidence`，**只 INSERT、从不 UPDATE/DELETE**，重跑只会多一条。
2. **按日取凭证 = 混配**：同一天可能有「系统调度(NULL 归属)盘前版」+「owner 盘后重跑版」
   +「盘前第二次重跑版」。若只按 `analysis_date` 取「最早一条可验证盘前」的凭证, 就会把
   盘后正文挂到盘前凭证上（把盘后结果记入盘前成绩）, 或把上一版因子挂到新版正文上。
   所以凭证必须绑定**产生它的那次报告**：`run_trace_id` + **完全相同的 user scope**
   （NULL 只配 NULL）, 匹配不上就 unknown —— **绝不按日期回退**。
3. **只有因子采样时点在 09:30 前不算盘前**。整份报告可能在 09:30 后才生成完成
   （LLM 慢/重试/手工补跑）: 采集 08:20 结束、因子 08:20 采样、报告 10:05 才落库,
   这种报告不是盘前报告。故盘前判定要求 **报告完成时刻 + 采集结束时刻都在报告日 09:30 前**,
   因子采样时点另外单独核对（见 `report_preopen_state` / `resolve_report_evidence`）。
   任一时刻缺失/不带时区 → unknown（不拿本机时区猜, 也不拿别的字段兜底）。

**不要把它读成「报告不可变」**：
- 报告正文仍在 `analysis_history`，同一天盘前内的重跑依然会覆盖正文（已记入风险）。
- 本表只保证「每一版被真实采样的因子快照与采样时点都留着」，不保证正文历史版本。
- `snapshot_sha256` 只是一致性校验（检测存储内容被改动/串行），不证明来源、
  不证明采样时点、更不证明数据完整。
"""

from __future__ import annotations

import logging
from typing import Any

from src.core import premarket_ambush as pa
from src.core.timezone import beijing_now

logger = logging.getLogger(__name__)

AGENT_NAME = "premarket_outlook"
TABLE = "premarket_factor_evidence"

# 来源描述：账本要能回答「这快照是从哪儿按什么条件读的」
SOURCE_TOP_N = "demon_factors(每股一行当前快照) SELECT top50 BY total; 过滤 factor_date<=报告日"

# 盘前窗口上界（与 premarket_ambush 同一口径: 报告日 09:30 开盘）
PREOPEN_AT = pa._PREOPEN


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
    """由「带时区采样时点 + 报告日」推导是否盘前；任一不可解析 → None(unknown)。

    注意: 这只是**因子采样时点**的判定, 不是「整份报告算不算盘前」。整份报告还要看
    报告完成/采集结束时刻（见 `report_preopen_state`）。
    """
    dt = pa._parse_dt_aware(generated_at)
    day = pa._norm_day(report_day)
    if dt is None or day is None:
        return None
    return dt.date().isoformat() == day and dt.time() < PREOPEN_AT


def _aware(value: Any, label: str) -> tuple[Any, str | None]:
    """解析带时区时刻；返回 (dt, None) 或 (None, 不可用的原因)。"""
    if value is None or str(value).strip() == "":
        return None, f"缺少{label}, 无法证明盘前生成"
    dt = pa._parse_dt_aware(value)
    if dt is not None:
        return dt, None
    if pa._parse_dt(value) is not None:
        return None, f"{label} {value!r} 不带时区, 无法核对报告日 09:30 边界, 按 unknown 处理"
    return None, f"{label} 无法解析({value!r}), 无法证明盘前生成"


def report_preopen_state(
    *,
    report_completed_at: Any,
    collection_finished_at: Any,
    report_day: Any,
) -> dict:
    """「整份报告是否在报告日 09:30 前生成完成」的唯一判定。

    门槛（缺一不可）:
    1. `report_completed_at`（整份报告落库完成时刻）—— 带时区、落在报告日 09:30 前;
    2. `collection_finished_at`（`raw_data["timestamp"]`，数据采集结束时刻）—— 同上;
    3. **时序自洽**: `report_completed_at >= collection_finished_at`。

    判定顺序很重要:
    - 先看"有没有哪个时刻**可证明**不早于 09:30 / 不在报告日" → 有就是 `preopen=False`
      （明确非盘前; 这时即使时序也不可能, 结论仍然是"不是盘前", 不含糊）;
    - 两个时刻都"看起来在 09:30 前"、却 `report_completed_at < collection_finished_at`
      → `preopen=None`(unknown): 报告不可能在采集结束前生成完成, 至少一个是事后补写的,
      证据链断了 —— 这时**绝不能**因为"两个都早于 09:30"就放行成盘前;
    - 任一缺失/不带时区/不可解析 → `preopen=None`（unknown, 不猜、不按日期回退）。

    返回 `{preopen: bool|None, reason: str|None, report_completed_at, collection_finished_at}`
    （后两个是规范化后的带时区 ISO 文本或 None, 供页面展示真实时刻）。
    """
    out: dict = {
        "preopen": None,
        "reason": None,
        "report_completed_at": None,
        "collection_finished_at": None,
    }
    day = pa._norm_day(report_day)
    if day is None:
        out["reason"] = f"报告日无法解析({report_day!r}), 无法确认盘前时点"
        return out

    pairs = (
        ("report_completed_at", "整份报告生成完成时刻", report_completed_at),
        ("collection_finished_at", "数据采集结束时刻", collection_finished_at),
    )
    parsed: list[tuple[str, Any]] = []
    for key, label, value in pairs:
        dt, why = _aware(value, label)
        if dt is None:
            # 缺字段/不可核验: 整个判定只能是 unknown —— 不拿另一个时间兜底
            out["reason"] = why
            return out
        out[key] = dt.isoformat()
        parsed.append((label, dt))

    late = [
        (label, dt)
        for label, dt in parsed
        if dt.date().isoformat() != day or dt.time() >= PREOPEN_AT
    ]
    if late:
        # 可证明非盘前: 这里的结论不依赖两个时刻的先后, 所以时序异常也不改判
        out["preopen"] = False
        out["reason"] = "；".join(
            f"{label} {dt.isoformat()} 不在报告日 {day} 09:30 前" for label, dt in late
        )
        return out

    completed_label, completed_dt = parsed[0]
    collected_label, collected_dt = parsed[1]
    if completed_dt < collected_dt:
        # 两个时刻都"看着盘前", 但完成时刻早于采集结束 → 不可能, 只能是补写的时刻:
        # 证据链断裂 → unknown, 不按"都在 09:30 前"放行。
        out["preopen"] = None
        out["reason"] = (
            f"{completed_label} {completed_dt.isoformat()} 早于{collected_label} "
            f"{collected_dt.isoformat()} —— 时序不可能, 无法证明盘前生成"
        )
        return out

    out["preopen"] = True
    return out


def build_snapshot_evidence(
    *,
    snapshot: dict | None,
    frozen_at: Any,
    report_day: str,
    dropped: list[str] | None = None,
    asof: str | None = None,
    source: str | None = None,
    run_trace_id: str | None = None,
) -> dict | None:
    """生成侧构造待落库凭证；采样时点不可核验 → None（不落假凭证）。

    只有真实完成采样（拿到快照）并且采样时点带时区时才返回 dict。返回里的
    `preopen` 由时点推导，不接受调用方自称；`snapshot_sha256` 仅一致性校验。
    `run_trace_id` 是这份凭证与**具体某一版报告**的绑定键 —— 缺了它凭证不参与对账
    （读者不会按日期去猜它是哪一版的）, 但仍会留档说明「这次采样确实发生过」。
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
    trace = str(run_trace_id).strip() if run_trace_id is not None else ""
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
        "run_trace_id": trace or None,
        "snapshot_sha256": pa.factor_snapshot_fingerprint(snapshot),
        "note": (
            "生成时冻结的股性因子快照; 快照日只说明上次计算日, 不证明数据已截至报告日前一交易日"
            + ("" if trace else "; 未绑定报告 run_trace_id, 不可用于对账")
        ),
    }


def append_evidence(db, evidence: dict, *, user_id: str | None = None) -> bool:
    """追加一条凭证（只 INSERT）。失败返回 False，由调用方决定是否算整体保存失败。

    `preopen` 在这里由 `generated_at` + `analysis_date` **重新推导**，不采信调用方传入的
    布尔值 —— 否则一个手滑的 True 就能把收盘后采样标成盘前。推导不出来(时点/日期不可解析)
    直接拒绝写入，不落半真半假的凭证。

    幂等: 同一 (user scope, 报告日, run_trace_id, 采样时点) 已存在时直接返回 True,
    避免重试把同一次采样写成两行（重复行会让对账条数看起来更多, 是假证据）。
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
        trace = evidence.get("run_trace_id")
        trace = str(trace).strip() if trace is not None else ""
        trace = trace or None
        if trace:
            dup = (
                db.query(PremarketFactorEvidence.id)
                .filter(
                    PremarketFactorEvidence.agent_name == (evidence.get("agent_name") or AGENT_NAME),
                    PremarketFactorEvidence.analysis_date == analysis_date,
                    PremarketFactorEvidence.run_trace_id == trace,
                    PremarketFactorEvidence.user_id.is_(None)
                    if user_id is None
                    else PremarketFactorEvidence.user_id == user_id,
                )
                .first()
            )
            if dup is not None:
                logger.info(
                    "盘前因子凭证已存在(同一 run_trace_id), 跳过重复写入: date=%s run=%s",
                    analysis_date,
                    trace,
                )
                return True
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
                run_trace_id=trace,
                note=str(evidence.get("note") or ""),
            )
        )
        db.flush()
        return True
    except Exception as e:  # noqa: BLE001 — 由调用方回滚并报告
        logger.error("盘前因子凭证写入失败: %s", e)
        return False


def _fetch_rows(db, *, analysis_date: str, user_id: str | None, agent_name: str) -> list:
    """取某报告日 + **完全相同 user scope** 的全部凭证行（不做 NULL 通配）。"""
    from src.db.models import PremarketFactorEvidence

    q = db.query(PremarketFactorEvidence).filter(
        PremarketFactorEvidence.agent_name == agent_name,
        PremarketFactorEvidence.analysis_date == analysis_date,
    )
    if user_id is None:
        q = q.filter(PremarketFactorEvidence.user_id.is_(None))
    else:
        q = q.filter(PremarketFactorEvidence.user_id == user_id)
    return (
        q.order_by(
            PremarketFactorEvidence.generated_at.asc(),
            PremarketFactorEvidence.id.asc(),
        )
        .all()
    )


def _pick_row(rows: list, run_trace_id: str | None, report_day: str | None = None):
    """在**同一 run_trace_id** 的行里选一条: 最早一条可验证盘前 > 最新一条。

    同一 run 正常只有一条（`append_evidence` 已做幂等）; 真出现多条时选「最早一条能验证
    盘前的」= 那次运行第一次真实采样, 与旧口径一致; 都不满足则取最新一条仅用于展示。
    """
    if not run_trace_id:
        return None, []
    matched = [
        r
        for r in rows
        if str(getattr(r, "run_trace_id", "") or "").strip() == run_trace_id
    ]
    if not matched:
        return None, []
    for r in matched:
        if report_day and preopen_flag(r.generated_at, report_day) and bool(r.preopen):
            return r, matched
    return matched[-1], matched


def resolve_report_evidence(
    db,
    record: Any,
    *,
    agent_name: str = AGENT_NAME,
) -> dict:
    """把「某一版盘前报告」和它自己的因子凭证对上, 并判定这一期是否盘前。

    绑定规则（这是 P0 的核心）:
    - 只接受 `run_trace_id` **与这一版报告完全相同**、且 `user_id` **与这一版报告完全相同**
      （NULL 只配 NULL）的凭证行; 同一天其他版本的凭证一律**不作数**（不按日期回退）。
    - 凭证行缺失时回落到**同一行** `raw_data` 里生成时冻结的快照（那是这一版自己写的）,
      仍然走同一套时点门禁; 但 `raw_data` 里没有 `run_trace_id` 时不算「绑定成功」。
    - 盘前判定 = 报告完成时刻 + 采集结束时刻都在报告日 09:30 前（`report_preopen_state`）,
      因子采样时点另外用 `demon_evidence_verified` 核对; 任一时点缺失 → unknown。
    - `raw_data["premarket_report_preopen"]`（落库时算的同一个门槛）若与重算结果冲突 →
      按「凭证自相矛盾」判 unknown, 不让任何一方单方面把期次标成盘前。

    返回字段: `verified` / `preopen` / `generated_at` / `report_completed_at` /
    `collection_finished_at` / `reason` / `kept` / `notes` / `dropped` / `asof` / `source` /
    `evidence_rows` / `matched_rows` / `run_trace_id` / `scope` / `binding` / `gate`。
    """
    raw = getattr(record, "raw_data", None)
    raw = raw if isinstance(raw, dict) else {}
    day = pa._norm_day(getattr(record, "analysis_date", None))
    scope_uid = getattr(record, "user_id", None)
    trace = str(raw.get("run_trace_id") or "").strip() or None

    view: dict = {
        "verified": False,
        "preopen": None,
        "generated_at": None,
        "report_completed_at": None,
        "collection_finished_at": None,
        "reason": None,
        "kept": {},
        "notes": {},
        "dropped": [],
        "asof": None,
        "source": None,
        "evidence_rows": 0,
        "matched_rows": 0,
        "run_trace_id": trace,
        "scope": "system" if scope_uid is None else "user",
        "binding": {
            "run_trace_id": trace,
            "scope": "system" if scope_uid is None else "user",
            "matched": 0,
            "source_kind": None,
        },
        "gate": {"preopen": None, "reason": None},
    }
    if day is None:
        view["reason"] = "报告日无法解析, 无法确认盘前时点"
        view["gate"] = {"preopen": None, "reason": view["reason"]}
        return view

    rows: list = []
    if db is not None:
        try:
            rows = _fetch_rows(db, analysis_date=day, user_id=scope_uid, agent_name=agent_name)
        except Exception as e:  # noqa: BLE001 — 表缺失(未迁移) → 按无凭证处理(unknown)
            logger.warning("盘前因子凭证查询失败(按无凭证处理): %s", e)
            rows = []
    view["evidence_rows"] = len(rows)

    row, matched = _pick_row(rows, trace, day)
    view["matched_rows"] = len(matched)

    if row is not None:
        view["binding"]["matched"] = len(matched)
        view["binding"]["source_kind"] = "appended"
        factor = pa.demon_evidence_verified(
            row.snapshot,
            row.generated_at,
            day,
            source=row.source,
            declared_preopen=bool(row.preopen),
            fingerprint=row.snapshot_sha256,
        )
        view["source"] = row.source
    else:
        factor = pa.demon_evidence_from_record(raw, day)
        view["source"] = raw.get("demon_factors_source")
        view["binding"]["source_kind"] = "raw_data" if factor.get("frozen_at") else None
        if trace is None:
            view["binding"]["reason"] = (
                "该版报告没有 run_trace_id(旧记录), 无法与追加式凭证绑定; "
                "只认它自己 raw_data 里的冻结快照"
            )
        elif rows:
            view["binding"]["reason"] = (
                f"同日有 {len(rows)} 条凭证, 但没有一条属于这一版报告"
                f"(run_trace_id={trace}), 按 unknown 处理, 不按日期回退"
            )
        else:
            view["binding"]["reason"] = (
                f"同一天同一归属下没有这一版报告的凭证(run_trace_id={trace}); "
                "其他归属/其他版本的凭证一律不作数, 只认它自己 raw_data 的冻结快照"
            )

    gate = report_preopen_state(
        report_completed_at=raw.get("report_completed_at"),
        collection_finished_at=raw.get("timestamp"),
        report_day=day,
    )
    stored = raw.get("premarket_report_preopen")
    if isinstance(stored, bool) and gate["preopen"] is not None and stored is not gate["preopen"]:
        gate = {
            "preopen": None,
            "reason": (
                f"凭证自相矛盾: 落库时标记 premarket_report_preopen={stored}, "
                f"但按时刻重算为 {gate['preopen']}, 一律按 unknown"
            ),
            "report_completed_at": gate["report_completed_at"],
            "collection_finished_at": gate["collection_finished_at"],
        }

    view["gate"] = gate
    view["preopen"] = gate["preopen"]
    view["report_completed_at"] = gate.get("report_completed_at")
    view["collection_finished_at"] = gate.get("collection_finished_at")
    # 采样时点: 无论是否 verified 都如实显示真实采样时刻(不可验证时由 gate/reason 说明)
    view["generated_at"] = factor.get("frozen_at") or (
        normalize_generated_at(row.generated_at) if row is not None else None
    )

    if gate["preopen"] is not True:
        # 整份报告不是盘前(或时点不可证明) → 因子快照也不能当盘前凭证
        view["reason"] = gate["reason"] or factor.get("reason")
        return view

    view["verified"] = bool(factor.get("verified"))
    view["kept"] = factor.get("kept") or {}
    view["notes"] = factor.get("notes") or {}
    view["dropped"] = factor.get("dropped") or []
    view["asof"] = factor.get("asof")
    if not view["verified"]:
        view["reason"] = factor.get("reason")
        if view["binding"].get("reason"):
            view["reason"] = f"{view['reason']}；{view['binding']['reason']}"
    return view
