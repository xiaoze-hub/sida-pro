# 已知问题 / Known Issues

> 全仓"已知未修问题"的唯一台账, 编号 KI-001 起递增, 永不复用。
> 每条必须带齐 8 字段: `id` / `level`(P0-P3) / `owner`(人或 `unassigned`) / `发现日期` / `现象` / `影响` / `涉及文件` / `建议修复`。
> 约定: 问题修复后把本条移入 CHANGELOG 对应日期段(只进不出); 自 2026-09-09(W4.3/F5)起, "未做/待办/已知限制"类信息只登记在这里, CHANGELOG 不再散落。
> 台账来源: CHANGELOG 全文扫描迁入(W4.3/F5) + W2.5/W2.6 既有两节收编编号 + 方案 §4.3 点名项。对账说明见文末。

## 级别定义

| 级别 | 定义 |
|---|---|
| P0 | 数据错误或安全风险, 立即处理 —— 当前无 |
| P1 | 运行时可触达(安全/功能/生产稳定性)或有硬期限, 必须带 owner 与期限 |
| P2 | 技术债: 影响面可控(构建链/测试链/局部功能)或有明确触发条件, 排期处理 |
| P3 | 留痕记录/接口先行未接 UI/低风险接受项, 触发时再评估 |

## 总览

| ID | 级别 | 标题 | 发现 | Owner |
|---|---|---|---|---|
| KI-001 | P1 | react-router-dom 开放重定向→XSS(唯一运行时可触达) | 2026-09-09 | TianXiang |
| KI-002 | P2 | rollup 任意文件写/路径穿越(仅构建链) | 2026-09-09 | TianXiang |
| KI-003 | P2 | vite dev server fs.deny 绕过(跨大版本升级) | 2026-09-09 | TianXiang |
| KI-005 | P2 | forecast 4g 限额沿用既定值, 未按实测推理峰值校准 | 2026-09-09 | TianXiang |
| KI-006 | P2 | 构建/测试链传递依赖已知漏洞 18 条(不进产物) | 2026-09-09 | TianXiang |
| KI-007 | P2 | forecast_lib 交易日按 weekday 计(法定节假日偏晚) | 2026-09-09 | TianXiang |
| KI-008 | P1 | chat_upload 上传文本直喂 LLM(提示注入面) | 2026-09-08 | unassigned |
| KI-009 | P3 | chat.py f-string 拼 SQL(已核实安全, 留痕) | 2026-09-09 | unassigned |
| KI-010 | P2 | ShadowAccount window.open 兜底链接 401(缺鉴权头) | 2026-09-08 | TianXiang |
| KI-011 | P2 | 主板 K 线"不可能缺口"残留 10 条(vendor 源头级) | 2026-09-09 | TianXiang |
| KI-012 | P3 | 港美市场无交易日历, 盘中判定维持周末口径 | 2026-09-09 | unassigned |
| KI-013 | P3 | forecast 容器 PANWATCH_DB 指向不存在的 sqlite 文件 | 2026-09-08 | TianXiang |
| KI-014 | P2 | 应用容器无 pg_dump, 迁移前 PG schema 快照静默跳过 | 2026-09-09 | TianXiang |
| KI-015 | P2 | 前端 6 处手抄 fmt 金额精度互分叉 | 2026-09-08 | TianXiang |
| KI-016 | P2 | 前端设计债 7 项(W3.7 审计重跑产出) | 2026-09-09 | TianXiang |
| KI-017 | P3 | release.yml 单 job 混合测试+构建+推送(执行方案 T15) | 2026-09-08 | TianXiang |
| KI-020 | P3 | 命中榜因子自动降权未做(权重策略待定) | 2026-09-08 | unassigned |
| KI-022 | P2 | hermes-gateway CLOSE-WAIT 无自愈(独立仓库) | 2026-09-07 | unassigned |
| KI-023 | P2 | ws_hub PubSub 自回显疑似循环(未验证) | 2026-09-07 | TianXiang |
| KI-024 | P3 | WS Hub 抽独立进程(部署拓扑变更) | 2026-09-07 | TianXiang |
| KI-026 | P2 | KlineChart/InteractiveKline 大重构 + orval 全量 codegen | 2026-09-07 | TianXiang |
| KI-027 | P3 | 本地环境损坏测试文件 2 个(不进 CI, 本地基线 2 failed) | 2026-09-08 | TianXiang |
| KI-028 | P1 | 交易日历静态表须在 2028 年初前补 2028 表 | 2026-09-09 | TianXiang |
| KI-029 | P3 | dark-flow 冷缓存撞冒烟 1s 超时(重建后门禁误报) | 2026-09-09 | TianXiang |
| KI-030 | P3 | JWT_SECRET 24 字节低于 RFC 7518 HS256 建议 32 字节 | 2026-09-09 | TianXiang |
| KI-041 | P3 | 前端 toFixed 存量基线冷冻包干(14 文件 + Quote.tsx 8→11) | 2026-09-10 | TianXiang |
| KI-042 | P2 | 分钟K线端点腾讯 ifzq 单源, 失败态 `points: []` 静默(文案误归因) | 2026-09-10 | TianXiang |
| KI-043 | P2 | 自选批量行情 /stocks/quotes 逐市场吞异常(缺项无提示) | 2026-09-10 | TianXiang |
| KI-044 | P2 | 板块资金 board-capital-flow 空列表静默(单源 ths_flow) | 2026-09-10 | TianXiang |
| KI-045 | P2 | /news 8s 超时静默置空(超时与"无新闻"不可分) | 2026-09-10 | TianXiang |
| KI-046 | P3 | marketdata_authoritative_sources.py 死配置(零 import, 待决议) | 2026-09-10 | TianXiang |
| KI-047 | P3 | 板块异动阈值为暂定值(涨速±0.5%/量比2.0)且仅盘中生效, 待实盘观察调优 | 2026-09-10 | TianXiang |
| KI-048 | P3 | 通达信云数据(板块异动类型/轮动系数等)需客户端数据权限, TQ 接口当前返空 | 2026-09-10 | TianXiang |
| KI-055 | P2 | 离线门禁存量 7 红(日历相关断言/缺包/用例间 mock 污染), 与 v0.5.76 无关 | 2026-09-12 | TianXiang |
| KI-056 | P3 | 行情页大图(KlineChart)还没有每 pane 信息栏 —— 只有 InteractiveKline 有 |

## 详情

### KI-005 forecast 4g 限额未按实测校准 (P2)

- 发现: 2026-09-09(W4.2/E7)
- 现象: forecast 服务 4g/1g/2.0 沿用 P1-14 既定值; 本机 8010 未部署(0.0 勘查既定, /api/health forecast_engine=down), 方案要求的"实测推理峰值×1.5"本波无法执行。
- 影响: Kronos/Chronos-Bolt/XGBoost/Lag-Llama 集成推理的真实峰值未知, 4g 可能过紧(推理期 OOM)或过松(挤占宿主)。
- 涉及文件: docker-compose.yml forecast 服务。
- 建议修复: 8010 首次真正部署时 `docker stats` 实测推理峰值×1.5 校准并回写 compose 与本条, 不许拍脑袋。

