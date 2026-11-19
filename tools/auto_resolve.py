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
4. Blackout US power outages against ORNL's EAGLE-I outage history (figshare), once figshare lists that year.
5. COP outage triage's OONI question against OONI's own hourly counts for the country (api.ooni.io).

Every result is a proposal in `drafts/resolutions_proposed.json`. A proposal that lists `needs_review` items is
never written to the ledger by this module. `--apply` writes only the rest to `ledger/resolutions.jsonl`, and only
with `--by` naming the person who resolves them. Anything it cannot check is listed under `skipped` with the reason.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import verdict

UA = {"User-Agent": "Verdict/1.0 (+https://theintelligence.company)"}


class _Rows(list):
    """Rows read from a source; body_sha256 is the hash of the raw bytes fetched (None for fixtures)."""
    body_sha256 = None


class _Doc(dict):
    body_sha256 = None


def _evidence(url: str, fetched_at: str | None, rows, body_sha256: str | None = None, **extra) -> dict:
    """The evidence block a proposal carries into ledger/evidence/<id>.json."""
    return {"source_url": url, "fetched_at": fetched_at, "body_sha256": body_sha256, "rows": rows, **extra}

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
    return {"bulletins": rows, "fetched_at": verdict.now_utc(), "url": EASA_CSV,
            "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body)}


def evaluate_czib_rule(fc: dict, rule: str, easa: dict | None) -> dict | None:
    """An EASA CZIB renewal rule against a read of EASA's table. None if the rule is not a CZIB rule."""
    m = CZIB_RE.search(rule)
    if not m:
        return None
    fid = fc["id"]
    ref = m.group("ref")
    cutoff = _parse_dmy(m.group("day"))
    check_after = verdict.check_after(fc)  # resolution.check_after, or the time the rule states in its own words
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
            "rule_type": "easa_czib", "needs_review": review,
            "evidence": _evidence(easa.get("url") or EASA_CSV, read_at, rows, easa.get("body_sha256"),
                                  body_bytes=easa.get("body_bytes"))}


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
        raw = resp.read(2_000_000)
    doc = _Doc(json.loads(raw))
    doc.body_sha256 = hashlib.sha256(raw).hexdigest()
    return doc, url


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

    fetched_at = None
    fixture = (weather_archives or {}).get(f"{icao}:{day}") or (weather_archives or {}).get(fid)
    if fixture is not None:
        archive = fixture
        url = archive.get("url") or f"https://archive-api.open-meteo.com/v1/archive?icao={icao}&date={day}"
    elif not allow_fetch:
        return _skip(fid, "the weather archive was not read")
    elif lat is None or lon is None:
        return _skip(fid, "no coordinates to read the weather archive at")
    else:
        fetched_at = verdict.now_utc()
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
    ev = _evidence(url, fetched_at, [{k: v for k, v in hourly.items()}], getattr(archive, "body_sha256", None),
                   hash_note="Open-Meteo archive API body")
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
                "needs_review": review, "evidence": ev}

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
                "needs_review": review, "evidence": ev}

    thr, p_thr, min_h = float(m_joint.group("thr")), float(m_joint.group("p_thr")), int(m_joint.group("min_h"))
    temps, precs = _full_day(hourly.get("temperature_2m")), _full_day(hourly.get("precipitation"))
    if temps is None or precs is None:
        return _skip(fid, f"Open-Meteo has not filled all 24 hours of temperature and precipitation for {day} yet")
    qualifying = sum(1 for t, p in zip(temps, precs) if t <= thr and p >= p_thr)
    outcome = 1 if qualifying >= min_h else 0
    note = (f"Open-Meteo archive at {icao}'s coordinates, {day} UTC: joint icing (T <= {thr} C and precip >= "
            f"{p_thr} mm/h) for {qualifying}/24 h (required >= {min_h} h).")
    return {"id": fid, "outcome": outcome, "url": url, "note": note, "rule_type": "vis_joint", "needs_review": review,
            "evidence": ev}


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
            "rule_type": "wake_portwatch", "needs_review": [],
            "evidence": _evidence(rec.get("url") or "https://portwatch.imf.org/", rec.get("fetched_at"), [rec],
                                  rec.get("body_sha256"))}


