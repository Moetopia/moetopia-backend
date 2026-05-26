"""
回填脚本：为迁移前上传的图片计算 pHash 并写入数据库。
幂等操作，仅处理 phash IS NULL 的记录，可重复运行。
"""
import asyncio
import sys
import os
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tortoise import Tortoise
from app.core.config import settings

UPLOAD_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads", "artworks")


def compute_phash(file_path: str):
    try:
        import imagehash
        from PIL import Image as PILImage
        with PILImage.open(file_path) as img:
            img = img.convert("RGB")
            return str(imagehash.phash(img, hash_size=8))
    except Exception as e:
        print(f"  ⚠️  pHash 失败: {e}")
        return None


async def run():
    await Tortoise.init(
        db_url=settings.DATABASE_URL,
        modules={"models": ["app.models"]},
    )

    from app.models.artwork import ArtworkImage

    # 只处理 phash=NULL 的记录
    null_records = await ArtworkImage.filter(phash__isnull=True).values("id", "file_url")
    total = len(null_records)
    print(f"📊 共 {total} 条图片需要补充 pHash")

    ok = 0
    fail = 0
    for row in null_records:
        # file_url 示例: /uploads/artworks/123_0.jpg
        rel_path = row["file_url"].lstrip("/")
        full_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel_path)

        if not os.path.exists(full_path):
            print(f"  ❌ 文件不存在: {full_path}")
            fail += 1
            continue

        ph = await asyncio.to_thread(compute_phash, full_path)
        if ph:
            await ArtworkImage.filter(id=row["id"]).update(phash=ph)
            ok += 1
            if ok % 50 == 0:
                print(f"  ✅ 已处理 {ok}/{total}")
        else:
            fail += 1

    await Tortoise.close_connections()
    print(f"\n🎉 回填完成: 成功={ok}, 失败={fail}")


if __name__ == "__main__":
    asyncio.run(run())
