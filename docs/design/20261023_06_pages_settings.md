# 双源网关与系统设置中心域 · UI/配置体验/状态可视化走查

> 基线：**v0.48.0 / 5427af0**（分支 `docs/design-6`，worktree `/home/ubuntu/wt/design-6`）
> 走查日期：2026-10-23
> 文档性质：**静态代码走查**。未运行应用、未发起任何真实接口/网络测试，所有时延/可用性结论均来自代码路径推断；所有「现状」都带 `文件:行`，未逐行核实的地方在第六节「未审」中列出。
> 密钥红线：本文只出现**字段名**与**脱敏形态**（`sk-****1234` / `********`），不含任何真实 Key、Token、密码。

---

## 一、审计路径清单

### 前端
| 路径 | 行数 | 在本域中的角色 |
|---|---|---|
| `frontend/src/pages/DataSources.tsx` | 818 | 数据源网关总览：源条目 / 优先级 / 测速 / 启用开关 |
| `frontend/src/pages/ApiKeys.tsx` | 404 | **Skill API Key 自助控制台**（`/keys/my`），非模型 Key 池 |
| `frontend/src/pages/SettingsHub.tsx` | 66 | 设置中心外壳：设置 / 审计 / 帮助 三 Tab |
| `frontend/src/pages/Settings.tsx` | 60 | 设置中心装配（12 个 section + 3 组 Dialog） |
| `frontend/src/pages/settings/AiSection.tsx` | 144 | AI 服务商列表 + **场景绑定**下拉 |
| `frontend/src/pages/settings/GeneralSettingsSection.tsx` | 132 | 接口 Key 区块 + 同花顺账号卡片 + 系统设置平铺区 |
| `frontend/src/pages/settings/KeyDialogs.tsx` | 132 | 接口 Key / 系统键的编辑弹窗（两个 Dialog 共用一文件） |
| `frontend/src/pages/settings/useSettingsData.ts` | 205 | 首屏 `allSettled` 并发拉取 + 场景绑定写回 |
| `frontend/src/pages/settings/types.ts` | 205 | 配置键元信息契约（仅精读 175–197 段） |

### 后端
| 路径 | 角色 |
|---|---|
| `src/web/api/datasources.py` | `GET/PUT/POST/DELETE /datasources`、`/trust`、`/health/*`、`/capabilities`、`/{id}/test` |
| `src/web/api/settings.py` | `app_settings` 白名单键读写、掩码策略、头像 |
| `src/web/api/api_keys.py` | Skill Key 的 CRUD / reset / usage（不存明文） |
| `src/core/ai_client.py` | 场景 → 模型解析（`get_model_for_scene` / `_resolve_global_model`） |

---

## 二、覆盖范围

### 已覆盖
1. **DataSources.tsx**：四源（通达信 TQ / 同花顺 / 东财 / 腾讯）的呈现形态、主源→降级备份链、Ping 徽标、启用 Switch、caliber 对齐状态。
2. **ApiKeys.tsx + SettingsHub.tsx + AiSection.tsx**：模型池与场景绑定（对话 / 战报 / AI 裁判 / 视觉 OCR）、Key 脱敏显隐、测可用性、推送测试。
3. **接线缺陷**：前端有入口但后端不读 / 后端有键但前端无入口 / 掩码与明文口径不一致，逐条给出 `文件:函数` 级接线方案。

### 明确未审（本轮不产出结论）
- `settings/AiDialogs.tsx`(490) / `NotifySection.tsx` / `NotifyDialogs.tsx` / `AppearanceSection.tsx` / `PackSection.tsx` / `UsersSection.tsx` / `MyServicesSection.tsx` / `useSettingsActions.ts`(736) —— 仅在调用点间接观察。
- `packages/marketdata/` 各 vendor 的实际请求实现（只用到 `vendors/tdx.py` 的 token 读取顺序一处）。
- 任何真实时延、真实 Key 可用性、真实降级切换、真实同花顺 SDK 登录行为。
- 响应式断点、暗色/浅色主题实际渲染（仅审 class 与 token 字面）。
- Frozen Ring（`symbol_guard` / `number_audit` / `caliber.py` / 交易 403 / TQ 优先链 / `SYSTEM_PROMPT`）：**只核口径一致性，不提改动建议**。

---

## 三、DataSources.tsx —— 双源网关总览

### 3.1 现状：它是"按数据类型"的清单，不是"按 vendor"的网关面板

- **现状**：页面骨架是 `DATASOURCE_CATEGORIES`（`DataSources.tsx:78-83`）四个分类（行情&K线 / 资讯&快讯 / 基本面&财务 / 资金&市场面），分类下面是 `DATASOURCE_TYPES` 的 14 个类型段（`:60-75`），每个类型段内再列出该类型的源条目（`:274-351`）。四源 vendor 只作为一条灰字 `source.provider` 出现（`:296`）。
- **问题**：用户要回答的是"通达信 TQ 现在是不是主源、挂了会切到谁"，而这套结构只能回答"K线类有几个源、它们的 priority 数字是多少"。**TQ 优先这条产品铁律在页面上没有任何位置**——全页 `caliber` 命中数为 0（`DataSources.tsx` 内 grep `caliber|口径` = 0 命中）。
- **改法**：在 `DataCapabilities`（`:383`）之上加一层 **vendor 网关面板**：按 `provider` 聚合，固定四行（`tq` / `ths` / `em` / `tx`），每行 `<vendor> · 角色(主源/备份n) · 覆盖数据集数 · 当前成功率 · p50`。数据可直接复用已有的 `/datasources/trust`（`datasources.py:190-235` 已返回 vendor 级 success_rate / p50 / ewma / samples）。
- **验收线**：打开页面前 200px 内能同时看到四个 vendor 及其"主/备"角色；断掉 TQ 后（模拟）该面板第一行变 `down` 且备份链首项高亮。

