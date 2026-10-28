import copy
import unittest

from core import verify
from tests.helpers import SETTINGS


class TestPreSendBlockers(unittest.TestCase):
    def _task(self, **kw):
        base = {
            "state": "READY_TO_SEND",
            "contact_verified": 1,
            "allowlisted": 1,
            "draft_text": "那你早点休息，别硬撑",
            "requires_quote": 0,
            "quote_target_verified": 0,
            "last_error": None,
        }
        base.update(kw)
        return base

    def test_dry_run_blocks(self):
        blockers = verify.pre_send_blockers(self._task(), SETTINGS)
        self.assertIn("dry_run", blockers)

    def test_ready_with_send_enabled_has_no_blockers(self):
        live = copy.deepcopy(SETTINGS)
        live["app"]["dry_run"] = False
        self.assertEqual(verify.pre_send_blockers(self._task(), live), [])

    def test_quote_required_but_unverified(self):
        live = copy.deepcopy(SETTINGS)
        live["app"]["dry_run"] = False
        blockers = verify.pre_send_blockers(self._task(requires_quote=1), live)
        self.assertIn("quote_unverified", blockers)
        self.assertIn("quote_verification_required", blockers)

    def test_empty_draft_blocks(self):
        live = copy.deepcopy(SETTINGS)
        live["app"]["dry_run"] = False
        blockers = verify.pre_send_blockers(self._task(draft_text="   "), live)
        self.assertIn("empty_draft", blockers)

    def test_state_must_be_ready(self):
        live = copy.deepcopy(SETTINGS)
        live["app"]["dry_run"] = False
        blockers = verify.pre_send_blockers(self._task(state="WAITING_APPROVAL"), live)
        self.assertTrue(any(b.startswith("state_not_ready") for b in blockers))

    def test_unverified_contact_blocks(self):
        live = copy.deepcopy(SETTINGS)
        live["app"]["dry_run"] = False
        blockers = verify.pre_send_blockers(self._task(contact_verified=0), live)
        self.assertIn("contact_unverified", blockers)


class TestQuoteVerification(unittest.TestCase):
    def test_platform_id_match(self):
        check = verify.verify_quote(
            {"target_id": "vid-42"}, {"preview_id": "vid-42"}, SETTINGS
        )
        self.assertTrue(check.ok)
        self.assertEqual(check.level, "platform_id")

    def test_media_hash_match_is_ui_verified(self):
        check = verify.verify_quote(
            {"hash": "a1b2c3d4e5f6a7b8"}, {"hash": "a1b2c3d4e5f6a7b8"}, SETTINGS
        )
        self.assertTrue(check.ok)
        self.assertEqual(check.level, "ui_verified")

    def test_hash_mismatch_blocks(self):
        check = verify.verify_quote(
            {"hash": "aaaaaaaaaaaaaaaa"}, {"hash": "bbbbbbbbbbbbbbbb"}, SETTINGS
        )
        self.assertFalse(check.ok)
        self.assertIn("hash_mismatch", check.reasons)

    def test_quote_chip_text_match(self):
        check = verify.verify_quote(
            {"text": "这个红烧肉看着真香", "author": "她"},
            {"quote_chip": True, "preview_text": "回复消息 这个红烧肉看着真香", "author": "她"},
            SETTINGS,
        )
        self.assertTrue(check.ok)
        self.assertEqual(check.level, "ui_verified")

    def test_quote_chip_text_mismatch_blocks(self):
        check = verify.verify_quote(
            {"text": "红烧肉"},
            {"quote_chip": True, "preview_text": "回复消息 别的东西"},
            SETTINGS,
        )
        self.assertFalse(check.ok)

    def test_author_mismatch_blocks(self):
        check = verify.verify_quote(
            {"text": "红烧肉", "author": "她"},
            {"quote_chip": True, "preview_text": "回复消息 红烧肉", "author": "别人"},
            SETTINGS,
        )
        self.assertFalse(check.ok)
        self.assertIn("author_mismatch", check.reasons)

    def test_no_evidence_blocks(self):
        check = verify.verify_quote({}, {}, SETTINGS)
        self.assertFalse(check.ok)
        self.assertEqual(check.level, "unverified")


class TestSendClassification(unittest.TestCase):
    def test_error_is_failed(self):
        self.assertEqual(verify.classify_send({"error": "boom"}).status, "failed")

    def test_input_cleared_only_is_unknown(self):
        # 这是抖音历史上踩过的坑：输入框空了不等于发出去了
        self.assertEqual(verify.classify_send({"input_cleared": True}).status, "unknown")

    def test_bubble_plus_quote_is_verified(self):
        r = verify.classify_send({"input_cleared": True, "bubble_seen": True, "quote_seen": True})
        self.assertEqual(r.status, "verified")
        self.assertTrue(r.ok)

    def test_preview_plus_cleared_is_verified(self):
        r = verify.classify_send({"input_cleared": True, "preview_seen": True})
        self.assertEqual(r.status, "verified")

    def test_editor_kept_text_is_failed(self):
        self.assertEqual(verify.classify_send({"input_cleared": False}).status, "failed")

    def test_verified_evidence_is_ui_level(self):
        r = verify.classify_send({"bubble_seen": True})
        self.assertEqual(r.evidence.value, "ui_verified")


if __name__ == "__main__":
    unittest.main()
