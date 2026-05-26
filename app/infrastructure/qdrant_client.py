import logging
from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter
from app.core.config import settings

logger = logging.getLogger(__name__)


class QdrantManager:
    """
    Qdrant 客户端管理器：负责 9083 维 AI 特征向量的高效存储与“以图搜图”
    """

    def __init__(self):
        # 建立 HTTP/gRPC 连接
        self.client = QdrantClient(url=settings.QDRANT_URL)
        self.collection_name = "artworks_vectors"
        self.style_collection_name = "style_refs"
        # 严格匹配 WD14 模型提取出的数组长度
        self.vector_size = 9083

    def init_collection(self) -> None:
        """
        初始化向量集合 (应用启动时调用)
        """
        try:
            collections_response = self.client.get_collections()
            existing_collections = [c.name for c in collections_response.collections]

            for col in (self.collection_name, self.style_collection_name):
                if col not in existing_collections:
                    self.client.create_collection(
                        collection_name=col,
                        vectors_config=VectorParams(
                            size=self.vector_size,
                            distance=Distance.COSINE
                        ),
                    )
                    logger.info(f"✅ Qdrant [{col}] 集合初始化成功！")
                else:
                    logger.info(f"✅ Qdrant [{col}] 集合已存在，跳过创建。")
        except Exception as e:
            logger.error(f"❌ Qdrant 初始化失败: {e}")

    def upsert_vector(self, image_id: str, vector: List[float], payload: Optional[Dict[str, Any]] = None) -> None:
        """
        插入或更新向量数据
        :param image_id: 必须是 UUID 格式字符串，对应 Postgres 中 ArtworkImage 的 ID
        :param vector: 9083 维特征浮点数组
        :param payload: 绑定的元数据（用于硬过滤，如 {"rating": "safe", "is_ai": false}）
        """
        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    PointStruct(
                        id=str(image_id),
                        vector=vector,
                        payload=payload or {}
                    )
                ]
            )
            logger.debug(f"Qdrant 向量已成功写入，Image ID: {image_id}")
        except Exception as e:
            logger.error(f"❌ Qdrant 向量写入失败: {e}")
            raise e

    def delete_vector(self, image_id: str) -> None:
        """
        根据 ID 删除向量
        """
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=[str(image_id)]
            )
            logger.info(f"🗑️ Qdrant 已删除向量: {image_id}")
        except Exception as e:
            logger.error(f"❌ Qdrant 向量删除失败: {e}")

    def search_similar(self, query_vector: List[float], query_filter: Optional[Filter] = None, limit: int = 20) -> List[
        Any]:
        """
        执行“以图搜图”相似度匹配
        :param query_vector: 用户上传图片的特征向量
        :param query_filter: 安全强制过滤条件 (用于隔离 R18/AI)
        :param limit: 返回相似图片的数量限制
        """
        try:
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 相似度检索失败: {e}")
            return []

    def upsert_style_ref(self, ref_id: str, vector: List[float], payload: Optional[Dict[str, Any]] = None) -> None:
        """插入或更新风格参考图向量"""
        try:
            self.client.upsert(
                collection_name=self.style_collection_name,
                points=[PointStruct(id=ref_id, vector=vector, payload=payload or {})],
            )
            logger.debug(f"Qdrant 风格向量已写入，Ref ID: {ref_id}")
        except Exception as e:
            logger.error(f"❌ Qdrant 风格向量写入失败: {e}")
            raise e

    def delete_style_ref(self, ref_id: str) -> None:
        """删除风格参考图向量"""
        try:
            self.client.delete(collection_name=self.style_collection_name, points_selector=[ref_id])
        except Exception as e:
            logger.error(f"❌ Qdrant 风格向量删除失败: {e}")

    def search_duplicates(self, vector: List[float], score_threshold: float = 0.97, limit: int = 5) -> List[Any]:
        """搜索与给定向量高度相似的图片（用于上传时撞车检测）"""
        try:
            # 先不带阈值查 top-1，记录最高相似度（便于诊断阈值是否合适）
            probe = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                limit=1,
                with_payload=False,
            )
            if probe.points:
                best_score = probe.points[0].score
                logger.info(f"🔍 [撞车诊断] 当前 Qdrant 最高相似度: {best_score:.4f}（阈值={score_threshold}）")
            else:
                logger.info("🔍 [撞车诊断] Qdrant artworks_vectors 为空，无可比较向量")

            response = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 撞车检测失败: {e}")
            return []

    def search_anchors(self, query_vector: List[float], limit: int = 5, score_threshold: float = 0.65) -> List[Any]:
        """用新作品向量查询最相似的锚点基准图（用于锚点基准图解包）"""
        try:
            response = self.client.query_points(
                collection_name=self.style_collection_name,
                query=query_vector,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 锚点搜索失败: {e}")
            return []

    def search_by_style_ref(self, ref_id: str, query_filter: Optional[Filter] = None, limit: int = 30) -> List[Any]:
        """以风格参考图 ID 为查询向量，在 artworks_vectors 中搜索风格相似作品"""
        try:
            # 先从 style_refs 取回该参考图的向量
            results = self.client.retrieve(
                collection_name=self.style_collection_name,
                ids=[ref_id],
                with_vectors=True,
            )
            if not results:
                return []
            vector = results[0].vector
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 风格相似搜索失败: {e}")
            return []

    def retrieve_vectors_by_ids(self, image_ids: List[str]) -> Dict[str, List[float]]:
        """批量按 image_id 取回向量，返回 {image_id: vector} 字典（无向量则跳过）"""
        if not image_ids:
            return {}
        try:
            results = self.client.retrieve(
                collection_name=self.collection_name,
                ids=image_ids,
                with_vectors=True,
                with_payload=False,
            )
            return {str(r.id): r.vector for r in results if r.vector}
        except Exception as e:
            logger.error(f"❌ Qdrant 批量向量获取失败: {e}")
            return {}

    def search_similar_to_vector(
        self,
        query_vector: List[float],
        exclude_artwork_id: str,
        limit: int = 32,
    ) -> List[Any]:
        """用给定向量搜索相似作品，排除 exclude_artwork_id 本身（用于相似推荐）"""
        from qdrant_client.models import FieldCondition, MatchValue
        try:
            must_not = [FieldCondition(key="artwork_id", match=MatchValue(value=exclude_artwork_id))]
            q_filter = Filter(must_not=must_not)
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                query_filter=q_filter,
                limit=limit,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 相似推荐检索失败: {e}")
            return []

    def search_similar_to_vectors(
        self,
        query_vector: List[float],
        limit: int = 100,
    ) -> List[Any]:
        """用给定向量搜索 top-N 相似点（用于用户个性化推荐，不过滤 artwork_id）"""
        try:
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                limit=limit,
                with_payload=True,
            )
            return response.points
        except Exception as e:
            logger.error(f"❌ Qdrant 用户推荐检索失败: {e}")
            return []


# 导出一个全局单例，供 Services 层调用
qdrant_client = QdrantManager()