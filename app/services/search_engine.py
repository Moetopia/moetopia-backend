"""
强制安全注入层（Mandatory Server-Side Security Injection）
架构规约：所有对 Meilisearch 和 Qdrant 的查询必须经过此模块。
绝对禁止在 API 路由层或其他 Service 中直接调用搜索客户端。
"""
import logging
from typing import Optional, List, Any

from app.models.user import User
from app.infrastructure.meilisearch_client import meili_client
from app.infrastructure.qdrant_client import qdrant_client

logger = logging.getLogger(__name__)


class SearchEngine:

    # ---------------------------------------------------------------
    # 内部：构建强制过滤条件
    # ---------------------------------------------------------------

    @staticmethod
    def _build_meili_filter(user: Optional[User], extra_filter: Optional[str] = None) -> Optional[str]:
        """
        构建 Meilisearch 过滤字符串，强制注入安全条件。
        返回值直接传给 meili_client.search() 的 filter_exp 参数。
        """
        conditions: List[str] = []

        # 规则 1：R-18 屏蔽 —— 游客或 r18_enabled=False 时强制 safe
        if user is None or not user.r18_enabled:
            conditions.append("rating = 'safe'")

        # 规则 2：AI 内容屏蔽
        if user is not None and user.hide_ai_generated:
            conditions.append("is_ai = false")

        # 规则 3：标签黑名单
        if user is not None and user.muted_tags:
            for tag in user.muted_tags:
                safe_tag = tag.lower().replace("'", "\\'")
                conditions.append(f"NOT tags = '{safe_tag}'")

        # 规则 4：屏蔽用户内容（muted_user_ids）
        if user is not None and user.muted_user_ids:
            uid_list = ", ".join(str(uid) for uid in user.muted_user_ids)
            conditions.append(f"author_id NOT IN [{uid_list}]")

        # 规则 5：只返回公开作品
        conditions.append("visibility = 'public'")

        # 拼合前端传来的合法额外过滤条件（括号隔离防注入）
        if extra_filter:
            conditions.append(f"({extra_filter})")

        return " AND ".join(conditions) if conditions else None

    @staticmethod
    def _build_qdrant_filter(user: Optional[User], extra_conditions: Optional[List] = None):
        """
        构建 Qdrant Filter 对象，强制注入安全条件。
        """
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        must: List = []

        # 规则 1：R-18 屏蔽
        if user is None or not user.r18_enabled:
            must.append(FieldCondition(key="rating", match=MatchValue(value="safe")))

        # 规则 2：AI 内容屏蔽
        if user is not None and user.hide_ai_generated:
            must.append(FieldCondition(key="is_ai", match=MatchValue(value=False)))

        # 附加前端合法过滤条件
        if extra_conditions:
            must.extend(extra_conditions)

        return Filter(must=must) if must else None

    # ---------------------------------------------------------------
    # 公开接口 — artworks
    # ---------------------------------------------------------------

    @staticmethod
    def search_artworks(
        user: Optional[User],
        query: str,
        sort_by: Optional[List[str]] = None,
        extra_filter: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        attributes_to_search_on: Optional[List[str]] = None,
    ) -> dict:
        """安全文本搜索（走 Meilisearch artworks 索引）"""
        safe_filter = SearchEngine._build_meili_filter(user, extra_filter)
        logger.debug(f"[SearchEngine] Meili artworks filter = {safe_filter!r}")
        return meili_client.search(
            query=query,
            filter_exp=safe_filter,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
            attributes_to_search_on=attributes_to_search_on,
        )

    @staticmethod
    def browse_artworks(
        user: Optional[User],
        sort_by: Optional[List[str]] = None,
        extra_filter: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """无关键词浏览（排行榜 / Feed 场景），空 query + filter + sort"""
        safe_filter = SearchEngine._build_meili_filter(user, extra_filter)
        logger.debug(f"[SearchEngine] Meili browse filter = {safe_filter!r}, sort = {sort_by}")
        return meili_client.search(
            query="",
            filter_exp=safe_filter,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    # ---------------------------------------------------------------
    # 公开接口 — users
    # ---------------------------------------------------------------

    @staticmethod
    def search_users(
        query: str,
        filter_exp: Optional[str] = None,
        sort_by: Optional[List[str]] = None,
        limit: int = 30,
        offset: int = 0,
    ) -> dict:
        """用户搜索（走 Meilisearch users 索引）"""
        params: List[str] = []
        if filter_exp:
            params.append(filter_exp)
        # 默认过滤掉封禁用户
        params.append("is_banned = false")
        combined = " AND ".join(params) if params else None
        logger.debug(f"[SearchEngine] Meili users filter = {combined!r}")
        return meili_client.search_users(
            query=query,
            filter_exp=combined,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def search_users_admin(
        query: str,
        filter_exp: Optional[str] = None,
        sort_by: Optional[List[str]] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """管理员用户搜索（不过滤封禁）"""
        return meili_client.search_users(
            query=query,
            filter_exp=filter_exp,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )

    # ---------------------------------------------------------------
    # 公开接口 — tags
    # ---------------------------------------------------------------

    @staticmethod
    def search_tags(
        query: str,
        sort_by: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """标签搜索 / 自动补全（走 Meilisearch tags 索引）"""
        return meili_client.search_tags(
            query=query,
            sort_by=sort_by or ["count:desc"],
            limit=limit,
            offset=offset,
        )

    # ---------------------------------------------------------------
    # 公开接口 — 向量搜索
    # ---------------------------------------------------------------

    @staticmethod
    def search_by_vector(
        user: Optional[User],
        query_vector: List[float],
        extra_conditions: Optional[List] = None,
        limit: int = 20,
    ) -> List[Any]:
        """安全向量相似度搜索（以图搜图，走 Qdrant）"""
        safe_filter = SearchEngine._build_qdrant_filter(user, extra_conditions)
        logger.debug(f"[SearchEngine] Qdrant filter = {safe_filter}")
        return qdrant_client.search_similar(
            query_vector=query_vector,
            query_filter=safe_filter,
            limit=limit,
        )

    # ---------------------------------------------------------------
    # 辅助
    # ---------------------------------------------------------------

    @staticmethod
    def get_meili_filter_for_user(user: Optional[User], extra_filter: Optional[str] = None) -> Optional[str]:
        """仅获取过滤字符串，供需要直接操作 Meili 客户端的场景使用"""
        return SearchEngine._build_meili_filter(user, extra_filter)


# 全局单例
search_engine = SearchEngine()
