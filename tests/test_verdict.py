"""Offline tests for tools/verdict.py. Run as a plain script or with unittest."""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verdict  # noqa: E402

ITEM = {
    "contract": "tic-feed/2", "id": "verdict:test:one", "program": "verdict", "kind": "forecast", "tier": "routine",
    "title": "Test event happens", "summary": "60% chance the test event happens by 6 Nov 2026.",
    "status": "draft", "msg_type": "new", "human_review": "full", "us_person_data": "none",
    "status_class": "assessed", "product_category": "estimative", "countries": [],
    "data_as_of": "2026-10-31T18:00:00Z", "published_at": "2026-10-31T18:00:00Z",
    "valid_until": "2026-11-06T23:59:00Z", "probability": 0.6, "likelihood_term": "likely",
    "confidence": "low", "confidence_basis": {"rubric": "test"},
    "alternatives": [{"hypothesis": "It does not happen", "tell_apart": "the source's release"}],
    "change_from_prior": "new", "requirement_ids": ["VD-PIR-1"], "method_version": "verdict-1.0",
    "resolution": {"rule": "Yes if it happens", "source": "Test source", "resolve_by": "2026-11-07T00:00:00Z",
                   "known_at": "2026-11-06T10:00:00Z"},
    "sources": [{"id": "test", "publisher": "Test", "licence": "public", "accessed_at": "2026-10-31T18:00:00Z",
                 "descriptor": "test"}],
    "visual": {"type": "page", "url": "https://example.org"}, "geometry": None,
}


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "doctrine", self.tmp / "doctrine")
        (self.tmp / "drafts").mkdir()
        self.drafts = self.tmp / "drafts" / "test.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_drafts(self, *items, approved="Tom Brummer"):
        doc = {"drafts": [{"approved": approved, "item": it} for it in items]}
        self.drafts.write_text(json.dumps(doc), encoding="utf-8")

    def test_register_resolve_score_check(self):
        two = copy.deepcopy(ITEM)
        two.update({"id": "verdict:test:two", "probability": 0.15, "likelihood_term": "very_unlikely"})
        self.write_drafts(ITEM, two)
        ids = verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        self.assertEqual(ids, ["verdict:test:one", "verdict:test:two"])
        self.assertEqual(verdict.check(self.tmp), [])
        line = verdict.read_lines(self.tmp / verdict.FORECASTS)[0]
        self.assertEqual((line["status"], line["published_at"], line["approved_by"]),
                         ("actual", "2026-11-01T12:00:00Z", "Tom Brummer"))
        self.assertEqual(verdict.register(self.drafts, self.tmp), [], "registered drafts are not registered twice")
        verdict.resolve("verdict:test:one", 1, "https://example.org/r", root=self.tmp, when="2026-11-06T12:00:00Z")
        verdict.resolve("verdict:test:two", 0, "https://example.org/r", root=self.tmp, when="2026-11-06T12:00:00Z")
        s = verdict.score(self.tmp)["by_program"]["all"]
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["brier"], ((0.6 - 1) ** 2 + 0.15 ** 2) / 2, places=4)
        self.assertEqual(verdict.check(self.tmp), [])

    def test_unapproved_drafts_stay_out(self):
        self.write_drafts(ITEM, approved=None)
        self.assertEqual(verdict.register(self.drafts, self.tmp), [])
        self.assertEqual(verdict.read_lines(self.tmp / verdict.FORECASTS), [])

    def test_late_registration_is_refused(self):
        self.write_drafts(ITEM)
        with self.assertRaises(SystemExit) as cm:
            verdict.register(self.drafts, self.tmp, when="2026-11-06T11:00:00Z")
        self.assertIn("not a pre-registration", str(cm.exception))

    def test_linter_errors_block_registration(self):
        bad = copy.deepcopy(ITEM)
        bad["likelihood_term"] = "almost_certain"
        self.write_drafts(bad)
        with self.assertRaises(SystemExit):
            verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")

    def test_double_resolution_and_unknown_ids(self):
        self.write_drafts(ITEM)
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        verdict.resolve("verdict:test:one", 1, "https://example.org", root=self.tmp)
        with self.assertRaises(SystemExit):
            verdict.resolve("verdict:test:one", 0, "https://example.org", root=self.tmp)
        with self.assertRaises(SystemExit):
            verdict.resolve("verdict:test:nope", 0, "https://example.org", root=self.tmp)

    def test_append_only_against_git(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        run = lambda *a: subprocess.run(["git", *a], cwd=self.tmp, check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@example.org")
        run("config", "user.name", "t")
        self.write_drafts(ITEM)
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        run("add", "-A")
        run("commit", "-q", "-m", "register")
        self.assertEqual(verdict.append_only(self.tmp, "HEAD"), [])
        path = self.tmp / verdict.FORECASTS
        path.write_text(path.read_text(encoding="utf-8").replace('"probability":0.6', '"probability":0.7'),
                        encoding="utf-8")
        self.assertTrue(verdict.append_only(self.tmp, "HEAD"), "an edited line must fail")


if __name__ == "__main__":
    unittest.main()
