"""
作品翻译 API
- POST /api/v1/artworks/{id}/translate   需登录，需会员翻译权限
- GET  /api/v1/artworks/{id}/translations 公开
"""
from datetime import datetime, timezone
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from pydantic import BaseModel

from app.api.dependencies import get_current_user, get_optional_user
from app.models.user import User
from app.models.artwork import Artwork
from app.models.artwork_translation import ArtworkTranslation
from app.models.membership_plan import MembershipPlan
from app.schemas.common import ResponseBase

router = APIRouter()


async def _check_translation_enabled() -> None:
    """检查翻译总开关，未启用则抛 503。优先读缓存，缓存 miss 则查 DB。"""
    try:
        from app.infrastructure.cache import cache_get, cache_set, TTL_SITE_CONFIG
        cfg = await cache_get("site_config")
        if cfg is None:
            from app.models.site_config import SiteConfig
            rows = await SiteConfig.all().values("key", "value")
            cfg = {r["key"]: r["value"] for r in rows}
            if cfg:
                await cache_set("site_config", cfg, TTL_SITE_CONFIG)
        if not (cfg or {}).get("translation_enabled", False):
            raise HTTPException(status_code=503, detail="翻译功能暂未启用")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="翻译功能暂未启用")


async def _check_translation_permission(user: User) -> None:
    """检查用户会员翻译权限，无权限返回 403 + requires_membership。"""
    from app.api.v1.membership import get_active_membership
    sub = await get_active_membership(user)
    if sub and sub.plan.permissions.get("translation"):
        return
    # 无权限：返回可订阅的档位列表
    plans = await MembershipPlan.filter(is_active=True).order_by("sort_order", "id")
    plans_data = [
        {"id": p.id, "name": p.name, "monthly_price": float(p.monthly_price)}
        for p in plans
        if p.permissions.get("translation")
    ]
    raise HTTPException(
        status_code=403,
        detail={
            "code": "requires_membership",
            "message": "翻译功能需要会员权限",
            "plans": plans_data,
        },
    )


class TranslateRequest(BaseModel):
    target_lang: str
    save_as_default: bool = True


@router.post("/{artwork_id}/translate", response_model=ResponseBase[dict])
async def request_translation(
    artwork_id: int,
    body: TranslateRequest,
    current_user: User = Depends(get_current_user),
):
    """触发翻译任务。已有 done 记录则直接返回，否则入队。"""
    await _check_translation_enabled()
    await _check_translation_permission(current_user)

    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")

    target_lang = body.target_lang.upper()

    # 保存默认语言偏好
    if body.save_as_default and current_user.preferred_translation_lang != target_lang:
        current_user.preferred_translation_lang = target_lang
        await current_user.save(update_fields=["preferred_translation_lang"])

    from app.models.artwork import ArtworkImage
    from app.worker.enqueue import enqueue

    all_images = await ArtworkImage.filter(artwork_id=artwork_id).order_by("sort_order", "id")
    if not all_images:
        raise HTTPException(status_code=400, detail="作品没有图片")

    done_count = 0
    first_done_url = None

    for i, _ in enumerate(all_images):
        existing = await ArtworkTranslation.get_or_none(
            artwork_id=artwork_id, target_lang=target_lang, image_index=i
        )
        if existing:
            if existing.status == "done":
                done_count += 1
                if i == 0:
                    first_done_url = existing.translated_image_url
                continue
            if existing.status in ("pending", "processing"):
                continue
            # failed → 重置并重新排队
            existing.status = "pending"
            existing.error_msg = None
            existing.requested_by_id = current_user.id
            await existing.save(update_fields=["status", "error_msg", "requested_by_id"])
            await enqueue("task_translate_artwork", translation_id=existing.id)
        else:
            record = await ArtworkTranslation.create(
                artwork_id=artwork_id,
                target_lang=target_lang,
                image_index=i,
                status="pending",
                requested_by_id=current_user.id,
            )
            await enqueue("task_translate_artwork", translation_id=record.id)

    total = len(all_images)
    if done_count == total:
        return ResponseBase(data={
            "status": "done",
            "translated_image_url": first_done_url,
            "target_lang": target_lang,
            "image_count": total,
        })
    return ResponseBase(data={"status": "pending", "target_lang": target_lang, "image_count": total})


