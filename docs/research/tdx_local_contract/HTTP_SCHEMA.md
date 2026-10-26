# 通达信本地 HTTP 请求体契约

- 来源: `/tmp/tqcenter.py`；方法数 **58**（含 `id`/`type`/`table_list` 等隐藏字段）

| 方法 | 传输 | 请求体字段 | 网关 |
|---|---|---|---|
| `cancel_order_stock` | GetOrderStr | `id`, `type`, `account_id`, `stock_code`, `order_id` | ⛔ 禁用 |
| `clear_sector` | ? | `id`, `type`, `block_code` | ✅ |
| `create_sector` | ? | `id`, `type`, `block_code`, `block_name` | ✅ |
| `delete_sector` | ? | `id`, `type`, `block_code` | ✅ |
| `download_file` | GetTdxDataStr | `id`, `type`, `stock_code`, `down_time`, `down_type` | ✅ |
| `exec_to_tdx` | ? | `id`, `type`, `url` | ⛔ 禁用 |
| `formula_get_all` | TdxFuncMain | `id`, `type`, `formula_type` | ✅ |
| `formula_get_data` | TdxFuncMain | `id`, `type` | ✅ |
| `formula_get_info` | TdxFuncMain | `id`, `type`, `formula_type`, `formula_code` | ✅ |
| `formula_process_mul` | TdxFuncMain | `id`, `type`, `formula_name`, `formula_arg`, `formula_type`, `xsflag`, `return_count`, `return_date`, `stock_list`, `stock_period`, `start_time`, `end_time`, `count`, `dividend_type` | ✅ |
| `formula_set_data` | TdxFuncMain | `id`, `type`, `stock_code`, `stock_period`, `stock_data`, `count`, `dividend_type` | ✅ |
| `formula_set_data_info` | TdxFuncMain | `id`, `type`, `stock_code`, `stock_period`, `start_time`, `end_time`, `count`, `dividend_type` | ✅ |
| `get_bkjy_value` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `start_time`, `end_time` | ✅ |
| `get_bkjy_value_by_date` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `year`, `mmdd` | ✅ |
| `get_divid_factors` | GetTdxDataStr | `id`, `type`, `stock_code`, `start_time`, `end_time` | ✅ |
| `get_exday_data` | GetTdxDataStr | `id`, `type`, `stock_code`, `count` | ✅ |
| `get_financial_data` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `start_time`, `end_time`, `report_type` | ✅ |
| `get_financial_data_by_date` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `year`, `mmdd` | ✅ |
| `get_gb_info` | GetTdxDataStr | `id`, `type`, `stock_code`, `date_list`, `count`, `gb_type` | ✅ |
| `get_gb_info_by_date` | GetTdxDataStr | `id`, `type`, `stock_code`, `start_date`, `end_date`, `gb_type` | ✅ |
| `get_gp_one_data` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list` | ✅ |
| `get_gpjy_value` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `start_time`, `end_time` | ✅ |
| `get_gpjy_value_by_date` | GetProDataInStr | `id`, `type`, `stock_list`, `table_list`, `year`, `mmdd` | ✅ |
| `get_ipo_info` | GetTdxDataStr | `id`, `type`, `ipo_type`, `ipo_date` | ✅ |
| `get_kzz_info` | GetTdxDataStr | `id`, `type`, `stock_code` | ✅ |
| `get_market_snapshot` | GetTdxDataStr | `id`, `type`, `stock_code` | ✅ |
| `get_match_stkinfo` | GetTdxDataStr | `id`, `type`, `key_word` | ✅ |
| `get_more_info` | GetTdxDataStr | `id`, `type`, `stock_code` | ✅ |
| `get_pricevol` | GetTdxDataStr | `id`, `type`, `stock_list` | ✅ |
| `get_relation` | GetTdxDataStr | `id`, `type`, `stock_code` | ✅ |
| `get_scjy_value` | GetProDataInStr | `id`, `type`, `code`, `table_list`, `start_time`, `end_time` | ✅ |
| `get_scjy_value_by_date` | GetProDataInStr | `id`, `type`, `code`, `table_list`, `year`, `mmdd` | ✅ |
| `get_sector_list` | GetTdxDataStr | `id`, `type`, `list_type` | ✅ |
| `get_stock_info` | GetTdxDataStr | `id`, `type`, `stock_code` | ✅ |
| `get_stock_list` | GetTdxDataStr | `id`, `type`, `market`, `list_type` | ✅ |
| `get_stock_list_in_sector` | GetTdxDataStr | `id`, `type`, `block_code`, `block_type`, `list_type` | ✅ |
| `get_trackzs_etf_info` | GetTdxDataStr | `id`, `type`, `zs_code` | ✅ |
| `get_trading_calendar` | GetTdxDataStr | `id`, `type`, `market`, `start_time`, `end_time`, `count` | ✅ |
| `get_trading_dates` | GetTdxDataStr | `id`, `type`, `market`, `start_time`, `end_time`, `count` | ✅ |
| `get_user_sector` | GetTdxDataStr | `id`, `type` | ✅ |
| `get_zdt_data` | GetTdxDataStr | `id`, `type`, `stock_list` | ✅ |
| `order_stock` | GetOrderStr | `id`, `type`, `account_id`, `stock_code`, `order_type`, `order_volume`, `price_type`, `price`, `notify` | ⛔ 禁用 |
| `query_stock_asset` | GetOrderStr | `id`, `type`, `account_id` | ⛔ 禁用 |
| `query_stock_orders` | GetOrderStr | `id`, `type`, `account_id`, `stock_code`, `cancelable_only` | ⛔ 禁用 |
| `query_stock_positions` | GetOrderStr | `id`, `type`, `account_id` | ⛔ 禁用 |
| `refresh_cache` | GetTdxDataStr | `id`, `type`, `market`, `force` | ✅ |
| `refresh_kline` | GetTdxDataStr | `id`, `type`, `stock_list`, `period` | ✅ |
| `rename_sector` | ? | `id`, `type`, `block_code`, `block_name` | ✅ |
| `send_bt_data` | ? | `id`, `type`, `stock_code`, `time_list`, `data_list`, `count` | ✅ |
| `send_file` | ? | `id`, `type`, `file_path` | ✅ |
| `send_message` | ? | `id`, `type`, `msg_str` | ✅ |
| `send_source` | ? | `id`, `type`, `py_code`, `handle_type` | ✅ |
| `send_user_block` | ? | `id`, `type`, `block_code`, `stock_list`, `show` | ✅ |
| `send_warn` | ? | `id`, `type`, `stock_list`, `time_list`, `price_list`, `close_list`, `volum_list`, `bs_flag_list`, `warn_type_list`, `reason_list`, `count` | ✅ |
| `stock_account` | GetOrderStr | `id`, `type`, `account`, `account_type` | ⛔ 禁用 |
| `subscribe_hq` | GetTdxDataStr | `id`, `type`, `stock_list`, `sub_type` | ✅ |
| `tdx_formula` | TdxFuncMain | `id`, `type`, `formula_type`, `formula_name`, `formula_arg`, `xsflag` | ✅ |
| `unsubscribe_hq` | GetTdxDataStr | `id`, `type`, `stock_list`, `sub_type` | ✅ |
