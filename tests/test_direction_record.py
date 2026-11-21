"""The direction record: frozen baseline, one call per run, a shared series once, and no backfilled word."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import direction_record as dr  # noqa: E402

TH = {"min_events": 4, "min_read_days": 5, "move_p": 0.10, "move_ratio": 1.5, "level_ratio": 1.25}
PRESS = {"kind": "level", "recent_median": 160, "prior_median": 120, "prior_q1": 110, "prior_q3": 130, "ra": 7, "rb": 14}
NOTICES = {"kind": "count", "a": 12, "b": 6, "ra": 7, "rb": 14}
W1, W2 = "2026-11-23", "2026-11-30"


def call(area, word, week=W1, up=("press",), down=(), zone=None, steady=()):
    layers = []
    for x in up:
        layers.append({"layer": x, "program": "p", "state": "up", "test": PRESS if x == "press" else NOTICES})
    for x in down:
        layers.append({"layer": x, "program": "p", "state": "down", "test": PRESS if x == "press" else NOTICES})
    for x in steady:
        layers.append({"layer": x, "program": "p", "state": "steady", "test": PRESS if x == "press" else NOTICES})
    return {"type": "call", "schema": dr.SCHEMA, "rule": "1", "join_version": "0.1", "week": week,
            "logged_at": f"{week}T00:21:00Z", "area": area, "name": area, "word": word, "press_zone": zone or area,
            "scored": True, "up": list(up), "down": list(down), "layers": layers, "thresholds": TH}


def follow(area, week=W1, **layers):
    return {"type": "followup", "schema": dr.SCHEMA, "rule": "1", "week": week, "read_at": "2026-12-03T00:21:00Z",
            "area": area, "layers": [{"layer": k} | v for k, v in layers.items()]}


def level(median, days=7):
    return {"kind": "level", "days_read": days, "median": median}


def count(a, days=7):
    return {"kind": "count", "days_read": days, "a": a}


def chain(rows):
    out, prev = [], dr.GENESIS
    for r in rows:
        text = json.dumps(r | {"prev": prev}, separators=(",", ":"), sort_keys=True)
        out.append(text)
        prev = dr.line_hash(text)
    return "".join(x + "\n" for x in out)


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "ledger").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def score(self, rows):
        (self.tmp / dr.LOG).write_text(chain(rows), encoding="utf-8", newline="\n")
        return dr.record(self.tmp)["rules"][0]["words"]

    def test_the_tests_match_cops(self):
        self.assertEqual(dr.count_move(12, 6, 7, 14, TH), "up")
        self.assertEqual(dr.count_move(1, 20, 7, 14, TH), "down")
        self.assertEqual(dr.count_move(7, 12, 7, 14, TH), "steady")
        self.assertEqual(dr.count_move(2, 0, 7, 14, TH), "steady")

    def test_held_faded_reversed_against_the_frozen_baseline(self):
        c = call("gulf", "edging_up")
        self.assertEqual(dr.outcome(c, follow("gulf", press=level(160))), "held", "the same level still reads up")
        self.assertEqual(dr.outcome(c, follow("gulf", press=level(125))), "faded")
        self.assertEqual(dr.outcome(c, follow("gulf", press=level(90))), "reversed")

    def test_a_layer_not_read_enough_voids_the_call(self):
        self.assertEqual(dr.outcome(call("gulf", "edging_up"), follow("gulf", press=level(160, days=4))), "void")

    def test_rising_needs_two_named_layers(self):
        c = call("x", "rising", up=("press", "notices"))
        self.assertEqual(dr.outcome(c, follow("x", press=level(160), notices=count(12))), "held")
        self.assertEqual(dr.outcome(c, follow("x", press=level(160), notices=count(3))), "faded")

    def test_a_run_counts_once(self):
        rows = [call("gulf", "edging_up"), call("gulf", "edging_up", week=W2), follow("gulf", press=level(160))]
        row = self.score(rows)["edging_up"]
        self.assertEqual((row["new_calls"], row["continued"], row["scored"]), (1, 1, 1))

    def test_a_shared_press_series_counts_once(self):
        rows = [call("gulf", "edging_up", zone="z"), call("iran", "edging_up", zone="z"),
                follow("gulf", press=level(160)), follow("iran", press=level(160))]
        row = self.score(rows)["edging_up"]
        self.assertEqual((row["new_calls"], row["scored"], row["held"]), (2, 1, 1))
        self.assertEqual(row["press_only"]["scored"], 1)

    def test_the_reference_is_the_same_layer_where_it_had_not_moved(self):
        rows = [call("a", "edging_up"), follow("a", press=level(160)),
                call("b", "steady", up=(), steady=("press", "notices")), follow("b", press=level(160), notices=count(3)),
                call("c", "steady", up=(), steady=("press", "notices")), follow("c", press=level(125), notices=count(3))]
        ref = self.score(rows)["edging_up"]["reference"]
        self.assertEqual((ref["share"], ref["n"]), (0.5, 2))

    def test_skill_is_held_below_thirty(self):
        row = self.score([call("a", "edging_up"), follow("a", press=level(160))])["edging_up"]
        self.assertIsNone(row["skill"])
        self.assertIn("30", row["skill_withheld"])

    def test_a_word_before_the_first_locked_week_is_refused(self):
        text = chain([call("gulf", "edging_up", week="2026-11-16")])
        self.assertTrue(any("before the first" in e for e in dr.problems(text)))
        with self.assertRaises(SystemExit):
            dr.sync(self.tmp, fetched=text)

    def test_sync_only_appends(self):
        first = chain([call("a", "edging_up")])
        self.assertEqual(dr.sync(self.tmp, fetched=first), 1)
        longer = chain([call("a", "edging_up"), follow("a", press=level(160))])
        self.assertEqual(dr.sync(self.tmp, fetched=longer), 1)
        changed = chain([call("a", "edging_down", up=(), down=("press",))])
        with self.assertRaises(SystemExit):
            dr.sync(self.tmp, fetched=changed)

    def test_a_changed_line_breaks_the_chain(self):
        text = chain([call("a", "edging_up"), follow("a", press=level(160))])
        self.assertTrue(any("chain" in e for e in dr.problems(text.replace("edging_up", "rising", 1))))

    def test_the_record_never_touches_the_forecast_ledger(self):
        source = (ROOT / "tools" / "direction_record.py").read_text(encoding="utf-8")
        self.assertNotIn("FORECASTS", source)
        self.assertNotIn("RESOLUTIONS", source)


if __name__ == "__main__":
    unittest.main()