### KI-008 chat_upload 提示注入面 (P1)

- 发现: 2026-09-08(W1.4/C1+C2 遗留登记, 方案 §6.3 后续波次处理)
- 现象: src/web/api/chat_upload.py 把上传文件逐字解析, 截断到 100,000 字符后整体进 LLM prompt(`_MAX_TEXT_CHARS`, :36)。
- 影响: 恶意构造的文件内容可注入指令, 诱导 AI 产出误导性投资结论或带出系统上下文; 上传者须为已登录用户且注入发生在其自身会话, 风险可控但需治理。
- 涉及文件: src/web/api/chat_upload.py:36。
- 建议修复: 方案 §6.3 后续波次——不可信内容标注/prompt 分区隔离/内容消毒; 与 S5 权限口径对齐后实施。

### KI-009 chat.py f-string 拼 SQL(留痕, 已核实安全) (P3)

- 发现: 2026-09-09(方案 §4.3 点名; 代码存在更早)
- 现象: src/web/api/chat.py:234 `sql = f"SELECT {', '.join(cols)} FROM {table}"` 用 f-string 拼标识符(方案记录时行号 :695, W3.3 重构后迁移)。
- 影响: 无——已核实 cols 全部来自硬编码 `_FORECAST_COLUMN_MAP`, table 仅 forecasts/prediction_runs 白名单二选一, symbol/limit 走参数化绑定; 纯风格留痕, 无注入面。
- 涉及文件: src/web/api/chat.py:222-240(_read_forecast)。
- 建议修复: 不修; 若后续统一 SQL 风格可改为白名单列名预拼常量。

### KI-010 ShadowAccount window.open 兜底 401 (P2)

- 发现: 2026-09-08(W1.4/C1 报告端点加鉴权的连带面)
- 现象: 影子报告端点挂上 get_current_user 后, 前端 ShadowAccount 的 window.open 兜底链接(不带 Authorization 头)请求 401。
- 影响: 主路径 fetch+Bearer 不受影响, 仅兜底路径死链, 用户极少触达。
- 涉及文件: frontend ShadowAccount 相关组件(兜底 window.open)。
- 建议修复: 去掉兜底链接或改为 fetch+blob 下载(blob 导出属 W3.7 信封审计确认的合法旁路)。

### KI-011 主板 K 线缺口残留 10 条 (P2)

- 发现: 2026-09-09(v0.5.19 K线全量重刷验收)
- 现象: 主板"不可能缺口"由 19 降至 10 条; tencent RAW 交叉验证 600502 的 +11.67% 缺口在原始数据中逐字存在, 判定 vendor 源头级非重刷引入; 第二源裁决不可用(本机到 eastmoney 不通、sina 无个股 K 线拉取)。
- 影响: 10 只标的的历史 K 线存在 vendor 侧缺口, 依赖历史连续性的计算(缺口识别/形态/回测)在这些区间可能失真。
- 涉及文件: klines 数据层; data_quality_sentinel 每小时哨兵。
- 建议修复: 待第二源(eastmoney)网络可达时交叉裁决; vendor 确认错误的柱按"诚实缺失"语义丢弃而非保留假缺口。

### KI-012 港美市场无交易日历 (P3)

- 发现: 2026-09-09(W2.6/B6 接线时的显式豁免)
- 现象: 交易日历静态表仅覆盖 CN; models/market.py `MarketDef.is_trading_time` 的 HK/US 分支维持周末判定。
- 影响: 港美标的当地节假日(如感恩节)被当交易日, 盘中判定/TTL 对港美标的口径偏粗; 当前组合以 CN 为主, 无实际受害场景。
- 涉及文件: src/web/models/market.py、src/core/trading_calendar.py。
- 建议修复: 港美标的占比上升时, 按同一静态表机制补 HK/US 日历。

### KI-013 forecast 容器 PANWATCH_DB 遗留 (P3)

- 发现: 2026-09-08(feat/pg-default-0908)
- 现象: forecast 容器的 PANWATCH_DB 仍指 sqlite 文件路径, PG 生产下该文件不存在; 预测引擎读设置走 HTTP 不走文件, sqlite 直读仅遗留兜底。
- 影响: 无实际故障(兜底路径静默失败), 仅为配置陈旧易误导排查。
- 涉及文件: docker-compose.yml forecast 服务(注释已标注遗留兜底)。
- 建议修复: 8010 首次部署时清理该 env 或指向真实只读通道。

### KI-014 应用容器无 pg_dump (P2)

- 发现: 2026-09-09(v0.5.21 生产部署记录)
- 现象: 应用容器 PATH 无 pg_dump, src/db/backup.py 的迁移前 PG schema 快照打 warning 后跳过(实测发生在 v0.5.21 部署)。
- 影响: 迁移事故时缺 schema 级快速回滚参照; 仍有全库 pg_dump 兜底与幂等迁移保护, 属备份链降级而非数据风险。
- 涉及文件: Dockerfile、src/db/backup.py。
- 建议修复: 镜像补 postgresql-client(alpine apk 需走 CN 镜像源), 或 schema 快照改由宿主机/侧车执行。

### KI-015 前端 6 处手抄 fmt 精度分叉 (P2)

- 发现: 2026-09-08(fix-InteractiveKline 主力金额口径条目)
- 现象: WencaiPanel/MinuteLwcChart/MarketMainlineCard/MainFlowCompareCard/BoardDetail/stock-insight-modal 共 6 处改名手抄金额格式化, 精度互分叉(执行方案 T18/Q5 lint+门禁缝隙范围)。
- 影响: 同一金额在不同面板显示位数不一致; UI 一致性问题, 无数据错误。
- 涉及文件: 上列 6 个前端组件。
- 建议修复: 统一改 import format.ts 的 toAmount 家族, 并考虑扩 check_ui_rules.mjs 检测手抄 fmt 模式防回潮。

### KI-016 前端设计债 7 项 (P2)

- 发现: 2026-09-09(W3.7/D7 审计重跑产出)
- 现象: ①932 行大文件任意 px; ②7 处硬编码 hex; ③2 页空态缺失; ④loading 微组件重复; ⑤3 个死文件; ⑥ShareCard 6 文件重复; ⑦第三套 Tab 实现。OpportunityRow 拆分切线已勘明(4 个行内专用纯函数可随组件走, 6 个跨区共用须导出不可复制)。
- 影响: 均为可维护性债, 无运行时故障。
- 涉及文件: frontend/(逐项定位见 docs/research/UI终端化_设计系统审计_重跑_20260909.md)。
- 建议修复: 按审计报告排序逐项清(死文件/重复组件先行); OpportunityRow 拆分严格按勘线执行。

### KI-017 release.yml 单 job 未拆 (P3)

