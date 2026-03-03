import unittest
from unittest.mock import patch

try:
    from app.agent import provider
except Exception as import_error:  # pragma: no cover - env-dependent import
    provider = None
    _IMPORT_ERROR = import_error
else:
    _IMPORT_ERROR = None


class ReasoningEffortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if provider is None:
            raise unittest.SkipTest(f"Missing backend deps for import: {_IMPORT_ERROR}")

    def test_resolve_runtime_options_uses_default_reasoning_effort(self):
        with (
            patch.object(provider.settings, "llm_reasoning_effort", "low"),
            patch.object(provider.settings, "llm_reasoning_escalate_on_retry", True),
        ):
            options = provider.resolve_runtime_options(None)

        self.assertEqual(options.reasoning_effort, "low")
        self.assertTrue(options.reasoning_escalate_on_retry)

    def test_resolve_runtime_options_allows_override(self):
        with patch.object(provider.settings, "llm_reasoning_effort", "minimal"):
            options = provider.resolve_runtime_options({"reasoning_effort": "HIGH"})

        self.assertEqual(options.reasoning_effort, "high")

    def test_resolve_runtime_options_rejects_invalid_effort(self):
        with self.assertRaises(ValueError):
            provider.resolve_runtime_options({"reasoning_effort": "turbo"})

    def test_retry_escalation_steps_up(self):
        self.assertEqual(
            provider.resolve_reasoning_effort_for_attempt("minimal", 0, True),
            "minimal",
        )
        self.assertEqual(
            provider.resolve_reasoning_effort_for_attempt("minimal", 1, True),
            "low",
        )
        self.assertEqual(
            provider.resolve_reasoning_effort_for_attempt("minimal", 2, True),
            "medium",
        )
        self.assertEqual(
            provider.resolve_reasoning_effort_for_attempt("minimal", 5, True),
            "high",
        )

    def test_retry_escalation_can_be_disabled(self):
        self.assertEqual(
            provider.resolve_reasoning_effort_for_attempt("low", 3, False),
            "low",
        )


if __name__ == "__main__":
    unittest.main()
