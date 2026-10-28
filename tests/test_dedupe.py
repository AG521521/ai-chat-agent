import unittest

from core import dedupe
from tests.helpers import msg


class TestNormalize(unittest.TestCase):
    def test_fullwidth_and_space(self):
        self.assertEqual(dedupe.normalize("ＡＢＣ　１２３"), "ABC 123")

    def test_zero_width_removed(self):
        self.assertEqual(dedupe.normalize("你\u200b好"), "你好")

    def test_content_fingerprint_ignores_punctuation(self):
        self.assertEqual(
            dedupe.content_fingerprint("你好，世界！"),
            dedupe.content_fingerprint("你好世界"),
        )


class TestDedupeKey(unittest.TestCase):
    def test_same_native_id_same_key(self):
        a = msg(source_message_id="m-1", text="第一句")
        b = msg(source_message_id="m-1", text="完全不同的文本也不影响 ID 键")
        self.assertEqual(dedupe.dedupe_key(a), dedupe.dedupe_key(b))

    def test_different_native_id_different_key(self):
        a = msg(source_message_id="m-1")
        b = msg(source_message_id="m-2")
        self.assertNotEqual(dedupe.dedupe_key(a), dedupe.dedupe_key(b))

    def test_ui_key_stable_across_whitespace(self):
        a = msg(text="今天  教了 新动作")
        b = msg(text="今天 教了 新动作\n")
        self.assertEqual(dedupe.dedupe_key(a), dedupe.dedupe_key(b))

    def test_ui_key_changes_with_media(self):
        a = msg(content_type="video", text="", media_path=r"C:\x\a.mp4")
        b = msg(content_type="video", text="", media_path=r"C:\x\b.mp4")
        self.assertNotEqual(dedupe.dedupe_key(a), dedupe.dedupe_key(b))

    def test_contact_isolation(self):
        a = msg(text="同一句话", contact_id="c1")
        b = msg(text="同一句话", contact_id="c2")
        self.assertNotEqual(dedupe.dedupe_key(a), dedupe.dedupe_key(b))

    def test_key_shape(self):
        self.assertTrue(dedupe.dedupe_key(msg(source_message_id="x")).startswith("m_"))
        self.assertTrue(dedupe.dedupe_key(msg()).startswith("c_"))


class TestDuplicateHint(unittest.TestCase):
    def test_confirmed_on_same_fingerprint(self):
        self.assertEqual(dedupe.duplicate_hint("你到家了吗？", "你到家了吗"), "confirmed")

    def test_possible_on_close_text(self):
        self.assertEqual(
            dedupe.duplicate_hint("今天上课好累啊", "今天上课真的好累啊"), "possible"
        )

    def test_none_on_unrelated(self):
        self.assertEqual(dedupe.duplicate_hint("记得吃饭", "明天几点"), "none")


if __name__ == "__main__":
    unittest.main()
