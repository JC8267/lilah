"""Tool definitions and handlers for the agent loop."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.provider import get_provider, resolve_runtime_options
from app.config import settings
from app.db.duckdb_engine import execute_query, execute_query_params, search_questions


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _normalize_active_filters(active_filters: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(active_filters, dict):
        return {}

    normalized: dict[str, str] = {}
    for raw_demo_id, raw_demo_level in active_filters.items():
        demo_id = str(raw_demo_id).strip()
        demo_level = str(raw_demo_level).strip()
        if not demo_id or not demo_level:
            continue
        normalized[demo_id] = demo_level
    return normalized


def _resolve_demo_clause(
    *,
    demo_level: str | None = None,
    active_filters: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Resolve SQL demo slice clause with active filter precedence."""
    normalized = _normalize_active_filters(active_filters)
    if normalized:
        # Filters are a mapping of demo_id -> demo_level. Use first item as active slice.
        for demo_id, filter_level in normalized.items():
            did_sql = _escape_sql_literal(demo_id)
            lvl_sql = _escape_sql_literal(filter_level)
            return (
                f"demo_id = '{did_sql}' AND demo_level = '{lvl_sql}'",
                [{"demo_id": demo_id, "demo_level": filter_level}],
            )

    if demo_level and demo_level.strip():
        return (
            f"demo_level = '{_escape_sql_literal(demo_level.strip())}'",
            [],
        )

    return (
        "demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'",
        [],
    )


def _short_demo_label(demo_id: str) -> str:
    if ":" in demo_id:
        return demo_id.split(":", 1)[1].strip()
    return demo_id.strip()


def _short_demo_level(demo_level: str) -> str:
    if ":" in demo_level:
        return demo_level.split(":", 1)[1].strip()
    return demo_level.strip()


