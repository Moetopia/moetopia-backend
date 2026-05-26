"""
自定义验证码系统
题型：text（文字填空）| choice（单选）| tile（图块选择，类 reCAPTCHA）

Redis 存储格式（JSON）：
  cap:pending:{token} = {"type": "text|choice", "answer": "xxx"}
                      | {"type": "tile", "correct": [0, 2, 5]}  ← 原始正确下标（不打乱）
  cap:ok:{token}      = "1"  ← 已验证令牌，2 min 一次性使用
"""
import json
import secrets
import random  # 仍用于随机选题
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.captcha import CaptchaQuestion
from app.schemas.common import ResponseBase

router = APIRouter()
logger = logging.getLogger(__name__)

_PENDING_TTL  = 300   # 待作答令牌 5 min
_VERIFIED_TTL = 120   # 已验证令牌 2 min
_PENDING_PFX  = "cap:pending:"
_VERIFIED_PFX = "cap:ok:"


# ── 公开接口 ─────────────────────────────────────────────────────────────────

@router.get("/challenge", response_model=ResponseBase[dict])
async def get_challenge():
    """随机返回一道验证题；若题库为空则返回 token=null（前端跳过验证）"""
    from app.infrastructure.redis_client import get_redis

    questions = await CaptchaQuestion.filter(is_active=True)
    if not questions:
        return ResponseBase(data={"token": None, "question": None})

    q = random.choice(questions)
    token = secrets.token_hex(24)
    r = get_redis()

    resp: dict = {
        "token":         token,
        "question":      q.question,
        "question_type": q.question_type,
    }

    if q.question_type == "tile":
        # 保持原始顺序 —— 图块来自同一场景切割，位置有语义意义，不可打乱
        tiles: list = list(q.tile_images or [])
        correct = list(q.correct_indices or [])
        pending = json.dumps({"type": "tile", "correct": correct})
        resp["tiles"]         = tiles
        resp["tile_rows"]     = q.tile_rows
        resp["tile_cols"]     = q.tile_cols
        resp["correct_count"] = len(correct)
        resp["hint_image"]    = q.hint_image
    else:
        pending = json.dumps({"type": q.question_type, "answer": q.answer.strip().lower()})
        if q.question_type == "choice":
            resp["choices"] = q.choices

    await r.set(f"{_PENDING_PFX}{token}", pending, ex=_PENDING_TTL)
    return ResponseBase(data=resp)


class VerifyBody(BaseModel):
    token:            str
    answer:           Optional[str]       = None   # text / choice
    selected_indices: Optional[List[int]] = None   # tile


@router.post("/verify", response_model=ResponseBase[dict])
async def verify_challenge(body: VerifyBody):
    """验证答案；正确则颁发 verified_token（2 min 内在 send-code 等接口使用）"""
    from app.infrastructure.redis_client import get_redis

    r = get_redis()
    raw = await r.get(f"{_PENDING_PFX}{body.token}")
    if not raw:
        raise HTTPException(status_code=400, detail="验证码已过期，请刷新后重试")

    try:
        data = json.loads(raw)
    except Exception:
        # 向下兼容旧版纯字符串存储
        data = {"type": "text", "answer": raw.strip().lower()}

    correct = False
    if data["type"] == "tile":
        expected = set(data.get("correct", []))
        submitted = set(body.selected_indices or [])
        correct = (submitted == expected) and len(expected) > 0
        if not correct:
            raise HTTPException(status_code=400, detail="选择有误，请重试")
    else:
        submitted_ans = (body.answer or "").strip().lower()
        if submitted_ans != data.get("answer", ""):
            raise HTTPException(status_code=400, detail="答案错误，请重新作答")

    # 答对：删除 pending，颁发已验证令牌
    await r.delete(f"{_PENDING_PFX}{body.token}")
    verified_token = secrets.token_hex(24)
    await r.set(f"{_VERIFIED_PFX}{verified_token}", "1", ex=_VERIFIED_TTL)

    return ResponseBase(data={"verified_token": verified_token})


# ── 供其他端点内联调用 ────────────────────────────────────────────────────────

async def check_captcha(verified_token: Optional[str]) -> None:
    """
    若题库有启用题目，则强制验证 verified_token；
    验证通过后立即撤销令牌（一次性）。
    抛出 HTTPException(400) 表示验证失败。
    """
    from app.infrastructure.redis_client import get_redis

    has_questions = await CaptchaQuestion.filter(is_active=True).exists()
    if not has_questions:
        return  # 题库为空 → 跳过

    if not verified_token:
        raise HTTPException(status_code=400, detail="请先完成安全验证")

    r = get_redis()
    key = f"{_VERIFIED_PFX}{verified_token}"
    val = await r.get(key)
    if not val:
        raise HTTPException(status_code=400, detail="安全验证已过期，请重新验证")

    await r.delete(key)  # 一次性消费
