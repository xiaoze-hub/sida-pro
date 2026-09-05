[SIDA-Pro][全量审查][TQ-AUDIT-DONE] v0.5.5 Bug Audit 回执（commit 8803fa3）

Hermes：全量审查完成。代码获取无受限——本机 SSH deploy key 对 sida-pro 有效，已 clone 并 checkout v0.5.5 (8803fa3)，四块（后端 API / 前端 / 数据源链路 / 部署配置）全部完成源码级审查。结论：**1 个 P0 + 16 个 P1 + 30 个 P2**。P0 建议立即修（未鉴权任意文件读取，生产公网可直达）。

━━━━━━━━━━━━━━━━━━━━
## P0（阻塞，1 条）

**P0-1【server.py:1980-1993】SPA 静态路由路径穿越 → 未鉴权任意文件读取**
现象：`file_path = os.path.join(static_dir, path)` 无 containment 校验、无鉴权（该路由在 protected 路由之外）。URL 编码 `GET /..%2f..%2f.env`、`GET /..%2f..%2fserver.py` 可原样读走容器内任意文件——.env 里有 JWT_SECRET、DB 密码、AI key。经 nginx 反代同样透传。本人已复核代码证据成立。
修复：`real = os.path.realpath(os.path.join(static_dir, path))`，校验 `real.startswith(os.path.realpath(static_dir) + os.sep)` 不满足返 404。

━━━━━━━━━━━━━━━━━━━━
## P1（影响使用，16 条）

**P1-1【src/web/middleware.py:186-195 + src/web/cache/redis_client.py:141-154】限流计数器 TTL 每次请求被重置**
现象：`incr(key, ttl_seconds=60)` 内部每次请求都 `EXPIRE` 重刷 60s。Dashboard 30s 轮询即满足"相邻请求<60s"，key 永不过期、count 无限累加，累计到 300 后该用户**永远 429**（直到停手 60s）。与内存降级路径（正规窗口）行为不一致；429 请求也先 incr 继续涨。
修复：仅 count==1 时设 TTL（`pipe.expire(key, ttl, nx=True)`），或改滑动窗口。

**P1-2【src/web/api/stocks.py:285-300 / 303-314 / 360-388】reorder / PUT /{stock_id} / PUT /{stock_id}/agents 三接口缺 user_id 归属校验**
现象：三处 `db.query(Stock).filter(Stock.id==id).first()` 均无 user 过滤（对比同文件 delete_stock 已有校验）。枚举 stock_id 可重排序/改名/改写其他用户自选股的 Agent 绑定（含 AI 模型与通知渠道覆盖）。违反多用户红线。
修复：补 `or_(Stock.user_id==user.id, Stock.user_id.is_(None))`。

**P1-3【src/web/api/forecast.py:334-375】async 路由内跑同步资金流采集，阻塞全站**
现象：`async def forecast_report_push` 里直接调 `CapitalFlowCollector(...).get_capital_flow(symbol)`（同步外网 HTTP，超时可数十秒），期间全站请求含健康检查全部卡死。chat.py 同类调用已用 asyncio.to_thread，此处漏了。
修复：`await asyncio.to_thread(...)` 包裹。

**P1-4【src/core/notify_center.py:142-154】通知任务引用未保存 + 事件循环误用**
现象：`loop.create_task(_async_push(...))` 返回的 task 未存任何集合，可能被 GC 中途回收 → 推送静默丢失、push_status 永远 pending；`asyncio.get_event_loop()` 在 3.11 运行 loop 中已弃用。
修复：模块级 set 持有 task + add_done_callback(discard)；统一走 push_notification_async。

**P1-5【frontend/src/pages/Settings.tsx:427】通知渠道列表调了不存在的接口，列表永远为空**
现象：`fetchAPI('/notify/channels')`——后端渠道路由挂载在 `app.py:366` prefix="/api/channels"（channels.py:71），全后端无 /api/notify/channels。`Promise.allSettled` 吞掉 404 → setChannels([])。已有渠道无法查看/启停/删除，"0/0 渠道启用"恒为 0，无任何报错极难察觉。同文件其余 CRUD（957/959/963/992 行）都用对了 /channels/...，仅列表路径错。
修复：改 `fetchAPI('/channels', { cacheMode: 'reload' })`。

