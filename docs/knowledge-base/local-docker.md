# SIDA 本地 Docker 运行手册

> 基线标识：应用源码 `d774ae01`，`VERSION=v0.80.0`；本地编排为本次新增的 docker-compose.local.yml / Dockerfile.local；验证日期：2026-10-26。
> 盘点对象：`/Users/winter/Documents/zicalWork/sida-pro`；Docker context `desktop-linux`；Compose project `sida-local`。
> 覆盖范围：独立三服务编排、当前源码与前端挂载、全新 PG 初始化、认证状态、HTTP 健康检查及静态入口；构建与相关后端回归。
> 未覆盖：独立预测引擎、全量业务页面/供应商数据、L2 专有 SDK 在 arm64 上的能力、浏览器截图/PDF 能力、生产发布和完整测试套件。

## 当前运行方式

| 服务 | 容器 | 访问与存储 |
| --- | --- | --- |
| 主服务 | `sida-local-app-1` | `http://localhost:8000`；`sida-local_app_data` |
| PostgreSQL + TimescaleDB | `sida-local-postgres-1` | 仅 Compose 网络 `postgres:5432`；`sida-local_pg_data` |
| Redis | `sida-local-redis-1` | 仅 Compose 网络 `redis:6379`，密码认证；`sida-local_redis_data` |

登录使用仓库现有 `.env` 的 `AUTH_USERNAME` / `AUTH_PASSWORD`。新环境在独立数据库中初始化该账号，不继承旧数据库的会话、持仓和用户。密码、JWT 和接口 Key 不写入本手册。

原有 `panwatch`、`panwatch-postgres`、`panwatch-redis` 保持停用，旧容器与旧数据卷保留。本地新栈不复用它们，避免新版本迁移改变旧运行数据。

## 首次启动

在仓库根目录执行。需要 Docker Desktop 已启动、pnpm 可用，以及 `.env` 中已设置 `POSTGRES_PASSWORD`、`REDIS_PASSWORD` 和认证配置。

```bash
# 1. 构建当前前端（包括 TypeScript 检查）
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..

# 2. 为本地产物注入 Service Worker 版本；不改 frontend/public 源文件
python3 - <<'PY'
from pathlib import Path
path = Path('frontend/dist/sw.js')
version = Path('VERSION').read_text().strip()
path.write_text(path.read_text().replace('__SW_VERSION__', version))
PY

# 3. 构建依赖补层并启动三个服务
docker compose -f docker-compose.local.yml config --quiet
docker compose -f docker-compose.local.yml build app
docker compose -f docker-compose.local.yml up -d --no-build --wait --wait-timeout 180
```

这是独立编排，直接指定 `-f docker-compose.local.yml`，不要与默认 compose 或 dev overlay 合并。默认 compose 的 forecast 依赖、固定容器名和旧数据卷策略与本地最小栈不同。

Dockerfile.local 复用机器已有的 **arm64** 运行时镜像，固定 digest `sha256:27713687b6a2b23eaa0d41646e71915bca4ed462999958a37aaa84b459b550c9`。本次核对该基底的已安装 Python distribution：与锁文件的具名版本一致，只有新增的 `pypinyin==0.55.0` 缺失，因此本地镜像只补该依赖。该核对不等于重新从源码构建所有第三方依赖或核验它们的源代码。

业务代码、`packages/marketdata`、prompt、策略和 VERSION 从当前仓库只读挂载；前端挂载 `frontend/dist`。所以页面与后端使用当前源码，不使用基底镜像内的 9 月旧业务文件。`PYTHONPATH` 明确优先本地 marketdata，避免 editable 包链接仍指向旧安装位置。

其他架构的机器需要用兼容的 `LOCAL_BASE_IMAGE` 覆盖基底，并重新核对锁定依赖。后续依赖、镜像或产品版本变化时，同步维护 Dockerfile.local 和本地 image tag。本地补层不是生产发布镜像；生产按标准 Dockerfile/发布流程构建。

