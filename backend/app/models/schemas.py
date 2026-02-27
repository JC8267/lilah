"""Pydantic request/response models."""

from pydantic import BaseModel, Field


class LLMOverride(BaseModel):
    provider: str | None = Field(
        default=None,
        description="Optional per-request provider override (anthropic|gemini|openai|openrouter|ollama|openai_compatible)",
    )
    model_id: str | None = Field(
        default=None, description="Optional per-request model id override."
    )
    max_tokens: int | None = Field(
        default=None, ge=128, le=32768, description="Optional max output tokens."
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0, le=300, description="Optional request timeout seconds."
    )
    retry_attempts: int | None = Field(
        default=None, ge=0, le=5, description="Optional retries for transient errors."
    )
    fallback_model_ids: list[str] | None = Field(
        default=None,
        description="Optional ordered fallback model IDs if primary model fails.",
    )


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str
    filters: dict | None = None  # optional demo filters
    llm_override: LLMOverride | None = None


class ConversationCreate(BaseModel):
    title: str = "New conversation"


class ConversationUpdate(BaseModel):
    title: str
