import base64
import logging
import os
import uuid
from dataclasses import dataclass
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import AppSettings, AuditLog, User
from src.config import Settings
from src.core.update_checker import check_update
from src.db.session import WriteSessionLocal
from src.web.api.auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


def get_app_version() -> str:
    """获取应用版本号"""
    # 优先从环境变量读取
    version = os.getenv("APP_VERSION")
    if version:
        return version

    # 从 VERSION 文件读取（支持多个位置）
    possible_paths = [
        "VERSION",  # 当前工作目录（开发和生产）
        os.path.join(os.path.dirname(__file__), "../../../VERSION"),  # 相对于本文件
    ]
    for path in possible_paths:
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "dev"


class SettingUpdate(BaseModel):
    # value 仍为必填: /settings/avatar 也复用本模型, 收空 body 就等于清头像,
    # 不能让"少传字段"变成数据删除。
    value: str
    # 2026-10-23 P0(走查 W-3): 敏感键的"清除"必须显式声明 —— 裸空值一律 400,
    # 防止前端把未填写误当清空, 把已配置的 token 抹掉。
    clear: bool = False


class SettingResponse(BaseModel):
    key: str
    value: str
    description: str

    class Config:
        from_attributes = True


# 配置项描述
SETTING_DESCRIPTIONS = {
    "http_proxy": "HTTP 代理地址(配置后所有对外请求含行情/新闻/AI/通知统一走此代理)",
    # 2026-11-07(复审): 空值不是"关闭" —— runtime._build_notifier 是
    # `_get_app_setting(...) or settings.notify_quiet_hours`, 空值会回落到环境默认。
    "notify_quiet_hours": "通知静默时间段（HH:MM-HH:MM，留空继承环境默认 NOTIFY_QUIET_HOURS）",
    "notify_retry_attempts": "通知失败重试次数（不含首次）",
    "notify_retry_backoff_seconds": "通知重试退避秒数（基数）",
    "notify_dedupe_ttl_overrides": "通知幂等窗口覆盖（JSON，空为默认）",
    "stock_link_platform": "股票链接平台（点击股票代码跳转的行情网站）",
    "sida_base_url": "SIDA 公开访问地址（用于通知里的分析详情页链接，如 https://sida.example.com）",
    # ---- 接口 Key(数据源凭证,设置页维护,DB 优先于环境变量) ----
    "wudao_mcp_token": "悟道(wudao) MCP 接口 Token(竞价/题材数据源)",
    "zhitu_token": "智兔(zhitu) 数据接口 Token(分红/股东数据源,200次/天)",
    "tdx_api_key": "通达信问小达 MCP 接口 Token(TDX 自然语言投研/选股数据源)",
    # ---- 同花顺 SDK 凭证(设置页维护, DB 优先于环境变量, 改完即时生效) ----
    "ths_username": "同花顺SDK账号(正式账户,留空则读容器 env THS_USERNAME)",
    "ths_sdk_password": "同花顺SDK密码(留空则读容器 env THS_PASSWORD)",
    # ---- 同花顺扫码凭证(2026-09-05 已下线, 遗留键仅兼容旧 DB 行, 无写入方) ----
    "ths_account": "同花顺账号(遗留键,扫码已下线,勿手改)",
    "ths_password": "同花顺登录凭证(遗留键,扫码已下线)",
    "ths_userid": "同花顺用户ID(遗留键,扫码已下线)",
    "ths_expires": "同花顺 session 过期时间(遗留键,扫码已下线)",
}

# 敏感 key:列表接口不回显完整值,只返回是否已配置
# 安全审计 2026-09-15: ths_username 为账号标识, 一并掩码
# P0(走查 2026-10-23 W-2): http_proxy 移出敏感集合 —— 代理地址不是凭证, 掩码它只会让
# 系统设置区预填 "********", 用户改不动也存不进(掩码拒写), 是个死路; 代理若含
# userinfo(user:pass@)请在部署侧 env / 网关维护, 不放设置页。
SECRET_SETTING_KEYS = {
    "wudao_mcp_token",
    "zhitu_token",
    "tdx_api_key",
    "ths_password",
    "ths_sdk_password",
    "ths_username",
}
SECRET_MASK = "********"
# 掩码占位可能出现过的字符(半角/全角星号、中点、圆点)
SECRET_MASK_CHARS = frozenset("*＊•·")