- 发现: 2026-09-08(fix-ACR 流水线门禁条目)
- 现象: release.yml(Docker Hub 通道)为单 job 混合测试+构建+推送, 未像 build-push-acr.yml 拆 gates/build 两段。
- 影响: 测试与发布耦合, 重跑浪费构建时长; 门禁本身存在(非安全洞), 纯流程债。
- 涉及文件: .github/workflows/release.yml。
- 建议修复: 按执行方案 T15 拆分, 参照 build-push-acr.yml 现成结构。

### KI-020 命中榜因子自动降权未做 (P3)

- 发现: 2026-09-08(feature-分Agent命中榜)
- 现象: 方向 3 下半场"因子自动降权"未做。
- 影响: 命中差的因素仍以固定权重参与合成, 无自动退化。
- 涉及文件: src/web/api/profile.py(_accuracy_board)。
- 建议修复: 先定权重策略(人工调参 vs 自动退化), 再另开实现。

### KI-022 hermes-gateway CLOSE-WAIT 无自愈 (P2)

- 发现: 2026-09-07(feature-P4 可观测)
- 现象: hermes-gateway(独立仓库)连接出现 CLOSE-WAIT 后无自愈逻辑。
- 影响: 长期运行的网关连接可能僵死, 需人工重启。
- 涉及文件: hermes-gateway 仓库(不在本仓)。
- 建议修复: 在该仓库加连接健康探测+自动重连; 本仓不动。

### KI-023 ws_hub PubSub 自回显疑似循环 (P2)

- 发现: 2026-09-07(feature-P2 实时 envelope 条目顺带发现)
- 现象: ws_hub 的 Redis PubSub 疑似自回显循环(本进程 publish 的消息回到本进程消费), 未动, 需单开验证。
- 影响: 若真实存在会造成重复推送/无谓 CPU; 目前仅"疑似", 未复现。
- 涉及文件: src/web/notifications/ws_hub.py、src/web/realtime/envelope.py。
- 建议修复: 写专项测试复现(本进程 publish→subscribe 回环断言), 复现后再定抑制方案。

### KI-024 WS Hub 抽独立进程 (P3)

- 发现: 2026-09-07(feature-P2/P4 未做项)
- 现象: WS Hub 仍在 8000 进程内, 抽独立进程未做; envelope/seq/ring 代码前置条件已铺好。
- 影响: WS 高连接量时与 HTTP 争资源; 当前规模无感, 属部署拓扑变更(有迁移成本)。
- 涉及文件: src/web/notifications/ws_hub.py。
- 建议修复: 连接量上升后随 CLOSE-WAIT 自愈(P4 原计划)一起做。

### KI-026 KlineChart 大重构 + orval 全量 codegen (P2)

- 发现: 2026-09-07(feature-P3 未做项)
- 现象: KlineChart/InteractiveKline(2001 行)大重构未做(风险高, 留终端化专项); orval 全量 codegen 等后端契约稳定。
- 影响: 图表组件可维护性差; API 客户端手写, 与后端契约漂移靠人肉对齐。
- 涉及文件: frontend/packages/biz-ui/src/components/KlineChart.tsx、InteractiveKline.tsx、frontend/packages/api/。
- 建议修复: 终端化专项一并处理; orval 待 docs/_frozen/openapi 契约冻结后全量生成(P1 已冻结 291 paths 快照, 可评估启动)。

### KI-027 本地环境损坏测试文件 2 个 (P3)

- 发现: 2026-09-08(v0.5.19 发版记录, 此后历次基线恒定复现)
- 现象: 本地全量套件恒定 2 failed: tests/test_ta_load_ohlcv_patch.py(依赖本机损坏文档)与 tests/test_thsdk_buffer_size.py(flaky)。两文件从未进 CI; W2.3 后 CI 主门禁 `-m "not network"` 天然不受影响(thsdk 相关本就 network 打标)。
- 影响: 不阻塞发版, 但每次发版对账都要解释这 2 个失败(历次 CHANGELOG 反复出现)。
- 涉及文件: tests/test_ta_load_ohlcv_patch.py、tests/test_thsdk_buffer_size.py(损坏源于本机环境非代码)。
- 建议修复: 修复/移除本机损坏的依赖文档; 或两文件加环境探测 skip, 让本地基线收敛到 0 failed。

### KI-028 交易日历 2028 表补录 (P1)

- 发现: 2026-09-09(v0.5.20 部署注意; W2.6 红线既定)
- 现象: 交易日历静态表覆盖 2025-2027; 2028-01-01 起未覆盖年份显式抛 TradingCalendarError(fail-loud, 不回落 weekday)。
- 影响: 若 2028 年初未补表, 交易日判定相关链路(调度/TTL/竞价池/评估窗口)将显式报错——报错是设计行为, 但必须按期补表避免生产停摆。
- 涉及文件: src/core/trading_calendar.py(_HOLIDAYS)。
- 建议修复: 2027-12-31 前依官方放假安排补 2028 表; 与 KI-007 修复同一维护窗口执行。

### KI-029 dark-flow 冷缓存撞冒烟 1s 超时 (P3)

- 发现: 2026-09-09(维护窗口容器重建后首跑冒烟 8/9)
- 现象: 容器重建清空 dark_flow_verdict 磁盘缓存, 冷态首请求需重算(1.5 CPU 上限下实测 6.8/3.2/4.9s, 重建后首批曾 67/21s), 冒烟客户端 1s 读超时判 FAIL —— 端点本身 200 且 main_net 正确, 属门禁误报非功能故障; 预热 3 连后命中缓存回落亚秒, 复跑 9/9。
- 影响: 仅容器重建/清缓存后的首个冒烟窗可能 8/9; 线上用户冷态同样遇慢响应(浏览器无超时, 仅慢不失败)。
- 涉及文件: scripts/smoke_test.py(dark-flow 检查 1s 超时)、scripts/post_deploy_smoke.sh、src/web/api/darkflow.py(磁盘缓存)。
- 建议修复: post_deploy_smoke.sh 在重建场景加 dark-flow 预热步(带 symbol 请求 1-3 次再跑门禁), 或 dark-flow 检查单独放宽超时; 重建 runbook 已补"预热后再冒烟"。

### KI-030 JWT_SECRET 24 字节低于 HS256 建议 32 字节 (P3)

- 发现: 2026-09-09(维护窗口容器日志 InsecureKeyLengthWarning)
- 现象: 生产 JWT_SECRET 24 字节, python-jose 对 HS256(SHA256)建议密钥 ≥32 字节(RFC 7518 §3.2)。
- 影响: 非已知可利用面(24 字节 HS256 仍非弱密钥, 低于规范建议与告警基线); 每次签发刷一条告警日志。
- 涉及文件: 生产 env(JWT_SECRET)、src/web/api/auth.py。
- 建议修复: 随维护窗口换 ≥32 字节随机值 —— 注意会使全部现存会话失效(用户重新登录), 与口令类轮换同窗口执行成本最低。

