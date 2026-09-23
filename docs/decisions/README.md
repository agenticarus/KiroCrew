# Decisions

A record of product and design decisions a person on the team made. It exists
so that nobody — a human, a review lane, the nightly tester, an agent — undoes
one by accident while fixing something else.

A decision record is history, so an entry may cite the pull requests, issues,
commits and dates it rests on. That is the one place in `docs/` where such
citations belong; this front page states the rules in present tense and carries
none.

Two homes, one boundary. [`request-for-change/`](../request-for-change/README.md)
holds the design of a large or contested change and the debate around it; an
entry here holds one settled outcome — a label, a placement, a behaviour — in a
few lines, so it can be grepped before a change and never edited after. The
`recorded-decisions-are-not-regressions` rule and issue triage read only this
directory: a decision an RFC settles is protected here only once it also has an
entry.

## Rules

1. **One file per decision**, named `YYYY-MM-DD-<slug>.md`, where the date is the
   day the decision was made.
2. **Entries are immutable.** An entry is never edited, renamed or deleted.
   `scripts/check_decisions_history.py` enforces this in Fast Gate (the
   `decision-ledger-history` job): every entry present at the base of a pull
   request must exist at its head byte-identical. New entries are always
   allowed; this README is free to change.
3. **A change of mind is a new entry** that names the one it replaces on a
   `Supersedes:` line. It is written by a maintainer, or quotes one. The same
   path corrects a typo or a wrong date in a shipped entry: a superseding entry
   may exist solely to fix wording, and says so in its `Why`.
4. **An agent never decides.** An agent may add an entry only when it quotes a
   decision a human made in the same conversation, pull request or issue, and
   the entry names that human.
5. **An entry outranks a report.** A friction report, an AI-review finding, a
   failing expectation in a test, or a first-time-user observation that collides
   with an entry is not a bug. The right response is to close it as decided and
   link the entry. Issue triage checks this directory before investigating an
   issue (the Issue Radar crew brief and Investigate prompt both say so), and
   the `recorded-decisions-are-not-regressions` rule in `AUTOSDE.yaml` holds
   reviewers to it.
6. **Grep this directory before changing any user-facing label, wording,
   placement or behaviour.** A pull request that reverses an entry without a
   superseding entry is a regression and is blocked.

## Entry format

```
# <Decision in one sentence>

Decided by: <name and role>
Date: YYYY-MM-DD
Supersedes: <file>            (optional)

## Decision
One sentence.

## Why
Only what is true and was said at the time.

## Evidence
Links to the pull requests, issues and commits that carry the decision.
```

## Entries

| Entry | Decision |
|---|---|
| [2026-07-20-sessions-sidebar-create-button-says-new.md](2026-07-20-sessions-sidebar-create-button-says-new.md) | The Sessions sidebar's primary create button shows the visible label "New". |
