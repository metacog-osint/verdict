"""Verdict: register, check, resolve and score forecasts. Standard library only.

    python tools/verdict.py check [--base <git ref>]     ledger valid, linted; since <ref>: append-only, and each new
                                                         line published within an hour of the commit that adds it
    python tools/verdict.py register drafts/<file>.json  move approved drafts into the ledger, timestamped now
    python tools/verdict.py resolve <id> <0|1> --url <evidence> [--note "..."] [--by "..."]
                                                         refused before the rule's check time (resolution.check_after)
    python tools/verdict.py score [--out scores.json]    Brier score, Murphy decomposition, by program, a declared
                                                         cluster counted once; skill against the base rate and against
                                                         each forecast's own reference from MIN_SKILL_N resolved
    python tools/verdict.py cluster <id> --members <id> <id> ... --reason "..."
                                                         declare linked forecasts, before any can be known, to be
                                                         scored as one (ledger/clusters.jsonl)
    python tools/verdict.py due [--days 14] [--markdown]  forecasts to resolve now, and those coming up
    python tools/verdict.py calendar [--feed out.geojson] check the event calendar; export it as a contract v2 feed
    python tools/verdict.py ingest-feed <path-or-url> [--drafts drafts/ingested.json] [--approve "<name>"]
    python tools/verdict.py auto-resolve [--now <iso>] [--apply --by "<name>"] [--out drafts/resolutions_proposed.json]
                                         propose resolutions for due forecasts (tools/auto_resolve.py); --apply
                                         writes only those that need no review, under the named resolver

The ledger is two append-only JSON-lines files in git: ledger/forecasts.jsonl (feed contract tic-feed/2 forecast
items, each with its resolution rule written at issue time) and ledger/resolutions.jsonl. A registered line is never
edited or removed; a correction is a new line (msg_type update or cancel, with supersedes). Git commit history is
the timestamp lock. ledger/clusters.jsonl, append-only too, names forecasts that one decision settles together.
"""
import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doctrine" / "kit" / "python"))
from tic_doctrine import contract, registers  # noqa: E402

FORECASTS = Path("ledger") / "forecasts.jsonl"
RESOLUTIONS = Path("ledger") / "resolutions.jsonl"
CLUSTERS = Path("ledger") / "clusters.jsonl"
EVIDENCE_DIR = Path("ledger") / "evidence"
DROPPED = Path("calendar") / "dropped.jsonl"
PROPOSALS = Path("drafts") / "resolutions_proposed.json"
# A fetched body is stored in the evidence file only when it is this small and the caller says the source allows it;
# otherwise the file keeps the rows read and the body's SHA-256.
BODY_MAX = 64 * 1024
CALENDAR = Path("calendar") / "events.json"
BINS = 10
# Resolved forecasts a program needs before its skill is given, a declared cluster counting once. Below this the base
# rate comes from the same few outcomes, so the figure mostly shows which questions were picked: one easy No and one
# easy Yes score 0.95.
MIN_SKILL_N = 30
SKILL_HELD = (f"Skill against the base rate is held until every program counted has {MIN_SKILL_N} resolved forecasts, "
              "a cluster of linked questions counting once. With fewer, the base rate comes from the same few "
              "outcomes, so the figure would mostly show which questions were picked.")
SKILL_UNDEFINED = ("Skill against the base rate is held while every resolved forecast has the same outcome: there is "
                   "no spread in the base rate to score against.")
REFERENCE_HELD = (f"Skill against each question's own reference is held until every program counted has "
                  f"{MIN_SKILL_N} resolved forecasts that carry one, a cluster counting once. A reference is the "
                  "probability a plain rule (a base rate, a market, no change) gave the question when it was "
                  "registered; forecasts registered before 15 Nov 2026 carry none.")
REFERENCE_UNDEFINED = ("Skill against each question's own reference is held while the references scored perfectly: "
                       "there is nothing to improve on.")
PUBLISH_WINDOW_S = 3600  # a new line's published_at must be within this many seconds of the commit that adds it
EVENT_KINDS = {"data_release", "rule_date", "bulletin_expiry", "mandate_deadline", "election", "summit", "deadline",
               "advisory_change"}
