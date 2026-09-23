# SIDA-Pro 项目现状

> 整理时间：2026-09-02 · 当前版本：v0.4.63（CI 中）
> 仓库：`github.com/xiaoze-hub/sida-pro`（private）
> 闭源版 / 自主服务器部署 / 个人+小团队使用

---

## 一、代码结构

```
sida-pro/ (主仓库: github.com/xiaoze-hub/sida-pro, private)
│
├── backend Python (~81k LOC, ~150 个 .py)
│   ├── src/
│   │   ├── agents/                ← 12 个分析 Agent
│   │   │   ├── intraday_monitor.py    ← 主力意图（关键 P4）
│   │   │   ├── chart_analyst.py       ← K 线形态
│   │   │   ├── daily_report.py        ← 日报
│   │   │   ├── premarket_outlook.py   ← 盘前
│   │   │   ├── auction_review.py      ← 竞价复盘
│   │   │   ├── news_digest.py         ← 资讯摘要
│   │   │   ├── theme_launch_detector.py
│   │   │   ├── stock_attribution.py   ← 归因
│   │   │   └── chat/                 ← AI 助手工具
│   │   │       ├── chat_tools.py       ← 39 个核心工具
│   │   │       └── tools_thsdk.py      ← +11 个 thsdk 工具 = 50+ 工具
│   │   ├── collectors/            ← 数据采集（klines_ingestor 等）
│   │   ├── core/                  ← 核心业务逻辑（57+ 文件）
│   │   │   ├── dark_*              ← 暗盘融合（P4 主线）
│   │   │   │   ├── dark_flow_fusion.py   ← 主路径
│   │   │   │   ├── dark_l2.py           ← thsdk L2 入口
│   │   │   │   ├── dark_flow.py         ← 腾讯逐笔兜底
│   │   │   │   ├── dark_fund_scan.py    ← 全市场扫描
│   │   │   │   ├── dark_split.py        ← 拆单识别
│   │   │   │   ├── dark_pool_flow.py    ← 委托号级聚簇
│   │   │   │   └── dark_flow_l2.py      ← thsdk 主笔/被动
│   │   │   ├── gs_strategy.py       ← GS 买卖点
│   │   │   ├── chip_distribution.py ← 筹码分布
│   │   │   ├── market_scan.py      ← 异动共振
│   │   │   ├── resonance.py         ← 多源共振选股
│   │   │   └── ai_client.py         ← LLM 调用（429 限流熔断）
│   │   ├── models/                 ← SQLAlchemy ORM
│   │   └── web/api/                ← FastAPI 路由
│   │       ├── klines.py            ← K 线 + summary 聚合
│   │       ├── quotes.py            ← 实时行情
│   │       ├── chat.py              ← AI 对话
│   │       ├── marketScan.py        ← 全市场扫描 API
│   │       ├── portfolio.py / watchlist.py
│   │       ├── ws_hub.py            ← WebSocket Hub
│   │       ├── ws_notifications.py  ← WS 通知
│   │       └── source_health.py     ← 数据源健康
│   ├── tests/                      ← pytest 86+ passed
│   └── data/ / config/             ← 运行期数据/配置
│
├── frontend (React 18 + TS + Vite + Tailwind, ~43k LOC)
│   ├── src/
│   │   ├── pages/                  ← 27 个页面
│   │   │   ├── Quote.tsx (v0.4.60 去卡片化重写) ← ★ 行情核心
│   │   │   ├── Dashboard.tsx
│   │   │   ├── Forecast.tsx / AnalysisDetail.tsx
│   │   │   ├── Opportunities.tsx / DarkFundTop.tsx
│   │   │   ├── ReportsHub.tsx / Reports.tsx
│   │   │   ├── SettingsHub.tsx / NotificationsHub.tsx
│   │   │   └── ... (21+)
│   │   ├── components/
│   │   │   ├── KlineChart.tsx (biz-ui) ← Lightweight Charts v5
│   │   │   ├── ContextCard.tsx (设计稿 §13)
│   │   │   ├── ErrorBanner.tsx
│   │   │   └── ...
│   │   └── hooks/
│   │       └── useSourceHealth.ts  ← §12 兜底灰显
│   └── packages/                   ← pnpm workspace
│       ├── api/                    ← API 客户端
│       │   ├── insight.ts / auth.ts / portfolio.ts
│       │   └── marketScan.ts / chat.ts
│       ├── base-ui/                ← shadcn 基础组件
│       └── biz-ui/                 ← 业务组件
│           ├── KlineChart.tsx / klineEvents.ts
│           └── ...
│
├── docs/                           ← 设计文档
│   ├── TIMEZONES.md                ← 三口径时间约定
│   ├── KNOWN_ISSUES.md
│   └── PROJECT_MAP.md
├── forecast_lib/                   ← 预测引擎（Kronos）
├── data_source/                    ← thsdk_l2.py 等 vendor（私有）
├── deploy/                         ← 部署脚本
├── AGENTS.md / CLAUDE.md / CONTRIBUTING.md
├── Dockerfile / docker-compose.yml / build.sh
├── VERSION                          ← 镜像版本号（CI 读这个）
└── .github/workflows/
    ├── build-push-acr.yml           ← ★ tag push → 构建 → 推阿里云 ACR
    ├── build-and-push-image.yml     ← 海外 ghcr.io 备用
    ├── release.yml                  ← 发版编排
    └── pullfrog.yml
```

