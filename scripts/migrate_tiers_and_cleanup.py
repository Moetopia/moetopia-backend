"""
迁移脚本：
1. 创建 commission_tiers 表
2. 为 commissions 表添加 tier_id 列
3. 删除 artwork_images 表中的 phash 列（撞车检测已迁移至 Qdrant 向量相似度）
幂等操作，可重复运行。
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

    # 1. commission_tiers 表
    await conn.execute_script("""
        CREATE TABLE IF NOT EXISTS commission_tiers (
            id                  SERIAL PRIMARY KEY,
            creator_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title               VARCHAR(100) NOT NULL,
            description         TEXT,
            price               NUMERIC(10, 2) NOT NULL,
            allow_custom_amount BOOLEAN NOT NULL DEFAULT FALSE,
            min_custom_amount   NUMERIC(10, 2),
            sort_order          INTEGER NOT NULL DEFAULT 0,
            is_active           BOOLEAN NOT NULL DEFAULT TRUE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_commission_tiers_creator ON commission_tiers(creator_id);
    """)
    print("✅ commission_tiers 表已就绪")

    # 2. commissions.tier_id 列
    await conn.execute_script("""
        ALTER TABLE commissions
            ADD COLUMN IF NOT EXISTS tier_id INTEGER REFERENCES commission_tiers(id) ON DELETE SET NULL;
    """)
    print("✅ commissions.tier_id 列已就绪")

    # 3. 删除 artwork_images.phash 列（Qdrant 向量相似度替代 pHash）
    await conn.execute_script("""
        ALTER TABLE artwork_images DROP COLUMN IF EXISTS phash;
    """)
    print("✅ artwork_images.phash 列已删除")

    await Tortoise.close_connections()
    print("\n🎉 迁移完成")


if __name__ == "__main__":
    asyncio.run(run())