def _truncate_label(text: str, max_len: int = 40) -> str:
    """Shorten long response option labels for chart readability."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "\u2026"


# Shared config block applied to every generated chart for visual consistency.
_CHART_CONFIG: dict[str, Any] = {
    "background": "#ffffff",
    "view": {"stroke": "transparent"},
    "font": "Inter, system-ui, -apple-system, sans-serif",
    "padding": {"left": 5, "right": 15, "top": 5, "bottom": 35},
    "axis": {
        "labelFontSize": 11,
        "labelLimit": 350,
        "labelColor": "#4b5563",
        "titleFontSize": 12,
        "titleColor": "#374151",
        "titlePadding": 12,
        "gridColor": "#f3f4f6",
        "gridDash": [2, 4],
        "domainColor": "#e5e7eb",
        "tickColor": "#e5e7eb",
    },
    "title": {
        "fontSize": 14,
        "fontWeight": 600,
        "color": "#1f2937",
        "subtitleFontSize": 11,
        "subtitleColor": "#6b7280",
        "offset": 12,
    },
    "bar": {
        "continuousBandSize": 18,
    },
    "rect": {
        "stroke": "#ffffff",
        "strokeWidth": 2,
    },
    "range": {
        "category": [
            "#0058a3", "#ffdb00", "#cc0008", "#929292",
            "#0a8a00", "#e87d1e", "#48a9a6", "#6b4c9a",
            "#d4a843", "#b5525c",
        ],
    },
}


# Branded blue palette for single-dimension horizontal bar charts.
_BRAND_BLUE = "#0058a3"
_BRAND_BLUE_LIGHT = "#5b9bd5"


_QUESTION_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "being", "been",
    "how", "does", "do", "did", "what", "which", "who", "whom", "where",
    "when", "why", "can", "could", "should", "would", "about", "for", "with",
    "from", "into", "onto", "that", "this", "these", "those", "your", "their",
    "our", "and", "or", "to", "of", "in", "on", "by", "differ", "difference",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


_MATCH_TOKEN_ALIASES = {
    "homes": "home",
    "housing": "home",
    "houses": "house",
    "households": "household",
    "types": "type",
    "kinds": "kind",
    "people": "person",
    "respondents": "respondent",
}


def _normalize_match_token(token: str) -> str:
    t = token.lower().strip()
    if not t:
        return ""

    t = _MATCH_TOKEN_ALIASES.get(t, t)
    if len(t) > 4 and t.endswith("ies"):
        t = f"{t[:-3]}y"
    elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return _MATCH_TOKEN_ALIASES.get(t, t)


def _tokenize_for_match(text: str) -> list[str]:
    out: list[str] = []
    for token in _tokenize(text):
        normalized = _normalize_match_token(token)
        if normalized:
            out.append(normalized)
    return out


def _extract_json_dict(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _model_assisted_question_decision(
    query: str,
    matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not settings.question_match_model_assist or not matches:
        return None

    try:
        top_k = max(3, min(int(settings.question_match_model_top_k), 20))
    except Exception:
        top_k = 8
    candidates = matches[:top_k]

    try:
        llm_options = resolve_runtime_options(None)
        provider = get_provider(llm_options)
    except Exception:
        return None

    inferred_demo_id = _resolve_demo_id_from_keywords(query)
    payload = {
        "query": query,
        "inferred_demo_id": inferred_demo_id,
        "candidates": [
            {
                "rank": i + 1,
                "question_id": str(c.get("question_id", "")),
                "question_group": str(c.get("question_group", "")),
                "question_text": str(c.get("question_text", "")),
                "match_score": c.get("match_score"),
            }
            for i, c in enumerate(candidates)
        ],
    }

    system_prompt = (
        "You are a strict classifier for survey-question retrieval. "
        "Choose the best candidate ONLY from provided candidates. "
        "If the user intent is a demographic distribution request (for example housing type), "
        "set intent to demographic_breakout and provide selected_demo_id when possible. "
        "Prefer semantic fit over token overlap and avoid household-size questions for housing-type asks. "
        "Return JSON only with keys: intent, selected_question_id, selected_demo_id, confidence, reason. "
        "intent must be one of: question, demographic_breakout, unclear."
    )

    timeout_seconds = max(
        2.0,
        min(
            float(settings.question_match_model_timeout_seconds),
            float(llm_options.timeout_seconds),
        ),
    )

    try:
        turn = provider.run_turn(
            system_prompt=system_prompt,
            tools=[],
            history=[{"role": "user", "content": json.dumps(payload, ensure_ascii=True)}],
            model_id=llm_options.model_id,
            max_tokens=min(512, llm_options.max_tokens),
            timeout_seconds=timeout_seconds,
            reasoning_effort=llm_options.reasoning_effort,
        )
    except Exception:
        return None

    parsed = _extract_json_dict(turn.text)
    if not parsed:
        return None

    intent = str(parsed.get("intent", "question")).strip().lower()
    if intent not in {"question", "demographic_breakout", "unclear"}:
        intent = "unclear"

    raw_qid = parsed.get("selected_question_id")
    selected_question_id = raw_qid.strip() if isinstance(raw_qid, str) else ""
    raw_demo = parsed.get("selected_demo_id")
    selected_demo_id = raw_demo.strip() if isinstance(raw_demo, str) else ""
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "intent": intent,
        "selected_question_id": selected_question_id,
        "selected_demo_id": selected_demo_id,
        "confidence": max(0.0, min(confidence, 1.0)),
        "reason": str(parsed.get("reason", "")),
    }


def _score_question_match(query: str, candidate: dict[str, Any]) -> float:
    query_text = query.lower()
    cand_text = " ".join(
        [
            str(candidate.get("question_text", "")),
            str(candidate.get("question_group", "")),
            str(candidate.get("question_id", "")),
        ]
    ).lower()
    cand_tokens = set(_tokenize_for_match(cand_text))
    q_tokens = [
        t
        for t in _tokenize_for_match(query_text)
        if len(t) >= 3 and t not in _QUESTION_STOPWORDS
    ]

    score = 0.0
    for t in q_tokens:
        if t in cand_tokens:
            score += 1.0
            if len(t) >= 7:
                score += 0.5

    group = str(candidate.get("question_group", "")).upper()

    # Intent boosts for common semantics.
    if "furniture" in query_text and ("furniture" in cand_text or "furnishings" in cand_text):
        score += 8.0
    if "ownership" in query_text and any(x in cand_text for x in ("have", "own", "ownership")):
        score += 4.0
    if "furniture" in query_text and group.startswith("IKEA102"):
        score += 6.0

    # Strong penalties for obviously mismatched question families.
    if "furniture" in query_text and any(
        x in cand_text for x in ("stories/floors", "how many stories", "square footage")
    ):
        score -= 8.0

    # Disfavor household-count demographics for housing-type asks.
    housing_type_intent = (
        any(t in query_text for t in ("home", "homes", "housing"))
        and any(t in query_text for t in ("type", "types", "kind", "kinds"))
    )
    if housing_type_intent and "household" in cand_text:
        score -= 5.0

    if _is_age_demographic_intent(query_text) and any(
        x in cand_text
        for x in (
            "square footage",
            "approximate square footage",
            "average size",
            "bedroom(s)",
        )
    ):
        score -= 10.0

    if _is_home_size_intent(query_text):
        if (
            "square footage" in cand_text
            and "your home" in cand_text
            and "bedroom" not in cand_text
        ):
            score += 8.0
        if any(
            x in cand_text
            for x in (
                "bedroom",
                "this room",
                "kitchen area",
                "dining room",
                "room?",
            )
        ):
            score -= 8.0

    if _is_planned_purchase_intent(query_text):
        if any(x in cand_text for x in ("planning to purchase", "plan to purchase")):
            score += 5.0
        if "obstacles" in cand_text:
            score -= 6.0
        room_hint = _extract_room_hint(query_text)
        if room_hint and room_hint in cand_text:
            score += 3.0

    return score


def _pick_best_question_match(query: str, matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not matches:
        return None
    best_idx = 0
    best_score = float("-inf")
    for idx, candidate in enumerate(matches):
        score = _score_question_match(query, candidate)
        if score > best_score:
            best_score = score
            best_idx = idx
    # If no meaningful signal, keep original ranking.
    if best_score <= 0:
        return matches[0]
    return matches[best_idx]


def _select_question_match_and_route(
    query: str,
    matches: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    if not matches:
        return None, None

    heuristic_best = _pick_best_question_match(query, matches) or matches[0]

    decision = _model_assisted_question_decision(query, matches)
    try:
        min_conf = float(settings.question_match_model_min_confidence)
    except Exception:
        min_conf = 0.55
    min_conf = max(0.0, min(min_conf, 1.0))

    if (
        not isinstance(decision, dict)
        or float(decision.get("confidence", 0.0)) < min_conf
    ):
        return heuristic_best, None

    intent = str(decision.get("intent", "question")).strip().lower()
    if intent == "demographic_breakout":
        demo_id = str(decision.get("selected_demo_id", "")).strip()
        if not demo_id:
            demo_id = _resolve_demo_id_from_keywords(query) or ""
        if demo_id:
            return None, demo_id

    if intent == "question":
        selected_qid = str(decision.get("selected_question_id", "")).strip()
        if selected_qid:
            for candidate in matches:
                if str(candidate.get("question_id", "")).strip() == selected_qid:
                    return candidate, None

    return heuristic_best, None


def _is_matrix_ownership_intent(text: str) -> bool:
    q = text.lower()
    has_own = any(t in q for t in ("ownership", "owned", "own", "have", "has"))
    has_itemish = any(
        t in q
        for t in (
            "furniture",
            "furnishings",
            "items",
            "products",
            "pieces",
            "sofa",
            "couch",
            "loveseat",
            "sofa bed",
            "rug",
            "table",
            "chair",
            "armchair",
            "bookcase",
            "closet",
            "media stand",
        )
    )
    return has_own and has_itemish


def _extract_matrix_item_keywords(text: str) -> list[str]:
    q = text.lower()
    keywords: list[str] = []
    if any(t in q for t in ("sofa", "couch", "loveseat", "sofa bed")):
        keywords.extend(["sofa", "couch", "loveseat"])
    if "rug" in q:
        keywords.append("rug")
    if "chair" in q or "armchair" in q:
        keywords.extend(["chair", "armchair"])
    if "table" in q:
        keywords.append("table")
    return sorted(set(keywords))


def _pick_preferred_binary_response_option(options: list[str]) -> str | None:
    lowered = {o.lower(): o for o in options}
    for candidate in ("selected", "yes", "true", "own"):
        if candidate in lowered:
            return lowered[candidate]
    return None


def _is_binary_response_option_set(options: list[str]) -> bool:
    if not options:
        return False
    normalized = {str(o).strip().lower() for o in options if str(o).strip()}
    if not normalized:
        return False
    allowed = {
        "selected",
        "not selected",
        "yes",
        "no",
        "true",
        "false",
        "own",
        "not own",
    }
    return len(normalized) <= 2 and normalized.issubset(allowed)


def _is_housing_type_breakout_intent(text: str) -> bool:
    q = (text or "").lower()
    has_home_context = any(t in q for t in ("home", "homes", "housing", "house", "houses"))
    has_type_context = any(t in q for t in ("type", "types", "kind", "kinds"))
    has_living_context = any(t in q for t in ("live", "living", "reside", "residing"))
    return has_home_context and has_type_context and has_living_context


def _is_age_demographic_intent(text: str) -> bool:
    q = (text or "").lower()
    has_age = bool(re.search(r"\bage\b", q))
    if not has_age:
        return False
    # Do not hijack child-age phrasing that maps to household-children questions.
    if "children" in q and "household" in q:
        return False
    return True


def _is_age_homeownership_intent(text: str) -> bool:
    q = (text or "").lower()
    return _is_age_demographic_intent(q) and any(
        t in q for t in ("homeowner", "home ownership", "renter", "own home", "owns home")
    )


def _is_bedroom_size_intent(text: str) -> bool:
    q = (text or "").lower()
    return any(t in q for t in ("bedroom", "bed room", "master bedroom"))


def _is_home_size_intent(text: str) -> bool:
    q = (text or "").lower()
    if any(t in q for t in ("homeowner", "home ownership", "ownership")):
        return False
    has_home_context = any(t in q for t in ("home", "homes", "house", "houses"))
    has_size_context = any(
        t in q
        for t in (
            "square footage",
            "sq ft",
            "sqft",
            "how large",
            "how big",
            "size",
            "large",
            "big",
            "average home",
            "average house",
        )
    )
    return has_home_context and has_size_context and not _is_bedroom_size_intent(q)


def _is_planned_purchase_intent(text: str) -> bool:
    q = (text or "").lower()
    has_purchase = any(t in q for t in ("purchase", "purchases", "buy", "buying"))
    has_plan = any(t in q for t in ("plan", "planned", "planning", "next 12 months"))
    has_top_purchase_phrase = any(
        t in q for t in ("top purchases", "top purchase", "most purchased", "most bought")
    )
    return has_purchase and (has_plan or has_top_purchase_phrase)


def _extract_room_hint(text: str) -> str | None:
    q = (text or "").lower()
    if any(t in q for t in ("kitchen", "kitchens")):
        return "kitchen"
    if any(t in q for t in ("bedroom", "bedrooms")):
        return "bedroom"
    if any(t in q for t in ("bathroom", "bathrooms")):
        return "bathroom"
    if any(t in q for t in ("dining room", "dining")):
        return "dining"
    if "main area" in q:
        return "main"
    if any(t in q for t in ("living room", "family room")):
        return "living"
    if any(t in q for t in ("home", "homes", "house", "houses")):
        return "home"
    return None


def _resolve_planned_purchase_group(text: str) -> dict[str, str] | None:
    if not _is_planned_purchase_intent(text):
        return None

    room_hint = _extract_room_hint(text)
    if not room_hint:
        return None

    room_sql = _escape_sql_literal(room_hint)
    sql = f"""
        SELECT
            question_group,
            MIN(question_text) AS question_text,
            COUNT(*) AS n_rows
        FROM question_catalog
        WHERE LOWER(question_text) LIKE '%plan%'
          AND LOWER(question_text) LIKE '%purchase%'
          AND LOWER(question_text) LIKE '%select all that apply%'
          AND LOWER(question_text) LIKE '%{room_sql}%'
        GROUP BY question_group
        ORDER BY n_rows DESC, question_group
        LIMIT 1
    """
    result = execute_query(sql)
    if "error" in result:
        return None
    rows = result.get("rows", [])
    if not rows:
        return None
    return {
        "question_group": str(rows[0][0]),
        "question_text": str(rows[0][1]),
        "room_hint": room_hint,
    }


_ROOM_INTENT_GROUPS: dict[str, dict[str, str]] = {
    "have_items": {
        "main": "IKEA102",
        "bedroom": "IKEA202",
        "kitchen": "IKEA304",
        "living": "IKEA402",
        "dining": "IKEA504",
        "home": "IKEA8",
    },
    "planned_purchases": {
        "main": "IKEA107",
        "bedroom": "IKEA206",
        "kitchen": "IKEA312",
        "living": "IKEA407",
        "dining": "IKEA509",
        "home": "IKEA703",
    },
    "buying_factors": {
        "main": "IKEA109",
        "bedroom": "IKEA208",
        "kitchen": "IKEA314",
        "living": "IKEA409",
        "dining": "IKEA511",
    },
    "activities_current": {
        "main": "IKEA104a",
        "bedroom": "IKEA204a",
        "kitchen": "IKEA309a",
        "dining": "IKEA506a",
    },
    "activities_desired": {
        "main": "IKEA104b",
        "bedroom": "IKEA204b",
        "kitchen": "IKEA309b",
        "dining": "IKEA506b",
    },
    "obstacles": {
        "home": "IKEA705",
    },
    "improvements_made": {
        "home": "IKEA12",
    },
    "improvements_planned": {
        "home": "IKEA703",
    },
    "children_play_rooms": {
        "home": "IKEA604",
    },
    "children_study_rooms": {
        "home": "IKEA603",
    },
}


_ROOM_INTENT_META: dict[str, dict[str, str]] = {
    "have_items": {
        "analysis_type": "top_owned_items",
        "title_prefix": "Top Owned Items",
        "item_label": "Owned item",
        "summary_noun": "owned item",
    },
    "planned_purchases": {
        "analysis_type": "top_planned_purchases",
        "title_prefix": "Top Planned Purchases",
        "item_label": "Planned purchase",
        "summary_noun": "planned purchase",
    },
    "buying_factors": {
        "analysis_type": "top_buying_factors",
        "title_prefix": "Top Buying Factors",
        "item_label": "Buying factor",
        "summary_noun": "buying factor",
    },
    "activities_current": {
        "analysis_type": "top_current_activities",
        "title_prefix": "Top Current Activities",
        "item_label": "Current activity",
        "summary_noun": "activity",
    },
    "activities_desired": {
        "analysis_type": "top_desired_activities",
        "title_prefix": "Top Desired Activities",
        "item_label": "Desired activity",
        "summary_noun": "desired activity",
    },
    "obstacles": {
        "analysis_type": "top_obstacles",
        "title_prefix": "Top Obstacles",
        "item_label": "Obstacle",
        "summary_noun": "obstacle",
    },
    "improvements_made": {
        "analysis_type": "top_completed_improvements",
        "title_prefix": "Top Completed Improvements",
        "item_label": "Completed improvement",
        "summary_noun": "completed improvement",
    },
    "improvements_planned": {
        "analysis_type": "top_planned_improvements",
        "title_prefix": "Top Planned Improvements",
        "item_label": "Planned improvement",
        "summary_noun": "planned improvement",
    },
    "children_play_rooms": {
        "analysis_type": "top_children_play_rooms",
        "title_prefix": "Top Rooms Children Play In",
        "item_label": "Room",
        "summary_noun": "room",
    },
    "children_study_rooms": {
        "analysis_type": "top_children_study_rooms",
        "title_prefix": "Top Rooms Children Study In",
        "item_label": "Room",
        "summary_noun": "room",
    },
}


def _resolve_group_question_text(question_group: str) -> str:
    qg_sql = _escape_sql_literal(question_group)
    sql = f"""
        SELECT MIN(question_text) AS question_text
        FROM question_catalog
        WHERE question_group = '{qg_sql}'
    """
    result = execute_query(sql)
    if "error" in result:
        return question_group
    rows = result.get("rows", [])
    if not rows or not rows[0]:
        return question_group
    return str(rows[0][0] or question_group)


def _detect_room_matrix_intent(text: str) -> str | None:
    q = (text or "").lower()

    if _is_planned_purchase_intent(q):
        return "planned_purchases"

    has_children_terms = any(
        t in q
        for t in (
            "children",
            "child",
            "kids",
            "kid",
        )
    )
    has_room_terms = any(t in q for t in ("room", "rooms", "which room", "which rooms"))
    has_where_children = bool(re.search(r"\bwhere\b.*\b(children|child|kids|kid)\b", q)) or bool(
        re.search(r"\b(children|child|kids|kid)\b.*\bwhere\b", q)
    )
    has_play_terms = any(t in q for t in ("play", "playing", "playtime"))
    has_study_terms = any(t in q for t in ("study", "studying", "homework", "schoolwork"))
    if has_children_terms and (has_room_terms or has_where_children):
        if has_play_terms:
            return "children_play_rooms"
        if has_study_terms:
            return "children_study_rooms"

    has_have = any(t in q for t in ("have ", " have", "owns", "own ", "features", "amenities"))
    has_item_context = any(
        t in q for t in ("furniture", "furnishings", "appliances", "items", "features", "amenities")
    )
    if has_have and has_item_context and "homeowner" not in q and "home ownership" not in q:
        return "have_items"

    if any(t in q for t in ("obstacle", "obstacles", "challenge", "barrier", "difficult")):
        return "obstacles"

    if any(t in q for t in ("factor", "factors", "important", "priority", "priorities")) and any(
        t in q for t in ("buy", "buying", "purchase", "shopping")
    ):
        return "buying_factors"

    if any(t in q for t in ("currently do", "regularly do", "current activities")):
        return "activities_current"

    if (
        any(t in q for t in ("would like to", "if you could", "wish you could", "remaining activities"))
        or bool(re.search(r"\bwould\b.*\blike to\b", q))
    ):
        return "activities_desired"

    has_improvement_terms = any(t in q for t in ("improvement", "improvements", "improvment", "improvments", "changes"))

    if has_improvement_terms and (
        any(t in q for t in ("made in the last 12 months", "made in last 12 months", "have you made", "already made"))
        or bool(re.search(r"\bmade\b.*\b12 months\b", q))
    ):
        return "improvements_made"

    if has_improvement_terms and (
        any(
            t in q
            for t in (
                "planned improvements",
                "plan improvements",
                "planning improvements",
                "improvements are planned",
                "planned improvments",
                "plan improvments",
                "planning improvments",
                "improvments are planned",
                "planned changes",
                "changes planned",
            )
        )
        or bool(re.search(r"\bplan\w*\b.*\bimprov\w*\b|\bimprov\w*\b.*\bplan\w*\b", q))
    ):
        return "improvements_planned"

    return None


def _resolve_room_intent_group(text: str) -> dict[str, Any] | None:
    # Keep the high-precision planned-purchase catalog resolver as the first choice.
    planned = _resolve_planned_purchase_group(text)
    if planned:
        meta = _ROOM_INTENT_META["planned_purchases"]
        return {
            "intent": "planned_purchases",
            "question_group": planned["question_group"],
            "question_text": planned["question_text"],
            "room_hint": planned["room_hint"],
            **meta,
            "item_keywords": None,
        }

    intent = _detect_room_matrix_intent(text)
    if not intent:
        return None

    room_map = _ROOM_INTENT_GROUPS.get(intent, {})
    if not room_map:
        return None

    room_hint = _extract_room_hint(text)
    target_room = room_hint if room_hint in room_map else None
    if target_room is None and room_hint:
        if room_hint in {"home", "living"} and "main" in room_map:
            target_room = "main"
        elif len(room_map) > 1:
            # For multi-room families, avoid silently falling back to an unrelated room.
            # Example: "bathroom planned purchases" should not auto-route to "home".
            return None
    if target_room is None:
        if "home" in room_map:
            target_room = "home"
        elif "main" in room_map:
            target_room = "main"
        elif room_map:
            target_room = next(iter(room_map.keys()))
    if target_room is None:
        return None

    question_group = room_map[target_room]
    question_text = _resolve_group_question_text(question_group)
    meta = _ROOM_INTENT_META.get(intent, {})

    item_keywords: list[str] | None = None
    if intent == "obstacles" and room_hint in {"kitchen", "bedroom", "living", "dining"}:
        synonyms = {
            "kitchen": ["kitchen"],
            "bedroom": ["bedroom"],
            "living": ["living", "family room", "main area"],
            "dining": ["dining"],
        }
        item_keywords = synonyms.get(room_hint, [room_hint])

    return {
        "intent": intent,
        "question_group": question_group,
        "question_text": question_text,
        "room_hint": target_room,
        "item_keywords": item_keywords,
        **meta,
    }


def _build_top_selected_for_question_group(
    *,
    question_group: str,
    question_text: str,
    room_hint: str,
    top_n: int = 10,
    analysis_type: str = "top_selected_items",
    title_prefix: str = "Top Selected Items",
    item_label: str = "Item",
    summary_noun: str = "item",
    item_keywords: list[str] | None = None,
    active_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    top_n = max(3, min(int(top_n), 20))
    qg_sql = _escape_sql_literal(question_group)
    demo_sql, applied_filters = _resolve_demo_clause(active_filters=active_filters)
    demo_slice = applied_filters[0]["demo_level"] if applied_filters else "TOTAL: Total respondents"

    option_sql = f"""
        SELECT DISTINCT response_option
        FROM survey_long
        WHERE question_group = '{qg_sql}'
          AND response_option IS NOT NULL
          AND TRIM(response_option) <> ''
          AND {demo_sql}
        ORDER BY response_option
    """
    option_result = execute_query(option_sql)
    if "error" in option_result:
        return {"error": option_result["error"], "question_group": question_group}

    options = [str(r[0]).strip() for r in option_result.get("rows", []) if r and str(r[0]).strip()]
    preferred = _pick_preferred_binary_response_option(options)
    if not preferred:
        return {
            "error": "Could not find a selected/yes-style response option for this question group.",
            "question_group": question_group,
        }

    pref_sql = _escape_sql_literal(preferred)
    item_filter_sql = ""
    clean_item_keywords: list[str] = []
    if item_keywords:
        clean_item_keywords = [k.strip().lower() for k in item_keywords if str(k).strip()]
        if clean_item_keywords:
            filters = " OR ".join(
                f"LOWER(COALESCE(NULLIF(TRIM(question_level), ''), question_text, question_id)) "
                f"LIKE '%{_escape_sql_literal(k)}%'"
                for k in clean_item_keywords
            )
            item_filter_sql = f"AND ({filters})"

    data_sql = f"""
        SELECT
            COALESCE(NULLIF(TRIM(question_level), ''), question_id) AS item,
            100.0 * AVG(response_value_num) AS percent
        FROM survey_long
        WHERE question_group = '{qg_sql}'
          AND LOWER(response_option) = LOWER('{pref_sql}')
          AND {demo_sql}
          {item_filter_sql}
        GROUP BY item
        ORDER BY percent DESC
        LIMIT {top_n}
    """
    data_result = execute_query(data_sql)
    if "error" in data_result:
        return {"error": data_result["error"], "question_group": question_group}

    rows = data_result.get("rows", [])
    if not rows and clean_item_keywords:
        # Fallback to full group when keyword-filtered subset is empty.
        data_sql = f"""
            SELECT
                COALESCE(NULLIF(TRIM(question_level), ''), question_id) AS item,
                100.0 * AVG(response_value_num) AS percent
            FROM survey_long
            WHERE question_group = '{qg_sql}'
              AND LOWER(response_option) = LOWER('{pref_sql}')
              AND {demo_sql}
            GROUP BY item
            ORDER BY percent DESC
            LIMIT {top_n}
        """
        data_result = execute_query(data_sql)
        if "error" not in data_result:
            rows = data_result.get("rows", [])

    if not rows:
        return {
            "error": "No selected-item rows returned for this question group.",
            "question_group": question_group,
        }

    chart_values = []
    for row in rows:
        item = str(row[0]).strip()
        try:
            pct = float(row[1])
        except (TypeError, ValueError):
            continue
        chart_values.append({"item": _truncate_label(item, 42), "percent": round(pct, 2)})

    if not chart_values:
        return {
            "error": "No numeric selected-item values available for this group.",
            "question_group": question_group,
        }

    top1 = chart_values[0]
    top2 = chart_values[1] if len(chart_values) > 1 else None
    top3_sum = sum(v["percent"] for v in chart_values[:3])
    spread = top1["percent"] - chart_values[-1]["percent"]

    title_room = room_hint.capitalize()
    chart_spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {
            "text": f"{title_prefix} — {title_room}",
            "subtitle": f"{question_group} · Response basis: {preferred} · Slice: {demo_slice}",
            "anchor": "start",
        },
        "data": {"values": chart_values},
        "height": {"step": 28},
        "encoding": {
            "y": {
                "field": "item",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 400, "labelFontSize": 12},
            },
            "x": {
                "field": "percent",
                "type": "quantitative",
                "title": "% selected",
                "axis": {"format": ".0f", "grid": True},
            },
            "tooltip": [
                {"field": "item", "type": "nominal", "title": item_label},
                {"field": "percent", "type": "quantitative", "title": "% selected", "format": ".1f"},
            ],
        },
        "layer": [
            {"mark": {"type": "bar", "cornerRadiusEnd": 4, "color": _BRAND_BLUE}},
            {
                "mark": {
                    "type": "text",
                    "align": "left",
                    "dx": 4,
                    "fontSize": 11,
                    "fontWeight": 500,
                    "color": "#374151",
                },
                "encoding": {
                    "text": {"field": "percent", "type": "quantitative", "format": ".1f"}
                },
            },
        ],
        "config": _CHART_CONFIG,
    }

    lines = [
        f"**{question_group} — {question_text}**",
        f"- Top {summary_noun}: **{top1['item']}** at **{top1['percent']:.2f}%** selected.",
    ]
    if top2:
        lines.append(
            f"- Runner-up {summary_noun}: **{top2['item']}** at **{top2['percent']:.2f}%** "
            f"({top1['percent'] - top2['percent']:.2f} pts behind)."
        )
    lines.append(f"- Concentration: top 3 items sum to **{top3_sum:.2f}%**.")
    lines.append(f"- Spread across shown items: **{spread:.2f} pts**.")
    lines.append(f"- Response basis: **{preferred}** in **{demo_slice}**.")
    if applied_filters:
        af = applied_filters[0]
        lines.append(
            f"- Active filter applied: **{af['demo_id']} = {af['demo_level']}**."
        )
    if clean_item_keywords:
        lines.append(f"- Filtered to items matching: {', '.join(sorted(set(clean_item_keywords)))}.")

    return {
        "analysis_type": analysis_type,
        "question_group": question_group,
        "question_text": question_text,
        "room_hint": room_hint,
        "selected_response_option": preferred,
        "row_count": len(chart_values),
        "top_rows": chart_values,
        "item_keywords": clean_item_keywords or None,
        "applied_filters": applied_filters,
        "sql": data_sql.strip(),
        "insight_text": "\n".join(lines),
        "chart": chart_spec,
    }


def _pick_home_size_question_match(matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candidate in matches:
        text = str(candidate.get("question_text", "")).lower()
        if (
            "square footage" in text
            and "your home" in text
            and "bedroom" not in text
        ):
            return candidate

    sql = """
        SELECT
            question_id,
            question_group,
            question_text,
            has_question_level,
            response_option_count,
            demo_break_count
        FROM question_catalog
        WHERE LOWER(question_text) LIKE '%square footage%'
          AND LOWER(question_text) LIKE '%your home%'
          AND LOWER(question_text) NOT LIKE '%bedroom%'
        ORDER BY
          CASE WHEN question_id = 'IKEA5' THEN 0 ELSE 1 END,
          question_id
        LIMIT 1
    """
    result = execute_query(sql)
    if "error" in result:
        return None
    rows = result.get("rows", [])
    cols = result.get("columns", [])
    if not rows or not cols:
        return None
    row = rows[0]
    return dict(zip(cols, row))


def _has_comparison_intent(text: str) -> bool:
    q = (text or "").lower()
    return any(
        marker in q
        for marker in (
            " differ ",
            " difference ",
            " compare ",
            " compared ",
            " versus ",
            " vs ",
        )
    )


def _resolve_segment_first_comparison_request(text: str) -> dict[str, str] | None:
    """Parse phrasing like: 'compare <segment> vs <other> for <topic>'."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    q = cleaned.lower()
    if not _has_comparison_intent(q):
        return None
    if " vs " not in q and " versus " not in q:
        return None

    for_idx = q.rfind(" for ")
    if for_idx == -1:
        return None

    segment_expr = cleaned[:for_idx].strip(" ?.")
    segment_expr = re.sub(r"^(compare|comparing)\s+", "", segment_expr, flags=re.IGNORECASE).strip()
    subject_question = cleaned[for_idx + 5 :].strip(" ?.")
    if not segment_expr or len(subject_question) < 3:
        return None

    demo_id = _resolve_demo_id_from_text(segment_expr)
    if not demo_id:
        return None

    return {
        "segment_expr": segment_expr,
        "subject_question": subject_question,
        "demo_id": demo_id,
    }


