# 全代码审计报告 · 2026-09-15

> 基线: main @ 50b44f2 (v0.7.1)
> 范围: src/ (355 Python 文件, ~71K 行) + frontend/src (~150 TS/TSX)
> 方法: 7 维度并行审计 (安全/口径/错误处理/性能/质量/前端/依赖)

---

## 总览

| 维度 | P0 | P1 | P2 | 合计 |
|---|---|---|---|---|
| A. 安全 | 0 | 10 | 10 | 20 |
| B. 数据口径 | 5 | 12 | 3 | 20 |
| C. 错误处理 | 6 | 12 | 7 | 25 |
| D. 性能 | 6 | 9 | 5 | 20 |
| E. 代码质量 | 15 | 8 | 6 | 29 |
| F. 前端 | 3 | 7 | 10 | 20 |
| G. 依赖配置 | 3 | 8 | 5 | 16 |
| **合计** | **38** | **66** | **46** | **150** |

---

## P0 清单（必须立即修复，38 条）

### 数据口径（5 条 — 会产出错误数字）
1. `tdx_tick_parser.py:50` — tdx_tck 逐笔 vol 单位为「股」但 dark_l2 契约约定「手」，未 ÷100，明盘/暗盘分层全面失真
2. `stock_l2.py:72` — TDX snapshot Amount 未做万元→元换算（FCAmo/OpenAmo 已换，Amount 漏了）
3. `dark_flow.py:104,160,269` — 交易日/时段判定用 naive `datetime.now()`，UTC 宿主上整段错位
4. `orderbook_engine.py:86` — 盘口时段判定同样用 naive `datetime.now()`
5. `caliber.py:69` — `require_directional()` 契约已定义但生产出口从未调用，代码层红线未生效

### 错误处理（6 条 — 数据污染/服务崩溃）
6. `klines_ingestor.py:160` — K线日期解析失败 fallback 到 `datetime.now(UTC)`，脏柱写入错误时间戳
7. `market_scan_jobs.py:82` — `SessionLocal()` 后无 `finally: db.close()`，连接池泄漏
8. `quotes.py:253` — 同上，`db.close()` 在 try 块内，异常路径泄漏
9. `intraday_monitor.py:156` — `_main_intent` 整函数 `except: return "", None` 无日志，核心决策静默失败
10. `kline_collector.py:210,328,509` — ATR/MACD/形态识别三处静默失败
11. `capital_flow_collector.py:188` — 网关故障完全不可观测

### 性能（6 条 — 明显瓶颈）
12. `kline_collector.py:693` — 每次落库新建+销毁引擎
13. `klines_repo.py:34` — 每次查询新建+销毁引擎
14. `market_data.py:273` — async 端点内同步 `requests.get` 阻塞事件循环
15. `market_data.py:816` — 全市场分页拉取同步阻塞 5-8 秒
16. `context_builder.py:442` — 逐股 N+1：每只股新建 Session + 查历史 + 拉K线
17. `market_data.py:513` — 龙虎榜逐日循环最多 30 次串行外部 API

### 代码质量（15 条 — 死代码/循环依赖/超长函数）
18-25. **8 个死代码模块**: `experiment_log.py`, `hv_api_log.py`, `halts.py`, `analysis_link.py`, `signal_explain.py`, `confidence.py`, `quant_adapters.py`, `login_ratelimit.py`
26-28. **3 个循环依赖**: paper_trading engine↔notifier, dark_flow↔tick_archive, l2_event_stream↔seal_sampler
29-33. **5 个超长函数** (>220行): `refresh_entry_candidates`(370行), `get_entry_candidate_stats`(254行), `refresh_strategy_signals`(277行), `get_strategy_stats`(271行), `analyze`(378行)

### 前端（3 条 — XSS/会话劫持）
34. `Login.tsx:73` — JWT 明文写入 localStorage，XSS 可窃取会话
35. `jwt.ts:22` 等多处 — 直接 `localStorage.getItem('token')` 拼 Authorization
36. `ShadowAccount.tsx:120` — `document.write` 拼接未转义，可 XSS

### 依赖配置（3 条）
37. `docker-compose.infra.yml` — Redis 无 requirepass
38. `.env.example` — 缺 JWT_SECRET/AUTH_USERNAME/AUTH_PASSWORD
39. 5 处硬编码基础设施 IP `115.190.177.213:8100`

---

## P1 清单（高优先级，66 条）

### 安全（10 条）
- `chat.py:632` — SSRF 重定向绕过（follow_redirects=True）
- `skills_gateway.py:455` — API Key 注册完全免鉴权，可无限领取
- `logs.py:206` — DELETE /api/logs 任意 member 可清空全站日志
- `logs.py:71` — GET /api/logs 任意用户可读全站日志
- `my_ai_services.py:112` — BYOK base_url 可指向内网（认证后 SSRF）
- `market_scan.py:88,178` — 任意 member 可触发全市场扫描（DoS）
- `settings.py:124` — http_proxy 凭证明文回显
- `jobs.py:36` — 任意用户可取消系统任务
- `paper_trading.py:544` — 任意用户可触发全局扫描