CORDON_EVENTS = "https://cordon.theintelligence.company/data/events.json"
HEADWIND_EVENTS = "https://headwind.theintelligence.company/data/events.json"
# Programs whose dated events the daily sync pulls, by the name `calendar --sync <name>` takes.
SYNC_SOURCES = {"cordon": CORDON_EVENTS, "headwind": HEADWIND_EVENTS}
# Refs that name one document, so a forecast whose resolution rule names it can be linked: an EASA bulletin
# (CZIB-2026-04, from Cordon) or a Federal Register document number (2026-22793, from Headwind). Refs such as
# FOMC, ECB or FFPI name a series of events and link nothing.
LINKABLE_REF = re.compile(r"^(CZIB-\S+|\d{4}-\d{5})$")
# The earliest check a rule allows, in the rule's own words: "on a check after 00:00 UTC on 17 Nov 2026".
CHECK_AFTER_RE = re.compile(r"check after (\d{2}):(\d{2}) UTC on (\d{1,2}) ([A-Za-z]{3,9}) (\d{4})")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
UTC_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _is_time(s) -> bool:
    """A UTC time written the ledger's way: 2026-11-17T00:00:00Z."""
    if not isinstance(s, str) or not UTC_TIME_RE.fullmatch(s):
        return False
    try:
        _t(s)
    except ValueError:
        return False
    return True


def _rule_check_after(rule: str) -> str | None:
    """The check time a rule states in words, as a UTC time. None if it states none; "" if what it states is not a
    real time."""
    m = CHECK_AFTER_RE.search(rule or "")
    if not m:
        return None
    hh, mm, day, month, year = m.groups()
    if month[:3].lower() not in MONTHS:
        return ""
    stamp = f"{year}-{MONTHS.index(month[:3].lower()) + 1:02d}-{int(day):02d}T{hh}:{mm}:00Z"
    return stamp if _is_time(stamp) else ""


def check_after(item: dict) -> str | None:
    """The earliest time a forecast's rule allows a check, as a UTC time, or None when it sets none.

    resolution.check_after where the line carries it; otherwise the time the rule states in its own words, so a line
    registered before the field existed is held to its rule without being edited.
    """
    res = item.get("resolution") or {}
    explicit = res.get("check_after")
    return explicit if _is_time(explicit) else (_rule_check_after(res.get("rule") or "") or None)


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
    item_regs = regs if item.get("program") == "verdict" else None
    errs = [f"{std}: {m}" for std, sev, m in contract.check(item, item_regs) if sev == "error"]
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
    stated, explicit = _rule_check_after(res.get("rule") or ""), res.get("check_after")
    if stated == "":
        errs.append("verdict: the rule's check time is not a real time (write 'check after HH:MM UTC on D Mon YYYY')")
    if explicit is not None and not _is_time(explicit):
        errs.append("verdict: resolution.check_after must be a UTC time like 2026-11-17T00:00:00Z")
    elif explicit and stated and explicit != stated:
        errs.append(f"verdict: resolution.check_after {explicit} is not the check time the rule gives ({stated})")
    ca = check_after(item)
    try:
        too_late = bool(ca and res.get("resolve_by")) and _t(ca) > _t(res["resolve_by"])
    except (TypeError, ValueError):
        too_late = False  # a resolve_by that is not a time is the contract's error to report
    if too_late:
        errs.append(f"verdict: check_after {ca} is later than resolve_by, so the rule could never be checked in time")
    if "reference" in item:
        ref = item["reference"]
        p = ref.get("probability") if isinstance(ref, dict) else None
        if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1:
            errs.append("verdict: reference.probability must be a number from 0 to 1 (what a plain rule gives)")
        if not isinstance(ref, dict) or not str(ref.get("basis") or "").strip():
            errs.append("verdict: reference.basis missing (the plain rule the reference comes from, and its source)")
    return errs


def reference_probability(item: dict) -> float | None:
    """The probability a plain rule gave the question at registration (item.reference), or None when it has none."""
    ref = item.get("reference")
    p = ref.get("probability") if isinstance(ref, dict) else None
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def cluster_problems(root: Path, forecasts: list[dict]) -> list[str]:
    """Each cluster names two or more registered forecasts, none in another cluster, declared before any member's
    outcome could be known: a cluster chosen after the fact could pick which results count once."""
    by_id = {f.get("id"): f for f in forecasts}
    errs, seen, member_of = [], set(), {}
    for i, c in enumerate(read_lines(root / CLUSTERS), 1):
        cid = c.get("id")
        tag = f"clusters line {i} ({cid})"
        if not cid:
            errs.append(f"clusters line {i}: id missing")
            continue
        if cid in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(cid)
        members = c.get("members")
        if not isinstance(members, list) or len(set(members)) < 2 or len(set(members)) != len(members):
            errs.append(f"{tag}: members must list two or more distinct forecast ids")
            continue
        if not str(c.get("reason") or "").strip():
            errs.append(f"{tag}: reason missing (why one decision settles these together)")
        for m in members:
            if m not in by_id:
                errs.append(f"{tag}: unknown forecast {m!r}")
            elif m in member_of:
                errs.append(f"{tag}: {m} is already in cluster {member_of[m]}")
            else:
                member_of[m] = cid
        if not _is_time(c.get("declared_at")):
            errs.append(f"{tag}: declared_at must be a UTC time like 2026-11-15T18:00:00Z")
            continue
        known = [(by_id[m].get("resolution") or {}).get("known_at") for m in members if m in by_id]
        known = [k for k in known if _is_time(k)]
        if known and _t(c["declared_at"]) >= min(_t(k) for k in known):
            errs.append(f"{tag}: declared at {c['declared_at']}, not before its first member became knowable "
                        f"({min(known, key=_t)})")
    return errs


