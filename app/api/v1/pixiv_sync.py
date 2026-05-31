"""
Pixiv 分布式同步 API
- /admin/pixiv-sync/*  — 管理员操作（节点管理、作者管理、审批提交）
- /pixiv-sync/submit   — 用户提交同步请求（需登录）
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_current_user, require_role
from app.models.user import User
from app.models.pixiv_sync import PixivSyncNode, PixivSyncAuthor, PixivSyncSubmission, PixivArtworkCache
from app.schemas.common import ResponseBase

router = APIRouter()

_ADMIN = require_role(["admin"])
_STAFF = require_role(["admin", "moderator"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────

class NodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=500)
    api_key: str = Field(..., min_length=8, max_length=256)
    note: Optional[str] = None


class AuthorAdd(BaseModel):
    pixiv_user_id: int
    pixiv_username: Optional[str] = None


class SubmissionCreate(BaseModel):
    pixiv_user_id: int
    pixiv_username: Optional[str] = None
    reason: Optional[str] = Field(None, max_length=1000)


class SubmissionResolve(BaseModel):
    admin_note: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────
# 节点管理
# ─────────────────────────────────────────────────────────────────────

@router.get("/admin/pixiv-sync/nodes", response_model=ResponseBase[List[dict]])
async def list_nodes(current_user: User = Depends(_ADMIN)):
    """列出所有已注册的 pixiv-agent 节点。"""
    nodes = await PixivSyncNode.all().order_by("created_at")
    return ResponseBase(data=[{
        "id": n.id,
        "name": n.name,
        "url": n.url,
        "status": n.status,
        "last_ping": n.last_ping.isoformat() if n.last_ping else None,
        "author_count": n.author_count,
        "note": n.note,
        "created_at": n.created_at.isoformat(),
    } for n in nodes])


@router.post("/admin/pixiv-sync/nodes", response_model=ResponseBase[dict])
async def register_node(body: NodeCreate, current_user: User = Depends(_ADMIN)):
    """注册新 pixiv-agent 节点。"""
    node = await PixivSyncNode.create(
        name=body.name,
        url=body.url.rstrip("/"),
        api_key=body.api_key,
        note=body.note,
        status="online",
    )
    return ResponseBase(data={"id": node.id, "name": node.name})


@router.get("/admin/pixiv-sync/nodes/{node_id}/status", response_model=ResponseBase[dict])
async def node_status(node_id: int, current_user: User = Depends(_STAFF)):
    """代理节点 /health 请求，实时返回节点状态和统计。"""
    import httpx
    from app.services.pixiv_sync_service import _node_headers
    node = await PixivSyncNode.get_or_none(id=node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{node.url}/health",
                params={"include_logs": "false"},
                headers=_node_headers(node),
            )
            resp.raise_for_status()
            
            node.status = "online"
            node.last_ping = _now()
            await node.save(update_fields=["status", "last_ping"])
            
            return ResponseBase(data=resp.json())
    except Exception as e:
        node.status = "offline"
        await node.save(update_fields=["status"])
        raise HTTPException(status_code=502, detail=f"节点请求失败: {e}")


@router.get("/admin/pixiv-sync/nodes/{node_id}/logs", response_model=ResponseBase[dict])
async def node_logs(
    node_id: int,
    limit: int = Query(100, ge=1, le=500),
    level: Optional[str] = Query(None),
    current_user: User = Depends(_STAFF),
):
    """代理节点 /logs 请求，实时返回节点日志。"""
    import httpx
    from app.services.pixiv_sync_service import _node_headers
    node = await PixivSyncNode.get_or_none(id=node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    params = {"limit": limit, "source": "db"}
    if level:
        params["level"] = level
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{node.url}/logs",
                params=params,
                headers=_node_headers(node),
            )
            resp.raise_for_status()
            return ResponseBase(data=resp.json())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"节点请求失败: {e}")


@router.delete("/admin/pixiv-sync/nodes/{node_id}", response_model=ResponseBase[dict])
async def deregister_node(node_id: int, current_user: User = Depends(_ADMIN)):
    """注销节点，同时将其下所有作者重置为未分配。"""
    node = await PixivSyncNode.get_or_none(id=node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    await PixivSyncAuthor.filter(assigned_node_id=node_id).update(
        assigned_node=None, status="pending"
    )
    await node.delete()
    return ResponseBase(data={"message": f"节点 {node_id} 已注销，其作者已重置为待分配"})


# ─────────────────────────────────────────────────────────────────────
# 作者订阅管理
# ─────────────────────────────────────────────────────────────────────

@router.get("/admin/pixiv-sync/authors", response_model=ResponseBase[List[dict]])
async def list_sync_authors(
    status: Optional[str] = Query(None),
    node_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """列出订阅作者列表。"""
    qs = PixivSyncAuthor.all()
    if status:
        qs = qs.filter(status=status)
    if node_id:
        qs = qs.filter(assigned_node_id=node_id)
    authors = await qs.offset(offset).limit(limit).order_by("-created_at")
    return ResponseBase(data=[{
        "id": a.id,
        "pixiv_user_id": a.pixiv_user_id,
        "pixiv_username": a.pixiv_username,
        "assigned_node_id": a.assigned_node_id,
        "moetopia_user_id": a.moetopia_user_id,
        "sync_enabled": a.sync_enabled,
        "claimed": a.claimed,
        "status": a.status,
        "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
        "artwork_count": a.artwork_count,
        "error_msg": a.error_msg,
        "created_at": a.created_at.isoformat(),
    } for a in authors])


@router.post("/admin/pixiv-sync/authors", response_model=ResponseBase[dict])
async def add_sync_author(body: AuthorAdd, current_user: User = Depends(_ADMIN)):
    """管理员直接添加 Pixiv 作者到同步列表。"""
    existing = await PixivSyncAuthor.get_or_none(pixiv_user_id=body.pixiv_user_id)
    if existing:
        raise HTTPException(status_code=409, detail="该作者已在同步列表中")
    author = await PixivSyncAuthor.create(
        pixiv_user_id=body.pixiv_user_id,
        pixiv_username=body.pixiv_username,
        status="pending",
        sync_enabled=True,
    )
    return ResponseBase(data={"id": author.id, "pixiv_user_id": author.pixiv_user_id})


@router.delete("/admin/pixiv-sync/authors/{pixiv_user_id}", response_model=ResponseBase[dict])
async def remove_sync_author(pixiv_user_id: int, current_user: User = Depends(_ADMIN)):
    """取消订阅某作者。"""
    author = await PixivSyncAuthor.get_or_none(pixiv_user_id=pixiv_user_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不在同步列表中")
    await author.delete()
    return ResponseBase(data={"message": f"已取消订阅作者 {pixiv_user_id}"})


@router.post("/admin/pixiv-sync/authors/{pixiv_user_id}/reassign", response_model=ResponseBase[dict])
async def reassign_author(
    pixiv_user_id: int,
    node_id: int = Query(..., description="目标节点 ID"),
    current_user: User = Depends(_ADMIN),
):
    """手动将作者重新分配到指定节点。"""
    author = await PixivSyncAuthor.get_or_none(pixiv_user_id=pixiv_user_id)
    if not author:
        raise HTTPException(status_code=404, detail="作者不在同步列表中")
    node = await PixivSyncNode.get_or_none(id=node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    author.assigned_node_id = node_id
    author.status = "pending"
    await author.save(update_fields=["assigned_node_id", "status"])
    for nid in {author.assigned_node_id, node_id}:
        if nid:
            count = await PixivSyncAuthor.filter(assigned_node_id=nid).count()
            await PixivSyncNode.filter(id=nid).update(author_count=count)
            
    import httpx
    import logging
    from app.services.pixiv_sync_service import _node_headers
    logger = logging.getLogger(__name__)
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{node.url}/sync/author/{author.pixiv_user_id}",
                headers=_node_headers(node),
            )
            if resp.status_code != 200:
                logger.error(f"[PixivSync] 节点 {node.name} 接收重新分配作者失败: {resp.status_code}")
    except Exception as e:
        logger.error(f"[PixivSync] 通知重新分配作者到节点 {node.name} 出错: {e}")
        
    return ResponseBase(data={"pixiv_user_id": pixiv_user_id, "assigned_node_id": node_id})


@router.post("/admin/pixiv-sync/trigger", response_model=ResponseBase[dict])
async def trigger_sync(current_user: User = Depends(_ADMIN)):
    """手动触发一次全量分发（将未分配作者分配到节点，并通知节点开始同步）。"""
    from app.worker.enqueue import enqueue
    await enqueue("task_pixiv_sync_assign")
    return ResponseBase(data={"message": "同步分发任务已入队"})


# ─────────────────────────────────────────────────────────────────────
# 缓存管理
# ─────────────────────────────────────────────────────────────────────

@router.get("/admin/pixiv-sync/cache", response_model=ResponseBase[dict])
async def cache_stats(current_user: User = Depends(_STAFF)):
    """PixivArtworkCache 统计。"""
    total = await PixivArtworkCache.all().count()
    imported = await PixivArtworkCache.filter(imported=True).count()
    pending = total - imported
    return ResponseBase(data={"total": total, "imported": imported, "pending_import": pending})


@router.post("/admin/pixiv-sync/cache/reimport", response_model=ResponseBase[dict])
async def trigger_cache_reimport(current_user: User = Depends(_ADMIN)):
    """从 PixivArtworkCache 批量导入未导入的作品（清库恢复用）。"""
    from app.worker.enqueue import enqueue
    await enqueue("task_pixiv_sync_import_cached")
    return ResponseBase(data={"message": "缓存恢复导入任务已入队"})


# ─────────────────────────────────────────────────────────────────────
# 用户提交管理
# ─────────────────────────────────────────────────────────────────────

@router.get("/admin/pixiv-sync/submissions", response_model=ResponseBase[List[dict]])
async def list_submissions(
    status: Optional[str] = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(_STAFF),
):
    """列出用户提交的同步请求。"""
    qs = PixivSyncSubmission.all().prefetch_related("submitter")
    if status:
        qs = qs.filter(status=status)
    items = await qs.offset(offset).limit(limit).order_by("-created_at")
    result = []
    for s in items:
        try:
            submitter_name = s.submitter.username
        except Exception:
            submitter_name = None
        result.append({
            "id": s.id,
            "submitter_id": s.submitter_id,
            "submitter_username": submitter_name,
            "pixiv_user_id": s.pixiv_user_id,
            "pixiv_username": s.pixiv_username,
            "reason": s.reason,
            "status": s.status,
            "admin_note": s.admin_note,
            "created_at": s.created_at.isoformat(),
            "resolved_at": s.resolved_at.isoformat() if s.resolved_at else None,
        })
    return ResponseBase(data=result)


@router.post("/admin/pixiv-sync/submissions/{submission_id}/approve", response_model=ResponseBase[dict])
async def approve_submission(
    submission_id: int,
    body: SubmissionResolve,
    current_user: User = Depends(_ADMIN),
):
    """审批通过：将作者加入同步列表。"""
    sub = await PixivSyncSubmission.get_or_none(id=submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    if sub.status != "pending":
        raise HTTPException(status_code=400, detail=f"申请状态已为 {sub.status}")

    existing = await PixivSyncAuthor.get_or_none(pixiv_user_id=sub.pixiv_user_id)
    if not existing:
        await PixivSyncAuthor.create(
            pixiv_user_id=sub.pixiv_user_id,
            pixiv_username=sub.pixiv_username,
            status="pending",
            sync_enabled=True,
        )

    sub.status = "approved"
    sub.admin_note = body.admin_note
    sub.reviewed_by_id = current_user.id
    sub.resolved_at = _now()
    await sub.save(update_fields=["status", "admin_note", "reviewed_by_id", "resolved_at"])
    return ResponseBase(data={"message": f"已批准，作者 {sub.pixiv_user_id} 已加入同步列表"})


@router.post("/admin/pixiv-sync/submissions/{submission_id}/reject", response_model=ResponseBase[dict])
async def reject_submission(
    submission_id: int,
    body: SubmissionResolve,
    current_user: User = Depends(_ADMIN),
):
    """拒绝提交请求。"""
    sub = await PixivSyncSubmission.get_or_none(id=submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    if sub.status != "pending":
        raise HTTPException(status_code=400, detail=f"申请状态已为 {sub.status}")
    sub.status = "rejected"
    sub.admin_note = body.admin_note
    sub.reviewed_by_id = current_user.id
    sub.resolved_at = _now()
    await sub.save(update_fields=["status", "admin_note", "reviewed_by_id", "resolved_at"])
    return ResponseBase(data={"message": "已拒绝"})


# ─────────────────────────────────────────────────────────────────────
# 用户提交接口
# ─────────────────────────────────────────────────────────────────────

@router.post("/pixiv-sync/submit", response_model=ResponseBase[dict])
async def submit_sync_request(
    body: SubmissionCreate,
    current_user: User = Depends(get_current_user),
):
    """用户提交同步某 Pixiv 作者的请求，待管理员审批。"""
    already_synced = await PixivSyncAuthor.exists(pixiv_user_id=body.pixiv_user_id, sync_enabled=True)
    if already_synced:
        raise HTTPException(status_code=409, detail="该作者已在同步列表中")

    duplicate = await PixivSyncSubmission.exists(
        pixiv_user_id=body.pixiv_user_id,
        submitter_id=current_user.id,
        status="pending",
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="你已提交过该作者的同步申请，请等待审核")

    sub = await PixivSyncSubmission.create(
        submitter_id=current_user.id,
        pixiv_user_id=body.pixiv_user_id,
        pixiv_username=body.pixiv_username,
        reason=body.reason,
        status="pending",
    )
    return ResponseBase(data={"id": sub.id, "message": "已提交，请等待管理员审核"})
