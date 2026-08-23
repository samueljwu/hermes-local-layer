from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path("/home/hermes")
JOBS = ROOT / ".hermes/cron/jobs.json"
WRAPPER = ROOT / ".hermes/scripts/feed_digest.sh"


class FeedCronIsolationTests(unittest.TestCase):
    def test_feed_cron_runs_harness_without_agent_tools(self):
        jobs = json.loads(JOBS.read_text(encoding="utf-8"))["jobs"]
        job = next(item for item in jobs if item["id"] == "4f1e659aad86")
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["script"], "feed_digest.sh")
        self.assertEqual(job["prompt"], "")
        self.assertIsNone(job["enabled_toolsets"])
        self.assertEqual(job["skills"], [])

        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("exec /home/hermes/feed/_tools/feed_ops.py digest", text)
        self.assertNotIn("hermes chat", text)


if __name__ == "__main__":
    unittest.main()
