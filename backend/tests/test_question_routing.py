import unittest
from unittest.mock import patch

try:
    from app.agent import tools
except Exception as import_error:  # pragma: no cover - env-dependent import
    tools = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class QuestionRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def setUp(self):
        self.matches = [
            {
                "question_id": "IKEA100",
                "question_group": "IKEA100",
                "question_text": "Question A",
                "match_score": 0.9,
            },
            {
                "question_id": "IKEA200",
                "question_group": "IKEA200",
                "question_text": "Question B",
                "match_score": 0.8,
            },
        ]

    def test_routes_to_demographic_breakout_when_confident(self):
        decision = {
            "intent": "demographic_breakout",
            "selected_demo_id": "TOTAL: Income",
            "confidence": 0.9,
        }

        with (
            patch.object(tools.settings, "question_match_model_min_confidence", 0.55),
            patch.object(tools, "_pick_best_question_match", return_value=self.matches[0]),
            patch.object(tools, "_model_assisted_question_decision", return_value=decision),
        ):
            selected, demo_id = tools._select_question_match_and_route(
                "show by income",
                self.matches,
            )

        self.assertIsNone(selected)
        self.assertEqual(demo_id, "TOTAL: Income")

    def test_routes_to_selected_question_id_when_confident(self):
        decision = {
            "intent": "question",
            "selected_question_id": "IKEA200",
            "confidence": 0.95,
        }

        with (
            patch.object(tools.settings, "question_match_model_min_confidence", 0.55),
            patch.object(tools, "_pick_best_question_match", return_value=self.matches[0]),
            patch.object(tools, "_model_assisted_question_decision", return_value=decision),
        ):
            selected, demo_id = tools._select_question_match_and_route(
                "question b",
                self.matches,
            )

        self.assertEqual(selected, self.matches[1])
        self.assertIsNone(demo_id)

    def test_low_confidence_falls_back_to_heuristic(self):
        decision = {
            "intent": "question",
            "selected_question_id": "IKEA200",
            "confidence": 0.2,
        }

        with (
            patch.object(tools.settings, "question_match_model_min_confidence", 0.55),
            patch.object(tools, "_pick_best_question_match", return_value=self.matches[0]),
            patch.object(tools, "_model_assisted_question_decision", return_value=decision),
        ):
            selected, demo_id = tools._select_question_match_and_route(
                "question b",
                self.matches,
            )

        self.assertEqual(selected, self.matches[0])
        self.assertIsNone(demo_id)


if __name__ == "__main__":
    unittest.main()
