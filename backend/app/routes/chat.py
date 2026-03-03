"""POST /api/chat — SSE stream."""

import json
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.agent.loop import run_agent_loop
from app.db.sqlite_store import (
    add_message,
    create_conversation,
    get_messages,
    update_conversation_title,
)
from app.config import settings
from app.models.schemas import ChatRequest

router = APIRouter()
logger = logging.getLogger("lilah.chat")


def _log_struct(level: int, event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, default=str))


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """Stream an agent response via SSE."""
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex

    if req.llm_override and not settings.allow_llm_override:
        _log_struct(
            logging.WARNING,
            "llm_override_rejected",
            request_id=request_id,
            conversation_id=req.conversation_id,
        )
        return JSONResponse(
            status_code=403,
            content={
                "detail": "llm_override is disabled on this deployment.",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        )

    # Create or reuse conversation
    created_new_conversation = False
    if req.conversation_id:
        conv_id = req.conversation_id
        initial_title = ""
    else:
        conv = await create_conversation()
        conv_id = conv["id"]
        initial_title = conv["title"]
        created_new_conversation = True
    _log_struct(
        logging.INFO,
        "chat_request_start",
        request_id=request_id,
        conversation_id=conv_id,
        created_new_conversation=created_new_conversation,
        message_len=len(req.message),
        has_filters=bool(req.filters),
    )

    # Save user message
    await add_message(conv_id, "user", req.message)

    # Build message history from DB
    db_messages = await get_messages(conv_id)
    if settings.chat_history_max_messages > 0:
        db_messages = db_messages[-settings.chat_history_max_messages :]
    api_messages = []
    for m in db_messages:
        if m["role"] in ("user", "assistant"):
            api_messages.append({"role": m["role"], "content": m["content"]})

    # If filters are provided, prepend context
    if req.filters:
        filter_desc = ", ".join(f"{k}: {v}" for k, v in req.filters.items())
        api_messages[-1]["content"] = (
            f"[Active filters: {filter_desc}]\n\n{api_messages[-1]['content']}"
        )

    async def event_generator():
        collected_text = ""
        collected_charts = []

        # Emit conversation metadata immediately so the client can bind messages/errors
        # to a concrete conversation even if the model call fails early.
        if created_new_conversation:
            yield {
                "event": "conversation",
                "data": json.dumps({"id": conv_id, "title": initial_title}),
            }

        llm_override = req.llm_override.model_dump(exclude_none=True) if req.llm_override else None

        try:
            async for event in run_agent_loop(
                api_messages,
                llm_override=llm_override,
                request_id=request_id,
                conversation_id=conv_id,
            ):
                etype = event["event"]
                data = event["data"]

                if etype == "text_delta":
                    collected_text += data["content"]

                if etype == "chart":
                    collected_charts.append(data["spec"])

                if etype == "done":
                    # Save assistant message
                    await add_message(
                        conv_id, "assistant", collected_text, collected_charts or None
                    )

                    # Auto-title on first exchange
                    if len(db_messages) <= 1:
                        title = req.message[:60]
                        await update_conversation_title(conv_id, title)
                        yield {
                            "event": "conversation",
                            "data": json.dumps({"id": conv_id, "title": title}),
                        }

                if etype == "error":
                    if isinstance(data, dict):
                        data.setdefault("request_id", request_id)
                    _log_struct(
                        logging.ERROR,
                        "chat_stream_error",
                        request_id=request_id,
                        conversation_id=conv_id,
                        error=(data.get("message") if isinstance(data, dict) else str(data)),
                    )

                yield {
                    "event": etype,
                    "data": json.dumps(data, default=str),
                }
        except Exception as e:
            _log_struct(
                logging.ERROR,
                "chat_stream_crash",
                request_id=request_id,
                conversation_id=conv_id,
                error=str(e),
            )
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "message": f"Chat stream crashed: {e}",
                        "request_id": request_id,
                    },
                    default=str,
                ),
            }

    return EventSourceResponse(event_generator(), headers={"X-Request-ID": request_id})
