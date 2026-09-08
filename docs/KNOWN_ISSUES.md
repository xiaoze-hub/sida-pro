# 已知问题 / Known Issues

> 记录已发现但尚未修复的问题。修复后移入 CHANGELOG。
> 高危项必须有 owner 与期限; 依赖类漏洞升级后由 dependabot/手动 bump 关闭并在下轮审计复核。

## 依赖安全审计 (W2.5/E5+E6, 2026-09-09)

复现命令:

```bash
# 前端 (npmmirror 镜像无 audit 端点, 必须指回官方 registry)
cd frontend && pnpm audit --registry=https://registry.npmjs.org/
# 后端 (在装有锁定依赖的 python:3.11 环境执行; 生产镜像由 CI/ACR 构建时安装)
pip-audit -r requirements-lock.txt --no-deps
```

结论汇总: pnpm audit 28 条 (critical 0 / high 11 / moderate 15 / low 2);
pip-audit 结果见下节。

### 高危项登记

| # | 依赖 | 装机版本 | 问题 | 修复版本 | 暴露面 | Owner | 期限 |
|---|------|---------|------|---------|--------|-------|------|
| 1 | react-router-dom | 6.30.3 | 开放重定向→XSS (moderate, 5 条含 react-router/@remix-run/router) | >=6.30.6 (同大版本 patch) | **运行时**, 用户可触达 | TianXiang | 2026-09-30 |
| 2 | rollup | 4.56.0 | 任意文件写/路径穿越 (high) | >=4.59.0 | 仅构建链, 不进产物 | TianXiang | 2026-09-30 |
| 3 | vite | 5.4.21 | dev server fs.deny 绕过 (high) 等 3 条 | >=6.4.3 (跨大版本) | 仅 dev server | TianXiang | 2026-10-31 |
| 4 | tailwind/babel 构建链传递依赖 (postcss/nanoid/picomatch/browserslist/@babel/core/esbuild 等) | 见 pnpm-lock | ReDoS/原型污染/文件读 等 17 条 | 均 patch/minor 可修 | 仅构建链/dev 依赖, 不进产物 | TianXiang | 2026-10-31 |

说明:

- 依赖升级由 dependabot (W2.5 同批接入, weekly, 三生态) 接管: patch/minor
  自动开 PR 合并即关闭; vite 5→6 / vitest 3→4 跨大版本需手动评估。
- 暴露面判定: 前端构建工具链 (vite/rollup/postcss/babel/tailwind) 只在本机
  dev/build 阶段执行, 产物是静态 bundle, 漏洞不影响线上用户; 真正运行时
  依赖里的已知漏洞当前只有 react-router-dom 一处。
- vitest 3.2.7 的 @vitest/mocker 路径穿越 (moderate) 仅测试环境, 随 W2.4
  引入的测试栈, 跟随 vitest 大版本升级处理。

### pip-audit 结果

2026-09-09 在 python:3.11-slim 容器(装 requirements-lock.txt 精确依赖)执行
`pip-audit -r requirements-lock.txt --no-deps`: **No known vulnerabilities found**
—— 后端 171 个锁定包在 OSV/PyPI 数据库中无已知漏洞, 暂无登记项。
前端见上表(dependabot 接管后随周更 PR 逐项关闭)。

## forecast_server 预测目标日按 weekday 计 (W2.6/B6 审计豁免, 2026-09-09)

forecast_server.py 独立部署(运行目录 forecast_lib/, 不含 src/), 其"未来 N 个
交易日"计数按 weekday<5 推算(两处: target_date 换算天数 + pred_dates 生成),
法定节假日会被当作交易日, 目标日期偏晚(如国庆前预测 3 个交易日, 实际应跨到
节后)。影响面: 仅预测展示的目标日期标注, 不影响 src/ 内任何评估/回测链路
(那部分已统一走 src/core/trading_calendar.py)。

修复方向: 给 forecast_lib 内联一份静态日历表或部署时附带日历 JSON,
随 src/ 表同步维护。Owner: TianXiang, 期限: 2026-10-31(与日历 2028 表
补录同一维护窗口处理)。
