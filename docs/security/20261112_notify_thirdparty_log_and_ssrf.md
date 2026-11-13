# 通知外发: 第三方日志凭据泄漏 / 假成功 / 渠道 SSRF(2026-11-12 复审六)

涉及代码: `src/core/notifier.py`、`src/core/third_party_log_guard.py`(新)、
`src/core/net_guard.py`(新)、`src/core/notify_errors.py`、`src/core/notify_center.py`、
`src/web/api/notifications.py`、`src/web/api/wechat_bind.py`、`src/web/api/channels.py`、
`src/bootstrap/env.py`。

## 1. 第三方库把渠道凭据打进日志(已修)

### 现象(实测)

```
apprise|DEBUG|Bark Payload: {'device_key': 'barkdev_...', 'title': 't', 'body': 'b', ...}
apprise|DEBUG|Bark POST URL: http://host/push (cert_verify=True, encrypted=False)
apprise|ERROR|Unparseable Telegram URL tgram://B...Z/123/
```

`apprise` 不在 `_TransportNoiseFilter` 的噪音名单里, 所以这些 **DEBUG 会原样进
`log_entries`(DB 日志表)** 和控制台 —— 令牌落到另一个数据库, 可被「错误日志」页签
导出/截图/工单带出。bark 的 `device_key`、Telegram bot token 都在正文里。

另外 `src/web/api/wechat_bind.py` 有一处更直接的泄漏:

```python
logger.error(f"iLink 扫码确认但凭证不完整: {result}")   # result 含完整 bot_token
```

### 修法: logger 层整体替换, **不是**调级别

`src/core/third_party_log_guard.py` 用一个全局 `LogRecord` 工厂把
`apprise` / `httpx` / `httpcore` / `urllib3` 的**正文整体替换**成:

```
[apprise] suppressed level=WARNING category=apprise ref=ERR-XXXXXXXX
```

- 记录本身保留(级别、来源、时间), 只是没有任何原文进入 handler;
- `record.args` / `exc_info` / `exc_text` / `stack_info` 一并清掉(httpx 的异常文本
  里就是完整 URL);
- `third_party_log_scope(ref=..., category=...)` 把**本轮外发**的关联号绑上去,
  与 `push_error` 里的「参考号」对齐;
- 不选级别过滤的理由: WARNING/ERROR 正文同样带 URL(如
  `A Connection error occurred sending Bark notification to ...`), 而且第三方升版把
  凭据打到 INFO 后策略会静默失效; 级别过滤还会把"出错了"这个事实一起丢掉。

实测: 修复前后 apprise 产生的**记录条数相同**(10 条), 但泄漏行从 1 条降到 0 条。

## 2. HTTP 200 被当成"已送达"(已修)

`_send_hermes` 旧逻辑:

```python
try: data = resp.json()
except Exception: data = {}
if data.get("status") not in (None, "delivered", "ok"):
    raise NotifyFailure("not_delivered", ...)
```

`data = {}` 让 `status is None` 命中"成功"分支 → **网关前面挂的登录页/反代错误页/
维护页(HTTP 200 + HTML)** 被写成 `push_status=sent`。现在按协议三态判定:

| 回包 | 判定 |
|---|---|
| 非 2xx | 失败(`auth_rejected` 401/403 / `http_rejected`), **先于**正文解析 |
| 200 + 非 JSON / 非对象 | 失败 `bad_response`(没有送达回执) |
| 200 + `status/ok/delivered/success/accepted` 明确肯定 | 成功 `sent` |
| 200 + 明确否定 | 失败 |
| 200 + 无任何可识别 ack(如 `{}`) | **未知** → 失败 `not_delivered`(「渠道未确认送达」) |

其它渠道也补了"HTTP 错误优先"(telegram/wecom/serverchan), 并把
`JSONDecodeError` 在 `classify()` 里排在 `ValueError` 之前 —— 否则 403 的 HTML 会被
判成「渠道配置不完整」, 排障方向完全错。

