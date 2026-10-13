# TdxW TQ 网关看门狗（Windows 小主机运维件）

`tdxw_watchdog.ps1` —— 部署到 Windows 小主机（`TIANXIANG@100.91.30.35`），
用**真 JSON-RPC** 探活通达信 `TdxW.exe` 自带的 TQ HTTP 服务（`127.0.0.1:17709`），
假活/无响应时自动杀进程重拉。

## 为什么要它（别再只探端口）

- TdxW **假死**（zombie）时：进程还在、内存掉到 14–28MB（正常 200MB+）、主窗口消失，
  但 **17709 端口仍 `LISTENING`、JSON-RPC 空响应** —— 只 `Test-NetConnection` 端口
  永远判它「活」，看门狗形同虚设。
- 因此本脚本的探活**必须真发一笔 `get_market_snapshot` 并校验返回里有非零现价
  （`Now`）**才算活。这是铁律（见 `windows-mini-host-ops` / `n150-tq-gateway` 技能）。

## 行为

1. 探活：POST `{"id":1,"method":"get_market_snapshot","params":{"stock_code":"002361.SZ"}}`，
   `result.ErrorId==0` 且 `result.Now>0` 才算活。
2. **连续 2 轮失败才动作**（5min 一轮 ⇒ ~10min 观察窗）：开机后「记住密码」自动登录
   有几分钟窗口期，此时内存 200MB+、无窗口、暂无报价——**不能误杀**。
3. 满足阈值后**仍不杀**若 TdxW 持有主窗口（登录屏判别）。
4. 通过上述闸门 → `taskkill /F /IM TdxW.exe /T`（+`tdxcef.exe`）→
   `schtasks /run /tn TdxAutoStart` 重拉 → 等 90s → **复探 JSON-RPC**。
5. 全程写日志 `C:\Users\tianxiang\tqwatch.log`；连续失败计数存
   `C:\Users\tianxiang\tqwatch_state.txt`（每轮读改，阈值后清零防杀循环）。

## 安装（替换现有只探端口的 `TqKeepAlive`）

> 脚本**纯 ASCII**（PS 5.1 按 GBK 解析 .ps1，中文会把语法改坏）。
> 经 SSH 用管道下发/执行脚本时，**内层命令一律 `</dev/null`** 防吞 stdin
> （同一个 stdin 被内部命令消费会让脚本半路静默中断）。

1. 下发脚本到小主机家目录（**必须走 scp 落文件，不要内联 `-Command`**）：

   ```bash
   sshpass -p '<pw>' scp -o StrictHostKeyChecking=no \
     deploy/windows/tdxw_watchdog.ps1 TIANXIANG@100.91.30.35:C:/Users/tianxiang/tdxw_watchdog.ps1
   ```

2. 自测（dry-run，只探活/决策/写日志，**不杀不拉**）：

   ```bash
   sshpass -p '<pw>' ssh TIANXIANG@100.91.30.35 \
     "powershell -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\tianxiang\\tdxw_watchdog.ps1 -DryRun" </dev/null | tr -d '\r'
   ```

   看 `tqwatch.log` 末行：`OK: ... returns price` = 链路正常；`FAIL: ...` = 探测失败。

3. 建 5min 周期计划任务并**删掉旧的只探端口任务**：

   ```bat
   schtasks /delete /tn "TqKeepAlive" /f
   schtasks /create /tn "TqRpcWatchdog" ^
     /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\tianxiang\tdxw_watchdog.ps1" ^
     /sc minute /mo 5 /f
   schtasks /run /tn "TqRpcWatchdog"
   ```

   > 用 `schtasks` 也可用 `/xml` 精确控制；关键点：`/sc minute /mo 5`（不要被默认
   > `ExecutionTimeLimit` 影响——本任务是**周期短任务**，不是常驻，无需 `PT0S`）。

## 与现有自启体系的关系

- `TdxAutoStart`（onlogon 拉 TdxW）、`TQ-frpc`、`RustDeskKeep`、`ZCodeAutoStart` 保持不变；
  本看门狗**只**替换「只探 17709 端口」的 `TqKeepAlive` 逻辑。
- ⚠️ 看门狗**绝不碰 portproxy / 17709 转发**——17709 是 TdxW 的端口，portproxy 抢占
  它是 TQ 反复断链的总根源（见技能「坑 8」）。本脚本只杀进程重拉。
- 重拉后若仍无报价：脚本记 `STILL-DOWN` 并**每 5min 重新计数**（不会杀循环）。
  人工升级路径：RustDesk 登录客户端，或 `rebootfix.bat` 整机重启兜底。

## 手工验证链（云侧/小主机侧）

```bash
# 小主机本机打 TQ：ErrorId 0 + 有 Now = 通
curl -s --max-time 12 -X POST http://127.0.0.1:17709/ -H 'Content-Type: application/json' \
  -d '{"id":1,"method":"get_market_snapshot","params":{"stock_code":"002361.SZ"}}'
```

## 文件

- `tdxw_watchdog.ps1` — 看门狗本体（纯 ASCII，`-DryRun` 自测）。
- 自动化回归：`tests/test_tdxw_watchdog_script.py`（校验脚本纯 ASCII + 真 JSON-RPC
  探活/阈值/杀拉/日志等关键标记未回退）。
