import base64
import json
import logging
import asyncio
import random
import threading
import time
from pathlib import Path

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# ── 全局 LLM 熔断器 (2026-08-25, P0 AI 审计整改) ──
# 复用 intraday_monitor.py:304-318 令牌桶(10/min) + 600s 冷却逻辑,
# 按 service 分桶避免跨通道误伤。进程级共享, 线程安全。

class GlobalLLMCircuitBreaker:
    """进程级全局 LLM 熔断器: 令牌桶 + 429 冷却 + 按 service 分桶。

    每个 service 独立计费, 避免 deepseek 冷却误伤 agnes 通道。
    线程安全: 所有状态变更由 threading.Lock 保护。
    用法:  AIClient 在 __init__ 时自动获取／创建对应 service 的桶。

    Reference:
        intraday_monitor.py `_ai_rate_allow()` (行 304-318)
        intraday_monitor.py `_ai_rate_mark_429()` (行 321-326)
    """

    _instances: dict[str, "GlobalLLMCircuitBreaker"] = {}
    _instances_lock = threading.Lock()

    DEFAULT_RATE = 10         # 每分钟最多 N 次
    DEFAULT_COOLDOWN = 600    # 429 冷却秒数

    __slots__ = ("service", "rate", "cooldown", "_lock",
                 "_window_start", "_count", "_429_until")

    def __init__(self, service: str, rate: int = DEFAULT_RATE,
                 cooldown: int = DEFAULT_COOLDOWN):
        self.service = service
        self.rate = rate
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._count = 0
        self._429_until = 0.0

    @classmethod
    def get_or_create(cls, service: str, rate: int = DEFAULT_RATE,
                      cooldown: int = DEFAULT_COOLDOWN) -> "GlobalLLMCircuitBreaker":
        """获取或创建 service 级别的熔断器。"""
        with cls._instances_lock:
            if service not in cls._instances:
                cls._instances[service] = cls(service, rate, cooldown)
            return cls._instances[service]

    def acquire(self) -> bool:
        """尝试获取一个令牌。返回 True 表示可以调用, False 表示限流/冷却中。"""
        now = time.time()
        with self._lock:
            if now < self._429_until:
                return False
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._count = 0
            if self._count >= self.rate:
                return False
            self._count += 1
            return True

    def mark_429(self):
        """标记 429 响应: 进入冷却窗口。"""
        with self._lock:
            self._429_until = time.time() + self.cooldown

    @property
    def is_cooling(self) -> bool:
        """是否在 429 冷却中。"""
        with self._lock:
            return time.time() < self._429_until


# ── 指数退避常量 (与 max_retries=0 的 SDK 配合) ──
_CB_RETRY_MAX = 2          # 最多重试次数
_CB_BASE_DELAY = 1.0       # 基础退避秒数(每次翻倍 + jitter)


def _is_rate_limit_error(e: Exception) -> bool:
    """判断异常是否为 429 限流(兼容 openai v1+/httpx 等)。"""
    if hasattr(e, "status_code"):
        return e.status_code == 429
    if hasattr(e, "response") and hasattr(e.response, "status_code"):
        return e.response.status_code == 429
    err_name = type(e).__name__.lower()
    return "ratelimit" in err_name or "rate_limit" in err_name


def _is_retryable_error(e: Exception) -> bool:
    """判断是否为可重试的临时错误(超时/连接/500 系)。"""
    if hasattr(e, "status_code"):
        return e.status_code in (500, 502, 503)
    if hasattr(e, "response") and hasattr(e.response, "status_code"):
        return e.response.status_code in (500, 502, 503)
    err_name = type(e).__name__.lower()
    return any(x in err_name for x in ("timeout", "connection", "internalserver"))


