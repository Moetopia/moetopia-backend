import logging
import asyncio
from app.models.artwork import Artwork, ArtworkImage
from app.models.moderation import ModerationQueue
from app.services.storage_service import storage
from app.core.config import settings

logger = logging.getLogger(__name__)


_AI_CFG_KEYS = (
    "enable_ai_features",
    "enable_wd14_tagging",
    "enable_qdrant",
    "enable_content_moderation",
)

_AI_CFG_DEFAULTS: dict[str, bool] = {k: True for k in _AI_CFG_KEYS}


async def _get_ai_cfg() -> dict[str, bool]:
    """
    一次读取 site_config，返回所有 AI 子功能开关字典。
    优先级：env ENABLE_AI_FEATURES=false → 全部返回 False。
    其次：从缓存或 DB 读取各 key，缺失时用默认值（True）。
    """
    if not settings.ENABLE_AI_FEATURES:
        return {k: False for k in _AI_CFG_KEYS}
    result = dict(_AI_CFG_DEFAULTS)
    try:
        from app.infrastructure.cache import cache_get
        cfg = await cache_get("site_config")
        if cfg is None:
            from app.models.site_config import SiteConfig
            rows = await SiteConfig.filter(key__in=list(_AI_CFG_KEYS)).values("key", "value")
            cfg = {r["key"]: r["value"] for r in rows}
        for k in _AI_CFG_KEYS:
            if k in cfg:
                result[k] = bool(cfg[k])
    except Exception:
        pass
    return result


async def _is_ai_enabled() -> bool:
    """向后兼容：仅检查总开关。"""
    cfg = await _get_ai_cfg()
    return cfg["enable_ai_features"]