def cluster_weights(root: Path = ROOT) -> dict[str, tuple[str, float]]:
    """{forecast id: (cluster id, weight)}: each member of a k-forecast cluster weighs 1/k in the scores."""
    out = {}
    for c in read_lines(root / CLUSTERS):
        members = c.get("members") or []
        for m in members:
            out.setdefault(m, (c.get("id"), 1 / len(members)))
    return out


def declare_cluster(cid: str, members: list[str], reason: str, root: Path = ROOT, when: str | None = None) -> dict:
    """Append one cluster line, declared now, if it passes cluster_problems."""
    rec = {"id": cid, "members": members, "declared_at": when or now_utc(), "reason": reason}
    path = root / CLUSTERS
    old = path.read_text(encoding="utf-8") if path.exists() else None
    append_lines(path, [rec])
    errs = cluster_problems(root, read_lines(root / FORECASTS))
    if errs:
        if old is None:
            path.unlink()
        else:
            path.write_text(old, encoding="utf-8", newline="\n")
        raise SystemExit("not declared:\n  " + "\n  ".join(errs))
    return rec


def check(root: Path = ROOT, base: str | None = None, now: str | None = None) -> list[str]:
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
        ca = check_after(by_id[rid])
        read = [t for t in (r.get("resolved_at"), (r.get("evidence") or {}).get("accessed_at")) if t]
        if ca and any(_t(t) < _t(ca) for t in read):
            errs.append(f"resolutions line {i}: {rid} was resolved, or its evidence read, before the rule allows a "
                        f"check ({ca})")
    errs += cluster_problems(root, forecasts)
    errs += calendar_problems(root, regs)
    if base:
        errs += append_only(root, base)
        errs += published_when_committed(root, base, now)
    return errs


def due(root: Path = ROOT, now: str | None = None, days: int = 14) -> dict:
    """Unresolved forecasts ready to resolve now ('due'), and those that will be within `days` ('soon').

    Ready (due_at) is when the outcome is knowable (known_at) or, where the rule sets an earliest check time
    (check_after) later than that, the check time: a forecast is not due while its rule forbids the check.
    """
    t_now = _t(now or now_utc())
    resolved = {r.get("id") for r in read_lines(root / RESOLUTIONS)}
    fc = read_lines(root / FORECASTS)
    superseded = {f.get("supersedes") for f in fc if f.get("supersedes")}
    out = {"now": now or now_utc(), "due": [], "soon": []}
    for f in fc:
        if f["id"] in resolved or f["id"] in superseded or f.get("msg_type") == "cancel":
            continue
        res = f.get("resolution") or {}
        ca = check_after(f)
        due_at = ca if ca and _t(ca) > _t(res["known_at"]) else res["known_at"]
        ready = _t(due_at)
        row = {"id": f["id"], "title": f["title"], "probability": f["probability"], "known_at": res["known_at"],
               "check_after": ca, "due_at": due_at, "resolve_by": res.get("resolve_by"), "rule": res.get("rule"),
               "source": res.get("source")}
        if ready <= t_now:
            out["due"].append(row)
        elif (ready - t_now).total_seconds() <= days * 86400:
            out["soon"].append(row)
    for k in ("due", "soon"):
        out[k].sort(key=lambda r: _t(r["due_at"]))
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
        lines += ["", "## Coming up", ""]
        for r in d["soon"]:
            held = (f", knowable from {r['known_at'][:10]} but the rule allows a check only after {r['check_after']}"
                    if r["due_at"] != r["known_at"] else "")
            lines.append(f"- {r['due_at'][:10]}: {r['title']} (`{r['id']}`){held}")
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


