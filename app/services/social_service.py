from fastapi import HTTPException
from app.models.user import User
from app.models.social import Follow, UserBlock, Notification
from app.services.notification_service import push_notification


class SocialService:

    # ------------------------------------------------------------------
    # 关注
    # ------------------------------------------------------------------

    @staticmethod
    async def toggle_follow(follower_id: int, followed_id: int, is_private: bool = False) -> dict:
        """切换关注状态，返回 {following, is_private}。
        非公开关注限额：普通用户 100，会员 1000。"""
        if follower_id == followed_id:
            raise HTTPException(status_code=400, detail="不能关注自己")

        if not await User.exists(id=followed_id):
            raise HTTPException(status_code=404, detail="User not found")

        if await UserBlock.exists(blocker_id=followed_id, blocked_id=follower_id):
            raise HTTPException(status_code=403, detail="Cannot follow this user")

        from app.infrastructure.cache import invalidate_following, invalidate_block_filter
        follow_record = await Follow.get_or_none(follower_id=follower_id, followed_id=followed_id)
        if follow_record:
            await follow_record.delete()
            await invalidate_following(follower_id)
            return {"following": False, "is_private": False}
        else:
            if is_private:
                from datetime import datetime, timezone
                from app.models.user_membership import UserMembership
                has_membership = await UserMembership.filter(
                    user_id=follower_id, status="active",
                    expires_at__gte=datetime.now(timezone.utc)
                ).exists()
                private_limit = 1000 if has_membership else 100
                private_count = await Follow.filter(follower_id=follower_id, is_private=True).count()
                if private_count >= private_limit:
                    raise HTTPException(
                        status_code=403,
                        detail=f"已达非公开关注上限（{private_limit} 人）{'，升级会员可提升至 1000 人' if not has_membership else ''}",
                    )
            await Follow.create(follower_id=follower_id, followed_id=followed_id, is_private=is_private)
            await invalidate_following(follower_id)
            if not is_private:
                await push_notification(
                    user_id=followed_id,
                    actor_id=follower_id,
                    type="follow",
                    content="关注了你",
                )
            return {"following": True, "is_private": is_private}

    @staticmethod
    async def get_followers(user_id: int, limit: int = 30, offset: int = 0):
        follows = await Follow.filter(followed_id=user_id).prefetch_related("follower").offset(offset).limit(limit)
        return [f.follower for f in follows]

    @staticmethod
    async def get_following(user_id: int, limit: int = 30, offset: int = 0, exclude_private: bool = False):
        qs = Follow.filter(follower_id=user_id)
        if exclude_private:
            qs = qs.filter(is_private=False)
        follows = await qs.prefetch_related("followed").offset(offset).limit(limit)
        return [f.followed for f in follows]

    @staticmethod
    async def get_friends(user_id: int, limit: int = 30, offset: int = 0):
        """好友 = 双向互关（互相关注）"""
        my_following = set(await Follow.filter(follower_id=user_id, is_private=False).values_list("followed_id", flat=True))
        my_followers = set(await Follow.filter(followed_id=user_id, is_private=False).values_list("follower_id", flat=True))
        friend_ids = sorted(my_following & my_followers)
        page_ids = friend_ids[offset: offset + limit]
        if not page_ids:
            return []
        users = await User.filter(id__in=page_ids)
        id_map = {u.id: u for u in users}
        return [id_map[fid] for fid in page_ids if fid in id_map]

    @staticmethod
    async def is_following(follower_id: int, followed_id: int) -> bool:
        return await Follow.exists(follower_id=follower_id, followed_id=followed_id)

    # ------------------------------------------------------------------
    # 拉黑
    # ------------------------------------------------------------------

    @staticmethod
    async def toggle_block(blocker_id: int, blocked_id: int) -> bool:
        """切换拉黑状态，返回当前是否已拉黑"""
        if blocker_id == blocked_id:
            raise HTTPException(status_code=400, detail="不能拉黑自己")

        if not await User.exists(id=blocked_id):
            raise HTTPException(status_code=404, detail="User not found")

        from app.infrastructure.cache import invalidate_block_filter, invalidate_following
        block_record = await UserBlock.get_or_none(blocker_id=blocker_id, blocked_id=blocked_id)
        if block_record:
            await block_record.delete()
            # 取消拉黑→失效双方的屏蔽过滤缓存
            await invalidate_block_filter(blocker_id)
            await invalidate_block_filter(blocked_id)
            return False
        else:
            # 检查拉黑上限（普通用户 100，会员 1000）
            from datetime import datetime, timezone
            from app.models.user_membership import UserMembership
            has_membership = await UserMembership.filter(
                user_id=blocker_id, status="active", expires_at__gte=datetime.now(timezone.utc)
            ).exists()
            block_limit = 1000 if has_membership else 100
            current_count = await UserBlock.filter(blocker_id=blocker_id).count()
            if current_count >= block_limit:
                raise HTTPException(
                    status_code=403,
                    detail=f"已达屏蔽上限（{block_limit} 人）{'，升级会员可提升至 1000 人' if not has_membership else ''}",
                )
            await UserBlock.create(blocker_id=blocker_id, blocked_id=blocked_id)
            # 拉黑后自动取消双向关注
            await Follow.filter(follower_id=blocker_id, followed_id=blocked_id).delete()
            await Follow.filter(follower_id=blocked_id, followed_id=blocker_id).delete()
            # 失效双方的屏蔽过滤缓存和关注列表缓存
            await invalidate_block_filter(blocker_id)
            await invalidate_block_filter(blocked_id)
            await invalidate_following(blocker_id)
            await invalidate_following(blocked_id)
            return True

    @staticmethod
    async def get_blocked_users(user_id: int, limit: int = 100, offset: int = 0):
        blocks = await (
            UserBlock.filter(blocker_id=user_id)
            .prefetch_related("blocked")
            .order_by("-id")
            .offset(offset)
            .limit(limit)
        )
        return [b.blocked for b in blocks]
