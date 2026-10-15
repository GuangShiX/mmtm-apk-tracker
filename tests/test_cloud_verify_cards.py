"""No-render tests for the isolated card-selection verification adapter."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_verify_cards as cards


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.characters = self.skills / "characters"
        self.characters.mkdir(parents=True)
        self.output = self.root / "cards"
        self.originals = {}
        for cid in (1, 2, 9):
            raw = ('{ "character": {"id": %d}, "marker": "original bytes %d" }\n' % (cid, cid)).encode()
            self.originals[cid] = raw
            (self.characters / f"{cid:06d}.json").write_bytes(raw)
        self.latest = b'{"latest_characters":[{"id":9}],"official":"unchanged"}\n'
        (self.skills / "latest.json").write_bytes(self.latest)

    def test_selected_set_uses_original_bytes_without_official_latest_mutation(self):
        calls = []
        def verify(view, output, *, language, mode):
            calls.append(view)
            self.assertNotEqual(view, self.skills)
            self.assertEqual(output, self.output)
            self.assertEqual((language, mode), ("ja-JP", "split"))
            latest = json.loads((view / "latest.json").read_bytes())
            self.assertEqual([row["id"] for row in latest["latest_characters"]], [2, 1])
            self.assertIn("verification-only", latest["selection"])
            self.assertEqual(sorted(path.name for path in (view / "characters").iterdir()), ["000001.json", "000002.json"])
            for cid in (2, 1):
                self.assertEqual((view / "characters" / f"{cid:06d}.json").read_bytes(), self.originals[cid])
            self.assertEqual((self.skills / "latest.json").read_bytes(), self.latest)
            return [output / "verified.png"]
        self.assertEqual(cards.verify_selected(self.skills, self.output, [2, 1], language="ja-JP", verifier=verify), [self.output / "verified.png"])
        self.assertFalse(calls[0].exists())
        self.assertEqual((self.skills / "latest.json").read_bytes(), self.latest)
        for cid, raw in self.originals.items():
            self.assertEqual((self.characters / f"{cid:06d}.json").read_bytes(), raw)

    def test_invalid_ids_fail_before_verifier(self):
        for ids in ([], [0], [-1], [True], ["1"], [1.0], [1, 1]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    cards.verify_selected(self.skills, self.output, ids, verifier=lambda *a, **k: self.fail("must not verify"))

    def test_missing_source_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            cards.verify_selected(self.skills, self.output, [99], verifier=lambda *a, **k: self.fail("must not verify"))

    def test_payload_identity_must_match_selection(self):
        (self.characters / "000001.json").write_text('{"character":{"id":2}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity"):
            cards.verify_selected(self.skills, self.output, [1], verifier=lambda *a, **k: self.fail("must not verify"))

    def test_verifier_failure_propagates_and_temporary_view_is_removed(self):
        views = []
        def fail(view, *args, **kwargs):
            views.append(view)
            raise RuntimeError("PNG hash mismatch")
        with self.assertRaisesRegex(RuntimeError, "PNG hash mismatch"):
            cards.verify_selected(self.skills, self.output, [1], verifier=fail)
        self.assertFalse(views[0].exists())
        self.assertEqual((self.skills / "latest.json").read_bytes(), self.latest)

    def test_cli_passes_requested_set_to_adapter(self):
        with patch("sys.argv", ["cloud_verify_cards.py", "--character-ids", "1,2", "--skills-dir", str(self.skills), "--output-dir", str(self.output)]), patch.object(cards, "verify_selected", return_value=[]) as verify:
            self.assertEqual(cards.main(), 0)
            verify.assert_called_once_with(self.skills, self.output, [1, 2], language="zh-CN")

    def test_cli_rejects_malformed_ids(self):
        with patch("sys.argv", ["cloud_verify_cards.py", "--character-ids", "1,,2", "--skills-dir", str(self.skills), "--output-dir", str(self.output)]), patch.object(cards, "verify_selected") as verify:
            with self.assertRaises(SystemExit) as result:
                cards.main()
            self.assertEqual(result.exception.code, 1)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