---

## 二、项目架构

### 生产拓扑

```
┌────────────────────────────────────────────────────────────────┐
│ 用户浏览器 │
└──────────────────────────┬─────────────────────────────────────┘
                           │ HTTPS
┌──────────────────────────▼─────────────────────────────────────┐
│ 101.35.244.238 (国内云，4C4G70G6M)                              │
│  · nginx 反代: proxy_pass → 小主机 Tailscale IP:8000 │
│  · SSL 终结 (Let's Encrypt)                                   │
│  · 兜底容器: 旧 v0.4.28 镜像 standby                         │
└──────────────────────────┬─────────────────────────────────────┘
                           │ (Tailscale Mesh)
┌──────────────────────────▼─────────────────────────────────────┐
│ 小主机 N150 (100.91.30.35, Win11 + WSL Ubuntu 22.04)         │
│  ┌───────────────────────────────────────────────────┐         │
│  │ WSL Docker 网络 (panwatch-net)                    │         │
│  │ ┌────────────────┐  ┌────────────────┐  ┌───────┐  │         │
│  │ │  panwatch       │  │ panwatch-  │  │panwatch-│  │         │
│  │ │  (v0.4.63)      │  │ postgres   │  │ redis  │  │         │
│  │ │  PanWatch 主容器 │  │ TimescaleDB │  │ Redis7 │  │         │
│  │ │  8000 ← HTTP │  │  PG16 │  │        │  │         │
│  │ └────────────────┘  └────────────────┘  └───────┘  │         │
│  │                                                     │         │
│  │  TdxW.exe (Win11) → 172.27.16.1:17709 (TQ L2)      │         │
│  │  ↓                                               │         │
│  │  WSL 内 Tailscale IP: 100.91.30.35                  │         │
│  └───────────────────────────────────────────────────┘         │
│                                                                │
│ /root/sida-pro/   ← 源码 (cron 同步 + exclude .git)         │
│ /mnt/c/Users/tianxiang/sida-pro/  ← 同样源码备份           │
└──────────────────────────┬─────────────────────────────────┘
                           │ (单向 cron 同步)
┌──────────────────────────▼─────────────────────────────────┐
│ 43.128.140.167 (海外本机 Hermes Agent)                        │
│ /home/ubuntu/sida-pro/  ← git 主仓库 (开发/编译起点)       │
│  · cron 45d3592196a4 (每小时整点) → sync_sida.sh → 小主机│
│  · dev/feature/quote-decardify 等本地分支                  │
└────────────────────────────────────────────────────────────┘
                           │ git push (普通 commit + tag)
┌──────────────────────────▼─────────────────────────────────┐
│ GitHub (github.com/xiaoze-hub/sida-pro, private)             │
│  Actions workflow build-push-acr.yml 触发条件: tag push    │
│       ↓                                                       │
│  ACR 镜像 (阿里云上海)                                       │
│  crpi-mte80ai8o78b1429.cn-shanghai.personal.cr.aliyuncs.com │
│       /xiaozexwz/xzxwz:v0.4.X                                │
└────────────────────────────────────────────────────────────┘
```

