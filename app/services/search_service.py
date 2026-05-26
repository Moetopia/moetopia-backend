import os
import math
import asyncio
import tempfile
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Any, Dict, Optional

from app.models.artwork import Artwork
from app.models.user import User
from app.services.search_engine import search_engine
from app.services.ai_engine import ai_engine

logger = logging.getLogger(__name__)


class SearchService:

    # ═══════════════════════════════════════════════════════════════
    # 内部工具
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def _blocked_author_filter(user: User) -> Optional[str]:
        """返回 Meilisearch filter 字符串，排除拉黑和被拉黑用户的作品。结果 Redis 缓存 5 min。"""
        from app.infrastructure.cache import cache_get, cache_set, TTL_BLOCK_FILTER
        cache_key = f"block_filter:{user.id}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached or None  # cached empty string → no block filter
        from app.models.social import UserBlock
        blocked, blocking = await asyncio.gather(
            UserBlock.filter(blocker_id=user.id).values_list("blocked_id", flat=True),
            UserBlock.filter(blocked_id=user.id).values_list("blocker_id", flat=True),
        )
        excluded = set(blocked) | set(blocking)
        result = "author_id NOT IN [{}]".format(", ".join(str(i) for i in excluded)) if excluded else ""
        await cache_set(cache_key, result, TTL_BLOCK_FILTER)
        return result or None

    @staticmethod
    def _type_filter(artwork_type: Optional[str]) -> Optional[str]:
        """Build Meilisearch filter for artwork_type, treating 'illustration' as including 'animated'."""
        if not artwork_type:
            return None
        if artwork_type == 'illustration':
            return "artwork_type IN ['illustration', 'animated']"
        return f"artwork_type = '{artwork_type}'"

    @staticmethod
    async def _enrich_meili_hits(hits: List[dict]) -> List[dict]:
        """将 Meilisearch 命中结果用 Postgres 完整数据补齐（images/author/artwork_type 等）"""
        from app.schemas.artwork_schema import serialize_artwork
        artwork_ids = [int(h["id"]) for h in hits if h.get("id")]
        if not artwork_ids:
            return []
        artworks = await Artwork.filter(id__in=artwork_ids, visibility="public").prefetch_related("images", "tags", "author")
        artwork_map = {a.id: a for a in artworks}
        enriched = []
        for h in hits:
            artwork = artwork_map.get(int(h["id"])) if h.get("id") else None
            if artwork:
                enriched.append(serialize_artwork(artwork).model_dump(mode="json"))
        return enriched

    @staticmethod
    def _ts(days_ago: int) -> int:
        """返回 N 天前的 Unix 时间戳（整数，Meili 用）"""
        return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())

    # ═══════════════════════════════════════════════════════════════
    # 作品 — 关键词搜索
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def keyword_search(
        query: str,
        user: Optional[User] = None,
        sort_by: Optional[List[str]] = None,
        extra_filter: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """安全关键词搜索（走 Meilisearch），结果自动补齐完整作品数据"""
        result = await asyncio.to_thread(
            search_engine.search_artworks,
            user, query, sort_by, extra_filter, limit, offset
        )
        hits = result.get("hits", [])
        estimated_total = result.get("estimatedTotalHits", len(hits))
        enriched = await SearchService._enrich_meili_hits(hits)
        return {"hits": enriched, "estimated_total": estimated_total}

    @staticmethod
    async def advanced_keyword_search(
        query: str,
        user: Optional[User] = None,
        sort_by: Optional[List[str]] = None,
        min_bookmarks: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        author_id: Optional[int] = None,
        artwork_type: Optional[str] = None,
        is_ai: Optional[bool] = None,
        any_keywords: Optional[str] = None,
        exclude_keywords: Optional[str] = None,
        search_scope: Optional[str] = None,  # 'all' | 'tags_partial' | 'tags_exact'
        content_origin: Optional[str] = None,  # 'original' | 'fanart' | 'repost'
        limit: int = 50,
        offset: int = 0,
    ) -> List[Any]:
        """带高级过滤器的关键词搜索"""
        extra_parts = []

        # Build combined query (all + any keywords)
        combined_query = query.strip()
        if any_keywords and any_keywords.strip():
            combined_query = (combined_query + " " + any_keywords.strip()).strip()

        # Search scope → attributesToSearchOn or exact-tag filter
        attributes_to_search_on = None
        if search_scope == 'tags_partial':
            attributes_to_search_on = ['tags']
        elif search_scope == 'tags_exact' and combined_query:
            for term in combined_query.split():
                t = term.replace("'", "")
                if t:
                    extra_parts.append(f"tags = '{t}'")
            combined_query = ""

        # Exclude keywords
        if exclude_keywords and exclude_keywords.strip():
            for term in exclude_keywords.strip().split():
                t = term.replace("'", "")
                if t:
                    extra_parts.append(f"NOT tags = '{t}'")

        if min_bookmarks is not None:
            extra_parts.append(f"bookmark_count >= {int(min_bookmarks)}")
        if date_from:
            try:
                ts = datetime.fromisoformat(date_from).timestamp()
                extra_parts.append(f"created_at >= {int(ts)}")
            except ValueError:
                pass
        if date_to:
            try:
                ts = datetime.fromisoformat(date_to).timestamp()
                extra_parts.append(f"created_at <= {int(ts)}")
            except ValueError:
                pass
        if author_id is not None:
            extra_parts.append(f"author_id = '{author_id}'")
        type_f = SearchService._type_filter(artwork_type)
        if type_f:
            extra_parts.append(type_f)
        if is_ai is not None:
            extra_parts.append(f"is_ai = {'true' if is_ai else 'false'}")
        if content_origin:
            extra_parts.append(f"content_origin = '{content_origin}'")

        extra_filter = " AND ".join(extra_parts) if extra_parts else None
        result = await asyncio.to_thread(
            search_engine.search_artworks,
            user, combined_query, sort_by, extra_filter, limit, offset, attributes_to_search_on
        )
        hits = result.get("hits", [])
        estimated_total = result.get("estimatedTotalHits", len(hits))
        enriched = await SearchService._enrich_meili_hits(hits)
        return {"hits": enriched, "estimated_total": estimated_total}

    # ═══════════════════════════════════════════════════════════════
    # 作品 — 以图搜图 / 混合
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def image_search(
        image,
        user: Optional[User] = None,
        limit: int = 20,
    ) -> dict:
        """以图搜图（经安全注入层），返回 {hits, detected_tags}。
        搜索分两路：
        1. 直接在 artworks_vectors 中向量搜索
        2. 在 style_refs 中匹配最近锚点，再由锚点向量扩展搜索作品
        """
        from app.schemas.artwork_schema import serialize_artwork
        from app.infrastructure.qdrant_client import qdrant_client as _qdrant
        from app.services.search_engine import SearchEngine

        ext = os.path.splitext(image.filename)[1] if image.filename else ".jpg"
        temp_fd, temp_path = tempfile.mkstemp(suffix=ext)
        os.close(temp_fd)
        try:
            # O2: 用 asyncio.to_thread 避免阻塞事件循环
            img_data = await image.read()
            def _write_tmp():
                with open(temp_path, "wb") as buf:
                    buf.write(img_data)
            await asyncio.to_thread(_write_tmp)

            vector, detected_tags = await asyncio.to_thread(ai_engine.extract_vector, temp_path)

            safe_filter = SearchEngine._build_qdrant_filter(user)

            # O3: 并行执行直接向量搜索和锁点匹配
            direct_task = asyncio.to_thread(search_engine.search_by_vector, user, vector, None, limit)
            anchor_task = asyncio.to_thread(_qdrant.search_anchors, vector, 3, 0.65)
            direct_points, anchor_matches = await asyncio.gather(direct_task, anchor_task)

            # O3: 并行搜索所有匹配到的锁点
            if anchor_matches:
                anchor_results = await asyncio.gather(*[
                    asyncio.to_thread(_qdrant.search_by_style_ref, str(a.id), safe_filter, limit)
                    for a in anchor_matches
                ])
            else:
                anchor_results = []

            anchor_points: list = []
            matched_anchors: list = []
            for anchor, pts in zip(anchor_matches, anchor_results):
                anchor_points.extend(pts)
                matched_anchors.append({
                    "id": anchor.id,
                    "score": round(anchor.score, 4),
                    "name": anchor.payload.get("name", ""),
                    "file_url": anchor.payload.get("file_url", ""),
                })

            # ── 合并：score_map[artwork_id] = 最高得分 ────────────────
            score_map: dict = {}
            for p in direct_points:
                aid = p.payload.get("artwork_id")
                if aid:
                    score_map[aid] = max(score_map.get(aid, 0.0), p.score)
            for p in anchor_points:
                aid = p.payload.get("artwork_id")
                if aid:
                    # 锚点扩展路径略微降权，避免压过直接相似命中
                    score_map[aid] = max(score_map.get(aid, 0.0), p.score * 0.92)

            sorted_ids = sorted(score_map, key=lambda k: score_map[k], reverse=True)[:limit]

            # O1: 批量查询替代逐条 Artwork.get（N+1 修复）
            artworks_db = await Artwork.filter(id__in=[int(aid) for aid in sorted_ids]).prefetch_related("images", "tags", "author")
            artwork_map = {a.id: a for a in artworks_db}
            hits = []
            for aid in sorted_ids:
                artwork = artwork_map.get(int(aid))
                if artwork:
                    serialized = serialize_artwork(artwork).model_dump(mode="json")
                    serialized["_score"] = round(score_map[aid], 4)
                    hits.append(serialized)

            sorted_tags = sorted(detected_tags, key=lambda t: t["confidence"], reverse=True)[:30]
            return {"hits": hits, "detected_tags": sorted_tags, "matched_anchors": matched_anchors or None}
        finally:
            if os.path.exists(temp_path):
                await asyncio.to_thread(os.remove, temp_path)

    @staticmethod
    async def hybrid_search(
        query: Optional[str] = None,
        image=None,
        user: Optional[User] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """混合检索分发入口，返回 {hits, detected_tags}"""
        if image:
            return await SearchService.image_search(image, user, limit)
        if query:
            res = await SearchService.keyword_search(query, user, limit=limit, offset=offset)
            return {"hits": res["hits"], "detected_tags": None, "matched_anchors": None}
        return {"hits": [], "detected_tags": None, "matched_anchors": None}

    # ═══════════════════════════════════════════════════════════════
    # 作品 — 排行榜（全部走 Meilisearch）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def get_ranking(mode: str = "daily", limit: int = 50, user=None) -> List[Any]:
        """通用排行榜：daily=7天, weekly=30天, monthly=90天（Meilisearch + Redis 缓存 60s）"""
        from app.infrastructure.cache import cache_get, cache_set, TTL_RANKING
        cache_key = f"ranking:{mode}:{limit}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
        days = {"daily": 7, "weekly": 30, "monthly": 90}.get(mode, 7)
        ts = SearchService._ts(days)
        result = await asyncio.to_thread(
            search_engine.browse_artworks,
            user, ["like_count:desc", "view_count:desc"],
            f"created_at >= {ts} AND content_origin != 'repost'",
            limit, 0,
        )
        hits = await SearchService._enrich_meili_hits(result.get("hits", []))
        await cache_set(cache_key, hits, TTL_RANKING)
        return hits

    # ═══════════════════════════════════════════════════════════════
    # 作品 — Feed（走 Meilisearch，仅从 Postgres 获取关注 ID）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def get_following_feed(
        user: User,
        limit: int = 30,
        offset: int = 0,
        artwork_type: Optional[str] = None,
        rating: Optional[str] = None,
    ) -> List[Any]:
        """关注画师最新作品流（Postgres 取关注 ID → Meilisearch 过滤排序）"""
        from app.infrastructure.cache import cache_get, cache_set, TTL_FOLLOWING_IDS
        cache_key = f"following_ids:{user.id}"
        following_ids = await cache_get(cache_key)
        if following_ids is None:
            from app.models.social import Follow
            following_ids = await Follow.filter(follower_id=user.id).values_list("followed_id", flat=True)
            await cache_set(cache_key, list(following_ids), TTL_FOLLOWING_IDS)
        if not following_ids:
            return []

        # 构建 Meili author_id IN [...] 过滤
        id_list = ", ".join(f"'{fid}'" for fid in following_ids)
        parts = [f"author_id IN [{id_list}]"]
        type_f = SearchService._type_filter(artwork_type)
        if type_f:
            parts.append(type_f)
        if rating:
            parts.append(f"rating = '{rating}'")
        block_f = await SearchService._blocked_author_filter(user)
        if block_f:
            parts.append(block_f)

        extra = " AND ".join(parts)
        result = await asyncio.to_thread(
            search_engine.browse_artworks,
            user, ["created_at:desc"], extra, limit, offset
        )
        return await SearchService._enrich_meili_hits(result.get("hits", []))

    @staticmethod
    async def get_tag_feed(
        user: User,
        limit: int = 30,
        offset: int = 0,
        artwork_type: Optional[str] = None,
        rating: Optional[str] = None,
    ) -> List[Any]:
        """关注标签最新作品流（Postgres 取关注标签 → Meilisearch 过滤排序）"""
        from app.infrastructure.cache import cache_get, cache_set, TTL_FOLLOWED_TAGS
        tag_cache_key = f"followed_tags:{user.id}"
        followed_tags = await cache_get(tag_cache_key)
        if followed_tags is None:
            from app.models.social import FollowTag
            followed_tags = await FollowTag.filter(user_id=user.id).values_list("tag_name", flat=True)
            await cache_set(tag_cache_key, list(followed_tags), TTL_FOLLOWED_TAGS)
        if not followed_tags:
            return []

        # 构建 Meili tags IN [...] 过滤
        tag_list = ", ".join(f"'{t.replace(chr(39), '')}'" for t in followed_tags)
        parts = [f"tags IN [{tag_list}]"]
        type_f = SearchService._type_filter(artwork_type)
        if type_f:
            parts.append(type_f)
        if rating:
            parts.append(f"rating = '{rating}'")
        block_f = await SearchService._blocked_author_filter(user)
        if block_f:
            parts.append(block_f)

        extra = " AND ".join(parts)
        result = await asyncio.to_thread(
            search_engine.browse_artworks,
            user, ["created_at:desc"], extra, limit, offset
        )
        return await SearchService._enrich_meili_hits(result.get("hits", []))

    @staticmethod
    async def get_recommended_feed(
        user: User,
        limit: int = 30,
        offset: int = 0,
        artwork_type: Optional[str] = None,
        rating: Optional[str] = None,
    ) -> List[Any]:
        """
        HVCR-U: Hybrid Visual-Content Recommender — User Feed.
        score(u,C) = 0.50·cos(u_vec,v_C) + 0.35·TagMatch(u,C) + 0.15·Freshness(C)
        Interaction weights: Bookmark×3, Like×2, View×0.5; half-life 30 days.
        Cold start (<5 interactions): fallback to daily ranking.
        """
        from app.infrastructure.cache import cache_get, cache_set, TTL_REC_TAGS, TTL_REC_UVEC
        from app.models.interaction import Bookmark, Like, ViewHistory
        from app.models.tag import ArtworkTag
        from app.models.artwork import ArtworkImage
        from app.infrastructure.qdrant_client import qdrant_client
        from collections import Counter
        import numpy as np

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=90)
        LAMBDA = math.log(2) / 30  # interaction decay half-life 30 days

        # ── 1. Fetch interaction history ───────────────────────────
        bookmarks, likes, views = await asyncio.gather(
            Bookmark.filter(user_id=user.id, created_at__gte=cutoff)
                    .order_by("-created_at").limit(50)
                    .values("artwork_id", "created_at"),
            Like.filter(user_id=user.id, created_at__gte=cutoff)
                .order_by("-created_at").limit(50)
                .values("artwork_id", "created_at"),
            ViewHistory.filter(user_id=user.id, viewed_at__gte=cutoff)
                       .order_by("-viewed_at").limit(100)
                       .values("artwork_id", "viewed_at"),
        )

        total = len(bookmarks) + len(likes) + len(views)
        if total < 5:
            return await SearchService.get_ranking("daily", limit=limit, user=user)

        # ── 2. Weighted interaction map with time decay ────────────
        interactions: Dict[int, float] = {}
        def _add(artwork_id, base_weight, ts):
            if not artwork_id:
                return
            dt_utc = SearchService._ensure_utc(ts)
            delta_days = (now - dt_utc).total_seconds() / 86400
            w = base_weight * math.exp(-LAMBDA * delta_days)
            interactions[artwork_id] = interactions.get(artwork_id, 0.0) + w

        for b in bookmarks:
            _add(b["artwork_id"], 3.0, b["created_at"])
        for l in likes:
            _add(l["artwork_id"], 2.0, l["created_at"])
        for v in views:
            _add(v["artwork_id"], 0.5, v["viewed_at"])

        interacted_ids = set(interactions.keys())

        # ── 3. User preference vector (cached 10 min) ──────────────
        uvec_key = f"rec_uvec:{user.id}"
        user_vector = await cache_get(uvec_key)

        if user_vector is None:
            img_rows = await ArtworkImage.filter(
                artwork_id__in=list(interacted_ids)
            ).values("id", "artwork_id")
            art_to_imgs: Dict[int, List[str]] = {}
            for row in img_rows:
                art_to_imgs.setdefault(row["artwork_id"], []).append(str(row["id"]))

            all_img_ids = [iid for ids in art_to_imgs.values() for iid in ids]
            if all_img_ids:
                vector_map = await asyncio.to_thread(
                    qdrant_client.retrieve_vectors_by_ids, all_img_ids
                )
                if vector_map:
                    weighted_sum = None
                    for aid, img_ids in art_to_imgs.items():
                        vecs = [np.array(vector_map[iid]) for iid in img_ids if iid in vector_map]
                        if not vecs:
                            continue
                        art_vec = np.mean(vecs, axis=0) * interactions.get(aid, 0.0)
                        weighted_sum = art_vec if weighted_sum is None else weighted_sum + art_vec
                    if weighted_sum is not None:
                        norm = np.linalg.norm(weighted_sum)
                        if norm > 0:
                            user_vector = (weighted_sum / norm).tolist()
                            await cache_set(uvec_key, user_vector, TTL_REC_UVEC)

        # ── 4. Tag preference profile (cached 5 min) ───────────────
        tag_cache_key = f"rec_tags:{user.id}"
        user_tag_profile: Dict[str, float] = await cache_get(tag_cache_key) or {}

        if not user_tag_profile:
            # pref(u,t) = Σ_{i∈I(u)} w_i · [t∈T_i] · idf(t)  (§6.4.4)
            tag_rows = await ArtworkTag.filter(
                artwork_id__in=list(interacted_ids)
            ).values("artwork_id", "tag_name")
            art_to_tags: Dict[int, List[str]] = {}
            all_tag_set: set = set()
            for row in tag_rows:
                art_to_tags.setdefault(row["artwork_id"], []).append(row["tag_name"])
                all_tag_set.add(row["tag_name"])
            idf_prof = await SearchService._get_tag_idf_weights(list(all_tag_set))
            pref: Dict[str, float] = {}
            for aid, tags_i in art_to_tags.items():
                w_i = interactions.get(aid, 0.0)
                for t in tags_i:
                    pref[t] = pref.get(t, 0.0) + w_i * idf_prof.get(t, 1.0)
            user_tag_profile = dict(
                sorted(pref.items(), key=lambda kv: kv[1], reverse=True)[:30]
            )
            await cache_set(tag_cache_key, user_tag_profile, TTL_REC_TAGS)

        # ── 5. Candidate generation ────────────────────────────────
        candidate_scores: Dict[str, float] = {}  # artwork_id -> qdrant_sim

        if user_vector:
            qdrant_points = await asyncio.to_thread(
                qdrant_client.search_similar_to_vectors, user_vector, 100
            )
            for p in qdrant_points:
                aid = p.payload.get("artwork_id")
                if aid and int(aid) not in interacted_ids:
                    candidate_scores[str(aid)] = max(candidate_scores.get(str(aid), 0.0), p.score)

        # Supplement with tag-based candidates if Qdrant insufficient
        if len(candidate_scores) < limit * 2 and user_tag_profile:
            extra_parts = []
            type_f = SearchService._type_filter(artwork_type)
            if type_f:
                extra_parts.append(type_f)
            if rating:
                extra_parts.append(f"rating = '{rating}'")
            block_f = await SearchService._blocked_author_filter(user)
            if block_f:
                extra_parts.append(block_f)
            extra = " AND ".join(extra_parts) if extra_parts else None
            query = " ".join(list(user_tag_profile.keys())[:10])
            result = await asyncio.to_thread(
                search_engine.search_artworks, user, query, None, extra, limit * 3, 0
            )
            for h in result.get("hits", []):
                aid = str(h.get("id"))
                if aid and int(aid) not in interacted_ids and aid not in candidate_scores:
                    candidate_scores[aid] = 0.0

        if not candidate_scores:
            return await SearchService.get_ranking("daily", limit=limit, user=user)

        candidate_ids = list(candidate_scores.keys())

        # ── 6. Batch fetch candidate tags + created_at ─────────────
        cand_tag_rows, cand_artworks = await asyncio.gather(
            ArtworkTag.filter(artwork_id__in=[int(cid) for cid in candidate_ids])
                      .values("artwork_id", "tag_name"),
            Artwork.filter(id__in=[int(cid) for cid in candidate_ids], visibility="public")
                   .values("id", "created_at", "author_id"),
        )
        cand_tags: Dict[str, List[str]] = {}
        for row in cand_tag_rows:
            cand_tags.setdefault(str(row["artwork_id"]), []).append(row["tag_name"])

        cand_meta: Dict[str, dict] = {str(r["id"]): r for r in cand_artworks}

        # ── 7. HVCR-U re-ranking ───────────────────────────────────
        MU = math.log(2) / 7  # freshness half-life 7 days
        EPS = 1e-9

        # TagMatch_raw(u,C) = Σ_{t∈T_C} pref(u,t) / |T_C|  (§6.4.5)
        raw_tag_scores: Dict[str, float] = {
            cid: (sum(user_tag_profile.get(t, 0.0) for t in tags) / len(tags)
                  if tags else 0.0)
            for cid, tags in ((c, cand_tags.get(c, [])) for c in candidate_ids)
        }
        max_raw_tag = max(raw_tag_scores.values(), default=0.0)

        scored: List[tuple] = []
        for cid in candidate_ids:
            meta = cand_meta.get(cid)
            if not meta:
                continue
            qdrant_sim = candidate_scores.get(cid, 0.0)
            # TagMatch(u,C) = TagMatch_raw / (max_raw + ε)
            tag_match = raw_tag_scores.get(cid, 0.0) / (max_raw_tag + EPS)
            created_at = SearchService._ensure_utc(meta["created_at"])
            delta_days = (now - created_at).total_seconds() / 86400
            freshness = math.exp(-MU * delta_days)
            final_score = 0.50 * qdrant_sim + 0.35 * tag_match + 0.15 * freshness
            scored.append((final_score, cid, meta["author_id"]))

        scored.sort(key=lambda x: x[0], reverse=True)

        # ── 8. Diversity filter + pagination ──────────────────────
        author_count: Dict[int, int] = {}
        diverse_ids: List[str] = []
        for _, cid, author_id in scored:
            cnt = author_count.get(author_id, 0)
            if cnt < 3:
                diverse_ids.append(cid)
                author_count[author_id] = cnt + 1
            if len(diverse_ids) >= offset + limit:
                break

        page_ids = diverse_ids[offset: offset + limit]
        return await SearchService._enrich_meili_hits([{"id": int(cid)} for cid in page_ids])

    # ═══════════════════════════════════════════════════════════════
    # 内部工具 — HVCR 算法辅助
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def _get_tag_idf_weights(tag_names: List[str]) -> Dict[str, float]:
        """
        IDF(t) = log(1 + N / max(1, df(t)))
        N 由缓存读取，df(t) 按当前标签集合从数据库聚合计算。
        """
        from app.infrastructure.cache import cache_get, cache_set, TTL_TAG_IDF
        from app.models.tag import ArtworkTag
        from tortoise.functions import Count

        if not tag_names:
            return {}

        N = await cache_get("tag_idf_N")
        if N is None:
            N = await Artwork.filter(visibility="public").count()
            await cache_set("tag_idf_N", N, TTL_TAG_IDF)
        N = max(N, 1)

        rows = (
            await ArtworkTag.filter(tag_name__in=tag_names)
            .annotate(df=Count("artwork_id", distinct=True))
            .group_by("tag_name")
            .values("tag_name", "df")
        )
        df_map = {r["tag_name"]: r["df"] for r in rows}
        return {
            t: math.log(1 + N / max(1, df_map.get(t, 1)))
            for t in tag_names
        }

    @staticmethod
    def _weighted_jaccard(tags_a: List[str], tags_b: List[str], idf: Dict[str, float]) -> float:
        """
        Weighted Jaccard: WJ(A,B) = Σidf(t∩) / Σidf(t∪)
        Rare tags (high IDF) contribute more to similarity.
        """
        set_a, set_b = set(tags_a), set(tags_b)
        union = set_a | set_b
        if not union:
            return 0.0
        num = sum(idf.get(t, 1.0) for t in set_a & set_b)
        den = sum(idf.get(t, 1.0) for t in union)
        return num / den if den > 0 else 0.0

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    # ═══════════════════════════════════════════════════════════════
    # 作品 — 相关推荐 HVCR-S
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def get_related_artworks(artwork_id: int, user: Optional[User] = None, limit: int = 12) -> List[Any]:
        """
        HVCR-S: Hybrid Visual-Content Recommender — Similarity.
        score(A,C) = α·Sim_V(A,C) + β·WJ(T_A,T_C)
        α=0.65, β=0.35 when Qdrant vector exists; fallback α=0,β=1.
        Redis cache 5 min.
        """
        from app.infrastructure.cache import cache_get, cache_set, TTL_RELATED
        cache_key = f"related:{artwork_id}:{limit}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        from app.models.tag import ArtworkTag
        from app.models.artwork import ArtworkImage
        from app.infrastructure.qdrant_client import qdrant_client
        import numpy as np

        # ── 1. Query artwork features ──────────────────────────────
        tag_names, image_ids = await asyncio.gather(
            ArtworkTag.filter(artwork_id=artwork_id).values_list("tag_name", flat=True),
            ArtworkImage.filter(artwork_id=artwork_id).values_list("id", flat=True),
        )
        tag_names = list(tag_names)
        image_ids = list(image_ids)

        if not tag_names and not image_ids:
            await cache_set(cache_key, [], TTL_RELATED)
            return []

        # ── 2. Build query vector (mean of image vectors) ──────────
        query_vector = None
        if image_ids:
            vector_map = await asyncio.to_thread(
                qdrant_client.retrieve_vectors_by_ids,
                [str(iid) for iid in image_ids]
            )
            if vector_map:
                vecs = [np.array(v) for v in vector_map.values()]
                mean_vec = np.mean(vecs, axis=0)
                norm = np.linalg.norm(mean_vec)
                if norm > 0:
                    query_vector = (mean_vec / norm).tolist()

        # ── 3. Qdrant ANN search (top-32 excluding self) ───────────
        qdrant_scores: Dict[str, float] = {}
        if query_vector:
            qdrant_points = await asyncio.to_thread(
                qdrant_client.search_similar_to_vector,
                query_vector, str(artwork_id), limit * 4
            )
            for p in qdrant_points:
                aid = p.payload.get("artwork_id")
                if aid and aid != str(artwork_id):
                    qdrant_scores[str(aid)] = max(qdrant_scores.get(str(aid), 0.0), p.score)

        # ── 4. Meilisearch tag-filter supplement / fallback ────────
        meili_ids: List[str] = []
        if tag_names:
            top_tags = tag_names[:15]
            tag_list = ", ".join(f'"{t}"' for t in top_tags)
            result = await asyncio.to_thread(
                search_engine.search_artworks,
                user, "", None, f"tags IN [{tag_list}]", limit * 4, 0
            )
            meili_ids = [
                str(h["id"]) for h in result.get("hits", [])
                if str(h.get("id")) != str(artwork_id)
            ]

        # ── 5. Merge candidates ────────────────────────────────────
        candidate_ids = list(dict.fromkeys(
            [cid for cid in list(qdrant_scores.keys()) + meili_ids
             if cid != str(artwork_id)]
        ))
        if not candidate_ids:
            await cache_set(cache_key, [], TTL_RELATED)
            return []

        # ── 6. Batch fetch candidate tags ──────────────────────────
        cand_tag_rows = await ArtworkTag.filter(
            artwork_id__in=[int(cid) for cid in candidate_ids]
        ).values("artwork_id", "tag_name")
        cand_tags: Dict[str, List[str]] = {}
        for row in cand_tag_rows:
            cand_tags.setdefault(str(row["artwork_id"]), []).append(row["tag_name"])

        # ── 7. IDF weights for all relevant tags ───────────────────
        all_tags = set(tag_names)
        for tags in cand_tags.values():
            all_tags.update(tags)
        idf = await SearchService._get_tag_idf_weights(list(all_tags))

        # ── 8. HVCR-S scoring ─────────────────────────────────────
        alpha = 0.65 if query_vector and qdrant_scores else 0.0
        beta = 1.0 - alpha

        scored: List[tuple] = []
        for cid in candidate_ids:
            c_tags = cand_tags.get(cid, [])
            wj = SearchService._weighted_jaccard(tag_names, c_tags, idf)
            qdrant_sim = qdrant_scores.get(cid, 0.0)
            final_score = alpha * qdrant_sim + beta * wj
            if final_score > 0:
                scored.append((final_score, cid))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_ids = [cid for _, cid in scored[:limit]]

        enriched = await SearchService._enrich_meili_hits([{"id": int(cid)} for cid in top_ids])
        await cache_set(cache_key, enriched, TTL_RELATED)
        return enriched

    # ═══════════════════════════════════════════════════════════════
    # 标签
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def tag_autocomplete(query: str, limit: int = 10) -> List[str]:
        """标签自动补全（走 Meilisearch tags 索引，按使用频次排序，空时回退到 DB LIKE 查询）"""
        result = await asyncio.to_thread(
            search_engine.search_tags,
            query, ["count:desc"], limit, 0
        )
        tags = [h["tag_name"] for h in result.get("hits", []) if h.get("tag_name")]
        if tags:
            return tags
        # DB fallback
        from app.models.tag import ArtworkTag
        rows = await ArtworkTag.filter(tag_name__icontains=query).distinct().limit(limit).values_list("tag_name", flat=True)
        return list(rows)

    @staticmethod
    async def get_trending_tags(limit: int = 20) -> List[dict]:
        """热门标签：Meilisearch 优先 + Redis 缓存 10 min，回退到 DB 聚合"""
        from app.infrastructure.cache import cache_get, cache_set, TTL_TAG_LIST
        cache_key = f"trending_tags:{limit}"
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
        result = await asyncio.to_thread(
            search_engine.search_tags,
            "", ["count_7d:desc"], limit, 0
        )
        hits = [
            {"tag_name": h["tag_name"], "count": h.get("count_7d", 0)}
            for h in result.get("hits", []) if h.get("tag_name")
        ]
        if hits:
            await cache_set(cache_key, hits, TTL_TAG_LIST)
            return hits
        # DB fallback: aggregate by count from ArtworkTag
        from app.models.tag import ArtworkTag
        from tortoise.functions import Count
        from datetime import timedelta
        since_7d = datetime.now(timezone.utc) - timedelta(days=7)
        rows = (
            await ArtworkTag.filter(created_at__gte=since_7d)
            .annotate(cnt=Count("id"))
            .group_by("tag_name")
            .order_by("-cnt")
            .limit(limit)
            .values("tag_name", "cnt")
        )
        if rows:
            result_data = [{"tag_name": r["tag_name"], "count": r["cnt"]} for r in rows]
            await cache_set(cache_key, result_data, 120)
            return result_data
        # Last resort: just return most-used tags overall
        rows = (
            await ArtworkTag.annotate(cnt=Count("id"))
            .group_by("tag_name")
            .order_by("-cnt")
            .limit(limit)
            .values("tag_name", "cnt")
        )
        result_data = [{"tag_name": r["tag_name"], "count": r["cnt"]} for r in rows]
        if result_data:
            await cache_set(cache_key, result_data, 120)
        return result_data

    # ═══════════════════════════════════════════════════════════════
    # 用户搜索（走 Meilisearch users 索引）
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    async def search_users(query: str, limit: int = 30, offset: int = 0) -> dict:
        """公开用户搜索"""
        result = await asyncio.to_thread(
            search_engine.search_users,
            query, None, ["followers_count:desc"], limit, offset
        )
        return {
            "hits": result.get("hits", []),
            "total": result.get("estimatedTotalHits", 0),
        }

    @staticmethod
    async def search_users_admin(
        query: str,
        role: Optional[str] = None,
        is_banned: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """管理员用户搜索"""
        parts = []
        if role:
            parts.append(f"role = '{role}'")
        if is_banned is not None:
            parts.append(f"is_banned = {'true' if is_banned else 'false'}")
        extra = " AND ".join(parts) if parts else None
        result = await asyncio.to_thread(
            search_engine.search_users_admin,
            query, extra, ["created_at:desc"], limit, offset
        )
        return {
            "hits": result.get("hits", []),
            "total": result.get("estimatedTotalHits", 0),
        }
