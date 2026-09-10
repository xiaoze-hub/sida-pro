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
