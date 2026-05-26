"""
迁移脚本：artwork_translations 表添加 image_index / is_manual 字段，
并将唯一约束从 (artwork_id, target_lang) 更新为 (artwork_id, target_lang, image_index)。
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

    await conn.execute_script("""
        ALTER TABLE "artwork_translations"
        ADD COLUMN IF NOT EXISTS "image_index" INT NOT NULL DEFAULT 0;
    """)
    print("✅ image_index 列已就绪")

    await conn.execute_script("""
        ALTER TABLE "artwork_translations"
        ADD COLUMN IF NOT EXISTS "is_manual" BOOL NOT NULL DEFAULT false;
    """)
    print("✅ is_manual 列已就绪")

    await conn.execute_script("""
        ALTER TABLE "artwork_translations"
        DROP CONSTRAINT IF EXISTS "uid_artwork_tra_artwork_65573f";
    """)
    print("✅ 旧唯一约束 (artwork_id, target_lang) 已移除")

    await conn.execute_script("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uid_artwork_tra_artwork_img_idx'
            ) THEN
                ALTER TABLE "artwork_translations"
                ADD CONSTRAINT "uid_artwork_tra_artwork_img_idx"
                UNIQUE ("artwork_id", "target_lang", "image_index");
            END IF;
        END $$;
    """)
    print("✅ 新唯一约束 (artwork_id, target_lang, image_index) 已添加")

    await Tortoise.close_connections()
    print("🎉 迁移完成")


if __name__ == "__main__":
    asyncio.run(run())
