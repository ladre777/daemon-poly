"""
Production-readiness assessment. Reporting only.

This module answers one question: *is there enough trustworthy evidence to
justify a separately-reviewed conversation about deploying capital?* It never
answers "should we trade", it cannot enable orders, and it holds no reference
to any execution client.

There is deliberately no ``LIVE_READY`` state. The best available outcome is
``MEASUREMENT_READY``, meaning the instrumentation is sound enough that a
human decision could now rest on it. Naming a state that sounds like the
decision invites the report being mistaken for the decision, and the whole
point of a readiness report is that it is an input to a judgement made by
somebody accountable.

The default is ``INSUFFICIENT_EVIDENCE``. Requirements must actively
demonstrate they are satisfied; silence is never taken as success. Anything
actively disqualifying is ``BLOCKED``, and one BLOCKED requirement blocks the
overall result — see :func:`core.reasons.worst`.

Nothing here resets, clears or modifies state. If the kill switch is tripped
this reports it tripped; it does not untrip it.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from core.reasons import (
    BILLING_REASONS, PROVIDER_FAULT_REASONS, THROTTLE_REASONS,
    TRUNCATION_REASONS, Mode, Reason, Readiness, worst,
)
from reporting.evidence import (
    DEFAULT_MIN_FILL_SAMPLE, Unavailable, _is_value, _table_names,
    duplicate_accounting, mode_contamination,
)

#: A reconciliation older than this is stale for reporting purposes. Chosen
#: to be several multiples of a normal pass interval, so an ordinary gap
#: between passes never reads as staleness.
DEFAULT_RECONCILE_STALE_SECONDS = 1800

#: Material divergence between two category cells' observed event rates,
#: above which they are flagged rather than pooled.
DEFAULT_DIVERGENCE = 0.15


@dataclass
class Requirement:
    key: str
    title: str
    state: Readiness
    detail: str
    evidence: dict = field(default_factory=dict)


@dataclass
class ReadinessReport:
    overall: Readiness
    requirements: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    maker_prerequisites: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "overall": self.overall.value,
            "requirements": [
                {"key": r.key, "title": r.title, "state": r.state.value,
                 "detail": r.detail, "evidence": _plain(r.evidence)}
                for r in self.requirements
            ],
            "observed_blockers": self.blockers,
            "future_maker_prerequisites": self.maker_prerequisites,
        }


def _plain(obj):
    """Render Unavailable as a labelled string so JSON stays honest."""
    if isinstance(obj, Unavailable):
        return {"available": False, "reason": obj.reason}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj


#: The exact work a future, separately reviewed maker branch would require.
#: Listed here rather than in prose so the report can render it as a checklist
#: and nothing gets quietly dropped from it.
MAKER_PREREQUISITES = [
    "Verified order lifecycle: TTL expiry, market-close cancellation and "
    "reprice-after-confirmed-cancel, running in production and observed.",
    "Exchange-state reconciliation for resting orders, including recovery of "
    "unknown states without operator intervention.",
    "A quote generator. The quant path currently produces a fair value, not a "
    "two-sided quote, so there is nothing to rest.",
    "A GTC path in build_intent. Only IOC taker intents are built today and "
    "expires_at is never set, so the TTL sweep has nothing to act on.",
    "Inventory-aware sizing. Risk sizes for a taker fill; a resting quote must "
    "skew as the position builds.",
    "Fill and adverse-selection measurement: what fraction of resting fills "
    "occur immediately before an unfavourable move.",
    "Cancel/replace latency measurement, with a timeout policy for cancels "
    "that do not confirm.",
    "Per-order exposure reservation, already present, re-verified under "
    "resting orders rather than IOC.",
    "A verified Kalshi fee schedule. FEE_RATE=0.07 is documented as "
    "unverified; a maker earns the spread, so fee uncertainty is a far larger "
    "share of maker margin than of a wide taker edge.",
]


def _reason_counts(conn: sqlite3.Connection, since: float = None) -> dict:
    """Every recorded reason, summed across both places reasons are written.

    ``stage_events`` carries funnel refusals; ``provider_calls`` carries the
    outcome of each LLM attempt. A provider billing failure is normally only
    in the second table — the call failed, so the candidate never reached a
    stage worth recording. Reading only ``stage_events`` therefore reported
    zero billing failures on a database that contained several, which is the
    precise failure this whole module exists to prevent, reproduced inside
    the tool meant to detect it.
    """
    tables = _table_names(conn)
    counts: dict = {}

    if "stage_events" in tables:
        w, p = "", []
        if since is not None:
            w, p = " WHERE recorded_at >= ?", [float(since)]
        for r in conn.execute(
                f"""SELECT reason, SUM(count) AS n FROM stage_events{w}
                    GROUP BY reason""", p):
            if r["reason"]:
                counts[r["reason"]] = counts.get(r["reason"], 0) + int(r["n"] or 0)

    if "provider_calls" in tables:
        w, p = " WHERE outcome IS NOT NULL", []
        if since is not None:
            w += " AND recorded_at >= ?"
            p = [float(since)]
        for r in conn.execute(
                f"""SELECT outcome, COUNT(*) AS n FROM provider_calls{w}
                    GROUP BY outcome""", p):
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + int(r["n"] or 0)

    return counts


def assess(conn: sqlite3.Connection, *, live_ev, calibration_cells,
           dry_run: Optional[bool] = None,
           min_fill_sample: int = DEFAULT_MIN_FILL_SAMPLE,
           stale_after: float = DEFAULT_RECONCILE_STALE_SECONDS,
           now: float = None) -> ReadinessReport:
    """Evaluate every requirement and combine them conservatively."""
    now = time.time() if now is None else now
    tables = _table_names(conn)
    reqs: list = []
    reasons = _reason_counts(conn)

    # 1 -- DRY_RUN ------------------------------------------------------
    observed_dry = dry_run
    if observed_dry is None and "pass_telemetry" in tables:
        row = conn.execute(
            "SELECT dry_run FROM pass_telemetry WHERE dry_run IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        observed_dry = None if row is None else bool(row["dry_run"])
    if observed_dry is None:
        reqs.append(Requirement(
            "dry_run", "DRY_RUN state detected", Readiness.INSUFFICIENT_EVIDENCE,
            "DRY_RUN could not be determined from telemetry or parameters. "
            "Without it, no statement about live execution is defensible."))
    elif observed_dry:
        reqs.append(Requirement(
            "dry_run", "DRY_RUN state detected", Readiness.NOT_APPLICABLE,
            "DRY_RUN is true. No live P&L claim is possible from this data: "
            "orders were simulated and no capital was at risk. Every economic "
            "figure in this report is counterfactual.",
            {"dry_run": True}))
    else:
        reqs.append(Requirement(
            "dry_run", "DRY_RUN state detected", Readiness.MEASUREMENT_READY,
            "DRY_RUN is false; live execution was possible in this window.",
            {"dry_run": False}))

    # 2 -- reconciliation health ----------------------------------------
    snap = None
    if "account_snapshot" in tables:
        snap = conn.execute(
            "SELECT balance_cents, reconciled_at FROM account_snapshot WHERE id=1"
        ).fetchone()
    unknown_orders = 0
    if "orders" in tables:
        unknown_orders = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE state='unknown'").fetchone()[0] or 0)
    if snap is None or snap["reconciled_at"] is None:
        reqs.append(Requirement(
            "reconciliation", "Reconciliation health", Readiness.INSUFFICIENT_EVIDENCE,
            "No account snapshot has ever been written, so freshness cannot "
            "be established."))
    else:
        age = now - float(snap["reconciled_at"])
        if age < 0:
            # The snapshot is stamped after the report's as-of time. Either
            # the clock moved or --as-of predates the data; both make every
            # freshness claim meaningless. Reported as a broken measurement
            # rather than rendered as a negative age, which reads as "very
            # fresh" to a skimming eye.
            reqs.append(Requirement(
                "reconciliation", "Reconciliation health", Readiness.BLOCKED,
                f"Snapshot timestamp is {abs(age):.0f}s AFTER the report's "
                "as-of time. Freshness cannot be assessed against a clock "
                "that disagrees with the data.",
                {"snapshot_age_seconds": None,
                 "anomaly": "reconciled_at is in the future relative to as-of"}))
        elif unknown_orders:
            reqs.append(Requirement(
                "reconciliation", "Reconciliation health", Readiness.BLOCKED,
                f"{unknown_orders} order(s) are in state 'unknown'. Exposure "
                "cannot be bounded while any order state is unresolved. Not "
                "modified by this report.",
                {"unknown_orders": unknown_orders, "snapshot_age_seconds": age}))
        elif age > stale_after:
            reqs.append(Requirement(
                "reconciliation", "Reconciliation health", Readiness.BLOCKED,
                f"Last reconciliation was {age:.0f}s ago, beyond the "
                f"{stale_after:.0f}s staleness limit for this report.",
                {"snapshot_age_seconds": age}))
        else:
            reqs.append(Requirement(
                "reconciliation", "Reconciliation health", Readiness.MEASUREMENT_READY,
                f"Snapshot is {age:.0f}s old with no unknown order states.",
                {"snapshot_age_seconds": age, "unknown_orders": 0}))

    # 3 -- data integrity -----------------------------------------------
    contamination = mode_contamination(conn)
    duplicates = duplicate_accounting(conn)
    if contamination or duplicates:
        reqs.append(Requirement(
            "data_integrity", "Ledger data integrity", Readiness.BLOCKED,
            "Rows were found whose mode or accounting identity cannot be "
            "trusted. Any aggregate over them would be wrong in an unknown "
            "direction.",
            {"mode_contamination": contamination, "duplicates": duplicates}))
    else:
        reqs.append(Requirement(
            "data_integrity", "Ledger data integrity", Readiness.MEASUREMENT_READY,
            "Modes are cleanly separable; no duplicate fill or settlement "
            "identities found.", {"mode_contamination": [], "duplicates": []}))

    # 4 -- execution viability ------------------------------------------
    filled = getattr(live_ev, "contracts_filled", 0) or 0
    if filled >= min_fill_sample:
        reqs.append(Requirement(
            "execution_viability", "Fill-verified sample", Readiness.MEASUREMENT_READY,
            f"{filled} fill-verified contracts meet the explicit minimum of "
            f"{min_fill_sample}.",
            {"contracts_filled": filled, "minimum": min_fill_sample}))
    else:
        reqs.append(Requirement(
            "execution_viability", "Fill-verified sample", Readiness.INSUFFICIENT_EVIDENCE,
            f"{filled} fill-verified contracts against an explicit minimum of "
            f"{min_fill_sample}. No fee-net performance conclusion may rest on "
            "this. The minimum is a report parameter, stated so a reviewer can "
            "disagree with it rather than have it applied invisibly.",
            {"contracts_filled": filled, "minimum": min_fill_sample}))

    # 5 -- cost and latency ---------------------------------------------
    if _is_value(getattr(live_ev, "fees", None)) and \
            _is_value(getattr(live_ev, "avg_slippage_cents", None)):
        reqs.append(Requirement(
            "cost_latency", "Actual costs measured", Readiness.MEASUREMENT_READY,
            "Actual fees and realized slippage are both available for live rows.",
            {"fees": live_ev.fees, "avg_slippage_cents": live_ev.avg_slippage_cents}))
    else:
        reqs.append(Requirement(
            "cost_latency", "Actual costs measured", Readiness.INSUFFICIENT_EVIDENCE,
            "Actual fees and/or realized slippage are unavailable for live "
            "rows. Modelled costs alone do not satisfy this requirement: "
            "FEE_RATE is documented as unverified, so a modelled fee is an "
            "assumption being checked, not evidence.",
            {"fees": getattr(live_ev, "fees", None),
             "avg_slippage_cents": getattr(live_ev, "avg_slippage_cents", None)}))

    # 6 -- strategy stability -------------------------------------------
    live_cells = [c for c in calibration_cells if c.mode is Mode.LIVE]
    pool = live_cells or calibration_cells
    divergences = []
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if not (_is_value(a.observed_event_rate) and _is_value(b.observed_event_rate)):
                continue
            gap = abs(a.observed_event_rate - b.observed_event_rate)
            if gap >= DEFAULT_DIVERGENCE:
                divergences.append({
                    "a": f"{a.category}/{a.source}/{a.mode.value}",
                    "b": f"{b.category}/{b.source}/{b.mode.value}",
                    "observed_rate_gap": round(gap, 4),
                })
    if not pool:
        reqs.append(Requirement(
            "strategy_stability", "Per-strategy stability",
            Readiness.INSUFFICIENT_EVIDENCE,
            "No settled calibration cells exist, so divergence between "
            "strategies cannot be assessed."))
    else:
        reqs.append(Requirement(
            "strategy_stability", "Per-strategy stability",
            Readiness.INSUFFICIENT_EVIDENCE if divergences else Readiness.MEASUREMENT_READY,
            (f"{len(divergences)} category/source pair(s) diverge by at least "
             f"{DEFAULT_DIVERGENCE:.0%} in observed event rate. Reported "
             "separately rather than pooled — a pooled average across "
             "materially different cells describes none of them."
             if divergences else
             "No material divergence between category/source cells."),
            {"divergences": divergences,
             "cells": [f"{c.category}/{c.source}/{c.mode.value}" for c in pool]}))

    # 7 -- operational reliability --------------------------------------
    billing = sum(reasons.get(r.value, 0) for r in BILLING_REASONS)
    throttle = sum(reasons.get(r.value, 0) for r in THROTTLE_REASONS)
    faults = sum(reasons.get(r.value, 0) for r in PROVIDER_FAULT_REASONS)
    truncations = sum(reasons.get(r.value, 0) for r in TRUNCATION_REASONS)
    scan_cap = reasons.get(Reason.SCAN_CAP_REACHED.value, 0)
    no_spec = reasons.get(Reason.NO_CONTRACT_SPEC.value, 0)
    op_evidence = {
        "provider_billing_failures": billing,
        "provider_rate_limited": throttle,
        "provider_faults": faults,
        "truncations_total": truncations,
        "truncated_reject_recovered":
            reasons.get(Reason.TRUNCATED_REJECT_RECOVERED.value, 0),
        "truncated_approval_abstained":
            reasons.get(Reason.TRUNCATED_APPROVAL_ABSTAINED.value, 0),
        "scan_cap_reached": scan_cap,
        "contract_spec_exclusions": no_spec,
    }
    if not reasons:
        reqs.append(Requirement(
            "operational_reliability", "Provider and coverage reliability",
            Readiness.INSUFFICIENT_EVIDENCE,
            "No stage telemetry recorded. Provider availability, truncation "
            "rate, scan-cap incidence and spec exclusions are unmeasured — "
            "which is not the same as being zero.", op_evidence))
    elif billing:
        reqs.append(Requirement(
            "operational_reliability", "Provider and coverage reliability",
            Readiness.BLOCKED,
            f"{billing} provider billing failure(s) recorded. A checker that "
            "abstains because a vendor is unpaid is non-functional, not "
            "conservative, and its abstentions are not judgements.",
            op_evidence))
    else:
        reqs.append(Requirement(
            "operational_reliability", "Provider and coverage reliability",
            Readiness.MEASUREMENT_READY,
            "Provider outcomes, truncations, scan-cap incidence and spec "
            "exclusions are all measured and separately counted.", op_evidence))

    # 8 -- risk safety ---------------------------------------------------
    kill = None
    if "bot_state" in tables:
        kill = conn.execute(
            "SELECT kill_switch_tripped, kill_switch_tripped_at FROM bot_state "
            "WHERE id=1").fetchone()
    tripped = bool(kill["kill_switch_tripped"]) if kill else False
    if tripped:
        reqs.append(Requirement(
            "risk_safety", "Risk safety state", Readiness.BLOCKED,
            "The persistent kill switch is TRIPPED. Not reset by this report.",
            {"kill_switch_tripped": True,
             "tripped_at": kill["kill_switch_tripped_at"]}))
    elif unknown_orders:
        reqs.append(Requirement(
            "risk_safety", "Risk safety state", Readiness.BLOCKED,
            f"{unknown_orders} unknown order state(s) leave exposure unbounded.",
            {"unknown_orders": unknown_orders}))
    else:
        reqs.append(Requirement(
            "risk_safety", "Risk safety state", Readiness.MEASUREMENT_READY,
            "Kill switch not tripped; no unknown order states.",
            {"kill_switch_tripped": False, "unknown_orders": 0}))

    report = ReadinessReport(
        overall=worst(r.state for r in reqs),
        requirements=reqs,
        blockers=observed_blockers(conn, live_ev=live_ev, snapshot=snap,
                                  reasons=reasons, dry_run=observed_dry),
        maker_prerequisites=list(MAKER_PREREQUISITES),
    )
    return report


def observed_blockers(conn: sqlite3.Connection, *, live_ev, snapshot,
                      reasons: dict, dry_run: Optional[bool]) -> list:
    """Name the current blockers in a way that distinguishes lookalikes.

    Each entry separates two conditions that produce identical-looking
    inaction and have different remedies. That separation is the whole point:
    the 2026-08 production window produced ``approved=0`` for a reason
    ("balance is $0") that looks exactly like a working risk gate, and would
    have been read as one.
    """
    out = []

    balance = None if snapshot is None else snapshot["balance_cents"]
    if balance is not None and float(balance) <= 0:
        out.append({
            "blocker": "zero_exchange_balance",
            "detail": f"Reconciled balance is {float(balance)/100:.2f} USD. Risk "
                      "refuses upstream of every gate, so no gate judgement is "
                      "being exercised at all.",
            "not_to_be_confused_with": "risk-gate rejection on a populated "
                                       "account, which would be a real judgement",
        })
    elif balance is not None:
        out.append({
            "blocker": "none_from_balance",
            "detail": f"Balance is {float(balance)/100:.2f} USD, so risk "
                      "rejections in this window are genuine gate decisions.",
            "not_to_be_confused_with": "zero-balance upstream refusal",
        })

    if dry_run:
        out.append({
            "blocker": "dry_run_simulation",
            "detail": "Orders were simulated. Absence of fills is a "
                      "configuration consequence, not an execution failure.",
            "not_to_be_confused_with": "a live execution failure, which would "
                                       "appear as rejected/unknown order states",
        })

    billing = sum(reasons.get(r.value, 0) for r in BILLING_REASONS)
    throttle = sum(reasons.get(r.value, 0) for r in THROTTLE_REASONS)
    trunc_rej = reasons.get(Reason.TRUNCATED_REJECT_RECOVERED.value, 0)
    trunc_app = reasons.get(Reason.TRUNCATED_APPROVAL_ABSTAINED.value, 0)
    if billing or throttle or trunc_rej or trunc_app:
        out.append({
            "blocker": "provider_degradation",
            "detail": (f"billing failures={billing}, rate-limited={throttle}, "
                       f"truncated rejects recovered={trunc_rej}, truncated "
                       f"approvals abstained={trunc_app}."),
            "not_to_be_confused_with": "a single generic 'LLM unavailable' "
                                       "count — these four have four different "
                                       "remedies (money, waiting, token cap, "
                                       "token cap)",
        })

    scan_cap = reasons.get(Reason.SCAN_CAP_REACHED.value, 0)
    no_cands = reasons.get(Reason.NO_CANDIDATES.value, 0)
    if scan_cap or no_cands:
        out.append({
            "blocker": "coverage_limit",
            "detail": f"scan-cap reached on {scan_cap} pass(es); "
                      f"{no_cands} pass(es) genuinely found no candidates.",
            "not_to_be_confused_with": "each other — a cap means markets were "
                                       "never examined, no-candidates means "
                                       "they were examined and rejected",
        })

    no_spec = reasons.get(Reason.NO_CONTRACT_SPEC.value, 0)
    quality = reasons.get(Reason.MARKET_QUALITY.value, 0)
    if no_spec or quality:
        out.append({
            "blocker": "pricing_coverage",
            "detail": f"{no_spec} declined for missing contract spec; "
                      f"{quality} declined on market quality.",
            "not_to_be_confused_with": "each other — a missing spec is a "
                                       "capability gap fixed by adding a spec; "
                                       "market quality is a per-instance "
                                       "rejection of a priceable family",
        })

    deduped = reasons.get(Reason.LADDER_DEDUPED.value, 0)
    out.append({
        "blocker": "ladder_dedup_activity",
        "detail": (f"{deduped} proposal(s) dropped by the ladder cap."
                   if deduped else
                   "Zero dedup actions. The cap is deployed and instrumented "
                   "but has never bound: no (event_ticker, direction) group "
                   "exceeded it. The mechanism is unexercised in production, "
                   "so its projected saving remains a projection."),
        "not_to_be_confused_with": "the dedup being disabled or absent",
    })
    return out
