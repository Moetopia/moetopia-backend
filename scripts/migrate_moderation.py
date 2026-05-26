"""
迁移脚本：添加 phash / moderation_status 字段 + 创建 moderation_queue 表
幂等操作，重复执行无副作用。
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tortoise import Tortoise
from app.core.config import settings


async def run():
    await Tortoise.init(
        db_url=settings.DATABASE_URL,
        modules={"models": ["app.models"]},
    )
    conn = Tortoise.get_connection("default")

    # 1. artwork_images.phash
    await conn.execute_script("""
        ALTER TABLE artwork_images
        ADD COLUMN IF NOT EXISTS phash VARCHAR(16);
    """)
    await conn.execute_script("""
        CREATE INDEX IF NOT EXISTS idx_artwork_images_phash ON artwork_images(phash);
    """)
    print("✅ artwork_images.phash 已就绪")

    # 2. artworks.moderation_status
    await conn.execute_script("""
        ALTER TABLE artworks
        ADD COLUMN IF NOT EXISTS moderation_status VARCHAR(20) NOT NULL DEFAULT 'approved';
    """)
    print("✅ artworks.moderation_status 已就绪")

    # 3. moderation_queue 表（新表，generate_schemas 会建，此处保底）
    await conn.execute_script("""
        CREATE TABLE IF NOT EXISTS moderation_queue (
            id                      SERIAL PRIMARY KEY,
            artwork_id              INTEGER NOT NULL REFERENCES artworks(id) ON DELETE CASCADE,
            reason                  VARCHAR(30) NOT NULL,
            confidence              DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            duplicate_of_artwork_id INTEGER,
            status                  VARCHAR(20) NOT NULL DEFAULT 'pending',
            reviewer_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewer_note           TEXT,
            reviewed_at             TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    await conn.execute_script("""
        CREATE INDEX IF NOT EXISTS idx_moderation_queue_artwork  ON moderation_queue(artwork_id);
        CREATE INDEX IF NOT EXISTS idx_moderation_queue_status   ON moderation_queue(status);
    """)
    print("✅ moderation_queue 表已就绪")

    await Tortoise.close_connections()
    print("🎉 迁移完成")


if __name__ == "__main__":
    asyncio.run(run())
