import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from core import cli


class TestCliSelfTest(unittest.TestCase):
    def test_selftest_passes(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["selftest"])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 0, msg=data)
        self.assertTrue(data["ok"], msg=data)

    def test_selftest_covers_key_guarantees(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.main(["selftest"])
        steps = {s["step"]: s for s in json.loads(out.getvalue())["steps"]}
        self.assertEqual(steps["observe_again"]["duplicates"], 1)
        self.assertEqual(steps["send_check_input_cleared_only"]["status"], "unknown")
        self.assertEqual(steps["fallback_is_not_r0"]["risk"], "R1")
        self.assertEqual(steps["risky"]["risk"], "R2")

    def test_config_check(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["config-check"])
        data = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertIsInstance(data["dry_run"], bool)
        self.assertIn(data["mode"], ("manual", "auto_with_risk_gate"))
        self.assertEqual(data["persona_id"], "relationship_assistant_v1")


if __name__ == "__main__":
    unittest.main()