def is_mask_placeholder(value) -> bool:
    """判定一个值是否只是掩码占位(而非真凭证)。前后端共用同一判据。

    P0(走查 2026-10-23 W-3): 旧判据是 ``(value or "") == SECRET_MASK`` 精确相等,
    在掩码后多敲一个空格(" ******** ")、或把星号敲成 "·", 就能绕过拦截,
    结果是**字面掩码被写进 DB 覆盖真 token**: 之后 zhitu/wudao 静默失效,
    而设置页仍显示"已配置"(它判的就是 value == 掩码), 故障极难归因。

    现按**形状**判定: strip 掉两侧空白后, 长度 ≥6 且只由掩码字符构成。
    """
    if value is None:
        return False
    v = str(value).strip()
    if len(v) < 6:
        return False
    return all(ch in SECRET_MASK_CHARS for ch in v)


SETTING_KEYS = list(SETTING_DESCRIPTIONS.keys())


# ════════════════════════════════════════════════════════════════════
# 全局设置写权限(2026-11-07 独立复审 P0: 权限绕过)
# ════════════════════════════════════════════════════════════════════
# 事故面: 本模块的 PUT 端点**自身没有任何权限依赖**, 唯一防线是
# `app.demo_isolation_middleware` 对 `/api/settings` 前缀的 manage_settings 判定。
# 而 `ApiVersionAliasMiddleware` 是内层中间件, 会把 `/api/v1/settings/{key}`
# 改写成 `/api/settings/{key}` —— 外层中间件看到的仍是 `/api/v1/...`, 前缀匹配
# 落空 ⇒ 任意 member 可以写 `zhitu_token` / `wudao_mcp_token` / `http_proxy`
# (把全站出网流量导向自己的代理)等全局配置。实测 member 走 /api/v1 一路 200。
#
# 结论: 权限判定必须落在**执行端**(端点依赖), 不能只靠路径前缀。这里同时:
#   1. 实时从**写库**读真实角色(不信 JWT claim、不信读副本滞后) —— 降权立即生效;
#   2. 复用 core 的角色权限表 + `users.permissions` 白名单(既有兼容通道);
#   3. 拒绝服务身份(ServicePrincipal / role=service), 写链路不给服务方开口子;
#   4. 判权过程任何异常一律 403(fail closed), 绝不因为查不到就放行。
SETTINGS_MANAGE_PERM = "manage_settings"
#: 服务身份的角色名(ServicePrincipal.role / 服务账号 role) —— 不得写全局设置
SERVICE_ROLE = "service"


@dataclass(frozen=True)
class SettingsActor:
    """已鉴权的设置写入者(id/username 供审计, role 供排障)。"""

    id: str | None
    username: str
    role: str


def _granted_perms(row) -> set[str]:
    """`users.permissions` 白名单权限点(兼容 list 与 {"permissions": [...]} 两种形态)。

    与 `app._resolve_user_auth` 同口径: owner 给某个 member 单独开权限点时,
    中间件与端点必须给出同一裁决, 否则出现"中间件放行、端点拒绝"的幽灵 403。
    """
    raw = getattr(row, "permissions", None)
    if isinstance(raw, dict):
        raw = raw.get("permissions") or []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {p for p in raw if isinstance(p, str)}
    return set()