### 3.2 现状：没有"主源 ➔ 自动降级备份链"的可视化，只有组内上下箭头

- **现状**：排序能力是 `moveSource`（`:222-234`），在**同 type 分组内**与相邻源交换 `priority`；UI 是两个 chevron 图标按钮（`:323-328`）。降级链的真实消费方在后端引擎，前端没有呈现。
- **问题**：
  1. 用户看到的是"优先级: 3"这样的数字（`:297`），看不到"这一类的第 1 顺位是谁、第 2 顺位是谁"。
  2. **`priority` 相等时交换是空操作**：`:228-231` 把两行的 priority 值互换，若两值相同（seed 数据常见默认 0），PUT 前后无任何变化，`load()` 重拉后 UI 不动，也没有 toast 解释。
  3. `moveSource` 只用 `groupedSources` 的当前顺序判断边界，`:226` 的 `if (!swap) return` 静默失败——第一行点"上移"像按钮坏了。
- **改法**：
  - 每个 type section 头部渲染 **链路条**：`主 tq ▸ 备 ths ▸ 备 em`，取自该 type 下 enabled 源按 priority 排序；`caliber` 标签按 `docs/_frozen/caliber_matrix.md` 的既有口径词（只读对齐，不改定义）。
  - `moveSource` 改 **index 交换**：`PUT {priority: idx}` 按新下标批量重排，或后端新增 `POST /datasources/reorder`；比较值相等时前端先做本地归一化再提交。
  - 边界按钮 `disabled` 而非静默 return。
- **验收线**：任一 type 下点"下移"后链路条顺序立即变化（不依赖 priority 数值偶然不等）；首行"上移"按钮为禁用态；链路条上的备源名称与 `/trust` 的 vendor 列表可对上。

### 3.3 现状：没有 Ping 徽标，测速结果只落在弹窗里

- **现状**：测速按钮 `:329-342` 调 `testSource`（`:200-211`）→ `POST /datasources/{id}/test`（`datasources.py:380-409`）→ 结果进 `testResult` state 并**在同一时刻打开一个全局弹窗**（`:204-205`、`:539`）。行内除了 `:309-318` 那块 `engine_attached` 才显示的"成功率/p50"灰字，没有任何时延数字。
- **问题**：一次只能测一个、测完必须关弹窗；关掉后行内不留痕，用户无法"扫一眼看哪个源慢"。`:337-341` 只有 loading 转圈，转完就没了。
- **改法**：`testSource` 成功后把 `{duration_ms, test_passed, count}` 写入 `sourceTestCache: Record<number, ...>`，行内渲染徽标 `12ms`（>800ms amber、>2000ms red，与 `datasources.py:201-214` 的 `_score` 分档同阈值）；弹窗降级为"点徽标才展开明细"。
- **验收线**：测完一个源不关弹窗，行内即出现彩色时延徽标；连续测 3 个源，三项徽标同时存在；徽标颜色阈值与 `/trust` 的 score 分档一致。

### 3.4 现状：Switch 与编解码细节

- **现状**：启用开关 `:343` → `toggleEnabled`（`:188-198`）PUT `{enabled}` 后整页 `load()`；无乐观更新。
- **问题**：`:334` 测速按钮 `disabled={... || !source.enabled}`，但**禁用后没有任何提示**（title 仍是"测试连接"）。用户点不动会判定为 UI 故障；正确信息应是"该源已停用，启用后可测"。
- **改法**：`title={source.enabled ? '测试连接' : '该源已停用，先启用再测'}`，并在 disabled 态给 `opacity-40 cursor-not-allowed`；`toggleEnabled` 先本地 setState 再 PUT，失败回滚 + toast。
- **验收线**：停用某源后悬停测速按钮出现解释文案；拨动 Switch 的视觉反馈在 100ms 内出现（不等待 2 次网络往返）。

### 3.5 现状：`caliber` 对齐状态缺失

- **现状**：`DataSources.tsx` 全文 grep `caliber|口径` = **0 命中**；`datasources.py` 返回字段里也没有 caliber 相关项（`:117-132` 的响应体只有 type/priority/health(is_orphan)/engine_attached/key_count）。
- **问题**：走查要求"每源卡上的 caliber 对齐状态"在代码里不存在。引擎侧的健康度（成功率/p50）被当成了"对不对"的代理指标，但**成功率 100% 也不代表口径对**（如成交量单位、复权方式、北向口径）。这条与 `docs/_frozen/caliber_matrix.md` 的既有矩阵脱节：矩阵是文档级约定，页面无对应读数。
- **改法**（不改 `caliber.py`，只读）：`/capabilities`（`datasources.py:281-317`）已按数据集聚合，可在响应里补 `caliber_version`（读矩阵声明的版本号字段，不改判据），前端在链路条每项后挂 `口径 vX` 小标；不一致时显示 amber `口径≠矩阵`。
- **验收线**：每个源条目能看到当前口径版本标签；矩阵版本变化后（改文档常量）页面标签随之变化；不一致状态不得用绿色。

