"""POST /api/chat — SSE stream."""

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging
import time
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
_RATE_WINDOW_SECONDS = 60.0
_rate_lock = asyncio.Lock()
_recent_requests_by_client: dict[str, deque[float]] = {}
_active_streams_by_client: dict[str, int] = {}
_active_streams_total = 0


def _log_struct(level: int, event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, default=str))


def _resolve_client_key(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def _acquire_chat_slot(client_key: str) -> tuple[bool, dict[str, int | str]]:
    global _active_streams_total
    now = time.monotonic()
    rate_limit = max(1, int(settings.chat_rate_limit_per_minute))
    per_client_limit = max(1, int(settings.chat_max_active_streams_per_client))
    global_limit = max(1, int(settings.chat_max_active_streams_global))

    async with _rate_lock:
        dq = _recent_requests_by_client.setdefault(client_key, deque())
        while dq and (now - dq[0]) > _RATE_WINDOW_SECONDS:
            dq.popleft()

        if len(dq) >= rate_limit:
            retry_after = max(1, int(_RATE_WINDOW_SECONDS - (now - dq[0])))
            return False, {
                "status_code": 429,
                "retry_after": retry_after,
                "detail": "Rate limit exceeded for chat requests.",
            }

        client_active = _active_streams_by_client.get(client_key, 0)
        if client_active >= per_client_limit:
            return False, {
                "status_code": 429,
                "retry_after": 1,
                "detail": "Too many active chat streams for this client.",
            }

        if _active_streams_total >= global_limit:
            return False, {
                "status_code": 503,
                "retry_after": 1,
                "detail": "Server is busy. Try again shortly.",
            }

        dq.append(now)
        _active_streams_by_client[client_key] = client_active + 1
        _active_streams_total += 1

    return True, {}


async def _release_chat_slot(client_key: str) -> None:
    global _active_streams_total
    async with _rate_lock:
        current = _active_streams_by_client.get(client_key, 0)
        if current <= 1:
            _active_streams_by_client.pop(client_key, None)
        else:
            _active_streams_by_client[client_key] = current - 1

        if _active_streams_total > 0:
            _active_streams_total -= 1


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    """Stream an agent response via SSE."""
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex
    client_key = _resolve_client_key(request)

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

    slot_acquired = False
    slot_ok, rejection = await _acquire_chat_slot(client_key)
    if not slot_ok:
        status_code = int(rejection.get("status_code", 429))
        retry_after = str(rejection.get("retry_after", 1))
        detail = str(rejection.get("detail", "Chat limit exceeded."))
        _log_struct(
            logging.WARNING,
            "chat_slot_rejected",
            request_id=request_id,
            client_key=client_key,
            status_code=status_code,
            detail=detail,
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail, "request_id": request_id},
            headers={"X-Request-ID": request_id, "Retry-After": retry_after},
        )
    slot_acquired = True

    try:
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
            client_key=client_key,
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

        async def event_generator():
            collected_text = ""
            collected_charts = []
            cancel_event = asyncio.Event()
            poll_seconds = max(0.05, float(settings.chat_disconnect_poll_ms) / 1000.0)
            llm_override = req.llm_override.model_dump(exclude_none=True) if req.llm_override else None
            active_filters = dict(req.filters) if req.filters else None
            agent_iter = run_agent_loop(
                api_messages,
                llm_override=llm_override,
                request_id=request_id,
                conversation_id=conv_id,
                active_filters=active_filters,
                cancel_event=cancel_event,
            )
            next_event_task: asyncio.Task | None = None

            try:
                # Emit conversation metadata immediately so the client can bind messages/errors
                # to a concrete conversation even if the model call fails early.
                if created_new_conversation:
                    yield {
                        "event": "conversation",
                        "data": json.dumps({"id": conv_id, "title": initial_title}),
                    }

                next_event_task = asyncio.create_task(agent_iter.__anext__())
                while True:
                    if await request.is_disconnected():
                        _log_struct(
                            logging.INFO,
                            "chat_client_disconnected",
                            request_id=request_id,
                            client_key=client_key,
                            conversation_id=conv_id,
                        )
                        cancel_event.set()
                        if next_event_task is not None and not next_event_task.done():
                            next_event_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await next_event_task
                        break

                    if next_event_task is None:
                        next_event_task = asyncio.create_task(agent_iter.__anext__())

                    done, _ = await asyncio.wait({next_event_task}, timeout=poll_seconds)
                    if not done:
                        continue

                    try:
                        event = next_event_task.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        next_event_task = None

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
            finally:
                cancel_event.set()
                if next_event_task is not None and not next_event_task.done():
                    next_event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await next_event_task
                with suppress(Exception):
                    await agent_iter.aclose()
                await _release_chat_slot(client_key)
                _log_struct(
                    logging.INFO,
                    "chat_slot_released",
                    request_id=request_id,
                    client_key=client_key,
                    conversation_id=conv_id,
                )

        return EventSourceResponse(event_generator(), headers={"X-Request-ID": request_id})
    except Exception as e:
        if slot_acquired:
            await _release_chat_slot(client_key)
        _log_struct(
            logging.ERROR,
            "chat_request_setup_crash",
            request_id=request_id,
            client_key=client_key,
            error=str(e),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to initialize chat stream.", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
