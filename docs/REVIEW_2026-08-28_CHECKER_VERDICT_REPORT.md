# Review — Checker Verdict Report

**Date:** 2026-08-28
**Branch:** `chore/checker-verdict-report`
**Base:** `de9f467` — current `main`

**Nothing merged, nothing deployed. No Railway variable, `DRY_RUN`,
`CHECKER_MAX_TOKENS`, `ORDER_STRATEGY`, threshold, cap, gate or risk
parameter changed. `main.py` and every counter untouched. No contract specs
added. `core/fee_schedule.py` not wired.**

---

## Bottom line

**The script is built, tested and works. I could not run it against
production, because this session has no path to the ledger file.**

That is the honest outcome, and it is the same answer as
`FINDINGS.md` Q2 — but now with a tool ready to answer the question the
moment someone with volume access spends thirty seconds on it.

The command is at the end of this document.

---

## Why the run is blocked

The task brief states the database "already exists on the Railway volume and
is already populated." That is correct. It is also not reachable from here.

Confirmed against the live service config:

```
volumeMounts: { 9d1d9daf-…: { mountPath: "/data" } }
variableNames: [ …, LEDGER_DB_PATH, … ]
```

The ledger sits at `/data` inside the container. The Railway MCP surface
available to this session is: logs, metrics, service config, variables,
deployments, domains, feature flags, redeploy, restart, docs. **There is no
file read, no volume download, and no container exec.**

A Railway *agent* tool exists and might be able to shell into the container.
I did not use it. It is a write-capable agent operating on production, and
invoking it to perform a read that the operator can do directly is
disproportionate risk for the benefit — particularly under a standing
instruction not to touch production.

### Verified first: the logs genuinely cannot answer it

Before concluding, I re-checked ~40 recent log entries filtered for
`verdict OR approve OR Checker OR confidence`. The only verdict-bearing line
in the entire window is a truncation recovery:

```
18:50:49 WARNING validation: Checker response for KXHIGHNY-26AUG29-T79
was cut off; recovered verdict=reject confidence=0.90
```

That fires **only on truncation**. A normal verdict is never logged. The
structural reason, traced this morning:

- The funnel line prints `checked` and `approved`, neither `checker_rejected`
  nor `risk_refused`.
- A Checker rejection does not `continue` — it falls through to
  `risk.evaluate`, which refuses it on bankroll at step 2, before the verdict
  branch at step 4.
- So an approved verdict and a rejected verdict produce **byte-identical**
  ledger log lines.

The database is not merely the easiest source. It is the only one.

---

## Diff summary

| File | Reason | Safety impact | Test coverage | Config/deploy action later? |
|---|---|---|---|---|
| `scripts/checker_verdict_report.py` *(new, 300 lines)* | Read-only verdict distribution, confidence by verdict, and breakdowns by family, day and category | **None.** Read-only via `file:…?mode=ro`; no network client; nothing imports it | 17 tests | **Yes** — someone with volume access must run it |
| `tests/test_checker_verdict_report.py` *(new, 220 lines)* | Read-only posture, absence handling, grouping, rendering | None | — | No |
| `docs/REVIEW_2026-08-28_CHECKER_VERDICT_REPORT.md` *(new)* | This document | None | — | No |

**Net: 3 new files. No file modified. No existing test changed.**

---

## Test results

```
$ python -m pytest -q
15 failed, 1013 passed in 23.92s

$ python -m ruff check .
All checks passed!
```

| | Baseline (`de9f467`) | This branch | Delta |
|---|---|---|---|
| Passed | 996 | 1013 | **+17** |
| Failed | 15 | 15 | **0** |

Same 15 pre-existing failures, unchanged in count and identity: 8 ×
`test_checker_budget`, 4 × `test_funnel_accounting`, 2 ×
`test_signal_alert_dedupe`, 1 × `test_vol_smoothing_bias`. Not investigated,
per scope.

One lint fix during development: a comprehension variable named `l` tripped
`E741`. Renamed to `row`.

---

## What the report produces

Verified end-to-end against a synthetic fixture. Real output, invented data:

```
========================================================================
CHECKER VERDICT REPORT
========================================================================
Window   : 2026-08-26T00:00:00+00:00  ->  unbounded
Rows span: 2026-08-26T00:01:40+00:00  ->  2026-08-27T00:06:40+00:00
Checked  : 6 rows with a Checker verdict

------------------------------------------------------------------------
VERDICT DISTRIBUTION
------------------------------------------------------------------------
verdict              count     share   mean conf    median
abstain                  1     16.7%         n/a       n/a
approve                  1     16.7%       0.800     0.800
reject                   4     66.7%       0.713     0.675

APPROVAL RATE: 1/6 = 16.7%

------------------------------------------------------------------------
BY MARKET FAMILY
------------------------------------------------------------------------
family                     abstain     approve      reject    total   appr%
KXBTCD                           0           1           2        3   33.3%
KXHIGHNY                         0           0           2        2    0.0%
KXWTI                            1           0           0        1    0.0%

------------------------------------------------------------------------
BY DAY (UTC)
------------------------------------------------------------------------
day                        abstain     approve      reject    total   appr%
2026-08-26                       0           1           2        3   33.3%
2026-08-27                       1           0           2        3    0.0%
```

Followed by a by-category table, a downstream `action_taken` breakdown, and
the interpretation block reproduced below.

---

## Design decisions worth stating

**A row counts as checked only when `checker_verdict` is non-NULL.** Rows
stopped earlier — coherence gate, per-event cap, ladder dedup — never got a
verdict and are correctly excluded. Including them would deflate the
approval rate against a denominator that never saw the Checker. A test pins
this with a `skipped_incoherent` row that must not appear.

**A family with zero approvals renders as `0`, not as an absent row.** That
was the explicit requirement behind the by-family split, and it carries a
real distinction the report states in prose:

> A family showing 0 approvals is a family the Checker never approved in
> this window. That is a finding. A family absent from the table entirely
> never reached the Checker at all, which is a different finding with a
> different cause.

**A NULL confidence is absent, not zero.** Averaging a missing confidence as
`0.0` would drag the mean toward a number nobody recorded — the same
manufactured-zero this codebase has now caught three separate times.

**An empty window says so.** It prints `NO ROWS WITH A CHECKER VERDICT` and
explicitly notes that this is *not* the same as "the Checker approved
nothing". A missing database exits `2` with `will not guess` on stderr
rather than printing an empty report, because an empty report reads like a
finding.

**Every report carries the interpretation block**, verbatim:

> The verdicts are real. The Checker ran on live quotes and returned an
> answer for every row counted here.
>
> The outcomes are not. Throughout this window the exchange balance was
> $0.00, so every one of these rows was refused downstream by the bankroll
> check inside `risk.evaluate` — before the verdict branch and before any
> sizing rule. A Checker approval here never became a trade, and never
> could have.
>
> Therefore: do NOT present approval rate and counterfactual P&L as if one
> caused the other.

---

## How to get the answer

Copy the ledger off the volume once, then this and every other read-only
script in the repo works locally, forever. `scripts/weekend_export.py`
already exists for exactly this and its docstring says the same thing: take
a copy first, never point a reader at the file the daemon is writing.

```bash
# from a machine with Railway access
railway link                      # select daemon-kalshi-v2 / production
railway ssh "cat \$LEDGER_DB_PATH" > ledger-copy.db
#   or: railway volume download, or any route that yields the file

git fetch origin chore/checker-verdict-report
git checkout chore/checker-verdict-report
python -m scripts.checker_verdict_report \
    --db ./ledger-copy.db --since 2026-08-26T00:00:00Z
```

Add `--json` for a machine-readable form, `--out FILE` to write it down.

---

## Unresolved

1. **The answer itself.** Still unknown. This is the second time the volume
   has blocked it. A periodic sanitized export would retire the problem
   permanently and is worth considering as its own small change.
2. **Whether a Checker *approval* is even reachable at a $0 balance is not
   in question** — it is. The Checker runs upstream of the bankroll check.
   What is unknown is how often it says yes, and on which families.
3. **The KXHIGHNY / KXHIGHCHI stuck claim.** Production has been refusing
   the same weather markets every pass with an identical coherence message
   (model 15% against a market at 0.5%). If the Checker also never approves
   weather, those are two independent signals pointing at the same model.
   This report would show it in one line.

---

## Noted in passing — production moved today

Not part of this task, but observed while confirming the logs and worth
recording: **the ladder dedup is now binding hard and consistently.** Across
18 consecutive passes on 28 August it dropped 111 of 534 priced proposals —
**20.8%** — with individual passes reaching 10 dropped (`35 priced -> 25 to
the Checker`).

Two days ago it recorded zero and I said plainly that its projected ~70%
saving was a projection rather than a result. It is now a measured 20.8%.
Lower than projected, real, and no longer an open question.

**Awaiting human review.**