### 3.6 显示与排版缺陷（可直接量化）

| 编号 | 现状（文件:行） | 问题 | 改法 | 验收线 |
|---|---|---|---|---|
| D-1 | `DataSources.tsx:717, 736, 749, 794, 797` | `{cond ? '--' : safeFixed(...)}万` —— 单位 `万` 无条件拼接，null 时渲染成 **`--万`** | 先算 `const s = cond ? '--' : ...; return s === '--' ? '--' : s + '万'` | 资金流/龙虎榜/两融/北向四类测试预览在无数据时只显示 `--`，全文无 `--万` |
| D-2 | 全文 `tabular-nums` **0 命中**（grep 确认）；数字集中在 `:314-315, 570-574, 651-655, 669, 699-701, 720, 763, 777` | 等宽字体但比例数字，刷新时列宽抖动 | 所有含数值的 `font-mono` 容器补 `tabular-nums` | 连续两次 `load()` 后列宽不位移；DevTools 里数值节点 computed `font-variant-numeric: tabular-nums` |
| D-3 | `:368` | `text-[20px] md:text-[20px]` —— 断点前后同值，死代码 | 删除 md 变体或改为 `md:text-[20px]`→统一 | 该行不再出现重复断点类 |
| D-4 | `:124, 485` | `secretVisible` 是**单个全局布尔**，点开 Tushare Token 会同时把雪球 Cookies 变明文 | 改为 `Record<string, boolean>` 按 `field.key` 记录 | 只展开一行时另一行仍为密码态 |
| D-5 | `:95-102` | `PROVIDER_CREDENTIAL_FIELDS` 只有 `tushare` / `xueqiu` 两家；同花顺、TQ、东财、腾讯无凭证字段入口 | 补 `ths`（`ths_username`/`ths_sdk_password`）等条目，或把该 section 指向同花顺账号卡片 | 新增同花顺源时能在弹窗里填账号，而不是只能改环境变量 |
| D-6 | `:449` 注释 + `:175-176` | 编辑态只提交 `priority/test_symbols/config`，但弹窗标题显示 `name`（`:453`）、副标题显示 `provider`（`:454`），无只读视觉区分 | 名称/Provider 显示为只读文本行（非 Input），只读原因加 tooltip | 用户能一眼看出哪些字段不可改，不再尝试点标题改名字 |
| D-7 | `:539-543, 807-815` | 测试结果弹窗 `onInteractOutside` 直接 `preventDefault`（点外部不关），而弹窗内容以"测试股票"一行收尾（`:807-812`），**没有底部动作区**（无"关闭/重新测一次/去编辑"） | 在内容末尾加动作区：`关闭` + `重新测一次`（复用 `testSource(id)`）+ `去编辑该源`（`openDialog(source)`） | 用户测完失败后能在弹窗内直接重测或跳到编辑，不必先关弹窗再回列表找 |

### 3.7 后端契约侧的问题（影响前端能力上限）

- **现状**：`_to_response` 返回 **原始 `config`**：`datasources.py:124` `"config": source.config or {}`。前端 `:486` 用 `form.config?.[field.key]` 直接回填输入框。
- **问题**：`tushare.token`、`xueqiu.cookies` 以**明文**进入浏览器内存与 DOM（`:485` 仅靠 `type="password"` 遮挡视觉，DevTools 可直接读 value）。而设置页对同类凭证走的是掩码策略（`settings.py:76-77, 124-133`）。**同一产品两套密钥标准**，这是本域最硬的一条红线问题。此外 `:110-116` 还会把 `config.api_keys` 数组算出 `key_count`，说明 config 里确实存在多 Key 池。
- **改法**：`datasources.py:_to_response` 增加 `_mask_config(config)`：命中 `KEY_LIKE_FIELDS = {token, cookies, api_key, api_keys, password, secret}` 的字段值替换为 `sk-****1234` 形态 + 保留长度提示；PUT 时若收到掩码占位则视同"未修改"（复用 `settings.py:253-255` 的拒收逻辑）。
- **验收线**：`GET /datasources` 响应体中任一凭证字段不出现可还原的明文；前端回填框显示 `sk-****1234`；不修改直接保存不会把掩码写回。
- **附带**：`_is_orphan`（`:95-104`）在 `_to_response` 里**逐行**被调用（`:131`），每次 `import marketdata` + `_seed_providers_by_type()`；列表 40+ 行时是一次请求内的重复计算。建议在 `list_datasources` 外层算一次合法集合传入（低危，功能正确）。
- **附带**：`health_map` 以 `provider` 为 key（`:109`），同一 provider 的不同 `type` 行会显示**同一份**成功率/p50，容易被读成"这个源对这个数据集也健康"。

