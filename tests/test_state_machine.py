import tempfile
import unittest
from pathlib import Path

from core.model import Decision, RiskLevel, State
from core.store import Store, StoreError
from tests.helpers import msg


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _observed(self) -> str:
        return self.store.observe(msg(text="今天好累啊")).task_id

    def _walk(self, task_id: str, *states: State) -> None:
        for s in states:
            self.store.set_state(task_id, s, "test")


class TestObserve(StoreTest):
    def test_duplicate_observe_returns_same_task(self):
        first = self.store.observe(msg(text="同一条消息"))
        second = self.store.observe(msg(text="同一条消息"))
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_native_id_wins(self):
        a = self.store.observe(msg(text="文本 A", source_message_id="id-9"))
        b = self.store.observe(msg(text="文本 B", source_message_id="id-9"))
        self.assertEqual(a.task_id, b.task_id)
        self.assertFalse(b.created)

    def test_different_contacts_do_not_collide(self):
        a = self.store.observe(msg(text="同样的话", contact_id="c1"))
        b = self.store.observe(msg(text="同样的话", contact_id="c2"))
        self.assertNotEqual(a.task_id, b.task_id)


class TestTransitions(StoreTest):
    def test_illegal_transition_rejected(self):
        task_id = self._observed()
        with self.assertRaises(StoreError):
            self.store.set_state(task_id, State.DONE, "不该允许")

    def test_idempotent_same_state(self):
        task_id = self._observed()
        self.store.set_state(task_id, State.VALIDATING, "ok")
        self.store.set_state(task_id, State.VALIDATING, "again")
        self.assertEqual(self.store.get_task(task_id)["state"], "VALIDATING")

    def test_happy_path_to_done(self):
        task_id = self._observed()
        self._walk(
            task_id,
            State.VALIDATING,
            State.ANALYZING,
            State.DECIDING,
            State.PREPARING_REPLY,
            State.READY_TO_SEND,
        )
        state = self.store.mark_sent(task_id, "verified", "ui_verified", "bubble")
        self.assertIs(state, State.DONE)

    def test_send_unknown_cannot_be_resent_directly(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING,
                   State.PREPARING_REPLY, State.READY_TO_SEND, State.SENDING)
        state = self.store.mark_sent(task_id, "unknown", "ui_observed", "editor cleared only")
        self.assertIs(state, State.SEND_UNKNOWN)
        with self.assertRaises(StoreError):
            self.store.set_state(task_id, State.SENDING, "直接重发")
        # 只能经过复核后转 ERROR_HOLD / DONE / FAILED_CONFIRMED
        self.store.set_state(task_id, State.ERROR_HOLD, "人工复核")

    def test_failed_send_is_terminal(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING,
                   State.PREPARING_REPLY, State.READY_TO_SEND, State.SENDING)
        state = self.store.mark_sent(task_id, "failed", "ui_observed", "editor kept text")
        self.assertIs(state, State.FAILED_CONFIRMED)
        with self.assertRaises(StoreError):
            self.store.set_state(task_id, State.SENDING, "retry")


class TestApproval(StoreTest):
    def test_approve_moves_to_preparing(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING, State.WAITING_APPROVAL)
        self.store.enqueue_approval(task_id)
        pending = self.store.pending_approvals()
        self.assertEqual(len(pending), 1)
        self.store.decide_approval(task_id, True, "tester")
        self.assertEqual(self.store.get_task(task_id)["state"], "PREPARING_REPLY")
        self.assertEqual(len(self.store.pending_approvals()), 0)

    def test_reject_cancels(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING, State.WAITING_APPROVAL)
        self.store.enqueue_approval(task_id)
        self.store.decide_approval(task_id, False, "tester", "不合适")
        self.assertEqual(self.store.get_task(task_id)["state"], "CANCELLED")

    def test_double_decision_rejected(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING, State.WAITING_APPROVAL)
        self.store.enqueue_approval(task_id)
        self.store.decide_approval(task_id, True, "tester")
        with self.assertRaises(StoreError):
            self.store.decide_approval(task_id, True, "tester")


class TestPause(StoreTest):
    def test_platform_and_global(self):
        self.assertFalse(self.store.is_paused("wechat"))
        self.store.set_pause("douyin", True)
        self.assertTrue(self.store.is_paused("douyin"))
        self.assertFalse(self.store.is_paused("wechat"))
        self.store.set_pause("all", True)
        self.assertTrue(self.store.is_paused("wechat"))


class TestDecisionWrite(StoreTest):
    def test_apply_decision_requires_reachable_state(self):
        task_id = self._observed()
        decision = Decision(State.WAITING_APPROVAL, ["manual"], RiskLevel.R1, True)
        with self.assertRaises(StoreError):
            self.store.apply_decision(task_id, decision)

    def test_apply_decision_after_walk(self):
        task_id = self._observed()
        self._walk(task_id, State.VALIDATING, State.ANALYZING, State.DECIDING)
        decision = Decision(State.WAITING_APPROVAL, ["manual"], RiskLevel.R1, True)
        self.store.apply_decision(task_id, decision, draft="草稿")
        task = self.store.get_task(task_id)
        self.assertEqual(task["state"], "WAITING_APPROVAL")
        self.assertEqual(task["draft_text"], "草稿")


if __name__ == "__main__":
    unittest.main()
