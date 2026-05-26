from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "captcha_questions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "question" TEXT NOT NULL,
    "question_type" VARCHAR(20) NOT NULL DEFAULT 'text',
    "answer" VARCHAR(200) NOT NULL DEFAULT '',
    "choices" JSONB,
    "tile_images" JSONB,
    "correct_indices" JSONB,
    "hint_image" VARCHAR(500),
    "tile_rows" INT NOT NULL DEFAULT 3,
    "tile_cols" INT NOT NULL DEFAULT 3,
    "is_active" BOOL NOT NULL DEFAULT True,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE "captcha_questions" IS '自定义验证码题库';
CREATE TABLE IF NOT EXISTS "concept_anchors" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tag_name" VARCHAR(100) NOT NULL UNIQUE,
    "namespace" VARCHAR(50) NOT NULL DEFAULT 'general'
);
CREATE INDEX IF NOT EXISTS "idx_concept_anc_tag_nam_cf10ad" ON "concept_anchors" ("tag_name");
COMMENT ON TABLE "concept_anchors" IS 'AI 概念基准库：专门存你官方图的特征，物理隔离防污染';
CREATE TABLE IF NOT EXISTS "membership_plans" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "description" TEXT NOT NULL,
    "monthly_price" DECIMAL(10,2) NOT NULL,
    "yearly_price" DECIMAL(10,2),
    "permissions" JSONB NOT NULL,
    "is_active" BOOL NOT NULL DEFAULT True,
    "sort_order" INT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE "membership_plans" IS '会员档位（由管理员动态配置）';
CREATE TABLE IF NOT EXISTS "site_configs" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "key" VARCHAR(100) NOT NULL UNIQUE,
    "value" JSONB
);
CREATE INDEX IF NOT EXISTS "idx_site_config_key_186b69" ON "site_configs" ("key");
COMMENT ON TABLE "site_configs" IS '全站配置 — 键值对形式，支持 JSON 值';
CREATE TABLE IF NOT EXISTS "users" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "login_id" VARCHAR(50) UNIQUE,
    "username" VARCHAR(50) NOT NULL,
    "email" VARCHAR(255) NOT NULL UNIQUE,
    "password_hash" VARCHAR(255) NOT NULL,
    "login_id_changed_at" TIMESTAMPTZ,
    "avatar_url" VARCHAR(500),
    "background_url" VARCHAR(500),
    "bio" TEXT,
    "website_url" VARCHAR(500),
    "twitter_url" VARCHAR(500),
    "gender" VARCHAR(10),
    "birth_year" INT,
    "birth_month" INT,
    "birth_day" INT,
    "location" VARCHAR(100),
    "occupation" VARCHAR(100),
    "social_links" JSONB NOT NULL,
    "role" VARCHAR(20) NOT NULL DEFAULT 'user',
    "r18_enabled" BOOL NOT NULL DEFAULT False,
    "hide_ai_generated" BOOL NOT NULL DEFAULT False,
    "muted_tags" JSONB NOT NULL,
    "muted_user_ids" JSONB NOT NULL,
    "show_likes_public" BOOL NOT NULL DEFAULT True,
    "show_followers_public" BOOL NOT NULL DEFAULT True,
    "show_following_public" BOOL NOT NULL DEFAULT True,
    "is_creator" BOOL NOT NULL DEFAULT False,
    "commission_enabled" BOOL NOT NULL DEFAULT False,
    "commission_info" TEXT,
    "commission_max_revisions" INT NOT NULL DEFAULT 3,
    "notification_prefs" JSONB NOT NULL,
    "is_banned" BOOL NOT NULL DEFAULT False,
    "banned_reason" VARCHAR(500),
    "banned_at" TIMESTAMPTZ,
    "token_version" INT NOT NULL DEFAULT 0,
    "preferred_translation_lang" VARCHAR(10),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_users_login_i_c08925" ON "users" ("login_id");
CREATE INDEX IF NOT EXISTS "idx_users_usernam_266d85" ON "users" ("username");
CREATE INDEX IF NOT EXISTS "idx_users_email_133a6f" ON "users" ("email");
CREATE TABLE IF NOT EXISTS "announcements" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL,
    "content" TEXT NOT NULL,
    "cover_image" VARCHAR(500),
    "category" VARCHAR(50) NOT NULL DEFAULT 'notice',
    "is_pinned" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "author_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "announcements" IS '站内公告 / 文章（仅 admin 可发布）';
