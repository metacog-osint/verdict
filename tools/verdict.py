"""Verdict: register, check, resolve and score forecasts. Standard library only.

    python tools/verdict.py check [--base <git ref>]     ledger valid, linted, append-only since <ref>
    python tools/verdict.py register drafts/<file>.json  move approved drafts into the ledger, timestamped now
    python tools/verdict.py resolve <id> <0|1> --url <evidence> [--note "..."] [--by "..."]
    python tools/verdict.py score [--out scores.json]    Brier score, Murphy decomposition, skill, by program
    python tools/verdict.py due [--days 14] [--markdown]  forecasts to resolve now, and those coming up
    python tools/verdict.py calendar [--feed out.geojson] check the event calendar; export it as a contract v2 feed

The ledger is two append-only JSON-lines files in git: ledger/forecasts.jsonl (feed contract tic-feed/2 forecast
items, each with its resolution rule written at issue time) and ledger/resolutions.jsonl. A registered line is never
edited or removed; a correction is a new line (msg_type update or cancel, with supersedes). Git commit history is
the timestamp lock.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doctrine" / "kit" / "python"))
from tic_doctrine import contract, registers  # noqa: E402

FORECASTS = Path("ledger") / "forecasts.jsonl"
RESOLUTIONS = Path("ledger") / "resolutions.jsonl"
CALENDAR = Path("calendar") / "events.json"
BINS = 10
EVENT_KINDS = {"data_release", "rule_date", "bulletin_expiry", "mandate_deadline", "election", "summit", "deadline",
               "advisory_change"}
CORDON_EVENTS = "https://cordon.theintelligence.company/data/events.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise SystemExit(f"{path}:{n}: not JSON ({e})")
    return out


def append_lines(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n")


def forecast_problems(item: dict, regs: dict) -> list[str]:
    """Contract errors plus Verdict's own rules for one ledger forecast."""
    errs = [f"{std}: {m}" for std, sev, m in contract.check(item, regs) if sev == "error"]
    if item.get("kind") != "forecast":
        errs.append("verdict: only kind=forecast items go in the ledger")
    if item.get("status") != "actual":
        errs.append("verdict: a registered forecast has status 'actual'")
    if not item.get("approved_by"):
        errs.append("verdict: approved_by missing (who set the final probability)")
    res = item.get("resolution") or {}
    known = res.get("known_at")
    if not known:
        errs.append("verdict: resolution.known_at missing (when the outcome first becomes knowable)")
    elif item.get("published_at") and _t(item["published_at"]) >= _t(known):
        errs.append("verdict: registered at or after the outcome became knowable (not a pre-registration)")
    return errs


def check(root: Path = ROOT, base: str | None = None) -> list[str]:
    regs = registers.ids(root / "doctrine")
    errs = []
    forecasts = read_lines(root / FORECASTS)
    seen = set()
    for i, f in enumerate(forecasts, 1):
        fid = f.get("id")
        if fid in seen:
            errs.append(f"forecasts line {i}: duplicate id {fid!r}")
        seen.add(fid)
        errs += [f"forecasts line {i} ({fid}): {m}" for m in forecast_problems(f, regs)]
        if f.get("supersedes") and f["supersedes"] not in seen:
            errs.append(f"forecasts line {i} ({fid}): supersedes unknown or later id {f['supersedes']!r}")
    by_id = {f.get("id"): f for f in forecasts}
    resolved = set()
    for i, r in enumerate(read_lines(root / RESOLUTIONS), 1):
        rid = r.get("id")
        if rid not in by_id:
            errs.append(f"resolutions line {i}: unknown forecast id {rid!r}")
            continue
        if rid in resolved:
            errs.append(f"resolutions line {i}: {rid} resolved twice")
        resolved.add(rid)
        if r.get("outcome") not in (0, 1):
            errs.append(f"resolutions line {i}: outcome must be 0 or 1")
        if not (r.get("evidence") or {}).get("url"):
            errs.append(f"resolutions line {i}: evidence.url missing")
        if r.get("resolved_at") and _t(r["resolved_at"]) < _t(by_id[rid]["published_at"]):
            errs.append(f"resolutions line {i}: resolved before the forecast was registered")
    errs += calendar_problems(root, regs)
    if base:
        errs += append_only(root, base)
    return errs