### 关键依赖

- **数据源**：
 - 通达信 L2（TdxW.exe 小主机 Win11 跑，WSA: 17709）
 - 同花顺 thsdk（同花顺 L2 官方账号：mx_8lj4le6qd / xzxwz170530）
 - 兜底：东财 / 腾讯 / 新浪 HTTP 接口
- **数据库**：TimescaleDB hypertable（klines 12万+ 行，PG16）
- **缓存**：Redis 7（缓存 + 限流 + Streams）
- **LLM**：多模型 (custom_tokenrhythm 池 + BYOK)
- **thsdk vendor**：`ghcr.io/xiaoze-hub/thsdk-vendor:v1.7.18`（私有 amd64 二进制）

---

## 三、目前存在的问题

### 🔴 P0：生产阻塞

1. **v0.4.62 容器崩溃**（我自己 IndentationError 引入）—— v0.4.63 已修复，正在重新部署
2. **小主机单点故障**：网络 / 断 / dockerd 任何一处挂，公网就 502（已发生过 1 次）

### 🟡 P1：功能缺失 / 不正确

| # | 问题 | 状态 |
|---|---|---|
| 1 | 设计稿 §11.5 通知送达回执：后端无 status 机，前端无抽屉 UI | v0.4.64 候选 |
| 2 | 持仓成本线画 K 线（替代 ContextCard 顶部卡） | v0.4.64 候选 |
| 3 | 后端 `main_net_ratio` 字段名错（thsdk "主力净量" 不是百分比） | v0.4.64 待修 |
| 4 | `intraday_monitor.py` 文本格式化硬编码 "万"（v0.4.62 部分修但当时崩了） | v0.4.63 验证中 |

### 🟢 P2：体验/数据

| # | 问题 | 状态 |
|---|---|---|
| 1 | 暗盘 TOP 榜数据本身小（如 12.52 万主力净流入）—— thsdk DDE 字段含义需核实 | 待查 |
| 2 | 30 万边界注释 `">"` vs 实现 `">="`（xiaoze 黄组修了注释，未改实现） | 保留 `>=` |
| 3 | L1 趋势层 MA30/120：xiaoze 确认设计稿只到 MA5/10/20/60 | ✅ 正确 |

### ⚠️ 协作流程问题

1. **小主机 `/root/sida-pro` 不是 git 仓库**（cron sync exclude `.git`）—— 小主机改的代码**不会自动回 main**
2. **xiaoze 红 5 黄 3 修复**（2026-09-02 11:53Z 报告）**0 附件**—— 已回信等 diff
3. **v0.4.62 教训**：execute_code 用 str.replace 改多行带缩进代码，缩进经常出错 —— 下次先 sed 看缩进基准

---

## 四、开发目录在哪

| 机器 | 路径 | git? | 备注 |
|---|---|---|---|
| 海外本机（Hermes Agent）| `/home/ubuntu/sida-pro/` | ✅ git 主仓库 | 开发主力，本地分支 |
| 海外本机（dev）| `/home/ubuntu/sida-pro/`（同）| ✅ git | 当前 `feat/quote-decardify` 分支 |
| 小主机 WSL（root）| `/root/sida-pro/` | ❌ **无 git** | cron 同步目标，源码拷贝 |
| 小主机 Windows| `/mnt/c/Users/tianxiang/sida-pro/` | ❌ 无 git | Windows 用户目录（同样拷贝）|
| 小主机 通达信相关| `/mnt/c/TdxQ/mi1/sida-pro/` | ❌ | TdxQ 关联 |

