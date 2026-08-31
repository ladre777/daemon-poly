"""
DÆMON-KALSHI orchestrator.

If Maker has no usable LLM key, LLM path is skipped quietly and quant continues.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from config import CONFIG
from core.account_state import AccountState, ReconciliationError
from core.errors import CircuitBreaker, classify
from core.llm_client import LLMRateLimited
from core.kalshi_client import KalshiClient
from core.telegram_commands import TelegramCommandListener
from core.reasons import Reason, Stage, stats_to_events
from core.tickers import family_of
from memory.db import storage_status
from memory.edge_store import EdgeStore
from memory.order_store import OrderStore, SignalAlertStore, signal_key
from memory.price_store import PriceStore
from memory.telemetry_store import TelemetryStore
try:
    from scripts.ledger_answers import emit as emit_ledger_answers
except Exception:  # pragma: no cover - the report is never worth a crash loop
    # An observer that cannot be imported must degrade to nothing. The call
    # site below already swallows exceptions, but an unguarded import runs
    # before any of that and would take the process down at boot.
    def emit_ledger_answers(*_a, **_k):
        pass
from workers.scout import Scout
from workers.maker import Maker
from workers.checker import Checker
from workers.risk_guardrail import RiskGuardrail, KillSwitchTripped
from workers.order_lifecycle import OrderLifecycle
from workers.execution import (
    DuplicateOrderBlocked,
    Execution,
    UnmanagedMakerMode,
    assert_order_strategy_supported,
)
from workers.ledger import Ledger
from workers.reflect import Reflector
from workers.context import ContextEnricher
from core.espn_client import ESPNClient
from core.slash_golf_client import SlashGolfClient
from core.weather_client import NOAAClient
from core.fred_client import FredClient
from core.rti_runner import RTIFeedRunner
from core.spot_price_client import SpotPriceClient
from core.telegram_client import TelegramClient
from workers.quant_maker import QuantMaker
from workers.arbitrage import ArbitrageScanner
from workers.quote_observer import QuoteObserver
from workers.coherence import CoherenceGate
from workers.ladder_dedup import select_for_checker

logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(CONFIG.httpx_log_level)
logging.getLogger("httpcore").setLevel("WARNING")
log = logging.getLogger("daemon_kalshi.main")
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def _log_vol_signature(spot_client, symbols) -> None:
    for symbol in symbols:
        history = spot_client.history.get(symbol)
        if history is None:
            continue
        lookback = CONFIG.risk.vol_history_retention_seconds
        rungs = []
        for interval, vol in history.vol_signature(lookback):
            label = "tick" if not interval else f"{interval:.0f}s"
            rungs.append(
                f"{label} {vol * _SECONDS_PER_YEAR ** 0.5:.0%}" if vol else f"{label} n/a"
            )
        log.info("Volatility signature %s over %.0fs (annualized): %s",
                 symbol, lookback, "  ".join(rungs))

        # The signature above samples at several intervals; the quant path
        # prices with realized_vol_robust at a lookback chosen per contract,
        # max(3600, min(seconds_to_expiry * 20, 86400)). Those can disagree,
        # which is not a bug — but a diagnostic reporting a number other than
        # the one setting prices is exactly how a 6.6%-annualized bitcoin
        # survived a whole session unnoticed. So both ends of the range the
        # quant path can actually choose are logged alongside it.
        floor_vol = history.realized_vol_robust(3600)
        cap_vol = history.realized_vol_robust(86400)

        def _annualized(v):
            # None is "not enough history to say", which is a different fact
            # from a low volatility and must not render as one.
            return f"{v * _SECONDS_PER_YEAR ** 0.5:.0%}" if v else "n/a"

        log.info("Pricing vol %s (realized_vol_robust, annualized): "
                 "3600s %s  86400s %s", symbol,
                 _annualized(floor_vol), _annualized(cap_vol))


def _alert(notifier, method: str, *args, **kwargs) -> None:
    if notifier is None:
        return
    try:
        getattr(notifier, method)(*args, **kwargs)
    except Exception:
        log.exception("Notification %s failed — continuing", method)


class Health:
    def __init__(self, notifier=None):
        started = time.time()
        self.notifier = notifier
        self.started_at = started
        self.last_scan_at = started
        self.last_reconcile_at = started
        self.orders_placed = 0
        self.fills = 0
        self._last_summary_date = None

    def mark_scanned(self) -> None:
        self.last_scan_at = time.time()

    def mark_reconciled(self) -> None:
        self.last_reconcile_at = time.time()

    def mark_order(self) -> None:
        self.orders_placed += 1

    def mark_fill(self) -> None:
        self.fills += 1

    def check_stalled(self) -> None:
        limit = CONFIG.telegram.stall_alert_seconds
        if limit <= 0:
            return
        now = time.time()
        for what, last in (("scan", self.last_scan_at),
                           ("reconciliation", self.last_reconcile_at)):
            age = now - last
            if age > limit:
                log.error("No successful %s in %.0fs", what, age)
                _alert(self.notifier, "notify_stalled", what, age)

    def maybe_daily_summary(self, risk, account, order_store) -> bool:
        hour = CONFIG.telegram.daily_summary_hour_utc
        if hour < 0:
            return False
        now = datetime.now(timezone.utc)
        if now.hour != hour or self._last_summary_date == now.date():
            return False
        self._last_summary_date = now.date()
        snapshot = account.snapshot
        _alert(
            self.notifier, "notify_daily_summary",
            trades=self.orders_placed,
            fills=self.fills,
            realized_pnl=risk.realized_pnl_today(),
            kill_switch_tripped=order_store is not None and risk._killed,
            exposure_usd=snapshot.worst_case_exposure_cents() / 100 if snapshot else 0.0,
            open_positions=snapshot.open_position_count() if snapshot else 0,
        )
        return True


def install_shutdown_handlers() -> dict:
    shutdown = {"signal": None}

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        if shutdown["signal"] is None:
            log.warning("%s received — finishing this pass, then shutting down", name)
            shutdown["signal"] = name

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    return shutdown


def _check_balance_change(snapshot, store, order_store, notifier) -> None:
    """Alert when the exchange balance moves, in either direction.

    The baseline lives in the database, not in memory: Railway restarts on
    every push, and an in-memory baseline would fire a false alert on the
    first reconcile after each deploy until the alert got muted as noise.

    A first-ever observation records the baseline and stays silent — there is
    nothing to compare against, and "None" must not be reported as a drop to
    zero. After that, every move past the threshold is reported, with the
    count of fills the bot recorded in the same window, because that is what
    separates "we spent it" from "something else moved it".
    """
    if store is None:
        return
    current_cents = snapshot.balance_cents
    try:
        previous_cents = store.load_last_balance()
    except Exception:
        # Alerting must never be able to stop a pass. A balance we failed to
        # read is a missed alert, not a reason to skip trading.
        log.exception("Could not read the balance baseline — skipping the check")
        return

    if previous_cents is None:
        store.set_last_balance(current_cents)
        log.info(
            "Balance baseline recorded at $%.2f — changes from here are alerted",
            current_cents / 100,
        )
        return

    delta_cents = current_cents - previous_cents
    threshold_cents = CONFIG.telegram.balance_alert_threshold_usd * 100
    if abs(delta_cents) < threshold_cents:
        return

    fills = 0
    if order_store is not None:
        try:
            # Bounded to the same window as the balance change, so the count
            # answers "did we cause THIS move" rather than "have we ever
            # traded". Falls back to counting nothing if the timestamp is
            # missing, which reads as "the bot did not do this" — the
            # conservative direction, since it prompts a look.
            fills = order_store.fills_recorded_since(store.load_last_balance_at())
        except Exception:
            log.exception("Could not count recent fills — alerting without that detail")

    log.warning(
        "Balance moved $%+.2f ($%.2f -> $%.2f) with %d bot fill(s) recorded "
        "in the window",
        delta_cents / 100, previous_cents / 100, current_cents / 100, fills,
    )
    _alert(notifier, "notify_balance_change",
           previous_cents / 100, current_cents / 100, fills)
    # Written after alerting, so a crash mid-alert re-reports rather than
    # silently swallowing the change.
    store.set_last_balance(current_cents)


def _log_checker_verdicts(stats, verdict_confidence, verdict_families=None) -> None:
    """One INFO line summarising what the Checker actually decided.

    The pass funnel says how many verdicts came back; it cannot say what
    they were. While the exchange balance is zero the distinction is
    invisible everywhere else: risk.evaluate refuses on bankroll before it
    reaches the verdict branch, so an approval and a rejection are logged
    identically by the ledger. Without this line the only durable record is
    edges.checker_verdict, which needs the database to read.

    Every number here is already in hand when this is called. Nothing is
    recomputed and nothing is queried.
    """
    checked = stats["checked"]
    if not checked:
        return
    approved = checked - stats["checker_rejected"]

    def _mean(bucket):
        vals = verdict_confidence.get(bucket) or []
        # No confidences recorded is not a confidence of zero. Averaging an
        # absence would report a number nobody produced.
        return f"{sum(vals) / len(vals):.2f}" if vals else "n/a"

    log.info(
        "Checker verdicts: approved=%d rejected=%d of %d checked "
        "(%.0f%% approved) | mean confidence approve=%s reject=%s",
        approved, stats["checker_rejected"], checked,
        100.0 * approved / checked, _mean("approve"), _mean("reject"),
    )

    # Which families the Checker is rejecting, which the aggregate above
    # cannot say. A family rejected every pass is a different problem from
    # one rejected occasionally: the first is a systematic disagreement
    # between the model and that market's pricing, and it costs a Checker
    # call every pass to rediscover. Sorted by rejection count so the
    # worst offender is first rather than whichever ticker sorted first.
    if verdict_families:
        parts = [
            f"{fam} {a}/{a + r}"
            for fam, (a, r) in sorted(
                verdict_families.items(), key=lambda kv: (-kv[1][1], kv[0])
            )
        ]
        log.info("Checker verdicts by family (approved/checked): %s", " ".join(parts))


def _frozen_probe_pass(pass_index: int) -> bool:
    """Is this a pass on which frozen categories get priced at all?

    Deterministic on the pass index rather than random, so the behaviour is
    reproducible in a test and predictable in a log. A rate of 0 or 1 means
    every pass is a probe — which still never trades the category, it only
    stops saving the model calls.
    """
    rate = CONFIG.frozen_category_sample_rate
    if rate <= 1:
        return True
    return pass_index % rate == 0


def run_once(scout, maker, quant_maker, checker, risk, execution, ledger, account,
             notifier=None, health=None, alert_store=None, arb_scanner=None,
             quote_observer=None,
             coherence_gate=None, store=None, order_store=None, telemetry=None,
             pass_index: int = 0):
    try:
        snapshot = account.reconcile()
        if health is not None:
            health.mark_reconciled()
    except ReconciliationError as e:
        log.error("Reconciliation failed — placing no orders this pass: %s", e)
        _alert(notifier, "notify_systemic_error", "reconciliation", str(e))
        return 0
    # Before the tradeability gate: a balance that just went to zero makes the
    # account untradeable, and that is exactly the change worth alerting on.
    # Checking after the early return would guarantee silence in the one case
    # this exists for.
    _check_balance_change(snapshot, store, order_store, notifier)

    if not snapshot.is_tradeable:
        log.error("Not safe to trade this pass: %s", snapshot.blocking_reason())
        _alert(notifier, "notify_systemic_error", "account_state",
               snapshot.blocking_reason() or "account not tradeable")
        return 0

    quant_maker.begin_pass()

    # Telemetry is an observer and is allowed to fail. Every call below
    # tolerates a None pass_id, and TelemetryStore swallows its own errors,
    # so nothing in this block can abort a trading pass.
    pass_id = None
    pass_started = time.time()
    if telemetry is not None:
        pass_id = telemetry.begin_pass(
            dry_run=CONFIG.risk.dry_run, kalshi_env=CONFIG.kalshi.env,
            order_strategy=CONFIG.risk.order_strategy,
            balance_cents=getattr(snapshot, "balance_cents", None),
        )

    scout_started = time.time()
    candidates = scout.scan()
    scout_ms = (time.time() - scout_started) * 1000.0
    if health is not None:
        health.mark_scanned()
    log.info("Scout returned %d candidates", len(candidates))

    def _is_priority(c):
        title = c.title.lower()
        return any(kw in title for kw in CONFIG.priority_keywords)

    def _is_preferred(c):
        return c.category.lower() in CONFIG.priority_categories

    candidates.sort(key=lambda c: (not _is_priority(c), not _is_preferred(c)))

    filled_this_pass = 0
    #: Confidence of every verdict the Checker returned this pass, split by
    #: disposition. Collected from verdicts already in hand inside the loop
    #: below — no second pass, no query. Exists because checker_rejected and
    #: risk_refused were counted and then never printed, which made the gap
    #: between `checked` and `approved` unreadable from the log stream even
    #: though the numbers were sitting in `stats`.
    verdict_confidence: dict[str, list[float]] = {"approve": [], "reject": []}
    verdict_families: dict[str, list[int]] = {}
    priced: list = []
    llm_calls_this_pass = 0
    llm_calls_by_event: dict[str, int] = defaultdict(int)
    stats: dict[str, int] = defaultdict(int)
    coherence_gate = coherence_gate or CoherenceGate()
    coherence_gate.begin_pass()

    if arb_scanner is not None:
        arb_scanner.begin_pass()
        stats["locked_arbs"] += len(arb_scanner.scan(candidates))
        # How much of the book the arb scanner could actually see. A pass that
        # priced every market off a derived NO ask searched for crossed books,
        # not for arbs — and for ten days every pass was that pass.
        stats["arb_real_no_ask"] += arb_scanner.real_no_ask
        stats["arb_derived_no_ask"] += arb_scanner.derived_no_ask

    # The quoting probe. Records the quote it would have rested on each
    # candidate and resolves quotes recorded on earlier passes against the
    # book as it stands now. Placed beside the arb scan because both are
    # read-only passes over the same books, and neither can place an order.
    if quote_observer is not None:
        for k, v in quote_observer.run(candidates, now=pass_started).items():
            stats[f"quote_{k}"] += v

    model_breaker = CircuitBreaker(
        name="model-calls", threshold=CONFIG.model_failure_threshold, cooldown_seconds=0
    )
    # No key / broken provider → skip LLM quietly; quant still runs.
    llm_disabled = not getattr(maker, "available", True)

    frozen_probe = _frozen_probe_pass(pass_index)
    frozen_seen: dict[str, int] = defaultdict(int)

    for candidate in candidates:
        proposal = None
        # A frozen category never reaches risk or execution. It is sampled
        # rather than dropped so a trickle of counterfactual rows keeps
        # landing — that is the only way a future model on this path can be
        # seen flipping the sign that froze it.
        is_frozen = candidate.category.lower() in CONFIG.frozen_categories
        if is_frozen:
            frozen_seen[candidate.category.lower()] += 1
            if not frozen_probe:
                stats["frozen_not_probed"] += 1
                continue
        if quant_maker.can_handle(candidate):
            stats["quant_attempted"] += 1
            quant_result = quant_maker.propose(candidate)
            if quant_result:
                proposal = quant_result.to_maker_proposal()
                if proposal is None:
                    stats["quant_below_edge_threshold"] += 1
            else:
                stats["quant_no_proposal"] += 1
        elif candidate.category.lower() in CONFIG.llm_reasoning_categories:
            if llm_disabled:
                stats["llm_disabled"] += 1
                continue

            is_priority = _is_priority(candidate)
            call_cap = (
                CONFIG.max_fallback_llm_calls_per_pass
                if getattr(maker, "on_fallback", False)
                else CONFIG.max_llm_calls_per_pass
            )
            if coherence_gate.is_tainted(candidate):
                stats["llm_skipped_tainted"] += 1
                continue

            # Same idea as the line above, one timescale up. is_tainted
            # forgets at begin_pass, so a family refused for incoherence on
            # every pass for hours still cost a Maker call every pass to
            # rediscover it. Sampled rather than dropped: the family is
            # re-probed periodically and returns to full rate the moment it
            # produces a coherent answer.
            if coherence_gate.should_skip_maker(candidate):
                stats["llm_skipped_incoherent_family"] += 1
                continue

            event_key = candidate.event_ticker or candidate.ticker
            # The per-event cap binds on priority candidates too. It used to
            # be skipped for them, and PRIORITY_KEYWORDS matches essentially
            # every in-scope candidate (btc, eth, high, wti, gold, fed, cpi,
            # golf...), so in practice the cap was dead: a deep ladder on one
            # event could consume the whole pass. The per-pass cap below is
            # still waived for priority candidates — that is what keeps a
            # priority event from being starved by earlier ones.
            per_event_cap = CONFIG.max_llm_calls_per_event
            if per_event_cap and llm_calls_by_event[event_key] >= per_event_cap:
                stats["llm_capped_per_event"] += 1
                continue

            if not is_priority and call_cap and llm_calls_this_pass >= call_cap:
                stats["llm_capped"] += 1
                continue
            try:
                proposal = maker.propose(candidate)
            except LLMRateLimited as e:
                stats["llm_rate_limited"] += 1
                llm_disabled = True
                log.warning(
                    "LLM provider rate-limited; skipping remaining LLM candidates "
                    "for this pass: %s", e
                )
                _alert(notifier, "notify_provider_rate_limited", "gemini", str(e))
                continue
            except Exception as e:
                severity = classify(e)
                stats["maker_failed"] += 1
                log.warning("Maker failed on %s (%s): %s", candidate.ticker, severity.value, e)
                if model_breaker.record_failure(f"{type(e).__name__}: {e}"):
                    llm_disabled = True
                    _alert(
                        notifier, "notify_systemic_error", "maker",
                        f"{model_breaker.consecutive_failures} consecutive Maker "
                        f"failures — LLM path paused for this pass (quant continues). "
                        f"Last error: {e}"
                    )
                continue
            model_breaker.record_success()
            llm_calls_this_pass += 1
            llm_calls_by_event[event_key] += 1
            stats["llm_called"] += 1
            if proposal is None:
                stats["llm_below_edge_threshold"] += 1
        else:
            stats["no_grounding_source"] += 1
            continue

        if not proposal:
            continue
        stats["proposed"] += 1

        # Before the coherence gate on purpose. A frozen proposal is not being
        # judged, it is being recorded, so it must not touch CoherenceGate's
        # per-family counters — letting it do so would let a path we have
        # stopped trading still change the sampling of one we have not.
        if is_frozen:
            stats["frozen_refused"] += 1
            ledger.log_refused_proposal(
                proposal,
                action="skipped_frozen",
                reason=(
                    f"{candidate.category} is frozen: demonstrated loss "
                    f"(cluster-robust t=-2.11 over 18 events). Priced for "
                    f"counterfactual grading only, never traded."
                ),
            )
            continue

        coherence = coherence_gate.check(proposal)
        if not coherence.ok:
            stats["incoherent"] += 1
            # Recorded under which gate refused, not a single
            # `skipped_incoherent`. A model that says a higher strike is more
            # likely is broken; a model that disagrees with the market by a
            # wide margin may simply be right, and those two need opposite
            # responses. Sharing one label made the refused-PnL for a family
            # unable to tell them apart. Falls back to the old label if a
            # report ever arrives without a kind, so an unlabelled refusal
            # lands somewhere honest rather than being dropped.
            ledger.log_refused_proposal(
                proposal,
                action=f"skipped_{coherence.kind}" if coherence.kind
                else "skipped_incoherent",
                reason=coherence.reason,
            )
            continue

        priced.append(proposal)

    # The pass is split here, and it has to be. Selecting the best strikes on
    # a ladder means ranking them against each other, and nothing above this
    # line ever has a whole ladder in hand — CoherenceGate says so in its own
    # docstring, and it is right: it is incremental by design because it must
    # refuse a proposal before the next one is priced.
    #
    # Pricing is cheap and local; the Checker is the expensive, remote call.
    # So everything up to here still runs per candidate, and only the Checker
    # and what follows it wait for the full set. Quotes are refreshed again
    # before execution, so the added delay does not trade on a stale price.
    selected, dropped = select_for_checker(
        priced, CONFIG.max_checker_calls_per_event_direction
    )
    stats["ladder_deduped"] += dropped
    if dropped:
        log.info(
            "Ladder cap: %d priced -> %d to the Checker (%d duplicate strikes "
            "dropped)", len(priced), len(selected), dropped,
        )

    for proposal in selected:
        candidate = proposal.candidate
        try:
            verdict = checker.check(proposal)
        except Exception as e:
            stats["checker_failed"] += 1
            log.warning("Checker failed on %s: %s", candidate.ticker, e)
            if model_breaker.record_failure(f"{type(e).__name__}: {e}"):
                llm_disabled = True
            continue
        model_breaker.record_success()
        stats["checked"] += 1
        if not verdict.approved:
            stats["checker_rejected"] += 1
        # Deliberately no `continue` on a rejection. The fall-through to
        # risk.evaluate below is what writes edges.checker_verdict, and that
        # column is the only durable record of what the Checker decided —
        # the funnel line cannot distinguish an approval from a rejection
        # once the bankroll check refuses both with the same string. Adding
        # a short-circuit here would look like tidying and would silently
        # destroy the data.
        bucket = "approve" if verdict.approved else "reject"
        if verdict.confidence is not None:
            verdict_confidence[bucket].append(float(verdict.confidence))
        # Recorded for every verdict, not only the ones carrying a
        # confidence, so the family counts total to `checked` rather than to
        # some smaller number whose shortfall has no visible explanation.
        seen = verdict_families.setdefault(family_of(candidate.ticker), [0, 0])
        seen[0 if verdict.approved else 1] += 1

        if account.snapshot is None or account.snapshot.is_stale(
            CONFIG.risk.max_reconciliation_age_seconds * 0.5
        ):
            try:
                account.reconcile()
            except ReconciliationError as e:
                log.error("Could not refresh account state mid-pass — ending pass: %s", e)
                break
            if health is not None:
                health.mark_reconciled()

        if verdict.approved and not scout.refresh_quote(candidate):
            stats["quote_refresh_failed"] += 1
            continue

        try:
            decision = risk.evaluate(verdict, account.snapshot)
        except KillSwitchTripped as e:
            log.error("%s — halting this pass", e)
            break

        edge_id = ledger.log_decision(verdict, decision)
        if not decision.approved:
            stats["risk_refused"] += 1
            continue
        stats["approved"] += 1

        decision.edge_id = edge_id
        try:
            record = execution.execute(verdict, decision)
        except DuplicateOrderBlocked:
            stats["duplicate_blocked"] += 1
            continue
        except ReconciliationError as e:
            log.error("Execution refused for %s: %s — ending pass", candidate.ticker, e)
            break
        except Exception:
            log.exception("Order submission failed for %s — ending pass", candidate.ticker)
            break

        ledger.record_execution(edge_id, record)
        if health is not None:
            health.mark_order()
        if CONFIG.telegram.notify_trades:
            key = signal_key(record.ticker, record.action, record.side,
                             verdict.proposal.source)
            edge = decision.detail.get("net_edge")
            price = decision.executable_price_cents or record.limit_price_cents
            if alert_store is None:
                # No suppression state to consult. Say that, rather than
                # implying a judgement was made.
                should_alert, alert_reason = True, "no suppression state"
            else:
                # evaluate() returns BOTH the decision and why it reached it
                # — "new signal", "edge moved 33.0% -> 41.0%", "price moved",
                # "still standing". Taking only .should_alert threw the
                # explanation away and every alert then read "trade", which
                # is the one thing the operator already knew from the fact
                # that an alert arrived.
                alert_decision = alert_store.evaluate(key, edge, price)
                should_alert = alert_decision.should_alert
                alert_reason = alert_decision.reason
            if should_alert:
                if alert_store is not None:
                    alert_store.record(key, record.ticker, record.action, record.side,
                                       verdict.proposal.source, edge, price)
                _alert(notifier, "notify_trade", record, decision,
                       reason=alert_reason)

        if record.filled_count > 0:
            filled_this_pass += 1
            if health is not None:
                health.mark_fill()

        try:
            account.reconcile()
        except ReconciliationError as e:
            log.error("Post-trade reconciliation failed — ending pass: %s", e)
            break

    # quant_no_proposal and quant_below_edge sit immediately after the
    # attempt count they decompose. Without them a priced candidate that
    # cleared the model and then failed MIN_EDGE_THRESHOLD left no trace at
    # all: on the first production pass where crypto priced, the funnel read
    # 18 attempted and 11 proposed, and the fourteen that had simply not
    # cleared the threshold were invisible — the quant path working
    # correctly, reported as though nothing had happened.
    log.info(
        "Pass funnel: candidates=%d quant=%d quant_no_proposal=%d "
        "quant_below_edge=%d llm_called=%d llm_disabled=%d "
        "proposed=%d ladder_deduped=%d checked=%d checker_rejected=%d "
        "risk_refused=%d approved=%d filled=%d",
        len(candidates), stats["quant_attempted"], stats["quant_no_proposal"],
        stats["quant_below_edge_threshold"], stats["llm_called"],
        stats["llm_disabled"], stats["proposed"], stats["ladder_deduped"],
        stats["checked"], stats["checker_rejected"], stats["risk_refused"],
        stats["approved"], filled_this_pass,
    )
    _log_checker_verdicts(stats, verdict_confidence, verdict_families)

    # Said out loud, because this is the one change in the pass that makes
    # the bot do LESS than it otherwise would. A silent reduction in what
    # gets proposed is indistinguishable from a bug that does the same.
    if arb_scanner is not None and (stats["arb_real_no_ask"]
                                    or stats["arb_derived_no_ask"]):
        total = stats["arb_real_no_ask"] + stats["arb_derived_no_ask"]
        log.info(
            "Arb scan: %d/%d candidates had a real NO ask (%.0f%%); %d priced "
            "off a derived 100-yes_bid, which cannot detect an arb",
            stats["arb_real_no_ask"], total,
            100.0 * stats["arb_real_no_ask"] / total,
            stats["arb_derived_no_ask"],
        )
        # The distribution, not just the count. Zero arbs found says nothing
        # about whether this venue offers the trade at all; the distance to
        # the nearest lock does.
        gap = arb_scanner.gap_summary()
        if gap is not None:
            log.info(
                "Arb gap (cost of a YES+NO pair minus 100c, fees in): "
                "n=%d best=%+.2fc on %s p10=%+.2fc median=%+.2fc | "
                "within 1c=%d 3c=%d 10c=%d",
                gap["n"], gap["best"], gap["best_ticker"],
                gap["p10"], gap["median"],
                gap["within_1c"], gap["within_3c"], gap["within_10c"],
            )
            log.info(
                "Arb gap BEFORE fees: best=%+.2fc on %s (fees would be %.2fc) "
                "median=%+.2fc | %d book(s) through parity — those are locks a "
                "maker could take and a taker never can",
                gap["raw_best"], gap["raw_best_ticker"], gap["raw_best_fees"],
                gap["raw_median"], gap["through_parity"],
            )

    # The quoting probe's own pass counters, then the standing per-zone
    # picture. Both, because they answer different questions: the counters say
    # whether the probe ran, the report says what it has found so far, and a
    # probe that silently stopped recording looks identical to one finding
    # nothing.
    if quote_observer is not None and (stats["quote_quoted"]
                                       or stats["quote_refused"]):
        log.info(
            "Quote probe: %d quoted, %d refused | %d fill check(s), %d mark(s)"
            ", %d left open (market not in this pass)",
            stats["quote_quoted"], stats["quote_refused"],
            stats["quote_filled_checked"], stats["quote_marked"],
            stats["quote_unreadable"],
        )
        quote_observer.report()

    # A path that has been switched off and cannot be seen in the logs is
    # indistinguishable from a bug that switched it off.
    if frozen_seen:
        log.info(
            "Frozen categories (never reach risk): %s | probed this pass: %s "
            "(1 pass in %d) | priced+recorded=%d skipped=%d",
            " ".join(f"{k}x{v}" for k, v in sorted(frozen_seen.items())),
            "yes" if frozen_probe else "no",
            max(1, CONFIG.frozen_category_sample_rate),
            stats["frozen_refused"], stats["frozen_not_probed"],
        )

    sampled = coherence_gate.sampled_families()
    if sampled:
        log.info(
            "Sampling %d family(ies) refused for incoherence every pass "
            "(skipped %d Maker call(s) this pass, re-probing 1 pass in %d): %s",
            len(sampled), stats["llm_skipped_incoherent_family"],
            CONFIG.risk.coherence_family_reprobe_every,
            " ".join(f"{fam}x{n}" for fam, n in
                     sorted(sampled.items(), key=lambda kv: (-kv[1], kv[0]))),
        )

    # Same numbers as the line above, kept as rows so they can be aggregated
    # later instead of grepped out of a log with a retention window. A zero
    # balance reclassifies risk refusals — see stats_to_events — because an
    # unfunded account refusing everything is not a gate exercising judgement.
    if telemetry is not None:
        zero_balance = not getattr(snapshot, "balance_cents", 0)
        events = stats_to_events(stats, zero_balance=zero_balance)
        events[(Stage.SCOUTED.value, None)] = len(candidates)
        events[(Stage.FILLED.value, None)] = filled_this_pass
        if not candidates:
            events[(Stage.SCOUTED.value, Reason.NO_CANDIDATES.value)] = 1
        telemetry.record_stages(pass_id, events)
        telemetry.finish_pass(
            pass_id, candidates=len(candidates), scout_ms=scout_ms,
            pricing_ms=(time.time() - pass_started) * 1000.0,
        )
    return filled_this_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--reflect-every", type=int, default=200)
    args = parser.parse_args()

    if args.dry_run:
        CONFIG.risk.dry_run = True

    notifier = TelegramClient()

    try:
        assert_order_strategy_supported()
    except UnmanagedMakerMode as e:
        _alert(notifier, "notify_systemic_error", "config", str(e))
        notifier.flush()
        raise SystemExit(str(e)) from e

    storage = storage_status()
    if not storage.usable:
        raise SystemExit(f"Cannot open ledger database: {storage.reason}")

    if not storage.durable:
        message = f"Ledger storage is NOT durable: {storage.reason}"
        if CONFIG.kalshi.env == "prod" and not CONFIG.risk.dry_run:
            raise SystemExit(message)
        log.warning("%s Continuing because env=%s dry_run=%s.",
                    message, CONFIG.kalshi.env, CONFIG.risk.dry_run)

    if storage.effective_path != CONFIG.ledger_db_path:
        CONFIG.ledger_db_path = storage.effective_path

    client = KalshiClient()
    store = EdgeStore()
    order_store = OrderStore()
    # Observer only. Its constructor disables itself rather than raising
    # if the database cannot be prepared, so this cannot stop the bot.
    telemetry = TelemetryStore()
    alert_store = SignalAlertStore()
    account = AccountState(client, order_store)

    scout = Scout(client)
    enricher = ContextEnricher(
        espn=ESPNClient(),
        slash_golf=(
            SlashGolfClient(CONFIG.models.slash_golf_api_key)
            if CONFIG.models.slash_golf_api_key else None
        ),
        weather=NOAAClient(),
        fred=FredClient() if CONFIG.models.fred_api_key else None,
    )
    maker = Maker(enricher=enricher)
    rti_runner = RTIFeedRunner() if CONFIG.risk.rti_feed_enabled else None
    spot_client = SpotPriceClient(
        rti_feed=rti_runner.feed if rti_runner else None,
        price_store=PriceStore() if CONFIG.risk.persist_vol_history else None,
    )
    spot_client.restore_history()
    if rti_runner is not None:
        rti_runner.on_quote = spot_client.record_tick
        rti_runner.start()
    quant_maker = QuantMaker(spot_client)
    arb_scanner = ArbitrageScanner(notifier=notifier)
    try:
        quote_observer = QuoteObserver()
    except Exception:
        # Same posture as the telemetry store and the ledger report: an
        # observer that cannot open its table must not be the reason the bot
        # refuses to boot. run_once skips the probe when this is None.
        log.exception("Quote observer unavailable; the probe will not run")
        quote_observer = None
    coherence_gate = CoherenceGate()
    checker = Checker()
    risk = RiskGuardrail(bankroll_usd=args.bankroll, store=store,
                         order_store=order_store, notifier=notifier)
    execution = Execution(client, order_store, account)
    order_lifecycle = OrderLifecycle(client, order_store, account)
    ledger = Ledger(client, store, order_store)
    reflector = Reflector(store)

    log.info("DÆMON-KALSHI starting | env=%s dry_run=%s",
             CONFIG.kalshi.env, CONFIG.risk.dry_run)

    # One-shot, read-only report over the edge ledger, answering questions the
    # review environment cannot reach the volume to ask. Opens the database
    # with mode=ro so SQLite refuses a write regardless of what it issues, and
    # swallows every exception — same posture as TelemetryStore above, and for
    # the same reason: an observer must never be able to stop the bot. Placed
    # before reconciliation deliberately, so a failed startup still produces it.
    emit_ledger_answers()

    try:
        snapshot = account.reconcile()
    except ReconciliationError as e:
        raise SystemExit(f"Startup reconciliation failed: {e}") from e
    if not snapshot.is_tradeable:
        raise SystemExit(f"Account not tradeable: {snapshot.blocking_reason()}")

    health = Health(notifier)
    _alert(
        notifier, "notify_startup",
        env=CONFIG.kalshi.env,
        dry_run=CONFIG.risk.dry_run,
        strategy=CONFIG.risk.order_strategy,
        balance_usd=snapshot.balance_cents / 100,
        positions=snapshot.open_position_count(),
        exposure_usd=snapshot.worst_case_exposure_cents() / 100,
        bankroll_usd=risk.effective_bankroll_usd(snapshot),
    )

    # Operator halt that does not require a redeploy. The persisted flag and
    # the guardrail that reads it already existed; this is the way a human
    # reaches them from a phone.
    def _halt(reason: str) -> None:
        store.set_kill_switch(True, reason)

    def _status() -> str:
        snap = account.snapshot()
        killed = store.load_kill_switch()
        return (
            f"env={CONFIG.kalshi.env} dry_run={CONFIG.risk.dry_run}\n"
            f"balance ${snap.balance_cents / 100:,.2f} | "
            f"{snap.open_position_count()} position(s)\n"
            f"kill switch: {'TRIPPED — ' + (killed['reason'] or 'no reason recorded') if killed['tripped'] else 'clear'}\n"
            f"passes this run: {pass_count}"
        )

    shutdown = install_shutdown_handlers()
    pass_count = 0
    exit_reason = "loop ended"

    # Started after pass_count exists: _status closes over it, and the
    # listener thread can be answering a /status within milliseconds.
    commands = TelegramCommandListener(
        on_halt=_halt,
        status_provider=_status,
        send=notifier.send,
    )
    commands.start()
    try:
        while True:
            try:
                # Before the scan, not after. Resting orders are the
                # largest live risk in the process, and a TTL or a market
                # close does not wait for a pass to finish proposing.
                order_lifecycle.sweep()
                run_once(scout, maker, quant_maker, checker, risk, execution,
                         ledger, account, notifier=notifier, health=health,
                         alert_store=alert_store, arb_scanner=arb_scanner,
                         quote_observer=quote_observer,
                         coherence_gate=coherence_gate,
                         store=store, order_store=order_store,
                         telemetry=telemetry, pass_index=pass_count)
                ledger.reconcile_settlements()
                ledger.reconcile_forecasts()
                spot_client.persist_history()
                pass_count += 1
                if args.reflect_every and pass_count % args.reflect_every == 0:
                    reflector.reflect()
            except KillSwitchTripped as e:
                exit_reason = f"kill switch: {e}"
                raise SystemExit(exit_reason) from e
            except Exception:
                log.exception("Unhandled error in pass — continuing")

            health.check_stalled()
            health.maybe_daily_summary(risk, account, order_store)

            if shutdown["signal"]:
                exit_reason = f"{shutdown['signal']} received"
                break
            if args.once:
                exit_reason = "--once completed"
                break

            deadline = time.time() + CONFIG.scout_poll_seconds
            while time.time() < deadline and not shutdown["signal"]:
                time.sleep(min(1.0, max(deadline - time.time(), 0)))
    finally:
        log.info("Shutting down: %s", exit_reason)
        if rti_runner is not None:
            rti_runner.stop()
        _alert(notifier, "notify_shutdown", reason=exit_reason,
               filled_today=health.fills, realized_pnl=0.0)
        notifier.flush()
        notifier.close(flush=False)


if __name__ == "__main__":
    main()
