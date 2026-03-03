"""In-memory DuckDB engine for survey data queries."""

from collections import Counter
import math
import duckdb
from pathlib import Path
import re
import threading
from typing import Any

from app.config import settings

_con: duckdb.DuckDBPyConnection | None = None
_semantic_index: dict[str, Any] | None = None
_demo_dimensions_cache: list[dict[str, str]] | None = None
_con_lock = threading.RLock()

_TOKEN_RE = re.compile(r"[a-z0-9_']+")
_QUERY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "ownership": ("selected", "have", "own"),
    "own": ("selected", "ownership"),
    "furniture": ("furnishings", "furnishing"),
    "furnishings": ("furniture", "furnishing"),
    "furnishing": ("furniture", "furnishings"),
    "couch": ("sofa", "loveseat"),
    "sofa": ("couch", "loveseat"),
    "kids": ("children",),
    "children": ("kids",),
    "salary": ("income",),
    "earnings": ("income",),
    "apartment": ("flat",),
    "flat": ("apartment",),
}
_SEARCH_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "between",
    "by",
    "can",
    "could",
    "does",
    "doing",
    "differ",
    "difference",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "should",
    "show",
    "that",
    "the",
    "their",
    "them",
    "these",
    "this",
    "those",
    "to",
    "us",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "would",
    "you",
    "your",
}


def _tokenize_text(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2]


def _expand_query_tokens(tokens: list[str]) -> list[str]:
    out: list[str] = list(tokens)
    seen = set(out)
    for t in tokens:
        for expanded in _QUERY_EXPANSIONS.get(t, ()):
            if expanded not in seen:
                out.append(expanded)
                seen.add(expanded)
    return out


def _filter_query_terms(tokens: list[str]) -> list[str]:
    filtered = [t for t in tokens if t not in _SEARCH_STOPWORDS and not t.isdigit()]
    if filtered:
        return filtered
    # Fall back to original tokens if everything was filtered out.
    return tokens


def _intent_boost(query_tokens: set[str], row: dict[str, Any]) -> float:
    row_text = " ".join(
        [
            str(row.get("question_text", "")),
            str(row.get("question_group", "")),
            str(row.get("question_id", "")),
        ]
    ).lower()

    has_ownership_intent = bool({"ownership", "own", "owned", "have", "has"} & query_tokens)
    has_furniture_intent = bool(
        {
            "furniture",
            "furnishing",
            "furnishings",
            "sofa",
            "couch",
            "loveseat",
            "rug",
            "chair",
            "armchair",
            "table",
            "bookcase",
        }
        & query_tokens
    )
    has_purchase_intent = bool(
        {"purchase", "buy", "bought", "planning", "plan"} & query_tokens
    )

    boost = 0.0

    if has_furniture_intent and has_ownership_intent:
        if "do you have" in row_text or "have in this room" in row_text:
            boost += 0.35
        if "select all that apply" in row_text:
            boost += 0.15
        if "agree or disagree" in row_text:
            boost -= 0.25
        if "stories/floors" in row_text or "square footage" in row_text:
            boost -= 0.35

    if has_furniture_intent and has_purchase_intent:
        if "planning to purchase" in row_text or "plan to purchase" in row_text:
            boost += 0.25

    if "income" in query_tokens and "income" in row_text:
        boost += 0.08
    if "children" in query_tokens and "children" in row_text:
        boost += 0.08

    return boost


