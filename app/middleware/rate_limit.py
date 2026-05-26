"""
滑动窗口速率限制 — 基于 Redis。

用法（在路由函数上）：
    from app.middleware.rate_limit import rate_limit

    @router.post("/login")
    async def login(request: Request, ...):
        await rate_limit(request, "login", limit=10, window=60)
        ...

规则说明：
- key 格式: rl:{scope}:{identifier}
- identifier 优先使用 JWT user_id，其次使用 IP
- 超限时返回 429，并在响应头中附带 Retry-After
"""
from __future__ import annotations

import time
from fastapi import HTTPException, Request

from app.infrastructure.redis_client import get_redis

# 预设规则（scope → (limit, window_seconds)）
PRESETS: dict[str, tuple[int, int]] = {
    "login":          (10,  60),    # 每 IP 每分钟最多 10 次登录尝试
    "register":       (5,   300),   # 每 IP 5 分钟内最多 5 次注册
    "forgot_password":(5,   600),   # 每 IP 10 分钟内最多 5 次找回密码
    "upload":         (20,  3600),  # 每用户每小时最多上传 20 件
    "comment":        (30,  60),    # 每用户每分钟最多 30 条评论
    "report":         (10,  3600),  # 每用户每小时最多 10 条举报
    "like":           (60,  60),    # 每用户每分钟最多 60 次点赞（防刺刷计数器）
    "message":        (30,  60),    # 每用户每分钟最多 30 条消息
    "tag":            (20,  60),    # 每用户每分钟最多 20 次打标
    "api_global":     (300, 60),    # 全局 API：每 IP 每分钟 300 次
}


def _get_identifier(request: Request) -> str:
    """从 request 中取 user_id（如已认证）或 IP。"""
    uid = getattr(request.state, "user_id", None)
    if uid:
        return f"u:{uid}"
    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"ip:{ip}"


async def rate_limit(
    request: Request,
    scope: str,
    limit: int | None = None,
    window: int | None = None,
) -> None:
    """
    检查并记录速率限制。超限抛出 HTTP 429。
    limit/window 可覆盖 PRESETS 中的默认值。
    """
    preset = PRESETS.get(scope, (300, 60))
    effective_limit  = limit  if limit  is not None else preset[0]
    effective_window = window if window is not None else preset[1]

    identifier = _get_identifier(request)
    key = f"rl:{scope}:{identifier}"

    try:
        r = get_redis()
        now = int(time.time())
        window_start = now - effective_window

        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, effective_window + 1)
        results = await pipe.execute()

        count = results[2]
        if count > effective_limit:
            retry_after = effective_window
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，请 {retry_after} 秒后重试",
                headers={"Retry-After": str(retry_after)},
            )
    except HTTPException:
        raise
    except Exception:
        # Redis 不可用时降级放行，不影响正常使用
        pass
