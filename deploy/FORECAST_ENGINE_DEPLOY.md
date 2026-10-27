# 预测引擎(8010) 部署形态与发版接线

> 基线: main=v0.13.53 (3f4cac9)。覆盖: `forecast_server.py` / `forecast_lib/` / `Dockerfile.forecast`
> / `.github/workflows/build-push-acr-forecast.yml` / `docker-compose.yml`(forecast 服务) /
> `deploy/*`。未覆盖: 模型权重质量、Kronos 源码构建。

## 1. 结论先行: 引擎是**裸进程**, 不是容器

预测引擎**历来不是容器化部署**。历史事实与证据:

- `docker-compose.yml` 里的 `forecast:8010` 服务、`Dockerfile.forecast`、
  `.github/workflows/build-push-acr-forecast.yml` 只是**早期规划形态**;
- 生产实测 ACR 上**不存在** `xzxwz-forecast` 镜像仓库
  (`docker manifest inspect .../xzxwz-forecast:v0.13.5x` → MISS; `docker images | grep forecast` → 空);
- 生产小主机(WSL `Ubuntu-22.04`)从无 forecast 容器, 也从未接入发版链。

**真实生产形态**: 主机 venv 里跑 `python3 forecast_server.py`(端口 8010),
由 systemd 单元 `sida-forecast.service` 常驻 + 开机自启; 历史库 `~/.sida_forecast.db`。

主服务(sida, 容器) 经 `src/web/api/forecast.py::_detect_engine_url()` **从容器默认网关**推出引擎地址
(容器内无 `FORECAST_ENGINE_URL` 时 → `http://<网关IP>:8010`, 生产实测 `http://172.19.0.1:8010`)。
**推论(关键)**: 裸进程必须绑 `0.0.0.0` —— 只听 `127.0.0.1` 时, 容器连宿主机网关:8010 会被拒
(引擎"起来了"但主服务够不着)。这与主服务 `-p 8000:8000`(0.0.0.0)暴露面一致
(生产主机 ufw inactive, 仅 tailnet + docker 网络可达)。

## 2. 部署产物

| 文件 | 作用 |
|---|---|
| `deploy/deploy_forecast_engine.sh` | 幂等部署: 同步 `forecast_server.py` + `forecast_lib/` → 建/复用 venv → 装依赖 → 写 `forecast.env` → 写 systemd unit → 拉起 → 健康检查 |
| `deploy/sida-forecast.service` | unit 参考副本(脚本实际生成同名 unit; 内容一致) |
| `scripts/tests/test_deploy_forecast_engine.sh` | stub 契约测试(不触网/不建 venv/不真装) |

安装布局(默认, 可经 `FORECAST_*` 环境变量覆盖):

```
/opt/sida-forecast/
├── forecast_server.py
├── forecast_lib/*.py
├── venv/                  # forecast_requirements.txt 全量依赖(torch cpu/xgboost/chronos/timesfm…)
└── forecast.env           # SIDA_MAIN_API_URL / FORECAST_HOST=0.0.0.0 / FORECAST_DB_PATH / TZ
/etc/systemd/system/sida-forecast.service
~/.sida_forecast.db    # 历史库
```

依赖是**懒加载**的: torch/xgboost/chronos/timesfm 缺失时引擎降级(起得来、`/health` 与
`/referee/stats` 可用), 仅 `/predict` 会因缺模型失败。故依赖安装失败**不阻塞**引擎拉起。

### 2.1 Kronos 主模型(脚本 ③b 步, 2026-10-21 补)

`forecast_lib/forecast_models.py` 用 **`~/Kronos` 作源码根**、`import model` 取
`KronosPredictor`。旧脚本完全不装 Kronos, 于是生产出现「部署全绿但 `kronos_ready:false`」的
**静默降级**: 5 票模型里主模型缺席, 只用 Chronos-Bolt / XGBoost / 线性回归 / TimesFM 投票。

现在脚本幂等处理这一步:

| 步骤 | 行为 |
|---|---|
| 源码 | `kronos_src/model/`(vendored 在仓库, ~60KB py) → `$KRONOS_HOME/model/`(`/root/Kronos`, `FORECAST_KRONOS_HOME` 可覆盖) |
| 权重 | venv python 内 `huggingface_hub.snapshot_download` 预拉 `NeoQuasar/Kronos-small` + `NeoQuasar/Kronos-Tokenizer-base`(幂等命中缓存; 失败不阻断, 首次推理会在线拉) |
| 校验 | `python3 -c "import model"`(显式把 `$KRONOS_HOME` 塞 sys.path) |
| 跳过 | `FORECAST_SKIP_KRONOS=1`(仅在明知要缺主模型时用) |

源码为什么随仓库走而不现 clone: `kronos_src/` 是上游 `shiyu-coder/Kronos` 的快照,
部署机不需要 git / 外网 GitHub, 且版本与 `forecast_models.py` 的调用契约钉死。