DEMO_KEYWORD_TO_ID: list[tuple[str, str]] = [
    ("children in household", "TOTAL: Children in Household"),
    ("kids in household", "TOTAL: Children in Household"),
    ("children household", "TOTAL: Children in Household"),
    ("living with children", "TOTAL: Children in Household"),
    ("with children", "TOTAL: Children in Household"),
    ("living without children", "TOTAL: Children in Household"),
    ("without children", "TOTAL: Children in Household"),
    ("no children", "TOTAL: Children in Household"),
    ("income", "TOTAL: Income"),
    ("age", "TOTAL: Age"),
    ("ages", "TOTAL: Age"),
    ("age cohort", "TOTAL: Age"),
    ("age cohorts", "TOTAL: Age"),
    ("gender", "TOTAL: Gender"),
    ("sex", "TOTAL: Gender"),
    ("ethnicity", "TOTAL: Ethnicity"),
    ("etnicity", "TOTAL: Ethnicity"),
    ("ethnic", "TOTAL: Ethnicity"),
    ("hispanic", "TOTAL: Ethnicity"),
    ("hispanics", "TOTAL: Ethnicity"),
    ("latino", "TOTAL: Ethnicity"),
    ("latina", "TOTAL: Ethnicity"),
    ("race", "TOTAL: Ethnicity"),
    ("region", "TOTAL: Region"),
    ("education", "TOTAL: Education"),
    ("home ownership", "TOTAL: Home Ownership"),
    ("homeowner", "TOTAL: Home Ownership"),
    ("homeowners", "TOTAL: Home Ownership"),
    ("owner", "TOTAL: Home Ownership"),
    ("owners", "TOTAL: Home Ownership"),
    ("renter", "TOTAL: Home Ownership"),
    ("renters", "TOTAL: Home Ownership"),
    ("housing type", "TOTAL: Housing Type"),
    ("home type", "TOTAL: Housing Type"),
    ("type of home", "TOTAL: Housing Type"),
    ("type of homes", "TOTAL: Housing Type"),
    ("types of home", "TOTAL: Housing Type"),
    ("types of homes", "TOTAL: Housing Type"),
    ("kind of home", "TOTAL: Housing Type"),
    ("kinds of homes", "TOTAL: Housing Type"),
    ("customer type", "TOTAL: Customer Type"),
    ("area type", "TOTAL: Area Type"),
    ("work from home", "TOTAL: Work From Home Frequency"),
    ("wfh", "TOTAL: Work From Home Frequency"),
]


def _contains_keyword_phrase(text: str, keyword: str) -> bool:
    phrase = " ".join(keyword.lower().split())
    if not phrase:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(part) for part in phrase.split(" ")) + r"\b"
    return bool(re.search(pattern, text))


def _resolve_demo_id_from_keywords(text: str) -> str | None:
    if _is_housing_type_breakout_intent(text):
        return "TOTAL: Housing Type"
    q = (text or "").lower()
    if any(t in q for t in ("apartment", "apartments")) and any(
        t in q for t in ("home", "homes", "house", "houses")
    ):
        return "TOTAL: Housing Type"

    for keyword, demo_id in DEMO_KEYWORD_TO_ID:
        if _contains_keyword_phrase(q, keyword):
            return demo_id
    return None


def _resolve_demo_id_from_text(text: str) -> str | None:
    exact = _resolve_demo_id_from_keywords(text)
    if exact:
        return exact

    # Fallback: fuzzy match against available demo_id values in survey_long.
    q = (text or "").lower()
    tokens = [
        t
        for t in re.split(r"[^a-z0-9]+", q)
        if len(t) >= 3 and t not in {"the", "and", "for", "with", "from", "that"}
    ]
    if not tokens:
        return None

    patterns = [f"%{t}%" for t in tokens[:4]]
    where = " OR ".join("LOWER(demo_id) LIKE ?" for _ in patterns)
    sql = f"""
        SELECT DISTINCT demo_id
        FROM survey_long
        WHERE {where}
        ORDER BY
            CASE
                WHEN demo_id LIKE 'TOTAL:%' THEN 0
                WHEN demo_id = 'Total' THEN 0
                ELSE 1
            END,
            demo_id
        LIMIT 10
    """
    result = execute_query_params(sql, patterns)
    if "error" in result:
        return None
    rows = result.get("rows", [])
    if not rows:
        return None
    return str(rows[0][0])


_BROAD_DIFF_SUPPORTED_DEMO_IDS = {
    "TOTAL: Home Ownership",
    "TOTAL: Age",
    "TOTAL: Ethnicity",
    "TOTAL: Income",
    "TOTAL: Gender",
    "TOTAL: Education",
    "TOTAL: Region",
    "TOTAL: Area Type",
    "TOTAL: Housing Type",
}

_BROAD_DIFF_OPERATOR_TERMS = (
    "differ most",
    "differ the most",
    "most different",
    "biggest difference",
    "biggest differences",
    "largest difference",
    "largest differences",
    "vary the most",
    "stand out the most",
)

_BROAD_DIFF_SCOPE_TERMS = (
    "what sort of things",
    "what kinds of things",
    "what kind of things",
    "what things",
    "which things",
    "which topics",
    "what topics",
    "in general",
    "overall",
    "across the survey",
)


def _normalize_broad_demo_id(demo_id: str | None) -> str | None:
    if not demo_id:
        return None

    cleaned = str(demo_id).strip()
    if cleaned == "TOTAL: Race":
        return "TOTAL: Ethnicity"
    if cleaned in _BROAD_DIFF_SUPPORTED_DEMO_IDS:
        return cleaned

    if ":" not in cleaned:
        return None
    suffix = cleaned.split(":", 1)[1].strip()
    if not suffix:
        return None
    candidate = f"TOTAL: {suffix}"
    if candidate == "TOTAL: Race":
        candidate = "TOTAL: Ethnicity"
    return candidate if candidate in _BROAD_DIFF_SUPPORTED_DEMO_IDS else None


def _resolve_broad_demo_difference_request(text: str) -> dict[str, str] | None:
    q = (text or "").lower().strip()
    if not q:
        return None

    has_operator = any(t in q for t in _BROAD_DIFF_OPERATOR_TERMS)
    if not has_operator:
        return None

    has_scope_hint = any(t in q for t in _BROAD_DIFF_SCOPE_TERMS)
    has_what_form = bool(re.search(r"\bwhat\b.*\bdiffer\b.*\bmost\b", q))
    if not (has_scope_hint or has_what_form):
        return None

    demo_id = _normalize_broad_demo_id(_resolve_demo_id_from_text(q))
    if not demo_id:
        return None

    subject = _extract_extreme_subject_query(text)
    if subject and len(_extract_answerability_anchor_tokens(subject)) >= 1:
        return None

    return {"demo_id": demo_id}


def _is_extreme_difference_intent(text: str) -> bool:
    q = (text or "").lower()
    has_operator = any(
        t in q
        for t in (
            "most different",
            "biggest difference",
            "largest difference",
            "smallest difference",
            "least different",
            "closest",
            "most similar",
            "furthest",
            "farthest",
            "higher than",
            "lower than",
            "over index",
            "under index",
            "over-index",
            "under-index",
        )
    )
    has_operator = has_operator or bool(
        re.search(r"\bdiffer\w*\b.*\b(most|least)\b|\b(most|least)\b.*\bdiffer\w*\b", q)
    )

    has_comparison = any(
        t in q
        for t in (
            "different",
            "difference",
            "differ",
            "vs ",
            " versus ",
            "compared",
            "against",
            "than national",
            "from national",
            "national average",
            "national averages",
            "than total",
            "from total",
            "total respondents",
        )
    )
    return has_operator and has_comparison


def _detect_extreme_operator(text: str) -> str:
    q = (text or "").lower()
    if any(t in q for t in ("lower than", "lowest versus", "under index", "under-index", "below")):
        return "lower"
    if any(t in q for t in ("higher than", "highest versus", "over index", "over-index", "above")):
        return "higher"
    if any(t in q for t in ("closest", "most similar", "least different", "smallest difference")):
        return "closest"
    if bool(re.search(r"\bdiffer\w*\b.*\bleast\b|\bleast\b.*\bdiffer\w*\b", q)):
        return "closest"
    return "most_different"


