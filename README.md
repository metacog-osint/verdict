# Verdict

**The Intelligence Company's forecast record: every forecast logged before it can be known, then scored, misses
left in.**

Verdict keeps a public, append-only ledger of probability forecasts, each with the rule that will settle it written
at the moment it is made. Git's commit history is the timestamp lock: a forecast in `ledger/forecasts.jsonl` was
public before its outcome could be known, and nothing registered is ever edited or removed.

## The ledger

| File | What it holds |
|---|---|
| `ledger/forecasts.jsonl` | One forecast per line, in feed contract v2 (`tic-feed/2`): the question, the probability and its ICD 203 term, confidence kept separate, the alternatives, the resolution rule and source, when the outcome becomes knowable (`resolution.known_at`) and who set the number (`approved_by`). |
| `ledger/resolutions.jsonl` | One line per resolved forecast: the outcome (1 or 0), when, and the evidence link. |
| `drafts/` | Forecasts being prepared. A draft enters the ledger only once its probability is approved. |
| `calendar/events.json` | The event calendar: dated, resolvable events worth forecasting, each with the source of its date (`verified: false` where the date comes from secondary reporting). |
| `site/` | The public scorecard page and its data, generated from the above by `tools/build_site.py` (edit `site_src/`, never `site/`). `site/data/feed.geojson` and `site/data/calendar.geojson` are feed contract v2 feeds for COP. |

A correction is a new line that supersedes the old one (`msg_type` update or cancel); scores use the latest live line
and still show the history.

## Commands

```
python tools/verdict.py check                       # every line valid and linted; append-only with --base <ref>
python tools/verdict.py register drafts/2026-11.json  # approved drafts into the ledger, timestamped now (UTC)
python tools/verdict.py resolve <id> 1 --url <evidence>
python tools/verdict.py score                       # Brier score, Murphy decomposition, skill, by program
python tools/verdict.py due --markdown              # what to resolve now, and what is coming up
python tools/verdict.py calendar                    # check the event calendar
python tools/build_site.py                          # rebuild site/ after any ledger or calendar change
```

Standard-library Python only. CI runs the doctrine kit's checks, the tests, the append-only check and the site
check on every push. A daily workflow (`due.yml`) opens one GitHub issue, labelled `verdict-due`, when forecasts are
ready to resolve, and closes it when nothing is due.

## Scoring

The Brier score with its decomposition (reliability, resolution, uncertainty), skill against the base rate, and
counts, by program. As the record grows: reliability diagrams, POD, FAR, CSI and Peirce for threshold warnings, and
SEDI for rare events.

Forecasts follow ICD 203's likelihood terms and keep confidence separate from likelihood (aligned with the directive's
standards; it binds US intelligence agencies, not us). The method is in `doctrine/method.md`.

Code under the MIT licence. A project of The Intelligence Company.
