"""邀请码注册（2026-09-19 用户拍板：内部使用 + 邀请制）。

## 为什么需要
产品判定为**内部使用**。合规上真正的红线是"**面向公众**"——公开自助注册的口子开着，
形式上就是面向公众提供分析建议。邀请码把"谁能进来"收回到管理员手里。

## 设计要点
1. **并发不超发**：核销用**单条条件 UPDATE**（`used_count < max_uses` 写进 WHERE），
   受影响行数=0 即拒绝。绝不"先查后改"——那样两个并发请求都能通过检查。
2. **失败原因分得清**：不存在 / 已作废 / 已用尽 / 已过期 各自给文案，不一律说"邀请码无效"。
3. **审计必落库**：每次核销写 `invite_code_uses`（谁、什么时候、什么 IP），
   并要求调用方再落一条 `log_audit`——邀请码是准入凭证，必须可回溯。
4. **日志不留明文**：日志/审计里只出现**后 4 位**，避免邀请码从日志泄露。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 去掉易混字符（0/O、1/I/L），避免用户手抄错
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8


def _utcnow() -> datetime:
    """naive UTC（与库里列的类型一致）。`datetime.utcnow()` 在 3.12 已废弃。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value) -> datetime | None:
    """把库里的时间字段解析成 naive datetime。

    **为什么必须有这个函数**：SQLite 把 DATETIME 读成**字符串**、PG 读成 datetime。
    v0.10.51 之前这里只判 `isinstance(exp, datetime)`，于是在 SQLite 上"过期"判断被
    静默跳过 —— 过期邀请码照样能注册（测试当场抓到）。准入判据不能有这种静默洞。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        txt = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt.replace(" ", "T"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _is_expired(value) -> bool:
    dt = _parse_dt(value)
    return bool(dt is not None and dt < _utcnow())


class InviteCodeError(Exception):
    """核销失败。`reason` 给前端用于区分文案。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def mask_code(code: str) -> str:
    """日志/审计用：只留后 4 位。"""
    c = (code or "").strip().upper()
    return f"****{c[-4:]}" if len(c) > 4 else "****"


def generate_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))


def normalize_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "").replace("-", "")


def create_invite_code(
    db: Session,
    *,
    created_by: str = "",
    note: str = "",
    max_uses: int = 1,
    expires_in_days: int | None = None,
    code: str | None = None,
) -> dict:
    """生成邀请码（重复概率极低，仍重试保证唯一）。"""
    max_uses = max(1, min(int(max_uses or 1), 1000))
    expires_at = None
    if expires_in_days:
        expires_at = _utcnow() + timedelta(days=int(expires_in_days))

    for _ in range(8):
        c = normalize_code(code) if code else generate_code()
        exists = db.execute(
            text("SELECT 1 FROM invite_codes WHERE code = :c"), {"c": c}
        ).first()
        if exists and code:
            raise InviteCodeError("duplicate", "该邀请码已存在")
        if exists:
            continue
        db.execute(
            text(
                """
                INSERT INTO invite_codes
                  (code, note, max_uses, used_count, expires_at, disabled, created_by, created_at)
                VALUES (:c, :n, :m, 0, :e, :d, :b, :t)
                """
            ),
            {
                "c": c,
                "n": note or "",
                "m": max_uses,
                "e": expires_at,
                "d": False,
                "b": created_by or "",
                "t": _utcnow(),
            },
        )
        db.commit()
        logger.info("[invite] 生成邀请码 %s (可用 %d 次, 备注=%s)", mask_code(c), max_uses, note or "-")
        return {
            "code": c,
            "note": note or "",
            "max_uses": max_uses,
            "used_count": 0,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "disabled": False,
        }
    raise InviteCodeError("duplicate", "生成失败（连续碰撞），请重试")


def list_invite_codes(db: Session, limit: int = 100) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT code, note, max_uses, used_count, expires_at, disabled, created_by, created_at
            FROM invite_codes ORDER BY created_at DESC LIMIT :l
            """
        ),
        {"l": max(1, min(int(limit), 500))},
    ).mappings().all()
    out = []
    for r in rows:
        exp = r["expires_at"]
        out.append(
            {
                "code": r["code"],
                "note": r["note"] or "",
                "max_uses": r["max_uses"],
                "used_count": r["used_count"],
                "remaining": max(0, int(r["max_uses"]) - int(r["used_count"])),
                "expires_at": exp.isoformat() if hasattr(exp, "isoformat") else (str(exp) if exp else None),
                "disabled": bool(r["disabled"]),
                "created_by": r["created_by"] or "",
                "created_at": r["created_at"].isoformat()
                if hasattr(r["created_at"], "isoformat")
                else str(r["created_at"]),
            }
        )
    return out


def disable_invite_code(db: Session, code: str) -> bool:
    res = db.execute(
        text("UPDATE invite_codes SET disabled = :d WHERE code = :c AND disabled = :f"),
        {"d": True, "c": normalize_code(code), "f": False},
    )
    db.commit()
    return bool(res.rowcount)


def _record_use(db: Session, code: str, user_id: int | None, username: str, client_ip: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO invite_code_uses (code, user_id, username, client_ip, used_at)
            VALUES (:c, :u, :n, :i, :t)
            """
        ),
        {"c": code, "u": user_id, "n": username or "", "i": client_ip or "", "t": _utcnow()},
    )


