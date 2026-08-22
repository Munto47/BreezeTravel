# BreezeTravel 仓库开发约束

## 当前唯一产品目标

BreezeTravel 下一阶段只建设「行程查」（内部技术名称：行程核验）：帮助 2～5 人核验北京、上海或杭州的 2～5 天单城市行程，并对地点、时间、交通、住宿、偏好、强度、天气和风险给出有依据的结论与可执行调整。

当前固定主链：

```text
文本/截图 → OCR/结构解析 → TripBrief 确认 → 地点消歧
→ 事实采集 → Audit → 风险补充 → Advice
→ 预览/采纳 → 新 Revision → 完整 postcheck
```

拖拽式路线 Builder、旧 Planner、RAG、多 Agent、LoRA、Yjs 等仅是保留的技术资产，不是当前产品目标。除最低回归和 Roadmap 记录外，不得主动扩建。

## 权威顺序

发生冲突时按以下顺序处理，不能选择更容易实现的一份：

1. 本文件；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/governance/CURRENT_GOAL.md` 与 `docs/governance/ROADMAP.md`；
5. 已接受的 ADR；
6. 当前 commit/config 对应的 evidence。

历史方案、旧报告、测试数量和 README 描述都不能覆盖上述文件。Git/GitHub 提交规则继承全局 `AGENTS.md`，本文件不重复。

## 固定范围与停止项

- 只支持北京、上海、杭州；只支持单城市、2～5 人、2～5 天。
- 输入只包括粘贴文本、手工文字和截图；截图限 PNG/JPEG/WebP，最多 6 张，每张不超过 10MB。
- 不扩城、不支持跨城，不新增 Agent、消息队列、Kubernetes、GraphRAG，不重新微调模型。
- 不启动拖拽 Builder，不把历史 RAGAS、LoRA、Planner 或推荐指标作为「行程查」放行证据。
- 不承诺实时客流、医疗安全、自动订票、最低价、全国覆盖或跨城规划。

## 领域与证据不可变量

- `TripWorkspace → ItineraryRevision → EvidenceSnapshot → AuditEngine → RepairOption/EditCommand` 是权威主干。
- OCR/解析的不确定字段必须经用户确认；推断信息不得成为 `HARD`；无偏好显式保存 `NO_PREFERENCE`。
- 任何有语义的编辑或建议采纳必须创建新 revision；旧报告随即 stale；完整 postcheck 后才能显示为已解决。
- `UNKNOWN`、`UNAVAILABLE` 不得变相计为 PASS。Provider 局部失败必须保留成功事实并标注失败字段。
- 确定性事实与建议性判断分开。建议性判断必须展示依据，不得伪装成硬事实。
- 候选地点必须来自冻结的真实 CandidateSet，并绑定地点与路线 receipt；模型举例不得写成已验证地点。
- 原始截图不得进入数据库、日志或 Git；任务成功或失败终止后删除，只保留 hash、OCR 版本、文本框、置信度和处理回执。
- fixture、source prior、自动 Judge、snapshot、live Provider、公网 E2E 和真人证据是不同证明等级，不能互相替代。

## 单一 Goal 合同

任何开发只能执行 `docs/governance/CURRENT_GOAL.md` 中唯一处于 `APPROVED` 的切片。Goal 必须写明 Outcome、Scope、Non-goals、Authority、Baseline、Invariants、Verification、Budget、HITL 和 Stop conditions。完成当前切片不等于获准进入下一阶段。

连续两个切片无法改善同一门禁、需要扩大范围或新增基础设施、需要修改 blind/oracle、出现证据矛盾或成本超限时，立即停止并请求人工决策。

## 必须人工批准

- 合并分支、进入下一阶段、改变产品目标或降低 Gate；
- 修改公共 schema/API、添加或执行 migration；
- 新增生产依赖、使用真实付费 Provider 或扩大外部数据范围；
- 修改 frozen blind/oracle、晋级证据等级；
- 开始真人内测、公网部署、发布或对外能力声明。

开发分支内的小切片实现、离线测试、明确暂存、commit 和 push 按全局 Git 规则执行，无需逐文件审批。

## 默认验证命令

按当前 Goal 选择最小充分集合；未实际执行的层级必须写 `NOT_RUN`，不能推断通过。

```powershell
cd backend
python -m pytest tests/ -q
python -m ruff check app tests scripts

cd ../frontend
npm run build

cd ..
python backend/scripts/validate_dual_entry_testset.py
```

PostgreSQL、固定快照、真实高德/天气/Brave、浏览器恢复与性能、release manifest 必须各自实际重跑并绑定同一 commit/config/dataset/model/receipt，才能用于对应 Gate。
