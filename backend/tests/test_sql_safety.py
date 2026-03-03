import unittest
from unittest.mock import patch

try:
    from app.db import duckdb_engine
except Exception as import_error:  # pragma: no cover - env-dependent import
    duckdb_engine = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class _FakeResult:
    def __init__(self, description, rows):
        self.description = description
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, list[str] | None]] = []

    def execute(self, sql: str, params: list[str] | None = None):
        self.calls.append((sql, params))
        if "FROM question_catalog" in sql:
            return _FakeResult(
                [
                    ("question_id",),
                    ("question_group",),
                    ("question_text",),
                    ("has_question_level",),
                    ("response_option_count",),
                    ("demo_break_count",),
                    ("lexical_score",),
                ],
                [
                    (
                        "IKEA100",
                        "IKEA100",
                        "Sample catalog question",
                        "false",
                        "3",
                        "8",
                        2,
                    )
                ],
            )
        return _FakeResult([], [])


class SqlSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if duckdb_engine is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_execute_query_rejects_non_select(self):
        con = _FakeConn()
        with patch.object(duckdb_engine, "get_connection", return_value=con):
            result = duckdb_engine.execute_query("DELETE FROM survey_long")

        self.assertIn("Only SELECT statements are allowed.", result.get("error", ""))
        self.assertEqual(con.calls, [])

    def test_execute_query_params_rejects_forbidden_keyword(self):
        con = _FakeConn()
        with patch.object(duckdb_engine, "get_connection", return_value=con):
            result = duckdb_engine.execute_query_params(
                "WITH cte AS (SELECT 1) SELECT * FROM survey_long; DROP TABLE x",
                [],
            )

        self.assertIn("forbidden keyword", result.get("error", "").lower())
        self.assertEqual(con.calls, [])

    def test_search_questions_uses_parameterized_terms(self):
        con = _FakeConn()
        with (
            patch.object(duckdb_engine, "get_connection", return_value=con),
            patch.object(duckdb_engine, "_semantic_search_questions", return_value=[]),
        ):
            result = duckdb_engine.search_questions("income furniture")

        self.assertEqual(result.get("count"), 1)
        self.assertEqual(len(con.calls), 1)

        sql, params = con.calls[0]
        self.assertIn("ILIKE ?", sql)
        self.assertNotIn("%income%", sql)
        self.assertNotIn("%furniture%", sql)

        self.assertIsNotNone(params)
        assert params is not None
        self.assertIn("%income%", params)
        self.assertIn("%furniture%", params)


if __name__ == "__main__":
    unittest.main()