def require_settings_write(user: User = Depends(get_current_user)) -> SettingsActor:
    """FastAPI 依赖: 全局设置**写**权限(manage_settings), 任何异常都拒绝。

    只挂在写端点(`PUT /{key}`、`PUT /avatar`)上; GET 保持"登录即可浏览"
    (敏感 key 已掩码) —— 成员看版本/头像/通知参数是既有功能, 不收口。

    四道关:
    1. 登录身份由 `get_current_user` 给出(401 未登录/失效; 服务 token 无法通过);
    2. **实时从写库**取角色 —— 不信 JWT claim(降权后旧 token 仍是 owner claim),
       也不信读副本(带读分离时副本滞后会让降权延后生效);
    3. 权限 = 角色权限表 ∪ `users.permissions` 白名单(既有兼容通道, 与中间件同口径);
    4. 服务身份(role=service)与查询异常一律 403 —— fail closed, 绝不"查不到就放行"。
    """
    from src.core.permissions import get_role_permissions, normalize_role

    # ServicePrincipal(get_user_or_service 侧)不是 User; 万一将来被挂到本路由上,
    # 按鸭子类型显式拒绝, 不给服务身份留写全局配置的口子。
    if getattr(user, "is_service", False):
        raise HTTPException(403, "服务身份不可修改全局设置")

    try:
        db = WriteSessionLocal()
        try:
            row = db.query(User).filter(User.id == user.id).first()
            if row is None:
                raise HTTPException(403, "账号不存在, 拒绝修改全局设置")
            raw_role = str(getattr(row, "role", "") or "").strip().lower()
            perms = get_role_permissions(normalize_role(raw_role)) | _granted_perms(row)
            active = bool(getattr(row, "is_active", False)) and not bool(
                getattr(row, "is_deleted", False)
            )
            actor = SettingsActor(
                id=getattr(row, "id", None),
                username=str(getattr(row, "username", "") or ""),
                role=raw_role or "member",
            )
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 判权失败必须拒绝, 不能放行
        logger.warning("settings 写权限判定异常, 保守拒绝: %r", exc)
        raise HTTPException(403, f"无权限: {SETTINGS_MANAGE_PERM}") from exc

    if not active:
        raise HTTPException(403, "账号不可用, 拒绝修改全局设置")
    if raw_role == SERVICE_ROLE:
        raise HTTPException(403, "服务身份不可修改全局设置")
    if SETTINGS_MANAGE_PERM not in perms:
        raise HTTPException(403, f"无权限: {SETTINGS_MANAGE_PERM}")
    return actor


def _stage_audit(
    db: Session, actor: SettingsActor, action: str, detail: str, ip: str = ""
) -> None:
    """把审计行**挂到当前事务**, 由调用方与设置写入一并提交。

    P0(2026-11-07): 原实现把 `log_audit(...)` 整个包在 `except Exception: pass` 里 ——
    审计写失败既不回滚也不报错, 前端照样拿到 200「改好了」, 事后无从追溯是谁把
    token / 代理换掉的。现在审计与设置写入**同一次 commit**:
      - 审计失败 ⇒ 整笔回滚 + 5xx, 绝不谎报成功(设置也不会落地);
      - 不用 `log_audit` 的独立 session —— 同请求双 session 双 commit 就是
        2026-08-21 那次 `PendingRollbackError` 挂死端点的成因。
    """
    db.add(
        AuditLog(
            user_id=actor.id,
            username=actor.username,
            action=action,
            detail=detail or "",
            ip=ip or "",
        )
    )


def _client_ip(request: Request) -> str:
    try:
        from src.web.middleware import _get_client_ip

        return _get_client_ip(request) or ""
    except Exception:  # noqa: BLE001 —— 审计 ip 只影响可读性, 不影响判权
        return ""


def _audit_safe(key: str, value: str) -> str:
    """审计明细里的值: 敏感键一律掩码, 其余去掉 URL userinfo 后再截断。

    `http_proxy` 不在 SECRET_SETTING_KEYS(它不是凭证, 掩码会让设置页改不动),
    但 `http://user:pass@host` 形态里确实带凭证, 写进审计表就等于把密码落库。
    """
    if key in SECRET_SETTING_KEYS and value:
        return SECRET_MASK
    v = str(value or "")
    if "://" in v and "@" in v.split("://", 1)[1]:
        scheme, rest = v.split("://", 1)
        v = f"{scheme}://***@{rest.split('@', 1)[1]}"
    return v[:50] + ("…" if len(v) > 50 else "")


