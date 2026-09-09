# Changelog

> **给 AI 协作者的读法**: 本文件是"别人改了什么"的唯一入口。接手前先读最近 3 个 `## 日期` 段。
> 每条 entry 末尾 `[commit <hash>]` 可直接 `git show <hash>` 看完整 diff。
> 写新 entry 时: 同一 commit 内改代码+记 changelog, 末尾缀 `[commit <short-hash>]`,
> 写清改了哪个文件、为什么改、测了什么。分支规范见 `AGENTS.md` "分支工作流"。

## 2026-09-09

### docs-已知问题台账收口(CHANGELOG未做项迁入KNOWN_ISSUES 28条+对账)(W4.3/F5)
- 背景: F5 —— "已知问题"散落 CHANGELOG 各条目的"未做/待办/已知限制"里, 新接手只能读全文猜; docs/KNOWN_ISSUES.md 此前只有 W2.5 依赖审计与 W2.6 weekday 豁免两节, 无统一编号台账。
- 做法: ①CHANGELOG 全文扫描(未做/待办/已知限制/暂不/明确不做/遗留)逐条判定开/闭状态, **28 条登记为 KI-001..028**, 每条 8 字段(id/level/owner/发现日期/现象/影响/涉及文件/建议修复); ②KI-004(生产容器无限制)/KI-005(8010 校准)沿用 W4.2 已发布锚点编号; ③W2.5 审计表与 W2.6 豁免两节保留为详情并收编 KI-001/002/003/006/007; ④方案 §4.3 点名项入册: chat_upload.py 提示注入面(KI-008, P1, 方案 §6.3 后续波次)、chat.py f-string SQL(KI-009, 方案记录行号 :695 现为 :234, 已核实 cols/table 均白名单+参数化无注入面, 留痕不修); ⑤"明确不做"决策(Alembic/W3.1 三项/前端设计取舍/裸 fetch 10 处)单列"决策留痕"节, 防被当欠账重复提出; ⑥两条此前无归属的新登记: KI-027 本地环境损坏测试文件 2 个(test_ta_load_ohlcv_patch/test_thsdk_buffer_size, 本地基线恒 2 failed 的出处, 建议加环境探测 skip 收敛到 0)、KI-028 交易日历 2028 表硬期限(2027-12-31 前补, 逾期 fail-loud 停摆, P1)。
- 级别分布: P0=0 / **P1=4**(KI-001 react-router-dom、KI-004 生产容器限额、KI-008 提示注入面、KI-028 2028 表) / P2=13 / P3=11。
- 对账(验收第 3 条): `grep -c "未做\|待办\|已知限制" CHANGELOG.md` = **13 行 → 13 条 KI**(L543 一行产 2 条: KI-022+024; L560 一行产 2 条: KI-025+023, "Hub 抽独立进程"与 L543 重复计一次; L569 产 0 条——audit 独立 Session 已于 08-21 修复/orval 并入 KI-026/Alembic 归决策留痕; L1275 产 0 条——设计取舍归决策留痕); 扩词 grep(加 暂不|明确不做|遗留, 29 行)与其它来源另产 15 条; 已修复项(GS 配色 v0.4.71 已统一等)不迁入, 明细对账表落在 KNOWN_ISSUES.md 文末。
- 验证: 28 条 ≥ 20; 总览表每条含 id/level/owner; 编号唯一性 `grep -o "KI-0[0-9][0-9]" | sort -u` = 28 无重复。
- [branch fix/wave4-流程债-20260909, `git show HEAD`]

### fix-三compose资源限制收口+deploy脚本限制克隆+static陈述实证(W4.2/E7)
- 背景: 4.2 —— 方案写作时"3 个 compose 全无资源限制"; 实测现状: docker-compose.yml 的 panwatch(1500m)/forecast(4g)/postgres(1g) 已在 P1-14 时代加了 mem_limit, 但 **cpus/memswap_limit/mem_reservation 全缺**(8010 推理高峰 CPU 争抢与 swap 超限无保护); docker-compose.infra.yml 6 个服务只有 restart 无任何 mem 限额; deploy/deploy_panwatch.sh 克隆重建时只克隆 --memory, swap/cpus 丢失。另: **生产 panwatch 容器实测 mem=0(完全无限制)** —— 非 compose 管理的历史 docker run 部署, compose 里的 1500m 从未生效。
- 做法: ①docker-compose.yml 三服务补齐 memswap_limit(=mem_limit, 不给额外 swap)+mem_reservation+cpus: panwatch 1500m/512m/1.5, forecast 4g/1g/2.0, postgres 1g/256m/1.0; ②docker-compose.infra.yml 六服务加 mem_limit+memswap_limit(与主 compose 同值防漂移: redis 320m/prometheus 512m/loki 512m/promtail 192m/grafana 512m/alertmanager 128m); ③deploy 脚本克隆逻辑补 MemorySwap/NanoCpus inspect 与 --memory-swap/--cpus 透传; ④docker-compose.dev.yml 是 overlay(继承主 compose 限额), 不重复加(overlay 加限会覆盖基线值, 反而引入漂移)。
- **8010 峰值内存实测约束(如实记录)**: 本机 8010 未部署(0.0 勘查既定, /api/health forecast_engine=down), 方案要求的"实测推理峰值×1.5"**本波无法执行**; 4g 沿用 P1-14 既定值, 校准项登记 KNOWN_ISSUES(KI-005, 8010 首次部署前实测校准, 不许拍脑袋)。
- **static 陈述实证(方案观察已被前波修复)**: build.sh 实际行为 `rm -rf static && cp -r frontend/dist/* static/`、Dockerfile:162 `COPY --from=frontend-builder /app/frontend/dist ./static/`、AGENTS.md:19 陈述三方一致; frontend/vite.config.ts 无 outDir 覆盖(默认 dist)——方案"声称与实际不符"不复存在, 仅记录实证无改动。
- 验证: `docker compose config --quiet --no-interpolate` 主/infra 两文件均 0 error; `bash -n deploy_panwatch.sh` 语法通过。**遗留(登记 KI-004)**: 生产 panwatch 容器(非 compose 管理)重建以套用 1500m 限额属生产可见变更, 待老板确认后随下次维护窗口执行。
- [branch fix/wave4-流程债-20260909, `git show HEAD`]

### feat-指令文件与版本号统一+行尾欠账后第4波启动(W4.1/F1)
- 背景: 4.1 —— ①CLAUDE.md(96 行)与 AGENTS.md 大量重叠且互相不一致(commit type 一个写 `{feat,fix,docs,refactor,style,test,chore}`, 一个写 `{fix,feature,update,doc}`), AI 读到哪份按哪份做; ②版本号三处不一致: VERSION=v0.5.21(真值) vs README 徽章 v0.5.0 vs 拉取命令 v0.4.3; ③README 运行示例卷名 `sida_data`, 全仓其余地方均为 `panwatch_data`(docker-compose.yml:204,294-295 / deploy/deploy_panwatch.sh), 照 README 起容器会挂到空卷(数据"消失")。
- 做法: ①CLAUDE.md 改 3 行指针(规范唯一入口 AGENTS.md, 冲突以 AGENTS.md 为准); ②两 README 徽章 v0.5.0→v0.5.21 并加 HTML 注释"发版时随 VERSION 同步"; 拉取命令 v0.4.3→`:$(cat VERSION)` 形式并注明"版本以仓库 VERSION 文件为准"; ③卷名 sida_data→panwatch_data(两 README); ④AGENTS.md "Commit & Pull Request Guidelines" 重写: type 词汇表对齐实际主流 `{feat,fix,update,refactor,docs,test,chore,style,perf}`, CHANGELOG 标题格式固化 `### <type>-<中文标题>`(与 commit type 一致), 新增发版步骤行"VERSION bump 必须与两 README 徽章同 commit"。历史 CHANGELOG 标题不回改, 自本条起按新规范。
- 验收: CLAUDE.md=3 行(≤10); `grep v0.5.0|v0.4.3 README*` 命中 0; 卷名 grep `sida_data` 在 README/compose/deploy 脚本命中 0(全仓其余命中均为 Prometheus 指标名 `sida_datasource_failures_total` 的子串, 非卷名, 不属本项清理范围); commit type 三处(AGENTS/CLAUDE/CHANGELOG)一致。
- [branch fix/wave4-流程债-20260909, `git show HEAD`]

### update-v0.5.21生产部署(docker cp覆盖层, 冒烟9/9, 迁移143-148首执行)
- **生产部署**: tag v0.5.21(8ecfeb7 merge) 经 docker cp 覆盖层部署到 panwatch 容器(同 v0.5.19/20 既定路径)。步骤: 备份现行代码 tar.gz(WSL `/tmp/app_backup_pre_v0521_20260909_1438.tar.gz`, 12.9MB, 排除 data/static-data/downloads/node_modules/__pycache__) → `git archive v0.5.21` → 容器 `/app` 解包 → **显式 rm 3 个本波删除文件**(src/core/chat_tools.py、tests/test_chat_tools_a4.py、tests/test_chat_tools_p1p2.py —— 覆盖层解包不会删文件) → restart → 50s healthy → 冒烟 **9/9 通过**(7.4s), /api/health 报 version=v0.5.21, PG/Redis ok。
- **迁移对账(与发版条目预判完全一致)**: v108/v121/v124 因 checksum 变更各幂等重跑一次; **v143-148(收编历史 A 层)首次在生产库执行**, 全部 Applying 无 ERROR, 耗时 ~600ms; 多用户旧数据归 owner 5 表处理正常。调度器重启后 5 个 scheduler 正常。
- **/api/health 基线 diff(已解释非静默)**: 唯一新键 `components.forecast_engine = {status: down, url: http://172.19.0.1:8010, Connection refused}` —— **W3.6 验收预期项**(生产 8010 forecast 服务未部署, W3.6 后健康检查如实暴露而非隐藏); 其余组件 database/redis/biz_cache/scheduler/rate_limit 全 ok(biz_cache l1_entries 15→0 为重启冷启动)。
- **发版流程两坑(已记 tdai)**: ① `git archive` 前**禁止把失败命令与后续命令用 `&&` 串联在管道后** —— 首次 merge 失败被 `| tail` 吞掉退出码, `&&` 链继续把 tag v0.5.21 打在旧 commit 上并推送(已重打 8ecfeb7 并同步远端); ② 容器内备份 tar 必须用**绝对路径 `/app`**(WORKDIR=/app, 相对路径 `app` 解析为 /app/app 报 Cannot stat, 首次备份得到 45B 空包)。
- **迁移前备份的降级记录(非阻塞)**: src.db.backup 创建了 `/app/data/panwatch.db.bak.20260909_143911`(对遗留陈旧 sqlite 文件的 copy2); pg_dump 不在应用容器 PATH → **PG schema 快照跳过**。生产真库(PG)安全: v143-148 均为幂等 DDL, 且有 v0.5.20 时代全库备份兜底。此点提示 W4.2 资源限制排查时顺带确认应用容器是否需要装 pg_dump。
- 本波 0 个新增 schema 迁移(143-148 为已上线历史 DDL 的收编), 前端 W3.7 改动仅进仓库源码, 容器 static/ 构建产物未变(与发版条目预判一致)。
- [tag v0.5.21]

### chore-17文件行尾renormalize(CRLF→LF, 清除上游欠账)
- 背景: 0.0 勘查既定欠账 —— 19 个 .py 文件的 index blob 本身是 CRLF, 而 .gitattributes(2026-09-07)规定 `*.py text eol=lf` → 这批文件在任何新检出里永远显示"已修改"(工作副本被写为 LF, 与 index 的 CRLF blob 比对不等), 且阻塞 merge/checkout(本次 v0.5.21 合并被 test_chat_tools_a4/two、test_orderbook_a1 三文件挡下)。
- 做法: 对 17 个幻影脏文件执行 renormalize(仅行尾 CRLF→LF, `git diff --ignore-cr-at-eol` 为空, 零内容变化, 3767↔3767 对称); 另 2 文件已在历史波次中自然归一。合并后 main 侧 blob 全 LF, 永恒脏状态与 merge 阻塞一并消除。
- 验证: 内容零变化(忽略行尾 diff 为空); 全量套件在 renormalize 前的同内容树上已跑 1891 passed / 2 failed / 5 skipped(行尾不参与 Python 语义, 无需重跑)。
- [commit 1fb5ee5]

### update-发版 v0.5.21(风险整改第3波合入main)
- 本次发版内容: 方言层 src/db 收口(IS_PG 全仓唯一化+upsert/insert-ignore 统一模板+历史 A 层收编为迁移 143-148+check_is_pg_scope 门禁进 4 工作流, W3.1/D2) / server.py 巨石拆解为 src/bootstrap 装配层(2104→46 行+agent 装饰器自动发现+启动行为前后对账一致, W3.2/D1) / 删死代码 chat_tools.py(679 行)+工具注册表 registry 统一注册 41 工具+双 tool loop 合并+流式断开守卫(chat.py 3021→1974, W3.3/D3) / 口径治理产品化(caliber 契约+资金流出口标注+文档红线修订, W3.4/B3) / 单位一致性恒等式校验出口+每日对账 job+结算路径 Decimal(W3.5/B5) / 8000→8010 依赖改单向+DDE 端点唯一化+依赖方向图 CI 门禁 9 例(W3.6/D5+D6) / 前端 TanStack Query 服务端状态层+机会页去卡片化+/quote/:symbol 路由+审计重跑(W3.7/D7)。
- 部署注意: ① **启动时迁移 143-148 将首次在生产库执行**(收编的历史 A 层 DDL, 幂等已验证), 且 m108/m121/m124 因 checksum 变更各幂等重跑一次(去重无重复可去/CREATE IF NOT EXISTS/DROP IF EXISTS, W3.1 已验证安全); 迁移前自动双备份(sqlite copy2+pg_dump schema-only)。② **W3.6 后 /api/health 新增 components.forecast_engine 键**——生产 8010 forecast 服务未部署(0.0 勘查既定), 该组件报 down 属验收预期而非故障。③ 前端改动(W3.7)仅进仓库源码, 容器 static/ 构建产物不在 git 跟踪内不受覆盖层影响。
- 验证: 全量离线套件 **1891 passed / 2 failed / 5 skipped**(2 failed 均为已知本地环境损坏文件 ta_load_ohlcv_patch/thsdk_buffer_size, 从未进 CI, 与 wave3 分支基线一致; 较 W3.6 基线 +10 = W3.6 依赖方向 9 例+W3.5 对账例); 前端 tsc/vitest 17/17/eslint 0 error(W3.7)。
- [tag v0.5.21]

### feat-前端TanStack Query服务端状态层+机会页去卡片化+/quote/:symbol路由+审计重跑(W3.7/D7)
- 背景: D7 —— 前端历史包袱: ①136 处手写 setLoading/setError try/finally, 每页自绘加载/错误/空态, 无统一缓存与去重(切页即重取); ②2026-09-02 审计(《UI终端化_设计系统审计_研究.md》)判定的 P0"K 线弹窗承载无全屏路由 / Opportunities 卡片堆叠"两项; ③响应信封审计需复核裸 fetch 旁路。本波为**服务端状态层统一 + P0 收尾 + 审计基线重跑**, 不动后端。
- **TanStack Query 引入**(v5, `frontend/src/hooks/useApiQuery.ts` 新): useApiQuery/useApiMutation 薄壳, queryFn 固定 `cacheMode:'reload'` 绕开 fetchAPI 内置 30s _RESP_CACHE —— **TanStack 是唯一缓存所有者**, 否则 mutation 后 refetch 拿到旧缓存值; main.tsx 装 QueryClientProvider(全局 staleTime 30s / retry 1 / refetchOnWindowFocus false)+ `registerQueryClient` 单例, 登出流程 `clearQueryCache()` 配合既有 clearResponseCache。pnpm add @tanstack/react-query(npm install 在本 workspace 会挂, 必须走 pnpm)。
- **三页迁移**(可度量: `const [loading` 31 → **28**, `useState.*loading` 剩 1 为弹窗一次性加载, 合法): ①Audit.tsx 整页重写为 useApiQuery(['audit', user]), 刷新→refetch, ErrorBanner 加 dismissed 标志(数据失败≠空态的既有语义保留); ②Reports.tsx 列表迁移(['reports'], items+jobs 单请求), 弹窗内容保留一次性 fetch(非共享服务端状态); ③Profile.tsx GET /profile 与 /profile/stats 迁移, 编辑态(nicknameDraft 等)仍本地 state 由 data 同步, PUT 保存后 `setQueryData(['profile'], updated)` 就地更新缓存不整页重取, 头像变更广播事件保留。
- **机会页去卡片化**(Opportunities.tsx, 2195 行, 全站第三大页): 列表区从"每股 8-10 行信息全展开的大卡片"改为**表头对齐扫读行+点击展开明细** —— 桌面端 9 列表头(标的/动作/评分/入场/止损/目标/来源/操作)与行 grid 对齐; 扫读行 `role="button"`+`aria-expanded`+键盘 Enter/Space 可操作; 移动端保留名称+动作两列(三要素在展开区补齐); 多源共振 🔥×N 徽章、来源 chips 截断 +N、AI 评分入指标行; 展开区含完整信号文本/失效/策略/来源徽章/因子解释 chips/组合约束/反馈操作。**产物**: before-12(旧卡片 ~230px/股全展开) vs after-13(新扫读行 ~56px/股) 截图对照。
- **Quote 路由化补完**: App.tsx 新增 `/quote/:symbol` 路径式路由(PermGuard view_forecast, 与 /quote、/forecast 同渲染 QuotePage), QuotePage 做 path→query 参数规范化(?symbol=); stock-insight-modal.tsx(2806 行 biz-ui)加**「全屏行情」出口按钮**(桌面端带文字/移动端图标, `navigate(/quote/:symbol)` 关弹窗直达) —— 弹窗速览→全屏终端的动线闭环(09-02 审计 P0 残项收窄)。
- **审计重跑报告**(docs/research/UI终端化_设计系统审计_重跑_20260909.md 新): 校正 09-02 版三处失实 —— "缺失 6-7 页"全部存在; 13 个真实页面被漏盘(实际 28 页); 5 个条目已过时(hub 化后成 Tab)。28 页×路由矩阵实测核对(5 hub 壳全量可达, 无孤儿页面); 响应信封结论: 10 处裸 fetch 全为 blob 导出/报告沙箱/WS 合法旁路, **信封已全量采纳**; 遗留设计债 7 项排序(932 行任意 px / 7 处硬编码 hex / 2 页空态缺失 / loading 微组件重复 / 3 个死文件 / ShareCard 6 文件重复 / 第三套 Tab 实现); OpportunityRow 拆分切线已勘明(4 个行内专用纯函数可随组件走, 6 个跨区共用须导出不可复制)。
- **截图基线**(docs/screenshots/w37/, 25 张): before 取自 3047f87 worktree、after 取自本波工作树, 本地栈 8021(临时 SQLite+SIDA_ALLOW_SQLITE=1, **非 8000**), 4 账号视角(未登录/owner/member1/demo guest)+策略信号种子数据 8 行(临时库, 与生产无关)。顺带实证: /quote/600519 在 before 版回退首页、after 版直达行情页。
- 测试: 前端门禁 `npx tsc -b` 全绿 / `pnpm test`(vitest) 17/17 / `pnpm lint`(eslint) 0 error(items 派生数组 useMemo 包裹修 exhaustive-deps); 后端未动, 全量离线套件沿用 W3.6 基线 1881/2/5。**坑**: ①vite.config strictPort 且 pnpm 传参会把 `--` 字面透传, 双 dev server 需 `pnpm exec vite --port N`; ②pnpm workspace 的 biz-ui 包内 react-router-dom 经主包提升解析, useNavigate 可直接用(KpiBand 先例)。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### feat-依赖方向单向化8000→8010+DDE端点唯一化+口径矩阵D6行(W3.6/D5+D6)
- 背景: D5 —— 8000(主服务)与 8010(预测引擎)之间历史上有三条通道: ①8000→8010 预测代理(合法编排), ②8010→8000 HTTP 回调(含**网关探测兜底** `_detect_panwatch_url`: 172.17.0.1/172.18.0.1/10.8.0.1 逐个试 + `/api/providers/services` 未鉴权读 LLM 配置 —— 同一配置既有服务令牌通道又开了一个裸口), ③sqlite 直读(0.0 已切 HTTP)。D5 目标: 依赖改**单向** 8000→8010, 配置由编排方推送而非 8010 自取; D6 目标: 同类能力端点唯一化 + 暗盘 source 开关登记进口径矩阵。
- **8010 侧删探测/回调**: forecast_lib/forecast_sentiment.py 删 `_detect_panwatch_url`(网关探测+硬编码网关 IP 兜底)与 `_load_llm_config` 的 /api/providers/services 回调分支; 新增 `RUNTIME_LLM_CONFIG` + `set_runtime_llm_config()`(8000 编排方经 /predict payload 推送, 优先级最高, 免重启), 独立进程跑 GET /predict 时 LLM 配置仍兜底 `~/.panwatch_forecast.env`; panwatch_client.get_panwatch_url() 环境变量更名 `SIDA_MAIN_API_URL`(默认 http://127.0.0.1:8000 不变); panwatch_bridge docstring 同步。
- **8010 新增 POST /predict 编排入口**(forecast_server.py): body `{symbol, days, task_id, target_date, force, llm_config}`, `set_runtime_llm_config` 后复用既有 `_predict_with_guard`(symbol 校验+结果缓存+409 节流+semaphore, 从 GET 原体逐字抽出, GET 行为零变化); GET /predict 保留给 systemd/手动调用。
- **8000 侧编排推送**: src/web/api/forecast.py 新增 `_forecast_llm_payload(db)`(AppSettings forecast_llm_base_url/model/api_key 三键归一), forecast_predict 改 `client.post(f"{FORECAST_ENGINE_URL}/predict", json={..., "llm_config": ...})` —— api_key 经内网 8000→8010 传递(与既有 service_config 通道同模式, 不落日志); 13 处降级文案统一为"预测引擎不可用(需在主机运行 forecast_server.py)"。
- **健康反映**: src/web/api/health.py 新增 `components.forecast_engine`(`_forecast_engine_probe`: 30s 缓存 + 1.5s 超时探 8010 /health, 结果喂既有 record_component_status 监控链); 8010 是可选加速器, down **不翻转 overall**。
- **compose**: forecast 服务环境变量 `PANWATCH_URL` → `SIDA_MAIN_API_URL`(值不变 http://panwatch:8000; PANWATCH_SERVICE_TOKEN 与此禁令无关, 不动)。
- **CI 源码级门禁 tests/test_w36_dependency_direction.py(新, 9 例)**: forecast_lib/ 禁止出现旧环境变量名 `PANWATCH_URL` / 硬编码网关 IP(172.17.0.1 等)/ 内联硬编码 http://*:8000 拉配置; forecast_lib+forecast_server 禁止 `import src.*`; src/ 禁止 `import forecast_lib`(编排只走 HTTP, 唯一既有 mention 是 chat.py docstring); forecast.py 必须含 POST /predict+llm_config; thsdk_ext 禁 /dde 路由 + thsdk_extended 必有 /dde/{symbol}; compose 必含 SIDA_MAIN_API_URL 且无 `PANWATCH_URL=`; docs/dependency_direction.md 存在且含 mermaid。
- **DDE 端点唯一化(D6)**: `/api/thsdk/dde/{symbol}`(thsdk_extended.py)定 canonical 并**补 30s TTL 缓存**(thsdk 是限频源, 原 ext 端点的缓存不能随合并丢失); 重复的 `/api/thsdk/ext/dde/{symbol}`(thsdk_ext.py)删除 —— 两版响应结构不同且全仓无前端调用方(grep 验证, chat 工具走 data_source 直连不受影响), 保留结构更扁平的 canonical 版; 测试同步(test_thsdk_ext: merged_away 断言 404 + 无 _DDE_CACHE; test_thsdk_extended: autouse 清 TTL 缓存 fixture)。
- **文档**: docs/dependency_direction.md(新, 权威依赖图): mermaid 拓扑(前端→8000→8010 单向编排)+5 条方向规则(编排单向 / 白名单回调清单: AI 裁判对话+shadow profile+forecast-config+数据桥全部 X-Service-Token / 禁止项 / 优雅降级 / 端点唯一化), **新增回调须先在本文登记并走评审**; docs/_frozen/caliber_matrix.md 增 2 行(D6): 内外盘 inner_outer(tick 随 source 可变+Quote 兜底字段无方向语义) + `/api/dark-flow` source 开关(`_SOURCE_ALLOW`: tencent_ticks/tdx_tck=tick 可判方向 vs thsdk/thsdk_big_order=ths 禁止) + file:line 实测依据。
- 顺带修: test_thsdk_extended.py::test_thsdk_extended_router_registered 对新版 FastAPI 恒挂(路由以 `_IncludedRouter` 包装, app.routes 无扁平 path, 端点集变 {None,'/api/version'})→ 改用 `app.openapi()` paths 断言(294 路径, DDE 新拓扑一致); 该文件本就在全量套件 ignore 列表, 不动基线。
- 测试: 新增 tests/test_w36_forecast_orchestration.py 10 例(POST /predict 推送 llm_config 落 RUNTIME_LLM_CONFIG / 无 body 配置清 runtime / days 非 int 400 / GET 不触 runtime / 推送配置优先级压过本地 env / 8010 停机 ConnectError→503 明确文案非 500 / _forecast_llm_payload 三键归一 / health probe down+ok+30s 缓存窗口); 邻域 69 例全绿(orchestration 10 + 依赖方向门禁 9 + test_thsdk_ext/test_thsdk_extended/test_forecast_container_config/test_forecast_config_channel/test_ai_referee_http/test_p4_alerts/test_health_metrics_guard 50)。
- 验证: 全量离线套件 **1881 passed / 2 failed / 5 skipped**(6 分 14 秒; 较 W3.5 基线 1872 +9 即依赖方向门禁, 另 10 例 orchestration 在邻域轮跑绿; 2 failed 仍为已知本地环境损坏 ta_load_ohlcv_patch/thsdk_buffer_size, 不进 CI, 与本次改动无关); **坑**: forecast_server direct-run 把 forecast_lib/ 插进 sys.path, bare `forecast_sentiment` 与 `forecast_lib.forecast_sentiment` 是两个模块实例 —— 生产消费 bare 实例, 测试断言必须对同一实例(orchestration 测试首版读错实例已修正)。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### feat-单位一致性恒等式校验+每日对账job+结算路径Decimal(W3.5/B5)
- 背景: B5 —— 单位错配历史上真实发生过(P1-13 main_net_inflow_pct 东财/腾讯 ×100, W1.1 已修), 但全仓仍无一道"volume(股)×price(元)≈amount(元)"恒等式出口校验, 再发生只能靠人眼看盘面离谱程度。且方案原拟直接在现网 qfq 数据上校验, **实测证伪**: 000651 除息前 qfq 柱 dev≈5.6%(复权因子本身), 2% 阈值会全量误报 → 重设计为只校**未复权(fqt=0)**数据, 阈值按实测分布校准而非拍脑袋。
- **src/core/unit_check.py(新, 恒等式出口校验)**: `unit_deviation(volume, close, amount)`(amount/volume 与 close 的相对偏差%) + `assert_unit_consistency(symbol, trade_date, volume, close, amount, source)`; amount/volume 缺失或 ≤0 → None 跳过(诚实缺失语义, 绝不当 0 参与计算); `DEFAULT_TOL_PCT=5.0`(**实测校准**: 东财 push2his fqt=0 n=120 柱 max dev=1.285% / p99=1.19% / p95=1.02% / p50=0.29%, 5.0% ≈ 4× 实测最大, 又比最小单位错 ×2 对应 dev≈100% 低 20 倍; SIDA_UNIT_TOL_PCT 可覆盖, SIDA_STRICT_UNITS=1 fail-closed 抛供入库门禁); 超阈值 logger.error + 复用 0.2 失败计数器 record_datasource_failure(source, kind="parse")(lazy import, 单元测试/独立脚本无 server 也能跑)。
- **src/core/unit_recon.py(新, 每日对账 job)**: 东财 push2his **显式 fqt=0** 未复权取数(volume 手→股 ×100, amount=f57 元原始), 样本池 get_default_symbols CN 前 20(拉取异常回退 8 只流动性股), 逐柱 assert 后汇总 max_dev/max_dev_at/violations, 报告落 DATA_DIR/reports/unit_recon/YYYY-MM-DD.json(W2.2 DATA_DIR 唯一口径); 单股失败只记警告 fail-soft 不影响整体报告。
- **调度接入**: 挂在既有 KlineBackfillScheduler 上作第二个 cron(id=unit_recon_daily, 18:35 mon-fri, 参数与既有 job 统一 misfire_grace_time=300/max_instances=1/coalesce=True)而非新建第 7 个 scheduler —— /api/health "scheduler 5 running" 冒烟基线保持不变(W3-REL 比对面); 交易日 gate + to_thread + 异常仅 warn。
- **src/core/money.py(新) + 结算路径 Decimal**: to_dec(float→str→Decimal 往返, ≤4dp 精确)+ q2/q4/q6(ROUND_HALF_EVEN); cost_model.fill() 全程 Decimal(fill_price 6dp / gross·fees·cash_delta 4dp); paper_trading_engine 四处结算落点(compute_market_cash / _close_position / _check_entries / _update_account_metrics)Decimal 化 —— 消灭 10000×0.3=3000.0000000000005 类浮点伪差; **分界明确**: DB 列与对外 API 仍 float(注释写明), 本波不动 schema。
- **marketdata Bar.amount(加法, 不破坏)**: Bar 增 `amount: float | None = None`(东财 f57 元原始, 不做手/股换算; 其余 vendor 不产 → None → 校验自动跳过); fetch_eastmoney_kline fields 增 f57, amount=0 → None(诚实缺失); docstring 标注 qfq 数据不做恒等式校验的原因并指回 unit_check。
- **docs/_frozen/data.md**: 在既有"腾讯 turnover=元"实测证据基础上合并出完整数据层单位契约 —— 5 源 K线单位表(东财手+元/腾讯手/新浪股/TQ 股/Yahoo 无 amount)、恒等式 dev 实测分布(fqt=0 vs fqt=1 000651 全部除息前柱)、tol=5.0 校准依据、校验落点(unit_check + 18:35 job + 报告路径)、结算 Decimal 分界。
- 测试: 新增 tests/test_w35_units.py 10 例(×100 手当股 dev 9900% / ×10⁴ 万元当元 dev 99.99% / 缺字段 None / 容差内放行 / 超阈 log+失败计数器断言(mock record_datasource_failure) / strict 抛 / 自定义与环境容差 + DEFAULT_TOL_PCT==5.0 锚点 / 东财 f57 解析(amount>0 与 amount=0→None) / Bar 默认 None / 对账 job 产出报告(注入 ×10⁴ 脏柱 → 1 violation dev>99, 报告文件内容断言) / 池失败回退) + tests/test_w35_settlement.py 7 例(fill 与独立 Decimal 重算在 5 价位×2 方向矩阵逐字段相等 / 0.1×3=0.3、0.07×10⁷=700000.0 浮点漂移场景 Decimal 精确 / round_trip pnl 无漂移 / market_cash 10000×0.3==3000.0 精确 / 真实模型 _close_position 结算精确值 88.3793/8.79%/capital 精确到 4dp / drawdown 8.33% Decimal); 邻域 kline/scheduler/cost/settlement 111 例全绿。
- 运维注: 本机东财 push2his 当日累计 ~90 请求后触发限流("Server disconnected without sending a response"), live 对账跑通 fail-soft 路径(报告产出、bars_checked=0), 生产 18:35 每日节奏 + 20 样本不会触及。
- 验证: W3.5 新测试 17/17; 全量离线套件 **1872 passed / 2 failed / 5 skipped**(11 分钟全跑一轮, 跑出 W3.1 源码一致性测试误报新 job misfire_grace_time=600 → 统一改 300 后重跑相关文件 42 例全绿, 即 1871+1; 较 W3.4 基线 1855 passed +17 即本波新测试; 2 failed 仍为已知本地环境损坏 ta_load_ohlcv_patch/thsdk_buffer_size, 不进 CI, 与本次改动无关)。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### feat-口径治理产品化caliber契约+资金流出口标注+文档红线修订(W3.4/B3)
- 背景: B3 —— "主力净流入"类指标与"主力意图"是两个口径: 东财/腾讯四档是**按单金额归类**净额, 与逐笔主动买卖方向可能相反; 此前只有 chat.py 0.0 系统提示里一段 prompt 文案约束, 无代码层类型, 下游(signal_pack/forecast/UI)拿到 get_capital_flow 数据时无从判断能不能做方向性结论, docs/PROJECT_MAP.md:57 甚至还在推荐 get_capital_flow(与 AGENTS.md 红线自相矛盾)。
- **src/core/caliber.py(新, 口径契约)**: `Caliber` Literal[tick/eastmoney4/ths/unknown] + direction_semantics 语义常量 + `CaliberTag` frozen dataclass(caliber/direction_semantics/source, ui_label() 中文出口文案, to_dict() 四键透传) + `CAPITAL_FLOW_TAG`(collector 全部取数路径同属按单金额归类, 统一 eastmoney4 标) + **`require_directional(tag, usage)`** 出口校验(非 tick 抛 `CaliberViolationError`, 指回 get_main_intent) + **`reconcile_direction(main, main_value, ref, ref_value)`** 双口径裁决(方向冲突 → statement 明确说明"逐笔主动买卖盘 vs 按单金额四档归类"差异并**以逐笔为准**; main 非 tick 直接抛 —— 方案 B3 验收"构造方向相反场景输出差异+优先逐笔"从 prompt 承诺变成代码可验证)。
- **collector 口径随数据透传**: capital_flow_collector.CapitalFlow 增 caliber/direction_semantics/source_label 字段(默认值即正确: 东财 push2 直连/网关、腾讯四档、Engine 四档全部构造路径同属归类口径)+ caliber_tag(); `get_capital_flow_summary` 返回 dict `**CAPITAL_FLOW_TAG.to_dict()` 展开透传(signal_pack/chat/forecast/UI 拿到数据即见口径)。
- **三个消费出口全部标注**: ① chat.py `_fetch_capital_flow_context` 资金流上下文首行缀 CAPITAL_FLOW_TAG.ui_label() + "口径说明: 按单金额四档归类, 禁止用于主力意图判定; 主力意图一律以 get_main_intent(逐笔)为准, 冲突时优先采信逐笔"; ② forecast.py payload["capital_flow"] 增 caliber/direction_semantics/caliber_label 键; ③ signal_pack 过时注释("wudao 盘中实时→Engine 四档兜底"与现链路不符)替换为真实链路+口径治理注释(summary dict 连标签整体进 SignalPack.capital_flow)。
- **豁免显式化**(get_capital_flow 全仓命中逐一处置, 无悬空引用): data_collector.py 两处(存储路径/健康检查连通性, 不做方向判断)、portfolio_context.py(只注入逐笔口径数据, 不消费 get_capital_flow)、registry.py get_capital_flow handler(W3.3 已带红线前缀, 原样)。
- **UI 标注**: market_data.py market-capital-flow 代理返回增 caliber/caliber_label; Dashboard 大盘资金流标题增"四档口径 · 资金面参考"chip(title 提示禁用主力意图判定), DarkFundTop 增"DDE口径 · 资金面参考"chip —— 与既有 thsdk_dde 数据源标注对齐; dashboard.ts 类型同步。
- **文档矛盾修复**: AGENTS.md 口径红线条目由一句话升级为 4 条可执行规则(用途映射/口径类型/冲突裁决 require_directional+CaliberViolationError/UI 标注), 指向 src/core/caliber.py 与 **docs/_frozen/caliber_matrix.md(新)** —— 9 类资金指标 × 数据源/口径类型/方向语义/单位/可用于方向判定矩阵 + 使用规则 + file:line 实测依据(northbound kamt 断供 2024-08、TQ 通达信网关、ths_flow data.10jqka 单位亿等); PROJECT_MAP.md 标题/49/57 行修订: 8010 资金面应走 get_main_intent(逐笔), get_capital_flow 仅作资金面参考**禁止**主力意图判定。
- 测试: 新增 tests/test_w34_caliber.py 15 例(require_directional tick 放行/四类拒绝含文案锚点、reconcile 冲突场景断言"口径冲突"+"以逐笔为准"+agree 场景"方向一致"+main 非 tick 抛、CapitalFlow 默认口径+caliber_tag、summary dict 四标签透传(monkeypatch 不打网络)、chat 资金流上下文含 ui_label+口径说明+优先采信逐笔(monkeypatch)、PROJECT_MAP 旧推荐话术删除+AGENTS.md 可执行规则+caliber_matrix ≥5 指标×≥3 数据源家族、signal_pack 透传链路结构断言); 邻域 test_capital_flow_routing/test_capital_flow_zero_guard/test_signal_* + test_ai_layer_data_sources + test_w33_chat_registry 61 例全绿。
- 验证: W3.4 新测试 15/15; 邻域 61 例; 前端门禁 tsc --noEmit 干净 + vitest 17/17 + eslint(3 个触碰文件)零告警; 全量离线套件 **1855 passed / 2 failed / 5 skipped**(较 W3.3 基线 +15 即本波新测试; 2 failed 仍为已知本地环境损坏 ta_load_ohlcv_patch/thsdk_buffer_size, 不进 CI, 与本次改动无关)。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### refactor-删死代码chat_tools.py+工具注册表registry+合并双tool loop+流式断开守卫(W3.3/D3)
- 背景: D3 —— src/core/chat_tools.py(679 行)是"生产死代码": 39 个工具包装函数与 chat.py 内联实现同名并行维护, 全仓无任何调用点(引用全部来自其自身测试, server 启动链不触达), 却持续随实现漂移; chat.py 3021 行同时承担 30 个工具 handler + 40 个 schema + 流式/非流式两套近乎重复的 tool loop。
- **删除**: src/core/chat_tools.py 整文件(679 行); tests/test_chat_tools_a4.py + tests/test_chat_tools_p1p2.py(纯包装层测试)git rm; test_chat_tools_two.py 263→137 / test_orderbook_a1.py 187→156(删包装层用例, 保留 to_ths_code/find_tck_file/dark_flow_from_tck/ImgSnapshot/算法等底层实活测试); src/core/dark_flow.py 删一条指向 chat_tools 的过时 docstring。
- **src/agents/chat/registry.py(新, 1314 行)工具注册表**: ChatTool frozen dataclass(name/schema/handler/caliber/requires) + `@register_chat_tool` 装饰器(重复注册 raise ValueError) + chat_tool_schemas() 按注册顺序导出。41 个工具 schema 与 handler 同处注册: 29 核心(原 CHAT_TOOLS 顺序不变) + get_opportunities(handler-only 无 schema, 兼容存量) + 11 thsdk(schemas 由 build_thsdk_tool_schemas 提供, handler 经模块属性解析 THSDK_TOOL_HANDLERS, 保留 tt._l2 monkeypatch 面)。**caliber 口径标签强制非空**: get_capital_flow 固定红线文案"东财四档，方向位与逐笔口径相反，禁止据此判定主力意图"; **requires="self"** 标注 get_portfolio/get_stock_suggestions/get_watchlist/get_notifications(S5 用户维度读数)。handler 内部延迟 import chat.py 助手, chat.py→registry 单向 import 无环; 30 个核心 handler 分支体逐字搬移(仅删过时的局部 import asyncio 与相应注释)。
- **chat.py 3021→1974 行**(≥800 验收, −1047): CHAT_TOOLS 块与 40 个 schema 删除改 registry 导入; _execute_tool 变薄调度器(注册表查找→handler(db,args,user), 未知工具/异常文案与原一致); 流式/非流式两个 tool loop 合并为 `_run_tool_loop(..., *, stream=False)` 单实现, 事件语义逐项保持: stage 提示/delta/done/text、chat_multi 兜底、LLMDegradedError 不兜底重试(0.3)、MAX_TOOL_ROUNDS=5 兜底、异常兜底。
- **流式端点断开守卫**: send_message_stream 增 `request: Request`, 工具循环消费处 `request.is_disconnected()` → break 中止循环(停止 LLM 计费); `except asyncio.CancelledError: log+raise`(BaseException 不被外层 except Exception 吞, 挂起中的上游 LLM 调用随任务取消终止)。
- **兼容面零破坏**: chat_api.CHAT_TOOLS / _exec_thsdk_tool(未知工具文案原样) / _execute_tool monkeypatch 面 / tools_thsdk tt._l2 全保留; test_ai_layer_data_sources.py::test_chat_news_uses_flash_news 结构性断言改指 registry.py(handler 新家, flash_news 先于悟道 news_hotlist 的顺序断言原样保留)。
- **测试**: 新增 tests/test_w33_chat_registry.py 23 例(死代码全仓 *.py grep=0、注册表完整性 41 工具/caliber 非空/requires 合法/schema 名一致、get_capital_flow 红线、self 集合、重复注册拒绝、导出顺序、调度器未知工具/异常文案、合并 loop 12 例 mock AI client 语义对账(正常出字/tool 轮次消息入列/chat_multi 兜底/降级不兜底/轮次兜底含 stage 事件/异常兜底)、流式端点含 is_disconnected+CancelledError、chat.py ≤2221 行、_exec_thsdk_tool 兼容面); 邻域回归 test_chat_thsdk_tools/test_chat_l2_tools/test_chat_ai_layer_tools/test_chat_stream/test_chat_tools_two/test_orderbook_a1 68 例全绿。
- **断开守卫真实行为验证**(本机): 隔离环境 uvicorn(127.0.0.1:8021, 临时 DATA_DIR+sqlite) + 黑洞 LLM 端点(18111 接受连接永不响应, 默认模型指向它) → 真实 curl 流式请求收到 stage 事件后 --max-time 3 断开 → 服务端日志出现"流被取消, 清理上游 LLM 调用"(CancelledError 守卫在真实 uvicorn 运行时生效)。
- 验证: W3.3 新测试 23/23 + 邻域 68 例 + 全量离线套件 **1840 passed / 2 failed / 5 skipped**, 2 failed 均为已知本地环境损坏(ta_load_ohlcv_patch/thsdk_buffer_size, 不进 CI, 与本次改动无关)。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### refactor-拆server.py巨石为src/bootstrap启动装配层+agent装饰器自动发现(W3.2/D1)
- 背景: D1 —— 根目录 server.py 2104 行同时承担 FastAPI 入口/AGENT_REGISTRY 手工注册/seed_* 种子/数据源对账/调度器编排/启动自检/代理与日志环境; AGENTS.md 还要求"新 agent 必须改 server.py 两处", 约定本身在制造巨石。
- **src/bootstrap/ 新装配层(6 模块)**: app.py(create_app: discover_agents → lifespan 挂载 → SPA 兜底路由, 幂等可重入) / agents.py(AGENT_REGISTRY + `@register_agent("<name>")` 装饰器 + discover_agents() pkgutil 自动发现 src.agents 全部模块 + seed_agents) / datasources.py(DATA_SOURCE_SEEDS 43 条 + seed_data_sources/_seed_providers_by_type/reconcile_data_sources/seed_strategies/seed_sample_stocks, 原样搬移) / runtime.py(6 个调度器全局实例 + load_watchlist/load_portfolio/resolve_ai_model/resolve_notify_channels/_build_notifier/_build_ai_client/build_context/build_scheduler/reload_scheduler/trigger_agent/trigger_agent_for_stock, 原样搬移) / startup.py(lifespan: 方言门禁→SIGCHLD→init_db→自检→Redis 预热→环境装配→init_auth→种子→TA 建议回填→股票缓存后台刷新→选主→调度器×6+微信 BOT worker→yield→全量 shutdown) / env.py(apply_proxy_env/setup_proxy/setup_ssl/setup_logging/_ConsoleNoiseFilter/setup_playwright)。
- **server.py 2104→46 行**(≤50 验收): 只留 `app = create_app()` + PEP 562 `__getattr__` 兼容转发 + uvicorn 入口。转发是关键设计: 调度器实例是运行期被 lifespan 写入的可变状态, 按 re-export 值拷贝会把 None 固化, `__getattr__` 让 `import server; server.scheduler` / `from server import price_alert_scheduler` 永远读到 runtime 的 live 值 —— health 深检/price_alerts/templates/datasources/stocks/presets/settings/agents 等 12 处存量调用面零改动零破坏。
- **新 agent 约定: 一个文件 + 一个装饰器**(验收 demo 已验证后删): 在 src/agents/ 新建文件, 类上加 `@register_agent("name")` 即完成注册, 不再改任何手工 dict; 配置种子仍走 AGENT_SEED_SPECS。9 个内置 agent(daily_report/premarket_outlook/news_digest/chart_analyst/intraday_monitor/tradingagents/theme_launch_detector/stock_attribution/auction_review)已全部改为装饰器注册。AGENTS.md "Coding Style" Agent 条目同步更新。
- **路径修正两处**: 拆进 src/bootstrap/ 后 `__file__` 变深两级, setup_ssl 的 data/ca-bundle.pem 与 SPA 的 static/ 改经 REPO_ROOT 解析(容器内 /app 不变); logger 名保持 `logging.getLogger("server")`, 启动日志在 UI 日志板/DB 里与拆分前逐字一致。
- **启动行为前后对账(验收第3条, 本机实测)**: 用 git worktree 取 HEAD(e28a915, 拆分前)与本分支各起一个真 uvicorn(临时 DATA_DIR + SIDA_ALLOW_SQLITE=1, 端口 8021/8022), 逐项比对: /api/health 全字段一致(components 集合 {biz_cache,database,rate_limit,redis,scheduler}、scheduler degraded/leader_state=failed/0 实例(Redis 不可用 fail-closed, 预期)、database sqlite ok、service 键集)、startup_check 7 项输出一致(门禁/方言/jwt/data_dir/zhitu/notify/thsdk)、启动日志序列逐行一致(数据源对账 43 条清单逐字相同、策略目录、示例股票 5 只、选主 fail-closed、Uvicorn running)。生产基线(v0.5.20 容器 /api/health: status ok, 5 scheduler running)已存档, 发版部署后按同口径复对。
- 测试: 新增 tests/test_w32_bootstrap.py 7 例(shim ≤50 行且无注册表/lifespan 残留、PEP 562 live 转发语义、存量 from-server import 面 11 名逐一可导入、9 内置 agent 装饰器注册齐、demo agent 写文件即被发现+幂等+验后删、create_app 装配与幂等不重复挂路由、static/ca-bundle 路径解析到仓库根)。
- 验证: W3.2 新测试 7/7 + test_datasource_reconcile 回归绿; 本机真实启动对账(上述)通过; 全量离线套件结果见下条发版汇总。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### refactor-方言层src/db收口+历史A层迁移收编143-148(W3.1/D2)
- 背景: D2 —— IS_PG 布尔散布 19 处/6 文件, upsert/insert-ignore 在 summary_cache/mainline/l2_ticks 各自手写 PG/SQLite 双分支(其中 summary_cache 分支因 PG 折叠未加引号标识符+SQLite 关键字大小写不敏感本来就是冗余); database.py 876 行里 660 行是"历史 A 层" _migrate* 无版本迁移, 与 B 层(101-142)并行跑且 A 层先于 B 层 —— 一旦 B 层迁移依赖 A 层补的列, 古老库会炸。
- **src/db/ 方言层(新包)**: dialect.py 独占 env 解析(DATA_DIR→DB_PATH, DOCKER=1 无 SIDA_DB_URL 且无 SIDA_ALLOW_SQLITE=1 → RuntimeError fail-fast)、`IS_PG` 模块级布尔(全仓唯一)、is_postgres()/declared_backend()/build_engine()(PG pool_pre_ping/20+40/timeout 10/recycle 1800 + statement_timeout 8000; SQLite NullPool+WAL 五 pragma+busy_timeout 60s)、acquire_write()(SQLite 写信号量, 30s 超时报"数据库写入繁忙")、upsert_sql()/insert_ignore_sql()(统一模板, excluded. 小写双方言通吃, 删业务处双分支); backup.py 收编迁移前备份(sqlite copy2 / pg_dump --schema-only, PGPASSWORD 只走 env)。database.py 876→约76 行(保留 Base+重载防御+engine/SessionLocal/get_db), init_db 变薄: create_all → 有待跑迁移先双备份 → run_versioned_migrations。
- **历史 A 层收编为新版本迁移 143-148**: _m143_legacy_missing_columns(18 项缺列补齐+悬空 ai_provider FK 清理+sort_order 回填+4 张兜底表)、_m144_legacy_old_providers、_m145_legacy_settings_to_models、_m146_legacy_positions_to_accounts、_m147_legacy_remove_stock_enabled、_m148_legacy_add_user_id_columns。修掉 **A/B 层顺序危害**: 现在版本号统一 101→148 顺序跑, m108 加 stock_suggestions.meta 存在性守卫、m121 加 stocks.user_id(has_user) 守卫后才有唯一索引、m124 自建 analysis_history.user_id —— 三个 B 层迁移改为自足, 不再假设 A 层先跑过。
- **m108/m121/m124 加固属最小必要改动**: checksum=sha256(version:name:runner源码) 随之变化 → 已部署库下次发版会**幂等重跑各一次**(已验证安全: 去重逻辑无重复可去、CREATE IF NOT EXISTS、DROP CONSTRAINT IF EXISTS+ADD); 其余 142 个迁移零字节不动, checksum 稳定。
- **业务侧收口**: summary_cache(put_cached_summary 走 upsert_sql, 顺带修 params 键名 ts/ttl→computed_at/ttl_s 与列名对齐 —— 旧键名下 fail-soft 吞异常致缓存写入静默失败)、history_store.persist_l2_ticks(insert_ignore_sql)、startup_check/health 错误路径(declared_backend)、market_mainline._upsert_today_snapshot(upsert_sql)、kline_backfill_scheduler 改从 sqlalchemy 导 create_engine(原 src.web.database.create_engine 是重写前意外再导出, 调度器建裸引擎本就该走 sqlalchemy, 行为零变化)。
- **CI 门禁 scripts/check_is_pg_scope.py**: rglob 全 *.py 扫词法 token, IS_PG 只许出现在 src/db/ 下(含测试, 门禁脚本自豁免); 接入 4 个工作流(build-and-push-image/release/build-push-acr/build-push-acr-forecast), 紧随 check_scoped_queries 步。AGENTS.md "SIDA 业务硬约束"新增数据库分层条目。
- **测试**: 新增 tests/test_w31_db_dialect.py 8 例(门禁干净/违规可抓、upsert/insert-ignore 模板与双方言分支、真 SQLite upsert 往返、legacy_sqlite_engine 造古库→create_all+run_versioned_migrations 后 143/148 生效且二次运行幂等、全新库 applied==全量); test_pg_default._reload_database 改为先 reload dialect 再 reload database 并返回 (dbd, db)(engine=build_engine() 读 dialect.DB_URL); test_auction_pool/test_market_phase/test_startup_check* 改 patch src.db.dialect.is_postgres(裸 IS_PG 全局已被门禁禁改)。
- 验证: 门禁通过("IS_PG 只存在于 src/db/ 方言层"); check_migrations 通过(48 迁移 101-148 连续、命名合规); 邻域回归 w31 8例 + pg_default/startup_check/auction_pool 31例 + summary_cache 5例 + migrations/history/mainline 46例全绿; 全量离线套件 **1972 passed / 4 failed / 7 skipped**, 4 failed 均与本次改动无关且不进 CI: ta_load_ohlcv_patch/thsdk_buffer_size(已知本地环境损坏)、test_dark_l2_engine 与 test_thsdk_extended(文件级 pytestmark=network 且 release.yml 主门禁 --ignore, 本地全量跑时 thsdk 实时行情/导入环境不可用, 单跑 test_dark_l2_engine 6/6 通过)。
- 明确不做: migrations.py 不随迁 src/db/migrations/(checksum 稳定与生产安全优先于目录美观, 收编已用新版本号实现等价效果); SessionLocal 不拆 session.py(reload 脆弱); 仓库层抽象不在本波。
- [branch fix/wave3-依赖与口径-20260909, `git show HEAD`]

### update-v0.5.20生产部署(docker cp覆盖层, 冒烟9/9)
- **生产部署**: tag v0.5.20(752b35b merge) 经 docker cp 覆盖层部署到 panwatch 容器(同 v0.5.19 既定路径)。步骤: 备份现行代码 tar.gz(WSL `/tmp/app_backup_pre_v0520_20260909_072719.tar.gz`, 排除 data/downloads/node_modules/__pycache__) → `git archive v0.5.20` → 容器 `/app` 解包 → restart → healthy → 容器内签发 owner token 跑 scripts/smoke_test.py **9/9 通过**, /api/health 报 version=v0.5.20, PG/Redis ok。static/data/downloads 均不在 git 跟踪内, 覆盖层不触碰生产数据与前端构建产物。
- **发版冒烟两处坑(已记 tdai)**: ① 容器内签 token 必须带 `PYTHONPATH=/app`(脚本放 /tmp 跑时 src 不可导入, v0.5.19 同坑); ② 冒烟用 token 的用户角色是 `owner` 不是 `admin`(M2 多账号口径, filter role=='admin' 查不到会静默拿到空 token → 全 401)。
- 本波 0 个 schema 迁移(纯代码+CI/测试/门禁面), 启动日志无报错。
- [tag v0.5.20]

### update-发版 v0.5.20(风险整改第2波合入main)
- 本次发版内容: 多租户数据访问统一收口(W2.1/C3: scoped()/owned_or_404()/writable() 三助手+20处越权面修复+AST静态门禁进4个CI工作流) / 测试与真实 DATA_DIR 彻底隔离(W2.2/E4: conftest 顶层重定向+Base重载防御) / 联网测试打标 network+CI 主门禁 `-m "not network"`+夜间网络工作流(W2.3/E2) / 前端四道门禁(W2.4/E3: vitest 17例+eslint 铁律+UI规则 R6棘轮/R7禁null→0+Dockerfile 恢复 tsc) / 覆盖率棘轮+requirements-lock 171包精确锁定+dependabot 三生态+pnpm/pip 双审计入档(W2.5/E5+E6) / 统一交易日历接线9处+缺失年份显式报错 TradingCalendarError(W2.6/B6)。
- 部署注意: 本波 0 个 schema 迁移(纯代码+CI/测试/门禁面); 交易日历静态表覆盖 2025-2027, **2028 年初必须补 2028 表**(否则交易日判定显式报错, 不再静默); requirements-lock.txt/dependabot/coverage 基线为仓库侧新文件, 对运行中容器无影响(容器仍用 v0.5.14 底座已装依赖)。
- 验证: 全量离线套件 **1825 passed / 2 failed / 5 skipped**(2 failed 均为已知本地环境损坏文件 ta_load_ohlcv_patch/thsdk_buffer_size, 从未进 CI, 与 wave2 分支基线一致); 新增 test_w26_trading_calendar_wiring 29 例; 前端 tsc/lint/vitest/UI-rules 四道全绿。
- [tag v0.5.20]

### fix-统一交易日历接线+缺失年份显式报错(W2.6/B6)
- 背景: B6 —— 仓内已有 src/core/trading_calendar.py(静态表 2025-2027, S6 产物), 但仅 prediction_outcome 一家在用; 其余交易日/竞价/时段判定仍是手写 weekday, 法定节假日(恰为工作日)会被当交易日: 国庆白天 kline TTL 按交易档刷新、竞价异动"当日池"节假日返回空池、dark_flow 盘中把上一交易日 tick 当未来时刻误丢、节假日空拉误告警、市场状态接口节假日白天误标"盘前/已收盘"。
- **红线落地(缺失年份显式报错)**: is_trading_day 此前对未覆盖年份回落 weekday 推测(`_HOLIDAYS.get(year, set())`) —— 违反方案红线"缺失年份必须显式报错而不是回落 weekday 判断, 禁止推测"。新增 `TradingCalendarError`, 年份不在 2025-2027 静态表内直接抛(报错文案指明补表位置); add_trading_days/next/prev/trading_day_anchor 经同一判定继承 fail-loud。存量调用方 prediction_outcome 评估窗口最长 10 交易日, 恒在覆盖范围内, 不受影响。
- **新 API(盘中判定收口)**: `is_auction_time(now)`(9:15-9:25 含端点 + 交易日) / `is_trading_session(now)`(9:30-11:30, 13:00-15:00) / `trading_day_anchor(d)`(交易日取自身, 否则取最近上一交易日 —— "当日数据池"语义); 均上海时区, naive 入参视为上海墙上时间(单测可注入)。
- **六处接线**: ① models/market.py `MarketDef.is_trading_time` CN 分支走 is_trading_day(港美无日历维持周末判定, 已知限制) —— intraday_monitor/paper_trading/price_alert/seal_sampler/scheduler/agents 等全部 is_trading_time 调用方统一获得节假日感知; ② kline_collector `_is_auction_time` 删本地实现改委托日历; `_kline_cache_ttl` 竞价档提到 is_trading_time 之外(旧写法竞价档嵌在 9:30 起的会话判定里, 9:15-9:25 永远走不到是死分支; 现在 CN 竞价期真正用 15s TTL; 仅 CN 生效); _kline_cache_ttl/_fail_cooldown 的异常从静默 pass 改 logger.warning 留痕(未覆盖年份不得无声当收盘档); ③ abnormal_moves.py 当日竞价池 `created_at >= trading_day_anchor(today) 00:00` —— 周末/节假日回看上一交易日池子而非空池; ④ dark_flow `_drop_future_ticks` 交易日走日历(节假日白天不再把上一交易日 tick 当未来误丢) + `_in_trading_hours` 走日历(节假日空拉不再误告警); ⑤ kline_backfill_scheduler `_is_market_day` 走日历(原注释预留的"交易日历 hook"落地, 顺带修正 UTC 时区口径为上海时区, 支持 now 注入); ⑥ darkflow.py `_tick_staleness` 走日历 + stocks.py `/markets/status` CN 增"休市（节假日）"状态。
- **全仓 weekday 审计(grep 命中 14 处逐个处置)**: 接线 9 处(上列); 豁免 3 处 —— forecast_server.py×2(forecast_lib 独立部署不含 src/, 预测目标日暂按 weekday 计, 登记 docs/KNOWN_ISSUES.md 附 owner/期限)、shadow_account/extractor.py(entry_weekday 是特征值非交易日判断); 语义无关 2 处(prediction_outcome 文档串、trading_calendar 自身实现)。
- **回归测试 tests/test_w26_trading_calendar_wiring.py 29 例**: 未覆盖年份 is_trading_day / add_trading_days 跨进 2028 / trading_day_anchor 回看 2024 三路抛错 + 2027 预估表可用; 竞价/会话窗口含端点与节假日周休排除; anchor 交易日自身/周六→周五/国庆→9/30; MarketDef CN 节假日/补班周六盘中/午休/未覆盖抛错 + HK/US 维持原判定; kline TTL 竞价档可达/CN 限定/节假日收盘档(验收用例)/交易档; dark_flow 节假日全放行+交易日照丢未来+非交易日不告警; darkflow 节假日不误报 stale + 交易日照常判定; backfill 节假日/周六/交易日。
- 验证: 新测试 29/29 + 邻域回归(日历 S6 既有/dark_flow/kline 缓存与合并/abnormal_moves×2/darkflow_ops/paper_trading/price_alert/scheduler_guard/seal/market_phase) 87+174 passed; 全量离线套件(-m "not network")与改动前基线一致。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### fix-覆盖率棘轮+依赖精确锁定+dependabot+双审计(W2.5/E5+E6)
- 背景: E5 —— CI 只判"测试过不过", 不看覆盖率, 删测试/裸写代码静默无感; E6 —— requirements.txt 全 loose 约束(>=), 生产镜像每次构建随 PyPI 漂移, 依赖 never 锁版本 never 审计。
- **requirements-lock.txt(171 包精确锁定)**: 版本取自生产容器 panwatch 实测枚举(`docker exec panwatch python -c "importlib.metadata..."` —— 镜像瘦身删了 pip 本体, 不能用 pip freeze), 即"生产真实在跑的版本"而非本地猜。对容器 freeze 的 3 处手工修正(文件头有完整说明): ① tradingagents==0.3.0 还原为 git+https 直链(不在 PyPI); ② 删 marketdata 本地包(与 Dockerfile 同口径 `--no-deps -e` 单独装); ③ 补 tzdata==2026.3(requirements.txt 2026-09-09 新增, v0.5.14 基座构建时尚无, 容器靠系统 tzdata 兜底)。文件名不叫 requirements.lock: dependabot pip 生态只匹配 requirements-*.txt, 叫 .lock 永远收不到升级 PR。
- **CI/Dockerfile 统一走 lock**: 3 个工作流(建-and-push-image/release 的 Install 步, build-push-acr gates 的 Backend pytest 步)改为 `pip install -r requirements-lock.txt` + `--no-deps -e ./packages/marketdata`; Dockerfile 依赖层 COPY requirements-lock.txt 直接装(lock 无 -e 行, 原 grep 过滤删除)。**漂移守卫 scripts/check_lock_covers_reqs.py**: requirements.txt 新增顶层依赖而未再生 lock → CI 红(首轮自测就抓到真实缺口: tzdata 在 requirements.txt 有、容器 freeze 没有)。
- **覆盖率棘轮 scripts/check_coverage_ratchet.py + scripts/coverage-baseline.json**: pytest `--cov=src --cov-report=json` → 断言"当前 < 基线即红"(只许升不许降; 高于基线 3% 提示上调; 红测验证: 基线 50.0/实测 0.1 → exit 1, 缺基线文件 → exit 1)。**基线 50.0**: python:3.11-slim 容器 + lock 精确依赖 + CI gates 同口径(`pytest tests/ -q --timeout=60 -m "not network" --cov=src`)实测 **50.8%**(41683 语句/20507 未覆盖), 下浮 0.8% 作 CI 平台差安全余量; 本机 Python 3.14 装不上依赖, 按方案要求在 3.11 测。每波结束按 CI 实测值手动上调。棘轮只接在 build-push-acr gates(PR 主门禁)—— 三个工作流测试子集不同(2 个带 --ignore/+marketdata 测试), 混用会把基线口径搅浑, 其余两个只装 lock 不断言覆盖率。
- **dependabot(.github/dependabot.yml)**: pip(/) + npm(/frontend) + github-actions(/) 三生态 weekly; 各生态归一组(每组一个 PR 控噪音); 安全更新走 Dependabot security updates 自动单独开 PR(不与周更组混)。
- **双审计入档 docs/KNOWN_ISSUES.md**: ① pnpm audit(须 `--registry=https://registry.npmjs.org/`, npmmirror 无 audit 端点): 28 条(critical 0/high 11/moderate 15/low 2, 15 个模块) —— 唯一运行时可触达的是 react-router-dom 6.30.3 开放重定向→XSS(fix 6.30.6 同大版本 patch, owner TianXiang 期限 2026-09-30); 其余全是 vite/rollup/tailwind 链等构建期工具链(不进产物), vite 5→6/vitest 3→4 跨大版本需手动评估(期限 2026-10-31)。② pip-audit -r requirements-lock.txt --no-deps(容器内执行): **No known vulnerabilities found** —— 后端 171 锁定包无已知 CVE。
- 验证: 基线容器实测 50.8%(1798 passed/1 failed/1 error —— 均为已知非 CI 环境损坏项: ta_load_ohlcv_patch 本地损坏文档在案 + ws_hub redis teardown 容器特有, 本地单跑 passed, CI 全量环境历史上绿); check_lock_covers_reqs 33 个顶层依赖全覆盖 OK; 棘轮红/绿/缺文件三路 exit code 验证; 3 工作流 + dependabot YAML safe_load 通过; 基线测量容器顺带发现 **lock 装完不装 marketdata 会 11 个文件收集失败**(src/web/api/__init__ → marketdata_client → import marketdata), 已在 3 个工作流补 `--no-deps -e ./packages/marketdata` 与 Dockerfile 对齐 —— 该坑若不本地实测, 合并后才会第一次红。
- 已知边界: dependabot PR 要配置落 main 后才自动开(本周更周期内验收"dependabot PR 已自动开出"); 基线 50.0 待首个 CI gates run 校准为精确实测值。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### fix-前端四道门禁落地(W2.4/E3: vitest 17例+eslint铁律+UI规则R6棘轮R7禁null→0+Dockerfile恢复tsc)
- 背景: E3 —— 前端零测试零 lint: Dockerfile `npx vite build` 跳过 tsc(注释自认"兼容 fork 源码既有 TS 警告"), check_ui_rules.mjs 只在本地手跑未进 CI, format.ts 全家桶(safeNum/safeMoney 等, 2026-08-23 S-5/M-3~M-8 收敛产物)无一行单测; `c.price.toFixed is not a function` 事故模式的防复发只靠 code review。
- **vitest 单测 17 例(frontend/tests/lib/, node 环境纯逻辑零 mock)**: format.test.ts 12 例 —— safeNum 护栏(null/''/NaN/非数值串)、safeFixed 位数与 fallback、safePercent 符号与 '--'、safeMoney 元→亿万分档+尾零去除、safePrice 去尾零、safeInt 千分位、safeNetInflow 带符号亿、toAmount 元口径、toAmountFromWan 万口径(单位/口径标签全覆盖); kline-scorer.test.ts 5 例 —— 多头 buy/空头 avoid 评分与动作映射、持仓语义 hold/reduce/watch、空数据技术面中性、形态强弱分档(金针探底+2/三只乌鸦-2)。`pnpm test` = vitest run。
- **eslint 9 flat config(frontend/eslint.config.js)**: 刻意最小规则集 —— `@typescript-eslint/no-unused-vars` + `react-hooks/rules-of-hooks` + `react-hooks/exhaustive-deps` 全 error(存量 any/类型告警由 tsc -b 把关, 不重复设岗)。首跑 20 error 全部修复: 3 处未使用 catch 变量改无绑定 catch; DiscoveryPanel 3 处 `rawPct == null ? 0 :` 缺数据渲染 "+0.00%" 改 safeNum/safePercent; SentimentGauge 补 score/pointerColor 依赖(score 变化此前不重绘, 顺手修 bug); KlineChart 回调 prop 走 latest-ref(父组件内联箭头函数身份不再触发整图重建)+ subchart deps 换 state(无调用方传该 prop); InteractiveKline 类型位 `typeof props.events` 致插件误报整对象 props, 改直接引用 KlineEvent; logs-modal loadLatest 走 latest-ref(query 防抖语义保留); 各页 load/loadConfigAsync/loadFeedbackStats 稳定化为 useCallback, Stocks loadPortfolio 经 quotesRef 解除对 WS 5s 推送 quotes 的闭包依赖, Stocks 挂载 effect 走 latest-ref(refreshQuotes/refreshKlines 依赖 buildQuoteItems(←stocks) 身份随数据变, 直接进 deps 会无限重拉); Dashboard normalizeMarket/openStock useCallback 链。
- **check_ui_rules.mjs 扩 R6/R7 并进 CI**: R6 toFixed 棘轮 —— 裸 `.toFixed(` 冻结在 scripts/ui-rules-baseline.json(51 文件 340 处, 只许降不许升), 新文件出现即失败; R7 禁 `x == null ? 0 :` 三元(缺数据渲染成 0 掩盖缺失, 应走 '--' fallback) —— 该规则自己抓出 2 处此前 grep(只扫 frontend/src)漏掉的 biz-ui 站点(macd.map 暖机种子属数学输入非渲染, 进 R7_SKIP_LINE 豁免并附理由)。红测验证: 临时写入 `x.v === null ? 0 : x.v` → exit 1, 删除后恢复 OK。
- **Dockerfile 恢复 tsc**: 构建行 `npx vite build` → `npx tsc -b && npx vite build`(本地 tsc -b 实测全绿, "fork 既有 TS 警告"已不存在); `grep -iE "skip|--noEmit" Dockerfile` 无命中。
- **CI 接线**: build-and-push-image.yml / release.yml 的 "Frontend typecheck" 单步升级为 "Frontend gates"(tsc -b + pnpm lint + pnpm test + node ../scripts/check_ui_rules.mjs); build-push-acr.yml gates job 同步追加 lint/test/ui-rules(build-push-acr-forecast 为纯后端镜像无前端段, 不涉及)。
- 验证: `pnpm typecheck`(tsc -b) / `pnpm lint`(0 error) / `pnpm test`(17/17) / `node scripts/check_ui_rules.mjs`(UI-RULES OK) 四道全绿; 红测 exit 1 复现。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### fix-多租户数据访问统一收口(W2.1/C3: scoped()/owned_or_404()/writable()三助手+20处越权面修复+静态门禁)
- 背景: C3 —— M2 多账号改造后 user_id 列已铺开, 但读路径仍散落 `db.query(Stock)`/`db.query(NotifyChannel)` 等全库查询, 任意登录用户可枚举他人自选/提醒命中/通知渠道; plans 文档列名 9 处, 实际逐文件排查出 29 处裸查(含 3 个完全无鉴权端点)。
- **统一助手 src/web/api/_scope.py**: `scoped(q, user)` 读过滤(user 自己 + 全局 NULL 共享, 单条出口); `owned_or_404(obj, user)` 读单条(他人资源 404 防账号探测, 与 S1-S4 口径一致); `writable(obj, user)` 写单条(自己的可改, 全局共享仅 owner); `allow_cross_user` 纯标记装饰器(静态门禁的显式豁免口, 须附理由注释)。缓存键助手 `user_scoped_key(prefix, user, **parts)` 进 biz_cache —— abnormal-moves 此前 `am:all:{threshold}` 全用户共享, A 的自选扫描结果会被 B 命中。
- **越权面逐文件修(20 处)**: price_alerts(建提醒按归属校验 stock 防借他人 stock_id 建单; `/hits/today` 此前全表命中+全表规则跨用户可见, 经 rule_id→PriceAlertRule.user_id join 过滤; `/scan` 此前完全无鉴权) / abnormal_moves(自选池 scoped + 缓存键 user_scoped_key) / notifications(`/configured_channels` scoped; send_test 无鉴权→鉴权+通知落本人) / quotes(get_quotes_root 无鉴权→鉴权+scoped 自选列表) / chat(站内通知读取按 user 过滤; losing_q 兜底过滤) / dashboard(持仓/自选计数 or_(user_id==me, NULL)) / feedback(他人建议 owned_or_404 防探测) / news / paper_trading(权益曲线/持仓/交易/渠道全部走 _user_scope_pos/_user_scope_trade) / stocks(trigger_agent 两处 Stock 查询 scoped; 他人自选落无绑定一次性分析路径) / strategies(`/scan` watchlist 池此前拉全库所有用户自选, 按归属过滤)。
- **显式豁免(有意跨用户, 附理由)**: templates export/import 收敛为 owner-only(require_owner —— 全库配置备份/恢复语义, 含各用户自选与全部 agent config, 本就不该对 member 开放) / agents health(全局运维视图) / tradingagents running+幂等去重(trace 按 symbol 全局唯一, 防重复触发烧 LLM 费用) / insights 消息面与公告解读(仅取股票名称做展示上下文)。
- **静态门禁 scripts/check_scoped_queries.py**: AST 扫 `db.query(<带 user_id 的模型>)`, 模型清单运行时从 Base.registry.mappers 解析(防手写清单漂移); 同函数(含嵌套祖先)内须命中其一: `scoped()/owned_or_404()/writable()`/`*user_scope*` 助手调用、任意 `.user_id` 归属处理(覆盖先取后验模式)、`@allow_cross_user` 豁免、行尾 `# scoped-check: allow`。已接线 4 个 CI 工作流(build-and-push-image 独立 step + release + build-push-acr×2 pytest 步内), 裸查即红。
- **回归测试 tests/test_multitenant_isolation.py 16 例**: 借他人 stock_id 建提醒 404 / hits-today 双向隔离 / 他人规则 hits 404 / 异动扫描池按归属+缓存键隔离(monkeypatch vendor+analyze 全离线) / user_scoped_key 同用户同键异用户异键 / stocks PUT member 改全局 403+owner 200+改他人私有 403 / feedback 他人建议 404 / strategies scan watchlist 排除他人自选 / templates member 403 owner 200 / 静态门禁红绿样例(裸查报/scoped 过/owned_or_404 过/普通装饰器不豁免/allow_cross_user 过)。
- **存量测试适配(2 文件)**: test_home_phase_a 的 list_today_hits 直调改传 user=_FAKE_USER + 规则种 user_id(与 S3 严格归属口径一致) / test_multitenant_isolation 自身补 _cleanup_module_owner —— 引导 owner 不清理会让 test_permissions_rbac 删 admin 后的 env 重新引导被"已存在 owner"短路 → 中间件 4 测试全量跑时 401(教训同 test_user_isolation_api, 已记 tdai)。
- 验证: checker 0 violation; 新测试 16/16 + 邻域 test_user_isolation_api/migrations 25 passed 1 skipped; 全量离线套件 **1796 passed / 2 failed / 5 skipped / 123 deselected**(2 失败均为已知本地环境损坏文件 ta_load_ohlcv_patch/thsdk_buffer_size, 从未进 CI, CI 历史全绿; 修复前同口径 8 failed: home_phase_a×2+permissions_rbac×4 均由本次改动引入并已修); 4 个工作流 YAML safe_load 通过。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### fix-测试与真实数据目录彻底隔离(W2.2/E4: conftest顶层DATA_DIR重定向+Base重载防御+factories工厂)
- 背景: E4 —— 单测会读写仓库真实 data/ 目录: error_tracker/disk_cache/media_utils/chat_tools 在模块 import 期把 DATA_DIR 烤进常量, conftest 在 fixture 期改 env 已晚; 更严重的是 test_pg_default 的 delenv+reload 会把 database 模块全局指回真实 data/panwatch.db, 污染后续所有用 SessionLocal 的测试(实测曾把迁移跑进真实库, 留下 7 个 .bak); 仓库 data/ 下 chat_tools_cache.json 也被测试写。真实库/真实数据目录从此是单测禁区。
- **conftest 顶层隔离**(tests/conftest.py): 在任何 src.* import 之前把 DATA_DIR/PANWATCH_DATA_DIR/PANWATCH_CACHE_DIR 重定向到 tempfile.mkdtemp("sida_test_data_"), PANWATCH_DB(zhitu vendor 本地库回退)/SIDA_DB_URL setdefault 指向临时库 —— import 期烘焙路径的模块全部拿到临时目录。原 0.4② SIDA_ALLOW_SQLITE setdefault/_init_test_db/_clear_module_caches 保留。
- **src/web/database.py 两处加固**: ① DB_PATH 跟随 DATA_DIR(与 error_tracker 同口径; 容器 DATA_DIR=/app/data 生产路径不变); ② Base 重载防御 `if "Base" not in globals()` —— importlib.reload 重执行 `class Base(DeclarativeBase)` 造出新 Base, models.py 持旧 Base, 运行时 `from src.web.database import Base` + create_all 建出空库 → permissions_rbac×16/pdf_export×2/ws_hub teardown "no such table: ai_services" 连锁(此前被测试收集顺序掩盖)。reload 保留原模块 __dict__, 守卫使 Base 不再重建。
- **tests/test_pg_default.py autouse 兜底**: _restore_database_module 在用例结束后按恢复后 env 再 reload 一次, 本文件不再向后续测试泄漏 DB_URL/engine。
- **src/core/chat_tools.py**: _cache_path() 跟随 DATA_DIR —— 会话级快照守卫实测抓到 chat_tools_cache.json 被写进仓库 data/ 后修复。
- **tests/factories.py 新建**: make_user/make_stock/make_notify_channel/make_notification 纯构造器(不碰 Session), 5 个手搓 ORM/直连真实库的测试文件迁移(channels_isolation/chat_tools_p1p2/ratelimit_per_user/selfcheck/internal_scene_model_auth)。
- **元测试 tests/test_conftest_isolation.py 4 例**: DATA_DIR 落临时目录/默认 DB_URL 隔离/error_tracker._FILE 隔离/子进程级 delenv+reload 后仍隔离(规避进程内 reload 污染)。
- **会话级执法守卫**: _verify_real_data_untouched 对真实数据目录(调用方 DATA_DIR 或仓库 data/)做 rglob 文件快照(mtime_ns+size), 会话结束发现新增/删除/变更即 AssertionError —— "测试不得触碰真实数据"从此有自动化执法。
- 验证: 全量套件 1919 passed / 4 failed(全部为已知环境损坏/本地 flaky: dark_l2_engine/ta_load_ohlcv_patch/thsdk_buffer_size/thsdk_extended, 与改动前基线一致); 真实 data/panwatch.db mtime 跨轮稳定; 元测试 4/4 过。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### fix-联网测试打标network+CI门禁一刀切(W2.3/E2: -m "not network"替代19项ignore清单+夜间网络工作流)
- 背景: E2 —— 联网测试靠 CI 手工维护 19 项 --ignore 清单(build-and-push-image/release), 其中 2 项指向已删除文件(main_flow_hengsheng/datasources_health); build-push-acr×2 完全没排除(联网用例靠 --timeout 兜底); 新增联网测试须记得改 4 处工作流, 漏一处即红。
- **打标**: pyproject.toml 注册 `markers = ["network: ..."]`; 10 个联网测试文件(auction_pool/chat_thsdk_tools/context_enrichments/dark_flow/dark_l2_engine/main_flow_compare/thsdk_api/thsdk_board/thsdk_ext/thsdk_extended)EOF 追加 `pytestmark = pytest.mark.network`(置于文件末尾: 模块级任意位置生效, 避开 docstring/__future__/import 顺序坑)。分区: 1787 offline / 143 network / 1930 共。
- **CI 主门禁**: build-and-push-image.yml + release.yml 的 19 项 --ignore 清单退役, 改 `-m "not network"`; 仅保留 2 个 CI 环境级 broken 文件的 --ignore(test_dark_l2_engine/test_thsdk_extended, 打标前就 red, 打标后双保险防收集期 import); 7 个曾被误伤的离线文件(datasource_admin_api/datasource_reconcile/selfcheck/shadow_account/ta_us_news_passthrough/source_health/events_routing)重新进主门禁(本地验证 19 passed)。build-push-acr.yml + build-push-acr-forecast.yml 补 `-m "not network"`(保留 --timeout=60 对未打标联网用例兜底)。
- **nightly-network-tests.yml 新增**: 每日 UTC 18:30(北京 02:30)+手动 dispatch 跑 `pytest tests/ -m network --timeout=120`; 装钉版 `thsdk==1.7.18`(同花顺 SDK 不在 requirements, 仅部署机安装, PyPI 有 1.7.18 与部署机/本地一致); continue-on-error —— 海外机房访问国内行情源可达性有波动, 红钟只告警, 连续多日红再排查。联网用例从此有独立归宿不烂尾。
- 验证: `--collect-only` 双向 1787/143 一致; 本地全量 `pytest tests/ -m "not network"` = **1780 passed / 2 failed / 5 skipped / 143 deselected**, 2 个失败均为已知本地环境损坏文件(ta_load_ohlcv_patch/thsdk_buffer_size, 从未进过 CI ignore 清单, CI 历史全绿); 6 个工作流 YAML 全部 safe_load 验证通过。
- [branch fix/wave2-门禁-20260909, `git show HEAD`]

### update-v0.5.19生产部署+K线全量重刷完成(复权污染清除, 勘查报告附D对账)
- **生产部署**: tag v0.5.19(21fc03f) 经 docker cp 覆盖层部署到 panwatch 容器(0.7 勘查既定路径: 本机无 ACR 凭据, docker config auths 为空, 镜像构建发布不可用)。步骤: 备份现行代码 tar.gz(WSL `/tmp/app_backup_pre_v0519_20260909_024720.tar.gz`, 仅代码不含 data) → `git archive` tag → 容器内解包 → restart → 42 条迁移全 success(含 _m137~_m142) → 容器内签发 admin token 跑 scripts/smoke_test.py **9/9 通过**。
- **K线全量重刷**(0.7 §4 加列半场后的"重刷半场", 附C 验收全过): 预备份 pg_dump 踩坑(4,447B 归档含 TABLE DATA 条目但 restore-and-count=0 行, 表内实有 209,094 行, 根因未查明, stocks 对照组正常 —— **规则固化: 破坏性操作前备份必须 restore-and-count 验证**) → 依据 klines 属可再生派生数据 + 存量即待清污染, 执行 TRUNCATE → ingestor 重灌 71 股次(fail_details=0)。
- **对账表**: 209,094 行(tencent/sina/eastmoney 三源假标签各 69,698, _m137 回填 adjust='none') → **43,949 行全 qfq/tencent 单源单分区**, 55 股, 2023-05-11→2026-09-08。明细见 `docs/research/K线复权污染勘查_20260907.md` 附D。
- **验收(附C SQL 生产实测)**: 双柱键 537→**0**; 多源同键→**0**(单源不变量); volume ×20 跳变 46 只→**16**(逐条核对均真实停牌/复牌, 002251 系列); 主板不可能缺口 19→**10** —— tencent RAW 交叉验证 600502 的 +11.67% 缺口在原始数据中逐字存在, 判定 vendor 源头级非重刷引入; 本机到 eastmoney 不通(亦为 tencent 全胜 vendor 竞速的原因)、sina 无个股 K 线拉取, 第二源裁决暂不可用, 残留 10 条登记 wave-2 数据质量哨兵跟进。
- [tag v0.5.19]

### fix-CI pytest门禁4红修复+全新PG库首启崩溃修复(v0.5.19发版门禁收敛)
- 背景: v0.5.18 起 4 个发版工作流(release/build-and-push-image/build-push-acr/build-push-acr-forecast)的 pytest 门禁首次端到端跑即红(此前从未全绿), ACR 镜像又因 gitleaks generic-api-key 误报未产出(另修, 见 `fix: 渠道脱敏测试fixture变量名SECRET→FAKE_PK`), 生产一直停在 v0.5.14 底座+热修覆盖。本机用 python:3.11 容器跑与 CI 完全一致的命令(18 个 --ignore 相同)复现 4 failed/1737 passed, 逐个定位修复。
- **门禁 4 红逐个修**:
  1. `tests/test_pg_default.py::test_docker_without_db_url_fails_fast` —— conftest 给全部测试 setdefault `SIDA_ALLOW_SQLITE=1`(0.4② 逃生口), 把 `DOCKER=1` 无连接串的 fail-fast 门整个豁免, RuntimeError 永不触发。`_reload_database` 改为: 摘 DOCKER/SIDA_DB_URL/SIDA_ALLOW_SQLITE 三键 → reload → finally 原样恢复(保住 conftest 基线), 三个用例去掉冗余 try/finally。
  2. `tests/test_source_health.py::test_wencai_with_credentials` —— thsdk 不进 requirements(CI 无真包), `check_wencai` 诚实地判 down。测试注入假 thsdk 模块(检查只看"可 import + 凭据已注入", 不发真实查询), 断言升格 `== connected`。
  3. `tests/test_collectors_robust_20260823.py::test_today_cn_format_is_iso` —— `_today_cn()`(Asia/Shanghai) 与 naive `datetime.now()`(UTC 宿主) 比日期, UTC 16:00-24:00 必差一天(容器 18:14 UTC 实复现: 09-09 vs 09-08), 测试随运行时间随机红。改比 `ZoneInfo("Asia/Shanghai")` 的今天, 口径与被测函数一致。
  4. `tests/test_error_tracker.py::test_file_write_failure_is_silent` —— 原 fixture 用 `/nonexistent_dir_xyz` 制造失败(权限性), root 容器 mkdir 反而成功(result=True); CI 非 root 其实会过, 但该写法不鲁棒, 且曾在 Windows `C:\` 根留过同名垃圾目录(已清理)。改 tmp_path 内"父路径=文件"制造 ENOTDIR, 任何用户/平台必失败。
- **requirements.txt 补 `tzdata>=2024.1`**: Windows/精简容器 zoneinfo 无系统库时 `_today_cn` 直接缺日期 —— 资金流日期口径属数据正确性依赖, 显式声明不再搭系统环境便车。
- **全新 PG 库首启崩溃(生产级真 bug, throwaway timescaledb:latest-pg16 复现)**: A 层迁移向 Boolean 列插整型字面量 —— `_migrate_positions_to_accounts` 两处 accounts INSERT(`'默认账户', 0, 1` / `:funds, 1`)、`_migrate_ai_and_notify` 的 ai_models `is_default`/notify_channels `enabled,is_default` 字面量, 共 4 处 → `psycopg2.errors.DatatypeMismatch: column "enabled" is of type boolean but expression is of type integer`, 全新 PG 首启直接崩(SQLite 宽容整数掩盖至今)。全部改绑定参数传 `True`, 双方言通用(与 `_migrate_remove_stock_enabled` 的方言字面量教训同源)。存量生产库因已有 accounts/ai_models/notify_channels 不走这些 INSERT, 不受影响; 但新装/重建必须能起来。
- 验证: 本地(Windows) 5 个相关测试文件 76 passed; python:3.11 root 容器跑 CI 全量命令 **1741 passed / 4 skipped / 0 failed**(修复前同环境 4 failed); 全新 PG16 `init_db` 端到端 OK —— schema_migrations 42 行, _m137~_m142 全 success=1, klines 唯一索引 `uq_klines_symbol_period_ts_adjust` 就位, `adjust VARCHAR DEFAULT 'none'` 就位。
- 注: test_source_health.py/test_collectors_robust_20260823.py 随本次提交一次性 CRLF→LF 归一化(.gitattributes eol=lf 本就要求), diff 行数放大属预期。
- [tag v0.5.19]

## 2026-09-08

### update-发版 v0.5.19(数据正确性第1波合入main)
- 本次发版内容: K线复权维度入列(_m137, qfq/none分区+PG优先读取+DO UPDATE自愈, 存量复权污染待发版后重刷) / vendor缺失字段None化+status标记(B2) / 调度器选主fail-closed+租约丢失真停+三态健康探针+SidaSchedulerLeaderDown告警(A3) / 影子报告路径穿越修复+通知渠道跨用户越权修复+config脱敏(C1+C2) / 迁移PG advisory锁串行化+运行时DDL 6处收编B层(_m138~_m142)+PG迁移前schema快照(A5)。
- 部署注意: 启动时将自动跑版本化迁移 _m137~_m142(全部幂等, 已有表/列自动跳过); _m137 会重建 klines 唯一索引(存量行回填 adjust='none'), 建议发版后择机执行 K线全量重刷(backup → TRUNCATE → 95股×800日重灌), 未重刷前 qfq 读取仍吃存量 none 数据。
- 验证: 全量套件 1912 passed / 7 failed(全部为已知环境损坏文件×6 + 已知 flaky×1, 与 wave1 分支基线一致); 迁移幂等性有专项测试覆盖。
- [tag v0.5.19]

### fix-迁移advisory锁串行化+运行时DDL全面收编B层+PG迁移前schema快照(风险方案1.5/A5)
- 背景: A5 —— schema 变更散落三层: A 层(database.py `_migrate*` 历史遗留)、B 层(migrations.py 版本化迁移+checksum)、C 层(业务模块运行时 `CREATE TABLE`/`__table__.create` 兜底)。C 层 DDL 与 B 层/ORM 漂移无人对账(同表两处定义); `run_versioned_migrations` 无锁, 生产 2 容器同时重启会并发跑同一 DDL(PG 撞死锁/duplicate); PG 迁移前无任何备份(SQLite 有整库 .bak, PG 什么都没有)。
- **advisory lock 串行化**(src/web/migrations.py): `run_versioned_migrations` 在 PG 下先取会话级 `pg_advisory_lock(729138)` 再跑迁移, 拿锁实例执行, 等待实例轮到时迁移已全部 success=1 秒过; 锁挂**独立 AUTOCOMMIT 连接**(会话锁随事务回滚即释放, 不能放 per-migration 事务), 持锁横跨全部迁移事务, finally 解锁+关连接 —— inner 抛异常/取锁失败都保证关连接不泄漏, 解锁失败不掩盖迁移原始异常(连接断开会话锁由 PG 自动释放); SQLite 路径不加锁(dialect 探测, 无循环依赖); 锁 key 固定常量(换值=新旧实例锁不互斥)。
- **C 层 DDL 收编进 B 层**(6 处全清): 手写双方言 DDL 4 处 —— summary_cache.py `_ensure_summary_cache_table`(B 层 _m129 早已覆盖, 纯冗余拆除)、datasources.py `_ensure_health_columns` + 6 个调用点(_m126 已覆盖, 纯冗余拆除)、market_data.py `_ensure_snapshot_table`(新增 `_m138_market_flow_snapshots_table`, 30s 节流逻辑保留)、market_mainline.py `_ensure_mainline_rank_table`(新增 `_m139_mainline_rank_daily_table`); ORM `__table__.create` 兜底 2 处 —— market_scan.py 两张表(新增 `_m140_market_scan_ranks_table`/`_m142_dark_fund_top_snapshots_table`)、signal_summary.py(新增 `_m141_signal_summary_daily_table`), 后三个迁移用 `Model.__table__.create(bind=conn, checkfirst=True)` 让 ORM 自带方言类型, 免手写双份 DDL(延迟 import models 规避 database↔migrations 循环依赖)。生产老库已有表 → IF NOT EXISTS/checkfirst 幂等跳过。
- **PG 迁移前 schema 快照**(src/web/database.py `_backup_pg_schema_before_migration`): has_pending_migrations 时与 SQLite .bak 并列执行, `pg_dump --schema-only --no-owner --no-privileges` 落 `data/migrations_backup/schema_<ts>.sql`; 密码只经 PGPASSWORD 环境变量传递(不进命令行/日志); pg_dump 不在应用容器 PATH → 警告跳过(fail-soft 不阻断迁移); 只 dump 不 restore(红线: 不在生产库跑 pg_restore)。init_db 补 A/B 层注释: B 层是唯一 schema 变更入口。
- 测试: `tests/test_migration_lock_and_failures.py` 8 用例 —— PG 锁包装(锁→inner→unlock→close 顺序; inner 抛异常仍解锁+关连接; 取锁失败关连接且不跑 inner; sqlite 路径零 connect)、迁移失败落 success=0+error 且 has_pending=True、修复换 runner 重跑 success=1、checksum 篡改重跑、_m138/_m139 sqlite 建表可写入、_m140-142 ORM 三表落库。
- 验证: 新批 8 passed; 邻域(market_mainline/summary_cache/summary_layer/datasource_admin/permissions_rbac/price_alert/source_health/source_trust/market_scan×3/dark_fund/dark_pool) 109 passed; 全量套件(本次未排除任何文件) 1912 passed / 7 failed —— 6 个失败全部属于 5 个已知环境损坏文件(dark_l2_engine/error_tracker/pg_default×2/ta_load_ohlcv_patch/thsdk_extended), 1 个为已知 flaky thsdk_buffer_size, 无本次改动引入的失败。
- 顺带修复: test_summary_cache.py 模块级 `SIDA_DB_URL=sqlite:///:memory:` 在子集运行时让 init_db 的 A 层 `_migrate` 撞 "no such table: users"(全量 suite 因收集序早于其它模块 import database 而被掩盖) —— 本任务测试批次统一显式临时文件 DB 规避, 未改业务代码(worktree 对照 HEAD 复现确认系存量问题, 与本次改动无关)。
- 注: market_scan.py/signal_summary.py 两文件随本次提交做一次性 CRLF→LF 归一化(.gitattributes eol=lf 本就要求), diff 行数放大属预期, 之后不再出现。
- [branch fix/wave1-数据正确性-20260908, `git show HEAD`]

### fix-影子报告路径穿越+通知渠道跨用户越权+config明文脱敏(风险方案1.4/C1+C2)
- 背景: C1 —— src/web/api/shadow.py 上传落盘名直接拼 `file.filename`(`../../` 可写出上传目录), `/report/{shadow_id}` 无格式/路径包含/归属校验且端点**未挂鉴权**, 任意(甚至未登录)请求可枚举读他人交割单分析报告(含交易画像/行为诊断); C2 —— src/web/api/channels.py GET 把 `config`(webhook URL/bot token 明文)整包返回给任何登录用户(全局渠道密钥泄露), PUT/DELETE 用"自己的+全局"谓词 → 非 owner 可改/删全局渠道, `POST /{id}/test` 完全无鉴权(任意用户拿别人的 webhook 发消息/探测)。
- **C1**(shadow.py): 落盘名改 `uuid4().hex + 校验过后缀`(与用户输入彻底解耦, 后缀白名单不变, 保留供解析器识别格式); 新增 `_resolve_report` 三重校验 —— shadow_id 格式白名单(`shadow_<8hex>`, 与 `extractor._new_shadow_id` 生成器一致) → resolve 后 `is_relative_to` 报告目录(路径包含) → 落库 `users.shadow_profile_json` 的 shadow_id 必须与请求者匹配(归属), 报告不存在同样 404(不泄露存在性); `get_report`/`get_report_pdf` 挂 `get_current_user`。
- **C2**(channels.py): 列表/创建/更新响应的 config 按键名脱敏(键名含 token|secret|key|password|webhook 不分大小写), 只留末 4 位供辨认, **DB 内仍存明文可用**(PUT 不传 config 不覆盖); 更新/删除改 `_get_channel_owned` 读宽写严(对齐 stocks.py: 自己的放行, 全局(NULL)仅 owner, 他人 403, 不存在 404); test 端点补 `get_current_user` + 同款归属校验; ChannelResponse 迁移 ConfigDict(顺手消 class-Config 弃用告警)。notifications.py `_configured_channels` 与 paper_trading.py 渠道列表核实已只回 id/name/type, 无泄露。
- 索引核实(方案第4条): notify_channels.user_id 生产 PG 已有 `ix_notify_channels_user_id`, 无需迁移。
- 遗留登记: chat_upload.py 把上传文件逐字解析到 100,000 字符喂 LLM(提示注入面), 按方案登记 §6.3 后续波次处理, 本任务不动; 前端 ShadowAccount 的 window.open 兜底链接(不带 Authorization 头)在报告端点加鉴权后会 401, 主路径 fetch+Bearer 不受影响(前端跟进归第4波)。
- 测试: `tests/test_shadow_path_safety.py` 9 用例(4 种恶意/正常文件名落盘 `is_relative_to` 断言 + 用户名成分不入盘名; owner 可读/他人 404/无画像 404/格式与 URL 编码穿越全 404/缺文件 404 非 500/符号链接逃逸被路径包含校验拦下), `tests/test_channels_isolation.py` 13 用例(demo 列表脱敏: 响应体无任何完整 secret + 他人渠道不可见 + chat_id 不误伤; 创建响应掩码而库内明文; demo PUT/DELETE 全局 403、owner 200; 他人渠道 403; test 端点 demo→全局 403 不触达 NotifierManager / owner 200 且内部发送用明文 / demo 自己渠道走通到发送)。
- 验证: 新批(shadow_path_safety + channels_isolation + pushplus_channel + shadow_account 存量) 36 passed 1 skipped(Windows 符号链接权限跳过); 邻域(test_user_isolation_api + test_selfcheck) 33 passed。
- [branch fix/wave1-数据正确性-20260908, `git show HEAD`]

### fix-调度器选主fail-closed+租约丢失真停+调度可观测性(风险方案1.3/A3)
- 背景: WEB_WORKERS=2 时旧选主在 Redis 不可用时"回退为本 worker 启动"——每个 worker 都自认 leader, 定时 Agent 双跑(LLM 费用翻倍/通知重复/撮合双触发); 租约被抢/续期失败仅打日志, 调度器一直跑到进程重启, 选主形同虚设; 调度执行无 context 规模观测、异常不上报 error_tracker; 多数 add_job 站点缺防并发参数。
- **fail-closed 选主**(src/core/scheduler_leader.py): Redis 不可用**绝不自认 leader** —— 指数退避(2s→30s 封顶)探测至 40s deadline, 仍不可用则放弃并置 `_state="failed"`; 锁在别人手里到期让位置 `_state="standby"`(合法状态); Redis 恢复由探测自动选主。显式口子: `SIDA_ENABLE_SCHEDULERS=1` 强制启动(兼容旧部署)、`SIDA_SCHEDULER_SINGLE_INSTANCE=1` 单实例部署跳过选主(开发)。`is_leader()` 删除, 改 `leader_state()` 三态(leader/standby/failed/init), 区分"合法没当上"与"没敢当"。裸连 Redis 不走 biz_cache 的红线例外已在模块 docstring 声明(分布式锁 NX/EX 语义, 非业务缓存)。
- **租约丢失真停**: 续期线程每 10s 探测; 租约被抢/过期/续期异常 → `scheduler_registry` 全部 `.pause()`(旧逻辑只打日志), 同时立即 NX 尝试抢回, 重新取得后 `.resume()`; 续期线程永不退出。循环体抽成 `_renewal_once(r, wid, paused) -> bool` 供测试(免 10s 线程等待)。
- **/health 三态探针 + 告警**(src/web/api/health.py, deploy/prometheus-rules.yml): scheduler 探针改用 `leader_state()` —— running≥2 记 `scheduler_leader` gauge=1; running=0 且 standby → ok 注明 non-leader(不记 0, 不误报); 其余(含 failed)→ gauge=0 + overall down, failed 注明"选主失败(fail-closed)"; 新增 `SidaSchedulerLeaderDown` 告警(`sida_health_component_status{component="scheduler_leader"} == 0` 持续 5m, critical, 与 database/redis 同 gauge 机制, tests/test_p4_alerts.py 双向锁不破)。
- **调度可观测性**(src/core/scheduler.py): 每轮 agent 执行记录 `context_chars`(watchlist+portfolio 序列化长度)入 agent_runs; 单只模式 error 拼接截断 2000; 外层异常接 `capture_exception`(source=scheduler); `start()` 装 `install_scheduler_error_tracking` 监听 APScheduler 执行错误(report/context 调度器已有, 补齐 paper_trading/price_alert/l2_ticks 三处)。
- **add_job 防并发参数统一**: 11 文件 17 站点全部补齐 `max_instances=1 + coalesce=True + misfire_grace_time=300` —— 此前 report/context/paper_trading/price_alert/l2_ticks 的 10 处缺 misfire, kline_backfill 每日 cron 缺 misfire(one-off date job 缺三件套), data_quality_sentinel 每小时哨兵全缺, auction_pool/kline_precache/thsdk_board 三个辅助 cron 缺 misfire。
- 测试: `tests/test_scheduler_leader_failclosed.py` 25 用例 —— try_acquire 六态(Redis 异常→False+failed / 锁在他人→standby / SINGLE_INSTANCE 口子 / 强制 0 / 选主成功起续期 / reload 重入续期接管), `_renewal_once` 五态(仍持有续期 / 被抢真停且不重复 pause / 过期抢回 resume / Redis 异常真停 / 恢复 resume), 11 文件 add_job 参数 grep 一致性, health 探针接线锁 + scheduler_leader gauge 记录, 告警规则锁; `tests/test_scheduler_leader.py` 旧用例 `test_redis_unavailable_falls_back` 断言的正是本次要消灭的 bug, 改为 fail-closed 断言。
- 测试隔离修复: `tests/test_ws_auth_guard.py` 两个广播用例偶发 queue.Empty(全量 suite 实测) —— `subscribe()` 会拉起真实聚合器线程, 其 5s tick 用 DB 重建 `_user_symbols_cache`, 清掉用例预置的 per-user 集合(T8 用例固有竞态, 与本次改动无关); patch `_ensure_aggregator`/`_collect_watchlist_symbols` 断开两条 mutation 路径, 修后文件 6 passed。
- 验证: 新批(scheduler_leader×2 + scheduler_guard + p4_alerts + health_metrics_guard) 38 passed; 邻域(test_thsdk_board/data_quality_sentinel/auction_pool/auction_gap) 58 passed 1 skipped; 全量套件(排除 5 个环境损坏文件) 1854 passed / 2 failed —— test_thsdk_buffer_size 已知 flaky, test_ws_auth_guard 即上述隔离竞态(修后复跑通过)。
- [branch fix/wave1-数据正确性-20260908, `git show HEAD`]

### fix-K线复权维度入列+PG优先读取+入库DO UPDATE自愈(风险方案1.2/B1)
- 背景: 0.7 勘查实证 klines 表 qfq 与不复权无列区分(全靠 source 隐含口径)、P2-19 单链复写 tencent/eastmoney/sina 三份假标签(69,154 个 (symbol,date) 三源逐格相等)、DO NOTHING 把前复权基准永久冻结在首次写入日。按 0.7 §4 结论落地「加列 + 全量重刷」的加列半场(重刷在发版后另行执行)。
- **迁移 `_m137_klines_adjust_dimension`**(src/web/migrations.py): klines 加 `adjust VARCHAR(4) NOT NULL DEFAULT 'none'`(存量行回填 none, 待重刷), 唯一索引 `uq_klines_symbol_period_ts` 让位于 `uq_klines_symbol_period_ts_adjust(symbol, market, period, ts, source, adjust)`, qfq 与 none 互不覆盖; 幂等可重跑。
- **读取按复权分区**(src/collectors/kline_collector.py): `get_klines(symbol, days, adjust="qfq")` 显式声明复权维度; 缓存键升级为 `market:symbol:1d:adjust`(不同复权维度不共用缓存); `_fetch_all_sources` 重排为 **PG 优先**(镜像 /api/klines._pg_klines 口径: 厚度 ≥ min(30,days) 且最新柱 12 天内 → 不发 HTTP) → qfq 走 engine(腾讯/东财均前复权) → HTTP 全挂宁服务同分区陈旧数据不混维度; **`_pg_fallback` 改名 `_pg_read` 并加 adjust 分区过滤**(修 0.7 勘查"连 source 都不过滤"的三倍序列指标问题); **qfq 请求绝不落新浪** —— `_sina_fallback` 仅服务 adjust='none', 且成功后诚实落 PG(source='sina', adjust='none', DO UPDATE) 供后续 none 读。
- **入库单链单标签**(src/collectors/klines_ingestor.py): `ingest_symbol` 重写 —— 废除 P2-19 三份假标签复写, 改用 `klines_with_vendor()`(packages/marketdata/src/marketdata/client.py 新增 facade, klines() 委托之) 返回真实胜出 vendor, 只写一份 source=真源(vendor 空则诚实标 'unknown'); adjust='qfq'; **DO NOTHING → DO UPDATE**(同键重跑覆盖, 除权后 qfq 基准变化自愈, 冻结机制根除); **单一 source 不变量**: 写入前 DELETE 该股 qfq 分区中非本轮胜出 vendor 的旧行, 防 vendor 切换日新旧两源并存成同日双柱; ingestor 直连 engine 不走 KlineCollector(那会"PG 旧数据抄回 PG"永远刷不新); ts 口径不变(交易日 00:00 Asia/Shanghai)。5m 盘中路径维持原状并在 docstring 标注弃用(实际写日K柱, 无读取方)。
- **读取方加 adjust='qfq' 过滤**: `src/web/api/klines.py` `_pg_klines` 与 `src/core/backtest/data_adapter.py` `load_price_history` —— 前端图表与回测序列必须吃前复权, 严禁混入 none 原始价; 旧库无 adjust 列时查询异常 → 静默回落联网(fail-soft)。
- 测试: `tests/test_kline_adjust_dimension.py` 18 用例(迁移加列/回填/索引互换/幂等/DO UPDATE 不产生第二行且不误伤 none 分区; 缓存键含 adjust 互不串用; PG 厚而新不发 HTTP / 陈旧或过薄回落 engine / qfq 空+engine 空宁空不落新浪 / none 走新浪不走 engine; `_pg_usable` 厚度与 12 天新鲜度边界; ingestor 单标签/unknown 诚实标签/重跑覆盖不双行/vendor 切换清理旧 source 分区/空结果 fail_details; `_persist_bars` 落 none 分区 + 库不可达 fail-soft), 另 test_kline_routing/coalesce/cache/flagon 四文件的假包层补 `klines_with_vendor` 方法、两处 `_pg_fallback` patch 改 `_pg_read`。
- 验证: 目标批 57 passed + 邻域回归(test_backtest/abnormal_moves/auction/chip/entry_outcomes/index_klines 等消费方) 201 passed 1 skipped; 全量套件(排除 5 个环境损坏文件) 1828 passed / 2 failed —— test_p2_realtime 当轮修于下条 entry, test_thsdk_buffer_size 已知 flaky。
- [branch fix/wave1-数据正确性-20260908, `git show HEAD`]

### fix-vendor缺失字段None化+status完整性标记(风险方案1.1/B2)
- 背景: 腾讯行情解析 `float(parts[3] or 0)` 等把缺失字段变 0 —— 价格 0 参与涨跌幅算术伪造 -100% 假暴跌, 直接违反「数据缺失必须显式标注『无数据』, 禁止推测」红线; 且 `turnover` 取自 parts[35] 第三段单位无标注(违反「金额=元」约定, AGENTS.md「对不上先怀疑单位换算」)。
- **turnover 单位实测**(qt.gtimg.cn 真实报文, 2026-09-08 收盘后): sh600519 parts[35]="1309.30/17534/2302823753", 恒等式 amt/(price×vol(手)×100)=**1.0031**; sh601318=**1.0069**(偏差<0.7% 即 VWAP≠收盘的正常范围) → **单位=元**; 交叉印证 parts[37]=amt/10000(230282 vs 2302823753, 比值 10000.02, 万元口径)。已写入 `vendors/tencent.py` 模块 docstring 与 `docs/_frozen/data.md` 新增「单位约定」节。
- `packages/marketdata/types.py`: `Quote.current_price` 改 `float | None`, 新增 `status`(ok/partial/missing) 与 `missing_fields: list[str]`。
- `vendors/tencent.py`: 核心 10 字段(价/量/内外盘/涨跌/高低)全部 `_to_float` 保留 None; 缺失进 `missing_fields`; 全部价格字段缺 → status="missing", 部分缺 → "partial"; turnover 缺失保留 None。`fetch()`/`fetch_raw()` 过滤改 None 安全: 缺价 Quote 不出现(调用方视角=「无数据」), **partial(价在字段缺)照常透传** status/missing_fields。
- 其余 vendor 逐个判定(全仓 `or 0)` 审计): `alphavantage.py`/`twelvedata.py`/`yfinance.py` OHLC+量缺失全部 None 化+partial 标注(此前 `or 0) or None` 双重洗白); `zhitu_full.py` 新增 `_num` 助手 —— K线 OHLC 缺失的行**整行丢弃**(绝不产出 0 价 bar 混进均线), 资金流/股东/估值缺失保留 None(字段皆 Optional); `sina.py` 美股/港股价格守卫去掉误导性 `or 0.0`(行为不变, 0 从不外泄)。判定保留: `kline.py:63` volume(Bar 契约 0 默认, K线维度治理归 1.2/B1)、`ths_hot.py:157` rank(0=未上榜约定哨兵)、`board_fund_flow.py:149` total(分页控制流非行情值)。
- 下游接线: `src/core/marketdata_client.py` `_quote_to_row` 透传 `status`/`missing_fields`(前端 JSON 可显式标「无数据」); `md_stock_data` **跳过缺价 Quote**(绝不 0.0 进 agent 算术), status 随行; `src/models/market.py` `StockData` 增 `status: str = "ok"`(加性, 旧契约数值字段不动)。复权污染涉及的涨跌幅算术点(accounts.py:522 已有 None 守卫, 价格 None 时不再算出 -100%)。
- 测试: `tests/test_vendor_missing_fields.py` 16 用例 —— tencent 残缺报文 fixture(缺价→None 非 0.0; 全价格缺→missing; partial 保价; turnover 缺失; turnover 恒等式单位=元; 真实"0.00"≠缺失), zhitu(_num/K线丢行/资金流 None), alphavantage/twelvedata(缺字段 None+partial/missing), Quote 默认值, `_quote_to_row` 透传, `md_stock_data` 跳过缺价。
- 验证: 新用例 16 passed; 全量 `PYTHONUTF8=1 pytest tests/` 1838 passed / 6 failed —— 6 失败均为环境问题(缺 tradingagents/psycopg2/thsdk、DOCKER 门控、Windows 文件权限), **均不 import 本次改动模块**(grep 验证), 与 0.5 收编时基线一致。
- [branch fix/wave1-数据正确性-20260908, `git show HEAD`]

### docs-0.7 K线复权污染勘查报告(只读, 四类污染实证)
- 背景: 风险方案 0.7 勘查任务 —— 审计 B1 断言"qfq 与不复权混存于同一批唯一键, 除权后假跳空"。本任务只读 PG + 走读代码, 产出 `docs/research/K线复权污染勘查_20260907.md`。全程零写入(仅 SELECT), 未改任何代码。
- 修正 B1 模型: 实测 69,154 个 (symbol,date) 三源 close 逐格相等零差异 —— ingestor 自 P2-19(2026-09-05) 起单链拉一次复写三个 source 标签(klines_ingestor.py:73-83), `source` 列无区分度; 取数口径实为前复权(腾讯 fqkline/qfq + 东财 fqt=1), 新浪不复权兜底不写 PG。表结构三处出入: 日期列是 `ts` 非 `trade_date`、唯一键已含 `period`、**无 `amount` 列**(方案 dev 判据不可执行, 改用涨跌停边界缺口判据)。
- 实证四类污染: ①相邻柱复权基准漂移 —— 主板 19 个物理不可能缺口(超 ±10% 涨跌幅 0.5~1.6%, 11 只股, 2023-06~2025-04), 600639 一字板互证相邻比率 ×1.112 连续复利(真实涨停链必须精确 ×1.10); ②同日双柱 —— 537 键因 ts 时区约定切换(P2-19 前后)各写两份, 含 708 行平线占位柱(volume=0, OHLC=开盘/昨收, 全落 08-31~09-04), **09-07/09-08 仍有 55 只/日同值双柱, 污染持续中**; ③volume 单位混用 —— 46/86 只在 08-28→08-31 整体 ×100(腾讯手→股修复部署), 全史 85 个 ×20+ 跳变散点, 跨界量比/筹码类指标 ×100 失真; ④qfq 基准冻结(B1 原机制) —— 历史段基准钉死在 T0(2026-08-17~24 回灌), DO NOTHING 永不重算, 勘查窗口内除权假缺口零发作(分红季在回灌前), 属"必然而未至"。
- 读取方影响: `/api/klines`(PG 优先主路径)与 `backtest/data_adapter.py` 均按 source='tencent' 过滤但**无 ts 日期去重** → 双柱直进前端图表与回测序列; `kline_collector._pg_fallback` 连 source 都不过滤 → HTTP 全挂时指标在 3 倍序列上计算。
- 结论: **B1 必须走「加列 + 全量重刷」**(污染是散点不成区间 + 基准冻结全局性质 + 46 只 volume 历史段整体错单位; 209K 行重刷成本分钟级)。配套前提与三条复跑验收 SQL 已写入报告 §4/附C; 动生产前须老板确认节奏(与 0.4③ 同批)。
- 无法判定项(诚实项): 19 缺口精确成因(需外部对账/engine 择源日志, 三源同值导致供数方信息已丢失)、缺口日是否恰为除权日(未查外部分红日历)、双柱写入方时间线(需容器日志)、09-08 后双柱是否停止(需次日复查)。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-8010切断主库旁路+裁判abstain不伪装(风险方案0.0)
- 背景: 8010 预测引擎此前持有多条主库旁路 —— panwatch_client 直读主库 sqlite 取 jwt_secret **自签 owner JWT**(等价伪造全权凭据, 能调任何 owner 接口含改配置/删数据), ai_referee/forecast_sentiment 各有一套三候选 sqlite 直读配置; 且裁判 fail-open: 任何异常降级 verdict=confirm, 等于把"裁判确认过"写进预测记录。主库切 PG 后这些直读全部静默失效(读到冻结旧值且不报错)。
- `forecast_lib/panwatch_client.py`: 自签 JWT 路径整体删除(`_read_auth_settings`/`_create_service_token` 与 HMAC 签名逻辑, forecast 侧不再有任何 JWT 签名密钥来源); 改为只读服务凭据 `PANWATCH_SERVICE_TOKEN`/`SIDA_SERVICE_TOKEN` → 请求头 `X-Service-Token`(优先), 显式账号密码登录换真实 Bearer 的缓存路径保留(与伪造无关); 无任何凭据时 `auth_headers()` 返回空 dict、`get_token()` 返回空串, 由调用方显式报错, 绝不静默兜底。
- `forecast_lib/ai_referee.py`: 删 `_db_paths`/`_db_scene_binding_model_id`/`_db_model_by_id` sqlite 直读; 新增 `RefereeConfigUnavailable` + `resolve_referee_model_cfg()` —— 裁判模型配置唯一通道是 `/api/service/forecast-config`(服务令牌鉴权), referee 场景绑定 > 设置页 forecast_llm_*, 均不可得即抛错, **绝不回落硬编码 agnes**; `evaluate_prediction` 任何异常/无凭据/解析失败一律返回 `verdict="abstain"`(维持模型方向, 不伪 confirm), abstain 不落 prediction_referee_evals; `_parse_verdict` 接受 abstain; prompt 增 abstain 输出约定 + 口径裁决句(严禁用 get_capital_flow 的东财口径直接下"主力派发/吸筹"结论; 与 get_main_intent 冲突时说明口径差异并优先采信逐笔口径)。
- `forecast_lib/forecast_sentiment.py`: 删 `_db_llm_config` 三候选直读与其在 `_load_llm_config` 的调用分支; LLM 情绪打分配置唯一通道 = `/api/service/forecast-config`(T7 已落的 HTTP 通道)。
- 8000 侧配套: `src/web/api/auth.py` 新增 `get_service_principal`(仅 X-Service-Token 可过, 用户 JWT 一律 403 —— 会下发明文 api_key 的端点专用); `src/web/api/service_config.py` forecast-config 依赖从 `get_user_or_service` 收紧为它, 关闭"登录用户也能读明文 api_key"的洞; `src/web/api/chat.py` 建会话/发消息两端点挂 `get_user_or_service`(8010 裁判经服务令牌建/发, 会话 `user_id=NULL` 为系统会话, 与用户会话按归属互相隔离, S2 语义不变)。
- `forecast_server.py`: 裁判异常从"降级 confirm"改为 abstain(维持模型方向, 不再伪造"裁判确认过"); `docker-compose.yml` forecast 服务删 `PANWATCH_DB` env 与 `panwatch_data:/app/panwatch-data:ro` 主数据卷挂载 —— 8010 对主库零接触。
- 测试: `tests/test_ai_referee_http.py` 新增 12 用例(绑定命中/回落 forecast_llm_*/三者皆无抛错/API 不可达抛错/响应异常抛错; prompt 口径句; abstain 解析; 网络错误/无凭据/配置不可得 → abstain 且不落库; confirm 正常落库且建会话带服务令牌+ai_model_id), `tests/test_internal_scene_model_auth.py` 新增 7 用例(正确令牌 200 且读到 forecast_llm_*、错/无令牌 403、**合法 admin JWT 也 403 且响应不含 api_key**), `tests/test_forecast_container_config.py` 重写(原用例断言的正是本次删除的自签 JWT 行为; 改为断言服务令牌头/无凭据空 dict)。
- 验证: 新用例 22 passed(此两文件+container_config); 回归 test_user_isolation_api/test_p1_service_token/test_forecast_config_channel/test_chat_stream/test_ai_client_degradation/test_agent_notify_gate/test_chat_tools_p1p2|a4|two 共 100 passed; 验收 grep `PANWATCH_DB|_db_paths|panwatch\.db`(forecast_lib/+docker-compose.yml)与 `_read_auth_settings|_create_service_token|jwt_secret|auth_token_version`(forecast_lib/)全 0 命中, `优先采信 get_main_intent` 在 prompt 中(测试断言); `docker compose config -q` 通过; 旧 panwatch.db mv 出卷属生产数据操作, 与 0.4③ 同节奏待老板确认。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-LLM降级改抛类型化异常+显式超时+推送总闸+成本护栏fail-closed(风险方案0.3)
- 背景: 限流时 ai_client 返回普通字符串 `"AI 服务暂时不可用(限流)…"`, 类型上与正常 LLM 输出无法区分, 曾被 daily_report 当日报存库并推送给老板; 且 AsyncOpenAI 未传 timeout 走 SDK 默认(~600s), 一次挂起能把 agent 卡住十分钟。本次按风险方案 0.3 全链路整改"降级不伪装"。
- `src/core/ai_client.py`:
  - 新增 `LLMDegradedError(RuntimeError)`(携带 reason/scene/retry_after), chat/chat_multi/chat_with_tools 的全部 6 处降级点(令牌桶耗尽 + 429 + 超时/连接错误)一律改抛, 不再返回字符串或伪装 SimpleNamespace; 配置类错误(401/400)仍原样抛出不掩盖。
  - 构造 AsyncOpenAI 显式 `httpx.Timeout(connect=10, read=可配, write=30, pool=10)`, read 由 `LLM_READ_TIMEOUT` 环境变量调节并钳制 [10,300]s, 默认 90s。
  - 修存量 bug: `_is_retryable_error` 靠类名字符串匹配, "ConnectError" 匹配不上 "connection" 导致连接错误从不重试/降级 → 改 `isinstance(e, httpx.TransportError)` 显式判定。
  - `_log_usage` 同步 DB commit 阻塞事件循环 → 拆 `_log_usage_sync` + run_in_executor fire-and-forget(无事件循环时回退同步); `chat_multi` 此前从不记账 → 补 `_log_usage` 调用。
- `src/agents/base.py`: `AnalysisResult` 增 `status`(success/degraded/failed)+`error` 字段; `analyze` catch LLMDegradedError → `_degraded_result()`(title 生成失败/content "AI 分析未生成：<原因>"/status=degraded); `should_notify` 推送总闸最前面拦截非 success。5 个 agent(daily_report/premarket_outlook/news_digest/chart_analyst/intraday_monitor)逐个接 catch, 其中 3 个覆写 should_notify 的自己补了同款拦截; daily_report/news_digest 降级时以 status="degraded" 落库 save_analysis, 不再解析正文。
- `src/web/api/agents.py` 盘中建议循环 `_analyze_item` catch LLMDegradedError → log+return 跳过该股, 不伪造建议条目; `src/web/api/chat.py` 两个 tool-loop 对 LLMDegradedError 显式 re-raise(不走"tool use 不可用→chat_multi 兜底"的放大路径, 用户侧仍由外层给出诚实文案)。
- 成本护栏 fail-closed: `src/agents/tradingagents/cost_tracker.py` 查询失败从"默认放行"改为 `exceeded=True` + `reason="预算查询失败，保守拦截"`, 明确知情后可 `TA_BUDGET_FAIL_OPEN=1` 切回放行; `SessionLocal()` 挪进 try(原在 try 外, 库连不上会裸抛而非拦截)。
- 持久化与前端: `AnalysisHistory` 增 `status`/`error` 列 + 迁移 `_m136_analysis_history_status`(存量回填 success, 幂等); `save_analysis` 增同名参数双路径写入; `/api/history` 列表与详情 `HistoryResponse` 透出 status/error; 前端 `frontend/src/pages/History.tsx` 非 success 显示「未生成」红色徽标与红框态(列表+正文+详情弹窗), 不把失败文案当分析正文渲染 —— 落实"数据缺失显式标注, 禁止编造"红线。
- 测试: `tests/test_ai_client_degradation.py` 20 用例(429/503/read-timeout/connect-error × chat/chat_multi/chat_with_tools 全部断言抛 LLMDegradedError 且不返回字符串; 令牌桶耗尽不触达 SDK; chat_multi 记账; 构造 timeout 默认/钳上下限/非法值; cost_tracker DB 故障 fail-closed + env 放行), `tests/test_agent_notify_gate.py` 11 用例(degraded/failed × 5 agent should_notify 全 False + _degraded_result 字段)。
- 验证: 新用例 31 passed; 回归 test_tradingagents_agent/test_user_isolation_api/test_user_isolation_core/test_chat_stream/test_daily_report_index/test_paper_trading_notify/test_ai_provider_sniff 95 passed, test_user_isolation_migrations/test_premarket_catalyst/test_context_enrichments/test_chat_ai_layer_tools/test_chat_l2_tools 89 passed; 验收 grep `"AI 服务暂时不可用"` ai_client.py 0 命中, 构造处含 httpx.Timeout, 8 文件含 except LLMDegradedError; `pnpm typecheck` 通过。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-启动方言门禁fail-stop+/api/health方言标签+清docs明文密码(风险方案0.4①②残留)
- 背景: 丢 `SIDA_DB_URL` 曾让生产静默回退 SQLite 跑 4 天。compose 侧(方案步骤①)v0.5.18 已解开 SIDA_DB_URL 并对 POSTGRES/REDIS 密码 fail-fast(`:?` 语法), 本次补齐启动期门禁与泄露清理。
- `src/core/startup_check.py` 新增 `check_db_dialect_explicit()`: `SIDA_DB_URL` 未设置 → 拒绝启动, 除非显式 `SIDA_ALLOW_SQLITE=1`(仅本地开发/快速上手, 打醒目 WARNING 横幅); `server.py` lifespan 在 `init_db` 之前调用该门禁, 不过即 raise 终止启动 —— "丢 env 带病起服务"物理上不可能再发生。`src/web/database.py` 容器内 fail-fast(DOCKER=1 无 SIDA_DB_URL 即 raise)同步加 `SIDA_ALLOW_SQLITE=1` 唯一逃生口(容器内开发/测试用)。
- `src/web/api/health.py` `components.database` 新增 `dialect` 字段(取 `engine.url.get_backend_name()`, DB 查不通时按声明方言 IS_PG 兜底), 一眼看出连的是哪个库; 测试环境为 sqlite, 生产 PG 部署后应为 postgresql。前端顶栏展示不在本批残留清单内, 留给后续前端批次。
- `tests/conftest.py` 顶部 `setdefault("SIDA_ALLOW_SQLITE", "1")`(测试即本地开发模式, 免得门禁生效后全部测试被拒启动)。
- `tests/test_startup_check_dialect.py` 新增 5 用例: 无任何 env → 拒绝启动; `SIDA_ALLOW_SQLITE=1` → 放行+WARNING 横幅断言; 显式 PG / 显式 SQLite URL → 放行; `/api/health` `dialect == "sqlite"`。
- `docs/_frozen/data.md:14` 明文 PG 密码改为"密码见部署机 .env 的 POSTGRES_PASSWORD"; `git grep PanWatch2026PG` tracked 文件 0 命中。密码轮换(0.4③)涉及生产, 按方案需老板确认节奏后再执行, 本批未动凭证。
- compose 顺手修一个阻断性 bug: panwatch 的 `depends_on` 写了 `panwatch-redis`(那是 container_name, 服务键是 `redis`), 且 redis 带 `profiles: ["infra"]` 默认不启动 → `docker compose config` 直接报 invalid project, 0.4 验收第 1 条(config 解析出 SIDA_DB_URL)在 v0.5.18 上本来就不可能通过。改为 `redis: {condition: service_healthy, required: false}`(infra profile 开启时仍守健康门, 默认栈靠应用自身 redis 降级路径, /api/health 对 redis down 已按预期降级处理)。
- 验证: 新用例 5 passed; 存量回归 `test_startup_check.py` 6 passed / 改密+防回归静态 7 passed / multi_user+ratelimit 14 passed; `POSTGRES_PASSWORD=dummy REDIS_PASSWORD=dummy GF_SECURITY_ADMIN_PASSWORD=dummy SIDA_SERVICE_TOKEN=dummy docker compose config` 成功输出 `SIDA_DB_URL: postgresql+psycopg2://sida:***@panwatch-postgres:5432/sida`(dummy 为验证用假值); py_compile 6 文件通过。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-部署脚本改克隆式安全重建+接线冒烟门禁+CI stub自检(风险方案0.1残留)
- `deploy/deploy_panwatch.sh` rebuild_container 重写。根因: 实测生产拓扑与脚本硬编码不一致(真实卷 `panwatch-data`/`panwatch-tck` 连字符命名 + 自定义网络 `panwatch-net` + `restart=always`/无内存限制; 原脚本硬编码 `panwatch_data` 下划线卷、无 `--network`、固定 unless-stopped+1g) —— 照原硬编码重建会造出连不上 redis/postgres 的孤儿容器。改为克隆式: 以运行中容器为唯一事实源 harvest env/卷/端口/网络/重启策略/内存 → 临时容器(`${CONTAINER}_new`, 8001)先起 → curl 健康 + `WEB_HOST=0.0.0.0` inspect 校验(任一失败删临时容器退出, 旧容器原样在跑, 无损回滚) → swap → 最终容器端口继承旧容器; 防漂移告警改与 harvested 值比对(生产 restart=always 不误报); 全新安装走 default_config 兜底(卷名同步改连字符); `DOCKER` 可环境变量覆盖供 CI stub。
- 冒烟门禁: 部署尾部硬闸调用 `scripts/post_deploy_smoke.sh`, 失败 exit 1 并保留运行容器便于排查(`PANWATCH_SKIP_SMOKE=1` 供 CI/测试跳过)。同时修 post_deploy_smoke.sh 三处静默假通过: smoke_test.py 缺失/python3 无 requests → 显式 FAIL exit 1(原会 traceback 后因输出无 "FAIL" 而 exit 0); 退出码改透传 smoke_test.py 的 RC, 不再以输出含 "FAIL" 判定(误报源); PW 容器名/LOG/脚本路径全部环境变量可覆盖(本机实测 /home/ubuntu/scripts 与 backups 均不存在, 原脚本必然假通过)。
- `scripts/tests/test_deploy_script.sh` 新增 13 断言(`DOCKER=echo` 桩, 不动真 Docker): run 行含全部关键参数(-e WEB_HOST=0.0.0.0 / --memory / --restart / -p 8000:8000 与 8001 / -v panwatch-data:/app/data / 镜像名), 无 `-e: command not found`(0.1 类"注释截断续行命令"事故回归), "新容器健康"先于 `rm -f panwatch`(先验证后删旧顺序), create 失败场景退出非零且全程无 `rm -f`(失败不删旧容器), 部署脚本确实接线 post_deploy_smoke.sh。
- CI: build-push-acr.yml gates 增 step 跑该 stub 测试 —— 这是防止"注释再次被插回 docker run 续行块"的唯一机制(2026-09-08 停机事故)。
- 验证: 三个脚本 `bash -n` 通过; stub 测试 13 passed 0 failed; harvest 格式串对生产容器实测逐项解析正确(27 env/2 卷/8000 端口映射/panwatch-net/restart=always/MEM=0)。真机 `--full` 重建未执行(生产操作需老板确认节奏), 上生产时由临时容器验证+冒烟门禁双兜底。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-删固定管理员密码/开关, 兜底首启改随机密码+stdin外一次性打印, workflow action 全量钉 SHA(风险方案0.6残留)
- `src/web/api/auth.py` — 删除 `DEFAULT_ADMIN_PASSWORD` 常量与固定密码兜底分支(公开仓库源码内固定密码=失守入口); 兜底首启改 `secrets.token_urlsafe(12)` 随机强密码, 仅启动时 stderr 打印一次并引导"设置 → 修改密码"(自助改密端点 `POST /api/auth/change-password` 旧密码校验+token_version 踢人本就有, 前端 AccountMenu/Profile 已接入, docs/KNOWN_ISSUES.md 的 P1"无改密入口"就此关闭); env 凭证改惰性读取 `_env_credentials()`, 允许测试 import 后注入。生产部署注意: 裸库且无 `AUTH_USERNAME/AUTH_PASSWORD` 时, 升级后首启密码以 Docker logs 为准(仅打印一次)。
- 测试去硬编码: test_multi_user_auth / test_permissions_rbac(派单清单漏了此文件, grep 全仓补出 4 处) / test_chat_stream / test_entry_candidate_feedback_api 的 admin 登录改 env fixture 引导(清库+`AUTH_USERNAME/AUTH_PASSWORD`); test_p1_service_token 删无用的默认密码开关 setenv; test_security_20260823 两个 P2-5 用例重写为断言"常量/开关不存在+兜底走 secrets+改密引导存在"(旧断言引用已删常量必 AttributeError); 密码字面量在测试源码内一律拆串拼接, 测试文件自身不做明文载体。
- `tests/test_auth_no_default_password.py` 新增 4 静态用例(密码字面量/常量/开关全仓零残留+兜底随机+改密端点存在)、`tests/test_auth_change_password.py` 新增 3 行为用例(旧密码错 400、新密码过短 400、改密成功踢旧 token+旧密码失效)。5 个登录密集 fixture 统一 `RATE_LIMIT_ENABLED=False`(登录防爆破 20/min/IP 是进程级共享桶, 多文件合跑必然 429, test_ratelimit_per_user 自建 app 不受影响)。
- workflows: build-and-push-image.yml / release.yml 删 `AUTH_ALLOW_DEFAULT_ADMIN: "1"`(CI 门禁登录测试已自备凭证); 全部 8 个 workflow 共 35 处 `uses:` 从 tag 钉到 40-hex commit SHA(pnpm/action-setup v4 为 annotated tag, 取 `^{}` 解引用值), 消除第三方 action tag 劫持面。
- `scripts/p3a_accept.py` admin 凭证改 `P3A_ADMIN_USER/P3A_ADMIN_PASS` 环境变量注入, 缺失即拒绝运行(生产验收脚本不得内嵌凭证); frontend 移除零引用幽灵依赖 `date-fn`。
- 验证: 7 个关联测试文件组合 53 passed 零失败; test_security_20260823 stash 基线对比 17 failed → 13 failed(差值 4 全为本改修复, 余 13 均为 Windows GBK 环境存量: 该文件 30 处 bare `open()` 无 encoding, CI UTF-8 不复现); 验收 grep `xz.170530|DEFAULT_ADMIN_PASSWORD|AUTH_ALLOW_DEFAULT_ADMIN` 全仓(除 CHANGELOG 历史与 git-ignored 的 data/panwatch.db 运行数据)零命中; 本地 gitleaks `detect --no-git` 0 命中; `pnpm typecheck` 通过。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-CI门禁补密钥扫描+bash语法检查+Python钉3.11(风险方案0.5残留)
- `build-push-acr.yml` gates 补三个 step(pipefail/删`||true`/PR触发已随全面体检T3落地, 勿重做):
  1. `bash -n deploy/*.sh scripts/*.sh` — 0.1 类"续行块内注释截断命令"语法事故在 CI 即可发现。
  2. gitleaks 密钥扫描: 固定 v8.18.4 二进制 + SHA256 校验和(`ba6dbb...8e7d`), **不引入未钉版的第三方 action**(与 0.6 的 action 钉版治理方向一致); `--no-git` 扫工作区不依赖历史深度, `--redact` 防命中内容泄进 CI 日志。
  3. `actions/setup-python@v5` 钉 3.11(与生产一致; 此前用 runner 默认 python3, 版本随 ubuntu-latest 漂移)。
- 验证: workflow YAML 解析 OK; 本地 `bash -n` 全部 7 个脚本通过; 本地 Windows 版 gitleaks 8.18.4 全仓 `detect --no-git` 实跑 0 命中(存量内容不会卡红门禁; 注意默认规则不识别 docs/_frozen/data.md:14 的自拟 PG 密码, 该文件清理仍归 0.4)。CI 端到端红/绿演练待 PR 触发后补录。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-数据源失败计数接线激活SidaDatasourceFailures告警(风险方案0.2)
- 根因: `sida_datasource_failures_total` 计数器定义后全仓零调用方(仅注释提及), prometheus-rules.yml 的 `increase(...[15m]) > 50` 告警永不触发 —— 数据源(东财/新浪/腾讯/通达信)全黑监控无声。
- 做法与派单方案的偏差: 方案建议改 marketdata vendor 层并经 `src/collectors/_metrics.py` 包 CM; 实测 vendor 既被 collectors/core/web 直调(21+ 处)又经单源 Engine 调用, 且 packages/marketdata 不能反向依赖 src.web。改为: `marketdata/vendors/base.py` 的 `Vendor.__init_subclass__` 在每个子类 `fetch` 定义处自动包失败上报(`emit_vendor_failure(name, kind)`, 异常原样抛出不吞; 监听者异常互不反噬); `engine.py` 的 TimeoutError 分支补发 `kind="timeout"`、异常分支按凭证错补发 `kind="auth"`/`"fetch"`; `src/web/api/health.py` 导入时 `on_vendor_failure` 注册桥接 → `record_datasource_failure`。一处覆盖直调/Engine/未来新增三条路径。
- `record_datasource_failure` 加 kind 枚举归一(fetch|parse|timeout|auth, 未知归 fetch)防 label 基数膨胀; 注释与 docstring 同步指明调用点; BoardFundFlow/Discovery 两类本就不进 Engine/DataSource 体系, 不入告警域。
- `tests/test_datasource_failure_metrics.py` 新增 7 用例: fetch 失败上报+原样上抛/成功不误报/监听者异常隔离/注册幂等/真实 tencent vendor 断网(monkeypatch market_get)上报/桥接真实驱动 Prometheus 计数/labelnames 无 symbol+kind 归一。
- 验证: 新用例 7 passed; marketdata/datasource 相关 44 个测试文件分两批 86 passed(批1 79)与 226 passed(批2), stash 本改动前后两批失败集完全一致(17 failed 均为本机缺 .env/AUTH_ALLOW_DEFAULT_ADMIN 的环境存量), 零新增失败。本机环境修正: `marketdata` editable 安装原指向旧 clone sida-pro, 已重指本仓 packages/marketdata(否则测试解析到 v0.5.11 旧代码)。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### fix-每用户限流分桶取错JWT claim(恒按IP)改统一解析sub(风险方案0.8)
- `src/web/api/auth.py` — 新增 `principal_from_payload()`: JWT payload → request.state.user 的统一形状, 用户 id 取 `sub`(兜底历史 `user_id`), username/role 平级; 单一出口防止第三处中间件再各写各的。
- `src/web/middleware.py` — JWTDecodeMiddleware 原 `"user_id": payload.get("user_id")` 取的是 JWT 里不存在的 claim(恒 None)→ 限流分桶永远走 IP, 同出口 IP 的多账号一人跑重活全员被限; 改调 `principal_from_payload()`。AuditMiddleware 的手写解析同款收编(原取法碰巧对, 但属重复实现)。
- `tests/test_ratelimit_per_user.py` 新增 6 用例: principal_from_payload 三态(sub/兜底/空)、真 JWT 闭环下 state.user.user_id==sub、用户 A 打满 429 同 IP 用户 B 不受影响(修复前必 429 的核心回归)、匿名仍按 IP。
- 验证: 新用例 6 passed; 关联 5 文件组合(test_ratelimit_per_user/security_20260823/multi_user_auth/p1_service_token/ws_auth_guard)基线对比 —— stash 本改动前后均 25 failed/33 passed, 失败集完全一致(本机缺 AUTH_ALLOW_DEFAULT_ADMIN/.env 的环境存量, 与 v0.5.18 发版注记的"Windows 环境存量"同类), 本改动零新增失败; `grep 'payload.get("user_id")' middleware.py` 零命中。全量门禁留待批次合并前统一跑。
- [branch fix/wave0-止血-20260907, `git show HEAD`]

### update-发版 v0.5.18(全面体检P0+P1修复合入main)
- 本次发版内容: 前端错误上报端点修复+R5门禁 / 部署脚本截断修复 / CI门禁pipefail+PR触发+forecast镜像CI / envelope Redis连接复用 / 调度防双跑 / 模拟盘user_id隔离(迁移_m135) / InteractiveKline金额口径 / health脱敏 / WS鉴权+广播过滤 / forecast配置走HTTP服务token / compose安全加固 / Alertmanager告警闭环+备份docker exec。
- 部署注意(.env 新增必填): POSTGRES_PASSWORD / REDIS_PASSWORD / SIDA_SERVICE_TOKEN —— 缺失时 compose up 直接报错(fail-fast, 勿用旧 env 直接拉起)。
- 验证: 后端全量 1746 passed / 3 failed(Windows 环境存量, 与 main 基线一致); 前端 tsc+build+UI门禁 OK; 迁移校验 35 个迁移一致。
- [tag v0.5.18]

### feature-SIDA直调skill(Hermes可取生产数据)
- 新增`skills/sida-pro-data/` — SKILL.md(端点表/两级认证/单位铁律)+`scripts/sida.py`(thin-client,只拼URL带头打印JSON,零密钥落仓)。
- 已装进 Hermes:`~/.hermes/skills/sida-pro-data`软链指向仓内, 仓内改即时生效。
- **实测生产**: health ok(v0.5.14/PG); quote 002361 现价10.27(-0.87%); decision verdict 看看(缺资金信号); trust tencent 100分(p50 154ms)。
- 认证说明: svc口(quotes/klines)服务token可进, 用户JWT同样可进(data_read双轨); user口(decision/trust/accuracy/darkflow)需用户JWT(`/api/auth/login`拿, 字段是`data.token`)。
- [branch feat/sida-skill-0908, `git show HEAD`]

### fix-test_ws_auth_guard全量suite下因asyncio.get_event_loop无运行loop报错→改同步get_nowait
- `tests/test_ws_auth_guard.py` — `_broadcast` 用 put_nowait 塞帧, 测试同步 `get_nowait()` 取即可; 原 `asyncio.get_event_loop()` 在全量 suite 中(主线程无运行 loop, 被其他用例关闭)抛 RuntimeError。单跑过、合跑挂的典型 flaky, 回归 `tests/test_ws_auth_guard.py` 单文件 + P1 全量 suite 1746 passed 双验证。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-告警闭环(无Alertmanager五条规则无出口)+备份脚本适配compose拓扑(docker exec)+失败告警
- 新增 `deploy/alertmanager.yml` — 路由到 Hermes 企微桥(docker0 网关, 与 forecast 企微推送同通道); critical 级 1h 重复, 常规 4h。
- `deploy/prometheus.yml` — 补 `alerting.alertmanagers: [alertmanager:9093]`("告警从没响过"的后一半根因: 指标名修对但根本没有接收端)。
- `deploy/loki-config.yml` — ruler 的 `alertmanager_url` 由 `localhost:9093`(指向不存在服务)改 `alertmanager:9093`。
- `docker-compose.yml` + `docker-compose.infra.yml` — 新增 alertmanager 服务(prom/alertmanager v0.27, 127.0.0.1:9093, 配置/数据卷挂载)。
- `scripts/backup_pg.sh` — ①默认改 `docker exec panwatch-postgres pg_dump`(原 PGHOST=127.0.0.1 但 compose PG 无端口映射, cron 必失败且无人知晓); 显式 TCP 降为兜底模式; ②备份失败(脚本异常/dump 为空/非 PGDMP)经 Hermes webhook 告警——静默的备份等于没有备份; ③恢复演练步骤更新为 docker exec 形态。
- 验证: 五份 YAML `yaml.safe_load` 全过; bash 语法/实弹告警待小主机部署实测(本地无 bash/docker)。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-compose安全加固(弱默认密码强制化/infra端口仅本机/Redis加密码/非root/内存与日志上限/规则挂载/主卷只读)
- `docker-compose.yml` — ① PG 密码 `${POSTGRES_PASSWORD:?}` 强制(两处; 原 `:-sida_dev_only` 弱默认静默兜底); ② Redis 加 `--requirepass ${REDIS_PASSWORD:?}` + healthcheck 带密码 + 主服务 REDIS_URL 带密码(原无密码, 主机任意进程可直读全部缓存); ③ Prometheus/Loki/Grafana 端口绑 127.0.0.1(原 0.0.0.0 且无鉴权, 与 infra 编排对齐); ④ prometheus 补挂 `prometheus-rules.yml`(原缺失, --profile infra 启动即崩); ⑤ 全部 8 服务加 mem_limit(主 1.5g/forecast 4g/PG 1g/redis 320m/infra 各 192-512m)+ json-file 日志轮转(max-size 20-50m × 3, 上次 / 分区打满根因之一); ⑥ infra 容器补 TZ; ⑦ forecast 对主数据卷改 :ro(原 root 容器可写主卷)。
- `Dockerfile.forecast` — 非 root(uid 10001 app, 对齐主 Dockerfile)+ 补装 tzdata(已设 TZ 但 slim 无时区数据, zoneinfo 实际失效)。
- `Dockerfile.kronos` — 非 root。
- `.env.example` — 新增 POSTGRES_PASSWORD / REDIS_PASSWORD 必填说明。
- 验证: compose YAML 解析 OK; 脚本断言 8 服务全有 mem_limit+logging、弱默认清除、端口绑定、规则挂载、只读卷 —— ALL OK。docker build/实机拉起待小主机下次部署实测。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-forecast配置通道PG下静默失效(LLM/裁判绑定/鉴权三线断裂)→改走HTTP服务token下发
- 新增 `src/web/api/service_config.py` — `GET /api/service/forecast-config`(挂 data_read 组, 服务 token/用户 JWT): 一次性下发 ①app_settings.forecast_llm_* ②ai_scene_bindings 的 referee 绑定 + ai_models/ai_services 连接信息。app.py 挂载 + import。
- `forecast_lib/forecast_sentiment.py` — ①删除模块级重复两遍的 `PANWATCH_URL=_detect_panwatch_url()` 死代码(探测逻辑原跑两次); ②新增 `_fetch_llm_config_via_api()` 走 HTTP 优先, sqlite 直读降为遗留兜底; ③兜底硬编码 agnes 时显式打警告(原静默回落, 用户配置被无视无感知)。
- `forecast_lib/ai_referee.py` — `resolve_referee_model_cfg()` 优先级插入 API 通道(PANWATCH_DB 或 PANWATCH_SERVICE_TOKEN 存在时启用), DB 直读降为遗留; 失败打 warning 不静默。
- `forecast_lib/panwatch_client.py` — ①`request_json` 支持 `X-Service-Token` 头(compose 注入 PANWATCH_SERVICE_TOKEN=SIDA_SERVICE_TOKEN 即通, 不再依赖 jwt_secret 自签 JWT —— PG 下 forecast 读不到主库, 旧自签通道必断); ②`_read_auth_settings` 对"环境变量指了但文件不存在"打 warning(原静默跳过)。
- `docker-compose.yml` — forecast 服务注入 `PANWATCH_SERVICE_TOKEN=${SIDA_SERVICE_TOKEN:-}`; PANWATCH_DB 注释标注为遗留兜底。
- `.env.example` — 新增 SIDA_SERVICE_TOKEN 说明项。
- 新增 `tests/test_forecast_config_channel.py` — 3 用例: llm 下发/referee 绑定下发(含连接信息)/路由挂载于 data_read 组。
- 验证: 3 passed; forecast_lib 三文件 ast 语法 OK; compose YAML 解析 OK。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-WS鉴权只验签不验状态(禁用/踢人后旧token仍可连)+行情广播跨用户泄露关注集合
- `src/web/api/auth.py` — 新增 `verify_ws_token_payload(db, payload)`: WS 握手专用, 对齐 HTTP 层口径(校验 `is_active` + `token_version`, 畸形 ver 显式拒); 通过返回 user_id。原两处 WS(quote_stream/ws_hub)只 `decode_token`, 禁用账号/改密踢人后旧 JWT 在剩余有效期(默认 12h)内仍可连 WS 收通知/行情。
- `src/web/api/quote_stream.py` — ① 握手改走 `verify_ws_token_payload`; ② 订阅绑定 user_id(`subscribe(user_id)`), 聚合器刷新 per-user 关注集合缓存(`_user_symbols_cache`, ""=历史遗留共享账户人人可见), `_broadcast` 按订阅者 symbol 集过滤后再 pack(定向帧 envelope.user_id=本人, 与 `?last_seq=` 重放过滤口径一致); 快照同样过滤; 无关注标的的用户不收帧(不再从推送内容推断他人持仓/自选)。
- `src/web/notifications/ws_hub.py` — 通知 WS 握手同样补 is_active/token_version 校验。
- 新增 `tests/test_ws_auth_guard.py` — 6 用例: 正常通过/禁用拒/旧版本拒/畸形 ver 拒/广播按用户过滤(A 收不到仅属于 B 的标的)/无关注用户不收帧。
- 验证: ws_guard + p2_realtime 共 12 passed。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-health/metrics信息泄露(REDIS_URL原文/全量指标匿名可读)+删除/api/health/health双挂载
- `src/web/api/health.py` — ① redis 组件回显完整 `REDIS_URL`(可能带密码)改为 `_mask_url` 只留 scheme://host:port; ② `/api/metrics` 收紧为仅内网/回环来源或持 `SIDA_SERVICE_TOKEN` 者可读, 外部匿名 403(Prometheus 同 compose 网络来源为容器内网 IP, 抓取配置无需改)。
- `src/web/cache/biz_cache.py` — `stats()`(喂 /health 的 biz_cache 组件)同样脱敏; 新增 `_mask_url` 助手。
- `src/web/app.py` — 删除 health router 的重复挂载(原 app.py 两处挂载产生 `/api/health/health` 冗余路径, 且与 `/api/health` 鉴权语义相反); 现单挂载 `/api` 前缀 → `/api/health` + `/api/metrics`。Docker healthcheck 打的 `/api/health` 不受影响(匿名可用)。
- 新增 `tests/test_health_metrics_guard.py` — 5 用例: 内网/回环放行、公网拒绝、连接串脱敏、metrics 匿名 403、内网 200。
- `tests/test_selfcheck.py` — 路由挂载断言更新为单挂载拓扑(并断言冗余路径不得回潮)。
- 验证: health_guard + p4_alerts + selfcheck 共 27 passed。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-InteractiveKline主力意图金额硬除1e4(≥1亿显示"12345万")→toAmount万/亿口径
- `frontend/packages/biz-ui/src/components/InteractiveKline.tsx` — 主力净额/超大/大单金额由 `(x / 1e4).toFixed(0)万` 改 `toAmount()`(含 isFinite 守卫, ≥1亿自动切亿), 与全站金额口径收敛。业务硬约束: 金额=元。
- 验证: `pnpm typecheck`(tsc -b) 0 错; `node scripts/check_ui_rules.mjs` → UI-RULES OK。
- 未做: WencaiPanel/MinuteLwcChart/MarketMainlineCard/MainFlowCompareCard/BoardDetail/stock-insight-modal 另有 6 处改名手抄 fmt(精度互分叉), 属执行方案 T18/Q5 lint+门禁缝隙范围, 本轮不动。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-模拟盘跨账号越权(三表补user_id/引擎多账户扫描/API全链路归属过滤/全局动作收敛owner)
- `src/web/migrations.py` — 新增 `_m135_paper_trading_user_id`(v135): paper_trading_account/positions/trades 三表补 `user_id TEXT` + 索引, 存量行回填最早 owner(与 _m122 同口径, 幂等)。修复: 三表原设计即"单例", 任一登录用户可查看并操作其他账号的模拟盘。
- `src/web/models.py` — 三个模拟盘模型补 `user_id = Column(String(36), nullable=True, index=True)`。
- `src/core/paper_trading_engine.py` — 引擎多账户化: `_get_or_create_account(db, user_id)`(None=owner 调度路径); `_scan_sync` 遍历全部账户逐一建仓/平仓(各扣各的资金); `_check_entries/_check_exits/_update_account_metrics/market_realized_open` 全部按 `account.user_id` 过滤(`_user_scope` 助手, NULL 行=冷启动遗留); 建仓/平仓落 trade 均带归属; `close_position_manual(position_id, user_id)` 跨账号平仓拒绝; `reset_account(user_id)` 只清本人数据。
- `src/web/api/paper_trading.py` — 数据端点(account/positions/trades/metrics/diagnostics/toggle/reset/close/settings)全部 `Depends(get_current_user)` + 归属过滤; 系统级动作(scan/notify-settings 写/notify-test/premarket-plan/daily-summary)收敛 `require_owner`(通知配置是全局 AppSettings 非按用户隔离)。
- `src/core/portfolio_diagnostics.py` — `diagnose_paper_portfolio(user_id)` 按用户过滤。
- `src/web/api/chat.py` — 删除 `hasattr(PaperTradingPosition, "user_id")` 防御式判断(列已真实存在)。
- 新增 `tests/test_paper_trading_isolation.py` — 5 用例: 账户按用户隔离/调度路径归 owner/跨账号平仓拒绝+本人平仓落账/reset 只清本人/组合诊断按用户过滤。fixture 同时替换 database/engine/diagnostics 三处 SessionLocal 引用(引擎是 `from x import` 持独立引用, 只 patch 源模块会误写真实库——首跑已误建 2 行假账户, 已清理)。
- 验证: pytest isolation+notify 23 passed; `scripts/check_migrations.py` ✅ 35 个迁移(v101-v135) 列与模型一致; `from src.web.app import app` OK。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-envelope每帧WS推送新建Redis连接→单例复用(连接风暴)+自愈
- `src/web/realtime/envelope.py` — `_next_seq()` 原在函数体内每次 `redis_sync.from_url()` 新建连接，每帧 WS 推送/每条通知都建连，高频推送下连接风暴。改模块级单例 `_get_seq_client()`（对齐 ws_hub.py 既有写法），建连后 ping 校验；连接异常置空标记、下次取 seq 重建（简单自愈）；`reset_for_tests()` 同步清单例。
- `tests/test_p2_realtime.py` — 新增 `test_seq_client_reused`：mock redis.from_url 计次，三次取 seq 断言 INCR 单调且建连仅 1 次。
- 验证：`pytest tests/test_p2_realtime.py` 6 passed（原 5 用例 + 新 1 用例）。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-AgentScheduler防双跑(max_instances=1/coalesce/misfire)——LLM Agent并发双跑重复通知
- `src/core/scheduler.py` — `add_job` 补 `max_instances=1, coalesce=True, misfire_grace_time=300`：LLM Agent 单次执行数分钟，interval 任务上一轮没跑完下一轮就启动 → 同一 Agent 并发双跑（重复通知/token 翻倍/record_agent_run 竞态）。对齐 server.py 后注册的 4 个 job 与 report/kline_backfill scheduler 的既有口径（此前唯独最核心的 Agent 调度没有防护）。
- 新增 `tests/test_scheduler_guard.py` — 1 用例：mock add_job 捕获参数，断言注册 job 必带三参数。
- 验证：`pytest tests/test_scheduler_guard.py tests/test_p2_realtime.py` 7 passed。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-ACR生产流水线pytest门禁被管道吞退出码(全红照样绿)+补PR门禁+新增forecast镜像CI
- `.github/workflows/build-push-acr.yml` — gates 的 pytest 改 `set -o pipefail` + 去掉 `| tail -3` 与 `pip install || true`（原写法管道退出码取 tail，生产 ACR 镜像构建零测试门禁，与注释宣称相反）；新增 `pull_request: [main]` 触发，build job 加 `if: github.event_name != 'pull_request'`（PR 只跑门禁不推镜像）。
- `.github/workflows/build-and-push-image.yml` — 同步补 PR 门禁 + build job 事件守卫（GHCR 流水线 test job 本身写法正确，只缺 PR 触发）。
- 新增 `.github/workflows/build-push-acr-forecast.yml` — forecast 镜像此前完全没有 CI（生产拉的 `xzxwz-forecast:latest` 只能手工构建）：tag v* 构建 `Dockerfile.forecast` 推 ACR + PR 触发路径过滤门禁。
- 未做：`release.yml`（Docker Hub 通道）为单 job 混合测试+构建+推送，拆分属执行方案 T15 流水线收敛，本轮不动。
- 验证：三份 workflow YAML `yaml.safe_load` 解析通过；门禁红灯/绿灯实弹验证待 push 分支后由 GitHub Actions 执行。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-deploy_panwatch.sh热补丁部署docker run被续行内注释截断(WEB_HOST/memory/restart全丢)+防御校验
- `deploy/deploy_panwatch.sh` — bash 先按行尾 `\` 拼逻辑行再解析注释，`docker run` 续行中间夹的两行 `#` 注释吞掉后续参数：实际只拿到 `-e TZ` 之前的部分，`WEB_HOST=0.0.0.0`、`--memory=1g`、`--restart=unless-stopped`、镜像名全部丢失 → `--full` 热补丁重建必然失败，且丢的正是 v0.4.47 修外部 502 的关键参数。注释移出命令块并在原地写明 bash 语义陷阱。
- 新增防御校验：`docker run` 后 `docker inspect` 核对 WEB_HOST 环境变量（缺失则删容器硬失败）、Memory/RestartPolicy（缺失打 warning），参数丢失当场暴露而不是等线上 502。
- 验证：续行+注释模式全文件扫描无残留；Windows 本地无 bash/WSL，语法与 docker inspect 输出解析待小主机下次部署实测（inspect 字段取法与脚本内既有 `--format` 用法同源）。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-前端错误上报端点双前缀(API_BASE已含/api再拼/api致404全链路失效)+门禁新增R5禁双前缀
- `frontend/src/lib/error-report.ts` — ENDPOINT 由 `${API_BASE}/api/logs/frontend` 改 `${API_BASE}/logs/frontend`：API_BASE 已含 `/api`，原路径实际 POST `/api/api/logs/frontend` 404 且被 `.catch(()=>{})` 吞掉，9-08 建的系统日志闭环前端上报(window.onerror/unhandledrejection/ErrorBoundary)全部静默丢失。顺手把吞错改 `console.debug` 留痕。
- `scripts/check_ui_rules.mjs` — 新增 R5：源码禁出现 `'/api/api/` 双前缀字面量，防同类死链回潮。
- 验证：`node scripts/check_ui_rules.mjs` → UI-RULES OK；`pnpm typecheck`(tsc -b) 0 错。
- [branch fix/audit-p0-0908, `git show HEAD`]

### fix-UI门禁脚本Windows路径兼容(URL.pathname中文编码/盘符双写/反斜杠致R4豁免失效)
- `scripts/check_ui_rules.mjs` — ROOT 改 `fileURLToPath(new URL(...))`（原 `.pathname` 在 Windows + 中文路径下产生 `E:\E:\user\%E6%96%87...` 直接 ENOENT）；R4 stock-colors 豁免匹配前路径分隔符归一化（Windows `join` 反斜杠致 `/lib\/stock-colors\.ts$/` 永不命中，误报 7 条）。
- 验证：Windows 本地 `node scripts/check_ui_rules.mjs` → UI-RULES OK。
- [branch fix/audit-p0-0908, `git show HEAD~1`]

### fix-PG唯一生产口径(容器无连接串fail-fast/SQLite仅本地/compose默认接PG/布尔迁移按方言)
- `src/web/database.py` — DOCKER=1 无 SIDA_DB_URL 直接 RuntimeError(历史教训:env丢失静默落容器内sqlite→database is locked+重建丢数据);本地(DOCKER未设)默认仍是 data/panwatch.db。
- `src/web/database.py::_migrate_remove_stock_enabled` — `enabled` 布尔字面量按方言(FALSE/TRUE vs 0/1,PG上`=0`直接operator崩);PRAGMA重建分支仅SQLite可进(PG DROP COLUMN必成功,失败直接raise)。
- `docker-compose.yml` — SIDA_DB_URL 默认解开(指 panwatch-postgres);panwatch 加 depends_on postgres healthy;PG 注释"可选/默认SQLite"全部改"默认生产口径"。
- 新增`tests/test_pg_default.py` — 3用例(fail-fast/带串PG/本地sqlite),3 passed。
- **回归**: P1/P2/P4共15 passed + syslog/source/decision/accuracy共11 passed + app import OK + compose YAML合法。
- **未做**: forecast 容器的 PANWATCH_DB 仍指 sqlite 文件(PG 下该文件不存在,预测引擎读设置页走 HTTP 不走文件,暂不动,另开);生产小主机已有 SIDA_DB_URL,不受 fail-fast 影响,无需重启。
- [branch feat/pg-default-0908, `git show HEAD`]

### chore-双树 verdict 落定(TQ 件已在主树, 切 venv 指向)
- 核查结论:所谓"待合三件套"(formula引擎/_TQ_URL/dark_l2/.tck)早已在 `sida-pro` 主树且是超集;sida-src 是 08-31  snapshot 的 TQ 试验田,缺 09-02 以来全部线上修复(WAF/volume×100/陈旧门禁/限流/BJ前缀)。无代码可合,不碰 sida-src(28 号试验田, 另行归档)。
- 环境修复:本地 venv 的 marketdata editable 重装指向 `sida-pro/packages`(之前指 sida-src 旧拷贝,本地包测试测的不是发版代码, volume 差 100 倍)。验证:包+宿主 9 passed, 无需 PYTHONPATH。
- [branch feat/tq-merge-0908, `git show HEAD`]

### feature-系统日志闭环(scheduler监听/前端上报/errors接口/JSONL轮转)
- `src/core/error_tracker.py` — JSONL超2000行丢最老一半(之前无界增长);新增`install_scheduler_error_tracking()`(APScheduler EVENT_JOB_ERROR/MISSED→capture_exception,一个监听盖住该实例所有任务)。
- 三处`start()`接入:context/kline_backfill/report scheduler(失败不阻断启动)。
- `src/web/api/logs.py` — `GET /errors`(owner, recent_errors终于有接口);`POST /frontend`(登录用户,字段截断,进统一去重+聚合告警,user记context)。
- 前端 — 新增`src/lib/error-report.ts`(同message 60s去重,fetch+keepalive,未登录丢弃);`main.tsx`装window.onerror/unhandledrejection;`App.tsx`给ErrorBoundary接onError上报。
- 新增`tests/test_syslog.py` — 4用例(job异常/missed/前端上报/轮转),4 passed。
- **回归**: tsc 0错; UI门禁OK; P1/P2/P4共15 passed。
- **未做**: errors的UI页(接口先行,投研/系统页接展示留待);Loki/promtail不变。
- [branch feat/syslog-0908, `git show HEAD`]

### feature-来源透传+vendor质量分(方向1: 存活检查→质量记分)
- 包内: `Quote`加`source/latency_ms`字段;`client.quotes()`把胜出vendor打到每条Quote(之前Response带但到Quote就丢了)。
- 宿主:`md_quote_rows`→`_quote_to_response`透传`source/source_latency_ms`(空=未知源,不编造;空quote分支同形)。
- 新增`GET /api/datasources/trust`:Engine近100次/源滚动窗口→score(成功率−延迟档,未调用过null不冒充,高分在前)。
- 测试:包内`test_quotes_stamps_winning_vendor`(PYTHONPATH指正树跑,4 passed);宿主2 passed。
- **大发现(未修,需定)**:marketdata双树分叉 — 本地venv editable装的是`sida-src`旧拷贝,Docker按requirements装`sida-pro`包内拷贝,5文件已分歧(client/http/types/kline/tq)。本地包测试测的不是发版代码,改哪棵、删哪棵你定。
- **未做**:前端来源徽标(接口先行);与PG收盘价偏离记分(第二阶段)。
- [branch feat/source-trust-0908, `git show HEAD`]
### feature-分Agent命中榜(方向3上半场: 先看见谁准)
- `src/web/api/profile.py` — 旧全局命中率收敛为`_accuracy_board()`的overall(口径不变,`/stats`只多`avg_return_pct`,旧前端不崩);新增`GET /stats/accuracy?days=30&min_n=5`(分agent命中/平均收益/qualified小样本保护,高分在前)。
- 新增`tests/test_accuracy_board.py` — 2用例(分agent切分+中性/窗外排除+小样本不参评+旧形保持),2 passed。
- **未做**:下半场因子自动降权(需定权重策略,另开);UI榜单页(接口先行)。
- [branch feat/prediction-grading-0908, `git show HEAD`]
### feature-决策合成(方向2: 三信号→动手/看看/别碰+一行理由)
- 新增`src/core/decision.py` — `synthesize()`纯函数(复用gs/ai_activity/dark_pool/resonance,不重写算法)+`decide()`IO入口(PG/K线→三信号,失败永不抛只看看)。
- 新增`GET /api/decision/{symbol}`(protected,app.py挂载) — verdict映射(向好动手/拐点分歧看看/走坏别碰,缺数表外一律看看+理由)。
- 新增`tests/test_decision.py` — 3用例(verdict域/缺数看看/端点行为),3 passed。
- **未做**:前端决策卡片(接口先行);与命中榜联动(方向3数据回灌,另开)。
- [branch feat/decision-synthesis-0908, `git show HEAD`]

## 2026-09-07

### feature-P4可观测修死告警(指标对齐/PG告警/磁盘门禁/演练)
- **死规则实锤**:`deploy/prometheus-rules.yml`三条数据告警引用的指标名全不存在(`request_count_total`实为`sida_http_requests_total`、`datasource_failures_total`实为`sida_datasource_failures_total`、`sida_health_redis_status`从未emit),上线以来一条没响过;无relabel可救。
- `src/web/api/health.py` — 新增`sida_health_component_status{component}` gauge + `record_component_status()`,/health的DB/Redis检查每次刷新(redis disabled按预期降级记1不告警)。
- 规则文件 — 三条expr对齐真实指标名;新增`SidaPostgresDown`(critical, database==0/2m);`SidaRedisDown`改走新gauge;文件头写死双向锁定(改名必改规则+跑测试)。
- `build.sh` — /分区≥85%中断发版;build后清dangling+本仓库旧tag(不碰运行容器/数据卷);末尾打印磁盘占用。
- `scripts/backup_pg.sh` — 追加恢复演练四步(演练库pg_restore+三表行数对账+删库+异地份)。
- 新增`tests/test_p4_alerts.py` — 3用例(规则指标全emit/四指标存在/gauge真写值),3 passed。
- **回归**: P1+P2+audit共15 passed; app import OK; rules YAML合法; build.sh语法OK。
- **未做**: Gateway CLOSE-WAIT自愈(hermes-gateway是另一个仓库,不在本分支动);Hub抽独立进程(见P2未做)。
- [branch feat/mature-baseline-0907, `git show HEAD`]

### feature-P3前端收敛(toAmount归一/CRLF清零/UI门禁/envelope客户端)
- `frontend/src/lib/format.ts` — 新增`toAmount`(元→万/亿)+`toAmountFromWan`(万元口径)+`toWan`别名;Quote/L2/DarkFundTop三处手抄删除改import。实测已分叉:Quote旧版缺isFinite守卫(脏数渲染NaN万),DarkFundTop空值符'-'与全站'--'不一致,本次一并收敛。
- 换行:8文件CRLF→LF(P0清单)+漏网`useSourceHealth.ts`,新增`.gitattributes`锁`eol=lf`,防Windows检出回潮。
- 新增`scripts/check_ui_rules.mjs`(零依赖,CI可直接`node`跑) — R1图层禁卡片/R2禁手抄toAmount/R3禁CRLF/R4禁GS色硬编码,首跑11违规已定级:R1两Card是分时图下方堆叠面板(白名单留档,挪位置重审)、R4拆股marker绿是事件色板(按`拆${`豁免)、stock-colors令牌定义豁免。现`UI-RULES OK`。
- 新增`frontend/src/realtime/envelope.ts` — P2信封的客户端:parseFrame(新envelope/旧裸帧兼容)/reconnectUrl拼last_seq/maxSeq取最大seq。WS接线页改留待(当前前端无WS消费,全轮询)。
- **验过**: `tsc -b` 0错; `vite build` EXIT 0(15s, 仅chunk-size旧警告); 门禁OK。
- **未做**: KlineChart/InteractiveKline大重构(2001行,风险高,留待终端化专项); orval全量codegen(等后端契约稳定)。
- [branch feat/mature-baseline-0907, `git show HEAD`]

### feature-P2实时envelope+K线陈旧failover(ring重放/12天线/asof)
- 新增`src/web/realtime/envelope.py` — 统一下行帧`{seq,ts,topic,user_id,payload}`,seq走Redis INCR `biz:ws:seq`(无Redis退进程内),本进程deque(200)供`?last_seq=`断线重放。quotes广播包`quote.tick`、快照包`quote.snapshot`、通知广播包`notif.push`;两handler在accept/hello后按`last_seq(+user_id过滤)`补发missed帧。PubSub通道原文格式不变,消费侧漏斗到发送点只包一次。
- `src/web/api/klines.py` — `_pg_klines`改返`(bars, asof)`:最新bar超12天(覆盖春节级长假)视为陈旧网关快照→`(None,None)`回落联网,不再静默服务旧数;单股/batch响应加`asof`字段。另两处调用方已同步。
- 新增`tests/test_p2_realtime.py` — 5用例(seq单调/重放过滤/新鲜返asof/陈旧穿透/过薄穿透),5 passed。
- **回归**: P1双轨7 + audit回归共10 passed; app/quote_stream/ws_hub/envelope import OK。
- **未做**: Hub抽独立进程(部署拓扑变更,留P4随自愈一起做;envelope/seq/ring已把代码前置条件铺好);前端WS消费envelope解析(留P3);顺带发现ws_hub PubSub自回显疑似循环,未动,需单开issue验证。
- [branch feat/mature-baseline-0907, `git show HEAD`]

### feature-P1认证双轨+契约快照(服务token只读行情口/291 paths冻结)
- `src/web/api/auth.py` — 新增服务token双轨: `get_service_token()`(env SIDA_SERVICE_TOKEN优先,否则AppSettings自动生成持久化,同jwt_secret模式) + `get_user_or_service()`(先试Bearer用户JWT,再试X-Service-Token) + `ServicePrincipal`。写链路不动,服务token进require_owner永远403。
- `src/web/app.py` — quotes/klines挂载从`protected`切`data_read`(双轨),其余66模块保持用户JWT。终结监控/回填拿服务token调行情口401。
- 新增`scripts/export_openapi.py` — 进程内导出`docs/_frozen/openapi.p1.json`(291 paths),P3 orval codegen命令已写进脚本头注释。
- 新增`tests/test_p1_service_token.py` — 7用例:无凭证401/服务token放行/错token401/写口拒服务token/owner口拒服务token/用户JWT行为不变,7 passed。
- **回归**: test_audit_p1_regression + test_ambush_events_input共6 passed; `import src.web.app` OK。
- **未做**: Alembic(已有自研versioned migrations,不重复造轮子); audit独立Session(08-21已修,有回归测试); 全量orval迁移(留P3随前端终端化一起做)。
- [branch feat/mature-baseline-0907, `git show HEAD`]

### doc-P0成熟化基线冻结(路由15组/68API模块/9Agent/PG50表)
- 新增`docs/_frozen/routes.md` — 15路由组: /驾驶舱/机会/暗盘/行情forecast+quote别名+L2/指数/板块/持仓portfolio/研报详情/system+reports+shadow+notifications+settings五Hub/profile/login。CRLF 8文件记入P3修。
- 新增`docs/_frozen/ai-tools.md` — 9 Agents + 68 API模块计数(paper_trading16/recommendations20为核心, 单路由模块列P1合并候选, ws_*抽Hub)。
- 新增`docs/_frozen/data.md` — PG50表 + klines hypertable三源幂等 + Redis biz:TTL规范 + 生产铁律(network-alias postgres)。
- **测了**: 基线只读统计未改业务, `git status`仅新增3文件。P1从此分支起。
- [branch feat/mature-baseline-0907, `git show HEAD`]

### fix-盘前埋伏空榜+报告落盘失败(09-07 早盘实测: 埋伏榜 0 条)

- `src/core/catalyst_screener.py`: 新增 events_to_calendar() — 事件流 subjects 经
  受益解析落代码(低 confidence 丢掉), 转日历项; 同 symbol 留日期最近一条。
  此前漏斗只吃本地日历, 无 symbol 的宏观项多时直接空榜。
- `src/agents/premarket_outlook.py`: 6.7 合并 `event_cal + catalyst_local`(事件优先,
  去重时事件项在前), 日志加 日历/事件计数。事件流 37 条不再被丢弃。
- `src/web/api/reports.py`: 报告根目录不可写(/hermes 未挂载)时退到
  DATA_DIR/cron/output(容器持久卷); 两处都不可写时保持原行为不崩 import。
  修 08:30 盘前报告 `Permission denied: '/hermes'` 落盘失败。
- **测试**: tests/test_ambush_events_input.py +3, ambush 相关共 20 passed。
- **未修**: wudao 连续 401 是生产没配 key(DB/env 双空, 非过期), 需补 key
  (设置页 app_settings.wudao_mcp_token, 改完立即生效无需重建)。
  [commit b847945]

## 2026-09-06

### feature-妖股因子接入盘前埋伏(批次C×B 集成, 老板指令)

- `src/core/demon_factors.py`: top_demon_factors(n) 先锋组因子映射 + DEMON_BOOST_BY_GRADE
  (极妖+3/妖+2/活跃+1)。
- `src/core/ambush_score.py`: demon_boost() — 埋伏候选落代码命中先锋组按等级加成;
  **退潮/高潮期反向禁推**(demon_veto, 妖股退潮期跌最狠); enrich_ambush_list 签名扩展
  demon_factors 参数, 输出 demon_hit 标记, action 新增"禁推(先锋组退潮期)"。
- `src/agents/premarket_outlook.py`: collect 换 top_demon_factors(30) 因子映射;
  build_prompt 新增"妖股先锋组"段(股性分/等级/封板次数/连板/参与方式 + 情绪禁推提示),
  盘前简报含先锋组名单; 简报推送不变。
- **测试**: test_ambush_score +14(demon_boost/端到端) 共 26 passed。

### feature-妖股因子存档+增量管线(批次B 存档化, 老板要求: 不从头重算/新日期走增量)

- `src/web/migrations.py`: Migration 134 demon_factors(每股最新六维因子快照,
  unique(symbol), total 索引; 原始 limit_up_events 是存档层, 因子可全量重算)。
- `src/core/demon_factors.py`: 因子层+回填双通道 —
  * backfill_direct_tq: TQ 直连批量回填(本机网关 ~30ms/股, 4 线程; 存档重建用)。
    Engine 多源链是盘中兜底用的, 批量陪长尾失败链熬没有意义(实测周日每股 10-15s);
  * backfill_incremental: 每日增量——只拉近 15 根 K 线, 只补库内没有的新日期,
    只对当日有新事件的股票重算因子(增量后每天分钟级);
  * recompute_factors / load_factor_pool / update_pipeline(15:35 cron, 替换原 15:30 回填)。
- `src/web/api/demon_pool.py`: GET /api/demon-pool 直读因子表(空表回退现算);
  POST /backfill 支持 mode=incremental(默认)/full。
- `src/core/demon_factors.py` 内嵌于管线; server.py cron 切换 update_pipeline。
- **实测发现并修复**: TQ 新鲜度门禁周末误杀(floor today-1→today-3, tq.py;
  周日跑批周五数据被拒→TQ 主链全灭+长尾备选源全挂每股 10-15s→首次回填半途停滞)。
- **测试**: test_demon_factors roundtrip 2 项 + demon_score 10 项 passed。

### feature-前端徽章UI pass(批次A3+E3 MVP, feat/ui-badges)

- `src/web/api/klines.py`: /klines/{symbol}/summary 挂 mainflow_tri+A/B/C 置信度载荷
  (随 summary 双层缓存, 不增加请求成本)。
- `frontend/packages/api/src/insight.ts`: sealQuality(symbol) API 方法。
- `frontend/src/pages/Quote.tsx`:
  * 主力意图块加"置信 A/B/C"徽章(A=主色/B=单源灰/C=分歧琥珀, title 带三源明细;
    非 A/B/C 无徽章不占位)。
  * 新增"封单成色"窄栏块(仅 metrics.available 渲染): 成色(异常琥珀色)/撤单率/
    撤单异动 z/封板成功率 + "未封住"提示; 无数据不占位不编造。
- **范围说明**: E3 新鲜度徽章 MVP 先覆盖封单成色块(盘中60s采样标注); 全站新鲜度/
  E2 WS 通道统一/G2 锥图分层/G3 ⌘K 留下一轮 UI pass。
- **测试**: pnpm typecheck(tsc -b) 全绿; 后端回归 25 passed。

### feature-L2事件流推送+预警(批次F核心, feat/alert-rules)

- `src/core/l2_event_stream.py`: 两类独家粒度事件 → 全局渠道推送(同股同日同类一次,
  复用 darkflow_alerts 节流+渠道模式) —
  ①dark_cluster: 暗盘净流入聚簇 ≥100万元(逐笔拆单识别口径, 金额=元红线);
  ②seal_anomaly: 涨停封单成色异常(撤单率 z≥2 或 成色<0.6 且封住; 炸板后不推)。
  eval_tick() 由 seal_sampler 60s 盘中任务顺带调用, 失败静默不拖垮采样。
- `src/core/seal_sampler.py`: sample_tick 接入 L2 事件流评估。
- **范围说明(诚实)**: F1 价格类用户级预警已存在(PriceAlertScheduler+price_alerts API),
  不重复造; F3 大盘温度页前端部分并入 UI pass; G1 CI 门禁 v0.5.8 已落地(gates job +
  GITHUB_REF_TYPE 判定), 本批未动 CI。
- **测试**: test_l2_event_stream 6 项 passed。

### feature-信号→复盘闭环(批次D, feat/signal-review)

- `src/web/migrations.py`: Migration 133 signal_snapshots(emit_ts/emit_date/signal_type/symbol/
  direction/strength/payload + outcome_t1/t5 + checked 标记; (type,symbol,emit_date) 唯一首条口径)。
- `src/core/signal_review.py`: record_signal(信号 emit 快照, 幂等)/nightly_review(每晚对账,
  T+h=事件日后第 h 个**交易日**收盘, 日K走 Engine 主备链; 事件日无 K 线显式 checked+None 不编造)/
  hit_rate(按类型聚合 T+1/T+5 胜率+均值, direction=short 反号归一; resonance 类对照官方
  75.42%/3.45 基准并注明口径差异, n≥20 才显示)。
- `src/web/api/signals_review.py` + app.py 注册 /api/signals(protected): GET /hit-rate、POST /review。
- server.py lifespan: 信号对账 cron(交易日 18:30)。
- `src/agents/premarket_outlook.py`: 埋伏候选 emit 时快照落库(禁推候选不入库)。
- **测试**: test_signal_review 4 项(交易日顺延/幂等/方向归一/基准) passed; 全部新功能测试 50 passed。

### feature-盘前埋伏Agent补全(批次C, feat/ambush-mvp)

- `src/core/mood_cycle.py`: C1 情绪周期→题材容许度映射(不重复造轮子, 直接复用
  market_phase 七阶段判定: 冰点5/启动10/主升8/高潮0/退潮1/修复4/积累3;
  current_mood() 读 market_phase_daily 最新行, veto=高潮硬否决, demon_veto=退潮+高潮)。
- `src/core/ambush_score.py`: C2 四维评分合成 — 事件(预期差+临近度, 解禁类不加分)×
  传导(高置信落代码面+妖股先锋组+2封顶)×情绪(容许度直通, 缺数据中性5)×信号
  (TQ zjl_hb 主力净额, 缺数据三维归一+flags 不编造); 风险日历 15 日内解禁扣 3;
  证伪条件规则生成(推演与实测显式分离, 每候选必带); 高潮期 action=禁推。
- `src/agents/premarket_outlook.py`: C2/C3/C4 接入 — collect 新增 6.8 四维评分
  (mood+先锋组∩埋伏榜)+6.9 期货价格证据; build_prompt 情绪定位段+四维埋伏榜
  (分项/flags/证伪条件进 LLM prompt); analyze 末尾盘前简报推送 notify_center
  (asyncio.to_thread, 失败静默)。
- `src/core/commodity_quotes.py`: C5 期货主力连续(新浪公开接口, 无账户) — SC/CU/AL/RB/
  M/CF/C/AU/AG; 最新价[7]/昨结算[9] 社区口径**未实测**, 解析失败整源 available=False
  降级, 绝不用可疑字段编造轮动信号; momentum_score 动量纯函数。
- `src/core/commodity_rotation.py`: C5 升级 detect_rotation_stage(events, price_evidence) —
  期货当日涨跌(≥0.8%) 权重×2 合入幕判定; **黄金拆出并行风险温度计**(金股同涨期
  不占轮动幕位, 2.9 修正); 接口失败回落纯事件版(降级路径保留)。
- `src/web/api/demon_pool.py`: top_demon_symbols(n) 先锋组查询辅助。
- **诚实项**: 新浪 nf_ 字段口径周一盘中实测校准; 20/60 日动量走 InnerFuturesNewService
  日K(同样待实测); 先锋组加成依赖 limit_up_events 回填完成度。
- **测试**: test_ambush_score 12 项 + 盘前/催化/埋伏既有 24 项 = 36 passed(无回归)。

### feature-妖股池/股性雷达(批次B, feat/demon-pool)

- `src/web/migrations.py`: Migration 132 limit_up_events(涨停事件表, symbol+trade_date 唯一,
  touched/sealed/one_way 判定, open_count 留空待逐笔)。
- `src/core/limit_up_backfill.py`: 每日盘后回填(交易日 15:30 cron 已挂 lifespan) — 全市场
  ~5562 只日K(Engine 主备链路) → limit_rules 涨停价判定 → 涨停事件入库, 重跑幂等;
  backfill_all/backfill_symbol/get_events_window。
- `src/core/demon_score.py`: 六维评分纯函数 — freq30(封板次数分档 ≥20 极妖/10-19 妖/5-9 活跃)
  /lianban20(最高连板+反复激活加成, 跨周末≤5天近似连续)/seal15(封板成功率)/theme15(wencai
  未接入→flags 标缺数据)/lhb10(同)/stamina10(近60日触及); 调整项: 市值带 20-120亿 +5、
  一字板"不可参与"。MVP 满分 75+5, 全权重制缺维计 0 并标注, 不编造。
- `src/web/api/demon_pool.py` + app.py 注册 /api/demon-pool(protected): TopN 池(300s 缓存,
  min_events=3 起评)/单股明细/手动回填。
- `scripts/backtest_demon_leading.py`: 妖股领先效应回测 — 事件日代理=全市场涨停家数>1.5×
  前5日均值(板块口径待传导链), 妖股组 vs 其余涨停股 T+1/T+5 均值/胜率/超额; 待数据回填后
  跑真值校准权重, 结果入 docs。
- **诚实项**: 六维权重为初版经验值; open_count/封单额历史留空; T+1 一字板买入偏差未剔除。
- **测试**: test_demon_score 10 项 + 批次A 28 项 + 审计回归共 34 passed; py_compile 全过。

### feature-封单成色检测器+双源置信度(批次A, feat/seal-quality)

- `src/core/limit_rules.py`: A股涨停价规则纯函数(主板10%/创科20%/ST5%/北交30%, 四舍五入到分,
  拒绝银行家舍入; ST 由名称判断, 新股首日 prev_close 缺失 → None 显式无数据)。
- `src/core/seal_quality.py`: 封单成色纯函数 — cancel_rate_5m=Δ撤/(Δ买+Δ卖+Δ撤)(口径对齐
  decision_pioneer P2 撤单率, 但做在窗口差分上)、seal_quality=1-cancel_rate、撤单方向 bias
  (买撤多>0 托单虚)、撤单率 z-score(基线=窗口起点前历史对, 不被暴增自身污染)、封板成功率。
  数据不足/累计字段回退(跨日重置) → available=false+reason, 绝不编造。
- `src/core/seal_sampler.py`: 盘中 60s 采样任务 — 涨停池(get_limit_up_pool, wudao 带 name 判
  ST+封单额) → fetch_tq_l2 累计字段(BCancel/SCancel, TQ9 实测当日累计→差分口径) + TQ 快照
  Now/LastClose → seal_quality_samples 表(symbol+ts 唯一, 先查后插幂等)。
- `packages/marketdata .../tq.py`: 新增公开 `tq_rpc()` 入口(get_stock_info/get_zdt_data 等
  未封装方法复用, 免 import 私有 _rpc)。
- `src/web/migrations.py`: Migration 131 seal_quality_samples(ts ISO 文本, 双方言安全)。
- `src/web/api/seal_quality.py` + app.py 注册 /api/seal-quality(protected): /{symbol} 指标、
  /{symbol}/raw 原始序列、/sample-now 手动采样。
- `src/web/api/darkflow.py`: dark-flow 响应的 mainflow_tri 挂 A/B/C 置信度徽章
  (`src/core/confidence.py`: 双源一致=A/单源=B/分歧=C, 永不抛异常)。
- **诚实项**: FCAmo 封单额字段语义、BCancel/SCancel 差分口径, 周一盘中实测校准
  (docs/innov-dev-plan.md 风险清单); 前端徽章与 E3 新鲜度徽章合并一次 UI pass。
- **测试**: test_seal_quality(12)+test_confidence(6) 18 passed; 审计回归+埋伏相关 9 passed;
  TestClient 冒烟: 3 端点 401(protected) vs 未注册 404。

## 2026-09-05

### fix-28号审计全量修复(v0.5.8, P0在v0.5.7)

- P1: 限流TTL误锁/自选跨用户写/forecast阻塞/通知task丢失/渠道接口404/
  设置缓存30s/掩码回写毁凭证+后端兜底/WS无限重连/切股竞态×4/买绿卖红×2/
  单例凭据冻结/dark_l2无视设置页凭证/资金流单位差100倍/部署三链路对齐/密钥清仓。
- P2: 设置白名单/JWT角色DB优先/审计to_thread/微信worker取消/klines复用engine+
  batch同口径/LRU上限×3/日志后台刷/调度to_thread/owner可设guest/Quote无数据+
  DECIMAL防崩/机会守卫/亿股/Login8位/SectionHeader收尾/明暗盘列名/TQ920前缀+
  网关重探+非dict跳过/补数单链+CST日期/节流锁外睡/重试熔断/P2-22已修复确认/
  缓存上限/注释修正/config键名/热补丁清单/CI门禁+tag判定。
- P2-22(主买主卖diff)核过现代码已全量先差分, 未改动。
- **测试**: 95 passed(审计回归4+相关91) + tsc/build 全绿。

### hotfix-SPA路径穿越(v0.5.7)

- P0-1(28号审计): `server.py serve_spa` 加 realpath 越界校验, 越界 404。
- **测试**: 穿越用例拦截验证 + py_compile。

### feature-埋伏雷达进盘前(v0.5.6)

- CKPT1 未来催化日历(`catalyst_calendar`: 解禁分层降级+静态宏观窗口+30天API位) →
  盘前 6.6 步 + prompt 日历段。
- CKPT2 行情快照进 prompt(`_fetch_market_snapshot` via md_quote_rows;
  无快照时预期差禁断言"尚未反应")。
- CKPT3 埋伏漏斗(`catalyst_screener`: 规则打分→Top8 LLM→排序) → 盘前 6.7 步。
- CKPT4 受益落代码(`beneficiary_resolver`: exact/sector/fuzzy, 低置信不进榜)。
- prompt 新增埋伏榜段(观察池第一输入)。
- **测试**: 33 passed(日历3+落地/prompt3+漏斗3+引擎/盘前回归)。

### feature-同花顺账号密码设置页自助维护(v0.5.5)

- 新增 `ths_username`/`ths_sdk_password` 设置键(DB 优先于 env, 30s 生效,
  密码掩码); `THSDKL2` 经 `resolve_ths_creds()` 读取, 显式参数仍最高。
- `/ths/account` 模式判定同口径 + 返回 source(db/env); 卡片显示当前凭证来源。
- fix: 设置项描述以代码为准(DB 旧描述不再盖住代码新文案)。
- **测试**: 新增 test_ths_creds 5 passed + tsc/build 全绿。

### remove-同花顺扫码登录下线(v0.5.4)

- 删 `src/core/ths_auth.py` + `tests/test_ths_auth.py` +
  `/ths/qrcode*` `/ths/session` `/ths/logout`(扫码 session 无数据链路消费)。
- `/ths/account` 仅留 SDK 模式 + 能力一览; 卡片去扫码/登出/登录态展示。
- **测试**: tsc/build 全绿 + 相关 pytest。

### feature-同花顺账号维护模块(v0.5.3)

- 后端: `POST /ths/logout`(清凭证, docstring 早写了但没实现) +
  `GET /ths/account`(SDK 正式/游客模式 + 扫码态 + 已验证能力一览)。
- 前端: biz-ui `ThsAccountCard` 自包含卡片(模式徽标/扫码/登出/能力勾选),
  设置页 sec-ths 旧逻辑整体替换(删 60+ 行)。
- **测试**: pytest 5 passed + tsc/build 全绿。

### feature-UI标题统一(v0.5.2): SectionHeader 全站收敛

- 机会精选/最新报告/机会发现/情绪周期/市场主线×2 全部迁 SectionHeader。
- 全站分区标题只剩一种样式(色条+13px semibold+右侧action)。
- **测试**: tsc/build 全绿。

### feature-UI全量改造 Wave2+3(v0.5.1): 全站进场动画(22页)

- Quote/Opportunities/DarkFundTop/IndexDetail/L2Orderbook/BoardDetail/Help/
  Audit/History/Reports/PaperTrading/Forecast/Notifications/ShadowAccount/
  PriceAlerts/Stocks/Profile/Agents/DataSources/Settings/AnalysisDetail
  根容器统一加 `sida-page-enter`(0.3s fade-up, reduced-motion 全禁)。
- Hub 系/登录页为框架壳, 不加。
- **测试**: tsc/build 全绿。

### feature-UI全量改造 Wave1(v0.5.0): primitives + 首页迁移

- 新增 biz-ui 共享组件: AnimatedNumber(数字滚动)/FlashValue(涨跌闪)/
  SectionHeader(hairline分区标题)/Stat(三档字体锁死)。
- index.css: sida-flash-up/down + sida-page-enter(0.3s) + reduced-motion 全禁。
- Dashboard: 指数点位滚动+涨跌闪、3 处标题迁 SectionHeader、整页进场动画。
- KpiBand 已有 count-up, 不动。
- **测试**: tsc/build 全绿, 自截验收通过。

### feature-双L2深研落地 v0.4.98(全线上实测)

- DDE 定稿: 装机 thsdk 无 `dde` 方法, 但 `query_data` 官方通道通
  (主动/被动×特大/大单8列+主力净流入, 生产已验)。`get_dde` 改走官方,
  异常回退 big_orders 四档。两条 `/dde` 路由本就调官方通道, 不用改。
- `get_hs300_constituents`: `hs300` 方法同样不存在, 改走 TQ
  `get_stock_list(market=23)` 回退, 空表兜底。
- 性能: thsdk 每查一次 TCP 登录一次, `get_comprehensive_snapshot` 加 30s
  进程缓存(一次快照 6 次登录→1 次)。
- `dark_l2.py` 注释"游客模式"扶正为正式账户。
- 纠正: `get_market_data_cn_extended`(扩展1)正式可用, 未坏, 不动。
- **测试**: 新增 8 单测全绿 (四档分桶/官方透传/回退/TQ 成功失败/快照缓存)。
- 待用户: 盘后专业包(GP/SC)仍 ErrorId=10, 真封板率改走 wudao 备用。

### fix-首页UI第二轮(截图验收)

- 市场温度字表重叠：ECharts gauge detail 富文本 `\n` 不换行
  （渲染成 `60n市场温度·修复` 挤出一行掉到表外），改 HTML 叠加层两行渲染，
  定位圆心之下（指针扫不到），容器 140→154px。
- 封板率100%去而复返：库里周五旧行 seal_rate=1.0（周末无 sync 覆盖），
  读接口 `_row_to_dict` 把 legacy 1.0 转 None，前端直接显“--”。
- 流出榜“+41.7亿”：两处渲染都是原符号打印，后端只收负数，
  系截图模型误读；周末板块资金为空是正常的（收盘后无数据）。
- **测试**: pytest 46 passed(含新增 legacy 清洗回归测试) + tsc 全绿。

### UI-图表质感(交易所大屏风)

- 涨跌分布：单向横条 → 双向镜像柱（左绿右红/中央0轴/渐变+圆角）。
- 市场温度表：进度弧渐变覆盖+指针阴影。
- 资金流榜：纯色底 → 左实右虚渐变“水位感”+金额等宽。
- 指数迷你线：线宽1.5→2+尾端点光晕。
- **测试**: tsc/build 全绿（效果待截图验收）。

### fix-首页三修(截图验收)

- 涨跌分布永久空白：useECharts init只在mount跑，但图表容器数据到了才挂载，
  chart永为null。ref改callback ref（挂载瞬间init），4个用量全受益。
- ST豆神重复两条：东财同股同规则下多条（days 9/10），按(symbol, rule_code)
  只留days最大。
- 封板率100%误导：见v0.4.95（待部署验证）。
- **测试**: tsc/build 全绿。

### fix-首页数据口径

- 大盘资金流：上游网关 sh/sz/cyb 的 point/change_pct 放大100倍
  （393012/-30），网关层归一化/100。前端未展示但API口径已正。
- 封板率：数据源只有已封板池、无炸板分母，硬编码1.0误导成"100%封板"，
  改落None（前端显"--"）；市场温度说明加"缺项按0"。
- **测试**: tsc 全绿；breadth/mainline/phase/指数/资金流生产接口逐个验数自洽。

### fix-K线null防崩

- 问题：forecast页`Value is null`全页崩——klines含null OHLC行（周末/预测拼接），
  lightweight-charts setData遇null直接抛。
- KlineChart主series提前过滤非有限OHLC行（均线/副图数据源同步）；空数据只空图不崩。
- **测试**: tsc/build 全绿。

### fix-周末复盘放行

- 问题：周六/盘后 dark-flow 502无数据——未来过滤把跨日tick杀光。
- `_drop_future_ticks`加now注入：非工作日交易时段（周末/09:25前/15:05后）
  不过滤，看最近完整交易日（trade_date标注）；盘中仍严格丢未来。
- **测试**: 3个新用例过（周六/盘后放行+盘中仍丢）；另5个真实链路挂系周六环境（同前）。

### P2-撤单率维度(TQ独家)

- `_l2_summary`加撤单率/偏向/信号：撤单率=(撤买+撤卖)/(买卖+撤单)，
  ≥80%假挂单多；偏向±20%线（买方撤多托单虚/卖方撤多压单虚）。阈值初版实盘再调。
- 前端主力意图卡加 violet 撤单行（有信号才展示，hover看明细）。
- 神剑实测：撤单率84%→"撤单频繁"，偏向-16.2%（未达线）。
- **测试**: 纯函数实测（None/缺字段不抛）+ tsc/build 全绿。

### P1-明盘三源交叉验证

- 新模块`mainflow_tri`: 腾讯四档(元→万元)+thSdk DDE官方(万元)+TQ Zjl_HB(万元)，
  并发8s兜底，n≥2同号且离散≤50%判一致，否则分歧标记（不断主链路）。
- darkflow API加`mainflow_tri`字段；主力意图卡加一行一致绿/分歧amber（hover看三源明细）。
- 附带发现：腾讯单位是元（3498万），thSdk是万元（3795万），方向一致差8%。
- **测试**: test_mainflow_tri 6过；test_dark_flow合跑5挂系周六环境（stash原代码同挂5个，未来过滤杀光跨日tick），与改动无关。
- P2状态：撤单率待TQ网关恢复（小主机WSL未起TdxW）；5日趋势数已随tri回传（net_5d_wan），选股功能另排。

## 2026-09-04

### v6-平盘误标修正(涨停股破局)

- 根因(P0d实锤): 涨停股98%成交价持平, 腾讯chg=0几乎全标S
  (龙版2147:3/亚盛3040:30) → 卖出端爆仓方向反。
- `_neutralize_flat_mislabel`: 平盘笔S占比>80% → 平盘S转M;
  非涨停(平盘B/S均衡)原样保留。M聚类跳过不断簇。
- 解析保留chg字段(向后兼容, 老tick缺省非零)。
- 5只验证: 龙版0.76/我爱我家0.86/金螳螂1.01/亚盛1.51/利欧0.82,
  **方向5/5全对**(此前涨停3/3反)。
- **测试**: pytest 25过(含4个新用例)。

### C计划对账定论(v5回滚+涨停降权)

- 5只同花顺暗盘榜对账: 金螳螂1.01/利欧0.76(非涨停全对);
  龙版-1.68/我爱我家-1.53/亚盛-0.47(涨停全反)。
- v5实验(hi=100万单笔上限)失败回滚: 涨停方向没回来, 反把准的两只砍塌——
  同花顺暗盘本就含均笔几百万大簇; 去开盘15分钟同样误杀真信号。
- 定论: L1逐笔无委托号, 涨停日散户潮与主力拆单不可区分, 属数据源天花板, 停止调参。
  前端涨停(≥9.5%)暗盘卡加"散户潮污染方向仅供参考"降权提示。
- **测试**: pytest 21过; tsc 全绿。

### fix-暗盘金额兜底

- 问题: 5笔3629万买入簇被标"散户追涨" — reason 只看位置+方向, 不看金额。
- `_classify_split`: 簇总额>=500万直接判主力(获利区买=主力买入,
  套牢区卖=主力卖出); `_detect_split_orders` 新增全簇主力/散户分项
  (main_buy/main_sell/herd_buy/herd_sell, 暗盘净额口径不变)。
- 前端疑似主力买/卖改取分项(回退旧字段); 散户买/卖此前一直"--"现在有数。
- **测试**: pytest 21过(含3个新用例)+实测模拟簇; tsc 全绿。

### UI-机会卡层级+首页迷你图

- 机会卡: 评分 13px 加粗 → 11px 次级(去抢戏); 入场/止损/目标独立一行
  12px 加粗(止损绿跌色/目标红涨色); 建仓红绿经核符合 A 股红多语义, 不改。
- 首页指数迷你图加面积渐变(深色底细线看不清趋势)。
- 审计纠偏:"机会精选/机会发现"是不同功能, 不砍; "主力净量"列保留+tooltip 注明口径。
- **测试**: tsc 全绿。

### UI-暗盘TOP行跳+排序

- 整行点击跳行情(此前只有代码列小链接); 列头点选排序(净流入/净量/成交额,
  点同列切方向); 表头 sticky+底色; 主力净量列 title 注明股数口径;
  tck 无数据"仅持仓股"改为"-"。
- **测试**: tsc 全绿。

### UI-P0 崩溃加固+资金柱独立轴

- **资金柱独立轴**(真 bug): 副图 `volumeSeries` 只被喂过活跃度/资金柱,
  默认 L3 开 = 资金柱(元)冒充成交量顶着 volume 轴 → 轴撑到 5 亿(506.43M)、
  真量柱压扁。现在: 成交量 bars 回归(股, volume 轴, L3 关也不再空白);
  L3 资金柱独立 `fund` 轴(左)同显, 关/切档清空藏轴; 活跃度/MACD 档保持独占。
- **白屏加固**(偶发未复现): Quote 主力意图加 `typeof string` 守卫;
  ErrorBoundary 生产显示错误摘要+折叠堆栈+一键复制(下次崩直接定位)。
- **未修**(实证无害): forecast 503(引擎本机正常, 容器够不着, 页面有红绿灯+降级);
  30 元轴(4 月 23 元真历史); 08-31/09-01 零成交扁平(疑似停牌)。
- **测试**: tsc 全绿(与下条同批验证)。

### UI-行情页去重去空

- 副图控件去重: KlineChart 内部副图行父受控时隐藏(两套互不同步),
  SUBCHART_OPTS 抽常量; 筹码块两项全空不渲染(此前挂两个"--")。
- **测试**: tsc 全绿。

### 整体优化③#6构建分层+#7离线单测

- **构建分层**(#6): 第三方与本地包分开装, 改 marketdata 不再触发
  全量 pip 重装(httpx 由主 requirements 提供, --no-deps 秒装)。
- **离线单测**(#7): 跨日残留→全量重拉转 urlopen mock(210 笔, 时刻/价格
  双轨防指纹碰撞+午夜边角), 不再依赖腾讯可达。
- **测试**: dark_flow.py 18 passed + ops/archive/guard/dedup 26 passed; ruff 全绿。

### 整体优化②异常告警+#2灰度切源

- **异常告警**(#5): `darkflow_alerts`(suspect/stale 推全局默认渠道,
  同股同类每日一次, 无渠道/失败静默); 主链路钩子失败不影响计算。
  WeCom corp 权限(850003/853006)仍需用户在管理页授权+购买, 修好前走其他渠道。
- **灰度切源**(#2): `_DARK_SOURCE_CTX` per-request 覆盖,
  `GET /api/dark-flow?source=thsdk` 单股验证, 灰度不读写共享缓存
  (零残留可回滚); 非法 source 400; diag 透出本次实际 `source`;
  默认源不动, 等验证数据再定。
- **测试**: ops 18 例(灰度隔离/400/ctx)+archive 3 例; ruff 全绿。

### 整体优化①前端展示+#3存档+#4序列

- **前端展示**(①): `DarkFlowCards` 接 `verdict_note`(翻转蓝条, 有值才显)、
  `diag.stale`(停滞徽标+落后分钟 tooltip)、数据行(逐笔 N 笔·末笔·N 页);
  tsc 全绿。
- **逐笔日存档**(#3): 迁移 m130 `tick_archive`(code,trade_date 唯一,
  全天逐笔 JSON+结论); 收盘后(≥15:05)自动快照一次(只存被查过的股),
  `POST /api/dark-flow/archive` 可手动补; 失败永不影响主链路。
- **跨日序列**(#4): `GET /api/dark-flow/series`(新→旧, "连续流入 N 天"底座),
  与存档同表。
- **测试**: tick_archive 3 例(sqlite 内存全链路)+ops 11 例, 14 passed; ruff 全绿。

### P0 拉取层根治:未来tick过滤+按日快照+并发丢数修复

- **未来 tick 根治**(P0-1): `_drop_future_ticks`(超 now+60s 丢弃),
  全量三路径+增量合并统一前置(去重洗不掉未来 tick);
  磁盘快照 key 按日分(`all:YYYY-MM-DD`, 只读今天, 旧 `all` 脏 key
  首次 delete+TTL 自然过期)。
- **并发丢数修复**(P0-2): `_drain_pages` 同批收齐按页码排序后处理,
  只有批尾连续空页才停(此前 as_completed 乱序计数误杀同批数据页);
  新增 `_LAST_FETCH{pages,ticks}` 观测 + 交易时段拉空重试一次;
  `compute_dark_flow` 出 `tick_pages`, diag 透出。
- **测试**: ops 7 例全绿(含冻结时钟的未来 tick 用例);
  dark_flow 回归 25 passed; ruff 全绿。

### P1 结论可信度:翻转注记+停滞检测+P2-7 diff重拉

- **翻转注记**(P1-4): `compute_dark_flow` 出 `verdict_note`,
  同日上次结论变号或差超 5 千万则注记("上次净流出X→本次净流入Y"),
  main_intent 透出, 前端有值才展示。
- **停滞检测**(P1-5): `_tick_staleness` 纯函数(工作日 09:25-15:05 内
  末笔落后超 10 分钟 → stale), 只进 diag, data_status/口诀链路不动,
  盘后/跨日/无数据不误报。
- **diff 重拉**(P2-7): `POST /api/dark-flow/refetch`(仅管理员),
  清缓存→重算一次返回 before/after/dedup_removed/verdict_changed。
- **测试**: ops 11 例全绿; ruff 全绿。

### P2 收尾:港股volume实测+P0-3降级结论

- **港股实测**(P2-6): 00700 东财 K 线量 16557646 vs 腾讯 Quote
  15980655(股), 同量级 → `116.` 不 ×100 闸门正确, 用例缀实测证据。
  K 线归一矩阵齐了(腾讯 CN×100/东财 CN×100/东财 HK×1)。
- **P0-3 降级结论**: 查了 TQ L2(成品净额, 非逐笔)+sina(仅 Quote),
  无 tick 级备用源可接。腾讯全挂时已有 `insufficient` 标记 +
  L2 明盘照常 + diag.tick_pages==0 即"源挂"信号, 不硬编 fallback。

### ops dark-flow 运维杠杆:diag 可见性+清缓存接口(main_net 钉死治本)

- **背景**: v0.4.83 上线后 main_net=-125035724 一字不动;
  去重修复只在"拉到新页/合并"时生效, 内存+磁盘双层快照+增量"无新增"
  原样返回够不着, 且接口不暴露 tick 数/末笔时刻, 冻住了也看不见。
- **改动**: `compute_dark_flow` 新增 `last_tick_t`(末笔时刻);
  `/api/dark-flow/*` 返回新增 `diag{tick_count,last_tick_t,trade_date}`;
  新增 `POST /api/dark-flow/cache/clear`(仅管理员, 不传 symbol=清全部,
  同步落盘防重启回血), 下次请求全量重拉。
- **测试**: `test_darkflow_ops` 4 例(清单/清全/diag 转发/清接口);
  回归 dark_flow 系 25 passed; ruff 全绿。

### fix 三问题:逐笔去重补齐+volume归一+口诀文案(决策先锋算法对账)

- **① 全量路径指纹去重**: `_fetch_all_ticks` 全量三处 return 此前无去重
  (仅增量合并有), 并发翻页内容漂移致重复计数 → 主力买卖 10.54亿 vs
  成交 6.05亿熔断。抽取 `_dedup_ticks()` (t,price,amt 三元组) 三处统一。
- **② volume 归一为股**: 腾讯 fqkline/东财为手, sina/TQ 原生股, Engine
  透传致跨源差 100 倍。`fetch_tencent_kline_raw` ×100, `fetch_eastmoney_kline`
  仅 A股 secid(0./1.)×100(港股 116. 不动, 未实测)。
- **③ 口诀 suspect 文案**: 熔断时误套"不足30笔"模板(实际 5 万笔)。
  suspect →"数据异常…口诀暂停", insufficient 保留原模板;
  `dark_order` 加 `trade_date` 标注(簇只有日内时刻)。
- **测试**: `test_dark_flow_dedup` 5 例 + kline volume 2 例新增/1 例同步;
  dark_flow 系 25 passed; marketdata 196 passed; ruff 全绿。
- **未修**(证据不足): clusters 混入昨日 14:51 簇单次观测; 翻页实测为
  朝前(p=0 开盘)无跨日泄漏口, 先观察, 复现再修。

### fix TQ 陈旧快照门禁 + summary 强刷(09-03 漏数事故治本)

- **根因**: TdxW 未更新时 TQ 网关返 09-02 快照却报成功, Engine 视为成功
  不再 failover(quote/K线双链全压住); summary 无强刷手段, 热切后只能干等
  5min 缓存过期。
- **改动**: `packages/marketdata/.../vendors/tq.py` 新增 `tq_bars_fresh()`
  (最新 bar < today-1 视为陈旧, `TqKlineVendor.fetch` 返回 [] 触发降级;
  阈取 today-1: 盘前/周末/节假日不误杀); `GET /api/klines/{s}/summary`
  新增 `refresh` 参数跳过 L1+L2 强制重算。
- **测试**: `test_tq_stale_guard.py` 6 例(当日/昨天边界/两天前陈旧/空/
  横线格式/today floor); marketdata 全量 196 passed; test_decision_pioneer
  + test_summary_layer_api 19 passed; ruff 真 bug 类全绿。注: 本机
  marketdata editable 指向旧 clone sida-src, 本地验证须
  `PYTHONPATH=sida-pro/packages/marketdata/src` 覆盖(CI/Docker 用仓内
  相对路径不受影响) [commit 7e13c4d]。

### update 海外K线补数通道 + 行情主备热切(09-03 漏数事故)

- **事故**: 持仓页停在 09-02。根因三层: ①小主机 WSL 内 DNS 被劫到
  198.18.x(透明代理), 容器出口 HTTPS GET 被掐(腾讯/东财/新浪全挂),
  09-03 18:00 容器内 backfill 0 行, 全市场漏 09-03;
  ②TQ 网关吐 09-02 陈旧快照却报成功, 挡住正常腾讯(quote 链 tq prio 0);
  ③engine 三源标签名不副实(同一合并列表), 误导排查。
- **止血**: 海外直抓腾讯 fqkline(口径 vol×100=股, 与 PG 现存行核对一致),
  49 股×3 源 147 行幂等灌入, API 已验 09-03 10.55;
  `/api/datasources/46` tq priority 0→4 热切, quotes 切到腾讯实时
  (10.55/-4.44%, quote_time 09-03 16:14), tencent 抖动仍回落 tq。
- **durable**: 新增 `scripts/offsite_kline_backfill.py` (Hermes 宿主机跑,
  当日无行跳过不编造, 501 退避+1.2s 间隔), 配 Hermes cron 每交易日
  18:35 兜底 [commit 05c4a05]。
- **待治本**: TdxW.exe 需重启/重登录(09-03 数据都没拉, 28号此前已预警
  RPC 假死); WSL 出口代理掐 HTTPS 需查 Clash/Tailscale DNS(198.18.0.2);
  `publish_kline_backfill` 跨 loop 发布报错另起任务修。

### doc P3-A 四账号生产验收报告(v0.4.81, 21/21 PASS)

- **部署**: tag v0.4.81 → ACR 构建成功 → 小主机 `panwatch` 重建
  (env 原样复用 `--env-file`, 卷/网络不变, 数据零动), 健康 `ok`,
  旧镜像 v0.4.75-80 已删 + prune。
- **验收** (`scripts/p3a_accept.py` 已入仓, 下次发版可重跑):
  admin 登录/读四项/datasources 44 项无 500/health 累计 5 列齐 (P2-C 生产验证);
  临时 member 建号→登录→自建账户→看不见 admin 持仓(0 vs 1)→自选独立→
  自写自选/持仓 ok→用户列表 403→删号→登录被拒; 4 账号存在+active。
  临时号已删, 级联清理无残留。
- **缺口**: demo 密码未知, 直接登录未测(仅存在性+历史 demo 权限实测)。
  黄磊/娟姐已闭合: 用户给密码后补测 13/13 PASS(登录/持仓隔离 n=0/
  自选行 id 与 admin 0 交叉/通知 200/健康累计 403 符合无 manage 权限/
  用户列表 403)。注: 自选同标的出现系各自独立建行(行 id 不同),
  非越权(初版脚本按 symbol 判曾误报, 已按行 id 纠正)。全程只读零写入。

### release v0.4.81(P2 部署防护 + CI 门禁根因 + 健康列 server_default)

- 自 v0.4.80 起 3 个 fix 合并: P2-C 健康计数列 server_default [commit edd7abc] + P2-A 部署防护3条(tar备份/迁移校验/暂停开关) [commit 3a50e36] + P2-B ghcr CI ruff F811 根因修 [commit e9e0613].
- 验证: ghcr run 52 test+build 双绿(终结 run 48-51 四连 fail), GHCR latest 已重推; CI 子集 1377 passed + marketdata 190 passed; ruff 全绿.
- 发版后做 P3-A 四账号生产验收(验收报告见部署回复) [commit be1925d]。

### fix P2-B ghcr CI test 门禁根因修(ruff F811 拦构建)

- **根因**: `src/core/l2_ticks_scheduler.py:101` (v0.4.77 新文件)
  `__init__` 形参名 `timezone` 遮蔽了 `from datetime import ... timezone`
  (line 18), CI `Lint (ruff --select E9,F821,F601,F811)` 报 F811,
  ghcr run 48-51 四连 fail → build job 全 skip。注: 任务原标题的
  "test_ws_hub AsyncMock" 是 v0.4.51 时代的失败, 当前 pytest 子集本地
  1377 passed + marketdata 190 passed, 测试本身没问题, 真凶是 Lint。
- **改动**: 形参改名 `timezone` → `tz_name`
  (`AsyncIOScheduler(timezone=tz_name)` 保持 APScheduler 原语义);
  未使用的 `timezone` 导入一并删除; 唯一调用点
  `server.py:1918` 同步改 `tz_name=settings.app_timezone`。
  全仓 grep 确认无其他 `L2TicksScheduler(timezone=` 调用。
- **测试**: 本地 ruff 0.16.6 (与 CI 同版) `All checks passed`;
  构造器 kwarg + 默认值双路径实测 OK; `py_compile` 过;
  marketdata 190 passed; tests CI 子集 1377 passed + 2 skipped(修前已验证) [commit f59f7cf]。

### update P2-A 部署流程防护 3 条(tar 备份/迁移校验/cron 暂停开关)

- **① tar 备份**: `~/sync_sida_to_host.sh` (repo 外运维脚本, 原文件已备份
  `sync_sida_to_host.sh.bak-20260904`) 远端 `rm -rf` 前先打时间戳 tar 包到
  `/root/sida-backup/sida-pro-YYYYMMDD-HHMMSS.tar.gz`(排除 data/.env/
  node_modules/dist), 只留最近 5 个。防同步误清小主机未合入改动。
- **② 迁移校验**: 新增 `scripts/check_migrations.py` (29 个迁移 v101-v129
  连续性 + HEALTH_COLUMNS⊆模型列 + 计数列 server_default 防回归 +
  dtype DEFAULT 0 一致性, 任意 cwd 可跑); 同步脚本打包前必跑, 失败中止;
  ghcr CI `build-and-push-image.yml` 新增 `Migration consistency check`
  步骤 (v0.4.51 缺列 500 教训)。
- **③ 暂停开关**: 同步脚本顶部检查 `/home/ubuntu/.sida_sync_paused`
  存在即退出(与 cron `45d3592196a4` pause 双保险, 28号改代码期间 touch 即可)。
- **测试**: `bash -n` 过; 暂停开关实测 exit 0; check 脚本 repo 根/`/tmp`
  双路径 `29 个迁移通过`; workflow YAML 解析 OK [commit 6fd467d]。

### fix P2-C data_sources 健康计数列加 server_default(防御新鲜 create_all 建库)

- **改动**: `src/web/models.py::DataSource.success_count/error_count` 加
  `server_default=text("0")`(python 侧 `default=0` 保留), 与 `_m126` 的
  `INTEGER DEFAULT 0` 同口径。此前仅 python-default, 裸 SQL/新库 create_all
  建表无 DB 级默认值, 与老库迁移后语义不一致。
- **测试**: sqlite `:memory:` create_all + 裸 INSERT(不带计数列)回读
  `(0, 0, None)` [commit 8e0d9f5]; 既有 `test_datasource_{reconcile,test_path,admin_api}` 7 passed。

### fix v0.4.80 三路并行审计P0 batch(后端盘点/前端接线/稳定性)

- **P0-1 _fmt_amount 嵌套重复定义清理**: `src/agents/intraday_monitor.py` 模块级(28行)已存在,
  `_main_intent_both_inner`(81行)与`_main_intent_summary`(180行)内两处同名嵌套def删除,
  统一走模块级(含非数值兜底, 更健壮)。测试: test_dark_flow系33例仍过。
- **P0-2 DarkFundTop亿阈值统一**: `frontend/src/pages/DarkFundTop.tsx::toAmountFromWan`
  `>=1.5e4万`改为`>=1e4万`, 与Quote/L2Orderbook/safeMoney/后端_1e8同口径(1亿切亿)。
- **P0-3 金额符号统一**: `lib/format.ts::safeMoney`与`toAmountFromWan`补恒显符号(+/-),
  与后端`_fmt_amount`一致。注: 审计原文"Quote负数不带号"误报, Quote/L2靠toFixed自带负号,
  实际缺号的是safeMoney/DarkFundTop两处, 已修。验证: tsc 0 + pnpm build过。
- **P0-4 thsdk成交量口径定案(非bug)**: 查清`dark_l2.py`thsdk逐笔单位=股(文件头18行契约+
  399行`/100`股→手实证), `quotes.py:318`是腾讯分时(单位=手, ×100正确)。两边都对,
  只在178行注释加注防后人重踩, 零行为改动。
- **P0-5 数据源健康加tq_moreinfo第5源**: `src/core/source_health.py`新增`check_tq_moreinfo`
  (只探`TDX_QUANT_URL`或vendor已缓存地址, 2s超时; 从未发现→degraded不编造;
  不跑全候选扫描防health卡15s)。`SOURCE_DEFS`+5, 单测2例新增。
  前端: `useSourceHealth.ts`加`明盘→tq_moreinfo`映射, `DataSources.tsx`顶加"实时链路"
  状态条(hairline分隔, 无卡片), Quote图标灰显逻辑自动生效。
- **RISK-2 auction日期洗白**: `auction.py`endpoint不收不返date(YYYYMMDD只进thsdk),
  无白屏路径, 不改。
- **全量唯一失败根治**: `tests/test_thsdk_buffer_size.py`真thsdk已装时全量必挂
  (模块级`from thsdk import THS`绑定时机问题)。改法: import M前换假模块、绑完恢复
  sys.modules。验证: 与test_thsdk_api.py同跑55 passed。
- **接线审计结论**: 前端57路径×后端228端点全对照, 真断线0条(12条疑点全为参数名/
  查询串/自注册归一化误报, 逐条实锤); 后端未接线183条中用户价值高的
  (决策先锋/情绪周期/策略信号/预测报告/影子/龙虎榜等)列入下批接线。
  测试: source_health 39 passed; 全量1587例 1582 passed+4 skipped(本修之前),
  唯一失败即上述thsdk用例, 已根治(待合main后全量复核)。[commit 5680f75]

### fix

- **v0.4.72 health 端点超时保护（生产事故 hotfix）** (28号 hotfix). **事故**: 09:44 thsdk 腾讯数据源 (web.ifzq.gtimg.cn:443) 连接风暴, worker 进程飙至 73% CPU + 1.2GB RAM, 主进程事件循环被拖死 → /api/health hang 30s+, Docker healthcheck 10s 超时堆积（实测 4 个 healthcheck 进程堆着占 ~80MB）, 容器状态 `unhealthy` 持续 30 次（FailingStreak=30）。**修复**: `src/web/api/health.py` health 端点改为薄壳, 业务逻辑包成嵌套 `_check()`, 外层 `await asyncio.wait_for(_check(), timeout=HEALTH_TIMEOUT=5.0)`; 超时或异常立即返回 `down`(不再卡事件循环), Docker healthcheck 不会再堆积。**根因 thsdk 重试风暴的 circuit breaker 根治留单独 hotfix**(目前仅防二阶 healthcheck 堆积, thsdk 仍可能让业务接口变慢)。回滚预案: 立即 `docker restart panwatch`(已实测, 8 小时前那次事故用此恢复)。测试: py_compile 通过 + 模块 import 成功。

### fix

- **v0.4.73 thsdk 进程级熔断器 + 并发限流(v0.4.72 事故根因修复)** (28号, feat 分支). **根因回顾**: v0.4.72 健康事故是 thsdk 腾讯数据源(web.ifzq.gtimg.cn:443)连接风暴→worker 死循环重试(30s×3=90s/次)→主进程事件循环被拖死→health 端点 hang。v0.4.72 的 health 超时保护只是防二阶(healthcheck 堆积),未根治 thsdk 重试风暴。**根治**: 新增 `src/core/thsdk_breaker.py` 进程级三态熔断器(closed/open/half_open, 线程安全): 连续失败 ≥ `THRESHOLD=5` 次→`cooldown=60s` 冷却→冷却期间 thsdk_call 直接返回 default 不再调底层; 冷却后下次调用放行做半开探测(成功→关闭,失败→重新开); 配套 `Semaphore(3)` 并发信号量(同时最多 3 路 thsdk 调用,超出排队)从源头截断无限堆。**接入点**: `src/core/dark_l2.py` `_fetch_thsdk` 包 `_fetch_raw_rows` 调用(v0.4.72 事故的主要风暴点),失败/熔断中返回 `[]` 走原有降级链(腾讯逐笔→None)。**测试**: `tests/test_thsdk_breaker.py` 8 例全过(三态转换/失败计数重置/半开探测/超时/skip 验证); 集成测试: 5 次连续 ConnectionError→state=open→后续 fn 被调 0 次。其他 thsdk 调用点(chat_tools wencai/orderbook_engine/thsdk_alert 等)后续 hotfix 逐个接入,本次只动事故主路径 dark_l2,风险最小。

## 2026-09-03

### update v0.4.79 后端常量统一 + 主力意图抽公共判据(进行中)

- **P1-#8 strong_absorb 三常量统一完成**: `src/agents/intraday_monitor.py` 内 10 处硬编码
  `500e4/35/48` 全部改为 `from src.core.dark_flow import MAIN_NET_LIMIT, ABSORB_INTENSITY, ABSORB_BUY_RATIO`。
  解 2026-08-25 审计约束"仅落地 dark_flow 侧"的限制, 两端判据口径现已 100% 一致。
  影响函数: `_main_intent_both` (line 80/128/130)、`_main_intent_structured` (line 179/267/269)、
  `_derive_direction` (line 481)、`_main_intent_report` (line 482/484)、`_board_snapshot_tag` (line 635)。
  测试: `test_dark_flow / test_dark_flow_guard / test_dark_flow_tq / test_intraday_monitor_json_format /
  test_intraday_noalert_reason / test_tradingagents_main_intent` 共 33 passed。
- **P0-#5 口诀⑥⑦ 活代码化完成**: `src/core/dark_flow.py::_judge_mnemonic` 新增 `tck_active_ratio` 入参
  (0-100)。有 .tck 数据时走原始「主动率」口诀(双大单>85% / 双小单<30%), 无 .tck 时走腾讯兜底
  (缩量+震荡 / 内外失衡+不动+放量), 不破坏现有行为。新增 helper `compute_tck_active_ratio(symbol)`
  从 `PANWATCH_TCK_DIR/{sh|sz}{code}_{yyyymmdd}.tck` 解析大单占比(amt≥30万为门槛)。
  接入点: `src/web/api/darkflow.py::darkflow` 在 data_status=="ok" 时调用 helper 传参。
  阈值常量 `_MNEMONIC_TCK_DUAL_SMALL=30.0 / _MNEMONIC_TCK_DUAL_LARGE=85.0` 写在 dark_flow 顶部。
  实现细节: tck 路径的 detail 用 `lambda` 延迟格式化(避免 tck_active=None 时 f-string 立刻炸)。
  测试: `tests/test_dark_flow_mnemonic_tck.py` 8 例全过(双大单/双小单/中区间/None/越界/非数值/
  既有兜底仍工作/tck 优先于兜底), 既有 dark_flow 6 文件 33 例仍全过, 总 41 passed。
- **P1-#10 _derive_direction 抽公共判据 → 决定不抽**: 验证 `grep -rn _derive_direction src/` 仅
  1 处调用(`intraday_monitor.py::_ai_counter_check`)。28 号邮件原话"抽到 core 共享"无第二调用点
  证据, YAGNI 原则不抽。已在 dark_flow.py 内用统一常量 (`MAIN_NET_LIMIT/ABSORB_INTENSITY/ABSORB_BUY_RATIO`),
  口径已 100% 一致, 不需要跨文件抽公共函数。
- **P0-#3 get_dark_flow_precise AI 包装 → 已存在**: 28 号邮件"需暴露 + 补测试"实际已完成。
  `src/core/chat_tools.py::get_dark_flow_precise` 已在 v0.4.30+ 暴露, 包含主笔级还原 +
  拆单簇暗盘 hook(dark_review_from_tck, 失败独立兜底) + units 标注 + note 说明。
  测试覆盖完整: `test_chat_tools_a4.py` 3 例(hook 成功/失败/异常) + `test_chat_tools_two.py`
  包含 8 例(无文件/成功/空成交/parse 错等), 共 31 passed。本次未改动代码, 仅验证完整性。
- **P1-#9 盘中暗盘主笔级切线 → 已实装**: 28 号邮件"需新增 PANWATCH_DARK_SOURCE=thsdk 切线"
  实际在 v0.4.71 已实装 `src/core/dark_flow_fusion.py::compute_dark_fusion`, 暗盘主线 =
  .tck (官方方向 2B/2S) + thsdk L2 逐笔(被动覆盖) 融合, 单链路无法闭环的两侧互补。
  `compute_pool_flow` 通过 `_dark_flow` 自动调用融合, 不需要新增 `primary_only` 开关。
  本次未改动代码, 仅验证完整性。

### feature v0.4.78 abnormal_moves DB 超时根治

**问题**：v0.4.77 部署后 `/api/abnormal-moves` 仍 30s 超时撞 502。根因：
- `analyze_for_symbols` 串行遍历 30+ 只股，每只股内调 K线 API（~1s），串行=30s
- 后端 `pool_size=10 + max_overflow=20 + pool_timeout=30` 撞池时直接等 30s
- PG `statement_timeout=30s` 让慢查询也等满

**修复**：
1. `analyze_for_symbols` 改 ThreadPoolExecutor 并发 4 路 + `wait(FIRST_COMPLETED)` 循环 + 全局 deadline 强制退出 + `executor.shutdown(wait=False)` 让接口立即返回。30+ 只股 30s → ~8s（并发 4 路 + 8s 单股兜底）
2. `database.py` PG 引擎 `pool_size=20 + max_overflow=40 = 60 总`，`pool_timeout=30s→10s`（失败快速失败），`pool_recycle=1800s`，`statement_timeout=30s→8s`
3. 测试 `tests/test_abnormal_moves_concurrent.py` 5 例全过（并发加速 / 单股超时 / 空列表 / 全过滤 / 异常隔离），现有 `test_abnormal_moves.py` 67 例 + `test_summary_cache` 5 例 + summary/history 32 例 共 109 passed
4. 前端 tsc 0 error

### feature v0.4.77 性能大修（5 项缓存落库 + 慢查询根治）

**问题**：实测生产页面 12.5s 并发加载（14 个接口），`/api/klines/{symbol}/l2-ticks` 30s 超时撞 502、`/api/market/mainline` 5-20s 冷启动、`/api/market-data/fundamentals-detail` 12-17s 冷启动、`/api/abnormal-moves` 30s DB 池超时、`/api/history?limit=20` 3.1s 拉 2.1MB 全文。根因：l2-ticks 默认 fetch=1 每请求实时拉 thsdk，summary/fundamentals 只走 30s 进程内缓存进程重启失效，history 列表无 content 截断。

**修复**：

1. **P0 新增 summary_cache 表 + L1/L2 双层缓存**（`src/core/summary_cache.py` + migration `_m129`）。`/api/klines/{symbol}/summary` 进程内 5min → 命中 PG summary_cache → 计算。`fetch_l2_ticks` 端点 fetch 默认 1→0（只读库），新加 `src/core/l2_ticks_scheduler.py` 5min cron 盘中自动落库自选+候选池。L2 冷启动从 30s 撞 502 → 秒级返回。
2. **P1 `/api/history?summary_only=true`** 列表省 content/raw_data 全部重字段（2.1MB → ~1KB），Dashboard history 列表秒开；详情走 `GET /history/{id}`。前端 `dashboard.history()` 默认加 `summary_only: true`。
3. **P1 `/api/market-data/fundamentals-detail/{symbol}` 24h PG 缓存**（复用 summary_cache 表），冷启动 12-17s → <300ms；新增 `_refresh=1` 强刷参数。
4. **P2 `/api/market/mainline` 缓存 TTL 60s→30s**，涨停池冷启动 5-20s 太贵但页面 30s 内轮询会撞一次，30s 命中率足够。
5. **测试**：`tests/test_summary_cache.py` 5 例全过（roundtrip/expired/overwrite/market 隔离/clear），现有 summary_layer/summary_resonance/history_store 测试 32 例全过。前端 tsc 0 error。

生产部署：v0.4.77 镜像构建后需 `sudo docker exec panwatch python scripts/init_db.py` 跑 migration 129 建 summary_cache 表（生产 PG 已 init_db 但需补 m129）。

### release v0.4.75（终端化§4.3清零 + GS降噪 + 活跃度副图载体）

- 自 v0.4.74 起 4 个 feature 合并: GS抖动合并P2 [commit e3c79da] + 终端化token落地(bg-kline/字体) [commit 2908684] + §4.3剩余色板(flat/threshold/accent/tint) [commit 7c00f5f] + GS色收敛+活跃度三色载体 [commit d081a24].
- 验证: 前端 tsc 0 + vite build 15s + 后端 test_summary_layer_api 13 passed + test_decision_pioneer/activity 11 passed.

### fix

- **撤大额撤单重叠显示修复** (用户截图: 神剑 002361 K线弹窗右上 7 个"撤大额撤单"摞成一摞). 根因 `l4_events.cancel_anomalies` 同一日每笔大撤单各产一条事件(最多 20 条), 前端按事件画 marker, 同 date 下 N 个 marker 全叠在同一根 K 线上完全重合. **修复**: 后端按日聚合成一条(label `大额撤单(N笔)`, shares 合计, count 笔数, time 取最晚一笔, 新增 `_safe_vol`/`_t_sort_key` 安全取值); 前端 `InteractiveKline` + `KlineChart` 同 date+kind marker 合并为一个(文案缀 `×N`), 防以后其他多事件同日再叠. 测试: `test_l4_events.py` 27 passed (含新增聚合用例) + 前端 tsc 0 error. [commit 2bf0033]

### feature

- **终端化 P1-1 小三页去卡片** (研究文档 §5 P1 地基层: IndexDetail/DarkFundTop/AnalysisDetail). card 边框底 → hairline 分隔 + 颜色收敛 stock 令牌, 零逻辑改动. tsc 0 error. [commit f0078db]
- **终端化 P1-2 Dashboard 去卡片** (4 大区 hairline + 指数 pill 去底 + FEED_BADGE/amber 暗色收敛 + 基准占位虚线框). 数字层零改动(单位亿/万口径已核对). tsc 0 error. [commit 65adfdb]
- **终端化 P1-3 Forecast 去卡片** (输入侧栏/进度/结果/报告/回测/历史 6 区 hairline + 方向色 stock 令牌 + hit/miss emerald/rose 暗色修复 + T+N chips/report 框去底). tsc 0 error. [commit 4119780]
- **终端化 P1-4 Opportunities 候选卡瘦身 + 共振可视** (研究文档 §5: OpportunityCard 堆叠 → 紧凑列表行; 15 行元数据压成入场/止损/目标/策略/来源徽章 2 行 + 因子 1 行; 顶部统计/因子/状态条去卡片; 🔥共振徽章保留前置; 修 tdx 行 dark 下 emerald-700 隐形 bug). tsc 0 error. [commit 475d05f]
- **终端化 P1-5 Forecast 预测锥图** (研究文档 §5: 预测结果卡 → K线+预测曲线叠加). 新建 `ForecastConeChart.tsx`: ECharts 历史收盘 60 日(灰线, `/klines` 接口) + 预测中线延伸(方向着色) + Kronos P5-P95 置信带(堆叠面积); 未来横轴只标 T+1..n 不编造日期; 历史拉不到降级纯预测段. tsc 0 error + vite build 通过. [commit 542dec4]
- **终端化 P1-6 Dashboard 首屏重排 + 情绪三卡去卡片** (研究文档 §5: 总览卡片 → 模块化大屏). 首屏改先市场后个人: 情绪周期/市场主线/市场温度/大盘资金流/异动+分布整体前置到 KpiBand 之后, 个人工作台(要紧事/体检/机会)后置; MarketPhaseCard/MarketMainlineCard(仅 Dashboard 引用, 源头去卡片)/本地 PhaseGaugeCard 根容器去 card 底. tsc 0 error + vite build 通过. [commit 0a63aae]
- **终端化 P1-7 AnalysisDetail 嵌 K线主图 + P1-8 收尾** (研究文档 §5: 详情页 → 主图+副图+文字). 正文顶部嵌入 InteractiveKline(与 IndexDetail 同款, 未碰 28号 Quote/KlineChart scope); DarkFundTop thead 去底色; IndexDetail 经核对已达标(主图裸放+副图)零改动. tsc 0 error + vite build 通过. [commit 0a63aae]
- **终端化 P2-1 Reports/Login/ShadowAccount 去卡片** (研究文档 §5 P2 辅助页). Reports 分组 card-subtle → hairline 分组 + 空态去盒; Login 表单 card → hairline(轻量化); ShadowAccount StatCard 背底 → 左 hairline 格 + 画像区去 card 底; 上传虚线框/语义 error 盒保留(功能性). tsc 0 error. [commit fd2e129]
- **终端化 P2-2 Settings 表单统一** (研究文档 §5 P2: 设置 → 表单统一). Hero/搜索空态去 card 底(hero 渐变保留); 11 个表单 section `card` → border-t hairline(网格布局保留); 5 处 `rounded-lg bg-accent/30` 列表行 → hairline 行(hover 底去掉). 零逻辑改动. tsc 0 error + vite build 通过. [commit 0deade6]
- **终端化 Quote/K线主题跟随** (原 28号 scope, 用户移交). KlineChart 图表底/网格/轴/十字线硬编码深 slate → 全走 CSS token(`readChartTheme`, MutationObserver 跟随 dark 切换, 不重建图); 中性 marker/无数据灰收敛 muted; L1 均线灰阶收敛 muted(牛蓝/马橙专业语义色保留); 主图容器去框裸放; 修周期按钮 `pxpx-2` 笔误 + 无效 hover 类. Quote GS 色 rose/emerald → stock-up/down 令牌(与 P0 一致). tsc 0 error + vite build 通过. [commit 7ab2c08]
- **终端化 A-1 公共组件去卡片** (TabbedPage 两处空态 + ContextCard CardShell → hairline; 图标 chip/loading/empty 保留). tsc 0 error. [commit 62aea73]
- **终端化 A-2 Stocks 持仓页去卡片** (6 统计卡 → 左 hairline 格; 账户卡 → hairline 分隔; 骨架/空态去盒; 下拉浮层/分段控件保留功能性). tsc 0 error. [commit 55f6436]
- **终端化 A-3 PaperTrading 去卡片** (5 统计卡 → hairline 格; 收益曲线/策略绩效/持仓三区 → hairline; 回撤 emerald/胜率 rose 语义色不动). tsc 0 error. [commit 92fc4f4]
- **终端化 A-4 五页去卡片** (Help/Profile hero+section; Agents 调度条+空态+agent行; BoardDetail 指标格+成分股/轮动区, error语义盒保留; PriceAlerts 工具条+空态+规则行; 顺手修启用徽章 dark 下 emerald-700 隐形, 与 P1-4 同类). tsc 0 error. [commit a2f6b19]
- **终端化 A-5 三页去卡片** (DataSources section; History 空态/移动切换/目录+正文窗格; Notifications 过滤条+主从网格去框, 内部分隔保留). tsc 0 error + vite build 通过. [commit 8aa6071]
- **P2 推断页核实结论 (B线, 只读未动代码)**: 共振扫描/L2盘口/主题设置/持仓成本线均无独立路由. 共振后端(wencai/decision_pioneer)就绪且前端已有徽章+窄栏展示 → 不新建页; L2 orderbook 后端就绪(盘口队列+托压)但前端仅 shape 文字 → 十档明细页列后续候选; 主题切换无入口 + klines 无持仓成本字段 → 两页不做等后端.
- **L2 盘口资金页立项交付** (Phase1 核实: /api/orderbook-ob+summary.orderbook+more-info 三端点现成, 后端零新增; 十档买卖"额"有, 十档明细/逐笔无 — 页面诚实口径五档+L2成品). Phase2: `insightApi.orderbookOb` + 新建 `L2Orderbook.tsx`(/l2: 十档双向条+OB事件+幽灵单+L2成品6字段+raw主力净额容错, 30s轮询, hairline无卡片) + 行情组导航/路由. Phase3: Quote 盘口 shape 旁链入明细页. tsc 0 error + vite build 通过. [commit 5fafdf5/fa0612e]
- **v0.4.74 L2盘口资金页上线 + P1 thsdk盘中验证 + GS校准**. 前端`/l2`随版上线(生产盘中验证十档/形态/主力净额). P1: 09-03 11:08盘中实测神剑002361 — tick_super_level1游客1962笔实时(B1045/S882/M35, 8.76亿) + big_order_flow账号512笔(B215/S297, active296/passive216, 2.92亿), 云端L2通, 注释落`dark_l2.py`. GS校准: 上证501根实测非重绘60/60一致, G/S位置滞后(中位0.53/0.37)+抖动16/62, 维持趋势过滤定位, 报告`docs/GS校准报告_原版视频口径.md`. 另: 生产v0.4.73 thsdk重连风暴致unhealthy一次, `docker restart`恢复(风暴源未定位, 待观察). [commit 731685c]
- **GS抖动合并P2** (校准报告跟进). 新增`merge_whipsaw`(纯函数, 窗口`WHIPSAW_MERGE_DAYS=3`): 相邻反向信号间隔≤3天成对丢弃, 日期缺失不猜保留; `compute_gs_signals`默认`denoise=True`(传`False`取裸序列, 公式零改动). 上证501根实测63→37, ≤3天残留0/36; 单元(成对丢/超窗保留/日期缺保留)全过. 现有`test_decision_pioneer`+`test_summary_resonance` 22 passed. `eval_gs`区间语义不动.
- **终端化残留token落地 §4.3** (研究报告跟进, feat分支). `index.css`新增`--bg-kline`(亮纸面白/暗接近黑hsl(222,47%,6%)) + `--font-cn`/`--font-ui`; body字体改走变量; `readChartTheme().bg`改读`--bg-kline`(K线底与页面底分离, 暗色K线更沉). tsc 0 + vite build 20s通过. 未做: 活跃度三色(无副图柱载体不硬套) + GS琥珀/紫(与A股红绿惯例冲突, 维持G红S绿).
- **终端化§4.3剩余色板** (awesome-design-html Robinhood tint手法校准, feat分支). `index.css`亮/暗新增`--flat-color`/`--threshold-line`(平盘灰#94A3B8基, 阈值线使用处alpha .5) + `--accent-primary`(强调橙#F59E0B) + `--stock-up/down-tint`(涨跌淡底, 涨停行/强势标记背景); `stock-colors.ts`新增`readFlatColor`/`thresholdLine`/`readAccentPrimary`/`readStockTints`读取函数. stock-up/down已有(#E53935/#43A047)不动, K线与文字涨跌同源. tsc 0 + vite build 15s通过.
- **GS色收敛+活跃度三色载体** (09-03, feat分支). GS: `index.css`新增`--gs-go/--gs-stop`(var引用stock-up/down同源) + `readGsColors()`(注释原版G绿S红→SIDA G红S绿映射, 验收以位置为准); KlineChart/InteractiveKline GS marker改走gs语义色(值不变). 活跃度: `--activity-bull/strong/weak`(大牛紫#8B5CF6/强势红#EF4444/生命绿#22C55E, 暗色提亮; 弱档走flat灰) + `activityLevelColor(level)`; 后端`ai_activity.activity_series()`(逐前缀复用内核, 公式零分叉, 30根实测末点与单算一致) + klines layer_data `activity_series`字段(同fund_flow门控/降级); 前端L5副图新增`活跃度`档(Histogram三色柱 + 生命1.56/强势3/大牛6阈值线, 无序列不画线) + Quote接线 + DecisionPioneerCard配色收敛(fuchsia/rose/orange→三色token). tsc 0 + build 15s + 后端13 passed(含更新2处activity断言).
- **行情检索+记忆+DP历史+L2落库** (09-03, feat分支). ①Quote搜索联想: `insightApi.searchStocks` + 300ms debounce下拉(名称+代码, 回车名称直解, Esc关闭); ②上次股票: localStorage `quote:lastSymbol`(URL>记忆>上证, 仅首次 fallback 上证); ③DP历史: `history_store.record_dp_snapshot`(新鲜快照落库, 30s缓存天然节流, 180天retention) + `GET /decision-pioneer/{symbol}/history`; ④L2落库: `persist_l2_ticks`(唯一索引幂等去重, 写入数用水位delta精确计数, 60天retention) + klines明盘顺手落库 + `GET /klines/{symbol}/l2-ticks`(fetch=1攒数据/fetch=0纯回查); 新表经m127/m128(PG/SQLite通用). 测试`test_history_store` 3 passed(含幂等去重). tsc 0 + build 15s.
- **Phase 0 Quote收尾** (09-03, [88a056b]). KlineChart新增`costLines`(强调橙实线, 与支撑压力虚线区分, 独立管理+卸载清理); Quote拉`portfolioSummary`全账户持仓按代码归一匹配、有持仓才画成本线(无持仓不画不编造), ContextCard死代码删除; 板块占位换成可点`/boards/{symbol}`跳转; Quote内rounded-lg/xl清零. tsc 0 + build 18s.
- **Phase 1 Dashboard去卡片化** (09-03, [f693ed4]). MarketPhaseCard大字盒→左色条+文字(阶段色只留border/text)、Stat小格→hairline左刻线; MarketMainlineCard行盒→border-b分隔; Dashboard分享下拉rounded-lg→md. Dashboard页其余区(资金流/异动/工作台)此前已是hairline, 本次动三处卡片残留. tsc 0 + build 15s.
- **Phase 2 Opportunities去卡片化** (09-03, [c2e5112]). DiscoveryPanel热门板块/股票/弹窗三处`rounded-xl bg-accent/20`盒→border-b行(含骨架); AbnormalMovesCard容器盒→border-t区、行hover边框盒→hover底色、告警盒rounded-lg→md; Opportunities页Tab/共振开关rounded-lg→md. tsc 0 + build 15s.
- **Phase 3 长尾13页批量** (09-03, [f1f6f17]). 圆角收敛: `rounded-lg`→md 55处、`rounded-xl`→md 36处(图标砖5处保留); 交互列表行展平: DataSources数据源行/Settings通道行/Stocks智能体行与新闻行`bg-accent`盒→border-b行. 13页无base-ui Card引用. 静态表单区盒仅收圆角未改结构(未读不重构). tsc 0 + build 15s.
- **Phase 4 走查修** (09-03, feat分支). ①`/quote`无路由(站内多处链`/quote?type=...`, §4.3只留了`/forecast`, 落空跳首页)——加同守卫别名路由; ②通知页主从双"暂无通知"——有列表无选中右栏改选择提示、空列表右栏留白. 实机走查6页: Quote真K线+成本线可见、Dashboard/Opportunities/Settings扁平无堆叠. tsc 0 + build 36s.

## 2026-09-02

### fix

- **v0.4.53.2 orderbook available 假阳性修复** (xiaoze 复核发现). `orderbook_engine.order_book_queue` 之前只要 snapshot 非空就 `available: True`, 但 thsdk degraded 时返回空 bid/ask 快照 → 前端拿到 available=true 就走不到 §12 灰显兜底, 却没有任何真实盘口数据(假阳性). 改为 `available = bool(bid) or bool(ask)`(有真实盘口价才算), 空快照(thsdk degraded)正确置 False 走灰显. 后端 orderbook 单测: 空快照 False / 无盘口价 False / 有价 True 全合预期. `pytest -k "orderbook or a1"` 44 passed.
- **v0.4.55 TQ 网关自动发现 + 暗盘融合 + WAF 识别 (整合 xiaoze 11 文件附件, P4 主线)**. **根因**: 生产 TQ 一直连不上 — `packages/marketdata/src/marketdata/vendors/tq.py` 默认地址 `172.18.0.1:5100` 是容器网桥/旧 frps, 本机 WSL2 实际是宿主网卡 `172.27.16.1:17709` (TdxW.exe 监听, p50 19ms, 比东财快 50 倍). `dark_l2._fetch_tdx_tck` 读 `TDX_TCK_DIR`, 与生产注入的 `PANWATCH_TCK_DIR` 不一致 → 暗盘侧互补断链. **修复**:
  - `marketdata_tq.py`: 加 `_resolve_tq_url()` 自动发现(默认网关→WSL/Docker 常见网段→回环, 一探测缓存); `_host_gateway()` 读 /proc/net/route; `_probe_tq()` 1.5s 探测. 备 `_FALLBACK_URL` 沿用旧默认, 行为不变.
  - `marketdata_kline.py`: 腾讯 WAF 501 显式识别 + 日志(以前静默返空无从定位).
  - `dark_l2.py`: ① `.tck` 目录改读 `PANWATCH_TCK_DIR` 优先 (兼容 `TDX_TCK_DIR`); ② thsdk 调用加 12s 硬超时 + 线程池, 避免 30s×3 = 90s 拖垮 summary 接口; ③ big_order_flow 代码风格回退 (USZA002361 不通时再试 `002361.SZ`).
  - `dark_pool_flow.py`: 暗盘三级降级 融合→L2→腾讯; 返回带 `active_net`/`split_net`/`passive_est` 分口径明细; 两源都无返 None 不编造.
  - 新增 `dark_flow_fusion.py`: 暗盘融合核心. 主动净额 (.tck 官方方向) + 委托级拆单簇 + 被动侧估计; 被动占比越界 (经验 5%~60%) 标 `suspect`; coverage: fusion / thsdk_only / tck_only / None.
  - 新增 `dark_flow_l2.py`: thsdk L2 逐笔暗盘次选主线.
  - 新增 `fund_flow_nd.py`: 主力资金 1/3/5 日 + 0 轴上穿/下穿.
  - 新增 `decision_backtest.py`: 三指标共振回测 (带官方基准对照).
  - `market_scan.py`: 新增 `resonance_pick()` 三指标 AND 联合选股.
  - `test_dark_pool_flow.py` / `test_decision_enhance.py`: 13+13 新单测. **测试**: pytest 176 passed (相关) + marketdata 190 passed + test_l4_events 26 passed. **未合并待重发**: `klines.py` `if not bars` 不整体早退 + `decision_pioneer.py` `_fallback_bars` 兜底 — xiaoze 在 02:50 邮件正文描述修复 diff 但附件 attachment_count=0 (可能漏附), 已发邮件让重发.
- **v0.4.56 fetch_bars 兜底 + bars 不整体早退 (xiaoze 重发)**. `src/core/decision_pioneer.py`: 新增 `_tencent_symbol()` + `_fallback_bars()`, `fetch_bars` 改为两级取数(主链路 marketdata Engine 优先 → 空则直连兜底东财→新浪→腾讯, 复用包内既有 `fetch_eastmoney_kline`/`fetch_sina_index_kline`/`fetch_tencent_kline_raw`). `src/web/api/klines.py`: `_build_layer_data` `if not bars: return out` 改为不整体早退 — bars 空时仍尝试计算 orderbook/events/chips(`.tck`/wencai/筹码 不依赖 bars). `tests/test_summary_layer_api.py` 10 例. 配合 v0.4.55 的 TQ 自动发现 + 暗盘融合, P4 主线拼图齐了. pytest 81 passed (含新增 10). xiaoze 提示 **这台机器就是部署环境**, 后续部署完它自己 docker exec 验证, 我无需截图/贴日志.
- **v0.4.57 .tck 事件日期错位 + 日期格式统一 (xiaoze 自验 v0.4.55 发现)**. **问题实证**: `/app/data/tck/sz002361_20260827.tck` (8-27 数据) 产出的 465 条 split_cluster + cancel_anomaly 事件被标成 20260902 (今日) — 前端会把 8-27 的拆单/撤单画到今日 K 线. 根因 `_build_events` 用 `bars[-1].date`(今日) 而非 .tck 文件名里的交易日. **修复**: `src/web/api/klines.py` 新增 `_date_from_tck_name(path)` 从 `.tck` 文件名 `sz002361_20260827.tck` 提取 `2026-08-27` 作为事件 date; 新增 `_norm_date(d)` 统一日期格式到 `YYYY-MM-DD` (TQ 给 `20260827` / 东财给 `2026-08-27` / .tck 文件名 `20260827` 三种样式→统一). `tests/test_summary_layer_api.py` 适配 no_bars 新语义 + 2 个新测试. xiaoze 在小主机自验后确认 v0.4.55 验证通过: TQ 自动发现 OK / fund_flow 120 行 / ming_net +8972 万 / 事件日期**待 v0.4.57 修**. pytest 81 passed.
- **v0.4.58 gs_signals date 规范化 (xiaoze 自验 v0.4.57 发现)**. v0.4.57 修了 events / fund_flow 日期统一, **gs_signals 仍返回原始 `20260902`** — 同接口三种日期样式混着, TQ↔东财切换时前端按日期匹配 GS 标记会错位. `src/core/gs_strategy.py` 新增**本地** `_norm_date()` (core 层不反向依赖 api 层同名函数), `compute_gs_signals()` 组装 dates 时过一遍: 8 位数字 → `YYYY-MM-DD` / 规范格式 → 原样 / 空 → None. `tests/test_summary_layer_api.py` +3 例 (norm_date 纯函数 / TQ 样式规范化 / 东财样式不被破坏). xiaoze 自验后给具体计划: 13 passed / 287 回归. 集成后 pytest 84 passed. **附 `docs/TIMEZONES.md` 文档**: 容器日志=UTC+8 / 邮件 created_at=UTC / TQ HqDate=交易日无时区 三口径约定.
- **v0.4.60 Quote.tsx 去卡片化重写**(用户: 不要卡片堆叠 UI). 去 4 Tab → 单页; 去卡片边框 → hairline 分隔; K线 ≥80% 屏宽无卡片包; 决策三问顶部条带 (一行文字); 窄栏紧凑列表 (主力意图/暗盘簇/事件 数字流); 数据明细默认折叠; GS 信号买红卖绿 (用户 override 设计稿 §5.2); 副图切换 (成交量/MACD). tsc 0 error + pnpm build 15.74s. xiaoze 通知 "小改我自己来" 单文件 ≤1 文件原则.
- **v0.4.61 三处紧急修复**(xiaoze 自验 + 用户反馈).
  - **Bug 1: K线 L2/L3/L4 图层不渲染** (用户: 买卖点也没出来). 根因 `toChartTime()` 日K返回字符串 "YYYY-MM-DD" — LC v5 markers/series 必须用 UTCTimestamp (秒数字), 字符串会被 silent drop. 修复: 统一转秒 (日K也用 `new Date().getTime()/1000`). 预期 L2 GS 买卖点 (绿/红 G/S 圆点 + 实心/空心)、L3 资金柱、L4 事件图标 (涨跌停/拆单/撤单/龙虎榜/公告)、支撑压力价位线全部显示.
  - **Bug 2: 单位万元 → 自动万/亿元**(用户: 单位应该是亿元吧). 改 `toWan(v)` → `toAmount(v)`: |v| < 1 亿 → "+X.XX万", |v| >= 1 亿 → "+X.XX亿". Quote.tsx 决策三问数字、窄栏数字、暗盘簇数字、FundDetail 表头全部更新. DarkFundTop.tsx 列标题 "主力净流入(万元)" → "(万/亿)" + 同 `toAmountFromWan` 函数 (输入万元, 自动选单位).
  - **Bug 3: 暗盘 TOP 主力占比显示 2131.3% 异常** (用户截图). 根因: 后端 `main_net_ratio` 字段直接取 thsdk `主力净量` (数值/股), 不是百分比. 前端按百分比渲染就出来 2131.3%. 临时前端修复: 改字段名/列标题 "主力占比" → "主力净量", 显示 `(r.main_net_ratio).toFixed(0)` 不乘 100. 后端字段重命名 + 加注释留 v0.4.62.
  - 验证: tsc 0 error + pnpm build 15.72s. pytest 86 passed (未受影响).
- **v0.4.59 thsdk buffer_size 扩容 (xiaoze 自验 v0.4.58 发现新问题)**. **根因**: `thsdk.tick_super_level1` 当日累计数据 > 2MB, thsdk 默认 socket 接收缓冲区 2MB 不够装, 服务端返 `缓冲区大小不足,当前大小: 2.00 MB,需要调整扩大 buffer_size 接收返回数据` (data=None) —— 暗盘融合被迫降到 tck_only, dark_net 缺口 **17 倍** (1.63 亿 → 939 万). **排除项**: ❌ 配额限制 (无401/429) / ❌ 时段限制 (11:0x 正常 14:1x 故障) / ❌ 个股维度 (002361/600519/000001 三只同错) / ✅ **thsdk 客户端接收缓冲区配置问题**. **修复**: `data_source/thsdk_l2.py` 新增 `THS_BUFFER_SIZE = 8*1024*1024`, `_query` 注入 `buffer_size=THS_BUFFER_SIZE` 到 method 调用; TypeError fallback 兼容旧版 thsdk. **实测**: 8MB → ok data=4877 rows / 16MB → ok 同; 2MB → 失败. **降级机制真扛过**: `coverage: tck_only + fusion_note: thsdk 不可得: 只有通达信主动侧, 被动侧(maker)未落盘, 暗盘不完整` + `main_net` 仍出数不编造. `tests/test_thsdk_buffer_size.py` 2 例 (mock thsdk, 验证 buffer_size 注入正确). pytest 86 passed. xiaoze 提的前端 coverage 角标建议 (fusion/tck_only/thsdk_only 三态) **记入 v0.4.60 候选**, P4 暗盘展示阶段处理.

### feature

- **v0.4.64 wencai 三指标共振选股 3 模板** (28号, Phase 2 入口#1 后端 P0 三件套之 PR-1). `src/core/chat_tools.py` `WENCAI_TEMPLATES` 追加 `三指标买共振`/`三指标卖共振`/`三指标选股共振` 3 模板, 供 AI 助手 `get_stock_screen`/`get_market_scan` 调用同花顺一句话选股. **口径以官方《决策先锋8问8答》为准** (§3.4 七行状态表 / §5 三步战法 / §6 选股条件), **非桌面 5_GZZH.txt 公式** — 甄别发现该公式 2 处与官方不一致: ① 卖共振公式用"活跃度较前日下降", 官方要求"跌破强势线(<3)", 已按官方修正; ② 选股共振公式用"活跃度>6(大牛线)+暗盘单日>0", 官方是"活跃度连续多日强势线上(>3)+暗盘持续买", 已按官方修正. 差异写入各模板 `note` 字段供后续校准. 模板总数 22→25. py_compile 通过 + 模块 import 验证 3 模板结构 (src/q/note) 全对. [commit 42cdf7a]

### feature

- **v0.4.65 resonance 实战文案 API 字段化** (28号, Phase 2 入口#1 后端 P0 三件套之 PR-3). `src/core/resonance.py` 新增 `state_action_label(row)` — 行号→决策先锋 GO/STOP 风格实战文案 (8问8答 §3.1: G信号→"GO: 趋势有望启动, 建议逢低建仓"; S信号→"STOP: 趋势暂或结束, 建议减仓避险"). 返回 `{label, text, tone}`: row1/2→GO(bull), row3→持有(bull), row4/5/6→警惕(warn), row7→STOP(bear), row0/非法→观望(neutral). 纯增量(不动 evaluate_state), 与既有 "action"(7行表操作思路长句) 互补: action=完整思路, label=前端可直渲的短标签+色彩语义. 验证: 8 行映射 + 边界(None/"x"/-1/99→观望) + row1→GO/row7→STOP 联动全过. 前端 K线/机会页可直接用 `tone` 映射红涨绿跌色彩. [commit 090e1f3]

### feature

- **v0.4.66 主力资金 N 日序列 + 0 轴 CROSS** (28号, Phase 2 入口#1 后端 P0 三件套之 PR-2). `src/core/dark_pool_flow.py` 新增 `compute_pool_flow_series(symbol, days, today_overlay)` + `fund_flow_cross(series)` — 决策先锋 1/3/5 日主力资金 + "由绿转红上穿0轴=资金看多"(8问8答 §3.2). **数据约束诚实声明**: thsdk 明盘仅当日可回溯, 历史日明盘不可得 → 历史日序列用暗盘 OHLC 分摊近似(对照项, 方向可靠/幅度有偏差), 全标 `approximation=True` 不冒充完整主力口径(不编造红线); 当日(最后一根)经 `today_overlay=True` 叠加权威 ming+dark(`compute_pool_flow`). `fund_flow_cross`: prev<=0且cur>0→cross_up / prev>=0且cur<0→cross_down, None 跳过不补0, 输出 `{cross_up, cross_down, last_direction(多/空/平), points_used}`. 纯增量不动既有函数. 验证: fund_flow_cross 6 场景(上穿/下穿/等号边界/None跳过/空/多次穿越) + compute_pool_flow_series 3 场景(当日叠加权威/全近似/total汇总) + 空bars→无数据 全过. 注: `market_scan.dark_top` 接真实值留作后续(避免本次动扫描链路). [commit ee2d8b9]

### fix

- **v0.4.67 _fmt_amount 提升模块级修复 F821 NameError 隐患** (28号, v0.4.62 事件残留真 bug). `src/agents/intraday_monitor.py`: `_fmt_amount` 原为两处**嵌套函数**(line 80/179, 各自在 `_main_intent_both_inner` 等内部), 但 line 524/621/622/623/630/633/637/643 等**其它函数**也调用它 → 那些作用域未定义, 运行时触发即 `NameError`(生产 v0.4.63 也带着, 只是未命中那些代码路径). CI `build-and-push-image` test job ruff 门禁(--select E9,F821,F601,F811)拦下, 报 11 处 F821. **修复**: 提升为模块级 `_fmt_amount`(line 27), 加非数值兜底; 原两处嵌套定义保留(等价影子, 最小改动). py_compile + AST 验证模块级定义就位. 注: 此修复与三件套同发, 故 v0.4.66 tag(ee2d8b9, 含 bug)不部署, 直接发 v0.4.67. [commit 559b9fd]

### fix

- **v0.4.68 ws_hub 测试 AsyncMock 修复** (28号, 修 CI 飘红). `tests/test_ws_hub.py::test_l5_03_too_many_connections_close_4402`: 用 `MagicMock()` 模拟 websocket, 但 `ws_notifications_handler` 在鉴权通过后先 `await websocket.accept()`(ws_hub.py:356), `MagicMock.accept()` 返回不可 await 的 MagicMock → `TypeError: object MagicMock can't be used in 'await' expression`. l5_01/l5_02 因在鉴权阶段(4401)先被拒、没走到 accept 才没暴露. **修复**: import `AsyncMock` + 给该测例 `ws.accept = AsyncMock()`。纯测试 mock 修复, 不动生产代码(生产 accept/close 均为真 awaitable). 该测试由 commit 78774cf(xiaoze 通知中心 P0)引入, 非本次改动引入; 此前被 v0.4.66 的 lint 门禁失败掩盖, v0.4.67 lint 通过后才暴露. [commit b67b6c6]

### feature

- **v0.4.69 决策先锋三指标共振接入个股行情页** (28号, 并行子智能体协作). **后端** `src/web/api/klines.py`: summary API 新增 `resonance` 字段 `{available, row, phase, action_label, action_text, tone, bad_count}` — 组装既有三指标(`gs_strategy.trend_label` + `ai_activity` + `dark_pool_flow.compute_pool_flow`)经 `resonance.evaluate_state` + `state_action_label` 得 7 行状态 + GO/STOP 实战文案; 三指标全可得才 `available:True`, 缺任一返安全默认不编造; 双层 try/except 兜底不拖垮 summary。复用已拉 bars, 唯一联网项 compute_pool_flow 受 5min summary 缓存摊销。注: 行2"拐点"因历史明盘不可得(fund_net_prev 恒 None)此路径不可达, 属数据固有约束非 bug。**前端** `frontend/src/pages/Quote.tsx`: 新增 `ResonanceInfo` interface + 窄栏「决策先锋共振」区块(替换原写死操作建议), GO/STOP 主标签按 tone 上色(bull红涨/bear绿跌/warn琥珀/neutral灰, 中国市场红涨绿跌), 无卡片 hairline 分隔终端风。测试 `tests/test_summary_resonance.py` 16 例 + 回归 68 全过, 前端 typecheck 零错误。 [commit 99c05fb]

### update

- **v0.4.70 前端终端化 P0: 涨跌色令牌统一 + 暗色优先 + 圆角收紧 + 金融数字字体** (28号, 并行子智能体协作, 响应老板"像专业行情终端"诉求). **① 涨跌色统一**: 全站 ~120 处硬编码涨跌色(#ef4444/#22c55e/#10b981/text-rose-*/text-emerald-*)收敛到 `--stock-up:#E53935`/`--stock-down:#43A047` 令牌; Tailwind 层用 `text-stock-up/text-stock-down`, 图表 JS 层新增 `packages/biz-ui/src/lib/stock-colors.ts` 运行时 `getComputedStyle` 读 CSS 变量(`readStockColors`/`withAlpha`)。覆盖 K线/分时/资金柱/盈亏/涨跌家数等(27 文件); 残留 59 处均为非涨跌语义(状态/错误/命中/买卖动作徽章)有意保留。**② 暗色优先**: `use-theme.ts` 默认 system→dark(仅影响未设置过的新用户, 尊重已选手动选择), index.html 加首帧防闪脚本; theme-color #4f46e5→#0a0a0f。**③ 圆角收紧**: `--radius` 0.75rem→0.375rem, tailwind 补派生 xl/2xl(卡片 12→8px)。**④ 金融数字字体**: `--font-num` 字体栈置顶 DIN Alternate/Bahnschrift(Windows 不再落代码字体)+`tabular-nums` 等宽, 保守不加 CDN webfont(CSP 限制)。前端 typecheck 零错误。⚠️ 遗留待产品决策: GS 信号配色 Quote(买红卖绿) 与 K线图(G绿S红) 相反, 本次未擅动。 [commit af8521d]

### fix

- **v0.4.71 GS 信号配色统一为 G红/S绿** (28号, 修前端配色矛盾). 此前个股页 `Quote.tsx` GS 是「买红卖绿」(G红/S绿), 但 K 线图 `KlineChart.tsx`/`InteractiveKline.tsx` 的 GS 买卖点标记却是 G绿(#16a34a)/S红(#dc2626), 两者相反, 且违背 A 股红涨绿跌惯例。**统一为 G=红(买入/趋势启动)、S=绿(卖出/趋势结束)**: 两处 markers 改用 `readStockColors()` 的 `sc.up`(红)/`sc.down`(绿), 随亮/暗主题自动切换, 不再硬编码; 实心/空心(已确认/待确认)逻辑保留只换色相; `Quote.tsx` GS_COLOR 本就 G红/S绿 无需改。决策先锋口径: G=GO 趋势启动(红), S=STOP 趋势结束(绿)。前端 typecheck 零错误。 [commit 316211b]

## 2026-09-01

### feature

- **v0.4.53 设计稿 §5 补全: KlineChart 补 L1/L2/L5 三层 + 清死代码** (Hermes 自接). 行情主视图(/forecast) KlineChart 之前只实现了 L3 资金柱 + L4 事件 + 价位线, 比二级页 InteractiveKline 弱. 现:
  - **L1 趋势**: MA5/10/20/60 灰阶 + 牛线(MA5 蓝粗)/马线(MA20 橙粗), 受 `layers.trend` 开关, 关整层清空 series.
  - **L2 GS 买卖点**: 接后端 `summary.gs_signals`(gs_strategy.compute_gs_signals 产出), G=买绿(下方)/S=卖红(上方); 实心=已确认(收盘)/空心=待确认(LC v5 无 circleOutline 形状 → size 区分 + 文字 ○G/○S 前缀).
  - **L5 副图切换 UI**: 成交量(已有 volumeSeries)/MACD(DIF 蓝 + DEA 橙, 前端 computeMacd 自算)/主动买卖比/情绪周期(后两者需实时行情, 真数据待接 → disabled 灰显 tooltip). `subchart` prop + `onSubchartChange` 回调供父组件写 URL.
  - 新增 `LineSeries` import + sma/ema/computeMacd 辅助函数 + `GsSignalPoint`/`KlineSubchart` 类型.
  - `Quote.tsx`: summary 类型加 `gs_signals` + 归一化 → 传 `gsSignals` prop 给 KlineChart. K线图层标注 L0-L5 全链路在行情主视图打通.
  - **死代码清理**: 删除无引用的 `MinuteEChart.tsx`(187 行 ECharts 版分时, 已被 MinuteLwcChart 取代). ECharts 依赖保留(Dashboard 3 个非K线图 SentimentGauge/BreadthDistribution/FlowHistoryChart 在用).
  - 前端 typecheck 0 error + pnpm build 14.42s 全绿; 后端 test_l4_events 26 passed(纯前端改动, 后端未动).

- **v2.1 §11.5 通知送达回执**(端点 + 时间戳落库 + 测试)。`src/core/notifier.py` 每渠道独立记录 `delivered_at`(成功)/`failed_at`(失败) ISO 时间戳。`src/web/api/notifications.py` 新增 `GET /api/notifications/{nid}/status` 端点: 返回 `push_status`(pending/sent/failed/skipped)+ `channels[]` 每渠道独立状态 + `delivered_at`(首个成功渠道时间)+ `created_at`。`tests/test_notification_status.py` 5 例 (单通知状态、404、未授权用户 404、delivered_at 取首个成功、无成功渠道时 None)。验收硬约束: 409/500 防账号探测;返回字段不含 payload;S5 归属校验 (user_id 隔离, 仅看本人 nid)。
- **v2.1 §10 K线大图 → 右栏资金面板联动**（完成 §10 半成品）。`frontend/src/pages/Quote.tsx` 接 KlineChart 两个 props：`onRangeSelect` → 选段时间窗 `selectedRange: {from,to}` → FundPanel 过滤 rows + 区间聚合摘要（明盘/暗盘净额累计）+ 表格只显区间内 row；`onCrosshairMove` → `hoveredDate` → FundPanel 行高亮（背景色 + 蓝色 ring）。日期维度都是 YYYY-MM-DD 字符串可直接字典序比较，无需 Date 解析。Quote.tsx 不需要新依赖（无新 npm）。`pnpm tsc -b` 0 错 + `pnpm build` 15.01s 全绿。
- **v2.1 §11 P0-B 一致性补丁(2026-09-01)**: `src/web/api/datasources.py` create/update/delete 三处路由也加 `_ensure_health_columns(db)` 自愈(xiaoze6096 上封信建议 + 我认可的防御一致性;原本只有 list/get/health/data-sources 三处, v0.4.51 已全绿但 update/delete 在老库上仍可能因缺列崩 500)。`pytest tests/test_datasource_admin_api.py tests/test_datasource_reconcile.py tests/test_datasource_test_path.py` 7 passed。
- **v0.4.52 P1-B 前端暗盘资金 TOP 榜页面**(v0.4.50 后端已接入,前端补齐):
  - `frontend/packages/api/src/marketScan.ts` 新增: `marketScanApi.darkFundTop()` + `refreshDarkFundTop()` + `DarkFundTopSnapshot` / `DarkFundTopUnavailable` / `DarkFundTopRow` 类型(联合类型区分 available:true/false,无快照时显示 note + 手动刷新按钮,不编造榜单)。
  - `frontend/src/pages/DarkFundTop.tsx` 新增: 头部摘要卡(快照日 + universe + computed + TOP + thsdk_dde 来源标识)+ 7 列表格(代码/名称/主力净流入/占比/总成交/.tck 暗盘对照/数据源)+ 链接跳转 quote?type=stock&symbol=。无快照态显示警告 + 立即扫描按钮。代码-单元格用 `<a href="/quote?type=stock&symbol=X">` 直跳行情页。
  - `App.tsx` 加 lazy import + Route `/dark-fund-top` + navItems 「暗盘 TOP」侧栏入口(归「机会」组,复用 `view_opportunities` 权限)。
  - `pnpm tsc -b` 0 错 + `pnpm build` 15.16s 全绿。

### fix

- **生产 500 事故修复（audit fixes + redeploy）**。xiaoze6096 审计发现 `/api/datasources` 报 `UndefinedColumn: last_used_at does not exist` → 设置页整页空白（数据源+AI服务商卡片全不显示），三层根因：①`data_sources` 模型新增 5 个健康列（`last_used_at/last_error_at/success_count/error_count/last_status`）但 `migrations.py` 漏了补列迁移；②`server.py` lifespan 里 `report_scheduler` 变量在 `_leader_ok=False` 或构造异常时未绑定，shutdown 时 `UnboundLocalError`，报告调度器静默未启动；③`Settings.tsx` 用 `Promise.all` 拼单接口，一挂拖垮整页。**集成变更**：①新增 `Migration(126, "datasource_health_columns", _m126_datasource_health_columns)` 幂等补齐 5 列；②`datasources.py` 加 `_ensure_health_columns()` 全局一次自愈，list/get/health 三处调用；③`server.py` lifespan 在 `init_db()` 后加 `report_scheduler = None` 兜底绑定；④`providers.py` AI 服务商 `name` 去首尾空格（线上 `" Agnes 2.5 Flash"` 带前导空格展示错位）；⑤`Settings.tsx` 改 `Promise.allSettled` + 逐接口降级（单接口失败不拖垮整页）。**集成注意**：xiaoze6096 `server.py` 删了 v0.4.47 Playwright 国内镜像兜底 + v0.4.36 WS Hub 绑定/解绑逻辑，整文件覆盖会丢，**仅采纳其新增的 `report_scheduler = None` 一行**，其余保留我仓库版本。xiaoze6096 已临时热补线上 5 列（直接改库），源码修复+正式部署后即固化。

### feature

- **v0.4.50 Phase Y 四块收尾整合 (A6 暗盘资金TOP + A4 拆单簇暗盘 + A1/A5 十档盘口补测)**:
  - **A6 暗盘资金 TOP 全市场扫描**: 新增 `src/core/dark_fund_scan.py` (`scan_dark_fund_top` + `attach_tck_dark`). 用 thsdk DDE **批量**接口(200只/批, 全市场 5223 只约 16s)拿同花顺官方主力净流入, 替代 old `market_scan.scan()` 的 OHLC 分摊对照项. 老实过滤 int32 溢出哨兵值(盘后无真实资金流的次新股返回 2^31-1/2^31 → 剔除不编造成 0). `source="thsdk_dde"` 显式标注不冒充暗盘; .tck 融合对持仓股并列 `tck_dark_net_wan`(委托号级精确暗盘)供对照. `src/web/models.py` 新表 `DarkFundTopSnapshot`(`dark_fund_top_snapshots`). `src/web/api/market_scan.py` 加 GET/POST `/dark-fund-top` + `run_dark_fund_top_job()`. `src/core/report_scheduler.py` 盘后 15:30 cron 三榜+信号摘要后追加暗盘资金 TOP 扫描(独立 try 失败不抛).
  - **A4 get_dark_flow_precise 拆单簇暗盘增强**: `src/core/chat_tools.py` 在原有主笔级还原(active/passive)基础上 hook `postmarket_review.dark_review_from_tck` 的委托号级**五条件拆单簇**识别, 新增 `dark_clusters` 字段(available/dark_net/ming_net/main_net/cancel_rate/active_passive_ratio/cluster_count). 这才是「暗盘资金=对倒拆单/大单拆小单」的正确定义(旧 dark_basis="small_orders" 只是小单口径). 独立 try, 拆单簇失败不影响主笔级.
  - **A1+A5 .img 十档盘口补测**: `tests/test_orderbook_a1.py` 10 例(ImgSnapshot 派生指标 best_bid/spread/bid_pressure/queue_imbalance, img_frame_to_snapshot 同构转换, order_book_queue 托压单形态, 三算法合成数据: evolution/imbalance/ghost_order, get_order_book_queue 工具链路 .img+thsdk 回退+无源降级). thsdk 真实盘口实测: 神剑股份 USZA002361 buy1 10.61/sell1 10.62, bid_pressure 0.482 shape=均衡(盘后静态快照属正常).
  - **诚实口径**: A6 主力净流入 ≠ 暗盘拆单资金, 字段标注 thsdk_dde 不冒充; A4 拆单簇失败独立降级 available=False.
  - **验证**: 新增 19 例(test_dark_fund_scan 6 + test_chat_tools_a4 3 + test_orderbook_a1 10)全绿, (全量回归统计随本轮测试).

### feature

- **v0.4.52 设计稿 §5 K线 4 图层开关真正落地 + Quote 接入** (Hermes 自接). 修复: KlineChart 的 `layersVisible` (4 开关) 原来是死 prop — 声明了但渲染层只按 `kindsVisible` 过滤, 4 开关从未生效. 现 `KlineChart.tsx` 渲染 effect 全量对齐图层语义: `event`(L4 事件 markers) / `signal`(L2 支撑压力价位线) / `capital`(L3 资金柱) / `trend`(L1 预留). Markers 改单一 plugin + `setMarkers()` 整体替换 (原来每次切周期/切开关都堆新 plugin, 旧 marker 残留), 切开关幂等. 价位线先 `removePriceLine` 再重建 (避免虚线累积), 资金柱关层时 `setData([])` 清空. 组件卸载清 markers+priceLines. 前端 `Quote.tsx` 加 4 图层开关 UI (pill toggle, 关层显划线+降透明度) + 传 `layersVisible={layers}` 与 `fundFlow`(资金柱随 L3 开关控制). 前端 typecheck 0 error + pnpm build 16.41s 全绿.
- **v0.4.44 P1 阶段三: KlineChart 资金柱 + 4 开关 + priceLines 过滤**. `KlineChart.tsx` 加 `fundFlow`/`kindsVisible`/`priceLinesVisible` 3 props. HistogramSeries 叠加K线下方 30% (priceScaleId='volume' 独立 scale), 主净分色: 暗盘净正→深红 / 暗盘净负→深绿 / 仅明盘净正→浅红 / 仅明盘净负→浅绿 / 无数据→灰. L4 events 按 `kindsVisible[kind] !== false` 过滤 marker 渲染. 支撑压力位按 priceLinesVisible.{support,pressure} 过滤. typecheck 0 error, pnpm build 16.31s+ 全绿. `frontend/src/pages/Quote.tsx` 移除 InteractiveKline import, 改用 KlineChart; `KlineChart.tsx` 加 `events` + `supportPressure` props (v5 `createSeriesMarkers` plugin 标 L4 事件 + `createPriceLine` 标支撑/压力位, 接 xiaoze6096 的 `klineEvents.ts` 标准化层 KIND_ICON/KIND_LABEL 映射). 首次将 lightweight-charts 打包进产物 (chunk `lightweight-charts.production-BJN___ny.js 177.53 kB / gzip 57.58 kB`). 前端 typecheck 0 error + pnpm build 16.31s 全绿.
- **v0.4.46 设计稿 §7.3 系统信号摘要 5 块注入** (整合 xiaoze6096 8-04 交付). 新增 `src/core/signal_summary.py` (325 行: 5 块聚合 + 渲染 + 落库 + 读快照 + 存量库兜底建表). 5 块 = 情绪周期 + 市场主线 + 三榜 + 涨停复盘 + 指数资金 (拍板: 5块/方案A/≤800字). 每块独立 try/except → data_status 三态 ok/partial/missing, AI 看字段自识别. `src/web/models.py` 追加 SignalSummaryDaily 表 (snapshot_date+stock_market 唯一约束, blocks JSON + text Text). `src/core/report_scheduler.py` 盘后 15:30 cron 顺序: 三榜扫描 → 信号摘要 → 报告生成 (各自独立 try, 互不影响). `src/web/api/chat.py` `_build_ai_messages()` 末尾追加 `read_latest_summary_text(db)` 注入, 无快照/异常静默降级不抛. `tests/test_signal_summary.py` 10 例 (渲染/聚合/快照存在/缺失/三态) 全绿. 整合结果: 10 new passed, 1458 pre-existing passed, 23 pre-existing 失败 (RuntimeError 拒绝默认账号 + 'Depends' has no attribute 'id' — 跟我无关). 等 dockerd 重启 + 容器恢复后部署 v0.4.46.
- **v0.4.47 502 Bad Gateway 真根因三件套固化** (整合 xiaoze6096 在小主机上找到的两层根因). dockerd 没启动只是表象, 底下藏: (1) Playwright 死循环 — `setup_playwright` 装到容器层 /app/.cache/ms-playwright 而运行时 PLAYWRIGHT_BROWSERS_PATH 指向 /app/data/playwright 持久卷, 不一致 → 每次重启重下 15 分钟. 修法: `server.py` 启动检测加 `PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright` 国内镜像兜底 (默认国外 CDN 实测 4-7MB/分钟). (2) 应用监听 127.0.0.1 — `server.py:1987` `_host = os.environ.get("WEB_HOST", "127.0.0.1")`, Docker `-p 8000:8000` 转发到容器 IP 172.19.0.x 时 connection reset 外部全 502. 修法: `docker-compose.yml` panwatch services + `deploy/deploy_panwatch.sh` rebuild_container 都加 `-e WEB_HOST=0.0.0.0`. 容器已用新 env 起好 health 200 全绿, 数据没丢 (43 数据源/DB/Playwright 都在).
- **v0.4.48 §13 持仓上下文卡接入 Quote + §4.4 快捷键导航补齐** (Hermes 自接). 之前 ContextCard.tsx (3 上下文卡: holdings/watchlist/opportunities) 写完但 Quote.tsx 0 引用 — 组件在白板上, 用户看不到. 现: `Quote.tsx` 资金面板顶部按 `?source=` URL 解析后渲染 `<ContextCard source={source} symbol={symbol} market="CN" />`, 无 source → 不渲染 (设计稿默认行为). 资金面板 `FundPanel` 加 3 props (source/symbol/market). 同步补 §4.4 快捷键导航: `desktopNavGroups` 6 项主导航早就有 (驾驶舱/行情/机会/投研/我的/系统), 但 g+{key} 只 2 个 (g+d, g+p). 现: 加 g+m (行情/预测) / g+o (机会) / g+r (投研/报告) / g+u (系统/设置) / g+n (通知) / g+s (影子账户), 共 8 个 g+{key} 全覆盖. 前端 typecheck 0 error + pnpm build 15.71s 全绿. Quote chunk 27.21 kB (含 ContextCard).
- **v0.4.49 §10.2 K线大图交互规范** (Hermes 自接). KlineChart.tsx 加 3 个 v5 API 订阅: (1) `chart.subscribeDblClick()` → `chart.timeScale().fitContent()` 全局还原. (2) `chart.timeScale().subscribeVisibleTimeRangeChange()` 推 `{from, to}` 选段时间给父组件 (v5 API, v4 的 `subscribeSelection` 已移除). (3) `chart.subscribeCrosshairMove()` 推 `{time, price}` 给副图/资金面板联动. 加 2 个可选 props: `onRangeSelect` + `onCrosshairMove` (向后兼容, 不传则纯本地交互). typecheck 0 error + pnpm build 15.08s 全绿. Quote.tsx 暂不消费新 props (等下一轮接入资金面板选段时间聚合 + 副图十字光标联动).
- **v0.4.41 §4.3 三项 Tab 合并补齐** (整合 xiaoze6096 自查派活遗漏). 新增 `frontend/src/pages/ReportsHub.tsx` (报告+历史合并) + `ShadowHub.tsx` (影子+模拟盘合并, 路由级不套 PermGuard, Tab 级各自权限点过滤) + `NotificationsHub.tsx` (通知+提醒合并). `frontend/src/App.tsx` 整覆盖 (62 行 diff: 撤6 个独立懒加载页 → 3 个 Hub 懒加载 + LegacyTabRedirect 重定向旧路由 + 侧边栏瘦身 11 项 + 移动端底栏 /alerts → /notifications). `frontend/src/components/CommandPalette.tsx` 精准 patch (6 行: 4 个旧路径改带 ?tab= 直达 + 删 /alerts 项). `frontend/src/pages/Stocks.tsx` 精准 patch 1 处 (navigate /paper-trading → /shadow?tab=paper). `frontend/src/pages/Dashboard.tsx` 精准 patch 1 处 (同). 前端 typecheck 0 error + pnpm build 14.98s 全绿 (3 个新 chunk: ReportsHub/ShadowHub/NotificationsHub). 教训: 整合被冲掉的交付时应对照原始派活文件核对完整性, 而不是只看最新一封邮件清单.
- **v0.4.40 P1 派活 (阶段一): Lightweight Charts v5 K 线骨架** (装 `lightweight-charts@^5.2.1` + 新增 `frontend/packages/biz-ui/src/components/KlineChart.tsx` 213 行). 蜡烛 + 十字光标 magnet 模式 + 时间区间切换器 (1分/5分/15分/30分/60分/日K/周K/月K, 8 种) + ResizeObserver 自适应宽度 + 暗色主题 (slate 调色) + 红涨绿跌 A 股配色 + 日级 YYYY-MM-DD vs 分钟级 unix time 自动切换 + 缺失数据显式"无数据"占位. **保留 InteractiveKline.tsx 兼容路径**, Quote.tsx 暂不切换 (等阶段二三验证完再切). 前端 typecheck 0 error + pnpm build 17.36s 全绿. v0.4.41 阶段二 (MA60/牛马线/GS) + v0.4.42 阶段三 (资金柱/L4 事件标注/4 开关) 排期.
- **v0.4.39 P0 派活 3: §13 持仓上下文卡** (整合 xiaoze6096). 新增 `frontend/src/components/ContextCard.tsx` (3 种卡片: holdings/watchlist/opportunities, 接口未返字段显式 '--' + 注明原因, 不冒充) + `frontend/src/components/StockContextMenu.tsx` (新增可选 `onOpenQuote` prop, 零侵入其他页面). `frontend/src/pages/Quote.tsx` 整覆盖 (集成 L4 事件图标灰显 useSourceHealth + 解套盘位价位线 + chips 字段 + ?source= 解析). `frontend/src/pages/Stocks.tsx` 精准 patch (3 处: openQuoteFromMenu 回调 + Agent 链接改 /system?tab=agents + onOpenQuote prop). `frontend/src/hooks/useSourceHealth.ts` (cron下载到 out/attachments/xiaoze_p0_2/) + `frontend/packages/api/src/datasources.ts` 加 datasourcesApi.health(). 前端 typecheck 0 error + pnpm build 15.29s 全绿; 后端 82 passed (l4_events + source_health + ws_hub).
- **v0.4.38 L4 修正版整合** (整合 xiaoze6096 #1 修正). 新增 `src/core/l4_events.py` (拆单簇 / 撤单异常 / 龙虎榜 / 公告 / 解套盘位 / 筹码结构 — 用标准 `chip_distribution.compute_near_term_chips` 替代自算). `src/web/api/klines.py` summary 加 chips + unlock_levels 字段; `_build_events` + `_wencai_event_pairs` 拆出 (wencai 加 10s 硬超时护栏). **my_trade 暂缓** (交割单多用户未透传 user_id). test_l4_events.py 重写: 删除 3 例 my_trade + 4 例过时的 unlock_levels(bars,...) 自算版; 新增 3 例 unlock_levels_from_chips(chips) 标准接口版 + 1 例 my_trade_not_exported 占位. 全绿 (25/25 + 56 source_health/ws_hub + 2 skipped).
- **v0.4.36 P0 派活 2: §12 数据源健康检查** (整合 xiaoze6096 派活包). 新增 `src/core/source_health.py` (4 逻辑源探测: tck/img/wencai/shadow, 30s 缓存) + `/api/datasources/health` (4 源) + `/api/datasources/health/data-sources` (通用 data_sources 表累计统计) + `/api/datasources/health/{id}` (单源). data_sources 表加 5 列 (last_used_at/last_error_at/success_count/error_count/last_status). 路由顺序锁定: `/health` → `/health/data-sources` → `/health/{source_id}` → `/{source_id}` (避免 "health" 被 source_id 捕获成 422). 状态四值: connected/degraded/down/unknown. 前端 `useSourceHealth.ts` (60s 轮询) + Quote.tsx 图标灰显对接. 37 例单测 + 验证全量 234 passed.
- **v0.4.36 P0 派活 1: 通知中心 WebSocket Hub** (`src/web/notifications/ws_hub.py` + `src/web/api/ws_notifications.py`). 多用户分发 (按 user_id 隔离) + Redis PubSub 跨进程兜底 + 未读计数 (HINCRBY) + 30s 服务端心跳 + 客户端 subscribe/reset_unread/ack 协议. 端点: `ws://host/api/notifications/ws` (鉴权复用 ws_quotes: Sec-WebSocket-Protocol 优先 + ?token= 兜底). 与 `push_notification` 集成: 落库后 `_after_push_hook` 自动 broadcast + incr_unread (user_id=None 跳过未读). 21 例单测覆盖 7 层 (模块/订阅/广播/未读/端点/集成/PubSub), 全绿 (19 passed, 2 skipped).
- **v0.4.35 生产部署同步**: 同步 git HEAD `2199f07` + `f50c40a` + `45b2914` 到生产小主机(本轮完整覆盖 v2.1 设计稿 4 个补丁章前的全部交付), 含: 设计稿 v2.0 §4.2 可折叠侧边栏(6项主导航) / §4.4 Ctrl+K 全局搜索 / §4.3 行情三合一(Quote.tsx)/ §4.3 设置/审计/帮助 Tab 收纳 / §4.3 系统 Agent/数据源 Tab 收纳 / 设计稿 v2.0 第三章 .img 链路接通(orderbook_engine 五函数)/ 设计稿 v2.0 第七章 2 工具补实(get_dark_flow_precise/get_order_book_queue). 前端 typecheck 0 error + pnpm build 13.7s 全绿; 后端 1342 passed + 57 例新增单测全绿; 推 tag v0.4.35 触发 GitHub Actions ACR 重建

### fix
- **明盘链路断裂修复**: `dark_l2.py` 补全 thsdk_big_order 数据源实现（152行新增），解决 `dark_pool_flow._ming_flow` 调 `fetch_l2_ticks(code, "thsdk_big_order")` 抛 NotImplementedError 导致明盘恒为 None 的问题
  - 新增 `_rows_from_resp()` 兼容 .data/.df 两种 Response 形态
  - 新增 `_query_thsdk()` 带限频/重试/熔断
  - 新增 `_fetch_big_order()` 实现 big_order_flow → 同构 ticks
  - `fetch_l2_ticks()` 路由扩展支持 "thsdk_big_order"
- **测试**: `tests/test_dark_realtime.py` 新增 9 例（路由/格式/过滤/异常），全部通过
- 验收证据: pytest 26 passed（test_dark_pool_flow）+ 9 passed（test_dark_realtime）

### feature

- SIDA Pro 设计稿 v2.0 §4.2 可折叠侧边栏落地: `frontend/src/App.tsx` 桌面端从顶部横排导航改为左侧可折叠侧边栏(6 项主导航竖排, 交易线顶/研究线中/系统沉底), 折叠态持久化 localStorage(`sida_sidebar_collapsed`), 折叠按钮 PanelLeftClose/Open, 展开 `w-60`/折叠 `w-16`(仅图标), 主内容区 `md:pl-64`/`md:pl-20` 自适应, GitHub/日志/通知/头像收纳到侧边栏底部; 移除未用的 `Fragment` 导入. typecheck + build 全绿(13.8s)

- SIDA Pro 设计稿 v2.0 §4.2/§4.3/§4.4 信息架构重构第一步: `frontend/src/App.tsx` 桌面导航从 21 项扁平三组重构为 **6 项主导航**(驾驶舱/行情/机会/投研/我的/系统, 组标签可见), 补齐 `/notifications`(通知)/`/profile`(个人中心)进导航; 新增 `frontend/src/components/CommandPalette.tsx` 全局搜索命令面板(Ctrl+K 打开, 股票模糊搜索 `/stocks/search` 跳 `/analysis/:symbol/:date`, 功能搜索跳页面, ↑↓选择/↵打开/esc关闭, 200ms 防抖). 原 Ctrl+K 占位(打开日志弹窗)替换为真实搜索. typecheck + build 全绿(14.5s)

- SIDA Pro P2 后端数据源五件套 + 图层数据接口整合落地 (xiaoze6096 qwen3.8-max 交付, Hermes 整合): 新增 `src/core/` 十个文件 — `gs_strategy.py`(GS 信号, 收盘定死/末根疑似 pending 与前端实心/空心圆同口径) + `dark_pool_flow.py`(明盘大单 big_order_flow 全口径, 误差<2% 优秀线) + `ohlc_dark.py`(暗盘 L1 近似 OHLC 分摊, approximation 硬标记) + `ai_activity.py`(7 因子 MAX×1.2, 阈值 1.56/3/6) + `resonance.py`(三指标共振状态机, 完整 7 行官方买卖体系 + 口诀) + `tdx_img_parser.py`(.img 十档盘口 + 委托队列 TLV 解析 + 托压单派生, 22 例单测) + `chat_tools.py`(28 号交付, 4 空壳工具接真实源 + user_id 四账号隔离) + `dark_split.py`(拆单识别) + `market_scan.py`(formula 全市场扫描) + `marketdata_authoritative_sources.py`(数据源权威源定位表). `src/web/api/klines.py` summary 接口拓展三字段 `gs_signals`(全量 GS 交叉序列)/`fund_flow`(明盘+暗盘日级净额)/`events`(涨停/跌停, 龙虎榜/公告待 28 号数据源接入). 测试: `test_five_pack/test_ohlc_dark/test_market_scan/test_dark_pool_flow/test_tdx_img_parser`(90 passed) + `test_summary_layer_api/test_chat_tools_p1p2`(18 passed) 共 108 例全绿. 实测 002361 神剑股份 gs_signals 2 个 G 信号, fund_flow ming_net -3,407,363 元(与官方扩展1对齐), dark_net +403,145,519 元(approximation)

- SIDA Pro 设计稿 v2.0 落地 P1: K线 6 层图层化架构 (`packages/biz-ui/src/components/InteractiveKline.tsx`) — L1 趋势(MA5/10/20/60 + 牛线/马线)、L2 GS 买卖点(实心=已确认/空心=待确认)、L3 资金柱(明盘+暗盘)、L4 事件标注(涨停/龙虎榜/公告/拆单簇/⚠撤/🛡托/🔒压/解套/我的买卖 共 10 种图标)、L5 副图切换; 新增 4 按钮图层开关(trend/signal/capital/event, 默认全开), 受 `layers` state 控制, props `gsSignals/fundFlow/events/supportPressure` 全部可选(后端 P2 输出前为空数组, 不报错); 新增类型 `GsSignalPoint/FundFlowBar/KlineEvent/KlineEventKind/SupportPressureLine/LayerState` 均 export, 父组件 `stock-insight-modal` 接入 state+props+summary API 拓展字段 (`gs_signals/fund_flow/events`), `IndexDetail` 复用默认图层; 严格按设计稿 §5.2/§5.3 实现, 防"把疑似当确认"(空心 ○G/○S); 支撑/压力位走虚线价线 (`lineStyle: 2`), 不受 layers 开关影响; build 14s 通过, InteractiveKline bundle 50→63kB(+13kB 合理)

- SIDA Pro 设计稿 v2.0 §4.3 信息架构合并 + 第三章 .img 链路 + 第七章 2工具补实 (xiaoze6096 交付, Hermes 整合): ① 后端 .img 链路接通 — `orderbook_engine.py` 新增 `img_frame_to_snapshot`/`load_snapshots_from_img`/`order_book_queue`/`find_img_file`/`to_ths_code` 五函数(输出与 fetch_snapshot 同构, 三算法零改动), `klines.py` summary 新增 `orderbook` 字段 + thsdk 8秒硬超时护栏(90s→19.6s); ② 2 空壳工具补实 — `chat_tools.py` `get_dark_flow_precise`/`get_order_book_queue` 从"待接入"改真实实现(被动侧 a28/a32 反推标 `partial`, 暗盘标 `small_orders` 不冒充暗盘); ③ 前端 §4.3 — 新增 `PageTabs.tsx`(受控 Tab 栏, 沿用分段控件样式)/`TabbedPage.tsx`(Tab 状态写 URL `?tab=`, 权限过滤, 未挂组件显式"待接入"占位), `Quote.tsx`(行情三合一 个股/指数/板块, 四Tab 分时日K/预测/资金/事件, 板块K线无后端则显式引导不编造), `SettingsHub.tsx`(设置/审计/帮助), `System.tsx`(Agent/数据源), 旧路由 /agents /datasources /audit /help 全部 `LegacyTabRedirect` 重定向保留. 验证: 前端 typecheck + build 14s 全绿, 后端 57 例新增单测全绿

### fix

- InteractiveKline 分钟模式 ref 初始 prev 同步 (`minuteRef.current = { pts: [], prev: minutePrevClose }` 立即赋值, 避免 mount 阶段 race, 原写法初始 prev=null 被立即覆盖但易让 lint 误判)
- InteractiveKline 主力意图徽标亮色对比度修复(rose-400→rose-700+dark:400, WCAG AA 4.5:1 达标)

## 2026-08-31

### fix

- 骨架3坑修复(双方言 SQLite/PG 兼容 + 缺初始化脚本): ①`src/web/database.py` `init_db()` 延迟 `import src.web.models`(避循环依赖, 触发 ORM 注册到 Base.metadata, 否则 `create_all` 不建 users 表 → ALTER users ADD COLUMN nickname 失败); ②`src/web/migrations.py` `_m111_strategy_layer` 10 处 PG 方言 `ec.strategy_tags::text` → `CAST(ec.strategy_tags AS TEXT)`(SQLite 不识别 `::` 转换); ③`_m111` 两段 `INSERT...ON CONFLICT DO NOTHING` 加 `_dialect_is_pg` 分支(PG 保留, SQLite 改 `INSERT OR IGNORE INTO`); ④新增 `scripts/init_db.py` 一键初始化(create_all + 跑全部 m101..m125 迁移, 双方言, 幂等)。实测 SQLite 路径 56 表全建/迁移 apply

## 2026-08-31

### feature

- 通达信 TQ formula 引擎接入 + `_TQ_URL` 修复: `vendors/tq.py` 新增 `formula_mul`(formula_process_mul_zb 批量指标公式) + `formula_zb_single`(单只, 依赖客户端打开数据); `core/marketdata_client.py` 新增 `md_formula_mul` + `md_main_flow_zljc`(ZLJC 主力进出三档 jcl/jcm/jcs)。周期参数须 stock_period+periodstr 同传(缺 periodstr 报 periodstr error)。内置公式: MACD/ZLJC/ZJL。L2_AMO 是公式函数非独立公式, 需客户端公式管理器自定义后按名调。同时修复 `_TQ_URL` 硬编码旧 frps 地址 `172.18.0.1:5100`(已不通) → 改读 `TDX_QUANT_URL` 环境变量(生产容器注入 `172.27.16.1:17709` 直连), 兜底旧地址
- 通达信 .tck 逐笔解析器落地 + 暗盘双数据源框架: 新增 `src/core/tdx_tick_parser.py`(36字节委托号级解析, 官方方向 2B主买/2S主卖 + 主动侧委托号 a28/a32 + 撤单 0C) + `src/core/dark_l2.py`(fetch_l2_ticks 接入层)。dark_flow 通过 `PANWATCH_DARK_SOURCE=tdx_tck` 切换: .tck 文件存在用官方方向(盘后精确), 找不到自动回退腾讯逐笔(盘中实时兜底)。官方方向比腾讯自解析方向准(交易所级标记), 修正暗盘方向误差。局限: .tck 仅主动侧(被动 maker 未落盘), 盘后数据(超盘回放落盘 zst_cache)

### fix

- 拆单识别重写(暗盘对齐同花顺, 两次修复): ①`_classify_split` 改位置主判据(获利区卖=主力派发、套牢区买=主力抄底), 补"散户追涨/散户割肉"两类 + `TestClassifySplit` 6 例。②`_detect_split_orders` 重写为"时间间隔聚类(gap10s+window90s)+全簇累计暗盘流入/流出", 修复方向反+漏检90%(金健米业 600127 同花顺暗盘流入8亿 vs 我们净流出358万 → 修复后暗盘流入10.2亿/净流入+4.5亿, 方向对齐量级125倍)。根因: 同花顺暗盘=所有拆单簇(买簇=流入/卖簇=流出), 不分主力/散户; 原逻辑"连续同方向5-100万"在涨停股成交密集夹杂反向/超范围单时 seq 频繁被打断漏检90%+, 且把获利区买入误判散户排除在暗盘外。contrarian/reason 降级为意图展示属性(不再决定是否计入暗盘)

## 2026-08-30

### feature

- 决策先锋三指标(GS策略 + AI机构活跃度 + L2主力净流入, 盘中实时): src/core/decision_pioneer.py 新增 AI机构活跃度(7因子MAX×1.2零调参, 阈值1.56/3/6, 连强天数+5日均值) + GS策略(BB0慢线/A0快线交叉G买S卖) + L2主力净流入(TQ get_more_info Zjl_HB, 明盘口径=同花顺主力净额); 后端 /api/decision-pioneer/{symbol} 端点 + /api/dark-flow 增 l2 字段; 前端 DecisionPioneerCard 卡片挂分时图下方; AI助手 get_decision_pioneer 工具; 盘中监测推送增"决策先锋三指标"段
- 决策先锋选股池(三指标共振扫描, 盘中实时): src/web/api/stock_pool.py 新增 POST /api/stock-pool/screen(批量算GS+机构活跃度+L2净流入, 按共振强度排序); 前端机会页新增"选股池"Tab
- K线缓存表迁移(_m125): klines 表自动建表(优先 TimescaleDB hypertable, 无扩展降级普通表), 恢复 PG 直读支撑机会页全盘扫描; 生产 PG 换 timescale/timescaledb:latest-pg16 镜像
- 暗盘资金前端展示: 拆单识别结果(主力伪装的中小单, 逆势+位置确认)从 compute_dark_flow 暴露到 /api/dark-flow 响应(新增 dark_order 字段), 前端 DarkFlowCards 新增「暗盘资金(拆单识别)」卡片(暗盘净额+疑似主力买/卖+散户顺势/解套+拆单组明细top5)

### fix

- 决策先锋 L2 主力净流入单位修复: 通达信 get_more_info 的 Zjl_HB 单位为万元(与成交额 Amount 同量纲), 此前 _l2_summary 误当元返回致前端/推送显示小 1 万倍; 现统一转元返回
- 修正三指标口径: L2 Zjl_HB 是「主力净流入」(明盘口径, 同花顺主力净额), 非「暗盘资金」; 暗盘=拆单识别走 dark_flow(腾讯逐笔+.tck)。修正 decision_pioneer.py/intraday_monitor.py/darkflow.py/DecisionPioneerCard.tsx 中"对齐同花顺暗盘"的错误注释

### update

- 决策先锋三指标 GS 定位调整: GS 从"买卖触发"降级为"趋势过滤"(知乎第三方实测 GS 买卖点滞后一天, 日线均线交叉天然右侧滞后); 共振判定改为 GS 只做 S区过滤 + 活跃度/L2资金打分; 前端 GS 卡片改"趋势过滤"标注

### fix

- 日K线不显示修复: klines 表缺失走 fallback 联网源时 date 返回 '20260828'(8位无横杠), 前端 parseBusinessDay 正则要求 'YYYY-MM-DD' → 全部过滤空白; 后端 _fmt_date 统一格式 + 前端正则兼容 8 位

## 2026-08-29

### feature

- 通达信 L2 暗盘资金接入口(逐笔还原+十档盘口+自建分档, 盘后 ZCode TQ4 采集): types.py 新增 DarkFlowTq 强类型 + marketdata_client.md_dark_flow_tq 读取 DATA_DIR/darkflow/*.json + /quotes/{symbol}/dark-flow-tq 端点 + 前端暗盘资金卡片(超大/大/中/小单净额+拆单委托+撤单比+托盘/压盘/锁盘), 盘后数据无文件时静默降级

## 2026-08-27

### fix

- 盘中监测等 Agent 推送渠道失效: `notify_task_done` 无 user_id 上下文时兜底推 owner(role=owner)。多用户改造后 notify_channels 全归属 user_id、无全局渠道，此前 agent 后台任务收尾通知调 `push_notification` 未传 user_id → `_build_notifier(None)` 查不到渠道 → 站内通知永远 skipped("未配置通知渠道")、不外发，用户收不到盘中监测等 Agent 的信号/完成通知

## 2026-08-26

### feature

- 通达信 TQ 数据源回归开源仓库(撤销"移出私有维护"): tq.py 重新入 git, server.py/registry 重新注册 TQ 为 quote/kline 主源(priority 0), 隧道断开自动降级腾讯/东财
- MoreInfo 扩展指标透传(104字段): types.py 新增 MoreInfo 强类型 + TqMoreInfoVendor + /quotes/{symbol}/more-info 端点 + 前端洞察卡片(封成比/封单额/撤单量/逐笔笔数等)
- L2 主力净额字段: MoreInfo 新增 zjl(主买净额)/zjl_hb(主力净流入), 对齐同花顺口径, 前端卡片展示
- 通达信本地数据解析器 src/collectors/tdx_local_parser.py: 北向分券商持仓(signals_sys_*.dat) + 概念归属(extern_sys.txt), 含 11 单测
- L2AMO 分档资金 scripts/l2_amo_formula.py: 系统内置 ZJBY 公式输出超/大/中/小 4 档净额, 与 get_more_info Zjl_HB 精确对齐(误差<0.01)

### fix

- 多用户隔离修复(S5, P0×5+P1×2, 4 账号并存下跨账号读/删数据):
  - agents.py 深度分析三端点(latest/analysis/pdf)与 run 历史/进度补 user_id 过滤(NULL 兼容), trace_id 归属校验防枚举他人运行详情
  - 建议池读端全链路隔离(suggestion_pool get_suggestions_for_stock/get_latest_suggestions 子查询按用户圈定; suggestions.py 三端点注入 user), 非本人建议不下发 prompt_context/ai_response, cleanup 限本人范围
  - chat.py 四个上下文 helper(自选/个股/持仓/推荐问题)与 tool 调用链(_execute_tool/_run_tool_loop/_run_tool_loop_stream)全传 user, 只注入本人持仓/建议/报告/通知/自选
  - accounts.py delete_account 补归属校验(唯一漏网的写操作, 越权返回 404 防账号探测); dashboard.py overview insights/get_brief 按 user 过滤报告
  - 写端补 user_id 归属: tradingagents agent save_analysis/save_suggestion 从 context.user 提取, intraday scan save_suggestion 同步修复(防止建议写到 NULL 共享池被他人读到 Prompt/AI 原文)
  - 顺手修 strategies.py apply 的 amount 字段恒为 None(行情对象字段名是 turnover, 改 getf('amount') or getf('turnover'))
- 修复 migration boolean 类型 bug: agent_configs.visible/enabled 与 users.is_active 用 0/1 导致 PG 严格类型启动崩溃(SQLite 宽松不报错), 改 false/true
- 修复 migration SQLite→PG 系统性兼容(生产临时库实测 101-124 全跑通): AUTOINCREMENT→SERIAL PRIMARY KEY(19处)、DATETIME→TIMESTAMP(31处)、INSERT OR IGNORE→INSERT...ON CONFLICT DO NOTHING(5处)、boolean 列 INTEGER→BOOLEAN+seed 0/1→True/False、json 列 strategy_tags LIKE 加 ::text 转型(10处)+TEXT→JSON(3处)、_m117 try/except ALTER→_add_column_if_missing(PG 失败会 abort transaction)、_m124 PG 分支加 DROP CONSTRAINT IF EXISTS 幂等
- 修复 migration 数据回填缺列+OR REPLACE 语法(有数据临时库实测回填通过): entry_candidates 回填补 candidate_source(NOT NULL 违反)、建表 SQL 补 candidate_source/strategy_tags/is_holding_snapshot/plan_quality 列+evidence/plan/meta TEXT→JSON、INSERT OR REPLACE→INSERT...ON CONFLICT DO UPDATE(2处)
- 技术性修复(5+1 评审 B 轨, 4 项):
  - migrations.py run_versioned_migrations 失败记录事务毒化: PG 下 runner 抛异常后同事务写 success=0 必报 current transaction is aborted, 失败记录写不进/原始错误被掩盖/启动持续失败 → 失败记录改用新连接新事务 INSERT...ON CONFLICT 写入后 re-raise 原始异常(幂等语义不变)
  - database.py 四处 last_insert_rowid()(SQLite 专属, PG 下报 function does not exist 启动即崩) → 新增 _insert_returning_id 方言无关助手: PG 走 INSERT...RETURNING id, SQLite 保留 last_insert_rowid
  - kline_backfill_scheduler.py schedule_one_off: APScheduler 兜底块原错误缩进嵌在 except 内(只在 Stream publish 抛异常时触发, 实际是死代码)→ 移到正常流程无条件调度; 删除死字符串字面量; Stream 发布保留作未来 worker 预留
  - quote_stream.py WebSocket 订阅泄漏: 删除重复的 accept+subscribe 块(复制粘贴残留, 二次 accept 必抛且首次订阅队列永不退订, 每次断连泄漏一个队列); send 失败区分 WebSocketDisconnect 与发送异常; _ensure_aggregator check-then-set 加 threading.Lock 防并发首连起双聚合器线程

### update

- TQ 数据源(vendors/tq.py + registry/seed 注册)移出开源仓库,转私有维护(依赖个人小主机通达信网关,他人无法复现)。生产环境保留:文件仍在生产容器与本地工作区,后续 TQ 迭代只在私有副本进行,不再进 git。仓库内 quote/kline 路由回到 腾讯→ths→新浪/智兔 主链。

## 2026-08-25

### feature

- 交易策略新增3(情绪周期自适应/事件预期差/暗盘资金跟随): `panwatch_strategies.yaml` + `strategy_catalog.py` 注册，11个孤岛模块首次接入策略引擎
- TimesFM(Google)接入替代Lag-Llama: `forecast_models.py` 新增 `timesfm_predict` + 5模型投票(`model_weights.py` 0.35/0.25/0.20/0.15/0.05)，`forecast_server.py` 全链路打通，`forecast_requirements.txt` +timesfm
- DarkFlowCard前端: `DarkFlowCard.tsx` 主力意图+内外盘占比+七口诀标签，背离时⚠️提示+「咨询AI助手」按钮

### fix

- 暗盘七口诀全修(`dark_flow.py`): ⑥永不触发→缩量+震荡，⑦恒真→失衡+不动+放量，③④追加量能确认，抽公共常量防漂移
- AI全局熔断(`ai_client.py`): 新增 `GlobalLLMCircuitBreaker` 按service分桶(10/min+600s冷却)，三方法入口限流+429优雅降级，指数退避
- K线/资金流TTL收紧: `kline_collector.py` 180→60s(集合竞价15s)，`capital_flow_collector.py` 600→120s，区分竞价/连续竞价
- 方向预测摘除: `chat.py` get_forecast加31.7%不可靠警告+去suggested_questions引流，`daily_report.txt` 明日关注→证伪清单
- ECharts主题注册: `echarts-core.ts` 注册SIDA双态主题，`echarts-theme.ts` CSS变量双态，`useECharts` hook+ResizeObserver，4图表白屏修复


## 2026-08-25

### update

- 首页 UI 重构(v0.4.11): 信息优先级重排——「今日要紧事+组合体检+机会精选」工作台上提到
  大盘指标带之后(盯盘系统先看自己的票, 大盘级情绪/主线/资金流/板块榜下沉); 涨跌红绿三套色值
  统一到 stock.up/down token(#E53935红涨/#43A047绿跌, 清理 red-600/emerald/rose 硬编码);
  KPI带跌停无数据源显式标注「暂无」; 机会精选满格评分进度条改为详情箭头; 组合体检无持仓空态
  紧凑化+去添加持仓引导

## 2026-08-25

### feature

- K线盘前预缓存(v0.4.10): 工作日 09:20 主动增量入库「自选+候选池当日」全部标的
  (kline_precache.py, 复用 ingest_symbol 幂等入库), 开盘后自选页/机会页/预测/AI
  直接命中 PG 缓存, 对外请求数砍约80%, 从源头降低触发数据源风控的概率


## 2026-08-25

### fix

- /api/klines PG 优先路径加最小条数校验(<30根视为无效, 继续走联网+新浪兜底) —
  修复新加股回填失败时 PG 只有2根也直接命中返回的问题(v0.4.9.1 兜底因此没触发)


## 2026-08-25

### fix

- 个股K线新浪直拉兜底(v0.4.9.1): 新自选股(有研新材600206等5只)PG 仅6行且
  800天补数因腾讯501+东财断连全失败 → K线只显示两天。KlineCollector 在
  PG兜底之后追加新浪 CN_MarketData 日K直拉(容器内实测可达且含当日数据),
  /api/klines 与 summary 链路立即恢复完整历史


## 2026-08-25

### fix

- AI 反证层 429 风暴根治(v0.4.9, 五连修):
  ① 全局令牌桶限速 ≤10 次/分钟 + 撞 429 后 10 分钟全局面板冷却(期间反证直接静默降级)
  ② AIClient 关闭 SDK 自动重试(max_retries=0), 消灭 retry 放大
  ③ 反证结果按 (股票,日期) 写 biz_cache(TTL 6h), 同股一天只评一次
  ④ _run_coro 改进程级复用后台事件循环(run_coroutine_threadsafe),
     修 "no running event loop"/"Event loop is closed"(旧实现每次新建+close,
     AsyncOpenAI 客户端持有旧 loop 引用)
  ⑤ /api/quotes/ws 加入限流豁免列表 — WebSocket 行情轮询被自家限流挡(429 重连风暴)


## 2026-08-25

### fix

- 自选页技术指标全部显示"观望"修复: summary 接口冷启动(主力意图逐笔翻页)20-30s,
  前端默认 20s 超时导致首轮大面积 abort → kline=null → 徽章回落"观望",
  点进弹窗单只重拉时后端已有缓存才有真数据。双修:
  ① 前端自选页 K线摘要拉取显式 timeoutMs=45s
  ② 后端 summary 进程内缓存 TTL 30s→5min(冷启动重算太贵, 指标分钟级刷新足够)


## 2026-08-25

### update

- ECharts 按需加载(v0.4.8): 新增 lib/echarts-core.ts 统一按需注册
  (Bar/Gauge/Line/Candlestick/Heatmap + 常用组件 + CanvasRenderer),
  全部图表组件切换至该入口, Dashboard chunk 减重约 300KB
- 新增「终端感」ECharts 主题(lib/echarts-theme.ts): 轴线/分割线/tooltip
  对齐设计 token, 四张大盘图 + 分时图统一观感; tooltip 毛玻璃圆角


## 2026-08-25

### fix

- push2delay 每页上限100条 → 改分页拉全A(140页保护), 修复涨跌分布只统计到100只


## 2026-08-25

### fix

- 涨跌分布数据源再修: 新浪对容器 IP 弹瑞数风控验证页(宿主机可达但容器不可达) →
  东财 push2delay.eastmoney.com(延迟行情域)实测容器内 200 可达, 改为主源
  (15分钟延迟对统计图无影响), 原新浪/东财push2 链路保留为兜底


## 2026-08-25

### fix

- 涨跌分布数据源修复: 东财 push2 clist 在生产云 IP 断连 → 新浪 Market_Center 主源
  (分页拉全A ~68页, 60ms/页防限流), 东财降为兜底; 合理下限1000只校验


## 2026-08-25

### feature

- 首页大盘区动态图表化(v0.4.7, 多智能体协作: Codex 后端 + Hermes 前端):
  ① 涨跌分布双向柱: 全A 9档分桶(东财 clist 单页5000行, 60s biz_cache), 左绿右红 ECharts
  ② 市场温度仪表盘: 高度×15+晋级率×40+封板率×45 半圆 gauge, 指针色随情绪阶段
  ③ 主力净流入日内面积图: market_flow_snapshots 每30s快照落表(PG), /history 接口4h回溯, 30s轮询
  ④ 资金流入/流出板块改横向条形榜(宽度按占比动画过渡)
  ⑤ mainline 主线榜新增 rank_change 昨日排名变动(mainline_rank_daily 快照表)
  ⑥ KPI带: 「市场体检」占位格换「涨停/跌停+封板率」; 数值 count-up 滚动动画

### update

- 移除鸡肋: 首页热榜整块(与发现页重复)/盘前盘后简报卡(与报告中心重复);
  组合体检双分享按钮合并为「分享▾」下拉


## 2026-08-25

### fix

- K线采集器新增 PG hypertable 兜底(v0.4.6.3): 腾讯风控(501)+东财被掐+智兔429 全挂时,
  KlineCollector._fetch_all_sources 回落读 PG klines 本地缓存(与 /api/klines 同源),
  修复个股弹窗 summary"无K线数据"→ 技术指标区消失的问题。fail-soft, 不影响正常链路。


## 2026-08-25

### fix

- /api/klines 指数分支新浪兜底未生效修复: 腾讯被风控时 market_get 抛异常早于兜底判断,
  现单独捕获异常后再走新浪回落; 兜底失败报错文案改为"腾讯+新浪均失败"


## 2026-08-25

### fix

- 指数K线生产不可用修复: 腾讯 ifzq fqkline 对生产云服务器 IP 风控(501 Not Implemented),
  东财 push2his 同样被掐 → 首页指数 sparkline 与大盘详情页K线全空。
  新增新浪指数日K兜底(CN_MarketData.getKLineData, 生产实测可达):
  ① marketdata.index_klines 腾讯空后回落 fetch_sina_index_kline
  ② /api/klines 指数分支腾讯空 bars 后同源兜底(A股 sh/sz 前缀)


## 2026-08-25

### feature

- 首页信息架构重排(借鉴 TSP tick-stock-panel, MIT):
  ① 新增 KPI 带: 涨/跌家数·主力净流入·两市成交额·情绪周期阶段·主线Top1 六格数字优先布局
  ② 情绪周期卡+主线条形榜上移 C 位(指数条之后)
  ③ 最新报告从首页顶部整卡降级为工作台右栏紧凑列表
  ④ 阶段时间线 30 天→120 个交易日(recent_days 字段, recent_30d 兼容保留)
  ⑤ 后端新增 register_cron: 工作日 15:10 自动同步涨停池指标落库(此前需手动 sync)


## 2026-08-25

### fix

- 主线识别: wudao theme='无' 时回落 sector 字段,修复主线条榜为空


## 2026-08-24

### feature

- **市场环境三件套(借鉴 tick-stock-panel 设计,MIT)**:
  ① 情绪周期6阶段: 连板梯队指标驱动(高度/宽度/晋级率/封板率/首板数),阈值取自
  TSP 2020-2026 分位数标定,EMA平滑+2日确认防频繁切换,大盘<-2%弱档否决;
  每日落库 market_phase_daily,Dashboard 展示当前阶段+30天时间线
  ② 主线识别: 按概念聚合涨停家数/最高板/梯队档位数/二板宽度 rank 归一加权打分,
  宽基标签过滤,涨停<3家不入榜;Dashboard Top10 主线条形榜+龙头股
  ③ 异动接近度预警: 交易所异常波动规则口径(主板3日±20%/创业板±30%/10日+100%/
  30日+200%,负向更严),偏离值=个股-基准指数,接近度>=1触发/0.7边缘/0.5观察;
  机会页新增"异动预警"Tab

## 2026-08-24

### feature

- **盘前分析接入亚太市场 + 美股股指期货**: 新增 `src/core/global_indices.py`(yahoo
  finance 免费源,无 key,5min 进程内缓存),采集 日经225/韩国KOSPI/台湾加权/恒生指数 +
  纳指100/道指/标普期货实时报价 + 美股三大指数。盘前 agent 的 prompt 新增
  "亚太市场与隔夜衍生品"模块,并注明口径(日韩台早盘是 A 股情绪前导,期货反映隔夜消息面)。
  实测 10/10 指数全部有数据。

## 2026-08-24

### fix

- **竞价异动池 gap_pct/withdraw_rate 推导口径二次修正**: 任务2实测再次确认 thsdk `call_auction_anomaly` 返回的「价格」列**不是价格**,而是异动幅度小数比例;「总金额」列恒为 2147483648 (int32 上限占位垃圾)。v0.3.1 旧版用 `(价格/昨收-1)*100` 反推 gap_pct 是错误假设。修正:删除 `_compute_gap_pct`/`_batch_prev_close` + klines 昨收依赖,改用「异动类型 + 价格列」直接推导 — 急速涨跌/大幅高低开 → `gap_pct = 价格×100`; 涨停/跌停试盘 → 价格=1.0 占位无信息 → `gap_pct=None`; 涨停/跌停撤单 → `withdraw_rate = 价格×100`(撤单率 0.5~0.9 区间);其他类型兜底 `|价格|<0.21` 按涨跌幅处理。`MISSING_FIELDS` 收紧到仅 `[volume_ratio]` (withdraw_rate 已部分填充,不再 always-missing)。前端 `AuctionAnomalyTab.tsx` 无大改(对 None 显 '—' 逻辑保留)。附 26 个新单测覆盖各类型推导 + 边界条件。
- **机会页候选股 K线只显示一天**: 18:00 K线增量 cron 的 `get_default_symbols()` 原来只读
  自选股,候选池(market_scan 等)入池的票无 800 天历史回填 → PG 缓存只有当天 1 根K线。
  现在 `get_default_symbols()` 并入 `entry_candidates` 当日(CST) distinct 股票,与自选股
  合并去重;新增 `_today_cst()` helper 防 UTC 时区跨日。附 8 个单测。
- **竞价异动池 竞价涨幅/撤单率/量比 不显示**: 实测确认 thsdk `call_auction_anomaly` 的
  "价格"列并非价格,而是随异动类型变化的幅度小数: 急速上涨/下跌、大幅高开/低开 = 涨跌幅
  比例(0.0523=+5.23%)→ 直接 ×100 作为 gap_pct;涨停撤单/跌停撤单 = 撤单率(0.5~0.9)→
  填入 withdraw_rate;涨停/跌停试盘恒为 1.0 占位 → gap_pct 置 None。"总金额"列恒为
  int32 上限 2147483648,识别为占位垃圾跳过。volume_ratio 数据源确实不提供,API 响应
  missing_fields 如实标注,前端显示 "—" + tooltip。附 26 个新单测(真实口径 mock)。
- **同一股票多条竞价异动去重策略**: 同一股可能先出现"涨停试盘"(占位无信息)后又出现
  "涨停撤单"(含撤单率),原去重逻辑保留首条导致撤单率丢失。现在按信息量保留:
  撤单 > 其他类型 > 试盘。

## 2026-08-24

### update

- **移除恒生聚源 DDE 第三数据源（主力意图收敛回双源对比）**: 主力意图一致性比对
  从三源（腾讯逐笔 vs 同花顺 L2 vs 恒生 DDE）收敛回双源（腾讯逐笔 vs 同花顺 L2）。
  删除 `src/core/hengsheng_client.py`、`src/core/hengsheng_fund_flow.py` 及相关测试；
  `main_flow_compare.py` 三源 min-pairwise 一致性改回双源 `_consistency`，移除
  hengsheng/dde_ratio/rising_up_days 字段；`chat.py` 的 `get_main_flow_compare` 工具、
  `main_flow.py` API、`startup_check.py` 自检项同步移除恒生引用。前端零改动
  （双源对比卡本就只渲染腾讯 + 同花顺两列）。

## 2026-08-24

### doc

- **README/仓库 meta 突出"缝合怪"定位 + meta 刷新**: 中英双语 README 在开头新增诚实定位
  blockquote(缝合市面最强开源量化项目,整合本身就是产品);GitHub About 描述重写为
  "stitches the best open-source quant projects into one pipeline";homepage 从镜像
  仓库页改为在线演示站;topics 换入 multi-agent/llm/foundation-models/
  time-series-forecasting/tradingagents/kronos/stock-prediction,移除 panwatch/cron/
  capital-flow/wecom(20 上限精选)。

## 2026-08-24

### doc

- **README 全面重写（中英双语同步）**: 新增「开源技术整合」板块(TradingAgents/Kronos/Chronos-Bolt/
  XGBoost/TA-Lib/TimescaleDB/Redis/Lightweight Charts/Grafana 全家桶等 13+ 项目,全部真实接入),
  突出多智能体分析、基础模型预测闭环、三引擎共振等亮点;截图区扩为 6 张(新增 K线主力意图/AI对话/
  模拟盘);修正过时技术栈(SQLite→PostgreSQL+TimescaleDB、ECharts→TradingView Lightweight Charts);
  版本徽章 v0.2.41→v0.4.3;镜像 tag 同步 v0.4.3;新增功能总览表(8 大模块);架构图更新
  (TradingAgents/验证闭环/基础设施栈)。

## 2026-08-24

### fix

- **时区 +8 小时偏移（全站时间显示错误）**: SQLite→PG 迁移后，PG 的
  `timestamp without time zone` 列 + func.now()（PG timezone=Asia/Shanghai）
  存的是北京 naive 时间，但读取侧仍按旧 SQLite 口径 `replace(tzinfo=timezone.utc)`
  把 naive 当 UTC 再转回北京 → 所有时间超前 8 小时（盘中监测 09:27 显示成 17:27）。
  `timezone.py` 新增 `_db_naive_tz()` 按 DB 方言解读 naive（SQLite=UTC、PG=北京），
  修 to_beijing/to_iso_with_tz/to_utc 并新增 `format_app_tz`；修 10+ 处序列化/比较点
  （建议池、history/dashboard/agents/logs/context/price_alerts/paper_trading 的
  _format_datetime，以及 price_alert_engine/paper_trading_engine/entry_candidates/
  strategy_engine 的 naive 比较）；更新 test_timezone.py 覆盖 SQLite/PG 双方言断言。

## 2026-08-23

### 判断准确性大修(P1-P4)

- **P1 后验样本存活**: 候选池由"每日 3 次全量删除重建"改幂等 upsert + 消失候选标
  `retired`(信号层同理标 `inactive`), 候选 ID 稳定 → 后验 Outcome/因子快照不再被
  FK CASCADE 连坐删, 盘中真实信号进入 1/3/5/10 日胜率与因子标定闭环(修幸存者偏差)
- **P2 共振加分接线**: `_score_suggestion` 此前读 ORM 对象 meta 从未生效、
  `_score_market_scan_candidate` 完全没读 — 🔥 共振现在真正参与候选排序
- **P3 策略口径**: 字段缺失=不通过(防"无量能的放量策略"裸筛); reversal 因子方向
  修正(企稳高分); low_pe 对 PE<3 异常封顶; 策略描述对齐实现
- **P4 主力意图物理守卫**: 主力成交额 > 总成交额 130% 盘中即标 `suspect` 并拒判
  吸筹/派发(2026-08 两次净额翻倍事故的实时拦截, 下游与 insufficient 同款跳过)

### 系统质量(Q1-Q4)

- **Q1 调度器选主**: Redis 租约防多 uvicorn worker 双跑定时任务(LLM 费用翻倍/通知
  重复/撮合双份), `SIDA_ENABLE_SCHEDULERS=1/0` 可强制, Redis 不可用回退旧行为
- **Q2 Secrets**: Grafana 密码变量化(不再入仓); `admin/admin123` 兜底需
  `AUTH_ALLOW_DEFAULT_ADMIN=1`(生产未配置则拒绝创建默认账号)
- **Q3 超时+lint**: marketdata per-vendor 8s 超时(坏源不拖垮主备链); CI 加 ruff
  门禁(真 bug 类); 存量修复 intraday 主力意图 MDSymbol 未导入(该路径一直静默
  返回空)、delta_engine f-string、kline 重复键、redis_client 双定义等
- **Q4 备份+告警**: scripts/backup_pg.sh(pg_dump+滚动保留); Prometheus 告警规则
  (5xx率/心跳/数据源失败/Redis 降级)

### 体验(U1-U2) + 接线(F1)

- **U1 对话真流式**: `chat_with_tools_stream` 单次调用边流式出字边执行工具,
  SSE 端点替换 6字/4ms 假打字机; 非流式路径不变
- **U2 前端**: 定义 `.page-container` + 去三处双倍留白; Dashboard 涨跌色收敛到
  `stock.up/down` token; 帮助页机会板块重写(共振查询/统一筛选/双策略口径)
- **F1 接线死件**: `sentiment_cycle` 情绪周期注册为对话工具 `get_sentiment_cycle`;
  auction_review/theme_launch_detector/stock_attribution 补种子(默认关)

### fix — 共振查询策略精筛不再重调引擎(结果缓存 + 切换即时精筛)

- 问题: 并发查询后切换精筛策略需要重新点「并发查询」, 问小达/问财被
  重复调用(多烧 1 次 tdx 配额)
- 修法: 双引擎合并结果缓存为 resBaseRows, 精筛独立成 applyResFilter
  只对缓存 symbols 调 scan; 策略下拉切换即时生效, 「不精筛」纯本地
  清除零调用; 精筛失败退回未精筛结果并标注
- 效果: 切策略只发 1 次 scan(生产日志验证 tdx/wencai 零重调);
  双引擎+策略全命中时表格出现 🔥×3(三重共振)

### feature — 机会页统一筛选入口 + 共振查询三引擎联动(PR #2)

**feature(opportunities): 分散筛选收敛为单个「筛选」Popover(草稿模式)**

- 7 项筛选(市场/来源/持仓/策略/风险/评分/题材)从两行工具栏收进一个 Popover:
  基础/信号质量/信号策略/题材 四组, 底部「清空/应用筛选」
- 草稿模式: 弹层内改动只进草稿, 点「应用」才写回并加载; Esc/关闭丢弃,
  按钮徽章 = 已生效非默认筛选数(与列表当前展示一致)
- load() 加 override 参数: 同一拍内 setState 异步读旧值问题
- 🔥只看共振保留外露快捷开关
- 策略选股/问小达/问财三卡合一为「选股工具」卡(Tabs, 当前 tab 持久化);
  WencaiPanel 加 embedded prop 去卡片壳
- 筛选内策略下拉改名「信号策略」并独立分组: 策略目录(信号标签)与
  策略库(可执行规则)两套口径, 命名区分不混列表

**feature(resonance): 共振查询 — 一句输入 → 多引擎共识**

- 选股工具卡新增「共振查询」tab(默认): 问小达+问财并发(各自失败降级)
  → 前端归一化合并(问小达中文键兜底/问财剥 USZA 前缀) → 可选策略库精筛
  → 按 共振数 > 策略分 > 引擎内排名 排序, 🔥×N + 「只看共振≥2」
- 引擎不可用降级标注("基于剩余引擎共振"); 行点击开个股洞察
- `POST /strategies/scan` 新增 symbols 自定义股票池(≤100, 优先于 universe,
  策略精筛只扫合并标的不做全市场)
- `POST /recommendations/strategy-signals/refresh` 新增 skip_market_scan:
  跳过东财榜单抓取(全量重算重头), 市场池沿用 7 日快照, 交互查询落库后
  秒级重算共振(实测 0.6s vs 全量 1-3 分钟)
- 新增 wencaiApi 封装(packages/api/wencai.ts)

**验证**: scan symbols 真实腾讯行情 5/5 通过带评分; pytest 860 passed
(9 失败 stash 对照确认预存); pnpm build 通过; 浏览器实测筛选弹层/草稿
语义/徽章/四 tab/共振降级路径

### 全面代码审计修复(Codex 三路并行审计, 后端安全19项/业务15项/前端采集层23项)

三路只读审计全部修复并补测试。本地回归 995 passed。

### update — 后端安全硬化(P0×3 / P1×11 / P2×5)

- P0: 删除智兔 token 硬编码 fallback(改必读 env + startup_check 告警); llm_adapter 只注入当前 provider env(不再同时挂 OPENAI/DEEPSEEK 三套 key); Dockerfile 非 root 运行
- P1: grafana 密码改 env 引用; redis 端口绑 127.0.0.1; forecast_server 默认绑 127.0.0.1 + 可选 FORECAST_API_KEY; scrypt 提参 n=2^15 + 旧哈希登录透明升级; XFF 仅信任直连私网 peer; WS token 支持 Sec-WebSocket-Protocol; Prometheus 高基数 label 归一; 中间件顺序修正
- P2: JWT TTL 改 env 可配置(默认不变, 保桌面 App 静态 token); 默认 owner 密码改确定性非弱密码(非 admin123); 启动提示去 /docs 诱导

### fix — 多用户隔离(事故级)+ 业务口径

- S1-S4: history/chat/price_alerts/notifications 四端点按 user_id 过滤(404 防账号探测); 相关表加 user_id 列 + 幂等迁移(SQLite/PG 双方言, 存量回填最早 owner)
- S5: stock_attribution 主力意图证据 get_capital_flow → get_main_intent(逐笔口径, 对齐其他 Agent)
- S6: 交易日判定加 2025-2027 法定节假日+调休静态表(预测命中率统计口径)
- M: suggestion_pool/save_analysis 补 user_id; 万元/万股单位标注修正; safe_num 挡 NaN/Inf

### update — 前端+采集层健壮性

- 前端: 报告窗口 document.write → sandbox iframe; index.html 加 CSP; 抽 lib/format.ts 统一 safeFixed 替换各页裸 toFixed; 401 单飞 logout
- 采集层: screenshot_collector try/finally + 批量超时; auction_collector 移出事件循环; klines_ingestor 失败聚合告警; market_http 重试总耗时封顶; capital_flow 开盘全 0 识别为"数据未生成"

### fix — 哨兵推送正文中文化(人话可读, 不用猜英文标识)

- 标题: 数据质量哨兵[FAIL] → 数据质量哨兵: 发现异常
- 正文: `tick_reconciliation:ok; null_created_at:fail` 逐项翻译成
  ✅ 逐笔对账: 正常 / ❌ 时间戳缺失: 异常 — 细节 / ⚠️ 建议数突降: 警告 — 细节

### fix — 哨兵推送目标修复(生产渠道全是用户级, user_id=None 推不出去)

- 生产 notify_channels 全部带 user_id(用户级), 哨兵 user_id=None 只匹配全局
  渠道 → push_status='skipped'(未配置通知渠道), 依然收不到
- 修法: 学 scheduler.py 订阅推送模式, 查 owner 活跃用户逐个
  user_id=uid 推送; 无 owner 时回退全局兜底

## 2026-08-22

### fix — 预测引擎/报告中心/时区口径三处线上问题修复

**fix(forecast): baostock socket 无超时挂死 → 预测任务卡住、/history 不返回**

- 根因: baostock 的 socket connect/recv 均无超时, 服务端半死时 send_msg 永久阻塞
  (py-spy 抓到预测卡在 get_stock_name(bs.login)、history 卡在
  _fetch_kline_pairs(bs.logout) 两个挂死栈)
- 修法: forecast_history.py 加 `patch_baostock_timeout()` 给 SocketUtil.connect
  设 15s 超时, 一处覆盖 login/logout/query 全部网络操作; 超时抛异常被各调用点
  try/except 吞掉返回空, 不再永久阻塞
- 效果: /forecast/history 从永久挂死 → 4.5s 返回; 预测端到端恢复

**fix(reports): 报告中心读错目录(容器缺 HERMES_HOME env)**

- 根因: reports.py 的 HERMES_HOME 默认 /hermes(容器内临时目录, 重启丢),
  但 v0.3.5 容器重建时漏了 `-e HERMES_HOME=/app/data/hermes_reports`,
  导致报告生成器写到临时目录、报告中心读临时目录看不到历史报告
- 修法: 重建容器补 `HERMES_HOME=/app/data/hermes_reports` env(纯部署修复, 无代码改动)

**fix(timezone): 统一时区口径 — 后端 4 处 datetime.utcnow() 混入 UTC naive**

- 根因: PG timestamp without time zone + func.now() 在 Asia/Shanghai 时区下存
  【北京 naive 时间】, 但 notifications/thsdk_board/auto_trigger 用
  datetime.utcnow()(UTC naive) 写库/比较, 造成 8 小时口径割裂
- 修法: timezone.py 新增 `beijing_now_naive()`, 统一替换 4 处 utcnow()
- 前端 parseServerTime 同步修正: 无时区标记的裸字符串按本地(北京)时间解析,
  不再加 Z 当 UTC(当年 SQLite UTC 时代的过时假设)

### feature — 事件驱动预期差接入盘前分析 Agent

**feature(premarket): 盘前分析新增「个股事件催化与预期差」采集 + prompt 渲染**

- `collect()` 新增 6.5 步: 对 watchlist 的 A 股(CN)标的(上限 8 只)并发调
  `event_catalyst_engine.analyze_event_catalyst`, 把每只的「催化题材/方向/置信度/
  受益链/预期差分」存进 `catalyst_analysis`; 失败静默降级为空 dict 不阻塞盘前主流程
- `build_prompt()` 在事件驱动扫描段之后渲染「个股事件催化与预期差」段:
  预期差高 = 利好/利空尚未充分反映在股价(提前潜伏/规避的核心信号),
  预期差低 = 已兑现追高需谨慎
- 与现有「全网事件流」(市场级)互补: 事件流是市场级题材输入, 本段是自选/持仓
  个股的当日公告 AI 推理, 落到具体标的

**验证**: tests/test_premarket_catalyst.py 6 passed(渲染/空降级/非 dict 跳过/
并发采集/失败降级/非 A 股跳过)

### feature — 三个 AI 推理层模块 + 注册为对话工具(DeepSeek 量化推理)

**feature(core): 新增事件驱动预期差 / 主力意图 AI 解释 / 因子 IC 归因 三模块**

- `src/core/event_catalyst_engine.py`: 事件驱动预期差引擎。当日公告 → 因果链推理
  (停产→供给收缩→涨价→受益链) → `{catalyst, direction, confidence, beneficiary_pool,
  expectation_gap{level,note}, reason}`。空事件不调 LLM, 失败静默降级。
- `src/core/intent_explain.py`: 主力意图 AI 解释层。规则给结论(dark.signal) + DeepSeek
  给「为什么 + 置信度 + 方向(吸筹/派发/洗盘/中性)」。data_status=insufficient 不解释,
  规则仍是主, AI 只做解释不改结论。
- `src/core/factor_ic_report.py`: 因子 IC 归因报告。读 factor_eval 的 IC/IR, DeepSeek
  输出「哪些因子有真实 alpha / 失效 / 市态依赖」+ 调权建议。全 ic=None 不调 LLM。
- 三者均纯函数 + LLM 层分离, 复用 intraday_monitor 的 db 场景绑定 + 8s 超时 +
  静默降级模式。
- 注册为 3 个对话工具: `get_event_catalyst` / `get_intent_explain` /
  `get_factor_ic_report`(chat.py CHAT_TOOLS + _execute_tool + stage labels)。

**验证**: test_event_catalyst_engine 10 + test_intent_explain 18 +
test_factor_ic_report 15 + test_chat_ai_layer_tools 9 = 52 passed

### feature — 新增 A 股短线情绪周期判别器

**feature(core): 新增 sentiment_cycle 纯函数情绪周期判别模块**

- 新增 `src/core/sentiment_cycle.py`:
  - `classify_sentiment_cycle(metrics)`: 判断冰点/修复/发酵/高潮/退潮 + 置信度 + 证据 + 操作提示
  - `format_cycle(result)`: 文本格式化
  - 阈值集中文件顶部常量(经验值, 后续可 IC 标定)
- 修复置信度计算: 用命中周期满分做分母(非全局最大), 修复/发酵满分周期也能到高置信度

**验证**: tests/test_sentiment_cycle.py 15 passed

### feature — 主力意图/暗盘/内外盘接入交易智能体(多智能体委员会资金面裁判)

**feature(tradingagents): 把 dark_flow 暗盘体系喂给 TradingAgents 多智能体委员会**

- `collect()` 新增第 5 类数据采集: A 股(CN)标的并发拉 `_main_intent_summary`
  (腾讯逐笔口径: 主力/超大单/大单净额、参与度、买占比、5日阶段、竞价、筹码峰/
  成本带、拆单、内外盘口诀、背离、时段节奏), HK/US 跳过; 失败静默降级不阻塞主流程
- `analyze()` 把主力意图摘要经纯函数 `build_main_intent_context` 注入
  past_context(复用 patch_propagator 扩展通道 → PM 节点可见), 并显式标注口径
  (腾讯逐笔 vs 资金面东财四档不同源, 判断吸筹/派发以本段为准)
- 新增 `portfolio_context.build_main_intent_context` 纯函数(无 IO, 可单测)

**验证**: tests/test_tradingagents_main_intent.py 4 passed(纯函数渲染 /
None 空串 / collect A 股采集 / 失败降级)

### feature — 三个 L2 引擎注册为 AI 助手对话工具

**feature(chat): 注册 get_main_flow_compare / get_delta_series / get_orderbook 三个 L2 对话工具**

- 新增 `get_main_flow_compare`: 比对三路主力资金(腾讯逐笔/同花顺L2/恒生DDE)的一致性, 返回每路主力净额(元)及一致性评分(0-100)。仅限A股(CN), 入参 symbol=6位A股代码
- 新增 `get_delta_series`: 基于THS L2逐笔穿透计算秒级Delta序列(主动买-主动卖金额)及顶底背离信号。先拉取全天逐笔, 再计算每秒净额、30秒平滑Delta、累计Delta、顶背离/底背离。仅限A股(CN)
- 新增 `get_orderbook`: 采集THS L2盘口(20档)多快照演变分析: 托单/压单/撤单/幽灵单检测 + 订单簿失衡(OB) + 幽灵单比率。入参6位A股代码, 自动转THS代码。采集8个快照(间隔1.5s, 约12秒)。仅限A股(CN)
- 三个工具均遵循项目热修规范: 同步网络调用用 asyncio.to_thread 包裹, 防阻塞事件循环; 返回文本开头带数据源口径标注; market != 'CN' 返回明确拒绝; 失败返回友好文案不抛异常
- 新增 `_TOOL_STAGE_LABELS` 三行(流式阶段提示文案)

**验证**: tests/test_chat_l2_tools.py 13 passed(成功分支含口径标注验证 / market!=CN拒绝 / 全部失败降级 / 异常降级 / 带信号渲染 / 无效symbol降级)

### feature — Redis 业务缓存落地(L1 内存 + L2 Redis)

**feature(cache): 新增统一业务缓存层 biz_cache, 业务数据缓存跨进程 + 重启不丢**

- 新增 `src/web/cache/biz_cache.py`: L1 进程内 dict + L2 Redis 两级缓存,
  同步接口(redis-py 连接池, 线程安全), 优雅降级(Redis 不可达退回纯 L1,
  行为等价于原内存 dict), 连接失败 30s 冷却避免反复撞超时
- 接入三处业务缓存点:
  - 发现页热点(stocks/boards, TTL 45/60s) — discovery.py 的 _cache 迁 biz_cache
  - 汇率缓存(HKD/USD, TTL 3600s) — accounts.py 成功结果写 Redis, 内存 miss 时跨进程兜底
  - 组合基准/归因结果(TTL 600s) — _PORTFOLIO_RESULT_CACHE 从 TTLCache 迁 biz_cache,
    持仓指纹 key 加 portfolio: 前缀
- `/api/health` 新增 `biz_cache` 组件字段(l1_entries + redis 连通状态)
- server.py 启动时预热 biz_cache 连接
- 修复 Redis 连接前提: 生产容器此前未设 REDIS_URL(默认 localhost:6379 不通,
  Redis 在独立容器 panwatch-redis), 需在部署时注入 REDIS_URL=redis://panwatch-redis:6379/0

**验证**: py_compile 通过; Redis 不可达降级读写 ✅; Redis 可达跨进程共享(进程A写/进程B读)✅;
get_or_fetch 防穿透 / TTL 过期 / delete ✅

### feature — 新增 A 股短线情绪周期判别器

**feature(core): 新增 sentiment_cycle 纯函数情绪周期判别模块**

- 新增 `src/core/sentiment_cycle.py`:
  - `classify_sentiment_cycle(metrics) -> dict`: 输入涨停家数/连板/炸板率/昨日涨停表现/亏钱效应,
    输出 冰点/修复/发酵/高潮/退潮/数据不足 之一, 含置信度、证据、操作提示
  - `format_cycle(result) -> str`: 格式化输出供 AI 助手/报告使用
  - 全部字段可空降级, 核心字段全缺失不抛异常, 返回 `{cycle:'数据不足', ...}`
  - 阈值集中文件顶部常量, 注释标记'经验值,后续可 IC 标定'
- 新增 `tests/test_sentiment_cycle.py`:
  - 覆盖 5 个周期边界 + 空数据降级 + 单/多指标缺失 + 格式化输出
  - 15 个测试用例全部通过

**验证**: pytest tests/test_sentiment_cycle.py -v → 15 passed ✅

## 2026-08-21

### feature — 机会页整合 P2 前端: 今日机会榜+共振标记+统一筛选

**feature(web): 机会页漏斗式改版(多源共振可视化)**

- 筛选栏: 来源下拉补4新源(策略/竞价/问小达/问财) + 🔥只看共振开关
  (过滤 resonance_count>=2, localStorage 持久化)
- 机会榜排序: 共振票优先 → 市场池 → 分数; 每行标题旁 🔥×N 徽章
  (hover 显示共振来源明细)
- 卡片新增"来源"徽章行: 该票被哪些来源命中全部展示
  (自选建议/盘中扫描/策略信号/竞价异动/问小达/问财)
- StrategySignalItem 类型补 candidate_source/meta 字段;
  strategy_engine._format_signal 透传 payload 里的 resonance_* 到 API
- pnpm build 通过

### feature — AI 助手新增 get_northbound 北向资金工具

**feature(chat): 激活同花顺北向资金孤儿数据源**

- 背景: data_sources id=24 "同花顺北向资金" enabled 但零消费(agents/
  dashboard/前端均无调用), 实测接口存活(当日 hgt_net=-9.28亿)
- 新增 get_northbound 工具: 返回当日沪股通净额+口径标注(2024-08 后交易所
  停止披露实时净买入, 同花顺估算口径仅供参考; 主力意图以 get_main_intent 为准)
- 生产热修验证: 工具返回完整口径标注文本; 21 chat tests passed

### feature — P1 产品化五连(限流分级/API版本化/个人中心/CSV导出/监控告警)

**feature: 限流分级 + /api/v1 别名 + 个人中心&CSV导出生效 + Prometheus 全链路监控**

- middleware.py 限流分级: GET 60→300/min, 写操作→150/min, 登录等敏感端点
  单独 20/min 防爆破; 环境变量可调(RATE_LIMIT_DEFAULT/GET/WRITE/SENSITIVE)
- api_version.py: /api/v1/* → /api/* 透明改写中间件, 为将来 v2 平滑过渡
- profile/export 路由挂载生效(后端224+250行早已存在, 前端 Profile.tsx 已有
  路由, 生产容器旧版未挂载 → 热修生效, /api/v1/health 别名实测 200)
- 监控全链路打通: health.py 补 record_request_metrics/datasource_failures
  埋点(之前指标定义存在但从未接线), RequestLoggerMiddleware 接入;
  prometheus.yml target 修复(panwatch 容器接入 panwatch-net, 容器名解析);
  新增 4 条告警规则(5xx率/P95延迟/服务失联/数据源失败) promtool 校验通过;
  Grafana datasource 修正 + "SIDA 生产监控"面板(QPS/P95/错误率/状态码/
  数据源失败/进程内存)已导入(uid=sida-prod)
- 国内机 alert_forwarder.py: 每2分钟拉 Prometheus firing alerts → pushplus
  微信推送, 30分钟去重, cron 已配

### update — Dashboard 并发性能三连修(连接池/版本检查缓存/news开关)

**update(perf): PG 连接池扩容 + GitHub 版本检查 24h 缓存 + news 紧急开关**

- database.py: PG pool_size 5→10, max_overflow 10→20(实测 26 并发打满
  QueuePool 触发 TimeoutError)
- update_checker.py: GitHub release API 每次调用 11s+ 且无缓存 → 进程级
  24h TTL 缓存, Dashboard 自动刷新不再被拖累
- news.py: 加 NEWS_DISABLE=1 紧急开关(偶发 15s+ 超时拖累首页时启用)

### feature — 产品化加固六件套(哨兵/自检/错误追踪/备份/冒烟/UI统一)

**feature(ops): 数据质量哨兵 + 启动自检 + 错误追踪 + 备份容灾 + 冒烟门禁 + UI统一**

- `src/core/data_quality_sentinel.py`: 每小时 4 项检查(逐笔总额对账/created_at
  NULL/建议数突降/失败通知计数), 异常写 Notification, 全 ok 静默
- `src/core/startup_check.py`: 启动时 7 项配置自检(DB方言/SIDA_DB_URL缺失/
  恒生mock/thsdk游客/JWT/数据目录/通知渠道), warning 打横幅, 接入 server.py lifespan
- `src/core/error_tracker.py`: 未处理异常 JSONL 落盘 + 同指纹去重 +
  高频异常(10min内3次)聚合发通知, install_error_tracker(app) 已接入 app.py
- 国内生产: PG 每日 23:30 自动备份(保留7天) + 异地同步海外机(保留14天),
  SSH key 免密已配, 手动全流程验证通过(16MB gz, 海外落地)
- `scripts/smoke_test.py` + `post_deploy_smoke.sh`: 发版后自动等 healthy →
  10 个核心 API 冒烟(9/9 passed 2.1s 实测), 结果落 smoke.log, FAIL 退出码 1
- 前端: ErrorState(技术错误翻译人话+重试)/LoadingState/usePolling 统一组件,
  MainFlowCompareCard 迁移示范; pnpm build 通过
- 测试: 775 passed(+18)

### update — Dockerfile 分层缓存优化(按变更频率排序 COPY)

**update(docker): 低频层在前/高频层在后, 改 src 不再失效低频层缓存**

- 后端阶段 COPY 顺序调整: server.py/prompts/strategies(低频) → src/+data_source/(高频)
  → thsdk vendor → VERSION(每次发版必变, 放最后只失效末两层)
- 效果: 只改 src/*.py 重新构建时, apt/pip install 及低频文件层全部命中缓存
- 前端阶段原本已符合最佳实践(package.json → install → 源码), 未动

### fix — 主力意图卡片净额翻倍(盘后增量续拉重复拉取)

**fix(dark_flow): 增量合并改三元组指纹去重 + 总量守恒校验**

- 现象: 用户截图 主力净额-15733万/外盘额7.15亿, 但当日实际成交额仅11.68亿(+47%)
- 根因: 盘后腾讯重排逐笔页码/seq, `_fetch_all_ticks` 增量续拉把同一批成交以
  **不同 seq** 再拉一遍 → 仅按 seq 去重失效 → 合并翻倍; 翻倍数据落盘,
  容器重启后从磁盘加载污染快照继续错
- 修复: ① 合并去重改用 (时间t, 价格price, 成交额amt) 三元组指纹(同一笔成交
  无论 seq 怎么变指纹不变); ② 加总量守恒校验——合并后总额超 max(old,new)×1.1
  即弃增量全量重拉
- 验证: 全量重拉 +11853万(买6.04亿+卖5.03亿=11.07亿≈实际11.68亿✓),
  连续增量调用不再漂移; 757 passed

### fix — 国内生产 PG created_at 大面积 NULL(数据"像昨天的"根因)

**fix(db): 国内生产全库回填 NULL created_at + 补 default now()**

- 现象: 用户反馈"主力资金数据不对, 可能是昨天的"
- 根因: 国内生产 PG 多张表(stock_suggestions 1447行/notifications 16行/
  stock_context_snapshots 192行/strategy_* 等)的 ORM 写入行 created_at=NULL
  (列无 default, SQLAlchemy 模型 default 不写 DB 层) → 按 created_at 排序/
  过滤时今天的数据沉底, 界面显示旧数据
- 修复: 全库扫描 30+ 表, id 邻近锚点回填 NULL, 全部补 default now();
  另修正 18 行被 expires_at-6h 错误回填到未来的行(expires_at-16h)
- 验证: suggestions 今日=734 / 最新=今天16:39; 未来行=0; 无 default 表=0

### fix — 主力意图模块全面体检(2026-08-21 收盘后)

**验证结论: 4 个入口全部正常**

| 入口 | 结果 |
|---|---|
| chat 工具 get_main_intent | ✅ 127 字完整返回(口径标注+净额/参与度/阶段/筹码), 连续 3 次稳定 |
| chat 工具 get_thsdk_dde | ✅ available=True(官方 DDE -5647万) |
| dark-flow 轻接口(前端卡片) | ✅ main_net/inner_outer(外盘42.4%/内盘53.2%)/口诀 正常 |
| 三源对比卡片 | ✅ 腾讯-1.57亿 / thsdk +3620万 / 恒生-230万, 量级可比 |

注: 此前一次 _execute_tool 返回 LEN=127 疑似数据缺失, 经 codepoint 解码确认
127 字 = 完整内容(口径标注19字+换行+数据107字), 非截断。

### fix — 主力意图三源对比 thsdk 源净额放大 1363 倍(累计口径未差分)

**fix(thsdk_l2): compute_main_flow 改相邻行差分还原区间增量**

- 现象: 三源对比卡片 thsdk 源 main_net = -2144亿, 腾讯 -1.57亿(放大 1363 倍),
  一致性恒 0
- 根因: tick_super_level1 的 总金额 是**当日累计**口径(约 3 秒条),
  旧实现对累计列直接 sum(); docstring 早有警告但代码没做差分
- 修复: 与 dark_l2.fetch_l2_ticks 同一套逻辑——按行序对 总金额 相邻 diff
  还原每条 3 秒棒的成交额增量, 再按方向汇总; 大单阈值同样按增量比较。
  修复后神剑 thsdk 净额 +3620万(量级与腾讯/恒生可比)
- 全量 757 passed

### fix — AI 助手 get_thsdk_dde 工具线上故障(国内生产)

**fix(chat): get_thsdk_dde 改走 get_main_flow_official(THS 无 dde 方法)**

- 现象: 国内生产 v0.3.3 AI 助手调 DDE 大单动向 →
  "'THS' object has no attribute 'dde'"(thsdk 当前版本无该接口)
- 修复: 改走 `get_main_flow_official`(底层 query_data id=200 同花顺官方 DDE 口径:
  主力净流入 + 特大单/大单主动/被动明细), 国内生产实测可用(神剑 -5647万)
- 单测 FakeL2 补 get_main_flow_official mock; 757 passed

### fix — AI 助手 get_main_intent 工具线上故障(国内生产)

**fix(chat): _execute_tool 入口统一 import asyncio, 修复 UnboundLocalError**

- 现象: 国内生产 v0.3.3 AI 助手问主力意图 → "主力意图获取失败:
  cannot access local variable 'asyncio' where it is not associated with a value"
- 根因: `_execute_tool` 内 `get_market_news` 等分支的局部 `import asyncio`
  使 asyncio 成为整个函数作用域的局部名; `get_main_intent` /
  `get_rally_analysis` 分支在绑定前引用 → UnboundLocalError
- 修复: 函数入口统一 `import asyncio` 一次, 全分支可用;
  各分支内重复局部 import 变为冗余但无害
- 验证: 本地 757 passed; 生产容器热修后实测工具返回完整逐笔数据

## 2026-08-21

### fix — CI 测试门禁修复 (GHCR build 恢复)

**fix(tests): conftest 加 DB 建表 + 模块缓存清理两个 autouse fixture**

- `_init_test_db`(session 级): CI 无 init_db() → `no such table: stocks`,
  test_announcement_eval 挂 → 门禁拦 GHCR build。现 session 开始时 create_all。
- `_clear_module_caches`(每测试): kline_collector/_FLOW_CACHE 模块级 TTL 缓存
  跨测试残留 → 单跑过合跑挂(flaky)。每测试前清空。
- **workflow 排除联网测试文件**(18 个): CI 海外机房访问不了国内行情源
  (腾讯逐笔/thsdk/恒生等), 这些测试只能本地跑。本地模拟 CI 跑法:
  tests/ 578 passed + marketdata 包 190 passed, 全绿。
- 本地全量回归: **757 passed, 0 failed**(修前 4 failed)。

### fix — K线摘要端点 30s 超时 (v0.3.3 后端热修, commit 8a25606)

**fix(intraday_monitor): `_main_intent_both` 加 12s 硬超时 + 拆内部函数**

**根因**(用户实测 `/api/klines/688137/summary` 10s 超时):
- 摘要接口同步调 `_main_intent_both(symbol)` → 内部 `compute_dark_flow` + `compute_near_term_chips` 拉逐笔/分价表/5日资金流
- 同步阻塞调用 + 数据源慢 → 30s 内不返回 → 前端 AbortController 超时
- memory #40 提示过"主力意图逐笔翻页冷启动 20-30s 撞 502"

**修复**:
- `_main_intent_both` 改用 `concurrent.futures.ThreadPoolExecutor(1)` + `future.result(timeout=12.0)` 硬限
- 超时/异常 → 返回 `("", None)`,**不让摘要接口拖到 30s**
- 重构原逻辑为 `_main_intent_both_inner`, 加完整 try/except 防御

**验证**:
- bench 修前: `/api/klines/688137/summary` 10011ms (Top 1 慢)
- 修后: 该端点不进 Top 10 慢, 全 API 0 失败

### update — 数据源国内全可用(memory #40 验证)

**fix(datasources): 12 个东方财富数据源国内实测 100% 启用 + 引擎已接入**

| 数据源 | priority | enabled | engine_attached | test |
|---|---|---|---|---|
| 东方财富资金流 | 5 | ✅ | ✅ | success (74% p50 173ms, 非交易时段正常) |
| 东财龙虎榜 | 5 | ✅ | ✅ | success |
| 东财融资融券 | 5 | ✅ | ✅ | success |
| 东财分红 | 5 | ✅ | ✅ | success |
| 东财基本面 | 5 | ✅ | ✅ | success (p50 2114ms) |
| 东财股东户数 | 0 | ✅ | ✅ | success |
| 东财事件日历 | 0 | ✅ | ✅ | success |
| 东财快讯 7x24 | 10 | ✅ | ✅ | success |
| 东财行情 | 5 | ✅ | ✅ | success (p50 12968ms 一次性, 后续 <200ms) |
| 东财资讯/公告 | 0/2 | ✅ | ✅ | success |
| **同花顺板块资金**(图标) | 0 | ✅ | ✅ | success 100% p50 142ms |
| **同花顺大盘资金**(图标) | 0 | ✅ | ✅ | success |

**两个图标 = `ths_flow` (id=38) + `ths_market_flow` (id=39)**, 都 priority=0 首选,生产**已接入新引擎 + 启用**。

**前端用户感知的"默认关闭"** — DataSources.tsx 308 行 `disabled={testing === source.id || !source.enabled}` 只是**测试按钮的禁用条件**,**不影响数据源本身**。截图时显示的"关闭"状态可能是其他视角(看截图具体位置)。

memory #40 修复:海外 502/超时的"关闭"是**海外节点**被封,国内生产**全可用**。

### fix — 首页 ErrorBoundary 崩溃 "页面遇到了问题" (v0.3.3)

**fix(dashboard): 防御后端数值类型变化导致 TypeError: c.price.toFixed is not a function**

**根因**(用户报"首页报错" + console 显示 `c.price.toFixed is not a function`):
- 切到 PG 后,部分后端 `numeric/DECIMAL` 字段经 psycopg2 → JSON 序列化变成字符串
- 前端 `Dashboard.tsx` / `DiscoveryPanel.tsx` 直接 `.toFixed()` 抛 TypeError
- `AppErrorBoundary` 兜底 → 整页显示"页面遇到了问题 / 重试 / 回到首页"
- 用户刷新偶发可恢复,但非交易时段数据稀疏时也可能触发

**修复**:
- `Dashboard.tsx` 加 `safeNum / safeFixed / safeFlow` helper(string / null / undefined / 非有限数 → fallback)
- 替换所有 `.toFixed()` 调用:`ix.current_price / b.net_inflow / marketFlow.{total_main_flow, total_amount, sh_flow, sz_flow}`
- `DiscoveryPanel.tsx` 三个 map(`hotBoards / visibleHotStocks / boardStocks`)的 `pct` 显式做类型转换 `typeof rawPct === 'number' ? rawPct : Number(rawPct)` + `isFinite()` 防御
- `s.price` 防御:`typeof === 'number' && isFinite` 直接 toFixed;否则 `Number(s.price)` 试一次;都不行才 `--`

**验证**:
- Playwright 公网: 错误页? False
- console errors: 0, pageerrors: 0
- 31 个 API 全部 200, 异动池/热榜/板块资金流全部正常渲染

### fix — 首页 Onboarding 遮罩拦截点击 (v0.3.3)

**fix(dashboard): Onboarding 不再自动弹出** — `Dashboard.tsx` 移除 `useEffect` 里的
`if (!localStorage...) setShowOnboarding(true)` 自动触发;改为头部低存在感"新手引导"按钮,用户主动点击才打开。

**根因**: `panwatch_onboarding_completed` 用 localStorage 持久化 → 用户清浏览器缓存 / 换浏览器 / 换设备 → Onboarding 自动全屏遮罩重弹 → **遮罩拦截所有点击** → 用户看到"异动池/热榜点击股票不显示数据"(实际是按钮永远点不到)。

**诊断证据**:
- Playwright + 生产公网复现: `Onboarding open` 时所有 button 都被 `div.fixed.inset-0.z-50` 遮罩拦截, 报 `<div ...backdrop-blur-sm...> intercepts pointer events`
- 临时绕过(`localStorage.setItem('panwatch_onboarding_completed', 'true')` + reload)→ 模态框正常打开, 数据完整

**修复后**: 换浏览器/清缓存后不再被强制挡页;老用户(localStorage 已标记)行为不变;新用户第一次进入也不再被强制引导,需主动点"新手引导"按钮。

### fix — 登录卡死 "一直在加载中" (v0.3.3 后端热修)

**fix(audit): 审计写入用独立 session,失败不再污染主请求** — `src/web/api/audit.py:log_audit`
改为 `SessionLocal()` 独立 session 写 audit_logs,失败 rollback 该 session + log error,**绝不影响调用方 session**。

**根因**(用户报"现在登录一直在加载中"):

1. 原实现 `db.add(entry) + db.commit()` 直接复用主请求的 SQLAlchemy session
2. audit_logs 表高频写入 → SQLite WAL 偶发 lock → `database is locked`
3. SQLAlchemy 把主 session 标记为 `PendingRollbackError`
4. 后续 `user_to_dict(user)` 触发 lazy load → 同一 session 二次报错 → 整个 login 端点永远不返回
5. 前端 AbortController 等 30s → 用户看到"加载中"

**修复后**:
- 审计写入失败只 log error,不抛错,主 session 永不被污染
- 删 auth.py 里的 `except Exception: pass` 兜底(原意是"审计失败不影响登录",现在 audit 自己 best-effort,不需要外层吞错)

**应急重启**: 修复前手动 `docker restart panwatch` 清掉污染 session,登录 200 (71ms) 恢复。

### update — 生产数据库 SQLite → PostgreSQL 切换

**feat(infra): 生产 panwatch 容器切到 PostgreSQL** — 通过 `SIDA_DB_URL` env(`postgresql+psycopg2://sida:***@172.17.0.1:5432/sida`)连接宿主机 PG 16。

**根因**(memory #53 镜像漂移):
- 源码早支持 PG(`database.py:IS_PG` 切换),但 v0.3.2 容器 env 没设 `SIDA_DB_URL`,**仍跑 SQLite**
- SQLite WAL 偶发 lock = 登录卡死的根因之一(见上一条)
- PG 已装好(`apt install postgresql-16`)且 `sida` 库就绪(50 表 schema),只是没被容器用

**步骤**:
1. **核实数据一致性**:50 表逐行对比,SQLite 是 8/17 旧快照,PG 是持续运行新库
2. **增量同步**:把 SQLite 多出来的 120,819 行 UPSERT 到 PG(主要是 8/17 之后的 audit_logs + log_entries + agent_runs 等),布尔列 0/1 显式转 bool
3. **重建容器**:停 → 删 → 加 env `SIDA_DB_URL` → `docker run` 同 image v0.3.2(数据卷保留)
4. **热修 audit.py**:v0.3.2 镜像不含 commit 9e7162c 修复,再触发同样 bug → `docker cp` 把已修 audit.py 进容器 + restart
5. **同步前端 bundle**:v0.3.2 容器 index.html 引用 `DWBuKCQT.js / -xojz8-N.css`(老 v0.3.0 镜像引用),本地 dist 是 `BLpaJ7XA.js / xdV8ueV7.css` → 用本地 dist 同步覆盖

**修复后**:
- 31 个核心 API 全部 200,0 失败
- 登录 53-81ms(原 SQLite 慢时 800ms+)
- Onboarding 修复 / 审计死锁修复 / 限流未触发三层修复全部在生产生效

## 2026-08-20

### fix — 生产热修补丁合入源码 (v0.2.69.0)

**fix(health): 调度器存活误判修复** — 健康检查不再用封装类的 `_running`(实为 job 重入锁,平时恒 False),
改为优先探测内部 APScheduler 实例的 `.running`(真·运行状态)。
修复前: `/api/health` 误报 scheduler: degraded / shutdown=5(实际 5 个调度器正常);
修复后: `scheduler: {status: ok, running: 5, shutdown: 0}`。

**fix(ratelimit): `/api/metrics` 加入限流豁免** — `EXEMPT_PATHS` 补上 `/api/metrics`,
Prometheus 抓取不再被限流(60/min)挡成 429。
修复前: 1 小时 240 次 429, target=down; 修复后: 连打 70 次全 200。

**chore(watchdog)**: 生产 watchdog 改读 `/api/health` 的 database 组件(适配 PG 迁移,脚本本机不入仓)。

### fix — K线/分时双修复 (v0.2.70)

**fix(kline): K线接口 20→24s 串联阻塞** — `data_sources` 表中 `eastmoney.kline`(priority=15)
和 `stooq.kline`(priority=20) 在海外节点反复 502, 触发 5 源串联降级链(tencent→zhitu→ths→yahoo→eastmoney)。
20 并发同接口 = 全部 24s 等价堵塞。
应急 DB 修复: `UPDATE data_sources SET enabled=0 WHERE type='kline' AND provider IN ('eastmoney','stooq'); UPDATE ... SET priority=3 WHERE provider='ths'`。
效果: 冷启 5.3s → 0.29s, 20 并发 24s → 0.22s(恢复 110x)。
**根因待修**: marketdata Engine 按 priority 顺次试源无 per-vendor 超时护栏, 需补超时+并行。

**fix(minute): 分时接口 30s 轮询撞前端 20s 超时** — `analyze_swings` 全量逐笔翻页冷启动 ~15s,
前端默认 20s 超时必中招。后端 `_MINUTE_TTL` 15s → 60s、`_TICKS_TTL` 30s → 90s, 保证 30s 轮询始终命中缓存。
前端 `InteractiveKline.loadMinute` / `MinuteDialog` 加 `timeoutMs: 60000`, `MinuteDialog` 路径式请求
(`/quotes/minute/{symbol}`) 修原 `?symbol=` 撞路由 404 的坑。K线 4 种粒度(分时/日K/周K/月K)
实测均 < 4s 返回(冷启), 缓存命中 0.01s。

**fix(summary): 502 Bad Gateway** — `/api/klines/{symbol}/summary` 开盘后冷启动 ~20-30s
(主力意图+筹码逐笔翻页), 与前端其他请求叠加撞 Caddy 30s 反代超时 → 502。
加 30s 进程内缓存, 单次冷启动后所有同标的请求直接秒回。

### feature — thsdk L2 全能力落地 (v0.3.0)

后端接入 **7 个新接口** + **3 张新表**, 23 个 thsdk L2 能力首次全链路可用:

| 模块 | 后端 | 测试 |
|---|---|---|
| **主力意图双源对比** | `src/core/main_flow_compare.py` + `src/web/api/main_flow.py` (GET `/api/main-flow/compare/{symbol}`) | 10 passed |
| **竞价异动池** | `src/core/auction_pool.py` + `src/web/api/auction_pool.py` (GET `/api/auction/anomaly` + history + sync) + cron 09:25 | 11 passed |
| **个股 L2 综合快照** | `src/web/api/thsdk_snapshot.py` (GET `/api/thsdk/snapshot/{symbol}`) | 14 passed |
| **thsdk 三大算法输出** | `src/web/api/thsdk_alert.py` (GET `/api/thsdk/alert/{symbol}`, 包 close_surge/auction/wencai_pool) | 14 passed (合并) |
| **thsdk 板块数据** | `src/core/thsdk_board.py` + `src/web/api/boards.py` (4 端点) + cron 08:30 | 16 passed |
| **DB 新表** | `Board` / `BoardDaily` / `AuctionAnomalyRecord`(SQLite/PG 双兼容 + 唯一约束 + 索引) | — |

**测试汇总**: 51 passed (新增) / 694 passed (总回归)。

**前端 UI 组件**:
- 主力意图双源对比卡(挂在 DarkFlowCards)
- 竞价异动池 Tab(挂 Opportunities 机会页)
- 板块详情页 + 板块轮动 Top 5

(前端 UI 在 feature commit 集成)

### fix — wencai_nlp + 龙虎榜主源修复 (v0.3.1 hotfix)

**修复 1**: thsdk `wencai_nlp` 端点从来没工作过
- 症状: `/api/wencai?query=...` 一直返空(运营预设查询/机会页问财选股/AI 工具全靠这个)
- 根因: `data_source/thsdk_l2.py::get_wencai_nlp` 写的是 `resp.df if hasattr(resp, "df") else pd.DataFrame()`,但 thsdk Response **没有 .df 属性**(只有 .data: list/dict/str)
- 修法: 改用 `resp.data`, list/dict → DataFrame
- 验证: "神剑股份昨日龙虎榜买入卖出营业部" 查询返 10 条席位明细(含深股通 -1.48亿 / 机构 3 家 -1.71亿 / 国信浙江互联网 +0.32亿)

**修复 2**: 龙虎榜主源 ftshare → eastmoney
- 症状: ftshare 海海外节点访问慢, 默认 page_size=20 神剑等"普通上榜"票在 page 2+
- 改后: eastmoney datacenter 国内节点直连, page_size=500 一次性
- DB: `data_sources` 表 ftshare.enabled=0, eastmoney.enabled=1 priority=0
- 验证: 8/19 神剑龙虎榜正常返回 83 行, 含 2 条神剑记录(收盘11.27/-7.01%/净买-2.65亿)

**修复 3**: 东财 vendor YYYYMMDD → YYYY-MM-DD 格式修正
- 症状: `EastmoneyDragonTigerVendor.fetch(date="20260819")` 直接传给东财 datacenter, 但东财 API 要求 `YYYY-MM-DD` 格式, 否则 '参数预处理错误'
- 修法: vendor 内部判断 `len(date) == 8` 时自动补短横线
- 影响: `/api/market-data/dragon-tiger/{YYYYMMDD}` 端点可用

### feat — 龙虎榜席位明细端点 (v0.3.1)

**端点 1**: `/api/market-data/dragon-tiger/{trade_date}`
- 调东财 vendor 拿汇总(净买/原因/上榜明细)
- **旁路调 ftshare vendor 拿席位明细**(`top_buyers`/`top_sellers`, 机构/游资/深股通专用)
- 合并: 每条 item 同时含东财汇总 + ftshare 席位
- 验证: 8/19 神剑可看 深股通 -1.48亿 / 机构 3 家 -1.71亿 / 国信浙江互联网 +0.32亿

**端点 2**: `/api/market-data/fundamentals-detail/{symbol}?dt_days=N`
- 已有: 龙虎榜(汇总) / margin / shareholders / dividend / events
- 新增: 龙虎榜每条 item 加 `top_buyers` / `top_sellers` 字段

### update — ftshare vendor 完善

- 加分页翻页(page 1-10), ftshare 每页固定 20 条需循环
- `DragonTigerItem` dataclass 加 `top_buyers` / `top_sellers` 字段(可选, 默认 None)
- 不破坏 v0.3.0 已上线端点(原字段保留, 新增字段向后兼容)

### feature — 恒生数据库三源接入主力意图(v0.4.0 预备)

- 新增 `src/core/hengsheng_client.py` (168 行): 聚源/恒生金融数据库 client, Bearer auth + POST
- 新增 `src/core/hengsheng_fund_flow.py` (174 行): `get_hs_fund_flow(symbol, days=10)` 调 3.1 AStockCashFlow + 2.6 RealStockFundFlow, 30s 缓存
- `src/core/main_flow_compare.py` 双源→三源(腾讯逐笔 + thsdk L2 + 恒生 DDE), 一致性 = min pairwise, 降级友好
- `src/web/api/main_flow.py` 响应加 `hengsheng` 字段 (dde_ratio / rising_up_days / 4档资金)
- **实战心法直接对齐同花顺口径**: `rising_up_days` (连红天数) + `dde_ratio` (资金比) + 4档分化
- 凭证未提供前 mock 模式跑通; 凭证到位后 .env 加 2 行 (`HENGSHENG_BASE_URL` / `HENGSHENG_API_KEY`) 自动切真接口

## 2026-08-20

### feature — thsdk 全能力落地 (v0.3.2)

**thsdk_l2.py 扩展 (+404 行)**:
- 新增 `_to_dataframe()` 静态转换器 (.df / .data list/dict → DataFrame)
- 新增 `_WENCAI_CACHE` + 30s TTL 增强版问财缓存
- 新增 7 个方法: get_corporate_action / get_dde / get_hs300_constituents /
  get_market_data_cn_extended (主力净流入, 游客返0, 正式账户解锁) /
  get_market_data_bond / get_market_data_fund / get_wencai_enhanced
- 增强 3 个方法返 DataFrame: get_market_data_index / hk / us
- `get_news` 支持按 symbol 过滤

**API 端点 (14 个新端点)**:
- `/api/thsdk/ext/*` (3 个, B 智能体落地):
  - dde/{symbol} (DDE 主力资金, 同花顺官方)
  - code/{code} (证券代码补齐, 支持批量)
  - market/{market} (全市场代码表)
- `/api/thsdk/*` (11 个, A 智能体落地):
  - news / corporate_action / dde / hs300
  - market_data_cn_extended / index / hk / us / bond / fund
  - wencai_enhanced

**对话助手 (CHAT_TOOLS) 新增 11 个工具** (C 智能体):
- get_thsdk_news / corporate_action / dde / hs300_constituents
- market_data_cn_extended / index / hk / us / bond / fund
- wencai_enhanced
- 每个工具返 `{available, data, note}`, 失败降级不 panic
- SYSTEM_PROMPT 加 thsdk 数据源指引

**测试 (40 个新用例全过)**:
- tests/test_thsdk_ext.py (B): 11 用例
- tests/test_thsdk_extended.py (A): 14 用例
- tests/test_chat_thsdk_tools.py (C): 15 用例
- 回归 738 passed, 6 known failures (与改动无关, network test)

**游客账户限制**:
- 主力净流入 / 指数 / 港股 端点返 0 行
- 代码/路由已建好, 等正式同花顺账户解锁
- 不破坏 v0.3.0 / v0.3.1 已上线功能 (K线 / 分时 / summary / wencai / 龙虎榜端点)

## 2026-08-18

### feat — AuditMiddleware 操作审计全覆盖 (v0.2.65.5)

- 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 补齐渠道/服务商/数据源/设置/用户等管理操作审计
- 内部自行 decode JWT, 独立 session 异步落库, 失败静默不阻塞
- 排除 auth(已有埋点)/静态/health/webhook

## 2026-08-18

### fix — 生产稳定性 + 微信通道重构 (v0.2.65.4)

**ths_web 403 熔断(修复生产 42s 卡死 + 403 风暴)**:
- `_fuyao_post`/`_ths_get` 遇 HTTP 403 抛 `_ThsBlockedError`,Engine 识别后直接跳过整个源
- 不再 per-symbol 逐个 403 浪费 14 次无用请求(此前 254 次/10min 拖垮事件循环)

**个人微信通道彻底移除 OpenClaw, 全链路 iLink 直连**:
- 渠道类型标识 `openclaw` → `wechat_ilink`(notifier / wechat_bind / wechat_bot_worker)
- 移除 openclaw 的 `webhook_url` 必填校验(iLink 扫码绑定自动写入 token/base_url/user_id)
- `_send_openclaw` → `_send_wechat_ilink`
- 前端 Settings/Notifications 类型标识与表单同步改 `wechat_ilink`, fields 置空引导扫码
- DB 迁移: notify_channels.type `openclaw` → `wechat_ilink`, 清理重复渠道

## 2026-08-17

### feat(infra) — 基础设施层 Phase 1 (v0.2.65)

参考架构方案落地第一批 5 件:
- ✅ **Redis 7**: 缓存 + 限流 token bucket + Redis Streams 任务队列
- ✅ **Prometheus**: /metrics 端点 + 业务指标
- ✅ **Grafana**: 预置 Prometheus + Loki 数据源 (35099 端口)
- ✅ **Loki**: 日志聚合 (7 天保留)
- ✅ **Promtail**: 收 panwatch stdout 送 Loki (结构化 JSON)
- ✅ **统一网关中间件**: JWT decode + 限流 (Redis 优先 + 内存降级) + 请求日志
- ✅ **深度 /health**: PG / Redis / 调度器 / 限流 状态分别报告
- ✅ **Redis Streams**: kline_backfill 任务 publish (替代部分 APScheduler 职责)

**核心模块**:
- `src/web/cache/redis_client.py` (230 行): 单例 + 降级策略
- `src/web/cache/streams.py` (70 行): Stream publish + stats
- `src/web/middleware.py` (250 行): JWT/限流/日志 3 个中间件
- `src/web/api/health.py` (180 行): /health + /metrics (合并了原 /health)
- `deploy/*.yml`: 4 个监控配置

**降级**:
- Redis 不可达 → 缓存降级到源数据 / 限流降级到进程内 dict (仍生效)
- /health 返回 200 + body.status="degraded" 表示有组件故障

**docker-compose.yml**: 加 infra profile (Redis/Prom/Loki/Promtail/Grafana)
- 默认 `docker compose up -d` 不启动
- 启用: `docker compose --profile infra up -d`
- Grafana: http://localhost:35099 (admin/xz.170530)

**累计改动**: v0.2.60 → v0.2.65 = 5 commits
## 2026-08-17

### polish(ui/ux) — 协议 P1/P2 闭环第三轮 (v0.2.64)

**a11y (B 报告 P1-1/4 + P1-10)**:
- **toast**: 加 `role="region"` + `aria-live="polite"` + button `aria-label="关闭通知"`; 单一 toast 按 type 加 `role="alert|status"` 和 `aria-label` (B P1-4)
- **toast 对比度**: `text-emerald-500/red-500` → `600` 级 (B P1-3)
- **Settings.tsx**: 11 个 Input 加 `aria-label` (复用 placeholder 文案) (B P1-1)
- **AppErrorBoundary**: 新建 + App.tsx 包装主路由 (B P1-10) — 任何 subtree 抛错降级 UI 而非整页崩溃

**错误系统强化 (B 报告 P1-5/6 + A 报告 P2-1)**:
- **ErrorBanner**: 新增 `makeErrorId()`, `onDismiss` 按 id 而非 index (B P1-6 — 并发 push 时 index 会错位)
- **ErrorBanner**: 加 maxDisplay=3 折叠 — "还有 N 个源失败" 防横幅占满 (B P1-5)
- **Dashboard pushError**: 同 source 已存在则合并更新 (不是 push) — 防横幅重复堆积
- **api-error.ts**: 新建, `classifyApiError` + `describeApiError` 区分 TIMEOUT/HTTP_5xx/HTTP_4xx/NETWORK (B P1-9)
- **IndexDetail**: 接入 describeApiError — 用户看到"请求超时, 请重试"而非统一"加载失败"

**累计 v0.2.60 → v0.2.64**:
- 4 commits (含 v0.2.63 协议第 2 轮)
- 共 ~26 文件改动, ~330 insertions, ~180 deletions
- 3 轮协议(3 + 6 + 6 = 15 P0/P1 修复)
## 2026-08-17

### polish(ui) — 协议闭环第二轮 (v0.2.63)

**新增/优化(B 方案积累)**:

- **B-1 错误态系统统一**(A P0-3): 6 个高频页面接入 ErrorBanner
  - Reports (代替原 灰色加载失败块)
  - Agents (新增, 解决 P0 静默吞错)
  - PaperTrading (新增, 解决 10 个空 catch 中主页面静默)
  - IndexDetail (代替原 红色裸文字)
  - Notifications (代替原 红色横条)
  - Audit (代替原 红色裸文字)
- **B-2 ErrorBanner auto_dismiss 真起作用**(A P2-1): 死代码变成 5 秒自动关闭
- **B-3 改密码抽公共 helper**(A P1-8): 新建 `src/lib/change-password.ts`, AccountMenu + Profile 改用 `submitChangePassword` (消除双份实现)
- **B-4 UserManagement 5 个 icon button 加 aria-label**(B P0-4): 配置 AI/模块/重置密码/启禁用/删除

**协议驱动**: A 轨 P0-3 + P1-8 + P2-1 / B 轨 P0-4 — 闭环第二轮

**累计未发版改动**: v0.2.60 → v0.2.63 共有 13 文件改动
## 2026-08-17

### fix(regression) — ErrorBanner 重试按钮回归修复 (v0.2.62)

- **v0.2.60 回归**: Dashboard `pushError()` 8 处调用都没传 `retry` 回调, ErrorBanner 的"重试"按钮永不渲染
- 修法: `pushError` 加可选第 3 参数 `retry?: () => void`, 默认挂 `load` 让用户能重试
- 8 处 pushError 全部传 `load` 作为重试回调(包含机会池兜底 + 异动池/热榜 + 5 个快车道接口)

**协议驱动**: B 轨子智能体独立审查时发现, 不在 A 轨报告里
## 2026-08-17

### 5+1 skill 协议评审闭环 (v0.2.61)

**协议执行**: 5+1 skill 协议全栈评审(A 设计 + B 技术双轨独立)

**A 轨(设计评审)**: 6 维评分 3.4/5,3 P0 + 10 P1 + 10 P2
**B 轨(技术审查)**: 7 维评分 2.4/5,6 P0 + 12 P1 + 6 P2

**闭环修正(本版)**:
- **P0-1 Settings 全局搜索空态**: sectionMatches 升级为 sectionSearchHints 关键词字典;空态卡片 + 'N 个区块匹配' 计数(260/1348)
- **P0-2 AnalysisDetail 标题层级倒挂**: H1 16px → 20-22px; text-[12.5px] → 12px(280/284/293)
- **P0-3 错误状态体系分裂**: Stocks 失败横幅接入 ErrorBanner(1858)
- **P0-4 全仓对比度**: text-rose-500/emerald-500/amber-400/blue-500 → 600/700 级(Stocks 29 + 4 文件 25 = **54 处**文本对比度修复)
- **P0-5 Settings 头像保存 catch 留痕**: 加 console.error(625)
- **P0-6 PriceAlerts 11px 字号**: text-[11px] → text-[12px](10 处)
- **ErrorBanner 接入 Stocks 失败横幅**
## 2026-08-17

### polish(ui) — Settings 全局搜索 + 数据源失败显式标识 (v0.2.60)

**Settings.tsx**:
- 新增全局搜索框(Hero 下方):输入关键词,过滤 section(不匹配的隐藏)
- "清空搜索"快捷按钮在 jump pills 右侧
- section 默认全部展开(以后可以改成按需折叠)

**ErrorBanner 组件 + Dashboard.tsx**:
- 新建 `frontend/src/components/ErrorBanner.tsx`:接收 `{source, message}` 数组,显示具体哪个数据源挂了
- 7 处 catch 改用 `pushError(source, message)` 收集具体源(大盘指数/资金流/异动池/热榜/报告/机会池/自选股)
- 替换旧的统一"部分数据加载失败"横幅
## 2026-08-17

### fix(kline) — server.py import 路径修复 (v0.2.59)

- `schedule_one_off` 改用 `import server`(根 module), 不是 `src.web.server`(不存在)
- 加 None 检查: server 未启动时优雅跳过
## 2026-08-17

### fix(kline) — 加股 60s backfill 真触发(跨线程调度) (v0.2.58)

- `kline_backfill_scheduler.schedule_one_off()` 改用 `loop.call_soon_threadsafe` 跨线程
  - APScheduler 跑在自己线程(无 event loop)
  - server 跑在 uvicorn 的 asyncio loop
  - 必须用 call_soon_threadsafe 把 coroutine 派发到 uvicorn loop
- server.py 暴露 `_kline_oneoff_loop` 全局
## 2026-08-17

### fix(kline) — 加股 backfill 修复 (v0.2.57)

- `create_stock()` 修复 `db_stock.market.value` / `db_stock.symbol.value` 类型问题
  (market/symbol 是字符串不是 Enum, 直接用 str() 即可)
## 2026-08-17

### fix(kline) — K线入库去重 + 加股 60s 快速 backfill (v0.2.56)

- **`get_default_symbols()` 加 set 去重** — 多用户各加同一股时, 拉取次数从 52 → 38(0 网络浪费)
- **`stocks` 表加 UNIQUE 约束 `(user_id, symbol, market)`** — 根除重复
- **加股 60s 快速 backfill** — 用户加自选股后 60s 延迟入库, 不必等 18:00 cron
  - `_global_scheduler` 单例, server.py lifespan 启动时赋值
  - `schedule_one_off(symbol, market, delay=60)` API
  - 失败静默, 18:00 cron 兜底
## 2026-08-17

### feature(scheduler) — K线每日 backfill cron 18:00 (v0.2.55)

- 新增 `src/core/kline_backfill_scheduler.py`: 收盘后 18:00 自动入库
  - 拉最近 2 天日 K(覆盖当日 + 周末/节假日补齐)
  - 工作日(Mon-Fri) 18:00 触发, 复用 `klines_ingestor.ingest_batch`
  - 失败 retry(0 行入库 → 7 天兜底)
  - 静默时段跳过非交易日
- server.py 启动/关停这个 scheduler(同 PriceAlertScheduler 模式)
- 手动触发 API: `sched.trigger_now()`(测试用)
## 2026-08-17

### feature(storage) — TimescaleDB hypertable 上线 + K线入库 worker (v0.2.54)

- **PostgreSQL + TimescaleDB 2.29.1** 装在测试机 + 生产(3.6GB 内存 + B 档配置)
- 新建 `klines` hypertable(按 ts 分块 / 7 天一块 / 30 天后自动压缩 / 5 年后自动 drop)
- 新增 `src/collectors/klines_ingestor.py` 后台 worker: 腾讯/东财/新浪 三源并发拉 + 入库(幂等 ON CONFLICT)
- 回测 `data_adapter.py` 改造: 优先查 PG klines 表(~70ms), fallback 联网拉
- K线 API `klines.py` 改造: 同样优先查库, 标注 `source: "pg_klines_hypertable"`
- 入库 5 只股 800 天 × 3 数据源 = 12,540 行, 写入 ~2,000 行/秒
- 测试机: 124,800 行写入 6 秒; 单股 800 天查询 70ms; 聚合查询 45ms

回测/前端 K线查询不再每次联网, 速度 ~5-10x 提升。
## 2026-08-16

### feature(rbac) — 设置页/导航权限细化(member 只见个人配置)

- Settings 页 owner-only 区块(member 隐藏): AI 服务商&模型+场景分配、接口 Key、
  同花顺登录、系统设置、配置包(导入/导出)、Hero 快捷按钮(导出配置包/配置 AI)
  与服务商/模型统计徽标; member 保留个人配置: 通知渠道(per-user)、我的服务商
  (BYOK)、定时报告订阅(per-user)、AI 调用统计、反馈
- 侧栏「数据源」导航 owner-only(与审计页同模式): member 打开 /datasources
  全部 API 403(manage_datasources), 页面本就不可用; 路由守卫跳首页
- 后端写接口已有中间件拦截(manage_*), 本次为前端展示层对齐, 无需后端改动

### fix(rbac) — 机会页策略功能对 member 全 403(v0.2.47 迁移遗留)

- v0.2.47 把策略库并入机会页(member 可见), 但 `/api/strategies` 仍在中间件
  管理区(manage_strategies) → member 机会页策略筛选下拉永远为空、扫描按钮 403
  (前端 catch 静默吞掉, 无任何提示); 该前缀下 list/get/scan/apply 全部为只读或
  纯计算端点(无写操作, 策略写入在 /api/recommendations) → 移出管理区
- `theme_launch_detector.py` 补 `AgentContext` 导入(其他 agent 均有,
  缺失导致 IDE/mypy 解析注解报错; 运行时因 `from __future__ import annotations` 未炸)
- demo(guest)隔离策略保持不变(只读演示定位)

### fix(rbac) — 子用户模型授权全链路失效(三层修复)

- **granted 语义修复**(`src/core/ai_client.py`): 旧逻辑把授权列表当"全局场景模型白名单"
  —— 场景绑定模型不在列表内即全场景 None,owner 授权了模型子用户也用不了;
  新逻辑: 场景绑定模型在列表内优先用,否则从授权列表挑(is_default 优先/id 升序),
  授权什么就能用什么; 空列表仍为显式全禁
- **热路径接入用户级解析**: 聊天(`chat.py _get_ai_client` 传 user + 会话显式模型过
  granted 校验)、Agent 触发(`stocks.py` → `trigger_agent_for_stock` 注入 context.user,
  后台线程只传 id 重加载)、加仓评估/公告解读(insights 两端点)、图片描述(vision 场景)
  全部走 BYOK/平台授权; 调度器系统级调用(无 user)行为不变
- **越权拦截**: deny_all/granted 空列表用户手动触发 Agent 时预检直接返回
  "管理员未给当前用户授权任何 AI 模型"(旧逻辑会静默保留全局 client 造成越权)
- **中间件权限调整**(`src/web/app.py`): `GET /api/agents` 放行(member 个股 AI 分析页
  需拉 Agent 列表, 旧配置连只读都 403); 移除死配置 `/api/reports/generate`(无对应路由)

### test(rbac)

- 新增 4 个 granted 行为用例: 从授权列表挑模型/多模型排序/空列表全禁/场景绑定在列表内优先
- `test_chat_stream` mock 签名适配 `_get_ai_client(db, model_id, user)`

## 2026-08-15 (v0.2.38)

### feat(settings) — 设置页第二窗口改造(对齐 AI 服务商模式)

- 接口 Key 区块 → 合集卡片(悟道/智兔/通达信一行一卡 + 已配置/未配置徽标)+「管理」第二窗口编辑(密码框/眼睛切换)
- 系统区块 → 合集行(描述+当前值摘要)+「编辑」第二窗口编辑
- 未修改时保存按钮禁用,防掩码覆盖真实 token

### fix(settings) — 敏感 key 掩码脏写(生产卡死根因)

- list_settings 掩码改为返回新对象,不再修改 ORM 对象 → 消除 autoflush 把字面 `********` 写回 DB 的隐患
- 该 bug 曾导致 SQLite 锁竞争 → 事件循环阻塞 → 生产 7 小时无响应(已热修+本版固化)

### ci — GitHub 源镜像自动构建修复

- GHCR Actions: npm → pnpm(workspace 依赖 echarts 等装不上导致 v0.2.36/37 构建全失败)
- 补传 VERSION build-arg;tag 保留 v 前缀;labels 更新(SIDA/AGPL-3.0)

### license / docs

- 许可证 GPL-3.0 → **AGPL-3.0**(防 SaaS 白嫖,网络服务必须开源改动)
- README: 赞助区恢复(微信赞赏码不打码)/ K线主力意图截图 / 生产部署双镜像源(ghcr+ACR)/ 删除开源许可描述
- 对话助手截图更新

### test

- marketdata registry 漂移校准(同花顺 ths/ths_f10 vendor 后加未同步测试)
- get_market_news 断言兼容 to_thread 写法

# Changelog

## 2026-08-18

### feat — AuditMiddleware 操作审计全覆盖 (v0.2.65.5)

- 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 补齐渠道/服务商/数据源/设置/用户等管理操作审计
- 内部自行 decode JWT, 独立 session 异步落库, 失败静默不阻塞
- 排除 auth(已有埋点)/静态/health/webhook

## 2026-08-14 (v0.2.37)

### feat(wechat) — 个人微信 iLink 直连全链路(零 OpenClaw 依赖)

- 扫码绑定: 设置页扫码 → 腾讯官方 iLink 授权(纯 Python 直连 ilinkai.weixin.qq.com, 参考 Hermes weixin.py 架构)
- 双向对话: 微信里直接和「数智分析BOT」对话(长轮询 getupdates → AI 回复 → sendmessage)
- 回复状态: 微信显示「正在输入」(getconfig typing_ticket + sendtyping)
- 媒体消息: 微信发图片/文件/链接 → iLink 媒体下载(AES-128-ECB 解密)→ OCR/解析 → AI 分析
- 多模态: 图片由视觉代理(agnes-2.5-flash)看图描述 → deepseek 分析(自称保持数智分析BOT)
- 推送自称: 所有微信推送以【数智分析BOT】开头
- 会话自愈: context_token 自动刷新(推送失败 → getupdates 拉新重试)

### feat(chat) — 对话助手多模态 + 链接抓取

- 网页上传图片/文件: POST /api/chat/upload(20MB, 图片 OCR / Excel / PDF / txt 解析)
- 链接抓取: get_web_content 工具(html.parser 正文提取, 3000 字截断, SSRF 防护)
- 视觉代理场景化: 设置页「场景分配」新增 vision(视觉代理/图片识别), 可随时更换多模态模型

### feat(reports) — SIDA 内置报告生成器(不再依赖 Hermes cron 同步)

- 盘前(8:30)/盘后(15:30)交易日自动生成: 数据收集(指数/资金流/涨停/持仓/信号)+ LLM 生成
- 直接归集报告中心(数据卷持久化), 数据获取失败显式标注, LLM 失败模板降级(不编造)
- 去掉 Obsidian 依赖(后端/前端/部署配置全清, 其他用户零安装)

### feat(branding) — 改名 + 文档

- 对话机器人 → 数智分析BOT; 登录页/引导弹窗 → 数智分析 SIDA(旧名盯盘侠全库清零)
- README 完全重写(突出 AI 全链路 + 截图打码入档)
- 仓库改 PRIVATE(商业分版: 入门版开源 / 专业版闭源)

### compliance — 合规加固

- 免责声明全链路: 对话 SYSTEM_PROMPT(买卖倾向必须附「仅供参考, 不构成投资建议」)/ 登录页 / 预测页 / 报告
- 移除 GPL 依赖 backtrader(TradingAgents 间接安装但未使用, 商业镜像不再含 GPL 代码)

## 2026-08-14 (v0.2.36)

### fix(darkflow) — 跨日残留洗白 + AI 反证层 db 自建(生产热修, 已 docker cp 生效)

- 跨日残留: 昨收后接口异常只拉到少量残留时, 增量续拉"无新增"分支把残留 day 刷新成今天, 跨日 stale 判断被绕过, 主力意图永远拿残留(实测 tick=2)。修复: 无新增时校验旧数据新鲜度(未来时间/早于开盘且笔数<30 → 全量重拉), 生产 tick 2→2392
- AI 反证层: 生产容器 env 无 AI_* 配置且调用不传 db → 回落空配置永远 None。修复: db=None 时内部自建 SessionLocal 读场景绑定, 生产实测输出正常

### build(docker) — 国内 ACR 代码源构建适配

- 基础镜像: docker.io 超时/阿里云 library 需登录 → DaoCloud 公开镜像(docker.m.daocloud.io, 免登录实测可用)
- pip → 阿里云 pypi 镜像; pnpm → npmmirror
- VERSION: ACR 个人版无构建参数功能({{.Tag}} 不支持), Dockerfile 兜底读仓库根 VERSION 文件
- 验证: ACR 构建 v0.2.36 成功, 国内 docker pull 正常

## 2026-08-14 (v0.2.35)

### feat(darkflow) — 主力意图算法增强五件套

- **①超大单/大单背离**: 超大单拉抬+大单出逃+价格滞涨=托盘出货(危险); 反向=压盘吸筹。±800万阈值
- **②量价背离**: 主力净流入但价格不涨=对倒/换手嫌疑; 净流出但价格抗跌=压盘吸筹。±500万
- **③时段节奏**: 早吸尾抛=拉高出货 / 早压尾拉=洗盘 / 尾盘方向与全天背离=尾盘异动。±300万
- **④托单/压单识别**(新模块 board_snapshot): 腾讯五档 30s 间隔采样, 挂单量×1.5+价格滞涨 → 托单诱多/压单吸筹; 海外节点 v_ 接口被拦截自动降级无前缀
- **⑤AI 反证层**: 算法结论+当日公告(东财 events) → LLM 评级(支持/存疑/错误)+置信度(高/中/低); 8s 超时+异常全静默降级; 数据不足(<30笔)跳过 AI 判断
- 主力意图卡片新增字段: divergence / price_divergence / rhythm / board / ai_verdict(前端待展示, 接口已就绪)
- 全量回归 617 passed(3 failed 为真实数据/网络偶发, 与改动无关)

## 2026-08-14 (v0.2.34)

### fix(login-timeout) — 登录请求超时修复(生产热修, 已部署)

- **根因**: 海外节点外部数据源(wudao MCP/智兔/东财)抖动挂起时, async 端点里同步 `requests/urlopen`(timeout 30-60s)阻塞 asyncio 事件循环 40s+, 所有请求(登录/health)排队超时; healthcheck 超时 fork 的子进程不被 PID1 reap, 堆积 87 个僵尸 + 容器 unhealthy 7h
- **修复**: chat.py `_execute_tool`(主力意图/拉升分析/问小达/wudao 热榜+简报)、tdx.py、quotes.py 同步网络调用全部包 `asyncio.to_thread`; wudao_mcp_client 超时收紧 `(5,25)`; server.py `SIGCHLD` 置 `SIG_IGN` 自动回收僵尸
- 实测: health 5 连测 2ms(修复前偶发 18-49s), 登录 7ms, 僵尸 87→0

### feat(chat) — 重复提问守卫(dsh loop-hygiene)

- 同股+同意图连续提问 ≥3 次 → 模型回复注入温和提醒("是否已获答案?可问:主力意图/资金流向/技术形态"), 只提醒不阻断
- 同股不同意图=正常深化不触发; 换话题重置; 阈值常量可调
- 实测: 10/10 用例(4连问/换话题/阈值5/日期金额不误报)

## 2026-08-14 (v0.2.33)

### style(ui) — 反 AI 模板 P2 + 收尾

- **P2 Dashboard 去卡片化**: 大盘资金流/异动池/热榜 card-subtle 盒 → border-t hairline + 留白直排; 段落头 8处图标→2处(仅保留要紧事/体检); 指数 pills 保留(可点击元素)
- **⑤ 预测文案防既成事实**: 预测结果/历史表/详情弹窗加"模型预测"限定, 目标价→模型目标价; DigestShareCard 标"涨跌幅为当日实际行情"
- **④ card-hover 去光晕**: primary 彩色光晕 → 亮度抬升+中性边框(暗色无阴影, 亮色轻灰阴影)
- 实测: build 8.70s, 审计零回归

## 2026-08-13 (v0.2.32)

### style(ui) — 反 AI 模板改造(P0+P1, 基于 hallmark 58门审计)

- **P0 清理**: SelfCheckModal 紫蓝渐变→品牌色; transition-all 23处→指定属性; emoji 10处→Lucide(✅🔥✨💧→CheckCircle2/Flame/Sparkles/Droplets)
- **P1 治本**: --primary 234默认蓝→215深青钢蓝(A股数据感); 中性色 220→215 色度化; 纯白卡→纸面白; 红涨绿跌不动
- **数字字体**: --font-num 等宽数字栈应用 8处核心数字(指数/涨跌幅/预测值/持仓市值); tabular-nums 11→22处
- 新增审计工具: bash ~/workspace/research/panwatch-ui-audit.sh(58门→11自动检查项)
- 实测: build 8.70s, 纯白卡清零, transition-all/emoji/紫渐变全零

## 2026-08-13 (v0.2.31)

### feat(dashboard) — 首页顶部持仓速览条换成最新报告区

- 顶部"组合速览条"(当日盈亏/累计浮盈/60日超额/仓位)→ "最新报告"区
- 最近 4 条 Hermes cron 报告: 标题 + job名 + 相对时间(刚刚/N分钟前/今天HH:MM), 点击跳报告页
- 并入首页 30s 自动刷新(visibility 暂停 + 防叠加 + 静默更新), cacheMode:reload 保证轮询拿新数据
- 空态提示 + 首载骨架; 持仓数据不丢(仍在持仓页/组合体检)
- 实测: /api/reports/list 返回预测复盘16:03/盘后复盘15:30, build 8.92s

## 2026-08-13 (v0.2.29)

### feat(dashboard) — 首页加异动池+热榜

- 后端: GET /anomalies(东财异动池, 结构化JSON) + GET /hot-stocks(同花顺热榜, hour/day)
- 前端: Dashboard 大盘资金流下方两个并排区块
  - 异动池(AlertTriangle·东财): 涨跌幅/累计偏离+天数/规则/当日角标
  - 热榜(Flame·同花顺): 排名/热度/概念标签/AI归因
  - 点击行打开个股详情 + 右键菜单, 独立加载失败静默, 30s自动刷新同步
- 实测: 哈药股份偏离+192.27%(25日) / 太极实业热榜第1(存储芯片·先进封装), 14 passed, build 8.73s

## 2026-08-13 (v0.2.28)

### feat(chat) — AI 助手打磨(第四轮)+ 数据源激活

- **建议问题动态化**: 按今日状态生成(机会候选/未到期预测/未读通知/持仓浮亏 → 通用模板兜底, 最多5条)
- **数据源口径标注**: get_main_intent 标[腾讯逐笔·主力意图口径], get_capital_flow 标[东财四档·资金流向口径], 提示词加口径规则
- **上下文摘要滚动**: 历史>20条时旧消息压缩成【早期对话摘要】(规则抽取结论句, 不调LLM)
- **今日要闻横条**: 空对话时顶部展示未读通知(3条), 点击自动提问+标记已读
- **激活3个死代码数据源为对话工具**:
  - get_irm_qa: 巨潮互动易问答(公司官方回应, 题材潜伏信源)
  - get_market_anomalies: 东财异动池(规则码+累计偏离+窗口)
  - get_hot_stocks: 同花顺热榜(小时/日榜, 热度+概念+AI归因)
  - 对话助手工具 16→19

### feat — 基本面明细可见

- 后端: GET /fundamentals-detail/{symbol} 合并端点(龙虎榜回溯10日+两融+股东+分红+事件), 每类独立容错
- 对话工具: get_fundamentals_detail(五类分列, 空类显"暂无", 金额亿/户数千分位)
- 前端: 个股详情弹窗新增第9个tab"基本面"(K线与公告之间), 五分区卡片, 懒加载+优雅降级

### 实测
- 互动易: 002361 火箭题材官方回应(碳纤维火箭/朱雀三号) ✅
- 异动池: 哈药股份累计偏离+192.27%/25日 ✅
- 热榜: 太极实业第1 热度79万 概念存储芯片/先进封装 ✅
- 基本面: 002361 龙虎榜净买-1.53亿/两融3.81亿/股东19万户/分红13次 ✅
- 14 passed, pnpm build 通过

## 2026-08-13 (v0.2.27)

### feat(chat) — 对话助手打通系统数据(系统管家)

- 新增 4 个工具: `get_forecast`(读预测引擎库: 方向/目标价/置信度/到期状态)、
  `get_opportunities`(机会候选 active)、`get_strategy_signals`(策略买/关注信号)、
  `get_notifications`(通知/提醒, 支持未读过滤)
- 对话助手从"市场分析师"升级为"系统管家": 可回答"系统预测了什么/今天发现什么机会/哪个策略给了信号/有什么通知"
- 预测库独立文件只读连接(mode=ro), 主库走应用 Session

### feat(ui) — 第三轮打磨

- **预测权重透明度**: 预测结果区展示当前 4 模型权重(Kronos 44% / Chronos 34% / XGB 11% / 线性回归 11%, 按历史命中率动态调整; 数据来自生产落盘权重文件, 留接口化 TODO)
- **移动端底部导航**: 5 槽位改 首页/持仓/机会/预测/提醒(模拟盘移入"更多"下拉), 桌面端导航不变
- **机会页刷新反馈**: 提交后按钮变"刷新中"防重复提交, 10s 轮询任务状态 → 完成 toast + 自动重载, 超时提示"1-3 分钟"

### 实测
- pnpm build 8.70s, 4 个新工具真实库验证通过(002361 预测/今日候选/策略信号/通知全返回)
- 机会刷新轮询实测: 任务 14s 完成, running 变 false, 完成 toast 触发

## 2026-08-13 (v0.2.26)

### 数据质量 — 去噪音 + 验证闭环

- **扫描池 A 股化**: market_scan 只扫 CN, 港美股不再生成快照/候选(实测占库 65% 噪音)
- **候选池精选化**: entry_candidates 只存 active 有信号记录(原 72% 观望占位不再落库)
- **自选去重**: (symbol, market, user_id) 唯一约束迁移(跨账户各自保留, 只防同账户重复)
- **权重闭环修复**: 只聚合 runs/live 回测(弃 legacy 污染) + MIN_SAMPLES 10→3 + 贝叶斯收缩
  → linreg 0.262(最高)→0.110, kronos 0.441 主导; 双写文件打通
- **预测节流**: 同 symbol 未到期不重复预测(HTTP 409 + force 参数)
- **候选验证闭环**: 扫描集不截断+最老优先, 到期即 100% 验证(真实库 401/401), 缺口报告
- **dark_flow 跨日修复**: 逐笔缓存加交易日字段, 跨日强制全量重拉(曾返 2 条残留)

### UX — 流式 + 自动刷新 + 可视化

- **聊天流式输出(SSE)**: stage 阶段提示 + delta 打字机 + done 落库, 兼容非流式
- **Dashboard/通知 30s 自动刷新**(visibility 暂停 + 防叠加)
- **骨架屏组件**(SkeletonRows)接入 Dashboard/通知
- **预测页四模型分歧度可视化**(纯 CSS 区间条图)
- **机会卡片 👍/👎 反馈按钮** + 后端 GET 接口补齐
- MinuteLwcChart 存量 TS 错误清理(build 前置)

### 实测
- 624 passed(新增 5 个测试文件), pnpm build 通过
- 真实库副本迁移验证: 48 条不变(跨账户保留), 唯一索引拦截同账户重复

## 2026-08-13 (v0.2.25)

### fix(frontend) — 首页白屏(旧 SW 缓存 + SPA 资源回退 HTML)

**根因**: 两个问题叠加:
1. SW 缓存名固定(panwatch-v13), 发版后旧缓存不清, 用户浏览器回退旧 index.html
2. SPA fallback 对所有路径返回 index.html —— 旧 index.html 引用的旧 hash JS 已不存在,
   服务器返回 HTML → 浏览器把 HTML 当 JS 执行 → 语法错误 → 整页白屏

**修复**:
- server.py: 静态资源(.js/.css/图片等)不存在时返回 404, 绝不回退 index.html
- sw.js: CACHE_NAME 注入版本号(构建时 sed), 发版后 SW 字节变化 → 浏览器自动更新清旧缓存
- sw.js: 不再缓存 '/' (index.html) 且导航请求不回退旧 HTML —— 旧 HTML 残留是白屏根源

### 实测
- 前端 build 9.2s, dist/sw.js 缓存名 = panwatch-v0.2.25
- server.py 语法 OK

# Changelog

## 2026-08-18

### feat — AuditMiddleware 操作审计全覆盖 (v0.2.65.5)

- 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 补齐渠道/服务商/数据源/设置/用户等管理操作审计
- 内部自行 decode JWT, 独立 session 异步落库, 失败静默不阻塞
- 排除 auth(已有埋点)/静态/health/webhook

## 2026-08-13 (v0.2.24)

### fix(ai) — 场景绑定跨服务商 404 "model is not found"

**根因**: 统一 LLM 配置中心场景绑定时只改 `ai_client.model` 字符串, base_url/api_key 未同步切换。
agent_configs 里 premarket_outlook/daily_report 绑定商汤 deepseek-v4-flash, reports 场景绑定 agnes-2.5-flash,
绑定后请求仍发往商汤 API + agnes 模型名 → 404 (商汤无此模型)。商汤 API 实测有 deepseek-v4-flash,
直接调用正常, 故 404 非 key/模型缺失, 而是绑定切换不完整。

**修复**:
- src/agents/base.py `apply_scene_binding`: 绑定命中时整体重建 AIClient(base_url+api_key+model 一起换), 保留 total_tokens_used
- src/web/api/insights.py 两处(评估/公告解读): 改走 `_client_from_scene_cfg` 整体重建(原实现 _coerce_bound_model 返回字符串, bound.get() 静默失败)

### 实测
- 模拟 build_context(商汤 deepseek) → apply_scene_binding(reports) → client 变为 agnes base_url/model, 真实 chat 成功
- 231 tests passed

# Changelog

## 2026-08-18

### feat — AuditMiddleware 操作审计全覆盖 (v0.2.65.5)

- 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 补齐渠道/服务商/数据源/设置/用户等管理操作审计
- 内部自行 decode JWT, 独立 session 异步落库, 失败静默不阻塞
- 排除 auth(已有埋点)/静态/health/webhook

## 2026-08-13 (v0.2.23)

### refactor(settings) — 删除多余模型引擎配置(统一 LLM 配置中心)

- 删除设置页「预测引擎 LLM(情绪打分)」配置组(forecast_llm_base_url/model/api_key 三个输入框)
- 删除设置页「预测引擎模型清单」只读区块(loadForecastModels + /forecast/models 前端调用)
- 后端: SETTING_DESCRIPTIONS / SECRET_SETTING_KEYS 移除 forecast_llm_* 三键, 删除废弃路由 /forecast-llm-config 与 /forecast-llm-sync-guide
- 预测引擎 AI 裁判/情绪打分模型统一走「场景分配」(ai_scene_bindings), 旧 forecast_llm_* 仅存 DB fallback(无 UI 入口)

### 实测
- 前端 build 9.0s, 产物无 forecast_llm 残留
- 后端 settings 相关测试 17 passed

# Changelog

## 2026-08-18

### feat — AuditMiddleware 操作审计全覆盖 (v0.2.65.5)

- 所有 2xx 写操作(POST/PUT/PATCH/DELETE)自动落 audit_logs, 补齐渠道/服务商/数据源/设置/用户等管理操作审计
- 内部自行 decode JWT, 独立 session 异步落库, 失败静默不阻塞
- 排除 auth(已有埋点)/静态/health/webhook

## 2026-08-13 (v0.2.22)

### fix(shadow) — 影子账户页白屏(React #31)

- 根因: rules 是 ShadowRule 对象数组(含 human_text), 前端却当字符串直接渲染 → React error #31 → 整页空白
- 修复: ruleLabel() 统一取 human_text(兼容字符串/对象), 两处渲染(我的画像区 + 行为画像区)
- key 改用 rule_id / index(对象不能作 key)

### 实测
- 前端 build 8.4s

## 2026-08-13 (v0.2.21)

### feat(shadow) — 影子账户"我的画像"区

- 进页面自动加载已存画像(profile_text 全文 + 盈利回合/总回合/胜率/偏好市场/持仓中位 5 指标 + 规则标签)
- 无画像显示引导卡片; 失败静默不阻断上传
- "更新画像"按钮复用上传文件选择器; 上传完成后自动刷新画像区



### feat — 统一 LLM 配置中心

- **场景绑定**: ai_scene_bindings 表, 6 场景(对话助手/TradingAgents/报告/AI裁判/自检/机会评分)各自绑定模型池模型, 回落默认。
- **画像注入**: build_system_prompt 统一入口, 有画像自动追加"用户交易风格画像"段, 无画像用默认提示词。
- **前端**: 设置页"场景分配"区(6场景×模型下拉, 绑定/解绑)。
- **停用独立预测情绪打分**: 消息面判断由 AI 裁判接管(用户决策)。

### feat — 内盘外盘口诀 + 分时双卡片

- **7 条实战口诀规则**: 真金进攻/主力撤退/诱多出货/压盘吸筹/多空平衡/控盘洗盘/对倒造假(位置优先+量价结合)。
- **轻接口**: GET /api/dark-flow?symbol= → {main_intent, inner_outer, mnemonic}。
- **分时图双卡片**: 主力意图 + 内盘外盘, 渲染在量柱下方; 背离时"咨询AI助手"按钮带上下文跳对话。

### 实测
- 600 tests passed
- 002361: 内外盘 48.6/49.3 均衡无口诀命中(符合预期), signal=主力净流入(主动买占优)低位承接
- 前端 build 8.8s



### feat(shadow) — 影子账户画像落地

- **A 方案**: 交割单画像落库 users.shadow_profile_json(按用户隔离) + 对话助手 system prompt 注入用户交易风格(截断300字+前3条规则, 无画像零开销)。
- **B 方案**: AI 裁判评估时注入用户画像(仅影响建议贴合度/表达方式, 不改 verdict/direction 判断)。
- 新增 GET /api/shadow/profile 端点; database.py 迁移自动加列(旧库兼容)。

### fix(forecast) — 预测单日超涨跌停

- clip ±40% → ±25%(linreg 外推 +39.7% 未被截断污染投票)。
- 新增单日 ±10% 物理约束(超限等比压缩, 方向不变)。
- 不做首日温和化(活跃票涨停常态, 用户决策)。

### 实测
- 画像注入: 对话助手输出"你423笔回合216笔盈利, 持仓中位5天" ✅
- 预测: 002361 T+1 +9.7% 单日 ≤10% ✅
- 27 tests passed


### fix

- 增强主程序 Docker 镜像安装 Debian 系统依赖时的下载重试、读取超时和 HTTP 管线容错，避免字体及 Playwright 运行库因上游 EOF 或临时 500 响应导致构建中断。
- 增加主镜像版本文件的非空构建校验，避免磁盘写入异常时生成缺少版本标识的可部署镜像。
- 增加主镜像 Python 依赖下载容错及 SQLAlchemy、行情本地包的构建期导入校验，阻止依赖层不完整的镜像进入部署流程。
- 将定时价格提醒的完整扫描移入工作线程，避免同步 SQLite 等待占用 Web 事件循环并造成健康接口间歇性超时。
- 移除服务启动 15 秒后自动执行的完整后验评估补跑，避免历史数据较多时启动阶段长期占用解释器并阻塞 Web 请求；保留每 6 小时维护计划和手动执行入口。
- 将主镜像系统依赖安装升级为同一构建层内的整体重试：保留已下载的 Debian 包并补拉失败项，解决 APT 连接重试无法覆盖单包 CDN 500 响应的问题。

### update

- 将本地开发 Compose 的主程序与预测引擎镜像标签从 `dev-0.1.1` 升级为 `dev-0.2.0`，并同步主程序内置版本号，避免部署 v0.2.x 代码时仍显示旧开发版本。
- 增加预测引擎 Docker 构建安装 Python 依赖时的网络超时与重试配置，避免 SciPy、XGBoost 等大型依赖在慢速网络下读取超时导致构建中断。

### fix(shadow) — 交割单上传无反馈

- **前端 accept 缺 .pdf**: 文件选择器直接拒绝 PDF 交割单(用户点文件无反应),已加 `.pdf`。
- **前端 20s 超时**: fetchAPI 默认 20s,但 586 笔交割单分析实测 111s,请求被 abort 静默失败;已提升到 180s。
- 提示文案更新: 支持 .csv / .xlsx / .pdf。

### 实测
- 生产 PDF 上传成功(shadow_c5c4c54e, 111s), 后端无问题, 问题纯在前端。



### feat(forecast) — 预测引擎全面升级

- **模型体系重构**: 移除 Lag-Llama(50%命中+自回归爆炸),接入 **Chronos-Bolt-small**(Encoder无累积误差, CPU 0.07s),投票改**加权平均**(XGBoost 0.4 / Kronos 0.25 / Chronos 0.25 / 线性回归 0.1,保留±40% sanity clip)。
- **AI 裁判层**: 模型预测结果交给对话助手评估(调 get_main_intent 主力意图/资金流/技术面/K线形态),裁判可**强势改最终方向**(B方案),verdict+理由入响应,失败自动降级不阻断。
- **权重闭环**: 新增 model_weights.py,模型权重按历史命中率(命中率²强化+下限8%保护)动态调整,回测后自动更新。
- **置信度融合**: 置信度融合 4 模型分歧度(≥75%一致升档/≤50%降档)+ AI裁判意见(confirm背书/adjust质疑)。
- **LLM 情绪修正**: 修复埋点 bug(adjustment_pct 全 0 → 真实记录 score×0.75);接入**腾讯近5日主力资金流**(免key)进 LLM prompt,资金面影响情绪打分。
- **交易日口径统一**: prediction_outcome 改用交易日(工作日)计算 horizon,与回测引擎一致。
- **历史到期对照**: /forecast/history 增加 outcome_return_pct/outcome_status(hit/miss/pending),前端历史表展示预测vs实际。
- **前端**: 副标题更新(Kronos+Chronos-Bolt)、回测注脚诚实文案、自选/持仓一键预测(单选)、预测进行中锁定输入(严格单只)。
- **修复**: 服务 token sub 用 owner 真实 UUID(此前固定 "user" 导致 401)。

### 实测
- AI 裁判真实调用通过(conv_id=27, 裁判引用主力意图/技术面数据给出 confirm+up)
- 4 模型加权投票 vs 旧中位数: final 12.86 vs 12.5(XGB 权重生效)
- 历史到期对照: 002361 miss(-3.04%) / 000938 hit(-0.97%) / 002661 hit(+3.87%) 全对
- pytest 4 passed + 前端 build 8.5s



### fix(shadow) — 影子账户交割单上传

- **修复上传报错 422 `missing file`**: `fetchAPI` 对 FormData 不再强制设置 `Content-Type: application/json`(由浏览器自动带 multipart boundary),FastAPI 才能收到 `file` 字段。
- **修复交割单解析**: Excel/PDF 统一走"文本行 → 去 nan 占位/相邻重复 → 业务名称关键词定位 → 10 列对齐"解析,兼容券商交割单的合并单元格展开、表头不在首行、中文名称被拆开等情况。
- **新增 PDF 格式支持**: 允许上传 .pdf 交割单(pypdf 提取文本),与 Excel 版解析结果一致(实测 PDF 更完整,xlsx 导出偶有漏行)。
- 已实测: xlsx 562 笔 / PDF 586 笔解析成功,端到端上传 → 画像 → 报告全通,回归测试 14 passed。



### feat(auth)

- **修改密码功能**: 头像下拉菜单新增「修改密码」→ 弹窗(旧密码/新密码/确认新密码)。
  - 后端 `/api/auth/change-password` 增加旧密码校验(错误返回 400「旧密码不正确」),新密码 ≥ 8 位;
  - 改密成功后 `token_version += 1` 使该用户所有旧 token 失效,强制重新登录(防会话劫持);
  - 前端校验:新密码长度、两次输入一致(红色提示),成功 toast「密码已更新」,失败展示后端错误;
  - 已通过 pytest(12 认证测试) + tsc + 浏览器全流程实测。



### feat(ui)

- **PC化改造(5项, 多智能体流水线完成)**: ①桌面导航13项全平铺并按业务分组(行情/交易/系统), 不再藏进头像下拉; ②界面密度三档体系(compact/normal/comfortable, 通过 html[data-density] + CSS 变量驱动, 默认 normal 完全不变); ③PC快捷键(⌘/Ctrl+K 打开日志、⌘/Ctrl+, 跳设置、g→d 首页、g→p 持仓、? 帮助, 桌面端≥768px生效、输入框内自动失效); ④股票行右键菜单(加入自选/查看详情/复制代码/模拟买入, Stocks+Dashboard 共用 StockContextMenu 组件); ⑤Dashboard 多列工作台(≥1280px 三列: 今日要紧事3|组合体检6|机会精选3, 次级两列简报6|机会发现6, 1280px以下逐像素回退)。
- 全部改动经生产构建(vite build) + tsc 编译 + 浏览器实测验证。



### perf

- **汇率源新浪→腾讯**: 海外节点新浪超时(每次冷启动白等 3s×2), 腾讯 qt.gtimg.cn 秒回。
  失败短缓存 5 分钟 + 线程锁防并发重复等待 → **持仓/自选汇总 35.71s → 0.57s 冷 / 0.006s 热**
- **逐笔增量续拉**(用户设计): 缓存记录 last_page+last_seq, 过期只拉新增页合并去重,
  序号断裂(新交易日)才全量重拉 → summary 冷启动大幅加速, TTL 内零请求
- **summary 一次计算**: `_main_intent_both` 一次 compute_dark_flow 产出字符串+结构化(原各调一次翻倍耗时)
- **筹码分布腾讯优先**: 腾讯当日分价(0.17s 秒回)优先, 新浪(海外慢 8-10s)兜底 → summary 冷启动 4.16s→2.48s

### fix

- `data_status`/`tick_count` 透传到主力意图正常路径(与 insufficient 分支一致)

## 2026-08-12 (v0.2.12)

### fix

- **腾讯逐笔单行页被误判为空页(2026-08-12 盘中修复)**: 竞价/开盘初期只有 1 笔成交(如 09:25:00 竞价单)时, `_fetch_all_ticks` 的 `len(rows) < 2` 空页判定把它当空页丢弃 → 逐笔为空 → 主力意图返回 None → 前端"看不到"。修复: 空页判定改为 `['']`(真正空页), 单行页正常处理。
- **竞价/开盘初期数据量门槛**: 非竞价成交 <30 笔时 `data_status=insufficient`, 前端显示"数据不足(N笔)"占位, 不标误导性吸筹/派发箭头。
- **dark_flow 真实数据测试弹性化**: 断言从"盘后全天 >1000 笔"改为盘中/盘后自适应(盘中只要求非空), 盘中跑测试不再误报。

## 2026-08-12 (v0.2.10)
### fix

- **主力意图方向判据对齐 v14(2026-08-12 用户发现)**: `_main_intent_structured` 的 direction 原逻辑只看主力净额(>500万=买 / <-500万=卖), 与同花顺"表面结论"同样粗暴——神剑股份 002361 主力净流出-2466万(超大单+5967万/大单-8433万)被误判"派发"。修正为 v14 完整判据: 参与度≥35% 且 买占≥48% = 强吸筹力度, 净流出+强吸筹 → wash(洗盘吸筹), 净额平衡+强吸筹 → absorb(疑似吸筹)。K线 markers 同步支持 5 档(吸筹红↑/洗盘吸筹橙↑/疑似吸筹黄↑/派发绿↓/平衡), 图例加参与度/买占展示。注: 同花顺同数字判"派发"是它的结论规则问题(只看净额), 数据本身与我们逐笔一致。



### feature

- **TradingView Lightweight Charts v5 升级**(图表重构):
  - K线图 multi-pane 原生多面板(价格/量能/MACD/RSI 四区), 替代原 3 个独立 chart + 手动可见范围 sync
  - 主力意图 markers: 吸筹↑红/派发↓绿 标在最新 K 线, 近 60 根涨停/跌停点自动标注
  - 筹码叠加: 筹码峰/成本带上沿/下沿 price lines(黄线筹码峰, 灰线成本带)
  - 主力意图图例卡: 方向/净额/超大单大单/筹码峰/成本带/获利盘
  - 分时图从 ECharts 迁至 Lightweight(价格+均价+昨收虚线+量能 pane), 砍掉 ECharts 依赖, 前端统一一套图表库
- **TradingView Alert Webhook 接收端点**: `POST /api/webhooks/tradingview`(X-PanWatch-Secret 鉴权, 环境变量 PANWATCH_TV_WEBHOOK_SECRET; 未配置时禁用), 用户 Pine 策略 Alert 可直接推送 PanWatch 告警 → 站内通知 + 外发渠道

### fix

- klines summary API 新增 `main_intent_structured` 结构化字段(方向/净额/筹码峰/成本带), 前端 markers/筹码叠加不再依赖字符串解析

## 2026-08-12 (v0.2.9)

### fix

- **腾讯逐笔翻页截断修复**(`src/core/dark_flow.py`): 原逻辑遇第一个空页即 break, 腾讯偶发限流让单页返回空时, 盘中实时拉取会中途截断 → 主力意图算在残缺数据上。实测第60页首次请求为空、重试后正常返回。修复: 连续 2 个空页才停, 单页空(限流)自动跳过继续翻页。
- **事件溯源 v2**(盘中监测): 题材词改为包含匹配(修复"机器人概念"≠"机器人"导致题材词池为空), 命中条件=标题必须含该股题材词, 通用事件词仅用于发现不直接命中(修紫光股份 000938 误报火箭实验室新闻); 看涨形态去重限流(十字星变体合并, 同方向最多3个, 11→7个); 板块面新增"个股所属概念"行。
- **收盘复盘主力意图口径对齐**(`src/agents/daily_report.py`): 收盘复盘自选股详情新增"主力意图(逐笔V14)"行, 与盘中监测/AI助手三处口径对齐, 修"主力流出1.16亿"东财口径误判(逐笔口径实际超大单+5967万仍在吸筹)。

本文件记录项目中每一次可提交变更。新记录按日期倒序添加，并归入 `fix`、`feature`、`update` 或 `doc` 类别。

## 2026-08-11

### feature

- **主力意图独立段(盘中监测)**: 资金面外新增 `## 主力意图` 段,口径隔离(资金面=东财/腾讯静态资金流,主力意图=逐笔实时+筹码面+股东户数交叉验证)。包含: 主力方向(≥20万腾讯官方口径=超大单+大单)、参与度/买占比(同花顺暗盘强度口径)、5日阶段、竞价撮合、尾盘特征、吸筹价位、拆单识别(逆势+套牢位)、筹码分布(峰/获利盘/套牢盘/COST50)、股东户数变化(筹码集中度)。
- **暗盘资金算法 v14(主力买入强度口径)**: 同花顺"暗盘流入多=主力吸筹"确认后,主力信号=主力(≥20万或600手)净额+参与度/买占比;识别"主力净流出但参与度高=洗盘吸筹"、"超大单托盘+大单出货=派发"等分歧形态。竞价单(9:25-9:30)独立处理(非主动买卖,混入会方向反转)。
- **筹码分布计算器**(`src/core/chip_distribution.py`): 三角分布+换手率衰减模型(对齐通达信/同花顺,偏差<10%),输出 COST(10/50/90)、获利盘比例、筹码峰(主力成本区)、集中度。数据源=腾讯800天前复权日K,免费。
- **事件溯源+题材关联研判(新闻段)**: 个股新闻之外,用题材词(军工/航天/无人机等事件驱动型优先)+通用事件词(火箭/发射/卫星/获批等)反查 `news_by_keyword` 市场级事件;利好✅/利空⚠️/中性三级标记(失利/推迟/爆炸=⚠️利空,获批/中标/成功=✅利好),⚠️>✅>中性排序;研判指引强制 LLM 结合「主力意图」段综合判断事件性质对主力行为的影响(利空低吸/利好派发陷阱)。可关联"长征七号甲发射失利/朱雀三号推迟"类市场级事件(实测命中)。
- K线形态识别已在技术分析段(自研同花顺形态+TA-Lib 61种双引擎,含位置/强度/规则提示),本轮验证完整链路可用(神剑: 纺锤线看涨/上升三法看跌)。
- **主力意图三处落地**: ① 盘中监测Prompt独立段(LLM综合研判) ② 个股通知卡片结构化摘要(`_main_intent_summary`,不依赖LLM复述) ③ AI助手对话工具 `get_main_intent`(问答"主力意图/吸筹/派发")。
- **口径隔离修复**: AI助手System Prompt引导(主力意图以get_main_intent逐笔口径为准,get_capital_flow东财仅参考)+盘中监测资金面/主力意图段显式标注口径(东财四档vs腾讯逐笔,研判以逐笔为准)。修"神剑主力一致撤退"误判(东财-1.16亿 vs 逐笔超大单+5967万,逐笔已验证与同花顺暗盘对齐)。
- **个股K线窗口独立展示主力意图**: `/klines/{symbol}/summary` API 增加 `main_intent` 字段,前端 KlineSummaryDialog 底部玫瑰色区块展示(筹码峰/成本带/参与度/竞价)。
- **数据源开关**: `PANWATCH_DARK_SOURCE` 环境变量(L2预留: l2_tencent/l2_sina/l2_itick),`src/core/dark_l2.py` 占位,未接入自动回退腾讯逐笔。
- **性能打磨**: 腾讯逐笔30s缓存+单页重试(盘中轮询9.3s→0ms/标的),新浪分价表1h缓存(2.6s→0ms)。

### fix

- 修复预测/回测股票搜索检索不到部分深市主板股（如豫能控股 001896）的问题：① `query_all_stock` 盘中/未收盘时当天返回空列表，现回退最近 8 天找最近有完整列表的交易日；② 主板过滤白名单原为 `("60","00","002")`，漏掉深市主板新代码段 `001`（121 只，含豫能控股）和 `003`（42 只），现改为 `600/601/603/605 + 000/001/002/003`。引擎直连与 web 转发（`/api/stocks/search`）均已验证可搜到豫能控股。
- 修复盘中监测报告三大数据失真问题（神剑股份 002361 案例定位）：
  - **量比口径混用**：K 线口径量比（今日总量/5日均量）盘中系统性偏低，开盘初期可低至 0.04 被误判"缩量"，而实时量比 13.52（放量）。修复：新增 `get_realtime_volume_ratio()` 直取腾讯实时量比（30s 缓存），Prompt 量能段实时口径优先。
  - **KDJ 临界误报**：开盘瞬间 K≈D（如 K=79.2/D=79.3 差 0.15）时金叉/死叉随价格抖动翻转。修复：K/D 差值 <1.0 时状态标注"临界(金叉弱/死叉弱)"，Prompt 附"禁止据此单独判断方向"提示。
  - **快照时间误导**：报告把采集时刻快照说成确定事实。修复：Prompt 顶部注入数据时刻提醒，要求 AI 使用"当前/截至采集时刻"口径。
- `StockData` 增加 `volume_ratio` 字段透传实时量比（`md_stock_data` 同步）。
- 修复资金流 P0：东财 push2delay 开盘初期 f62/f184/分项全 0（数据未初始化）被当有效数据，导致盘中监测把"主力净流入 1.59 亿"报成"数据缺失"。现全 0 视为未就绪回退其他源（`_fetch_direct_flow`）。
- 资金流链路移除悟道 `intraday_main_flow`（9:15-10:30 限流且只给主力净额无四档），直接走东财 push2delay → Engine（新浪 T-1/东财）两级。
- 修复盘中监测"无需提醒"建议无分析内容：AI 输出 `[无需提醒]` 时直接 return 导致 signal/reason 为空，建议池里只有"持有"动作没有分析。现提取"无需提醒"后的原因作为 reason（无内容时兜底"AI 判断无需提醒"），signal 兜底"无异常"。
- AI 助手层数据接入修复（去悟道单点）：
  - `get_market_news` 改为**底层多源快讯优先**（`flash_news` 引擎：财联社/新浪/东财7x24 市场级快讯，多源主备+降级），悟道热榜/简报降级为补充，失败不影响主链路。
  - 新增 `auction_collector.py`（竞价统一入口）：悟道优先（独家 bidStrength/弱转强字段），限流窗口 9:15-10:30 快速失败不白等，悟道空返回/失败时降级腾讯批量行情算竞价高开榜；30s 缓存避免助手重复提问重复请求。
  - `_fetch_auction_context`（chat 助手）与 `auction_review`（竞价复盘 agent）改用 auction_collector，消除双实现。
- 盘中监测**均线临界保护**：现价与 MA5/MA10 距离 <1% 时 Prompt 注入警告行，禁止 AI 断言"站上/跌破"（修复神剑股份场景：现价 11.95 在 MA5 11.90 之上 0.42%，AI 却说"跌破MA5"）。新增模块级函数 `build_ma_critical_warnings`（build_prompt 与测试共用）。
- **腾讯证券数据源接入**（2026-08-11，网页端 gu.qq.com 同源接口）：
  - 新增 `TencentFundflowVendor`：`proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab` 当日实时资金流（主力/超大/大/中/小四档 + 5日主力净额），作为东财 push2delay 之后的**第二实时源**（东财开盘全 0 未就绪时腾讯顶上）；口径：主力=超大单+大单。
  - 新增 `tencent_panel.py`：盘口大单占比（`qt.gtimg.cn/q=s_pk`）、大单分档统计（`stock.gtimg.cn/data/index.php?appn=dadan`）、分价表（`appn=price`）。
  - 盘中监测资金面追加"盘口大单占比 + 大单分档统计"段（腾讯面板，失败静默不影响主链路）。

### feature

- 新增**策略批量选股**：机会页新增「策略选股」区块（策略库 7 个 YAML 策略 → 全市场/自选池批量扫描 → 按分数排序输出）。
  - 后端 `POST /api/strategies/scan`：腾讯行情 100 只/批批量拉取（盘中含 PE/PB/市值全字段，非交易时段量能字段为 0 时前端提示），逐只跑策略硬过滤+因子打分，返回命中名单。
  - `_evaluate_strategy` 纯函数抽取：apply/scan 共用同一套过滤打分逻辑（原 apply 150 行重复代码删除）。
  - 腾讯行情 vendor 补 `pb_ratio` 字段（parts[46] 市净率，之前解析遗漏）；`pe_ratio` 自动规范化到 `pe_ttm`。
  - dual_low 策略补 `pe_ttm_min: 0.01`：排除负 PE 亏损公司混入"低估值"名单（真实扫描发现振华新材/晶科能源负 PE 入选）。
  - 全量测试 531 通过（新增 7 个策略评估单测）。

## v0.2.0 (2026-08-10) - 多用户系统

### 🆕 多用户(团队 4-5 人)
- **用户表+认证**: users 表(UUID 主键, role owner|member), 旧 admin 自动迁移为 owner, JWT 加 user_id/role, token_version 踢人
- **用户管理 API**(仅 owner): 建子账号/禁用/启用/重置密码/删除
- **数据隔离**: 持仓/自选/账户/渠道按 user_id 隔离(自己的+全局共享), 旧数据迁移归 owner
- **预测并发**: predict 信号量(2并发)+ 结果缓存(30min), backtest 单并发
- **推送隔离**: 渠道按用户, push_notification(user_id), 定时报告订阅(盘前/盘中/复盘/预测), cron 按订阅用户推送
- **前端**: 登录存用户, Settings 用户管理页+订阅开关, 非 owner 无管理权限
- 全量 524 测试通过

## 2026-08-10

### fix

- 修复 ECharts 分时图依赖无法按仓库 pnpm 工作区规范部署的问题：统一更新根 `pnpm-lock.yaml`、移除子包 npm 锁文件、迁移 pnpm 11 构建脚本白名单，并让 Docker 前端构建阶段预先加载全部 workspace 清单，确保 frozen-lockfile 构建可复现。
- 通知详情现会按 Trace ID 加载关联的 Agent 执行结果，显示完整分析、状态、耗时和模型；同时修复非交易时段实际已跳过却通知为“已完成”的问题，并兼容纠正已有历史通知。
- 增强通知管理中心左侧列表的选中态：增加主色竖条、背景与描边、图标光环、“正在查看”标记和高亮箭头，使深色主题下也能明确识别当前通知。
- 修复任务通知点击后跳到不存在的 `/stocks` 路由而显示空白页的问题：新通知改用持仓页链接，已存历史通知自动兼容转换，未知路由也不再停留在空壳页。
- 将持仓中的“提醒”建议和价格提醒操作改为圆形闹钟图标，已启用规则使用高亮状态并保留数量提示。
- 在持仓盈亏卡片中同时显示相关市场的当前状态（如“A股 · 盘前”或“美股 · 休市（周末）”），并升级静态缓存版本，避免旧页面继续显示过期的“今日盈亏”标签。
- 修复盘前或休市时把上一交易日涨跌误标为“今日盈亏”的问题：保留腾讯行情真实时间，并按报价日期显示“今日盈亏”“上一交易日盈亏（日期）”或“最近交易日盈亏”。
- 修复 PushPlus 设置页重复测试固定文案时被服务端判定为验证错误的问题；测试消息现带有唯一时间与编号，并提供更明确的失败提示。
- 修复右下角 Chat 助手未解析 GFM Markdown 的问题，现可渲染表格、代码块、列表和链接，并为窄屏表格提供横向滚动。
- 修复 PushPlus 渠道可能只留站内通知的问题：开启 `info` 外发、严格验证渠道配置和 API 回执，并在设置保存时自动发送测试消息。
- 修复预测容器内 PanWatch 地址和认证硬编码，改用 `PANWATCH_URL` 与数据库签发的短时 Token；同时将预测历史持久化到 `panwatch_forecast_data` 数据卷。
- 让 Docker Compose 预测引擎直接读取 PanWatch 数据库中的 LLM 配置，移除不适用于容器部署的 systemd 同步提示。

### feature

- PC 版 AI 助手支持拖动标题栏自由移动，移动过程自动限制在浏览器可视范围内；自由位置会在刷新后恢复，并在窗口尺寸或浏览器大小变化时重新约束，选择固定停靠位置可随时复位。
- PC 版 AI 助手新增窗口设置：支持紧凑、标准、大窗口、宽屏四档尺寸，以及左下、底部居中、右下三种停靠位置；窗口与悬浮入口同步移动并在浏览器中记忆选择，移动端仍保持全屏。
- 通知管理中心新增紧凑的推送渠道筛选，可与未读、推送失败和通知类型组合使用，支持 PushPlus 等实际渠道、仅站内通知及历史未记录渠道。
- 通知中心新增当次实际推送渠道回执：列表和详情会显示 PushPlus、Telegram、企业微信等渠道的名称及发送状态，混合渠道分别展示；只记录安全元数据，不保存 Token 或 Webhook，历史数据明确标注“未记录渠道”。
- 新增通知管理中心：支持全部、未读和推送失败筛选，可查看完整 Markdown 正文、站内送达状态、外部推送结果与错误、来源、Trace ID 及关联页面；顶栏消息现先进入详情中心。
- 新增电脑 Web 推送：用户可在设置页授权并测试浏览器系统通知，页面打开或在后台运行时会将新站内消息去重后推送到电脑。

### update

- 移除 PC 版 AI 助手的全屏遮罩和全屏背景模糊，窗口打开时仍可操作、点击和滚动其他页面区域；视觉聚焦改为仅作用在助手窗口周围的柔和模糊阴影，不再覆盖页面。
- 优化 PC 版 AI 助手打开后的视觉层级：增加自适应明暗主题的背景遮罩与轻微模糊，强化窗口描边和阴影，并支持点击遮罩关闭助手，使窗口边界更清晰；移动端全屏模式不受影响。
- 将通知中心三张大型统计卡收紧为带数量徽标的分段筛选栏，并与通知类型筛选合并到同一行，保留全部功能的同时减少空间占用。

### doc

- 明确开发阶段默认使用热加载或重启服务，仅在发布或修改未挂载的前端产物、依赖、Dockerfile、本地安装包时重建镜像。
- 将 Docker 打包后的资源清理纳入固定开发流程：新容器健康后清除未使用的旧 PanWatch 镜像，同时保护运行中镜像、数据卷和共享构建缓存。
- 建立 `CHANGELOG.md` 及每次可提交变更都必须同步记录的开发规则。

## 2026-08-09

### fix

- 无。

### feature

- 新增 `dev-0.1.1` 本地 Docker 开发环境，同时运行 PanWatch 主服务和预测引擎，并复用已有 `panwatch_data` 数据卷。
- 新增同花顺 Web 数据源 vendor（`ths_web.py`）：实时行情（fuyao 统一行情聚合接口，沪 market=17/深 market=33，免登录）、日K线（d.10jqka.com.cn）、快讯（news.10jqka.com.cn）、F10 基本面；已注册 quote/kline/flash_news/fundamentals 四类，香港节点实测可用。
- 新增同花顺板块/大盘资金数据源（`ths_flow.py`）：行业/概念资金流向页面版解析 + 全市场大盘资金汇总，已在数据源设置页可维护。
- 新增同花顺扫码登录 session 管理（`src/core/ths_auth.py` + `/api/ths/*`）：扫码生成二维码→轮询→自动登录+持久化，设置页可查看登录态并扫码续期，凭证自动续期无需人工干预。
- 新增影子账户（Shadow Account）页面端入口（`/shadow`）：拖拽上传交割单 → 行为画像（回合/胜率/持仓/偏好市场）+ 行为诊断（处置效应/过度交易/追涨/锚定）+ 归因分析（影子收益/实际收益/差值），支持 HTML/PDF 报告。
- 新增 K 线组合形态识别（`src/core/kline_pattern.py`，同花顺教学体系）：金针探底/双针探底/红三兵/涨停双响炮/揭竿而起/上升三法/小步上扬/放量突破 8 种形态；已接入技术指标 `kline_pattern` 字段（单根形态未命中时输出组合形态），并新增 AI 助手工具 `get_kline_patterns`（回答"K线什么形态"自动识别）。
- 新增八大看跌 K 线形态识别（同花顺第二篇学习文）：三只乌鸦/黑三兵/空方炮/倾盆大雨/黄昏之星/看跌尽头线/兄弟剃平头/二级倒锤头；高位判断含涨幅门槛（形态启动前前5日涨超3%）排除横盘误报；真实数据验证 000001 空方炮。
- 新增经典形态识别（同花顺《K线形态大全》可量化部分）：双底突破(W底)/双顶破位(M头)/上升三角形突破/下降三角形破位/上升旗形突破/下降旗形破位；双底双顶用两低/高点索引精确计算颈线；全量 502 测试通过。
- 新增 AI 助手集合竞价工具 `get_auction_data`（6 场景）：竞价全景（涨停/跌停/委买额/昨炸板反馈）/最强个股（bidStrength）/主线题材/弱转强/被核风险/盯盘名单；9:25 前返回"当日竞价未生成"提示。
- K线组合形态接入技术指标建议评分体系：后端 summary 新增 `kline_patterns`（全部识别到的组合形态含信号/位置）；前端技术指标建议按形态评分（看涨 +1/+2、看跌 -1/-2，强信号金针/双针/双响炮/三只乌鸦/黄昏之星等 ±2），形态作为可解释因子展示。
- 大宗商品轮动前瞻接入盘前事件驱动（同花顺学习文《大宗商品的轮动顺序》）：轮动剧本 能源冲锋（石油/煤炭）→ 金属狂潮（铜/铝/钢铁）→ 农产压轴（粮食/棉花/大豆）→ 黄金返场（避险）；`commodity_rotation.py` 从盘前事件流识别当前阶段 + 预判下一幕题材（能源涨→埋伏金属，金属涨→埋伏农产品，黄金启动→防御），盘前 Agent 报告自动注入。
- 地缘冲突传导链检测（学习文《以伊开战五波冲击》）：冲突关键词（开战/战争/袭击/导弹/制裁/中东/俄乌等）命中即进入地缘冲突阶段（优先级高于商品轮动）；五波传导 能源→大宗→通胀→货币→避险，联动板块 石油/油气/黄金/军工/国防；测试 9/9 通过。
- 八大进场信号识别（学习文《手把手带你看懂八大进场信号》）：早晨之星/底部十字星/底部强势大阳线/底部大长腿/大锤和小锤/大阳包小阴/大阴后两小阳/进击两阳线 8 种底部看涨/抄底形态；低位判断用形态启动前最低价（收盘已反弹也算低位），锤子线独立判定不卡小实体，底部十字星需前 5 日跌超 5% 排除横盘；真实数据 600519 进击两阳线；全量 514 测试通过。
- 接入 TA-Lib 61 种标准 K 线形态识别（ta-lib==0.7.1，新版自带二进制免编译）：`_detect_talib_patterns` 扫描全部 CDL* 函数 + 中文名映射；技术指标 summary `kline_patterns` = 自研 30 种（同花顺教学体系）+ TA-Lib 61 种合并；AI 助手返回 TA-Lib 标准形态（中文名+强度）；前端评分器适配 TA-Lib 字段（强信号锤子线/早晨之星/乌云盖顶等 ±2）；真实数据茅台 15 个 TA 形态；全量 515 测试通过。
- 修复 AI 助手技术面数据获取失败：`_fetch_technical_context` 引用不存在的 `DataCollector` 类（只有 DataCollectorManager）→ import 抛异常返回空；改用 KlineCollector + MarketCode，修正 summary 结构读取（无嵌套键兼容）与字段名（rsi_14→rsi6/rsi_status、support_level→support、resistance_level→resistance），并附带 K 线形态；验证 603061 金海通技术面完整返回。
- 盘中监控加 K 线形态提示 + 技术指标建议形态解释：盘中监控技术分析块输出自研同花顺形态 + TA-Lib 标准形态（信号方向/强度/位置 + 交叉确认规则）；技术指标建议弹窗为每个组合形态加独立 TechnicalBadge（看涨红/看跌绿）+ 悬停解释（来源/强度 + 50+ 形态中文含义映射表 TALIB_PATTERN_EXPLAIN）；typecheck+build 通过。
- 接入国内数据网关（115.190.177.213:8100，Debian12 + FastAPI）：香港节点东财资金流字段被风控断连（push2 ulist f62 间歇 RemoteDisconnected），网关用东财 push2delay 域名稳定返回今日实时四档资金流（主力/超大/大/中/小单）；`capital_flow_collector` 优先调网关（今日实时，含数据基准日标注），失败回退悟道/Engine（T-1）；`CN_GATEWAY_DISABLE=1` 测试禁用开关；验证神剑股份今日主力净流入 5781 万（与券商同源，不再是 T-1 的 2.62 亿）；全量 515 测试通过。
- 资金流双模式接入（大陆/海外）：`CN_FLOW_MODE=direct` 大陆本地直连东财 push2delay（不依赖网关）；`gateway` 海外走国内网关代理；`auto`（默认）先直连失败走网关自动检测；生产在韩国首尔节点，auto 模式实测直连 push2delay 可用、网关作兜底。
- 预测引擎隔夜事件源接入同花顺快讯（免key 7×24 新闻流）：根因是 wudao API 免费额度 50/50 用完 → `official_announcements` 拉取失败 → FCC 禁令类隔夜新闻没进模型 → 预测方向失准；新增同花顺快讯源按股票名/代码匹配（东财行情 f58 拿名称），覆盖公告源不含的财经新闻（FCC 禁令/行业政策/涨价）；兜底链 wudao公告→同花顺快讯→东财公告；验证克明食品增持利好 → 修正 +1.25%。
- TradingAgents 资金流接入今日实时（双模式网关）：之前 `md.capital_flow`（东财/新浪 T-1）与今日实时网关是两条独立路径；新增 `_fetch_ta_capital_flow` 优先走 CapitalFlowCollector 今日实时（direct/gateway/auto 双模式）失败回退引擎；TradingAgents 多 Agent 分析资金面不再用 T-1 数据；验证神剑股份主力 5781 万/超大单 4615 万。
- 修复分时K线不显示 + IndexDetail 大盘资金流：①分时路由 `/quotes/minute` 被 `/{symbol}` 抢占（404 行情不存在）→ 改 `/minute/{symbol}` 明确路径；②补 `import time as _time`（容器内 NameError 500）；③IndexDetail 页新增大盘资金流卡片（同花顺源，总流入/流出/净流入/板块数），替代"东财502成交额趋势替代"的空白提示；生产验证分时 267 点 + 大盘资金净流入 109.8 亿。
- 热门股票假展示修复 + 大盘资金流板块明细榜：①根因是东财 push2 clist 海外 502 → 回退 DB 旧快照（几天前数据=假展示）；国内网关新增 `/cn/hot-stocks` `/cn/hot-boards`（push2delay 国内直连），discovery 失败顺序 live→网关→DB快照，生产验证中际旭创/新易盛真实热门；②大盘资金流从"50板块求和"改为同花顺行业资金明细 流入Top10/流出Top10 板块榜，前端 IndexDetail+Dashboard 展示🔥流入/💧流出板块（贵金属+27.4/白酒+19.2，建材-15.3/电力-12.5）。
- 大盘资金流对齐同花顺APP：之前用同花顺 hyzjl 行业资金求和（总流入2611亿口径不对）；改为国内网关 `/cn/market-overview`（东财 push2delay 沪深指数主力净流入），两市主力净流入 -414.7亿（沪-136.5/深-278.2）、总成交额 25231亿（APP 25387 接近）、涨跌家数 3924/1266（APP 4068/1391）、上证点位 3966.59 +0.67% 与APP完全一致；前端展示主力净流入+成交额+涨跌家数+沪深分项+板块榜。
- 分时K线双修复：①指数分时代码冲突——上证指数000001与平安银行000001撞车，prefix 误判 sz → 显示个股数据；加指数代码白名单（000001/000300/000016/000905/000852→sh，399开头→sz），生产验证上证指数 242点 最新3966.59（与APP一致）；②分时图±分界线——后端返回 prev_close（腾讯 qt list[4] 昨收），前端 y 轴以昨收为对称中心 + 灰色虚线"昨收"基准线（昨收上=红涨下=绿跌，同花顺/腾讯标准分时）。
- 分时图切换 ECharts 6.1.0：新组件 MinuteEChart（价格线红涨绿跌+渐变面积、均价线黄色仅个股、昨收基准虚线±分界线、下方成交量柱红涨绿跌、十字光标+tooltip 价格/涨跌幅/均价/量、y轴以昨收对称±2.5%保底），替换 InteractiveKline 手写 SVG（删~100行）；数据卡改为现价/较昨收/均价(个股)/昨收/成交量；日K线仍用 Lightweight Charts（CDN 全局 window.LightweightCharts，unpkg+jsdelivr 双源降级）。
- 分时图白屏修复（v0.1.40→v0.1.44，本地 dev 完整错误栈定位）：①React 崩溃白屏=setOption 抛异常无错误边界→加 try/catch+init 防重入；②`undefined.get`=成交量 series xAxisIndex:1 但只定义 1 个 xAxis→补 xAxis[1]；③`xAxis and yAxis must use the same grid`=series xAxisIndex:0+yAxisIndex:1 跨 grid→series 改 xAxisIndex:1+yAxisIndex:1（双 grid 双 xAxis 双 yAxis 各自配对）；④容器 0 尺寸→init 后强制 resize+ResizeObserver；生产验证 canvas 渲染成功，价格线/昨收虚线/成交量柱正常，上证指数分时 3966.59。

### update

- 数据源设置页可维护全部数据源：`board_capital_flow`（板块资金）/`market_capital_flow`（大盘资金）加入类型注册与分类，设置页「同花顺登录」区块显示账号/UserID/过期时间。
- DB 数据源初始化：补齐同花顺实时行情/K线/快讯/基本面配置，生产环境同花顺实时行情成为默认主源（priority 0）。

### doc

- 无。


## 2026-08-25

### update

- 大盘区三个后端小改动(v0.4.7):
  - **资金流历史快照落表**: 新建 PG 表 `market_flow_snapshots`(CREATE TABLE IF NOT EXISTS, 双方言),
    接口成功返回后异步后台线程写一条快照(30s 同进程节流, 失败静默 logger.debug)。
    新增 `GET /api/market-data/market-capital-flow/history?hours=4` 返回当日 ts 序列(上限 500 条)。
  - **涨跌分布分桶**: 新增 `GET /api/market-data/breadth-distribution`, 9 档分桶
    (跌停/<-5%/-5~-3%/-3~-1%/-1~1%/1~3%/3~5%/>5%/涨停) + 60s biz_cache 缓存。
    数据源: 东财 push2 clist 全 A 股列表(f2 最新价、f3 涨跌幅%)。
  - **mainline 昨日排名**: 新建 PG 表 `mainline_rank_daily`(date, name, rank, score, PRIMARY KEY(date,name)),
    主线榜每次计算成功后 upsert 当日快照, 然后查昨日 max(date)<today 对比算
    `rank_change`(昨日 rank - 今日 rank, 正=上升), 首次无昨日数据 → rank_change=null。