顺带修掉一个纯假成功: `_send_custom` 遇到未知渠道类型只 `logger.warning` 然后返回
`None`, 上层把"一条都没发"写成 `sent`; 现在抛错 → 如实写 failed。

## 3. 历史已存通知的泄漏(只读处理, 不改数据)

修复前 `notifications.push_error` / `push_channels` 里可能已经有 token/URL。
这些行是既成事实, **没有**做原地清洗、删除或改写; 只在出口
(`GET /api/notifications`、`GET /api/notifications/{id}`、
`GET /api/notifications/{id}/status`)按**形态白名单**收敛:

- 认得出的「固定类别 + 关联号」和站内自有原因("未配置通知渠道"等)原样保留;
- 认不出的文本替换成「历史推送失败（内部错误）；参考号: ERR-XXXXXXXX」;
- `push_channels[]` 只放行 `id/name/type/status/success/error/delivered_at/failed_at/receipt`,
  其余键(历史里可能存了整段异常上下文)丢弃。

代价: 历史行的具体失败原因在页面上变成兜底文案(原文可能就是把凭据打进了通知中心)。
这是有意的取舍; 排障看服务端日志。

## 4. 渠道 SSRF(已加最小防护, 并保留合法内网部署)

`notify_channels.config` 里有三个**用户可写**的任意 URL: `hermes.webhook_url`、
`bark.server_url`、`wechat_ilink.base_url`(member 可建/改自己的渠道)。不校验时,
一个登录用户就能让服务端去打云元数据(169.254.169.254)、同机管理面或内网主机。

`src/core/net_guard.py`:

1. **解析即校验**: 任一 A/AAAA 落在回环/私网/链路本地/保留/组播/未指定 → 拒绝;
   `::ffff:127.0.0.1` 先拆成 IPv4 再判; 元数据域名(`metadata.google.internal` 等)
   额外按域名拦;
2. **连接到已校验的 IP(pinning)**: URL host 换成通过校验的 IP, 显式带 `Host` 头 +
   `extensions={"sni_hostname": 原域名"}`(httpcore 1.x 用它做 TLS SNI 与证书校验)。
   **同一请求只解析一次 DNS**, 校验用的地址就是连接用的地址 —— 这才是 rebinding 的
   可靠解法; 也**不跟随重定向**(302 → 内网是经典绕过)。经代理的 HTTPS 是本条的例外,
   见 §5.1(out-of-band 明确拒绝, 不静默降级);
3. **自建 Bark 不再经过 apprise**: apprise 自己解析域名, 我们无法把连接固定到校验过的
   IP, 所以 `server_url` 非空时改由本模块直连(`POST {server}/push`, 协议与 apprise 一致,
   语义不变); 默认 `api.day.app` 仍走 apprise(host 固定, 非用户输入);
4. **iLink `base_url`**: 其 HTTP 栈在 `src/core/wechat_ilink.py` 内部(无法 pinning),
   因此用**域名后缀白名单**(`*.weixin.qq.com`)—— 固定域名不存在 rebinding 问题;
5. **代理字段**: 只拦元数据/链路本地/保留段(`policy="no_metadata"`), 放行
   127.0.0.1/私网 —— 本机 clash、局域网 squid 是合法常态, 一刀切会打死正常渠道。

### 部署兼容性(重要)

自建 Hermes 网关 / Bark 常在同机或局域网, 默认会被拦。**运维**用环境变量显式放行
(不是让用户在渠道配置里自己勾, 否则防护形同虚设):

```bash
SIDA_NOTIFY_PRIVATE_HOSTS=127.0.0.1,::1,hermes-gw.internal,192.168.1.0/24
SIDA_NOTIFY_ILINK_HOSTS=ilink.test.local     # 仅当 iLink 用了非腾讯域名
```

