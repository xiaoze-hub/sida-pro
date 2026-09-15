"""我的服务商(BYOK) — 用户自定义 LLM 服务商 (2026-08-15)

每个登录用户维护自己的 AI 服务商列表(名称/base_url/api_key/模型列表), 数据按 user_id 隔离:
- 仅 member/owner 可用; guest(demo, username==demo) 一律 403
- api_key 不回显明文, 已配置显示掩码 "********"(与 providers/settings 的 SECRET_MASK 一致)
- 数据落 UserAIService 表(models.py 定义: id, user_id, name, base_url, api_key,
  models_json(TEXT 存 [{name, model, is_default, scene, capabilities}] JSON), created_at)

⚠ 路由挂载: 本文件只定义 router, 需要主模型在 app.py 挂载(本文件被并行开发, 不碰 app.py):
    from src.web.api import my_ai_services
    app.include_router(my_ai_services.router, prefix="/api/my-ai-services", tags=["my-ai-services"])
"""
import json
import ipaddress
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User, UserAIService

router = APIRouter()

SECRET_MASK = "********"

# 场景下拉(与 providers.SCENES 对齐, 仅用于前端下拉提示; 后端宽松存储, 不校验枚举)
SCENES = ("chat", "trading_agents", "reports", "referee", "selfcheck", "insights", "vision")


def _validate_base_url(base_url: str) -> str:
    """安全审计 2026-09-15: BYOK base_url 拒绝内网/云 metadata 地址(SSRF 防护)。

    用户自定义 LLM 服务商的 base_url 若指向 169.254.169.254 / 10.x / 192.168.x
    等私网段, 可借服务器发起内网请求。此处拒绝私网/环回/链路本地/保留地址。
    """
    url = (base_url or "").strip()
    if not url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        raise HTTPException(400, "base_url 格式非法")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(400, "base_url 缺少主机名")
    # IP 直连: 拒绝私网/环回/链路本地/保留/组播
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(400, f"base_url 不允许指向内网/本地地址: {host}")
    except ValueError:
        pass  # 非 IP 形式, 继续走域名解析校验
    # 域名解析: 命中内网段也拒绝(防 DNS 指向内网)
    import socket
    try:
        for info in socket.getaddrinfo(host, None):
            try:
                resolved = ipaddress.ip_address(info[4][0])
                if resolved.is_private or resolved.is_loopback or resolved.is_link_local:
                    raise HTTPException(400, f"base_url 域名解析到内网地址, 已拒绝: {host}")
            except ValueError:
                continue
    except (socket.gaierror, OSError):
        pass  # 解析失败不阻断, 交给后续请求报错
    return url


def _require_not_guest(user: User) -> User:
    """guest(demo) 一律 403, 与权限体系对齐: BYOK 仅 member+ 可用。"""
    if user.username == "demo":
        raise HTTPException(403, "演示账号不可用")
    return user


def _parse_models(raw) -> list:
    """从 models_json(TEXT) 解析模型列表; 空/非法返回 []。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


def _to_response(svc) -> dict:
    """序列化(api_key 掩码, 不回显明文)。"""
    return {
        "id": svc.id,
        "name": svc.name,
        "base_url": svc.base_url,
        "api_key": SECRET_MASK if svc.api_key else "",
        "models": _parse_models(svc.models_json),
        "created_at": svc.created_at.isoformat() if getattr(svc, "created_at", None) else None,
    }


class MyModelItem(BaseModel):
    name: str = ""
    model: str
    is_default: bool = False
    scene: str = "chat"
    capabilities: list[str] = Field(default_factory=list)


class MyServiceCreate(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    models: list[MyModelItem] = Field(default_factory=list)


class MyServiceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[MyModelItem] | None = None


@router.get("")
def list_my_services(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的服务商列表(key 掩码显示)。"""
    _require_not_guest(user)
    rows = (
        db.query(UserAIService)
        .filter(UserAIService.user_id == user.id)
        .order_by(UserAIService.id)
        .all()
    )
    return [_to_response(s) for s in rows]


@router.post("")
def create_my_service(
    body: MyServiceCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建服务商(归属当前登录 user.id)。"""
    _require_not_guest(user)
    # 安全审计 2026-09-15: 校验 base_url 不指向内网
    _validate_base_url(body.base_url)
    svc = UserAIService(
        user_id=user.id,
        name=body.name.strip(),
        base_url=body.base_url.strip(),
        api_key=body.api_key.strip() if body.api_key else "",
        models_json=json.dumps(
            [m.model_dump() for m in body.models], ensure_ascii=False
        ),
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return _to_response(svc)


@router.put("/{sid}")
def update_my_service(
    sid: int,
    body: MyServiceUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新自己的服务商(归属校验: 只能改自己的; api_key 传掩码时保留原值)。"""
    _require_not_guest(user)
    svc = (
        db.query(UserAIService)
        .filter(UserAIService.id == sid, UserAIService.user_id == user.id)
        .first()
    )
    if not svc:
        raise HTTPException(404, "服务商不存在")

    data = body.model_dump(exclude_unset=True)
    # 安全审计 2026-09-15: 更新 base_url 时同样校验内网地址
    if data.get("base_url"):
        _validate_base_url(data["base_url"])
    # 掩码占位(编辑未修改时回传 "********")/空串/None 不覆盖真 key
    if data.get("api_key") in (SECRET_MASK, "", None):
        data.pop("api_key", None)
    if "models" in data and data["models"] is not None:
        data["models_json"] = json.dumps(
            [m.model_dump() for m in data.pop("models")], ensure_ascii=False
        )
    for key, value in data.items():
        if key == "api_key":
            value = value.strip() if value else ""
        setattr(svc, key, value)

    db.commit()
    db.refresh(svc)
    return _to_response(svc)


@router.delete("/{sid}")
def delete_my_service(
    sid: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除自己的服务商(归属校验)。"""
    _require_not_guest(user)
    svc = (
        db.query(UserAIService)
        .filter(UserAIService.id == sid, UserAIService.user_id == user.id)
        .first()
    )
    if not svc:
        raise HTTPException(404, "服务商不存在")
    db.delete(svc)
    db.commit()
    return {"ok": True}