def _extract_extreme_subject_query(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None

    patterns = [
        r"when it comes to\s+(.+)$",
        r"in terms of\s+(.+)$",
        r"regarding\s+(.+)$",
        r"about\s+(.+)$",
        r"on\s+(.+)$",
        r"for\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .?!")
        if not candidate:
            continue
        if len(_extract_answerability_anchor_tokens(candidate)) >= 1:
            return candidate

    return None


def _resolve_target_demo_level_for_query(demo_id: str, query: str) -> str | None:
    did_sql = _escape_sql_literal(demo_id)
    sql = f"""
        SELECT DISTINCT demo_level
        FROM survey_long
        WHERE demo_id = '{did_sql}'
          AND demo_level IS NOT NULL
          AND TRIM(demo_level) <> ''
        ORDER BY demo_level
    """
    result = execute_query(sql)
    if "error" in result:
        return None

    levels = [str(r[0]).strip() for r in result.get("rows", []) if r and str(r[0]).strip()]
    if not levels:
        return None
    if len(levels) == 1:
        return levels[0]

    q = query.lower()
    q_tokens = {
        t
        for t in _tokenize_for_match(q)
        if len(t) >= 3 and t not in _QUESTION_STOPWORDS
    }

    preferred_tokens: list[str] = []
    if demo_id == "TOTAL: Home Ownership":
        if any(t in q for t in ("renter", "renters", "renting")):
            preferred_tokens = ["renter", "rent"]
        elif any(t in q for t in ("homeowner", "homeowners", "owner", "owners", "own")):
            preferred_tokens = ["homeowner", "own"]
    elif demo_id == "TOTAL: Housing Type":
        if any(t in q for t in ("apartment", "apartments", "apt")):
            preferred_tokens = ["apartment", "apt"]
        elif any(t in q for t in ("single family", "single-home", "single home", "detached")):
            preferred_tokens = ["single", "home"]
        elif any(t in q for t in ("multifamily", "multi-family", "duplex", "condo", "townhome", "townhouse")):
            preferred_tokens = ["multifamily", "duplex", "condo", "townhome", "townhouse", "multi"]
    elif demo_id == "TOTAL: Ethnicity":
        if any(t in q for t in ("hispanic", "hispanics", "latino", "latina", "latinx")):
            preferred_tokens = ["hispanic"]
        elif "black" in q:
            preferred_tokens = ["black"]
        elif "white" in q:
            preferred_tokens = ["white"]
        elif "asian" in q:
            preferred_tokens = ["asian"]
    elif demo_id == "TOTAL: Area Type":
        if "urban" in q:
            preferred_tokens = ["urban"]
        elif "suburban" in q:
            preferred_tokens = ["suburban"]
        elif "rural" in q:
            preferred_tokens = ["rural"]
    elif demo_id == "TOTAL: Region":
        for region in ("midwest", "northeast", "south", "west"):
            if region in q:
                preferred_tokens = [region]
                break
    elif demo_id == "TOTAL: Children in Household":
        if any(t in q for t in ("without children", "no children", "childless")):
            preferred_tokens = ["without", "no"]
        elif any(t in q for t in ("with children", "kids", "children")):
            preferred_tokens = ["children", "with"]

    best_level: str | None = None
    best_score = float("-inf")
    for level in levels:
        short_level = _short_demo_level(level).lower()
        level_tokens = set(_tokenize_for_match(short_level))
        score = 0.0

        if short_level and short_level in q:
            score += 6.0

        overlap = len(level_tokens.intersection(q_tokens))
        score += float(overlap) * 1.5

        if demo_id == "TOTAL: Ethnicity" and any(t in q for t in ("hispanic", "hispanics", "latino", "latina", "latinx")):
            if "hispanic" in level_tokens and "non" not in level_tokens:
                score += 6.0
            elif "hispanic" in level_tokens and "non" in level_tokens:
                score -= 3.0
        for token in preferred_tokens:
            if token and token in level_tokens:
                score += 4.0

        if score > best_score:
            best_score = score
            best_level = level

    if best_level and best_score > 0:
        return best_level
    return None


def _resolve_extreme_difference_request(text: str) -> dict[str, str] | None:
    if not _is_extreme_difference_intent(text):
        return None

    demo_id = _resolve_demo_id_from_text(text)
    if not demo_id:
        return None

    subject = _extract_extreme_subject_query(text)
    if not subject:
        return None

    target_demo_level = _resolve_target_demo_level_for_query(demo_id, text)
    if not target_demo_level:
        return None

    operator = _detect_extreme_operator(text)
    return {
        "question": subject,
        "demo_id": demo_id,
        "target_demo_level": target_demo_level,
        "operator": operator,
    }


def _build_extreme_difference_by_demographic(
    *,
    question: str,
    demo_id: str,
    target_demo_level: str,
    operator: str = "most_different",
    top_n: int = 8,
) -> dict[str, Any]:
    top_n = max(3, min(int(top_n), 15))
    did_sql = _escape_sql_literal(demo_id)
    level_sql = _escape_sql_literal(target_demo_level)
    question_group: str | None = None
    question_id: str | None = None

    room_route = _resolve_room_intent_group(question)
    if room_route:
        question_group = str(room_route.get("question_group", "")).strip()
        question_text = str(room_route.get("question_text", "")).strip()
        if not question_group:
            return {"error": "Resolved room intent is missing question_group."}
    else:
        search_result = search_questions(question)
        if "error" in search_result:
            return {"error": search_result["error"]}

        matches = search_result.get("results", [])
        if not matches:
            return {"error": "No matching questions found for extreme-difference analysis."}

        best, routed_demo_id = _select_question_match_and_route(question, matches)
        if routed_demo_id:
            return {"error": "Extreme-difference analysis requires a concrete survey question, not a demographic-only route."}
        best = best or matches[0]
        question_id = str(best.get("question_id", "")).strip()
        question_text = str(best.get("question_text", "")).strip()
        if not question_id:
            return {"error": "Matched question is missing question_id."}

        # Matrix-style questions often come back as *_1 item IDs; promote to full group-level
        # comparison so we rank meaningful items instead of Selected/Not Selected only.
        candidate_group = str(best.get("question_group", "")).strip()
        has_question_level = str(best.get("has_question_level", "")).strip().lower() in {"1", "true", "t", "yes"}
        if candidate_group and "_" in question_id and has_question_level:
            cg_sql = _escape_sql_literal(candidate_group)
            count_sql = f"""
                SELECT COUNT(DISTINCT question_id) AS n_items
                FROM question_catalog
                WHERE question_group = '{cg_sql}'
            """
            count_result = execute_query(count_sql)
            if "error" not in count_result:
                count_rows = count_result.get("rows", [])
                if count_rows and count_rows[0] and int(count_rows[0][0] or 0) > 1:
                    question_group = candidate_group
                    question_id = None
                    question_text = _resolve_group_question_text(candidate_group)

    qid_sql = _escape_sql_literal(question_id) if question_id else ""
    qg_sql = _escape_sql_literal(question_group) if question_group else ""

    if question_group:
        option_sql = f"""
            SELECT DISTINCT response_option
            FROM survey_long
            WHERE question_group = '{qg_sql}'
              AND demo_id = '{did_sql}'
              AND demo_level = '{level_sql}'
              AND response_option IS NOT NULL
              AND TRIM(response_option) <> ''
            ORDER BY response_option
        """
    else:
        option_sql = f"""
            SELECT DISTINCT response_option
            FROM survey_long
            WHERE question_id = '{qid_sql}'
              AND demo_id = '{did_sql}'
              AND demo_level = '{level_sql}'
              AND response_option IS NOT NULL
              AND TRIM(response_option) <> ''
            ORDER BY response_option
        """
    option_result = execute_query(option_sql)
    option_values = [
        str(r[0]).strip()
        for r in option_result.get("rows", [])
        if r and str(r[0]).strip()
    ] if "error" not in option_result else []

    response_filter_sql = ""
    selected_only = False
    if _is_binary_response_option_set(option_values):
        preferred = _pick_preferred_binary_response_option(option_values)
        if preferred:
            selected_only = True
            response_filter_sql = (
                f"AND LOWER(response_option) = LOWER('{_escape_sql_literal(preferred)}')"
            )

    if question_group:
        sql = f"""
            WITH base AS (
                SELECT
                    demo_id,
                    demo_level,
                    COALESCE(NULLIF(TRIM(question_level), ''), question_id) AS response_option,
                    AVG(response_value_num) AS response_value,
                    AVG(weighted_margin_of_error_num) AS weighted_moe,
                    AVG(unweighted_margin_of_error_num) AS unweighted_moe
                FROM survey_long
                WHERE question_group = '{qg_sql}'
                  AND response_option IS NOT NULL
                  AND TRIM(response_option) <> ''
                  AND (
                        (demo_id = '{did_sql}' AND demo_level = '{level_sql}')
                        OR (demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents')
                  )
                  {response_filter_sql}
                GROUP BY
                    demo_id,
                    demo_level,
                    COALESCE(NULLIF(TRIM(question_level), ''), question_id)
            ),
            pivoted AS (
                SELECT
                    response_option,
                    MAX(
                        CASE
                            WHEN demo_id = '{did_sql}' AND demo_level = '{level_sql}'
                            THEN 100.0 * response_value
                            ELSE NULL
                        END
                    ) AS target_percent,
                    MAX(
                        CASE
                            WHEN demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'
                            THEN 100.0 * response_value
                            ELSE NULL
                        END
                    ) AS national_percent,
                    MAX(
                        CASE
                            WHEN demo_id = '{did_sql}' AND demo_level = '{level_sql}'
                            THEN 100.0 * COALESCE(weighted_moe, unweighted_moe)
                            ELSE NULL
                        END
                    ) AS target_moe,
                    MAX(
                        CASE
                            WHEN demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'
                            THEN 100.0 * COALESCE(weighted_moe, unweighted_moe)
                            ELSE NULL
                        END
                    ) AS national_moe
                FROM base
                GROUP BY response_option
            )
            SELECT
                response_option,
                target_percent,
                national_percent,
                target_moe,
                national_moe
            FROM pivoted
            WHERE target_percent IS NOT NULL
              AND national_percent IS NOT NULL
        """
    else:
        sql = f"""
            WITH base AS (
                SELECT
                    demo_id,
                    demo_level,
                    response_option,
                    AVG(response_value_num) AS response_value,
                    AVG(weighted_margin_of_error_num) AS weighted_moe,
                    AVG(unweighted_margin_of_error_num) AS unweighted_moe
                FROM survey_long
                WHERE question_id = '{qid_sql}'
                  AND response_option IS NOT NULL
                  AND TRIM(response_option) <> ''
                  AND (
                        (demo_id = '{did_sql}' AND demo_level = '{level_sql}')
                        OR (demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents')
                  )
                  {response_filter_sql}
                GROUP BY demo_id, demo_level, response_option
            ),
            pivoted AS (
                SELECT
                    response_option,
                    MAX(
                        CASE
                            WHEN demo_id = '{did_sql}' AND demo_level = '{level_sql}'
                            THEN 100.0 * response_value
                            ELSE NULL
                        END
                    ) AS target_percent,
                    MAX(
                        CASE
                            WHEN demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'
                            THEN 100.0 * response_value
                            ELSE NULL
                        END
                    ) AS national_percent,
                    MAX(
                        CASE
                            WHEN demo_id = '{did_sql}' AND demo_level = '{level_sql}'
                            THEN 100.0 * COALESCE(weighted_moe, unweighted_moe)
                            ELSE NULL
                        END
                    ) AS target_moe,
                    MAX(
                        CASE
                            WHEN demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'
                            THEN 100.0 * COALESCE(weighted_moe, unweighted_moe)
                            ELSE NULL
                        END
                    ) AS national_moe
                FROM base
                GROUP BY response_option
            )
            SELECT
                response_option,
                target_percent,
                national_percent,
                target_moe,
                national_moe
            FROM pivoted
            WHERE target_percent IS NOT NULL
              AND national_percent IS NOT NULL
        """

    result = execute_query(sql)
    if "error" in result:
        return {
            "error": result["error"],
            "question_id": question_id,
            "question_group": question_group,
            "question_text": question_text,
            "demo_id": demo_id,
            "target_demo_level": target_demo_level,
        }

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    if not rows:
        return {
            "error": "No comparable rows returned for target-vs-national analysis.",
            "question_id": question_id,
            "question_group": question_group,
            "question_text": question_text,
            "demo_id": demo_id,
            "target_demo_level": target_demo_level,
        }

    i_opt = cols.index("response_option")
    i_target = cols.index("target_percent")
    i_nat = cols.index("national_percent")
    i_target_moe = cols.index("target_moe") if "target_moe" in cols else -1
    i_nat_moe = cols.index("national_moe") if "national_moe" in cols else -1

    stats: list[dict[str, Any]] = []
    for row in rows:
        option = str(row[i_opt]).strip()
        try:
            target_pct = float(row[i_target])
            national_pct = float(row[i_nat])
        except (TypeError, ValueError):
            continue
        target_moe: float | None = None
        national_moe: float | None = None
        if i_target_moe >= 0:
            try:
                raw = row[i_target_moe]
                target_moe = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                target_moe = None
        if i_nat_moe >= 0:
            try:
                raw = row[i_nat_moe]
                national_moe = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                national_moe = None

        delta = target_pct - national_pct
        abs_delta = abs(delta)
        combined_moe: float | None = None
        is_significant = False
        significance_ratio: float | None = None
        if target_moe is not None and national_moe is not None:
            combined_moe = (target_moe ** 2 + national_moe ** 2) ** 0.5
            if combined_moe > 0:
                significance_ratio = abs_delta / combined_moe
            is_significant = abs_delta > combined_moe

        stats.append(
            {
                "response_option": option,
                "target_percent": target_pct,
                "national_percent": national_pct,
                "delta_points": delta,
                "abs_delta_points": abs_delta,
                "target_moe_points": target_moe,
                "national_moe_points": national_moe,
                "combined_moe_points": combined_moe,
                "is_significant": is_significant,
                "significance_ratio": significance_ratio,
            }
        )

    if not stats:
        return {
            "error": "No numeric rows available for target-vs-national analysis.",
            "question_id": question_id,
            "question_group": question_group,
            "question_text": question_text,
            "demo_id": demo_id,
            "target_demo_level": target_demo_level,
        }

    candidates = list(stats)
    if operator == "higher":
        positive = [x for x in candidates if x["delta_points"] > 0]
        if positive:
            candidates = positive
        ranked = sorted(
            candidates,
            key=lambda x: (x["delta_points"], x["significance_ratio"] or -1.0),
            reverse=True,
        )
    elif operator == "lower":
        negative = [x for x in candidates if x["delta_points"] < 0]
        if negative:
            candidates = negative
        ranked = sorted(
            candidates,
            key=lambda x: (x["delta_points"], -x["significance_ratio"] if x["significance_ratio"] is not None else 1.0),
        )
    elif operator == "closest":
        ranked = sorted(
            candidates,
            key=lambda x: (x["abs_delta_points"], -(x["significance_ratio"] or 0.0)),
        )
    else:
        ranked = sorted(
            candidates,
            key=lambda x: (x["abs_delta_points"], x["significance_ratio"] or -1.0),
            reverse=True,
        )

    ranked = ranked[:top_n]
    if not ranked:
        return {
            "error": "No ranked differences were produced for this request.",
            "question_id": question_id,
            "question_group": question_group,
            "question_text": question_text,
            "demo_id": demo_id,
            "target_demo_level": target_demo_level,
        }

    operator_label = {
        "most_different": "Most Different",
        "closest": "Closest To National",
        "higher": "Most Above National",
        "lower": "Most Below National",
    }.get(operator, "Most Different")

    top = ranked[0]
    target_label = _short_demo_level(target_demo_level)
    question_ref = question_id or question_group or "Question"
    significant_count = sum(1 for x in ranked if x["is_significant"])
    missing_moe_count = sum(1 for x in ranked if x["combined_moe_points"] is None)

    lead_sign = "+" if top["delta_points"] >= 0 else ""
    lead_sig_txt = "significance unavailable"
    if top["combined_moe_points"] is not None:
        if top["is_significant"]:
            lead_sig_txt = "statistically significant at ~95%"
        else:
            lead_sig_txt = "not statistically significant at ~95%"

    narrative_lines = [
        f"**{question_ref} — {question_text}**",
        f"- {operator_label}: **{top['response_option']}** ({target_label} **{top['target_percent']:.2f}%** vs National **{top['national_percent']:.2f}%**, **{lead_sign}{top['delta_points']:.2f} pts**; {lead_sig_txt}).",
        f"- Scope: ranked by {'absolute gap vs national' if operator in {'most_different', 'closest'} else 'signed gap vs national'} for **{target_label}**.",
        f"- Significant differences among shown rows (~95%): **{significant_count}/{len(ranked)}**.",
    ]
    if missing_moe_count > 0:
        narrative_lines.append(
            f"- MOE missing for **{missing_moe_count}** row(s); significance for those rows is unavailable."
        )
    if selected_only:
        narrative_lines.append("- Binary response handling: ranked on the selected/yes-style response only.")
    narrative_lines.append("- Values shown are percentage-point differences vs national total respondents.")

    chart_values: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked, start=1):
        chart_values.append(
            {
                "rank": idx,
                "response_option": _truncate_label(row["response_option"], 42),
                "target_percent": round(row["target_percent"], 2),
                "national_percent": round(row["national_percent"], 2),
                "delta_points": round(row["delta_points"], 2),
                "abs_delta_points": round(row["abs_delta_points"], 2),
                "combined_moe_points": (
                    round(row["combined_moe_points"], 2)
                    if row["combined_moe_points"] is not None
                    else None
                ),
                "significant": "Significant" if row["is_significant"] else "Not significant",
            }
        )

    max_abs = max(abs(v["delta_points"]) for v in chart_values)
    max_abs = max(max_abs, 1.0)
    chart_spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {
            "text": f"{operator_label} Differences vs National",
            "subtitle": f"{target_label} · {question_ref}",
            "anchor": "start",
        },
        "data": {"values": chart_values},
        "height": {"step": 28},
        "layer": [
            {
                "mark": {"type": "bar", "cornerRadiusEnd": 4},
                "encoding": {
                    "color": {
                        "field": "significant",
                        "type": "nominal",
                        "scale": {
                            "domain": ["Significant", "Not significant"],
                            "range": [_BRAND_BLUE, _BRAND_BLUE_LIGHT],
                        },
                        "legend": {"orient": "bottom"},
                    },
                },
            },
            {
                "mark": {
                    "type": "rule",
                    "color": "#9ca3af",
                    "strokeDash": [3, 3],
                },
                "encoding": {"x": {"datum": 0}},
            },
            {
                "mark": {
                    "type": "text",
                    "fontSize": 11,
                    "fontWeight": 500,
                    "dx": 4,
                },
                "encoding": {
                    "text": {"field": "delta_points", "type": "quantitative", "format": "+.1f"},
                    "align": {
                        "condition": {"test": "datum.delta_points < 0", "value": "right"},
                        "value": "left",
                    },
                    "dx": {
                        "condition": {"test": "datum.delta_points < 0", "value": -4},
                        "value": 4,
                    },
                    "color": {"value": "#374151"},
                },
            },
        ],
        "encoding": {
            "y": {
                "field": "response_option",
                "type": "nominal",
                "sort": {"field": "rank", "order": "ascending"},
                "title": None,
                "axis": {"labelLimit": 400, "labelFontSize": 12},
            },
            "x": {
                "field": "delta_points",
                "type": "quantitative",
                "title": "Delta vs National (pts)",
                "scale": {"domain": [-(max_abs * 1.2), max_abs * 1.2]},
                "axis": {"grid": True, "format": "+.0f"},
            },
            "tooltip": [
                {"field": "response_option", "type": "nominal", "title": "Response"},
                {"field": "target_percent", "type": "quantitative", "title": f"{target_label} %", "format": ".1f"},
                {"field": "national_percent", "type": "quantitative", "title": "National %", "format": ".1f"},
                {"field": "delta_points", "type": "quantitative", "title": "Delta (pts)", "format": "+.1f"},
                {"field": "combined_moe_points", "type": "quantitative", "title": "Combined MOE (pts)", "format": ".2f"},
                {"field": "significant", "type": "nominal", "title": "Significance"},
            ],
        },
        "config": _CHART_CONFIG,
    }

    return {
        "analysis_type": "extreme_difference_by_baseline",
        "question_id": question_id,
        "question_group": question_group,
        "question_text": question_text,
        "demo_id": demo_id,
        "target_demo_level": target_demo_level,
        "baseline_demo_level": "TOTAL: Total respondents",
        "operator": operator,
        "operator_label": operator_label,
        "row_count": len(chart_values),
        "significant_count": significant_count,
        "top_rows": chart_values,
        "sql": sql.strip(),
        "insight_text": "\n".join(narrative_lines),
        "chart": chart_spec,
    }


