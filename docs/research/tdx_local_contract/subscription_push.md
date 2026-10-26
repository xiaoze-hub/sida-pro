# 通达信「订阅推送」（Register_DataTransferFunc / SubscribeGPData）可接入性裁决

- **基线**：`git -C /home/ubuntu/sida-pro rev-parse --short HEAD` → `53a5f8b`（分支 `main`；本文件未提交）
- **盘点对象完整路径**：`C:\new_tdx64\PYPlugins\user\{tqcenter.py(4149行,md5 7b3a3af341458cfd1dcdb7744a58306b), tqserver.py(958行,md5 e6ac1fb8ad37c357c819cd6565d2de31), tdxdata_test.py(549行), gw_keep.py(7行)}`、`C:\new_tdx64\PYPlugins\sys\tqcenter.py`（与 user 版同 md5）、原生层 `C:\new_tdx64\PYPlugins\{TPythClient.dll,TPyth.dll,tdxrpcx64.dll,TdxAiData.dll}`、`C:\new_tdx64\tdxw.exe`、客户端已有独立探针 `C:\TdxQ\tq_history_dump.py` 与 `C:\TdxQ\mi1\tq9\tq9_intraday_probe.py`
- **覆盖范围**：**读了** `user/tqcenter.py` 全文（重点 `subscribe_quote`/`subscribe_hq`/`unsubscribe_hq`/`_data_callback_transfer`/`_auto_initialize`/`is_callback_func`）、`user/tqserver.py` 全文（重点 `_NativeTdxAiData.subscribe`）、`tdxdata_test.py` 订阅示例段、`gw_keep.py` 全文；DLL 导出表/导入表/字符串表；`py_strategy.cfg`；Windows 事件日志、计划任务、监听端口。**没读** DLL **反汇编**（仅 strings + 导出/导入表静态提取）、`TdxAiData.dll` 的 `GetStockInfo_Sub` 实现、`TdxW.exe` 策略界面、`GNPlugins/`+`QHPlugins/`+`SEPlugins/`、原生 MCP 分发器完整参数绑定逻辑。**未做**：未在生产跑任何订阅调用、未重启 TdxW.exe、未改任何客户端文件/配置。

## 一、结论表

| 通道 | 裁决 | 关键证据 |
|---|---|---|
| **A. 17709 HTTP 拿推送** | ❌ **不能接** | 17709 由原生 `TPyth.dll` 的 `HandleMcpJsonRpc` 服务（无 python 进程也在跑），**不认识 `callback`**、不认识 `subscribe_quote`；`run_id` 是进程内会话 id，两边不是一个。证据 2 |
| **B. 进程内 DLL 调用** | ⚠️ **只能进程内接** | `Register_DataTransferFunc`/`SubscribeGPData` 是 `TPythClient.dll` **导出符号**，插件层用 ctypes 调；回调是**进程内 C 函数指针**。证据 1、3、4 |
| **C. `TdxAiData.dll` 独立订阅** | ⚠️ 理论可行，本机不可用 | `tqserver.py` 已封好真异步订阅，但 `TdxAiData.ini` 的 `token=` **为空**、无 `libTdxAiData.so`。证据 5 |
| **D. 客户端落文件推送** | ❌ **不存在** | `Outside_%s.ini` 格式串在，但 `PYPlugins\data\out\` **为空**、无实时写入。证据 6 |
| **E. UDP 推送** | ❌ **不存在** | TdxW 进程**零 UDP 端点**。证据 7 |

> **裁决一句话**：这两个符号是**原生 DLL 导出给插件层用的**；插件层已封 Python API（`tq.subscribe_hq`/`tq.subscribe_quote`），**回调是进程内 C 函数指针**。**经 17709 拿不到推送**；要接必须**另起进程加载 `TPythClient.dll`**，或**在客户端进程内挂策略插件**。

## 二、代码行号证据

**证据 1 — 原生导出，不是 Python API**
```
$ objdump -p PYPlugins/TPythClient.dll | sed -n '/Ordinal\/Name Pointer/,/^$/p'
	[  18] GetTdxDataStr      [  22] InitConnect
	[  25] Register_DataTransferFunc
	[  28] SubscribeGPData    [  29] SubscribeHQDUpdate   <-- 原生第 5 个订阅符号，Python 层完全没用
