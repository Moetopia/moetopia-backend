from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

REDIS_CHANNEL_PREFIX = "ws:user:"


class ConnectionManager:
    """
    本地 WebSocket 连接管理器 + Redis pub/sub 转发。

    每个进程管理自己连接的用户，通知通过 Redis channel 广播，
    确保多 worker 部署时消息正确路由到持有该 WebSocket 的进程。
    """

    def __init__(self) -> None:
        self.active_connections: Dict[int, WebSocket] = {}
        self._subscriber_task: asyncio.Task | None = None

    # ── 本地连接管理 ────────────────────────────────────────────────────────────

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        await websocket.accept()
        self.active_connections[user_id] = websocket
        if self._subscriber_task is None or self._subscriber_task.done():
            self._subscriber_task = asyncio.create_task(self._redis_subscriber())

    def disconnect(self, user_id: int) -> None:
        self.active_connections.pop(user_id, None)

    async def send_personal_message(self, message: str, user_id: int) -> None:
        """发送消息到本地用户（如连接在本进程）；同时通过 Redis pub/sub 广播给其他进程。"""
        # 1. 尝试直接本地发送
        ws = self.active_connections.get(user_id)
        if ws:
            try:
                await ws.send_text(message)
            except Exception:
                self.disconnect(user_id)

        # 2. 通过 Redis pub/sub 广播（其他 worker 上的同一用户也能收到）
        try:
            from app.infrastructure.redis_client import get_redis
            r = get_redis()
            await r.publish(f"{REDIS_CHANNEL_PREFIX}{user_id}", message)
        except Exception as e:
            logger.debug(f"Redis pub 失败（降级到本地模式）: {e}")

    async def broadcast(self, message: str) -> None:
        for ws in list(self.active_connections.values()):
            try:
                await ws.send_text(message)
            except Exception:
                pass

    # ── Redis Subscriber ────────────────────────────────────────────────────────

    async def _redis_subscriber(self) -> None:
        """
        订阅本进程所有在线用户的 Redis channel，
        接收来自其他 worker 发布的消息并转发到本地 WebSocket。
        """
        try:
            from app.infrastructure.redis_client import get_redis
            r = get_redis()
            pubsub = r.pubsub()

            while True:
                if not self.active_connections:
                    await asyncio.sleep(1)
                    continue

                channels = [f"{REDIS_CHANNEL_PREFIX}{uid}" for uid in self.active_connections]
                await pubsub.subscribe(*channels)

                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    # channel 格式: ws:user:{user_id}
                    channel: str = message.get("channel", "")
                    try:
                        uid = int(channel.split(":")[-1])
                    except (ValueError, IndexError):
                        continue
                    ws = self.active_connections.get(uid)
                    if ws:
                        try:
                            await ws.send_text(message["data"])
                        except Exception:
                            self.disconnect(uid)

                await pubsub.unsubscribe()
        except Exception as e:
            logger.debug(f"Redis subscriber 异常（降级模式）: {e}")


manager = ConnectionManager()

# WS 端点不支持头部的 Bearer Token, 一般通过 query 传递 token 或者通过 Cookie
@router.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket, token: str):
    from app.core.security import decode_access_token

    async def reject(code: int = 1008):
        # 必须先 accept 才能发送 close frame，否则 Starlette 返回 403 HTTP
        await websocket.accept()
        await websocket.close(code=code)

    payload = decode_access_token(token)
    if not payload:
        await reject()
        return

    user_id_str = payload.get("sub")
    if not user_id_str:
        await reject()
        return

    try:
        user = await User.get(id=int(user_id_str))
    except Exception:
        await reject()
        return

    # 校验 token 版本（改密/封禁后旧 token 立即失效）
    if payload.get("ver", 0) != user.token_version:
        await reject(code=4001)
        return

    if user.is_banned:
        await reject(code=4003)
        return

    await manager.connect(websocket, user.id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(user.id)
