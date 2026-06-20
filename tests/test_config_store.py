"""Tests for JSON-backed config persistence."""

import os
import tempfile
import unittest

from antiraid.config import GuildConfig, RaidAction
from antiraid.config_store import ConfigStore


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cfg.json")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def test_set_then_reload_persists(self):
        store = ConfigStore(self.path)
        store.set(GuildConfig(guild_id=42, raid_action=RaidAction.KICK,
                              trusted_roles=frozenset({1, 2})))
        # Fresh store reading the same file.
        reloaded = ConfigStore(self.path)
        cfg = reloaded.get(42)
        self.assertEqual(cfg.raid_action, RaidAction.KICK)
        self.assertEqual(cfg.trusted_roles, frozenset({1, 2}))

    def test_get_unknown_returns_default(self):
        store = ConfigStore(self.path)
        cfg = store.get(999)
        self.assertEqual(cfg.guild_id, 999)
        self.assertTrue(cfg.enabled)

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        store = ConfigStore(self.path)  # must not raise
        self.assertEqual(store.get(1).guild_id, 1)

    def test_atomic_save_leaves_no_temp_files(self):
        store = ConfigStore(self.path)
        store.set(GuildConfig(guild_id=7))
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_bad_entry_skipped_others_kept(self):
        import json
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "1": {"join_rate_threshold": 5},
                    "2": {"join_rate_threshold": 0},  # invalid -> skipped
                },
                fh,
            )
        store = ConfigStore(self.path)
        self.assertEqual(store.get(1).join_rate_threshold, 5)
        # guild 2's bad entry was dropped -> falls back to default
        self.assertEqual(store.get(2).join_rate_threshold,
                         GuildConfig().join_rate_threshold)


if __name__ == "__main__":
    unittest.main()