def _build_broad_differences_by_demographic(
    *,
    demo_id: str,
    top_n: int = 10,
    active_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rank broad, topic-level differences across the dataset for one demographic split."""
    top_n = max(5, min(int(top_n), 20))
    did_sql = _escape_sql_literal(demo_id)

    level_sql = f"""
        SELECT DISTINCT demo_level
        FROM survey_long
        WHERE demo_id = '{did_sql}'
          AND demo_level IS NOT NULL
          AND TRIM(demo_level) <> ''
        ORDER BY demo_level
    """
    level_result = execute_query(level_sql)
    if "error" in level_result:
        return {"error": level_result["error"], "demo_id": demo_id}

    demo_levels = [str(r[0]).strip() for r in level_result.get("rows", []) if r and str(r[0]).strip()]
    if len(demo_levels) < 2:
        return {
            "error": f"Broad difference scan requires at least two groups for {demo_id}.",
            "demo_id": demo_id,
        }

    response_option_exclusions = [
        "maximum",
        "minimum",
        "mean",
        "median",
        "stddev",
        "standard deviation",
        "first quartile",
        "third quartile",
        "interquartile range",
        "iqr",
        "mode",
        "count",
        "sum",
    ]
    exclusion_sql = ", ".join(f"'{_escape_sql_literal(v)}'" for v in response_option_exclusions)

    home_ownership_response_exclusion_sql = ""
    if demo_id == "TOTAL: Home Ownership":
        home_ownership_response_exclusion_sql = (
            "AND LOWER(TRIM(response_option)) NOT IN "
            "('home owner', 'homeowner', 'renter', 'renters')"
        )

    sql = f"""
        WITH base AS (
            SELECT
                question_id,
                question_group,
                MIN(question_text) AS question_text,
                COALESCE(NULLIF(TRIM(question_level), ''), question_id) AS item_label,
                response_option,
                demo_level,
                AVG(response_value_num) AS response_value,
                AVG(weighted_margin_of_error_num) AS weighted_moe,
                AVG(unweighted_margin_of_error_num) AS unweighted_moe
            FROM survey_long
            WHERE demo_id = '{did_sql}'
              AND question_id NOT LIKE 'IKEAdem%'
              AND response_value_num IS NOT NULL
              AND response_value_num BETWEEN 0 AND 1
              AND response_option IS NOT NULL
              AND TRIM(response_option) <> ''
              AND regexp_matches(response_option, '[A-Za-z]')
              AND LOWER(TRIM(response_option)) NOT IN ({exclusion_sql})
              AND NOT regexp_matches(TRIM(response_option), '^[0-9]+\\s*[-–]\\s*[0-9]+$')
              AND NOT regexp_matches(TRIM(response_option), '^[0-9]+(\\.[0-9]+)?\\+?$')
              {home_ownership_response_exclusion_sql}
            GROUP BY
                question_id,
                question_group,
                COALESCE(NULLIF(TRIM(question_level), ''), question_id),
                response_option,
                demo_level
        ),
        spread AS (
            SELECT
                question_id,
                question_group,
                question_text,
                item_label,
                response_option,
                MAX(response_value) - MIN(response_value) AS gap_raw,
                ARG_MAX(demo_level, response_value) AS high_group,
                MAX(response_value) AS high_value,
                ARG_MAX(100.0 * COALESCE(weighted_moe, unweighted_moe), response_value) AS high_moe,
                ARG_MIN(demo_level, response_value) AS low_group,
                MIN(response_value) AS low_value,
                ARG_MIN(100.0 * COALESCE(weighted_moe, unweighted_moe), response_value) AS low_moe,
                COUNT(*) AS group_count
            FROM base
            GROUP BY question_id, question_group, question_text, item_label, response_option
            HAVING COUNT(*) >= 2
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY question_id
                    ORDER BY
                        gap_raw DESC,
                        CASE
                            WHEN LOWER(TRIM(response_option)) IN ('selected', 'yes', 'true', 'own') THEN 0
                            ELSE 1
                        END ASC,
                        response_option
                ) AS rn
            FROM spread
        )
        SELECT
            question_id,
            question_group,
            question_text,
            item_label,
            response_option,
            low_group,
            100.0 * low_value AS low_percent,
            high_group,
            100.0 * high_value AS high_percent,
            100.0 * gap_raw AS gap_points,
            CASE
                WHEN low_moe IS NOT NULL AND high_moe IS NOT NULL
                    THEN SQRT(low_moe * low_moe + high_moe * high_moe)
                ELSE NULL
            END AS combined_moe_points
        FROM ranked
        WHERE rn = 1
        ORDER BY gap_raw DESC, question_id
        LIMIT {top_n * 4}
    """

    result = execute_query(sql)
    if "error" in result:
        return {"error": result["error"], "demo_id": demo_id}

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    if not rows:
        return {
            "error": f"No broad difference rows were returned for {demo_id}.",
            "demo_id": demo_id,
        }

    i_qid = cols.index("question_id")
    i_qg = cols.index("question_group") if "question_group" in cols else -1
    i_qtext = cols.index("question_text")
    i_item = cols.index("item_label")
    i_opt = cols.index("response_option")
    i_low_group = cols.index("low_group")
    i_low_pct = cols.index("low_percent")
    i_high_group = cols.index("high_group")
    i_high_pct = cols.index("high_percent")
    i_gap = cols.index("gap_points")
    i_moe = cols.index("combined_moe_points") if "combined_moe_points" in cols else -1

    findings: list[dict[str, Any]] = []
    for row in rows:
        try:
            low_percent = float(row[i_low_pct])
            high_percent = float(row[i_high_pct])
            gap_points = float(row[i_gap])
        except (TypeError, ValueError):
            continue
        if gap_points <= 0:
            continue

        combined_moe: float | None = None
        if i_moe >= 0:
            try:
                raw_moe = row[i_moe]
                combined_moe = float(raw_moe) if raw_moe is not None else None
            except (TypeError, ValueError):
                combined_moe = None

        question_id = str(row[i_qid]).strip()
        question_group = str(row[i_qg]).strip() if i_qg >= 0 and row[i_qg] is not None else ""
        question_text = str(row[i_qtext]).strip()
        item_label = str(row[i_item]).strip()
        response_option = str(row[i_opt]).strip()
        low_group = _short_demo_level(str(row[i_low_group]).strip())
        high_group = _short_demo_level(str(row[i_high_group]).strip())

        topic = item_label if item_label and item_label != question_id else question_text
        if not topic:
            topic = question_text or question_id

        findings.append(
            {
                "question_id": question_id,
                "question_group": question_group or None,
                "question_text": question_text,
                "topic": topic,
                "response_option": response_option,
                "low_group": low_group,
                "low_percent": low_percent,
                "high_group": high_group,
                "high_percent": high_percent,
                "gap_points": gap_points,
                "combined_moe_points": combined_moe,
                "is_significant": (
                    combined_moe is not None and gap_points > combined_moe
                ),
            }
        )

    findings.sort(key=lambda x: x["gap_points"], reverse=True)
    top_findings = findings[:top_n]
    if not top_findings:
        return {
            "error": f"No usable broad-difference findings were available for {demo_id}.",
            "demo_id": demo_id,
        }

    significant_count = sum(1 for f in top_findings if f["is_significant"])
    missing_moe_count = sum(1 for f in top_findings if f["combined_moe_points"] is None)

    lead = top_findings[0]
    lead_sig_text = "significance unavailable"
    if lead["combined_moe_points"] is not None:
        if lead["is_significant"]:
            lead_sig_text = "statistically significant at ~95%"
        else:
            lead_sig_text = "not statistically significant at ~95%"

    demo_label = _short_demo_label(demo_id)
    narrative_lines = [
        f"**Largest differences by {demo_label} across survey topics**",
        f"- Ranked by the largest between-group gap for each question (top **{len(top_findings)}** shown).",
        f"- Biggest gap: **{lead['topic']}** ({lead['response_option']}) from **{lead['low_group']} ({lead['low_percent']:.2f}%)** to "
        f"**{lead['high_group']} ({lead['high_percent']:.2f}%)** (**{lead['gap_points']:.2f} pts**, {lead_sig_text}).",
        f"- Significant differences among shown rows (~95%): **{significant_count}/{len(top_findings)}**.",
    ]
    if missing_moe_count > 0:
        narrative_lines.append(
            f"- MOE missing for **{missing_moe_count}** row(s); significance is unavailable for those rows."
        )

    normalized_filters = _normalize_active_filters(active_filters)
    if normalized_filters:
        narrative_lines.append(
            "- Note: active filters are ignored for this broad scan because the source tables do not provide multi-demographic intersections."
        )

    narrative_lines.append(
        f"- Refine with: `Compare <topic> by {demo_label}` (for example: `Compare storage furniture by {demo_label}`)."
    )

    chart_values = [
        {
            "topic": _truncate_label(str(row["topic"]), 56),
            "gap_points": round(float(row["gap_points"]), 2),
            "significant": "Significant" if row["is_significant"] else "Not significant",
            "response_option": _truncate_label(str(row["response_option"]), 28),
            "low_group": str(row["low_group"]),
            "low_percent": round(float(row["low_percent"]), 2),
            "high_group": str(row["high_group"]),
            "high_percent": round(float(row["high_percent"]), 2),
            "combined_moe_points": (
                round(float(row["combined_moe_points"]), 2)
                if row["combined_moe_points"] is not None
                else None
            ),
            "question_id": str(row["question_id"]),
            "question_text": str(row["question_text"]),
        }
        for row in top_findings
    ]

    chart_spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {
            "text": f"Largest {demo_label} Differences Across Survey Topics",
            "subtitle": f"Top {len(chart_values)} question-level signals",
            "anchor": "start",
        },
        "data": {"values": chart_values},
        "mark": {"type": "bar", "cornerRadiusEnd": 4},
        "encoding": {
            "y": {
                "field": "topic",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 400},
            },
            "x": {
                "field": "gap_points",
                "type": "quantitative",
                "title": "Gap across groups (percentage points)",
                "axis": {"format": ".1f", "grid": True},
            },
            "color": {
                "field": "significant",
                "type": "nominal",
                "scale": {
                    "domain": ["Significant", "Not significant"],
                    "range": [_BRAND_BLUE, "#9ca3af"],
                },
                "legend": {"orient": "top-right"},
            },
            "tooltip": [
                {"field": "topic", "type": "nominal", "title": "Topic"},
                {"field": "response_option", "type": "nominal", "title": "Response option"},
                {"field": "low_group", "type": "nominal", "title": "Low group"},
                {"field": "low_percent", "type": "quantitative", "title": "Low %", "format": ".2f"},
                {"field": "high_group", "type": "nominal", "title": "High group"},
                {"field": "high_percent", "type": "quantitative", "title": "High %", "format": ".2f"},
                {"field": "gap_points", "type": "quantitative", "title": "Gap (pts)", "format": ".2f"},
                {"field": "combined_moe_points", "type": "quantitative", "title": "Combined MOE (pts)", "format": ".2f"},
                {"field": "significant", "type": "nominal", "title": "Significance"},
                {"field": "question_id", "type": "nominal", "title": "Question ID"},
            ],
        },
        "config": _CHART_CONFIG,
    }

    return {
        "analysis_type": "broad_demo_differences",
        "demo_id": demo_id,
        "demo_level_count": len(demo_levels),
        "row_count": len(top_findings),
        "significant_count": significant_count,
        "top_findings": [
            {
                "question_id": row["question_id"],
                "question_group": row["question_group"],
                "question_text": row["question_text"],
                "topic": row["topic"],
                "response_option": row["response_option"],
                "low_group": row["low_group"],
                "low_percent": round(float(row["low_percent"]), 2),
                "high_group": row["high_group"],
                "high_percent": round(float(row["high_percent"]), 2),
                "gap_points": round(float(row["gap_points"]), 2),
                "combined_moe_points": (
                    round(float(row["combined_moe_points"]), 2)
                    if row["combined_moe_points"] is not None
                    else None
                ),
                "is_significant": bool(row["is_significant"]),
            }
            for row in top_findings
        ],
        "sql": sql.strip(),
        "insight_text": "\n".join(narrative_lines),
        "chart": chart_spec,
    }


_ANSWERABILITY_GENERIC_TOKENS = {
    "home",
    "house",
    "housing",
    "room",
    "area",
    "respondent",
    "people",
    "person",
    "question",
    "survey",
    "total",
    "average",
    "common",
    "most",
    "least",
    "top",
    "share",
    "percent",
    "percentage",
    "number",
    "many",
    "much",
    "differ",
    "difference",
    "compare",
    "versu",
    "versus",
    "live",
    "living",
    "use",
    "used",
    "have",
    "has",
    "own",
    "owned",
}


_ROOM_LABELS = {
    "home": "home",
    "main": "main area",
    "living": "living room",
    "bedroom": "bedroom",
    "kitchen": "kitchen",
    "dining": "dining room",
    "bathroom": "bathroom",
}


def _room_label(room_key: str) -> str:
    return _ROOM_LABELS.get(room_key, room_key.replace("_", " ").strip())


def _extract_answerability_anchor_tokens(text: str) -> list[str]:
    anchors: list[str] = []
    for token in _tokenize_for_match(text):
        if len(token) < 3:
            continue
        if token in _QUESTION_STOPWORDS:
            continue
        if token in _ANSWERABILITY_GENERIC_TOKENS:
            continue
        anchors.append(token)
    return sorted(set(anchors))


def _build_close_question_suggestions(
    matches: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for row in matches:
        qid = str(row.get("question_id", "")).strip()
        qgroup = str(row.get("question_group", "")).strip()
        qtext = str(row.get("question_text", "")).strip()
        if not qid or not qtext:
            continue
        uniq = qgroup or qid
        if uniq in seen_keys:
            continue
        seen_keys.add(uniq)
        suggestions.append(
            {
                "question_id": qid,
                "question_group": qgroup or None,
                "question_text": qtext,
            }
        )
        if len(suggestions) >= limit:
            break

    return suggestions


def _build_not_answerable_result(
    *,
    query: str,
    reason: str,
    reason_code: str,
    alternatives: list[dict[str, Any]] | None = None,
    anchor_tokens: list[str] | None = None,
    matched_anchor_tokens: list[str] | None = None,
    top_match_score: float | None = None,
    second_match_score: float | None = None,
) -> dict[str, Any]:
    alts = alternatives or []
    anchors = anchor_tokens or []
    matched = matched_anchor_tokens or []

    lines = [
        "I could not find a reliable, answerable match for that request in this survey data.",
        f"- Reason: {reason}",
    ]
    if anchors:
        lines.append(
            f"- Key terms matched in top candidates: {len(matched)}/{len(anchors)} "
            f"({', '.join(matched[:5]) if matched else 'none'})."
        )
    if alts:
        lines.append("- Closest answerable questions in this dataset:")
        for alt in alts:
            qid = str(alt.get("question_id", "")).strip()
            qtext = str(alt.get("question_text", "")).strip()
            if qid and qtext:
                lines.append(f"- {qid} — {qtext}")
    else:
        lines.append("- Try restating with one in-dataset topic such as rooms, furniture, purchases, or demographics.")

    return {
        "analysis_type": "not_answerable",
        "answerable": False,
        "query": query,
        "reason_code": reason_code,
        "reason": reason,
        "anchor_tokens": anchors or None,
        "matched_anchor_tokens": matched or None,
        "top_match_score": round(float(top_match_score), 4) if top_match_score is not None else None,
        "second_match_score": round(float(second_match_score), 4) if second_match_score is not None else None,
        "alternatives": alts or None,
        "insight_text": "\n".join(lines),
    }


def build_unanswerable_result_for_user_query(
    user_message: str,
    precomputed_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return an abstain payload when a query is likely not answerable from this dataset."""
    if not user_message:
        return None

    cleaned = user_message.strip()
    if cleaned.startswith("[Active filters:"):
        parts = cleaned.split("\n\n", 1)
        cleaned = parts[1].strip() if len(parts) > 1 else cleaned
    if not cleaned:
        return None

    q = cleaned.lower()

    intent = _detect_room_matrix_intent(cleaned)
    room_hint = _extract_room_hint(cleaned)
    room_map = _ROOM_INTENT_GROUPS.get(intent, {}) if intent else {}
    if room_hint and room_map and room_hint not in room_map:
        if not (room_hint in {"home", "living"} and "main" in room_map):
            if len(room_map) > 1:
                ordered_rooms = [rk for rk in room_map.keys() if rk not in {"home"}] or list(room_map.keys())
                alternatives: list[dict[str, Any]] = []
                seen_groups: set[str] = set()
                for room_key in ordered_rooms:
                    qgroup = str(room_map.get(room_key, "")).strip()
                    if not qgroup or qgroup in seen_groups:
                        continue
                    seen_groups.add(qgroup)
                    alternatives.append(
                        {
                            "question_id": qgroup,
                            "question_group": qgroup,
                            "question_text": _resolve_group_question_text(qgroup),
                        }
                    )
                    if len(alternatives) >= 3:
                        break
                available = ", ".join(_room_label(rk) for rk in ordered_rooms)
                reason = (
                    f"No '{_room_label(room_hint)}' split is available for this question family. "
                    f"Available room splits: {available}."
                )
                return _build_not_answerable_result(
                    query=cleaned,
                    reason=reason,
                    reason_code="unsupported_room_scope",
                    alternatives=alternatives,
                )

    # Preserve known deterministic paths; this guard is only for uncertain retrieval.
    if _resolve_room_intent_group(cleaned):
        return None
    if _resolve_broad_demo_difference_request(cleaned):
        return None
    if _is_age_homeownership_intent(cleaned):
        return None
    if _is_age_demographic_intent(cleaned):
        return None
    if _is_home_size_intent(cleaned):
        return None
    if _is_housing_type_breakout_intent(cleaned):
        return None

    has_income = "income" in q
    has_children = (
        ("children" in q and "household" in q)
        or "kids" in q
        or "with children" in q
        or "people with children" in q
        or "have children" in q
    )
    if has_income and has_children:
        return None

    has_compare_intent = _has_comparison_intent(q)
    if has_compare_intent and (" vs " in q or " versus " in q):
        for_idx = q.rfind(" for ")
        if for_idx != -1:
            segment_expr = cleaned[for_idx + 5 :].strip(" ?.")
            if _resolve_demo_id_from_text(segment_expr):
                return None
        if _resolve_segment_first_comparison_request(cleaned):
            return None

    by_idx = q.rfind(" by ")
    if by_idx != -1:
        by_tail = cleaned[by_idx + 4 :].strip(" ?.")
        if _resolve_demo_id_from_text(by_tail):
            return None

    matches = precomputed_matches
    if matches is None:
        try:
            search_result = search_questions(cleaned)
        except Exception:
            return None
        if not isinstance(search_result, dict):
            return None
        if "error" in search_result:
            return None
        matches = search_result.get("results", [])

    if not matches:
        return _build_not_answerable_result(
            query=cleaned,
            reason="No close question match was found in the catalog.",
            reason_code="no_close_match",
        )

    anchors = _extract_answerability_anchor_tokens(cleaned)
    if not anchors:
        return None

    top_window = matches[:12]
    candidate_tokens: set[str] = set()
    for row in top_window:
        candidate_tokens.update(
            _tokenize_for_match(
                " ".join(
                    [
                        str(row.get("question_text", "")),
                        str(row.get("question_group", "")),
                        str(row.get("question_id", "")),
                    ]
                )
            )
        )
    matched_anchors = sorted(
        {
            token
            for token in anchors
            if token in candidate_tokens
        }
    )
    coverage = (len(matched_anchors) / len(anchors)) if anchors else 1.0

    try:
        top_score = float(matches[0].get("match_score", 0.0) or 0.0)
    except Exception:
        top_score = 0.0
    try:
        second_score = float(matches[1].get("match_score", 0.0) or 0.0) if len(matches) > 1 else 0.0
    except Exception:
        second_score = 0.0

    low_confidence = False
    if not matched_anchors:
        if len(anchors) >= 2:
            low_confidence = True
        elif len(anchors) == 1 and top_score < 0.85:
            low_confidence = True
    elif coverage < 0.34 and top_score < 0.65:
        low_confidence = True

    if not low_confidence:
        return None

    if not matched_anchors:
        reason = (
            "The key topic terms in your question do not appear in the closest catalog matches."
        )
    else:
        reason = (
            f"Only {len(matched_anchors)}/{len(anchors)} key terms matched close candidates, "
            "so the retrieval is too weak for a reliable answer."
        )

    alternatives = _build_close_question_suggestions(matches, limit=3)
    return _build_not_answerable_result(
        query=cleaned,
        reason=reason,
        reason_code="low_retrieval_confidence",
        alternatives=alternatives,
        anchor_tokens=anchors,
        matched_anchor_tokens=matched_anchors,
        top_match_score=top_score,
        second_match_score=second_score,
    )


