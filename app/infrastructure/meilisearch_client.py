import logging
import meilisearch
from typing import List, Dict, Any, Optional
from app.core.config import settings

logger = logging.getLogger(__name__)

# ── 索引名常量 ──
IDX_ARTWORKS = "artworks"
IDX_USERS = "users"
IDX_TAGS = "tags"


class MeiliSearchManager:
    """
    Meilisearch 客户端管理器：负责所有文本搜索、标签过滤、拼写纠错。
    管理三个索引：artworks、users、tags。
    """

    def __init__(self):
        self.client = meilisearch.Client(settings.MEILI_URL, settings.MEILI_MASTER_KEY)
        self.index_name = IDX_ARTWORKS  # 向后兼容

    # ═══════════════════════════════════════════════════════════════
    # 索引初始化
    # ═══════════════════════════════════════════════════════════════

    def init_index(self) -> None:
        """应用启动时调用，初始化全部三个索引。"""
        self._init_artworks_index()
        self._init_users_index()
        self._init_tags_index()

    def _init_artworks_index(self) -> None:
        try:
            self.client.create_index(IDX_ARTWORKS, {'primaryKey': 'id'})
        except Exception:
            pass
        idx = self.client.index(IDX_ARTWORKS)
        idx.update_searchable_attributes([
            'title', 'description', 'tags', 'author_name',
        ])
        idx.update_filterable_attributes([
            'tags', 'is_ai', 'rating', 'author_id', 'visibility',
            'artwork_type', 'bookmark_count', 'created_at', 'like_count', 'view_count',
            'content_origin',
        ])
        idx.update_sortable_attributes([
            'created_at', 'like_count', 'view_count', 'bookmark_count',
        ])
        logger.info("✅ Meilisearch [artworks] 索引初始化成功")

    def _init_users_index(self) -> None:
        try:
            self.client.create_index(IDX_USERS, {'primaryKey': 'id'})
        except Exception:
            pass
        idx = self.client.index(IDX_USERS)
        idx.update_searchable_attributes([
            'username', 'bio',
        ])
        idx.update_filterable_attributes([
            'role', 'is_creator', 'is_banned', 'followers_count', 'created_at',
        ])
        idx.update_sortable_attributes([
            'followers_count', 'created_at',
        ])
        logger.info("✅ Meilisearch [users] 索引初始化成功")

    def _init_tags_index(self) -> None:
        try:
            self.client.create_index(IDX_TAGS, {'primaryKey': 'id'})
        except Exception:
            pass
        idx = self.client.index(IDX_TAGS)
        idx.update_searchable_attributes([
            'tag_name',
        ])
        idx.update_filterable_attributes([
            'count', 'count_7d',
        ])
        idx.update_sortable_attributes([
            'count', 'count_7d',
        ])
        logger.info("✅ Meilisearch [tags] 索引初始化成功")

    # ═══════════════════════════════════════════════════════════════
    # artworks 索引操作
    # ═══════════════════════════════════════════════════════════════

    def add_documents(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量添加或更新作品文档"""
        try:
            task = self.client.index(IDX_ARTWORKS).add_documents(documents)
            logger.debug(f"Meilisearch artworks 写入任务，Task: {task.task_uid}")
            return task
        except Exception as e:
            logger.error(f"❌ Meilisearch artworks 写入失败: {e}")
            raise e

    def delete_document(self, document_id: str) -> None:
        """删除作品文档"""
        try:
            self.client.index(IDX_ARTWORKS).delete_document(document_id)
            logger.info(f"🗑️ Meilisearch artworks 已删除: {document_id}")
        except Exception as e:
            logger.error(f"❌ Meilisearch artworks 删除失败: {e}")

    def search(self, query: str, filter_exp: Optional[str] = None,
               sort_by: Optional[List[str]] = None, limit: int = 50,
               offset: int = 0,
               attributes_to_search_on: Optional[List[str]] = None) -> Dict[str, Any]:
        """作品文本搜索"""
        return self._search_index(IDX_ARTWORKS, query, filter_exp, sort_by, limit, offset, attributes_to_search_on)

    # ═══════════════════════════════════════════════════════════════
    # users 索引操作
    # ═══════════════════════════════════════════════════════════════

    def add_users(self, documents: List[Dict[str, Any]]):
        """批量添加或更新用户文档"""
        try:
            task = self.client.index(IDX_USERS).add_documents(documents)
            logger.debug(f"Meilisearch users 写入任务，Task: {task.task_uid}")
            return task
        except Exception as e:
            logger.error(f"❌ Meilisearch users 写入失败: {e}")
            raise e

    def delete_user(self, user_id: str) -> None:
        try:
            self.client.index(IDX_USERS).delete_document(user_id)
        except Exception as e:
            logger.error(f"❌ Meilisearch users 删除失败: {e}")

    def search_users(self, query: str, filter_exp: Optional[str] = None,
                     sort_by: Optional[List[str]] = None, limit: int = 30,
                     offset: int = 0) -> Dict[str, Any]:
        """用户搜索"""
        return self._search_index(IDX_USERS, query, filter_exp, sort_by, limit, offset)

    # ═══════════════════════════════════════════════════════════════
    # tags 索引操作
    # ═══════════════════════════════════════════════════════════════

    def add_tags(self, documents: List[Dict[str, Any]]):
        """批量添加或更新标签文档"""
        try:
            task = self.client.index(IDX_TAGS).add_documents(documents)
            logger.debug(f"Meilisearch tags 写入任务，Task: {task.task_uid}")
            return task
        except Exception as e:
            logger.error(f"❌ Meilisearch tags 写入失败: {e}")
            raise e

    def delete_tag(self, tag_id: str) -> None:
        try:
            self.client.index(IDX_TAGS).delete_document(tag_id)
        except Exception as e:
            logger.error(f"❌ Meilisearch tags 删除失败: {e}")

    def search_tags(self, query: str, filter_exp: Optional[str] = None,
                    sort_by: Optional[List[str]] = None, limit: int = 20,
                    offset: int = 0) -> Dict[str, Any]:
        """标签搜索 / 自动补全"""
        return self._search_index(IDX_TAGS, query, filter_exp, sort_by, limit, offset)

    # ═══════════════════════════════════════════════════════════════
    # 通用搜索
    # ═══════════════════════════════════════════════════════════════

    def _search_index(self, index_name: str, query: str,
                      filter_exp: Optional[str] = None,
                      sort_by: Optional[List[str]] = None,
                      limit: int = 50, offset: int = 0,
                      attributes_to_search_on: Optional[List[str]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {'limit': limit, 'offset': offset}
        if filter_exp:
            params['filter'] = filter_exp
        if sort_by:
            params['sort'] = sort_by
        if attributes_to_search_on:
            params['attributesToSearchOn'] = attributes_to_search_on
        try:
            return self.client.index(index_name).search(query, params)
        except Exception as e:
            logger.error(f"❌ Meilisearch [{index_name}] 查询失败: {e}")
            return {"hits": [], "estimatedTotalHits": 0}


# 导出一个全局单例，供 Services 层调用
meili_client = MeiliSearchManager()