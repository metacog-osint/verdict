"""Offline tests for tools/verdict.py. Run as a plain script or with unittest."""
import copy
import json
import os
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

    def test_ingest_feed_registers_and_deduplicates_program_forecasts(self):
        vis_fc = copy.deepcopy(ITEM)
        vis_fc.update({
            "id": "vis:11-iran-red-sea:2026-11-02:dust",
            "program": "vis",
            "requirement_ids": ["VS-PIR-1", "VS-PIR-2"],
            "probability": 0.4,
            "likelihood_term": "unlikely",
        })
        obs = copy.deepcopy(ITEM)
        obs.update({"id": "vis:obs:1", "program": "vis", "kind": "observation"})
        feed_doc = {
            "type": "FeatureCollection",
            "contract": "tic-feed/2",
            "program": "vis",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [56.25, 26.56]}, "properties": vis_fc},
                {"type": "Feature", "geometry": None, "properties": obs},
            ],
        }
        r1 = verdict.ingest_feed(self.tmp, feed_doc, draft_path=self.drafts, approved="Tom Brummer",
                                 when="2026-11-01T12:00:00Z")
        self.assertEqual(r1["ingested"], ["vis:11-iran-red-sea:2026-11-02:dust"])
        self.assertEqual(r1["registered"], ["vis:11-iran-red-sea:2026-11-02:dust"])
        self.assertEqual(verdict.check(self.tmp), [])
        # Second ingest is a no-op (deduplicated against ledger/drafts)
        r2 = verdict.ingest_feed(self.tmp, feed_doc, draft_path=self.drafts, approved="Tom Brummer",
                                 when="2026-11-01T13:00:00Z")
        self.assertEqual(r2["ingested"], [])
        self.assertEqual(r2["registered"], [])

    def _czib(self, fid="verdict:test:czib", **resolution):
        """A draft shaped like the ledger's EASA lines: knowable from 16 Nov, checked only after 00:00 UTC 17 Nov."""
        it = copy.deepcopy(ITEM)
        it.update({"id": fid, "valid_until": "2026-11-17T00:00:00Z"})
        it["resolution"] = {"rule": AutoResolve.EASA_RULE, "source": "EASA", "known_at": "2026-11-16T00:00:00Z",
                            "resolve_by": "2026-11-30T23:59:00Z", **resolution}
        return it

    def test_check_after_holds_due_resolve_and_check(self):
        # One line states its check time only in the rule's words (the six EASA lines); one carries the field.
        explicit = self._czib("verdict:test:explicit", rule="Yes if it happens", check_after="2026-11-17T00:00:00Z")
        self.write_drafts(self._czib(), explicit)
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        lines = verdict.read_lines(self.tmp / verdict.FORECASTS)
        self.assertEqual([verdict.check_after(f) for f in lines], ["2026-11-17T00:00:00Z"] * 2)
        self.assertIsNone(verdict.check_after(ITEM), "a rule that names no check time sets none")
        ids = ["verdict:test:czib", "verdict:test:explicit"]

        # 16 Nov 19:00 UTC, about when the reminder runs: knowable, but the rule does not allow a check yet.
        d = verdict.due(self.tmp, now="2026-11-16T19:00:00Z")
        self.assertEqual((d["due"], [r["id"] for r in d["soon"]]), ([], ids))
        md = verdict.due_markdown(d)
        self.assertIn("Nothing is due.", md)
        self.assertIn("- 2026-11-17: Test event happens", md)
        self.assertNotIn("- 2026-11-16:", md)
        self.assertEqual([r["id"] for r in verdict.due(self.tmp, now="2026-11-17T00:00:00Z")["due"]], ids)

        for fid in ids:
            with self.assertRaises(SystemExit) as cm:
                verdict.resolve(fid, 1, "https://example.org", root=self.tmp, when="2026-11-16T19:00:00Z")
            self.assertIn("only after 2026-11-17T00:00:00Z", str(cm.exception))
        self.assertEqual(verdict.read_lines(self.tmp / verdict.RESOLUTIONS), [])

        # A resolution written by hand is held to the same time, on its own date and on the date of its evidence.
        early = {"id": ids[0], "outcome": 1, "resolved_at": "2026-11-16T19:00:00Z", "note": "", "resolved_by": "T",
                 "evidence": {"url": "https://example.org", "accessed_at": "2026-11-16T19:00:00Z"}}
        for name, line in {"resolved early": early,
                           "evidence read early": {**early, "resolved_at": "2026-11-17T06:00:00Z"}}.items():
            with self.subTest(name):
                (self.tmp / verdict.RESOLUTIONS).write_text(json.dumps(line) + "\n", encoding="utf-8")
                self.assertIn("before the rule allows a check", " ".join(verdict.check(self.tmp)))
        (self.tmp / verdict.RESOLUTIONS).unlink()
        verdict.resolve(ids[0], 1, "https://example.org", root=self.tmp, when="2026-11-17T00:00:00Z")
        self.assertEqual(verdict.check(self.tmp), [])

    def test_a_bad_check_after_is_refused_at_registration(self):
        cases = {
            "not a UTC time": dict(check_after="17 Nov 2026"),
            "later than resolve_by": dict(rule="Yes if it happens", check_after="2026-12-01T00:00:00Z"),
            "not the time the rule itself gives": dict(check_after="2026-11-18T00:00:00Z"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.write_drafts(self._czib(**change))
                with self.assertRaises(SystemExit) as cm:
                    verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
                self.assertIn("check_after", str(cm.exception))
        self.assertEqual(verdict.read_lines(self.tmp / verdict.FORECASTS), [])

    def test_the_registered_easa_questions_are_held_to_their_rule_unedited(self):
        # The real ledger: the six lines are not changed (they carry no check_after field), and each is still held
        # to the check time its own rule names.
        easa = [f for f in verdict.read_lines(ROOT / verdict.FORECASTS)
                if f["id"].startswith("verdict:2026-11-16:easa-")]
        self.assertEqual(len(easa), 6)
        for f in easa:
            self.assertNotIn("check_after", f["resolution"])
            self.assertEqual(f["resolution"]["known_at"], "2026-11-16T00:00:00Z")
            self.assertEqual(verdict.check_after(f), "2026-11-17T00:00:00Z")

    def _minimal_ledger(self, counts):
        """Ledger lines with only what score() reads: {program: [(probability, outcome), ...]}."""
        fc, res = [], []
        for prog, pairs in counts.items():
            for i, (p, o) in enumerate(pairs):
                fc.append({"id": f"{prog}:t:{i}", "program": prog, "probability": p})
                res.append({"id": f"{prog}:t:{i}", "outcome": o})
        for rel, rows in ((verdict.FORECASTS, fc), (verdict.RESOLUTIONS, res)):
            (self.tmp / rel).parent.mkdir(exist_ok=True)
            (self.tmp / rel).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_skill_is_held_until_a_program_has_enough_resolved_forecasts(self):
        need = verdict.MIN_SKILL_N
        # The record's first two resolutions, one easy No and one easy Yes: the page printed "skill 0.95" from these.
        two = verdict._brier([(0.05, 0), (0.85, 1)])
        self.assertEqual((two["n"], two["brier"]), (2, 0.0125), "Brier and n stay")
        self.assertIsNone(two["skill_vs_base_rate"])
        self.assertIn(f"{need} resolved forecasts", two["skill_withheld"])
        enough = ([(0.8, 1), (0.2, 0)] * need)[:need]
        self.assertIsNone(verdict._brier(enough[:-1])["skill_vs_base_rate"], "one short of the minimum")
        full = verdict._brier(enough)
        self.assertIsNone(full["skill_withheld"])
        self.assertAlmostEqual(full["skill_vs_base_rate"], 1 - full["brier"] / full["uncertainty"], places=3)
        same = verdict._brier([(0.9, 1)] * need)
        self.assertIsNone(same["skill_vs_base_rate"])
        self.assertIn("same outcome", same["skill_withheld"])

        # By program: a program is shown on its own count; all programs together only once each has enough.
        self._minimal_ledger({"vis": enough, "verdict": [(0.05, 0), (0.85, 1)]})
        s = verdict.score(self.tmp)
        self.assertEqual(s["skill_min_n"], need)
        by = s["by_program"]
        self.assertIsNotNone(by["vis"]["skill_vs_base_rate"])
        self.assertIsNone(by["verdict"]["skill_vs_base_rate"])
        self.assertEqual((by["all"]["n"], by["all"]["skill_vs_base_rate"]), (need + 2, None))
        self.assertTrue(by["all"]["skill_withheld"])
        self._minimal_ledger({"vis": enough, "verdict": enough})
        self.assertIsNotNone(verdict.score(self.tmp)["by_program"]["all"]["skill_vs_base_rate"])

    def test_new_lines_must_be_published_when_they_are_committed(self):
        if not shutil.which("git"):
            self.skipTest("git not available")

        def git(*a, at=None):
            env = dict(os.environ)
            if at:  # git's own date form: seconds since 1970 and the UTC offset
                env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{int(verdict._t(at).timestamp())} +0000"
            return subprocess.run(["git", *a], cwd=self.tmp, check=True, capture_output=True, text=True,
                                  env=env).stdout.strip()

        def append(fid, published_at, committed_at=None):
            """A ledger line written by hand, as a back-dater would, then committed at the given time."""
            it = copy.deepcopy(ITEM)
            it.update({"id": fid, "status": "actual", "approved_by": "T", "published_at": published_at})
            verdict.append_lines(self.tmp / verdict.FORECASTS, [it])
            if committed_at:
                git("add", "-A")
                git("commit", "-q", "-m", fid, at=committed_at)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.org")
        git("config", "user.name", "t")
        self.write_drafts(ITEM)
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")
        git("add", "-A")
        git("commit", "-q", "-m", "register", at="2026-11-01T12:05:00Z")
        base = git("rev-parse", "HEAD")

        append("verdict:test:honest", "2026-11-02T09:00:00Z", committed_at="2026-11-02T09:20:00Z")
        self.assertEqual(verdict.check(self.tmp, base), [])

        # Written on 5 Nov and dated 31 Oct; and one dated half an hour before known_at (6 Nov 10:00) but committed
        # after it. Both claim an earlier time than the commit that made them public.
        append("verdict:test:backdated", "2026-10-31T18:00:00Z", committed_at="2026-11-05T15:00:00Z")
        append("verdict:test:late", "2026-11-06T09:30:00Z", committed_at="2026-11-06T10:20:00Z")
        errs = verdict.check(self.tmp, base)
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("verdict:test:backdated", errs[0])
        self.assertIn("within an hour", errs[0])
        self.assertIn("verdict:test:late", errs[1])
        self.assertIn("known_at", errs[1])
        self.assertEqual(verdict.check(self.tmp), [], "without --base nothing ties a line to its commit: CI passes it")

        # A registered line not committed yet is held to the clock, so the hour does not pass unnoticed.
        append("verdict:test:pending", "2026-11-03T08:00:00Z")
        self.assertEqual(verdict.check(self.tmp, "HEAD", now="2026-11-03T08:30:00Z"), [])
        self.assertIn("not committed yet", " ".join(verdict.check(self.tmp, "HEAD", now="2026-11-03T12:00:00Z")))


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
        fc["resolution"] = {**ITEM["resolution"], "rule": "Yes if EASA shows CZIB-2026-04 active after 16 Nov",
                            "known_at": "2026-11-16T00:00:00Z", "resolve_by": "2026-11-30T23:59:00Z"}
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

    SRC = {"publisher": "EASA", "url": "https://example.org"}
    # Cordon's event for a bulletin is keyed by its expiry date, so a renewal arrives as a different event and the
    # old one stops being sent.
    WAS = {"id": "2026-11-16-easa-czib-2026-04", "date": "2026-11-16", "title": "Iran bulletin R2 expires",
           "kind": "bulletin_expiry", "countries": ["IR"], "source": SRC, "ref": "CZIB-2026-04"}
    RENEWED = {**WAS, "id": "2027-01-31-easa-czib-2026-04", "date": "2027-01-31", "title": "Iran bulletin R3 expires"}

    def _iran(self):
        """A registered forecast shaped like the ledger's EASA lines: about the 16 Nov expiry, naming the bulletin."""
        (self.tmp / "ledger").mkdir(exist_ok=True)
        fc = copy.deepcopy(ITEM)
        fc.update({"id": "verdict:t:iran", "status": "actual", "approved_by": "T",
                   "valid_until": "2026-11-17T00:00:00Z"})
        fc["resolution"] = {"rule": AutoResolve.EASA_RULE, "source": "EASA", "known_at": "2026-11-16T00:00:00Z",
                            "resolve_by": "2026-11-30T23:59:00Z"}
        (self.tmp / "ledger" / "forecasts.jsonl").write_text(json.dumps(fc) + "\n", encoding="utf-8")
        return fc

    def test_a_renewed_bulletin_does_not_redate_its_forecast(self):
        import build_site
        from tic_doctrine import registers
        fc = self._iran()
        for today in ("2026-11-15", "2026-11-16", "2026-11-17"):  # the renewal is first synced before, on or after
            with self.subTest(synced=today):
                self.write()
                verdict.sync_calendar(self.tmp, {"source": "cordon", "events": [self.WAS]}, today="2026-10-31")
                r = verdict.sync_calendar(self.tmp, {"source": "cordon", "events": [self.RENEWED]}, today=today)
                ev = {e["id"]: e for e in verdict.read_calendar(self.tmp)}
                self.assertIn(self.WAS["id"], ev, "the event a forecast is about is never dropped")
                self.assertEqual(ev[self.WAS["id"]]["forecast_ids"], ["verdict:t:iran"])
                self.assertNotIn("forecast_ids", ev[self.RENEWED["id"]], "the next expiry is not this forecast's event")
                self.assertEqual((r["added"], r["dropped"]), ([self.RENEWED["id"]], []))
                self.assertEqual(build_site.event_dates([fc], verdict.read_calendar(self.tmp)),
                                 {"verdict:t:iran": "2026-11-16"})
                self.assertEqual(verdict.calendar_problems(self.tmp, registers.ids(self.tmp / "doctrine")), [])
                again = verdict.sync_calendar(self.tmp, {"source": "cordon", "events": [self.RENEWED]}, today=today)
                self.assertEqual((again["added"], again["dropped"]), ([], []))

    def test_a_wrong_link_already_in_the_calendar_is_removed_and_never_dates_the_forecast(self):
        import build_site
        fc = self._iran()
        # What a sync before this rule left behind: the next expiry linked to the forecast, the 16 Nov event gone.
        wrong = {**self.RENEWED, "origin": "cordon", "added": "2026-11-16", "forecast_ids": ["verdict:t:iran"]}
        self.write(wrong)
        self.assertEqual(build_site.event_dates([fc], [wrong]), {}, "a later event never dates a forecast")
        verdict.sync_calendar(self.tmp, {"source": "cordon", "events": [self.RENEWED]}, today="2026-11-17")
        self.assertNotIn("forecast_ids", verdict.read_calendar(self.tmp)[0])
        # Renewed again: with no forecast about it, the superseded future expiry is dropped as before.
        self.write(wrong)
        again = {**self.RENEWED, "id": "2027-04-30-easa-czib-2026-04", "date": "2027-04-30"}
        r = verdict.sync_calendar(self.tmp, {"source": "cordon", "events": [again]}, today="2026-11-20")
        self.assertEqual(r["dropped"], [wrong["id"]])


    def test_headwind_events_sync_and_link(self):
        """Headwind's live events.json of 15 Nov 2026 (two events of its eleven), synced beside hand-entered ones."""
        (self.tmp / "calendar").mkdir(exist_ok=True)
        (self.tmp / "ledger").mkdir(exist_ok=True)
        (self.tmp / "calendar" / "events.json").write_text(json.dumps({"events": []}), encoding="utf-8")
        rule = {"rule": "Yes if BIS publishes a document that supersedes Federal Register document 2026-22793."}
        with open(self.tmp / verdict.FORECASTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "verdict:t:affiliates", "resolution": rule}) + "\n")
        src = {"publisher": "US Federal Register (BIS)", "url": "https://www.federalregister.gov/documents/2026/11/09/"
               "2026-22793/extension-of-the-suspension-of-expansion-of-end-user-controls-for-affiliates-of-certain-listed"}
        events = {"source": "headwind", "events": [
            {"id": "2027-01-10-fr-2026-22793", "date": "2027-01-10", "kind": "rule_date", "countries": [],
             "ref": "2026-22793", "source": src,
             "title": "BIS: Extension of the Suspension of Expansion of End-User Controls for Affiliates of Certain "
                      "Listed Entities (date named in the rule)"},
            {"id": "2026-12-09-fomc-decision", "date": "2026-12-09", "kind": "rule_date", "countries": ["US"],
             "ref": "FOMC", "title": "Federal Reserve FOMC rate decision",
             "source": {"publisher": "Board of Governors of the Federal Reserve System",
                        "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"}}]}
        res = verdict.sync_calendar(self.tmp, events, today="2026-11-15")
        self.assertEqual(res["origin"], "headwind")
        ev = {e["id"]: e for e in verdict.read_calendar(self.tmp)}
        self.assertEqual(ev["2027-01-10-fr-2026-22793"]["forecast_ids"], ["verdict:t:affiliates"])
        self.assertNotIn("forecast_ids", ev["2026-12-09-fomc-decision"], "a series ref such as FOMC links nothing")
        self.assertEqual(ev["2027-01-10-fr-2026-22793"]["origin"], "headwind")
        self.assertEqual(verdict.SYNC_SOURCES["headwind"], verdict.HEADWIND_EVENTS)


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
        self.assertTrue(all(r["event_date"] <= r["known_at"][:10] for r in rows),
                        "a forecast is never dated after its outcome becomes knowable")

    def test_the_page_gives_no_skill_figure_from_a_small_sample_and_says_why(self):
        import build_site
        built = build_site.build()
        scores = json.loads(built["data/scores.json"])
        self.assertEqual(scores["skill_min_n"], verdict.MIN_SKILL_N)
        for prog, row in scores["by_program"].items():
            with self.subTest(prog):
                self.assertIn("brier", row)
                if row["n"] < scores["skill_min_n"]:
                    self.assertIsNone(row["skill_vs_base_rate"])
                    self.assertIn(f"{scores['skill_min_n']} resolved forecasts", row["skill_withheld"])
        # the page prints each row's reason under the table (site_src/app.js)
        self.assertIn("skill_withheld", built["app.js"])

    def test_the_claim_is_limited_to_forecasts_registered_here(self):
        # No program's own forecasts are taken into the ledger yet, so nothing may say every forecast is logged.
        for rel in ("site_src/index.html", "README.md", "doctrine/method.md"):
            with self.subTest(rel):
                text = " ".join((ROOT / rel).read_text(encoding="utf-8").split()).lower()
                self.assertIn("every forecast registered here", text)
                for old in ("every forecast the suite makes", "every forecast, logged", "every forecast logged"):
                    self.assertNotIn(old, text)


class AutoResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "doctrine", self.tmp / "doctrine")
        (self.tmp / "drafts").mkdir()
        self.drafts = self.tmp / "drafts" / "test.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    EASA_RULE = ("Yes if, on a check after 00:00 UTC on 17 Nov 2026 (repeated by 30 Nov), EASA's CZIB page, CSV or "
                 "RSS, or an Internet Archive copy, shows CZIB-2026-04 active, revised on or before 16 Nov 2026 and "
                 "valid beyond 16 Nov 2026. A successor CZIB for the same airspace counts only if active on 17 Nov "
                 "and not weaker. No if it is withdrawn, suspended, converted to an Information Note, or revised "
                 "only after 16 Nov.")
    CZIB_ID = "verdict:2026-11-16:easa-iran-czib-renewed"

    def _register(self, *items):
        doc = {"drafts": [{"approved": "Tom Brummer", "item": it} for it in items]}
        self.drafts.write_text(json.dumps(doc), encoding="utf-8")
        verdict.register(self.drafts, self.tmp, when="2026-11-01T12:00:00Z")

    def _czib_fc(self):
        fc = copy.deepcopy(ITEM)
        fc.update({"id": self.CZIB_ID, "program": "verdict", "valid_until": "2026-11-17T00:00:00Z",
                   "resolution": {"rule": self.EASA_RULE, "source": "EASA Conflict Zone Information Bulletins",
                                  "known_at": "2026-11-16T00:00:00Z", "resolve_by": "2026-11-30T23:59:00Z"}})
        return fc

    @staticmethod
    def _easa(read_at, **change):
        b = {"number": "CZIB-2026-04-R3", "id": "CZIB-2026-04", "subject": "Airspace of Iran", "status": "active",
             "issued": "2026-07-08", "valid_until": "2027-01-31", "revised": "2026-11-13", "revision": "fixture"}
        b.update(change)
        return {"bulletins": [b], "fetched_at": read_at, "url": "fixture"}

    def _run(self, easa, now="2026-11-17T12:00:00Z", **kw):
        import auto_resolve
        return auto_resolve.auto_resolve(root=self.tmp, now=now, easa=easa, fetch=False, **kw)

    def test_easa_csv_parse_reads_revision_dates(self):
        import auto_resolve
        csv_text = ('Subject,CZIB number,Issue date,Valid until,Status,Revision\n'
                    '"Airspace of Iran",CZIB-2026-04-R3,08/07/2026,31/01/2027,Active,'
                    '"13/11/2026 – The validity of the CZIB was extended until 31/01/2027."\n'
                    '"Airspace of the Persian Gulf",CZIB-2026-07R3,14/07/2026,16/11/2026,Withdrawn,\n')
        rows = {r["id"]: r for r in auto_resolve.parse_easa_csv(csv_text)}
        self.assertEqual(rows["CZIB-2026-04"]["revised"], "2026-11-13", "a date inside the note is not a revision")
        self.assertEqual(rows["CZIB-2026-04"]["valid_until"], "2027-01-31")
        self.assertEqual(rows["CZIB-2026-07"]["status"], "withdrawn")
        self.assertEqual(rows["CZIB-2026-07"]["revised"], "2026-07-14", "unrevised counts from its issue date")

    def test_easa_renewal_on_time_resolves_yes_under_the_named_resolver(self):
        self._register(self._czib_fc())
        res = self._run(self._easa("2026-11-17T06:00:00Z"), apply=True, by="Tom Brummer")
        self.assertEqual(res["applied"], [self.CZIB_ID])
        row = verdict.read_lines(self.tmp / verdict.RESOLUTIONS)[0]
        self.assertEqual((row["outcome"], row["resolved_by"]), (1, "Tom Brummer"))
        self.assertIn("CZIB-2026-04-R3 active", row["note"])
        self.assertEqual(verdict.check(self.tmp), [])

    def test_easa_is_not_read_before_the_rules_check_time(self):
        self._register(self._czib_fc())
        # Due from 16 Nov, but the rule checks only after 00:00 UTC on 17 Nov; a later --now does not move the read.
        early = self._easa("2026-11-16T00:30:00Z", valid_until="2026-11-16", revised="2026-09-30")
        res = self._run(early, now="2026-11-18T00:00:00Z", apply=True, by="Tom Brummer")
        self.assertEqual(res["proposed"], [])
        self.assertIn("only after 2026-11-17T00:00:00Z", res["skipped"][0]["reason"])
        self.assertEqual(verdict.read_lines(self.tmp / verdict.RESOLUTIONS), [])

    def test_easa_read_is_held_by_the_check_after_field_as_well_as_by_the_rules_words(self):
        fc = self._czib_fc()
        fc["resolution"].update({
            "check_after": "2026-11-17T00:00:00Z",
            "rule": "Yes if EASA's CZIB table shows CZIB-2026-04 active and valid beyond 16 Nov 2026."})
        self._register(fc)
        res = self._run(self._easa("2026-11-16T00:30:00Z"), now="2026-11-18T00:00:00Z", apply=True, by="Tom Brummer")
        self.assertEqual((res["proposed"], res["applied"]), ([], []))
        self.assertIn("only after 2026-11-17T00:00:00Z", res["skipped"][0]["reason"])

    def test_easa_no_outcomes_are_held_for_review(self):
        self._register(self._czib_fc())
        cases = {
            "not extended": dict(valid_until="2026-11-16", revised="2026-09-30"),
            "withdrawn": dict(status="withdrawn"),
            "revised only after the deadline": dict(revised="2026-11-18"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                res = self._run(self._easa("2026-11-17T06:00:00Z", **change), apply=True, by="Tom Brummer")
                self.assertEqual(res["proposed"][0]["outcome"], 0)
                self.assertTrue(res["proposed"][0]["needs_review"])
                self.assertEqual((res["held_for_review"], res["applied"]), ([self.CZIB_ID], []))
        self.assertEqual(verdict.read_lines(self.tmp / verdict.RESOLUTIONS), [])

    def test_easa_missing_or_unread_is_skipped(self):
        self._register(self._czib_fc())
        gone = self._run(self._easa("2026-11-17T06:00:00Z", id="CZIB-2026-99"))
        self.assertIn("successor CZIB", gone["skipped"][0]["reason"])
        unread = self._run({"error": "EASA CSV: HTTPError"})
        self.assertIn("HTTPError", unread["skipped"][0]["reason"])

    def test_apply_needs_a_named_resolver(self):
        self._register(self._czib_fc())
        with self.assertRaises(SystemExit):
            self._run(self._easa("2026-11-17T06:00:00Z"), apply=True)

    def test_vis_reading_is_held_and_needs_a_full_day(self):
        vis_wind = copy.deepcopy(ITEM)
        vis_wind.update({
            "id": "vis:11-iran-red-sea:2026-11-02:wind", "program": "vis", "requirement_ids": ["VS-PIR-1"],
            "geometry": {"type": "Point", "coordinates": [56.25, 26.56]},
            "resolution": {
                "rule": ("Yes if ICAO station OIKB (Bandar Abbas) reports wind gusts (wind_gusts_10m) >= 35 kn "
                         "for >= 2 hours on 2026-11-02 UTC."),
                "source": "NOAA Aviation Weather Center METAR / SYNOP archive for OIKB",
                "known_at": "2026-11-03T00:00:00Z", "resolve_by": "2026-11-05T00:00:00Z"},
        })
        self._register(vis_wind)
        gusts = [20.0] * 22 + [36.5, 38.0]
        full = {"OIKB:2026-11-02": {"url": "fixture", "hourly": {"wind_gusts_10m": gusts}}}
        res = self._run(None, now="2026-11-08T12:00:00Z", apply=True, by="Tom Brummer", weather_archives=full)
        self.assertEqual(res["proposed"][0]["outcome"], 1)
        self.assertIn("METARs", res["proposed"][0]["needs_review"][0])
        self.assertEqual((res["held_for_review"], res["applied"]), (["vis:11-iran-red-sea:2026-11-02:wind"], []))
        part = {"OIKB:2026-11-02": {"url": "fixture", "hourly": {"wind_gusts_10m": gusts[:20] + [None] * 4}}}
        res = self._run(None, now="2026-11-08T12:00:00Z", weather_archives=part)
        self.assertIn("all 24 hours", res["skipped"][0]["reason"])

    # The exact text Blackout writes (blackout/eaglei.py verdict_rule; Blackout's test holds the same text).
    EAGLEI_RULE = ("Yes if EAGLE-I's outage history (ORNL, figshare doi:10.6084/m9.figshare.24237376) shows California "
                   "(CA) with more than {line} customers out, its counties summed in each 15-minute run, in 4 or more runs "
                   "with no more than 3 runs under the line between them, between {a} and {b} UTC. No otherwise.")

    def _eaglei_data(self):
        """Real EAGLE-I rows (tests/fixtures/eaglei/MANIFEST.json), read the way fetch_eaglei reads a year file."""
        import gzip
        import auto_resolve

        fx = ROOT / "tests" / "fixtures" / "eaglei"
        caps = auto_resolve.eaglei_caps((fx / "MCC_CA.csv").read_text(encoding="utf-8"))
        with gzip.open(fx / "eaglei_outages_2025_CA_2025-01-08T12Z_24h.csv.gz", "rt", encoding="utf-8-sig",
                       newline="") as f:
            got = auto_resolve.read_eaglei_year(f, {"CA"}, caps)
        return {2025: {**got, "file": "eaglei_outages_2025.csv", "md5_ok": True}, "listed": list(range(2014, 2026))}

    def _eaglei_forecast(self, fid, line, a, b, known_at):
        fc = copy.deepcopy(ITEM)
        fc.update({
            "id": fid, "program": "blackout", "requirement_ids": ["BO-PIR-8"], "countries": ["US"],
            "valid_until": known_at,
            "resolution": {"rule": self.EAGLEI_RULE.format(line=line, a=a, b=b),
                           "source": "ORNL EAGLE-I outage history, figshare https://doi.org/10.6084/m9.figshare.24237376 "
                                     "(CC BY 4.0)",
                           "known_at": known_at, "resolve_by": "2027-06-30T00:00:00Z"},
        })
        return fc

    def test_blackout_us_outage_resolves_against_eaglei_history(self):
        import auto_resolve

        items = [
            self._eaglei_forecast("blackout:us-power:ca:2025-01-08:500k", "500,000", "2025-01-08 12:00",
                                  "2025-01-09 12:00", "2025-01-09T12:00:00Z"),
            self._eaglei_forecast("blackout:us-power:ca:2025-01-08:1m", "1,000,000", "2025-01-08 12:00",
                                  "2025-01-09 12:00", "2025-01-09T12:00:00Z"),
            self._eaglei_forecast("blackout:us-power:ca:2026-11-14:50k", "50,000", "2026-11-14 00:00",
                                  "2026-11-21 00:00", "2026-11-21T00:00:00Z"),
        ]
        feed_doc = {"type": "FeatureCollection", "contract": "tic-feed/2", "program": "blackout",
                    "features": [{"type": "Feature", "geometry": None, "properties": it} for it in items]}
        ing = verdict.ingest_feed(self.tmp, feed_doc, draft_path=self.drafts, approved="Tom Brummer",
                                  when="2025-01-01T12:00:00Z")
        self.assertEqual(len(ing["registered"]), 3)
        res = auto_resolve.auto_resolve(root=self.tmp, now="2026-11-22T00:00:00Z", apply=True, by="Tom Brummer",
                                        eaglei_data=self._eaglei_data(), fetch=False)
        by_id = {r["id"]: r for r in res["proposed"]}
        yes = by_id["blackout:us-power:ca:2025-01-08:500k"]
        self.assertEqual((yes["outcome"], yes["needs_review"], yes["rule_type"]), (1, [], "blackout_eaglei"))
        # The broken report's runs join the stretch but can't carry it: 12 of the 21 runs above the line are clean.
        self.assertIn("from 2025-01-08 21:00 to 2025-01-09 03:45 UTC (21 runs above, 9 of them suspect)", yes["note"])
        self.assertIn("510,705 or more held over 4 clean runs", yes["note"])
        # Southern California Edison's broken report (Ventura County at 3.4 times its customers, 21:00-22:30 UTC)
        # reads over a million (7 runs, and 2 more at 02:45-03:00 UTC). Suspect runs can't make a Yes, so the No is
        # held for a person to check.
        no = by_id["blackout:us-power:ca:2025-01-08:1m"]
        self.assertEqual(no["outcome"], 0)
        self.assertEqual(len(no["needs_review"]), 1)
        self.assertIn("9 suspect run(s) over the line, from 2025-01-08 21:00 UTC", no["needs_review"][0])
        self.assertEqual(res["applied"], ["blackout:us-power:ca:2025-01-08:500k"])
        self.assertEqual(res["held_for_review"], ["blackout:us-power:ca:2025-01-08:1m"])
        # A year figshare has not published yet stays due and unresolved.
        skip = {r["id"]: r["reason"] for r in res["skipped"]}
        self.assertEqual(skip["blackout:us-power:ca:2026-11-14:50k"],
                         "EAGLE-I has not published 2026 on figshare yet (it lists 2014-2025)")

    def test_wake_portwatch_auto_resolve_and_program_calendar_sync(self):
        import auto_resolve
        from tic_doctrine import registers

        wake_fc = copy.deepcopy(ITEM)
        wake_fc.update({
            "id": "wake:chokepoint6:2026-11-08:depressed",
            "program": "wake",
            "requirement_ids": ["WK-PIR-1"],
            "probability": 0.85,
            "likelihood_term": "very_likely",
            "valid_until": "2026-11-08T23:59:59Z",
            "resolution": {
                "rule": (
                    "Yes if IMF PortWatch Daily_Chokepoints_Data (portid='chokepoint6') reports a 7-day rolling "
                    "n_total <= 38.0 vessels/day (<= 55% of the 70.2 vessels/day baseline) on 2026-11-08 UTC."
                ),
                "source": "IMF PortWatch Daily_Chokepoints_Data (https://portwatch.imf.org/)",
                "known_at": "2026-11-09T00:00:00Z",
                "resolve_by": "2026-11-11T00:00:00Z",
            },
        })
        feed_doc = {
            "type": "FeatureCollection",
            "contract": "tic-feed/2",
            "program": "wake",
            "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [43.33, 12.58]},
                          "properties": wake_fc}],
        }
        ing = verdict.ingest_feed(self.tmp, feed_doc, draft_path=self.drafts, approved="Tom Brummer",
                                  when="2026-11-01T12:00:00Z")
        self.assertEqual(ing["registered"], ["wake:chokepoint6:2026-11-08:depressed"])

        pw_fixtures = {"chokepoint6:2026-11-08": {"n_total": 29.4, "url": "https://portwatch.imf.org/"}}
        res = auto_resolve.auto_resolve(root=self.tmp, now="2026-11-09T12:00:00Z", apply=True, by="Tom Brummer",
                                        portwatch_data=pw_fixtures, fetch=False)
        self.assertEqual(res["applied"], ["wake:chokepoint6:2026-11-08:depressed"])
        resolved_rows = {r["id"]: r for r in verdict.read_lines(self.tmp / verdict.RESOLUTIONS)}
        self.assertEqual(resolved_rows["wake:chokepoint6:2026-11-08:depressed"]["outcome"], 1)

        # Another program's dated events merge into the calendar the same way Cordon's do.
        (self.tmp / "calendar").mkdir(exist_ok=True)
        (self.tmp / "calendar" / "events.json").write_text(json.dumps({"events": []}), encoding="utf-8")
        events = {
            "source": "headwind",
            "events": [{
                "id": "2026-11-10-bis-affiliates-rule-suspension-ends",
                "date": "2026-11-10",
                "title": "BIS Affiliates rule's one-year suspension ends unless stayed again",
                "kind": "rule_date",
                "countries": ["US", "CN"],
                "ref": "90-FR-50857",
                "source": {"publisher": "US Federal Register (BIS)", "url": "https://www.federalregister.gov/"},
            }],
        }
        sync_res = verdict.sync_calendar(self.tmp, events, today="2026-10-31")
        self.assertEqual(sync_res["added"], ["2026-11-10-bis-affiliates-rule-suspension-ends"])
        self.assertEqual(verdict.calendar_problems(self.tmp, registers.ids(self.tmp / "doctrine")), [])


if __name__ == "__main__":
    unittest.main()
