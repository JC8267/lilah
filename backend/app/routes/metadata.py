"""Metadata routes: demographics, question search."""

from fastapi import APIRouter

from app.db.duckdb_engine import get_demo_dimensions, search_questions

router = APIRouter(prefix="/api/metadata")


@router.get("/demographics")
def get_demographics():
    """Return all available demo_id/demo_level pairs."""
    return get_demo_dimensions()


@router.get("/questions")
def search_q(q: str = ""):
    """Search question catalog by keywords."""
    if not q.strip():
        return {"results": [], "count": 0}
    return search_questions(q)
