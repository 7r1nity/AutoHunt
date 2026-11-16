from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import Job, Message, User, UserRole
from ..schemas import ConversationOut, MessageCreate, MessageOut
from ..test_manager import is_test_manager

router = APIRouter(prefix="/messages", tags=["聊天"])


@router.post("", response_model=MessageOut, status_code=201)
async def send_message(
    body: MessageCreate,
    sender: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Message:
    recipient = await db.get(User, body.recipient_id)
    if not recipient:
        raise HTTPException(status_code=404, detail="接收方不存在")
    if recipient.id == sender.id:
        raise HTTPException(status_code=400, detail="不能给自己发消息")

    sender_id = sender.id
    sender_role = sender.role.value
    if is_test_manager(sender):
        if body.job_id is None:
            raise HTTPException(status_code=400, detail="统一测试招聘方回复时必须指定职位")
        job = await db.get(Job, body.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="职位不存在")
        if recipient.role != UserRole.applicant:
            raise HTTPException(status_code=400, detail="统一测试招聘方只能回复求职者")
        # 聚合账号代表职位所属招聘方回复，求职者页面与 AutoHunt 监测
        # 仍按原职位 employer_id 读取会话。
        sender_id = job.employer_id
        sender_role = UserRole.employer.value

    message = Message(
        sender_id=sender_id,
        sender_role=sender_role,
        recipient_id=recipient.id,
        recipient_role=recipient.role.value,
        job_id=body.job_id,
        content=body.content,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


@router.get("", response_model=list[MessageOut])
async def list_messages(
    with_user_id: int,
    job_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    """获取双向聊天；统一测试招聘方可读取任意职位的应聘会话。"""
    if is_test_manager(user):
        if job_id is None:
            raise HTTPException(status_code=400, detail="统一测试招聘方查看会话时必须指定职位")
        job = await db.get(Job, job_id)
        applicant = await db.get(User, with_user_id)
        if not job or not applicant or applicant.role != UserRole.applicant:
            raise HTTPException(status_code=404, detail="职位或求职者不存在")
        stmt = select(Message).where(
            Message.job_id == job.id,
            or_(
                (Message.sender_id == applicant.id) & (Message.recipient_id == job.employer_id),
                (Message.sender_id == job.employer_id) & (Message.recipient_id == applicant.id),
            ),
        )
        unread_condition = (
            (Message.sender_id == applicant.id)
            & (Message.recipient_id == job.employer_id)
            & (Message.job_id == job.id)
        )
    else:
        stmt = select(Message).where(
            or_(
                (Message.sender_id == user.id) & (Message.recipient_id == with_user_id),
                (Message.sender_id == with_user_id) & (Message.recipient_id == user.id),
            )
        )
        if job_id is not None:
            stmt = stmt.where(Message.job_id == job_id)
        unread_condition = (Message.sender_id == with_user_id) & (Message.recipient_id == user.id)
        if job_id is not None:
            unread_condition = unread_condition & (Message.job_id == job_id)

    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    messages = list(await db.scalars(stmt))
    messages.reverse()
    await db.execute(
        update(Message)
        .where(unread_condition, Message.is_read == False)  # noqa: E712
        .values(is_read=True)
    )
    await db.commit()
    for message in messages:
        if is_test_manager(user):
            if message.sender_role == UserRole.applicant.value:
                message.is_read = True
        elif message.recipient_id == user.id:
            message.is_read = True
    return messages


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    """按求职者和职位列出会话；聚合账号读取所有职位会话。"""
    manager = is_test_manager(user)
    if manager:
        messages = list(await db.scalars(select(Message).where(Message.job_id.is_not(None))))
    else:
        messages = list(await db.scalars(
            select(Message).where(or_(Message.sender_id == user.id, Message.recipient_id == user.id))
        ))

    conv_map: dict[tuple[int, int | None], dict] = {}
    for message in messages:
        if manager:
            if message.sender_role == UserRole.applicant.value:
                other_id = message.sender_id
            elif message.recipient_role == UserRole.applicant.value:
                other_id = message.recipient_id
            else:
                continue
            other_role = UserRole.applicant.value
            unread = message.recipient_role == UserRole.employer.value and not message.is_read
        else:
            other_id = message.recipient_id if message.sender_id == user.id else message.sender_id
            other_role = message.recipient_role if message.sender_id == user.id else message.sender_role
            unread = message.recipient_id == user.id and not message.is_read

        key = (other_id, message.job_id)
        current = conv_map.get(key)
        if current is None:
            conv_map[key] = {
                "user_id": other_id,
                "user_name": "",
                "user_role": other_role,
                "job_id": message.job_id,
                "job_title": None,
                "last_message": message.content,
                "last_time": message.created_at,
                "unread_count": 1 if unread else 0,
            }
            continue
        if message.created_at > current["last_time"]:
            current["last_message"] = message.content
            current["last_time"] = message.created_at
        if unread:
            current["unread_count"] += 1

    user_ids = {key[0] for key in conv_map}
    if user_ids:
        users = await db.scalars(select(User).where(User.id.in_(user_ids)))
        name_map = {item.id: item.name for item in users}
        for (user_id, _), info in conv_map.items():
            info["user_name"] = name_map.get(user_id, f"用户{user_id}")

    job_ids = {key[1] for key in conv_map if key[1] is not None}
    if job_ids:
        jobs = await db.scalars(select(Job).where(Job.id.in_(job_ids)))
        title_map = {item.id: item.title for item in jobs}
        for (_, job_id), info in conv_map.items():
            info["job_title"] = title_map.get(job_id) if job_id is not None else None

    conversations = sorted(conv_map.values(), key=lambda item: item["last_time"], reverse=True)
    return [ConversationOut(**item) for item in conversations]


@router.get("/peek", response_model=list[MessageOut])
async def peek_job_messages(
    job_id: int = Query(..., ge=1),
    with_user_id: int = Query(..., ge=1),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    """Read a job conversation for automation without changing unread state."""
    if user.role != UserRole.applicant:
        raise HTTPException(status_code=403, detail="仅求职者可以查看自己的会话")
    job = await db.get(Job, job_id)
    if job is None or job.employer_id != with_user_id:
        raise HTTPException(status_code=404, detail="职位或招聘方不存在")
    rows = await db.scalars(
        select(Message).where(
            Message.job_id == job_id,
            or_(
                (Message.sender_id == user.id) & (Message.recipient_id == with_user_id),
                (Message.sender_id == with_user_id) & (Message.recipient_id == user.id),
            ),
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
    )
    return list(reversed(list(rows)))
