# 双入口可验证行程：本地交付验收（2026-08-20）

## 结论

P0～P8 的**本地开发实现**已完成验收；本文件不表示真人验收、真实 Provider 可用性或公网发布已完成。

| 范围 | 本地验收结果 | 关键边界 |
|---|---|---|
| M1-dev | 通过 | 三个 GPT-5.6-sol `synthetic_proxy` 角色、150 条三城样本；不是人类证据。 |
| P5 | 通过 | 三城 DRAFT 骨架、模板入口、候选/酒店投影；不宣称 `REVIEWED` 或真实酒店推荐。 |
| P6 | 通过 | 编辑、地图投影、变更路线边 refresh、增量/完整 parity、审计后确认；真实 Provider P95 不在本地结论内。 |
| P7 | 通过 | 成员 HARD 审计、受限分享、接收方页面、Yjs 引用和显式冲突恢复；Yjs 不是权威状态。 |
| P8 | 通过 | 临行窗口、不可变复检差异、可选 Provider adapter、持久化重放；默认不会声称实时 Provider 调用。 |

## 验证记录

在本次交付工作树中执行：

```text
backend: python -m pytest tests -q
结果：867 passed, 24 skipped

backend（受控 PostgreSQL）：
RUN_SERVICE_INTEGRATION=1 python -m pytest \
  tests/test_migrations_integration.py \
  tests/test_templates_sharing_postgres.py \
  tests/test_dual_entry_postgres_integration.py -q
结果：4 passed

frontend: npm run build
结果：通过；包括 /templates、/share/[token]、/workspace/[workspaceId]

frontend: playwright.local.config.js
结果：2 passed（缓存报告失效、模板入口）

frontend: playwright.workspace.config.js
结果：2 passed（revision 冲突、陈旧报告确认）

backend: python -m ruff check app evals scripts tests
git diff --check
结果：通过
```

数据库验证使用临时数据库；本地 PostgreSQL 容器在测试后已停止。

## 交付物

- 唯一开发基线：[最终方案](../BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md)
- 能力状态：[capability-status.md](capability-status.md)
- 5.6-sol 本地代理校准：[m1-dev-proxy-evidence-2026-08-20.md](m1-dev-proxy-evidence-2026-08-20.md)
- 最终真人协议：[m1-human-calibration-protocol.md](m1-human-calibration-protocol.md)
- 可复现交付 manifest：[latest.json](../../backend/evidence/releases/latest.json)。它绑定当前工作树指纹、迁移、最终方案、本地验收记录与 M1-dev gate 哈希；`release_approval_granted=false`，不替代本节的证据边界。

## 明确后置的发布门禁

以下内容必须在本地交付之后，且不能由本地合成代理结果代填：

1. 30 份真实原始行程和 15～20 名真实组织者的 consent、finding、Repair 行为；
2. 真实高德/天气链路的可用性和性能样本；
3. 真实双用户 Yjs + 服务重启 E2E；
4. 已授权环境中的公网双入口 E2E、分层证据包和部署验证。

在这些完成前，允许表述为“本地代理验收 candidate”，不得表述为真人验证、生产可用或商业验证。
