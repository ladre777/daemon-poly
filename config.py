"""
Central config for DÆMON-KALSHI. Everything comes from the environment so the
same code runs locally (.env) and on Railway (dashboard-set vars) with no
edits. Never hardcode secrets here.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine on Railway, where real env vars are already injected


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _floats(name: str, default: tuple) -> tuple:
    """Comma-separated floats.

    A malformed value falls back to the default wholesale rather than
    silently dropping the bad entries — a vol sampling ladder with one rung
    quietly missing is worse than one that was never changed.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        parsed = tuple(float(p) for p in raw.split(",") if p.strip())
    except (TypeError, ValueError):
        return default
    return parsed or default


#: Explicitly cheap model the Maker's Anthropic fallback uses when nothing
#: else is configured. Named here rather than inlined so config.py and
#: core/llm_client.py resolve the same value, and so "what does the
#: high-volume path cost when it degrades" has one answer.
DEFAULT_MAKER_FALLBACK_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class KalshiConfig:
    env: str = os.getenv("KALSHI_ENV", "demo")  # "demo" or "prod"
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    # Prefer a mounted file locally; on Railway store the PEM contents
    # directly in an env var since there's no persistent filesystem to mount.
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    private_key_pem: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
    # Minimum spacing between outbound Kalshi requests, in seconds. 0.15s is
    # ~6.7 requests/second. A full Scout pass is 400 paginated calls, so
    # without a floor here the bot ran at roughly 8/s sustained and re-ran
    # the whole scan on every container restart. Reacting to 429s afterwards
    # does not undo a rate that was too high to begin with. Set to 0 to
    # disable (tests do).
    min_request_interval_seconds: float = _float("KALSHI_MIN_REQUEST_INTERVAL", 0.15)

    @property
    def rest_base(self) -> str:
        return (
            "https://api.elections.kalshi.com/trade-api/v2"
            if self.env == "prod"
            else "https://demo-api.kalshi.co/trade-api/v2"
        )

    @property
    def ws_base(self) -> str:
        return (
            "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
            if self.env == "prod"
            else "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
        )

    def load_private_key_bytes(self) -> bytes:
        if self.private_key_pem:
            return self.private_key_pem.encode("utf-8")
        if self.private_key_path:
            return Path(self.private_key_path).read_bytes()
        raise RuntimeError(
            "No Kalshi private key configured. Set KALSHI_PRIVATE_KEY_PEM "
            "(recommended for Railway) or KALSHI_PRIVATE_KEY_PATH (local)."
        )


@dataclass
class ModelConfig:
    # Maker: fast, cheap, high-volume signal proposer — Kimi/Moonshot, same
    # role it plays in DÆMON-POLY.
    moonshot_api_key: str = os.getenv("MOONSHOT_API_KEY", "")
    moonshot_base_url: str = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
    moonshot_model: str = os.getenv("MOONSHOT_MODEL", "kimi-k2-turbo-preview")

    # Checker: slower, higher-trust second opinion before capital moves.
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    checker_model: str = os.getenv("CHECKER_MODEL", "claude-sonnet-5")
    # Token budget for a Checker verdict — thinking AND answer together.
    #
    # This was 500, then 1500, and production truncated verdicts mid-JSON at
    # both. Raising the number was treating the symptom: claude-sonnet-5 runs
    # adaptive thinking whenever `thinking` is omitted, thinking is billed
    # against max_tokens alongside the response, and deliberation simply
    # expanded to fill each larger budget. The fix is this value AND
    # checker_effort below, which bounds the thinking half.
    checker_max_tokens: int = _int("CHECKER_MAX_TOKENS", 4000)
    # How much of that budget the Checker may spend thinking. "low" suits a
    # small, well-scoped judgement with a fixed output shape; raise it only
    # if verdict quality measurably improves, and raise max_tokens with it.
    checker_effort: str = os.getenv("CHECKER_EFFORT", "low")

    # Which backend answers Maker calls. "auto" (default) uses Moonshot and
    # falls back to Anthropic when Moonshot is unreachable, unauthorised, or
    # has no usable model for the key — see core/llm_client.py. "moonshot" or
    # "anthropic" pin a single provider with no failover.
    maker_provider: str = os.getenv("MAKER_LLM_PROVIDER", "auto")
    # Per-call timeout for the Maker's primary provider. Short on purpose:
    # this budget is paid once per candidate, and in production a hung
    # Moonshot at 30s aged the account snapshot past its freshness limit
    # before the first proposal ever reached risk. Failing over quickly is
    # worth more here than waiting out a slow response.
    # Lowered from 15s on 2026-08-17, calibrated against observed latency
    # rather than guessed. In production a SUCCESSFUL Moonshot call returns in
    # roughly 2 seconds (05:16:32 scan end -> 05:16:34 first proposal); the
    # 15s budget was only ever spent by calls that were going to time out
    # anyway and then be answered by the fallback.
    #
    # That wasted time is not free: it is the direct cause of quotes ageing
    # past MAX_QUOTE_AGE_SECONDS before risk evaluates them. 8s leaves ~4x
    # headroom over the observed success latency while cutting the worst case
    # nearly in half.
    maker_timeout_seconds: float = _float("MAKER_TIMEOUT_SECONDS", 8.0)
    # Model used when the Maker falls back to Anthropic. NOT the Checker's
    # model: the Maker is the high-volume path (tens of calls per 30-second
    # pass) and the Checker is the low-volume one (only on proposals that
    # already cleared an edge threshold). Running the Maker's volume through
    # the Checker's model is how a fallback meant to keep the bot alive turns
    # into a bill larger than the trading account.
    #
    # The constant is shared with core/llm_client.py so the two cannot drift.
    # That mattered: llm_client used to resolve an empty value with
    # `... or models_config.checker_model`, so setting MAKER_FALLBACK_MODEL=""
    # silently routed every Maker call onto whatever the Checker was running —
    # exactly the bill this comment warns about, with the warning sitting one
    # file away from the code that ignored it.
    maker_fallback_model: str = os.getenv(
        "MAKER_FALLBACK_MODEL", DEFAULT_MAKER_FALLBACK_MODEL
    )

    # Grounding data sources (not LLMs, but live here alongside the other
    # external-service keys for a single place to look).
    fred_api_key: str = os.getenv("FRED_API_KEY", "")


