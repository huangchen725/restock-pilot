"""20 product scenarios. Nodes may be shared, but are collected/counted only once."""
def entry(label, *tests):
    return {'label':label,'tests':list(tests)}

SCENARIOS = {
 1:entry('上传到导出', 'tests/test_m4_workbench.py::test_offline_workbench_full_flow'),
 2:entry('预算减少', 'tests/test_replenishment_v2.py::test_hand_calculated_partial_purchase_and_shortfall', 'tests/test_m1_api.py::test_full_workbench_api_flow_diff_approval_execution_exports'),
 3:entry('优先类别', 'tests/test_fixed_scenarios.py::test_scenario_03_priority_categories', 'tests/test_replenishment_v2.py::test_priority_allocation_is_global_and_order_invariant'),
 4:entry('排除 SKU', 'tests/test_replenishment_v2.py::test_exclusions_and_zero_demand'),
 5:entry('缺失日期', 'tests/test_data_contract.py::test_missing_days_require_explicit_zero_confirmation'),
 6:entry('旧批准失效', 'tests/test_m1_api.py::test_full_workbench_api_flow_diff_approval_execution_exports'),
 7:entry('MOQ', 'tests/test_fixed_scenarios.py::test_scenario_07_moq_rounding','tests/test_replenishment_v2.py::test_budget_moq_and_pack_boundaries'),
 8:entry('整包取整', 'tests/test_rules.py::test_purchase_qty_moq_and_pack'),
 9:entry('有效在途', 'tests/test_replenishment_v2.py::test_inbound_window_boundary'),
10:entry('过晚在途', 'tests/test_replenishment_v2.py::test_late_inbound_is_not_deducted'),
11:entry('预算不足', 'tests/test_fixed_scenarios.py::test_scenario_11_budget_insufficient'),
12:entry('空数据', 'tests/test_data_contract.py::test_empty_or_malformed_csv', 'tests/test_m0_api.py::test_data_errors_stop_before_model_or_plan'),
13:entry('单位口径显式确认', 'tests/test_data_contract.py::test_no_implicit_business_confirmation'),
14:entry('价格缺失', 'tests/test_fixed_scenarios.py::test_scenario_14_price_missing'),
15:entry('重复 SKU', 'tests/test_data_contract.py::test_duplicates_dates_and_cross_table_mismatches'),
16:entry('非法数值', 'tests/test_data_contract.py::test_invalid_sales_values', 'tests/test_data_contract.py::test_invalid_stock_is_reported', 'tests/test_data_contract.py::test_supplier_numeric_validation'),
17:entry('部分成功', 'tests/test_m3_workbench.py::test_workbench_partial_reconcile_recover_refresh_export'),
18:entry('结果未知', 'tests/test_m3_live.py::test_live_unknown_revise_cannot_create_duplicate_purchase'),
19:entry('受控恢复', 'tests/test_m3_workbench.py::test_workbench_partial_reconcile_recover_refresh_export', 'tests/test_fault_injection.py::test_recover_only_not_submitted_keeps_created_no_duplicates'),
20:entry('故障恢复', 'tests/test_m3_workbench.py::test_real_api_process_restart_never_reissues_pending_submission', 'tests/test_fault_injection.py::test_f5_submit_hang_times_out_then_recovers'),
}