---

## 四、ApiKeys.tsx + SettingsHub.tsx + AiSection.tsx —— 模型池与场景绑定

### 4.1 首要事实：`ApiKeys.tsx` 不是模型 Key 池

- **现状**：`ApiKeys.tsx:1-30` 文件头写明"API Key 控制台 v2"，数据来自 `/keys/my`（`:86`），操作是创建/重置/删除 **SIDA Skill API Key**（`api_keys.py:1-17`，`sk_` 前缀、`key_hash` 落库、明文只返回一次）。模型服务商与模型池在 `settings/AiSection.tsx`（`/providers/services`，`useSettingsData.ts:61`）。
- **问题**：走查任务把 `ApiKeys.tsx` 当成"模型池场景绑定表单"，说明**命名本身就在误导**——两个页面都叫 "API Key"，一个管"我方发出去的 Key"，一个管"我买来的模型 Key"。`GeneralSettingsSection.tsx:42-46` 更进一步：用 `/datasources` 的 `key_count`（多 Key 池数量）渲染 `{provider} {n} 个 Key` 徽标，紧挨着下面就是 app_settings 的"接口 Key"行——**同一屏里三种 Key 语义**，彼此没有任何映射。
- **改法**：`ApiKeys.tsx` 页面标题/菜单文案改为「Skill API Key（对外发放）」；`GeneralSettingsSection.tsx:42-46` 徽标文案改为「多 Key 池 × n」并加一行小标题与下方接口 Key 区隔开。
- **验收线**：新用户在设置页能凭标题区分"对外发放的 Key / 数据源接口 Key / 模型服务商 Key"三者。

### 4.2 场景绑定：只有 vision 场景告诉用户"最终生效的是谁"

- **现状**：`AiSection.tsx:95-136` 为每个场景渲染一个 Select。仅 `b.scene === 'vision'` 时显示"当前绑定 / 默认: xxx"（`:102-109`），其余场景（对话 / 战报 / AI 裁判）没有任何生效模型回显。选项里"默认模型"只是一个哨兵值（`useSettingsData.ts:98` `SCENE_DEFAULT_VALUE = '__default__'`）。
- **问题**：后端 `ai_client.py:718-777` `_resolve_global_model` 是**四级回落**：场景绑定 → `SCENE_DEFAULT_MODEL_NAMES`（`:598-600`，目前只有 vision）→ `is_default` 模型 → 模型池第一个。用户在"默认模型"上看到的是一个空概念，实际可能落到池里第一个模型——**而这个模型在 UI 上不显示**。
- **改法**：去掉 `b.scene === 'vision'` 分支，所有场景都渲染 `生效: {b.effective_model_name}`；并在行尾加折叠的回落链（"未绑定 → 默认模型 → agnes-3.0-flash"）。字段 `b.effective_model_name` 前端已在使用（`:106`），无需改后端契约。
- **验收线**：任一场景选择"默认模型"后，行内立刻可见具体模型名；四个场景（对话/战报/AI 裁判/视觉 OCR）都有回显。

### 4.3 场景绑定的保存反馈不完整

- **现状**：`handleSceneChange`（`useSettingsData.ts:99-110`）`setBindingSaving(scene)` → 成功后用返回值替换列表项 → 失败只 toast。
- **问题**：`bindingSaving` 在 `AiSection.tsx:113` 只用于 `disabled`，**Select 内没有 loading 指示**；且 Select 是受控的（value 来自 `sceneBindings`），请求在途时用户看不到"正在保存"，请求失败时控件会短暂闪回旧值再弹 toast，观感像"点了没反应又跳回去"。
- **改法**：`bindingSaving === b.scene` 时在 SelectTrigger 右侧加 `Loader2` 小图标；失败时给该行加 2s 的 amber 边框。
- **验收线**：切换场景后 200ms 内出现保存中指示；失败行有可见的错误态而非只有 toast。

### 4.4 缺失的两类验证按钮（模型 Key 与推送）

- **现状**：`AiSection.tsx:61-67` 的服务商可用性判据是 `svc.api_key ? '已启用' : '未配置 Key'` —— **只判断字段非空**。`ApiKeys.tsx` 全文无任何连通性测试按钮；`GeneralSettingsSection.tsx` 的接口 Key 行也没有（只有"管理"）。
- **问题**：
  1. **不存在"一键测 Key 可用性（带延迟）"**。用户填完 Key 看到"已启用"绿色徽标，但可能是错的 Key、额度用尽、或 base_url 写错——绿色徽标会给出false confidence。
  2. **不存在"推送测试"**（发一条测试消息到已配置渠道）。通知渠道配置完只能等下一条真实通知来验证，失败时无从定位。
  3. 接口 Key 侧同理：`KeyDialogs.tsx` 只有保存（`:72`），没有"保存并测一次"。
- **改法**：
  - 服务商行加 `测速` 按钮 → 新增 `POST /providers/services/{id}/test`（`src/web/api/providers.py`，复用 `ai_client` 的最小 chat 调用，返回 `{ok, latency_ms, model, error}`），徽标从"已启用"改为"可用 · 320ms / 不可用"。
  - 接口 Key 侧在 `KeyDialogs` 保存成功后串一次供应商探测（tushare 用 `user` 接口，zhitu 用分红探针，tdx 用问小达 ping），返回延迟。
  - 通知渠道行加 `发测试消息` → 复用 `channels.py` 的发送函数，走既有幂等键避免污染统计。