- 支持主机名(精确、不分大小写)/ IP / CIDR; `*` 表示全放开(不建议);
- 放行按目标生效: 放行 `127.0.0.1` 不会顺带放开 `10.x`;
- 被拦时**不是静默失败**: 渠道初始化/发送会写 `push_status=failed` +
  「目标地址不被允许(内网/本地需运维显式授权)」+ 关联号, `POST /api/channels`
  返回 400 同文案, 服务端日志有 `category=blocked_target`。

### 影响与残余风险

- 升级后若某渠道指向内网且未配置上面的环境变量, 该渠道会**开始失败**(带明确文案 +
  日志), 不会静默 —— 按提示加放行即可恢复;
- 自建 Bark 从 apprise 改成本模块直连: 协议一致(`POST {server}/push`), 返回码判定
  `code == 200`; 若自建服务器只支持 GET 路径式旧接口, 需升级服务器;
- Hermes 网关若回 200 但**没有任何 ack 字段**, 现在会显示「渠道未确认送达」
  (消息可能仍然送到了, 只是无法证明)。要恢复 `sent`, 让网关回
  `{"status": "delivered"}` 或 `{"ok": true}`;
- 代理字段只拦元数据段: member 仍可把代理指向本机端口做**盲探测**(只能看到
  类别/失败, 拿不到回包内容)。要彻底收敛需把渠道写权限收紧到 owner, 属权限模型
  变更, 未在本次改动范围;
- `wechat_ilink` 的 `base_url` 走域名白名单而非 IP 固定: 若腾讯把 iLink 换到非
  `weixin.qq.com` 域名, 需要运维加 `SIDA_NOTIFY_ILINK_HOSTS`(会以
  「目标域名不在允许列表」明确报出)。
- 部署了 env `HTTPS_PROXY`/`ALL_PROXY` 时, 自建 Hermes / 自建 Bark 这类**https 目标**会以
  「HTTPS目标经代理时无法固定地址」失败(见 §5.1): 把目标域名加进 `NO_PROXY` 即恢复;
  选它而不是"悄悄放弃 pinning"或"悄悄绕过代理"。
- socks 代理需要 `socksio`; 未安装时该渠道报「代理不可用」(`proxy_failed`), 不再是
  「内部错误」。

## 5. 兼容性与错误归类收口(2026-11-13 复审七)

上一轮的防护有五处会**误伤合法配置**或**把排障带错方向**, 本次逐条收口
(`tests/test_notify_compat_guards.py`):

### 5.1 HTTPS + 代理: 明确拒绝, 不静默降级

httpcore 1.0.x 的 CONNECT 隧道 (`_async/http_proxy.py::AsyncTunnelHTTPConnection`) 不读
`sni_hostname`, 写死用 `remote_origin.host` 做 SNI/证书校验。pinning 后这个 host 是 IP
→ 无 SNI、证书按 IP 校验 → 必然 `IP address mismatch`。实测 env `HTTPS_PROXY` 存在时:

- 放弃 pinning 让代理解析 → rebinding 窗口重新打开(我们验不到代理侧解析);
- 静默跳过代理直连 → 偷偷改出口路由(只有代理能出网的部署莫名失败)。

两种都不做: `ProxyPinUnsupported` → 类别 `pinned_proxy_unsupported`
(「HTTPS目标经代理时无法固定地址(改用直连或NO_PROXY)」)。判定直接用 httpx 自己的
`_transport_for_url` + mounts, 所以 `NO_PROXY` 命中该域名时走直连、照常 pinning + SNI。
明文 `http://` 目标经转发代理不受影响(代理不解析目标, 我们发的就是 IP)。

### 5.2 URL userinfo 不再静默丢失

`https://user:pass@host/hook`: 之前 `urlsplit` 之后只用 host/port 重建 URL, userinfo 就
消失了 —— 设置页保存成功, 请求不带凭证(服务端 401), 没有任何报错。现在转成
`Authorization: Basic ...` 显式带上(调用方已给 `Authorization` 时以调用方为准), 明文只活在
`GuardedTarget.auth_header`(`repr=False`), 不进 URL、不进日志。含 CR/LF/NUL 的 userinfo
是头注入 → `ValueError` → `config_invalid`(不说成"目标被拦")。

