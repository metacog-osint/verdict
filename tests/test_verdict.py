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

    def test_due_lists_knowable_unresolved_forecasts(self):
        later = copy.deepcopy(ITEM)
        later.update({"id": "verdict:test:later"})
        later["resolution"] = {**ITEM["resolution"], "known_at": "2026-12-20T00:00:00Z", "resolve_by": "2026-12-21T00:00:00Z"}
        later["valid_until"] = "2026-12-20T00:00:00Z"
        self.write_drafts(ITEM, later)
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        d = verdict.due(self.tmp, now="2026-11-06T12:00:00Z", days=14)
        self.assertEqual([r["id"] for r in d["due"]], ["verdict:test:one"])
        self.assertEqual(d["soon"], [])
        self.assertIn("verdict:test:one", verdict.due_markdown(d))
        verdict.resolve("verdict:test:one", 1, "https://example.org", root=self.tmp, when="2026-11-06T13:00:00Z")
        d = verdict.due(self.tmp, now="2026-12-10T00:00:00Z", days=14)
        self.assertEqual(([r["id"] for r in d["due"]], [r["id"] for r in d["soon"]]), ([], ["verdict:test:later"]))


class Calendar(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "doctrine", self.tmp / "doctrine")
        (self.tmp / "calendar").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *events):
        (self.tmp / "calendar" / "events.json").write_text(json.dumps({"events": list(events)}), encoding="utf-8")

    EV = {"id": "e1", "date": "2026-11-16", "title": "Test bulletin expires", "kind": "bulletin_expiry",
          "source": {"publisher": "Test", "url": "https://example.org"}, "added": "2026-10-31"}

    def test_good_event_passes_and_exports_a_lintable_feed(self):
        from tic_doctrine import contract, registers
        self.write(self.EV)
        self.assertEqual(verdict.calendar_problems(self.tmp, registers.ids(self.tmp / "doctrine")), [])
        fc = verdict.calendar_feed(self.tmp, generated="2026-10-31T00:00:00Z")
        regs = registers.ids(self.tmp / "doctrine")
        for f in fc["features"]:
            self.assertEqual([p for p in contract.check(f, regs) if p[1] == "error"], [])
        self.assertEqual(fc["features"][0]["properties"]["valid_until"], "2026-11-16T00:00:00Z")

    def test_bad_kind_missing_fields_and_unknown_forecasts_fail(self):
        from tic_doctrine import registers
        regs = registers.ids(self.tmp / "doctrine")
        self.write({**self.EV, "kind": "party"}, {"id": "e2", "date": "2026-11-17"},
                   {**self.EV, "id": "e3", "forecast_ids": ["verdict:nope"]})
        msgs = " ".join(verdict.calendar_problems(self.tmp, regs))
        for needle in ("kind must be one of", "e2: missing", "unknown forecast"):
            self.assertIn(needle, msgs)

    def test_unconfirmed_dates_are_marked_claimed(self):
        item = verdict.calendar_item({**self.EV, "verified": False}, "2026-10-31T00:00:00Z")
        self.assertEqual(item["status_class"], "claimed")

    def test_sync_merges_links_and_keeps_history(self):
        from tic_doctrine import registers
        # a registered forecast whose rule names CZIB-2026-04
        (self.tmp / "ledger").mkdir()
        fc = copy.deepcopy(ITEM)
        fc.update({"id": "verdict:t:iran", "status": "actual", "approved_by": "T"})
        fc["resolution"] = {**ITEM["resolution"], "rule": "Yes if EASA shows CZIB-2026-04 active after 16 Nov"}
        (self.tmp / "ledger" / "forecasts.jsonl").write_text(json.dumps(fc) + "\n", encoding="utf-8")
        manual = {**self.EV, "id": "m1", "date": "2026-12-15"}
        past = {**self.EV, "id": "2026-10-01-easa-czib-2026-01", "date": "2026-10-01", "origin": "cordon"}
        stale = {**self.EV, "id": "2026-11-16-easa-czib-2026-05", "date": "2026-11-16", "origin": "cordon"}
        self.write(manual, past, stale)
        src = {"publisher": "EASA", "url": "https://example.org"}
        incoming = {"source": "cordon", "events": [
            {"id": "2026-11-16-easa-czib-2026-04", "date": "2026-11-16", "title": "Iran bulletin expires",
             "kind": "bulletin_expiry", "countries": ["IR"], "source": src, "ref": "CZIB-2026-04"},
            {"id": "2026-10-20-state-sa-posture", "date": "2026-10-20", "title": "Saudi Arabia: ordered departure",
             "kind": "advisory_change", "occurred": True, "countries": ["SA"], "source": src, "ref": "SA"},
            {"id": "2026-10-02-easa-czib-2026-02", "date": "2026-10-02", "title": "old expiry", "kind": "bulletin_expiry",
             "source": src, "ref": "CZIB-2026-02"}]}
        r = verdict.sync_calendar(self.tmp, incoming, today="2026-10-31")
        ev = {e["id"]: e for e in verdict.read_calendar(self.tmp)}
        self.assertEqual(ev["2026-11-16-easa-czib-2026-04"]["forecast_ids"], ["verdict:t:iran"])
        self.assertNotIn("forecast_ids", ev["2026-10-20-state-sa-posture"], "only bulletin numbers link forecasts")
        self.assertTrue(ev["2026-10-20-state-sa-posture"]["occurred"])
        self.assertIn("m1", ev, "hand-entered events stay")
        self.assertIn("2026-10-01-easa-czib-2026-01", ev, "past synced events stay as history")
        self.assertNotIn("2026-11-16-easa-czib-2026-05", ev, "a future synced event no longer sent is dropped")
        self.assertNotIn("2026-10-02-easa-czib-2026-02", ev, "an expiry already past when first seen is skipped")
        self.assertEqual(r["dropped"], ["2026-11-16-easa-czib-2026-05"])
        self.assertEqual(verdict.calendar_problems(self.tmp, registers.ids(self.tmp / "doctrine")), [])
        again = verdict.sync_calendar(self.tmp, incoming, today="2026-11-01")
        self.assertEqual((again["added"], again["dropped"]), ([], []))
        self.assertEqual(verdict.read_calendar(self.tmp)[0]["date"] <= verdict.read_calendar(self.tmp)[-1]["date"], True)


class Site(unittest.TestCase):
    def test_build_is_deterministic_and_complete(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import build_site
        a, b = build_site.build(), build_site.build()
        self.assertEqual(a, b)
        for rel in ("index.html", "app.js", "style.css", "tic-time.js", "data/forecasts.json", "data/scores.json",
                    "data/calendar.geojson", "data/feed.geojson"):
            self.assertIn(rel, a)
        rows = json.loads(a["data/forecasts.json"])["forecasts"]
        self.assertTrue(all(r["event_date"] and len(r["event_date"]) == 10 for r in rows))


if __name__ == "__main__":
    unittest.main()
