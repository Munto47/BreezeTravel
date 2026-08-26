# P0～P4 与 M1 门禁复核（2026-08-20）

本表按 Final 1.0 的完成定义逐项对照当前工作树。`技术通过` 只覆盖确定性代码、受控 PostgreSQL 或本地浏览器证据；真实样本指标没有数据时保持未通过。

| 阶段 | 门禁结论 | 当前证据 | 未通过项 |
|---|---|---|---|
| P0 基线 | 部分通过 | Final 1.0、ADR、范围和证据表述已冻结 | 10 份真实 AI 行程与人工排雷记录为 0 |
| P1 版本底座 | 技术通过 | append-only revision、canonical hash、If-Match、幂等、跨浏览器服务端恢复；PostgreSQL 并发一胜一 conflict、胜者重放不重复写入 | 无真人/公网声明 |
| P2 统一审计 | 技术通过 | 独立 Audit API、Evidence 四态、唯一 registry、Critic 全规则 parity 后退出主图、Provider 部分失败降级、PostgreSQL 报告回读 | 真实 Provider 可用性不在本地门禁内 |
| P3 文本导入 | 功能通过、指标未通过 | 原文/span/source sentence、失败草稿重试、Prompt Injection 降级、受控候选重搜、版本隔离、原子确认和 revision 1 | 真实 blind 为 0；字段 F1、自动匹配 precision、固定承诺 recall 无真人分母 |
| P4 风险与 Repair | 技术通过 | 三组风险 UI、Evidence 回读、Repair A/B、锁定保护、完整 postcheck、HIGH/UNKNOWN 非回归、apply/reject/幂等、最终 Tips artifact 绑定 report/revision | Repair 采纳率和真实 Tips 文案质量无真人数据 |
| M1-dev 代理校准 | 开发门禁通过 | 三个独立 GPT-5.6-sol `synthetic_proxy` 角色各覆盖 150 条，角色/模型/prompt/input/output hash 均通过；综合 precision=1.00、recall=1.00、关键一致率=0.92、Evidence 回读=1.00 | 不能写成真人结论，也不解除 P8 的真人/公网门禁 |
| P8 最终真人验收 | planned | 聚合器、schema、真人边界、唯一 source/organizer 反算和防计数膨胀已单测 | `BLOCKED_HUMAN_DATA`：0/30 份真人标注行程，0/15～20 名真实组织者；阻断公网发布，不阻断后续本地开发 |

## 继续/停止决定

- P5 三城路线骨架和 P6 完整拖拽工作台依赖 M1-dev 代理门禁；真实校准移动至 P8 的最终发布前验收。
- 现有 Planner、Yjs、RAG、Memory 和 MCP 仅保留兼容回归，不把相似旧功能计作 P5～P8 完成。
- 不调用 LLM-as-Judge，不生成或伪造 organizer hash、consent、human finding、Repair 采纳或拒绝原因。
- 当前可称为“行程排雷 MVP（本地代理验收）”；P5～P8 的本地实现仍需逐层验证。不得称为真人验证、生产可用或“受控三城 Beta”。