### KI-041 前端 toFixed 存量基线冷冻包干(14 文件 + Quote.tsx 8→11) (P3)

- 发现: 2026-09-10(P1-1 板块热力图提交前门禁复跑, `node scripts/check_ui_rules.mjs` 报 16 违规)
- 现象: UI 规则 R6 基线自 979d79c(W2.4/E3, 2026-09-09)冻结后未再补挂, 此后各波新增/迁移的 14 个 toFixed 文件(`insight/OverviewTab` 34、`insight/helpers.tsx` 9、`stocks/AccountsSection` 5、`PnlCharts` 2、`lib/drawdown.ts`/`lib/trades.ts`/`stocks/WatchlistSection` 等)不在基线 → 门禁持续红。`Quote.tsx` 8→11 系 v0.5.35(KI-019/021/018)落地时新增, 站点均有 `Number()`/null 守卫。
- 影响: 门禁在 CI release/image 工作流(task tag 触发)会红, 属流程阻塞而非运行时故障; 站点本身无字符串 `.toFixed` 崩溃风险(已逐点核)。
- 涉及文件: scripts/ui-rules-baseline.json(2026-09-10 重算冻结 60 键)、scripts/check_ui_rules.mjs(R7 补 difVals 豁免)。
- 建议修复: 后续波次将存量站点逐步改 `@/lib/format` safe* 并同步调低基线(只降不升); 基线维护可在 check_ui_rules.mjs 加 `--update` 模式(本次未加)。

### KI-042 分钟K线端点腾讯 ifzq 单源静默 (P2)

- 发现: 2026-09-10(P1-2 单源依赖审计, docs/research/单源依赖审计_20260910.md §4-P1)
- 现象: `GET /quotes/minute/{symbol}` 硬编码 `web.ifzq.gtimg.cn`(quotes.py:271), 取数失败与"真无分时"共用 `points: []`; 分时对话框文案为"暂无分时数据(非交易日或停牌)"(minute-dialog.tsx:92) → 源故障被误归因为非交易日/停牌。
- 影响: 源抖/风控时用户看到误导读数; 属"静默空白"类(审计判 🔴)。
- 涉及文件: src/web/api/quotes.py(minute 端点)、frontend/packages/biz-ui/src/components/minute-dialog.tsx、InteractiveKline.tsx(有错误态但空数据时无提示)。
- 建议修复: 响应加 `degraded: true, note: "分时源(腾讯)暂不可用"`, 对话框优先展示 note; 可评估 1m 落库(klines)兜底(注意 1m 源同为腾讯 mkline, 仅作传输面冗余)。

### KI-043 自选批量行情逐市场吞异常 (P2)

- 发现: 2026-09-10(P1-2 单源依赖审计 §4-P1)
- 现象: `GET /stocks/quotes`(stocks.py:219-229)对每个市场 `except Exception: logger.error(...)` 后静默跳过 → 失败市场标的全部缺价, 前端无任何提示。
- 影响: 自选页整列"--"/缺行且无原因提示; 属"缺项无提示"类(审计判 🔴)。
- 涉及文件: src/web/api/stocks.py、frontend Stocks 页消费侧。
- 建议修复: 收集 `degraded_markets: [{market, error}]` 随响应返回, 前端复用 ErrorBanner 显式展示+重试。

### KI-044 板块资金空列表静默 (P2)

- 发现: 2026-09-10(P1-2 单源依赖审计 §4-P1)
- 现象: `GET /market-data/board-capital-flow`(market_data.py:176-211)上游单源(ths_flow)返回空时输出 `count: 0, items: []`, 与"今日确实无数据"不可分(异常才有 502)。
- 影响: 资金流页板块明细静默空白; 同文件 `/market-capital-flow` 消费该函数时同样继承空语义。
- 涉及文件: src/web/api/market_data.py、packages/marketdata(ths_flow)、frontend 资金流页。
- 建议修复: 非节假日空态改显式 502 或带 `degraded` 标记(与 /market/indices 同款口径, 参考本轮已修模式); 备源 `board_fund_flow`(东财)海外 502 不推荐接链。

### KI-045 新闻 8s 超时静默置空 (P2)

- 发现: 2026-09-10(P1-2 单源依赖审计 §4-P1)
- 现象: `GET /news`(news.py:96-106)`asyncio.wait_for(..., 8.0)` 超时/异常一律 `return []`, 与"无相关新闻"不可分。
- 影响: 首页/资讯静默空白; 属"超时静默"类(审计判 🔴)。
- 涉及文件: src/web/api/news.py。
- 建议修复: 返回结构带 `degraded: "timeout"`(或信封层), 前端已有 ErrorBanner 基建可复用。

### KI-046 marketdata_authoritative_sources 死配置 (P3)

- 发现: 2026-09-10(P1-2 单源依赖审计 §1.3)
- 现象: `src/core/marketdata_authoritative_sources.py`(2026-08-31 P0 骨架)的 `AUTHORITATIVE`/`FALLBACK_CHAIN`/`get_vendor_for` **全仓零 import**, docstring 自述"待 marketdata_client.py 集成"; 实际降级链在 registry+DataSource seed 中实现。
- 影响: 无运行时影响; 但"权威源+降级链"设计有两处真相源(该文件 vs registry), 后续维护者可能被误导。
- 涉及文件: src/core/marketdata_authoritative_sources.py、packages/marketdata/registry.py。
- 建议修复: 三选一交老板决议 —— ① 接线(重构成本高, 价值与 registry 重叠); ② 降级为纯文档(docs/ 下注明与 registry 的关系); ③ 删除(信息已无增量)。

### KI-047 板块异动阈值暂定 + 仅盘中生效 (P3)

- 发现: 2026-09-10(板块热力图实时化, 老板拍板"先 60s 自动刷新 + 异动高亮, 不推送")
- 现象: 异动阈值 `ANOMALY_SPEED_PCT=0.5`(涨速)/`ANOMALY_VOLUME_RATIO=2.0`(量比)为先验取值, 未经实盘噪声校准; 且量比/涨速仅交易时段有实时快照 —— 收盘后(含盘前竞价)一律无异动提示, 纯日线口径。
- 影响: 阈值偏低 → 清单刷屏/频繁误报; 偏高 → 真异动不提示。仅展示层影响(不推送, 无下游动作)。
- 涉及文件: frontend/packages/biz-ui/src/lib/board-heatmap.ts(常量)、src/web/api/boards.py(live 合并)。
- 建议修复: 实盘观察 1-2 周后按命中率调阈值; 若要扩到盘前/收盘后需先给 thsdk 扩展档加时段外快照能力。

### KI-048 通达信云数据(板块异动/轮动系数)需数据权限 (P3)

