"""Runs the full end-to-end environment simulation as part of the test suite.

Drives the real bot pipeline (gateway handlers -> engine -> action queue ->
token-bucket executor -> bulk-ban/lockdown/anti-nuke) against an in-memory
Discord world.  Skipped automatically if discord.py is not installed.
"""

import os
import tempfile
import unittest

try:
    import discord  # noqa: F401
    import simulate

    HAVE_DISCORD = True
except Exception:  # pragma: no cover
    HAVE_DISCORD = False


@unittest.skipUnless(HAVE_DISCORD, "discord.py not installed")
class TestEnvironmentSimulation(unittest.IsolatedAsyncioTestCase):
    async def test_full_environment_passes_all_checks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sim_configs.json")
            rec, checks = await simulate.run(verbose=False, config_path=path)

        failed = [label for label, passed in checks if not passed]
        self.assertEqual(failed, [], f"environment checks failed: {failed}")

        # spot-check the headline outcomes directly too
        self.assertEqual(rec.total_banned, 20)
        self.assertEqual(len(rec.bulk_bans), 1)           # 20 bans -> 1 request
        self.assertEqual(len(rec.strips), 1)              # rogue admin neutralised
        self.assertGreaterEqual(rec.throttle_elapsed, 0.6)  # rate limiter engaged


if __name__ == "__main__":
    unittest.main()
