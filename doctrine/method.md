# Verdict method (doctrine summary)

Verdict logs every forecast the suite makes and scores it when it resolves. This page states the method in the terms
of The Intelligence Company's doctrine kit (`doctrine/kit/`).

## Pre-registration
A forecast enters the ledger before its outcome can be known: `published_at` must be earlier than
`resolution.known_at`, and the resolution rule, source and deadline are written at issue time. The ledger is
append-only JSON lines in git, so the commit history locks the timestamp. A correction is a new line (msg_type update
or cancel, with supersedes); nothing registered is edited or deleted.

## Who sets the number
Event forecasts are drafted with sourced reasoning and alternatives; Tom sets the final probability and is recorded in
`approved_by`. The number is stored as well as the ICD 203 term, and confidence is kept apart from likelihood.

## Scores
The Brier score with Murphy's decomposition (reliability, resolution, uncertainty), skill against the base rate, and
counts, broken out by program. Reliability diagrams, POD/FAR/CSI/Peirce for threshold warnings and SEDI for rare
events follow as the record grows. Misses stay in.