- 发现: 2026-09-10(方案B 落地时的客户端页面走查, §7)
- 现象: 客户端"板块异动"页的 异动类型/异动日涨%/异动天数/平均周期/轮动系数/近一年异动次数 等字段来自云数据源 `cfg_bk_bkld_yjhy`(本机配置 `T0002/cloud_cfg/GN_BKJJ_LSBKYD101.xml` 可见全部列定义); TQ 云数据接口(`get_bkjy_value`/`get_gpjy_value`/`get_scjy_value`, 形状 = table_list + start_time/end_time)**当前返回空值** —— 需通达信对应数据权限(专业数据/云数据订阅)。
- 影响: SIDA 只能自算异动(急拉/急跌/放量, v0.5.52), 无法复刻通达信"轮动系数/异动天数"等云分析字段。
- 涉及文件: 客户端云配置(非仓库)、src/core/tdx_boards.py(未接云数据)。
- 建议修复: 向老板确认客户端是否具备/愿意开通通达信云数据权限; 若开通, 在 `tdx_boards.py` 增加云数据拉取(字段名已在客户端 XML 中);
  若不开放, 保持自算口径并在展示层标注。

### KI-055 离线门禁存量 7 红 (P2)

- 发现: 2026-09-12(v0.5.76 发版跑全量门禁时发现)
- 现象: `pytest -m "not network"` = 2179 passed / **7 failed**。用 **v0.5.75 worktree 跑同一套全量**做基线, 失败清单**逐条相同** → 与 v0.5.76 无关, 是存量。三类:
  ① `tests/test_entry_candidate_outcomes.py` 5 条 —— 断言 `missing_pairs==6` 实得 4, 用例用 `date.today()` + 交易日推算, **2026-09-12 是周六**, 疑为日历相关(待工作日复跑确认);
  ② `tests/test_ta_load_ohlcv_patch.py` 1 条 —— `ModuleNotFoundError: tradingagents`(本机未装该包, CI 有);
  ③ `tests/test_thsdk_buffer_size.py::test_buffer_size_injected` 1 条 —— **单独跑绿、整套跑红**: mock 被前面用例污染, 实际发起了真 thsdk 连接(5 次失败), 且该用例漏标 `@pytest.mark.network`。
- 影响: 门禁不再"全绿可依赖", 每次发版都要人工比对基线才能确认没引入回归 —— 正是 CI 真门禁(0.5)想消除的成本。
- 涉及文件: 上述三个测试文件。
- 建议修复: ①把用例时间基准改成注入的固定日期(不依赖 `date.today()`); ②`tradingagents` 缺失时 `pytest.importorskip`; ③给 thsdk buffer 用例补 `@pytest.mark.network` 并修 mock 隔离(它现在会在整套跑时真连外网)。
- ✅ **2026-09-14 已关闭(三类全部清零)** —— 实测 `PYTHONUTF8=1 python -m pytest -q -p no:warnings -m "not network"` = **2280 passed / 0 failed / 5 skipped**(此前 7 failed / 2273 passed)。逐类处置:
  - **① `test_entry_candidate_outcomes.py` 5 条**: **无需改代码** —— 2026-09-14 是**周一(工作日)**, 同一套用例直接转绿, 证实原判断"疑为日历相关"。(仍建议按原方案改成注入固定日期, 否则每逢周末门禁就会假红 5 条; 本次未改, 因为不改也已复现并确认成因, 且改法属测试重构, 不在本批范围。)
  - **② `test_ta_load_ohlcv_patch.py` 1 条**: **没有采用 `importorskip`** —— 那只是把红变成跳过, 会**丢掉覆盖**。实况是 `tradingagents` 为软依赖时适配器抛的是**另一种**异常(`toolkit_adapter.py:452-461` 的 `except ImportError → RuntimeError`), 故改为**按环境断言对应异常类型**: CI(装了上游)仍钉住 `NoMarketDataError` 契约, 本机则真正覆盖那条**原本零覆盖**的兜底分支。两种环境都在测东西, 都不是跳过。
  - **③ `test_thsdk_buffer_size.py` 1 条**: **没有采用"标 `@pytest.mark.network`"** —— 那等于承认它本该联网, 与用例自述意图("不依赖真实 thsdk 安装")相反, 且会让离线门禁少守一条真契约。真根因是: 文件顶部换假 `sys.modules["thsdk"]` 只在 `data_source.thsdk_l2` **首次** import 时生效, 全量跑时别的用例早已 import 过它 ⇒ 拿到的是**绑着真 `THS`** 的缓存模块, 换假成了空操作 ⇒ `_query` 真去连行情服务(5 次重试全败, 返回 `error='未登录'`)。改为用 `monkeypatch` 直接替换 `_query` 实际取用的**模块属性** `M.THS`(`thsdk_l2.py:98` 的 `from thsdk import THS`, 用于 `:349`/`:361`)⇒ 与 import 顺序无关且**全程离线**。已补回归验证: 单文件跑与全量跑均绿。

### KI-056 KlineChart(行情页大图)还没有每 pane 信息栏 (P3)

- 发现: 2026-09-12(v0.5.79 走查 D1 时实测)
- 现象: D1 的"每 pane 信息栏"只落在 `InteractiveKline`(指数详情/分析详情/模拟盘/洞察模态用), 而**行情页 `/quote/:symbol` 的大图用的是 `KlineChart`** —— 生产实测 `/quote/600519` 页面上找不到 `MACD(12,26,9)` / `RSI(6)` 任何一条栏。
- 为什么没顺手做: 两个图表的副图集合不同 —— `KlineChart` 是 `vol | macd | active_ratio | phase | activity`(且 RSI 根本没算), `InteractiveKline` 是 `vol | macd | rsi`。注册表 `lib/subcharts.ts` 现在只描述后者, 直接套会把不存在的读数写成 `--`。
- 影响: 老板最常看的那张大图拿不到"悬停那根的副图读数", D1 的价值只兑现了一半。
- 涉及文件: frontend/packages/biz-ui/src/lib/subcharts.ts、frontend/packages/biz-ui/src/components/KlineChart.tsx。
- 建议修复: 把注册表按 chart 分组(或给 def 加 `series` 字段标明属于哪个图), 为 KlineChart 声明 vol/macd/active/phase 四类读数, 再接它已有的 `subscribeCrosshairMove`(350 行)把 hover 索引提到 state 渲染条带; 与 [[个股详情整页工作台]] 的图表主体改造合并做最省。
- **2026-09-14 订正(条目仍开启, 老板本批明确跳过)**: 上文"行情页 `/quote/:symbol`"已随 **v0.6.0** 退役 —— 该路由现在是 redirect, `KlineChart` 成了**个股工作台带2 的主图**(`/stocks/:symbol`)。缺陷本体不变(这张图仍无每 pane 信息栏), 只是入口路径变了; 修复落点也仍是同两个文件。

### KI-057 「振幅」两套分母口径(后端 `/low` vs 前端 `/prev_close`) (P2)

