# 「行程查」V2 API 与持久化合同

> 状态：`ACCEPTED`
>
> 版本：`trip-check-api-v2`

## 1. 兼容原则

- 保留现有 `POST /trip-workspaces/{workspace_id}/imports` 文本导入语义；旧 import、revision、repair、suggestion 和 evidence 保持可读。
- v1 workspace-first 路由继续兼容读取；v2 的新建主路径从 room 下的 `TripIntakeRevision` 开始，不把 v1 默认值迁入 v2 权威事实。
- 新资源只追加，不创建第二套 itinerary 编辑或“已解决”协议。
- 所有创建、确认、恢复和采纳命令要求 `Idempotency-Key`；基于 revision/state 的更新要求 `If-Match`。
- 服务端生成 canonical POI、Provider 事实、Finding 和 resolved 状态；客户端不能提交这些字段作为权威值。

## 2. 资源模型

### 2.0 TripIntakeRevision v2

必须包含版本、revision/content hash、原始文本 hash、解析器绑定，以及地点、人数、时间、偏好、问题和 readiness。每个原子值携带 `source_id/start/end/quote`，偏移按原始文本 Unicode code point 的半开区间解释。解析中间态允许范围、约数、至少、最多、未知、多候选和冲突，不得写入权威 Brief。

### 2.1 TripBriefRevision

必须包含：

- `brief_id`、`workspace_id`、`revision`、`parent_revision`、`content_hash`；
- 城市、日期、人数、到离信息、酒店或住宿区域；
- 四种交通方式与限制、预算、餐饮/住宿风格、饮食限制、每日节奏和活动强度；
- 字段级 `source_span`、`confidence`、`origin` 和确认状态；
- `DRAFT / NEEDS_CONFIRMATION / CONFIRMED`；
- `UNSPECIFIED` 表示未提及；只有用户明确确认时才使用 `NO_PREFERENCE`；`INFERRED` 不得成为 `HARD`。

### 2.2 TripCheckRun

必须包含：

- `run_id`、`workspace_id`、`itinerary_revision`、`brief_revision`；
- `stage`、`stage_attempt`、`lease_owner`、`lease_until`；
- `run_spec`、`config_hash`、已完成阶段和局部失败；
- `WAITING / RUNNING / PARTIAL / SUCCEEDED / FAILED / PRIVACY_BLOCKED / CANCELLED`；
- 稳定创建幂等键和每阶段副作用幂等键。

阶段固定为：

```text
PARSE → WAIT_BRIEF_CONFIRMATION → RESOLVE_PLACES → COLLECT_EVIDENCE
→ AUDIT → BUILD_ADVICE → WAIT_ADOPTION → POSTCHECK
```

恢复时 config hash 必须与原 Run 相同；不一致返回 `RUN_CONFIG_MISMATCH`，不得拼接旧结果。

### 2.3 RunSpec

绑定 commit SHA、Prompt/model/provider/rule 版本、execution mode、dataset/snapshot hash、fault profile、seed，以及 token、查询、重试、时间和成本预算。

### 2.4 AdviceBundle

绑定 Finding、具体行动、预期影响、不确定性、CandidateSet、Evidence/receipt、route delta、trade-off 和 RepairOption。具体地点必须来自冻结 CandidateSet；无可靠候选时只返回搜索区域和筛选条件。

## 3. API

### 3.1 输入与 TripBrief

- `POST /trip-workspaces/{workspace_id}/imports`：保留文本导入。
- `POST /trip-workspaces/{workspace_id}/imports/screenshots`：multipart，PNG/JPEG/WebP，最多 6 张、每张 10MB；整批校验后接收。
- `GET /trip-workspaces/{workspace_id}/trip-briefs/{revision}`：返回 ETag。
- `PATCH /trip-workspaces/{workspace_id}/trip-briefs/{revision}`：要求 `If-Match` 和 `Idempotency-Key`，产生新 brief revision。
- `POST /trip-workspaces/{workspace_id}/trip-briefs/{revision}/confirm`：要求完整必填确认；成功后只读。
- `POST /rooms/{room_id}/trip-intakes`：创建文本 Intake 草稿，要求 `Idempotency-Key`。
- `POST /rooms/{room_id}/trip-intakes/screenshots`：OCR 后创建同合同 Intake；原图清理规则不变。
- `GET/PATCH /trip-intakes/{intake_id}/revisions/{revision}`：读取或创建修正 revision；PATCH 要求 `If-Match` 与 `Idempotency-Key`。
- `POST /trip-intakes/{intake_id}/revisions/{revision}/confirm`：确认精确物化前提并创建只读 READY revision。
- `POST /trip-intakes/{intake_id}/revisions/{revision}/materialize`：幂等创建 workspace、confirmed Brief、Import 与 lineage receipt；Provider 解析不在数据库事务内执行。

### 3.2 Run 与进度

- `POST /trip-workspaces/{workspace_id}/trip-check-runs`：绑定已确认 brief 和 itinerary revision，要求 `Idempotency-Key`。
- `GET /trip-check-runs/{run_id}`：返回当前阶段、已完成阶段、局部失败和 ETag。
- `GET /trip-check-runs/{run_id}/events`：SSE；断线不取消后台 Run，重连按 `Last-Event-ID` 续传。
- `POST /trip-check-runs/{run_id}/resume`：只恢复 retryable/lease-expired Run，要求 `If-Match` 和 `Idempotency-Key`。

### 3.3 Advice 与 Repair

- `GET /trip-workspaces/{workspace_id}/reports/{report_id}/advice`：只返回与报告 revision/evidence 绑定的 Advice。
- Repair 预览、应用和 postcheck 继续使用现有 Repair/EditCommand 接口；旧报告在新 revision 后立即 stale。

## 4. 通用响应与错误

- 创建成功：`201`；幂等重放增加 `Idempotency-Replayed: true`。
- 乐观并发冲突：`409 ITINERARY_REVISION_CONFLICT` 或对应 brief/run conflict。
- 缺少 `If-Match`：`428 IF_MATCH_REQUIRED`。
- config 漂移：`409 RUN_CONFIG_MISMATCH`。
- 不支持范围：`422 SCOPE_NOT_SUPPORTED`，必须在 Provider 事实采集前返回。
- 原图清理失败：Run 为 `PRIVACY_BLOCKED`，不得返回成功。
- Provider 局部失败：HTTP 可以成功返回 `PARTIAL`，字段保持 `UNKNOWN/UNAVAILABLE` 并附失败类别。

## 5. Migration

- `022_trip_brief_revisions.sql`：brief、字段来源/确认和临时资产清理 receipt；
- `023_trip_check_runs.sql`：Run、stage、lease、attempt 和阶段幂等；
- `024_advice_bundles.sql`：Advice、CandidateSet 引用和 postcheck lineage。
- `025_trip_intake_v2.sql`：不可变 Intake revision/field source/materialization，并把城市、人数和天数的历史固定范围放宽为确认后的正值合同。

Migration 只追加；应用启动只检查兼容性，不自动执行 DDL。
