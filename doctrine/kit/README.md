# tic-doctrine

The shared doctrine kit for The Intelligence Company's programs (Noisefloor, Watchfloor, Context, Vigil, PropertyCast,
and the programs still to come). It turns each program's measurements into judgments that can be read the same way,
drawn on one map (COP) and scored (Verdict).

**What is in it**

| Path | What |
|---|---|
| `vocab/` | Reference vocabularies: ICD 203 likelihood with UK and Kent crosswalks; the confidence rubric; contract enums; JP 3-04 information aspects, PMESII-PT, ASCOPE, the competition continuum; CISA's 55 National Critical Functions with a published sector and lifeline crosswalk; FEMA's eight lifelines and status colours; response ladders (PAGER, USGS volcano alert levels, GDACS, Copernicus EMS, IFRC, EASA, State Department); Admiralty grades; ICD 203's nine tradecraft standards; actor roles and types, and source owners, for characterising entities and sides without friend-or-foe labels |
| `schema/` | Feed contract v2 as JSON Schema |
| `python/tic_doctrine/` | Likelihood, confidence, contract checks, the tradecraft linter, register checks, source health, withholding counts, tips between programs (standard library only) |
| `js/tic-doctrine.js` | The same likelihood, confidence, lifeline, ladder and tip-matching logic for web pages (ES module, no dependencies) |
| `vocab/programs.json` | The suite registry: every program, its feed, and the datasets it owns |
| `docs/` | The contract, the reader's language key, the confidence rubric, the sources-and-withholding page |
| `templates/doctrine/` | A program's registers: requirements, indicators and warning problems, assumptions, function map, sources, method |
| `tools/` | `sync.py` copies the kit into a program as `doctrine/kit/`; `build_js.py` and `build_schema.py` regenerate from `vocab/` |

**Use in a program**

```
python tools/sync.py D:/Projects/<program>            # vendor the kit into <program>/doctrine/kit/
python doctrine/kit/tic.py lint <feed>.geojson --doctrine doctrine
python doctrine/kit/tic.py registers doctrine
python doctrine/kit/tic.py health doctrine/sources.json --out doctrine/health.json
```

**Tips between programs (0.4.0, 1 Nov 2026).** Each dataset is collected once, by the program that owns it
(`vocab/programs.json`); the others read the owner's findings instead of collecting them again. A program lists what it
wants in `doctrine/interests.json` (areas as country sets, then interests: which programs, tiers, response stages,
title words or measure tests). `tips()` matches those interests against the bus, COP's merged snapshot of every live
feed, or against one feed; `cite()` turns a tip into a source entry that carries the upstream lineage, so the
two-source rule and convergence count each original source once; `diff()` finds new, changed and ended tips between
runs. A stale producer gives stale tips, drawn grey.

**Geometry (0.4.1).** Items placed only by shape, such as navigational warnings at sea, match an area through the
countries their points fall in, or through `boxes` in `interests.json` (`[west, south, east, north]` per area, one box
or a list). Matching uses bounding boxes, so a very large warning area can land in two neighbouring areas, and boxes do
not cross the 180° meridian. A registry entry stays `planned` (with a `status_note`) until its program publishes real
data at its address. Vis went live on 8 Nov 2026 (0.4.2).

```
python doctrine/kit/tic.py tips doctrine                  # against the bus
python doctrine/kit/tic.py tips doctrine --feed <url>     # against one program's feed
```

**Tests:** `python tests/test_kit.py` and `node tests/test_js.mjs`.

**Claims.** The methods are aligned with the principles of ICD 203, 206 and 208. Those directives bind US Intelligence
Community elements only; this is an independent, non-commercial research effort, so it never claims compliance. Only
released doctrine is cited.

Code: MIT. Vocabulary files restate public US government and allied publications, credited in each file.