**P1-6【frontend/src/pages/Settings.tsx:605-612 + 424】设置保存后界面 30 秒不刷新（v0.5.5 验收点失效）**
现象：GET /settings 走 client.ts 30s 内存缓存，PUT 不做缓存失效；handleSave 后调的 load() 返回保存前的旧快照。toast 提示"已保存"但界面仍显旧值，用户会重复保存或误判失败。同页其它请求都传了 cacheMode:'reload'，唯独 /settings 漏了。
修复：`fetchAPI<Setting[]>('/settings', { cacheMode: 'reload' })`，或 PUT 成功后按前缀失效 GET 缓存。

**P1-7【frontend/src/pages/Settings.tsx:223 + 2549-2554 + 599；src/web/api/settings.py:211-232】ths_sdk_password 可被掩码值写回，静默毁掉同花顺正式账户凭证**
现象：后端 GET 对 ths_sdk_password 回显掩码 "********"，但前端 SECRET_SETTING_KEYS（'wudao_mcp_token','zhitu_token','tdx_api_key'）**不含 ths_sdk_password**：a) 系统区块里它走普通文本框输入过程明文；b) Dialog 预填 currentValue='********'，用户微调后保存把字面 8 个星号 PUT 进 DB（后端 PUT 无掩码拒收），SDK 登录失败全量降游客、UI 仍显示"已配置"；c) 顶栏"X/3 已配"与后端 5 个 secret key 数不符。
修复：前端集合补 ths_sdk_password 并移入密码弹窗（type=password+Eye+默认空=不改）；后端 PUT 对 secret key 值==掩码时 400 拒收兜底。

**P1-8【frontend/src/pages/Stocks.tsx:781-824】WebSocket 卸载后无限重连**
现象：cleanup 里 ws.close() 异步触发 onclose，此时 cleanup 已结束，onclose 里 setTimeout(connect,5000) 重新挂上且无人再清理；connect 闭包持有建立时的旧 token。离开页面后仍每 5s 建连，登出后带过期 JWT 死循环重连直到刷新整页。
修复：effect 内 `let closed=false`，cleanup 置位并在 connect/onclose 检查；或按 event.code 1006/4401 不重连。

**P1-9【frontend/packages/biz-ui/src/components/InteractiveKline.tsx:330-362、394-428；frontend/src/pages/L2Orderbook.tsx:89-111】快速切股 race condition：旧请求覆盖新数据**
现象：loadMinute/load 及 L2Orderbook 的 Promise.all 均无 cancelled/序号守卫（对照 KlineChart.tsx:372-420 有守卫）。分时接口冷启动可达 15s、timeout 放宽到 60s，切股 A→B 时 A 的慢响应晚到把 A 的分时/盘口/L2 数据画到 B 的图上。决策终端显示错标的数据属决策级错误。usePolling.ts 的 load 同理。
修复：仿 KlineChart 加 cancelled 守卫或请求序号 ref，只接受最新序号响应。

**P1-10【frontend/packages/biz-ui/src/components/deep-analysis-modal.tsx:38-42 + stock-insight-modal.tsx:2563-2567、2740-2760】决策弹窗残留买绿卖红/涨绿跌红（违反配色红线）**
现象：DECISION_COLOR buy=emerald/sell=rose；stock-insight 内部自相矛盾（2069 行买量柱=红 而 2564 行 buy=绿；2740 行正收益=绿，与 Opportunities.tsx:1735 负值=绿正好相反）。是 v0.4.70/71 配色统一漏网之鱼，出现在 TradingAgents 深度分析与个股洞察两个决策弹窗。
修复：统一改 text-stock-up/text-stock-down 或 readGsColors() 令牌。

**P1-11【data_source/thsdk_l2.py:1291-1299】v0.5.5"凭证 30s 生效"对单例消费者失效**
现象：_get_default_client() 进程级单例，凭据在首次构造时冻结；30s TTL 只对新实例生效。而 tools_thsdk.py:89-92（11 个对话工具）、auction_pool.py:223、mainflow_tri.py:45、main_flow_compare.py:158 全走单例。容器游客态冷启动后，设置页填好账号，这些链路**重启前永远游客**（扩展1/港美/大单恒 0 行）。
修复：单例按凭据指纹缓存（key=(username,password) 变化重建），或 _build_config() 每次走 resolve_ths_creds()。

**P1-12【src/core/dark_l2.py:304-307】_query_thsdk 只读 env，无视 v0.5.5 设置页凭证**
现象：`_os.environ.get("THS_USERNAME")` 不设置就 raise。同文件 _fetch_raw_rows(128-129) 走 THSDKL2()（resolve_ths_creds 链），但 thsdk_big_order 源走这条 env-only 路径。生产若只按 v0.5.5 主推方式在设置页配凭证，thsdk_big_order 永远失败静默回退，与同文件逐笔源凭证来源割裂。
修复：_query_thsdk 改用 resolve_ths_creds()。

