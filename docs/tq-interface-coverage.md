# TQ（通达信客户端）接口覆盖盘点 —— 62 个接口逐项核对

- **基线**：`538ed7c`（VERSION `v0.13.13`）
- **盘点对象**：`packages/marketdata/src/marketdata/vendors/tq.py` 实现的全部 TQ 封装，以及 `src/`、`frontend/src/` 中的实际调用点
- **口径来源**：官方《TdxQuant接口说明文档》（231 页，59 个接口条目）+ 真网关逐一实测
- **覆盖范围**：审了 `src/`（全部 .py）、`packages/marketdata/src/`、`frontend/src/`、`tests/`；**没审**：`scripts/`、`docs/`、以及服务端 tqcenter 内部实现（无源码）
- **复核方式**：`grep -rn <接口名> src/ packages/ frontend/`（计数口径：去掉定义行）

结论：**18 已接 / 14 少量接 / 30 未接**。未接的 30 个里，真正值得接的只有 5 类（下表「建议接」），其余要么是交易/写操作（明确不接），要么是公式管线的中间件。

## 一、建议接（按价值排序）

| 接口 | 用途 | 为什么值得 |
|---|---|---|
| `get_*_by_date`（gpjy/scjy/bkjy/gb_info 四个） | 按**指定日期**取序列（现在只能整段拉再自己筛） | 现在拿历史要整段拉全量再筛，by_date 直接要某天 → 回补/对账更快、更省客户端压力 |
| `get_trackzs_etf_info` | 跟踪某指数的 ETF 列表（含 IOPV/规模） | ETF 页/指数页可直接用；免费源要么没有 IOPV 要么延迟 |
| `get_match_stkinfo` | 证券检索（代码 ↔ 名称/简称） | 代码标准化与名称补全，减少对第三方名称表的依赖 |
| `get_financial_data_by_date` | 指定日期专业财务数据 | 与已有 `get_financial_data` 配套，回补历史财务更精确 |
| `get_trading_dates` | 交易日列表 | ⚠️ **实测后改判：不需要接**。全仓交易日判定早已收口到 `src/core/trading_calendar.py`（静态表 2025-2028，含法定节假日/调休，未覆盖年份显式报错）。真正的问题是**新写的两个 TQ 调度器绕过了它**（按 `weekday()<5` 近似），已在 v0.13.14 修正 —— 结论：接 TQ 日历反而会引入第二个日历口径，不接。 |

## 二、明确**不接**（有理由，别重复调研）

| 接口 | 不接的原因 |
|---|---|
| `order_stock` | 真实下单 —— 生产绝不允许自动交易 |
| `cancel_order_stock` | 真实撤单, 同上 |
| `stock_account` | 需券商资金账户登录(实测未登录报 ErrorId=9) |
| `query_stock_asset` | 同上, 未登录拿不到; 将来若做持仓同步再议 |
| `query_stock_orders` | 同上 |
| `query_stock_positions` | 同上 |
| `create_sector` | 写用户客户端板块(改变用户环境), 非只读 |
| `rename_sector` | 同上 |
| `delete_sector` | 同上, 且删用户数据不可逆 |
| `clear_sector` | 同上, 不可逆 |
| `send_user_block` | 写用户自选股(客户端侧有副作用) |
| `send_bt_data` | 回测数据回传客户端, 属策略开发链路 |
| `send_file` | 把文件推给客户端界面, 非数据能力 |
| `print_to_tdx` | 调试打印 |
| `send_warn` | 把预警推到客户端(客户端已有), 非取数 |
| `send_message` | 同上是输出向 |
| `exec_to_tdx` | 通用客户端功能调用(可点界面按钮), 副作用不明, 不接 |

其余未接项属公式管线中间件：`formula_set_data` / `formula_set_data_info` / `formula_get_data` / `formula_format_data` / `formula_get_info` （要先生成盘后数据文件；现有 `formula_process_mul_*` 已覆盖批量选股/指标场景）、`get_subscribe_hq_stock_list`（订阅需长驻回调，网关是薄 RPC **不支持回调**，见下节）。

## 三、一个关键约束：网关是薄 RPC，**不支持推送**

实测（2026-09-25）：网关 `172.27.16.1:17709` 是 JSON-RPC 桥，`method` 直接是 tqcenter 函数名；
`initialize`/`tools/list` 均返回 `MCP不支持该tqcenter方法名`。因此
**`subscribe_hq`（订阅行情 + 回调）在当前架构下无法使用** —— 回调跨不过 HTTP。
=> 「最快」的现实路径是**批量 RPC**（`get_pricevol` 500只/片 0.07s、`formula_process_mul_zb/xg` 500只/批），而不是推送订阅。

## 四、已接清单（18 个，供对照）

`exp`、`formula_process_mul_xg`、`get_divid_factors`、`get_gb_info`、`get_market_data`、`get_market_snapshot`、`get_more_info`、`get_pricevol`、`get_relation`、`get_scjy_value`、`get_stock_info`、`get_stock_list`、`get_stock_list_in_sector`、`initialize`、`send_message`、`subscribe_hq`、`xg`、`zb`

## 五、少量接清单（14 个，多为有壳无人调或单点使用）

| 接口 | 用途 | 调用点 |
|---|---|---|
| `download_file` | 下载特定数据文件 | 2 |
| `formula_get_all` | 获取指定种类的公式列表 | 2 |
| `formula_zb` |  | 2 |
| `get_bkjy_value` | 获取板块交易数据 | 2 |
| `get_financial_data` | 获取专业财务数据 | 2 |
| `get_gp_one_data` | 获取股票的单个数据(非序列) | 2 |
| `get_gpjy_value` | 获取股票交易数据 | 3 |
| `get_ipo_info` | 获取新股申购信息 | 3 |
| `get_kzz_info` | 可转债信息 | 2 |
| `get_sector_list` | 获取A股板块代码列表 | 3 |
| `refresh_cache` | 刷新行情缓存 | 2 |
| `refresh_kline` | 缓存历史K线 | 3 |
| `send_warn` | 发送预警信号到客户端 | 2 |
| `unsubscribe_hq` | 取消订阅更新 | 2 |