def _split_url_userinfo(value: str):
    """拆 ``scheme://userinfo@host`` → ``(prefix, userinfo, host)``；无 userinfo 返回 None。"""
    v = str(value or "")
    if "://" not in v:
        return None
    scheme, rest = v.split("://", 1)
    if "@" not in rest:
        return None
    userinfo, host = rest.rsplit("@", 1)
    if not userinfo or not host:
        return None
    return f"{scheme}://", userinfo, host


def mask_url_password(value: str) -> str:
    """把 URL userinfo 里的密码掩成 SECRET_MASK（保留账号/主机，设置页仍可编辑）。

    P0(走查 2026-11-07): `http_proxy` 不在 SECRET_SETTING_KEYS(掩码整串会让设置页
    改不动)，但 `http://user:pass@host` 的 userinfo 是**真凭证** —— 原 GET 把它按
    普通字符串原样回吐，任意 member 一次 GET /api/settings 就拿到代理密码。
    """
    parts = _split_url_userinfo(value)
    if not parts:
        return str(value or "")
    prefix, userinfo, host = parts
    user = userinfo.split(":", 1)[0]
    if ":" not in userinfo:
        # 只有账号没有密码段, 没有可掩的凭证
        return str(value)
    return f"{prefix}{user}:{SECRET_MASK}@{host}"


def _looks_masked_proxy_password(pwd: str) -> bool:
    return pwd in {"***", "**"} or is_mask_placeholder(pwd)


def merge_proxy_credentials(new_value: str, old_value: str) -> str | None:
    """PUT 回填的代理地址 password 若是掩码占位，用旧值里的真密码补回。

    返回 None = 掩码占位但旧值没有可用凭证 ⇒ 调用方必须 400，**绝不把掩码写进 DB**
    (与 SECRET_SETTING_KEYS 的掩码拒写同一策略：否则代理静默失效且设置页仍显示可用)。
    """
    parts = _split_url_userinfo(new_value)
    if not parts:
        return new_value
    prefix, userinfo, host = parts
    if ":" not in userinfo:
        return new_value
    user, pwd = userinfo.split(":", 1)
    if not _looks_masked_proxy_password(pwd):
        return new_value
    old_parts = _split_url_userinfo(old_value)
    if not old_parts:
        return None
    _, old_userinfo, _ = old_parts
    if ":" not in old_userinfo:
        return None
    old_user, old_pwd = old_userinfo.split(":", 1)
    if not old_pwd or is_mask_placeholder(old_pwd):
        return None
    keep_user = user if user and not is_mask_placeholder(user) else old_user
    return f"{prefix}{keep_user}:{old_pwd}@{host}"


def _get_env_defaults() -> dict[str, str]:
    """从 .env / 环境变量读取当前值作为默认"""
    s = Settings()
    return {
        "http_proxy": s.http_proxy,
        "notify_quiet_hours": s.notify_quiet_hours,
        "notify_retry_attempts": str(s.notify_retry_attempts),
        "notify_retry_backoff_seconds": str(s.notify_retry_backoff_seconds),
        "notify_dedupe_ttl_overrides": s.notify_dedupe_ttl_overrides,
        "stock_link_platform": "xueqiu",
        "sida_base_url": os.getenv("SIDA_BASE_URL", ""),
        # 接口 key:初始迁移自环境变量(容器 -e 注入的 wudao/zhitu/tdx token 等)
        "wudao_mcp_token": os.getenv("WUDAO_MCP_TOKEN", ""),
        "zhitu_token": os.getenv("ZHITU_TOKEN", ""),
        "tdx_api_key": os.getenv("TDX_API_KEY", ""),
    }


