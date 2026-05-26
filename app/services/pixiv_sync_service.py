"""
Pixiv 同步服务：与 pixiv-agent 节点通信、分配作者、导入作品。
"""
import io
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.models.pixiv_sync import PixivSyncNode, PixivSyncAuthor, PixivArtworkCache
from app.models.user import User
from app.models.artwork import Artwork, ArtworkImage
from app.services.storage_service import storage
from app.core.security import get_password_hash

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0


def _node_headers(node: PixivSyncNode) -> dict:
    return {"X-API-Key": node.api_key}


async def ping_node(node: PixivSyncNode) -> bool:
    """向节点发 /health?include_logs=true，更新状态、统计和最近日志快照。"""
    health_data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{node.url}/health",
                params={"include_logs": "true"},
                headers=_node_headers(node),
            )
            online = resp.status_code == 200
            if online:
                health_data = resp.json()
    except Exception as e:
        logger.warning(f"[PixivSync] ping 节点 {node.name} ({node.url}) 失败: {e}")
        online = False

    node.last_ping = datetime.now(timezone.utc)
    node.status = "online" if online else "offline"

    if health_data:
        stats = health_data.get("stats", {})
        queue = health_data.get("queue", {})
        cooldown = queue.get("cooldown", {})
        # 把节点快照写入 note 字段（JSON），供管理页面展示
        import json as _json
        node.note = _json.dumps({
            "stats": stats,
            "cooldown": cooldown,
            "recent_logs": health_data.get("recent_logs", [])[-10:],
            "ts": health_data.get("ts"),
        }, ensure_ascii=False)

    await node.save(update_fields=["last_ping", "status", "note"])
    return online


async def assign_authors() -> int:
    """将未分配的作者按节点负载分配（取 author_count 最少的 online 节点）。返回分配数量。"""
    unassigned = await PixivSyncAuthor.filter(
        assigned_node=None, sync_enabled=True
    ).order_by("created_at")
    if not unassigned:
        return 0

    online_nodes = await PixivSyncNode.filter(status="online").order_by("author_count")
    if not online_nodes:
        logger.warning("[PixivSync] 无可用节点，无法分配作者")
        return 0

    assigned_count = 0
    node_idx = 0

    for author in unassigned:
        node = online_nodes[node_idx % len(online_nodes)]
        author.assigned_node_id = node.id
        author.status = "pending"
        await author.save(update_fields=["assigned_node_id", "status"])

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{node.url}/sync/author/{author.pixiv_user_id}",
                    headers=_node_headers(node),
                )
                if resp.status_code == 200:
                    logger.info(f"[PixivSync] 作者 {author.pixiv_user_id} → 节点 {node.name}")
                    assigned_count += 1
                else:
                    logger.error(f"[PixivSync] 节点 {node.name} 接收作者失败: {resp.status_code}")
        except Exception as e:
            logger.error(f"[PixivSync] 分配作者到节点 {node.name} 出错: {e}")

        node_idx += 1

    await _refresh_node_author_counts()
    return assigned_count


async def _refresh_node_author_counts() -> None:
    """刷新所有节点的 author_count 字段。"""
    nodes = await PixivSyncNode.all()
    for node in nodes:
        count = await PixivSyncAuthor.filter(assigned_node_id=node.id).count()
        await PixivSyncNode.filter(id=node.id).update(author_count=count)


async def reassign_offline_node_authors() -> int:
    """将离线超过 30 分钟的节点的作者重新分配。"""
    threshold = datetime.now(timezone.utc) - timedelta(minutes=30)
    offline_nodes = await PixivSyncNode.filter(
        status="offline", last_ping__lt=threshold
    )
    if not offline_nodes:
        return 0
    offline_ids = [n.id for n in offline_nodes]
    await PixivSyncAuthor.filter(assigned_node_id__in=offline_ids).update(
        assigned_node=None, status="pending"
    )
    logger.info(f"[PixivSync] {len(offline_ids)} 个离线节点的作者已重置为待分配")
    return len(offline_ids)