```
`user/tqcenter.py:24-34` 只是 ctypes 胶水：
```python
24| global_dll_path = Path(__file__).resolve().parents[1] / 'TPythClient.dll'
25| dll = ctypes.CDLL(str(global_dll_path))
34| dll.Register_DataTransferFunc.restype = None          # 注册外套回调函数
```

**证据 2 — 17709 是原生服务，`callback` 到不了原生层**
```
$ strings -a TPyth.dll | grep -cx <method>
  subscribe_hq -> 1     unsubscribe_hq -> 1
  subscribe_quote -> 0        <-- Python 独有，原生白名单没有
  get_subscribe_hq_stock_list -> 0
$ strings -a TPyth.dll | grep -c "allback"    # 'callback' 子串
  TPyth.dll = 0    TPythClient.dll = 0
$ tasklist | grep -ic python    # 0（但 17709 仍正常返回行情 => 走原生路径，tqcenter.py 的回调从未注册）
$ curl -s -X POST http://127.0.0.1:17709/ -d '{"id":1,"method":"get_subscribe_hq_stock_list","params":{}}'
{"error":{"code":-32601,"message":"MCP不支持该tqcenter方法名"},"id":1}   # 非白名单被原生直接拒
$ strings -a TPyth.dll | sed -n '6553,6558p'   # 原生自己造订阅体
type / key_word / sub_type / :stock_list
```

**证据 3 — 回调形态：进程内 C 函数指针，同步回调，非消息队列**　`user/tqcenter.py:2515-2528`：
```python
2516|            CALLBACK_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
2517|            cls.data_transfer = CALLBACK_FUNC_TYPE(cls._data_callback_transfer)
2518|            dll.Register_DataTransferFunc(cls._get_run_id(), cls.data_transfer)
2527|            ptr = dll.SubscribeGPData(cls._get_run_id(), codestr, startimestr, endtimestr, periodstr,
2528|            dividend_type_int, count, timeout_ms)
```
`user/tqcenter.py:1073-1089`（按 `Code` 路由）：`codes = data_json['Code']` → `callback = callback_map.get(codes)` → `return callback(data_str)`。
签名 `void(*)(char*)`，**同步**在 DLL 线程内执行；数据 `{"Code":"XXXXXX.XX","ErrorId":"0"}`（`tdxdata_test.py:341-352` 官方示例注释）；上限 100 只（`tqcenter.py:2564-2567` → `raise ValueError("订阅数大于100")`）；按 `run_id` 注册回调表（`tqcenter.py:625` `data_callback_func = defaultdict(dict)`）⇒ **必须在进程内**。

**证据 4 — `run_id` 只在进程内有效**　`user/tqcenter.py:724-741`：
```python
724|                ptr = dll.InitConnect(cls.file_name, dll_path_str, cls.run_mode, get_python_version_number(), cls._reInitialized)
732|                        cls.run_id = int(ptr_json.get('run_id', '-1'))
741|                    raise RuntimeError("TQ数据接口初始化失败或已有同名策略运行")
```
`TPythClient.dll` 走 `tdxrpcx64.dll` 的 **TCP RPC**（非管道）：`objdump -p` → `DLL Name: tdxRpcx64.dll`；strings → `initRpcClient / updateRpcClientAddrPort / rpcClientConnect`。⇒ **另起进程加载 `TPythClient.dll` 技术上可行**（自己 `InitConnect` 拿真 run_id），但回调仍只落在那个进程内。

**证据 5 — `tqserver.py` 已封好真异步订阅，但本机缺 Key**　`user/tqserver.py:115-118` + `295-306`：
```python
115|        dll.GetStockInfo_Sub.argtypes = [c_char_p, AsyncReceiveDataCallBack]
295|    def subscribe(self, codes: str, callback: Callable[[str, int, int], int]):
306|        self._check_result(self._dll.GetStockInfo_Sub(self._encode(codes), self._callback))
```
但 `TdxAiData.ini` → `token=` **为空**；`find /mnt/c -maxdepth 6 -iname "libTdxAiData*"` → **空**（只有 Windows `.dll`）；`aihs.tdx.com.cn:7709` 从 WSL **可达**（实测 REACHABLE）。

**证据 6/7 — 无落文件推送、无 UDP**
```
$ strings -a tdxw.exe | grep -E "data..out"      # 方向是「外部→客户端画图」，非「客户端→外部」行情推送
%sPYPlugins\data\out\Outside_%s.ini
%sPYPlugins\data\out\Outside_%s_%d#%s.dat
$ ls -la PYPlugins/data/out/    ->  total 0   <-- 空，无实时写入
$ Get-NetTCPConnection -State Listen | Where OwningProcess -eq <TdxW pid>
127.0.0.1:17709   0.0.0.0:14574   0.0.0.0:13572    # 后两个是 tdxrpcx64 内部 RPC
$ Get-NetUDPEndpoint | Where OwningProcess -eq <TdxW pid>   ->  (空，零 UDP 端点)
```

**为什么 17709 拿不到推送**：① `TPyth.dll` 只做「HTTP JSON → 方法名 → 原生调用 → JSON 响应」单向转发，`event-stream`/`chunked`/`Upgrade:`/`ws://` 在字符串表 **全 0 命中**；② 原生层**不认识 `callback`**，订阅是服务端行为，数据必须回到注册回调的进程；③ 17709 转发器用固定 `id: 1` 伪造 run_id（`tdx_gateway/contract.py` `run_id: int = 1`；`http_schema.json` 的 `"id": "$run_id"`），真实 run_id 由插件进程 `InitConnect` 分配 ⇒ **两个不同的回调注册表**；④ 所以 17709 上的 `subscribe_hq` 只会「订阅成功」、不会有数据回来。与仓库既有结论一致（`docs/tq-interface-coverage.md:54`「回调跨不过 HTTP」），本轮从**原生层**补上证据。

