"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.duckdb_engine import init_duckdb, close_duckdb
from app.db.sqlite_store import init_sqlite
from app.config import settings
from app.routes import chat, conversations, metadata


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_duckdb()
    await init_sqlite()
    print("Lilah backend ready")
    yield
    # Shutdown
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


@app.get("/api/health")
def health():
    return {"status": "ok"}
