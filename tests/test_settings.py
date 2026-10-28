import copy
import json
import tempfile
import unittest
from pathlib import Path

from core import cli
from core.memory import Memory
from core.store import Store

# 自包含的示例配置（不含任何真实联系人）
EXAMPLE = {
    "reply": {
        "allowlisted_contacts": {
            "wechat": ["小A", "小A（备注）", "测试号"],
            "douyin": ["小A", "测试号"],
        }
    },
    "memory": {
        "link_groups": [
            {
                "id": "same-person",
                "members": [["wechat", "小A"], ["wechat", "小A（备注）"], ["douyin", "小A"]],
            },
            {
                "id": "test-user",
                "members": [["wechat", "测试号"], ["douyin", "测试号"]],
            },
        ]
    },
}


def example_settings() -> dict:
    return cli.deep_merge(cli.SETTINGS_DEFAULTS, copy.deepcopy(EXAMPLE))


class TestSettingsMerge(unittest.TestCase):
    def test_local_overrides_base_without_losing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "config" / "settings.json").write_text(
                json.dumps(
                    {
                        "reply": {
                            "min_confidence": 0.9,
                            "allowlisted_contacts": {"wechat": ["a"], "douyin": []},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (base / "config" / "settings.local.json").write_text(
                json.dumps(
                    {"reply": {"allowlisted_contacts": {"wechat": ["b"], "douyin": ["c"]}}}
                ),
                encoding="utf-8",
            )
            s = cli.load_settings(base)
            self.assertEqual(s["reply"]["allowlisted_contacts"]["wechat"], ["b"])
            self.assertEqual(s["reply"]["allowlisted_contacts"]["douyin"], ["c"])
            self.assertEqual(s["reply"]["min_confidence"], 0.9)
            self.assertTrue(s["app"]["dry_run"])

    def test_local_file_absent_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = cli.load_settings(Path(tmp))
            self.assertEqual(s["reply"]["allowlisted_contacts"]["wechat"], [])


class TestSamePersonLinking(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "x.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_configured_aliases_share_one_namespace(self):
        mem = Memory(self.store, example_settings())
        self.assertEqual(mem.namespace("wechat", "小A"), "linked:same-person")
        self.assertEqual(mem.namespace("wechat", "小A（备注）"), "linked:same-person")
        self.assertEqual(mem.namespace("douyin", "小A"), "linked:same-person")
        mem.store.add_turn("linked:same-person", "wechat", "小A", "contact", "微信里说的话")
        self.assertEqual(len(mem.recent("douyin", "小A")), 1)

    def test_stranger_stays_isolated(self):
        mem = Memory(self.store, example_settings())
        self.assertEqual(mem.namespace("wechat", "另一个人"), "wechat:另一个人")
        mem.store.add_turn("linked:same-person", "douyin", "小A", "contact", "共享内容")
        self.assertEqual(len(mem.recent("wechat", "另一个人")), 0)

    def test_remark_with_suffix_still_links(self):
        mem = Memory(self.store, example_settings())
        self.assertEqual(mem.namespace("wechat", "小A（备注）"), "linked:same-person")
        self.assertEqual(mem.namespace("wechat", " 小A （备注） "), "linked:same-person")

    def test_allowlist_accepts_remark_suffix(self):
        settings = example_settings()
        self.assertTrue(cli.is_allowlisted(settings, "wechat", "小A（备注）"))
        self.assertTrue(cli.is_allowlisted(settings, "wechat", "小A"))
        self.assertTrue(cli.is_allowlisted(settings, "douyin", "测试号"))
        self.assertFalse(cli.is_allowlisted(settings, "wechat", "危险源"))
        self.assertFalse(cli.is_allowlisted(settings, "wechat", ""))
        self.assertTrue(cli.is_allowlisted(settings, "douyin", "小A（备注）"))
        self.assertFalse(cli.is_allowlisted(settings, "douyin", "小A粉丝"))
        self.assertFalse(cli.is_allowlisted(settings, "wechat", "小A的小号粉丝"))


if __name__ == "__main__":
    unittest.main()