**⚠️ 关键**：小主机上**直接改代码不会自动回 main**。要回 main 必须：

```
小主机 git commit + push (但小主机无 git)
   ↓
小主机把 diff 发附件给 Hermes
   ↓
Hermes 整合到 main + push
   ↓
cron 同步带回到小主机
```

---

## 五、怎么更新到 git 上

### 5.1 单文件修改

```bash
cd /home/ubuntu/sida-pro

# 1. 编辑文件 (vim / VS Code / Cursor)

# 2. 验证
python3 -m pytest tests/<相关>.py -v    # 跑测试
cd frontend && pnpm build                # 前端 tsc + build

# 3. 写 CHANGELOG（强制：commit 必须含 CHANGELOG 同改）
vim CHANGELOG.md   # 在 "## 2026-09-02" 下加 fix/feature/update/doc 条目

# 4. 提交（commit 规范）
git add .
git commit -m "fix: <中文 subject>"       # type: fix/feature/update/doc

# 5. 推送（普通 commit 不触发 ACR 构建）
git push origin main
# 或推送 tag（触发 ACR 构建）：
git tag v0.4.X
git push origin main v0.4.X             # 双 push: main + tag
```

### 5.2 Commit 规范（来自 AGENTS.md）

- **类型**：`fix` / `feature` / `update` / `doc`
- **主题**：英文 type + 冒号 + 中文 subject（例：`feature: 新增盘中监控 Agent`）
- **CHANGELOG**：每个 commit **必须**同 commit 更新 `CHANGELOG.md`
- **测试**：每个新模块加 happy-path 测试

### 5.3 关键脚本

- `sync_sida.sh`（cron 触发）：打包源码 → 上传 OSS → 小主机下载解压
- `build.sh <version>`：单镜像构建脚本
- `Makefile`：`make dev-api` / `make dev-web` 起开发环境

---

## 六、怎么自动传到阿里镜像自动构建

### 6.1 触发链

```
开发者 git push (普通 commit 或 tag)
   ↓
GitHub 接收 push
   ↓
.github/workflows/build-push-acr.yml 触发
   ↓
Step 1: Checkout (fetch-depth: 0)
Step 2: Read VERSION (从 GITHUB_REF_NAME 拿 tag)
Step 3: Set up QEMU
Step 4: Set up Docker Buildx
Step 5: Login GitHub Container Registry (拉私有 thsdk-vendor)
Step 6: Login Aliyun ACR
Step 7: Build and push (多阶段构建)
   ↓
阿里云 ACR (上海 region)
   ↓
crpi-mte80ai8o78b1429.cn-shanghai.personal.cr.aliyuncs.com/xiaozexwz/xzxwz:v{VERSION}
```

### 6.2 触发方式

| 触发 | 触发 ACR 构建？ | 备注 |
|---|---|---|
| `git push origin main`（普通 commit）| ❌ **不触发** | 浪费推送 |
| `git push origin v0.4.X`（tag push）| ✅ **触发** | 发版必须 |
| GitHub Actions 页面 → Run workflow | ✅ **触发** | 手动重跑 |

### 6.3 镜像地址

```
crpi-mte80ai8o78b1429.cn-shanghai.personal.cr.aliyuncs.com/xiaozexwz/xzxwz:v{VERSION}
```

### 6.4 关键文件

- `.github/workflows/build-push-acr.yml` — ACR 构建
- `.github/workflows/build-and-push-image.yml` — 海外 ghcr.io 备用
- `Dockerfile` — 多阶段构建（thsdk-vendor → frontend-builder → 后端）
- `Dockerfile.forecast` / `Dockerfile.kronos` — 预测引擎独立镜像

