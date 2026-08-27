# ADR-002：Final 基线后的迁移编号

- 状态：Partially superseded by ADR-012 / TC-VNEXT Program
- 日期：2026-08-20
- 关联基线：[BreezeTravel 双入口可验证行程产品与架构重构最终方案](../BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md)

> 当前只保留“已执行migration不重编号、后续只追加”的不变量。旧M1/P5和`014_route_templates.sql`计划不再有效；TC-VNEXT从当前实际末号027之后预定义028～033。

## 背景

Final 1.0 文档把 `011_route_templates.sql` 作为 P5 的计划迁移名。实际 P0～P4 实施时，`011_final_tips_artifacts.sql` 已进入本地 PostgreSQL 验证链路；随后移动端导入状态需要 `012_import_mobile_contract.sql`，创建型可靠重试与 `current_import_id` 需要 `013_idempotent_creation_commands.sql`。

已经执行过的迁移文件不能为了贴合计划文档而改名。改名会让已有数据库的迁移历史与仓库不一致，并增加重复建表或漏执行的风险。

## 决策

- 保留 `011_final_tips_artifacts.sql`、`012_import_mobile_contract.sql` 和 `013_idempotent_creation_commands.sql` 的现有编号与内容。
- Final 文档中的 `011_route_templates.sql` 只视为当时的计划名称，不再作为可执行文件名。
- M1 真人门禁通过后若启动 P5，路线模板迁移从下一个未使用编号 `014_route_templates.sql` 开始。
- 后续迁移只允许追加，不修改或重编号已经通过集成验证的历史迁移。

## 结果

数据库历史保持可追溯，P5 仍受 M1 真人门禁约束。本 ADR 只解决编号冲突，不代表路线模板已经开发或授权启动。
