# 全代码审计方案 · 2026-09-15

> 基线: main @ 50b44f2 (v0.7.1)
> 范围: src/core (47K行) + src/web/api (24K行) + src/agents + src/collectors + frontend/src
> 方法: 多智能体并行, 每个维度独立审计, 最后汇总

## 审计维度 (7 个并行任务)

### A. 安全审计 (P0)
- SQL 注入 (raw SQL, text(), format拼接)
- 命令注入 (os.system, subprocess, eval, exec)
- SSRF (用户可控 URL → httpx/requests)
- 路径穿越 (用户可控文件路径)
- 密钥泄露 (硬编码 token/key/password, 日志打印)
- JWT 安全 (密钥强度, 过期, 算法)
- 权限绕过 (缺少 enforce_perm 的敏感端点)
- CORS/CSRF 配置

### B. 数据口径审计 (P0)
- 单位一致性 (金额=元, 成交量=股)
- 方向位编码 (B/S/M, 主动/被动)
- 缺失数据处理 (None vs 0 vs 编造)
- 缓存一致性 (L1/L2 缓存失效策略)
- 时区处理 (Asia/Shanghai vs UTC)

### C. 错误处理与韧性 (P1)
- 裸 except (吞异常)
- 超时缺失 (外部调用无 timeout)
- 重试缺失 (关键调用无重试)
- 降级链完整性 (数据源 fallback)
- 资源泄漏 (未关闭的连接/文件)

### D. 性能审计 (P1)
- N+1 查询
- 阻塞调用在 async 上下文
- 缓存命中率 (热点数据)
- 大数据量处理 (全表扫描)
- 前端 bundle 大小

### E. 代码质量 (P2)
- 死代码 (未使用的函数/导入)
- 重复代码
- 超长函数 (>100行)
- 循环依赖
- 类型注解缺失

### F. 前端审计 (P1)
- XSS (dangerouslySetInnerHTML)
- 敏感数据暴露 (localStorage token)
- API 错误处理
- 内存泄漏 (useEffect cleanup)
- 权限守卫完整性

### G. 依赖与配置 (P2)
- 过期依赖
- 已知漏洞
- Docker 安全 (非root, 镜像瘦身)
- 环境变量缺失

## 执行方式

7 个 explore 子代理并行, 每个负责一个维度:
1. 扫描代码, 找出问题
2. 按严重程度分级 (P0/P1/P2)
3. 给出具体文件:行号 + 修复建议

最后主代理汇总成审计报告。
