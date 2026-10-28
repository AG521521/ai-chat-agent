import json
import unittest
from pathlib import Path

from core import desire
from core.classify import Classifier, ClassifyError

GOOD = {
    "content_summary": "一个投资人讲财务自由",
    "why_she_sent": "她最近在焦虑钱和未来（推断）",
    "what_she_wants": ["被理解", "想让你表态"],
    "emotional_need": "需要被稳稳接住",
    "suggested_move": "先认同她的焦虑，再说你会一起扛",
    "avoid": ["讲道理", "敷衍"],
    "confidence": 0.7,
    "uncertain": False,
}


class TestParseDesire(unittest.TestCase):
    def test_valid(self):
        d = desire.parse_desire(json.dumps(GOOD, ensure_ascii=False))
        self.assertEqual(d["what_she_wants"], ["被理解", "想让你表态"])
        self.assertEqual(d["confidence"], 0.7)

    def test_code_fence(self):
        d = desire.parse_desire("```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")
        self.assertIn("content_summary", d)

    def test_missing_field(self):
        bad = dict(GOOD)
        bad.pop("suggested_move")
        with self.assertRaises(ValueError):
            desire.parse_desire(json.dumps(bad, ensure_ascii=False))

    def test_confidence_out_of_range(self):
        bad = dict(GOOD, confidence=1.5)
        with self.assertRaises(ValueError):
            desire.parse_desire(json.dumps(bad, ensure_ascii=False))

    def test_empty(self):
        with self.assertRaises(ValueError):
            desire.parse_desire("")

    def test_not_json(self):
        with self.assertRaises(ValueError):
            desire.parse_desire("我觉得她想要被哄")


class TestRender(unittest.TestCase):
    def test_marks_as_inference(self):
        text = desire.render_for_draft(GOOD)
        self.assertIn("推断", text)
        self.assertIn("被理解", text)
        self.assertIn("不要复述视频内容", text)

    def test_empty(self):
        self.assertEqual(desire.render_for_draft({}), "")


class TestClassifierDesire(unittest.TestCase):
    def test_uses_desire_prompt(self):
        seen = {}

        def transport(payload, settings):
            seen["system"] = payload["messages"][0]["content"]
            return json.dumps(GOOD, ensure_ascii=False)

        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 0}},
                       base_dir=Path("."), transport=transport, sleep=lambda _s: None)
        from tests.helpers import msg

        d = c.desire(msg(content_type="video", media_summary="财务自由"), {}, [], [])
        self.assertIn("情感分析师", seen["system"])
        self.assertEqual(d["content_summary"], "一个投资人讲财务自由")

    def test_invalid_output_raises(self):
        def transport(payload, settings):
            return "她想要被哄"

        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 0}},
                       base_dir=Path("."), transport=transport, sleep=lambda _s: None)
        from tests.helpers import msg

        with self.assertRaises(ClassifyError):
            c.desire(msg(content_type="video"), {}, [], [])


if __name__ == "__main__":
    unittest.main()
