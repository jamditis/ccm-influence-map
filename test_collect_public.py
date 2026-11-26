import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from urllib.error import URLError

import collect_public


class CollectorTests(unittest.TestCase):
    def test_failed_account_read_keeps_previous_collection(self):
        staff = [{"id": "s1", "name": "Staff One", "bluesky": "one.example"}]
        kept = {"staff": "s1", "network": "Bluesky", "name": "Ari Example", "company": "", "account": "did:ari",
                "kind": "person", "relation": "mutual_follow", "event": ""}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "observations" / "bluesky.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"observations": [kept], "coverage": [], "staff_accounts": {"s1": "did:one"}}))
            with mock.patch.object(collect_public, "bsky_rows", side_effect=URLError("offline")):
                collect_public.bluesky(staff, Path(folder))
            saved = json.loads(path.read_text())
        self.assertEqual((saved["observations"], saved["staff_accounts"]), ([kept], {"s1": "did:one"}))
        self.assertIn("Kept 1 observations", saved["coverage"][0])


if __name__ == "__main__":
    unittest.main()