### 2.2 健康门禁: `kronos_ready` 不通 = 部署失败

`/health` 是**永不 500** 的探针(模型缺失只在 body 里报 `kronos_ready=false` + `kronos_error`),
所以"HTTP 200"不能当部署成功。脚本 ⑦ 步逐次解析 body:

- `kronos_ready:true` → `FORECAST_DEPLOY_OK`, exit 0;
- 引擎通了但 `kronos_ready:false`(等满 30 次探测) → **exit 1**, 并打印修复指引;
- 引擎完全不通 → exit 1(打印日志尾)。

这一门禁两侧都实测过: 把 `KRONOS_ROOT` 指到不存在目录 → 脚本拒绝通过并 exit 非 0;
恢复正常后 → `FORECAST_DEPLOY_OK`。

## 3. 在目标主机上拉起(生产实操)

```bash
# 1) 把仓库的 forecast_server.py + forecast_lib/ + deploy/ 送到目标主机(任选 scp/rsync/git clone)
# 2) 以 root 运行(幂等, 可重复执行):
sudo bash /path/to/repo/deploy/deploy_forecast_engine.sh
# 期望末尾:  FORECAST_DEPLOY_OK
# 3) 复核:
systemctl status sida-forecast --no-pager
curl -s http://127.0.0.1:8010/health
```

无 systemd 的兜底(WSL 未开 systemd 时):

```bash
cd /opt/sida-forecast
FORECAST_HOST=0.0.0.0 FORECAST_DB_PATH=$HOME/.sida_forecast.db \
  nohup setsid venv/bin/python3 forecast_server.py >/var/log/sida-forecast.log 2>&1 &
```

## 4. 发版接线: 让引擎随主服务拉起

主服务发版链是本机 `~/.hermes/scripts/sida_release.sh <tag>` → `~/.hermes/scripts/sida_prod_deploy.sh <tag>`
(部署 sida 容器)。引擎随之拉起有两种接法:

1. **随发版自动拉起(推荐)**: 在 `sida_prod_deploy.sh` 成功路径**末尾**、
   `DEPLOY_OK` 之后追加一步(非致命, 引擎失败不影响主服务判定):

   ```bash
   # 预测引擎(8010)随发版拉起 —— 裸 venv 进程, 非容器
   if [ "${SIDA_SKIP_FORECAST_DEPLOY:-0}" != "1" ]; then
     bash "$(dirname "${BASH_SOURCE[0]}")/sida_prod_deploy_forecast.sh" "$TAG" \
       || echo "⚠️ 预测引擎未拉起(主服务不受影响)"
   fi
   ```

   `sida_prod_deploy_forecast.sh`(与 `sida_prod_deploy.sh` 同级)负责把仓库的
   `forecast_server.py` + `forecast_lib/` + `deploy/` 经 scp 送到目标主机, 再远端执行
   `deploy/deploy_forecast_engine.sh`。引擎与主服务**版本解耦**(引擎代码取自发版所对应的仓库快照)。

2. **手动补拉**: 直接在一台机器上跑 `deploy/deploy_forecast_engine.sh`(见 §3)。

> 版本一致性: 引擎与主服务各自独立发版无碍 —— `/api/forecast/*` 代理协议稳定;
> 引擎侧 `/referee/stats` 无记录时返回**显式 no-data**, 主服务代理原样透传, 前端标注「样本不足」。

## 5. 验收口径(引擎连通)

```bash
# 主机侧
curl -s http://127.0.0.1:8010/health            # {"status":"ok","kronos_ready":true,...}
#   ⚠️ kronos_ready:true 才算主模型在位; false 时 body 带 kronos_error 说明原因(不是引擎故障)
# 生产侧(经主服务代理, 需登录 token)
curl -s -H "Authorization: Bearer <token>" https://SIDA.hengsheng-elec.com/api/forecast/health
#   → 原样透传引擎 /health 载荷; 引擎不通时返回 {"status":"unreachable",...}(HTTP 200, 不 500)
curl -s -H "Authorization: Bearer <token>" \
  "https://SIDA.hengsheng-elec.com/api/forecast/predict?symbol=002361&days=5&force=true"
#   → data.recommendation.models.kronos 非空 = 主模型真参与了投票(带 median/p5/p95/n_samples)
#   409 = 该股票已有未到期预测(节流, 非故障), 加 force=true 可强制重算
curl -s "https://SIDA.hengsheng-elec.com/api/forecast/referee-stats?symbol=002361"
#   引擎未起: {"total":0,...,"message":"预测引擎不可用(需在主机运行 forecast_server.py), ..."}
#   引擎已起但无裁判样本: {"total":0,"symbol":"002361","message":"暂无裁判记录(...)"}  ← 显式态
```

> 生产实测(2026-10-21): `/api/forecast/health` → `kronos_ready:true`,
> `POST :8010/predict {002361, days:5, force:true}` → 200 / ~35s,
> `models.kronos` 有 5 日 median + P5/P95 + n_samples=30。
