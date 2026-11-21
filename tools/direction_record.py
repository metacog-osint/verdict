"""Verdict's scored record of COP's direction words. Standard library only.

    python tools/direction_record.py sync                # append COP's locked log to ledger/direction-log.jsonl
    python tools/direction_record.py check [--base REF]  # chain intact, first week respected, append-only since REF
    python tools/direction_record.py score               # print the record

Rule adopted by Tom, 21 Nov 2026. COP locks one word per place each Monday. The word held if the layers that moved
still pass COP's own move test in the following Monday-to-Sunday, against the frozen 14-day baseline logged with the
word. Beside it stands the plain reference: how often the same layers moved that way where they had not moved.

A direction word is not a forecast and carries no probability. This record stays out of the forecast ledger and the
Brier score. Words from before the first locked week, including every backfilled word, are refused.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verdict  # noqa: E402

LOG = Path("ledger") / "direction-log.jsonl"
SOURCE_URL = "https://raw.githubusercontent.com/metacog-osint/cop/direction-log/direction-log.jsonl"
SCHEMA = "tic-direction-log/1"
FIRST_WEEK = "2026-11-23"
SCORE_AFTER_DAYS = 10
GENESIS = "0" * 64
UA = "Verdict/1.0 (The Intelligence Company)"
WORDS = {"rising": (1, 2), "edging_up": (1, 1), "easing": (-1, 2), "edging_down": (-1, 1)}  # direction, layers needed
LABELS = {"rising": "Rising", "edging_up": "Edging up", "easing": "Easing", "edging_down": "Edging down"}
SKILL_HELD = (f"Skill is held until a word has {verdict.MIN_SKILL_N} independently scored calls, a shared press series "
              "counting once.")
WHAT = ("Each Monday COP locks one direction word per place. A word held if the layers that moved still passed COP's "
        "move test the following week, against the same baseline that earned the word.")
NOT_A_FORECAST = ("A direction word says which measurements moved against their own recent normal. It is not a warning "
                  "or a forecast and carries no probability, so this record is kept apart from the Brier score.")


def line_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def problems(text: str) -> list[str]:
    """What is wrong with a log's text: a broken chain, an unknown schema, or a call before the first locked week."""
    errs, prev = [], GENESIS
    for n, line in enumerate(text.splitlines(), 1):
        try:
            rec = json.loads(line)
        except ValueError:
            return errs + [f"line {n}: not JSON"]
        if rec.get("prev") != prev:
            return errs + [f"line {n}: the chain breaks"]
        if rec.get("schema") != SCHEMA:
            errs.append(f"line {n}: unknown schema")
        if str(rec.get("week", "")) < FIRST_WEEK:
            errs.append(f"line {n}: week {rec.get('week')} is before the first locked week")
        prev = line_hash(line)
    return errs


def read(root: Path = ROOT) -> list[dict]:
    path = root / LOG
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    errs = problems(text)
    if errs:
        raise SystemExit("direction log: " + "; ".join(errs))
    return [json.loads(x) for x in text.splitlines()]


def sync(root: Path = ROOT, fetched: str | None = None) -> int:
    """Take COP's log if it is valid and only adds to what is held. Returns the lines added."""
    if fetched is None:
        req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                fetched = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("direction log: COP has not locked a word yet")
                return 0
            raise
    path = root / LOG
    held = path.read_text(encoding="utf-8") if path.exists() else ""
    errs = problems(fetched)
    if errs:
        raise SystemExit("direction log: refused: " + "; ".join(errs))
    if not fetched.startswith(held):
        raise SystemExit("direction log: refused: COP's copy does not begin with the lines already held")
    if fetched != held:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fetched, encoding="utf-8", newline="\n")
    return len(fetched.splitlines()) - len(held.splitlines())


def check(root: Path = ROOT, base: str | None = None) -> list[str]:
    path = root / LOG
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    errs = problems(text)
    if base:
        old = subprocess.run(["git", "show", f"{base}:{LOG.as_posix()}"], cwd=root, capture_output=True)
        if old.returncode == 0 and not text.startswith(old.stdout.decode("utf-8").replace("\r\n", "\n")):
            errs.append("the log was changed, not appended to")
    return errs


