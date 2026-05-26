import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from typing import Dict, Any, Optional
from app.schemas.user_schema import UserCreate, UserLogin, UserResponse
from app.services.auth_service import AuthService
from app.schemas.common import ResponseBase
from app.middleware.rate_limit import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    username:               str           = Field(..., max_length=100)
    password:               str           = Field(..., max_length=128)
    captcha_verified_token: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email:                  EmailStr
    captcha_verified_token: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class SendCodeRequest(BaseModel):
    email:                  EmailStr
    purpose:                str   # registration | password_change | login_id_change
    captcha_verified_token: Optional[str] = None


VERIFY_CODE_TTL = 600  # 10 minutes
VERIFY_ALLOWED_PURPOSES = {"registration", "password_change", "login_id_change", "account_claim"}


async def _redis_set_code(email: str, purpose: str, code: str) -> None:
    import json
    from app.infrastructure.redis_client import get_redis
    r = get_redis()
    key = f"vc:{email}:{purpose}"
    await r.set(key, json.dumps({"code": code}), ex=VERIFY_CODE_TTL)


async def verify_and_consume_code(email: str, purpose: str, code: str) -> None:
    """Raises HTTPException if code is missing, wrong or expired."""
    import json
    from app.infrastructure.redis_client import get_redis
    r = get_redis()
    key = f"vc:{email}:{purpose}"
    raw = await r.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail="验证码已过期或未发送")
    stored = json.loads(raw)
    if stored.get("code") != code.strip():
        raise HTTPException(status_code=400, detail="验证码错误")
    await r.delete(key)


@router.get("/site-info", response_model=ResponseBase[dict])
async def get_public_site_info():
    """公开站点信息（无需认证）"""
    from app.models.site_config import SiteConfig
    from app.infrastructure.cache import cache_get
    cached = await cache_get("site_config")
    cfg = cached if cached else {}
    return ResponseBase(data={
        "site_name":            cfg.get("site_name", "Moetopia"),
        "site_description":     cfg.get("site_description", "二次元插画社区"),
        "site_icon_url":        cfg.get("site_icon_url", ""),
        "site_favicon_url":     cfg.get("site_favicon_url", ""),
        "registration_enabled": cfg.get("registration_enabled", True),
        "tos_url":              cfg.get("tos_url", ""),
        "privacy_policy_url":   cfg.get("privacy_policy_url", ""),
    })


@router.post("/send-code", response_model=ResponseBase[dict])
async def send_verification_code(request: Request, body: SendCodeRequest):
    """发送邮符1次性验证码（10 分钟有效）"""
    import secrets
    from app.services.email_service import EmailService
    from app.api.v1.captcha import check_captcha
    await rate_limit(request, "forgot_password")
    if body.purpose not in VERIFY_ALLOWED_PURPOSES:
        raise HTTPException(status_code=400, detail="非法用途")
    await check_captcha(body.captcha_verified_token)
    code = str(secrets.randbelow(900000) + 100000)
    await _redis_set_code(body.email, body.purpose, code)
    try:
        await EmailService.send_verification_code(body.email, code, body.purpose)
    except Exception as e:
        logger.error("发送验证码邮件失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="邮件发送失败，请检查服务器 SMTP 配置")
    return ResponseBase(data={"message": "验证码已发送，请查收邮件（10 分钟内有效）"})


@router.post("/register", response_model=ResponseBase[UserResponse])
async def register(request: Request, user_in: UserCreate):
    """注册新用户（需验证码）"""
    await rate_limit(request, "register")
    from app.models.site_config import SiteConfig
    cfg_row = await SiteConfig.get_or_none(key="registration_enabled")
    if cfg_row is not None and cfg_row.value is False:
        raise HTTPException(status_code=403, detail="当前注册功能已关闭")
    await verify_and_consume_code(user_in.email, "registration", user_in.email_code)
    user = await AuthService.register(user_in)
    return ResponseBase(data=UserResponse.model_validate(user))


@router.post("/login", response_model=ResponseBase[Dict[str, Any]])
async def login(request: Request, body: LoginRequest):
    """账号密码登录，返回 access_token + refresh_token"""
    await rate_limit(request, "login")
    from app.api.v1.captcha import check_captcha
    await check_captcha(body.captcha_verified_token)
    user_in = UserLogin(username=body.username, password=body.password)
    data = await AuthService.login(user_in)
    return ResponseBase(data=data)


@router.post("/refresh", response_model=ResponseBase[Dict[str, Any]])
async def refresh_token(body: RefreshRequest):
    """使用 refresh_token 换取新的 access_token"""
    data = await AuthService.refresh(body.refresh_token)
    return ResponseBase(data=data)


@router.post("/forgot-password", response_model=ResponseBase[dict])
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    """发送密码重置邮件（无论邮件是否存在，均返回成功，防止账号枚举）"""
    await rate_limit(request, "forgot_password")
    from app.api.v1.captcha import check_captcha
    await check_captcha(body.captcha_verified_token)
    import secrets
    from datetime import datetime, timedelta, timezone
    from app.models.user import User
    from app.models.auth_token import PasswordResetToken
    from app.services.email_service import email_service

    user = await User.get_or_none(email=body.email)
    if user:
        # 清除该用户旧的重置令牌
        await PasswordResetToken.filter(user_id=user.id).delete()

        token = secrets.token_hex(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await PasswordResetToken.create(user_id=user.id, token=token, expires_at=expires_at)

        # 异步发送，失败不影响响应
        try:
            await email_service.send_password_reset(user.email, user.username, token)
        except Exception:
            pass

    return ResponseBase(data={"message": "如果该邮箱已注册，重置链接已发送，请查收邮件"})


@router.post("/reset-password", response_model=ResponseBase[dict])
async def reset_password(body: ResetPasswordRequest):
    """通过重置令牌设置新密码"""
    from datetime import datetime, timezone
    from app.models.auth_token import PasswordResetToken
    from app.core.security import get_password_hash

    record = await PasswordResetToken.get_or_none(token=body.token).prefetch_related("user")
    if not record:
        raise HTTPException(status_code=400, detail="无效或已过期的重置链接")

    if record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        await record.delete()
        raise HTTPException(status_code=400, detail="重置链接已过期，请重新申请")

    user = record.user
    user.password_hash = get_password_hash(body.new_password)
    user.token_version += 1  # 使所有旧 JWT 立即失效
    await user.save(update_fields=["password_hash", "token_version"])

    await record.delete()
    return ResponseBase(data={"message": "密码已重置，请使用新密码登录"})