CREATE TABLE IF NOT EXISTS "artworks" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL,
    "description" TEXT,
    "artwork_type" VARCHAR(20) NOT NULL DEFAULT 'illustration',
    "is_ai" BOOL NOT NULL DEFAULT False,
    "rating" VARCHAR(20) NOT NULL DEFAULT 'safe',
    "visibility" VARCHAR(20) NOT NULL DEFAULT 'public',
    "view_count" INT NOT NULL DEFAULT 0,
    "like_count" INT NOT NULL DEFAULT 0,
    "bookmark_count" INT NOT NULL DEFAULT 0,
    "allow_ai_tagging" BOOL NOT NULL DEFAULT True,
    "allow_community_tagging" BOOL NOT NULL DEFAULT True,
    "content_origin" VARCHAR(20) NOT NULL DEFAULT 'original',
    "moderation_status" VARCHAR(20) NOT NULL DEFAULT 'approved',
    "pixiv_id" INT,
    "source" VARCHAR(500),
    "original_author_name" VARCHAR(200),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "author_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_artworks_pixiv_i_6f7b04" ON "artworks" ("pixiv_id");
CREATE TABLE IF NOT EXISTS "artwork_images" (
    "id" UUID NOT NULL PRIMARY KEY,
    "file_url" VARCHAR(500) NOT NULL,
    "width" INT,
    "height" INT,
    "sort_order" INT NOT NULL DEFAULT 0,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS "artwork_reports" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "reason" VARCHAR(50) NOT NULL,
    "description" TEXT,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewed_at" TIMESTAMPTZ,
    "admin_note" TEXT,
    "appeal_text" TEXT,
    "appeal_submitted_at" TIMESTAMPTZ,
    "appeal_status" VARCHAR(20),
    "appeal_reviewed_at" TIMESTAMPTZ,
    "appeal_note" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "appeal_reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "reporter_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_artwork_rep_reporte_757ce9" UNIQUE ("reporter_id", "artwork_id")
);
COMMENT ON TABLE "artwork_reports" IS '作品举报';
CREATE TABLE IF NOT EXISTS "artwork_series" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL,
    "description" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "author_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "cover_artwork_id" INT REFERENCES "artworks" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "artwork_series" IS '作品系列（漫画/连载作品集）';
CREATE TABLE IF NOT EXISTS "artwork_series_items" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "order" INT NOT NULL DEFAULT 0,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "series_id" INT NOT NULL REFERENCES "artwork_series" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_artwork_ser_series__7a1c08" UNIQUE ("series_id", "artwork_id")
);
COMMENT ON TABLE "artwork_series_items" IS '系列中的单个作品项';
CREATE TABLE IF NOT EXISTS "artwork_tags" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tag_name" VARCHAR(100) NOT NULL,
    "type" VARCHAR(50) NOT NULL DEFAULT 'author',
    "confidence" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "upvotes" INT NOT NULL DEFAULT 0,
    "downvotes" INT NOT NULL DEFAULT 0,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_artwork_tag_artwork_55dbcd" UNIQUE ("artwork_id", "tag_name")
);
CREATE INDEX IF NOT EXISTS "idx_artwork_tag_tag_nam_059455" ON "artwork_tags" ("tag_name");
CREATE TABLE IF NOT EXISTS "artwork_translations" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "target_lang" VARCHAR(10) NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "translated_image_url" VARCHAR(500),
    "error_msg" VARCHAR(500),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "requested_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_artwork_tra_artwork_65573f" UNIQUE ("artwork_id", "target_lang")
);
COMMENT ON TABLE "artwork_translations" IS '翻译结果缓存，按 (artwork_id, target_lang) 唯一';
CREATE TABLE IF NOT EXISTS "bookmark_folders" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "is_private" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "cover_artwork_id" INT REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "bookmark_folders" IS '收藏夹（类 Pixiv 收藏集）';
CREATE TABLE IF NOT EXISTS "bookmarks" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "is_private" BOOL NOT NULL DEFAULT False,
    "user_custom_tags" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "folder_id" INT REFERENCES "bookmark_folders" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_bookmarks_user_id_16834a" UNIQUE ("user_id", "artwork_id")
);
CREATE TABLE IF NOT EXISTS "comments" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "content" TEXT NOT NULL,
    "is_deleted" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "parent_id" INT REFERENCES "comments" ("id") ON DELETE CASCADE,
    "reply_to_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS "commission_tiers" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(100) NOT NULL,
    "description" TEXT,
    "price" DECIMAL(10,2) NOT NULL,
    "allow_custom_amount" BOOL NOT NULL DEFAULT False,
    "min_custom_amount" DECIMAL(10,2),
    "sort_order" INT NOT NULL DEFAULT 0,
    "is_active" BOOL NOT NULL DEFAULT True,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "creator_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "commission_tiers" IS '画师约稿档位：每个认证画师可创建多个固定价位套餐，';