# ---- Blackout US power: EAGLE-I outage history ----------------------------------------------------------------
# Ground truth for US outage forecasts (Tom, 13 Nov 2026: "use EAGLE-I"). ORNL's EAGLE-I history on figshare
# (doi:10.6084/m9.figshare.24237376, CC BY 4.0): one row per county and 15-minute run, UTC, zero rows left out. A
# year's file comes out the next year (2024 on 10 Apr 2025, 2025 on 24 Feb 2026), so a forecast it settles stays
# due and skipped until figshare lists that year; nothing is resolved from another source in the meantime. The rule
# is the one Blackout's base rates use (blackout/eaglei.py): a state's total is its counties summed per run; runs
# missing from the file neither join nor split; Yes needs runs above the line inside the window, with no more than 3
# known runs under the line between them and no more than 6 hours from one to the next, 4 or more of them not
# suspect. A run is suspect for a state when one of its counties shows more than 100,000 customers out beyond 1.1
# times EAGLE-I's modelled customers for it (MCC.csv): 7 runs on 8 Jan 2025 have Ventura County at 1,077,426 out of
# 317,658 (a broken report), but Fiona (Sept 2022) and Uri (Feb 2021) trip the same test with real outages EAGLE-I put
# on one county, so suspect runs are kept and a person decides: a No is held for review when a suspect run is over
# the line, or when missing runs add up to an hour or more in a row in the window. The file is read from figshare
# itself and checked against figshare's MD5 (about 1.0-1.4 GB a year, streamed once for every due forecast in that
# year).

EAGLEI_RE = re.compile(
    r"EAGLE-I's outage history \(ORNL, figshare doi:10\.6084/m9\.figshare\.24237376\) shows (?P<name>[A-Za-z .]+?) "
    r"\((?P<st>[A-Z]{2})\) with more than (?P<line>[\d,]+) customers out, its counties summed in each 15-minute "
    r"run, in 4 or more runs with no more than 3 runs under the line between them, between "
    r"(?P<a>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) and (?P<b>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC\."
)
EAGLEI_API = "https://api.figshare.com/v2/articles/24237376"
EAGLEI_DOI = "https://doi.org/10.6084/m9.figshare.24237376"
EAGLEI_FIPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}
RUN_S = 900          # EAGLE-I's 15-minute collection run
JOIN_BELOW = 3       # at most 3 known runs under the line (an hour) between runs above it
JOIN_MAX_RUNS = 24   # and no more than 6 hours from one run above it to the next
MIN_READINGS = 4     # an hour of 15-minute runs above the line
SUSPECT_EXCESS = 100_000  # a county this far over 1.1 x its modelled customers makes the run suspect


