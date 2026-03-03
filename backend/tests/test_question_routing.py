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


class DirectSegmentFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_improvments_typo_routes_to_planned_improvements(self):
        intent = tools._detect_room_matrix_intent(
            "what are the top improvments planned for those living with children"
        )
        self.assertEqual(intent, "improvements_planned")

    def test_extracts_children_segment_filter(self):
        with patch.object(
            tools,
            "_resolve_target_demo_level_for_query",
            return_value="TOTAL: Living With Children",
        ):
            base_question, inferred_filters = tools._extract_query_segment_active_filters(
                "What are the top planned improvements for those living with children?"
            )

        self.assertEqual(base_question, "What are the top planned improvements")
        self.assertEqual(
            inferred_filters,
            {"TOTAL: Children in Household": "TOTAL: Living With Children"},
        )

    def test_direct_route_applies_inferred_segment_filters(self):
        inferred_filters = {
            "TOTAL: Children in Household": "TOTAL: Living With Children",
        }
        room_payload = {
            "question_group": "IKEA703",
            "question_text": "Which planned improvements do you expect to make?",
            "room_hint": "home",
            "analysis_type": "top_planned_improvements",
            "title_prefix": "Top Planned Improvements",
            "item_label": "Planned improvement",
            "summary_noun": "planned improvement",
        }
        built_payload = {
            "analysis_type": "top_planned_improvements",
            "insight_text": "ok",
        }

        with (
            patch.object(
                tools,
                "_extract_query_segment_active_filters",
                return_value=("What are the top planned improvements", inferred_filters),
            ) as extract_mock,
            patch.object(tools, "_resolve_room_intent_group", return_value=room_payload) as route_mock,
            patch.object(tools, "_build_top_selected_for_question_group", return_value=built_payload) as top_mock,
        ):
            result = tools.build_direct_result_for_user_query(
                "What are the top planned improvements for those living with children?"
            )

        self.assertEqual(result, built_payload)
        extract_mock.assert_called_once()
        route_mock.assert_called_once_with("What are the top planned improvements")
        self.assertEqual(
            top_mock.call_args.kwargs.get("active_filters"),
            inferred_filters,
        )


if __name__ == "__main__":
    unittest.main()
