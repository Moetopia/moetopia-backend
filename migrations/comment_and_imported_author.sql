-- ============================================================
-- Migration: comment_and_imported_author
-- Adds: comment like_count, comment_likes table,
--       imported author fields on users,
--       account_claim_requests table
-- ============================================================

-- 1. 评论点赞计数字段
ALTER TABLE comments ADD COLUMN IF NOT EXISTS like_count INTEGER NOT NULL DEFAULT 0;

-- 2. 评论点赞记录表
CREATE TABLE IF NOT EXISTS comment_likes (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    comment_id  INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comment_likes_comment ON comment_likes(comment_id);

-- 3. 用户表：导入账号字段
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_imported      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS pixiv_user_id    BIGINT  DEFAULT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS source_platform  VARCHAR(20) DEFAULT NULL;

-- 唯一索引（允许 NULL）
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_pixiv_user_id ON users(pixiv_user_id) WHERE pixiv_user_id IS NOT NULL;

-- 4. 账号认领申请表
CREATE TABLE IF NOT EXISTS account_claim_requests (
    id               SERIAL PRIMARY KEY,
    imported_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    claimant_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
    admin_note       TEXT        DEFAULT NULL,
    created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMP WITH TIME ZONE DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_acr_imported_user ON account_claim_requests(imported_user_id);
CREATE INDEX IF NOT EXISTS idx_acr_claimant      ON account_claim_requests(claimant_id);
CREATE INDEX IF NOT EXISTS idx_acr_status        ON account_claim_requests(status);
