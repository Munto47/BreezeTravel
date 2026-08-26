# BreezeTravel 双入口测试数据集 v1

本目录是 Final 2.0 的首批机器可读测试资产，不是发布通过证据。

## 当前内容

- 78 个 case；仓库内只有 60 份开发 label，18 份 frozen-blind truth 只保留不可评分 hash 承诺；
- 北京、上海、杭州各 26 个；
- 导入入口 37 个、路线构建入口 41 个；
- `pilot=6 / dev=42 / regression=12 / frozen_blind=18`；
- 4 份 RunSpec：PR 离线、nightly snapshot、weekly live、release blind。

每条 input 还固定记录 `normalized_input_sha256`、source/template/generator family、可空的 mutation parent、lineage 状态和 receipt 索引。hash 不是占位：validator 会对 `input` 做 Unicode NFC、换行归一、key 排序和紧凑 UTF-8 JSON 编码后逐条重算。任何 normalized input 重复或 Builder canonical candidate sequence 重复都会直接使 G0 失败。

历史 Generator/Prompt template 日志不存在，因此本版没有用 case ID 伪造 78 个“独立 family”。`template_family_id` 与 `generator_family_id` 使用共享 unavailable sentinel，并以 `lineage_status=UNAVAILABLE` 明示；72 条无外部 source document 的 case 同样如此。只有 6 条绑定已归档 source document set 的 case 标为 `source_family=RECORDED`。原来无法证明父子关系的 `controlled_mutation` 已降级为 `high_fidelity_synthetic`，`mutation_parent_case_id=null` 且 `mutation_family=NOT_APPLICABLE`。未来只有具备真实父 case 的变异才能标回 `controlled_mutation/RECORDED`。

本轮只向 `dev` 新增 12 条 Builder 合同 case、向 `regression` 新增 6 条 Builder 门禁种子；`frozen_blind` 仍为 18 条且其中 Builder 仍为 6 条。数据集静态计数仍只有 G2 四站 session seed 3/9、G5 恢复 seed 6/9，不能因外部运行证据或开发样本增长而抬高 release corpus 统计。另有独立九场景 Backend/Yjs 恢复矩阵已于 2026-08-21 实跑通过，但它只证明恢复合同，不会把这 6 条 seed 改写成 9 条数据集样本，也不替代 G2 四站门禁。

输入和标签隔离。产品、Generator、Judge 和 development runner 只能读取 `*.inputs.jsonl` 以及 pilot/dev/regression label。`sealed/frozen_blind.labels.jsonl` 已不含任何 case truth 或 metric oracle；它只是一个 metadata seal，固定 case-set hash、旧 payload byte hash 与 canonical label commitment，不能单独评分。

真实 frozen-blind truth 必须在仓库外保存为 `dual-entry-blind-label-bundle-v1`。产品输出落盘后，独立 scorer 才能通过显式外部路径或独立进程 stdin 读取 bundle，并要求另一路提供 bundle byte SHA-256。scorer 会同时核验 `run_id`、`run_spec.json` bytes、`dataset_content_sha256`、manifest、case-set、`product_outputs.jsonl` bytes 和 metadata seal；缺失、篡改、仓库内 bundle 路径或任意 binding 漂移都直接拒绝，不产生部分通过。

## 校验

```powershell
$env:PYTHONPATH="backend"
python -m scripts.validate_dual_entry_testset
python -m pytest -q backend/tests/test_dual_entry_testset_contract.py
```

结构校验通过不代表 release ready。若要把未完成项作为失败返回：

```powershell
python -m scripts.validate_dual_entry_testset --require-release-ready
```

当前预期退出码为 `2`，因为 G2/G5 Builder seed 数量不足且未执行，blind 规模、Import/Release Provider snapshot 覆盖、外部 blind bundle provision 和真人校准尚未完成。Builder slice 已绑定 2026-08-21 三城真实候选与步行路线快照，G0 preflight 可逐字节复算文件/载荷 SHA-256；该快照不证明营业、预约、无障碍、public E2E 或真人体验，也不替代 PostgreSQL 门禁。当前被测试 case 引用的北京/上海官方路线与北京/上海/杭州 Wikivoyage 社区内容均已冻结 capture receipt、许可/署名信息和最小结构化 extract；未被 case 引用的登记来源仍不计作已入库事实。

### 静态 subject receipt 的真实性收口

旧 G0 把“input 中没有 `provider_receipt_id`”直接称作 Provider 缺口，得到 25 个 case / 111 个 subject。逐路径核验后，这 111 条没有一条能与仓库 Amap snapshot 按 canonical identity + 原 receipt bytes 做精确绑定；地点名、坐标或抓取时间均未用于反推 receipt。重新分类如下：

| 旧缺口分类 | subject 数 | 证据边界 |
|---|---:|---|
| 真实 Amap snapshot 精确绑定 | 0 | 不存在可证明的一一绑定 |
| `CONTROLLED_FIXTURE_EXECUTION` | 90 | pilot/dev/regression 合成输入；`provider_call_attempted=false`、`current_fact_authority=false` |
| `UNAVAILABLE` | 21 | frozen blind 无仓库可见历史调用，必须由外部 Provider artifact 补齐 |

