-- PostgreSQL + pgvector 初始化脚本
-- 由 docker-compose 在 postgres 容器首次启动时执行

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================
-- 游记分块表（RAG 核心数据）
-- =============================================
CREATE TABLE IF NOT EXISTS travel_notes_chunks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    note_id         TEXT NOT NULL,              -- 关联原始游记 ID
    chunk_idx       INT  NOT NULL,              -- 该游记内的分块序号
    city            TEXT NOT NULL,              -- 城市（用于过滤检索范围）
    content         TEXT NOT NULL,              -- 分块文本内容（原始中文）
    content_tokens  TEXT DEFAULT '',            -- jieba 分词结果（空格分隔，供 BM25 使用）
    content_tsv     tsvector                    -- BM25 全文索引（从 content_tokens 生成）
                    GENERATED ALWAYS AS (
                        to_tsvector('simple', COALESCE(content_tokens, ''))
                    ) STORED,
    place_ids       TEXT[]   DEFAULT '{}',      -- 关联的高德 POI IDs（Entity Linking 结果）
    embedding       vector(1536),               -- text-embedding-3-small 向量（1536 维）
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 城市过滤索引（检索时 WHERE city = $2）
CREATE INDEX IF NOT EXISTS idx_chunks_city ON travel_notes_chunks(city);

-- pgvector IVFFlat 索引（Dense 向量相似度检索）
-- lists 参数：约为 sqrt(行数)，80 篇游记约 800 条 chunks，设为 10
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON travel_notes_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);

-- GIN 索引（Sparse BM25 全文检索）
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
    ON travel_notes_chunks USING gin(content_tsv);

-- =============================================
-- 原始游记元数据表
-- =============================================
CREATE TABLE IF NOT EXISTS travel_notes (
    id         TEXT PRIMARY KEY,          -- 游记唯一 ID（nanoid）
    title      TEXT,
    city       TEXT,
    author     TEXT DEFAULT '旅行者',
    content    TEXT,
    tags       TEXT[]   DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================
-- 用户表（持久化用户 ID 与昵称）
-- =============================================
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,          -- 前端 localStorage 生成的 UUID
    nickname   TEXT NOT NULL DEFAULT '旅行者',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================
-- 房间状态表（协同房间的元数据持久化）
-- =============================================
CREATE TABLE IF NOT EXISTS rooms (
    room_id    TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,             -- 对应 LangGraph PostgresSaver 的 thread_id
    trip_city  TEXT,
    trip_days  INT  DEFAULT 3,
    phase      TEXT DEFAULT 'exploring',  -- exploring / selecting / optimizing / planned
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================
-- 房间成员表（记录谁加入了哪个房间）
-- =============================================
CREATE TABLE IF NOT EXISTS room_members (
    room_id    TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    joined_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (room_id, user_id)
);

-- =============================================
-- 用户长期偏好表（Long-term Memory，Sprint 2 新增）
-- =============================================
CREATE TABLE IF NOT EXISTS user_preferences (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id    TEXT NOT NULL,              -- 对应 users.user_id
    content    TEXT NOT NULL,              -- 偏好摘要文本（自然语言）
    embedding  vector(1536),              -- text-embedding-3-small 向量（语义检索用）
    category   TEXT DEFAULT 'general',    -- 偏好类别（城市名或 "general"）
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 按用户 ID 查询索引（加载用户偏好时使用）
CREATE INDEX IF NOT EXISTS idx_user_prefs_user_id
    ON user_preferences(user_id);

-- pgvector 索引（语义相似偏好检索）
-- lists=5：用户偏好记录较少，用较小的 lists 值
CREATE INDEX IF NOT EXISTS idx_user_prefs_embedding
    ON user_preferences USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 5);