## 日常启停与源码更新

```bash
# 状态与日志
docker compose -f docker-compose.local.yml ps
docker compose -f docker-compose.local.yml logs --tail 100 app

# 停止，保留数据与容器
docker compose -f docker-compose.local.yml stop

# 再次启动
docker compose -f docker-compose.local.yml up -d --no-build --wait

# 需要时只重启后端
docker compose -f docker-compose.local.yml restart app
```

`src/` 改动由 uvicorn reload 监听。修改 prompt、marketdata 包或其他挂载文件后，必要时重启 app；前端修改后重跑 `pnpm build` 和上面的 SW 版本注入即可，通常无需重建 Docker。只有依赖、Dockerfile 或其他未挂载安装内容变化时才重建。

本地只开放回环端口。8000 冲突时可临时使用 `LOCAL_WEB_PORT=8001 docker compose -f docker-compose.local.yml up -d --no-build --wait`，访问对应端口。

`docker compose down` 会移除容器和项目网络但保留命名卷。**不要加 `-v`**，除非明确要删除此本地环境的数据。

## 明确的可选能力

- `SIDA_ENABLE_SCHEDULERS=0` 默认关闭定时任务、自动 Agent 与相关微信 BOT 启动；健康检查显示 `standby`。初次运行仍有股票列表等后台初始化请求，关闭调度不等于完全离线。
- 需要自动盯盘时，在启动命令前设置 `LOCAL_ENABLE_SCHEDULERS=1`；它会使本实例启动调度，可能调用 AI 和推送。恢复默认启动后会重新关闭。
- 独立预测引擎未包含在本地三服务中。主服务尝试 `http://host.docker.internal:8010`，可通过 `LOCAL_FORECAST_ENGINE_URL` 覆盖。引擎未启时健康字段为 `forecast_engine.status=down`，预测接口按现有逻辑降级；整体主服务仍可 `ok`。
- `PLAYWRIGHT_SKIP_BROWSER_INSTALL=1` 跳过浏览器下载；截图/PDF 等能力需另行安装运行时浏览器后再验证。arm64 上 thsdk 的 amd64 专有库也可能降级。
- 新数据卷没有历史持仓、报告、业务配置或完整行情库；接口 Key 和来源网络连通性决定具体业务能力。登录成功与 HTTP 健康不能证明全部行情/AI 功能可用。

## 验证状态

2026-10-26 已验证前端 `pnpm build`、三服务 Compose health、`/login`、`/api/auth/status` 和 `/api/health`；使用现有环境认证完成登录，并验证带 token 的 `/api/auth/me`。登录 HTML 引用的 4 个 JS/CSS 资源与本地 dist 内容一致，SW 版本已注入。服务健康响应经统一包装，读取方式为：

```bash
python3 - <<'PY'
import json
from urllib.request import urlopen
payload = json.load(urlopen('http://127.0.0.1:8000/api/health', timeout=15))
data = payload['data']
print(data['status'], data['version'])
for name, component in data['components'].items():
    print(name, component.get('status', component.get('redis', 'n/a')))
PY
```

主服务返回 `ok / v0.80.0`，数据库方言 `postgresql`，Redis 与 biz_cache 正常；调度处于显式关闭的 standby，预测引擎未启。

新 PG 启动时发现两处 `BOOLEAN DEFAULT 0` 导致建表失败，已在 `d774ae01` 改为 SQLAlchemy `false()`。独立测试容器执行 `test_schema_boolean_defaults.py` 与 `test_admin_user_console.py`，9 项通过；真实全新 PG 随后完成启动。此验证未运行全量测试。

镜像清理先确认替换服务健康，只删除没有被任何容器引用的旧 PanWatch/临时验证镜像；保留被停用容器引用的镜像、所有数据卷和共享构建缓存。