class AIClient:
    """OpenAI 协议兼容的 AI 客户端"""

    def __init__(self, base_url: str, api_key: str, model: str = "",
                 proxy: str = "", scene: str = "other"):
        kwargs = {
            "base_url": base_url,
            "api_key": api_key,
        }
        if proxy:
            kwargs["http_client"] = None  # TODO: 如需代理，用 httpx 配置
        # v0.4.9: 关闭 SDK 自动重试 — 429 时由上层限速/冷却控制, 避免 retry 放大风暴
        kwargs.setdefault("max_retries", 0)
        self.client = AsyncOpenAI(**kwargs)
        # 保留原始配置作为实例属性,供需要桥接到第三方 LLM 框架的 agent 使用
        # (e.g. TradingAgents 需要 base_url+api_key 重新构造 langchain 的 LLM)
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.scene = scene
        self.total_tokens_used = 0
        # v0.6.0: 全局 LLM 熔断器(按 scene 分桶)
        self._cb = GlobalLLMCircuitBreaker.get_or_create(scene)

    # ── LLM 调用日志(2026-08-15): 轻量记录 token/耗时/场景, 失败静默 ──
    def _log_usage(self, scene: str | None, model_name: str,
                   usage, latency_ms: int) -> None:
        try:
            from src.web.models import LLMUsage
            from src.web.database import SessionLocal
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            if prompt_tokens == 0 and completion_tokens == 0:
                return  # 无用量信息不记
            db = SessionLocal()
            try:
                db.add(LLMUsage(
                    scene=(scene or self.scene or "other"),
                    model_name=model_name or self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=int(latency_ms),
                ))
                db.commit()
            finally:
                db.close()
        except Exception as e:  # 记录失败绝不阻塞主流程
            logger.debug(f"LLM usage 记录失败(忽略): {e}")

    # ── 通用重试骨架 ──
    async def _call_with_retry(self, create_kwargs: dict,
                               method_name: str) -> tuple:
        """带指数退避的 LLM 调用骨架。

        Returns:
            (response, latency_ms) 成功时
        Raises:
            最后一次异常(非 429) — 所有重试用尽后抛出
        """
        last_exc = None
        for attempt in range(_CB_RETRY_MAX + 1):
            try:
                _t0 = time.perf_counter()
                response = await self.client.chat.completions.create(
                    **create_kwargs
                )
                _latency_ms = (time.perf_counter() - _t0) * 1000
                return response, _latency_ms
            except Exception as e:
                if _is_rate_limit_error(e):
                    self._cb.mark_429()
                    logger.warning(
                        f"429 限流(service={self.scene}), "
                        f"冷却 {self._cb.cooldown}s"
                    )
                    raise  # 让调用方捕获并降级
                if _is_retryable_error(e) and attempt < _CB_RETRY_MAX:
                    delay = _CB_BASE_DELAY * (2 ** attempt) + random.random() * 0.5
                    logger.warning(
                        f"{method_name} 失败(第{attempt+1}/{_CB_RETRY_MAX}次重试), "
                        f"{delay:.1f}s 后重试: {e}"
                    )
                    await asyncio.sleep(delay)
                    last_exc = e
                    continue
                last_exc = e
                break

        logger.error(
            f"{method_name} 调用失败(已重试 {_CB_RETRY_MAX} 次): {last_exc}"
        )
        raise last_exc  # type: ignore[arg-type]

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        images: list[str] | None = None,
        temperature: float | None = 0.4,
    ) -> str:
        """
        调用 LLM 获取文本回复。

        Args:
            system_prompt: 系统提示词
            user_content: 用户输入内容
            images: 图片路径列表（用于多模态，可选）
            temperature: 生成温度
        """
        # ── 限流检查: 令牌桶耗尽 → 优雅降级 ──
        if not self._cb.acquire():
            logger.warning(f"LLM 限流(service={self.scene}), 令牌桶耗尽")
            return "AI 服务暂时不可用（限流），请稍后重试"

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # 构建 user message
        if images:
            content_parts = [{"type": "text", "text": user_content}]
            for img_path in images:
                img_data = self._encode_image(img_path)
                if img_data:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_data}"}
                    })
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": user_content})

        create_kwargs = {"model": self.model, "messages": messages}
        if temperature is not None:
            create_kwargs["temperature"] = temperature

        try:
            response, _latency_ms = await self._call_with_retry(
                create_kwargs, "chat"
            )
            # 记录 token 用量
            if response.usage:
                self.total_tokens_used += response.usage.total_tokens
                logger.debug(
                    f"Token usage: {response.usage.prompt_tokens} + "
                    f"{response.usage.completion_tokens} = {response.usage.total_tokens}"
                )
                self._log_usage(None, self.model, response.usage, int(_latency_ms))

            return response.choices[0].message.content or ""

        except Exception as e:
            # 429 已被 _call_with_retry 标记冷却, 在这里优雅降级
            if _is_rate_limit_error(e):
                return "AI 服务暂时不可用（429 限流），请稍后重试"
            logger.error(f"AI 调用失败: {e}")
            raise

    async def chat_multi(
        self,
        messages: list[dict],
        temperature: float = 0.4,
    ) -> str:
        """
        多轮对话：传入完整 messages 列表。

        Args:
            messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
            temperature: 生成温度
        """
        # ── 限流检查: 令牌桶耗尽 → 优雅降级 ──
        if not self._cb.acquire():
            logger.warning(f"LLM 限流(service={self.scene}), 令牌桶耗尽")
            return "AI 服务暂时不可用（限流），请稍后重试"

        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        try:
            response, _latency_ms = await self._call_with_retry(
                create_kwargs, "chat_multi"
            )
            if response.usage:
                self.total_tokens_used += response.usage.total_tokens
                logger.debug(
                    f"Token usage: {response.usage.prompt_tokens} + "
                    f"{response.usage.completion_tokens} = {response.usage.total_tokens}"
                )
            return response.choices[0].message.content or ""
        except Exception as e:
            if _is_rate_limit_error(e):
                return "AI 服务暂时不可用（429 限流），请稍后重试"
            logger.error(f"AI 多轮对话调用失败: {e}")
            raise

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.4,
    ):
        """带 tool use 的对话调用，返回原始 message 对象。"""
        # ── 限流检查: 令牌桶耗尽 → 优雅降级 ──
        if not self._cb.acquire():
            logger.warning(f"LLM 限流(service={self.scene}), 令牌桶耗尽")
            from types import SimpleNamespace
            return SimpleNamespace(
                content="AI 服务暂时不可用（限流），请稍后重试",
                tool_calls=None,
            )

        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
        }

        try:
            response, _latency_ms = await self._call_with_retry(
                create_kwargs, "chat_with_tools"
            )
            if response.usage:
                self.total_tokens_used += response.usage.total_tokens
                self._log_usage(None, self.model, response.usage, int(_latency_ms))
            return response.choices[0].message
        except Exception as e:
            if _is_rate_limit_error(e):
                from types import SimpleNamespace
                return SimpleNamespace(
                    content="AI 服务暂时不可用（429 限流），请稍后重试",
                    tool_calls=None,
                )
            logger.error(f"AI tool use 调用失败: {e}")
            raise

    async def chat_with_tools_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.4,
    ):
        """带 tool use 的流式调用(2026-08-23 U1 真流式)。

        单次调用同时完成: 边流式产出正文 delta, 边累积工具调用;
        产出事件:
        - ("delta", str): 正文增量(最终回答直接由这些 delta 拼接, 无需二次调用)
        - ("tool_calls", message): 模型本轮返回了工具调用(累积完整后产出一次)
        """
        import time as _t

        _t0 = _t.perf_counter()
        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "stream": True,
        }
        try:
            stream = await self.client.chat.completions.create(**create_kwargs)
        except TypeError:
            # 兼容不支持 stream_options 的服务商: 去掉该参数重试
            create_kwargs.pop("stream_options", None)
            stream = await self.client.chat.completions.create(**create_kwargs)

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        usage = None
        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    yield "delta", delta.content
                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = tc.index
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    if getattr(tc.function, "name", None):
                        slot["function"]["name"] = tc.function.name
                    if getattr(tc.function, "arguments", None):
                        slot["function"]["arguments"] += tc.function.arguments
        finally:
            _latency_ms = (_t.perf_counter() - _t0) * 1000
            if usage:
                self._log_usage(None, self.model, usage, int(_latency_ms))

        if tool_calls:
            # 复用 chat_with_tools 返回的 message 结构(简化: 用 SimpleNamespace 兼容下游)
            from types import SimpleNamespace

            msg = SimpleNamespace(
                content="".join(content_parts) or None,
                tool_calls=[
                    SimpleNamespace(
                        id=tc["id"],
                        type="function",
                        function=SimpleNamespace(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                    for tc in sorted(tool_calls.values(), key=lambda x: x["id"])
                ],
            )
            yield "tool_calls", msg

    async def list_models(self) -> list[str]:
        """通过 OpenAI 兼容的 /v1/models 拉取可用模型 id 列表。"""
        resp = await self.client.models.list()
        return sorted(m.id for m in resp.data)

    def _encode_image(self, image_path: str) -> str | None:
        """将图片文件编码为 base64"""
        path = Path(image_path)
        if not path.exists():
            logger.warning(f"图片不存在: {image_path}")
            return None
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")


# ──────────────── 统一 LLM 配置中心 (2026-08-12) ────────────────
# 所有 AI 使用点(对话/Agent/裁判/报告/自检)统一经 get_model_for_scene 解析模型、
# build_system_prompt 组装 system prompt(含用户交易风格画像注入)。

# 画像注入节流(与 src/web/api/chat.py 口径一致, 独立实现避免循环依赖):
# profile_text 截断 + rules 只取前 N 条, 避免每次调用占过多 token
_SHADOW_PROFILE_TEXT_MAX = 300
_SHADOW_PROFILE_RULES_MAX = 3


def _build_shadow_profile_block(profile_json) -> str:
    """从 users.shadow_profile_json 构建精简版画像注入文本(无画像返回空串)。

    格式与 chat.py 的 _build_shadow_profile_block 保持一致:
    profile_text 截断300字 + 规则前3条 human_text + 偏好市场 + 典型持仓中位/P75。
    """
    if not profile_json or not isinstance(profile_json, dict):
        return ""
    parts: list[str] = []

    profile_text = (profile_json.get("profile_text") or "").strip()
    if profile_text:
        if len(profile_text) > _SHADOW_PROFILE_TEXT_MAX:
            profile_text = profile_text[:_SHADOW_PROFILE_TEXT_MAX] + "…"
        parts.append(f"画像: {profile_text}")

    rules = profile_json.get("rules") or []
    if rules:
        rule_lines = []
        for rule in rules[:_SHADOW_PROFILE_RULES_MAX]:
            if isinstance(rule, dict) and rule.get("human_text"):
                rule_lines.append(f"- {rule['human_text']}")
        if rule_lines:
            parts.append("交易规则:\n" + "\n".join(rule_lines))

    preferred_markets = profile_json.get("preferred_markets") or []
    if preferred_markets:
        parts.append("偏好市场: " + ", ".join(str(m) for m in preferred_markets))

    holding_days = profile_json.get("typical_holding_days")
    if holding_days:
        if isinstance(holding_days, (list, tuple)) and len(holding_days) == 2:
            parts.append(
                f"典型持仓天数: 中位 {holding_days[0]} 天 / P75 {holding_days[1]} 天"
            )
        else:
            parts.append(f"典型持仓天数: {holding_days} 天")

    if not parts:
        return ""
    return "以下是用户交易风格画像(AI 参考, 用于给出更贴合的建议):\n" + "\n".join(parts)


def get_model_for_scene(db, scene: str, user=None):
    """按场景解析应使用的模型(统一 LLM 配置中心入口, 2026-08-15 加用户级授权)。

    解析优先级(有 user 时):
      ③ demo/guest 用户(username=="demo" 或 role=="guest"): 零授权, 直接 None;
      ① 用户 BYOK: 查 user_ai_services(user_id=user.id), models_json 里 scene
         匹配或 is_default 的模型 → 返回 dict {"name","model","base_url",
         "api_key","is_default"};
      ② 平台授权: users.permissions.model_access
         {"mode": "inherit"|"granted"|"deny_all", "model_ids": [AIModel.id...]}:
           - deny_all → None
           - granted: 场景绑定模型在 model_ids 内 → 用它;
             否则从 model_ids 挑(is_default 优先, id 升序兜底);
             model_ids 为空 = 显式全禁 → None
           - inherit → 走全局解析逻辑
    无 user(系统调用): 原全局解析逻辑不变(场景绑定 → 默认模型 → 模型池第一个)。

    Returns:
        dict(BYOK: {"name","model","base_url","api_key","is_default"})
        | AIModel(平台模型) | None
    """
    # ③ demo/guest 零授权(最先拦截, 即使配置了 BYOK/授权)
    if user is not None:
        username = getattr(user, "username", None)
        role = getattr(user, "role", None)
        if username == "demo" or role == "guest":
            return None

    # ① 用户 BYOK(自有服务优先于平台模型)
    if user is not None:
        byok = _resolve_user_byok(db, user, scene)
        if byok is not None:
            return byok

        # ② 平台授权校验
        model_access = _get_model_access(user)
        if model_access is not None:
            mode = model_access.get("mode", "inherit")
            if mode == "deny_all":
                return None
            if mode == "granted":
                model_ids = set(model_access.get("model_ids") or [])
                if not model_ids:
                    return None  # granted 空列表 = 显式全禁
                # 场景绑定模型在授权列表内 → 优先用它(保持平台场景编排)
                model = _resolve_global_model(db, scene)
                if model is not None and model.id in model_ids:
                    return model
                # 否则从授权列表挑: is_default 优先, 其余按 id 升序
                # (owner 授权了模型, 用户就一定能用其中之一, 不再因场景
                #  绑定不在列表而全禁 —— 2026-08-16 修复)
                from src.web.models import AIModel

                granted = (
                    db.query(AIModel)
                    .filter(AIModel.id.in_(model_ids))
                    .order_by(AIModel.is_default.desc(), AIModel.id.asc())
                    .all()
                )
                return granted[0] if granted else None
            # mode == "inherit": 继承平台默认, 落到全局解析(不校验)

    # 无 user(系统调用) / inherit: 原全局解析逻辑
    return _resolve_global_model(db, scene)


def _resolve_global_model(db, scene: str):
    """原全局解析逻辑: 场景绑定 → 默认模型(is_default) → 模型池第一个。"""
    from src.web.models import AISceneBinding, AIModel

    # 1. 场景显式绑定
    binding = (
        db.query(AISceneBinding)
        .filter(AISceneBinding.scene == scene)
        .first()
    )
    if binding and binding.model_id is not None:
        model = db.query(AIModel).filter(AIModel.id == binding.model_id).first()
        if model:
            return model
        logger.warning(
            f"场景 {scene} 绑定的模型(id={binding.model_id})已不存在, 回落默认模型"
        )

    # 2. 默认模型
    default = (
        db.query(AIModel)
        .filter(AIModel.is_default == True)  # noqa: E712
        .order_by(AIModel.id)
        .first()
    )
    if default:
        return default

    # 3. 模型池第一个(兜底)
    return db.query(AIModel).order_by(AIModel.id).first()


def _get_model_access(user):
    """从 users.permissions 提取 model_access 配置(dict 或 None)。

    约定结构: {"mode": "inherit"|"granted"|"deny_all", "model_ids": [全局AIModel.id...]}
    permissions 兼容旧 list 形态(权限点字符串数组, 无 model_access)。
    """
    if user is None:
        return None
    perms = getattr(user, "permissions", None)
    if isinstance(perms, dict):
        ma = perms.get("model_access")
        if isinstance(ma, dict):
            return ma
    return None


def _resolve_user_byok(db, user, scene: str) -> dict | None:
    """用户 BYOK 解析: user_ai_services 里 scene 匹配或 is_default 的模型。

    Returns:
        {"name", "model", "base_url", "api_key", "is_default"} | None
    """
    if db is None or user is None:
        return None
    user_id = getattr(user, "id", None)
    if not user_id:
        return None
    try:
        from src.web.models import UserAIService

        services = (
            db.query(UserAIService)
            .filter(UserAIService.user_id == user_id)
            .all()
        )
    except Exception as e:
        logger.debug(f"BYOK 查询失败 user={getattr(user, 'username', '?')}: {e}")
        return None
    if not services:
        return None

    for svc in services:
        try:
            raw = svc.models_json
            if isinstance(raw, str):
                models_json = json.loads(raw) if raw.strip() else []
            elif isinstance(raw, (list, tuple)):
                models_json = raw
            else:
                models_json = []
        except Exception:
            models_json = []
        if not isinstance(models_json, list):
            continue

        matched = None
        default_m = None
        for m in models_json:
            if not isinstance(m, dict):
                continue
            if m.get("scene") == scene:
                matched = m
                break
            if m.get("is_default") and default_m is None:
                default_m = m
        m = matched or default_m
        if not m:
            continue
        return {
            "name": (m.get("name") or svc.name or "").strip(),
            "model": (m.get("model") or "").strip(),
            "base_url": (svc.base_url or "").strip(),
            "api_key": svc.api_key or "",
            "is_default": bool(m.get("is_default", False)),
        }
    return None


def build_system_prompt(db, scene: str, base_prompt: str, user) -> str:
    """组装最终 system prompt(统一 LLM 配置中心入口)。

    - 先 base_prompt;
    - 若 user.shadow_profile_json 非空(用户上传过交割单), 追加用户交易风格画像段;
    - 无画像(或 user 为 None)时原样返回 base_prompt, 完全向后兼容。
    """
    if not base_prompt:
        return base_prompt

    profile_json = getattr(user, "shadow_profile_json", None) if user is not None else None
    if not profile_json:
        return base_prompt

    block = _build_shadow_profile_block(profile_json)
    if not block:
        return base_prompt

    logger.debug(f"场景 {scene}: 已注入用户交易风格画像")
    return base_prompt + "\n\n--- 用户交易风格画像 ---\n" + block