import copy
import tempfile
import unittest
from pathlib import Path

from core.memory import Memory
from core.store import Store
from tests.helpers import SETTINGS


class MemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")
        self.settings = copy.deepcopy(SETTINGS)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestIsolation(MemoryTest):
    def test_namespaces_are_per_platform_and_contact(self):
        mem = Memory(self.store, self.settings)
        self.assertEqual(mem.namespace("wechat", "c1"), "wechat:c1")
        self.assertEqual(mem.namespace("douyin", "c1"), "douyin:c1")

    def test_turns_do_not_leak_across_platforms(self):
        mem = Memory(self.store, self.settings)
        mem.store.add_turn("wechat:c1", "wechat", "c1", "contact", "微信里说的话")
        self.assertEqual(len(mem.recent("wechat", "c1")), 1)
        self.assertEqual(len(mem.recent("douyin", "c1")), 0)

    def test_turns_do_not_leak_across_contacts(self):
        mem = Memory(self.store, self.settings)
        mem.store.add_turn("wechat:c1", "wechat", "c1", "contact", "给 c1 的话")
        self.assertEqual(len(mem.recent("wechat", "c2")), 0)

    def test_facts_isolated(self):
        mem = Memory(self.store, self.settings)
        mem.remember_fact("wechat", "c1", "她年底有考试")
        self.assertEqual(len(mem.facts("wechat", "c1")), 1)
        self.assertEqual(len(mem.facts("douyin", "c1")), 0)

    def test_clear_only_clears_that_contact(self):
        mem = Memory(self.store, self.settings)
        mem.store.add_turn("wechat:c1", "wechat", "c1", "contact", "a")
        mem.store.add_turn("wechat:c2", "wechat", "c2", "contact", "b")
        mem.clear("wechat", "c1")
        self.assertEqual(len(mem.recent("wechat", "c1")), 0)
        self.assertEqual(len(mem.recent("wechat", "c2")), 1)


class TestExplicitLinking(MemoryTest):
    def test_link_group_shares_memory(self):
        settings = copy.deepcopy(SETTINGS)
        settings["memory"]["link_groups"] = [
            {"id": "same-person", "members": [["wechat", "c1"], ["douyin", "d1"]]}
        ]
        mem = Memory(self.store, settings)
        self.assertEqual(mem.namespace("wechat", "c1"), "linked:same-person")
        self.assertEqual(mem.namespace("douyin", "d1"), "linked:same-person")
        mem.store.add_turn("linked:same-person", "wechat", "c1", "contact", "共享的事实")
        self.assertEqual(len(mem.recent("douyin", "d1")), 1)

    def test_no_link_group_means_no_sharing(self):
        mem = Memory(self.store, self.settings)
        mem.store.add_turn("wechat:c1", "wechat", "c1", "contact", "不共享")
        self.assertEqual(len(mem.recent("douyin", "c1")), 0)

    def test_recent_respects_limit(self):
        mem = Memory(self.store, self.settings)
        for i in range(30):
            mem.store.add_turn("wechat:c1", "wechat", "c1", "contact", f"第{i}句")
        self.assertEqual(len(mem.recent("wechat", "c1", limit=5)), 5)
        self.assertEqual(len(mem.recent("wechat", "c1")), 20)


if __name__ == "__main__":
    unittest.main()