- 发现: 2026-09-14(v0.6.0 遗留⑤ 给工作台带1 快照行加「振幅」格时, 核对既有实现发现)
- 现象: 同一个标签「振幅」在产品里有**两个不同公式**, 且两者能从**同一个页面**到达:
  ① **后端落库口径** `src/collectors/kline_collector.py:853` → `amplitude = (curr.high - curr.low) / curr.low * 100`(**分母是最低价**), 存进 `klines.amplitude`, 由 `kline-summary-dialog.tsx` 展示(其解释文案 `:793`「今日振幅≈(High-Low)/Low」与之一致), 并被 `daily_report`/`premarket_outlook`/`intraday_monitor` 三个 Agent 写进 AI 报告文本。
  ② **前端实时口径** `(high - low) / prev_close * 100`(**分母是昨收**, 即 A 股通行口径) —— 原在 `insight/useInsightDerived.ts:97`, v0.6.0 遗留⑤ 起也用于工作台带1 的「振幅」格(`workbench/HeaderBand.tsx::amplitudePct`)。
  同屏可达路径: 工作台带1 快照行显示 ②; 带1 的技术指标建议条 → `suggestion-badge.tsx` → `KlineSummaryDialog` 显示 ①。同一只票同一交易日, 两处数字**不相等**(分母 `low` ≤ `prev_close` 时 ① 恒 ≥ ②)。
- 影响: 老板在同一个页面看到两个都叫「振幅」的数, 无法判断哪个对 —— 与 KI-037(前端指标逐值对齐后端)同类的口径分叉, 只是这次分叉在"定义"层而非"实现"层。另: `useInsightDerived.amplitudePct` 现已**零消费方**(唯一消费者 `OverviewTab` 在 v0.6.0 清理第3批被删), 属死代码。
- 涉及文件: src/collectors/kline_collector.py、frontend/packages/biz-ui/src/components/workbench/HeaderBand.tsx、frontend/packages/biz-ui/src/components/insight/useInsightDerived.ts、frontend/packages/biz-ui/src/components/kline-summary-dialog.tsx。
- 建议修复: **先定口径再改代码**(需老板拍板, 不擅自改) —— 若取 A 股通行口径(分母=昨收), 则改 `kline_collector.py:853` 并同步 dialog 解释文案, 且要决定**历史 `klines.amplitude` 是否回填重算**(改了不回填 = 新旧行不同口径混在一张表里, 比现在更糟); 若保留 `/low`, 则带1 应改成消费后端口径而不是自己算(但那是 EOD 值, 盘中会显示昨日振幅, 也不对)。**当前处置**: 带1 保持 A 股通行口径(实时面本就该用实时 high/low/prev_close), 分叉登记在此不静默; 顺手可删 `useInsightDerived.amplitudePct` 死代码。

### KI-058 「设提醒」能力无 UI 入口 / `handleSetAlert` 成孤儿 (P2, 待老板拍板)

- 发现: 2026-09-14(v0.6.0 遗留③ 把工作台「触发盘中监测」改成**不动自选/绑定**的路径后, 反查调用方发现)
- 现象: 给个股**绑定 `intraday_monitor` Agent**(= 让它进定时扫描并推提醒)的唯一实现是 `insight/useInsightActions.ts::handleSetAlert`(`list()` → 未关注则 `create()` 写入自选 → `updateAgents()` 写入绑定 → `triggerAgent`), 而它现在**全仓零生产调用方**:
  - 原来的入口「一键设提醒」按钮在旧个股详情模态 `stock-insight-modal.tsx` 里, 该模态随 **v0.6.0** 退役(未恢复) ⇒ 能力入口当时就没了;
  - v0.6.0 期间工作台「建议」标签的「触发盘中监测」按钮**顺带**调了它, 于是"点一下分析就偷偷加自选+绑 Agent"成了副作用缺陷(复审 Finding 1);
  - 遗留③ 按要求把该按钮改成 `triggerIntradayOnce`(`stock_id=0` + `allow_unbound`, 后端 `src/web/api/stocks.py:523-533` 的"不落库"分支 ⇒ **不加自选、不绑 Agent**; 注: 这**不是**"零写入" —— 那轮运行仍会落一条 `AgentRun` 运行记录与一条站内「任务完成」通知, 2026-09-14 复审 Finding 1 已证伪原先的过度声称) ⇒ 那条顺带的路径也没了。
  现仅 `tests/components/suggest-tab.test.tsx` 用探针组件直调它(守护"保留项不被改坏")。`stocksApi.updateAgents` 因此也只剩这一个调用方。
- 影响: 用户**无法从界面上**给任何个股开启盘中监测提醒(只能靠已有的历史绑定行继续跑); 同时仓里留着一个"会做持久化写入却无人能触发"的动作 —— 后人若随手接上一个按钮, 就会把 ③ 刚消除的副作用重新引进来, 而当初那句警示文案已随旧模态一起删掉了。
- 涉及文件: frontend/packages/biz-ui/src/components/insight/useInsightActions.ts、frontend/src/pages/workbench/tabs/SuggestTab.tsx、frontend/packages/api/src/stocks.ts。
- 建议修复(**两条路, 需老板选, 未擅自决定**): (A) 在工作台「建议」标签补一个**独立且明示写入**的「设提醒」按钮调 `handleSetAlert`(与「触发盘中监测」并排, 各自把副作用讲清楚) —— 恢复 v0.6.0 之前的能力, 属**新功能**, 按 [[feedback-scope-before-product-work]] 先列清单再动; (B) 确认该能力不再需要, 则删 `handleSetAlert` + `SetAlertOutcome` + 对应探针测试(与清理第1/3批删死文件同手法)。当前批次**两条都没做**: 遗留③ 的既定范围明确写了"保留 `handleSetAlert` 给「一键设提醒」", 故先原样保留并登记在此。

### KI-059 封单额迁到带1 后失去 30s 轮询与快照时钟 (P2, 待老板裁定)

- 发现: 2026-09-14(v0.6.0 遗留⑤ 按去重契约把封单额从右栏迁到带1 后, 独立复审 Minor 5 提出)
- 现象: 迁移前「封单额」在右栏「盘口速览」卡里, 该卡 `/stocks/{s}/l2` **30s 轮询**且显示 `快照 HH:MM:SS`(`QuickRail.tsx`); 迁移后它在**带1 快照行**, 而 `HeaderBand` **没有任何轮询**(取数 effect 依赖 `[symbol, market, isStock, cnStock, tick]`, `tick` 只在手动点刷新时 +1), 也**没有快照时钟** ⇒ 封单额变成"进来时看一眼, 之后不动"。
- 为什么值得单列: 封单额恰恰是那一行里**变化最快**的读数(封板扛不扛得住就看它), 涨停股盘中几秒就能从几亿砸到 0; 而 涨停价/PE/PB 这些同排的格子本来就是慢变量。**这是遵循去重契约的必然结果, 不是实现错误**(契约 `DATA_OWNERSHIP.seal_amount='band1.snapshot'` 指定带1 为唯一拥有面), 但代价是新鲜度下降。
- 影响: 盯涨停板的用户会拿到一个**看着像实时、其实是首屏时刻**的封单额, 且屏上没有任何时效提示 —— 与"不伪装"纪律相冲突的是**缺少时效披露**这一点。
- 涉及文件: frontend/packages/biz-ui/src/components/workbench/HeaderBand.tsx。
- 建议修复(**三选一, 需老板拍板**): (A) 给带1 的 `/l2` 加 30s 轮询(与右栏同频; 代价是带1 从"静态吸顶带"变成持续取数, 首屏后每 30s 多一条请求); (B) **只加快照时钟** `快照 HH:MM:SS`(不轮询, 但让用户知道这个数是什么时候的 —— 成本最低、最符合"不伪装"); (C) 把封单额迁回右栏并改去重契约(不推荐: 会重新引入两处显示同一数据点)。**倾向 B**(或 A+B), 但未擅自实现。

