from app.models.tag import ConceptAnchor
from app.schemas.tag_schema import AnchorCreate
import uuid

class TagService:
    @staticmethod
    async def get_system_tags(limit: int = 50):
        # 简单返回基础的标签锚点
        tags = await ConceptAnchor.all().limit(limit)
        return tags
        
    @staticmethod
    async def register_anchor(data: AnchorCreate):
        # 创建新的基准点
        anchor = await ConceptAnchor.create(
            tag_name=data.tag_name,
            namespace=data.namespace
        )
        return anchor

    @staticmethod
    async def get_pending_tags():
        # 这里模拟返回需要人工审核的待定标签
        # 实际业务中可能是查询置信度位于 0.35 到 0.60 之间的预测结果
        return []