- **验收线**：错误 Key 保存后徽标必须变红并给出 `latency_ms` 或错误摘要；正确 Key 显示具体毫秒数；推送测试不写入正式通知记录（幂等键含 `test:` 前缀）。

### 4.5 ApiKeys.tsx 的密钥生命周期与主题问题

| 编号 | 现状（文件:行） | 问题 | 改法 | 验收线 |
|---|---|---|---|---|
| A-1 | `:181-200` | 明文 Key 用大字号 `code` 块常驻页面，只有手动点"我已保存"（`:195`）才清空；切 Tab / 失焦都不清 | 60s 倒计时自动清空 + 切换 `activeSection` 即清空 | 离开"我的 Key"分区后明文不再在 DOM |
| A-2 | `:152-163` | 明文 Key 被直接拼进可复制的 `curl ... \| bash -s -- <KEY>` 与 JSON 配置；`plaintext` 为空时用 `YOUR_API_KEY` 占位 | 生成时只显示 `sk-****1234` 形态，点"复制"时后端换发一次性令牌或前端二次确认 | 页面可见文本不含完整 Key；剪贴板内容含完整 Key（有意为之） |
| A-3 | `:189-190, 357-373, 388-400` | 硬编码 `text-white` / `text-slate-600` / `bg-cyan-500` / `text-cyan-400`，无暗色变体对冲 | 换成语义 token：`text-foreground` / `text-muted-foreground` / `text-stock-up`（买红卖绿口径）/ `bg-primary` | 浅色主题下正文对比度 ≥ 4.5:1；无 `text-white` 字面量 |
| A-4 | `:353` | `pct = daily_limit > 0 ? ... : 0` → 无限额 Key 进度条显示 0%，看起来"没用量" | 无限额时进度条改为"无限额"纹理或隐藏 | `daily_limit<=0` 的 Key 不出现误导性的 0% 条 |
| A-5 | `:264-269` | 时间全用 `formatDateTime`，但 `日限 {k.daily_limit}` 与用量数字无 `tabular-nums` | 数值容器补 `tabular-nums` | 用量数字列表对齐不抖动 |
| A-6 | `:60-65` | `TIER_LABEL` 只有 free/trial/pro，未知 tier 回退 `TIER_COLOR.free` 但标签用原值（`:256`）| 未知 tier 应显示 neutral 灰而非"免费"色 | 未知 tier 不冒充免费档 |

### 4.6 SettingsHub.tsx

- **现状**：三 Tab（设置 / 审计 / 帮助），`AuditPage` 标 `ownerOnly`（`SettingsHub.tsx:35`），旧路由 `/audit`、`/help` 重定向过来（`:14` 注释）。
- **问题**：无功能缺陷。唯一体验问题：`TabLoading`（`:54-60`）是整块 40vh 空白 + 12px 灰字，而内部 `Settings.tsx:33` 已有 `SettingsLoading` 骨架 —— **两层 loading 串联**，设置页首屏会先看到"加载中…"文字再看到骨架，闪两次。
- **改法**：`SettingsHub` 的 fallback 直接用 `SettingsLoading` 同款骨架（或提升 `SettingsPage` 为非 lazy）。
- **验收线**：冷启动 `/settings` 只出现一次 loading 形态，无文字→骨架的二次跳变。

---

## 五、接线缺陷清单（核心）

> 判定标准：① 前端有输入框但后端只存不读；② 后端有键但前端无入口；③ 两端对同一键的密级/取值口径不一致；④ 无效分支/死代码。

### W-1 ⚠️ 同花顺 SDK 凭证：后端有键、报错文案指向 UI、但 UI 上没有入口

- **现状**：
  - 后端描述与掩码都在：`settings.py:65-66`（`ths_username` / `ths_sdk_password`）、`:76`（两者都在 `SECRET_SETTING_KEYS`）。
  - 业务代码确实读它：`src/core/dark_l2.py:313` 抛错文案 `"同花顺凭证未设置(设置页 ths_username/ths_sdk_password 或 env THS_USERNAME/THS_PASSWORD)"`。
  - 前端入口不存在：`settings/types.ts:184-188` 的 `DATA_SOURCE_KEYS` 只有 `wudao_mcp_token` / `zhitu_token` / `tdx_api_key` 三项；接口 Key 区块就是遍历这个数组渲染（`GeneralSettingsSection.tsx:54`）；`KeyDialogs.tsx:41` 也只在 `DATA_SOURCE_KEYS` 里找。