CREATE TABLE IF NOT EXISTS "commissions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "title" VARCHAR(200) NOT NULL,
    "description" TEXT NOT NULL,
    "price" DECIMAL(10,2) NOT NULL,
    "status" VARCHAR(50) NOT NULL DEFAULT 'pending',
    "payment_status" VARCHAR(20) NOT NULL DEFAULT 'unpaid',
    "max_revisions" INT NOT NULL DEFAULT 3,
    "deadline" TIMESTAMPTZ,
    "delivered_artwork_id" INT,
    "delivered_file_url" VARCHAR(500),
    "delivered_file_name" VARCHAR(500),
    "creator_note" TEXT,
    "cancelled_reason" TEXT,
    "terminated_by" VARCHAR(20),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "client_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "creator_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "tier_id" INT REFERENCES "commission_tiers" ("id") ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS "commission_reviews" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "rating" INT NOT NULL,
    "comment" TEXT,
    "is_anonymous" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "commission_id" INT NOT NULL REFERENCES "commissions" ("id") ON DELETE CASCADE,
    "creator_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reviewer_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "commission_reviews" IS '客户对约稿的评价/评分';
CREATE TABLE IF NOT EXISTS "commission_revisions" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "description" TEXT NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "creator_reply" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "commission_id" INT NOT NULL REFERENCES "commissions" ("id") ON DELETE CASCADE,
    "requested_by_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "commission_revisions" IS '客户申请的约稿修改记录';
CREATE TABLE IF NOT EXISTS "creator_applications" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "portfolio_url" VARCHAR(500),
    "reason" TEXT NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewed_at" TIMESTAMPTZ,
    "review_note" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "applicant_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "creator_applications" IS '用户申请成为认证画师的记录';
CREATE TABLE IF NOT EXISTS "direct_messages" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "content" TEXT,
    "image_url" VARCHAR(500),
    "is_read" BOOL NOT NULL DEFAULT False,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "commission_id" INT REFERENCES "commissions" ("id") ON DELETE CASCADE,
    "recipient_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "sender_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS "follows" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "followed_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "follower_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_follows_followe_fa2a56" UNIQUE ("follower_id", "followed_id")
);
CREATE TABLE IF NOT EXISTS "follow_tags" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tag_name" VARCHAR(100) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_follow_tags_user_id_236cf6" UNIQUE ("user_id", "tag_name")
);
CREATE INDEX IF NOT EXISTS "idx_follow_tags_tag_nam_b953e0" ON "follow_tags" ("tag_name");
COMMENT ON TABLE "follow_tags" IS '关注标签（类 Pixiv 标签订阅）';
CREATE TABLE IF NOT EXISTS "likes" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_likes_user_id_a6f03b" UNIQUE ("user_id", "artwork_id")
);
CREATE TABLE IF NOT EXISTS "moderation_queue" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "reason" VARCHAR(30) NOT NULL,
    "confidence" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "duplicate_of_artwork_id" INT,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewer_note" TEXT,
    "reviewed_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "reviewer_id" INT REFERENCES "users" ("id") ON DELETE SET NULL
);
COMMENT ON TABLE "moderation_queue" IS '内容审核队列 — 存储疑似违规作品，等待人工复核';
CREATE TABLE IF NOT EXISTS "notifications" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "type" VARCHAR(50) NOT NULL,
    "content" TEXT NOT NULL,
    "is_read" BOOL NOT NULL DEFAULT False,
    "related_entity_id" VARCHAR(255),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "actor_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS "password_reset_tokens" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "token" VARCHAR(64) NOT NULL UNIQUE,
    "expires_at" TIMESTAMPTZ NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS "idx_password_re_token_3c8a31" ON "password_reset_tokens" ("token");
COMMENT ON TABLE "password_reset_tokens" IS '密码重置令牌（有效期 1 小时，使用后立即删除）';
CREATE TABLE IF NOT EXISTS "payment_records" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "amount" DECIMAL(10,2) NOT NULL,
    "method" VARCHAR(20) NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "transaction_id" VARCHAR(100),
    "paid_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "commission_id" INT NOT NULL REFERENCES "commissions" ("id") ON DELETE CASCADE,
    "payer_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "payment_records" IS '约稿支付记录（演示级实现，不接入真实支付网关）';