def _build_quick_insight(
    question: str,
    demo_level: str | None = None,
    top_n: int = 8,
    active_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fast path: search question -> fetch top response options -> create chart spec."""
    extreme_request = _resolve_extreme_difference_request(question)
    if extreme_request:
        extreme = _build_extreme_difference_by_demographic(
            question=str(extreme_request["question"]),
            demo_id=str(extreme_request["demo_id"]),
            target_demo_level=str(extreme_request["target_demo_level"]),
            operator=str(extreme_request.get("operator", "most_different")),
            top_n=min(top_n, 10),
        )
        if "error" not in extreme:
            return extreme

    broad_request = _resolve_broad_demo_difference_request(question)
    if broad_request:
        broad = _build_broad_differences_by_demographic(
            demo_id=str(broad_request["demo_id"]),
            top_n=min(top_n, 12),
            active_filters=active_filters,
        )
        if "error" not in broad:
            return broad

    segment_first = _resolve_segment_first_comparison_request(question)
    if segment_first:
        demo_id = str(segment_first["demo_id"])
        subject_question = str(segment_first["subject_question"])
        subject_room_route = _resolve_room_intent_group(subject_question)
        if subject_room_route:
            matrix = _build_question_group_by_demographic(
                question=str(subject_room_route["question_text"]),
                demo_id=demo_id,
                top_n_items=24,
                item_keywords=subject_room_route.get("item_keywords"),
            )
            if "error" not in matrix:
                return matrix
            cross = _build_question_by_demographic(
                question=str(subject_room_route["question_text"]),
                demo_id=demo_id,
                top_n_options=5,
            )
            if "error" not in cross:
                return cross

        cross = _build_question_by_demographic(
            question=subject_question,
            demo_id=demo_id,
            top_n_options=5,
        )
        if "error" not in cross:
            return cross

    room_route = _resolve_room_intent_group(question)
    if room_route:
        return _build_top_selected_for_question_group(
            question_group=str(room_route["question_group"]),
            question_text=str(room_route["question_text"]),
            room_hint=str(room_route["room_hint"]),
            top_n=min(top_n, 12),
            analysis_type=str(room_route.get("analysis_type", "top_selected_items")),
            title_prefix=str(room_route.get("title_prefix", "Top Selected Items")),
            item_label=str(room_route.get("item_label", "Item")),
            summary_noun=str(room_route.get("summary_noun", "item")),
            item_keywords=room_route.get("item_keywords"),
            active_filters=active_filters,
        )

    if _is_age_homeownership_intent(question):
        return _build_demographic_breakout(
            demo_ids=["TOTAL: Age", "TOTAL: Home Ownership"],
            top_n=8,
        )
    if _is_age_demographic_intent(question):
        return _build_demographic_breakout(demo_ids=["TOTAL: Age"], top_n=8)

    if _is_housing_type_breakout_intent(question):
        return _build_demographic_breakout(demo_ids=["TOTAL: Housing Type"], top_n=8)

    search_result = search_questions(question)
    if "error" in search_result:
        return {"error": search_result["error"]}

    matches = search_result.get("results", [])
    unanswerable = build_unanswerable_result_for_user_query(
        question,
        precomputed_matches=matches,
    )
    if unanswerable:
        return unanswerable
    if not matches:
        return {"error": "No matching questions found."}

    best: dict[str, Any] | None = None
    if _is_home_size_intent(question):
        best = _pick_home_size_question_match(matches)

    if best is None:
        best, routed_demo_id = _select_question_match_and_route(question, matches)
        if routed_demo_id:
            return _build_demographic_breakout(demo_ids=[routed_demo_id], top_n=8)

    best = best or matches[0]
    question_id = str(best.get("question_id", "")).strip()
    question_text = str(best.get("question_text", "")).strip()
    if not question_id:
        return {"error": "Matched question is missing question_id."}

    top_n = max(3, min(int(top_n), 20))
    qid_sql = _escape_sql_literal(question_id)

    demo_sql, applied_filters = _resolve_demo_clause(
        demo_level=demo_level,
        active_filters=active_filters,
    )
    demo_slice = applied_filters[0]["demo_level"] if applied_filters else (
        demo_level.strip() if demo_level and demo_level.strip() else "TOTAL: Total respondents"
    )

    sql = f"""
        SELECT
            response_option,
            AVG(response_value_num) AS response_value
        FROM survey_long
        WHERE question_id = '{qid_sql}'
          AND {demo_sql}
          AND response_option IS NOT NULL
          AND TRIM(response_option) <> ''
          AND response_value_num IS NOT NULL
        GROUP BY response_option
        ORDER BY response_value DESC
        LIMIT {top_n}
    """

    data_result = execute_query(sql)
    if "error" in data_result:
        return {
            "error": data_result["error"],
            "question_id": question_id,
            "question_text": question_text,
            "sql": sql.strip(),
        }

    rows = data_result.get("rows", [])
    columns = data_result.get("columns", [])
    if not rows or "response_option" not in columns:
        return {
            "error": "No response rows returned for quick insight query.",
            "question_id": question_id,
            "question_text": question_text,
            "sql": sql.strip(),
        }

    idx_option = columns.index("response_option")
    idx_value = columns.index("response_value")

    chart_values: list[dict[str, Any]] = []
    for row in rows:
        option = str(row[idx_option])
        raw_val = row[idx_value]
        try:
            pct = float(raw_val) * 100.0
        except (TypeError, ValueError):
            continue
        chart_values.append({
            "option": _truncate_label(option),
            "percent": round(pct, 2),
        })

    if not chart_values:
        return {
            "error": "No numeric values available for charting.",
            "question_id": question_id,
            "question_text": question_text,
            "sql": sql.strip(),
        }

    # Short readable title: use question text, fall back to question_id
    chart_title = question_text if len(question_text) <= 80 else question_text[:77] + "\u2026"

    max_pct = max(v["percent"] for v in chart_values)

    chart_spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {
            "text": chart_title,
            "subtitle": f"{question_id} · Slice: {demo_slice}",
            "anchor": "start",
        },
        "data": {"values": chart_values},
        "height": {"step": 30},
        "encoding": {
            "y": {
                "field": "option",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 350, "labelFontSize": 12},
            },
            "x": {
                "field": "percent",
                "type": "quantitative",
                "title": "% of respondents",
                "axis": {"format": ".0f", "grid": True},
                "scale": {"domain": [0, max_pct * 1.15]},
            },
            "tooltip": [
                {"field": "option", "type": "nominal", "title": "Response"},
                {"field": "percent", "type": "quantitative", "title": "%", "format": ".1f"},
            ],
        },
        "layer": [
            {
                "mark": {
                    "type": "bar",
                    "cornerRadiusEnd": 4,
                    "color": _BRAND_BLUE,
                },
            },
            {
                "mark": {
                    "type": "text",
                    "align": "left",
                    "dx": 4,
                    "fontSize": 11,
                    "fontWeight": 500,
                    "color": "#374151",
                },
                "encoding": {
                    "text": {"field": "percent", "type": "quantitative", "format": ".1f"},
                },
            },
        ],
        "config": _CHART_CONFIG,
    }

    top1 = chart_values[0]
    top2 = chart_values[1] if len(chart_values) > 1 else None
    top3 = chart_values[:3]
    top3_share = sum(x["percent"] for x in top3)
    bottom = chart_values[-1]
    spread = top1["percent"] - bottom["percent"]
    summary_lines = [
        f"**{question_id} — {question_text}**",
        f"- Top response: **{top1['option']}** at **{top1['percent']:.2f}%**.",
    ]
    if top2:
        summary_lines.append(
            f"- Runner-up: **{top2['option']}** at **{top2['percent']:.2f}%** "
            f"({top1['percent'] - top2['percent']:.2f} pts behind)."
        )
    summary_lines.append(
        f"- Concentration: top {len(top3)} options sum to **{top3_share:.2f}%**."
    )
    summary_lines.append(
        f"- Spread between highest and lowest shown options: **{spread:.2f} pts**."
    )
    if applied_filters:
        af = applied_filters[0]
        summary_lines.append(
            f"- Active filter applied: **{af['demo_id']} = {af['demo_level']}**."
        )
    summary_lines.append("- Values shown are percentages of respondents.")
    insight_text = "\n".join(summary_lines)

    return {
        "question_id": question_id,
        "question_text": question_text,
        "sql": sql.strip(),
        "row_count": len(chart_values),
        "top_rows": chart_values,
        "applied_filters": applied_filters,
        "insight_text": insight_text,
        "chart": chart_spec,
    }


def _build_demographic_breakout(demo_ids: list[str], top_n: int = 8) -> dict[str, Any]:
    demo_ids = [d.strip() for d in demo_ids if d and d.strip()]
    if not demo_ids:
        return {"error": "No demographic dimensions supplied."}

    top_n = max(3, min(int(top_n), 12))
    dimensions: list[dict[str, Any]] = []

    for demo_id in demo_ids:
        demo_sql = _escape_sql_literal(demo_id)
        sql = f"""
            WITH level_weight AS (
                SELECT
                    demo_level,
                    question_id,
                    MAX(weighted_n_num) AS weighted_n
                FROM survey_long
                WHERE demo_id = '{demo_sql}'
                  AND demo_level IS NOT NULL
                  AND TRIM(demo_level) <> ''
                GROUP BY demo_level, question_id
            ),
            level_stat AS (
                SELECT
                    demo_level,
                    quantile_cont(weighted_n, 0.5) AS n_est
                FROM level_weight
                WHERE weighted_n IS NOT NULL
                GROUP BY demo_level
            )
            SELECT
                demo_level,
                n_est,
                100.0 * n_est / NULLIF(SUM(n_est) OVER (), 0) AS pct
            FROM level_stat
            ORDER BY pct DESC
            LIMIT {top_n}
        """
        result = execute_query(sql)
        if "error" in result:
            dimensions.append(
                {
                    "demo_id": demo_id,
                    "label": _short_demo_label(demo_id),
                    "levels": [],
                    "error": result["error"],
                }
            )
            continue

        rows = result.get("rows", [])
        cols = result.get("columns", [])
        if not rows or "demo_level" not in cols or "pct" not in cols:
            dimensions.append(
                {
                    "demo_id": demo_id,
                    "label": _short_demo_label(demo_id),
                    "levels": [],
                    "error": "No levels returned.",
                }
            )
            continue

        i_level = cols.index("demo_level")
        i_n = cols.index("n_est")
        i_pct = cols.index("pct")

        levels = []
        for row in rows:
            try:
                pct = float(row[i_pct])
            except (TypeError, ValueError):
                continue
            n_est = row[i_n]
            try:
                n_est_val = float(n_est) if n_est is not None else None
            except (TypeError, ValueError):
                n_est_val = None
            level = str(row[i_level])
            levels.append(
                {
                    "level": level,
                    "label": _short_demo_level(level),
                    "percent": round(pct, 2),
                    "n_est": n_est_val,
                }
            )

        dimensions.append(
            {
                "demo_id": demo_id,
                "label": _short_demo_label(demo_id),
                "levels": levels,
            }
        )

    chart_values: list[dict[str, Any]] = []
    narrative_lines = [
        "**Demographic breakout (estimated from weighted base medians)**"
    ]

    for dim in dimensions:
        levels = dim.get("levels", [])
        label = dim.get("label", dim.get("demo_id", "Demographic"))
        if not levels:
            err = dim.get("error", "No data returned.")
            narrative_lines.append(f"- {label}: no usable levels ({err}).")
            continue

        if len(levels) == 1:
            only = levels[0]
            narrative_lines.append(
                f"- {label}: dataset only contains one level, **{only['label']}** "
                f"({only['percent']:.2f}%). A full split is not available."
            )
            continue

        top = levels[0]
        second = levels[1] if len(levels) > 1 else None
        top2_sum = sum(x["percent"] for x in levels[:2])
        min_level = min(levels, key=lambda x: x["percent"])
        narrative_lines.append(
            f"- {label}: largest segment is **{top['label']}** at **{top['percent']:.2f}%**."
        )
        if second:
            narrative_lines.append(
                f"- {label}: second is **{second['label']}** at **{second['percent']:.2f}%** "
                f"({top['percent'] - second['percent']:.2f} pts gap)."
            )
        narrative_lines.append(
            f"- {label}: top two segments account for **{top2_sum:.2f}%**; "
            f"smallest shown is **{min_level['label']}** at **{min_level['percent']:.2f}%**."
        )
        for lvl in levels:
            chart_values.append(
                {
                    "dimension": label,
                    "level": _truncate_label(lvl["label"]),
                    "percent": lvl["percent"],
                }
            )

    chart_spec: dict[str, Any] | None = None
    if chart_values:
        # Assign a distinct colour to each dimension for multi-facet clarity.
        dim_names = list(dict.fromkeys(v["dimension"] for v in chart_values))
        dim_colors = _CHART_CONFIG["range"]["category"][: len(dim_names)]

        chart_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": {
                "text": "Demographic Breakout",
                "subtitle": "Estimated share of respondents",
                "anchor": "start",
            },
            "data": {"values": chart_values},
            "facet": {
                "row": {
                    "field": "dimension",
                    "type": "nominal",
                    "header": {
                        "title": None,
                        "labelFontSize": 13,
                        "labelFontWeight": 600,
                        "labelColor": "#1f2937",
                        "labelPadding": 8,
                    },
                },
            },
            "spec": {
                "height": {"step": 28},
                "layer": [
                    {
                        "mark": {"type": "bar", "cornerRadiusEnd": 4},
                        "encoding": {
                            "color": {
                                "field": "dimension",
                                "type": "nominal",
                                "legend": None,
                                "scale": {
                                    "domain": dim_names,
                                    "range": dim_colors,
                                },
                            },
                        },
                    },
                    {
                        "mark": {
                            "type": "text",
                            "align": "left",
                            "dx": 4,
                            "fontSize": 11,
                            "color": "#374151",
                        },
                        "encoding": {
                            "text": {
                                "field": "percent",
                                "type": "quantitative",
                                "format": ".1f",
                            },
                        },
                    },
                ],
                "encoding": {
                    "y": {
                        "field": "level",
                        "type": "nominal",
                        "sort": "-x",
                        "title": None,
                        "axis": {"labelLimit": 320, "labelFontSize": 12},
                    },
                    "x": {
                        "field": "percent",
                        "type": "quantitative",
                        "title": "% of respondents",
                        "axis": {"format": ".0f", "grid": True},
                    },
                    "tooltip": [
                        {"field": "dimension", "type": "nominal", "title": "Dimension"},
                        {"field": "level", "type": "nominal", "title": "Level"},
                        {"field": "percent", "type": "quantitative", "title": "%", "format": ".1f"},
                    ],
                },
            },
            "config": _CHART_CONFIG,
        }

    narrative_lines.append(
        "- Method note: shares are estimated using median `weighted_n` across question IDs for each demographic level."
    )

    result: dict[str, Any] = {
        "analysis_type": "demographic_breakout",
        "dimensions": dimensions,
        "insight_text": "\n".join(narrative_lines),
    }
    if chart_spec:
        result["chart"] = chart_spec
    return result


def _build_question_by_demographic(
    question: str, demo_id: str, top_n_options: int = 5
) -> dict[str, Any]:
    search_result = search_questions(question)
    if "error" in search_result:
        return {"error": search_result["error"]}

    matches = search_result.get("results", [])
    if not matches:
        return {"error": "No matching questions found for cross-tab."}

    best, _ = _select_question_match_and_route(question, matches)
    best = best or matches[0]
    question_id = str(best.get("question_id", "")).strip()
    question_text = str(best.get("question_text", "")).strip()
    if not question_id:
        return {"error": "Matched question is missing question_id."}

    top_n_options = max(2, min(int(top_n_options), 8))
    qid_sql = _escape_sql_literal(question_id)
    did_sql = _escape_sql_literal(demo_id)
    selected_only = False
    response_filter_sql = ""

    option_sql = f"""
        SELECT DISTINCT response_option
        FROM survey_long
        WHERE question_id = '{qid_sql}'
          AND demo_id = '{did_sql}'
          AND response_option IS NOT NULL
          AND TRIM(response_option) <> ''
        ORDER BY response_option
    """
    option_result = execute_query(option_sql)
    option_values: list[str] = []
    if "error" not in option_result:
        option_values = [
            str(r[0]).strip()
            for r in option_result.get("rows", [])
            if r and str(r[0]).strip()
        ]

    if _is_matrix_ownership_intent(question):
        preferred = _pick_preferred_binary_response_option(option_values)
        if preferred:
            selected_only = True
            response_filter_sql = (
                f"AND LOWER(response_option) = LOWER('{_escape_sql_literal(preferred)}')"
            )

    sql = f"""
        WITH base AS (
            SELECT
                demo_level,
                response_option,
                AVG(response_value_num) AS response_value,
                AVG(weighted_margin_of_error_num) AS weighted_moe,
                AVG(unweighted_margin_of_error_num) AS unweighted_moe
            FROM survey_long
            WHERE question_id = '{qid_sql}'
              AND demo_id = '{did_sql}'
              AND response_option IS NOT NULL
              AND TRIM(response_option) <> ''
              {response_filter_sql}
              AND demo_level IS NOT NULL
              AND TRIM(demo_level) <> ''
            GROUP BY demo_level, response_option
        ),
        top_opts AS (
            SELECT response_option
            FROM base
            GROUP BY response_option
            ORDER BY AVG(response_value) DESC
            LIMIT {top_n_options}
        )
        SELECT
            demo_level,
            response_option,
            100.0 * response_value AS percent,
            100.0 * COALESCE(weighted_moe, unweighted_moe) AS moe_pct
        FROM base
        WHERE response_option IN (SELECT response_option FROM top_opts)
        ORDER BY response_option, percent DESC
    """

    result = execute_query(sql)
    if "error" in result:
        return {
            "error": result["error"],
            "question_id": question_id,
            "question_text": question_text,
            "demo_id": demo_id,
        }

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    if not rows:
        return {
            "error": "No rows returned for cross-tab query.",
            "question_id": question_id,
            "question_text": question_text,
            "demo_id": demo_id,
        }

    i_demo = cols.index("demo_level")
    i_opt = cols.index("response_option")
    i_pct = cols.index("percent")
    i_moe = cols.index("moe_pct") if "moe_pct" in cols else -1
    chart_values: list[dict[str, Any]] = []
    option_levels: dict[str, list[tuple[str, float, float | None]]] = {}
    demo_levels: set[str] = set()

    for row in rows:
        level = str(row[i_demo])
        option = str(row[i_opt])
        try:
            pct = float(row[i_pct])
        except (TypeError, ValueError):
            continue
        moe_pct: float | None = None
        if i_moe >= 0:
            try:
                raw_moe = row[i_moe]
                moe_pct = float(raw_moe) if raw_moe is not None else None
            except (TypeError, ValueError):
                moe_pct = None
        demo_levels.add(level)
        chart_values.append(
            {
                "demo_level": _truncate_label(_short_demo_level(level), 30),
                "response_option": _truncate_label(option, 35),
                "percent": round(pct, 2),
                "moe_pct": round(moe_pct, 2) if isinstance(moe_pct, float) else None,
            }
        )
        option_levels.setdefault(option, []).append((_short_demo_level(level), pct, moe_pct))

    if not chart_values:
        return {
            "error": "Cross-tab query returned non-numeric values only.",
            "question_id": question_id,
            "question_text": question_text,
            "demo_id": demo_id,
        }

    option_avgs = {
        opt: sum(v for _, v, _ in vals) / max(len(vals), 1)
        for opt, vals in option_levels.items()
    }
    dominant_option = max(option_avgs.items(), key=lambda x: x[1])[0]

    gap_stats: list[dict[str, Any]] = []
    for opt, vals in option_levels.items():
        hi = max(vals, key=lambda x: x[1])
        lo = min(vals, key=lambda x: x[1])
        gap = hi[1] - lo[1]
        combined_moe: float | None = None
        is_significant = False
        if hi[2] is not None and lo[2] is not None:
            combined_moe = (hi[2] ** 2 + lo[2] ** 2) ** 0.5
            is_significant = gap > combined_moe
        gap_stats.append(
            {
                "option": opt,
                "hi": hi,
                "lo": lo,
                "gap": gap,
                "combined_moe": combined_moe,
                "is_significant": is_significant,
            }
        )

    narrative_lines = [
        f"**{question_id} — {question_text} (by {_short_demo_label(demo_id)})**",
    ]
    if selected_only and len(option_levels) == 1:
        narrative_lines.append(
            f"- Ownership rate (**{dominant_option}**) averages **{option_avgs[dominant_option]:.2f}%** across groups."
        )
    else:
        narrative_lines.append(
            f"- Highest average response option across groups: **{dominant_option}** "
            f"at **{option_avgs[dominant_option]:.2f}%**."
        )
    if gap_stats:
        largest = max(gap_stats, key=lambda x: x["gap"])
        largest_sig_txt = "significance unavailable (no MOE for compared groups)"
        if largest["combined_moe"] is not None:
            if largest["is_significant"]:
                largest_sig_txt = (
                    f"statistically significant at ~95% (gap {largest['gap']:.2f} pts > "
                    f"combined MOE {largest['combined_moe']:.2f} pts)"
                )
            else:
                largest_sig_txt = (
                    f"not statistically significant at ~95% (gap {largest['gap']:.2f} pts <= "
                    f"combined MOE {largest['combined_moe']:.2f} pts)"
                )
        narrative_lines.append(
            f"- Largest between-group spread: **{largest['option']}** ranges from "
            f"**{largest['lo'][0]} ({largest['lo'][1]:.2f}%)** to "
            f"**{largest['hi'][0]} ({largest['hi'][1]:.2f}%)**, a **{largest['gap']:.2f} pt** gap "
            f"and is **{largest_sig_txt}**."
        )

    significant_diffs = sorted(
        [x for x in gap_stats if x["is_significant"]],
        key=lambda x: x["gap"],
        reverse=True,
    )
    if significant_diffs:
        narrative_lines.append(
            f"- Significant option-level differences at ~95%: **{len(significant_diffs)}**."
        )
        for d in significant_diffs:
            narrative_lines.append(
                f"- **{d['option']}**: {d['lo'][0]} {d['lo'][1]:.2f}% vs "
                f"{d['hi'][0]} {d['hi'][1]:.2f}% "
                f"(gap {d['gap']:.2f} pts; combined MOE {d['combined_moe']:.2f} pts)."
            )
    else:
        narrative_lines.append(
            "- No option-level between-group differences met ~95% significance with available MOEs."
        )

    missing_moe_count = sum(1 for d in gap_stats if d["combined_moe"] is None)
    if missing_moe_count > 0:
        narrative_lines.append(
            f"- Significance unavailable for **{missing_moe_count}** option(s) due to missing MOE values."
        )
    narrative_lines.append(
        f"- Coverage: **{len(demo_levels)}** demographic levels and **{len(option_levels)}** response options shown."
    )
    narrative_lines.append("- Values shown are percentages of respondents.")
    narrative_lines.append(
        "- Significance uses margin-of-error values from the source tabs; treat as directional when many pairwise comparisons are reviewed."
    )

    demo_label = _short_demo_label(demo_id)
    chart_title = question_text if len(question_text) <= 72 else question_text[:69] + "\u2026"
    n_options = len(option_levels)
    n_demos = len(demo_levels)

    # Choose chart type based on data shape.
    # Grouped bar for manageable combos; heatmap for larger matrices.
    if n_options * n_demos <= 30:
        # Grouped horizontal bar: demographic levels on Y, bars coloured by response option
        chart_spec: dict[str, Any] = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": {
                "text": f"{chart_title}",
                "subtitle": f"{question_id} \u00b7 by {demo_label}",
                "anchor": "start",
            },
            "data": {"values": chart_values},
            "mark": {"type": "bar", "cornerRadiusEnd": 4},
            "encoding": {
                "y": {
                    "field": "demo_level",
                    "type": "nominal",
                    "title": demo_label,
                    "axis": {"labelLimit": 280, "labelFontSize": 11},
                },
                "x": {
                    "field": "percent",
                    "type": "quantitative",
                    "title": "% of respondents",
                    "axis": {"format": ".0f", "grid": True},
                },
                "color": {
                    "field": "response_option",
                    "type": "nominal",
                    "title": "Response",
                    "legend": {"orient": "bottom", "columns": min(n_options, 3)},
                },
                "yOffset": {"field": "response_option", "type": "nominal"},
                "tooltip": [
                    {"field": "demo_level", "type": "nominal", "title": "Group"},
                    {"field": "response_option", "type": "nominal", "title": "Option"},
                    {"field": "percent", "type": "quantitative", "title": "%", "format": ".1f"},
                    {"field": "moe_pct", "type": "quantitative", "title": "MOE (pts)", "format": ".2f"},
                ],
            },
            "config": _CHART_CONFIG,
        }
    else:
        # Heatmap for larger matrices
        chart_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": {
                "text": f"{chart_title}",
                "subtitle": f"{question_id} \u00b7 by {demo_label}",
                "anchor": "start",
            },
            "data": {"values": chart_values},
            "layer": [
                {
                    "mark": {"type": "rect", "cornerRadius": 3},
                    "encoding": {
                        "color": {
                            "field": "percent",
                            "type": "quantitative",
                            "title": "%",
                            "scale": {
                                "scheme": "blues",
                                "domain": [0, max(v["percent"] for v in chart_values)],
                            },
                            "legend": {"format": ".0f", "gradientLength": 180},
                        },
                    },
                },
                {
                    "mark": {
                        "type": "text",
                        "fontSize": 10,
                    },
                    "encoding": {
                        "text": {"field": "percent", "type": "quantitative", "format": ".1f"},
                        "color": {
                            "condition": {
                                "test": f"datum.percent > {max(v['percent'] for v in chart_values) * 0.6}",
                                "value": "#ffffff",
                            },
                            "value": "#374151",
                        },
                    },
                },
            ],
            "encoding": {
                "y": {
                    "field": "demo_level",
                    "type": "nominal",
                    "title": demo_label,
                    "axis": {"labelLimit": 280, "labelFontSize": 11},
                },
                "x": {
                    "field": "response_option",
                    "type": "nominal",
                    "title": None,
                    "axis": {"labelAngle": -30, "labelLimit": 220, "labelFontSize": 11},
                },
                "tooltip": [
                    {"field": "demo_level", "type": "nominal", "title": "Group"},
                    {"field": "response_option", "type": "nominal", "title": "Option"},
                    {"field": "percent", "type": "quantitative", "title": "%", "format": ".1f"},
                    {"field": "moe_pct", "type": "quantitative", "title": "MOE (pts)", "format": ".2f"},
                ],
            },
            "config": _CHART_CONFIG,
        }

    return {
        "analysis_type": "question_by_demographic",
        "question_id": question_id,
        "question_text": question_text,
        "demo_id": demo_id,
        "row_count": len(chart_values),
        "significant_differences": [
            {
                "response_option": d["option"],
                "low_group": d["lo"][0],
                "low_percent": round(d["lo"][1], 2),
                "high_group": d["hi"][0],
                "high_percent": round(d["hi"][1], 2),
                "gap_points": round(d["gap"], 2),
                "combined_moe_points": round(d["combined_moe"], 2),
            }
            for d in significant_diffs
            if d["combined_moe"] is not None
        ],
        "insight_text": "\n".join(narrative_lines),
        "chart": chart_spec,
    }


def _build_question_group_by_demographic(
    question: str,
    demo_id: str,
    top_n_items: int = 20,
    item_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Cross-tab a matrix-like question group (e.g., IKEA102 items) by demographic."""
    search_result = search_questions(question)
    if "error" in search_result:
        return {"error": search_result["error"]}

    matches = search_result.get("results", [])
    if not matches:
        return {"error": "No matching question group found for matrix cross-tab."}

    best, _ = _select_question_match_and_route(question, matches)
    best = best or matches[0]
    question_group = str(best.get("question_group", "")).strip()
    question_text = str(best.get("question_text", "")).strip()
    if not question_group:
        return {"error": "Matched question is missing question_group."}
    if not question_text:
        question_text = question

    qg_sql = _escape_sql_literal(question_group)
    did_sql = _escape_sql_literal(demo_id)

    options_sql = f"""
        SELECT DISTINCT response_option
        FROM survey_long
        WHERE question_group = '{qg_sql}'
          AND demo_id = '{did_sql}'
          AND response_option IS NOT NULL
          AND TRIM(response_option) <> ''
        ORDER BY response_option
    """
    options_result = execute_query(options_sql)
    if "error" in options_result:
        return {"error": options_result["error"], "question_group": question_group, "demo_id": demo_id}
    option_rows = options_result.get("rows", [])
    option_values = [str(r[0]).strip() for r in option_rows if r and str(r[0]).strip()]
    if not option_values:
        return {"error": "No response options available for matrix cross-tab.", "question_group": question_group}

    lower_options = {o.lower(): o for o in option_values}
    preferred = None
    for candidate in ("selected", "yes", "true", "own"):
        if candidate in lower_options:
            preferred = lower_options[candidate]
            break
    if preferred is None:
        preferred = option_values[0]

    preferred_sql = _escape_sql_literal(preferred)
    top_n_items = max(5, min(int(top_n_items), 40))
    clean_item_keywords = [
        k.strip().lower()
        for k in (item_keywords or [])
        if isinstance(k, str) and k.strip()
    ]
    item_filter_sql = ""
    if clean_item_keywords:
        filters = " OR ".join(
            f"LOWER(COALESCE(NULLIF(TRIM(question_level), ''), question_id)) LIKE '%{_escape_sql_literal(k)}%'"
            for k in clean_item_keywords
        )
        item_filter_sql = f"AND ({filters})"

    sql = f"""
        WITH base AS (
            SELECT
                question_id,
                COALESCE(NULLIF(TRIM(question_level), ''), question_id) AS item_label,
                demo_level,
                AVG(response_value_num) AS response_value,
                AVG(weighted_margin_of_error_num) AS weighted_moe,
                AVG(unweighted_margin_of_error_num) AS unweighted_moe
            FROM survey_long
            WHERE question_group = '{qg_sql}'
              AND demo_id = '{did_sql}'
              AND LOWER(response_option) = LOWER('{preferred_sql}')
              AND demo_level IS NOT NULL
              AND TRIM(demo_level) <> ''
              {item_filter_sql}
            GROUP BY question_id, item_label, demo_level
        ),
        ranked_items AS (
            SELECT
                question_id,
                item_label,
                MAX(response_value) - MIN(response_value) AS gap_raw
            FROM base
            GROUP BY question_id, item_label
            ORDER BY gap_raw DESC
            LIMIT {top_n_items}
        )
        SELECT
            b.question_id,
            b.item_label,
            b.demo_level,
            100.0 * b.response_value AS percent,
            100.0 * COALESCE(b.weighted_moe, b.unweighted_moe) AS moe_pct
        FROM base b
        JOIN ranked_items r
          ON b.question_id = r.question_id
        ORDER BY r.gap_raw DESC, b.item_label, b.response_value DESC
    """

    result = execute_query(sql)
    if "error" in result:
        return {"error": result["error"], "question_group": question_group, "demo_id": demo_id}

    rows = result.get("rows", [])
    cols = result.get("columns", [])
    if not rows:
        return {"error": "No matrix rows returned for group cross-tab.", "question_group": question_group}

    i_item = cols.index("item_label")
    i_demo = cols.index("demo_level")
    i_pct = cols.index("percent")
    i_moe = cols.index("moe_pct") if "moe_pct" in cols else -1

    item_values: dict[str, list[tuple[str, float, float | None]]] = {}
    demo_levels: set[str] = set()
    for row in rows:
        item = str(row[i_item])
        level = _short_demo_level(str(row[i_demo]))
        try:
            pct = float(row[i_pct])
        except (TypeError, ValueError):
            continue
        moe: float | None = None
        if i_moe >= 0:
            try:
                raw_moe = row[i_moe]
                moe = float(raw_moe) if raw_moe is not None else None
            except (TypeError, ValueError):
                moe = None
        item_values.setdefault(item, []).append((level, pct, moe))
        demo_levels.add(level)

    if not item_values:
        return {"error": "No usable numeric matrix rows returned.", "question_group": question_group}

    item_stats: list[dict[str, Any]] = []
    for item, vals in item_values.items():
        hi = max(vals, key=lambda x: x[1])
        lo = min(vals, key=lambda x: x[1])
        gap = hi[1] - lo[1]
        combined_moe: float | None = None
        is_significant = False
        if hi[2] is not None and lo[2] is not None:
            combined_moe = (hi[2] ** 2 + lo[2] ** 2) ** 0.5
            is_significant = gap > combined_moe
        item_stats.append(
            {
                "item": item,
                "hi": hi,
                "lo": lo,
                "gap": gap,
                "combined_moe": combined_moe,
                "is_significant": is_significant,
            }
        )

    item_stats.sort(key=lambda x: x["gap"], reverse=True)
    significant = [x for x in item_stats if x["is_significant"]]
    missing_moe = [x for x in item_stats if x["combined_moe"] is None]

    top_item = item_stats[0]
    demo_label = _short_demo_label(demo_id)
    narrative_lines = [
        f"**{question_group} matrix — {question_text} (by {demo_label})**",
        f"- Response basis: **{preferred}**.",
        f"- Analyzed **{len(item_stats)}** items across **{len(demo_levels)}** {demo_label.lower()} groups.",
        f"- Largest item gap: **{top_item['item']}** from **{top_item['lo'][0]} ({top_item['lo'][1]:.2f}%)** "
        f"to **{top_item['hi'][0]} ({top_item['hi'][1]:.2f}%)** "
        f"({top_item['gap']:.2f} pts).",
        f"- Items with statistically significant between-group differences (~95%): **{len(significant)}**.",
    ]
    if clean_item_keywords:
        narrative_lines.append(
            f"- Item filter applied: {', '.join(sorted(set(clean_item_keywords)))}."
        )
    if significant:
        for s in significant:
            narrative_lines.append(
                f"- **{s['item']}**: {s['lo'][0]} {s['lo'][1]:.2f}% vs {s['hi'][0]} {s['hi'][1]:.2f}% "
                f"(gap {s['gap']:.2f} pts; combined MOE {s['combined_moe']:.2f} pts)."
            )
    if missing_moe:
        narrative_lines.append(
            f"- Significance unavailable for **{len(missing_moe)}** items because MOE values were missing."
        )
    narrative_lines.append(
        "- Significance uses source-tab MOE fields; treat as directional when reviewing many items."
    )

    chart_values = [
        {
            "item": _truncate_label(s["item"], 44),
            "gap_points": round(s["gap"], 2),
            "significant": "Significant" if s["is_significant"] else "Not significant",
            "low_group": s["lo"][0],
            "low_percent": round(s["lo"][1], 2),
            "high_group": s["hi"][0],
            "high_percent": round(s["hi"][1], 2),
            "combined_moe_points": round(s["combined_moe"], 2) if s["combined_moe"] is not None else None,
        }
        for s in item_stats
    ]

    chart_spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": {
            "text": f"{question_group} ownership gaps by {demo_label}",
            "subtitle": f"Response: {preferred}",
            "anchor": "start",
        },
        "data": {"values": chart_values},
        "mark": {"type": "bar", "cornerRadiusEnd": 4},
        "encoding": {
            "y": {
                "field": "item",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 350},
            },
            "x": {
                "field": "gap_points",
                "type": "quantitative",
                "title": "Gap across groups (percentage points)",
                "axis": {"format": ".1f", "grid": True},
            },
            "color": {
                "field": "significant",
                "type": "nominal",
                "legend": {"orient": "top-right"},
                "scale": {
                    "domain": ["Significant", "Not significant"],
                    "range": [_BRAND_BLUE, "#9ca3af"],
                },
            },
            "tooltip": [
                {"field": "item", "type": "nominal", "title": "Item"},
                {"field": "low_group", "type": "nominal", "title": "Low group"},
                {"field": "low_percent", "type": "quantitative", "title": "Low %", "format": ".2f"},
                {"field": "high_group", "type": "nominal", "title": "High group"},
                {"field": "high_percent", "type": "quantitative", "title": "High %", "format": ".2f"},
                {"field": "gap_points", "type": "quantitative", "title": "Gap (pts)", "format": ".2f"},
                {"field": "combined_moe_points", "type": "quantitative", "title": "Combined MOE (pts)", "format": ".2f"},
            ],
        },
        "config": _CHART_CONFIG,
    }

    return {
        "analysis_type": "question_group_by_demographic",
        "question_group": question_group,
        "question_text": question_text,
        "demo_id": demo_id,
        "selected_response_option": preferred,
        "item_keywords": clean_item_keywords or None,
        "item_count": len(item_stats),
        "significant_item_count": len(significant),
        "significant_differences": [
            {
                "item": s["item"],
                "low_group": s["lo"][0],
                "low_percent": round(s["lo"][1], 2),
                "high_group": s["hi"][0],
                "high_percent": round(s["hi"][1], 2),
                "gap_points": round(s["gap"], 2),
                "combined_moe_points": round(s["combined_moe"], 2),
            }
            for s in significant
            if s["combined_moe"] is not None
        ],
        "insight_text": "\n".join(narrative_lines),
        "chart": chart_spec,
    }


