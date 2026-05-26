-- Migration: Follow groups + Scheduled posts
-- Run manually: psql $DATABASE_URL -f this_file.sql

-- Follow Groups
CREATE TABLE IF NOT EXISTS follow_groups (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(50) NOT NULL,
    sort_order  INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_follow_groups_user ON follow_groups(user_id);

-- Follow Group Members
CREATE TABLE IF NOT EXISTS follow_group_members (
    id          SERIAL PRIMARY KEY,
    group_id    INT NOT NULL REFERENCES follow_groups(id) ON DELETE CASCADE,
    followed_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (group_id, followed_id)
);

CREATE INDEX IF NOT EXISTS idx_fgm_group    ON follow_group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_fgm_followed ON follow_group_members(followed_id);

-- Scheduled posts: add column to artworks
ALTER TABLE artworks ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_artworks_scheduled ON artworks(visibility, scheduled_at)
    WHERE visibility = 'scheduled';

-- Multi-period membership pricing
ALTER TABLE membership_plans ADD COLUMN IF NOT EXISTS quarterly_price   NUMERIC(10,2) DEFAULT NULL;
ALTER TABLE membership_plans ADD COLUMN IF NOT EXISTS semi_annual_price NUMERIC(10,2) DEFAULT NULL;

-- Image compression: original_url stores full-res file; file_url stores ≤1200px display version
ALTER TABLE artwork_images ADD COLUMN IF NOT EXISTS original_url VARCHAR(500);

-- Private follows: is_private flag (普通用户≤100，会员≤1000)
ALTER TABLE follows ADD COLUMN IF NOT EXISTS is_private BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_follows_private ON follows(follower_id, is_private);