## 依赖安全审计 (W2.5/E5+E6, 2026-09-09 → KI-001/002/003/006)

复现命令:

```bash
# 前端 (npmmirror 镜像无 audit 端点, 必须指回官方 registry)
cd frontend && pnpm audit --registry=https://registry.npmjs.org/
# 后端 (在装有锁定依赖的 python:3.11 环境执行; 生产镜像由 CI/ACR 构建时安装)
pip-audit -r requirements-lock.txt --no-deps
```

结论汇总: pnpm audit 28 条 (critical 0 / high 11 / moderate 15 / low 2);
pip-audit 结果见节末。

### 高危项登记

| KI | # | 依赖 | 装机版本 | 问题 | 修复版本 | 暴露面 | Owner | 期限 |
|----|---|------|---------|------|---------|--------|-------|------|
| KI-001 | 1 | react-router-dom | 6.30.3 | 开放重定向→XSS (moderate, 5 条含 react-router/@remix-run/router) | >=6.30.6 (同大版本 patch) | **运行时**, 用户可触达 | TianXiang | 2026-09-30 |
| KI-002 | 2 | rollup | 4.56.0 | 任意文件写/路径穿越 (high) | >=4.59.0 | 仅构建链, 不进产物 | TianXiang | 2026-09-30 |
| KI-003 | 3 | vite | 5.4.21 | dev server fs.deny 绕过 (high) 等 3 条 | >=6.4.3 (跨大版本) | 仅 dev server | TianXiang | 2026-10-31 |
| KI-006 | 4 | tailwind/babel 构建链传递依赖 (postcss/nanoid/picomatch/browserslist/@babel/core/esbuild 等) | 见 pnpm-lock | ReDoS/原型污染/文件读 等 17 条 | 均 patch/minor 可修 | 仅构建链/dev 依赖, 不进产物 | TianXiang | 2026-10-31 |

说明:

- 依赖升级由 dependabot (W2.5 同批接入, weekly, 三生态) 接管: patch/minor
  自动开 PR 合并即关闭; vite 5→6 / vitest 3→4 跨大版本需手动评估。
- 暴露面判定: 前端构建工具链 (vite/rollup/postcss/babel/tailwind) 只在本机
  dev/build 阶段执行, 产物是静态 bundle, 漏洞不影响线上用户; 真正运行时
  依赖里的已知漏洞当前只有 react-router-dom 一处。
- vitest 3.2.7 的 @vitest/mocker 路径穿越 (moderate) 仅测试环境, 随 W2.4
  引入的测试栈, 跟随 vitest 大版本升级处理(并入 KI-006 口径)。

### pip-audit 结果

2026-09-09 在 python:3.11-slim 容器(装 requirements-lock.txt 精确依赖)执行
`pip-audit -r requirements-lock.txt --no-deps`: **No known vulnerabilities found**
—— 后端 171 个锁定包在 OSV/PyPI 数据库中无已知漏洞, 暂无登记项。

## forecast_server 预测目标日按 weekday 计 (W2.6/B6 审计豁免 → KI-007, 2026-09-09)

forecast_server.py 独立部署(运行目录 forecast_lib/, 不含 src/), 其"未来 N 个
交易日"计数按 weekday<5 推算(两处: target_date 换算天数 + pred_dates 生成),
法定节假日会被当作交易日, 目标日期偏晚(如国庆前预测 3 个交易日, 实际应跨到
节后)。影响面: 仅预测展示的目标日期标注, 不影响 src/ 内任何评估/回测链路
(那部分已统一走 src/core/trading_calendar.py)。

修复方向: 给 forecast_lib 内联一份静态日历表或部署时附带日历 JSON,
随 src/ 表同步维护。Owner: TianXiang, 期限: 2026-10-31(与 KI-028 日历
2028 表补录同一维护窗口处理)。

## 决策留痕(明确不做, 非缺陷)

以下为评审后**有意不做**的决策, 记录于此防止被当欠账重复提出; 理由变更时再重开:

- **Alembic 不引入**(2026-09-07): 已有自研 versioned migrations(checksum+advisory lock+schema 快照), 不重复造轮子。
- **W3.1 三项**(2026-09-09): migrations.py 不随迁 src/db/migrations/(checksum 稳定与生产安全优先于目录美观, 收编已用新版本号 143-148 实现等价); SessionLocal 不拆 session.py(reload 脆弱); 仓库层抽象暂不做。
- **前端设计取舍**(2026-08-28, v0.4.50): 活跃度三色无副图柱载体不硬套; GS 琥珀/紫与 A 股红绿惯例冲突, 维持 G红S绿(Quote 与 K 线的 GS 配色矛盾本身已于 v0.4.71 修复)。
- **裸 fetch 10 处保留**(2026-09-09, W3.7 信封审计重跑): 全为 blob 导出/报告沙箱/WS 合法旁路, 响应信封已全量采纳——非欠账。

## 对账(W4.3/F5 验收)

`grep -c "未做\|待办\|已知限制" CHANGELOG.md` = **13 行 → 13 条 KI**: L147→KI-012, L438→KI-015, L468→KI-017, L495→KI-013, L510→KI-018, L519→KI-019, L524→KI-020, L530→KI-021, L543→KI-022+KI-024(一行两项), L552→KI-026, L560→KI-025+KI-023("Hub 抽独立进程"与 L543 重复计一次), L569→0 新增(Alembic 归决策留痕; audit 独立 Session 已于 08-21 修复; orval 并入 KI-026), L1275→0 新增(设计取舍归决策留痕)。

扩词 grep(加 `暂不|明确不做|遗留`, 29 行)与其它来源额外产出 15 条: KI-004(L15)、KI-005(L13 W4.2 做法行)、KI-006(L159 经 W2.5 条目)、KI-007(L148 豁免清单)、KI-008+KI-010(L249)、KI-011(L209)、KI-014(L29)、KI-016(L51)、KI-001/002/003(W2.5 既有表收编)、KI-027(历次发版"已知本地环境损坏"汇总)、KI-028(L139 部署注意)、KI-009(方案 §4.3 点名)。其余命中为历史叙事用词("历史遗留"描述)或已修复项(GS 配色 L1338 已于 v0.4.71 修复), 不迁入。合计 **28 条**(≥20 达标)。