`subject_receipt_registry.jsonl` 进一步枚举全部 259 个静态 subject，而不只检查原先没有 ID 的对象：224 个 development fixture、35 个 frozen-blind `UNAVAILABLE`、真实 Provider receipt 为 0。新增的 35 个 unavailable 包含旧统计中的 21 个 blind candidate，以及旧逻辑漏检的 6 个 blind seed、8 个 blind Import controlled-fact subject。每条记录绑定 `case_id + subject_path + subject_sha256 + normalized_input_sha256`，每个 case 再保存 record SHA-256；registry 整体 SHA-256 固定在 manifest。重复 POI 的不同 stop/path 必须使用不同 receipt，不能复用一次调用字符串掩盖 stop-level 缺口。

这些 receipt 只证明 controlled input bytes 或明确证明“不可用”，不包含伪造 request/response、`observed_at` 或 Provider 名称。Import 的 offered/rejected/materialized candidate receipt，Builder 的 candidate/route-leg/current-fact receipt 必须来自运行期 `provider_receipts.jsonl`；特别是 wrong-city rejected candidate 的完整 receipt 仍由 HTTP runner 实测，静态 fixture/source receipt 不可替代。官方路线和 UGC source archive 仍只贡献结构/多样性/相邻先验，不能升级为 POI、当前事实或路线 receipt。

当前 G0 因 35 个 frozen-blind 静态 subject evidence `UNAVAILABLE`、template/generator lineage 78/78 不可用以及其他发布门禁继续 fail closed。

## 可计算指标合同

每份 development label 都有 `metric_oracles`，六项指标必须二选一：`APPLICABLE` 并给出结构化真值，或只有 `applicability=N_A + reason_code`。仓库可读的 60 份开发标签当前覆盖：parse F1 5/60、实体 precision/recall 4/60、finding precision/recall 16/60、repair/postcheck predicate pass rate 7/60、Builder nDCG@5 11/60、Builder Recall@5 13/60。blind 覆盖率只有隔离 scorer 在验证外部 bundle 后才能计算，G0 validator 不读取或推断。

以上真值来自 synthetic/controlled case，授权真人真值仍为 0/78；自动指标可用于开发回归，不能替代 human calibration，也不能据此宣称 release ready。

### 三城 frozen snapshot 排序 oracle

`builder_oracles/three_city_frozen_suggestion_ranking_v1.json` 是 development-only 的 deterministic automated proxy，不是 human label。它通过 SHA-256 绑定 checked-in 高德实体/步行路线快照，使用真实 canonical POI ID 以及 entity、route、current-fact receipt 和各自 hash。固定 rubric 不读取产品的 rank position、score components 或 total score。

Wrong-city、canonical/anchor duplicate、显式 HARD block 和 UNKNOWN route 固定为 0 分；正向分量只有路线适合度、意图/品类覆盖与 receipt 完整性。缺少 visit slot 或成员需求时，开放、预约、无障碍适用性保持 `N_A`，真人偏好和当前热度质量保持 `UNKNOWN`。官方/社区 route prior 不会被提升为 current fact。

Builder HTTP scorer 只在 development RunSpec 中 overlay 该 oracle，并从首轮 frozen SuggestionSet 读取 `canonical_place.place_id`。临时 suggestion candidate ID 不会被映射成 oracle ID；artifact/source hash 漂移、污染或 canonical ID 不匹配都会在评分前或门禁中真实拒绝。

## 来源归档边界

`archives/` 不保存带版权页面的完整 HTML 或图片。官方路线只保留公开 URL、抓取时间、HTTP/robots/copyright 元数据、远端正文 SHA-256 和与当前测试有关的最小路线字段。`source_registry.jsonl` 的 `raw_hash` 是 capture receipt 文件自身的 SHA-256，`extract_hash` 是结构化 extract 文件的 SHA-256；receipt 内另存远端正文 SHA-256，extract 必须通过它回指 receipt。`allowed_use` 只能等于或收窄 registry 的 `usage_modes`，不能把路线结构先验升级成实时事实或当前热度。

Wikivoyage 链使用固定 revision 和 CC BY-SA 4.0 署名。三城 extract 只保留地点查询词、明确顺序或文章分组、体验/季节/人群标签；其中 `ARTICLE_CLUSTER_ORDER` 明确不是地理顺序。loader 输出仍是待 Provider 解析的 query hint，不能产生 canonical identity、坐标、当前营业/预约/价格/无障碍/路线耗时/热度结论，也不代表实时用户行为或采纳率。

## 事实边界

- 地点名称用于测试实体和路线覆盖，不是当前营业、预约或可达性的长期事实；
- frozen snapshot 只证明可重放，不是 live Provider；
- GPT-5.6-sol 样本是 `high_fidelity_synthetic`；
- GPT-5.6-sol Judge 是 `automated_proxy_judge`；
- 只有授权真实组织者可以增加 human manifest 计数。
