"""
补充 aerich init-db 之前漏掉的列（因 generate_schemas 不做 ALTER TABLE）。
运行一次即可，之后用 aerich migrate / upgrade 管理后续变更。
"""
import asyncio
import os
from pathlib import Path

# 从 .env 加载环境变量
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(env_path)

import asyncpg


MIGRATIONS = [
    # users 表 — preferred_translation_lang
    'ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "preferred_translation_lang" VARCHAR(10)',
    # membership_plans 表（若 init-db 未创建）
    """
    CREATE TABLE IF NOT EXISTS "membership_plan" (
        "id"            SERIAL PRIMARY KEY,
        "name"          VARCHAR(64) NOT NULL,
        "description"   TEXT,
        "monthly_price" DECIMAL(10,2) NOT NULL DEFAULT 0,
        "yearly_price"  DECIMAL(10,2),
        "permissions"   JSONB NOT NULL DEFAULT '{}',
        "is_active"     BOOLEAN NOT NULL DEFAULT TRUE,
        "sort_order"    INT NOT NULL DEFAULT 0,
        "created_at"    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # user_memberships 表
    """
    CREATE TABLE IF NOT EXISTS "user_membership" (
        "id"             SERIAL PRIMARY KEY,
        "user_id"        INT NOT NULL REFERENCES "users"("id") ON DELETE CASCADE,
        "plan_id"        INT NOT NULL REFERENCES "membership_plan"("id") ON DELETE CASCADE,
        "status"         VARCHAR(20) NOT NULL DEFAULT 'active',
        "period"         VARCHAR(10) NOT NULL DEFAULT 'monthly',
        "started_at"     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "expires_at"     TIMESTAMPTZ NOT NULL,
        "payment_ref"    VARCHAR(128),
        "created_at"     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # artwork_translations 表
    """
    CREATE TABLE IF NOT EXISTS "artwork_translations" (
        "id"                    SERIAL PRIMARY KEY,
        "artwork_id"            INT NOT NULL REFERENCES "artworks"("id") ON DELETE CASCADE,
        "target_lang"           VARCHAR(10) NOT NULL,
        "status"                VARCHAR(20) NOT NULL DEFAULT 'pending',
        "translated_image_url"  TEXT,
        "error_msg"             VARCHAR(512),
        "requested_by_id"       INT REFERENCES "users"("id") ON DELETE SET NULL,
        "created_at"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        "updated_at"            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE ("artwork_id", "target_lang")
    )
    """,
]


async def main() -> None:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")

    conn = await asyncpg.connect(url)
    try:
        for sql in MIGRATIONS:
            sql = sql.strip()
            if not sql:
                continue
            try:
                await conn.execute(sql)
                print(f"OK: {sql[:60].replace(chr(10), ' ')}…")
            except Exception as e:
                print(f"SKIP/ERR: {e}")
    finally:
        await conn.close()
    print("\n✅ 全部完成")


if __name__ == "__main__":
    asyncio.run(main())
