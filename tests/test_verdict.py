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
