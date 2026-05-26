-- Pixiv 分布式同步系统表
-- 已通过 generate_schemas=True 自动创建（无需手动执行）
-- 仅作记录与手动恢复参考
-- ⚠️  pixiv_artwork_cache 是持久化缓存表，清库时请先备份：
--     pg_dump -t pixiv_artwork_cache moetopia > pixiv_cache_backup.sql

CREATE TABLE IF NOT EXISTS "pixiv_sync_nodes" (
    "id"           SERIAL NOT NULL PRIMARY KEY,
    "name"         VARCHAR(100) NOT NULL,
    "url"          VARCHAR(500) NOT NULL,
    "api_key"      VARCHAR(256) NOT NULL,
    "status"       VARCHAR(20) NOT NULL DEFAULT 'online',
    "last_ping"    TIMESTAMPTZ,
    "author_count" INT NOT NULL DEFAULT 0,
    "note"         TEXT,
    "created_at"   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS "pixiv_sync_authors" (
    "id"               SERIAL NOT NULL PRIMARY KEY,
    "pixiv_user_id"    BIGINT NOT NULL UNIQUE,
    "pixiv_username"   VARCHAR(100),
    "assigned_node_id" INT REFERENCES "pixiv_sync_nodes"("id") ON DELETE SET NULL,
    "moetopia_user_id" INT,
    "sync_enabled"     BOOL NOT NULL DEFAULT TRUE,
    "claimed"          BOOL NOT NULL DEFAULT FALSE,
    "last_sync_at"     TIMESTAMPTZ,
    "artwork_count"    INT NOT NULL DEFAULT 0,
    "status"           VARCHAR(20) NOT NULL DEFAULT 'pending',
    "error_msg"        TEXT,
    "created_at"       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS "idx_pixiv_sync_authors_node"
    ON "pixiv_sync_authors"("assigned_node_id");

CREATE TABLE IF NOT EXISTS "pixiv_sync_submissions" (
    "id"              SERIAL NOT NULL PRIMARY KEY,
    "submitter_id"    INT NOT NULL REFERENCES "users"("id") ON DELETE CASCADE,
    "pixiv_user_id"   BIGINT NOT NULL,
    "pixiv_username"  VARCHAR(100),
    "reason"          TEXT,
    "status"          VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewed_by_id"  INT,
    "admin_note"      TEXT,
    "created_at"      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "resolved_at"     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS "idx_pixiv_sync_submissions_status"
    ON "pixiv_sync_submissions"("status");

-- ⚠️ 此表为持久化缓存，清库时单独备份
CREATE TABLE IF NOT EXISTS "pixiv_artwork_cache" (
    "id"                   SERIAL NOT NULL PRIMARY KEY,
    "pixiv_id"             BIGINT NOT NULL UNIQUE,
    "pixiv_user_id"        BIGINT NOT NULL,
    "node_name"            VARCHAR(100),
    "metadata"             JSONB NOT NULL DEFAULT '{}',
    "image_original_urls"  JSONB NOT NULL DEFAULT '[]',
    "image_local_paths"    JSONB NOT NULL DEFAULT '[]',
    "imported"             BOOL NOT NULL DEFAULT FALSE,
    "moetopia_artwork_id"  INT,
    "created_at"           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS "idx_pixiv_artwork_cache_user"
    ON "pixiv_artwork_cache"("pixiv_user_id");
CREATE INDEX IF NOT EXISTS "idx_pixiv_artwork_cache_imported"
    ON "pixiv_artwork_cache"("imported");

COMMENT ON TABLE "pixiv_artwork_cache" IS '⚠️ 持久化缓存：清库时请先 pg_dump -t pixiv_artwork_cache';