def record_dropped(root: Path, lost: list, today: str) -> int:
    """Append events the sync did not keep to calendar/dropped.jsonl (append-only, never rewritten).

    reason `first_seen_past`: the origin sent an event already past on the day it was first seen (and not marked
    occurred). reason `origin_stopped_sending`: a future event the origin no longer sends and no forecast is linked to.
    An (id, reason, date) already in the file is not written again, so a daily sync does not repeat itself. Returns
    the number of lines written."""
    if not lost:
        return 0
    path = root / DROPPED
    have = {(r.get("id"), r.get("reason"), (r.get("event") or {}).get("date")) for r in read_lines(path)}
    new = []
    for reason, ev in lost:
        key = (ev.get("id"), reason, ev.get("date"))
        if key in have:
            continue
        have.add(key)
        new.append({"id": ev.get("id"), "reason": reason, "dropped_on": today, "origin": ev.get("origin"),
                    "event": ev})
    if new:
        append_lines(path, new)
    return len(new)


def sync_calendar(root: Path, incoming: dict, today: str | None = None) -> dict:
    """Merge another program's dated events (Cordon's or Headwind's data/events.json) into calendar/events.json.

    Events from that origin dated today or later are replaced by the incoming set; past ones stay as history.
    Hand-entered events are never touched. An event whose ref names one document (an EASA bulletin, CZIB-...; a
    Federal Register document number) is linked to every forecast whose resolution rule names it.

    A renewed bulletin, or a rule whose stay is extended, arrives as a new event at its next date (the origin keys
    events by date) and the old event stops being sent. That must not move a forecast, so two rules hold for every
    origin's events, Headwind's included:
    - an event carries a forecast only when it is dated on or before that forecast's known_at, so the next date is
      never linked to a forecast about this one, and a link that breaks the rule is removed;
    - an event a forecast is linked to is never dropped, whatever its date: it is the date that forecast is about.
    """
    today = today or now_utc()[:10]
    origin = incoming.get("source", "unknown")
    path = root / CALENDAR
    cal = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"events": []}
    before = {e["id"]: e for e in cal["events"]}
    fc = read_lines(root / FORECASTS)
    known = {f.get("id"): ((f.get("resolution") or {}).get("known_at") or "")[:10] for f in fc}

    def about(ids, date: str) -> list[str]:
        """Of these forecasts, the ones an event of this date may carry. An id the ledger does not hold is kept, for
        `check` to report."""
        return [i for i in ids if not known.get(i) or date <= known[i]]

    fresh = []
    lost = []  # events that leave the calendar here; kept in calendar/dropped.jsonl (retention, 18 Nov 2026)
    for e in incoming.get("events", []):
        if e["date"] < today and e["id"] not in before and not e.get("occurred"):
            lost.append(("first_seen_past", {**e, "origin": origin}))
            continue  # an expiry already past when first seen
        prev = before.get(e["id"], {})
        item = {k: e[k] for k in ("id", "date", "title", "kind", "countries", "source", "ref") if k in e}
        item["origin"] = origin
        if e.get("occurred"):
            item["occurred"] = True
        item["added"] = prev.get("added", today)
        ref = e.get("ref") or ""
        links = [f["id"] for f in fc if LINKABLE_REF.match(ref) and ref in (f.get("resolution") or {}).get("rule", "")]
        item["forecast_ids"] = about(sorted(set(links) | set(prev.get("forecast_ids", []))), e["date"])
        if not item["forecast_ids"]:
            item.pop("forecast_ids")
        fresh.append(item)
    fresh_ids = {e["id"] for e in fresh}
    kept, kept_linked = [], []
    for e in cal["events"]:
        if e["id"] in fresh_ids:
            continue
        if e.get("origin") == origin:
            ids = about(e.get("forecast_ids", []), e["date"])
            if ids != e.get("forecast_ids", []):
                e = {k: v for k, v in e.items() if k != "forecast_ids"}
                if ids:
                    e["forecast_ids"] = ids
            if e["date"] >= today:  # no longer sent by the origin
                if not ids:
                    lost.append(("origin_stopped_sending", e))
                    continue
                kept_linked.append(e["id"])
        kept.append(e)
    cal["events"] = sorted(kept + fresh, key=lambda e: (e["date"], e["id"]))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    record_dropped(root, lost, today)
    dropped = [i for i in before if i not in {e["id"] for e in cal["events"]}]
    return {"origin": origin, "incoming": len(fresh), "added": sorted(fresh_ids - set(before)),
            "dropped": dropped, "kept_linked": kept_linked, "total": len(cal["events"])}


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
            "licence": "Verdict (code GNU AGPL-3.0); each event's source as cited",
            "attribution": "Verdict event calendar, The Intelligence Company",
            "language_key": "https://github.com/metacog-osint/tic-doctrine/blob/main/docs/language-key.md",
            "features": [{"type": "Feature", "geometry": None, "properties": {k: v for k, v in it.items()
                                                                              if k != "geometry"}} for it in items]}