def redeem(db: Session, code: str, *, user_id: int | None = None, username: str = "", client_ip: str = "") -> dict:
    """核销一次邀请码。失败抛 `InviteCodeError`（reason 可区分）。

    先做**条件 UPDATE**（原子），再落审计 —— 顺序不能反：审计写成功但核销失败会虚增用量。
    """
    c = normalize_code(code)
    if not c:
        raise InviteCodeError("missing", "请填写邀请码")

    row = db.execute(
        text("SELECT code, max_uses, used_count, expires_at, disabled FROM invite_codes WHERE code = :c"),
        {"c": c},
    ).mappings().first()
    if not row:
        raise InviteCodeError("not_found", "邀请码无效")
    if row["disabled"]:
        raise InviteCodeError("disabled", "邀请码已被停用")
    if int(row["used_count"]) >= int(row["max_uses"]):
        raise InviteCodeError("exhausted", "邀请码已用尽")
    if _is_expired(row["expires_at"]):
        raise InviteCodeError("expired", "邀请码已过期")

    # 原子核销：把剩余次数条件写进 WHERE，受影响行数=0 → 并发下已被别人抢走
    res = db.execute(
        text(
            """
            UPDATE invite_codes SET used_count = used_count + 1
            WHERE code = :c AND disabled = :f AND used_count < max_uses
            """
        ),
        {"c": c, "f": False},
    )
    if not res.rowcount:
        db.rollback()
        # 可能是并发抢走 / 恰好过期 / 被停用 —— 重查一次给出准确原因
        again = db.execute(
            text("SELECT max_uses, used_count, disabled FROM invite_codes WHERE code = :c"), {"c": c}
        ).mappings().first()
        if again and int(again["used_count"]) >= int(again["max_uses"]):
            raise InviteCodeError("exhausted", "邀请码已用尽")
        if again and again["disabled"]:
            raise InviteCodeError("disabled", "邀请码已被停用")
        raise InviteCodeError("expired", "邀请码已过期")

    _record_use(db, c, user_id, username, client_ip)
    db.commit()
    logger.info("[invite] 已核销 %s (user=%s ip=%s)", mask_code(c), username or user_id, client_ip or "-")
    return {"code": c, "user_id": user_id, "username": username}


def list_uses(db: Session, code: str | None = None, limit: int = 100) -> list[dict]:
    """审计查询：某码（或全部）的使用记录。"""
    sql = "SELECT code, user_id, username, client_ip, used_at FROM invite_code_uses"
    params: dict = {"l": max(1, min(int(limit), 500))}
    if code:
        sql += " WHERE code = :c"
        params["c"] = normalize_code(code)
    sql += " ORDER BY used_at DESC LIMIT :l"
    rows = db.execute(text(sql), params).mappings().all()
    return [
        {
            "code": r["code"],
            "user_id": r["user_id"],
            "username": r["username"] or "",
            "client_ip": r["client_ip"] or "",
            "used_at": r["used_at"].isoformat() if hasattr(r["used_at"], "isoformat") else str(r["used_at"]),
        }
        for r in rows
    ]

# ── 注册模式 ──────────────────────────────────────────────────────────────
#: invite(默认, 内部使用) / open(等同历史行为, 仅自测用) / closed(一律拒绝)
VALID_MODES = ("invite", "open", "closed")
DEFAULT_MODE = "invite"


def get_register_mode(db: Session) -> str:
    """注册模式。优先级: app_settings.register_mode(DB) > env REGISTER_MODE > 默认 invite。

    沿用本项目"DB 覆盖 env"的惯例(同花顺凭证那套): 管理员在设置里改立即生效, 不用重新部署;
    env 只作为首次部署的种子值。
    """
    import os

    raw = ""
    try:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key = :k"), {"k": "register_mode"}
        ).first()
        raw = str(row[0]).strip().lower() if row and row[0] is not None else ""
    except Exception:  # 表缺失/字段异常都不该让注册流程 500
        raw = ""
    if not raw:
        raw = (os.getenv("REGISTER_MODE") or "").strip().lower()
    return raw if raw in VALID_MODES else DEFAULT_MODE


def check_redeemable(db: Session, code: str) -> None:
    """**不消费**的预检: 明显无效的码要在烧掉邮箱验证码之前就拒掉。

    注意这只是预检 —— 真正保证"不超发"的是 `redeem()` 里的条件 UPDATE。
    """
    c = normalize_code(code)
    if not c:
        raise InviteCodeError("missing", "请填写邀请码")
    row = db.execute(
        text("SELECT max_uses, used_count, expires_at, disabled FROM invite_codes WHERE code = :c"),
        {"c": c},
    ).mappings().first()
    if not row:
        raise InviteCodeError("not_found", "邀请码无效")
    if row["disabled"]:
        raise InviteCodeError("disabled", "邀请码已被停用")
    if int(row["used_count"]) >= int(row["max_uses"]):
        raise InviteCodeError("exhausted", "邀请码已用尽")
    if _is_expired(row["expires_at"]):
        raise InviteCodeError("expired", "邀请码已过期")

