# G01-S0 现场 Preflight、旧入口隔离与 v3 公共边界

日期：2026-08-27

Goal：`TC-VNEXT-G01-TEXT-CARDS`

结论：`S0_READY_FOR_CHECKPOINT`

## 1. 现场基线

- 唯一写入工作树：`agentTravel-product-reset`；分支/上游：`codex/trip-check-product-reset` / `origin/codex/trip-check-product-reset`。
- 本地 HEAD、远端实现分支和`origin/develop`均为`d114d6a1e9a06b1e26fb62519710e35d50300d70`；写入前工作树为clean。
- `main@2238c5304e3c17fae8162cbfca345d2fbdf5f076`、其他worktree和历史分支未修改。
- 处置清单是`g01_s0_asset_disposition.json`，校验入口是`python -m scripts.check_g01_s0`。校验会枚举当前页面、99条旧OpenAPI路径、主要后端模块和全部后端测试文件，并要求每个资产恰好落入`KEEP / ADAPT / FREEZE / REMOVE_FROM_ENTRY`之一。

## 2. 处置边界

| 标记 | 本次决定 |
|---|---|
| `KEEP` | 兼容认证、用户/健康接口；PostgreSQL revision、CAS、幂等、lease、SSE；Evidence/Audit/EditCommand链；旧API可读测试。 |
| `ADAPT` | 首页、登录、历史、个人隐私入口、关于页；Trip Intake证据编译、AMap adapter、地图revision投影、API gateway和共享client。 |
| `FREEZE` | Builder/Planner/Yjs、旧RAG、ReAct/Critic、LoRA、模板/分享/旧建议入口、P0-P6、Trip NLU Candidate、blind/oracle/manifest和冻结测试。 |
| `REMOVE_FROM_ENTRY` | `/intake`、`/import`、`/room/**`、`/workspace/**`以及旧room/workspace/import入口。对应API继续注册、可读且受兼容测试保护；新首页不得链接或调用。 |

当前没有独立`/privacy`页；现有`/profile`承担待改造的隐私入口，因此标为`ADAPT`。`REMOVE_FROM_ENTRY`不表示删除路由、代码或数据。

## 3. 旧 OpenAPI 与运行容器

- 冻结兼容基线：`packages/trip-check-client/openapi.json`，SHA-256 `0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81`，99 paths / 106 operations。
- 当前实现源码生成105 paths、v3为0；旧99条路径/方法零缺失，新增的6条均为Trip Intake路由。`/api/rooms/**`继续可读但从新入口移除，`/api/trip-intakes/**`作为证据编译资产标为`ADAPT`。
- 现场旧容器：94 paths，v3 paths为0；健康状态为`local_fixture`、`AMAP_MOCK=true`。
- 容器少于冻结基线的5条路径是：微信登录1条、截图上传批次4条。分类为`LEGACY_CONTAINER_DRIFT`，不能把旧容器的94条反向固化为新基线。
- v3只做附加；旧99条路径和方法必须继续存在。旧API、旧表和旧数据均不删除。

## 4. 历史 Candidate Manifest 诊断

现场原样执行`tests/test_trip_nlu_v2_gate.py`得到：`18 passed / 3 skipped / 2 failed`。

- Candidate引用的10/10数据文件hash有效；动态schema绑定有效；generator在受支持的LF/CRLF字节材料化范围内绑定有效。
- `validator / scorer / gate`三项hash与当前冻结提交的对应Git/工作树字节均无法绑定，两个失败都先停在`manifest evaluator/schema code binding mismatch`。
- 仅在pytest临时目录复制manifest并替换为当前代码绑定后，公开validator可验证120条数据；`blind_labels_read=false`。
- 同一临时诊断继续把仓库内blind输入当外部标签时，仍按预期拒绝：`external blind labels must be outside the repository`。

结论固定为：`HISTORICAL_BINDING_INVALID / FROZEN`。这证明历史失败是绑定失效，不证明新版产品质量，也不授权重写manifest、blind、oracle、P0-P6证据或冻结测试。

## 5. Provider 现场边界

- Qwen live lane：`NOT_READY`。现有本地秘密配置可见key和通用兼容URL，但未确认账号region、workspace、exact model ID及账号价格绑定；应用配置仍是DeepSeek语义。S0和首个切片不调用Qwen。
- AMap live persistence：`BLOCKED_PENDING_WRITTEN_PERMISSION`。现有凭据和技术接口不等于持久化许可；取得书面许可前只使用fixture，不发起真实POI或路线调用。

## 6. 首个 v3 公共边界

- 首切片只把`{\"mode\":\"DEMO\"}`加入OpenAPI；`FULL`保留为后续discriminated-union分支，不暴露占位实现。
- create要求`Idempotency-Key`，返回`202`、随机非秘密`public_resource_id`、用户状态、result URL和events URL；匿名能力由签名的`HttpOnly + SameSite=Lax` cookie承载，秘密不进入URL或JSON。
- result处理中返回`202 TripUnderstandingProgressView`；完成后返回`200 UserFacingTripResult`及不可逆、不透明ETag。
- events使用持久事件游标并支持`Last-Event-ID`；仅允许`progress`和`result_available`等用户事件及固定友好文案。
- 公共结果顶层固定为`status / assumptions / days / map / stay / available_actions`。卡片仅含不展示的opaque activity token、名称、类别、区域/地址、时间提示、用户状态和可用动作。
- 公共JSON和DOM禁止原文、source/span/offset、confidence、模型/Provider、UUID/hash/revision/receipt/run/stage和内部错误。

## 7. S0 证据边界与下一动作

S0只证明现场基线、处置覆盖、旧路径边界、冻结资产零diff和历史失败诊断；不证明v3已实现。下一自主动作是追加migration 028，并实现独立worker驱动的固定北京`create → events → result`纵向切片。

H1、live Provider、公网、生产、商业、release、`main`均为`NOT_RUN`。
