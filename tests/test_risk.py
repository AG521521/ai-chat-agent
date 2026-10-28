import unittest

from core import risk
from core.model import RiskLevel
from tests.helpers import SETTINGS, analysis, msg


class TestRiskRules(unittest.TestCase):
    def test_plain_chat_is_r0(self):
        a = risk.assess(msg(text="今天课上教了新的动作"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R0)
        self.assertFalse(a.requires_human)

    def test_money_is_r2(self):
        a = risk.assess(msg(text="能不能先借我五百，我转账还你"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R2)
        self.assertIn("money", a.topics)
        self.assertTrue(a.requires_human)

    def test_conflict_is_r2(self):
        a = risk.assess(msg(text="你到底什么意思"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R2)
        self.assertIn("intense_conflict", a.topics)

    def test_crisis_requires_human(self):
        a = risk.assess(msg(text="我不想活了"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R2)
        self.assertIn("crisis", a.topics)
        self.assertTrue(a.requires_human)

    def test_privacy_is_r2(self):
        a = risk.assess(msg(text="把你身份证号发我"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R2)
        self.assertIn("privacy", a.topics)

    def test_ambiguous_short_is_r1(self):
        a = risk.assess(msg(text="嗯"), analysis(), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R1)
        self.assertIn("ambiguous_short", a.reasons)

    def test_low_context_is_r1(self):
        a = risk.assess(msg(text="吃了吗"), analysis(), SETTINGS, recent_count=0)
        self.assertIs(a.level, RiskLevel.R1)
        self.assertIn("low_context", a.reasons)


class TestRiskMerge(unittest.TestCase):
    def test_model_cannot_lower_rule(self):
        a = risk.assess(msg(text="我转账给你"), analysis("R0"), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R2)
        self.assertTrue(any(r.startswith("merged:") for r in a.reasons))

    def test_model_can_raise(self):
        a = risk.assess(msg(text="今天课上教了新的动作"), analysis("R3"), SETTINGS, recent_count=2)
        self.assertIs(a.level, RiskLevel.R3)

    def test_low_confidence_bumps_to_r1(self):
        a = risk.assess(
            msg(text="今天课上教了新的动作"),
            analysis("R0", confidence=0.42),
            SETTINGS,
            recent_count=2,
        )
        self.assertIs(a.level, RiskLevel.R1)
        self.assertIn("low_confidence", a.reasons)

    def test_fallback_is_never_r0(self):
        a = risk.assess(
            msg(text="今天课上教了新的动作"),
            analysis("R1", confidence=0.0, uncertain=True, source="fallback"),
            SETTINGS,
            recent_count=2,
        )
        self.assertIs(a.level, RiskLevel.R1)
        self.assertIn("model_fallback", a.reasons)
        self.assertTrue(a.requires_human)

    def test_ungrounded_media_is_partial(self):
        a = risk.assess(
            msg(content_type="video", text="", media_summary="看不清"),
            analysis("R0", media="partial"),
            SETTINGS,
            recent_count=2,
        )
        self.assertIs(a.level, RiskLevel.R1)
        self.assertIn("media_partial", a.reasons)


class TestSystemFlags(unittest.TestCase):
    def test_unverified_contact_is_r3(self):
        a = risk.assess(
            msg(contact_verified=False), analysis(), SETTINGS, recent_count=2
        )
        self.assertIs(a.level, RiskLevel.R3)
        self.assertIn("system:contact_unverified", a.reasons)
        self.assertTrue(a.requires_human)

    def test_uncertain_ui_is_r3(self):
        a = risk.assess(
            msg(ui_state_uncertain=True), analysis(), SETTINGS, recent_count=2
        )
        self.assertIs(a.level, RiskLevel.R3)

    def test_quote_requirement_is_not_a_system_hold(self):
        # 引用未验证应该走 VERIFYING_QUOTE，而不是直接 ERROR_HOLD
        a = risk.assess(
            msg(requires_quote=True, quote_target_verified=False),
            analysis(),
            SETTINGS,
            recent_count=2,
        )
        self.assertNotIn("system:quote_required_unverified", a.reasons)
        self.assertIsNot(a.level, RiskLevel.R3)


if __name__ == "__main__":
    unittest.main()
