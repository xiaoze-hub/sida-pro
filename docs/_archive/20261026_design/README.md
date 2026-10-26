# 归档：2026-10-26 从 docs 根层移下的开发设计/方案/审计文档

## 为什么在这里
用户要求「docs 文件夹下面不要把开发设计方案留在上面」。这些文件是**历史设计稿、开发计划、
审计报告、走查记录**，已落地或已过期，不再作为当前口径；留在根层会和
`PROJECT_MAP / PROJECT_STATUS / *_coverage / permission-matrix` 这类**仍然有效**的规范混在一起。

## 三类文件不要误删
- **`docs/_frozen/`** —— 冻结口径（如 `caliber_matrix.md`），代码与测试直接引用，禁止移动。
- **`docs/design/` / `docs/research/`** —— 被代码注释当作**契约源**引用
  （例：`src/web/api/copilot.py` 写「契约源: docs/research/…§4」；
  `src/core/wyckoff.py` 写「调参必须同步改 …本土三层滤网_设计… §3」）。位置是契约的一部分。
- 本目录内被代码引用的少数几份（如 `本土三层滤网_设计`、`开发计划_v3.0`、
  `innov-dev-plan.md`）：引用路径已同步改成 `docs/_archive/20261026_design/…`。

## 根层保留的（仍然是当前事实源）
`PROJECT_MAP` `PROJECT_STATUS` `KNOWN_ISSUES` `TIMEZONES` `permission-matrix`
`dependency_direction` `data-catalog-coverage` `data_source_triangulation`
`harness-data-map` `harness-tool-inventory` `tq-interface-coverage`
`decision-pioneer-spec` `STEP3_BOUNDARY` `UI规范` `skill-gateway-phase0`
`项目全景` `v0.3.0-release-notes`
