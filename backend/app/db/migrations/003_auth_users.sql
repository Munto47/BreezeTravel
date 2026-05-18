-- Sprint 4: 用户账号体系 + 景点持久化 + 路线保存

-- 扩展 users 表（手机号 + 个人信息）
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone     VARCHAR(20) UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS birthday   DATE;

-- 手机号索引（登录时按手机号查用户）
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL;

-- =============================================
-- 短信验证码表
-- =============================================
CREATE TABLE IF NOT EXISTS sms_verifications (
    id         UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone      VARCHAR(20) NOT NULL,
    code       VARCHAR(6)  NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN     DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_verifications(phone);

-- =============================================
-- 房间景点持久化表（Yjs 状态的 DB 备份，支持跨会话继续规划）
-- =============================================
CREATE TABLE IF NOT EXISTS room_places (
    id         UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    room_id    TEXT        NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    place_id   TEXT        NOT NULL,
    place_data JSONB       NOT NULL,
    voted_by   TEXT[]      DEFAULT '{}',
    added_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(room_id, place_id)
);

CREATE INDEX IF NOT EXISTS idx_room_places_room ON room_places(room_id);

-- =============================================
-- 已保存路线表
-- =============================================
CREATE TABLE IF NOT EXISTS saved_itineraries (
    id             UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    room_id        TEXT        REFERENCES rooms(room_id) ON DELETE SET NULL,
    user_id        TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    city           TEXT,
    trip_days      INT,
    itinerary_data JSONB       NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_saved_itin_user ON saved_itineraries(user_id);