- **问题**：**报错让用户去一个不存在的输入框填账号密码**。用户唯一能做的只有改容器环境变量并重启——而 `settings.py:65` 的文案明确写着"留空则读容器 env"，暗示 DB 优先、设置页可配。这是本域最典型的"文案承诺 > 实现"。附带副作用：`dark_l2.py:134` 注释警告旧 `ths_username` 键 `THS()` 不认、会**静默变游客**，说明这条凭证链本身对键名敏感，缺 UI 会长期靠人工改 env。
- **改法**：`settings/types.ts:DATA_SOURCE_KEYS` 追加两项
  `{ key: 'ths_username', name: '同花顺SDK', desc: '正式账户(留空读 env THS_USERNAME)' }`、
  `{ key: 'ths_sdk_password', name: '同花顺SDK密码', desc: '留空读 env THS_PASSWORD' }`；
  `GeneralSettingsSection.tsx` 的 `DATA_SOURCE_KEYS.map` 会自动渲染（无需改结构）；`KeyDialogs.tsx` 已按 `SECRET_SETTING_KEYS` 决定密码态，需确认前端集合同步（见 W-2）。若产品希望统一走同花顺账号卡片，则把 `docs` 与 `dark_l2.py:313` 的文案改为指向 `sec-ths` 卡片（`GeneralSettingsSection.tsx:79-82` 已挂 `ThsAccountCard`）。
- **验收线**：设置页「接口 Key」区块出现 5 行；填 `ths_sdk_password` 保存后 `dark_l2` 不再抛"凭证未设置"；`GET /settings` 对它返回 `********`。

### W-2 前端/后端的"敏感键"集合不一致（4 vs 7）

- **现状**：前端 `settings/types.ts:180` → `['wudao_mcp_token','zhitu_token','tdx_api_key','ths_sdk_password']`（4 项）；后端 `settings.py:76` → 外加 `ths_password`、`http_proxy`、`ths_username`（7 项）。
- **问题**：`http_proxy` 在前端是普通键（`KeyDialogs.tsx:111-116` 普通 Input 明文回显）。后端认为它"可含内网凭证"（`settings.py:75` 注释）并掩码，前端却按普通文本渲染 `********` 让用户编辑。更糟的是 `ths_username`：后端掩码、前端明码，**同一段落两套预期**。
- **改法**：前端不要自维护集合，改为从 `GET /settings` 的响应里读（后端在 `SettingResponse` 增加 `secret: bool`）——单一事实源。
- **验收线**：前端渲染密码态/普通态的判据与后端掩码集合完全一致；新增一个 secret 键只需改后端一处。

### W-3 ⚠️ 掩码值可被"加个空格"绕过，直接写坏凭证

- **现状**：`list_settings` 对 secret 返回 `********`（`settings.py:130-132`）；PUT 只拦**完全等于** `********` 的值（`:253-255`，`if key in SECRET_SETTING_KEYS and (update.value or "") == SECRET_MASK: raise 400`）。前端系统设置区把**所有** `SETTING_KEYS` 平铺成可编辑行（`GeneralSettingsSection.tsx:105-125`，经 `filteredSettings`），点"编辑"打开 `sysDialog`（`KeyDialogs.tsx:83-128`），`currentValue = edited[...] ?? setting.value`（`:89`）**预填掩码**，输入框是普通 `Input`（`:111-116`）——用户看不出它是掩码占位。
- **问题**：两条破坏路径。
  1. 用户原样保存 → 后端 400「敏感值未变更(掩码占位), 拒绝写入」→ 用户看不懂，以为保存功能坏了。
  2. 用户"微调"（多敲一个空格、或手动删两位）→ 值变成 `******** ` ≠ `SECRET_MASK` → **校验通过，字面 8 个星号写进 DB**，把真 token 覆盖掉。之后 `zhitu_token` 之类静默失效，`dark_l2.py:313`/`quotes.py:329` 报"未配置"，而 UI 上仍是"已配置"绿色徽标（`GeneralSettingsSection.tsx:67-69` 判 `value === SECRET_MASK`，恰好也是真）。
- **改法**：
  - `GeneralSettingsSection.tsx` 的 `filteredSettings` 过滤掉 `SECRET_SETTING_KEYS`（secret 键只从「接口 Key」区走 `KeyDialogs`，那里是密码态 + "已配置"角标，且保存按钮在未改动时禁用 `:72`）。
  - 后端 `settings.py:update_setting` 在掩码等值判断之外，加**形状校验**：`set(v) <= {'*'} and len(v) > 3` → 拒绝；或对 secret 键要求 `len(value) >= 8 and not value.strip('*') == ''`。
  - `KeyDialogs.tsx:89` 的 `currentValue` 若为掩码，输入框应为空 + placeholder「已配置（输入新值可替换）」。
- **验收线**：在 secret 键的系统设置行里保存任意数量星号（含带空格）均被 400 拒绝；secret 键不再出现在系统设置区；保存成功后 `value === SECRET_MASK` 与真实可用的凭证状态一致。

### W-4 ⚠️ PUT 响应把刚写入的明文回吐

- **现状**：`update_setting` 返回 ORM 对象 `setting`（`settings.py:293`），`response_model=SettingResponse`（`:224`）只声明了 `key/value/description`，**没有掩码**。对比 `list_settings` 明确"必须返回新对象、不能改 ORM"（`:124-132`）。
- **问题**：保存 secret 后，响应体里是该 secret 的**完整明文**。前端 `useSettingsActions` 若用返回值回填 `settings` 状态（本轮未精读该文件，标注为待确认），明文就会重新进入列表渲染；即便不回填，它也出现在 DevTools Network 面板与任何请求日志中间件里。
- **改法**：`update_setting` 返回前构造新 `SettingResponse(...)`，`if key in SECRET_SETTING_KEYS and value: value = SECRET_MASK`（与 list 同一函数，抽出 `_mask_row()`）。
- **验收线**：`PUT /settings/wudao_mcp_token` 响应体 value 为 `********`；Network 面板中该请求的任何字段不含明文。