def _run(t: datetime) -> int:
    return int(t.replace(tzinfo=timezone.utc).timestamp() // RUN_S)


def _utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def eaglei_spec(rule: str) -> dict | None:
    m = EAGLEI_RE.search(rule)
    if not m:
        return None
    a, b = _utc(m.group("a")), _utc(m.group("b"))
    return {"state": m.group("st"), "name": m.group("name"), "line": int(m.group("line").replace(",", "")),
            "start": a, "end": b, "years": list(range(a.year, (b - timedelta(minutes=1)).year + 1))}


def eaglei_caps(text: str) -> dict[str, int]:
    """MCC.csv (County_FIPS, Customers) -> {5-digit FIPS: 1.1 x modelled customers}."""
    out = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        try:
            out[r["County_FIPS"].strip().zfill(5)] = int(float(r["Customers"]) * 1.1)
        except (KeyError, ValueError):
            continue
    return out


def read_eaglei_year(lines, states: set[str], caps: dict[str, int] | None = None) -> dict:
    """One EAGLE-I year CSV (text lines) -> {"totals": {state: {run: customers out}}, "runs": set of runs with rows,
    "suspect": {state: set of suspect runs}}.

    Columns by position (fips_code, county, state, count, run_start_time): the 2023 file calls the count "sum", the
    2024 file adds a column and sorts by county, the 2020 file has empty counts (no customers out)."""
    caps = caps or {}
    totals: dict[str, dict[int, int]] = {s: {} for s in states}
    suspect: dict[str, set[int]] = {s: set() for s in states}
    runs: set[int] = set()
    cache: dict[str, int | None] = {}
    reader = csv.reader(lines)
    head = next(reader, None)
    if not head or head[0].strip().lstrip("﻿") != "fips_code":
        raise ValueError(f"not an EAGLE-I outage file: {head!r}")
    for r in reader:
        if len(r) < 5:
            continue
        i = cache.get(r[4], -1)
        if i == -1:
            try:
                i = _run(datetime.strptime(r[4].strip(), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                i = None
            cache[r[4]] = i
        if i is None:
            continue
        runs.add(i)
        fips = r[0].strip().zfill(5)
        st = EAGLEI_FIPS.get(fips[:2])
        if st not in totals or not r[3].strip():
            continue
        try:
            n = int(float(r[3]))
        except ValueError:
            continue
        if fips in caps and n - caps[fips] > SUSPECT_EXCESS:
            suspect[st].add(i)
        if n > 0:
            totals[st][i] = totals[st].get(i, 0) + n
    return {"totals": totals, "runs": runs, "suspect": suspect}


def fetch_eaglei(needs: dict[int, set[str]]) -> dict:
    """{year: read year} for each needed year figshare lists; {"missing": [...], "listed": [...]} for the rest."""
    import hashlib

    with urllib.request.urlopen(urllib.request.Request(EAGLEI_API, headers=UA), timeout=60) as r:
        rec = json.loads(r.read().decode("utf-8"))
    files = {f["name"]: f for f in rec.get("files", [])}
    caps = {}
    if "MCC.csv" in files:
        with urllib.request.urlopen(urllib.request.Request(files["MCC.csv"]["download_url"], headers=UA),
                                    timeout=60) as r:
            caps = eaglei_caps(r.read().decode("utf-8-sig"))
    out: dict = {"version": rec.get("version"),
                 "listed": sorted(int(n[15:19]) for n in files if n.startswith("eaglei_outages_") and n[15:19].isdigit())}
    for year, states in sorted(needs.items()):
        f = files.get(f"eaglei_outages_{year}.csv")
        if not f:
            continue
        md5 = hashlib.md5()
        sha = hashlib.sha256()

        class _Hash(io.RawIOBase):
            def __init__(self, raw):
                self.raw = raw

            def readable(self):
                return True

            def readinto(self, b):
                chunk = self.raw.read(len(b))
                b[:len(chunk)] = chunk
                md5.update(chunk)
                sha.update(chunk)
                return len(chunk)

        with urllib.request.urlopen(urllib.request.Request(f["download_url"], headers=UA), timeout=300) as raw:
            text = io.TextIOWrapper(io.BufferedReader(_Hash(raw), 1 << 20), encoding="utf-8-sig", newline="")
            got = read_eaglei_year(text, states, caps)
        ok = md5.hexdigest() == f.get("computed_md5")
        out[year] = {**got, "file": f["name"], "url": f["download_url"], "md5_ok": ok,
                     "sha256": sha.hexdigest(), "fetched_at": verdict.now_utc()}
    return out


def evaluate_eaglei_rule(fc: dict, rule: str, eaglei_data: dict | None = None) -> dict | None:
    """A US state outage rule against EAGLE-I's history (see the comment above EAGLEI_RE)."""
    spec = eaglei_spec(rule)
    if not spec:
        return None
    fid = fc["id"]
    if eaglei_data is None:
        return _skip(fid, "no EAGLE-I history supplied")
    lacking = [y for y in spec["years"] if y not in eaglei_data]
    if lacking:
        listed = eaglei_data.get("listed") or []
        return _skip(fid, f"EAGLE-I has not published {', '.join(map(str, lacking))} on figshare yet"
                          + (f" (it lists {listed[0]}-{listed[-1]})" if listed else ""))
    a, b = _run(spec["start"]), _run(spec["end"])
    series: dict[int, int] = {}
    runs: set[int] = set()
    sus: set[int] = set()
    review = []
    for y in spec["years"]:
        d = eaglei_data[y]
        if d.get("md5_ok") is False:
            review.append(f"{d.get('file', y)} did not match figshare's MD5")
        series.update({i: n for i, n in d["totals"].get(spec["state"], {}).items() if a <= i < b})
        runs |= {i for i in d["runs"] if a <= i < b}
        sus |= {i for i in d.get("suspect", {}).get(spec["state"], ()) if a <= i < b}
    unknown = set(range(a, b)) - runs
    above = sorted(i for i, n in series.items() if n > spec["line"])
    spans, cur = [], None
    for i in above:
        known_between = (i - cur[1] - 1) - sum(1 for k in range(cur[1] + 1, i) if k in unknown) if cur else 0
        if cur and i - cur[1] <= JOIN_MAX_RUNS and known_between <= JOIN_BELOW:
            cur[1], cur[2] = i, cur[2] + 1
        else:
            cur = [i, i, 1]
            spans.append(cur)
    clean = lambda s: sum(1 for i in range(s[0], s[1] + 1) if i in series and series[i] > spec["line"]  # noqa: E731
                          and i not in sus)
    hits = [s for s in spans if clean(s) >= MIN_READINGS]
    when = lambda i: datetime.fromtimestamp(i * RUN_S, timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    files = ", ".join(eaglei_data[y].get("file", str(y)) for y in spec["years"])
    if hits:
        s = max(hits, key=lambda h: h[1] - h[0])
        vals = [series[i] for i in range(s[0], s[1] + 1) if i in series and i not in sus]
        held = max(min(vals[k:k + MIN_READINGS]) for k in range(len(vals) - MIN_READINGS + 1))
        outcome = 1
        note = (f"EAGLE-I ({files}): {spec['state']} above {spec['line']:,} customers out from {when(s[0])} to "
                f"{when(s[1] + 1)} UTC ({s[2]} runs above, {s[2] - clean(s)} of them suspect), {held:,} or more held "
                f"over 4 clean runs; {len(hits)} such "
                f"stretch(es) in the window.")
    else:
        outcome = 0
        top = max(series.items(), key=lambda kv: kv[1]) if series else None
        note = (f"EAGLE-I ({files}): no hour with {spec['state']} above {spec['line']:,} customers out between "
                f"{when(a)} and {when(b)} UTC; " + (f"highest {top[1]:,} at {when(top[0])} UTC." if top
                                                       else "no customers out recorded."))
        missing = sorted(unknown)
        longest, run_len, prev = 0, 0, None
        for i in missing:
            run_len = run_len + 1 if prev is not None and i == prev + 1 else 1
            longest, prev = max(longest, run_len), i
        if longest >= MIN_READINGS:
            review.append(f"EAGLE-I has no run for {len(missing)} of the window's {b - a} runs (longest gap "
                          f"{longest * RUN_S / 3600:.2f} h); a No may hide an outage in a gap")
        over = sorted(i for i in sus if series.get(i, 0) > spec["line"])
        if over:
            review.append(f"{len(over)} suspect run(s) over the line, from {when(over[0])} UTC: a county reads more "
                          f"than 100,000 customers above its modelled size. That is a broken report (8 Jan 2025, "
                          f"Southern California Edison) or a real outage EAGLE-I put on one county (Fiona, Uri); "
                          f"a person decides")
    rows = [{"run_utc": when(i), "state_total": series[i], "suspect": i in sus} for i in above[:500]]
    d0 = eaglei_data[spec["years"][0]]
    return {"id": fid, "outcome": outcome, "url": EAGLEI_DOI, "note": note, "rule_type": "blackout_eaglei",
            "needs_review": review,
            "evidence": _evidence(d0.get("url") or EAGLEI_DOI, d0.get("fetched_at"), rows, d0.get("sha256"),
                                  hash_note=f"SHA-256 of {d0.get('file', 'the first year file')} as streamed; "
                                            f"rows are the window's runs over the line (first 500 of {len(above)})",
                                  files=[eaglei_data[y].get("file") for y in spec["years"]])}


# ---- COP outage triage: OONI's own hourly counts -------------------------------------------------------------
# COP drafts an OONI question on every confirmed outage (cop/tools/triage.py, ooni_rule). Since 18 Nov 2026 it settles
# on OONI's aggregation API for the country, not on Blackout's BO-IND-3 reading, which exists for six countries only.
# The lines are Blackout's outage-window lines (blackout/ooni.py): enough measurements, enough confirmed or anomalous,
# and that share at least some multiple of the share over the hours before the window. An hour OONI does not list had
# no measurements. OONI gives hourly counts only for ranges of 7 days or less, so the baseline and the window are read
# in separate requests. OONI data is CC BY-NC-SA 4.0.
OONI_RE = re.compile(
    r"Yes if OONI's aggregation API \(api\.ooni\.io, probe_cc=(?P<cc>[A-Z]{2}), all tests, hourly\) counts at least "
    r"(?P<min_m>\d+) measurements from (?P<start>\d{4}-\d{2}-\d{2}T\d{2}:00:00Z) to "
    r"(?P<end>\d{4}-\d{2}-\d{2}T\d{2}:00:00Z) UTC, at least (?P<share>\d+(?:\.\d+)?)% of them confirmed or "
    r"anomalous, and that share at least (?P<rise>\d+(?:\.\d+)?) times the share over the (?P<base_h>\d+) hours "
    r"before (?P=start) UTC"
)
OONI_API = "https://api.ooni.io/api/v1/aggregation"
OONI_HOURLY_MAX_D = 7


def _hour_key(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H")


def ooni_spans(start: datetime, end: datetime, base_h: int) -> list[tuple[str, str]]:
    """(since, until) date pairs, until exclusive, covering the baseline and the window in requests of 7 days or
    less."""
    spans, day, last = [], (start - timedelta(hours=base_h)).replace(hour=0), (end - timedelta(seconds=1)).replace(
        hour=0, minute=0, second=0)
    while day <= last:
        stop = min(day + timedelta(days=OONI_HOURLY_MAX_D), last + timedelta(days=1))
        spans.append((day.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")))
        day = stop
    return spans


def fetch_ooni_hours(cc: str, start: datetime, end: datetime, base_h: int) -> list[dict]:
    """OONI's hourly aggregation rows for a country over the baseline and the window."""
    rows = _Rows()
    digest = hashlib.sha256()
    for since, until in ooni_spans(start, end, base_h):
        q = urllib.parse.urlencode({"probe_cc": cc, "since": since, "until": until,
                                    "axis_x": "measurement_start_day", "time_grain": "hour"})
        with urllib.request.urlopen(urllib.request.Request(f"{OONI_API}?{q}", headers=UA), timeout=60) as r:
            raw = r.read(16_000_000)
        digest.update(raw)
        payload = json.loads(raw)
        res = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(res, list):
            raise ValueError(f"OONI aggregation {since} to {until}: no hourly result list")
        rows += res
    rows.body_sha256 = digest.hexdigest()  # of the responses, in the order read
    return rows


def ooni_tally(rows: list[dict], frm: datetime, to: datetime) -> tuple[int, int]:
    """(measurements, confirmed plus anomalous) over the whole hours from frm to to."""
    by_hour = {}
    for r in rows:
        ts = str(r.get("measurement_start_day") or "")
        if len(ts) >= 13:
            by_hour[ts[:13]] = r
    m = hit = 0
    h = frm
    while h < to:
        r = by_hour.get(_hour_key(h))
        if r:
            m += int(r.get("measurement_count") or 0)
            hit += int(r.get("confirmed_count") or 0) + int(r.get("anomaly_count") or 0)
        h += timedelta(hours=1)
    return m, hit


def evaluate_ooni_rule(fc: dict, rule: str, ooni_data: dict | None = None, allow_fetch: bool = True) -> dict | None:
    """A COP outage-triage OONI rule against OONI's own hourly counts for the country."""
    m = OONI_RE.search(rule)
    if not m:
        return None
    fid, cc = fc["id"], m.group("cc")
    iso = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)  # noqa: E731
    start, end = iso(m.group("start")), iso(m.group("end"))
    base_h, min_m = int(m.group("base_h")), int(m.group("min_m"))
    share_line, rise = float(m.group("share")) / 100, float(m.group("rise"))
    rows = (ooni_data or {}).get(fid)
    fetched_at = None
    if rows is None:
        if not allow_fetch:
            return _skip(fid, "no OONI counts supplied")
        try:
            rows = fetch_ooni_hours(cc, start, end, base_h)
        except Exception as exc:  # not read: skipped, never a No
            return _skip(fid, f"OONI not read: {type(exc).__name__}: {exc}"[:200])
        fetched_at = verdict.now_utc()
    wm, wh = ooni_tally(rows, start, end)
    bm, bh = ooni_tally(rows, start - timedelta(hours=base_h), start)
    share = wh / wm if wm else None
    base = bh / bm if bm else 0.0
    yes = wm >= min_m and share is not None and share >= share_line and share >= rise * base
    pct = lambda x: "none" if x is None else f"{x:.1%}"  # noqa: E731
    note = (f"OONI aggregation ({cc}, all tests, hourly): {wm:,} measurements from {m.group('start')} to "
            f"{m.group('end')} UTC, {wh:,} confirmed or anomalous ({pct(share)}); the {base_h} hours before: "
            f"{bm:,} measurements, {pct(base if bm else None)}. Lines: at least {min_m} measurements, "
            f"{share_line:.0%}, and {rise:g} times the share before. OONI data, CC BY-NC-SA 4.0.")
    review = []
    if not rows:
        review.append("OONI listed no hours at all for the baseline or the window; check the API answered for "
                      f"{cc} before taking this No")
    url = ("https://explorer.ooni.org/chart/mat?" + urllib.parse.urlencode({
        "probe_cc": cc, "since": start.strftime("%Y-%m-%d"), "until": (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        "time_grain": "hour", "axis_x": "measurement_start_day"}))
    return {"id": fid, "outcome": 1 if yes else 0, "url": url, "note": note, "rule_type": "cop_ooni_window",
            "needs_review": review,
            "evidence": _evidence(url, fetched_at, list(rows), getattr(rows, "body_sha256", None),
                                  hash_note="SHA-256 of OONI's aggregation responses concatenated in the order read")}


# ---- Driver ----------------------------------------------------------------------------------------------------

def auto_resolve(
    root: Path = verdict.ROOT,
    now: str | None = None,
    apply: bool = False,
    by: str | None = None,
    weather_archives: dict | None = None,
    easa: dict | None = None,
    portwatch_data: dict | None = None,
    eaglei_data: dict | None = None,
    ooni_data: dict | None = None,
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
    eaglei_needs: dict[int, set[str]] = {}
    for r in rules.values():
        spec = eaglei_spec(r)
        for y in (spec["years"] if spec else []):
            eaglei_needs.setdefault(y, set()).add(spec["state"])
    if eaglei_data is None and fetch and eaglei_needs:
        eaglei_data = fetch_eaglei(eaglei_needs)

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
            res = evaluate_eaglei_rule(fc, rule, eaglei_data=eaglei_data)
        if res is None:
            res = evaluate_ooni_rule(fc, rule, ooni_data=ooni_data, allow_fetch=fetch)
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
                                  when=when, evidence=res.get("evidence"))
            applied.append(rec["id"])

    out_file = out_path or (root / "drafts" / "resolutions_proposed.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {"generated_at": when, "due_count": len(due_rows), "proposed": proposed, "applied": applied,
           "held_for_review": held, "skipped": skipped}
    if easa and easa.get("fetched_at"):
        doc["easa_read_at"] = easa["fetched_at"]
    out_file.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return doc
