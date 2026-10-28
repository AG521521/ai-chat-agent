import json
import unittest
from pathlib import Path

from core.classify import Classifier, ClassifyError, parse_draft
from tests.helpers import msg

JUDGE = {
    "intent": "日常分享",
    "risk_level": "R0",
    "confidence": 0.93,
    "uncertain": False,
    "requires_human": False,
    "reason_codes": [],
    "media_understanding": "unavailable",
}


def make_transport(judge_payload=None, draft_text="记得吃饭。"):
    seen = {"judge": 0, "draft": 0}

    def transport(payload, settings):
        system = payload["messages"][0]["content"]
        if "风险判官" in system:
            seen["judge"] += 1
            return json.dumps(judge_payload or JUDGE, ensure_ascii=False)
        seen["draft"] += 1
        return draft_text

    return transport, seen


class TestParseDraft(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(parse_draft("  记得吃饭。 "), "记得吃饭。")

    def test_quoted_text(self):
        self.assertEqual(parse_draft('"记得吃饭。"'), "记得吃饭。")

    def test_json_wrapped(self):
        self.assertEqual(
            parse_draft('{"reply_text": "记得吃饭。"}'), "记得吃饭。"
        )

    def test_code_fence(self):
        self.assertEqual(parse_draft("```\n记得吃饭。\n```"), "记得吃饭。")

    def test_empty_rejected(self):
        with self.assertRaises(ClassifyError):
            parse_draft("   ")


class TestTwoStep(unittest.TestCase):
    def test_judge_then_draft(self):
        transport, seen = make_transport()
        c = Classifier(
            {"llm": {}, "recovery": {"max_analysis_retries": 0}},
            base_dir=Path("."),
            transport=transport,
            sleep=lambda _s: None,
        )
        analysis = c.judge(msg(), {}, [], [])
        self.assertEqual(analysis.risk_level.value, "R0")
        text = c.draft(msg(), analysis, {}, [], [])
        self.assertEqual(text, "记得吃饭。")
        self.assertEqual(seen, {"judge": 1, "draft": 1})
        self.assertEqual(c.calls, 2)

    def test_judge_payload_without_reply_text_is_valid(self):
        # 判断阶段不该要求模型写回复
        transport, _ = make_transport()
        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 0}},
                       base_dir=Path("."), transport=transport, sleep=lambda _s: None)
        analysis = c.judge(msg(), {}, [], [])
        self.assertEqual(analysis.source, "model")

    def test_judge_invalid_json_raises(self):
        def bad(payload, settings):
            return "我觉得没事"

        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 0}},
                       base_dir=Path("."), transport=bad, sleep=lambda _s: None)
        with self.assertRaises(ClassifyError):
            c.judge(msg(), {}, [], [])

    def test_draft_can_be_json(self):
        transport, _ = make_transport(draft_text='{"reply_text": "早点睡"}')
        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 0}},
                       base_dir=Path("."), transport=transport, sleep=lambda _s: None)
        a = c.judge(msg(), {}, [], [])
        self.assertEqual(c.draft(msg(), a, {}, [], []), "早点睡")


if __name__ == "__main__":
    unittest.main()