@router.get("/{artwork_id}/translations", response_model=ResponseBase[list])
async def list_translations(artwork_id: int):
    """公开：返回作品所有翻译记录（含状态），让前端可感知 pending/processing/failed。"""
    records = await ArtworkTranslation.filter(
        artwork_id=artwork_id
    ).order_by("target_lang")
    return ResponseBase(data=[
        {
            "target_lang": r.target_lang,
            "image_index": r.image_index,
            "translated_image_url": r.translated_image_url,
            "status": r.status,
            "is_manual": r.is_manual,
            "error_msg": r.error_msg,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ])


@router.post("/{artwork_id}/translations/manual", response_model=ResponseBase[dict])
async def upload_manual_translation(
    artwork_id: int,
    target_lang: str = Form(...),
    image_index: int = Form(0),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """作者手动上传翻译图（支持多图 image_index，直接标记为 done）。"""
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if artwork.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="仅作者可上传翻译图")

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件不能超过 20MB")
    ext = os.path.splitext(file.filename or "image.jpg")[1].lower() or ".jpg"
    allowed = {".jpg", ".jpeg", ".png", ".webp"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp 格式")

    from app.services.storage_service import storage
    key = f"translations/{artwork_id}/{target_lang.upper()}_img{image_index}_manual{ext}"
    url = await storage.save(data, key)

    target_lang = target_lang.upper()
    existing = await ArtworkTranslation.get_or_none(
        artwork_id=artwork_id, target_lang=target_lang, image_index=image_index
    )
    if existing:
        if existing.translated_image_url:
            await storage.delete_by_url(existing.translated_image_url)
        existing.translated_image_url = url
        existing.is_manual = True
        existing.status = "done"
        existing.error_msg = None
        existing.requested_by_id = current_user.id
        await existing.save()
    else:
        await ArtworkTranslation.create(
            artwork_id=artwork_id,
            target_lang=target_lang,
            image_index=image_index,
            status="done",
            translated_image_url=url,
            is_manual=True,
            requested_by_id=current_user.id,
        )

    return ResponseBase(data={"target_lang": target_lang, "image_index": image_index, "translated_image_url": url, "is_manual": True})


@router.delete("/{artwork_id}/translations/{target_lang}/manual", response_model=ResponseBase[dict])
async def delete_manual_translation(
    artwork_id: int,
    target_lang: str,
    image_index: int = Query(0),
    current_user: User = Depends(get_current_user),
):
    """作者删除手动上传的翻译图（支持 image_index 选择具体图）。"""
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")
    if artwork.author_id != current_user.id:
        raise HTTPException(status_code=403, detail="仅作者可删除翻译图")

    record = await ArtworkTranslation.get_or_none(
        artwork_id=artwork_id, target_lang=target_lang.upper(), image_index=image_index, is_manual=True
    )
    if not record:
        raise HTTPException(status_code=404, detail="手动翻译记录不存在")

    from app.services.storage_service import storage
    if record.translated_image_url:
        await storage.delete_by_url(record.translated_image_url)
    await record.delete()
    return ResponseBase(data={"deleted": True})


@router.get("/{artwork_id}/translations/{target_lang}/download")
async def download_translated_image(
    artwork_id: int,
    target_lang: str,
    image_index: int = Query(0),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """下载翻译图文件（会员或作者本人专属，支持 image_index）。"""
    artwork = await Artwork.get_or_none(id=artwork_id)
    if not artwork:
        raise HTTPException(status_code=404, detail="作品不存在")

    record = await ArtworkTranslation.get_or_none(
        artwork_id=artwork_id, target_lang=target_lang.upper(), image_index=image_index, status="done"
    )
    if not record or not record.translated_image_url:
        raise HTTPException(status_code=404, detail="翻译图不存在")

    is_author = current_user and current_user.id == artwork.author_id
    if not is_author:
        if not current_user:
            raise HTTPException(status_code=401, detail="请先登录")
        from app.api.v1.membership import get_active_membership
        sub = await get_active_membership(current_user)
        if not sub:
            raise HTTPException(
                status_code=403,
                detail={"code": "requires_membership", "message": "下载翻译图需要会员权限"},
            )

    from app.services.storage_service import storage
    ext = record.translated_image_url.rsplit(".", 1)[-1].split("?")[0] if "." in record.translated_image_url else "png"
    filename = f"moetopia_{artwork_id}_{target_lang.upper()}.{ext}"
    return await storage.make_download_response(record.translated_image_url, filename)
