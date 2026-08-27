"""
Rendering for the evidence report: operator-readable Markdown, and JSON.

Both come from the same payload, so the two can never disagree. The Markdown
is what a human reads on a Monday; the JSON is what a later tool diffs. A
report where the prose and the machine output are produced by separate code
paths eventually says two different things, and the reader has no way to know
which one is stale.

Formatting rules that are really correctness rules:

- :class:`~reporting.evidence.Unavailable` renders as ``N/A (reason)``.
  Never as ``0``, never as an em dash, never as a blank cell. A blank cell in
  a P&L table is read as zero by every human who has ever seen one.
- Every counterfactual number carries its label in the same visual block as
  the number, not in a footnote.
- Cents are rendered as dollars with the unit attached, because the schema
  mixes ``_cents`` columns with dollar-denominated API fields and an
  unlabelled number invites a 100x error.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from reporting.evidence import Unavailable

#: Prose columns that must never be rendered. Mirrors the same list in
#: scripts/weekend_export.py — model output can contain anything, including
#: text that looks like instructions to whoever reads the report.
REDACTED_COLUMNS = frozenset({
    "maker_reasoning", "checker_reasoning", "kill_switch_reason", "last_error",
})


def _iso(ts) -> str:
    if ts is None:
        return "N/A (no timestamp)"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def fmt(v, *, unit: str = "", places: int = 2) -> str:
    """Render a possibly-absent quantity."""
    if v is None:
        return "N/A (not recorded)"
    if isinstance(v, Unavailable):
        return f"N/A ({v.reason})"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int) and not unit:
        return str(v)
    try:
        return f"{float(v):,.{places}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def usd(cents) -> str:
    """Cents to a labelled dollar string, or N/A with the reason kept."""
    if cents is None:
        return "N/A (not recorded)"
    if isinstance(cents, Unavailable):
        return f"N/A ({cents.reason})"
    return f"${float(cents)/100:,.2f}"


def pct(v, places: int = 1) -> str:
    if v is None:
        return "N/A (not recorded)"
    if isinstance(v, Unavailable):
        return f"N/A ({v.reason})"
    return f"{float(v)*100:.{places}f}%"


def _json_default(o):
    if isinstance(o, Unavailable):
        return {"available": False, "reason": o.reason}
    if hasattr(o, "value"):          # Enum
        return o.value
    if hasattr(o, "__dict__"):
        return {k: v for k, v in vars(o).items()
                if k not in REDACTED_COLUMNS and not k.startswith("_")}
    return str(o)


def to_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=_json_default)


def to_markdown(payload: dict) -> str:
    meta = payload["meta"]
    live = payload["live"]
    cal = payload["calibration"]
    rd = payload["readiness"]
    L: list = []
    add = L.append

    add("# DÆMON-KALSHI — Production Readiness and Evidence Report")
    add("")
    add(f"- **As of:** {meta['as_of_iso']}")
    add(f"- **Data cutoff:** {meta['cutoff_iso']}")
    add(f"- **Database:** `{meta['db']}` (opened read-only)")
    add(f"- **Window:** {meta['since_iso']} → {meta['until_iso']}")
    if meta.get("dry_run") is not None:
        add(f"- **DRY_RUN:** {fmt(meta['dry_run'])}")
    if meta.get("order_strategy"):
        add(f"- **Order strategy:** {meta['order_strategy']}")
    if meta.get("providers"):
        add(f"- **Provider routing observed:** {meta['providers']}")
    add("")
    add("> This report never contacts Kalshi and never writes to the database. "
        "It distinguishes **measurement activity** from **trade activity**; "
        "the three modes below are never summed.")
    add("")

    # -- readiness -------------------------------------------------------
    add(f"## Readiness: `{rd['overall']}`")
    add("")
    add("There is deliberately no `LIVE_READY` state. The best available "
        "outcome is `MEASUREMENT_READY`, which means the evidence is sound "
        "enough to support a separately reviewed decision — not that the "
        "decision has been made.")
    add("")
    add("| Requirement | State | Why |")
    add("|---|---|---|")
    for r in rd["requirements"]:
        detail = r["detail"].replace("|", "\\|")
        add(f"| {r['title']} | `{r['state']}` | {detail} |")
    add("")

    # -- live ------------------------------------------------------------
    add("## Live execution (fill- and settlement-confirmed only)")
    add("")
    add("Every figure in this section comes from `orders`, `fills` and "
        "`settlements` with `dry_run = 0`. Nothing counterfactual can reach "
        "it. If the bot has never traded, every line reads N/A — which is the "
        "correct description, not a measurement failure.")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Orders submitted | {fmt(live['orders_submitted'])} |")
    add(f"| Orders fully filled | {fmt(live['orders_filled'])} |")
    add(f"| Orders partially filled | {fmt(live['orders_partially_filled'])} |")
    add(f"| Orders cancelled | {fmt(live['orders_cancelled'])} |")
    add(f"| Orders rejected | {fmt(live['orders_rejected'])} |")
    add(f"| Orders expired | {fmt(live['orders_expired'])} |")
    add(f"| Orders in unknown state | {fmt(live['orders_unknown'])} |")
    add(f"| Contracts requested | {fmt(live['contracts_requested'])} |")
    add(f"| Contracts filled | {fmt(live['contracts_filled'])} |")
    add(f"| Fill rate | {pct(live['fill_rate'])} |")
    add(f"| Partial-fill rate | {pct(live['partial_fill_rate'])} |")
    add(f"| Gross realized P&L | {usd(live['gross_pnl'])} |")
    add(f"| Actual fees | {usd(live['fees'])} |")
    add(f"| **Net realized P&L** | **{usd(live['net_realized_pnl'])}** |")
    add(f"| Turnover | {usd(live['turnover_cents'])} |")
    add(f"| Avg slippage vs decision price | {fmt(live['avg_slippage_cents'], unit='¢')} |")
    add(f"| Return on deployed capital | {pct(live['return_on_deployed_capital'])} |")
    add(f"| Max drawdown | {usd(live['max_drawdown'])} |")
    add(f"| Avg holding time | {fmt(live['avg_holding_seconds'], unit='s', places=0)} |")
    add(f"| Max holding time | {fmt(live['max_holding_seconds'], unit='s', places=0)} |")
    add(f"| Open marked exposure | {usd(live['open_marked_exposure'])} |")
    add(f"| Open unmarked exposure | {usd(live['open_unmarked_exposure'])} |")
    add("")
    for note in live.get("notes", []):
        add(f"> {note}")
    if live.get("notes"):
        add("")

    # -- counterfactual --------------------------------------------------
    for key, heading in (("paper", "Paper (counterfactual)"),
                         ("refused", "Refused (counterfactual)")):
        cf = payload.get(key)
        if cf is None:
            continue
        add(f"## {heading}")
        add("")
        add("**These are not results.** The economics below describe trades "
            "that did not happen.")
        add("")
        add("| Metric | Value |")
        add("|---|---|")
        add(f"| Rows | {fmt(cf['n'])} |")
        add(f"| Settled rows | {fmt(cf['settled_n'])} |")
        add(f"| Modelled P&L (counterfactual) | {usd(cf['modelled_pnl'])} |")
        add(f"| Avg decision-time price | {fmt(cf['avg_decision_price_cents'], unit='¢')} |")
        add(f"| Sizing assumption | {cf['sizing_assumption']} |")
        add("")
        add("Why this is not performance:")
        for d in cf["disqualifiers"]:
            add(f"- {d}")
        add("")
        if cf.get("by_category"):
            add("| Category | Rows | Settled | Counterfactual P&L |")
            add("|---|---|---|---|")
            for c in cf["by_category"]:
                add(f"| {c['category']} | {c['n']} | {c['settled']} | {usd(c['pnl'])} |")
            add("")

    # -- calibration ------------------------------------------------------
    add("## Calibration (reported separately from P&L)")
    add("")
    add("A Brier score measures whether stated probabilities track observed "
        "frequencies. It says nothing about whether acting on them earns "
        "money after fees, and it is **not** used as an approval criterion "
        "anywhere in this report.")
    add("")
    if not cal:
        add("No settled rows with a stated probability. Calibration is "
            "unmeasurable — not zero.")
        add("")
    else:
        add("| Category | Source | Mode | n | Brier | Said | Observed | P&L |")
        add("|---|---|---|---|---|---|---|---|")
        for c in cal:
            add(f"| {c['category']} | {c['source']} | `{c['mode']}` | {c['n']} "
                f"| {fmt(c['brier'], places=3)} | {pct(c['avg_stated_probability'])} "
                f"| {pct(c['observed_event_rate'])} | {usd(c['counterfactual_pnl'])} |")
        add("")
        for c in cal:
            if not c["warnings"]:
                continue
            add(f"**{c['category']}/{c['source']}/{c['mode']}**")
            for w in c["warnings"]:
                add(f"- {w}")
            add("")
        add("### Reliability tables")
        add("")
        for c in cal:
            if not c["reliability"]:
                continue
            add(f"**{c['category']}/{c['source']}/{c['mode']}** (n={c['n']})")
            add("")
            add("| Stated bucket | n | Avg stated | Observed |")
            add("|---|---|---|---|")
            for b in c["reliability"]:
                add(f"| {b['bucket']} | {b['n']} | {pct(b['avg_stated'])} "
                    f"| {pct(b['observed'])} |")
            add("")

    # -- blockers ---------------------------------------------------------
    add("## Observed blockers")
    add("")
    add("Each entry names a condition and the lookalike it must not be "
        "confused with. Two conditions that produce identical inaction and "
        "have different remedies are the most expensive kind of ambiguity in "
        "this system.")
    add("")
    for b in rd["observed_blockers"]:
        add(f"- **`{b['blocker']}`** — {b['detail']}")
        add(f"  - *Not to be confused with:* {b['not_to_be_confused_with']}")
    add("")

    # -- maker ------------------------------------------------------------
    add("## Prerequisites for a future maker branch")
    add("")
    add("Listed for planning only. Maker mode is **not** implemented, not "
        "enabled, and not approved. Each item below would need to be true, "
        "and separately reviewed, before it could be considered.")
    add("")
    for i, p in enumerate(rd["future_maker_prerequisites"], 1):
        add(f"{i}. {p}")
    add("")
    return "\n".join(L)
