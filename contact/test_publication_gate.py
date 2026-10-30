import unittest

from publication_gate import publication_decision


class PublicationDecisionTests(unittest.TestCase):
    def setUp(self):
        self.allowed_state = {
            "enabled": True,
            "founder_gate": {
                "external_mutation": "ALLOW_WITH_EXPLICIT_AUTHORIZATION"
            },
        }
        self.denied_state = {
            "enabled": True,
            "founder_gate": {
                "external_mutation": "DENY_WITHOUT_EXPLICIT_AUTHORIZATION"
            },
        }

    def test_schedule_never_publishes_even_if_state_and_input_allow(self):
        self.assertEqual(
            publication_decision(self.allowed_state, "schedule", "true"),
            (False, "MANUAL_DISPATCH_REQUIRED"),
        )

    def test_push_event_never_publishes(self):
        self.assertEqual(
            publication_decision(self.allowed_state, "push", "true"),
            (False, "MANUAL_DISPATCH_REQUIRED"),
        )

    def test_manual_run_requires_explicit_confirmation(self):
        self.assertEqual(
            publication_decision(self.allowed_state, "workflow_dispatch", "false"),
            (False, "EXPLICIT_CONFIRMATION_MISSING"),
        )

    def test_manual_run_respects_existing_founder_deny(self):
        self.assertEqual(
            publication_decision(self.denied_state, "workflow_dispatch", "true"),
            (False, "FOUNDER_GATE_DENY"),
        )

    def test_manual_run_requires_both_explicit_gates(self):
        self.assertEqual(
            publication_decision(self.allowed_state, "workflow_dispatch", "true"),
            (True, "EXPLICIT_MANUAL_AUTHORIZATION"),
        )

    def test_disabled_or_malformed_state_fails_closed(self):
        self.assertEqual(
            publication_decision({**self.allowed_state, "enabled": False}, "workflow_dispatch", "true"),
            (False, "CONTACT_DISABLED"),
        )
        self.assertEqual(
            publication_decision(None, "workflow_dispatch", "true"),
            (False, "STATE_INVALID"),
        )


if __name__ == "__main__":
    unittest.main()
