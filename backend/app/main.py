"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager
import json
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db.duckdb_engine import init_duckdb, close_duckdb
from app.db.sqlite_store import init_sqlite, close_sqlite
from app.config import settings
from app.routes import chat, conversations, metadata

logger = logging.getLogger("lilah.api")


def _log_struct(level: int, event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, default=str))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_duckdb()
    await init_sqlite()
    print("Lilah backend ready")
    yield
    # Shutdown
    await close_sqlite()
    close_duckdb()


app = FastAPI(title="Lilah", version="0.1.0", lifespan=lifespan)

allowed_origins = [
    origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=(settings.cors_allow_origin_regex or None),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(metadata.router)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id

    if request.method == "POST" and request.url.path == "/api/chat":
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = -1

            if content_length > settings.chat_request_max_bytes:
                _log_struct(
                    logging.WARNING,
                    "chat_payload_rejected",
                    request_id=request_id,
                    content_length=content_length,
                    max_bytes=settings.chat_request_max_bytes,
                    path=request.url.path,
                    method=request.method,
                )
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Chat payload too large. Max {settings.chat_request_max_bytes} bytes."
                        ),
                        "request_id": request_id,
                    },
                    headers={"X-Request-ID": request_id},
                )

    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:
        duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
        _log_struct(
            logging.ERROR,
            "request_crash",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            duration_ms=duration_ms,
            error=str(e),
        )
        raise

    response.headers["X-Request-ID"] = request_id
    duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    _log_struct(
        logging.INFO,
        "request_complete",
        request_id=request_id,
        path=request.url.path,
        method=request.method,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.get("/api/health")
def health():
    return {"status": "ok"}