**P1-13【src/collectors/capital_flow_collector.py:123,158 vs packages/marketdata/.../tencent_fundflow.py:87-89】main_net_inflow_pct 跨源单位差 100 倍（违反单位红线）**
现象：东财/网关路径 `/100.0` 存成小数（12.34%→0.1234），腾讯路径原样透传整数（12→12%）。消费端 get_capital_flow_summary(283-295) 按 >10/>5 百分数阈值判断——走东财源时任何真实占比都判成"小幅流入/流出"，影响 AI/策略提示语。
修复：统一百分数口径并在 types.CapitalFlow 注明单位。

**P1-14【deploy/deploy_panwatch.sh:21,156-171；docker-compose.yml:102,107-119】部署配置与宣称拓扑三方脱节（ACR/PG/Redis 三条链路生产是摆设）**
现象：①deploy 脚本和 compose 写死 ghcr 镜像，CI 推的 ACR 镜像（build-push-acr.yml:67-68）全仓无任何引用——"ACR→小主机拉镜像"无落地物；②全仓只有 database.py:22 读 SIDA_DB_URL，compose/deploy/.env.example 均未设置且无 PG 服务 → 生产实际静默跑 SQLite，hypertable 链路是死路径（失败被 except 吞成 debug）；③手工 docker run 的 panwatch 不在 panwatch-net、未传 REDIS_URL → biz_cache 连 localhost:6379 恒失败，Redis L2/限流/调度选主静默降级，prometheus.yml 抓 panwatch:8000 恒失败。
修复：deploy 改 ACR 地址；compose 加 PG 并设 SIDA_DB_URL；panwatch 入网并注入 REDIS_URL。

**P1-15【deploy/deploy_panwatch.sh:148-155；.env.example:27；server.py:688-690；src/core/dark_l2.py:8】真实密钥硬编码入库**
现象：ALPHAVANTAGE_KEYS/TWELVEDATA_KEYS 真实 key 作为脚本默认值；.env.example 提交真实智兔 ZHITU_TOKEN UUID；server.py:688-690 DATA_SOURCE_SEEDS 明文智兔 api_keys 两枚；dark_l2.py:8 写入正式账户 ID mx_8lj4le6qd（docs 自定"禁提交仓库"）。
修复：默认值清空 fail-fast、.env.example 换占位符、源码删 ID；已泄露 key 全部轮换。

━━━━━━━━━━━━━━━━━━━━
## P2（可优化，30 条，一行一条）

后端：
1. src/web/api/settings.py:211-232 — PUT /{key} 无 key 白名单，可写 jwt_secret/allow_register 等系统键；建议白名单+保留键拒收
2. src/web/app.py:244 — RBAC 判定 JWT payload.role 优先于 DB role，降权后旧 token 最长 12h 仍高权限；建议 DB 优先或改角色递增 token_version
3. src/web/middleware.py:333-350 — 审计中间件在事件循环内同步 DB 写 + task 未保存；建议 to_thread+存集合
4. server.py:1928,2009 — 微信 BOT worker shutdown 未 cancel；WEB_WORKERS=2 与 demo 限额/内存限流的单进程假设冲突（限额按 worker 翻倍）
5. src/web/api/klines.py:244-260 — 每请求新建 create_engine；复用 database.engine
6. src/web/api/klines.py:303-327,830-849 — batch 接口绕过 PG hypertable 直连外网，与单股口径不一致
7. src/web/cache/biz_cache.py:53-140 + klines.py:32 — L1/_SUMMARY_CACHE 无容量上限，内存缓慢上涨；建议 LRU
8. src/web/log_handler.py:95-130 — ERROR 日志在事件循环线程同步 flush DB；建议只进 buffer
9. src/core/scheduler.py:87,126-180 — 调度执行路径同步 DB 调用跑在 AsyncIOScheduler loop 上，定时触发瞬间全站毛刺
10. src/web/api/auth.py:565 — PATCH /users 排除 guest，owner 无法设 guest（与 create 三值校验不一致）

前端：
11. frontend/src/pages/Quote.tsx:337-338 — dark_clusters.available 但 main_net=null 时按 0 显示"数据中性"，违反"缺失标无数据"；同页 706/712 行 chips.cost_10.toFixed 未走 safeFixed（PG DECIMAL 序列化成字符串会崩整页）
12. frontend/src/pages/Opportunities.tsx:589-706 — 轮询刷新无卸载守卫（最长 2 分钟后台跑）、load() 无序号守卫
13. frontend/src/pages/IndexDetail.tsx:145 — 指数成交量 /1e8 后标"亿"应为"亿股"（单位红线标注）
14. frontend/src/pages/Login.tsx:52 vs 147 — 初始设密校验 6 位/提示 8 位不一致
15. frontend/src/pages/IndexDetail.tsx:152-156、Quote.tsx:616-618 — SectionHeader 迁移不彻底（v0.5.2 收尾）
16. frontend/src/pages/Quote.tsx:800-801 — 资金明细表两列同名"净额(万/亿)"，应标明盘/暗盘净额

