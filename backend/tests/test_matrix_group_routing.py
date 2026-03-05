import unittest
from unittest.mock import patch

try:
    from app.agent import tools
except Exception as import_error:  # pragma: no cover - env-dependent import
    tools = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class MatrixGroupRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_extract_matrix_item_keywords_for_storage_furniture(self):
        keywords = tools._extract_matrix_item_keywords(
            "Compare storage furniture by Housing Type"
        )

        for keyword in (
            "storage",
            "bookcase",
            "shelf",
            "dresser",
            "drawer",
            "closet",
            "nightstand",
            "bin",
            "container",
        ):
            self.assertIn(keyword, keywords)

    def test_direct_route_prefers_group_matrix_for_storage_furniture(self):
        payload = {
            "analysis_type": "question_group_by_demographic",
            "demo_id": "TOTAL: Housing Type",
            "insight_text": "ok",
        }

        with (
            patch.object(tools, "_build_question_group_by_demographic", return_value=payload) as matrix_mock,
            patch.object(tools, "_build_question_by_demographic") as cross_mock,
        ):
            result = tools.build_direct_result_for_user_query(
                "Compare storage furniture by Housing Type"
            )

        self.assertEqual(result, payload)
        matrix_mock.assert_called_once()
        cross_mock.assert_not_called()
        _, kwargs = matrix_mock.call_args
        self.assertEqual(kwargs.get("demo_id"), "TOTAL: Housing Type")
        self.assertIn("storage", kwargs.get("item_keywords", []))
        self.assertIn("bookcase", kwargs.get("item_keywords", []))

    def test_question_by_demographic_promotes_matrix_topic_match(self):
        payload = {
            "analysis_type": "question_group_by_demographic",
            "demo_id": "TOTAL: Housing Type",
            "insight_text": "ok",
        }
        candidate = {
            "question_id": "IKEA202_1",
            "question_group": "IKEA202",
            "question_text": "Which of the below home furniture or furnishings do you have in this room? Select all that apply.",
            "has_question_level": "1",
            "response_option_count": 2,
        }

        with (
            patch.object(tools, "search_questions", return_value={"results": [candidate]}),
            patch.object(tools, "_select_question_match_and_route", return_value=(candidate, None)),
            patch.object(tools, "_build_question_group_by_demographic", return_value=payload) as matrix_mock,
        ):
            result = tools._build_question_by_demographic(
                question="Compare storage furniture by Housing Type",
                demo_id="TOTAL: Housing Type",
            )

        self.assertEqual(result, payload)
        matrix_mock.assert_called_once()
        _, kwargs = matrix_mock.call_args
        self.assertEqual(kwargs.get("question"), "Compare storage furniture by Housing Type")
        self.assertEqual(kwargs.get("demo_id"), "TOTAL: Housing Type")
        self.assertIn("storage", kwargs.get("item_keywords", []))
        self.assertIn("closet", kwargs.get("item_keywords", []))


if __name__ == "__main__":
    unittest.main()
