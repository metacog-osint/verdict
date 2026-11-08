"""Propose resolutions for due Verdict forecasts whose rules a machine can check.

Rules it reads:
1. EASA CZIB renewals (Verdict):
   "Yes if, on a check after <HH:MM> UTC on <D Mon YYYY> ..., shows <CZIB-YYYY-NN> active, revised on or before
   <D Mon YYYY> and valid beyond <D Mon YYYY>. ..."
   Read from EASA's own CZIB table (the CSV export), never from a copy, and only once the rule's check time has
   passed. The time of that read is the check time.
2. Vis operational weather (hourly threshold, daily sum, joint icing at an ICAO station). Vis names the NOAA
   METAR/SYNOP archive as its source; this module reads Open-Meteo's reanalysis archive at the station's
   coordinates instead, so every Vis proposal is held for a person to check against the station's reports.
3. Wake IMF PortWatch chokepoint transits.

Every result is a proposal in `drafts/resolutions_proposed.json`. A proposal that lists `needs_review` items is
never written to the ledger by this module. `--apply` writes only the rest to `ledger/resolutions.jsonl`, and only
with `--by` naming the person who resolves them. Anything it cannot check is listed under `skipped` with the reason.
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

import verdict

UA = {"User-Agent": "Verdict/1.0 (+https://theintelligence.company)"}

VIS_HOURLY_RE = re.compile(
    r"Yes if ICAO station (?P<icao>[A-Z0-9]{4}) \((?P<station>[^)]+)\) reports (?P<name>.+?) "
    r"\((?P<var>[a-z0-9_]+)\) (?P<op>>=|<=) (?P<thr>-?\d+(?:\.\d+)?) (?P<unit>\S+) "
    r"for >= (?P<min_h>\d+) hours on (?P<day>\d{4}-\d{2}-\d{2}) UTC\."
)

VIS_SUM_RE = re.compile(
    r"Yes if ICAO station (?P<icao>[A-Z0-9]{4}) \((?P<station>[^)]+)\) records (?P<name>.+?) "
    r">= (?P<thr>-?\d+(?:\.\d+)?) (?P<unit>\S+) on (?P<day>\d{4}-\d{2}-\d{2}) UTC\."
)

VIS_JOINT_RE = re.compile(
    r"Yes if ICAO station (?P<icao>[A-Z0-9]{4}) \((?P<station>[^)]+)\) reports 2 m temperature "
    r"<= (?P<thr>-?\d+(?:\.\d+)?) C with precipitation >= (?P<p_thr>-?\d+(?:\.\d+)?) mm/h "
    r"in the same hour for >= (?P<min_h>\d+) hours on (?P<day>\d{4}-\d{2}-\d{2}) UTC\."
)

DMY = r"\d{1,2} [A-Za-z]{3,9} \d{4}"
CZIB_RE = re.compile(rf"shows (?P<ref>CZIB-\d{{4}}-\d{{2}}) active.*valid beyond (?P<day>{DMY})")
CHECK_AFTER_RE = re.compile(rf"check after (?P<hm>\d{{2}}:\d{{2}}) UTC on (?P<day>{DMY})")
REVISED_BY_RE = re.compile(rf"revised on or before (?P<day>{DMY})")
REPEAT_BY_RE = re.compile(r"repeated by (?P<day>\d{1,2} [A-Za-z]{3,9}(?: \d{4})?)")

PORTWATCH_RE = re.compile(
    r"Yes if IMF PortWatch Daily_Chokepoints_Data \(portid='(?P<portid>[a-z0-9_]+)'\) reports a 7-day rolling "
    r"(?P<metric>[a-z0-9_]+) (?P<op><=|>=) (?P<thr>-?\d+(?:\.\d+)?) vessels/day.*on (?P<day>\d{4}-\d{2}-\d{2}) UTC\."
)

EASA_CSV = "https://www.easa.europa.eu/en/domains/air-operations/czibs/export?page=&_format=csv"
EASA_PAGE = "https://www.easa.europa.eu/en/domains/air-operations/czibs"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_dmy(s: str) -> str:
    """'16 Nov 2026' or '16 November 2026' -> '2026-11-16'."""
    parts = s.strip().split()
    d = int(parts[0])
    m = MONTHS[parts[1][:3].title()]
    y = int(parts[2])
    return f"{y:04d}-{m:02d}-{d:02d}"


def _skip(fid: str, reason: str) -> dict:
    return {"id": fid, "skip": reason}


# ---- EASA CZIBs ------------------------------------------------------------------------------------------------

def _easa_date(v: str | None) -> str | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", v or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def czib_base(number: str) -> str:
    """'CZIB-2026-04-R2' -> 'CZIB-2026-04'; 'CZIB-2026-07R3' -> 'CZIB-2026-07'."""
    m = re.search(r"(\d{4})-(\d{2})", number or "")
    return f"CZIB-{m.group(1)}-{m.group(2)}" if m else (number or "").strip()


def parse_easa_csv(text: str) -> list[dict]:
    """EASA's CZIB table export -> one dict per bulletin. Dates in the CSV are dd/mm/yyyy."""
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        number = re.sub(r"\s+", " ", (r.get("CZIB number") or "").strip())
        if not number:
            continue
        revision = re.sub(r"\s+", " ", (r.get("Revision") or "").strip())
        # Revision entries read "30/09/2026 – The validity ..."; a date inside the text ("until 31/01/2027") is not one.
        revised = [f"{y}-{m}-{d}" for d, m, y in re.findall(r"\b(\d{2})/(\d{2})/(\d{4})\s*[–—-]", revision)]
        if not revised:
            lead = re.match(r"\s*(\d{2})/(\d{2})/(\d{4})\b", revision)
            revised = [f"{lead.group(3)}-{lead.group(2)}-{lead.group(1)}"] if lead else []
        out.append({
            "number": number,
            "id": czib_base(number),
            "subject": re.sub(r"\s+", " ", (r.get("Subject") or "").strip()),
            "status": (r.get("Status") or "").strip().lower(),
            "issued": _easa_date(r.get("Issue date")),
            "valid_until": _easa_date(r.get("Valid until")),
            # The latest dated entry in the revision note; an unrevised bulletin counts from its issue date.
            "revised": max(revised) if revised else (None if revision else _easa_date(r.get("Issue date"))),
            "revision": revision[:300],
        })
    return out


