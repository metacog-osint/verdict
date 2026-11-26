"""Vis's forecasts scored by Verdict: one per model run, stations without reports never filled, stores append-only."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vis_record as vr  # noqa: E402

NOW = datetime(2026, 11, 26, 13, 40, tzinfo=timezone.utc)


def pair(zone, day, wind, run="20261119T0000Z", gen="2026-11-19T03:00:00Z", lead=1):
    return {"forecast": f"{run}.forecast.gen-x.json.gz", "run": run, "generated_at": gen, "zone": zone, "day": day,
            "start": f"{day}T00:00Z", "lead_days": lead, "p": {"wind": [wind, 0.0, 82]}}


def obs(zone, day, amber, icao=None, reports=8, why=None):
    return {"method": "vis-verify/1", "zone": zone, "icao": icao or zone[:4].upper(), "day": day,
            "start": f"{day}T00:00Z", "steps": 8, "reports": reports,
            "obs": {"wind": {"amber": {"outcome": amber, "threshold": 65.0, "why": why},
                             "red": {"outcome": None if amber is None else 0, "threshold": 90.0, "why": why}}}}


def text(rows):
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)


class Scoring(unittest.TestCase):
    def test_one_forecast_per_run_newest_build(self):
        pairs = [pair("a", "2026-11-20", 0.9, gen="2026-11-19T03:00:00Z"),
                 pair("a", "2026-11-20", 0.2, gen="2026-11-19T09:00:00Z")]
        s = vr.score(pairs, [obs("a", "2026-11-20", 0)])
        amber = next(c for c in s["cells"] if c["level"] == "amber" and c["lead_days"] == "all")
        self.assertEqual(amber["n"], 1)
        self.assertAlmostEqual(amber["brier"], 0.04)

    def test_two_runs_both_count(self):
        pairs = [pair("a", "2026-11-20", 0.9), pair("a", "2026-11-20", 0.5, run="20261119T0600Z")]
        s = vr.score(pairs, [obs("a", "2026-11-20", 1)])
        amber = next(c for c in s["cells"] if c["level"] == "amber" and c["lead_days"] == "all")
        self.assertEqual((amber["n"], amber["events"]), (2, 2))
        self.assertAlmostEqual(amber["brier"], (0.01 + 0.25) / 2, places=4)

    def test_station_without_reports_is_counted_never_filled(self):
        pairs = [pair("a", "2026-11-20", 0.1), pair("b", "2026-11-20", 0.1)]
        o = [obs("a", "2026-11-20", 0), obs("b", "2026-11-20", None, icao="BBBB", reports=0, why="no reports from the station")]
        s = vr.score(pairs, o)
        self.assertEqual(s["stations_without_reports"], ["BBBB"])
        self.assertEqual((s["counted"], s["resolved"], s["unresolved"]), (4, 2, 2))
        self.assertIn({"hazard": "wind", "why": "no reports from the station", "n": 2}, s["unresolved_by_reason"])

    def test_before_first_day_is_left_out(self):
        s = vr.score([pair("a", "2026-11-18", 0.1)], [obs("a", "2026-11-18", 0)])
        self.assertEqual(s["counted"], 0)

    def test_skill_only_when_readable(self):
        pairs, o = [], []
        for i in range(120):
            z, d = f"z{i % 4}", f"2026-11-{20 + i // 4:02d}" if i < 40 else f"2026-12-{(i // 4) - 9:02d}"
            pairs.append(pair(z, d, 0.5))
            o.append(obs(z, d, 1 if i % 5 == 0 else 0))
        s = vr.score(pairs, o)
        amber = next(c for c in s["cells"] if c["level"] == "amber" and c["lead_days"] == "all")
        self.assertTrue(amber["readable"])
        self.assertLess(amber["skill"], 0)  # 0.5 every time against a 20% base rate
        red = next(c for c in s["cells"] if c["level"] == "red" and c["lead_days"] == "all")
        self.assertFalse(red["readable"])
        self.assertIsNone(red["skill"])

    def test_compare_agrees_and_differs(self):
        s = vr.score([pair("a", "2026-11-20", 0.2)], [obs("a", "2026-11-20", 0)])
        pub = {"generated_at": "x", "forecasts_counted": s["counted"],
               "scores": [{k: c[k] for k in ("hazard", "level", "lead_days", "n", "events", "brier")} for c in s["cells"]]}
        self.assertTrue(vr.compare(s, pub)["agrees"])
        pub["scores"][0]["brier"] = 0.5
        out = vr.compare(s, pub)
        self.assertFalse(out["agrees"])
        self.assertTrue(out["differences"])
        pub["forecasts_counted"] = 99
        self.assertIsNone(vr.compare(s, pub)["agrees"])
        self.assertIsNone(vr.compare(s, None)["agrees"])


class Sync(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.p = [pair("a", "2026-11-20", 0.2)]
        self.o = [obs("a", "2026-11-20", 0)]

    def tearDown(self):
        shutil.rmtree(self.root)

    def texts(self):
        return {"pairs": text(self.p), "observed": text(self.o)}

    def test_appends_then_nothing_then_grows(self):
        self.assertEqual(vr.sync(self.root, self.texts(), None, NOW), 1)
        self.assertEqual(vr.sync(self.root, self.texts(), None, NOW), 0)
        self.p.append(pair("a", "2026-11-21", 0.1))
        self.o.append(obs("a", "2026-11-21", 0))
        self.assertEqual(vr.sync(self.root, self.texts(), None, NOW), 1)
        lines = vr.read(self.root)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["stores"]["pairs"]["lines"], 2)
        self.assertEqual(vr.check(self.root), [])
        rec = vr.record(self.root)
        self.assertEqual(rec["latest"]["last_day"], "2026-11-21")
        self.assertEqual(rec["syncs"], 2)

    def test_refuses_a_changed_store(self):
        vr.sync(self.root, self.texts(), None, NOW)
        self.p[0]["p"]["wind"][0] = 0.9
        self.p.append(pair("a", "2026-11-21", 0.1))
        with self.assertRaises(SystemExit):
            vr.sync(self.root, self.texts(), None, NOW)

    def test_refuses_a_shorter_store(self):
        self.p.append(pair("a", "2026-11-21", 0.1))
        vr.sync(self.root, self.texts(), None, NOW)
        self.p.pop()
        with self.assertRaises(SystemExit):
            vr.sync(self.root, self.texts(), None, NOW)

    def test_a_broken_chain_is_caught(self):
        vr.sync(self.root, self.texts(), None, NOW)
        self.p.append(pair("a", "2026-11-21", 0.1))
        vr.sync(self.root, self.texts(), None, NOW)
        path = self.root / vr.LOG
        first, second = path.read_text(encoding="utf-8").splitlines()
        path.write_text(first.replace('"resolved":2', '"resolved":3') + "\n" + second + "\n", encoding="utf-8")
        self.assertTrue(vr.check(self.root))

    def test_empty_record(self):
        self.assertIsNone(vr.record(self.root)["latest"])


if __name__ == "__main__":
    unittest.main()
