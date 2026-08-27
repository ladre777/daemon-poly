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
What varies is a per-series multiplier `M`, published as either `0` or `1`
in the Non-Standard Fees table.

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
  multiplier applies: `0.0175 / 0.07 = 0.25` exactly.
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

The table is unambiguous and machine-checkable, so the table is what
`core/fee_schedule.py` implements. The prose discrepancy is recorded in
`provenance()["rounding_note"]` and listed as an open question in the review
document rather than silently resolved.

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

Two cases return it:

- **`KXMVE`** (Combos). The table row extracts as
  `Combos (excluding uncorrelated NFL combos) 12`, which could be
  maker=1/taker=2 or a single multiplier of 12. The PDF layout does not
  disambiguate the columns. A guess here would be a silently wrong cost on
  every leg of a combo.
- **Perpetual futures** (`KXPERP*`). Priced in basis points on a 30-day
  trailing volume tier — 12.0bps down to 2.6bps taker, 5.0 to 0.6 maker —
  and the tier depends on account volume history the ledger does not carry.

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
