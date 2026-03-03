"""Agentic tool_use loop — yields SSE events."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from app.agent.provider import get_provider, resolve_runtime_options
from app.agent.system_prompt import build_system_prompt
from app.agent.tools import TOOL_DEFINITIONS, build_direct_result_for_user_query, handle_tool_call
from app.config import settings


def _compact_tool_result_for_model(tool_name: str, result_data: Any) -> str:
    """Shrink verbose tool payloads before sending them back to the model."""
    if not isinstance(result_data, dict):
        return json.dumps(result_data, default=str)

    if tool_name == "query_data":
        rows = result_data.get("rows", [])
        columns = result_data.get("columns", [])
        max_rows = 20
        compact = {
            "columns": columns,
            "row_count": result_data.get("row_count", len(rows)),
            "rows_preview": rows[:max_rows],
            "truncated": len(rows) > max_rows,
        }
        if "error" in result_data:
            compact["error"] = result_data["error"]
        return json.dumps(compact, default=str)

    if tool_name == "search_questions":
        results = result_data.get("results", [])
        compact = {
            "count": result_data.get("count", len(results)),
            "results": results[:12],
            "truncated": len(results) > 12,
        }
        if "error" in result_data:
            compact["error"] = result_data["error"]
        return json.dumps(compact, default=str)

    if tool_name == "quick_insight":
        compact = {
            "analysis_type": result_data.get("analysis_type"),
            "answerable": result_data.get("answerable"),
            "reason_code": result_data.get("reason_code"),
            "question_id": result_data.get("question_id"),
            "question_text": result_data.get("question_text"),
            "row_count": result_data.get("row_count"),
            "top_rows": (result_data.get("top_rows") or [])[:12],
            "chart": result_data.get("chart"),
            "insight_text": result_data.get("insight_text"),
            "error": result_data.get("error"),
        }
        return json.dumps(compact, default=str)

    if tool_name == "demographic_breakout":
        compact = {
            "analysis_type": result_data.get("analysis_type"),
            "dimensions": result_data.get("dimensions"),
            "chart": result_data.get("chart"),
            "insight_text": result_data.get("insight_text"),
            "error": result_data.get("error"),
        }
        return json.dumps(compact, default=str)

    if tool_name == "question_by_demographic":
        compact = {
            "analysis_type": result_data.get("analysis_type"),
            "question_id": result_data.get("question_id"),
            "question_text": result_data.get("question_text"),
            "demo_id": result_data.get("demo_id"),
            "row_count": result_data.get("row_count"),
            "chart": result_data.get("chart"),
            "insight_text": result_data.get("insight_text"),
            "error": result_data.get("error"),
        }
        return json.dumps(compact, default=str)

    if tool_name == "question_group_by_demographic":
        compact = {
            "analysis_type": result_data.get("analysis_type"),
            "question_group": result_data.get("question_group"),
            "question_text": result_data.get("question_text"),
            "demo_id": result_data.get("demo_id"),
            "selected_response_option": result_data.get("selected_response_option"),
            "item_keywords": result_data.get("item_keywords"),
            "item_count": result_data.get("item_count"),
            "significant_item_count": result_data.get("significant_item_count"),
            "significant_differences": result_data.get("significant_differences"),
            "chart": result_data.get("chart"),
            "insight_text": result_data.get("insight_text"),
            "error": result_data.get("error"),
        }
        return json.dumps(compact, default=str)

    return json.dumps(result_data, default=str)


def _is_transient_llm_error(error: Exception) -> bool:
    msg = str(error).lower()
    transient_markers = (
        " 429 ",
        "503",
        "unavailable",
        "timed out",
        "timeout",
        "temporary",
        "temporarily",
        "overloaded",
        "high demand",
        "try again later",
    )
    return any(marker in msg for marker in transient_markers)


def _tool_call_signature(name: str, tool_input: dict[str, Any]) -> str:
    try:
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        payload = str(tool_input)
    return f"{name}:{payload}"


async def run_agent_loop(
    messages: list[dict],
    llm_override: dict[str, Any] | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Run the agent loop. Yields SSE event dicts:
      {event: "text_delta", data: {content: str}}
      {event: "tool_start", data: {tool: str, input: dict}}
      {event: "tool_result", data: {tool: str, result: dict}}
      {event: "chart", data: {spec: dict}}
      {event: "done", data: {}}
      {event: "error", data: {message: str}}
    """
    try:
        llm_options = resolve_runtime_options(llm_override)
    except Exception as e:
        yield {"event": "error", "data": {"message": str(e)}}
        return

    try:
        provider = get_provider(llm_options)
        system_prompt = build_system_prompt()
        charts: list[dict] = []
        full_text = ""
        fallback_insight_text = ""
        text_emitted = False
        history: list[dict] = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        model_candidates = [llm_options.model_id, *llm_options.fallback_model_ids]
        # Preserve order while removing duplicates/empties.
        model_candidates = list(dict.fromkeys([m.strip() for m in model_candidates if m.strip()]))
        repeated_tool_calls: dict[str, int] = {}
        stalled_turns = 0
        loop_aborted = False

        # Deterministic fast path for known demographic-breakout asks.
        last_user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content"):
                last_user_text = str(m.get("content"))
                break
        direct_result = build_direct_result_for_user_query(last_user_text)
        if isinstance(direct_result, dict) and "error" not in direct_result:
            direct_tool = str(direct_result.get("analysis_type", "direct_analysis"))
            yield {"event": "status", "data": {"message": "Direct analysis path"}}
            yield {
                "event": "tool_start",
                "data": {"tool": direct_tool, "input": {"query": last_user_text}},
            }
            yield {
                "event": "tool_result",
                "data": {"tool": direct_tool, "result": direct_result},
            }
            chart_spec = direct_result.get("chart")
            if isinstance(chart_spec, dict):
                charts.append(chart_spec)
                yield {"event": "chart", "data": {"spec": chart_spec}}
            insight_text = direct_result.get("insight_text")
            if isinstance(insight_text, str) and insight_text.strip():
                full_text = insight_text.strip()
                text_emitted = True
                yield {"event": "text_delta", "data": {"content": full_text}}
            yield {"event": "done", "data": {"charts": charts, "text": full_text}}
            return

        for _ in range(settings.llm_max_iterations):
            turn = None
            last_error: Exception | None = None

            for model_id in model_candidates:
                for attempt in range(llm_options.retry_attempts + 1):
                    yield {
                        "event": "status",
                        "data": {
                            "message": f"Model step: {model_id} (attempt {attempt + 1})"
                        },
                    }
                    try:
                        turn = await asyncio.to_thread(
                            provider.run_turn,
                            system_prompt=system_prompt,
                            tools=TOOL_DEFINITIONS,
                            history=history,
                            model_id=model_id,
                            max_tokens=llm_options.max_tokens,
                            timeout_seconds=llm_options.timeout_seconds,
                        )
                        break
                    except Exception as e:
                        last_error = e
                        if not _is_transient_llm_error(e):
                            break
                        if attempt >= llm_options.retry_attempts:
                            break
                        backoff = llm_options.retry_backoff_seconds * (2 ** attempt)
                        await asyncio.sleep(backoff)

                if turn is not None:
                    break

            if turn is None:
                message = str(last_error) if last_error else "LLM call failed."
                if len(model_candidates) > 1:
                    message += f" Tried models: {', '.join(model_candidates)}"
                yield {"event": "error", "data": {"message": message}}
                return

            if turn.text:
                full_text += turn.text
                stalled_turns = 0
                text_emitted = True
                yield {"event": "text_delta", "data": {"content": turn.text}}

            tool_uses = []
            blocked_repeated_call = False
            for tc in turn.tool_calls:
                yield {
                    "event": "tool_start",
                    "data": {"tool": tc.name, "input": tc.input},
                }

                signature = _tool_call_signature(tc.name, tc.input)
                repeat_count = repeated_tool_calls.get(signature, 0) + 1
                repeated_tool_calls[signature] = repeat_count

                if repeat_count > 2:
                    blocked_repeated_call = True
                    result_data: dict[str, Any] = {
                        "error": (
                            "Stopped repeated tool call loop for this same input. "
                            "Try refining the prompt to be more specific."
                        )
                    }
                    result_str = json.dumps(result_data)
                else:
                    try:
                        result_str = await asyncio.to_thread(
                            handle_tool_call,
                            tc.name,
                            tc.input,
                        )
                        try:
                            result_data = json.loads(result_str)
                        except json.JSONDecodeError:
                            result_data = {"raw": result_str}
                    except Exception as e:
                        result_data = {"error": f"Tool execution failed: {e}"}
                        result_str = json.dumps(result_data)

                if not isinstance(result_data, dict):
                    result_data = {"raw": result_data}

                insight = result_data.get("insight_text")
                if isinstance(insight, str) and insight.strip():
                    fallback_insight_text = insight.strip()

                result_for_model = _compact_tool_result_for_model(tc.name, result_data)

                yield {
                    "event": "tool_result",
                    "data": {"tool": tc.name, "result": result_data},
                }

                if "chart" in result_data and isinstance(result_data["chart"], dict):
                    charts.append(result_data["chart"])
                    yield {"event": "chart", "data": {"spec": result_data["chart"]}}

                tool_uses.append(
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                        "result": result_str,
                        "result_for_model": result_for_model,
                        "raw": tc.raw,
                    }
                )

            assistant_message: dict = {"role": "assistant", "content": turn.text or ""}
            if tool_uses:
                assistant_message["tool_calls"] = [
                    {
                        "id": tu["id"],
                        "name": tu["name"],
                        "input": tu["input"],
                        "raw": tu.get("raw"),
                    }
                    for tu in tool_uses
                ]
            if assistant_message["content"] or tool_uses:
                history.append(assistant_message)

            if not tool_uses:
                break

            for tu in tool_uses:
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tu["id"],
                        "name": tu["name"],
                        "content": tu.get("result_for_model", tu["result"]),
                    }
                )

            if blocked_repeated_call:
                loop_aborted = True
                break

            if not turn.text:
                stalled_turns += 1
                if stalled_turns >= 3:
                    loop_aborted = True
                    break

        if loop_aborted and not full_text:
            # Rescue path: fall back to deterministic tool execution before returning an error-like message.
            rescue_result = build_direct_result_for_user_query(last_user_text)
            if isinstance(rescue_result, dict) and "error" not in rescue_result:
                rescue_chart = rescue_result.get("chart")
                if isinstance(rescue_chart, dict):
                    charts.append(rescue_chart)
                    yield {"event": "chart", "data": {"spec": rescue_chart}}
                rescue_text = rescue_result.get("insight_text")
                if isinstance(rescue_text, str) and rescue_text.strip():
                    full_text = rescue_text.strip()
                else:
                    full_text = "Completed a deterministic fallback analysis."
            else:
                try:
                    quick_raw = await asyncio.to_thread(
                        handle_tool_call,
                        "quick_insight",
                        {"question": last_user_text, "top_n": 8},
                    )
                    quick = json.loads(quick_raw)
                except Exception:
                    quick = {"error": "quick_insight fallback failed."}

                if isinstance(quick, dict) and "error" not in quick:
                    quick_chart = quick.get("chart")
                    if isinstance(quick_chart, dict):
                        charts.append(quick_chart)
                        yield {"event": "chart", "data": {"spec": quick_chart}}
                    quick_text = quick.get("insight_text")
                    if isinstance(quick_text, str) and quick_text.strip():
                        full_text = quick_text.strip()
                    else:
                        full_text = "Completed a quick fallback analysis."
                elif fallback_insight_text:
                    full_text = fallback_insight_text
                elif charts:
                    full_text = "Chart generated. Ask a narrower follow-up for a written insight."
                else:
                    try:
                        sq_raw = await asyncio.to_thread(
                            handle_tool_call,
                            "search_questions",
                            {"keywords": last_user_text},
                        )
                        sq = json.loads(sq_raw)
                    except Exception:
                        sq = {}

                    candidate_lines: list[str] = []
                    if isinstance(sq, dict):
                        for row in (sq.get("results") or [])[:3]:
                            qid = str(row.get("question_id", "")).strip()
                            qtext = str(row.get("question_text", "")).strip()
                            if qid and qtext:
                                candidate_lines.append(f"- {qid}: {qtext}")

                    if candidate_lines:
                        full_text = (
                            "I could not stabilize the full tool loop, but I found close matches. "
                            "Try one of these phrasings:\n"
                            + "\n".join(candidate_lines)
                        )
                    else:
                        full_text = (
                            "I could not complete a stable analysis loop for that query. "
                            "Try specifying the metric and one demographic dimension (for example: "
                            "'How do number of stories differ by housing type?')."
                        )

        if full_text and not text_emitted and not full_text.isspace():
            # Ensure clients receive at least one text chunk when only fallback text exists.
            yield {"event": "text_delta", "data": {"content": full_text}}

        yield {"event": "done", "data": {"charts": charts, "text": full_text}}
    except Exception as e:
        yield {"event": "error", "data": {"message": f"Agent loop crashed: {e}"}}