@dataclass
class RiskConfig:
    # Per-trade and account-level guardrails. Tune these to your real
    # PF-04/PF-09/PF-10 numbers — these are conservative placeholders.
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.05)       # 5% of bankroll per position
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.10)    # kill switch trigger

    # -- model coherence gates (see workers/coherence.py) -------------------
    # Both gates can only REFUSE a proposal; neither can approve one. On by
    # default because production produced arithmetically impossible model
    # output — P(WTI>84.99)=32% alongside P(WTI>86.49)=45% — and the only
    # thing that caught it was the Checker's judgement on each trade.
    coherence_checks_enabled: bool = _bool("COHERENCE_CHECKS_ENABLED", True)
    # Slack allowed before two strikes on the same event count as
    # contradictory. Small: this is a model's own output, not an order book,
    # so it has no tick-rounding excuse. 0.01 = one percentage point.
    coherence_tolerance: float = _float("COHERENCE_TOLERANCE", 0.01)
    # Maximum distance, in log-odds, between the model and the market before
    # the disagreement is treated as model error.
    #
    # Log-odds rather than percentage points on purpose. A flat 30pp cap would
    # also reject the weather thesis — model 15% against a market at 50% is a
    # real disagreement, and only 1.73 apart in log-odds. Claiming 45% against
    # a market at 1.5% is 3.99 apart: not disagreeing with the market, but
    # asserting it is wrong by a factor of fifty. 3.0 separates the two.
    # Set 0 to disable this gate alone.
    max_log_odds_disagreement: float = _float("MAX_LOG_ODDS_DISAGREEMENT", 3.0)
    # Whether unrealized *gains* may offset realized losses in the daily-loss
    # control. Unrealized losses always count — that is the point of marking
    # to market. Gains are excluded by default, because letting paper profit
    # extend the day's loss budget is how a bot that is genuinely down keeps
    # trading: a mark on a thin prediction-market book can evaporate between
    # one pass and the next, and the realized loss it was offsetting cannot.
    # Set true for symmetric mark-to-market accounting.
    count_unrealized_gains: bool = _bool("COUNT_UNREALIZED_GAINS", False)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 15)
    min_edge_threshold: float = _float("MIN_EDGE_THRESHOLD", 0.04)    # 4pp min edge to act
    min_liquidity_usd: float = _float("MIN_LIQUIDITY_USD", 500.0)
    checker_min_confidence: float = _float("CHECKER_MIN_CONFIDENCE", 0.65)
    dry_run: bool = _bool("DRY_RUN", True)
    # Longshot bias guard (see risk_guardrail.py) — thresholds from Becker's
    # analysis showing contracts under ~20c systematically underperform.
    longshot_price_threshold_cents: float = _float("LONGSHOT_PRICE_THRESHOLD_CENTS", 20)
    longshot_edge_multiplier: float = _float("LONGSHOT_EDGE_MULTIPLIER", 1.5)
    # "taker" = IOC at current ask, fills now, pays taker fee (what this repo
    # did by default before). "maker" = GTC at current bid, doesn't cross the
    # spread, targets the documented maker-side edge — but can go unfilled;
    # see execution.py's module docstring before flipping this in production.
    # NOTE: maker mode is refused at startup until order-lifecycle management
    # exists — see main.py and workers/execution.py.
    order_strategy: str = os.getenv("ORDER_STRATEGY", "taker")

    # -- exposure caps (worst-case dollars, not position counts) -----------
    # A count of open positions says nothing about how much money is at risk:
    # 15 positions at $2 and 15 at $200 are the same number. These are the
    # limits risk actually enforces, all as a fraction of the effective
    # bankroll (the lesser of the CLI --bankroll and the real exchange
    # balance).
    max_total_exposure_pct: float = _float("MAX_TOTAL_EXPOSURE_PCT", 0.50)
    max_ticker_exposure_pct: float = _float("MAX_TICKER_EXPOSURE_PCT", 0.05)
    # Markets inside one event are usually mutually exclusive outcomes of the
    # same question, so several positions there are one correlated bet.
    max_event_exposure_pct: float = _float("MAX_EVENT_EXPOSURE_PCT", 0.10)
    max_category_exposure_pct: float = _float("MAX_CATEGORY_EXPOSURE_PCT", 0.25)
    # Kalshi's taker fee is roughly 0.07 * price * (1 - price) per contract,
    # peaking near 50c. Charged conservatively into every sizing and edge
    # calculation rather than discovered after the fact.
    fee_rate: float = _float("FEE_RATE", 0.07)
    # Added to the executable price when budgeting worst-case cost, so a
    # quote that moves between decision and fill doesn't breach a limit.
    slippage_cents: float = _float("SLIPPAGE_CENTS", 1.0)

    # -- reconciliation / lifecycle ---------------------------------------
    # Older than this and the account picture is not trusted for trading.
    # Two scan passes' worth of slack at the default 30s poll.
    max_reconciliation_age_seconds: float = _float("MAX_RECONCILIATION_AGE_SECONDS", 90.0)
    # Two intents for the same ticker/side/price/size inside one window
    # collapse to a single order, which is what stops the 30s scan loop from
    # stacking duplicates on a signal that persists across passes.
    dedupe_window_seconds: float = _float("DEDUPE_WINDOW_SECONDS", 3600.0)
    # TTL for resting orders. Unused while maker mode is refused, but the
    # lifecycle code reads it so enabling maker mode later has a bounded
    # default rather than orders that rest forever.
    order_ttl_seconds: float = _float("ORDER_TTL_SECONDS", 300.0)
    # First run against an account that already holds manually opened
    # positions will always show local-vs-exchange drift. Setting this true
    # logs the drift loudly instead of blocking startup. Leave it false in
    # production: drift normally means the fill record is wrong.
    allow_position_drift: bool = _bool("ALLOW_POSITION_DRIFT", False)

    # -- data validation ---------------------------------------------------
    # A quote older than this is not tradeable. Applies twice: Scout refuses
    # to build a Candidate from a stale quote, and risk re-checks the quote
    # captured at decision time immediately before submission, so a proposal
    # that sat in the LLM queue too long is rejected rather than acted on at
    # a price that no longer exists.
    max_quote_age_seconds: float = _float("MAX_QUOTE_AGE_SECONDS", 60.0)
    # Bounds on free text from models and feeds — it lands in the database,
    # in the next prompt as playbook context, and in log lines.
    max_reasoning_chars: int = _int("MAX_REASONING_CHARS", 2000)
    max_playbook_chars: int = _int("MAX_PLAYBOOK_CHARS", 4000)
    max_context_chars: int = _int("MAX_CONTEXT_CHARS", 4000)
    max_title_chars: int = _int("MAX_TITLE_CHARS", 300)

    # -- quant path / spot data quality ------------------------------------
    # A spot quote older than this is not usable for pricing a contract.
    max_spot_age_seconds: float = _float("MAX_SPOT_AGE_SECONDS", 120.0)
    # Subscribe to Kalshi's CF Benchmarks index relay on startup.
    #
    # Crypto contracts settle on a 60-second average of a CF Benchmarks RTI,
    # confirmed verbatim from three live markets' rules_primary. With this
    # off, families whose spec says source="kalshi_rti" get no price at all
    # and the quant path declines — deliberately, since the alternative is
    # pricing them off the spot feed the exchange states does not settle
    # them. Turning it off is therefore a way to stop trading crypto, not a
    # way to trade it differently.
    rti_feed_enabled: bool = _bool("RTI_FEED_ENABLED", True)
    # How long after startup to wait before saying out loud that the index
    # feed never came up. Long enough for a connect and first frame; short
    # enough that a silent crypto outage is not discovered a day later.
    rti_startup_grace_seconds: float = _float("RTI_STARTUP_GRACE_SECONDS", 90.0)
    # Minimum spacing between index observations kept for the volatility
    # estimate.
    #
    # The feed delivers roughly two frames a second. The history buffer holds
    # 500 points, so storing every frame would give it about four minutes of
    # span — permanently short of MIN_VOL_SPAN_SECONDS, no matter how long
    # the process runs. At 5s spacing the same buffer covers ~40 minutes and
    # clears the 600-second bar ten minutes after start. 0 stores every tick.
    rti_tick_sample_seconds: float = _float("RTI_TICK_SAMPLE_SECONDS", 5.0)
    # Distinct markets looked up per pass when grading forecast-only rows.
    #
    # Rows accumulate at roughly a hundred an hour and most resolve days
    # later, so an unbounded sweep would spend the rate-limit budget re-asking
    # about markets that are still open. Capped per call, oldest first, so
    # every row is reached eventually without any one pass being expensive.
    # 0 disables forecast grading entirely.
    forecast_reconcile_max_tickers: int = _int("FORECAST_RECONCILE_MAX_TICKERS", 25)
    # Boundary between two model regimes, as an RFC3339 timestamp. When set,
    # the calibration table is logged TWICE — once for forecasts made before
    # it, once for after — and never merged into a single number.
    #
    # It exists because the first refused-mode row ever produced read
    # "n=64 brier=0.175 said 46% actual 45% pnl $12.75", which reads as the
    # gates refusing winners, but spanned 2026-08-17T17:00Z. Before that
    # boundary sigma was 6.6% annualized and every crypto probability was
    # pinned at 0% or 100%. A single number across two models describes
    # neither of them.
    #
    # Unset means one combined table, which is the right behaviour once the
    # old regime has aged out of the data.
    calibration_regime_split_at: str = os.getenv(
        "CALIBRATION_REGIME_SPLIT_AT", "2026-08-17T17:00:00Z")
    # Keep index observations across restarts, so the volatility clock is not
    # reset by every redeploy.
    #
    # The quant path needs MIN_VOL_SPAN_SECONDS of observations before it will
    # price anything. That buffer lived only in memory, and production
    # redeploys often — so the 15-minute and hourly crypto families never
    # priced once, in any run. This keeps the SAME 600 seconds of real ticks
    # rather than lowering the bar.
    persist_vol_history: bool = _bool("PERSIST_VOL_HISTORY", True)
    # How far back stored observations stay useful.
    #
    # Was one hour, on the reasoning that a container down for an hour must
    # not come back and compute a "600-second span" across a 60-minute hole.
    # That worry turned out to be unfounded and is now disproven by test:
    # realized_vol normalises every return by its own elapsed time, so an hour
    # of 5-second ticks with five outages punched through it returns the same
    # estimate as an unbroken hour. Gaps are handled; they were never the risk.
    #
    # Meanwhile one hour was actively costing accuracy, because it is what
    # bounds how far out the sampling ladder can reach. BRTI is smoothed, so
    # the estimate has to be measured at intervals outside that smoothing —
    # and at one hour of retention the 300s and 600s rungs cannot muster
    # enough observations to be trusted, so the ladder stopped at 120s. Live,
    # neither asset had flattened by then:
    #
    #   btc  tick 17%  15s 28%  30s 37%  60s 51%  120s 60%
    #   eth  tick 12%  15s 20%  30s 26%  60s 36%  120s 45%
    #
    # Still climbing at the last rung means the plateau is past the end of the
    # ladder and the true volatility is higher than anything measured — which
    # is the understating direction, the one that produces overconfident
    # probabilities. The Checker caught it independently on a live ETH
    # contract: "vol-to-expiry of 0.008 seems low for ~3hr ETH horizon,
    # understating tail risk near the strike" — 43% annualized, where ETH
    # normally runs 50-90%.
    #
    # Four hours gives the 300s rung 48 observations and the 600s rung 24, so
    # the ladder can reach the plateau instead of being truncated short of it.
    vol_history_retention_seconds: float = _float("VOL_HISTORY_RETENTION_SECONDS", 14400.0)
    # A print this many times away from the recent median is treated as a
    # feed glitch. Volatility sits in the denominator of the probability
    # calculation, so one bad tick distorts every market on that symbol for
    # as long as it stays in the window.
    spot_outlier_ratio: float = _float("SPOT_OUTLIER_RATIO", 1.5)
    # Minimum observations before a volatility estimate is trusted. Below
    # this the quant path declines rather than pricing off noise.
    min_vol_observations: int = _int("MIN_VOL_OBSERVATIONS", 10)
    # Minimum wall-clock span the observation window must cover. Ten points
    # gathered in ten seconds say nothing about hourly volatility.
    min_vol_span_seconds: float = _float("MIN_VOL_SPAN_SECONDS", 600.0)
    # Sampling intervals the volatility estimate is measured at, in seconds.
    # BRTI is a smoothed cross-venue aggregate, so measuring it at 5-second
    # spacing sits well inside its smoothing window and understated bitcoin
    # vol by ~3x in production. Averaging destroys variance and cannot create
    # it, so the largest estimate across the ladder is the least damped one.
    #
    # The ladder once stopped at 120s because one hour of retention left the
    # 300s rung only 9-12 observations, and two containers 90 seconds apart
    # read 12% and 19-21% off it — sampling error, not signal. Since the
    # estimate is a MAXIMUM across rungs, a low reading is discarded and a
    # spuriously high one is adopted, so a rung that noisy can only hurt.
    #
    # The answer was never to truncate the ladder, though, because both assets
    # were still climbing at 120s — meaning the plateau lay past the end of it
    # and the estimate was understating. VOL_HISTORY_RETENTION_SECONDS is now
    # four hours, which gives the 300s rung 48 observations and the 600s rung
    # 24: enough to be trusted, so the ladder can reach the plateau instead of
    # stopping short of it.
    vol_sample_intervals: tuple = _floats(
        "VOL_SAMPLE_INTERVALS", (0.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0))
    # Fail-closed sanity band on the annualized volatility the estimate
    # implies. This is a REFUSAL, never a clamp — an estimate outside the band
    # means the feed or the estimator is wrong, and pricing off it is how the
    # quant path came to value bitcoin at 6.6% annualized and propose 0%/100%
    # on markets quoting 7-94%. Bitcoin has never sustained 10% realized vol.
    min_plausible_annual_vol: float = _float("MIN_PLAUSIBLE_ANNUAL_VOL", 0.10)
    max_plausible_annual_vol: float = _float("MAX_PLAUSIBLE_ANNUAL_VOL", 5.0)
    # How far past its own observation window a volatility estimate may be
    # carried. Volatility mean-reverts, so a trailing hour is a good estimate
    # of the next few minutes and a bad one of the next four days — measured
    # 52% annualized against a market implying 28% on a four-day contract,
    # which manufactured 15 points of phantom edge on out-of-the-money tails.
    # At 4x, one hour of history reaches four hours of horizon. The priority
    # families need far less: a 15-minute contract is 0.2x and an hourly one
    # 1.0x, which is why this refuses the multi-day contracts without touching
    # them.
    max_horizon_vol_span_ratio: float = _float("MAX_HORIZON_VOL_SPAN_RATIO", 4.0)
    # Skip Kalshi multi-value event shards — single markets standing for a
    # combination of legs. Nothing here can price a parlay: it resolves on the
    # joint outcome of several events and this bot has no joint model and no
    # grounding for one, so the Maker is handed a title and guesses. Live, that
    # produced "model says 72% against a market at 0.7%" — refused by the
    # coherence gate, after paying for the model call. They are also 2,717 of
    # 2,863 candidates on a typical scan, so they crowd the per-pass model
    # budget out of the 15-minute, hourly and weather families.
    skip_multi_event_shards: bool = _bool("SKIP_MULTI_EVENT_SHARDS", True)
    # Which taxonomy categories inside the Sports group may be scanned at all.
    #
    # Golf is the only sport in scope, but golf and every other sport share the
    # group "Sports", so SCOUT_CATEGORIES cannot separate them: dropping Sports
    # would drop golf too. Without this filter, MLB player props reached the
    # Checker — KXMLBKS-26AUG181835NYYBAL-NYYCRODON55-6 was evaluated and
    # rejected in production on 2026-08-18 — spending model budget on a sport
    # nobody asked to trade, with no grounding source behind it.
    #
    # Empty means no restriction, which is the old behaviour.
    scout_sports_categories: list = field(
        default_factory=lambda: [
            c.strip().lower() for c in os.getenv(
                "SCOUT_SPORTS_CATEGORIES", "Golf"
            ).split(",") if c.strip()
        ]
    )
    spot_backoff_base_seconds: float = _float("SPOT_BACKOFF_BASE_SECONDS", 30.0)
    spot_backoff_max_seconds: float = _float("SPOT_BACKOFF_MAX_SECONDS", 900.0)
    # Contract specs in core/contract_specs.py all ship verified=False,
    # because none has been checked against Kalshi's own settlement rules.
    # Setting this true prices them anyway — for demo experimentation only.
    quant_allow_unverified: bool = _bool("QUANT_ALLOW_UNVERIFIED", False)
    # Crypto contracts settle on a 60-second average of the CF Benchmarks
    # Real-Time Index, not a spot snapshot (Kalshi Help Center, "Crypto
    # Markets"). Point-in-time spot pricing is least reliable exactly inside
    # that averaging window, so the quant path stops pricing crypto this many
    # seconds before close. Set 0 to disable the blackout.
    crypto_settlement_blackout_seconds: float = _float(
        "CRYPTO_SETTLEMENT_BLACKOUT_SECONDS", 90.0
    )

    # -- fractional Kelly sizing (see core/kelly.py) ------------------------
    # Size by how good the bet is, not just by how much headroom is left.
    # Enters sizing as one more cap among the concentration gates, so it can
    # only ever make a position smaller — no existing control is weakened by
    # enabling it, which is why it defaults on.
    kelly_enabled: bool = _bool("KELLY_ENABLED", True)
    # Fraction of full Kelly. 0.25 (quarter-Kelly) matches the most
    # conservative of the surveyed bots; 0.5 is half-Kelly. Full Kelly (1.0)
    # is not recommended: it is optimal only if the model's probabilities are
    # exactly right, and ours are estimates.
    kelly_fraction: float = _float("KELLY_FRACTION", 0.25)


