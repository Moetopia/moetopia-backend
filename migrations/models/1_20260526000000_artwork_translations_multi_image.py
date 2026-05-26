from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> None:
    await db.execute_script("""
        ALTER TABLE "artwork_translations" ADD COLUMN IF NOT EXISTS "image_index" INT NOT NULL DEFAULT 0;
        ALTER TABLE "artwork_translations" ADD COLUMN IF NOT EXISTS "is_manual" BOOL NOT NULL DEFAULT false;
        ALTER TABLE "artwork_translations" DROP CONSTRAINT IF EXISTS "uid_artwork_tra_artwork_65573f";
        ALTER TABLE "artwork_translations" ADD CONSTRAINT "uid_artwork_tra_artwork_img_idx" UNIQUE ("artwork_id", "target_lang", "image_index");
        COMMENT ON TABLE "artwork_translations" IS '翻译结果缓存，按 (artwork_id, target_lang, image_index) 唯一';
    """)


async def downgrade(db: BaseDBAsyncClient) -> None:
    await db.execute_script("""
        ALTER TABLE "artwork_translations" DROP CONSTRAINT IF EXISTS "uid_artwork_tra_artwork_img_idx";
        ALTER TABLE "artwork_translations" ADD CONSTRAINT "uid_artwork_tra_artwork_65573f" UNIQUE ("artwork_id", "target_lang");
        ALTER TABLE "artwork_translations" DROP COLUMN IF EXISTS "is_manual";
        ALTER TABLE "artwork_translations" DROP COLUMN IF EXISTS "image_index";
    """)
