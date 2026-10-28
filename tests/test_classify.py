import json
import unittest

from core.classify import Classifier, ClassifyError, parse_analysis

GOOD = {
    "reply_text": "记得吃饭",
    "intent": "daily_sharing",
    "risk_level": "R0",
    "confidence": 0.94,
    "uncertain": False,
    "requires_human": False,
    "reason_codes": [],
    "media_understanding": "full",
    "suggested_reply_strategy": "warm_conversational",
}


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


class TestParseAnalysis(unittest.TestCase):
    def test_valid(self):
        a = parse_analysis(dump(GOOD))
        self.assertEqual(a.risk_level.value, "R0")
        self.assertEqual(a.confidence, 0.94)
        self.assertEqual(a.raw["reply_text"], "记得吃饭")

    def test_code_fence_tolerated(self):
        a = parse_analysis("```json\n" + dump(GOOD) + "\n```")
        self.assertEqual(a.risk_level.value, "R0")

    def test_missing_field_rejected(self):
        bad = dict(GOOD)
        bad.pop("risk_level")
        with self.assertRaises(ClassifyError):
            parse_analysis(dump(bad))

    def test_illegal_risk_rejected(self):
        bad = dict(GOOD, risk_level="R9")
        with self.assertRaises(ClassifyError):
            parse_analysis(dump(bad))

    def test_confidence_out_of_range(self):
        bad = dict(GOOD, confidence=1.7)
        with self.assertRaises(ClassifyError):
            parse_analysis(dump(bad))

    def test_wrong_type_rejected(self):
        bad = dict(GOOD, uncertain="no")
        with self.assertRaises(ClassifyError):
            parse_analysis(dump(bad))

    def test_bad_media_value_rejected(self):
        bad = dict(GOOD, media_understanding="maybe")
        with self.assertRaises(ClassifyError):
            parse_analysis(dump(bad))

    def test_empty_rejected(self):
        with self.assertRaises(ClassifyError):
            parse_analysis("")

    def test_not_json_rejected(self):
        with self.assertRaises(ClassifyError):
            parse_analysis("我觉得这条挺正常的")


class TestClassifierRetry(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(payload, settings):
            calls["n"] += 1
            if calls["n"] < 2:
                return "不是 JSON"
            return dump(GOOD)

        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 2}},
                       base_dir=__import__("pathlib").Path("."),
                       transport=flaky, sleep=lambda _s: None)
        a = c.analyze(_msg(), {}, [], [])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(a.risk_level.value, "R0")

    def test_all_attempts_fail_raises(self):
        def broken(payload, settings):
            return "{}"

        c = Classifier({"llm": {}, "recovery": {"max_analysis_retries": 1}},
                       base_dir=__import__("pathlib").Path("."),
                       transport=broken, sleep=lambda _s: None)
        with self.assertRaises(ClassifyError):
            c.analyze(_msg(), {}, [], [])


def _msg():
    from tests.helpers import msg

    return msg()


if __name__ == "__main__":
    unittest.main()