class SyncService:

    @staticmethod
    async def process_and_sync_artwork(artwork_id: str):
        """
        后台异步任务：提取 AI 特征，并同步至 Meilisearch 和 Qdrant
        """
        logger.info(f"🔄 [后台任务启动] 开始处理作品 ID: {artwork_id} 的跨库同步...")

        try:
            # 1. 从 Postgres 拉取元数据
            artwork = await Artwork.get(id=int(artwork_id))
            images = await ArtworkImage.filter(artwork_id=int(artwork_id))
            
            from app.models.tag import ArtworkTag
            
            # 读取所有 AI 子功能开关（一次 IO）
            ai_cfg = await _get_ai_cfg()
            image_vectors: list[tuple[list, list]] = []  # (vector, ai_tags) per image

            if not ai_cfg["enable_ai_features"]:
                logger.info(f"⏭️ AI 总开关已禁用，跳过所有 AI 处理（artwork_id={artwork_id}）")
            else:
                # ── Step A: WD14 特征提取 + AI 打标 ──────────────────────────
                if not ai_cfg["enable_wd14_tagging"]:
                    logger.info(f"⏭️ WD14 打标已禁用，跳过特征提取（artwork_id={artwork_id}）")
                else:
                    from app.services.ai_engine import ai_engine
                    for img in images:
                        logger.info(f"🤖 正在使用 WD14 提取图片特征与标签: {img.file_url}")
                        async with storage.open_for_processing(img.file_url) as file_path:
                            vector, ai_tags = await asyncio.to_thread(ai_engine.extract_vector, file_path)
                        image_vectors.append((vector, ai_tags))

                        if ai_tags:
                            tag_objs = [
                                ArtworkTag(
                                    artwork_id=artwork.id,
                                    tag_name=t["tag"],
                                    type="ai_unverified",
                                    confidence=t["confidence"],
                                )
                                for t in ai_tags
                            ]
                            await ArtworkTag.bulk_create(tag_objs, ignore_conflicts=True)

                # ── Step B: Qdrant 写入 + 撞车检测 + 锚点打标 ──────────────
                if not ai_cfg["enable_qdrant"]:
                    logger.info(f"⏭️ Qdrant 已禁用，跳过向量写入与撞车检测（artwork_id={artwork_id}）")
                elif not image_vectors:
                    logger.info(f"⏭️ 无向量数据（WD14 未运行），跳过 Qdrant 步骤（artwork_id={artwork_id}）")
                else:
                    from app.infrastructure.qdrant_client import qdrant_client
                    from app.models.artwork import StyleReference
                    for img, (vector, _) in zip(images, image_vectors):
                        payload = {
                            "artwork_id": str(artwork.id),
                            "file_url": img.file_url,
                            "rating": artwork.rating,
                            "is_ai": artwork.is_ai,
                        }

                        await asyncio.to_thread(qdrant_client.upsert_vector, str(img.id), vector, payload)
                        logger.info(f"✅ Qdrant 9083 维向量写入完成 (Image ID: {img.id})")

                        # 撞车检测（upsert 之后搜索，排除自身）
                        dup_hits = await asyncio.to_thread(qdrant_client.search_duplicates, vector, 0.97, 6)
                        dup_hits = [
                            h for h in dup_hits
                            if str(h.id) != str(img.id)
                            and h.payload.get("artwork_id") != str(artwork.id)
                        ]
                        if dup_hits:
                            best = dup_hits[0]
                            dup_artwork_id = best.payload.get("artwork_id")
                            score = float(best.score)
                            logger.info(f"🔍 撞车检测命中: score={score:.4f} dup_artwork_id={dup_artwork_id}")
                            if dup_artwork_id:
                                already_queued = await ModerationQueue.filter(
                                    artwork_id=artwork.id, reason="duplicate_suspected"
                                ).exists()
                                if not already_queued:
                                    await ModerationQueue.create(
                                        artwork_id=artwork.id,
                                        reason="duplicate_suspected",
                                        confidence=round(score, 4),
                                        duplicate_of_artwork_id=int(dup_artwork_id),
                                    )
                                    artwork.moderation_status = "under_review"
                                    update_fields = ["moderation_status"]
                                    if score >= 0.99 and artwork.visibility != "private":
                                        artwork.visibility = "private"
                                        update_fields.append("visibility")
                                        logger.info(f"🚫 作品 {artwork.id} 高置信度撞车（score={score:.4f}），已自动设为仅自己可见")
                                    await artwork.save(update_fields=update_fields)
                                    if "visibility" in update_fields:
                                        from app.infrastructure.cache import invalidate_artwork
                                        await invalidate_artwork(artwork.id)
                                        from app.worker.enqueue import enqueue
                                        await enqueue("task_sync_artwork_meili", artwork_id=artwork.id)
                                    logger.info(f"📋 作品 {artwork.id} 已进入审核队列（向量撞车）")

                        # 锚点基准图解包
                        anchor_hits = await asyncio.to_thread(qdrant_client.search_anchors, vector)
                        logger.info(f"🔍 锚点搜索返回 {len(anchor_hits)} 条结果")
                        for hit in anchor_hits:
                            hit_id_str = str(hit.id)
                            hit_id_hex = hit_id_str.replace("-", "")
                            logger.info(f"🔍 锚点候选: id={hit_id_str}, score={hit.score:.3f}")
                            ref = await StyleReference.filter(
                                qdrant_id__in=[hit_id_str, hit_id_hex]
                            ).first()
                            if not ref:
                                logger.warning(f"⚠️ Qdrant 返回锚点 id={hit_id_str} 但 DB 中未找到对应 StyleReference")
                                continue
                            has_hierarchy = any([ref.work_name, ref.faction_name, ref.character_name])
                            has_custom_tags = bool(ref.tags)
                            if not has_hierarchy and not has_custom_tags:
                                logger.info(f"⏭️ 锚点 {ref.name} 未配置任何标签，跳过自动打标")
                                continue
                            anchor_threshold = ref.similarity_threshold if ref.similarity_threshold else 0.75
                            if hit.score < anchor_threshold:
                                logger.info(f"⏭️ 锚点 {ref.name} score={hit.score:.3f} 低于阈值 {anchor_threshold}，跳过")
                                continue
                            injected: list[str] = []
                            seen_vals: set[str] = set()
                            anchor_tag_objs = []
                            for tag_val in filter(None, [ref.work_name, ref.faction_name, ref.character_name] + list(ref.tags or [])):
                                if tag_val not in seen_vals:
                                    seen_vals.add(tag_val)
                                    injected.append(tag_val)
                                    anchor_tag_objs.append(ArtworkTag(
                                        artwork_id=artwork.id,
                                        tag_name=tag_val,
                                        type="ai_verified",
                                        confidence=float(hit.score),
                                    ))
                            if anchor_tag_objs:
                                await ArtworkTag.bulk_create(anchor_tag_objs, ignore_conflicts=True)
                            logger.info(f"🎯 锚点命中: {ref.name} (score={hit.score:.3f}) → 注入标签 {injected}")

                # ── Step C: 内容安全审核 ──────────────────────────────────────
                if not ai_cfg["enable_content_moderation"]:
                    logger.info(f"⏭️ 内容安全审核已禁用，跳过（artwork_id={artwork_id}）")
                elif not image_vectors:
                    logger.info(f"⏭️ 无向量数据（WD14 未运行），跳过内容审核（artwork_id={artwork_id}）")
                else:
                    await SyncService._run_content_moderation(artwork, image_vectors)

            # 3. 同步文本数据到 Meilisearch（必须在插入完 AI 标签后执行）
            from app.services.meili_sync import sync_artwork_to_meili, sync_tags_to_meili
            all_tags = list(await ArtworkTag.filter(artwork_id=artwork.id).values_list('tag_name', flat=True))
            await sync_artwork_to_meili(artwork, all_tags)
            await sync_tags_to_meili(all_tags)
            logger.info(f"✅ Meilisearch 同步完成 (Artwork ID: {artwork_id})")

            logger.info("🎉 [后台任务圆满结束] 所有数据已就位！")

        except Exception as e:
            logger.error(f"❌ 跨库同步严重失败: {e}")
            raise

    @staticmethod
    async def _run_content_moderation(artwork, image_vectors: list[tuple[list, list]]):
        """
        基于 WD14 输出检测内容安全问题，必要时将作品推入人工审核队列。
        image_vectors: list of (vector, ai_tags)，vector[0..3] 为评级概率。
        """
        try:
            # 逐张取最高 explicit 概率，合并所有 AI 标签
            explicit_prob = 0.0
            all_ai_tags: list[dict] = []
            for vector, ai_tags in image_vectors:
                ep = float(vector[3]) if len(vector) > 3 else 0.0
                if ep > explicit_prob:
                    explicit_prob = ep
                all_ai_tags.extend(ai_tags)

            # 疑似违法内容标签（WD14 中出现时需关注）
            ILLEGAL_TAG_PATTERNS = {"loli", "shota", "child_on_child", "underage", "minor"}
            illegal_tags = [t for t in all_ai_tags if t["tag"].lower() in ILLEGAL_TAG_PATTERNS]

            reason = None
            confidence = 0.0

            AUTO_BAN_THRESHOLDS = {
                "illegal_suspected": 0.5,
                "r18_detected":      0.8,
                "explicit_suspected": 0.7,
            }

            if illegal_tags and explicit_prob > 0.25:
                reason = "illegal_suspected"
                confidence = max(t["confidence"] for t in illegal_tags)
            elif explicit_prob > 0.5 and artwork.rating == "safe":
                reason = "r18_detected"
                confidence = explicit_prob
            elif explicit_prob > 0.3 and artwork.rating == "safe":
                reason = "explicit_suspected"
                confidence = explicit_prob

            if not reason:
                return

            update_fields: list[str] = []

            # auto-ban 检查：不受"是否已在审核队列"影响，必须先执行
            auto_ban_threshold = AUTO_BAN_THRESHOLDS.get(reason, 1.0)
            if confidence >= auto_ban_threshold and artwork.visibility != "private":
                artwork.visibility = "private"
                update_fields.append("visibility")
                logger.info(f"🚫 作品 {artwork.id} 高置信度内容问题（reason={reason}, conf={confidence:.3f}），已自动设为仅自己可见")

            if artwork.moderation_status != "under_review":
                artwork.moderation_status = "under_review"
                update_fields.append("moderation_status")

            if update_fields:
                await artwork.save(update_fields=update_fields)
                if "visibility" in update_fields:
                    from app.infrastructure.cache import invalidate_artwork
                    await invalidate_artwork(artwork.id)
                    from app.worker.enqueue import enqueue
                    await enqueue("task_sync_artwork_meili", artwork_id=artwork.id)

            # 若已有待审记录则不重复入队
            existing = await ModerationQueue.filter(artwork_id=artwork.id, status="pending").first()
            if not existing:
                await ModerationQueue.create(
                    artwork_id=artwork.id,
                    reason=reason,
                    confidence=round(confidence, 4),
                )
            logger.info(f"⚠️  作品 {artwork.id} 已进入审核队列 (reason={reason}, conf={confidence:.3f})")
        except Exception as e:
            logger.error(f"❌ 内容安全检测失败 (artwork={artwork.id}): {e}")