### 6.5 Dockerfile 关键

```dockerfile
# 默认 DaoCloud 镜像源（国内构建快，无需登录）
ARG NODE_IMAGE=docker.m.daocloud.io/library/node:20-alpine
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
# thsdk vendor（私有 amd64 二进制）
ARG THSDK_IMAGE=ghcr.io/xiaoze-hub/thsdk-vendor:v1.7.18
# 多阶段构建：
# Stage 0: FROM thsdk-vendor AS thsdk-vendor（拉私有 binary）
# Stage 1: 前端 pnpm install + vite build
# Stage 2: 后端 COPY 前端 dist + Python deps + server.py
```

---

## 七、怎么拉到小主机部署

### 7.1 部署流程

```
阿里云 ACR (镜像就绪)
   ↓
ssh TIANXIANG@100.91.30.35 (走 Tailscale)
   ↓
wsl -e bash -lc "<部署命令>"
   ↓
1. docker pull crpi-xxx:v0.4.X
2. docker tag ... v0.4.X
3. docker rm -f panwatch (停旧)
4. docker run -d --name panwatch ... (启动新)
   ↓
5. docker exec panwatch cat /app/VERSION (验证)
6. curl http://localhost:8000/api/health (200)
```

### 7.2 部署脚本模板

```bash
#!/bin/bash
set -uo pipefail
IMG=crpi-mte80ai8o78b1429.cn-shanghai.personal.cr.aliyuncs.com/xiaozexwz/xzxwz:v0.4.X
docker pull $IMG || { echo PULL_FAILED; exit 1; }
docker rm -f panwatch 2>/dev/null
docker run -d --name panwatch \
  -p 8000:8000 \
  -v panwatch_data:/app/data \
  -v /home/ubuntu/.hermes:/hermes:ro \
  -e HERMES_HOME=/hermes \
  -e AUTH_USERNAME=admin \
  -e 'AUTH_PASSWORD=CYBgg#lAqKYB3KWg' \
  -e WEB_HOST=0.0.0.0 \
  -e TZ=Asia/Shanghai \
  -e DATA_DIR=/app/data \
  -e 'SIDA_DB_URL=postgresql+psycopg2://sida:PanWatch2026PG@postgres:5432/sida' \
  -e 'REDIS_URL=redis://redis:6379/0' \
  -e 'THS_USERNAME=mx_8lj4le6qd' \
  -e 'THS_PASSWORD=xzxwz170530' \
  -e 'PANWATCH_TCK_DIR=/app/data/tck' \
  --network panwatch-net \
  --restart unless-stopped \
  $IMG
sleep 3
docker ps --filter name=panwatch --format '{{.Image}} {{.Status}}'
docker exec panwatch cat /app/VERSION
```

**使用方式**：打包成 base64，通过 ssh 传到小主机 wsl 执行（避免引号嵌套问题）

```bash
# 本机
B64=$(base64 -w0 /tmp/rbXX.sh)
sshpass -p '980530' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  TIANXIANG@100.91.30.35 \
  "wsl -e bash -lc \"echo $B64 | base64 -d > /tmp/r.sh && bash /tmp/r.sh\""
```

### 7.3 关键环境变量

| 变量 | 作用 | 必需 |
|---|---|---|
| `AUTH_USERNAME` / `AUTH_PASSWORD` | admin 登录凭据 | ✅ |
| `HERMES_HOME=/hermes` | cron 输出只读挂载（报告中心数据源）| 推荐 |
| `WEB_HOST=0.0.0.0` | 监听所有接口（容器内必需）| ✅ |
| `TZ=Asia/Shanghai` | 时区（容器日志 / cron 对齐）| ✅ |
| `DATA_DIR=/app/data` | named volume 挂载点 | ✅ |
| `SIDA_DB_URL` | PG 连接串 | ✅ |
| `REDIS_URL` | Redis 连接串 | ✅ |
| `THS_USERNAME` / `THS_PASSWORD` | 同花顺 L2 凭据 | ✅ |
| `PANWATCH_TCK_DIR=/app/data/tck` | 通达信委托回放目录 | ✅ |