async def poll_and_import(since_minutes: int = 15) -> dict:
    """
    轮询所有 online 节点，拉取最近 since_minutes 分钟内新增的作品，
    写入 PixivArtworkCache，并导入到主库。
    """
    online_nodes = await PixivSyncNode.filter(status="online")
    if not online_nodes:
        return {"nodes_polled": 0, "cached": 0, "imported": 0}

    since = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat(timespec="seconds")
    total_cached = 0
    total_imported = 0

    for node in online_nodes:
        try:
            cached, imported = await _poll_node(node, since)
            total_cached += cached
            total_imported += imported
        except Exception as e:
            logger.error(f"[PixivSync] 轮询节点 {node.name} 出错: {e}", exc_info=True)

    return {
        "nodes_polled": len(online_nodes),
        "cached": total_cached,
        "imported": total_imported,
    }


async def _poll_node(node: PixivSyncNode, since: str) -> tuple[int, int]:
    """轮询单个节点，返回 (cached_count, imported_count)。复用单个 httpx 客户端防止连接飙涨。"""
    cached = 0
    imported = 0

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_node_headers(node)) as client:
        resp = await client.get(
            f"{node.url}/artworks",
            params={"since": since, "limit": 200},
        )
        resp.raise_for_status()
        artworks_data = resp.json()

        for aw in artworks_data:
            pixiv_id = aw.get("pixiv_id")
            if not pixiv_id:
                continue

            detail_resp = await client.get(f"{node.url}/artworks/{pixiv_id}")
            if detail_resp.status_code != 200:
                continue
            detail = detail_resp.json()

            cache_entry = await PixivArtworkCache.get_or_none(pixiv_id=pixiv_id)
            if not cache_entry:
                cache_entry = PixivArtworkCache(
                    pixiv_id=pixiv_id,
                    pixiv_user_id=aw.get("pixiv_user_id"),
                    node_name=node.name,
                    metadata=detail,
                    image_original_urls=[img["original_url"] for img in detail.get("images", [])],
                    image_local_paths=[img.get("local_path", "") for img in detail.get("images", [])],
                )
                await cache_entry.save()
                cached += 1
            else:
                cache_entry.metadata = detail
                cache_entry.node_name = node.name
                await cache_entry.save(update_fields=["metadata", "node_name", "updated_at"])

            if not cache_entry.imported:
                ok = await import_single_artwork(cache_entry, node)
                if ok:
                    imported += 1

    return cached, imported


