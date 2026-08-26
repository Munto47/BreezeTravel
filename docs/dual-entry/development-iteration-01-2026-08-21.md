# 双入口开发迭代 01：导入事实连续性与审计准确性

日期：2026-08-21  
依据：`BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md`

## 本轮结果

导入链路已经从“解析出地点名即可应用”收紧为可验证事实链：

1. EntityResolver 只允许带 canonical/provider identity、坐标、脱敏真实请求哈希、响应哈希、带时区观测时间和明确 execution mode 的候选进入 READY。
2. 异地候选在解析阶段丢弃，用户确认不能绕过地点事实完整性。
3. apply 在一个 PostgreSQL 事务内写 revision、地图坐标投影、`room_places` 协同投影、stop 级 immutable receipt、workspace/import pointer 和幂等 command。
4. `itinerary_place_receipts` 按 revision/stop 保存不可变事实；后续 revision 使用最近祖先 receipt，避免 `room_places` 被客户端同步覆盖后污染 Audit。
5. MapProjection 回读 canonical name、coordinates、provenance 和 receipt hash。
6. 到达、固定预约、返程已成为 `CommitmentKind`，并进入 revision content hash；航班事件不会伪装成 POI。
7. Audit 新增错城和冲突证据规则；冲突营业时间不会再重复报成“营业时间缺失”。
8. Audit 新增路线空档、固定预约和返程可行性规则；Repair preview 会刷新新增/变化路线边后再执行 full postcheck，Provider 失败仍保留 UNKNOWN 并拒绝候选。
9. 首日到达、末日返程、餐食窗口和末日酒店按真实可用行程边界判断，避免无条件跳过或末日误报。
10. wrong-city 命中作为不可确认的 `rejected_candidates` 回读完整 Provider receipt；空结果和不完整候选不会补造回执。

## 已加入的故障验证

- 缺坐标 / 缺真实 request hash：不能 READY，也不能确认。
- wrong-city：解析为 NOT_FOUND，不能确认或静默应用。
- InMemory apply 在 revision 写后或 place 写后发生故障：恢复 workspace、revision、place、import 和 command。
- PostgreSQL 在第二个 `room_places` 写入时由 trigger 注入异常：测试断言 revision、workspace pointer、room_places、immutable receipts、import status、command 六类状态全部回滚。
- revision 2 之后篡改 `room_places`：测试断言 Audit 仍回读 revision 1 的 immutable receipt。

## 当前实测

- 双入口数据集：78 cases / 78 labels，三城各 26；其中 Builder 41，结构校验通过。新增 18 条全部进入 dev/regression，frozen blind Builder 仍为 6/45。
- 后端全量：964 passed、24 skipped、0 failed；skip 包含本机不可用的 PostgreSQL/外部集成门禁，不计为通过。
- 前端 `next build`：编译、类型检查和 10 个页面生成通过。
- Continuous HTTP Runner：14 tests passed；注入受控 HTTP transport 的 12 个 PR import cases 全部 `PASS/PROMOTE`，删除 wrong-city rejection receipt 的负测准确 `INVALID/REJECT`。
- Continuous preflight 当前 `ACCEPT_PREFLIGHT`：实际引用的北京官方路线库、上海官方 citywalk 的 capture receipt / minimal extract 已冻结，文件字节 SHA-256、extract 追溯和用途边界校验均通过。
- checked-in runner 对真实 localhost 执行时因后端未启动返回 `INVALID/REJECT: PRODUCT_HTTP_ADAPTER_UNAVAILABLE`，并生成 hash-bound gate；没有把受控 transport 结果冒充真实服务结果。

## 尚不能宣称通过的门禁

- 本机 Docker Desktop 与 `127.0.0.1:5432` 均不可用，因此 migration 018、PostgreSQL trigger rollback、无 SQL seed 的真实数据库读回仍只是已编写、未实跑。
- Continuous IMPORT HTTP 执行器已覆盖受控的登录→房间→workspace→import→合法确认/异城拒绝→apply→readback，并记录脱敏 HTTP/Provider receipts；真实 PR RunSpec 已解除来源预检 blocker。
- 本轮只归档 PR 实际引用的两条官方路线来源；上海历史路线投票、OSM 许可页、Wikivoyage 复用政策仍未形成可复现 raw/extract archive，不能把整个 registry 称为已集成数据，更不能把开放用户内容称为实时用户热度。
- 冻结盲测仍为 import 12/90、builder 6/45、fault/recovery 5/24；human calibration 0/30，最终发布门禁保持关闭。
