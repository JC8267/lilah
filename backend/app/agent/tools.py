"""Tool definitions and handlers for the agent loop."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.provider import get_provider, resolve_runtime_options
from app.config import settings
from app.db.duckdb_engine import execute_query, search_questions


def _escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


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
    "axis": {
        "labelFontSize": 11,
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


DEMO_KEYWORD_TO_ID: list[tuple[str, str]] = [
    ("children in household", "TOTAL: Children in Household"),
    ("kids in household", "TOTAL: Children in Household"),
    ("children household", "TOTAL: Children in Household"),
    ("income", "TOTAL: Income"),
    ("age", "TOTAL: Age"),
    ("gender", "TOTAL: Gender"),
    ("sex", "TOTAL: Gender"),
    ("ethnicity", "TOTAL: Ethnicity"),
    ("race", "TOTAL: Ethnicity"),
    ("region", "TOTAL: Region"),
    ("education", "TOTAL: Education"),
    ("home ownership", "TOTAL: Home Ownership"),
    ("homeowner", "TOTAL: Home Ownership"),
    ("renter", "TOTAL: Home Ownership"),
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


def _resolve_demo_id_from_keywords(text: str) -> str | None:
    if _is_housing_type_breakout_intent(text):
        return "TOTAL: Housing Type"
    q = (text or "").lower()
    if any(t in q for t in ("apartment", "apartments")) and any(
        t in q for t in ("home", "homes", "house", "houses")
    ):
        return "TOTAL: Housing Type"

    for keyword, demo_id in DEMO_KEYWORD_TO_ID:
        if keyword in q:
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

    where = " OR ".join(
        f"LOWER(demo_id) LIKE '%{_escape_sql_literal(t)}%'" for t in tokens[:4]
    )
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
    result = execute_query(sql)
    if "error" in result:
        return None
    rows = result.get("rows", [])
    if not rows:
        return None
    return str(rows[0][0])


def _build_quick_insight(question: str, demo_level: str | None = None, top_n: int = 8) -> dict[str, Any]:
    """Fast path: search question -> fetch top response options -> create chart spec."""
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
    if not matches:
        return {"error": "No matching questions found."}

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

    if demo_level and demo_level.strip():
        demo_sql = f"demo_level = '{_escape_sql_literal(demo_level.strip())}'"
    else:
        demo_sql = "demo_id = 'Total' AND demo_level = 'TOTAL: Total respondents'"

    sql = f"""
        SELECT
            response_option,
            AVG(TRY_CAST(response_value AS DOUBLE)) AS response_value
        FROM survey_long
        WHERE question_id = '{qid_sql}'
          AND {demo_sql}
          AND response_option IS NOT NULL
          AND TRIM(response_option) <> ''
          AND TRY_CAST(response_value AS DOUBLE) IS NOT NULL
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
        "title": {"text": chart_title, "subtitle": question_id, "anchor": "start"},
        "data": {"values": chart_values},
        "height": {"step": 30},
        "encoding": {
            "y": {
                "field": "option",
                "type": "nominal",
                "sort": "-x",
                "title": None,
                "axis": {"labelLimit": 280, "labelFontSize": 12},
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
    summary_lines.append("- Values shown are percentages of respondents.")
    insight_text = "\n".join(summary_lines)

    return {
        "question_id": question_id,
        "question_text": question_text,
        "sql": sql.strip(),
        "row_count": len(chart_values),
        "top_rows": chart_values,
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
                    MAX(TRY_CAST(weighted_n AS DOUBLE)) AS weighted_n
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
                        "axis": {"labelLimit": 260, "labelFontSize": 12},
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
                AVG(TRY_CAST(response_value AS DOUBLE)) AS response_value,
                AVG(TRY_CAST(weighted_margin_of_error AS DOUBLE)) AS weighted_moe,
                AVG(TRY_CAST(unweighted_margin_of_error AS DOUBLE)) AS unweighted_moe
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
                    "axis": {"labelLimit": 200, "labelFontSize": 11},
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
                    "axis": {"labelLimit": 200, "labelFontSize": 11},
                },
                "x": {
                    "field": "response_option",
                    "type": "nominal",
                    "title": None,
                    "axis": {"labelAngle": -30, "labelLimit": 180, "labelFontSize": 11},
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
                AVG(TRY_CAST(response_value AS DOUBLE)) AS response_value,
                AVG(TRY_CAST(weighted_margin_of_error AS DOUBLE)) AS weighted_moe,
                AVG(TRY_CAST(unweighted_margin_of_error AS DOUBLE)) AS unweighted_moe
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
                "axis": {"labelLimit": 280},
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


def build_direct_result_for_user_query(user_message: str) -> dict[str, Any] | None:
    """Optional deterministic route for common demographic asks."""
    if not user_message:
        return None

    cleaned = user_message.strip()
    if cleaned.startswith("[Active filters:"):
        parts = cleaned.split("\n\n", 1)
        cleaned = parts[1].strip() if len(parts) > 1 else cleaned

    q = cleaned.lower()
    breakout_terms = ("breakout", "break down", "breakdown", "distribution", "split", "mix")
    has_breakout_intent = any(t in q for t in breakout_terms)
    has_compare_intent = _has_comparison_intent(q)

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

    return None


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


def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return JSON string result."""
    if tool_name == "quick_insight":
        result = _build_quick_insight(
            question=str(tool_input.get("question", "")),
            demo_level=tool_input.get("demo_level"),
            top_n=int(tool_input.get("top_n", 8)),
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