### W-5 死分支：系统保留键的 403 永远不可达

- **现状**：`settings.py:249-252`
  ```
  if key not in SETTING_KEYS: raise 400
  if key in ("jwt_secret", "allow_register"): raise 403
  ```
- **问题**：`jwt_secret` / `allow_register` 都不在 `SETTING_DESCRIPTIONS`（`:52-72`，`SETTING_KEYS = list(...)` `:79`），所以**先被 400 拦掉**，403 分支是死代码。读代码的人会误以为这两个键被特殊保护。
- **改法**：把 403 判断提到 `SETTING_KEYS` 白名单之前，或直接删掉并注释"保留键不在白名单，统一 400"。
- **验收线**：`PUT /settings/allow_register` 明确返回 403（语义为"系统保留"）而不是 400（语义为"未知键"）。

### W-6 GET /settings 有写副作用（首屏并发会重复插行）

- **现状**：`list_settings`（`:100-133`）对每个不在 DB 的键 `db.add(AppSettings(...))` 并在末尾 `db.commit()`（`:112-122`）。
- **问题**：GET 不是幂等的。多标签页/刷新双击同时首屏，两个请求都判定"不存在"→ 双插。若 `app_settings.key` 无唯一约束就是重复行（`existing_map` 用 dict 构造 `:103`，重复键后者覆盖前者，读出来会丢一行）。同时这解释了为什么 `tests/test_avatar_setting.py:83` 要断言 `ui_avatar not in keys` —— 该接口会把任何被列举的键固化成 DB 行。
- **改法**：初始化挪到迁移/启动 seed（`src/bootstrap/`），`list_settings` 只读 + 对缺失键返回 env 默认值不落库。
- **验收线**：连续调 10 次 `GET /settings`，`app_settings` 行数不变。

### W-7 系统设置无任何值校验，填错静默失效

- **现状**：`notify_quiet_hours` 的描述是"HH:MM-HH:MM"（`:54`），`notify_retry_attempts` 是整数（`:55`），`notify_dedupe_ttl_overrides` 是 JSON（`:57`），`panwatch_base_url` 是 URL（`:59`）；PUT 处理器（`:224-293`）**不做任何格式校验**。真正消费方 `src/bootstrap/runtime.py:439-460` 用 `float(backoff_raw)` / 直接读字符串，`panwatch_base_url` 由 `src/core/analysis_link.py:16` 拼进通知链接。
- **问题**：`notify_quiet_hours = "23:00~07:00"`（波浪号写错）会被静默保存，`runtime` 里解析失败后按"关闭"处理——**用户以为开了静默，实际半夜仍在推**。`notify_dedupe_ttl_overrides` 填错 JSON 同理。这类"配置写进去了但没生效"的故障，UI 上零反馈。
- **改法**：`settings.py` 增加 `_VALIDATORS: dict[str, Callable[[str], str]]`，PUT 时按 key 校验并 400 带人话错误（"静默时段需形如 23:00-07:00"）；前端 `KeyDialogs` 的 sysDialog 按 key 切控件（时段用两个 time input、重试次数用 number、JSON 用 textarea + 保存前 `JSON.parse`）。
- **验收线**：非法格式保存返回 400 且错误文案指出期望格式；前端在提交前即拦截并高亮字段。

### W-8 `stock_link_platform` 只有一个选项，等于不可配置

- **现状**：前端 `types.ts:190` `STOCK_LINK_OPTIONS = { xueqiu: '雪球' }`，`KeyDialogs.tsx:96-109` 用它渲染 Select；后端 `settings.py:91` 的 env 默认值也硬编码 `"xueqiu"`（注意：不是从 `Settings()` 读，是字面量）。消费方 `src/core/stock_link.py:20` `SETTING_KEY = "stock_link_platform"` 是"平台"抽象。
- **问题**：下拉里只有一个选项，用户点开无事可做；文案"点击股票代码跳转的行情网站"（`:58`）暗示多平台，实际单平台。`_get_env_defaults` 里唯一一个**不用配置、写死**的默认值就是它（`:91`），是个隐含的技术债。
- **改法**：要么在 `STOCK_LINK_OPTIONS` + `stock_link.py` 同时补齐第二平台（如东财），要么把该键在 UI 上降级为只读展示并注明"当前仅支持雪球"。**不要保留一个只能选一项的下拉。**
- **验收线**：下拉要么 ≥2 个可选项，要么不以下拉形式出现。

### W-9 未接线的"多 Key 池"可视化