def _build_semantic_index() -> None:
    """Build an in-memory TF-IDF semantic index over question metadata."""
    global _semantic_index
    con = get_connection()

    # Keep startup memory use low by avoiding a full scan/group-by over survey_long.
    with _con_lock:
        rows = con.execute("""
            SELECT
                question_id,
                question_group,
                question_text,
                has_question_level,
                response_option_count,
                demo_break_count,
                '' AS question_levels,
                '' AS response_options
            FROM question_catalog
        """).fetchall()

    docs: list[dict[str, Any]] = []
    doc_freq: Counter[str] = Counter()
    for row in rows:
        (
            question_id,
            question_group,
            question_text,
            has_question_level,
            response_option_count,
            demo_break_count,
            question_levels,
            response_options,
        ) = row

        combined = " ".join(
            [
                str(question_id or ""),
                str(question_group or ""),
                str(question_text or ""),
                str(question_levels or ""),
                str(response_options or ""),
            ]
        )
        tokens = _tokenize_text(combined)
        if not tokens:
            continue

        token_counts: Counter[str] = Counter(tokens)
        for tok in token_counts:
            doc_freq[tok] += 1

        docs.append(
            {
                "question_id": str(question_id or ""),
                "question_group": str(question_group or ""),
                "question_text": str(question_text or ""),
                "has_question_level": has_question_level,
                "response_option_count": response_option_count,
                "demo_break_count": demo_break_count,
                "token_counts": token_counts,
            }
        )

    n_docs = len(docs)
    if n_docs == 0:
        _semantic_index = {
            "docs": [],
            "idf": {},
            "default_idf": 1.0,
            "doc_count": 0,
        }
        return

    idf: dict[str, float] = {
        tok: math.log((1.0 + n_docs) / (1.0 + df)) + 1.0
        for tok, df in doc_freq.items()
    }
    default_idf = math.log(1.0 + n_docs) + 1.0

    for doc in docs:
        counts: Counter[str] = doc["token_counts"]
        weights: dict[str, float] = {}
        sq_sum = 0.0
        for tok, cnt in counts.items():
            tf = 1.0 + math.log(float(cnt))
            w = tf * idf.get(tok, default_idf)
            weights[tok] = w
            sq_sum += w * w
        norm = math.sqrt(sq_sum) if sq_sum > 0 else 1.0
        for tok in list(weights.keys()):
            weights[tok] /= norm
        doc["weights"] = weights
        doc.pop("token_counts", None)

    _semantic_index = {
        "docs": docs,
        "idf": idf,
        "default_idf": default_idf,
        "doc_count": n_docs,
    }


def _semantic_search_questions(query: str, limit: int = 50) -> list[dict[str, Any]]:
    idx = _semantic_index
    if not idx:
        return []

    tokens = _expand_query_tokens(_tokenize_text(query))
    if not tokens:
        return []

    idf: dict[str, float] = idx["idf"]
    q_counts: Counter[str] = Counter(tokens)
    q_weights: dict[str, float] = {}
    q_sq_sum = 0.0
    for tok, cnt in q_counts.items():
        tok_idf = idf.get(tok)
        if tok_idf is None:
            continue
        tf = 1.0 + math.log(float(cnt))
        w = tf * tok_idf
        q_weights[tok] = w
        q_sq_sum += w * w
    if not q_weights or q_sq_sum <= 0:
        return []

    q_norm = math.sqrt(q_sq_sum)
    for tok in list(q_weights.keys()):
        q_weights[tok] /= q_norm

    scored: list[dict[str, Any]] = []
    for doc in idx["docs"]:
        d_weights: dict[str, float] = doc["weights"]
        score = 0.0
        for tok, q_w in q_weights.items():
            d_w = d_weights.get(tok)
            if d_w is not None:
                score += q_w * d_w
        if score > 0:
            scored.append(
                {
                    "question_id": doc["question_id"],
                    "question_group": doc["question_group"],
                    "question_text": doc["question_text"],
                    "has_question_level": doc["has_question_level"],
                    "response_option_count": doc["response_option_count"],
                    "demo_break_count": doc["demo_break_count"],
                    "semantic_score": float(score),
                }
            )

    scored.sort(key=lambda r: r["semantic_score"], reverse=True)
    return scored[:limit]


def get_connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        raise RuntimeError("DuckDB not initialized. Call init_duckdb() first.")
    return _con


def init_duckdb() -> None:
    """Expose CSVs via DuckDB views (low-memory mode for small Render instances)."""
    global _con, _semantic_index, _demo_dimensions_cache
    with _con_lock:
        _con = duckdb.connect()

        survey_path = settings.data_dir / "survey_long.csv"
        catalog_path = settings.data_dir / "question_catalog.csv"

        survey_csv = survey_path.as_posix().replace("'", "''")
        catalog_csv = catalog_path.as_posix().replace("'", "''")

        # Constrain memory and allow spill to disk in constrained runtime environments.
        _con.execute("PRAGMA memory_limit='256MB'")
        _con.execute("PRAGMA threads=2")
        _con.execute("PRAGMA temp_directory='/tmp'")

        _con.execute(f"""
            CREATE OR REPLACE VIEW survey_long AS
            SELECT
                *,
                TRY_CAST(response_value AS DOUBLE) AS response_value_num,
                TRY_CAST(unweighted_n AS DOUBLE) AS unweighted_n_num,
                TRY_CAST(weighted_n AS DOUBLE) AS weighted_n_num,
                TRY_CAST(unweighted_margin_of_error AS DOUBLE) AS unweighted_margin_of_error_num,
                TRY_CAST(weighted_margin_of_error AS DOUBLE) AS weighted_margin_of_error_num
            FROM read_csv_auto(
                '{survey_csv}',
                header=true,
                sample_size=10000,
                all_varchar=true
            )
        """)

        _con.execute(f"""
            CREATE OR REPLACE VIEW question_catalog AS
            SELECT * FROM read_csv_auto(
                '{catalog_csv}',
                header=true,
                sample_size=10000,
                all_varchar=true
            )
        """)

        q_count = _con.execute("SELECT COUNT(*) FROM question_catalog").fetchone()[0]
    _build_semantic_index()
    _demo_dimensions_cache = None
    sem_count = int((_semantic_index or {}).get("doc_count", 0))
    print(f"DuckDB ready (CSV views): {q_count:,} questions")
    print(f"Semantic index ready: {sem_count:,} question docs")