def due(root: Path = ROOT, now: str | None = None, days: int = 14) -> dict:
    """Unresolved forecasts whose outcome is knowable now ('due'), and those knowable within `days` ('soon')."""
    t_now = _t(now or now_utc())
    resolved = {r.get("id") for r in read_lines(root / RESOLUTIONS)}
    fc = read_lines(root / FORECASTS)
    superseded = {f.get("supersedes") for f in fc if f.get("supersedes")}
    out = {"now": now or now_utc(), "due": [], "soon": []}
    for f in fc:
        if f["id"] in resolved or f["id"] in superseded or f.get("msg_type") == "cancel":
            continue
        res = f.get("resolution") or {}
        known = _t(res["known_at"])
        row = {"id": f["id"], "title": f["title"], "probability": f["probability"], "known_at": res["known_at"],
               "resolve_by": res.get("resolve_by"), "rule": res.get("rule"), "source": res.get("source")}
        if known <= t_now:
            out["due"].append(row)
        elif (known - t_now).total_seconds() <= days * 86400:
            out["soon"].append(row)
    for k in ("due", "soon"):
        out[k].sort(key=lambda r: r["known_at"])
    return out


def due_markdown(d: dict) -> str:
    lines = [f"Checked {d['now']}.", ""]
    if d["due"]:
        lines += ["## Due now", ""]
        for r in d["due"]:
            lines += [f"- **{r['title']}** (`{r['id']}`, {round(r['probability'] * 100)}%), resolve by {r['resolve_by']}",
                      f"  - Rule: {r['rule']}", f"  - Source: {r['source']}",
                      f"  - `python tools/verdict.py resolve {r['id']} 1|0 --url <evidence>`"]
    else:
        lines += ["Nothing is due."]
    if d["soon"]:
        lines += ["", "## Coming up", ""] + [f"- {r['known_at'][:10]}: {r['title']} (`{r['id']}`)" for r in d["soon"]]
    return "\n".join(lines) + "\n"


def read_calendar(root: Path = ROOT) -> list[dict]:
    p = root / CALENDAR
    return json.loads(p.read_text(encoding="utf-8"))["events"] if p.exists() else []


def calendar_item(ev: dict, generated: str) -> dict:
    """One calendar event as a feed contract v2 item: a scheduled fact (kind observation), not a forecast."""
    when = ev.get("at") or f"{ev['date']}T00:00:00Z"
    src = ev["source"]
    return {
        "contract": "tic-feed/2", "id": f"verdict:calendar:{ev['id']}", "program": "verdict",
        "title": f"{'On' if ev.get('occurred') else 'Scheduled'} {ev['date']}: {ev['title']}", "kind": "observation",
        "tier": "routine",
        "status": "actual", "msg_type": "new", "human_review": "full", "us_person_data": "none",
        "status_class": "reported" if ev.get("verified", True) else "claimed", "product_category": "current",
        "data_as_of": f"{ev['added']}T00:00:00Z", "published_at": generated, "valid_until": when,
        "countries": ev.get("countries", []), "requirement_ids": ["VD-PIR-1"], "method_version": "verdict-calendar-1.0",
        "precision_class": "none", "geometry": None, "visual": {"type": "page", "url": src["url"]},
        "sources": [{"id": src.get("id", ev["id"]), "publisher": src["publisher"], "url": src["url"],
                     "licence": src.get("licence", "public"), "descriptor": src.get("descriptor", ev["title"]),
                     "accessed_at": f"{ev['added']}T00:00:00Z"}],
        "event": {"kind": ev["kind"], "date": ev["date"], "at": ev.get("at"), "verified": ev.get("verified", True),
                  "forecast_ids": ev.get("forecast_ids", []), "note": ev.get("note"),
                  "occurred": bool(ev.get("occurred")), "origin": ev.get("origin", "verdict"), "ref": ev.get("ref")},
    }


def sync_calendar(root: Path, incoming: dict, today: str | None = None) -> dict:
    """Merge another program's dated events (Cordon's data/events.json) into calendar/events.json.

    Events from that origin dated today or later are replaced by the incoming set (a renewed bulletin moves to its new
    expiry); past ones stay as history. Hand-entered events are never touched. An event whose ref is a bulletin
    number (CZIB-...) is linked to every forecast whose resolution rule names it.
    """
    today = today or now_utc()[:10]
    origin = incoming.get("source", "unknown")
    path = root / CALENDAR
    cal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"events": []}
    before = {e["id"]: e for e in cal["events"]}
    fc = read_lines(root / FORECASTS)
    fresh = []
    for e in incoming.get("events", []):
        if e["date"] < today and e["id"] not in before and not e.get("occurred"):
            continue  # an expiry already past when first seen
        prev = before.get(e["id"], {})
        item = {k: e[k] for k in ("id", "date", "title", "kind", "countries", "source", "ref") if k in e}
        item["origin"] = origin
        if e.get("occurred"):
            item["occurred"] = True
        item["added"] = prev.get("added", today)
        ref = e.get("ref") or ""
        links = [f["id"] for f in fc if ref.startswith("CZIB-") and ref in (f.get("resolution") or {}).get("rule", "")]
        item["forecast_ids"] = sorted(set(links) | set(prev.get("forecast_ids", [])))
        if not item["forecast_ids"]:
            item.pop("forecast_ids")
        fresh.append(item)
    fresh_ids = {e["id"] for e in fresh}
    kept = [e for e in cal["events"] if e["id"] not in fresh_ids
            and not (e.get("origin") == origin and e["date"] >= today)]
    cal["events"] = sorted(kept + fresh, key=lambda e: (e["date"], e["id"]))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    dropped = [i for i in before if i not in {e["id"] for e in cal["events"]}]
    return {"origin": origin, "incoming": len(fresh), "added": sorted(fresh_ids - set(before)),
            "dropped": dropped, "total": len(cal["events"])}