后续台账变化: 2026-09-09 维护窗口 KI-004 修复移入 CHANGELOG(生产容器限额重建+PG 口令轮换条目); 同日新增 KI-029/030, 台账现 29 条在册(P1×3)。2026-09-09 晚《优化/改进/创新分析报告》新增 **KI-031..040**(P1×6: 回测口径/涨跌停成交/结果口径/因子 OOS/因子快照前视护栏/PIT universe; P2×4: 指标分叉/风控熔断/core→web 耦合/K 线校验), 台账现 **39 条在册(P1×9/P2×17/P3×13)**; 每条的修复任务编号(B0.x-B5.x)见 `docs/优化改进创新_开发方案_20260909.md`。

**2026-09-09 晚(第 0-6 波交付后)**: **KI-031/032/033/034/035/036/038/040 共 8 条修复移入 CHANGELOG**(对应 W0.1-W0.6 / W3 / W1), 台账 **31 条在册**; KI-039(存量 56 文件未清)保留开启并已更新进展。

**2026-09-09 深夜(KI-037 收口)**: 前端指标收敛到 `frontend/packages/biz-ui/src/lib/indicators.ts` 并**逐值对齐后端 `src/core/indicators.py`**(MACD HIST 补 ×2、RSI6 由 Wilder 改 Cutler), 新增跨语言 parity 测试 `frontend/tests/lib/indicators-parity.test.ts`(夹具由后端生成, 容差 1e-9) → **KI-037 修复移入 CHANGELOG**, 台账 **30 条在册(P1×9/P2×16/P3×13)**; 仅 KI-039 保留开启。

**2026-09-09 深夜(KI-039 清零)**: 第二阶段把剩余 13 个反向依赖全部下沉 —— `src/core/paths.py`(报告目录) / `src/db/redis_client.py`+`src/db/streams.py` / `src/collectors/stock_list.py` / `src/collectors/wencai.py` / `src/core/market_scan_jobs.py` / `src/core/auth_tokens.py` / `src/core/notify_sink.py`(WS 推送槽, ws_hub 导入时注册), 另 unit_check 直调 `core.datasource_failures.record`。**`src/core` 反向依赖 `src/web` = 0 文件**, 棘轮白名单清空(新增即失败) → **KI-039 关闭移入 CHANGELOG**, 台账 **29 条在册**。

**2026-09-09 深夜(接口先行项落地)**: KI-019(来源徽标)/KI-021(决策卡片)/KI-018(错误页签)前端落地发版 **v0.5.35**; KI-025 前端 WS 消费 envelope(新增 `src/realtime/useQuoteStream.ts`: SWP 鉴权 / last_seq 补发 / 指数退避 / 4401 不重连) + 后端补推自选标的(`_collect_watchlist_symbols` 补 `stocks` 表)发版 **v0.5.36** → **4 条修复移入 CHANGELOG**, 台账 **25 条在册(P1×3/P2×13/P3×9)**。

**2026-09-10(P1-1 板块热力图)**: 新增 **KI-041**(R6 基线自 979d79c 冻结后未补挂 → 门禁存量红; 本次重算冻结 60 键 + R7 difVals 豁免), 台账 **26 条在册(P1×3/P2×13/P3×10)**。

**2026-09-10(P1-2 单源依赖审计)**: 产出 `docs/research/单源依赖审计_20260910.md`(全量路径表 + 单源约 30 条三分类 + 处置决策); 本轮修复 = 指数行情补链(腾讯→新浪, CN/HK/US 全兜底) + `/market/indices` 静默 `[]`→显式 502; 审计遗留 4 项静默态登记 **KI-042..045**(分钟K线/自选批量/板块资金/新闻) + 死配置留痕 **KI-046**, 台账 **31 条在册(P1×3/P2×17/P3×11)**。

**2026-09-10(板块热力图实时化)**: 新增 **KI-047**(异动阈值暂定 + 仅盘中生效, 待实盘观察调优), 台账 **32 条在册(P1×3/P2×17/P3×12)**。

**2026-09-10 晚(方案B 通达信板块数据)**: 新增 **KI-048**(通达信云数据异动/轮动系数需数据权限, 当前返空), 台账 **33 条在册(P1×3/P2×17/P3×13)**。

**2026-09-12(v0.5.76 生产走查)**: 新增 **KI-049..055** 共 7 条 —— P2×4(矩阵成功率列口径 KI-050 / 作业面板无入口 KI-053 / 扫描器不上报进度 KI-054 / 离线门禁存量 7 红 KI-055) + P3×3(双 v 前缀 KI-049 / 能力胶囊错路由 KI-051 / 首页标题竖排 KI-052), 台账 **40 条在册(P1×3/P2×21/P3×16)**。全部来自真机走查与端到端实测, 非静态审阅。

**2026-09-12(v0.5.77 收口)**: **KI-049/050/051/052/053/054 六条修复移入 CHANGELOG**(其中 KI-050 改了契约: `/capabilities` 新增 `verdict` 与 `min_samples`; KI-054 新增 `JobStore.progress_reporter` 心跳), 台账 **34 条在册(P1×3/P2×18/P3×13)**; 本轮仅 **KI-055(离线门禁存量 7 红)** 保留开启。

**2026-09-12(v0.5.78/79)**: 新增 **KI-056**(行情页大图还没有每 pane 信息栏, 注册表目前只覆盖 InteractiveKline 的副图集合), 台账 **35 条在册(P1×3/P2×18/P3×14)**; 同日修复并移入 CHANGELOG 的两项(行情页 marker 越界整页崩、前端报错上报恒 405)因从未登记过, 直接记在 CHANGELOG。

**2026-09-14(v0.6.0 遗留清理第 4 批)**: **KI-055 关闭**(离线门禁存量红清零: 实测 `pytest -m "not network"` = **2280 passed / 0 failed / 5 skipped**, 三类处置见该条目 ✅ 段 —— 其中 ① 5 条日历敏感用例在工作日**自动转绿**, 未改代码); 新增 **KI-057**(「振幅」两套分母口径: 后端落库 `/low` vs 前端实时 `/prev_close`, 同一工作台页可同屏到达, 需老板先定口径且牵涉历史 `klines.amplitude` 是否回填) + **KI-058**(遗留③ 之后 `handleSetAlert` 成零生产调用方孤儿 ⇒ "绑定盘中监测提醒"自 v0.6.0 退役旧模态起**已无任何 UI 入口**; 补显式按钮=新功能 vs 删死代码, 待老板拍板); 另订正 **KI-056** 描述(其引用的 `/quote/:symbol` 已随 v0.6.0 退役, 条目仍开启)。台账 **36 条在册(P1×3/P2×19/P3×14)**; 同批复审另提出 Minor 5(封单额迁移后失去 30s 轮询与快照时钟)⇒ 新增 **KI-059**, 台账 **37 条在册(P1×3/P2×20/P3×14)**。
