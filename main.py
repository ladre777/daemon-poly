"""
DÆMON-KALSHI orchestrator. One pass = Scout -> Maker -> Checker -> Risk
Guardrail -> Execution -> Ledger, looped on SCOUT_POLL_SECONDS. Run with
--dry-run to force paper mode regardless of the DRY_RUN env var (useful for
a first live test against demo-api.kalshi.co without editing env vars).

Startup order is a safety property, not a convenience: the bot reconciles
with Kalshi before the first scan and refuses to run if that fails. Every
pass re-reconciles before evaluating any candidate, and risk is evaluated
against that reconciled snapshot rather than a process-local counter. If
account state cannot be verified, the pass places no orders.
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
from core.kalshi_client import KalshiClient, diagnose_auth_failure
from memory.db import storage_status
from memory.edge_store import EdgeStore
from memory.order_store import OrderStore, SignalAlertStore, signal_key
from memory.price_store import PriceStore
from workers.scout import Scout
from workers.maker import Maker
from workers.checker import Checker
from workers.risk_guardrail import RiskGuardrail, KillSwitchTripped
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
from core.weather_client import NOAAClient
from core.fred_client import FredClient
from core.rti_runner import RTIFeedRunner
from core.spot_price_client import SpotPriceClient
from core.validation import clamp_text
from core.telegram_client import TelegramClient
from workers.quant_maker import QuantMaker
from workers.arbitrage import ArbitrageScanner
from workers.coherence import CoherenceGate

# stream=stdout, not the logging default of stderr.
#
# Railway classifies everything a container writes to stderr as severity
# "error". With the default handler, every INFO line — every pass funnel,
# every successful reconciliation — arrived tagged as an error, so filtering
# the log stream by severity returned the entire log and a genuine failure
# was indistinguishable from a routine pass. Verified against deployment
# d3a6756b: `"level": "error"` on lines reading
# `INFO daemon_kalshi.account: Reconciled: ...`.
#
# WARNING and above still carry their real level in the message text, and
# real crashes still reach stderr via the interpreter itself.
logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

# httpx logs every request at INFO as "HTTP Request: <METHOD> <full URL>".
# Two reasons that is not acceptable here:
#
# 1. Secrets. Telegram puts the bot token in the URL path, so every alert
#    printed a working bot token into Railway's log stream, where it is
#    retained and visible to anyone with project access.
# 2. Volume. A scan is 400 paginated calls, so each 30-second pass wrote 400
#    lines of cursor noise, burying the handful of lines that say what the
#    bot actually decided.
#
# WARNING keeps genuine transport failures and drops the per-request chatter.
# Set HTTPX_LOG_LEVEL=INFO to get it back while debugging a request — but not
# while a real bot token is configured.
logging.getLogger("httpx").setLevel(CONFIG.httpx_log_level)
logging.getLogger("httpcore").setLevel("WARNING")

log = logging.getLogger("daemon_kalshi.main")

_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def _log_vol_signature(spot_client, symbols) -> None:
    """Print realized vol at each sampling interval, in annualized terms.

    The one number an operator can sanity-check at a glance. A feed that is
    being sampled inside its own smoothing window reads low at the short
    intervals and climbs; a clean one is flat. Production measured 6.6%
    annualized for bitcoin against a market pricing 18.4%, and nothing in the
    logs said so — the estimate was only ever visible as a probability, by
    which point it looked like a disagreement rather than a broken input.
    """
    for symbol in symbols:
        history = spot_client.history.get(symbol)
        if history is None:
            continue
        rungs = []
        for interval, vol in history.vol_signature():
            label = "tick" if not interval else f"{interval:.0f}s"
            rungs.append(f"{label} {vol * _SECONDS_PER_YEAR ** 0.5:.0%}"
                         if vol else f"{label} n/a")
        log.info("Volatility signature %s (annualized): %s",
                 symbol, "  ".join(rungs))
        # The signature above is measured over one lookback. The quant path
        # picks its lookback from each contract's horizon, so the two can
        # disagree — and a diagnostic that does not report the number actually
        # setting prices is the reason a 6.6%-annualized bitcoin survived a
        # whole session. Both ends of that range are printed here, and
        # QuantMaker logs the figure it really used per family per pass.
        used = []
        for lookback in sorted({3600.0, CONFIG.risk.vol_history_retention_seconds,
                                86400.0}):
            vol = history.realized_vol_robust(lookback)
            used.append(f"{lookback:.0f}s -> "
                        + (f"{vol * _SECONDS_PER_YEAR ** 0.5:.0%}" if vol else "n/a"))
        log.info("Volatility used %s by lookback (annualized): %s",
                 symbol, "  ".join(used))


def _alert(notifier, method: str, *args, **kwargs) -> None:
    """Call a notifier method, swallowing anything it throws.

    Alerting is observability, not control flow. A Telegram outage must never
    be able to stop the bot it is reporting on — that would make the alerting
    system the cause of the incident it exists to surface.
    """
    if notifier is None:
        return
    try:
        getattr(notifier, method)(*args, **kwargs)
    except Exception:
        log.exception("Notification %s failed — continuing", method)


class Health:
    """Tracks whether the bot is actually making progress.

    A bot that has quietly stopped trading looks exactly like a bot that is
    finding no edges: both sit there logging passes. The brief's requirement
    is to "alert when no successful scan or reconciliation has occurred
    within a defined interval", and this is the state that makes that
    answerable.
    """

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
        """Alert if either heartbeat has gone quiet. Throttled inside the
        client, so a sustained outage sends one alert per window, not one per
        pass."""
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
        """Send one summary per day at the configured UTC hour. Off by default."""
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
    """Install SIGTERM/SIGINT handlers and return the shutdown flag.

    Railway sends SIGTERM on every redeploy, so without this a deploy looks
    identical to a crash: no shutdown alert, and any in-flight order left for
    the next process to discover during reconciliation.

    The handler only *sets a flag*. It deliberately does not raise, exit, or
    interrupt anything, because the moments a redeploy is most likely to
    arrive — mid-scan, mid-submission, mid-reconciliation — are exactly the
    moments where being interrupted does damage. Tearing down between
    `place_order` returning and the fill being written to the ledger converts
    an orderly redeploy into an unrecorded position. The loop checks the flag
    at its own boundaries and finishes what it started.

    Returned as a mutable dict rather than a closure variable so the loop and
    the tests can both observe it.
    """
    shutdown = {"signal": None}

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        if shutdown["signal"] is None:
            log.warning("%s received — finishing this pass, then shutting down", name)
            shutdown["signal"] = name

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _on_signal)
    return shutdown


def run_once(scout, maker, quant_maker, checker, risk, execution, ledger, account,
             notifier=None, health=None, alert_store=None, arb_scanner=None,
             coherence_gate=None):
    """One scan pass. Returns the number of orders that actually filled.

    Reconciliation happens first: if it fails, the pass places no orders at
    all rather than trading on a stale picture.
    """
    try:
        snapshot = account.reconcile()
        if health is not None:
            health.mark_reconciled()
    except ReconciliationError as e:
        log.error("Reconciliation failed — placing no orders this pass: %s", e)
        _alert(notifier, "notify_systemic_error", "reconciliation", str(e))
        return 0
    if not snapshot.is_tradeable:
        log.error("Not safe to trade this pass: %s", snapshot.blocking_reason())
        _alert(notifier, "notify_systemic_error", "account_state",
               snapshot.blocking_reason() or "account not tradeable")
        return 0

    # One spot fetch per symbol per pass: ten BTC markets must not make ten
    # API calls, nor write ten duplicate observations into the volatility
    # buffer (duplicates drive the vol estimate toward zero, and vol is in
    # the denominator of the quant probability).
    quant_maker.begin_pass()

    candidates = scout.scan()
    if health is not None:
        health.mark_scanned()
    log.info("Scout returned %d candidates", len(candidates))

    def _is_priority(c):
        title = c.title.lower()
        return any(kw in title for kw in CONFIG.priority_keywords)

    def _is_preferred(c):
        return c.category.lower() in CONFIG.priority_categories

    # Three tiers, and the middle one is new.
    #
    # Priority candidates (golf, by default) go first and bypass every model
    # call cap. That was the only tier, and "everything else in whatever order
    # Scout returned it" turned out to mean "politics", because that is what
    # the catalog happens to list first. A production pass spent all ten of
    # its model calls on KXVOTEPRIMARY, KXTRUMPSAY and KXCLARITYVOTE while
    # weather — named as a main focus — got none, on every pass, for hours.
    #
    # Preferred categories now sort ahead of the rest but still obey the caps.
    # That split is deliberate: ordering decides who gets asked first, which is
    # a preference; bypassing the cap decides who gets asked without limit,
    # which is a spending decision and stays reserved for the one family the
    # operator named as always-first.
    candidates.sort(key=lambda c: (not _is_priority(c), not _is_preferred(c)))

    filled_this_pass = 0
    llm_calls_this_pass = 0
    #: Model calls spent per event this pass, so one strike ladder cannot
    #: consume the whole budget. See the per-event cap below.
    llm_calls_by_event: dict[str, int] = defaultdict(int)
    # Per-pass funnel counters. Without these, "Scout returned 2917 candidates"
    # followed by silence is indistinguishable from a crash, a threshold no
    # market cleared, and a category filter that matched nothing — three very
    # different problems that all look identical in the log. Every candidate
    # leaves via exactly one of these buckets.
    stats: dict[str, int] = defaultdict(int)
    # Built here when absent so existing callers keep working; it is per-pass
    # state with no persistence, so constructing one costs nothing.
    coherence_gate = coherence_gate or CoherenceGate()
    coherence_gate.begin_pass()

    # Structural arbs are model-free and cost nothing to look for, so they are
    # checked across the whole scan before any LLM budget is spent. Off unless
    # ARB_ENABLED; detection only — see workers/arbitrage.py.
    if arb_scanner is not None:
        arb_scanner.begin_pass()
        stats["locked_arbs"] += len(arb_scanner.scan(candidates))

    # One failing candidate must not cost us the other 2,913. Model calls are
    # contained per candidate and counted; only a *run* of failures — which
    # means the provider is down, not that one market confused it — stops the
    # pass. Before this, a single 404 from the Maker's provider unwound the
    # entire pass and did so every 30 seconds, silently, forever.
    model_breaker = CircuitBreaker(
        name="model-calls", threshold=CONFIG.model_failure_threshold, cooldown_seconds=0
    )
    for candidate in candidates:
        # Route: quant path for markets with a live spot feed + numeric
        # strike (fast, no LLM); LLM path only for categories where Maker
        # actually has something to reason from. Everything else gets
        # skipped outright — better to pass on a market than have Maker
        # guess with no real information behind it (e.g. GPU rental pricing,
        # gas prices with no live feed wired). Trading a market you have no
        # real edge in isn't a smaller edge, it's fee-paying speculation.
        proposal = None
        if quant_maker.can_handle(candidate):
            stats["quant_attempted"] += 1
            quant_result = quant_maker.propose(candidate)
            if quant_result:
                # Two distinct outcomes, and conflating them cost visibility
                # on the first pass where crypto was priced at all: the model
                # priced the market fine and the edge simply did not clear
                # MIN_EDGE_THRESHOLD after fees. That is the quant path
                # working, but it was landing in the same silence as a
                # failure — 18 attempted, 0 reported as no-proposal, and only
                # 4 reaching the Checker with nothing accounting for the
                # other 14.
                proposal = quant_result.to_maker_proposal()
                if proposal is None:
                    stats["quant_below_edge_threshold"] += 1
            else:
                stats["quant_no_proposal"] += 1
        elif candidate.category.lower() in CONFIG.llm_reasoning_categories:
            is_priority = _is_priority(candidate)
            # The budget tightens while the Maker is on its fallback provider,
            # which is dearer per call than the one the default cap was sized
            # for. Priority markets bypass both caps, as before.
            call_cap = (
                CONFIG.max_fallback_llm_calls_per_pass
                if getattr(maker, "on_fallback", False)
                else CONFIG.max_llm_calls_per_pass
            )
            # An event that has already contradicted itself gets no further
            # model calls. Production burned all ten calls in a pass on one
            # oil contract's strike ladder and the coherence gate refused
            # every one of them — so weather, crypto and golf saw no budget at
            # all while the bot re-asked a question it had already proved the
            # model could not answer.
            if coherence_gate.is_tainted(candidate):
                stats["llm_skipped_tainted"] += 1
                continue

            # Spread the budget across events rather than down one ladder.
            # Ten strikes of the same contract are ten calls answering nearly
            # the same question; a handful of different events is worth far
            # more per call, and the whole budget landing on one of them is
            # how the priority markets got starved.
            event_key = candidate.event_ticker or candidate.ticker
            per_event_cap = CONFIG.max_llm_calls_per_event
            if (not is_priority and per_event_cap
                    and llm_calls_by_event[event_key] >= per_event_cap):
                stats["llm_capped_per_event"] += 1
                log.debug("Event %s already used its %d call(s) this pass — "
                          "skipping %s", event_key, per_event_cap, candidate.ticker)
                continue

            if not is_priority and call_cap and llm_calls_this_pass >= call_cap:
                stats["llm_capped"] += 1
                log.debug("LLM call cap (%d) reached this pass — skipping non-priority %s",
                          call_cap, candidate.ticker)
                continue
            try:
                proposal = maker.propose(candidate)
            except Exception as e:                  # noqa: BLE001 - contained per candidate
                severity = classify(e)
                stats["maker_failed"] += 1
                log.warning("Maker failed on %s (%s): %s", candidate.ticker, severity.value, e)
                if model_breaker.record_failure(f"{type(e).__name__}: {e}"):
                    _alert(notifier, "notify_systemic_error", "maker",
                           f"{model_breaker.consecutive_failures} consecutive Maker "
                           f"failures — ending pass. Last error: {e}")
                    log.error("Maker has failed %d times in a row — ending pass",
                              model_breaker.consecutive_failures)
                    break
                continue
            model_breaker.record_success()
            llm_calls_this_pass += 1
            llm_calls_by_event[event_key] += 1
            stats["llm_called"] += 1
            if proposal is None:
                stats["llm_below_edge_threshold"] += 1
        else:
            log.debug(
                "Skipping %s [%s] — no quant path and category isn't in "
                "LLM_REASONING_CATEGORIES (no grounding data source)",
                candidate.ticker, candidate.category,
            )
            stats["no_grounding_source"] += 1
            continue

        if not proposal:
            continue
        stats["proposed"] += 1
        log.info(
            "Maker edge: %s -> %.2f%% (market %.2f%%, edge %.2f%%)",
            candidate.ticker, proposal.maker_probability * 100,
            candidate.implied_yes_probability * 100, proposal.edge_size * 100,
        )

        # Before spending a Checker call on it. Production produced model
        # output that was arithmetically impossible — P(WTI>84.99)=32%
        # alongside P(WTI>86.49)=45% on the same contract — and the Checker's
        # per-trade judgement was the only thing catching it. These gates can
        # only refuse; see workers/coherence.py.
        coherence = coherence_gate.check(proposal)
        if not coherence.ok:
            stats["incoherent"] += 1
            ledger.log_refused_proposal(proposal, action="skipped_incoherent",
                                        reason=coherence.reason)
            continue

        # Same containment for the Checker. A Checker that cannot answer means
        # this proposal is unreviewed, and an unreviewed proposal is never
        # traded — skipping is the fail-closed outcome, not a lost opportunity.
        try:
            verdict = checker.check(proposal)
        except Exception as e:                      # noqa: BLE001 - contained per candidate
            severity = classify(e)
            stats["checker_failed"] += 1
            log.warning("Checker failed on %s (%s): %s", candidate.ticker, severity.value, e)
            if model_breaker.record_failure(f"{type(e).__name__}: {e}"):
                _alert(notifier, "notify_systemic_error", "checker",
                       f"{model_breaker.consecutive_failures} consecutive Checker "
                       f"failures — ending pass. Last error: {e}")
                log.error("Checker has failed %d times in a row — ending pass",
                          model_breaker.consecutive_failures)
                break
            continue
        model_breaker.record_success()
        stats["checked"] += 1
        if not verdict.approved:
            stats["checker_rejected"] += 1
        # The reasoning is logged, not just the verdict.
        #
        # This gate has rejected 100% of everything it has ever seen — 24 for
        # 24 on a single pass, across weather and crypto, from both the LLM and
        # the quant path. That is either a correct gate in front of bad
        # proposals or a gate biased toward reject, and until now the logs
        # could not tell those apart: only "reject (conf 0.65)" was recorded.
        #
        # It is the same blindness that let a 6.6%-annualized bitcoin survive a
        # whole session. A refusal whose reason is invisible cannot be judged,
        # and the temptation when a gate blocks everything is to loosen it,
        # which is exactly the wrong move if the gate happens to be right.
        if verdict.approved:
            log.info("Checker verdict on %s: %s (conf %.2f)",
                     candidate.ticker, verdict.verdict, verdict.confidence)
        else:
            log.info("Checker verdict on %s: %s (conf %.2f) — %s",
                     candidate.ticker, verdict.verdict, verdict.confidence,
                     clamp_text(verdict.reasoning, 400))

        # Risk runs against the snapshot as it stands right now, including
        # every order already placed earlier in this same pass — that is why
        # the snapshot is refreshed after each fill rather than reused.
        #
        # It is also refreshed when it simply got old. A pass over 2,700
        # candidates takes minutes: a 400-page scan, then a model call per
        # candidate. The snapshot taken at the top of the pass was 109
        # seconds old by the time the first proposal reached risk, past the
        # 90-second freshness limit, so risk refused it — and would have
        # refused every proposal in every pass, forever, for a reason that
        # reads like a transient hiccup.
        #
        # Refusing to trade on stale state is right. Letting the state go
        # stale and calling that a risk decision is not: the fix is to go
        # get a current picture, and to fail closed only if that fails too.
        if account.snapshot is None or account.snapshot.is_stale(
            CONFIG.risk.max_reconciliation_age_seconds * 0.5
        ):
            try:
                account.reconcile()
            except ReconciliationError as e:
                log.error("Could not refresh account state mid-pass — ending pass: %s", e)
                _alert(notifier, "notify_systemic_error", "reconciliation", str(e))
                break
            if health is not None:
                health.mark_reconciled()

        # Re-read the book immediately before risk sees it. The quote on the
        # candidate was captured during the scan, and everything between —
        # ~53s of paginated scanning, then Maker and Checker per candidate —
        # aged it past MAX_QUOTE_AGE_SECONDS. In production that refused 149
        # of 150 Checker-approved candidates, none of them by less than the
        # limit. One extra call per approved candidate, and only for
        # candidates that have already cleared the Checker, so the cost is a
        # handful of requests per pass rather than one per market scanned.
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
            log.info("Risk refused %s: %s", candidate.ticker, decision.reason)
            continue
        stats["approved"] += 1

        decision.edge_id = edge_id
        try:
            record = execution.execute(verdict, decision)
        except DuplicateOrderBlocked as e:
            # The loop re-derives the same candidate for as long as the signal
            # persists; this guard is what stops that from stacking orders,
            # and blocking here is correct.
            #
            # What was wrong is that it `continue`d past the notification
            # below in silence. The ledger recorded "approved: 91 contracts
            # @ 53c" every few minutes while the operator's phone stayed
            # quiet, so an approval that was deliberately not acted on looked
            # exactly like alerting being broken. Every approved decision now
            # produces an operator signal, even when the signal is "already
            # holding this one". Throttled by intent, so a signal that
            # persists for hours costs one message, not one per pass.
            stats["duplicate_blocked"] += 1
            log.info("Skipping duplicate order for %s: %s", candidate.ticker, e)
            _alert(notifier, "notify_duplicate_blocked", candidate.ticker, str(e))
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
            # A standing signal is not news every time it is re-derived. The
            # order-level dedupe window lets an unchanged signal be submitted
            # again once an hour, which is deliberate — but alerting had
            # inherited that clock by accident, so one standing paper trade
            # announced itself four times in a day as though each were new.
            #
            # Suppression here is never permanent: a signal speaks again when
            # it is new, when it moves, or when the reminder interval
            # elapses. See SignalAlertStore.evaluate.
            key = signal_key(record.ticker, record.action, record.side,
                             verdict.proposal.source)
            # The same net edge the alert itself reports, so "moved 6pp"
            # always refers to the number the operator was shown last time.
            edge = decision.detail.get("net_edge")
            price = decision.executable_price_cents or record.limit_price_cents
            if alert_store is None:
                # No suppression memory was supplied. Speak — the pre-existing
                # behaviour — rather than opening a database at a hard-coded
                # production path on the caller's behalf. Silence must always
                # be a decision taken against remembered state, never a side
                # effect of having none; and a scan pass should not be the
                # thing that decides where state lives. main() owns that.
                should_alert, reason = True, "no suppression state"
            else:
                verdict_alert = alert_store.evaluate(key, edge, price)
                should_alert, reason = verdict_alert.should_alert, verdict_alert.reason
            if should_alert:
                if alert_store is not None:
                    alert_store.record(key, record.ticker, record.action, record.side,
                                       verdict.proposal.source, edge, price)
                _alert(notifier, "notify_trade", record, decision, reason=reason)
            else:
                stats["alert_suppressed"] += 1
                log.info("Not re-alerting %s: %s", record.ticker, reason)
        if record.filled_count > 0:
            filled_this_pass += 1
            if health is not None:
                health.mark_fill()

        # Re-reconcile so the next candidate is evaluated against exposure
        # that includes what just filled. Without this, N candidates in one
        # pass could each be approved against the same pre-trade exposure.
        try:
            account.reconcile()
        except ReconciliationError as e:
            log.error("Post-trade reconciliation failed — ending pass: %s", e)
            break

    # The funnel, on one line. Reading left to right tells you where every
    # candidate went and therefore which stage to look at when nothing trades.
    log.info(
        "Pass funnel: %d candidate(s) -> quant %d (no proposal %d, below edge "
        "%d), llm %d "
        "(below edge %d, failed %d, capped %d, per-event %d, tainted %d), "
        "no grounding source %d | "
        "proposed %d -> checked %d (rejected %d, failed %d) -> approved %d "
        "-> filled %d",
        len(candidates),
        stats["quant_attempted"], stats["quant_no_proposal"],
        stats["quant_below_edge_threshold"],
        stats["llm_called"], stats["llm_below_edge_threshold"],
        stats["maker_failed"], stats["llm_capped"],
        stats["llm_capped_per_event"], stats["llm_skipped_tainted"],
        stats["no_grounding_source"],
        stats["proposed"], stats["checked"], stats["checker_rejected"],
        stats["checker_failed"], stats["approved"], filled_this_pass,
    )
    return filled_this_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="force paper mode for this run")
    parser.add_argument("--once", action="store_true", help="run a single pass instead of looping")
    parser.add_argument("--bankroll", type=float, default=1000.0,
                        help="operator ceiling in USD; effective bankroll is the lesser "
                             "of this and the real exchange balance")
    parser.add_argument("--reflect-every", type=int, default=200, help="run reflection every N passes (0 disables)")
    args = parser.parse_args()

    if args.dry_run:
        CONFIG.risk.dry_run = True

    # Built first so configuration and connection failures below can be
    # alerted rather than dying silently into Railway's log stream.
    notifier = TelegramClient()
    if notifier.enabled:
        log.info("Telegram alerting enabled")
    else:
        log.info(
            "Telegram alerting disabled (set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID to enable)"
        )

    # Refuse maker mode before anything connects. Execution re-checks this at
    # the point of submission, since config is mutable at runtime.
    try:
        assert_order_strategy_supported()
    except UnmanagedMakerMode as e:
        _alert(notifier, "notify_systemic_error", "config", str(e))
        notifier.flush()
        raise SystemExit(str(e)) from e

    # Storage check before anything opens the database. Every P0 durability
    # guarantee — rebuilding exposure after a restart, not double-counting
    # settled PnL, a kill switch that cannot un-trip itself — depends on this
    # file outliving the process.
    storage = storage_status()
    if not storage.usable:
        _alert(notifier, "notify_systemic_error", "storage",
               f"No writable database location: {storage.reason}")
        notifier.flush()
        raise SystemExit(f"Cannot open a ledger database: {storage.reason}")

    if not storage.durable:
        message = (
            f"Ledger storage is NOT durable: {storage.reason}. Order, fill "
            f"and settlement history — and the kill-switch state — will be "
            f"lost on the next redeploy."
        )
        if CONFIG.kalshi.env == "prod" and not CONFIG.risk.dry_run:
            # Refusing here is the point. Live trading against amnesiac
            # storage means that after any redeploy the bot cannot reconstruct
            # what it holds, and a tripped kill switch silently clears itself.
            _alert(notifier, "notify_systemic_error", "storage", message)
            notifier.flush()
            raise SystemExit(
                f"{message}\n\n"
                f"Refusing to trade real money without durable storage.\n"
                f"Fix: attach a Railway Volume mounted at /data to this "
                f"service, then redeploy. See docs/SAFETY.md."
            )
        # Demo or paper mode: warn loudly but keep running, because losing
        # paper history is an inconvenience rather than a risk.
        log.warning("%s Continuing because env=%s dry_run=%s.",
                    message, CONFIG.kalshi.env, CONFIG.risk.dry_run)

    if storage.effective_path != CONFIG.ledger_db_path:
        log.warning("Falling back to %s for the ledger database",
                    storage.effective_path)
        CONFIG.ledger_db_path = storage.effective_path

    try:
        client = KalshiClient()
    except Exception as e:
        # Almost always a credentials problem: a missing or unarmoured
        # KALSHI_PRIVATE_KEY_PEM fails here, before any request is made.
        _alert(notifier, "notify_systemic_error", "auth",
               f"Could not build the Kalshi client: {e}")
        notifier.flush()
        raise
    store = EdgeStore()
    order_store = OrderStore()
    alert_store = SignalAlertStore()
    account = AccountState(client, order_store)

    scout = Scout(client)
    enricher = ContextEnricher(
        espn=ESPNClient(),
        weather=NOAAClient(),
        fred=FredClient() if CONFIG.models.fred_api_key else None,
    )
    maker = Maker(enricher=enricher)
    # The index feed that actually settles the crypto families. Started before
    # the first pass so it has a chance to receive a frame; the quant path
    # declines cleanly in the meantime rather than falling back to spot.
    rti_runner = RTIFeedRunner() if CONFIG.risk.rti_feed_enabled else None
    spot_client = SpotPriceClient(
        rti_feed=rti_runner.feed if rti_runner else None,
        price_store=PriceStore() if CONFIG.risk.persist_vol_history else None,
    )
    # Reload what a previous process observed. The quant path needs
    # MIN_VOL_SPAN_SECONDS of history before it prices anything, and that
    # buffer used to be in memory only — so every redeploy reset the clock and
    # the 15-minute and hourly crypto families never warmed up at all. The
    # requirement is unchanged; the observations just survive a restart now.
    restored = spot_client.restore_history()
    if restored:
        log.info("Restored volatility history: %s",
                 ", ".join(f"{sym} {n} point(s)" for sym, n in sorted(restored.items())))
        _log_vol_signature(spot_client, sorted(restored))
    else:
        log.info("No stored volatility history — the quant path starts cold "
                 "and needs ~%.0fs of feed before it can price crypto.",
                 CONFIG.risk.min_vol_span_seconds)
    if rti_runner is not None:
        # Every frame also feeds the volatility history. Without this the
        # estimate is built from one sample per scan pass, which needs the
        # better part of an hour of uptime to reach the 600-second span it
        # requires — while the feed is delivering two observations a second
        # of the same instrument. Wired before start() so no frame is lost.
        rti_runner.on_quote = spot_client.record_tick
        rti_runner.start()
    else:
        log.warning("RTI_FEED_ENABLED is off — crypto families will not be "
                    "priced. This suppresses trades; it does not risk any.")
    quant_maker = QuantMaker(spot_client)
    # Model-free structural-arb detection. Constructed unconditionally;
    # the scanner itself is a no-op unless ARB_ENABLED.
    arb_scanner = ArbitrageScanner(notifier=notifier)
    coherence_gate = CoherenceGate()
    checker = Checker()
    risk = RiskGuardrail(bankroll_usd=args.bankroll, store=store,
                         order_store=order_store, notifier=notifier)
    execution = Execution(client, order_store, account)
    ledger = Ledger(client, store, order_store)
    reflector = Reflector(store)

    log.info(
        "DÆMON-KALSHI starting | env=%s dry_run=%s strategy=%s categories=%s",
        CONFIG.kalshi.env, CONFIG.risk.dry_run, CONFIG.risk.order_strategy,
        CONFIG.scout_categories,
    )

    # Startup reconciliation: refuse to start rather than trade against an
    # unknown account. This is also what rebuilds exposure from positions and
    # orders opened by a previous process — the state a restart used to lose.
    try:
        snapshot = account.reconcile()
    except ReconciliationError as e:
        # The commonest cause of this is not an outage but a credential that
        # belongs to the other environment, and the raw 401 says
        # "NOT_FOUND", which reads like a routing bug. Name it instead.
        hint = diagnose_auth_failure(e)
        _alert(notifier, "notify_systemic_error", "reconciliation",
               f"Startup reconciliation failed, refusing to start: {e}{hint}")
        notifier.flush()
        if hint:
            log.error("%s", hint.strip())
        # Refusing to start is right. Refusing to start *instantly* is not:
        # the supervisor restarts the container immediately, so the process
        # re-runs this same failing call every couple of seconds forever. If
        # the cause is the exchange rate-limiting or throttling the key, that
        # restart loop is actively making the problem worse — several hundred
        # failed auth attempts per hour against a key already in trouble.
        #
        # Holding before exit converts the loop into a slow retry, which is
        # what a transient outage needs and what a genuinely revoked key
        # costs nothing.
        hold = CONFIG.startup_failure_hold_seconds
        if hold > 0:
            log.error("Startup reconciliation failed; holding %ds before exit so "
                      "the restart loop does not hammer the exchange", hold)
            time.sleep(hold)
        raise SystemExit(
            f"Startup reconciliation with Kalshi failed: {e}{hint}\n"
            f"Refusing to start — the bot cannot know its own exposure."
        ) from e
    if not snapshot.is_tradeable:
        _alert(notifier, "notify_systemic_error", "account_state",
               f"Startup blocked: {snapshot.blocking_reason()}")
        notifier.flush()
        raise SystemExit(
            f"Startup reconciliation succeeded but the account is not safe to "
            f"trade: {snapshot.blocking_reason()}\n"
            f"Resolve this before starting (see docs/SAFETY.md)."
        )
    log.info(
        "Startup state: $%.2f balance, %d open position(s), %d live order(s), "
        "$%.2f worst-case exposure",
        snapshot.balance_cents / 100, len(snapshot.positions),
        len(snapshot.open_orders), snapshot.worst_case_exposure_cents() / 100,
    )
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

    shutdown = install_shutdown_handlers()

    pass_count = 0
    exit_reason = "loop ended"
    #: Fires once, after the grace period, if the index feed never came up.
    #: A dead feed is safe — nothing gets priced — but it is invisible from
    #: the funnel, which just shows crypto producing no proposals, so it has
    #: to announce itself.
    rti_warned = rti_runner is None
    try:
        while True:
            try:
                run_once(scout, maker, quant_maker, checker, risk, execution,
                         ledger, account, notifier=notifier, health=health,
                         alert_store=alert_store, arb_scanner=arb_scanner,
                         coherence_gate=coherence_gate)
                ledger.reconcile_settlements()
                # Grade predictions that never became positions. In dry-run
                # that is every prediction the bot makes, so without this the
                # whole paper period produces no calibration data at all.
                ledger.reconcile_forecasts()
                # Once a pass is cheap and bounds how much history a crash
                # can cost to one pass's worth of ticks.
                spot_client.persist_history()
                pass_count += 1
                if args.reflect_every and pass_count % args.reflect_every == 0:
                    reflector.reflect()
            except KillSwitchTripped as e:
                # Persisted and requires a human to clear, so retrying the
                # loop would just spin. Exit loudly instead. The alert itself
                # already fired from inside RiskGuardrail.
                exit_reason = f"kill switch tripped: {e}"
                raise SystemExit(exit_reason) from e
            except Exception:
                log.exception("Unhandled error in main loop pass — continuing")

            health.check_stalled()
            health.maybe_daily_summary(risk, account, order_store)

            if not rti_warned:
                if rti_runner.is_ready:
                    log.info("%s", rti_runner.status())
                    rti_warned = True
                elif (time.time() - (rti_runner.started_at or 0)
                        > CONFIG.risk.rti_startup_grace_seconds):
                    log.error("%s", rti_runner.status())
                    _alert(notifier, "notify_systemic_error", "rti_feed",
                           rti_runner.status())
                    rti_warned = True

            if shutdown["signal"]:
                exit_reason = f"{shutdown['signal']} received (likely a redeploy)"
                break
            if args.once:
                exit_reason = "--once completed"
                break

            # Sleep in short slices so a SIGTERM during the idle window is
            # acted on promptly instead of after a full poll interval.
            deadline = time.time() + CONFIG.scout_poll_seconds
            while time.time() < deadline and not shutdown["signal"]:
                time.sleep(min(1.0, max(deadline - time.time(), 0)))
    finally:
        log.info("Shutting down: %s", exit_reason)
        if rti_runner is not None:
            rti_runner.stop()
        _alert(notifier, "notify_shutdown", reason=exit_reason,
               filled_today=health.fills,
               realized_pnl=_safe_realized_pnl(risk))
        # Bounded: a shutdown that hangs waiting on Telegram is its own
        # failure, and the container is going away regardless.
        notifier.flush()
        notifier.close(flush=False)


def _safe_realized_pnl(risk) -> float:
    try:
        return risk.realized_pnl_today()
    except Exception:
        log.exception("Could not read realized PnL for the shutdown alert")
        return 0.0


if __name__ == "__main__":
    main()