@dataclass
class ArbitrageConfig:
    """Structural (locked) arbitrage detection — see workers/arbitrage.py.

    Off by default. This is detection-only today: it reports opportunities
    and does not place orders, because a two-legged trade needs both legs or
    neither and the execution path has no order-lifecycle management yet.
    """

    enabled: bool = _bool("ARB_ENABLED", False)
    # Minimum guaranteed profit per YES+NO pair, in cents, AFTER both legs'
    # real Kalshi fees. Not a percentage of anything — the payout is fixed at
    # 100c, so cents are the natural unit. 1c is roughly the smallest profit
    # worth the two-sided execution risk.
    min_profit_cents: float = _float("ARB_MIN_PROFIT_CENTS", 1.0)
    # Ceiling on pairs per opportunity, so a fat-fingered book cannot size
    # into an unbounded position while the operator is asleep.
    max_pairs: int = _int("ARB_MAX_PAIRS", 100)


@dataclass
class TelegramConfig:
    """Operator alerting. Entirely optional — with no token or chat ID the
    client disables itself and the bot runs exactly as before, silently.

    Get a token from @BotFather and a chat ID from @userinfobot; see the
    README's "Telegram alerting" section.
    """

    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout_seconds: float = _float("TELEGRAM_TIMEOUT_SECONDS", 10.0)
    # Bounded queue: alerts are dropped rather than buffered without limit,
    # because unbounded buffering in a long-running process is a memory leak
    # and hours-old alerts are not worth the memory.
    queue_maxsize: int = _int("TELEGRAM_QUEUE_MAXSIZE", 200)
    # Telegram allows roughly one message per second to a given chat.
    min_interval_seconds: float = _float("TELEGRAM_MIN_INTERVAL_SECONDS", 1.0)
    max_backoff_seconds: float = _float("TELEGRAM_MAX_BACKOFF_SECONDS", 30.0)
    # Default suppression window for keyed alerts, so a condition that
    # repeats every scan pass does not send an alert every 30 seconds.
    throttle_seconds: float = _float("TELEGRAM_THROTTLE_SECONDS", 900.0)
    kill_switch_throttle_seconds: float = _float(
        "TELEGRAM_KILL_SWITCH_THROTTLE_SECONDS", 21_600.0
    )
    stall_throttle_seconds: float = _float("TELEGRAM_STALL_THROTTLE_SECONDS", 1_800.0)
    flush_timeout_seconds: float = _float("TELEGRAM_FLUSH_TIMEOUT_SECONDS", 5.0)

    # -- standing-signal alert suppression ---------------------------------
    #
    # A signal that persists is not news every time it is re-derived. These
    # bound how often an unchanged one may speak, and what counts as changed.
    #
    # Distinct from the order-level dedupe window (DEDUPE_WINDOW_SECONDS),
    # which governs whether an order may be *submitted* again. Conflating the
    # two is what produced four alerts in one day for one standing trade: the
    # hourly submission bucket was doing its job, and alerting inherited its
    # clock by accident.
    #
    # How much the net edge must move to count as a fresh signal, in absolute
    # probability. 0.05 = 5 percentage points.
    alert_edge_move_threshold: float = _float("ALERT_EDGE_MOVE_THRESHOLD", 0.05)
    # How far the executable price must move to count as fresh, in cents.
    alert_price_move_cents: float = _float("ALERT_PRICE_MOVE_CENTS", 5.0)
    # Re-alert an unchanged, still-standing signal at most this often, so
    # suppression is a quiet period rather than permanent silence. 0 disables
    # the reminder entirely (not recommended — a forgotten standing trade is
    # its own failure).
    alert_reminder_seconds: float = _float("ALERT_REMINDER_SECONDS", 3600.0)
    # Alert on every order that reaches the exchange. Turn off if the volume
    # is noisy; the kill switch and systemic errors still alert.
    notify_trades: bool = _bool("TELEGRAM_NOTIFY_TRADES", True)
    # Hour (UTC, 0-23) to send the daily summary. -1 disables it.
    daily_summary_hour_utc: int = _int("TELEGRAM_DAILY_SUMMARY_HOUR_UTC", -1)
    # Watchdog: alert when no scan or reconciliation has succeeded within
    # this many seconds. A bot that has quietly stopped trading looks exactly
    # like a bot finding no edges; this is what distinguishes them.
    stall_alert_seconds: float = _float("TELEGRAM_STALL_ALERT_SECONDS", 900.0)