@router.get("", response_model=list[SettingResponse])
def list_settings(db: Session = Depends(get_db)):
    settings = db.query(AppSettings).all()
    existing_map = {s.key: s for s in settings}

    env_defaults = _get_env_defaults()

    result = []
    for key in SETTING_KEYS:
        desc = SETTING_DESCRIPTIONS.get(key, "")
        env_val = env_defaults.get(key, "")

        if key not in existing_map:
            s = AppSettings(key=key, value=env_val, description=desc)
            db.add(s)
            result.append(s)
        else:
            s = existing_map[key]
            # 描述以代码为准(2026-09-05: DB 旧描述不覆盖代码新描述, 否则改了文案生产不生效)
            if s.description != desc:
                s.description = desc
            result.append(s)
    db.commit()

    # 敏感 key 不回显完整值(只暴露是否已配置,前端用掩码占位)
    # 注意: 必须返回新对象,不能改 ORM 对象 s.value —— 改了会挂起脏写,
    # 下一次同会话查询(autoflush)会把字面掩码写回 DB 覆盖真 token,并加剧 SQLite 锁竞争
    response = []
    for s in result:
        val = s.value
        if s.key in SECRET_SETTING_KEYS and val:
            val = SECRET_MASK
        elif s.key == "http_proxy":
            # member 也能 GET 设置列表: 代理 userinfo 的密码是真凭证, 一律掩码
            val = mask_url_password(val)
        response.append(SettingResponse(key=s.key, value=val, description=s.description))
    return response


AVATAR_KEY = "ui_avatar"  # DB 仅存文件名;图片本体落在 data/avatars/


def _avatar_dir() -> str:
    d = os.path.join(os.environ.get("DATA_DIR", "./data"), "avatars")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_avatar_fname(fname: str) -> str:
    """P2(audit-20260915): DB 中的头像文件名须经 basename 净化, 防路径穿越。"""
    safe = os.path.basename(fname)
    if not safe or safe != fname or "/" in fname or "\\" in fname:
        logger.warning("头像文件名非法, 忽略: %r", fname)
        return ""
    return safe


@router.get("/avatar")
def get_avatar(db: Session = Depends(get_db)):
    """读取用户头像:DB 存文件名,图片本体在 data/avatars/,读取后以 data URL 返回。

    GET /avatar 无同名 GET /{key},不存在路由抢匹配问题。
    """
    row = db.query(AppSettings).filter(AppSettings.key == AVATAR_KEY).first()
    fname = _safe_avatar_fname((row.value if row and row.value else "").strip())
    if not fname:
        return {"value": ""}
    path = os.path.join(_avatar_dir(), fname)
    if not os.path.isfile(path):
        return {"value": ""}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return {"value": ""}
    mime = "image/png" if fname.lower().endswith(".png") else "image/jpeg"
    return {"value": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}


def _remove_avatar_file(fname: str) -> None:
    """尽力删除头像文件(不在 DB 事务内): 失败只记日志, 不影响已提交的设置。"""
    if not fname:
        return
    try:
        os.remove(os.path.join(_avatar_dir(), fname))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("头像旧文件清理失败(忽略): %s err=%r", fname, exc)