def count_move(a, b, ra, rb, th):
    """COP's count test (tools/direction.py), with the thresholds logged beside the word."""
    n = a + b
    if n < th["min_events"]:
        return "steady"
    p = ra / (ra + rb)
    rate_a, rate_b = a / ra, b / rb
    if rate_a >= th["move_ratio"] * rate_b and \
            sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(a, n + 1)) <= th["move_p"]:
        return "up"
    if rate_a * th["move_ratio"] <= rate_b and \
            sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(0, a + 1)) <= th["move_p"]:
        return "down"
    return "steady"


def level_move(median, test, th):
    """COP's level test, against the logged median and quartiles of the days before the word."""
    if median >= th["level_ratio"] * test["prior_median"] and median > test["prior_q3"]:
        return "up"
    if median * th["level_ratio"] <= test["prior_median"] and median < test["prior_q1"]:
        return "down"
    return "steady"


def follow_state(test, reading, th):
    """The layer's state in the follow-up week against its frozen baseline, or None if it was not read enough."""
    if not test or not reading or reading.get("days_read", 0) < th["min_read_days"]:
        return None
    if test["kind"] == "count":
        return count_move(reading["a"], test["b"], reading["days_read"], test["rb"], th)
    return None if reading.get("median") is None else level_move(reading["median"], test, th)


def states(call, followup):
    tests = {l["layer"]: l.get("test") for l in call["layers"]}
    reads = {x["layer"]: x for x in followup.get("layers", [])}
    return {k: follow_state(t, reads.get(k), call["thresholds"]) for k, t in tests.items() if t}


def outcome(call, followup):
    """held, faded, reversed or void."""
    sign, need = WORDS[call["word"]]
    named = call["up"] if sign > 0 else call["down"]
    got = states(call, followup)
    if not named or any(got.get(x) is None for x in named):
        return "void"
    same = sum(got[x] == ("up" if sign > 0 else "down") for x in named)
    opposite = sum(got[x] == ("down" if sign > 0 else "up") for x in named)
    return "held" if same >= need else "reversed" if opposite > same else "faded"


def _sign(word):
    return WORDS[word][0] if word in WORDS else 0


def _week_before(week):
    return (date.fromisoformat(week) - timedelta(days=7)).isoformat()


def _round(x):
    return round(x, 4)


def references(calls, followups):
    """Per layer: place-weeks where it did not move, and how many then moved each way. A shared press series counts once.
    For two-layer words: Steady places with two or more layers read."""
    layer, seen, steady = {}, set(), {"n": 0, "up": 0, "down": 0}
    for c in calls:
        fu = followups.get((c["week"], c["area"]))
        if not fu:
            continue
        got = states(c, fu)
        for l in c["layers"]:
            if not l.get("test") or l["state"] not in ("steady", "quiet") or got.get(l["layer"]) is None:
                continue
            key = (c["week"], "press", c.get("press_zone")) if l["layer"] == "press" else (c["week"], c["area"], l["layer"])
            if key in seen:
                continue
            seen.add(key)
            row = layer.setdefault(l["layer"], {"n": 0, "up": 0, "down": 0})
            row["n"] += 1
            if got[l["layer"]] in ("up", "down"):
                row[got[l["layer"]]] += 1
        if c["word"] == "steady":
            read = [v for v in got.values() if v is not None]
            if len(read) >= 2:
                steady["n"] += 1
                steady["up"] += read.count("up") >= 2
                steady["down"] += read.count("down") >= 2
    return layer, steady


