"""Tests for durable incident persistence (active lockdowns across restarts)."""

import os
import tempfile
import unittest

from antiraid.incident_store import Incident, IncidentStore


class TestIncident(unittest.TestCase):
    def test_round_trip(self):
        inc = Incident(
            guild_id=7,
            lockdown_active=True,
            raid_active=True,
            raid_started_at=100.0,
            last_raid_signal=150.0,
            channel_snapshot={11: None, 22: True, 33: False},
        )
        again = Incident.from_dict(inc.to_dict())
        self.assertEqual(again, inc)
        # snapshot keys survive the JSON str-key round trip as ints
        self.assertEqual(again.channel_snapshot[11], None)
        self.assertEqual(again.channel_snapshot[22], True)
        self.assertEqual(again.channel_snapshot[33], False)


class TestIncidentStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "inc.json")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def test_persist_and_reload(self):
        store = IncidentStore(self.path)
        store.set(Incident(guild_id=1, lockdown_active=True,
                           channel_snapshot={5: None, 6: False}))
        reloaded = IncidentStore(self.path)
        inc = reloaded.get(1)
        self.assertIsNotNone(inc)
        self.assertTrue(inc.lockdown_active)
        self.assertEqual(inc.channel_snapshot, {5: None, 6: False})

    def test_active_guild_ids_only_locked(self):
        store = IncidentStore(self.path)
        store.set(Incident(guild_id=1, lockdown_active=True))
        store.set(Incident(guild_id=2, lockdown_active=False))
        self.assertEqual(store.active_guild_ids(), [1])

    def test_clear(self):
        store = IncidentStore(self.path)
        store.set(Incident(guild_id=1, lockdown_active=True))
        store.clear(1)
        self.assertIsNone(store.get(1))
        self.assertEqual(IncidentStore(self.path).get(1), None)

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{garbage")
        store = IncidentStore(self.path)  # must not raise
        self.assertEqual(store.active_guild_ids(), [])

    def test_atomic_save_no_temp_files(self):
        store = IncidentStore(self.path)
        store.set(Incident(guild_id=1, lockdown_active=True))
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
