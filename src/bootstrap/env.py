"""进程环境装配: 代理桥接 / SSL 证书 / 日志 / Playwright。

原 server.py 内联实现, W3.2/D1 原样搬移。logger 名保持 "server",
让启动日志在 UI 日志板/DB 里与拆分前逐字一致。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from src.web.database import SessionLocal
from src.web.log_handler import DBLogHandler
from src.config import Settings
from src.core.log_context import install_log_record_factory
from src.web.models import AppSettings

REPO_ROOT = Path(__file__).resolve().parents[2]

# 日志名沿用拆分前 server.py 的 __name__("server"), 观测面零变化。
logger = logging.getLogger("server")


def _ca_bundle_path() -> str:
    return str(REPO_ROOT / "data" / "ca-bundle.pem")


def apply_proxy_env(proxy: str | None) -> None:
    """统一更新进程环境变量代理,让所有 httpx 默认 Client (trust_env=True) 走该代理。

    传空字符串 / None 时清除环境变量(取消代理)。
    NO_PROXY 默认含 localhost / 回环地址,避免本地访问绕一圈。
    """
    p = (proxy or "").strip()
    if p:
        os.environ["HTTP_PROXY"] = p
        os.environ["HTTPS_PROXY"] = p
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,0.0.0.0")
        logger.info(f"HTTP/HTTPS 代理已应用: {p}")
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY"):
            os.environ.pop(key, None)
        logger.info("HTTP/HTTPS 代理已清除")


def setup_proxy():
    """启动时把已配置的 HTTP 代理桥接到环境变量。

    优先级:
    1. 已存在的 HTTP_PROXY / HTTPS_PROXY 环境变量(用户显式覆盖,不动)
    2. app_settings.http_proxy(UI 配置)
    3. .env 中的 http_proxy(Settings.http_proxy)
    """
    if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY"):
        logger.info(
            f"沿用现有环境变量代理: HTTP_PROXY={os.environ.get('HTTP_PROXY', '')} "
            f"HTTPS_PROXY={os.environ.get('HTTPS_PROXY', '')}"
        )
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,0.0.0.0")
        return

    proxy = ""
    try:
        db = SessionLocal()
        try:
            setting = (
                db.query(AppSettings).filter(AppSettings.key == "http_proxy").first()
            )
            if setting and setting.value:
                proxy = setting.value.strip()
        finally:
            db.close()
    except Exception:
        pass

    if not proxy:
        proxy = (Settings().http_proxy or "").strip()

    if proxy:
        apply_proxy_env(proxy)


def setup_ssl():
    """设置 SSL 证书环境（企业代理环境）"""
    settings = Settings()
    ca_cert = settings.ca_cert_file
    if not ca_cert or not os.path.exists(ca_cert):
        return

    import certifi

    bundle_path = _ca_bundle_path()
    os.makedirs(os.path.dirname(bundle_path), exist_ok=True)

    need_rebuild = not os.path.exists(bundle_path) or os.path.getmtime(
        ca_cert
    ) > os.path.getmtime(bundle_path)

    if need_rebuild:
        with open(bundle_path, "w") as out:
            with open(certifi.where(), "r") as f:
                out.write(f.read())
            out.write("\n")
            with open(ca_cert, "r") as f:
                out.write(f.read())

    os.environ["SSL_CERT_FILE"] = bundle_path
    os.environ["REQUESTS_CA_BUNDLE"] = bundle_path
    logger.info(f"SSL 证书已加载: {bundle_path}")


def setup_logging():
    """配置日志: 控制台 + 数据库

    分级策略:
    - root logger 始终 DEBUG,所有日志都会传播到 handler
    - 控制台 handler 按 LOG_LEVEL 过滤(默认 INFO),并丢弃 httpx 等三方库的 < WARNING 噪音
    - DB handler 始终 DEBUG 全量收录,UI 日志板永远可以看到包括心跳/httpx 请求在内的完整记录
    """
    console_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    console_level = getattr(logging, console_level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    install_log_record_factory()

    # reload/server restart 时避免重复 handler 导致日志放大。
    for h in list(root.handlers):
        if isinstance(h, DBLogHandler) or getattr(h, "_panwatch_console", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    # 控制台输出: 按 LOG_LEVEL 过滤,且丢弃三方库的低级别噪音
    console = logging.StreamHandler()
    console._panwatch_console = True  # type: ignore[attr-defined]
    console.setLevel(console_level)
    console.addFilter(_ConsoleNoiseFilter())
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", datefmt="%H:%M:%S"
        )
    )
    root.addHandler(console)

    # 数据库持久化: 始终全量收录,UI 日志板可查 DEBUG
    db_handler = DBLogHandler(level=logging.DEBUG)
    db_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(db_handler)

    # uvicorn 默认给自己挂了 stderr handler 并且 propagate=False,导致 access log
    # 走自己的链路(`INFO: 127.0.0.1 - "GET /api/..."`)不被我们的 filter 拦截。
    # 改成清空自己的 handler + propagate 到 root,让 _ConsoleNoiseFilter 生效。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(logging.DEBUG)


class _ConsoleNoiseFilter(logging.Filter):
    """控制台 handler 过滤器: 三方库的 INFO/DEBUG 不进 stdout,WARNING+ 仍然显示。
    DB handler 不挂这个过滤器,UI 日志板能看到完整请求记录。

    uvicorn.access 是每条请求的 access log(`INFO: 127.0.0.1 - "GET /api/..." 200 OK`),
    属于底层心跳;uvicorn / uvicorn.error 是应用级日志(启动、报错),保留。"""

    _NOISY_PREFIXES = ("httpx", "httpcore", "urllib3", "apscheduler", "uvicorn.access")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        name = record.name or ""
        for prefix in self._NOISY_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True


def setup_playwright():
    """检查并安装 Playwright 浏览器

    本地开发时使用系统安装的 Playwright，Docker 环境下安装到 data 目录。
    通过 DOCKER 环境变量或显式设置的 PLAYWRIGHT_BROWSERS_PATH 来判断。
    """
    import subprocess

    # 允许通过环境变量跳过首次安装（例如不需要截图功能时）
    if os.environ.get("PLAYWRIGHT_SKIP_BROWSER_INSTALL") == "1":
        logger.info(
            "已设置 PLAYWRIGHT_SKIP_BROWSER_INSTALL=1，跳过 Playwright 浏览器安装"
        )
        return

    # 如果用户已显式设置 PLAYWRIGHT_BROWSERS_PATH，尊重该设置
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        browser_dir = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
        logger.info(f"使用自定义 Playwright 路径: {browser_dir}")
    # Docker 环境下安装到 data 目录
    elif os.environ.get("DOCKER") == "1":
        data_dir = os.environ.get("DATA_DIR", "./data")
        browser_dir = os.path.join(data_dir, "playwright")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browser_dir
        logger.info(f"Docker 环境，Playwright 路径: {browser_dir}")
    else:
        # 本地开发，使用系统默认路径，不做任何安装
        logger.info("本地开发环境，使用系统 Playwright")
        return

    # 检查是否已安装
    if os.path.exists(browser_dir):
        try:
            dirs = os.listdir(browser_dir)
            if any(
                d.startswith("chromium")
                for d in dirs
                if os.path.isdir(os.path.join(browser_dir, d))
            ):
                logger.info(f"Playwright 浏览器已就绪: {browser_dir}")
                return
        except Exception:
            pass

    # 首次安装
    logger.info("首次启动，正在安装 Playwright 浏览器（可能需要几分钟）...")
    os.makedirs(browser_dir, exist_ok=True)

    # v0.4.47 fix (2026-09-01): 默认从国外 CDN 下载 109MB chromium 实测 4-7MB/分钟
    # (一次 15 分钟), 加上容器层 /app/.cache 不持久会死循环。优先用国内镜像。
    # 镜像源用阿里云镜像(国内可达, 速度稳定): https://registry.npmmirror.com/-/binary/playwright
    install_env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browser_dir}
    if not os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST"):
        install_env["PLAYWRIGHT_DOWNLOAD_HOST"] = (
            "https://registry.npmmirror.com/-/binary/playwright"
        )
        logger.info("使用国内镜像下载 Playwright (registry.npmmirror.com)")

    try:
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            env=install_env,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时
        )
        if result.returncode == 0:
            logger.info("Playwright 浏览器安装完成")
        else:
            logger.error(f"Playwright 安装失败: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("Playwright 安装超时（网络问题？）")
    except FileNotFoundError:
        logger.warning("Playwright 命令不可用，K线截图功能不可用")
    except Exception as e:
        logger.error(f"Playwright 安装失败: {e}")
