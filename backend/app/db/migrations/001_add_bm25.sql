-- Migration 001：为 travel_notes_chunks 添加 BM25 全文检索支持
--
-- 适用场景：数据库已存在（通过旧版 init.sql 创建），需要升级以支持混合检索。
-- 新建数据库无需运行此迁移，init.sql 已包含这些列。
--
-- 运行方式：
--   docker compose exec postgres psql -U postgres -d travel_agent -f /migrations/001_add_bm25.sql
--
--   或本地：
--   psql $DATABASE_URL -f backend/app/db/migrations/001_add_bm25.sql
--
-- 幂等：使用 ADD COLUMN IF NOT EXISTS，可安全重复执行。

-- Step 1：添加 content_tokens 列（jieba 分词结果，空格分隔）
ALTER TABLE travel_notes_chunks
    ADD COLUMN IF NOT EXISTS content_tokens TEXT DEFAULT '';

-- Step 2：添加 content_tsv 计算列（从 content_tokens 自动生成 tsvector）
-- 注：GENERATED ALWAYS AS ... STORED 为 PostgreSQL 12+ 特性
ALTER TABLE travel_notes_chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', COALESCE(content_tokens, ''))
    ) STORED;

-- Step 3：创建 GIN 索引（BM25 全文检索）
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
    ON travel_notes_chunks USING gin(content_tsv);

-- Step 4：用现有 content 字段做初步分词填充（简单空格分割，非 jieba）
-- 注意：这只是临时填充，运行 ingest_notes 脚本重新入库可获得更好的 jieba 分词效果
--   python -m scripts.ingest_notes --rebuild-tokens
UPDATE travel_notes_chunks
    SET content_tokens = regexp_replace(content, '[^一-龥a-zA-Z0-9]', ' ', 'g')
WHERE content_tokens = '';

-- 验证迁移结果
DO $$
DECLARE
    col_count INT;
    idx_count INT;
BEGIN
    SELECT COUNT(*) INTO col_count
    FROM information_schema.columns
    WHERE table_name = 'travel_notes_chunks'
      AND column_name IN ('content_tokens', 'content_tsv');

    SELECT COUNT(*) INTO idx_count
    FROM pg_indexes
    WHERE tablename = 'travel_notes_chunks'
      AND indexname = 'idx_chunks_content_tsv';

    IF col_count = 2 AND idx_count = 1 THEN
        RAISE NOTICE 'Migration 001 completed successfully.';
        RAISE NOTICE '  content_tokens column: OK';
        RAISE NOTICE '  content_tsv column: OK';
        RAISE NOTICE '  idx_chunks_content_tsv index: OK';
        RAISE NOTICE 'Next step: run "python -m scripts.ingest_notes --rebuild-tokens" for better jieba tokenization.';
    ELSE
        RAISE WARNING 'Migration 001 may have partially failed. col_count=%, idx_count=%', col_count, idx_count;
    END IF;
END
$$;
