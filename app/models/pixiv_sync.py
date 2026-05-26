"""
Pixiv 分布式同步系统 — 数据模型

⚠️  PixivArtworkCache 表在清库时应单独备份：
    pg_dump -t pixiv_artwork_cache moetopia > pixiv_cache_backup.sql
"""
from tortoise import fields, models


class PixivSyncNode(models.Model):
    """已注册的 pixiv-agent 节点。"""
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    url = fields.CharField(max_length=500)
    api_key = fields.CharField(max_length=256)
    status = fields.CharField(max_length=20, default="online")  # online | offline | banned
    last_ping = fields.DatetimeField(null=True)
    author_count = fields.IntField(default=0)
    note = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pixiv_sync_nodes"


class PixivSyncAuthor(models.Model):
    """订阅同步的 Pixiv 作者列表。"""
    id = fields.IntField(pk=True)
    pixiv_user_id = fields.BigIntField(unique=True, index=True)
    pixiv_username = fields.CharField(max_length=100, null=True)
    assigned_node = fields.ForeignKeyField(
        "models.PixivSyncNode", related_name="authors", null=True, on_delete=fields.SET_NULL
    )
    moetopia_user_id = fields.IntField(null=True)
    sync_enabled = fields.BooleanField(default=True)
    claimed = fields.BooleanField(default=False)
    last_sync_at = fields.DatetimeField(null=True)
    artwork_count = fields.IntField(default=0)
    status = fields.CharField(max_length=20, default="pending")  # pending | syncing | done | failed
    error_msg = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pixiv_sync_authors"


class PixivSyncSubmission(models.Model):
    """用户提交的同步作者请求（需管理员审批）。"""
    id = fields.IntField(pk=True)
    submitter = fields.ForeignKeyField(
        "models.User", related_name="pixiv_sync_submissions", on_delete=fields.CASCADE
    )
    pixiv_user_id = fields.BigIntField(index=True)
    pixiv_username = fields.CharField(max_length=100, null=True)
    reason = fields.TextField(null=True)
    status = fields.CharField(max_length=20, default="pending")  # pending | approved | rejected
    reviewed_by_id = fields.IntField(null=True)
    admin_note = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    resolved_at = fields.DatetimeField(null=True)

    class Meta:
        table = "pixiv_sync_submissions"


class PixivArtworkCache(models.Model):
    """
    Pixiv 作品元数据持久化缓存。
    此表独立于主业务表，清库时应保留（pg_dump -t pixiv_artwork_cache）。
    重导入时优先从此表读取，避免重复访问 Pixiv API。
    """
    id = fields.IntField(pk=True)
    pixiv_id = fields.BigIntField(unique=True, index=True)
    pixiv_user_id = fields.BigIntField(index=True)
    node_name = fields.CharField(max_length=100, null=True)
    metadata = fields.JSONField()
    image_original_urls = fields.JSONField(default=list)
    image_local_paths = fields.JSONField(default=list)
    imported = fields.BooleanField(default=False)
    moetopia_artwork_id = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "pixiv_artwork_cache"