def record_for_rule(rule, lines):
    calls = [r for r in lines if r["type"] == "call" and r["rule"] == rule and r.get("scored")]
    followups = {(r["week"], r["area"]): r for r in lines if r["type"] == "followup" and r["rule"] == rule}
    by_place = {(c["area"], c["week"]): c for c in calls}
    new = []
    words = {w: {"label": LABELS[w], "new_calls": 0, "continued": 0, "pending": 0, "void": 0, "scored": 0.0, "held": 0.0,
                 "faded": 0.0, "reversed": 0.0, "press_only": {"scored": 0.0, "held": 0.0},
                 "with_other_layers": {"scored": 0.0, "held": 0.0}, "layers": {}} for w in WORDS}
    for c in sorted(calls, key=lambda x: (x["week"], x["area"])):
        if c["word"] not in WORDS:
            continue
        before = by_place.get((c["area"], _week_before(c["week"])))
        if before and _sign(before["word"]) == _sign(c["word"]):
            words[c["word"]]["continued"] += 1
            continue
        new.append(c)
    shared = {}
    for c in new:
        if c["up"] + c["down"] == ["press"] and c.get("press_zone"):
            shared.setdefault((c["week"], c["press_zone"], _sign(c["word"])), []).append(c["area"])
    layer_ref, steady_ref = references(calls, followups)
    used = {w: [] for w in WORDS}
    for c in new:
        row = words[c["word"]]
        row["new_calls"] += 1
        fu = followups.get((c["week"], c["area"]))
        if not fu:
            row["pending"] += 1
            continue
        result = outcome(c, fu)
        if result == "void":
            row["void"] += 1
            continue
        press_only = c["up"] + c["down"] == ["press"]
        weight = 1 / len(shared[(c["week"], c["press_zone"], _sign(c["word"]))]) if press_only and c.get("press_zone") else 1
        row["scored"] += weight
        row[result] += weight
        side = row["press_only" if press_only else "with_other_layers"]
        side["scored"] += weight
        side["held"] += weight if result == "held" else 0
        named = c["up"] if _sign(c["word"]) > 0 else c["down"]
        for x in named:
            row["layers"][x] = _round(row["layers"].get(x, 0) + weight)
        used[c["word"]].append((named, weight))
    for w, row in words.items():
        way = "up" if WORDS[w][0] > 0 else "down"
        if WORDS[w][1] == 2:
            n, hits = steady_ref["n"], steady_ref[way]
            share = hits / n if n else None
            row["reference"] = {"share": None if share is None else _round(share), "n": n,
                                "basis": "Steady places where two or more layers moved this way the following week"}
        else:
            total, sum_rate, layers = 0.0, 0.0, set()
            for named, weight in used[w]:
                ref = layer_ref.get(named[0])
                if ref and ref["n"]:
                    total += weight
                    sum_rate += weight * ref[way] / ref["n"]
                    layers.add(named[0])
            row["reference"] = {"share": _round(sum_rate / total) if total else None,
                                "n": sum(layer_ref[x]["n"] for x in layers),
                                "basis": "The same layers, in place-weeks where they had not moved"}
        share = row["held"] / row["scored"] if row["scored"] else None
        row["held_share"] = None if share is None else _round(share)
        ready = row["scored"] >= verdict.MIN_SKILL_N - 1e-9 and share is not None and row["reference"]["share"] is not None
        row["skill"] = _round(share - row["reference"]["share"]) if ready else None
        row["skill_withheld"] = None if ready else SKILL_HELD
        for k in ("scored", "held", "faded", "reversed"):
            row[k] = _round(row[k])
        for k in ("press_only", "with_other_layers"):
            row[k] = {x: _round(v) for x, v in row[k].items()}
    other = {"mixed": sum(c["word"] == "mixed" for c in calls), "steady": sum(c["word"] == "steady" for c in calls),
             "building": sum(c["word"] == "building" for c in calls)}
    return {"rule": rule, "words": words, "not_scored": other,
            "weeks_locked": len({c["week"] for c in calls}), "weeks_followed_up": len({k[0] for k in followups})}


def record(root: Path = ROOT) -> dict:
    lines = read(root)
    stamps = [r.get("logged_at") or r.get("read_at") or r.get("noted_at") for r in lines]
    weeks = sorted({r["week"] for r in lines if r["type"] == "call"})
    done = {r["week"] for r in lines if r["type"] == "followup"}
    waiting = [w for w in weeks if w not in done]
    next_score = (date.fromisoformat(waiting[0]) + timedelta(days=SCORE_AFTER_DAYS)).isoformat() if waiting else None
    return {"schema": "tic-direction-record/1", "as_of": max(stamps) if stamps else None, "what": WHAT,
            "not_a_forecast": NOT_A_FORECAST, "first_week": FIRST_WEEK, "skill_min_n": verdict.MIN_SKILL_N,
            "weeks": {"locked": len(weeks), "scored": len(done), "missed": sum(r["type"] == "missed" for r in lines)},
            "next_score": next_score, "source": SOURCE_URL,
            "rules": [record_for_rule(r, lines) for r in sorted({x["rule"] for x in lines if x["type"] == "call"})]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["sync", "check", "score"])
    ap.add_argument("--base")
    a = ap.parse_args(argv)
    if a.cmd == "sync":
        print(f"direction log: {sync()} lines added")
    elif a.cmd == "check":
        errs = check(base=a.base)
        if errs:
            print("direction log: " + "; ".join(errs))
            return 1
        print("direction log: ok")
    else:
        print(json.dumps(record(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
