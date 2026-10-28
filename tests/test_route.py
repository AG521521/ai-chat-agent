import unittest

from core import route
from core.model import RiskAssessment, RiskLevel, State
from tests.helpers import SETTINGS, analysis, msg


def assess(level: str = "R0", requires_human: bool = False) -> RiskAssessment:
    return RiskAssessment(RiskLevel(level), [], requires_human)


class TestRouteOrder(unittest.TestCase):
    def test_paused_wins(self):
        d = route.route(msg(), analysis(), assess(), SETTINGS, paused=True)
        self.assertIs(d.state, State.PAUSED)

    def test_contact_unverified_holds(self):
        a = RiskAssessment(RiskLevel.R3, ["system:contact_unverified"], True)
        d = route.route(msg(contact_verified=False), analysis(), a, SETTINGS)
        self.assertIs(d.state, State.ERROR_HOLD)

    def test_not_allowlisted_cancels(self):
        d = route.route(msg(is_allowlisted=False), analysis(), assess(), SETTINGS)
        self.assertIs(d.state, State.CANCELLED)

    def test_duplicate_cancels(self):
        d = route.route(msg(), analysis(), assess(), SETTINGS, duplicate_of="other-task")
        self.assertIs(d.state, State.CANCELLED)

    def test_r3_holds(self):
        d = route.route(msg(), analysis(), assess("R3"), SETTINGS)
        self.assertIs(d.state, State.ERROR_HOLD)

    def test_r2_needs_approval(self):
        d = route.route(msg(), analysis(), assess("R2"), SETTINGS)
        self.assertIs(d.state, State.WAITING_APPROVAL)
        self.assertTrue(d.requires_human)

    def test_r1_needs_approval(self):
        d = route.route(msg(), analysis(), assess("R1"), SETTINGS)
        self.assertIs(d.state, State.WAITING_APPROVAL)

    def test_quote_unverified_goes_to_verifying_quote(self):
        d = route.route(
            msg(requires_quote=True, quote_target_verified=False),
            analysis(),
            assess("R0"),
            SETTINGS,
        )
        self.assertIs(d.state, State.VERIFYING_QUOTE)
        self.assertTrue(d.requires_human)

    def test_manual_mode_needs_approval_even_for_r0(self):
        d = route.route(msg(), analysis(), assess("R0"), SETTINGS)
        self.assertIs(d.state, State.WAITING_APPROVAL)
        self.assertIn("manual_mode", d.reasons)

    def test_dry_run_needs_approval(self):
        settings = {**SETTINGS, "reply": {**SETTINGS["reply"], "mode": "auto_with_risk_gate"}}
        d = route.route(msg(), analysis(), assess("R0"), settings)
        self.assertIs(d.state, State.WAITING_APPROVAL)
        self.assertIn("dry_run", d.reasons)

    def test_auto_and_live_can_prepare(self):
        settings = {
            "app": {"dry_run": False},
            "reply": {**SETTINGS["reply"], "mode": "auto_with_risk_gate"},
        }
        d = route.route(msg(), analysis(), assess("R0"), settings)
        self.assertIs(d.state, State.PREPARING_REPLY)

    def test_uncertain_model_needs_approval(self):
        d = route.route(msg(), analysis(uncertain=True), assess("R0"), SETTINGS)
        self.assertIs(d.state, State.WAITING_APPROVAL)


if __name__ == "__main__":
    unittest.main()
