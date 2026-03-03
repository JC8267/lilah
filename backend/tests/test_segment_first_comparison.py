import unittest
from unittest.mock import patch

try:
    from app.agent import tools
except Exception as import_error:  # pragma: no cover - env-dependent import
    tools = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class SegmentFirstComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_parse_segment_first_comparison(self):
        parsed = tools._resolve_segment_first_comparison_request(
            "Compare ages 18-34 vs other ages for top challenges of the home"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.get("demo_id"), "TOTAL: Age")
        self.assertEqual(parsed.get("subject_question"), "top challenges of the home")

    def test_direct_route_prefers_demographic_matrix_for_segment_first(self):
        payload = {
            "analysis_type": "question_group_by_demographic",
            "demo_id": "TOTAL: Age",
            "insight_text": "ok",
        }
        room_route = {
            "question_group": "IKEA705",
            "question_text": "Obstacle question",
            "room_hint": "home",
            "item_keywords": None,
        }

        with (
            patch.object(tools, "_resolve_room_intent_group", return_value=room_route),
            patch.object(tools, "_build_question_group_by_demographic", return_value=payload) as mocked_matrix,
        ):
            result = tools.build_direct_result_for_user_query(
                "Compare ages 18-34 vs other ages for top challenges of the home"
            )

        self.assertEqual(result, payload)
        mocked_matrix.assert_called_once()

    def test_unanswerable_guard_skips_segment_first_comparison(self):
        with (
            patch.object(tools, "_resolve_room_intent_group", return_value=None),
            patch.object(tools, "_resolve_demo_id_from_text", return_value=None),
            patch.object(
                tools,
                "_resolve_segment_first_comparison_request",
                return_value={
                    "segment_expr": "ages 18-34 vs other ages",
                    "subject_question": "top challenges of the home",
                    "demo_id": "TOTAL: Age",
                },
            ),
        ):
            result = tools.build_unanswerable_result_for_user_query(
                "Compare ages 18-34 vs other ages for top challenges of the home"
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
