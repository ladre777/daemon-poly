# Kalshi Fee Schedule — verified

## Source

| | |
|---|---|
| Document | Kalshi Fee Schedule |
| URL | https://kalshi.com/docs/kalshi-fee-schedule.pdf |
| Effective | **2026-07-07** ("Last updated and effective: July 7, 2026", printed on every page) |
| Retrieved | **2026-08-27**, supplied directly by the operator |

The document could not be fetched from the session that transcribed it —
this environment's egress policy answers `403` to `CONNECT` for
`kalshi.com`, `docs.kalshi.com`, `help.kalshi.com` and
`trading-api.kalshi.com`. The operator supplied their own copy. Anyone
re-verifying should re-fetch from the URL above and compare against
`tests/test_fee_schedule.py`, which encodes all 42 published table values.

This replaces `CONFIG.risk.fee_rate` as the authority. That constant's own
docstring admitted it "comes from published summaries, not a verified
schedule".

---

## The formulas, verbatim

```
Taker:  fees = round up(M x 0.07   x C x P x (1-P))
Maker:  fees = round up(M x 0.0175 x C x P x (1-P))

P = the price of a contract in dollars (50 cents is 0.5)
C = the number of contracts being traded
M = the multiplier for each contract
    taker default 1 unless otherwise indicated
    maker default 0 unless otherwise indicated
```

**`C` is inside the rounding.** The fee is computed on the whole order and
rounded once — not computed per contract and multiplied. This is the
substantive finding of the transcription; see "What changed" below.

---

## The three disputed points, settled

The task that produced this module listed three claims from third-party
write-ups that could not all be true. The document settles all three.

### 1. Does the multiplier vary by category? Is crypto higher?

**No, and no.** The base rate is `0.07` for every event contract market.
What varies is a per-series multiplier `M` in the Non-Standard Fees table.
The taker column is only ever `0` or `1`; the maker column is `0`, `1`, or —
in exactly one row, `KXMVE` — `2`.

Crypto is not charged more. The two crypto series that *are* non-standard go
the other way and are **fee-free**:

| Series | Description | Maker M | Taker M |
|---|---|---|---|
| `KXBTCY` | BTC price range EOY | 0 | 0 |
| `KXETHY` | ETH price EOY | 0 | 0 |

**None of the families this bot trades appear in the non-standard table**,
so all of them take the default `M = 1`:

`KXBTC`, `KXBTCD`, `KXBTC15M`, `KXETH`, `KXETHD`, `KXWTI`, `KXHIGHNY`,
`KXHIGHCHI`

The one traded family that *is* listed is `KXPGATOUR` (PGA Tour), at
maker 1 / taker 1 — i.e. standard on the taker side and, unusually, **not**
maker-exempt.

### 2. Maker treatment

Both disputed claims were half right.

- "Most standard markets carry 0% maker fee" — **true**, via the maker
  multiplier defaulting to `0`.
- "Maker is ~25% of taker" — **true as a rate ratio** where a maker
  multiplier applies: `0.0175 / 0.07 = 0.25` exactly. `KXMVE` is the sole
  exception, at maker `M = 2`, i.e. half of taker.
- "A flat 0.25% during major events" — **not supported**. Nothing of the
  kind appears in the document.

### 3. A $0.035 per-contract cap

**Absent.** No cap of any kind is stated anywhere in the schedule. Recorded
as `per_contract_cap: None` in `provenance()` with a note, so the absence
stays visible rather than merely unmentioned.

---

## Rounding, and one unresolved discrepancy

The document's prose defines round up as:

> rounds up such that the fee + positionCost is rounded to a centicent

Its own published fee table does not behave that way. **All 42 printed
values are reproduced exactly by ceiling the aggregate to a whole cent**,
and several exceed what a centicent ceiling would give — one contract at
$0.50 has a raw fee of $0.0175, already an exact centicent, and the table
charges **$0.02**.

**The arithmetic, so a reader does not have to take this on trust.** Someone
who sees only the word "centicent" in the prose has no reason to doubt it
until they watch it fail against the document's own table:

| rate | C | P | raw fee | ceil to cent | ceil to centicent | **published** |
|---|---|---|---|---|---|---|
| 0.07 | 1 | $0.01 | 0.000693 | **$0.01** | $0.0007 | **$0.01** |
| 0.07 | 1 | $0.50 | 0.017500 | **$0.02** | $0.0175 | **$0.02** |
| 0.07 | 100 | $0.45 | 1.732500 | **$1.74** | $1.7325 | **$1.74** |

Cent-rounding reproduces the printed values; centicent-rounding does not,
and is not merely less precise — it is a different number in the column the
document itself prints. The table is unambiguous and machine-checkable, so
the table is what `core/fee_schedule.py` implements. The prose discrepancy
is recorded in `provenance()["rounding_note"]` and listed as an open
question in the review document rather than silently resolved.

This reading was **independently confirmed** on 2026-08-27 by a separate
session that saw only the PDF — never this file, `core/fee_schedule.py`, or
the review. It reached the same cent-vs-centicent conflict from the prose
alone. That is a genuine second derivation of the one-cent-per-order floor,
which is the finding the Sep 4 arbitrage review depends on.

One consequence worth stating plainly: **there is no sub-cent fee.** Any
fee-bearing order costs at least one cent, however small.

