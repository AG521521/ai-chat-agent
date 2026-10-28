import unittest
from datetime import datetime, timedelta

from core import stats

BASE = datetime(2026, 10, 1, 9, 0, 0)


def _turn(i: int, role: str = "contact", text: str = "") -> dict:
    at = (BASE + timedelta(minutes=17 * i)).strftime("%Y-%m-%dT%H:%M:%S+0800")
    return {"role": role, "text": text or f"第{i}条消息", "at": at}


class TestStats(unittest.TestCase):
    def test_empty(self):
        s = stats.compute_stats([], "ns")
        self.assertEqual(s["total"], 0)
        self.assertIn("没有历史记录", s["note"])

    def test_counts_and_roles(self):
        turns = [_turn(i, "contact" if i % 2 == 0 else "self") for i in range(10)]
        s = stats.compute_stats(turns, "ns")
        self.assertEqual(s["total"], 10)
        self.assertEqual(s["from_contact"], 5)
        self.assertEqual(s["from_self"], 5)
        self.assertGreater(s["days_active"], 0)
        self.assertGreater(s["avg_chars"], 0)

    def test_top_terms(self):
        turns = [_turn(i, text="今天练了软度，软度有点酸") for i in range(5)]
        s = stats.compute_stats(turns, "ns")
        terms = dict(s["top_terms"])
        self.assertIn("软度", terms)

    def test_median_gap_present(self):
        turns = [_turn(i) for i in range(5)]
        s = stats.compute_stats(turns, "ns")
        self.assertIsNotNone(s["median_gap_minutes"])

    def test_render_mentions_partial_history(self):
        turns = [_turn(i) for i in range(3)]
        text = stats.render_stats(stats.compute_stats(turns, "ns"))
        self.assertIn("样本只覆盖部分历史", text)


class TestSampling(unittest.TestCase):
    def test_short_history_returned_as_is(self):
        turns = [_turn(i) for i in range(5)]
        self.assertEqual(stats.stratified_sample(turns, 12, 6), turns)

    def test_long_history_is_stratified(self):
        turns = [_turn(i) for i in range(100)]
        sample = stats.stratified_sample(turns, recent_limit=12, older_limit=6)
        self.assertLessEqual(len(sample), 18)
        self.assertGreater(len(sample), 0)
        # 必须包含最新一条
        self.assertEqual(sample[-1]["text"], turns[-1]["text"])
        # 且保持时间正序
        self.assertEqual(sample, sorted(sample, key=lambda t: t["at"]))

    def test_no_duplicates(self):
        turns = [_turn(i) for i in range(50)]
        sample = stats.stratified_sample(turns, 10, 5)
        keys = [(t["at"], t["text"]) for t in sample]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
