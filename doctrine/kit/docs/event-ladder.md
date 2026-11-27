# Lead, confirmed, verified: the event ladder and the coverage table

Kit 0.6.13 (27 Nov 2026; derived sources not counted from 0.6.14; "No independent source" from 0.6.17). The one place the suite defines how sure it is that a reported event happened, the rule
that moves a report up, how claims by a party to a conflict and by state media are handled, and the table that says
how well each watched place is covered. Feeds carry the state in the `corroboration` member of the
[feed contract](feed-contract-v2.md); pages show it with a link to this page.

## Where it applies

To **event reports**: an attack, a strike, an incident at sea, a security alert, an emergency appeal, an outbreak
notice. Something either happened or it did not, and the question is how well that is established.

It does not apply to **measurements** (a count, a rate, a price, a traffic level) or to **judgments** (an assessment or
a forecast). A measurement is labelled by its program's method and tier; a judgment by its likelihood term and a
separate confidence ([language key](language-key.md)). An item with no `corroboration` member is outside the ladder,
and a page shows no state for it.

## The three states

| State | What it means | How an item gets there |
|---|---|---|
| **Lead** | Reported by one origin and screened into the program's scope, but nobody has checked it. Read it as "reported", never as fact. | The first origin appears. |
| **Confirmed** | A person has read the report and its source and judged it a real event within the program's scope. It still rests on one origin: nothing independent has checked it. | A reviewer confirms it. The item's `human_review` is `full`. |
| **Verified** | Checked independently, in one of three ways: two or more independent origins report it; an official source reports something within its own remit; or a published geolocation places it. | The rule below is met. No person needs to have looked: `human_review` says whether one did. |

"Confirmed" here is a reviewer's check that the event is real and in scope. It is not the Admiralty grade
"1, confirmed by other sources", which needs independent sources and so matches **verified**
([`vocab/grading.json`](../vocab/grading.json)).

An item moves up only, and may go straight from lead to verified. A report that turns out wrong is withdrawn, not
moved down: a new item with `msg_type` `cancel` or `error`, or a `last_change` on the subject.

## The promotion rule

Printed in every item's `corroboration.rule`:

> **ladder-1:** a report is a lead until a reviewer confirms it or it is verified by two independent origins, an
> official source within its own remit, or a published geolocation; a party's claims and state media count as one
> origin per government and never verify or raise an item alone.

**Counting origins.** An origin is a separate first-hand account. Wire copies, and texts sharing 80% of their words,
are one origin. One government's outlets, agencies and officials are one origin. An aggregator (a news index, a
search API) is not an origin; the outlets it indexes are.

**Official within its own remit.** An issuer speaking about what it runs or records: UKMTO about incidents it logs, an
embassy about its own security alert, the IFRC about its own appeal, the WHO about an outbreak notice it publishes, a
national seismic network about its own reading. An official speaking about someone else's action is an origin, not an
official source.

**Geolocation.** A published geolocation of imagery of the event, cited as one of the item's sources. It shows where
an image was taken; it does not by itself date the event or say who did it.

## Claims by parties and by state media

- **Party claim** (`party_claim: true`): every origin so far is a party to the conflict at hand (its government,
  armed forces, officials or their media) describing its own action or an adversary's loss.
- **State media** (`state_media: true`): every origin so far is media that a state owns or controls.

Both are labelled wherever the item is shown. While an item rests only on them it stays below verified, it keeps tier
`routine`, and it raises nothing: it is never a pick, and it moves no join, count or alert on its own. A party's
ministry or armed force is never the official source for its own conflict: what it says about that conflict is a party
claim. Such an item is verified only on origins, with at least one origin outside the party and outside state media.

## Fields

The `corroboration` member, on event items only:

| Member | Meaning |
|---|---|
| `state` | `lead`, `confirmed` or `verified` |
| `basis` | How it was verified: `origins`, `official` or `geolocation`. Null for a lead or a confirmed item |
| `origins` | Independent origins counted so far, at least 1 |
| `official` | The issuer's source id when `basis` is `official` (`ukmto`, `usembassy-ae`), else null |
| `geolocation` | The source id of the published geolocation when `basis` is `geolocation`, else null |
| `party_claim`, `state_media` | Optional, default false: the item rests only on a party's claims, or only on state media |
| `first_reported_at` | When the first origin appeared (UTC) |
| `confirmed_at` | When a reviewer confirmed it; null if no one has |
| `verified_at` | When the rule was met; null until then |
| `rule` | The rule's id and text, printed |