数据源与部署：
17. packages/marketdata/.../tq.py:169-173 — 北交所 920 段先命中 "9"→.SH 分支，"92"→.BJ 不可达，920 票 TQ 拿错市场；建议 92 前缀优先判 BJ
18. packages/marketdata/.../tq.py:101-128,297-308 — TQ 网关探测失败永久缓存（本进程不重试）；fetch 对非 dict 响应在 try 外直接 _parse_more_info 炸批
19. src/collectors/klines_ingestor.py:54-63 — "三源入库"实为同一条 Engine 链跑三遍，source 列（tencent/eastmoney/sina）是假溯源；96 行 CST 交易日按 UTC 午夜落库
20. src/collectors/market_http.py:55-63 + packages/.../http.py:67-75 — 节流在全局锁内 sleep，跨 host 串行化，多线程采集退化近似单线程；建议锁内只算锁外睡
21. data_source/thsdk_l2.py:338-363 — _query 的 TypeError 兼容分支在 except 内重试，再抛异常会绕过重试/熔断/统一包装
22. data_source/thsdk_l2.py:1067-1074 + 370-382 — compute_main_flow 在无效行已被过滤后的序列上 diff，被丢行增量错归下一保留行方向，主买/主卖拆分有系统偏差；建议原始累计序列先 diff 再过滤
23. src/collectors/capital_flow_collector.py:170-172 — _today_cn 用宿主本地日期（UTC 宿主 0-8 点日期错一天），应 ZoneInfo("Asia/Shanghai")；215-267 行悟道合并分支不可达（死分支）
24. data_source/thsdk_l2.py:156-162,977-988,1109-1126 — _WENCAI_CACHE/_SNAPSHOT_CACHE 无上限；复用 TTLCache(max_size)
25. src/core/dark_l2.py:131-149 — ImportError 降级分支 config 键名错（ths_username vs username），当前死代码但走到必静默变游客
26. deploy/deploy_panwatch.sh:49-91 — 热补丁清单缺 data_source/，thsdk_l2.py 改动进不了容器
27. .github/workflows/build-push-acr.yml:11-36 — 生产镜像构建无 tsc/pytest 门禁（坏 tag 可直推生产）；`REF_NAME==refs/tags/v*` 永不匹配（应用 GITHUB_REF_TYPE=='tag'），当前靠 elif 运气正确

━━━━━━━━━━━━━━━━━━━━
## 红线对照（专项核过）

- 单位口径：逐笔 dark_flow 用 tick 自带 amt/vol 无自换算，无错配；违规点=P1-13（资金流占比）+ P2-13（指数成交量表注）
- 缺失显式"无数据"：klines/暗盘/L2 均符合；违规点=P2-11（Quote 数据中性）
- 主力意图走 get_main_intent：chat 工具层严格区分、darkflow 逐笔口径，forecast 用东财口径仅作资金流注入字段不作主力意图结论，未违规
- K线 PG 优先：单股符合，batch 缺口=P2-6
- Redis 走 biz_cache：全仓无违规裸连（scheduler_leader 分布式锁可接受）
- user_id 隔离：notifications/alerts/subscriptions/suggestions/history/accounts 完整，缺口=P1-2
- v0.5.2-0.5.5 变更面：扫码下线收尾干净（前后端无 ths_auth/qrcode 残留；Settings.tsx 的 QRCode 是微信 iLink 绑定走 /notify/wechat-bind/*，正常功能）；SectionHeader/ThsAccountCard 主体正确；resolve_ths_creds 本身实现正确（显式参数>DB>env，30s TTL，掩码正确）——但生效链路有两处破坏=P1-11/P1-12，验收点刷新失效=P1-6

## 修复优先级建议
1. 立即：P0-1（一行 realpath 校验，可 hotfix）
2. 发版前：P1-5/P1-6/P1-7（v0.5.5 自身验收点+凭证安全）、P1-11/P1-12（30s 生效承诺）、P1-1/P1-2（误限流+跨用户写）
3. 其余 P1 排期，P2 批量攒 commit

—— TianXiang (28号, zcode19645928)