def calendar_problems(root: Path, regs: dict) -> list[str]:
    errs, seen = [], set()
    fids = {f.get("id") for f in read_lines(root / FORECASTS)}
    for ev in read_calendar(root):
        tag = f"calendar {ev.get('id')}"
        missing = [k for k in ("id", "date", "title", "kind", "source", "added") if not ev.get(k)]
        if missing:
            errs.append(f"{tag}: missing {missing}")
            continue
        if ev["id"] in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(ev["id"])
        if ev["kind"] not in EVENT_KINDS:
            errs.append(f"{tag}: kind must be one of {sorted(EVENT_KINDS)}")
        for fid in ev.get("forecast_ids", []):
            if fid not in fids:
                errs.append(f"{tag}: forecast_ids names unknown forecast {fid!r}")
        errs += [f"{tag}: {std}: {m}" for std, sev, m in contract.check(calendar_item(ev, now_utc()), regs)
                 if sev == "error"]
    return errs


def calendar_feed(root: Path = ROOT, generated: str | None = None) -> dict:
    generated = generated or now_utc()
    items = [calendar_item(ev, generated) for ev in sorted(read_calendar(root), key=lambda e: e.get("at") or e["date"])]
    return {"type": "FeatureCollection", "contract": "tic-feed/2", "program": "verdict", "generated_at": generated,
            "licence": "Verdict (MIT); each event's source as cited",
            "attribution": "Verdict event calendar, The Intelligence Company",
            "language_key": "https://github.com/metacog-osint/tic-doctrine/blob/main/docs/language-key.md",
            "features": [{"type": "Feature", "geometry": None, "properties": {k: v for k, v in it.items()
                                                                              if k != "geometry"}} for it in items]}


def append_only(root: Path, base: str) -> list[str]:
    """Every ledger line present at <base> must still be there, unchanged and in order."""
    errs = []
    for rel in (FORECASTS, RESOLUTIONS):
        old = subprocess.run(["git", "show", f"{base}:{rel.as_posix()}"], cwd=root, capture_output=True, text=True,
                             encoding="utf-8")
        if old.returncode != 0:
            continue  # the file did not exist at base
        before = [ln for ln in old.stdout.splitlines() if ln.strip()]
        now_path = root / rel
        after = [ln for ln in now_path.read_text(encoding="utf-8").splitlines() if ln.strip()] if now_path.exists() else []
        if after[:len(before)] != before:
            errs.append(f"{rel}: a registered line was edited, removed or reordered since {base} (append only)")
    return errs


def register(draft_path: Path, root: Path = ROOT, when: str | None = None) -> list[str]:
    """Append every approved draft to the ledger with status actual and published_at = now; return their ids."""
    doc = json.loads(draft_path.read_text(encoding="utf-8"))
    regs = registers.ids(root / "doctrine")
    when = when or now_utc()
    existing = {f.get("id") for f in read_lines(root / FORECASTS)}
    ready, problems = [], []
    for d in doc["drafts"]:
        if not d.get("approved") or d.get("registered_at"):
            continue
        item = {k: v for k, v in d["item"].items()}
        item.update({"status": "actual", "published_at": when, "approved_by": d["approved"]})
        if item["id"] in existing:
            problems.append(f"{item['id']}: already in the ledger")
            continue
        errs = forecast_problems(item, regs)
        if errs:
            problems += [f"{item['id']}: {e}" for e in errs]
            continue
        ready.append((d, item))
    if problems:
        raise SystemExit("not registered:\n  " + "\n  ".join(problems))
    append_lines(root / FORECASTS, [it for _, it in ready])
    for d, _ in ready:
        d["registered_at"] = when
    draft_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return [it["id"] for _, it in ready]


def resolve(fid: str, outcome: int, url: str, note: str = "", by: str = "", root: Path = ROOT,
            when: str | None = None) -> dict:
    if fid not in {f.get("id") for f in read_lines(root / FORECASTS)}:
        raise SystemExit(f"{fid}: not in the ledger")
    if fid in {r.get("id") for r in read_lines(root / RESOLUTIONS)}:
        raise SystemExit(f"{fid}: already resolved")
    rec = {"id": fid, "outcome": int(outcome), "resolved_at": when or now_utc(),
           "evidence": {"url": url, "accessed_at": when or now_utc()}, "note": note, "resolved_by": by}
    append_lines(root / RESOLUTIONS, [rec])
    return rec


