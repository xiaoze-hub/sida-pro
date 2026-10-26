# 通达信本地 API 契约（静态提取，非逆向）

- 来源: `/tmp/tqcenter.py`  (4149 行)
- 公开方法: **81**

| 分组 | 方法数 | 方法 |
|---|---|---|
| 其他 | 15 | cancel_order_stock, check_stock_code_format, convert_or_validate, exec_to_tdx, filter_dict_by_fields, get_bt_struct_str, get_warn_struct_str, is_callback_func, market_str_to_int_market, order_stock, price_df, query_stock_asset, query_stock_orders, query_stock_positions, stock_account |
| 公式 | 15 | formula_exp, formula_format_data, formula_get_all, formula_get_data, formula_get_info, formula_process_mul, formula_process_mul_exp, formula_process_mul_xg, formula_process_mul_zb, formula_set_data, formula_set_data_info, formula_xg, formula_zb, process_tdx_formula_arg, tdx_formula |
| 订阅/推送 | 11 | get_subscribe_hq_stock_list, print_to_tdx, send_bt_data, send_file, send_message, send_source, send_user_block, send_warn, subscribe_hq, subscribe_quote, unsubscribe_hq |
| F10/基本面 | 10 | get_divid_factors, get_financial_data, get_financial_data_by_date, get_gb_info, get_gb_info_by_date, get_ipo_info, get_kzz_info, get_relation, get_stock_info, get_trackzs_etf_info |
| 工具 | 8 | close, download_file, get_match_stkinfo, get_python_version_number, get_stock_list, get_trading_calendar, get_trading_dates, initialize |
| 行情 | 8 | get_exday_data, get_market_data, get_market_snapshot, get_more_info, get_pricevol, get_zdt_data, refresh_cache, refresh_kline |
| 板块 | 7 | clear_sector, create_sector, delete_sector, get_sector_list, get_stock_list_in_sector, get_user_sector, rename_sector |
| 专业序列 | 7 | get_bkjy_value, get_bkjy_value_by_date, get_gp_one_data, get_gpjy_value, get_gpjy_value_by_date, get_scjy_value, get_scjy_value_by_date |

## 网关必须默认禁用的方法

| 方法 | 风险 |
|---|---|
| `cancel_order_stock` | 撤单 |
| `exec_to_tdx` | 客户端内执行 URL(可被滥用) |
| `order_stock` | 下单 |
| `query_stock_asset` | 资金查询 |
| `query_stock_orders` | 委托查询 |
| `query_stock_positions` | 持仓查询 |
| `stock_account` | 资金账户登录 |

## 实测结论（2026-10-19）

| 结论 | 证据 |
|---|---|
| `PYPlugins/` 是**明文 Python 源码**，不需要逆向 | `user/tqcenter.py` 4,149 行 / 81 个公开方法 / 零 `**kwargs`；`user/tqserver.py` 958 行 |
| `ErrorId=10` 是**请求体缺字段**，不是"要下盘后专业数据包" | `get_financial_data` 裸调报 `json has no table_list`；按 schema 补 `table_list` 后 `ErrorId=0`（142ms） |
| 17709 是**客户端自带的单线程 HTTP 服务** | 连打 30 次快请求后 `TdxW.exe` 整个进程消失、17709 全断；重启客户端后恢复 |
| 原生层导出 9 个符号 | `InitConnect/CloseConnect/GetTdxDataStr/GetProDataInStr/GetOrderStr/TdxFuncMain/Register_DataTransferFunc/SetMsgToMain/SubscribeGPData` |
| 交易接口和行情接口在同一张方法表里 | `order_stock`/`cancel_order_stock`/`exec_to_tdx` → 网关已硬禁用 |

请求体里的隐藏三件套（Python 层帮你填，裸调必须自己给）：

- `id`  会话 run_id（整数即可）
- `type` 方法判别码：快照 3 / 股票列表 0 / 财务 1 / 交易数据 3 / 公式 4 / 板块 23 / 涨跌停 26 …
- `table_list` 财务与 GP/SC 序列的字段清单（`field_list` 的映射）

## 产物

- `contract.json` — 81 个方法的参数/默认值/风险标记（`scripts/extract_tdx_contract.py`）
- `http_schema.json` — 58 个方法的真实请求体（`scripts/extract_tdx_http_schema.py`），网关直接吃这个
- `surface_report.json` — 探针实测状态（**慢速跑**，默认 1.2s 间隔）
