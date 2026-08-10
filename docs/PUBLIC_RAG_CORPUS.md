# 公开 RAG 语料与引用边界

正式 Demo 和评测只能使用 `corpus_kind=public` 的已审核资料。合成游记仅可用于离线开发或对照，界面会明确标记为“演示语料”。

## 收录规则

1. 自动采集仅允许固定白名单中的 Wikivoyage revision（CC BY-SA 4.0）与 Wikidata 地点事实（CC0）；保留 canonical URL、revision、获取时间、内容哈希与许可说明。
2. 不用爬虫绕过 robots、登录、付费墙或站点使用条款，不抓取受限攻略平台正文。`build_public_corpus` 只调用 Wikimedia Action API，`ingest_public_notes` 只接受这类 `corpus_kind=public` JSONL。
3. 每条资料须包含稳定 ID、城市、标题、短摘录、来源 URL、发布时间（如有）、获取时间、许可和主题标签。
4. 盲测题不得从调参记录、同篇原文或同一段改写而来；数据更新必须创建新的 evidence manifest。

## 导入

先确认后端已应用 `006_add_rag_source_provenance.sql`，再准备 JSONL：

```powershell
cd backend
python -m scripts.build_public_corpus --output data/generated/public_sources.jsonl --manifest evidence/corpus/latest.json
python -m scripts.ingest_public_notes --input data/generated/public_sources.jsonl
```

`data/public_sources.example.jsonl` 只说明格式，不能当作真实语料。导入后，SSE 会发送 `citations` 事件；前端在“回答依据”中显示 URL、摘录和公开/演示语料标签。