_SEGMENT_FILTER_MARKERS = (" for ", " among ", " amongst ")
_SEGMENT_FILTER_CUES = (
    "those ",
    "people ",
    "respondents ",
    "households ",
    "individuals ",
    "adults ",
    "customers ",
    "consumers ",
    "who ",
    "with ",
    "without ",
    "homeowner",
    "homeowners",
    "owner",
    "owners",
    "renter",
    "renters",
    "children",
    "kids",
    "income",
    "age",
    "gender",
    "ethnicity",
    "race",
    "region",
    "education",
    "urban",
    "suburban",
    "rural",
    "hispanic",
    "latino",
    "latina",
    "apartment",
    "apartments",
    "housing type",
)


def _looks_like_demographic_segment(segment_text: str) -> bool:
    seg = " ".join((segment_text or "").lower().split())
    if not seg:
        return False
    if _resolve_demo_id_from_keywords(seg):
        return True
    return any(cue in seg for cue in _SEGMENT_FILTER_CUES)


def _extract_query_segment_active_filters(query: str) -> tuple[str, dict[str, str] | None]:
    cleaned = (query or "").strip()
    if not cleaned:
        return cleaned, None

    lowered = cleaned.lower()
    split_idx = -1
    marker_len = 0
    for marker in _SEGMENT_FILTER_MARKERS:
        idx = lowered.rfind(marker)
        if idx > split_idx:
            split_idx = idx
            marker_len = len(marker)

    if split_idx == -1:
        return cleaned, None

    base_question = cleaned[:split_idx].strip(" ?.")
    segment_expr = cleaned[split_idx + marker_len :].strip(" ?.")
    if len(base_question) < 5 or not segment_expr:
        return cleaned, None
    if not _looks_like_demographic_segment(segment_expr):
        return cleaned, None

    demo_id = _resolve_demo_id_from_text(segment_expr)
    if not demo_id:
        return cleaned, None

    target_demo_level = _resolve_target_demo_level_for_query(demo_id, segment_expr)
    if not target_demo_level:
        target_demo_level = _resolve_target_demo_level_for_query(demo_id, cleaned)
    if not target_demo_level:
        return cleaned, None

    return base_question, {demo_id: target_demo_level}


