# Verdict

**The Intelligence Company's forecast record: every forecast registered here is logged before it can be known, then
scored, misses left in.** Scorecard: https://metacog-osint.github.io/verdict/

Verdict keeps a public, append-only ledger of probability forecasts, each with the rule that will settle it written
at the moment it is made. Git's commit history is the timestamp lock: a forecast in `ledger/forecasts.jsonl` was
public before its outcome could be known, and nothing registered is ever edited or removed.

## The ledger

| File | What it holds |
|---|---|
| `ledger/forecasts.jsonl` | One forecast per line, in feed contract v2 (`tic-feed/2`): the question, the probability and its ICD 203 term, confidence kept separate, the alternatives, the resolution rule and source, when the outcome becomes knowable (`resolution.known_at`), the earliest time the rule allows a check where it sets one (`resolution.check_after`), and who set the number (`approved_by`). |
| `ledger/resolutions.jsonl` | One line per resolved forecast: the outcome (1 or 0), when, and the evidence link. |
| `drafts/` | Forecasts being prepared. A draft enters the ledger only once its probability is approved. |
| `calendar/events.json` | The event calendar: dated, resolvable events worth forecasting, each with the source of its date (`verified: false` where the date comes from secondary reporting). EASA bulletin expiries and State advisory changes arrive daily from [Cordon](https://cordon.theintelligence.company/) (`origin: cordon`), and Federal Register rule dates, FOMC and ECB decisions and FAO release dates from [Headwind](https://headwind.theintelligence.company/) (`origin: headwind`); an event that names one document (a bulletin or a Federal Register document number) is linked to the forecasts whose rules name it (`calendar-sync.yml`). An event is linked only when it is dated on or before the forecast's `known_at`, and a linked event is never dropped: a renewed bulletin's next expiry is a new event and does not move the forecast. |
| `site/` | The public scorecard page and its data, generated from the above by `tools/build_site.py` (edit `site_src/`, never `site/`). `site/data/feed.geojson` and `site/data/calendar.geojson` are feed contract v2 feeds for COP. |

A correction is a new line that supersedes the old one (`msg_type` update or cancel); scores use the latest live line
and still show the history.

## Commands

```
python tools/verdict.py check                       # every line valid and linted; with --base <ref>: append-only,
                                                    # and each new line published within an hour of its commit
python tools/verdict.py register drafts/2026-11.json  # approved drafts into the ledger, timestamped now (UTC)
python tools/verdict.py resolve <id> 1 --url <evidence>   # refused before the rule's check time
python tools/verdict.py score                       # Brier score, Murphy decomposition, by program
python tools/verdict.py due --markdown              # what to resolve now, and what is coming up
python tools/verdict.py calendar                    # check the event calendar
python tools/build_site.py                          # rebuild site/ after any ledger or calendar change
```

Standard-library Python only. CI runs the doctrine kit's checks, the tests, the append-only check and the site
check on every push. It also holds each new ledger line to its commit: `published_at` must be within an hour of the
commit that adds the line, and that commit must be earlier than `known_at`, so commit a registration within the
hour. A daily workflow (`due.yml`) opens one GitHub issue, labelled `verdict-due`, when forecasts are
ready to resolve, and closes it when nothing is due. A forecast whose rule names an earliest check time is not due,
and cannot be resolved, before that time.

## Scoring

The Brier score with its decomposition (reliability, resolution, uncertainty) and counts, by program. Skill against
the base rate is given once a program has 30 resolved forecasts; with fewer, the base rate comes from the same few
outcomes and the figure would mostly show which questions were picked. As the record grows: reliability diagrams,
POD, FAR, CSI and Peirce for threshold warnings, and SEDI for rare events.

Forecasts follow ICD 203's likelihood terms and keep confidence separate from likelihood (aligned with the directive's
standards; it binds US intelligence agencies, not us). The method is in `doctrine/method.md`.

Code under the MIT licence. A project of The Intelligence Company.
