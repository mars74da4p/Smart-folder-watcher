import tempfile
import unittest
from pathlib import Path

from smart_folder_watcher import Rule, rule_matches, unique_destination


class RuleMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_matches_extension_and_size(self):
        path = self.root / "photo.JPG"
        path.write_bytes(b"abc")
        rule = Rule("images", self.root / "images", extensions=(".jpg",), min_size=3, max_size=3)
        self.assertTrue(rule_matches(path, rule))

    def test_rejects_file_that_does_not_match_all_conditions(self):
        path = self.root / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        rule = Rule("images", self.root / "images", extensions=(".jpg",), filename_contains=("photo",))
        self.assertFalse(rule_matches(path, rule))

    def test_unique_destination_adds_suffix(self):
        existing = self.root / "report.pdf"
        existing.touch()
        (self.root / "report (1).pdf").touch()
        self.assertEqual(unique_destination(existing).name, "report (2).pdf")


if __name__ == "__main__":
    unittest.main()