def _brier(pairs):
    """Brier score with Murphy's decomposition (reliability - resolution + uncertainty) over BINS bins."""
    n = len(pairs)
    bs = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    bins = {}
    for p, o in pairs:
        bins.setdefault(min(int(p * BINS), BINS - 1), []).append((p, o))
    rel = sum(len(v) * (sum(p for p, _ in v) / len(v) - sum(o for _, o in v) / len(v)) ** 2 for v in bins.values()) / n
    res = sum(len(v) * (sum(o for _, o in v) / len(v) - base) ** 2 for v in bins.values()) / n
    unc = base * (1 - base)
    return {"n": n, "brier": round(bs, 4), "base_rate": round(base, 4), "reliability": round(rel, 4),
            "resolution": round(res, 4), "uncertainty": round(unc, 4),
            "skill_vs_base_rate": round(1 - bs / unc, 4) if unc > 0 else None,
            "note": "decomposition is approximate within bins; skill_vs_base_rate uses the sample's own base rate"}


def score(root: Path = ROOT) -> dict:
    fc = {f["id"]: f for f in read_lines(root / FORECASTS)}
    live = {fid for fid, f in fc.items() if f.get("msg_type") != "cancel"}
    superseded = {f.get("supersedes") for f in fc.values() if f.get("supersedes")}
    pairs = {}
    for r in read_lines(root / RESOLUTIONS):
        f = fc.get(r["id"])
        if not f or r["id"] not in live or r["id"] in superseded:
            continue
        pairs.setdefault(f["program"], []).append((float(f["probability"]), int(r["outcome"])))
        pairs.setdefault("all", []).append((float(f["probability"]), int(r["outcome"])))
    return {"generated_at": now_utc(), "registered": len(fc), "resolved": len(pairs.get("all", [])),
            "by_program": {k: _brier(v) for k, v in sorted(pairs.items())}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base")
    r = sub.add_parser("register")
    r.add_argument("drafts")
    s = sub.add_parser("resolve")
    s.add_argument("id")
    s.add_argument("outcome", type=int, choices=(0, 1))
    s.add_argument("--url", required=True)
    s.add_argument("--note", default="")
    s.add_argument("--by", default="")
    sc = sub.add_parser("score")
    sc.add_argument("--out")
    du = sub.add_parser("due")
    du.add_argument("--days", type=int, default=14)
    du.add_argument("--markdown", action="store_true")
    ca = sub.add_parser("calendar")
    ca.add_argument("--feed")
    ca.add_argument("--sync", nargs="?", const=CORDON_EVENTS, help="merge another program's events (default: Cordon)")
    a = ap.parse_args(argv)
    if a.cmd == "due":
        d = due(ROOT, days=a.days)
        print(due_markdown(d) if a.markdown else json.dumps(d, indent=2), end="" if a.markdown else "\n")
        return 0
    if a.cmd == "calendar":
        if a.sync:
            if a.sync.startswith(("http://", "https://")):
                import urllib.request
                req = urllib.request.Request(a.sync, headers={"User-Agent": "Verdict (The Intelligence Company)"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    incoming = json.loads(r.read(4_000_000))
            else:
                incoming = json.loads(Path(a.sync).read_text(encoding="utf-8"))
            print(json.dumps(sync_calendar(ROOT, incoming)))
        errs = calendar_problems(ROOT, registers.ids(ROOT / "doctrine"))
        for e in errs:
            print("  -", e)
        if a.feed and not errs:
            Path(a.feed).write_text(json.dumps(calendar_feed(ROOT), indent=1, ensure_ascii=False) + "\n",
                                    encoding="utf-8", newline="\n")
        print(f"Calendar: {len(read_calendar(ROOT))} events: {'PASS' if not errs else f'FAIL ({len(errs)} problems)'}")
        return 1 if errs else 0
    if a.cmd == "check":
        errs = check(ROOT, a.base)
        for e in errs:
            print("  -", e)
        n = len(read_lines(ROOT / FORECASTS))
        print(f"Verdict ledger: {n} forecasts, {len(read_lines(ROOT / RESOLUTIONS))} resolutions: "
              f"{'PASS' if not errs else f'FAIL ({len(errs)} problems)'}")
        return 1 if errs else 0
    if a.cmd == "register":
        ids = register(Path(a.drafts))
        print("registered:", ", ".join(ids) if ids else "nothing (no approved, unregistered drafts)")
        return 0
    if a.cmd == "resolve":
        print(json.dumps(resolve(a.id, a.outcome, a.url, a.note, a.by)))
        return 0
    out = score()
    text = json.dumps(out, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
