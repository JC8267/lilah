import unittest

from pydantic import ValidationError

try:
    from app.agent import tools
    from app.models.schemas import ChatRequest
except Exception as import_error:  # pragma: no cover - env-dependent import
    tools = None
    ChatRequest = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class FilterBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None or ChatRequest is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_chat_request_rejects_multiple_active_filters(self):
        with self.assertRaises(ValidationError):
            ChatRequest(
                message="test",
                filters={
                    "TOTAL: Age": "TOTAL: Age 18-34",
                    "TOTAL: Income": "TOTAL: Income $50K-$74K",
                },
            )

    def test_user_selected_filter_wins_over_inferred_filter(self):
        resolved = tools._resolve_effective_active_filters(
            {"TOTAL: Age": "TOTAL: Age 18-34"},
            {"TOTAL: Income": "TOTAL: Income $50K-$74K"},
        )

        self.assertEqual(
            resolved,
            {"TOTAL: Age": "TOTAL: Age 18-34"},
        )

    def test_same_dimension_filter_becomes_target_comparison(self):
        target_demo_level, compare_to_others, applied_filters, ignored_filters = (
            tools._resolve_comparison_filter_context(
                demo_id="TOTAL: Age",
                active_filters={"TOTAL: Age": "TOTAL: Age 18-34"},
            )
        )

        self.assertEqual(target_demo_level, "TOTAL: Age 18-34")
        self.assertTrue(compare_to_others)
        self.assertEqual(
            applied_filters,
            [{"demo_id": "TOTAL: Age", "demo_level": "TOTAL: Age 18-34"}],
        )
        self.assertEqual(ignored_filters, [])

    def test_different_dimension_filter_is_marked_ignored(self):
        target_demo_level, compare_to_others, applied_filters, ignored_filters = (
            tools._resolve_comparison_filter_context(
                demo_id="TOTAL: Income",
                active_filters={"TOTAL: Age": "TOTAL: Age 18-34"},
            )
        )

        self.assertIsNone(target_demo_level)
        self.assertFalse(compare_to_others)
        self.assertEqual(applied_filters, [])
        self.assertEqual(len(ignored_filters), 1)
        self.assertEqual(ignored_filters[0]["demo_id"], "TOTAL: Age")


if __name__ == "__main__":
    unittest.main()