async def import_single_artwork(cache: PixivArtworkCache, node: PixivSyncNode) -> bool:
    """从 PixivArtworkCache + 节点下载图片，导入作品到主库。"""
    meta = cache.metadata
    pixiv_id = cache.pixiv_id
    pixiv_user_id = cache.pixiv_user_id

    already = await Artwork.get_or_none(pixiv_id=pixiv_id)
    if already:
        cache.imported = True
        cache.moetopia_artwork_id = already.id
        await cache.save(update_fields=["imported", "moetopia_artwork_id", "updated_at"])
        return True

    author_user = await _get_or_create_imported_user(meta, pixiv_user_id)
    if not author_user:
        logger.error(f"[PixivSync] 无法创建/获取作者账号 pixiv_user_id={pixiv_user_id}")
        return False

    images_data = meta.get("images", [])
    image_urls: list[str] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for idx, img in enumerate(images_data):
            if not img.get("downloaded"):
                logger.debug(f"[PixivSync] 图片 {pixiv_id}[{idx}] 尚未下载，跳过")
                continue
            try:
                resp = await client.get(
                    f"{node.url}/artworks/{pixiv_id}/images/{idx}",
                    headers=_node_headers(node),
                )
                if resp.status_code != 200:
                    continue
                data = resp.content
                content_type = resp.headers.get("content-type", "image/jpeg")
                ext = _ext_from_content_type(content_type)
                filename = f"pixiv_{pixiv_id}_p{idx}{ext}"
                url = await storage.save(data, f"artworks/{filename}")
                image_urls.append(url)
            except Exception as e:
                logger.error(f"[PixivSync] 下载/上传图片 {pixiv_id}[{idx}] 失败: {e}")

    if not image_urls:
        logger.warning(f"[PixivSync] 作品 {pixiv_id} 无可用图片，跳过导入")
        return False

    try:
        tags = meta.get("tags", [])[:20]
        artwork = await Artwork.create(
            title=(meta.get("title") or f"Pixiv #{pixiv_id}")[:200],
            description=meta.get("description") or "",
            author_id=author_user.id,
            rating=meta.get("rating", "safe"),
            is_ai=bool(meta.get("is_ai")),
            artwork_type=meta.get("artwork_type", "illustration"),
            visibility="public",
            content_origin="repost",
            allow_ai_tagging=True,
            allow_community_tagging=True,
            pixiv_id=pixiv_id,
            source=meta.get("source_url") or f"https://www.pixiv.net/artworks/{pixiv_id}",
            original_author_name=meta.get("author_username"),
        )

        from app.models.tag import ArtworkTag
        for tag_name in tags:
            tag_name = tag_name.strip().lower()
            if tag_name:
                await ArtworkTag.get_or_create(artwork_id=artwork.id, tag_name=tag_name)

        for idx, url in enumerate(image_urls):
            await ArtworkImage.create(
                artwork_id=artwork.id,
                file_url=url,
                sort_order=idx,
            )

        try:
            from app.services.meili_sync import sync_artwork_to_meili
            await sync_artwork_to_meili(artwork, all_tags=tags)
        except Exception:
            pass

        cache.imported = True
        cache.moetopia_artwork_id = artwork.id
        await cache.save(update_fields=["imported", "moetopia_artwork_id", "updated_at"])

        logger.info(f"[PixivSync] 作品 {pixiv_id} 已导入 → artwork_id={artwork.id}")
        return True

    except Exception as e:
        logger.error(f"[PixivSync] 导入作品 {pixiv_id} 失败: {e}", exc_info=True)
        return False


async def import_all_cached() -> dict:
    """从 PixivArtworkCache 批量导入所有未导入的作品（清库恢复用）。"""
    pending = await PixivArtworkCache.filter(imported=False).order_by("created_at")
    if not pending:
        return {"total": 0, "imported": 0, "failed": 0}

    imported = 0
    failed = 0
    node_cache: dict[str, Optional[PixivSyncNode]] = {}

    for cache_entry in pending:
        node_name = cache_entry.node_name
        if node_name not in node_cache:
            node_cache[node_name] = await PixivSyncNode.get_or_none(name=node_name)
        node = node_cache.get(node_name)

        if not node or node.status != "online":
            logger.warning(f"[PixivSync] 节点 {node_name} 不可用，跳过 pixiv_id={cache_entry.pixiv_id}")
            failed += 1
            continue

        ok = await import_single_artwork(cache_entry, node)
        if ok:
            imported += 1
        else:
            failed += 1

    return {"total": len(pending), "imported": imported, "failed": failed}


async def _get_or_create_imported_user(meta: dict, pixiv_user_id: int) -> Optional[User]:
    """获取或创建 Pixiv 作者对应的导入账号。"""
    existing = await User.get_or_none(pixiv_user_id=pixiv_user_id)
    if existing:
        return existing

    # author_username 来自节点 /artworks/{id} 的 LEFT JOIN authors
    username = (
        meta.get("author_username")
        or f"pixiv_{pixiv_user_id}"
    )

    username = username[:50]
    fake_email = f"imported_{pixiv_user_id}@internal.moetopia"
    random_pw = get_password_hash(secrets.token_hex(32))

    try:
        user = await User.create(
            username=username,
            email=fake_email,
            password_hash=random_pw,
            is_imported=True,
            pixiv_user_id=pixiv_user_id,
            source_platform="pixiv",
            is_creator=True,
            commission_enabled=False,
            token_version=0,
        )
        return user
    except Exception as e:
        logger.error(f"[PixivSync] 创建导入用户 pixiv_user_id={pixiv_user_id} 失败: {e}")
        return await User.get_or_none(pixiv_user_id=pixiv_user_id)


def _ext_from_content_type(ct: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct.split(";")[0].strip(), ".jpg")
