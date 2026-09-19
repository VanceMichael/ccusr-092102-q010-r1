"""检查共创贡献样例。"""

import json
from pathlib import Path
import unittest


class ContributionFixtureTest(unittest.TestCase):
    def test_contribution_uses_alias_and_sequence(self) -> None:
        payload = json.loads(Path("fixtures/contribution.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["participant_alias"].startswith("child-"))
        self.assertGreater(payload["device_sequence"], 0)
        self.assertTrue(payload["consent_scopes"])


if __name__ == "__main__":
    unittest.main()
