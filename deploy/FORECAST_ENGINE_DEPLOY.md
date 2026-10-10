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
由 systemd 单元 `panwatch-forecast.service` 常驻 + 开机自启; 历史库 `~/.panwatch_forecast.db`。

主服务(panwatch, 容器) 经 `src/web/api/forecast.py::_detect_engine_url()` **从容器默认网关**推出引擎地址
(容器内无 `FORECAST_ENGINE_URL` 时 → `http://<网关IP>:8010`, 生产实测 `http://172.19.0.1:8010`)。
**推论(关键)**: 裸进程必须绑 `0.0.0.0` —— 只听 `127.0.0.1` 时, 容器连宿主机网关:8010 会被拒
(引擎"起来了"但主服务够不着)。这与主服务 `-p 8000:8000`(0.0.0.0)暴露面一致
(生产主机 ufw inactive, 仅 tailnet + docker 网络可达)。

## 2. 部署产物

| 文件 | 作用 |
|---|---|
| `deploy/deploy_forecast_engine.sh` | 幂等部署: 同步 `forecast_server.py` + `forecast_lib/` → 建/复用 venv → 装依赖 → 写 `forecast.env` → 写 systemd unit → 拉起 → 健康检查 |
| `deploy/panwatch-forecast.service` | unit 参考副本(脚本实际生成同名 unit; 内容一致) |
| `scripts/tests/test_deploy_forecast_engine.sh` | stub 契约测试(不触网/不建 venv/不真装) |

安装布局(默认, 可经 `FORECAST_*` 环境变量覆盖):

```
/opt/panwatch-forecast/
├── forecast_server.py
├── forecast_lib/*.py
├── venv/                  # forecast_requirements.txt 全量依赖(torch cpu/xgboost/chronos/timesfm…)
└── forecast.env           # SIDA_MAIN_API_URL / FORECAST_HOST=0.0.0.0 / FORECAST_DB_PATH / TZ
/etc/systemd/system/panwatch-forecast.service
~/.panwatch_forecast.db    # 历史库
```

依赖是**懒加载**的: torch/xgboost/chronos/timesfm 缺失时引擎降级(起得来、`/health` 与
`/referee/stats` 可用), 仅 `/predict` 会因缺模型失败。故依赖安装失败**不阻塞**引擎拉起。

## 3. 在目标主机上拉起(生产实操)

```bash
# 1) 把仓库的 forecast_server.py + forecast_lib/ + deploy/ 送到目标主机(任选 scp/rsync/git clone)
# 2) 以 root 运行(幂等, 可重复执行):
sudo bash /path/to/repo/deploy/deploy_forecast_engine.sh
# 期望末尾:  FORECAST_DEPLOY_OK
# 3) 复核:
systemctl status panwatch-forecast --no-pager
curl -s http://127.0.0.1:8010/health
```

无 systemd 的兜底(WSL 未开 systemd 时):

```bash
cd /opt/panwatch-forecast
FORECAST_HOST=0.0.0.0 FORECAST_DB_PATH=$HOME/.panwatch_forecast.db \
  nohup setsid venv/bin/python3 forecast_server.py >/var/log/panwatch-forecast.log 2>&1 &
```

## 4. 发版接线: 让引擎随主服务拉起

主服务发版链是本机 `~/.hermes/scripts/sida_release.sh <tag>` → `~/.hermes/scripts/sida_prod_deploy.sh <tag>`
(部署 panwatch 容器)。引擎随之拉起有两种接法:

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
curl -s http://127.0.0.1:8010/health            # {"status":"ok","kronos_ready":...}
# 生产侧(经主服务代理)
curl -s "https://www.sida.hengsheng-elec.com/api/forecast/referee-stats?symbol=002361"
#   引擎未起: {"total":0,...,"message":"预测引擎不可用(需在主机运行 forecast_server.py), ..."}
#   引擎已起但无裁判样本: {"total":0,"symbol":"002361","message":"暂无裁判记录(...)"}  ← 显式态
```
