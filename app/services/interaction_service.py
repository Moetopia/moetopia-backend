from fastapi import HTTPException
from tortoise.expressions import F
from app.models.interaction import Like, Bookmark, BookmarkFolder, Comment, CommentLike
from app.models.artwork import Artwork
from app.models.social import Notification
from app.schemas.interaction_schema import BookmarkCreate, CommentCreate, BookmarkFolderCreate
from app.services.notification_service import push_notification


class InteractionService:

    # ------------------------------------------------------------------
    # 点赞
    # ------------------------------------------------------------------

    @staticmethod
    async def toggle_like(user_id: int, artwork_id: int) -> bool:
        artwork = await Artwork.get_or_none(id=artwork_id)
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")

        like = await Like.get_or_none(user_id=user_id, artwork_id=artwork_id)
        if like:
            await like.delete()
            await Artwork.filter(id=artwork_id, like_count__gt=0).update(like_count=F("like_count") - 1)
            return False
        else:
            await Like.create(user_id=user_id, artwork_id=artwork_id)
            await Artwork.filter(id=artwork_id).update(like_count=F("like_count") + 1)

            if artwork.author_id != user_id:
                await push_notification(
                    user_id=artwork.author_id,
                    actor_id=user_id,
                    type="like",
                    content=f"赞了你的作品《{artwork.title}》",
                    related_entity_id=str(artwork_id),
                )
            return True

    # ------------------------------------------------------------------
    # 收藏
    # ------------------------------------------------------------------

    @staticmethod
    async def create_bookmark(user_id: int, artwork_id: int, data: BookmarkCreate):
        artwork = await Artwork.get_or_none(id=artwork_id)
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")

        # 如果指定了收藏夹，验证归属
        if data.folder_id is not None:
            folder = await BookmarkFolder.get_or_none(id=data.folder_id, user_id=user_id)
            if not folder:
                raise HTTPException(status_code=404, detail="Bookmark folder not found")

        bookmark, created = await Bookmark.get_or_create(
            user_id=user_id,
            artwork_id=artwork_id,
            defaults={
                "is_private": data.is_private,
                "user_custom_tags": data.user_custom_tags,
                "folder_id": data.folder_id,
            },
        )
        if not created:
            bookmark.is_private = data.is_private
            bookmark.user_custom_tags = data.user_custom_tags
            bookmark.folder_id = data.folder_id
            await bookmark.save()
        else:
            await Artwork.filter(id=artwork_id).update(bookmark_count=F("bookmark_count") + 1)
            if artwork.author_id != user_id and not data.is_private:
                await push_notification(
                    user_id=artwork.author_id,
                    actor_id=user_id,
                    type="bookmark",
                    content=f"收藏了你的作品《{artwork.title}》",
                    related_entity_id=str(artwork_id),
                )
        return bookmark

    @staticmethod
    async def delete_bookmark(user_id: int, artwork_id: int):
        bookmark = await Bookmark.get_or_none(user_id=user_id, artwork_id=artwork_id)
        if not bookmark:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        await Artwork.filter(id=artwork_id, bookmark_count__gt=0).update(bookmark_count=F("bookmark_count") - 1)
        await bookmark.delete()

    @staticmethod
    async def get_my_bookmarks(user_id: int, folder_id: int = None, limit: int = 30, offset: int = 0):
        qs = Bookmark.filter(user_id=user_id).prefetch_related("artwork__images")
        if folder_id is not None:
            qs = qs.filter(folder_id=folder_id)
        return await qs.order_by("-created_at").offset(offset).limit(limit)

    # ------------------------------------------------------------------
    # 收藏夹
    # ------------------------------------------------------------------

    @staticmethod
    async def create_folder(user_id: int, data: BookmarkFolderCreate) -> BookmarkFolder:
        from fastapi import HTTPException
        existing_count = await BookmarkFolder.filter(user_id=user_id).count()
        if existing_count >= 20:
            raise HTTPException(status_code=400, detail="最多只能创建 20 个收藏夹")
        return await BookmarkFolder.create(user_id=user_id, name=data.name, is_private=data.is_private)

    @staticmethod
    async def get_folders(user_id: int):
        return await BookmarkFolder.filter(user_id=user_id).order_by("created_at")

    @staticmethod
    async def delete_folder(folder_id: int, user_id: int):
        folder = await BookmarkFolder.get_or_none(id=folder_id, user_id=user_id)
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")
        await folder.delete()

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------

    @staticmethod
    async def post_comment(user_id: int, artwork_id: int, data: CommentCreate):
        artwork = await Artwork.get_or_none(id=artwork_id)
        if not artwork:
            raise HTTPException(status_code=404, detail="Artwork not found")

        # 拉黑检查：被作者拉黑的用户不能评论
        from app.models.social import UserBlock
        if await UserBlock.exists(blocker_id=artwork.author_id, blocked_id=user_id):
            raise HTTPException(status_code=403, detail="无法评论该作品")

        # Resolve the root parent so all replies stay at depth-1 (flat 2-level model).
        # reply_to tracks the specific user being addressed (for @mention).
        actual_parent_id = None
        reply_to_user_id = data.reply_to_user_id
        if data.parent_id:
            parent = await Comment.get_or_none(id=data.parent_id)
            if not parent:
                raise HTTPException(status_code=404, detail="Parent comment not found")
            # If this parent is itself a reply, point to the grandparent (root)
            actual_parent_id = parent.parent_id if parent.parent_id is not None else parent.id
            # Auto-fill reply_to if not explicitly provided
            if reply_to_user_id is None:
                reply_to_user_id = parent.user_id

        comment = await Comment.create(
            user_id=user_id,
            artwork_id=artwork_id,
            content=data.content,
            parent_id=actual_parent_id,
            reply_to_id=reply_to_user_id,
        )

        if artwork.author_id != user_id:
            await push_notification(
                user_id=artwork.author_id,
                actor_id=user_id,
                type="comment",
                content=f"评论了你的作品《{artwork.title}》：{data.content[:50]}",
                related_entity_id=str(artwork_id),
            )

        # 若是回复某人，且被回复者非作者（否则重复通知）
        if reply_to_user_id and reply_to_user_id != user_id and reply_to_user_id != artwork.author_id:
            await push_notification(
                user_id=reply_to_user_id,
                actor_id=user_id,
                type="comment",
                content=f"回复了你的评论：{data.content[:50]}",
                related_entity_id=str(artwork_id),
            )

        return comment

    @staticmethod
    async def delete_comment(comment_id: int, user_id: int, role: str = "user"):
        comment = await Comment.get_or_none(id=comment_id)
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        if comment.user_id != user_id and role not in ("admin", "moderator"):
            raise HTTPException(status_code=403, detail="Forbidden")
        comment.is_deleted = True
        comment.content = "[已删除]"
        await comment.save()

    @staticmethod
    async def toggle_comment_like(user_id: int, comment_id: int) -> bool:
        comment = await Comment.get_or_none(id=comment_id)
        if not comment:
            raise HTTPException(status_code=404, detail="Comment not found")
        cl = await CommentLike.get_or_none(user_id=user_id, comment_id=comment_id)
        if cl:
            await cl.delete()
            await Comment.filter(id=comment_id, like_count__gt=0).update(like_count=F("like_count") - 1)
            return False
        await CommentLike.create(user_id=user_id, comment_id=comment_id)
        await Comment.filter(id=comment_id).update(like_count=F("like_count") + 1)
        return True

    @staticmethod
    async def get_comments(artwork_id: int, limit: int = 50, offset: int = 0, sort: str = "latest"):
        order = "-like_count" if sort == "hot" else "-created_at"
        return (
            await Comment.filter(artwork_id=artwork_id, parent_id=None)
            .prefetch_related("replies__user", "replies__reply_to", "user")
            .order_by(order)
            .offset(offset)
            .limit(limit)
        )
