"""
ARQ 后台任务定义。

每个任务函数签名为 async def task(ctx: dict, ...) — ctx 由 ARQ 自动注入。
任务在 worker.py 中通过 WorkerSettings.functions 注册。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# AI 自动打标 + 向量入库
# ──────────────────────────────────────────────────────────────────────────────

async def task_ai_tag_artwork(ctx: dict, artwork_id: int) -> None:
    """
    后台执行 WD14 AI 打标并将向量存入 Qdrant（包含撞车检测）。
    """
    from app.models.artwork import Artwork
    from app.services.sync_service import SyncService

    logger.info(f"[ARQ] 开始 AI 打标 artwork_id={artwork_id}")
    try:
        artwork = await Artwork.get_or_none(id=artwork_id)
        if not artwork or not artwork.allow_ai_tagging:
            logger.info(f"[ARQ] AI 打标跳过 artwork_id={artwork_id} (not found or ai_tagging disabled)")
            return
        await SyncService.process_and_sync_artwork(str(artwork_id))
        logger.info(f"[ARQ] AI 打标完成 artwork_id={artwork_id}")
    except Exception as e:
        logger.error(f"[ARQ] AI 打标失败 artwork_id={artwork_id}: {e}")
        raise


# ──────────────────────────────────────────────────────────────────────────────
# 邮件发送
# ──────────────────────────────────────────────────────────────────────────────

async def task_send_email(ctx: dict, email_type: str, to_email: str, **kwargs: Any) -> None:
    """
    后台发送邮件，避免 SMTP 握手阻塞 HTTP 响应。
    email_type: "welcome" | "password_reset" | "verification_code"
    """
    from app.services.email_service import EmailService

    logger.info(f"[ARQ] 发送邮件 type={email_type} to={to_email}")
    try:
        if email_type == "welcome":
            await EmailService.send_welcome(email=to_email, username=kwargs.get("username", ""))
        elif email_type == "password_reset":
            await EmailService.send_password_reset(
                email=to_email,
                username=kwargs.get("username", ""),
                token=kwargs.get("token") or kwargs.get("reset_link", ""),
            )
        elif email_type == "verification_code":
            await EmailService.send_verification_code(
                email=to_email,
                code=kwargs.get("code", ""),
                purpose=kwargs.get("purpose", "registration"),
            )
        else:
            logger.warning(f"[ARQ] 未知邮件类型: {email_type}")
            return
        logger.info(f"[ARQ] 邮件发送成功 to={to_email}")
    except Exception as e:
        logger.error(f"[ARQ] 邮件发送失败 to={to_email}: {e}")
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Fan-out 关注者通知（上传作品后批量通知）
# ──────────────────────────────────────────────────────────────────────────────

async def task_fanout_new_artwork(ctx: dict, artwork_id: int, author_id: int, title: str) -> None:
    """
    批量给作者的关注者发送「新作品」通知。
    将原来阻塞在 upload 接口中的 O(followers) DB 写拆分到后台。
    每批 100 人处理，避免单次事务过大。
    """
    from app.models.social import Follow
    from app.services.notification_service import push_notification

    logger.info(f"[ARQ] fan-out 通知 artwork_id={artwork_id} author_id={author_id}")
    try:
        follower_ids = await Follow.filter(followed_id=author_id).values_list("follower_id", flat=True)
        BATCH = 100

        async def _notify(fid: int) -> None:
            try:
                await push_notification(
                    user_id=fid,
                    actor_id=author_id,
                    type="new_artwork",
                    content=f"关注的作者发布了新作品：{title}",
                    related_entity_id=str(artwork_id),
                )
            except Exception as e:
                logger.warning(f"[ARQ] fan-out 通知用户 {fid} 失败: {e}")

        for i in range(0, len(follower_ids), BATCH):
            batch = follower_ids[i:i + BATCH]
            await asyncio.gather(*[_notify(fid) for fid in batch])
        logger.info(f"[ARQ] fan-out 完成，共通知 {len(follower_ids)} 人")
    except Exception as e:
        logger.error(f"[ARQ] fan-out 失败: {e}")
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Meilisearch 同步
# ──────────────────────────────────────────────────────────────────────────────

async def task_sync_artwork_meili(ctx: dict, artwork_id: int) -> None:
    """将作品数据同步到 Meilisearch 索引。"""
    from app.services.meili_sync import sync_artwork_to_meili
    from app.models.artwork import Artwork

    try:
        artwork = await Artwork.get_or_none(id=artwork_id)
        if artwork:
            await sync_artwork_to_meili(artwork)
            logger.info(f"[ARQ] Meilisearch 同步完成 artwork_id={artwork_id}")
    except Exception as e:
        logger.error(f"[ARQ] Meilisearch 同步失败 artwork_id={artwork_id}: {e}")
        raise


async def task_delete_artwork_meili(ctx: dict, artwork_id: int) -> None:
    """从 Meilisearch 删除作品（使用 asyncio.to_thread 避免阻塞）。"""
    from app.infrastructure.meilisearch_client import meili_client

    try:
        await asyncio.to_thread(
            meili_client.client.index("artworks").delete_document, artwork_id
        )
        logger.info(f"[ARQ] Meilisearch 删除完成 artwork_id={artwork_id}")
    except Exception as e:
        logger.warning(f"[ARQ] Meilisearch 删除失败 artwork_id={artwork_id}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# 浏览量 DB 刷写（定时任务，从 Redis 缓冲区批量写入 PostgreSQL）
# ──────────────────────────────────────────────────────────────────────────────

async def task_flush_view_counts(ctx: dict) -> None:
    """
    每 60 秒由定时调度触发一次。
    从 Redis Hash `view_counts` 中取出所有缓冲的增量并写入 DB，然后清空。
    """
    from app.infrastructure.redis_client import get_redis
    from app.models.artwork import Artwork

    r = get_redis()
    key = "view_counts"
    try:
        counts = await r.hgetall(key)
        if not counts:
            return

        # 原子地清空缓冲区（先 DEL，再写 DB；短暂窗口内的新增会在下次刷写）
        await r.delete(key)

        from tortoise.expressions import F
        from app.infrastructure.cache import cache_delete
        updated = 0
        for artwork_id_str, delta_str in counts.items():
            try:
                artwork_id = int(artwork_id_str)
                delta = int(delta_str)
                if delta > 0:
                    await Artwork.filter(id=artwork_id).update(
                        view_count=F("view_count") + delta
                    )
                    # O12: 刷写后失效 Redis 作品缓存，避免返回过时 view_count
                    await cache_delete(f"artwork:{artwork_id}")
                    updated += 1
            except Exception as e:
                logger.warning(f"[ARQ] flush view_count artwork {artwork_id_str} 失败: {e}")

        if updated:
            logger.info(f"[ARQ] 浏览量刷写完成，共更新 {updated} 件作品，已失效对应缓存")
    except Exception as e:
        logger.error(f"[ARQ] flush_view_counts 失败: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# 邮件通知摘要批量发送（定时任务，每 5 分钟将 Redis 队列中的通知合并发送）
# ──────────────────────────────────────────────────────────────────────────────

async def task_refresh_trending_tags(ctx: dict) -> None:
    """
    每小时触发一次。
    批量重新计算热门标签（top 200）的 count / count_7d 并推送到 Meilisearch，
    同时清除 Redis 中的 trending_tags 缓存。
    用两次 GROUP BY 聚合替代 N × 2 次 COUNT 查询。
    """
    from app.services.meili_sync import sync_tags_to_meili
    from app.models.tag import ArtworkTag
    from app.infrastructure.cache import cache_delete
    from tortoise.functions import Count
    from datetime import datetime, timedelta, timezone

    try:
        since_7d = datetime.now(timezone.utc) - timedelta(days=7)

        # 取近 7 天使用最多的前 200 个标签名
        week_rows = (
            await ArtworkTag.filter(created_at__gte=since_7d)
            .annotate(cnt=Count("id"))
            .group_by("tag_name")
            .order_by("-cnt")
            .limit(200)
            .values("tag_name")
        )
        top_names = [r["tag_name"] for r in week_rows]
        if not top_names:
            return

        await sync_tags_to_meili(top_names)
        # 清除 Redis trending_tags 缓存，下次请求时会重新从 Meilisearch 读取
        for limit in (10, 20, 30, 50):
            await cache_delete(f"trending_tags:{limit}")
        logger.info(f"[ARQ] trending tags 刷新完成，共同步 {len(top_names)} 个标签")
    except Exception as e:
        logger.error(f"[ARQ] refresh_trending_tags 失败: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Manga Image Translator 翻译任务
# ──────────────────────────────────────────────────────────────────────────────

async def task_translate_artwork(ctx: dict, translation_id: int) -> None:
    """
    从 ArtworkTranslation 记录读取来源图片，调用 MIT HTTP Server 翻译，
    将结果上传到 storage，更新记录状态。
    """
    from app.models.artwork_translation import ArtworkTranslation
    from app.models.artwork import ArtworkImage
    from app.services.translate_service import TranslateService
    from app.services.storage_service import storage
    import os

    record = await ArtworkTranslation.get_or_none(id=translation_id)
    if not record:
        logger.warning(f"[ARQ] translation_id={translation_id} 不存在，跳过")
        return

    if record.status == "done":
        logger.info(f"[ARQ] translation_id={translation_id} 已完成，跳过")
        return

    record.status = "processing"
    await record.save(update_fields=["status"])

    try:
        # 按 image_index 取对应图片
        images_list = await ArtworkImage.filter(artwork_id=record.artwork_id).order_by("sort_order", "id")
        images_list = list(images_list)
        if not images_list:
            raise ValueError("作品没有图片")
        idx = min(record.image_index, len(images_list) - 1)
        target_image = images_list[idx]

        logger.info(f"[ARQ] 翻译 artwork_id={record.artwork_id} image_index={idx} lang={record.target_lang}")

        async with storage.open_for_processing(target_image.file_url) as local_path:
            translated_bytes = await TranslateService.translate(local_path, record.target_lang)

        # 上传翻译结果
        suffix = os.path.splitext(target_image.file_url)[-1] or ".png"
        dest_key = f"translations/{record.artwork_id}/{record.target_lang}_img{idx}{suffix}"
        url = await storage.save(translated_bytes, dest_key)

        record.status = "done"
        record.translated_image_url = url
        record.error_msg = None
        await record.save(update_fields=["status", "translated_image_url", "error_msg"])
        logger.info(f"[ARQ] 翻译完成 translation_id={translation_id} url={url}")

    except Exception as e:
        logger.error(f"[ARQ] 翻译失败 translation_id={translation_id}: {e}")
        record.status = "failed"
        record.error_msg = str(e)[:480]
        await record.save(update_fields=["status", "error_msg"])
        raise


async def task_flush_email_notifications(ctx: dict) -> None:
    """
    每 5 分钟触发一次。
    取出所有有待发邮件的用户，读取其 Redis 通知队列，合并成一封摘要邮件发送。
    """
    import json as _json
    from app.infrastructure.redis_client import get_redis
    from app.models.user import User as UserModel
    from app.services.email_service import EmailService

    r = get_redis()
    pending_raw = await r.smembers("email_notif_pending")
    if not pending_raw:
        return

    # 原子清空 pending set，避免并发重复发送
    await r.delete("email_notif_pending")
    logger.info(f"[ARQ] 邮件通知摘要：处理 {len(pending_raw)} 位用户")

    for uid_bytes in pending_raw:
        uid = int(uid_bytes)
        key = f"email_notif:{uid}"
        items_raw = await r.lrange(key, 0, -1)
        if not items_raw:
            continue
        await r.delete(key)
        try:
            user = await UserModel.get_or_none(id=uid)
            if not user or not user.email:
                continue
            notifications = [_json.loads(item) for item in items_raw]
            await EmailService.send_notification_digest(
                email=user.email,
                username=user.username,
                notifications=notifications,
            )
            logger.info(f"[ARQ] 邮件摘要已发送至用户 {uid}，共 {len(notifications)} 条")
        except Exception as e:
            logger.warning(f"[ARQ] 邮件摘要发送失败 user={uid}: {e}")


# 定时投稿发布
# ──────────────────────────────────────────────────────────────────────────────

async def task_publish_scheduled_artworks(ctx: dict) -> None:
    """
    每分钟触发一次。
    查找所有 visibility='scheduled' 且 scheduled_at <= now 的作品，将其发布为 public，
    并触发关注者 fan-out 通知。
    """
    from datetime import datetime, timezone
    from app.models.artwork import Artwork
    from app.infrastructure.cache import invalidate_artwork

    now = datetime.now(timezone.utc)
    due = await Artwork.filter(visibility="scheduled", scheduled_at__lte=now)
    if not due:
        return
    logger.info(f"[ARQ] 定时发布 {len(due)} 件作品")
    for artwork in due:
        try:
            artwork.visibility = "public"
            artwork.scheduled_at = None
            await artwork.save(update_fields=["visibility", "scheduled_at"])
            await invalidate_artwork(artwork.id)
            # 同步到 Meilisearch
            try:
                from app.services.meili_sync import sync_artwork_to_meili
                await sync_artwork_to_meili(artwork)
            except Exception:
                pass
            # fan-out 通知
            try:
                from app.worker.enqueue import enqueue
                await enqueue(
                    "task_fanout_new_artwork",
                    artwork_id=artwork.id,
                    author_id=artwork.author_id,
                    title=artwork.title,
                )
            except Exception:
                pass
            logger.info(f"[ARQ] 定时发布成功 artwork_id={artwork.id}")
        except Exception as e:
            logger.error(f"[ARQ] 定时发布失败 artwork_id={artwork.id}: {e}")


# ──────────────────────────────────────────────────────────────────────
# Pixiv 分布式同步任务
# ──────────────────────────────────────────────────────────────────────

async def task_pixiv_node_ping(ctx: dict) -> None:
    """每 5 分钟：对所有节点发 /health 请求，更新 last_ping 和 status。"""
    from app.models.pixiv_sync import PixivSyncNode
    from app.services.pixiv_sync_service import ping_node
    nodes = await PixivSyncNode.all()
    if not nodes:
        return
    online = 0
    for node in nodes:
        if await ping_node(node):
            online += 1
    logger.info(f"[ARQ][PixivSync] 节点心跳检测完成: {online}/{len(nodes)} 在线")


async def task_pixiv_sync_assign(ctx: dict) -> None:
    """每小时：将未分配作者分配到节点，并将离线节点的作者重新分配。"""
    from app.services.pixiv_sync_service import assign_authors, reassign_offline_node_authors
    reassigned = await reassign_offline_node_authors()
    if reassigned:
        logger.info(f"[ARQ][PixivSync] 重新分配了 {reassigned} 个离线节点的作者")
    count = await assign_authors()
    logger.info(f"[ARQ][PixivSync] 本轮分配作者数: {count}")


async def task_pixiv_sync_poll(ctx: dict) -> None:
    """每 10 分钟：轮询所有在线节点，拉取新作品写入缓存并导入主库。"""
    from app.services.pixiv_sync_service import poll_and_import
    result = await poll_and_import(since_minutes=15)
    logger.info(
        f"[ARQ][PixivSync] 轮询完成: 节点={result['nodes_polled']} "
        f"新缓存={result['cached']} 导入={result['imported']}"
    )


async def task_pixiv_sync_import_cached(ctx: dict) -> None:
    """手动触发：从 PixivArtworkCache 批量恢复导入所有未导入的作品（清库后恢复用）。"""
    from app.services.pixiv_sync_service import import_all_cached
    result = await import_all_cached()
    logger.info(
        f"[ARQ][PixivSync] 缓存恢复导入完成: total={result['total']} "
        f"imported={result['imported']} failed={result['failed']}"
    )
