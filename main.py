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
from core.slash_golf_client import SlashGolfClient
from core.weather_client import NOAAClient
from core.fred_client import FredClient
from core.rti_runner import RTIFeedRunner
from core.spot_price_client import SpotPriceClient
from core.telegram_client import TelegramClient
from workers.quant_maker import QuantMaker
from workers.arbitrage import ArbitrageScanner
from workers.coherence import CoherenceGate

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


def run_once(scout, maker, quant_maker, checker, risk, execution, ledger, account,
             notifier=None, health=None, alert_store=None, arb_scanner=None,
             coherence_gate=None):
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

    candidates.sort(key=lambda c: (not _is_priority(c), not _is_preferred(c)))

    filled_this_pass = 0
    llm_calls_this_pass = 0
    llm_calls_by_event: dict[str, int] = defaultdict(int)
    stats: dict[str, int] = defaultdict(int)
    coherence_gate = coherence_gate or CoherenceGate()
    coherence_gate.begin_pass()

    if arb_scanner is not None:
        arb_scanner.begin_pass()
        stats["locked_arbs"] += len(arb_scanner.scan(candidates))

    model_breaker = CircuitBreaker(
        name="model-calls", threshold=CONFIG.model_failure_threshold, cooldown_seconds=0
    )
    # No key / broken provider → skip LLM quietly; quant still runs.
    llm_disabled = not getattr(maker, "available", True)

    for candidate in candidates:
        proposal = None
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

            event_key = candidate.event_ticker or candidate.ticker
            per_event_cap = CONFIG.max_llm_calls_per_event
            if (not is_priority and per_event_cap
                    and llm_calls_by_event[event_key] >= per_event_cap):
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

        coherence = coherence_gate.check(proposal)
        if not coherence.ok:
            stats["incoherent"] += 1
            ledger.log_refused_proposal(proposal, action="skipped_incoherent",
                                        reason=coherence.reason)
            continue

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
                should_alert = True
            else:
                should_alert = alert_store.evaluate(key, edge, price).should_alert
            if should_alert:
                if alert_store is not None:
                    alert_store.record(key, record.ticker, record.action, record.side,
                                       verdict.proposal.source, edge, price)
                _alert(notifier, "notify_trade", record, decision, reason="trade")

        if record.filled_count > 0:
            filled_this_pass += 1
            if health is not None:
                health.mark_fill()

        try:
            account.reconcile()
        except ReconciliationError as e:
            log.error("Post-trade reconciliation failed — ending pass: %s", e)
            break

    log.info(
        "Pass funnel: candidates=%d quant=%d llm_called=%d llm_disabled=%d "
        "proposed=%d approved=%d filled=%d",
        len(candidates), stats["quant_attempted"], stats["llm_called"],
        stats["llm_disabled"], stats["proposed"], stats["approved"],
        filled_this_pass,
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
    coherence_gate = CoherenceGate()
    checker = Checker()
    risk = RiskGuardrail(bankroll_usd=args.bankroll, store=store,
                         order_store=order_store, notifier=notifier)
    execution = Execution(client, order_store, account)
    ledger = Ledger(client, store, order_store)
    reflector = Reflector(store)

    log.info("DÆMON-KALSHI starting | env=%s dry_run=%s",
             CONFIG.kalshi.env, CONFIG.risk.dry_run)

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

    shutdown = install_shutdown_handlers()
    pass_count = 0
    exit_reason = "loop ended"
    try:
        while True:
            try:
                run_once(scout, maker, quant_maker, checker, risk, execution,
                         ledger, account, notifier=notifier, health=health,
                         alert_store=alert_store, arb_scanner=arb_scanner,
                         coherence_gate=coherence_gate)
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
