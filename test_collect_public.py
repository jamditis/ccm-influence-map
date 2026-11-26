import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.error import URLError

import collect_public


class CollectorTests(unittest.TestCase):
    def test_incomplete_mutual_check_keeps_follow_relations(self):
        kept = {"staff": "s1", "network": "Bluesky", "name": "Ari Example", "company": "", "account": "did:ari",
                "kind": "person", "relation": "mutual_follow", "event": ""}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "observations" / "bluesky.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"observations": [kept], "coverage": [], "staff_accounts": {"s1": "did:one"}}))
            with mock.patch.object(collect_public, "bsky_api", side_effect=URLError("offline")):
                collect_public.bluesky_mutual(Path(folder))
            saved = json.loads(path.read_text())
        self.assertEqual(saved["observations"], [kept])
        self.assertTrue(any("check failed" in line for line in saved["coverage"]))

if __name__ == "__main__":
    unittest.main()
