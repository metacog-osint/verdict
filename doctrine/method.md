# Verdict method (doctrine summary)

Verdict logs every forecast registered here and scores it when it resolves. This page states the method in the terms
of The Intelligence Company's doctrine kit (`doctrine/kit/`).

## Pre-registration
A forecast enters the ledger before its outcome can be known: `published_at` must be earlier than
`resolution.known_at`, and the resolution rule, source and deadline are written at issue time. The ledger is
append-only JSON lines in git, so the commit history locks the timestamp: CI holds each new line's `published_at` to
within an hour of the commit that adds it, and that commit must be earlier than `known_at`. Where a rule names an
earliest check time (`resolution.check_after`, or the rule's own words), the forecast is not due and cannot be
resolved before it. A correction is a new line (msg_type update or cancel, with supersedes); nothing registered is
edited or deleted.

## Who sets the number
Event forecasts are drafted with sourced reasoning and alternatives; Tom sets the final probability and is recorded in
`approved_by`. The number is stored as well as the ICD 203 term, and confidence is kept apart from likelihood.

## Scores
The Brier score with Murphy's decomposition (reliability, resolution, uncertainty) and counts, broken out by program.

Questions that one decision settles together are declared a cluster in `ledger/clusters.jsonl` before any of them
can be known, and each member of a k-question cluster weighs 1/k, so the record is not inflated by counting one call
six times. A cluster declared after a member became knowable is refused, because choosing clusters after the fact
would choose which results count once. The per-forecast Brier is shown beside the weighted one.

Skill is given once a program has 30 resolved forecasts, a cluster counting once; with fewer, the base rate comes
from the same few outcomes and the figure would mostly show which questions were picked. Skill against the base rate
uses the sample's own outcomes. Skill against the reference uses each question's own `reference` probability, written
at registration from a plain rule (a base rate, a market, no change) with its source, so a forecast is scored against
what could have been said without judgment. Forecasts registered before 15 Nov 2026 carry no reference.
Reliability diagrams, POD/FAR/CSI/Peirce for threshold warnings and SEDI for rare events follow as the record grows.
Misses stay in.