**Earlier names (kit 0.6.2).** `reported` is the earlier name of `lead` and `corroborated` of `verified`, with
`corroborated_at` for `verified_at` and the rule `corroborate-1`. Feeds that use them still pass; the linter notes the
earlier names, and `ladder.state_of()` (Python) and `eventState()` (browser module) read both. Two rules below are
new in kit 0.6.13 (a party claim is verified only on origins, and keeps tier `routine` until verified). For an item
that uses the earlier names they are notes, not errors, so a feed that passed under 0.6.12 still passes (kit 0.6.17);
under the names `lead` and `verified` they are errors.

The linter checks, as errors:
- a lead has no basis, no `confirmed_at` and no `verified_at`;
- a confirmed item has `confirmed_at`, its `human_review` is `full`, and it has no basis or `verified_at`;
- a verified item names its basis and `verified_at`; on origins it counts 2 or more; on official it names the issuer;
  on geolocation it names a source id from the item's own `sources`;
- a party claim or state-media item is verified only on origins, with 2 or more, and keeps tier `routine` until then;
- no time is before `first_reported_at` or after `published_at`.

## Showing it

A page shows the state's name with its one-line meaning, the party-claim or state-media label when set, and a link to
this page. `eventState(item)` in [`js/tic-doctrine.js`](../js/tic-doctrine.js) returns all of these, or null for an
item outside the ladder.

## The coverage table

A quiet place on a map can mean nothing happened, or that nobody was watching. The coverage table says which, place
by place.

| Class | Label | Meaning |
|---|---|---|
| `several` | Several independent sources | 3 or more independent source groups |
| `few` | One or two sources | 1 or 2 independent source groups |
| `no_independent` | No independent source | No independent group, but sources were read for the place: every one was set aside or is the suite's own (kit 0.6.17) |
| `none` | Not monitored | No source read for the place |

A place that only state media, a party's claim or a suite program reports on is `no_independent`, not `none`:
someone was watching it, but nothing independent of those sources reports there.

**Counting.** Count independence groups, not outlets: `sources[].independence_group`, or the publisher where a
source has none. Count only sources read on time; a stale copy does not count. Sources the suite makes itself are
not sources about the place and are not counted: a program's own reading, computation, model or machine summary
(`source_type` `suite_program`, `derived`, `model` or `machine summary`, listed in
[`vocab/event_ladder.json`](../vocab/event_ladder.json)); they are listed under `not_counted`, and the item's
program is still named in `programs`. Groups of state media (a source whose
`owner` is `state` and that is media) and the sources of items resting only on a party's claim or on state media are
listed but set aside: they never raise a place's class. Each table prints how it was counted and when.

**Format `tic-coverage/1`** (JSON):

```json
{
  "format": "tic-coverage/1",
  "generated_at": "2026-11-27T04:20:00Z",
  "basis": "independence groups behind the items held for each place at the snapshot, read on time",
  "rule": "several: 3 or more independent groups; few: 1 or 2; no_independent: none, but sources were read and set aside or not counted; none: not monitored",
  "places": [
    {"place": "example", "name": "Example place", "class": "few", "independent": 2,
     "groups": [{"id": "us-state-department", "label": "US Department of State", "programs": ["cordon"]},
                {"id": "ifrc", "label": "IFRC", "programs": ["relief"]}],
     "set_aside": [{"id": "example-state-media", "label": "Example state agency", "programs": ["watchfloor"],
                    "why": "state_media"}],
     "not_counted": [{"id": "Vis", "label": "Vis", "programs": ["vis"]}],
     "programs": ["cordon", "relief", "vis", "watchfloor"]}
  ]
}
```

`not_counted` is new in kit 0.6.17; a reader of an earlier table treats it as empty.
`ladder.coverage_place()` builds one row from a place's items and `ladder.coverage_class(independent, uncounted)`
gives the class, where `uncounted` is the number of groups set aside or not counted. In the browser module,
`coverageClass(row)` takes a row (or the same two counts) and returns its id and label. Shown as a table:

| Place | Coverage | Independent groups | Programs |
|---|---|---|---|
| Example place | One or two sources | 2 (1 set aside, 1 not counted) | Cordon, Relief, Vis, Watchfloor |

**Limits.** The table counts sources, not their quality or how often they report. Built from the items a consumer
holds, it misses a source that watches a place but has nothing to report there that day, so its counts are a lower
bound; a table built from each source's own stated coverage says so in `basis`.