## 三、替代路径

| 方案 | 可行性 | 说明 |
|---|---|---|
| **另起常驻进程加载 `TPythClient.dll`** | ✅ 技术可行 | `InitConnect`→`Register_DataTransferFunc`→`SubscribeGPData`→回调在本进程。Windows Python 3.11 **本机已有**（numpy 2.4.6/pandas 3.0.5 齐）。不碰 TdxW.exe 本体，但抢同一行情会话。 |
| **客户端内挂策略插件** | ✅ 官方形态 | `py_strategy.cfg` 已注册常驻策略 `gw_keep`（`AutoRun1=1`）；`TPY_StartClientSrv` / `PYPlugins\user\` / `PYPlugins\sys\`。缺点：改生产客户端目录，插件崩溃会带崩客户端。 |
| **继续轮询（现状）** | ✅ 零风险 | 30s 延迟；`get_pricevol` 500 只/片 0.07s、`formula_process_mul_zb/xg` 500 只/批，批量轮询够用。**当前推荐路径。** |

## 四、最小验证方案（人工执行，脚本不自动跑）

**方案 1（推荐先做）· 独立进程订阅探针**
- **在哪个进程跑 + 权限**：另起的 **Windows Python 3.11 进程**（不是 TdxW.exe 内部），用 `C:\Program Files\Python311\python.exe` 跑 `scripts/probe_tdx_push.py`。普通用户即可（只 ctypes 加载 DLL + 读行情）；**不要管理员、不要交易账户、不碰 `GetOrderStr` 系列。**
- **怎么验证收到推送**：`InitConnect` 拿 `run_id` → `Register_DataTransferFunc(run_id, CFUNCTYPE(None,c_char_p)(cb))` → `SubscribeGPData(run_id,"600111.SH","","","1d",0,0,5000)` → 回调每收到一次打印 `{"Code":...,"ErrorId":"0"}` 并计数。**判定：60~90 秒内 ≥1 条 ⇒ 链打通；0 条 ⇒ 订阅未生效（看 ErrorId）；`run_id < 0` ⇒ 客户端已有同名策略，立即放弃不重试。**
- **风险（对 TdxW.exe 稳定性）**：真实风险是**抢会话**（同名策略已跑时 `run_id<0`，必须放弃）；1 次低频订阅 ≠ 30 次快轮询那种打崩法；探针**必须 90 秒自动 `CloseConnect` 退出**，不留常驻僵尸。脚本 `scripts/probe_tdx_push.py` 已过 py_compile（只读、只 stdout、不改客户端、单实例自退）。

**方案 2 · 客户端内策略插件**：`PYPlugins\user\push_probe.py` + `py_strategy.cfg` 加一条，`tq.subscribe_hq(..., callback=cb)`，cb 里把 JSON POST 回我们的内部端点。**风险最高**（改生产客户端目录，策略崩溃会带崩客户端），**除非方案 1 验证通过且收益明确，否则不要在生产做**。**方案 3 · TdxAiData 订阅**：先申请数据服务 Key 写入 `TdxAiData.ini` 的 `token=`，再用 `tqserver.py` 的 `tqs.subscribe(...)` 走 `aihs.tdx.com.cn:7709`（独立通道，不经 TdxW.exe，对客户端零冲击，跨到 Linux 容器还需 `libTdxAiData.so`）。

## 五、⚠️ 本轮实测中的一起生产事故（必须记录）

**12:04:25 `TdxW.exe` 访问冲突崩溃并被自动拉起**（pid `13016`→`9868`，新进程 start `2026/10/22 12:05:07`）。Windows 事件日志 Application Error (Id=1000)：应用程序 `tdxw.exe 1.0.0.1`；故障模块 `TPyth.dll 1.0.0.1 (TPyth.dll_unloaded)`；异常代码 `0xc0000005`（访问冲突）；异常偏移 `0x79bc6`。

**归因（诚实结论）**：**无法证明是本轮探针直接造成的**。① 我本轮对 17709 的调用**全部只读**（`get_market_snapshot`/`get_more_info`/`initialize`/`get_subscribe_hq_stock_list`），**从未调过任何订阅接口**；崩溃时刻我正处在一条 `powershell` **静态取证**命令窗口内，那些命令只读文件/事件日志、不碰 17709。② **同一签名在本次会话前反复出现**：`10-16 16:14:50`/`10-16 16:21:37`（ntdll 堆损坏 0xc0000374）、`10-19 08:55:01`、`10-19 18:02:42`、`10-19 22:38:21`（TPyth.dll 0xc0000005）、`10-20 08:55:05`（TPyth.dll 0xc0000005）。③ 崩溃后 `TqKeepAlive` 在 12:03:02/12:08:02 均记 `PROBE_OK liveness=TRUE`，看门狗日志 12:03–12:08 **无任何 ACTION 行** ⇒ **不是被看门狗杀的**。④ 崩溃前后 `panwatch→tdx-gateway→17709` 请求量一直 **~4000 次/分钟**（12:00/12:03 尖峰 14672/6458），**该负载在我介入前就存在**，我本轮只加了**个位数**只读请求。⑤ **结论**：更像**既有高负载下的 TPyth.dll 内部竞态**（客户端老毛病），与本轮调研**无直接因果**；但**不能 100% 排除**我的调用参与了触发。
**当前状态（已复核）**：`TdxW.exe` pid `9868` 存活、监听 `127.0.0.1:17709`、只读 `get_market_snapshot` 返回 `{"ErrorId":"0","Now":...}`；`tdx-gateway`/`panwatch` 均 healthy。**服务已恢复，无遗留影响。**
**给后续人的硬约束**：在 TdxW.exe 上实测**任何新方法**前先查清 panwatch 的 4k/min 负载来源；**一次只打一个请求、间隔 ≥5 秒**。

## 六、给决策者

1. **现状（17709 轮询）是唯一在生产安全的路径**，30s 延迟可接受，批量轮询实测够快。
2. **要推送就得离开 HTTP**：客户端内策略插件（官方形态，改生产目录）**或**另起进程加载 `TPythClient.dll`（不动 TdxW.exe，抢会话）。两条路都需人工、灰度、带回滚。
3. **在量化收益（30s→<1s 能省多少）之前不建议投生产改造**；先只跑方案 1 的 90 秒只读探针，拿到「回调到底有没有数据」的答案再决策。