def fetch_easa() -> dict:
    """Read EASA's CZIB table now. Returns {bulletins, fetched_at, url} or {error}."""
    try:
        req = urllib.request.Request(EASA_CSV, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read(5_000_000)
    except Exception as e:  # noqa: BLE001
        return {"error": f"EASA CSV: {type(e).__name__}: {e}"}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("cp1252", "replace")  # EASA's CSV says UTF-8 but carries Windows-1252 dashes
    rows = parse_easa_csv(text)
    if len(rows) < 5:
        return {"error": f"EASA CSV: only {len(rows)} bulletins parsed"}
    return {"bulletins": rows, "fetched_at": verdict.now_utc(), "url": EASA_CSV}


def evaluate_czib_rule(fc: dict, rule: str, easa: dict | None) -> dict | None:
    """An EASA CZIB renewal rule against a read of EASA's table. None if the rule is not a CZIB rule."""
    m = CZIB_RE.search(rule)
    if not m:
        return None
    fid = fc["id"]
    ref = m.group("ref")
    cutoff = _parse_dmy(m.group("day"))
    ca = CHECK_AFTER_RE.search(rule)
    check_after = f"{_parse_dmy(ca.group('day'))}T{ca.group('hm')}:00Z" if ca else None
    rb = REVISED_BY_RE.search(rule)
    revised_by = _parse_dmy(rb.group("day")) if rb else None
    rp = REPEAT_BY_RE.search(rule)

    if not easa or easa.get("error") or not easa.get("bulletins"):
        return _skip(fid, (easa or {}).get("error") or "EASA's CZIB table was not read")
    read_at = easa["fetched_at"]
    if check_after and verdict._t(read_at) < verdict._t(check_after):
        return _skip(fid, f"the rule allows a check only after {check_after}; EASA was read at {read_at}")

    rows = [b for b in easa["bulletins"] if b["id"] == ref]
    if not rows:
        return _skip(fid, f"{ref} is not in EASA's table (read {read_at}): check whether it was withdrawn, "
                          f"converted to an Information Note or replaced by a successor CZIB")
    b = next((r for r in rows if r["status"] == "active"), rows[0])
    seen = (f"EASA CZIB table read {read_at}: {b['number']} {b['status']}, valid until {b['valid_until']}, "
            f"last revised {b['revised'] or 'unknown'}")
    successor = (f"confirm no successor CZIB for {b['subject'] or 'the same airspace'} was active at the check "
                 f"and not weaker (the rule counts one)")
    review = []
    if b["status"] != "active":
        outcome, why = 0, f"status is {b['status']}"
        review.append(successor)
    elif not b["valid_until"] or b["valid_until"] <= cutoff:
        outcome, why = 0, f"not valid beyond {cutoff}"
        review.append(successor)
        if rp:
            review.append(f"the rule repeats the check by {rp.group('day')}: confirm on a later read before "
                          f"resolving No")
    elif revised_by and not b["revised"]:
        outcome, why = 1, f"valid until {b['valid_until']}"
        review.append(f"EASA's revision note has no date: confirm it was revised on or before {revised_by}")
    elif revised_by and b["revised"] > revised_by:
        outcome, why = 0, f"last revised {b['revised']}, after {revised_by}"
        review.append(f"EASA's table shows only the latest revision: check an Internet Archive copy of "
                      f"{EASA_PAGE} for an earlier revision on or before {revised_by} that already extended it")
    else:
        outcome, why = 1, f"active, revised {b['revised']}, valid until {b['valid_until']} (beyond {cutoff})"
    return {"id": fid, "outcome": outcome, "url": EASA_PAGE, "note": f"{seen}. {'Yes' if outcome else 'No'}: {why}.",
            "rule_type": "easa_czib", "needs_review": review}


# ---- Vis weather -----------------------------------------------------------------------------------------------

def _fetch_open_meteo_day(lat: float, lon: float, day: str, variables: list[str]) -> tuple[dict, str]:
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": day,
        "end_date": day,
        "hourly": ",".join(variables),
        "timezone": "UTC",
        "wind_speed_unit": "kn",
    }
    url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read(2_000_000)), url


