import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import limits
from core.store import Store

SETTINGS = {
    "reply": {"daily_limit": 3, "min_gap_minutes": 5},
    "recovery": {"circuit_breaker_threshold": 3},
}


def _settings(daily=3, gap=5):
    return {
        "reply": {"daily_limit": daily, "min_gap_minutes": gap},
        "recovery": {"circuit_breaker_threshold": 3},
    }


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "l.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _done(self, minutes_ago: float = 0):
        ts = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S%z"
        )
        with self.store.tx() as cur:
            cur.execute(
                "INSERT INTO tasks(task_id, platform, contact_id, dedupe_key, state, created_at, updated_at)"
                " VALUES(?,?,?,?,'DONE',?,?)",
                (f"t{minutes_ago}-{datetime.now().timestamp()}", "wechat", "c1", f"k{minutes_ago}", ts, ts),
            )


class TestReplyAllowed(LimitsTest):
    def test_first_reply_allowed(self):
        ok, why = limits.reply_allowed(self.store, _settings(), "wechat", "c1")
        self.assertTrue(ok, why)

    def test_min_gap_blocks(self):
        self._done(1)
        ok, why = limits.reply_allowed(self.store, _settings(), "wechat", "c1")
        self.assertFalse(ok)
        self.assertIn("min_gap", why)

    def test_gap_passed_allows(self):
        self._done(10)
        ok, why = limits.reply_allowed(self.store, _settings(), "wechat", "c1")
        self.assertTrue(ok, why)

    def test_daily_limit_blocks(self):
        self._done(30)
        self._done(60)
        self._done(90)
        ok, why = limits.reply_allowed(self.store, _settings(daily=3, gap=0), "wechat", "c1")
        self.assertFalse(ok)
        self.assertIn("daily_limit", why)

    def test_other_contact_not_affected(self):
        self._done(1)
        ok, _ = limits.reply_allowed(self.store, _settings(), "wechat", "c2")
        self.assertTrue(ok)


class TestCircuit(LimitsTest):
    def test_trips_after_threshold(self):
        self.assertFalse(limits.record_failure(self.store, SETTINGS, "wechat"))
        self.assertFalse(limits.record_failure(self.store, SETTINGS, "wechat"))
        self.assertTrue(limits.record_failure(self.store, SETTINGS, "wechat"))
        ok, why = limits.reply_allowed(self.store, SETTINGS, "wechat", "c1")
        self.assertFalse(ok)
        self.assertIn("circuit", why.lower())

    def test_success_resets(self):
        limits.record_failure(self.store, SETTINGS, "wechat")
        limits.record_success(self.store, "wechat")
        self.assertFalse(limits.record_failure(self.store, SETTINGS, "wechat"))
        self.assertFalse(limits.record_failure(self.store, SETTINGS, "wechat"))


if __name__ == "__main__":
    unittest.main()
