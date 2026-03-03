"""Pydantic request/response models."""

from pydantic import BaseModel, Field, field_validator

from app.config import settings


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
    conversation_id: str | None = Field(default=None, max_length=128)
    message: str = Field(min_length=1, max_length=settings.chat_message_max_chars)
    filters: dict[str, str] | None = None  # optional demo filters
    llm_override: LLMOverride | None = None

    @field_validator("conversation_id")
    @classmethod
    def _validate_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        conv_id = value.strip()
        return conv_id or None

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("message cannot be empty.")
        if len(text) > settings.chat_message_max_chars:
            raise ValueError(
                f"message must be <= {settings.chat_message_max_chars} characters."
            )
        return text

    @field_validator("filters")
    @classmethod
    def _validate_filters(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None

        if len(value) > settings.chat_filters_max_items:
            raise ValueError(
                f"filters may contain at most {settings.chat_filters_max_items} items."
            )

        normalized: dict[str, str] = {}
        for raw_key, raw_val in value.items():
            key = str(raw_key).strip()
            val = str(raw_val).strip()
            if not key or not val:
                raise ValueError("filters must contain non-empty keys and values.")
            if len(key) > settings.chat_filter_key_max_chars:
                raise ValueError(
                    f"filter keys must be <= {settings.chat_filter_key_max_chars} chars."
                )
            if len(val) > settings.chat_filter_value_max_chars:
                raise ValueError(
                    f"filter values must be <= {settings.chat_filter_value_max_chars} chars."
                )
            normalized[key] = val

        return normalized


class ConversationCreate(BaseModel):
    title: str = "New conversation"


class ConversationUpdate(BaseModel):
    title: str