def _full_day(values) -> list[float] | None:
    """All 24 hours present, or None (the archive fills recent days late, and a part-day would count short)."""
    vals = list(values or [])
    if len(vals) < 24 or any(v is None for v in vals[:24]):
        return None
    return [float(v) for v in vals[:24]]


def evaluate_vis_rule(fc: dict, rule: str, weather_archives: dict | None = None,
                      allow_fetch: bool = True) -> dict | None:
    """A Vis ICAO-station rule, read from Open-Meteo's archive at the station's coordinates. Always held for review."""
    m_hourly = VIS_HOURLY_RE.search(rule)
    m_sum = VIS_SUM_RE.search(rule)
    m_joint = VIS_JOINT_RE.search(rule)
    if not (m_hourly or m_sum or m_joint):
        return None
    fid = fc["id"]
    m = m_hourly or m_sum or m_joint
    icao, day = m.group("icao"), m.group("day")
    geom = fc.get("geometry") or {}
    coords = geom.get("coordinates") or []
    lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)

    fixture = (weather_archives or {}).get(f"{icao}:{day}") or (weather_archives or {}).get(fid)
    if fixture is not None:
        archive = fixture
        url = archive.get("url") or f"https://archive-api.open-meteo.com/v1/archive?icao={icao}&date={day}"
    elif not allow_fetch:
        return _skip(fid, "the weather archive was not read")
    elif lat is None or lon is None:
        return _skip(fid, "no coordinates to read the weather archive at")
    else:
        if m_hourly:
            vars_needed = [m_hourly.group("var")]
        elif m_sum:
            vars_needed = ["precipitation"]
        else:
            vars_needed = ["temperature_2m", "precipitation"]
        try:
            archive, url = _fetch_open_meteo_day(float(lat), float(lon), day, vars_needed)
        except Exception as e:  # noqa: BLE001
            return _skip(fid, f"Open-Meteo archive: {type(e).__name__}")

    declared = (fc.get("resolution") or {}).get("source") or "the station's own reports"
    review = [f"the rule's source is {declared}; this reading is Open-Meteo's reanalysis at the station's "
              f"coordinates: check it against {icao}'s METARs before resolving"]
    hourly = archive.get("hourly") or {}
    if m_hourly:
        var, op, unit = m_hourly.group("var"), m_hourly.group("op"), m_hourly.group("unit")
        thr, min_h = float(m_hourly.group("thr")), int(m_hourly.group("min_h"))
        series = _full_day(hourly.get(var))
        if series is None:
            return _skip(fid, f"Open-Meteo has not filled all 24 hours of {var} for {day} yet")
        if op == ">=":
            qualifying, peak = sum(1 for v in series if v >= thr), max(series)
        else:
            qualifying, peak = sum(1 for v in series if v <= thr), min(series)
        outcome = 1 if qualifying >= min_h else 0
        note = (f"Open-Meteo archive at {icao}'s coordinates, {day} UTC: {var} {op} {thr} {unit} for {qualifying}/24 h "
                f"(required >= {min_h} h; extreme {round(peak, 2)} {unit}).")
        return {"id": fid, "outcome": outcome, "url": url, "note": note, "rule_type": "vis_hourly",
                "needs_review": review}

    if m_sum:
        thr, unit = float(m_sum.group("thr")), m_sum.group("unit")
        series = _full_day(hourly.get("precipitation"))
        if series is None:
            return _skip(fid, f"Open-Meteo has not filled all 24 hours of precipitation for {day} yet")
        total = round(sum(series), 2)
        outcome = 1 if total >= thr else 0
        note = (f"Open-Meteo archive at {icao}'s coordinates, {day} UTC: daily precipitation {total} {unit} "
                f"(threshold >= {thr} {unit}).")
        return {"id": fid, "outcome": outcome, "url": url, "note": note, "rule_type": "vis_sum",
                "needs_review": review}

    thr, p_thr, min_h = float(m_joint.group("thr")), float(m_joint.group("p_thr")), int(m_joint.group("min_h"))
    temps, precs = _full_day(hourly.get("temperature_2m")), _full_day(hourly.get("precipitation"))
    if temps is None or precs is None:
        return _skip(fid, f"Open-Meteo has not filled all 24 hours of temperature and precipitation for {day} yet")
    qualifying = sum(1 for t, p in zip(temps, precs) if t <= thr and p >= p_thr)
    outcome = 1 if qualifying >= min_h else 0
    note = (f"Open-Meteo archive at {icao}'s coordinates, {day} UTC: joint icing (T <= {thr} C and precip >= "
            f"{p_thr} mm/h) for {qualifying}/24 h (required >= {min_h} h).")
    return {"id": fid, "outcome": outcome, "url": url, "note": note, "rule_type": "vis_joint", "needs_review": review}


