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
Skill against the base rate is given once a program has 30 resolved forecasts; with fewer, the base rate comes from
the same few outcomes and the figure would mostly show which questions were picked. Reliability diagrams,
POD/FAR/CSI/Peirce for threshold warnings and SEDI for rare events follow as the record grows. Misses stay in.