@router.put("/avatar")
def set_avatar(
    update: SettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: SettingsActor = Depends(require_settings_write),
):
    """保存/清空用户头像:把 data URL 落成 data/avatars/avatar-<uuid>.* 文件,DB 仅记文件名。

    需在 /{key} 之前注册以优先匹配。传空字符串即清空(删文件 + 清记录)。

    2026-11-07: `ui_avatar` 是**全局单行**(不是 per-user), 属全局设置写 → 与
    /{key} 同门槛(manage_settings)。此前只有中间件前缀在拦, /api/v1 别名可绕过。

    2026-11-07 #2(复审): 文件系统不受 DB 事务保护。原实现先写/删固定名文件再
    commit —— commit 失败回滚后仍返回 503, 但**磁盘已经变了**: 新头像"没保存"
    却生效, 清空"失败"却真的没了。现在改为:
      1. 新图先落 staged 临时文件, 再 ``os.replace`` 到唯一名(绝不覆盖原头像);
      2. 先提交 DB(含审计), 成功后才清理旧文件;
      3. commit 失败(503) → 清掉刚写的新文件, 旧文件与 DB 原值保持一致;
      4. 清空同样先提交 DB 再删文件, 清理失败不影响结果。
    """
    row = db.query(AppSettings).filter(AppSettings.key == AVATAR_KEY).first()
    # P2(audit-20260915): old 来自 DB, 删除前须 basename 净化
    old = _safe_avatar_fname((row.value if row else "") or "")
    value = (update.value or "").strip()

    if not value:
        if row:
            row.value = ""
        _stage_audit(
            db, actor, f"settings_update:{AVATAR_KEY}", "改前=<已设置> 改后=<清空>",
            ip=_client_ip(request),
        )
        # 先提交: 提交失败时文件还在, DB 仍指向旧头像, 状态一致
        _commit_or_500(db, AVATAR_KEY, actor)
        _remove_avatar_file(old)
        return {"value": ""}

    if not (value.startswith("data:") and "," in value):
        raise HTTPException(400, "头像需为 data URL")
    header, b64 = value.split(",", 1)
    ext = "png" if "image/png" in header else "jpg"
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "头像数据无效")

    avatar_dir = _avatar_dir()
    # 唯一名: 并发写入/扩展名切换都不会互相覆盖, 也不会覆盖尚未提交的旧头像
    fname = f"avatar-{uuid.uuid4().hex}.{ext}"
    staged = os.path.join(avatar_dir, f".staged-{uuid.uuid4().hex}.part")
    try:
        with open(staged, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staged, os.path.join(avatar_dir, fname))
    except OSError as exc:
        _remove_avatar_file(os.path.basename(staged))
        _remove_avatar_file(fname)
        logger.error("头像文件写入失败: %r", exc)
        raise HTTPException(503, "头像未保存: 文件写入失败, 请重试") from exc

    if not row:
        row = AppSettings(key=AVATAR_KEY, value=fname, description="用户头像文件名")
        db.add(row)
    else:
        row.value = fname
    # 审计只记文件名/动作, 绝不记 data URL 本身(那是可还原的图片正文)
    _stage_audit(
        db, actor, f"settings_update:{AVATAR_KEY}", f"改后={fname}", ip=_client_ip(request)
    )
    try:
        _commit_or_500(db, AVATAR_KEY, actor)
    except HTTPException:
        # DB 回滚 ⇒ 旧头像仍被 DB 引用, 只清掉这张没被登记的新文件
        _remove_avatar_file(fname)
        raise
    _remove_avatar_file(old)
    return {"value": fname}


def _commit_or_500(db: Session, key: str, actor: SettingsActor) -> None:
    """提交设置 + 审计; 任一失败整笔回滚并 5xx(不谎报成功)。

    审计与设置同事务: 要么"改了且有据可查", 要么"没改且明确报错"。
    """
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— 审计/写入失败必须让调用方看到失败
        db.rollback()
        logger.error(
            "settings 写入失败(含审计), 已回滚: key=%s actor=%s err=%r",
            key, actor.username, exc,
        )
        raise HTTPException(503, "设置未保存: 审计写入失败, 已回滚, 请稍后重试") from exc