def close_duckdb() -> None:
    global _con, _semantic_index, _demo_dimensions_cache
    with _con_lock:
        if _con is not None:
            _con.close()
            _con = None
    _semantic_index = None
    _demo_dimensions_cache = None


def execute_query(sql: str) -> dict:
    """Execute a read-only SELECT and return {columns, rows, row_count}."""
    con = get_connection()

    cleaned = sql.strip().rstrip(";")
    upper = cleaned.upper()

    # Safety: only allow read-only SELECT queries, including CTE form (WITH ... SELECT ...)
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return {"error": "Only SELECT statements are allowed."}

    for forbidden in ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                      "TRUNCATE", "REPLACE", "ATTACH", "COPY", "EXPORT"]:
        if forbidden in upper.split():
            return {"error": f"Statement contains forbidden keyword: {forbidden}"}

    # Apply row limit
    if "LIMIT" not in upper:
        cleaned += f" LIMIT {settings.max_query_rows}"

    try:
        with _con_lock:
            result = con.execute(cleaned)
            columns = [desc[0] for desc in result.description]
            rows = [list(row) for row in result.fetchall()]
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


def execute_query_params(sql: str, params: list[Any] | tuple[Any, ...]) -> dict:
    """Execute a read-only SELECT with positional parameters."""
    con = get_connection()

    cleaned = sql.strip().rstrip(";")
    upper = cleaned.upper()

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return {"error": "Only SELECT statements are allowed."}

    for forbidden in ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                      "TRUNCATE", "REPLACE", "ATTACH", "COPY", "EXPORT"]:
        if forbidden in upper.split():
            return {"error": f"Statement contains forbidden keyword: {forbidden}"}

    if "LIMIT" not in upper:
        cleaned += f" LIMIT {settings.max_query_rows}"

    try:
        with _con_lock:
            result = con.execute(cleaned, list(params))
            columns = [desc[0] for desc in result.description]
            rows = [list(row) for row in result.fetchall()]
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


