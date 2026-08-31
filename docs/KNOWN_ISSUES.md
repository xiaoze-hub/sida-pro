# 已知问题 / Known Issues

> 记录已发现但尚未修复的问题。修复后移入 CHANGELOG。

## [P1] 前端没有"修改自己密码"的入口 (2026-08-11)

**现象**: 用户(包括 owner)在系统里找不到任何地方修改自己的登录密码。

**现状**:
- 后端接口**已存在**: `POST /api/auth/change-password` (`src/web/api/auth.py:307`)
  - 校验密码 ≥ 8 位, 更新 `password_hash`, `token_version += 1` 踢掉该用户旧 token
- 前端**完全未接入**: 全前端无 `change-password` / `changePassword` 调用
- `Settings.tsx` 只有数据源 key / 同花顺登录态, 无账号密码区块
- `UserManagement.tsx` 仅 owner 可**重置他人**密码 (`updateUser`), 且 owner 自己没有改密码入口

**影响**:
- 普通 member 无法自行改密, 只能找 owner 重置
- owner 初始密码无法自助修改(只能通过后端 API 或直接改库)

**建议修复**:
1. 在 Settings 页新增「账号安全」区块: 旧密码 + 新密码 + 确认新密码
2. 调 `POST /api/auth/change-password`; 成功后可提示重新登录(旧 token 已被踢)
3. 可选: 改密成功后前端强制登出跳转 Login
4. 注意 `change-password` 请求体用的是 `SetupRequest`(仅 password 字段), 若加旧密码校验需扩展请求模型

**涉及文件**:
- `src/web/api/auth.py` (接口已有, 可能需要扩展请求模型加 old_password 校验)
- `frontend/src/pages/Settings.tsx` (新增入口)
- `frontend/src/api/auth.ts` (新增 changePassword 方法)
