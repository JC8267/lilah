"""CRUD routes for conversations."""

from fastapi import APIRouter, HTTPException

from app.db.sqlite_store import (
    create_conversation,
    delete_conversation,
    get_conversation,
    get_messages,
    list_conversations,
    update_conversation_title,
)
from app.models.schemas import ConversationCreate, ConversationUpdate

router = APIRouter(prefix="/api/conversations")


@router.get("")
async def list_convos():
    return await list_conversations()


@router.post("")
async def create_convo(body: ConversationCreate):
    return await create_conversation(body.title)


@router.get("/{conv_id}")
async def get_convo(conv_id: str):
    conv = await get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.patch("/{conv_id}")
async def update_convo(conv_id: str, body: ConversationUpdate):
    conv = await get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await update_conversation_title(conv_id, body.title)
    return {"ok": True}


@router.delete("/{conv_id}")
async def delete_convo(conv_id: str):
    await delete_conversation(conv_id)
    return {"ok": True}


@router.get("/{conv_id}/messages")
async def get_convo_messages(conv_id: str):
    return await get_messages(conv_id)