def search_questions(keywords: str) -> dict:
    """Hybrid lexical + semantic search over question_catalog and aliases."""
    con = get_connection()

    query = keywords.strip()
    query_tokens = _tokenize_text(query)
    terms = _filter_query_terms(query_tokens)
    if not terms:
        return {"error": "No keywords provided."}

    lexical_rows: list[dict[str, Any]] = []
    lexical_error: str | None = None
    try:
        # Use parameterized patterns to avoid interpolating arbitrary search terms into SQL.
        patterns = [f"%{t}%" for t in terms]

        or_conditions = " OR ".join(
            "(question_text ILIKE ? OR question_id ILIKE ? OR question_group ILIKE ?)"
            for _ in patterns
        )
        score_expr = " + ".join(
            "CASE WHEN question_text ILIKE ? OR question_id ILIKE ? OR question_group ILIKE ? THEN 1 ELSE 0 END"
            for _ in patterns
        )

        sql = f"""
            SELECT
                question_id,
                question_group,
                question_text,
                has_question_level,
                response_option_count,
                demo_break_count,
                ({score_expr}) AS lexical_score
            FROM question_catalog
            WHERE {or_conditions}
            ORDER BY lexical_score DESC, question_group, question_id
            LIMIT 100
        """

        # Parameter order follows placeholder order in SQL text:
        # score expression placeholders first, then WHERE expression placeholders.
        params: list[str] = []
        for pattern in patterns:
            params.extend([pattern, pattern, pattern])
        for pattern in patterns:
            params.extend([pattern, pattern, pattern])

        with _con_lock:
            result = con.execute(sql, params)
            columns = [desc[0] for desc in result.description]
            lexical_rows = [dict(zip(columns, row)) for row in result.fetchall()]
    except Exception as e:
        lexical_error = str(e)

    semantic_rows = _semantic_search_questions(query, limit=100)

    if not lexical_rows and not semantic_rows:
        if lexical_error:
            return {"error": lexical_error}
        return {"results": [], "count": 0}

    merged: dict[str, dict[str, Any]] = {}

    max_lex = max(
        (float(r.get("lexical_score", 0.0) or 0.0) for r in lexical_rows),
        default=0.0,
    )
    for row in lexical_rows:
        qid = str(row.get("question_id", ""))
        lex_raw = float(row.get("lexical_score", 0.0) or 0.0)
        lex_norm = (lex_raw / max_lex) if max_lex > 0 else 0.0
        merged[qid] = {
            "question_id": qid,
            "question_group": row.get("question_group"),
            "question_text": row.get("question_text"),
            "has_question_level": row.get("has_question_level"),
            "response_option_count": row.get("response_option_count"),
            "demo_break_count": row.get("demo_break_count"),
            "_score": 0.55 * lex_norm,
            "_lexical": lex_norm,
            "_semantic": 0.0,
            "_sources": {"lexical"},
        }

    for row in semantic_rows:
        qid = str(row.get("question_id", ""))
        sem = float(row.get("semantic_score", 0.0) or 0.0)
        existing = merged.get(qid)
        if existing is None:
            merged[qid] = {
                "question_id": qid,
                "question_group": row.get("question_group"),
                "question_text": row.get("question_text"),
                "has_question_level": row.get("has_question_level"),
                "response_option_count": row.get("response_option_count"),
                "demo_break_count": row.get("demo_break_count"),
                "_score": 0.8 * sem,
                "_lexical": 0.0,
                "_semantic": sem,
                "_sources": {"semantic"},
            }
        else:
            existing["_score"] += 0.8 * sem
            existing["_semantic"] = max(existing["_semantic"], sem)
            existing["_sources"].add("semantic")

    rerank_tokens = set(_expand_query_tokens(_filter_query_terms(query_tokens)))
    for row in merged.values():
        boost = _intent_boost(rerank_tokens, row)
        if boost:
            row["_score"] += boost

    results = sorted(
        merged.values(),
        key=lambda r: (
            float(r["_score"]),
            float(r["_semantic"]),
            float(r["_lexical"]),
        ),
        reverse=True,
    )[:50]

    final_rows: list[dict[str, Any]] = []
    for row in results:
        final_rows.append(
            {
                "question_id": row["question_id"],
                "question_group": row["question_group"],
                "question_text": row["question_text"],
                "has_question_level": row["has_question_level"],
                "response_option_count": row["response_option_count"],
                "demo_break_count": row["demo_break_count"],
                "match_score": round(float(row["_score"]), 4),
                "match_sources": ",".join(sorted(row["_sources"])),
            }
        )

    return {"results": final_rows, "count": len(final_rows)}


def get_demo_dimensions() -> list[dict]:
    """Return distinct demo_id / demo_level pairs."""
    global _demo_dimensions_cache
    if _demo_dimensions_cache is not None:
        return list(_demo_dimensions_cache)

    con = get_connection()
    with _con_lock:
        result = con.execute("""
            SELECT DISTINCT demo_id, demo_level
            FROM survey_long
            ORDER BY demo_id, demo_level
        """)
        rows = result.fetchall()
    _demo_dimensions_cache = [
        {"demo_id": str(r[0]), "demo_level": str(r[1])}
        for r in rows
    ]
    return list(_demo_dimensions_cache)


def get_schema_description() -> str:
    """Return a compact schema description for the system prompt."""
    con = get_connection()

    with _con_lock:
        survey_cols = con.execute("DESCRIBE survey_long").fetchall()
        catalog_cols = con.execute("DESCRIBE question_catalog").fetchall()

    lines = ["## Table: survey_long (783K rows)"]
    for col in survey_cols:
        lines.append(f"  - {col[0]}: {col[1]}")

    lines.append("")
    lines.append("## Table: question_catalog (1,403 rows)")
    for col in catalog_cols:
        lines.append(f"  - {col[0]}: {col[1]}")

    return "\n".join(lines)
