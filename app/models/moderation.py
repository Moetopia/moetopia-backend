from tortoise import fields, models


class ModerationQueue(models.Model):
    """内容审核队列 — 存储疑似违规作品，等待人工复核

    reason 取值：
      duplicate_suspected  — Qdrant 向量相似度 ≥ 0.97（近似撞车）
      r18_detected         — AI 判定 safe 作品含 R18 内容（explicit prob > 0.5）
      explicit_suspected   — AI 判定疑似含 explicit 内容（explicit prob 0.3~0.5）
      illegal_suspected    — AI 检测到疑似违法内容标签（loli/shota + explicit）
      manual               — 管理员手动加入队列
    """
    id = fields.IntField(pk=True)
    artwork = fields.ForeignKeyField(
        'models.Artwork', related_name='moderation_entries', on_delete=fields.CASCADE
    )

    reason = fields.CharField(max_length=30)
    confidence = fields.FloatField(default=0.0)           # AI 置信度 / Qdrant 余弦相似度（0~1）
    duplicate_of_artwork_id = fields.IntField(null=True)  # 仅 duplicate_suspected 时非空

    status = fields.CharField(max_length=20, default='pending')  # pending | approved | rejected

    reviewer = fields.ForeignKeyField(
        'models.User', related_name='moderation_reviews', null=True, on_delete=fields.SET_NULL
    )
    reviewer_note = fields.TextField(null=True)
    reviewed_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "moderation_queue"