### 5.3 DNS 解析失败: 独立类别, 且不拦保存

| 场景 | 旧口径 | 现口径 |
|---|---|---|
| 目标域名解析不到(发送时) | `network_unreachable`(「网络连接失败」) | `dns_failed`(「目标域名解析失败」) |
| 目标域名解析不到(配置保存) | 400「渠道配置不完整或无效」 | **放行保存**, 发送/测试时如实报 `dns_failed` |
| 代理域名解析不到 | `network_unreachable` | `proxy_failed`(「代理不可用」) |

配置期放行是**有意**的: 内网 split-DNS、域名刚建、解析器抖动都会让"保存看运气"; 安全决策
在连接那一刻(`guarded_request` 再校验一次), 那时解析到内网照样被拦, 防护强度不变。

### 5.4 历史错误的参考号不再每次随机

`public_saved_error` / `public_failure_text` 的兜底文案原先调 `new_error_ref()`(uuid):
同一条只读收敛的历史行每读一次换一个号, 且那个号在服务端日志里根本不存在。现在由
**内容哈希**派生(`stable_error_ref`): 同一行永远同一个号, 相同文案共用(便于按号聚合),
哈希单向、拿号推不出原文。

### 5.5 socks 代理不再绕过校验

旧代码 `if proxy.lower().startswith(("http://", "https://"))` —— `socks5h://169.254.169.254:1080`
整条绕过, 且 httpx 因缺 `socksio` 抛 ImportError 后被归成「内部错误」。现在
`validate_proxy_target` 按 URL 解析: scheme ∈ {http, https, socks4, socks4a, socks5,
socks5h}, 主机沿用 `policy="no_metadata"`(本机/局域网代理合法, 元数据/链路本地/保留/组播拒),
其它协议 → `config_invalid`, socks 不可用(缺 socksio) → `proxy_failed`。

## 6. 测试

新增(全部 `tmp_path` 真实 SQLite + `127.0.0.1` 临时 HTTP 服务/mock 传输, 不访问
生产或真实渠道, 凭据用**非占位形态的假值**):

- `tests/test_notify_thirdparty_log_leak.py`: 真实 apprise 打 bark device_key /
  非法 telegram token / exc_info+args / httpx+urllib3 URL 行 → 日志零泄漏且记录不丢;
  微信绑定成功/凭证不完整/`base_url` 不可信/轮询异常 → 日志与响应都不带 bot_token。
- `tests/test_notify_delivery_honesty.py`: 200+HTML、空 body、纯文本、JSON 数组、
  `{}`、`{"status":"delivered"}`、`{"ok":true}`、显式否定、403+HTML; 历史行只读脱敏
  且数据库原值不变; 未知渠道类型不再假成功。
- `tests/test_notify_ssrf_guard.py`: 元数据/回环/IPv4-mapped IPv6/私网/非法协议、
  放行表(主机/CIDR)、多 A 夹带私网、pinning(只解析一次 + Host 保留)、不跟随 302、
  Bark 自建与 Hermes 配置期拦截、iLink 域名白名单、代理只拦元数据段。
- `tests/test_notify_compat_guards.py`: userinfo → Basic 头(线上真收到 + repr/URL 无线索)、
  CRLF 头注入 → `config_invalid`、HTTPS+env 代理 → `pinned_proxy_unsupported`
  (`NO_PROXY` 命中不拒)、http 经代理仍 pinning、DNS 失败 → `dns_failed` 且不拦保存、
  代理 DNS 失败 → `proxy_failed`、socks(socks4/5/5h)指元数据被拦而本机 socks 放行、
  历史行的参考号两次读出一致(`public_saved_error` / `_to_out`)。