def append_only(root: Path, base: str) -> list[str]:
    """Every ledger line present at <base> must still be there, unchanged and in order."""
    errs = []
    for rel in (FORECASTS, RESOLUTIONS, CLUSTERS):
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


def published_when_committed(root: Path, base: str, now: str | None = None) -> list[str]:
    """Every forecast and cluster line added since <base> was made public when it says it was.

    The commit that adds a line is what makes it public, so the line's own time (a forecast's published_at, a
    cluster's declared_at) must be within an hour of that commit, and the commit must be earlier than the time the
    outcome becomes knowable (a forecast's known_at; for a cluster, its first member's). A line dated earlier than its
    commit is back-dated. A line not committed yet is held to the current time. The commit time is the committer date,
    which the committer sets; GitHub's own record of the push and of this check's run is the outside timestamp.
    """
    def git(*args) -> str | None:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else None

    def lines(text: str | None) -> list[str]:
        return [ln for ln in (text or "").splitlines() if ln.strip()]

    if git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") is None:
        return [f"--base {base}: not a commit in this repository, so new lines cannot be checked against their commits"]
    known_of = {f.get("id"): (f.get("resolution") or {}).get("known_at") for f in read_lines(root / FORECASTS)}

    def cluster_known(c: dict) -> str | None:
        ks = [known_of.get(m) for m in c.get("members") or []]
        ks = [k for k in ks if _is_time(k)]
        return min(ks, key=_t) if ks else None

    targets = ((FORECASTS, "forecasts", "published_at", lambda it: (it.get("resolution") or {}).get("known_at"),
                "known_at", "its outcome became knowable"),
               (CLUSTERS, "clusters", "declared_at", cluster_known, "its first member's known_at",
                "its first member's outcome became knowable"))
    t_now, errs = _t(now or now_utc()), []
    for path_rel, label, stamp_key, deadline_of, deadline_name, deadline_text in targets:
        rel = path_rel.as_posix()
        path = root / path_rel
        before = lines(git("show", f"{base}:{rel}"))
        new = lines(path.read_text(encoding="utf-8") if path.exists() else "")[len(before):]
        if not new:
            continue
        # The commits since base that touch the file, oldest first, then HEAD itself to catch anything else.
        commits = lines(git("log", "--reverse", "--topo-order", "--format=%h %cI", f"{base}..HEAD", "--", rel))
        commits += lines(git("log", "-1", "--format=%h %cI", "HEAD"))
        added = {}  # line -> (commit, its time): the first commit that holds the line
        for entry in commits:
            sha, when = entry.split(" ", 1)
            held = set(lines(git("show", f"{sha}:{rel}")))
            for ln in new:
                if ln in held:
                    added.setdefault(ln, (sha, when))
        for n, ln in enumerate(new, len(before) + 1):
            item = json.loads(ln)
            tag = f"{label} line {n} ({item.get('id')})"
            sha, when = added.get(ln, (None, None))
            t_commit = _t(when) if when else t_now
            stamp = t_commit.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            where = f"commit {sha} of {stamp}" if sha else f"now, {stamp} (not committed yet)"
            try:
                gap = (t_commit - _t(item[stamp_key])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue  # a time that is not a time is the contract's (or cluster check's) error to report
            if abs(gap) > PUBLISH_WINDOW_S:
                errs.append(f"{tag}: {stamp_key} {item[stamp_key]} is {abs(gap) / 3600:.1f} h "
                            f"{'before' if gap > 0 else 'after'} {where}; it must be within an hour of the commit "
                            f"that adds the line")
            known = deadline_of(item)
            try:
                late = bool(known) and t_commit >= _t(known)
            except (TypeError, ValueError):
                late = False
            if late:
                errs.append(f"{tag}: {where} is not earlier than {deadline_name} {known}; the line was not public "
                            f"before {deadline_text}")
    return errs


def ingest_feed(root: Path, feed_doc: dict, draft_path: Path | None = None,
                approved: str | None = None, when: str | None = None) -> dict:
    """Ingest kind=forecast items from a contract-v2 feed into a drafts file, and optionally register them."""
    when = when or now_utc()
    t_when = _t(when)
    draft_path = draft_path or (root / "drafts" / "ingested.json")
    doc = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {"drafts": []}
    existing_ledger = {f.get("id") for f in read_lines(root / FORECASTS)}
    existing_drafts = {d.get("item", {}).get("id") for d in doc.get("drafts", [])}
    added_ids = []
    skipped = 0
    for feat in feed_doc.get("features") or []:
        props, geom = contract.split(feat)
        if props.get("kind") != "forecast":
            continue
        res = props.get("resolution") or {}
        known_at = res.get("known_at")
        fid = props.get("id")
        if not fid or not known_at:
            skipped += 1
            continue
        if fid in existing_ledger or fid in existing_drafts or t_when >= _t(known_at):
            skipped += 1
            continue
        item = {k: v for k, v in props.items()}
        item["geometry"] = geom
        item["status"] = "draft"
        doc.setdefault("drafts", []).append({"approved": approved, "item": item})
        existing_drafts.add(fid)
        added_ids.append(fid)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    registered = register(draft_path, root=root, when=when) if approved else []
    return {"ingested": added_ids, "registered": registered, "skipped": skipped, "draft_path": str(draft_path)}


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


def evidence_file(fid: str) -> Path:
    """ledger/evidence/<id>.json; characters Windows paths cannot hold (the colons in an id) become underscores."""
    return EVIDENCE_DIR / (re.sub(r"[^A-Za-z0-9._-]", "_", fid) + ".json")


def fetch_body(url: str, timeout: int = 40, limit: int = 8_000_000) -> dict:
    """Read `url` now. Returns {source_url, fetched_at, body (bytes), body_sha256, body_bytes}."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Verdict (The Intelligence Company)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(limit)
    return {"source_url": url, "fetched_at": now_utc(), "body": body,
            "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body)}


def evidence_from_proposals(root: Path, fid: str) -> dict | None:
    """The `evidence` block auto-resolve stored for this forecast in drafts/resolutions_proposed.json, if any."""
    p = root / PROPOSALS
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    for prop in doc.get("proposed", []):
        if prop.get("id") == fid and prop.get("evidence"):
            return {**prop["evidence"], "from_proposal_generated_at": doc.get("generated_at")}
    return None


def write_evidence(root: Path, fid: str, outcome: int, when: str, url: str, evidence: dict | None,
                   store_body: bool = False) -> dict:
    """Write ledger/evidence/<id>.json (create-only: an existing file is never overwritten). Returns the record.

    `evidence` may hold rows (the evidence rows read), source_url, fetched_at, body_sha256, body_bytes and body
    (bytes). The body itself is kept only when store_body is true and it is at most BODY_MAX bytes; otherwise the
    file keeps the rows and the hash."""
    ev = dict(evidence or {})
    body = ev.pop("body", None)
    rec = {"id": fid, "outcome": int(outcome), "resolved_at": when, "source_url": ev.get("source_url") or url,
           "fetched_at": ev.get("fetched_at"), "body_sha256": ev.get("body_sha256"),
           "body_bytes": ev.get("body_bytes"), "rows": ev.get("rows", [])}
    for k in ("from_proposal_generated_at", "hash_note"):
        if ev.get(k):
            rec[k] = ev[k]
    if body is not None and not rec["body_sha256"]:
        rec["body_sha256"] = hashlib.sha256(body).hexdigest()
        rec["body_bytes"] = len(body)
    rec["body_stored"] = False
    if body is not None and store_body and len(body) <= BODY_MAX:
        try:
            rec["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            rec["body_b64"] = base64.b64encode(body).decode("ascii")
        rec["body_stored"] = True
    if not (ev.get("rows") or rec["body_sha256"]):
        rec["snapshot"] = "none: no rows or fetched body were available when this was resolved"
    p = root / evidence_file(fid)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("x", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
    except FileExistsError:
        raise SystemExit(f"{fid}: {evidence_file(fid)} already exists; evidence is never overwritten")
    return rec


def resolve(fid: str, outcome: int, url: str, note: str = "", by: str = "", root: Path = ROOT,
            when: str | None = None, evidence: dict | None = None, store_body: bool = False) -> dict:
    fc = {f.get("id"): f for f in read_lines(root / FORECASTS)}
    if fid not in fc:
        raise SystemExit(f"{fid}: not in the ledger")
    if fid in {r.get("id") for r in read_lines(root / RESOLUTIONS)}:
        raise SystemExit(f"{fid}: already resolved")
    when = when or now_utc()
    ca = check_after(fc[fid])
    if ca and _t(when) < _t(ca):
        raise SystemExit(f"{fid}: not resolved. Its rule allows a check only after {ca}; it is {when}.")
    write_evidence(root, fid, outcome, when, url, evidence, store_body)  # first: no evidence file, no resolution
    rec = {"id": fid, "outcome": int(outcome), "resolved_at": when,
           "evidence": {"url": url, "accessed_at": when, "snapshot": evidence_file(fid).as_posix()},
           "note": note, "resolved_by": by}
    append_lines(root / RESOLUTIONS, [rec])
    return rec


def _brier(rows):
    """Brier score with Murphy's decomposition (reliability - resolution + uncertainty) over BINS bins.

    Each row is (probability, outcome) or (probability, outcome, weight, reference). A member of a declared k-forecast
    cluster weighs 1/k, so one decision that settles six questions counts once; n_independent is the total weight.
    brier_per_forecast weighs every forecast as one.

    Skill is given only from MIN_SKILL_N independent resolved forecasts; below that the figure is None and the
    *_withheld field says why. Against the base rate it uses the sample's own outcomes. Against the reference it uses
    each question's own reference probability (item.reference, written at registration), over the forecasts that
    carry one. Brier and n are always given.
    """
    rows = [(float(r[0]), int(r[1]), float(r[2]) if len(r) > 2 else 1.0, r[3] if len(r) > 3 else None) for r in rows]
    n, w_all = len(rows), sum(w for _, _, w, _ in rows)
    bs = sum(w * (p - o) ** 2 for p, o, w, _ in rows) / w_all
    base = sum(w * o for _, o, w, _ in rows) / w_all
    bins = {}
    for p, o, w, _ in rows:
        bins.setdefault(min(int(p * BINS), BINS - 1), []).append((p, o, w))

    def mean(v, i):
        return sum(x[i] * x[2] for x in v) / sum(x[2] for x in v)

    rel = sum(sum(x[2] for x in v) * (mean(v, 0) - mean(v, 1)) ** 2 for v in bins.values()) / w_all
    res = sum(sum(x[2] for x in v) * (mean(v, 1) - base) ** 2 for v in bins.values()) / w_all
    unc = base * (1 - base)
    withheld = SKILL_HELD if w_all < MIN_SKILL_N - 1e-9 else None if unc > 0 else SKILL_UNDEFINED

    with_ref = [(p, o, w, q) for p, o, w, q in rows if q is not None]
    w_ref = sum(w for _, _, w, _ in with_ref)
    bs_f = sum(w * (p - o) ** 2 for p, o, w, _ in with_ref) / w_ref if w_ref else None
    bs_ref = sum(w * (q - o) ** 2 for _, o, w, q in with_ref) / w_ref if w_ref else None
    ref_withheld = (REFERENCE_HELD if w_ref < MIN_SKILL_N - 1e-9 else None if bs_ref > 0 else REFERENCE_UNDEFINED)
    return {"n": n, "n_independent": round(w_all, 2), "brier": round(bs, 4),
            "brier_per_forecast": round(sum((p - o) ** 2 for p, o, _, _ in rows) / n, 4),
            "base_rate": round(base, 4), "reliability": round(rel, 4),
            "resolution": round(res, 4), "uncertainty": round(unc, 4),
            "skill_vs_base_rate": None if withheld else round(1 - bs / unc, 4), "skill_withheld": withheld,
            "n_with_reference": len(with_ref), "brier_reference": None if bs_ref is None else round(bs_ref, 4),
            "skill_vs_reference": None if ref_withheld else round(1 - bs_f / bs_ref, 4),
            "reference_withheld": ref_withheld,
            "note": "a declared cluster counts once (each member weighs 1/k); decomposition is approximate within "
                    "bins; skill_vs_base_rate uses the sample's own base rate and skill_vs_reference each question's "
                    f"own reference, each given only from {MIN_SKILL_N} independent resolved forecasts"}


def score(root: Path = ROOT) -> dict:
    fc = {f["id"]: f for f in read_lines(root / FORECASTS)}
    live = {fid for fid, f in fc.items() if f.get("msg_type") != "cancel"}
    superseded = {f.get("supersedes") for f in fc.values() if f.get("supersedes")}
    weights = cluster_weights(root)
    pairs = {}
    for r in read_lines(root / RESOLUTIONS):
        f = fc.get(r["id"])
        if not f or r["id"] not in live or r["id"] in superseded:
            continue
        row = (float(f["probability"]), int(r["outcome"]), weights.get(r["id"], (None, 1.0))[1],
               reference_probability(f))
        pairs.setdefault(f["program"], []).append(row)
        pairs.setdefault("all", []).append(row)
    by = {k: _brier(v) for k, v in sorted(pairs.items())}
    # All programs together: only once each one has enough, so a large program cannot carry the pooled figure.
    others = [v for k, v in by.items() if k != "all"]
    if any(v["n_independent"] < MIN_SKILL_N - 1e-9 for v in others):
        by["all"].update({"skill_vs_base_rate": None, "skill_withheld": SKILL_HELD})
    if any(v["reference_withheld"] == REFERENCE_HELD for v in others):
        by["all"].update({"skill_vs_reference": None, "reference_withheld": REFERENCE_HELD})
    clusters = read_lines(root / CLUSTERS)
    return {"generated_at": now_utc(), "registered": len(fc), "resolved": len(pairs.get("all", [])),
            "skill_min_n": MIN_SKILL_N, "clusters": len(clusters), "by_program": by}


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
    s.add_argument("--fetch", action="store_true",
                   help="read --url now and keep its fetch time and SHA-256 in ledger/evidence/<id>.json "
                        "(unless auto-resolve already left evidence for this forecast)")
    s.add_argument("--store-body", action="store_true",
                   help="with --fetch: also keep the body when it is 64 KB or less (only where the source's licence "
                        "allows redistribution)")
    sc = sub.add_parser("score")
    sc.add_argument("--out")
    cl = sub.add_parser("cluster")
    cl.add_argument("id", help="e.g. verdict:cluster:easa-czib-2026-11-16")
    cl.add_argument("--members", nargs="+", required=True)
    cl.add_argument("--reason", required=True)
    du = sub.add_parser("due")
    du.add_argument("--days", type=int, default=14)
    du.add_argument("--markdown", action="store_true")
    ca = sub.add_parser("calendar")
    ca.add_argument("--feed")
    ca.add_argument("--sync", nargs="?", const=CORDON_EVENTS,
                    help=f"merge another program's events: {' or '.join(SYNC_SOURCES)}, a URL or a file (default: Cordon)")
    ig = sub.add_parser("ingest-feed")
    ig.add_argument("source", help="path or URL to a contract-v2 feed.geojson")
    ig.add_argument("--drafts", default="drafts/ingested.json")
    ig.add_argument("--approve", default=None)
    ar = sub.add_parser("auto-resolve")
    ar.add_argument("--now", default=None, help="ISO UTC time that decides what is due (default: now)")
    ar.add_argument("--apply", action="store_true",
                    help="write proposals that need no review to ledger/resolutions.jsonl (needs --by)")
    ar.add_argument("--by", default=None, help="the person resolving, recorded as resolved_by")
    ar.add_argument("--out", default="drafts/resolutions_proposed.json")
    a = ap.parse_args(argv)
    if a.cmd == "auto-resolve":
        import auto_resolve
        res = auto_resolve.auto_resolve(ROOT, now=a.now, apply=a.apply, by=a.by, out_path=ROOT / a.out)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if a.cmd == "ingest-feed":
        if a.source.startswith(("http://", "https://")):
            import urllib.request
            req = urllib.request.Request(a.source, headers={"User-Agent": "Verdict (The Intelligence Company)"})
            with urllib.request.urlopen(req, timeout=40) as r:
                feed_doc = json.loads(r.read(8_000_000))
        else:
            feed_doc = json.loads(Path(a.source).read_text(encoding="utf-8"))
        res = ingest_feed(ROOT, feed_doc, draft_path=ROOT / a.drafts, approved=a.approve)
        print(json.dumps(res, indent=2))
        return 0
    if a.cmd == "due":
        d = due(ROOT, days=a.days)
        print(due_markdown(d) if a.markdown else json.dumps(d, indent=2), end="" if a.markdown else "\n")
        return 0
    if a.cmd == "calendar":
        if a.sync:
            a.sync = SYNC_SOURCES.get(a.sync, a.sync)
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
        ev = evidence_from_proposals(ROOT, a.id)
        if a.fetch and not ev:
            ev = fetch_body(a.url)
        print(json.dumps(resolve(a.id, a.outcome, a.url, a.note, a.by, evidence=ev, store_body=a.store_body)))
        return 0
    if a.cmd == "cluster":
        print(json.dumps(declare_cluster(a.id, a.members, a.reason)))
        return 0
    out = score()
    text = json.dumps(out, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
