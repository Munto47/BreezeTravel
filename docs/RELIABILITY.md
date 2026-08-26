# 「行程查」V1 可靠性合同

## 权威与恢复

- PostgreSQL 保存 Run、stage、lease、attempt、幂等命令、receipt 和业务结果。
- LangGraph checkpoint 保存可恢复计算进度，不等于副作用提交。
- Redis 丢失只能影响缓存、限流或性能，不能改变行程、Evidence、Finding 或 Advice。
- worker 只接管过期 lease；恢复前必须校验 RunSpec/config hash。

## 副作用边界

Provider 请求、数据库 mutation、Advice 采纳和 postcheck 分别使用由稳定业务输入派生的幂等键。数据库命令在事务内写业务状态和命令回执；Provider 重试只针对明确 retryable 失败，并保留每次 attempt receipt。

## 固定故障

| fault profile | 必须行为 |
|---|---|
| provider_timeout | 有界重试，受影响字段 UNKNOWN，成功事实保留 |
| provider_partial | Run PARTIAL，失败字段和类别可见 |
| duplicate_submit | 同一资源和 `Idempotency-Replayed` |
| concurrent_edit | 一个成功，失败方 409 并回读最新 revision |
| process_termination | lease 接管，不重复 Provider/repair/revision |
| config_drift | `RUN_CONFIG_MISMATCH`，创建新 Run |

## SSE

断线不取消后台 Run。事件使用稳定单调 ID；客户端通过 `Last-Event-ID` 重连。重复事件不得触发重复副作用，失败 UI 必须显示已完成阶段、不可用字段、可重试动作和稳定 Run ID。

## Trace 与脱敏

OpenTelemetry 领域属性固定为 `bt.run_id`、revision、brief、evidence、config、rule、provider、execution mode 和 failure category。禁止记录原图、完整 Prompt、原始文本、密钥、Authorization 和未脱敏 Provider 响应。

## 证明边界

可靠性只能由当前 commit 的固定 fault runner、PostgreSQL 集成、进程终止和 artifact readback 证明。配置存在、unit mock 或历史报告不能替代。