@dataclass
class AppConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    # Group names from core/kalshi_categories.py, ported from Jon Becker's
    # 72.1M-trade Kalshi analysis. These replace a guessed list
    # ("Sports,Crypto,Politics,Economics,Climate,Culture") in which three of
    # six names did not exist — and a name that matches nothing silently drops
    # that whole vertical rather than erroring. Scout logs an error at startup
    # for any entry here that is not a real group.
    #
    # Valid: Sports, Politics, Crypto, Finance, Weather, Entertainment,
    #        Science/Tech, Media, World Events, Esports, Other
    scout_categories: list = field(
        default_factory=lambda: os.getenv(
            "SCOUT_CATEGORIES", "Sports,Crypto,Politics,Finance,Weather"
        ).split(",")
    )
    # Ticker families Scout reports on individually, every pass, even when it
    # finds none of them.
    #
    # The aggregate scan line said 37,144 markets fell under the liquidity
    # floor and crypto was among the groups scanned — and could not say
    # whether KXBTC15M was in that pile or absent from the catalog entirely.
    # Those need different fixes, and the difference took a code change to
    # see. These are the families the operator named as the point of the bot,
    # so each gets its own line and a zero count is a reportable answer.
    scout_census_families: list = field(
        default_factory=lambda: os.getenv(
            "SCOUT_CENSUS_FAMILIES",
            "KXBTC15M,KXETH,KXBTCD,KXBTC,KXETHD,PGATOUR,KXHIGHNY,KXHIGHCHI",
        ).split(",")
    )
    # Categories where the LLM Maker is allowed to reason with no live spot
    # feed — because either you have real grounding data wired (ESPN for
    # sports, NOAA for weather, FRED for economics) or it's a genuinely
    # qualitative-reasoning domain (politics, culture) where an LLM's
    # synthesis IS the edge, not a stand-in for one. Categories NOT in this
    # set get skipped by candidates that also fail QuantMaker.can_handle() —
    # e.g. Commodities/Tech markets like NVIDIA H100 pricing, where there's
    # no live feed AND no principled qualitative-reasoning edge, only fell
    # into "reason about it anyway" if you added them here without a real
    # grounding source behind them. Add categories here deliberately, not
    # by default, as you wire up real data for each one.
    # Renamed to the real taxonomy groups: "economics" -> Finance,
    # "climate" -> Weather, "culture" -> Entertainment. The old names matched
    # nothing, so every candidate outside the quant path was being skipped
    # for want of a grounding source it actually had.
    llm_reasoning_categories: set = field(
        default_factory=lambda: {
            c.strip().lower() for c in os.getenv(
                "LLM_REASONING_CATEGORIES", "sports,politics,finance,weather"
            ).split(",") if c.strip()
        }
    )
    # Keyword match against candidate.title — matched candidates get
    # processed before everything else each pass, and never get crowded out
    # by max_llm_calls_per_pass. "golf" by default since that's the proven
    # category; add more as other categories earn their own track record.
    priority_keywords: list = field(
        default_factory=lambda: [
            k.strip().lower() for k in os.getenv("PRIORITY_KEYWORDS", "golf,pga").split(",") if k.strip()
        ]
    )
    # Taxonomy groups evaluated ahead of the rest — but still inside the call
    # caps, unlike PRIORITY_KEYWORDS.
    #
    # The model budget used to be spent in whatever order Scout returned
    # candidates, which in practice meant politics: a production pass put all
    # ten calls on KXVOTEPRIMARY, KXTRUMPSAY and KXCLARITYVOTE, on every pass,
    # while weather got none. Crypto does not need a place here — it is priced
    # on the quant path and spends no model budget at all.
    #
    # Ordering only. A preferred category is asked first; it is not asked
    # without limit. Raising a spending ceiling is a separate decision from
    # deciding who is asked first, and PRIORITY_KEYWORDS is where that lives.
    priority_categories: set = field(
        default_factory=lambda: {
            c.strip().lower() for c in os.getenv(
                "PRIORITY_CATEGORIES", "weather"
            ).split(",") if c.strip()
        }
    )
    # Caps Maker (Kimi) LLM calls per scan pass so a broad category list
    # doesn't dilute spend/rate-limit budget away from priority markets.
    # Quant-path markets (crypto/commodities) aren't affected — no LLM call
    # to cap there. 0 = unlimited.
    max_llm_calls_per_pass: int = _int("MAX_LLM_CALLS_PER_PASS", 40)
    # Model calls one event may consume in a single pass.
    #
    # Production spent all ten calls of every pass on ONE oil contract's
    # strike ladder — ten near-identical questions — while weather, crypto
    # and golf got none at all. Ten strikes of the same contract are worth far
    # less than five different events, and once the coherence gate had refused
    # that ladder the calls were not just poor value, they were wasted.
    #
    # Priority markets (PRIORITY_KEYWORDS) bypass this, as they do the
    # per-pass cap. 0 disables it.
    max_llm_calls_per_event: int = _int("MAX_LLM_CALLS_PER_EVENT", 2)
    # Tighter cap that applies while the Maker is running on its fallback
    # provider. The default cap is sized for Moonshot's price; the fallback
    # exists to keep the bot trading through an outage, not to run the same
    # volume through a dearer provider indefinitely. Priority markets still
    # bypass both caps.
    max_fallback_llm_calls_per_pass: int = _int("MAX_FALLBACK_LLM_CALLS_PER_PASS", 10)
    # Consecutive Maker/Checker failures that end a pass. Below this, a failed
    # model call skips that one candidate and the pass continues; at it, the
    # provider is presumed down and the pass stops with an alert. One bad
    # response should cost one candidate, not the whole scan.
    model_failure_threshold: int = _int("MODEL_FAILURE_THRESHOLD", 5)
    # How long to hold before exiting when startup reconciliation fails, so
    # the supervisor's restart loop becomes a slow retry rather than a
    # sustained burst of failing auth calls. See main().
    startup_failure_hold_seconds: int = _int("STARTUP_FAILURE_HOLD_SECONDS", 60)
    # Gap between scan passes.
    #
    # Was 30s, which is what a bot scanning a handful of markets wants. This
    # one paginates the entire ~80,000-market catalog every pass, so 30s meant
    # re-reading all of Kalshi twice a minute, forever, to act on at most a
    # few dozen candidates. Nothing downstream benefits: the LLM call cap
    # bounds how many candidates a pass can even evaluate, and market prices
    # do not move enough in 30 seconds to justify 400 more API calls.
    scout_poll_seconds: int = _int("SCOUT_POLL_SECONDS", 180)
    # Safety valve on pages fetched per scan, 200 markets each.
    #
    # MEASURED IN PRODUCTION, 2026-08-15: GET /markets returns a page in about
    # 29ms (25 pages in 0.72s), so the whole ~50,000-market catalog is roughly
    # 8-12 seconds — comfortably inside SCOUT_POLL_SECONDS, and well inside
    # MAX_QUOTE_AGE_SECONDS for the quotes read on the first page.
    #
    # An earlier version of this comment claimed a full scan took 6+ minutes.
    # That measurement was of GET /events?with_nested_markets=true, a far
    # heavier response, and it does not apply to /markets. Capping at 25 pages
    # on that basis was worse than useless: Kalshi returns the catalog in an
    # order that front-loads low-volume esports and multi-game prop markets,
    # so the bot was scanning ~5,000 markets that could never clear the
    # liquidity floor and concluding there was nothing to trade.
    #
    # 400 pages covers the current catalog with headroom. The cap exists so a
    # catalog that grows by an order of magnitude cannot silently turn a scan
    # into a multi-minute stall; it is not meant to bind in normal operation.
    # 0 disables it entirely.
    scout_max_pages: int = _int("SCOUT_MAX_PAGES", 400)
    ledger_db_path: str = os.getenv("LEDGER_DB_PATH", "/data/daemon_kalshi.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # httpx's INFO logging prints full request URLs, which for Telegram means
    # printing the bot token on every alert, and for Scout means 400 lines of
    # pagination cursors per pass. Kept at WARNING; raise it deliberately and
    # temporarily when debugging a specific request.
    httpx_log_level: str = os.getenv("HTTPX_LOG_LEVEL", "WARNING")


CONFIG = AppConfig()
