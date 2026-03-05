import unittest
from unittest.mock import patch

try:
    from app.agent import tools
except Exception as import_error:  # pragma: no cover - env-dependent import
    tools = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class BroadDemoDifferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if tools is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_detects_home_ownership_broad_diff_query(self):
        result = tools._resolve_broad_demo_difference_request(
            "What sort of things do renters vs owners differ the most in?"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.get("demo_id"), "TOTAL: Home Ownership")

    def test_detects_age_cohort_broad_diff_query(self):
        result = tools._resolve_broad_demo_difference_request(
            "Overall, what do age cohorts differ most in?"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.get("demo_id"), "TOTAL: Age")

    def test_detects_race_ethnicity_broad_diff_query(self):
        result = tools._resolve_broad_demo_difference_request(
            "What topics have the biggest differences by race/etnicity?"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.get("demo_id"), "TOTAL: Ethnicity")

    def test_detects_housing_type_major_differences_query(self):
        result = tools._resolve_broad_demo_difference_request(
            "What are the major differences between those that live in houses vs apartments?"
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.get("demo_id"), "TOTAL: Housing Type")

    def test_specific_subject_query_does_not_use_broad_route(self):
        result = tools._resolve_broad_demo_difference_request(
            "What do renters vs owners differ the most in for room satisfaction?"
        )
        self.assertIsNone(result)

    def test_direct_route_uses_broad_builder(self):
        payload = {
            "analysis_type": "broad_demo_differences",
            "demo_id": "TOTAL: Home Ownership",
            "insight_text": "ok",
        }
        with patch.object(tools, "_build_broad_differences_by_demographic", return_value=payload) as mocked:
            result = tools.build_direct_result_for_user_query(
                "What sort of things do renters vs owners differ the most in?"
            )

        self.assertEqual(result, payload)
        mocked.assert_called_once()

    def test_direct_route_uses_broad_builder_for_housing_type_major_differences(self):
        payload = {
            "analysis_type": "broad_demo_differences",
            "demo_id": "TOTAL: Housing Type",
            "insight_text": "ok",
        }
        with patch.object(tools, "_build_broad_differences_by_demographic", return_value=payload) as mocked:
            result = tools.build_direct_result_for_user_query(
                "What are the major differences between those that live in houses vs apartments?"
            )

        self.assertEqual(result, payload)
        mocked.assert_called_once()

    def test_unanswerable_guard_skips_broad_route_candidates(self):
        with patch.object(
            tools,
            "_resolve_broad_demo_difference_request",
            return_value={"demo_id": "TOTAL: Home Ownership"},
        ):
            result = tools.build_unanswerable_result_for_user_query(
                "What sort of things do renters vs owners differ the most in?"
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
