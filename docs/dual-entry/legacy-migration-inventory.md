# Legacy 保留、适配与淘汰清单

本清单服务于 P0/P1 渐进迁移。`retire gate` 未满足前不得删除旧接口、schema 或规则。

| Legacy 资产 | 当前职责 | P1～P4 处理 | retire gate | 当前状态 |
|---|---|---|---|---|
| `schemas/itinerary.py::Itinerary` | Planner 与前端的行程契约 | 保留；增加与 `ItineraryRevision` 的双向 adapter | new frontend 切换、round-trip 测试和调用量审计通过 | implemented_legacy |
| `schemas/verification.py::VerificationReport` | 三态约束报告 | 保留只读；转换为 new Audit view，不进入 `audit_reports` 写路径 | Audit parity、历史回读和新 API 切换通过 | implemented_legacy |
| `verification_reports` 表 | legacy 报告持久化 | 只读兼容；不得扩列承载新 Audit 语义 | 历史报告可回读且新写入全部进入 `audit_reports` | implemented_legacy |
| `POST /api/optimize` | 批量 Planner 入口 | 保留；P2 后附 workspace/revision/report 引用 | 新初始规划入口稳定且调用量允许下线 | implemented_legacy |
| `POST /api/edit` | 客户端整份行程 patch | P1 改为 Revision Command Service adapter；非 demo 不信任客户端事实 | revision-aware API、冲突与幂等测试通过 | implemented_legacy |
| `services/planning_hash.py` | legacy 规划输入哈希 | 保留；new revision/report hash 使用独立 canonical 实现 | legacy 调用归零且证据迁移完成 | implemented_legacy |
| `constraints/verifier.py` | 规则执行与 VerificationReport | P2 变为 AuditEngine adapter | parity 数据集通过且主流程只有一套 finding | implemented_legacy |
| `constraints/rules/*` | 当前确定性约束 | 作为第一批权威规则，补 rule version/dependency/evidence | 不淘汰；迁入统一 registry | implemented_reusable |
| `planner/nodes/critic_v2.py` | Planner 内第二套检查 | 独有规则逐条迁移；parity 前保留 | Critic/Verifier parity 通过 | implemented_legacy |
| `planner/repair_controller.py` | 单轮定向修复 | P4 委托 `RepairService`；保留 adapter | Repair A/B、锁定保护和 postcheck 通过 | implemented_legacy |
| `agents/editor/fast_path.py` | 直接修改传入 JSON | P1 经 Command Service 写新 revision | 兼容 API 不再直接修改客户端行程 | implemented_legacy |
| localStorage 行程/报告 | 浏览器恢复与 stale 判断 | 降为缓存；服务端 current revision/report 为权威 | 跨浏览器和刷新恢复测试通过 | implemented_legacy |
| Yjs room/places/chat | 协同状态 | P1 只同步 revision/report 引用；P7 再扩成员意图 | 服务端恢复与冲突测试通过 | implemented_reusable |

状态词只描述当前代码事实，不代表目标领域已完成。任何淘汰必须新增 ADR，并包含调用量、兼容性、回滚和数据回读证据。

