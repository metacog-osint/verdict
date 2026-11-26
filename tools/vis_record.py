"""Verdict's independent score of Vis's weather forecasts against METARs. Standard library only.

    python tools/vis_record.py sync                # fetch Vis's stores, score them, append a line to ledger/vis-skill.jsonl
    python tools/vis_record.py check [--base REF]  # chain intact, append-only since REF
    python tools/vis_record.py score               # print the record the site shows

Vis keeps two append-only stores in its own repository: history/verify/pairs.jsonl (each forecast's Amber and Red
probabilities per point and UTC day) and history/verify/observed.jsonl (what the point's METARs showed that day).
Verdict reads both, joins them with its own code, and computes the Brier score and reliability by hazard, level and
lead day. It does not reuse Vis's scoring code or take Vis's published figures on trust: it compares its own figures
with Vis's verification.json and logs whether they agree.

Each sync also checks that Vis only added to its stores: the lines held at the previous sync must be the first lines
of the new copy, byte for byte (by SHA-256). A store that was changed rather than appended to is refused.

Vis's forecasts are model probabilities at indicative thresholds, not questions registered in Verdict's ledger. This
record is kept apart from the ledger's Brier score.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verdict  # noqa: E402,F401  (puts the doctrine kit on sys.path)
from tic_doctrine import likelihood  # noqa: E402

LOG = Path("ledger") / "vis-skill.jsonl"
SCHEMA = "tic-vis-skill/1"
METHOD = "verdict-vis/1"
GENESIS = "0" * 64
UA = "Verdict/1.0 (The Intelligence Company)"
STORE_BASE = "https://raw.githubusercontent.com/metacog-osint/vis/main/history/verify/"
STORES = ("pairs", "observed")
PUBLISHED_URL = "https://vis.theintelligence.company/data/verification.json"
FIRST_DAY = "2026-11-19"  # Vis stored no forecast before this day
SCORED = ("wind", "vis", "heat", "cold")
LEVELS = ("amber", "red")
HAZARDS = {"wind": "Strong wind (gusts)", "vis": "Poor visibility", "heat": "Extreme heat", "cold": "Extreme cold"}
NOT_SCORED = ("Rain, snow, low cloud and icing are not scored: METARs do not measure them as Vis forecasts them.")
# Vis's own rule for a row it will call readable (tools/verify.py READ_MIN_*). Verdict applies it to its own counts.
READ_MIN_N, READ_MIN_EVENTS, READ_MIN_POINTS = 100, 10, 3
READABLE_WHEN = (f"at least {READ_MIN_N} forecasts and {READ_MIN_EVENTS} observed events, at {READ_MIN_POINTS} or "
                 "more points")
BRIER_TOLERANCE = 0.00015  # both sides round to 4 places
WHAT = ("Vis gives each point a daily probability of crossing an Amber and a Red threshold. Verdict joins each forecast "
        "to what the point's METARs showed that UTC day and scores it.")
CAVEAT = ("Thresholds are indicative (JP 3-59 gives no numeric limits). Vis's forecasts are not official forecasts and "
          "are not for flight planning. They are model probabilities, not questions registered in this ledger, so "
          "this record is kept apart from the ledger's Brier score.")


def line_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prefix_digest(text: str, lines: int) -> str:
    """SHA-256 of the first `lines` lines of a store, each with its newline."""
    return line_hash("".join(x + "\n" for x in text.splitlines()[:lines]))


def problems(text: str) -> list[str]:
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
        prev = line_hash(line)
    return errs


def read(root: Path = ROOT) -> list[dict]:
    path = root / LOG
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    errs = problems(text)
    if errs:
        raise SystemExit("vis record: " + "; ".join(errs))
    return [json.loads(x) for x in text.splitlines()]


def check(root: Path = ROOT, base: str | None = None) -> list[str]:
    path = root / LOG
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    errs = problems(text)
    if base:
        old = subprocess.run(["git", "show", f"{base}:{LOG.as_posix()}"], cwd=root, capture_output=True)
        if old.returncode == 0 and not text.startswith(old.stdout.decode("utf-8").replace("\r\n", "\n")):
            errs.append("the log was changed, not appended to")
    return errs


# --- scoring (Verdict's own; it does not import Vis's code) -----------------------------------------------------------

def _jsonl(text: str) -> list[dict]:
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def joined(pairs: list[dict], observed: list[dict]) -> list[dict]:
    """One row per forecast, point, day, hazard and level, with the observed outcome (1, 0 or None) and why not."""
    obs = {(o["zone"], o["day"], o["start"]): o for o in observed}
    rows = []
    for pr in pairs:
        if pr["day"] < FIRST_DAY:
            continue
        o = obs.get((pr["zone"], pr["day"], pr["start"]))
        for hz, vals in (pr.get("p") or {}).items():
            if hz not in SCORED:
                continue
            for i, lvl in enumerate(LEVELS):
                ob = (((o or {}).get("obs") or {}).get(hz) or {}).get(lvl) or {}
                rows.append({"run": pr.get("run") or pr["forecast"], "generated_at": pr["generated_at"],
                             "zone": pr["zone"], "icao": (o or {}).get("icao"), "day": pr["day"],
                             "lead_days": pr["lead_days"], "hazard": hz, "level": lvl,
                             "p": vals[i] if isinstance(vals, list) and len(vals) > i else None,
                             "outcome": ob.get("outcome") if o else None,
                             "why": (ob.get("why") or "unresolved") if o else "no observation line",
                             "threshold": ob.get("threshold")})
    return rows


def latest_per_run(rows: list[dict]) -> list[dict]:
    """One forecast per model run: the newest build of a run for each point, day, hazard and level."""
    keep = {}
    for r in rows:
        k = (r["run"], r["zone"], r["day"], r["hazard"], r["level"])
        if k not in keep or r["generated_at"] > keep[k]["generated_at"]:
            keep[k] = r
    return list(keep.values())


def _r(x, n=4):
    return None if x is None else round(x, n)


def cell(rows: list[dict]) -> dict:
    n = len(rows)
    ev = sum(r["outcome"] for r in rows)
    brier = sum((r["p"] - r["outcome"]) ** 2 for r in rows) / n if n else None
    base = ev / n if n else None
    clim = base * (1 - base) if n else None
    pts_ev = len({r["zone"] for r in rows if r["outcome"]})
    readable = n >= READ_MIN_N and ev >= READ_MIN_EVENTS and pts_ev >= READ_MIN_POINTS
    bands = {}
    for r in rows:
        term = likelihood.term_for(r["p"])["term"]
        b = bands.setdefault(term, [0, 0.0, 0])
        b[0] += 1
        b[1] += r["p"]
        b[2] += r["outcome"]
    order = [t["term"] for t in likelihood.terms()]
    return {"n": n, "events": ev, "points": len({r["zone"] for r in rows}), "points_with_events": pts_ev,
            "brier": _r(brier), "base_rate": _r(base),
            "skill": _r(1 - brier / clim, 3) if readable and clim else None, "readable": readable,
            "reliability": [{"band": t, "n": bands[t][0], "mean_p": _r(bands[t][1] / bands[t][0], 3),
                             "observed": _r(bands[t][2] / bands[t][0], 3)} for t in order if t in bands]}


def score(pairs: list[dict], observed: list[dict]) -> dict:
    use = latest_per_run(joined(pairs, observed))
    ok = [r for r in use if r["outcome"] in (0, 1) and isinstance(r["p"], (int, float)) and not isinstance(r["p"], bool)]
    why = {}
    for r in use:
        if not (r["outcome"] in (0, 1) and isinstance(r["p"], (int, float))):
            k = (r["hazard"], r["why"] if r["outcome"] not in (0, 1) else "no probability")
            why[k] = why.get(k, 0) + 1
    cells = []
    for hz in SCORED:
        for lvl in LEVELS:
            sub = [r for r in ok if r["hazard"] == hz and r["level"] == lvl]
            thr = sub[0]["threshold"] if sub else None
            cells.append({"hazard": hz, "level": lvl, "lead_days": "all", "threshold": thr, **cell(sub)})
            for lead in sorted({r["lead_days"] for r in sub}):
                c = cell([r for r in sub if r["lead_days"] == lead])
                if not c["readable"]:
                    c.pop("reliability")  # kept for every lead day once readable, and for all leads always (log size)
                cells.append({"hazard": hz, "level": lvl, "lead_days": lead, "threshold": thr, **c})
    reports = {}
    for o in observed:
        if o.get("icao") and o["day"] >= FIRST_DAY:
            reports[o["icao"]] = reports.get(o["icao"], 0) + (o.get("reports") or 0)
    days = sorted({r["day"] for r in use})
    return {"first_day": days[0] if days else None, "last_day": days[-1] if days else None,
            "counted": len(use), "resolved": len(ok), "unresolved": len(use) - len(ok),
            "unresolved_by_reason": [{"hazard": h, "why": w, "n": n} for (h, w), n in sorted(why.items())],
            "stations": len(reports), "stations_without_reports": sorted(k for k, v in reports.items() if v == 0),
            "cells": cells}


def compare(mine: dict, published: dict | None) -> dict:
    """Verdict's figures against Vis's own verification.json: counts and Brier per hazard, level and lead day."""
    if not published:
        return {"read": False, "agrees": None, "differences": []}
    theirs = {(c["hazard"], c["level"], str(c["lead_days"])): c for c in published.get("scores", [])}
    diffs = []
    for c in mine["cells"]:
        t = theirs.get((c["hazard"], c["level"], str(c["lead_days"])))
        if t is None:
            if c["n"]:
                diffs.append(f"{c['hazard']} {c['level']} lead {c['lead_days']}: not in Vis's figures")
            continue
        if t.get("n") != c["n"] or t.get("events") != c["events"]:
            diffs.append(f"{c['hazard']} {c['level']} lead {c['lead_days']}: {c['n']} forecasts and {c['events']} events "
                         f"here, {t.get('n')} and {t.get('events')} in Vis's")
        elif c["brier"] is not None and t.get("brier") is not None and abs(c["brier"] - t["brier"]) > BRIER_TOLERANCE:
            diffs.append(f"{c['hazard']} {c['level']} lead {c['lead_days']}: Brier {c['brier']} here, {t['brier']} in Vis's")
    same_store = published.get("forecasts_counted") == mine["counted"]
    return {"read": True, "generated_at": published.get("generated_at"), "same_store": same_store,
            "agrees": not diffs if same_store else None, "differences": diffs[:20]}


# --- sync ---------------------------------------------------------------------------------------------------------

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def store_problems(prev: dict | None, texts: dict[str, str]) -> list[str]:
    """Each store must begin with exactly the lines held at the previous sync."""
    errs = []
    for k in STORES:
        held = ((prev or {}).get("stores") or {}).get(k)
        if not held:
            continue
        if len(texts[k].splitlines()) < held["lines"]:
            errs.append(f"Vis's {k} store is shorter than at the previous sync")
        elif prefix_digest(texts[k], held["lines"]) != held["sha256"]:
            errs.append(f"Vis's {k} store was changed, not appended to")
    return errs


def make_line(prev_line: str | None, texts: dict[str, str], published: dict | None, now: datetime) -> dict:
    mine = score(_jsonl(texts["pairs"]), _jsonl(texts["observed"]))
    return {"schema": SCHEMA, "prev": line_hash(prev_line) if prev_line else GENESIS, "method": METHOD,
            "scored_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stores": {k: {"lines": len(texts[k].splitlines()), "sha256": prefix_digest(texts[k], len(texts[k].splitlines()))}
                       for k in STORES},
            **mine, "vis_published": compare(mine, published)}


def sync(root: Path = ROOT, texts: dict[str, str] | None = None, published: dict | None = None,
         now: datetime | None = None) -> int:
    """Append one line when Vis's stores grew. Returns 1 if a line was added, 0 if nothing changed."""
    now = now or datetime.now(timezone.utc)
    if texts is None:
        texts = {k: fetch(f"{STORE_BASE}{k}.jsonl") for k in STORES}
        try:
            published = json.loads(fetch(PUBLISHED_URL))
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            print(f"vis record: Vis's verification.json was not read ({e}); scoring without the comparison")
            published = None
    path = root / LOG
    held = path.read_text(encoding="utf-8") if path.exists() else ""
    errs = problems(held)
    if errs:
        raise SystemExit("vis record: " + "; ".join(errs))
    last_text = held.splitlines()[-1] if held else None
    last = json.loads(last_text) if last_text else None
    errs = store_problems(last, texts)
    if errs:
        raise SystemExit("vis record: refused: " + "; ".join(errs))
    if last and all(last["stores"][k]["lines"] == len(texts[k].splitlines()) for k in STORES):
        return 0
    line = json.dumps(make_line(last_text, texts, published, now), ensure_ascii=False, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return 1


# --- the record the site shows -------------------------------------------------------------------------------------

def record(root: Path = ROOT) -> dict:
    lines = read(root)
    last = lines[-1] if lines else None
    trend = [{"scored_at": x["scored_at"], "last_day": x["last_day"], "resolved": x["resolved"]} for x in lines]
    base = {"schema": "tic-vis-record/1", "as_of": last["scored_at"] if last else None, "what": WHAT, "caveat": CAVEAT,
            "not_scored": NOT_SCORED, "readable_when": READABLE_WHEN, "first_day": FIRST_DAY,
            "observations": "METARs from the NOAA Aviation Weather Center (aviationweather.gov), US public domain",
            "hazards": HAZARDS, "source": STORE_BASE, "published": PUBLISHED_URL, "syncs": len(lines), "trend": trend}
    if not last:
        return base | {"latest": None}
    keep = ("first_day", "last_day", "counted", "resolved", "unresolved", "unresolved_by_reason", "stations",
            "stations_without_reports", "cells", "vis_published", "stores")
    return base | {"latest": {k: last[k] for k in keep}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["sync", "check", "score"])
    ap.add_argument("--base")
    a = ap.parse_args(argv)
    if a.cmd == "sync":
        print(f"vis record: {sync()} line added")
    elif a.cmd == "check":
        errs = check(base=a.base)
        if errs:
            print("vis record: " + "; ".join(errs))
            return 1
        print("vis record: ok")
    else:
        print(json.dumps(record(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