@router.put("/{key}")
def update_setting(
    key: str,
    update: SettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    actor: SettingsActor = Depends(require_settings_write),
):
    # P2-1 (2026-09-05 28号审计): key 白名单, 系统保留键拒绝直写
    if key not in SETTING_KEYS:
        raise HTTPException(400, f"未知设置键: {key}")
    if key in ("jwt_secret", "allow_register"):
        raise HTTPException(403, "系统保留键, 拒绝直写")
    # P0-①(走查 2026-10-23 W-3): 掩码值拒收。按**形状**判定而不是精确相等 ——
    # " ******** " / "*********" / "········" 一律拦下, 否则字面掩码会覆盖真凭证。
    raw_value = update.value or ""
    value_to_store = raw_value
    setting = db.query(AppSettings).filter(AppSettings.key == key).first()
    old_value = (setting.value if setting else "") or ""
    if key in SECRET_SETTING_KEYS:
        if is_mask_placeholder(raw_value):
            raise HTTPException(400, "敏感值未变更(掩码占位), 拒绝写入")
        stripped = raw_value.strip()
        if not stripped and not update.clear:
            # 空值写入会让"已配置"变成"未配置"且无提示; 清除必须显式 clear=true
            raise HTTPException(400, "敏感值不能为空(如需清除请显式传 clear=true)")
        # 凭证两侧空白一律去掉: 带尾随空格/换行的 token 会静默失效
        value_to_store = "" if update.clear else stripped
    elif key == "http_proxy":
        # 设置页 GET 回来的是掩码 userinfo。用户只改主机时会把掩码原样提交回来,
        # 直接落库等于用字面掩码覆盖真密码 ⇒ 代理静默失效。这里按 SECRET 同一策略:
        # 掩码占位 → 用旧值补回真凭证; 补不回就 400, 绝不写掩码。
        value_to_store = raw_value.strip()
        # 生效值口径是 DB 优先、回落 env(见 _get_env_defaults / runtime), 掩码还原
        # 也必须按同一口径取旧凭证, 否则"env 配了带密码代理"时用户改主机就被 400 挡住。
        base_value = old_value or (_get_env_defaults().get("http_proxy") or "")
        merged = merge_proxy_credentials(value_to_store, base_value)
        if merged is None:
            raise HTTPException(
                400, "代理密码为掩码占位且无可还原凭证, 请重新输入完整代理地址"
            )
        value_to_store = merged

    if not setting:
        desc = SETTING_DESCRIPTIONS.get(key, "")
        setting = AppSettings(key=key, value=value_to_store, description=desc)
        db.add(setting)
    else:
        setting.value = value_to_store

    # B.6 系统设置审计: 记改前改后(敏感值记掩码, 不记明文; 代理去 userinfo)
    # 2026-11-07: 审计与写入**同一次 commit**, 审计失败整笔回滚 + 5xx。
    # 原实现 `except Exception: pass` 会在审计写不进去时仍然返回 200「已保存」,
    # 等于"改了没人知道"—— 对凭证/代理这类全局配置是不可接受的。
    _stage_audit(
        db,
        actor,
        f"settings_update:{key}",
        f"改前={_audit_safe(key, old_value)!r} 改后={_audit_safe(key, value_to_store)!r}",
        ip=_client_ip(request),
    )
    _commit_or_500(db, key, actor)
    db.refresh(setting)

    # http_proxy 改动立刻反映到进程 env,所有 httpx(trust_env=True)免重启即走新代理
    if key == "http_proxy":
        try:
            from server import apply_proxy_env
            # 用**落库值**(已把掩码 userinfo 补回真凭证), 掩码本身不能当代理地址用
            apply_proxy_env(value_to_store)
        except Exception:
            pass

    # P0-②(走查 2026-10-23 W-4): 敏感键的响应体不回吐任何明文 ——
    # 只回 {key, updated, masked}。原实现直接 return ORM 对象, 刚写入的明文会出现在
    # 响应体 / DevTools Network / 任何请求日志里, 与 list_settings 的掩码策略自相矛盾。
    if key in SECRET_SETTING_KEYS:
        return {"key": key, "updated": True, "masked": SECRET_MASK}
    if key == "http_proxy":
        # 与 GET 一致: 响应里的 userinfo 密码同样掩码(管理员也不例外)
        return SettingResponse(
            key=setting.key,
            value=mask_url_password(setting.value),
            description=setting.description,
        )

    return SettingResponse(key=setting.key, value=setting.value, description=setting.description)


@router.get("/version")
def get_version():
    """获取应用版本号"""
    return {"version": get_app_version()}


@router.get("/update-check")
def get_update_check(db: Session = Depends(get_db)):
    """检查是否有可用新版本（带服务端缓存）。"""
    current = get_app_version()
    app_proxy = (
        db.query(AppSettings)
        .filter(AppSettings.key == "http_proxy")
        .first()
    )
    proxy = (app_proxy.value if app_proxy and app_proxy.value else "").strip() or (
        Settings().http_proxy or ""
    )
    result = check_update(current, proxy=proxy)
    err = str(result.get("error") or "").strip()
    if err:
        return {
            "success": False,
            "code": 10061,
            "message": err,
        }
    return result
