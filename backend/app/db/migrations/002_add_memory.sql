-- Migration 002：添加用户长期偏好表（Long-term Memory）
--
-- 适用场景：数据库已存在，需要升级以支持跨会话用户记忆。
-- 新建数据库无需运行此迁移，init.sql 已包含这些表。
--
-- 运行方式：
--   docker compose exec postgres psql -U postgres -d travel_agent -f /migrations/002_add_memory.sql
--
-- 幂等：使用 CREATE TABLE IF NOT EXISTS，可安全重复执行。

-- 用户长期偏好表
CREATE TABLE IF NOT EXISTS user_preferences (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id    TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(1536),
    category   TEXT DEFAULT 'general',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_prefs_user_id
    ON user_preferences(user_id);

CREATE INDEX IF NOT EXISTS idx_user_prefs_embedding
    ON user_preferences USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 5);

-- 验证迁移结果
DO $$
DECLARE
    tbl_exists BOOLEAN;
    idx_count INT;
BEGIN
    SELECT EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_name = 'user_preferences'
    ) INTO tbl_exists;

    SELECT COUNT(*) INTO idx_count
    FROM pg_indexes
    WHERE tablename = 'user_preferences';

    IF tbl_exists AND idx_count >= 2 THEN
        RAISE NOTICE 'Migration 002 completed successfully.';
        RAISE NOTICE '  user_preferences table: OK';
        RAISE NOTICE '  indexes (% total): OK', idx_count;
    ELSE
        RAISE WARNING 'Migration 002 may have partially failed. tbl_exists=%, idx_count=%', tbl_exists, idx_count;
    END IF;
END
$$;