---

## What changed versus the previous model

`core/pricing.py::fee_cents_per_contract` computes a **per-contract** figure
and ceilings it to a *centicent*:

```python
fee_cents = CONFIG.risk.fee_rate * p * (1.0 - p) * 100.0
return math.ceil(round(fee_cents * 100.0, 6)) / 100.0
```

That is far closer to the published schedule than a naive per-contract
whole-cent ceiling would be, and at large order sizes it is **exact**. The
divergence is at small sizes, and it runs in the unsafe direction:

| Price | C | Previous model | Published | Difference |
|---|---|---|---|---|
| $0.01 | 1 | $0.0007 | **$0.01** | understates by $0.0093 |
| $0.10 | 1 | $0.0063 | **$0.01** | understates by $0.0037 |
| $0.25 | 1 | $0.0132 | **$0.02** | understates by $0.0068 |
| $0.50 | 1 | $0.0175 | **$0.02** | understates by $0.0025 |
| $0.50 | 10 | $0.175 | **$0.18** | understates by $0.005 |
| $0.50 | 100 | $1.75 | $1.75 | exact |
| $0.10 | 100 | $0.63 | $0.63 | exact |

The previous implementation's docstring states its own intent:

> Rounded up because underestimating a cost that is subtracted from edge is
> the direction that puts on trades which do not clear their own fees.

It rounds up *per contract to a centicent*, which is not enough to reach the
published minimum of one cent per order. So at small order sizes it
understates in exactly the direction it set out to avoid. At a 1¢ contract
price the published fee is **14x** the modelled one.

Absolute amounts are sub-cent per order, so this is not a large money error.
It matters because it sits directly in the edge calculation, and because the
error is largest at the wings — the longshot prices where `MIN_EDGE_THRESHOLD`
and `LONGSHOT_EDGE_MULTIPLIER` are already doing delicate work.

---

## What this module deliberately refuses to answer

A series whose treatment the document does not pin down returns
`Unavailable`, never a default. `Unavailable` is not arithmetic-capable —
adding it or calling `float()` on it raises — so an unverified fee cannot
quietly become `0.0` inside a cost calculation. Same guarantee as
`reporting.evidence.Unavailable`, kept as a separate class so `core` does
not depend on `reporting`.

One case returns it:

- **Perpetual futures** (`KXPERP*`). Priced in basis points on a 30-day
  trailing volume tier — 12.0bps down to 2.6bps taker, 5.0 to 0.6 maker —
  and the tier depends on account volume history the ledger does not carry.

That is currently the only case. `AMBIGUOUS_SERIES` is empty.

### `KXMVE` was wrongly listed here, and the correction matters

The first transcription put `KXMVE` in this section, asserting its row "did
not extract with two legible columns". **That was false.** An independent
transcription read it cleanly, and rendering page 8 at 170dpi settles it —
the row prints:

```
KXMVE   Combos (excluding uncorrelated NFL combos)   2   1
```

Maker **2**, taker **1**, in the same column order (`Maker Multipler`, then
`Taker Multiplier`) every other row uses. The positioned glyphs confirm it:
`2` at x=341, `1` at x=394.

It is the only row in the whole table where maker ≠ taker, and the only
multiplier above 1. That irregularity is what made it look like a misread —
but "different from every other row" is not the same failure as "columns did
not extract legibly", and only the first was ever true here.

A maker multiplier of 2 makes the maker rate `2 x 0.0175 = 0.035`, exactly
half the taker rate. Makers still pay less than takers on this series, just
not the usual quarter.

Note the coincidence: `0.035` is also the number third-party write-ups
assert as a "$0.035 per-contract cap". The folklore cap may be a garbled
reading of this multiplier — but that is a guess about provenance, not a
finding, and no cap exists in the document either way.

**The lesson, recorded because it generalises:** refusing to guess is only a
virtue when the refusal is itself checked. An extraction artifact was
promoted to a finding without anyone looking at the page, and it took an
independent reader to catch it. A row is only unreadable once someone has
actually looked at it.

### Two rows that needed the same visual check

`KXMLBNL` and `KXNASDAQ100Y` sit either side of `KXMVE` on page 8. Two
independent text extractions disagreed with *each other* about which of the
two was missing its taker value — one pass showed `KXMLBNL` with two values
and `KXNASDAQ100Y` with one, the other the reverse. The rendered page shows
**both as `1 | 1`**.

The first transcription assigned `(1, 1)` to both, which happens to be
right, but by pattern-matching the surrounding rows rather than by reading
them. Both are now pinned individually in the tests so neither can drift
back to a default that was never actually verified.

### A note on what "unverified" means here

Absence from the non-standard table is **positive information, not missing
information**. The document's scope sentence says the general terms

> apply to all event contract markets on the exchange, apart from specific
> products listed below

so a series that is not listed is verified to take the default. Returning
`Unavailable` for those would contradict the source. `Unavailable` is
reserved for cases where the document is genuinely ambiguous or where the
rate depends on data outside it.

---

## Status

`core/fee_schedule.py` is **not yet wired into `core/pricing.py`**. Switching
the live fee model changes every edge computation, which is a behavioural
change requiring its own review. The module and its tests exist so that the
switch, when reviewed, is a small and verified change rather than a
rewrite.