### 数据口径（12 条）
- `money.py:19` — `to_dec(None)=0`，缺失当 0
- `capital_flow_collector.py:142` — 缺字段 `or 0` 兜底
- `dark_flow.py:1018` — 时段节奏检测把 None 段当 0.0
- `delta_engine.py:86` — 缺失 amt 当 0 累加
- `kline_collector.py:675` — volume 缺失 `or 0` 落库
- `fund_flow_nd.py:62` — ZLJC 单位未验证却按「手」×100
- `tdx_boards.py:354` — 板块缓存按 `date.today()` 本地日
- `market_data.py:296` — 板块资金流 `or 0.0` 参与排序
- `dark_fund_scan.py:80` — 输出未挂 CaliberTag
- `market_mainline.py:101` — 缺失 amount 归 0.0
- `capital_flow_collector.py:22` — 脏值可能被 120s TTL 缓存

### 错误处理（12 条）
- 多处 `except: pass` 无日志（intraday_monitor/base/tradingagents 等）
- `notifier.py:506` — httpx.AsyncClient 无默认 timeout
- `market_sentiment_collector.py:117` — 日期解析失败用原字符串兜底
- 审计写失败静默（settings/profile/auth）

### 性能（9 条）
- `market_scan.py:186` — 全市场逐只串行拉 K线
- `kline_collector.py:731` — `_pg_read` per-call 创建引擎
- `kline_backfill_scheduler.py:62` — backfill 每次新建引擎
- `marketdata_client.py:25` — DbConfigProvider 每次查询新建 Session
- `quotes.py:342` — async 端点内同步 urlopen
- `entry_candidates.py:2273` — 候选回验逐只拉 K线
- `news.py:61` — 加载全表 Stock 无 limit
- `wudao_mcp_client.py:131` — 三处同步 requests 无异步包装
- `orderbook_engine.py:107` — time.sleep 在可能的 async 上下文

### 代码质量（8 条）
- `_safe_float` 7 处重复实现
- `_clamp` 2 处重复
- `_in_trading_hours` 4 处重复包装
- 2 个额外循环依赖
- 2 个超长函数 (220/331 行)
- 228 个函数缺返回类型注解 (16%)

### 前端（7 条）
- `/analysis/:symbol/:date` 路由缺 PermGuard
- PermGuard fail-open（myPerms=null 时放行）
- Agents/Forecast/Opportunities/Dashboard 异步无卸载守卫
- downloadCard 用 `Bearer null`

### 依赖配置（8 条）
- .env.example 变量名与代码不一致
- Dockerfile 含 git（攻击面）
- Dockerfile.forecast chown site-packages
- promtail 挂载 docker.sock
- 主服务 8000 绑 0.0.0.0
- SIDA_SERVICE_TOKEN 默认空
- pytest 进生产镜像
- 监控栈镜像版本落后

---

## P2 清单（46 条）

略（见各维度详细报告）。主要类型：
- 魔法数字无命名常量
- TODO/FIXME 标记
- URL 未 encodeURIComponent
- console.log 生产输出
- ReactMarkdown 无 sanitize
- 缓存 key 无交易日维度
- 类型注解缺失

---

## 修复优先级建议

### 立即（本周）
1. **数据口径 5 条 P0** — tdx_tck 单位、Amount 换算、时区、require_directional
2. **DB 泄漏 2 条** — market_scan_jobs + quotes 的 Session 未关闭
3. **日志权限** — DELETE /api/logs 加 require_owner
4. **JWT 存储** — localStorage → httpOnly Cookie

### 短期（两周）
5. 性能 6 条 P0 — 引擎复用、async 阻塞、N+1
6. SSRF 防护 — chat 重定向、BYOK base_url
7. 错误处理静默失败 — 核心指标加日志
8. 8 个死代码模块删除

### 迭代（一个月）
9. 超长函数拆分
10. 循环依赖解耦
11. 重复代码统一
12. 前端异步守卫补齐
13. 依赖升级

---

## 正面发现

| 项目 | 状态 |
|---|---|
| SQL 注入 | ✅ 未发现可利用漏洞 |
| 命令注入 | ✅ 未发现 |
| 路径穿越 | ✅ 已做 realpath/白名单修复 |
| JWT 设计 | ✅ HS256 固定 + token_version 踢人 + scrypt n=2^15 |
| Docker 非 root | ✅ 全部 USER app (UID 10001) |
| 无默认密码 | ✅ 首启随机生成 + 测试守卫 |
| dangerouslySetInnerHTML | ✅ 前端未发现 |
| 硬编码 API Key | ✅ 未发现 |
| lock 文件机制 | ✅ requirements-lock + CI 守卫 |
