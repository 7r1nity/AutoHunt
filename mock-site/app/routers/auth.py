from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..schemas import MfaChallengeOut, MfaRequest, MfaVerify, MfaVerifyOut, UserOut, UserRegister

router = APIRouter(prefix="/auth", tags=["认证"])

# 模拟站点不接入真实短信/邮件供应商；验证码只在内存中保留五分钟。
_mfa_challenges: dict[str, tuple[str, float]] = {}


@router.post("/mfa/request", response_model=MfaChallengeOut)
async def request_mfa(body: MfaRequest) -> MfaChallengeOut:
    challenge_id = secrets.token_urlsafe(18)
    code = f"{secrets.randbelow(1_000_000):06d}"
    _mfa_challenges[challenge_id] = (code, time.time() + 300)
    return MfaChallengeOut(challenge_id=challenge_id, expires_in=300, demo_code=code)


@router.post("/mfa/verify", response_model=MfaVerifyOut)
async def verify_mfa(body: MfaVerify) -> MfaVerifyOut:
    challenge = _mfa_challenges.get(body.challenge_id)
    if not challenge or challenge[1] < time.time():
        _mfa_challenges.pop(body.challenge_id, None)
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if not secrets.compare_digest(challenge[0], body.code):
        raise HTTPException(status_code=400, detail="验证码不正确")
    _mfa_challenges.pop(body.challenge_id, None)
    return MfaVerifyOut(verified=True)


@router.post("/register", response_model=UserOut)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.scalar(select(User).where(User.email == body.email))
    if existing:
        if existing.role != body.role:
            raise HTTPException(status_code=409, detail="该邮箱已注册为其他角色")
        # 幂等注册：同邮箱直接返回已有账号，方便 Agent 重复执行
        return existing

    user = User(
        role=body.role,
        name=body.name,
        email=body.email,
        phone=body.phone,
        company_name=body.company_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