### 7.4 关键挂载

| 挂载 | 说明 |
|---|---|
| `-p 8000:8000` | Web UI |
| `-v panwatch_data:/app/data` | named volume（DB + .tck + Playwright 浏览器）|
| `-v /home/ubuntu/.hermes:/hermes:ro` | cron 输出（只读）|
| `--network panwatch-net` | 必须加，让 panwatch 解析 `postgres`/`redis` 主机名 |
| `--restart unless-stopped` | 机器重启自动拉起 |

### 7.5 端到端验证清单

```bash
# 1. 健康检查
curl http://localhost:8000/api/health
# 期望: {"code":0,"success":true,"data":{"status":"ok","version":"v0.4.X",...}}

# 2. 摘要接口
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"xz.170530"}' | jq -r .data.token)
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/klines/002361/summary | jq '.data | {fund_flow_count: (.fund_flow|length), events_count: (.events|length), gs_signals_count: (.gs_signals|length)}'

# 4. 公网域名（验证 nginx upstream）
curl -I https://www.sida.hengsheng-elec.com/api/health

# 5. TQ 网关
docker logs panwatch | grep "TQ 网关自动命中"

# 6. thsdk 连接
docker logs panwatch | grep -i "thsdk\|账户"
```

---

## 八、版本节奏

| 版本 | 关键变更 |
|---|---|
| v0.4.50 | orderbook 假阳性修复 + 暗盘 TOP 榜 5 文件 |
| v0.4.55 | TQ 自动发现 + 暗盘融合 + WAF 识别（11 文件）|
| v0.4.56 | fetch_bars 兜底 + bars 不早退（3 文件）|
| v0.4.57 | .tck 事件日期错位 + 日期格式统一 |
| v0.4.58 | gs_signals date 规范化 + TIMEZONES.md |
| v0.4.59 | thsdk buffer_size 扩容 2MB→8MB（修暗盘 17x 缺口）|
| v0.4.60 | Quote.tsx 去卡片化重写 |
| v0.4.61 | 三处紧急修复（GS 标记 / 单位 / 占比显示）|
| v0.4.62 | 后端 main_intent 单位万/亿自动选（部署崩了）|
| **v0.4.63** | **v0.4.62 缩进修复（CI 中）** |

---

## 九、立即行动项

| # | 行动 | 紧急度 |
|---|---|---|
| 1 | 等 v0.4.63 CI 完 → 立即部署小主机 | **🔴 阻塞**（当前公网 500）|
| 2 | 等 xiaoze 红 5 黄 3 修复 diff | 🟡 |
| 3 | 暗盘 TOP 数据本身小（12.52 万）核实 thsdk 字段含义 | 🟡 |
| 4 | v0.4.64 计划：通知回执 + 持仓成本线 + 后端字段名修正 | 🟢 |
| 5 | 长期：小主机健康监测 + 自动 fallback | 🟢 |

---

## 十、文档与规范

| 文件 | 说明 |
|---|---|
| `AGENTS.md` | Agent 协作规范（commit / 测试 / 安全）|
| `CLAUDE.md` | AI 编码助手行为规范（受保护文件，写入需用户授权）|
| `CONTRIBUTING.md` | 贡献者指南 |
| `CHANGELOG.md` | 所有变更记录（强制 commit 包含）|
| `docs/TIMEZONES.md` | 三口径时间约定（容器 / 邮件 / TQ）|
| `docs/KNOWN_ISSUES.md` | 已知问题清单 |
| `docs/PROJECT_MAP.md` | 项目地图 |
| `DESIGN.md`（本地）| 设计稿 v2.0 + v2.1 |