# ---- Wake PortWatch --------------------------------------------------------------------------------------------

def evaluate_portwatch_rule(fc: dict, rule: str, portwatch_data: dict | None = None) -> dict | None:
    """A Wake IMF PortWatch chokepoint rule against PortWatch daily chokepoint records."""
    m = PORTWATCH_RE.search(rule)
    if not m:
        return None
    fid = fc["id"]
    if portwatch_data is None:
        return _skip(fid, "no PortWatch records supplied")
    portid, metric, op = m.group("portid"), m.group("metric"), m.group("op")
    thr, day = float(m.group("thr")), m.group("day")
    rec = portwatch_data.get(f"{portid}:{day}") or portwatch_data.get(fid)
    if not rec:
        return _skip(fid, f"no PortWatch record for {portid} on {day}")
    val = rec.get(metric) if metric in rec else rec.get("rolling_7d")
    if val is None:
        return _skip(fid, f"the PortWatch record has no {metric}")
    val = float(val)
    outcome = (1 if val <= thr else 0) if op == "<=" else (1 if val >= thr else 0)
    note = (f"IMF PortWatch ({portid} {day} UTC): 7-day rolling {metric} = {round(val, 2)} vessels/day "
            f"(threshold {op} {thr} vessels/day).")
    return {"id": fid, "outcome": outcome, "url": rec.get("url") or "https://portwatch.imf.org/", "note": note,
            "rule_type": "wake_portwatch", "needs_review": []}