def _resolve_effective_active_filters(
    active_filters: dict[str, str] | None,
    inferred_filters: dict[str, str] | None = None,
) -> dict[str, str] | None:
    inferred = _normalize_active_filters(inferred_filters)
    if inferred:
        return inferred
    normalized = _normalize_active_filters(active_filters)
    return normalized or None


def build_direct_result_for_user_query(
    user_message: str,
    active_filters: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Optional deterministic route for common demographic asks."""
    if not user_message:
        return None

    cleaned = user_message.strip()
    if cleaned.startswith("[Active filters:"):
        parts = cleaned.split("\n\n", 1)
        cleaned = parts[1].strip() if len(parts) > 1 else cleaned

    q = cleaned.lower()
    routed_question, inferred_filters = _extract_query_segment_active_filters(cleaned)
    effective_filters = _resolve_effective_active_filters(active_filters, inferred_filters)
    breakout_terms = ("breakout", "break down", "breakdown", "distribution", "split", "mix")
    has_breakout_intent = any(t in q for t in breakout_terms)
    has_compare_intent = _has_comparison_intent(q)

    extreme_request = _resolve_extreme_difference_request(cleaned)
    if extreme_request:
        extreme = _build_extreme_difference_by_demographic(
            question=str(extreme_request["question"]),
            demo_id=str(extreme_request["demo_id"]),
            target_demo_level=str(extreme_request["target_demo_level"]),
            operator=str(extreme_request.get("operator", "most_different")),
            top_n=10,
        )
        if "error" not in extreme:
            return extreme

    broad_request = _resolve_broad_demo_difference_request(cleaned)
    if broad_request:
        broad = _build_broad_differences_by_demographic(
            demo_id=str(broad_request["demo_id"]),
            top_n=10,
            active_filters=effective_filters,
        )
        if "error" not in broad:
            return broad

    segment_first = _resolve_segment_first_comparison_request(cleaned)
    if segment_first:
        demo_id = str(segment_first["demo_id"])
        subject_question = str(segment_first["subject_question"])
        subject_room_route = _resolve_room_intent_group(subject_question)
        if subject_room_route:
            matrix = _build_question_group_by_demographic(
                question=str(subject_room_route["question_text"]),
                demo_id=demo_id,
                top_n_items=24,
                item_keywords=subject_room_route.get("item_keywords"),
            )
            if "error" not in matrix:
                return matrix
            cross = _build_question_by_demographic(
                question=str(subject_room_route["question_text"]),
                demo_id=demo_id,
                top_n_options=5,
            )
            if "error" not in cross:
                return cross

        cross = _build_question_by_demographic(
            question=subject_question,
            demo_id=demo_id,
            top_n_options=5,
        )
        if "error" not in cross:
            return cross

    room_route = _resolve_room_intent_group(routed_question)
    if room_route:
        return _build_top_selected_for_question_group(
            question_group=str(room_route["question_group"]),
            question_text=str(room_route["question_text"]),
            room_hint=str(room_route["room_hint"]),
            top_n=10,
            analysis_type=str(room_route.get("analysis_type", "top_selected_items")),
            title_prefix=str(room_route.get("title_prefix", "Top Selected Items")),
            item_label=str(room_route.get("item_label", "Item")),
            summary_noun=str(room_route.get("summary_noun", "item")),
            item_keywords=room_route.get("item_keywords"),
            active_filters=effective_filters,
        )

    has_income = "income" in q
    has_children = (
        ("children" in q and "household" in q)
        or "kids" in q
        or "with children" in q
        or "people with children" in q
        or "have children" in q
    )
    if has_income and has_children and has_compare_intent:
        cross = _build_question_by_demographic(
            question="income",
            demo_id="TOTAL: Children in Household",
            top_n_options=5,
        )
        if "error" not in cross:
            return cross

    if has_income and has_children:
        return _build_demographic_breakout(
            demo_ids=["TOTAL: Income", "TOTAL: Children in Household"],
            top_n=8,
        )

    if _is_age_homeownership_intent(cleaned):
        return _build_demographic_breakout(
            demo_ids=["TOTAL: Age", "TOTAL: Home Ownership"],
            top_n=8,
        )
    if _is_age_demographic_intent(cleaned):
        return _build_demographic_breakout(
            demo_ids=["TOTAL: Age"],
            top_n=8,
        )

    if _is_home_size_intent(cleaned):
        quick = _build_quick_insight(
            question=routed_question,
            top_n=8,
            active_filters=effective_filters,
        )
        if "error" not in quick:
            return quick

    # Common phrasing like "What types of homes do people live in?"
    if _is_housing_type_breakout_intent(cleaned):
        return _build_demographic_breakout(
            demo_ids=["TOTAL: Housing Type"],
            top_n=8,
        )

    # Generic comparison phrasing: "<question> differ ... for <segment expr>".
    if has_compare_intent and (" vs " in q or " versus " in q):
        for_idx = q.rfind(" for ")
        if for_idx != -1:
            segment_expr = cleaned[for_idx + 5 :].strip(" ?.")
            demo_id = _resolve_demo_id_from_text(segment_expr)
            if demo_id:
                base_question = cleaned[:for_idx].strip(" ?.")
                if len(base_question) < 5:
                    base_question = cleaned

                if _is_matrix_ownership_intent(base_question):
                    item_keywords = _extract_matrix_item_keywords(base_question)
                    matrix = _build_question_group_by_demographic(
                        question=base_question,
                        demo_id=demo_id,
                        top_n_items=24,
                        item_keywords=item_keywords,
                    )
                    if "error" not in matrix:
                        return matrix

                cross = _build_question_by_demographic(
                    question=base_question,
                    demo_id=demo_id,
                    top_n_options=5,
                )
                if "error" not in cross:
                    return cross

    # Generic cross-tab path: "<question> by <demographic>"
    by_idx = q.rfind(" by ")
    if by_idx != -1:
        by_tail = cleaned[by_idx + 4 :].strip(" ?.")
        demo_id = _resolve_demo_id_from_text(by_tail)
        if demo_id:
            base_question = cleaned[:by_idx].strip(" ?.")
            if len(base_question) < 5:
                base_question = cleaned

            if _is_matrix_ownership_intent(base_question):
                item_keywords = _extract_matrix_item_keywords(base_question)
                matrix = _build_question_group_by_demographic(
                    question=base_question,
                    demo_id=demo_id,
                    top_n_items=24,
                    item_keywords=item_keywords,
                )
                if "error" not in matrix:
                    return matrix

            cross = _build_question_by_demographic(
                question=base_question,
                demo_id=demo_id,
                top_n_options=5,
            )
            if "error" in cross and base_question != cleaned:
                cross = _build_question_by_demographic(
                    question=cleaned,
                    demo_id=demo_id,
                    top_n_options=5,
                )
            return cross

    if has_breakout_intent:
        demo_id = _resolve_demo_id_from_text(q)
        if demo_id:
            return _build_demographic_breakout(demo_ids=[demo_id], top_n=8)

    # Broad ownership phrasing without explicit "by".
    if _is_matrix_ownership_intent(q):
        demo_id = _resolve_demo_id_from_text(q)
        if demo_id:
            item_keywords = _extract_matrix_item_keywords(cleaned)
            matrix = _build_question_group_by_demographic(
                question=cleaned,
                demo_id=demo_id,
                top_n_items=24,
                item_keywords=item_keywords,
            )
            if "error" not in matrix:
                return matrix

    if inferred_filters:
        segmented = _build_quick_insight(
            question=routed_question,
            top_n=8,
            active_filters=effective_filters,
        )
        if "error" not in segmented:
            return segmented

    return build_unanswerable_result_for_user_query(cleaned)


# Tool definitions sent to model API
TOOL_DEFINITIONS = [
    {
        "name": "quick_insight",
        "description": (
            "Fast path for most user questions. It searches the question catalog, "
            "queries top response options for the best-matching question, and returns "
            "a chart-ready summary in one call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "User intent or question text to match in survey questions.",
                },
                "demo_level": {
                    "type": "string",
                    "description": "Optional exact demo_level filter; omit for total respondents.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "How many response options to return (3-20).",
                    "minimum": 3,
                    "maximum": 20,
                    "default": 8,
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "demographic_breakout",
        "description": (
            "Build a demographic distribution breakout for one or more demo dimensions "
            "(for example income tiers), with an estimated share chart and deeper narrative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "demo_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact demo_id values, e.g. 'TOTAL: Income'.",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 3,
                    "maximum": 12,
                    "default": 8,
                },
            },
            "required": ["demo_ids"],
        },
    },
    {
        "name": "question_by_demographic",
        "description": (
            "Cross-tab a survey question by one demographic dimension "
            "(e.g., furniture ownership by income), and return chart + narrative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question intent text to map to a survey question_id.",
                },
                "demo_id": {
                    "type": "string",
                    "description": "Exact demographic dimension, e.g. 'TOTAL: Income'.",
                },
                "top_n_options": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 8,
                    "default": 5,
                },
            },
            "required": ["question", "demo_id"],
        },
    },
    {
        "name": "question_group_by_demographic",
        "description": (
            "Cross-tab a matrix question group (multiple items, e.g. IKEA102) "
            "by one demographic dimension and return item-level gap/significance analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "User question intent to map to a question_group.",
                },
                "demo_id": {
                    "type": "string",
                    "description": "Exact demographic dimension, e.g. 'TOTAL: Income'.",
                },
                "top_n_items": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 40,
                    "default": 20,
                },
                "item_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional item-level filters (e.g. ['sofa','couch']).",
                },
            },
            "required": ["question", "demo_id"],
        },
    },
    {
        "name": "search_questions",
        "description": (
            "Search the question catalog by keywords. Returns matching question IDs, "
            "texts, and metadata. Use this when quick_insight is not enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "Space-separated keywords to search for.",
                }
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "query_data",
        "description": (
            "Execute a read-only SQL SELECT against the DuckDB database. "
            "Tables available: survey_long, question_catalog. Returns up to 500 rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT query to execute",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "create_chart",
        "description": (
            "Create a Vega-Lite v5 chart specification for the client to render. "
            "Include data inline under `data.values`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "A complete Vega-Lite v5 specification object.",
                }
            },
            "required": ["spec"],
        },
    },
]


def handle_tool_call(
    tool_name: str,
    tool_input: dict,
    active_filters: dict[str, str] | None = None,
) -> str:
    """Execute a tool call and return JSON string result."""
    if tool_name == "quick_insight":
        result = _build_quick_insight(
            question=str(tool_input.get("question", "")),
            demo_level=tool_input.get("demo_level"),
            top_n=int(tool_input.get("top_n", 8)),
            active_filters=active_filters,
        )
    elif tool_name == "demographic_breakout":
        raw_demo_ids = tool_input.get("demo_ids", [])
        demo_ids = [str(x) for x in raw_demo_ids] if isinstance(raw_demo_ids, list) else []
        result = _build_demographic_breakout(
            demo_ids=demo_ids,
            top_n=int(tool_input.get("top_n", 8)),
        )
    elif tool_name == "question_by_demographic":
        result = _build_question_by_demographic(
            question=str(tool_input.get("question", "")),
            demo_id=str(tool_input.get("demo_id", "")),
            top_n_options=int(tool_input.get("top_n_options", 5)),
        )
    elif tool_name == "question_group_by_demographic":
        raw_item_keywords = tool_input.get("item_keywords", [])
        item_keywords = (
            [str(x) for x in raw_item_keywords]
            if isinstance(raw_item_keywords, list)
            else None
        )
        result = _build_question_group_by_demographic(
            question=str(tool_input.get("question", "")),
            demo_id=str(tool_input.get("demo_id", "")),
            top_n_items=int(tool_input.get("top_n_items", 20)),
            item_keywords=item_keywords,
        )
    elif tool_name == "search_questions":
        result = search_questions(tool_input["keywords"])
    elif tool_name == "query_data":
        result = execute_query(tool_input["sql"])
    elif tool_name == "create_chart":
        spec = tool_input.get("spec", {})
        if not isinstance(spec, dict):
            result = {"error": "spec must be a JSON object"}
        elif (
            "mark" not in spec
            and "layer" not in spec
            and "concat" not in spec
            and "hconcat" not in spec
            and "vconcat" not in spec
        ):
            result = {"error": "spec must include 'mark' or a composition keyword"}
        else:
            spec.setdefault("$schema", "https://vega.github.io/schema/vega-lite/v5.json")
            result = {"chart": spec}
    else:
        result = {"error": f"Unknown tool: {tool_name}"}

    return json.dumps(result, default=str)
