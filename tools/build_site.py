"""Build the public scorecard site (site/) from the ledger, the resolutions and the calendar.

    python tools/build_site.py          # write site/
    python tools/build_site.py --check  # exit 1 if site/ is out of date (CI)

site/ is generated: edit site_src/ (page, script, styles, the shared tic-time module) and the ledger, never site/.
Output is deterministic (stamped with the newest ledger or calendar time, not the clock), so --check is exact.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import verdict  # noqa: E402
from tic_doctrine import likelihood  # noqa: E402  (verdict put the kit on sys.path)

SRC, OUT = ROOT / "site_src", ROOT / "site"


def build() -> dict[str, str]:
    """Every file under site/ as {relative path: text}."""
    fc = verdict.read_lines(ROOT / verdict.FORECASTS)
    res = {r["id"]: r for r in verdict.read_lines(ROOT / verdict.RESOLUTIONS)}
    cal = verdict.read_calendar(ROOT)
    stamps = [f["published_at"] for f in fc] + [r["resolved_at"] for r in res.values()] + \
             [f"{e['added']}T00:00:00Z" for e in cal]
    as_of = max(stamps) if stamps else "1970-01-01T00:00:00Z"

    superseded = {f.get("supersedes") for f in fc if f.get("supersedes")}
    # the calendar date a forecast is about (a plain date, shown without time-zone conversion)
    event_date = {fid: e["date"] for e in cal for fid in e.get("forecast_ids", [])}
    rows = []
    for f in fc:
        r = res.get(f["id"])
        resolution = f.get("resolution") or {}
        status = ("cancelled" if f.get("msg_type") == "cancel" else "superseded" if f["id"] in superseded
                  else "resolved" if r else "open")
        rows.append({
            "id": f["id"], "title": f["title"], "summary": f.get("summary"),
            "probability": f["probability"], "likelihood": likelihood.label(f["probability"]),
            "confidence": f.get("confidence"), "raise_by": (f.get("confidence_basis") or {}).get("raise_by", []),
            "published_at": f["published_at"], "approved_by": f.get("approved_by"),
            "event_date": event_date.get(f["id"], (resolution.get("known_at") or "")[:10] or None),
            "known_at": resolution.get("known_at"), "resolve_by": resolution.get("resolve_by"),
            "rule": resolution.get("rule"), "resolution_source": resolution.get("source"),
            "void_if": resolution.get("void_if"), "alternatives": f.get("alternatives", []),
            "countries": f.get("countries", []),
            "sources": [{"publisher": s.get("publisher"), "url": s.get("url"), "descriptor": s.get("descriptor")}
                        for s in f.get("sources", [])],
            "status": status, "outcome": r["outcome"] if r else None, "resolved_at": r["resolved_at"] if r else None,
            "evidence": (r.get("evidence") or {}).get("url") if r else None,
            "brier": round((f["probability"] - r["outcome"]) ** 2, 4) if r else None,
        })
    rows.sort(key=lambda x: (x["status"] != "open", x["known_at"] or "", x["id"]))

    scores = verdict.score(ROOT)
    scores["generated_at"] = as_of
    feed = {"type": "FeatureCollection", "contract": "tic-feed/2", "program": "verdict", "generated_at": as_of,
            "licence": "Verdict (MIT); each forecast's sources as cited",
            "attribution": "Verdict, The Intelligence Company",
            "language_key": "https://github.com/metacog-osint/tic-doctrine/blob/main/docs/language-key.md",
            "features": [{"type": "Feature", "geometry": None,
                          "properties": {k: v for k, v in f.items() if k != "geometry"}} for f in fc]}
    dump = lambda o: json.dumps(o, indent=1, ensure_ascii=False) + "\n"  # noqa: E731
    files = {
        "data/forecasts.json": dump({"as_of": as_of, "forecasts": rows}),
        "data/scores.json": dump(scores),
        "data/calendar.geojson": dump(verdict.calendar_feed(ROOT, generated=as_of)),
        "data/feed.geojson": dump(feed),
    }
    for p in sorted(SRC.rglob("*")):
        if p.is_file():
            files[p.relative_to(SRC).as_posix()] = p.read_text(encoding="utf-8")
    return files


def main() -> int:
    files = build()
    if "--check" in sys.argv:
        stale = [rel for rel, text in files.items()
                 if not (OUT / rel).exists()
                 or (OUT / rel).read_text(encoding="utf-8").replace("\r\n", "\n") != text]
        extra = [p.relative_to(OUT).as_posix() for p in OUT.rglob("*") if p.is_file()
                 and p.relative_to(OUT).as_posix() not in files] if OUT.exists() else []
        if stale or extra:
            print("site/ is out of date; run python tools/build_site.py:", ", ".join(stale + extra))
            return 1
        print(f"site/ is current ({len(files)} files)")
        return 0
    if OUT.exists():
        shutil.rmtree(OUT)
    for rel, text in files.items():
        dest = OUT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote site/ ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
