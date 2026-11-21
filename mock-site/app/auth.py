from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_db
from .models import User, UserRole


async def get_current_user(
    x_user_id: int = Header(..., alias="X-User-Id", description="用户ID"),
    x_user_role: UserRole = Header(..., alias="X-User-Role", description="用户角色"),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await db.get(User, x_user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在，请先注册")
    if user.role != x_user_role:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户角色不匹配")
    return user


async def require_employer(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.employer:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅招聘方可执行此操作")
    return user


async def require_applicant(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.applicant:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅应聘方可执行此操作")
    return user
