# 权限矩阵 · 2026-09-15

> 目标：guest / 普通 / Pro / admin 四档；普通账号只开放行情、板块热力图、持仓；
> 锁死：机会、数智决策三指标、暗盘资金、L2资金。
> 设置：个人中心=我的东西，系统设置=admin专属。
> 约定：推送上限不设置。

## 角色定义

| 角色 | 标识 | 说明 |
|---|---|---|
| 游客 | `guest` | 未登录，只看首页驾驶舱 |
| 普通 | `member` | 行情、热力图、持仓；自选≤10、预警≤3 |
| Pro | `pro` | 全功能 |
| 管理员 | `owner` | 全功能 + 系统设置 |

## 功能/页面矩阵

| 功能 | guest | member | pro | owner | 备注 |
|---|---|---|---|---|---|
| 首页驾驶舱 | ✅ | ✅ | ✅ | ✅ | 只读行情/指数 |
| 行情（K线/个股） | 🔒 | ✅ | ✅ | ✅ | guest 弹注册 |
| 板块热力图 | 🔒 | ✅ | ✅ | ✅ | |
| 持仓（读写） | 🔒 | ✅ | ✅ | ✅ | 自选≤10 / 预警≤3 |
| **机会页** | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | member 用完弹 Pro |
| **数智决策三指标** | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | |
| **暗盘资金** | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | |
| **L2资金** | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | |
| 报告中心 | 🔒 | ✅ | ✅ | ✅ | |
| 影子账户 | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | |
| 预测 | 🔒 | ⚠️ 试用3次 | ✅ | ✅ | |
| 题材情绪 | 🔒 | ✅ | ✅ | ✅ | |
| 通知中心 | 🔒 | ✅ | ✅ | ✅ | |
| **个人中心** | 🔒 | ✅ | ✅ | ✅ | 个人资料/自选/BYOK/通知/用量 |
| **系统设置** | 🔒 | 🔒 | 🔒 | ✅ | 数据源/默认模型/限流/用户管理 |
| Skill Gateway | — | — | — | — | 独立 Key 体系，与登录态无关 |

## API 权限点

| 权限点 | guest | member | pro | owner |
|---|---|---|---|---|
| `view_quote` | ❌ | ✅ | ✅ | ✅ |
| `view_heatmap` | ❌ | ✅ | ✅ | ✅ |
| `edit_portfolio` | ❌ | ✅ | ✅ | ✅ |
| `view_opportunities` | ❌ | 试用 | ✅ | ✅ |
| `view_forecast` | ❌ | 试用 | ✅ | ✅ |
| `view_l2` | ❌ | 试用 | ✅ | ✅ |
| `view_dark` | ❌ | 试用 | ✅ | ✅ |
| `manage_users` | ❌ | ❌ | ❌ | ✅ |
| `manage_system` | ❌ | ❌ | ❌ | ✅ |

## 试用额度

- 普通账号对「机会 / 三指标 / 暗盘 / L2」各 **3 次/天**
- 用完返回 403 + `pro_guide` 字段，前端弹 Pro 引导
- 计数表 `member_trial_usage(user_id, feature, date, count)`

## 设备限制

- 同账号同时在线设备 ≤ 2
- 第 3 台登录 → 踢最早会话
- 实现：`user_sessions` 表 + 登录时清理过期 + 超限踢旧

## 设置拆分

| 模块 | 位置 | 权限 |
|---|---|---|
| 个人资料 | 个人中心 | self |
| 自选管理 | 个人中心 | self |
| 我的模型 key（BYOK） | 个人中心 | self |
| 通知渠道 | 个人中心 | self |
| 用量查询 | 个人中心 | self |
| Pro 申请 | 个人中心 | self |
| 数据源开关 | 系统设置 | owner |
| 系统默认模型 | 系统设置 | owner |
| 限流参数 | 系统设置 | owner |
| 用户管理 | 系统设置 | owner |
| key 冻结/额度 | 系统设置 | owner |

## `GET/PUT /api/settings` 的读写拆口（2026-11-07）

| 端点 | 门槛 | 说明 |
|---|---|---|
| `GET /api/settings`、`GET /api/settings/avatar`、`GET /api/settings/version` | 登录即可 | 敏感 key 已掩码（`********`），只读无风险 |
| `PUT /api/settings/{key}` | `manage_settings`（端点级依赖 `require_settings_write`） | 全局配置写：数据源 token / `http_proxy` / 通知参数 |
| `PUT /api/settings/avatar` | `manage_settings` | `ui_avatar` 是全局单行，不是 per-user |

判权细节（三条都必须同时成立）：

1. **必须在执行端判权**，不能只靠 `demo_isolation_middleware` 的路径前缀 ——
   `ApiVersionAliasMiddleware` 在内层，会把 `/api/v1/settings/x` 改写成
   `/api/settings/x`，前缀判断曾因此落空（member 实测 200 写穿）。
2. 角色**实时从写库读**，不信 JWT 里的 `role` claim（降权后旧 token 立即失效）。
3. 服务身份（`ServicePrincipal` / `role=service`）一律 403；判权查询异常一律 403（fail closed）。

写入审计与设置**同一次提交**：审计写不进去 → 整笔回滚 + 5xx，不谎报成功；
明细里敏感值只记 `********`，代理地址去 userinfo（`http://***@host`）。
