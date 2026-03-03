"""Provider-agnostic LLM adapter layer for tool-calling loops."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import settings

SUPPORTED_PROVIDERS = {
    "anthropic",
    "gemini",
    "openai",
    "openrouter",
    "ollama",
    "openai_compatible",
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str | None = None


@dataclass(frozen=True)
class LLMRuntimeOptions:
    provider: str
    model_id: str
    max_tokens: int
    timeout_seconds: float
    retry_attempts: int
    retry_backoff_seconds: float
    fallback_model_ids: list[str]
    base_url: str
    api_key: str

    @property
    def provider_signature(self) -> str:
        return "|".join([self.provider, self.base_url, self.api_key])


class BaseProvider:
    def run_turn(
        self,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        history: list[dict[str, Any]],
        model_id: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelTurn:
        raise NotImplementedError


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str) -> None:
        try:
            from anthropic import Anthropic
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "anthropic package is not installed. Install backend dependencies or "
                "switch provider."
            ) from e

        self._client = Anthropic(api_key=api_key)

    def run_turn(
        self,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        history: list[dict[str, Any]],
        model_id: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelTurn:
        _ = timeout_seconds  # Anthropic SDK handles its own timeout configuration.

        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": self._to_anthropic_messages(history),
        }
        if tools:
            payload["tools"] = tools

        response = self._client.messages.create(**payload)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=block.input or {},
                    )
                )

        return ModelTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
        )

    @staticmethod
    def _to_anthropic_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        for msg in history:
            role = msg.get("role")
            if role == "user":
                out.append({"role": "user", "content": msg.get("content", "")})
            elif role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    out.append({"role": "assistant", "content": msg.get("content", "")})
                    continue

                content_blocks: list[dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc.get("input", {}),
                        }
                    )
                out.append({"role": "assistant", "content": content_blocks})
            elif role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg["tool_call_id"],
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )

        return out


class OpenAICompatibleProvider(BaseProvider):
    def __init__(self, provider_name: str, base_url: str, api_key: str) -> None:
        self._provider_name = provider_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def run_turn(
        self,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]],
        history: list[dict[str, Any]],
        model_id: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelTurn:
        payload = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                *self._to_openai_messages(history),
            ],
        }
        if tools:
            payload["tools"] = self._to_openai_tools(tools)
            payload["tool_choice"] = "auto"

        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            details = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LLM HTTP error {e.code} from {self._provider_name}: {details}"
            ) from e
        except (TimeoutError, socket.timeout) as e:
            raise RuntimeError(
                f"LLM timeout from {self._provider_name} after {timeout_seconds:.0f}s"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"LLM connection error to {self._provider_name}: {e.reason}"
            ) from e

        parsed = json.loads(raw)
        choices = parsed.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM returned no choices: {raw[:400]}")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")

        text = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(message.get("tool_calls") or [], start=1):
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or "{}"
            try:
                parsed_args = json.loads(args_raw)
                if not isinstance(parsed_args, dict):
                    parsed_args = {"value": parsed_args}
            except json.JSONDecodeError:
                parsed_args = {"_raw_arguments": args_raw}

            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"tool_call_{i}",
                    name=name,
                    input=parsed_args,
                    raw=tc if isinstance(tc, dict) else None,
                )
            )

        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=finish_reason,
        )

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object"}),
                    },
                }
            )
        return out

    @staticmethod
    def _to_openai_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in history:
            role = msg.get("role")
            if role == "user":
                out.append({"role": "user", "content": msg.get("content", "")})
            elif role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    out.append({"role": "assistant", "content": msg.get("content", "")})
                    continue

                formatted_calls = []
                for tc in tool_calls:
                    raw = tc.get("raw")
                    if isinstance(raw, dict):
                        formatted_calls.append(raw)
                    else:
                        formatted_calls.append(
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc.get("input", {})),
                                },
                            }
                        )
                out.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content", ""),
                        "tool_calls": formatted_calls,
                    }
                )
            elif role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg["tool_call_id"],
                        "content": msg.get("content", ""),
                    }
                )
        return out


def _resolve_base_url(provider_name: str) -> str:
    if settings.llm_base_url:
        return settings.llm_base_url.rstrip("/")
    if provider_name == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/openai"
    if provider_name == "openai":
        return "https://api.openai.com/v1"
    if provider_name == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider_name == "ollama":
        return "http://localhost:11434/v1"
    return "https://api.openai.com/v1"


def _resolve_api_key(provider_name: str) -> str:
    if provider_name == "anthropic":
        return settings.anthropic_api_key or settings.llm_api_key
    if provider_name == "gemini":
        return settings.gemini_api_key or settings.llm_api_key
    return settings.openai_api_key or settings.llm_api_key


def resolve_runtime_options(llm_override: dict[str, Any] | None) -> LLMRuntimeOptions:
    """Resolve and validate effective LLM options for a request."""
    override = llm_override or {}

    provider = str(override.get("provider") or settings.llm_provider).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        allowed = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(f"Unsupported provider '{provider}'. Allowed: {allowed}.")

    model_id = str(override.get("model_id") or settings.model_id).strip()
    if not model_id:
        raise ValueError("model_id cannot be empty.")

    max_tokens_raw = override.get("max_tokens")
    max_tokens = (
        int(max_tokens_raw)
        if max_tokens_raw is not None
        else int(settings.llm_max_tokens)
    )
    if max_tokens < 128 or max_tokens > 32768:
        raise ValueError("max_tokens must be between 128 and 32768.")

    timeout_seconds = float(settings.llm_timeout_seconds)
    if override.get("timeout_seconds") is not None:
        timeout_seconds = float(override["timeout_seconds"])
    if timeout_seconds <= 0:
        raise ValueError("llm_timeout_seconds must be > 0.")

    retry_attempts = int(settings.llm_retry_attempts)
    if override.get("retry_attempts") is not None:
        retry_attempts = int(override["retry_attempts"])
    if retry_attempts < 0 or retry_attempts > 5:
        raise ValueError("retry_attempts must be between 0 and 5.")

    retry_backoff_seconds = float(settings.llm_retry_backoff_seconds)
    if retry_backoff_seconds <= 0:
        raise ValueError("llm_retry_backoff_seconds must be > 0.")

    fallback_models_raw = settings.llm_fallback_models.strip()
    fallback_model_ids = [
        x.strip() for x in fallback_models_raw.split(",") if x.strip()
    ] if fallback_models_raw else []

    override_fallbacks = override.get("fallback_model_ids")
    if override_fallbacks is not None:
        if not isinstance(override_fallbacks, list):
            raise ValueError("fallback_model_ids must be an array of model IDs.")
        fallback_model_ids = [str(x).strip() for x in override_fallbacks if str(x).strip()]

    return LLMRuntimeOptions(
        provider=provider,
        model_id=model_id,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        fallback_model_ids=fallback_model_ids,
        base_url=_resolve_base_url(provider),
        api_key=_resolve_api_key(provider),
    )


_provider_instance: BaseProvider | None = None
_provider_signature: str | None = None
_provider_lock = threading.Lock()


def get_provider(options: LLMRuntimeOptions) -> BaseProvider:
    """Return cached provider instance for effective provider/base_url/api_key."""
    global _provider_instance, _provider_signature

    signature = options.provider_signature
    with _provider_lock:
        if _provider_instance is not None and _provider_signature == signature:
            return _provider_instance

        if options.provider == "anthropic":
            _provider_instance = AnthropicProvider(api_key=options.api_key)
        else:
            _provider_instance = OpenAICompatibleProvider(
                provider_name=options.provider,
                base_url=options.base_url,
                api_key=options.api_key,
            )

        _provider_signature = signature
        return _provider_instance
