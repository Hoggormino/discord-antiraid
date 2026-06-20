"""Tests for config (de)serialisation and validation."""

import unittest

from antiraid.config import GuildConfig, RaidAction


class TestConfigSerialisation(unittest.TestCase):
    def test_round_trip(self):
        cfg = GuildConfig(
            guild_id=123,
            trusted_roles=frozenset({1, 2, 3}),
            allowlist_users=frozenset({9}),
            raid_action=RaidAction.KICK,
            join_rate_threshold=10,
        )
        data = cfg.to_dict()
        restored = GuildConfig.from_dict(data)
        self.assertEqual(restored.guild_id, 123)
        self.assertEqual(restored.trusted_roles, frozenset({1, 2, 3}))
        self.assertEqual(restored.allowlist_users, frozenset({9}))
        self.assertEqual(restored.raid_action, RaidAction.KICK)
        self.assertEqual(restored.join_rate_threshold, 10)

    def test_to_dict_is_json_friendly(self):
        import json

        cfg = GuildConfig(guild_id=1, trusted_roles=frozenset({5, 6}))
        # Must serialise without a custom encoder.
        text = json.dumps(cfg.to_dict())
        again = GuildConfig.from_dict(json.loads(text))
        self.assertEqual(again.trusted_roles, frozenset({5, 6}))

    def test_from_dict_ignores_unknown_keys(self):
        cfg = GuildConfig.from_dict(
            {"guild_id": 7, "bogus_key": "ignored", "join_rate_threshold": 3}
        )
        self.assertEqual(cfg.guild_id, 7)
        self.assertEqual(cfg.join_rate_threshold, 3)

    def test_string_ids_coerced_to_int(self):
        # JSON sometimes carries IDs as strings.
        cfg = GuildConfig.from_dict(
            {"guild_id": 1, "trusted_roles": ["10", "20"]}
        )
        self.assertEqual(cfg.trusted_roles, frozenset({10, 20}))


class TestConfigValidation(unittest.TestCase):
    def test_valid_default_passes(self):
        GuildConfig(guild_id=1).validate()  # should not raise

    def test_zero_threshold_rejected(self):
        with self.assertRaises(ValueError):
            GuildConfig(guild_id=1, join_rate_threshold=0).validate()

    def test_nonpositive_window_rejected(self):
        with self.assertRaises(ValueError):
            GuildConfig(guild_id=1, msg_rate_window=0).validate()


if __name__ == "__main__":
    unittest.main()
