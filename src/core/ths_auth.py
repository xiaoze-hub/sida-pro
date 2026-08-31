"""同花顺扫码登录 session 管理(2026-08-09 香港节点实测可用)。

能力:
- create_qrcode():  生成扫码登录二维码(qrid + 图片 URL)
- poll_qrcode():    轮询扫码状态,成功返回 {account, password(凭证), expireTime}
- login():          凭证 → verify2 登录 → sessionid/passport
- get_session():    自动续期: 持久化凭证,过期自动重新登录

认证链(反编译 Normandy.Identity.Client 复刻):
    do_rsa 拿公钥 → unified_login(RSA 加密账号密码) → sessionid
    → verify(passport 签发, product=S01 qsid=8012)
关键坑:
  1. RSA 密文已是 urlencode 后,拼 query 时**禁止二次 urlencode**(否则 % → %25 报"账号为空")
  2. 账号密码需 GBK 编码后 RSA PKCS1v15 加密 → 标准 base64 → urlencode
  3. 扫码返回的 password 字段是**登录凭证**(非用户密码),直接当密码用
  4. mx_ 前缀账号是妙想体系,不走 salt 协议(verify3/gs 返回空 result),走 MD5 unified_login
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

UPASS_HOST = "https://upass.10jqka.com.cn"
AUTH_HOST = "https://auth.10jqka.com.cn"
APPID = "2022021120720687"          # 从客户端 AuthAppInfo 反编译
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0"

# 凭证存储键(AppSettings)
K_ACCOUNT = "ths_account"           # mx_ 账号
K_PASSWORD = "ths_password"         # 扫码登录凭证(非用户密码)
K_EXPIRES = "ths_expires"           # session 过期时间(ISO)
K_USERID = "ths_userid"             # userid


@dataclass
class ThsSession:
    """同花顺登录态。"""

    account: str = ""
    password: str = ""               # 扫码凭证(非用户密码)
    userid: str = ""
    sessionid: str = ""
    expires: datetime | None = None
    passport: str = ""
    logged_in: bool = False


def _rsa_encrypt(pub_pem: str, text: str) -> str:
    """GBK 编码 → RSA PKCS1v15 → 标准 base64 → urlencode(禁二次编码)。"""
    pub = serialization.load_pem_public_key(pub_pem.encode())
    enc = pub.encrypt(text.encode("gbk"), padding.PKCS1v15())
    return urllib.parse.quote(base64.b64encode(enc).decode(), safe="")


def _post_form(url: str, params: dict[str, str], headers: dict | None = None) -> str:
    """POST 表单。值已是 urlencode 后,直接拼接避免二次编码。"""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    hd = {
        "User-Agent": _UA,
        "Referer": "https://upass.10jqka.com.cn/",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if headers:
        hd.update(headers)
    req = urllib.request.Request(url, data=q.encode("gbk"), headers=hd, method="POST")
    return urllib.request.urlopen(req, timeout=15).read().decode("gb2312", errors="ignore")


def _get(url: str, headers: dict | None = None) -> bytes:
    hd = {"User-Agent": _UA}
    if headers:
        hd.update(headers)
    return urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=15).read()


# --------------------------------------------------------------------------
# 1. 扫码登录
# --------------------------------------------------------------------------

def create_qrcode() -> dict:
    """生成扫码登录二维码。返回 {qrid, img_url, img_base64}。"""
    resp = _get(f"{UPASS_HOST}/scan/creatCode")
    j = json.loads(resp.decode("utf-8", errors="ignore"))
    qrid = j.get("qrid")
    if not qrid:
        raise RuntimeError(f"creatCode 失败: {resp[:200]}")
    img = _get(f"{UPASS_HOST}/scan/creatImg?qrid={qrid}")
    return {
        "qrid": qrid,
        "img_url": f"{UPASS_HOST}/scan/creatImg?qrid={qrid}",
        "img_base64": base64.b64encode(img).decode(),
        "created_at": datetime.now().isoformat(),
    }


def poll_qrcode(qrid: str, timeout_s: int = 180) -> dict:
    """轮询扫码状态。成功返回 {account, password, expireTime};超时抛 TimeoutError。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = urllib.parse.urlencode({"qrid": qrid, "state": 1, "page_source": "client_screen"}).encode()
        req = urllib.request.Request(
            f"{UPASS_HOST}/scan/getInfoNew",
            data=data,
            headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
            j = json.loads(resp)
            if j.get("status") == 3 and j.get("account"):
                return {
                    "account": j["account"],
                    "password": j.get("password", ""),
                    "expireTime": j.get("expireTime", ""),
                }
            if str(j.get("expired")) == "1" and j.get("status") == 0:
                raise TimeoutError("二维码已过期")
        except TimeoutError:
            raise
        except Exception as e:
            logger.warning(f"[ths_auth] 轮询异常: {e}")
        time.sleep(3)
    raise TimeoutError(f"等待扫码超时({timeout_s}s)")


# --------------------------------------------------------------------------
# 2. 登录 + passport
# --------------------------------------------------------------------------

def login(account: str, password: str) -> ThsSession:
    """凭证 → verify2 登录 → passport。password 是扫码凭证。"""
    body = _get(f"{AUTH_HOST}/verify2?reqtype=do_rsa&type=get_pubkey").decode("gb2312", errors="ignore")
    pub = re.search(r'pubkey="([^"]+)"', body).group(1)
    ver = re.search(r'rsa_version="([^"]+)"', body).group(1)

    resp = _post_form(f"{AUTH_HOST}/verify2", {
        "reqtype": "unified_login",
        "account": _rsa_encrypt(pub, account),
        "passwd": _rsa_encrypt(pub, password),
        "rsa_version": ver,
        "ta_appid": APPID,
    })
    m_ret = re.search(r'code="(-?\d+)" msg="([^"]*)"', resp)
    if not m_ret or m_ret.group(1) != "0":
        raise RuntimeError(f"登录失败: {resp[:200]}")
    m_uid = re.search(r'userid="(\d+)"', resp)
    m_sid = re.search(r'sessionid="([^"]+)"', resp)
    m_exp = re.search(r'expires="([^"]+)"', resp)
    if not m_sid:
        raise RuntimeError(f"未拿到 sessionid: {resp[:200]}")

    sess = ThsSession(
        account=account,
        password=password,
        userid=m_uid.group(1) if m_uid else "",
        sessionid=m_sid.group(1),
        expires=datetime.strptime(m_exp.group(1), "%Y-%m-%d %H:%M:%S") if m_exp else None,
        logged_in=True,
    )

    # passport 签发
    try:
        resp2 = _post_form(f"{AUTH_HOST}/verify2", {
            "reqtype": "verify",
            "product": "S01",
            "imei": "00000000-0000-0000-0000-000000000000",
            "userid": sess.userid,
            "sessionid": sess.sessionid,
            "qsid": "8012",
            "version": "2.9.1.2",
            "sdsn": "",
            "securities": "同花顺远航版",
            "nohqlist": "0",
            "newwgflag": "3",
        })
        m_pp = re.search(r'passport="([^"]+)"', resp2)
        if m_pp:
            sess.passport = m_pp.group(1)
    except Exception as e:
        logger.warning(f"[ths_auth] passport 签发失败(不影响 session): {e}")
    return sess


# --------------------------------------------------------------------------
# 3. 持久化 + 自动续期
# --------------------------------------------------------------------------

def _save(store: dict[str, str]) -> None:
    """写 AppSettings(惰性导入避免顶层耦合 DB)。"""
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        for k, v in store.items():
            row = db.query(AppSettings).filter(AppSettings.key == k).first()
            if row:
                row.value = str(v)
            else:
                db.add(AppSettings(key=k, value=str(v), description="同花顺登录态"))
        db.commit()
    finally:
        db.close()


def _load() -> dict[str, str]:
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        rows = db.query(AppSettings).filter(AppSettings.key.in_(
            [K_ACCOUNT, K_PASSWORD, K_EXPIRES, K_USERID])).all()
        return {r.key: r.value for r in rows}
    finally:
        db.close()


def save_session(sess: ThsSession) -> None:
    _save({
        K_ACCOUNT: sess.account,
        K_PASSWORD: sess.password,
        K_USERID: sess.userid,
        K_EXPIRES: sess.expires.isoformat() if sess.expires else "",
    })


def get_session(force: bool = False) -> ThsSession:
    """读取持久化 session;过期或缺失则尝试用凭证重新登录(自动续期)。

    无法续期时返回 logged_in=False(需要重新扫码)。
    """
    saved = _load()
    if not saved.get(K_ACCOUNT) or not saved.get(K_PASSWORD):
        return ThsSession()
    sess = ThsSession(
        account=saved[K_ACCOUNT],
        password=saved[K_PASSWORD],
        userid=saved.get(K_USERID, ""),
        logged_in=False,
    )
    if saved.get(K_EXPIRES):
        try:
            sess.expires = datetime.fromisoformat(saved[K_EXPIRES])
        except ValueError:
            sess.expires = None
    # 未过期且非强制 → 直接返回(login() 里才有 sessionid;这里补一次登录保持最新)
    # 简化: 始终重新登录一次拿新 sessionid(passport 长期有效,login 幂等)
    try:
        fresh = login(sess.account, sess.password)
        save_session(fresh)
        return fresh
    except Exception as e:
        logger.warning(f"[ths_auth] 自动续期失败: {e}")
        return sess


def session_status() -> dict:
    """供 API 返回的状态摘要。"""
    saved = _load()
    if not saved.get(K_ACCOUNT):
        return {"logged_in": False, "need_scan": True, "account": ""}
    expires = saved.get(K_EXPIRES, "")
    try:
        exp_dt = datetime.fromisoformat(expires) if expires else None
        expired = exp_dt < datetime.now() if exp_dt else True
    except ValueError:
        expired = True
    return {
        "logged_in": bool(saved.get(K_ACCOUNT)) and not expired,
        "need_scan": False,
        "account": saved.get(K_ACCOUNT, ""),
        "userid": saved.get(K_USERID, ""),
        "expires": expires,
        "expired": expired,
    }