# ---- Driver ----------------------------------------------------------------------------------------------------

def auto_resolve(
    root: Path = verdict.ROOT,
    now: str | None = None,
    apply: bool = False,
    by: str | None = None,
    weather_archives: dict | None = None,
    easa: dict | None = None,
    portwatch_data: dict | None = None,
    out_path: Path | None = None,
    fetch: bool = True,
) -> dict:
    """Propose resolutions for every due forecast; with apply=True, write the ones that need no review.

    `now` decides which forecasts are due. EASA rules are gated on the time EASA was actually read, so a `now` in
    the future cannot resolve one early. `fetch=False` reads nothing from the network (tests pass fixtures).
    """
    if apply and not (by or "").strip():
        raise SystemExit("auto-resolve --apply needs --by \"<name of the person resolving>\"")
    when = now or verdict.now_utc()
    due_rows = verdict.due(root, now=when)["due"]
    due_ids = {r["id"] for r in due_rows}
    fc_by_id = {f["id"]: f for f in verdict.read_lines(root / verdict.FORECASTS) if f.get("id") in due_ids}

    rules = {fid: ((fc.get("resolution") or {}).get("rule") or "") for fid, fc in fc_by_id.items()}
    if easa is None and fetch and any(CZIB_RE.search(r) for r in rules.values()):
        easa = fetch_easa()

    proposed, skipped, applied, held = [], [], [], []
    for row in due_rows:
        fid = row["id"]
        fc = fc_by_id.get(fid)
        if not fc:
            continue
        rule = rules[fid]
        res = evaluate_czib_rule(fc, rule, easa)
        if res is None:
            res = evaluate_vis_rule(fc, rule, weather_archives=weather_archives, allow_fetch=fetch)
        if res is None:
            res = evaluate_portwatch_rule(fc, rule, portwatch_data=portwatch_data)
        if res is None:
            skipped.append({"id": fid, "reason": "no machine-checkable rule; resolve by hand"})
            continue
        if "skip" in res:
            skipped.append({"id": fid, "reason": res["skip"]})
            continue
        proposed.append(res)
        if res["needs_review"]:
            held.append(fid)
            continue
        if apply:
            rec = verdict.resolve(fid, res["outcome"], res["url"], note=res["note"], by=by.strip(), root=root,
                                  when=when)
            applied.append(rec["id"])

    out_file = out_path or (root / "drafts" / "resolutions_proposed.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {"generated_at": when, "due_count": len(due_rows), "proposed": proposed, "applied": applied,
           "held_for_review": held, "skipped": skipped}
    if easa and easa.get("fetched_at"):
        doc["easa_read_at"] = easa["fetched_at"]
    out_file.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return doc
