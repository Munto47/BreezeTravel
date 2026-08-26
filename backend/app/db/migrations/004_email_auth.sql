-- P1-3（2026-05）：邮箱+密码登录作为短信兜底通道
-- 短信服务故障时仍可登录；运营商日级流控触顶时新用户仍可注册

ALTER TABLE users ADD COLUMN IF NOT EXISTS email          VARCHAR(255) UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash  TEXT;

-- 邮箱索引（登录时按邮箱查用户）
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