CREATE TABLE IF NOT EXISTS "series_follows" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "notify" BOOL NOT NULL DEFAULT True,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "series_id" INT NOT NULL REFERENCES "artwork_series" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_series_foll_user_id_4dc1f6" UNIQUE ("user_id", "series_id")
);
COMMENT ON TABLE "series_follows" IS '用户追更系列';
CREATE TABLE IF NOT EXISTS "style_references" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(100) NOT NULL,
    "description" TEXT,
    "file_url" VARCHAR(500) NOT NULL,
    "qdrant_id" VARCHAR(100) NOT NULL UNIQUE,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "work_name" VARCHAR(100),
    "faction_name" VARCHAR(100),
    "character_name" VARCHAR(100),
    "tags" JSONB,
    "similarity_threshold" DOUBLE PRECISION NOT NULL DEFAULT 0.75,
    "uploaded_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "style_references" IS '管理员上传的锚点基准图（存入 Qdrant style_refs 集合）';
CREATE TABLE IF NOT EXISTS "tag_translations" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "tag_name" VARCHAR(100) NOT NULL,
    "locale" VARCHAR(10) NOT NULL,
    "translated_name" VARCHAR(200) NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "approved_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "approved_by_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    "submitted_by_id" INT REFERENCES "users" ("id") ON DELETE SET NULL,
    CONSTRAINT "uid_tag_transla_tag_nam_326eb0" UNIQUE ("tag_name", "locale")
);
CREATE INDEX IF NOT EXISTS "idx_tag_transla_tag_nam_a6481e" ON "tag_translations" ("tag_name");
COMMENT ON TABLE "tag_translations" IS '标签多语言翻译（支持社区提交 + 管理员审批 + Crowdin 导出/导入）';
CREATE TABLE IF NOT EXISTS "tag_validator_applications" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "reason" TEXT NOT NULL,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewed_at" TIMESTAMPTZ,
    "review_note" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "applicant_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "tag_validator_applications" IS '用户申请成为 tag_validator 的申请记录';
CREATE TABLE IF NOT EXISTS "tag_votes" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "is_upvote" BOOL NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_tag_id" INT NOT NULL REFERENCES "artwork_tags" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_tag_votes_user_id_b87e8f" UNIQUE ("user_id", "artwork_tag_id")
);
CREATE TABLE IF NOT EXISTS "user_blocks" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "blocked_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "blocker_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_user_blocks_blocker_1f24c1" UNIQUE ("blocker_id", "blocked_id")
);
COMMENT ON TABLE "user_blocks" IS '用户拉黑';
CREATE TABLE IF NOT EXISTS "user_memberships" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "status" VARCHAR(20) NOT NULL DEFAULT 'active',
    "started_at" TIMESTAMPTZ NOT NULL,
    "expires_at" TIMESTAMPTZ NOT NULL,
    "payment_ref" VARCHAR(200),
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "plan_id" INT NOT NULL REFERENCES "membership_plans" ("id") ON DELETE CASCADE,
    "user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE
);
COMMENT ON TABLE "user_memberships" IS '用户会员订阅记录';
CREATE TABLE IF NOT EXISTS "user_reports" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "reason" VARCHAR(50) NOT NULL,
    "description" TEXT,
    "status" VARCHAR(20) NOT NULL DEFAULT 'pending',
    "reviewed_at" TIMESTAMPTZ,
    "admin_note" TEXT,
    "appeal_text" TEXT,
    "appeal_submitted_at" TIMESTAMPTZ,
    "appeal_status" VARCHAR(20),
    "appeal_reviewed_at" TIMESTAMPTZ,
    "appeal_note" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "appeal_reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    "reported_user_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reporter_id" INT NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    "reviewed_by_id" INT REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_user_report_reporte_826934" UNIQUE ("reporter_id", "reported_user_id")
);
COMMENT ON TABLE "user_reports" IS '用户举报';
CREATE TABLE IF NOT EXISTS "view_histories" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "ip_address" VARCHAR(45),
    "viewed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "artwork_id" INT NOT NULL REFERENCES "artworks" ("id") ON DELETE CASCADE,
    "user_id" INT REFERENCES "users" ("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS "aerich" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSONB NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "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"
)