- **现状**：`datasources.py:110-116` 会数 `config.api_keys` 的个数并作为 `key_count` 返回；`GeneralSettingsSection.tsx:42-46` 用它渲染 `{provider} {n} 个 Key` 徽标，而 `DataSources.tsx` 的弹窗里 `PROVIDER_CREDENTIAL_FIELDS`（`:95-102`）**只有单值 `token` / `cookies`，没有多 Key 池的编辑界面**。
- **问题**：徽标能显示"某 provider 有 3 个 Key"，但产品里没有任何地方能增删这 3 个 Key（只能改完整 config JSON 的 `api_keys` 数组，见 `DataSources.tsx:503-521` 的 `details` 原始 JSON 编辑器）。这就是"前端有展示、后端有数据、中间没有编辑器"。
- **改法**：`DataSources.tsx` 弹窗为 `provider in {zhitu, tushare}` 增加"Key 池"子表格（增删/启停/轮换顺序），写回 `config.api_keys`；或在设置页「接口 Key」区提供池化管理（`quotes.py:302-306` 的 `pick_zhitu_token` 已支持轮换）。
- **验收线**：能在 UI 里把某 provider 的 key_count 从 1 改到 3 并保存成功；徽标随之变化；不需要手改 JSON。

### W-10 场景绑定只暴露四级回落链的第一跳

- **现状**：`AiSection.tsx:110-133` 只展示"绑定值 / 默认模型"；后端 `ai_client.py:718-777` 实际是"场景绑定 → `SCENE_DEFAULT_MODEL_NAMES`（`:598-600`）→ `is_default` → 池内第一个"。异常类 `LLM 降级({scene})`（`ai_client.py:24-26`）说明运行时确会降级，但 UI 不呈现。
- **问题**：用户在页面配置完场景绑定，运行时若因额度/降级走到别的模型，UI 永远显示"已绑定 X"——**配置态与运行态不一致且不可观测**。
- **改法**：`GET /scene-bindings` 的响应增补 `resolution_chain: [{stage, model_name}]`（复用 `_resolve_global_model` 的分支，只读），前端在场景行内折叠展示。不改 `SYSTEM_PROMPT`、不改解析顺序。
- **验收线**：任一场景行展开即可看到 4 级回落中真正命中的那一级；降级发生后（日志可见）页面读数可对账。

---

## 六、验收线与本轮不修项

### 6.1 合并前必须满足（本域 P0）
1. **密钥出站**：`GET /datasources` 与 `PUT /settings/{secret_key}` 的响应体均不含明文凭证（W-4 + 3.7）。
2. **掩码不可写入**：任何形状的 `********`（含前后空格）都无法写进 secret 键（W-3）。
3. **文案不指向空 UI**：`dark_l2.py:313` 提到的 `ths_username` / `ths_sdk_password` 在设置页可得，或文案改为指向 `sec-ths` 卡片（W-1）。
4. **TQ 优先可见**：网关页首屏能看到 vendor 维度与主/备角色（3.1），否则"TQ 优先"只是引擎里的一条隐规则。

### 6.2 本轮明确不改（Frozen Ring / 口径冻结）
- `symbol_guard`、`number_audit`、`caliber.py`、交易 403、TQ 优先链、`SYSTEM_PROMPT` —— 本文所有与 caliber 相关的建议**只做展示层对齐，不动判据**。3.5 提出的 `caliber_version` 标签读的是既有矩阵常量，不新增判定逻辑。
- 买红卖绿：`DataSources.tsx:653` 使用 `text-stock-up` / `text-stock-down` 语义 token，方向正确；第四节 A-3 建议把 `ApiKeys.tsx` 的硬编码色换成语义 token 时，涨跌色必须复用同一对 token，不得引入新色值。

### 6.3 缺陷严重度排序（建议修复顺序）
| 序 | 编号 | 一句话 | 类型 |
|---|---|---|---|
| 1 | W-3 | 加个空格就把真 token 覆盖成 8 个星号 | 数据损坏 |
| 2 | 3.7 / W-4 | 凭证明文进前端 / 进 PUT 响应 | 安全红线 |
| 3 | W-1 | 报错让你去填一个不存在的输入框 | 功能缺失 |
| 4 | W-2 | 敏感键集合前后端不一致（4 vs 7） | 口径分裂 |
| 5 | 3.2 | priority 相等时"下移"是空操作 | 功能失效 |
| 6 | D-1 | `--万` 串字 | 显示错误 |
| 7 | W-7 | 无值校验，填错静默失效 | 可观测性 |
| 8 | 4.4 | 无测 Key / 无推送测试 | 能力缺失 |
| 9 | 3.1 / 3.5 | 无 vendor 面板、无 caliber 标签 | 可视化 |
| 10 | W-9 | Key 池只能看不能编 | 半成品 |

### 6.4 方法说明与可信度
- 本文所有行号取自 `v0.48.0 / 5427af0` 工作树，未做格式化或重排；若源文件在合并前变动，行号需重新对位。
- 所有"缺失"类结论均由**正向 grep 0 命中**（如 `DataSources.tsx` 内 `caliber` = 0、`tabular-nums` = 0）或**入口数组穷举**（`DATA_SOURCE_KEYS` 三项）得出，不是"没看到就断言没有"。
- 未做任何真实接口调用，因此**不存在**真实时延、真实可用性数据；4.4 的 `latency_ms` 是接口设计建议，不是实测值。
