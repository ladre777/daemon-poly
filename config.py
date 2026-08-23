"""
Central config for DÆMON-KALSHI.

Scope: Golf, Crypto, Weather, Finance/commodities.
Moonshot: longer timeouts — production saw successful calls then read timeouts.
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


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
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        parsed = tuple(float(p) for p in raw.split(",") if p.strip())
    except (TypeError, ValueError):
        return default
    return parsed or default


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default


DEFAULT_MAKER_FALLBACK_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MOONSHOT_MODEL = "kimi-k2.6"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


@dataclass
class KalshiConfig:
    env: str = os.getenv("KALSHI_ENV", "demo")
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    private_key_pem: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
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
        raise RuntimeError("No Kalshi private key configured.")


@dataclass
class ModelConfig:
    moonshot_api_key: str = field(
        default_factory=lambda: _first_env(
            "MOONSHOT_API_KEY", "KIMI_API_KEY", "MOONSHOT_KEY"
        )
    )
    moonshot_base_url: str = os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
    moonshot_model: str = _first_env(
        "MOONSHOT_MODEL", "KIMI_MODEL", default=DEFAULT_MOONSHOT_MODEL
    )
    moonshot_max_tokens: int = _int("MOONSHOT_MAX_TOKENS", 800)
    moonshot_prompt_cache_key: str = os.getenv(
        "MOONSHOT_PROMPT_CACHE_KEY", "daemon-kalshi-maker-v1"
    )
    # K2.6 thinking consumes the same max_tokens budget as final content.
    # Disable it for short, source-grounded one-shot probability decisions so
    # the required JSON response is not truncated before it is emitted.
    moonshot_disable_thinking: bool = _bool("MOONSHOT_DISABLE_THINKING", True)

    # Gemini is an optional failover provider. The key remains in Railway,
    # never in source control or logs.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_base_url: str = os.getenv(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
    )
    gemini_model: str = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    gemini_timeout_seconds: float = _float("GEMINI_TIMEOUT_SECONDS", 12.0)
    gemini_rate_limit_cooldown_seconds: float = _float(
        "GEMINI_RATE_LIMIT_COOLDOWN_SECONDS", 900.0
    )

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    checker_provider: str = os.getenv("CHECKER_LLM_PROVIDER", "moonshot")
    checker_model: str = field(
        default_factory=lambda: _first_env(
            "CHECKER_MODEL", "MOONSHOT_MODEL", "KIMI_MODEL",
            default=DEFAULT_MOONSHOT_MODEL,
        )
    )
    checker_anthropic_model: str = os.getenv(
        "CHECKER_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
    )
    # 25s — Moonshot often answers after 12s under load; short timeout = false failure.
    checker_timeout_seconds: float = _float("CHECKER_TIMEOUT_SECONDS", 25.0)
    checker_max_tokens: int = _int("CHECKER_MAX_TOKENS", 1200)
    checker_effort: str = os.getenv("CHECKER_EFFORT", "low")

    maker_provider: str = os.getenv("MAKER_LLM_PROVIDER", "moonshot")
    maker_timeout_seconds: float = _float("MAKER_TIMEOUT_SECONDS", 25.0)
    maker_fallback_model: str = os.getenv(
        "MAKER_FALLBACK_MODEL", DEFAULT_MAKER_FALLBACK_MODEL
    )

    fred_api_key: str = os.getenv("FRED_API_KEY", "")
    slash_golf_api_key: str = os.getenv("SLASH_GOLF_API_KEY", "")


@dataclass
class RiskConfig:
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.05)
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.10)
    coherence_checks_enabled: bool = _bool("COHERENCE_CHECKS_ENABLED", True)
    coherence_tolerance: float = _float("COHERENCE_TOLERANCE", 0.01)
    max_log_odds_disagreement: float = _float("MAX_LOG_ODDS_DISAGREEMENT", 3.0)
    count_unrealized_gains: bool = _bool("COUNT_UNREALIZED_GAINS", False)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 15)
    min_edge_threshold: float = _float("MIN_EDGE_THRESHOLD", 0.04)
    min_liquidity_usd: float = _float("MIN_LIQUIDITY_USD", 500.0)
    checker_min_confidence: float = _float("CHECKER_MIN_CONFIDENCE", 0.65)
    dry_run: bool = _bool("DRY_RUN", True)
    longshot_price_threshold_cents: float = _float("LONGSHOT_PRICE_THRESHOLD_CENTS", 20)
    longshot_edge_multiplier: float = _float("LONGSHOT_EDGE_MULTIPLIER", 1.5)
    order_strategy: str = os.getenv("ORDER_STRATEGY", "taker")
    max_total_exposure_pct: float = _float("MAX_TOTAL_EXPOSURE_PCT", 0.50)
    max_ticker_exposure_pct: float = _float("MAX_TICKER_EXPOSURE_PCT", 0.05)
    max_event_exposure_pct: float = _float("MAX_EVENT_EXPOSURE_PCT", 0.10)
    max_category_exposure_pct: float = _float("MAX_CATEGORY_EXPOSURE_PCT", 0.25)
    fee_rate: float = _float("FEE_RATE", 0.07)
    slippage_cents: float = _float("SLIPPAGE_CENTS", 1.0)
    max_reconciliation_age_seconds: float = _float("MAX_RECONCILIATION_AGE_SECONDS", 90.0)
    dedupe_window_seconds: float = _float("DEDUPE_WINDOW_SECONDS", 3600.0)
    order_ttl_seconds: float = _float("ORDER_TTL_SECONDS", 300.0)
    allow_position_drift: bool = _bool("ALLOW_POSITION_DRIFT", False)
    max_quote_age_seconds: float = _float("MAX_QUOTE_AGE_SECONDS", 60.0)
    max_reasoning_chars: int = _int("MAX_REASONING_CHARS", 2000)
    max_playbook_chars: int = _int("MAX_PLAYBOOK_CHARS", 4000)
    max_context_chars: int = _int("MAX_CONTEXT_CHARS", 2400)
    max_title_chars: int = _int("MAX_TITLE_CHARS", 300)
    max_spot_age_seconds: float = _float("MAX_SPOT_AGE_SECONDS", 120.0)
    rti_feed_enabled: bool = _bool("RTI_FEED_ENABLED", True)
    rti_startup_grace_seconds: float = _float("RTI_STARTUP_GRACE_SECONDS", 90.0)
    rti_tick_sample_seconds: float = _float("RTI_TICK_SAMPLE_SECONDS", 5.0)
    forecast_reconcile_max_tickers: int = _int("FORECAST_RECONCILE_MAX_TICKERS", 25)
    # Single boundary only — _regime_boundary() in workers/ledger.py splits
    # calibration into exactly two tables (pre-fix / post-fix), and there is
    # no mechanism for a third. Moved from 2026-08-17T17:00:00Z (the sigma
    # fix, #41) to 2026-08-21T19:01:00Z: the deploy of e4cade6, which rewrote
    # the weather prompt's error-band guidance from an invented 3-4F to a
    # measured 1-2F. Rows on either side of that prompt change are not
    # comparable, and the old boundary was pooling them into one "post-fix"
    # table.
    #
    # This is a real loss of resolution, stated plainly rather than hidden: a
    # third regime existed between the two boundaries — sigma-fixed but still
    # on the old weather prompt, 2026-08-17T17:00 to 2026-08-21T19:01 — and
    # moving the single boundary forward merges those rows into "pre-fix"
    # rather than giving them their own table. That is the correct side to
    # err on: a two-table split needs one boundary per open question, and the
    # question live right now is what the current prompt's calibration looks
    # like, not how the middle period compared to either edge.
    calibration_regime_split_at: str = os.getenv(
        "CALIBRATION_REGIME_SPLIT_AT", "2026-08-21T19:01:00Z")
    persist_vol_history: bool = _bool("PERSIST_VOL_HISTORY", True)
    vol_history_retention_seconds: float = _float("VOL_HISTORY_RETENTION_SECONDS", 14400.0)
    spot_outlier_ratio: float = _float("SPOT_OUTLIER_RATIO", 1.5)
    min_vol_observations: int = _int("MIN_VOL_OBSERVATIONS", 10)
    min_vol_span_seconds: float = _float("MIN_VOL_SPAN_SECONDS", 600.0)
    vol_sample_intervals: tuple = _floats(
        "VOL_SAMPLE_INTERVALS", (0.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0))
    min_plausible_annual_vol: float = _float("MIN_PLAUSIBLE_ANNUAL_VOL", 0.10)
    max_plausible_annual_vol: float = _float("MAX_PLAUSIBLE_ANNUAL_VOL", 5.0)
    max_horizon_vol_span_ratio: float = _float("MAX_HORIZON_VOL_SPAN_RATIO", 4.0)
    max_vol_spike_ratio: float = _float("MAX_VOL_SPIKE_RATIO", 1.75)
    skip_multi_event_shards: bool = _bool("SKIP_MULTI_EVENT_SHARDS", True)
    scout_sports_categories: list = field(
        default_factory=lambda: [
            c.strip().lower() for c in os.getenv(
                "SCOUT_SPORTS_CATEGORIES", "Golf"
            ).split(",") if c.strip()
        ]
    )
    spot_backoff_base_seconds: float = _float("SPOT_BACKOFF_BASE_SECONDS", 30.0)
    spot_backoff_max_seconds: float = _float("SPOT_BACKOFF_MAX_SECONDS", 900.0)
    quant_allow_unverified: bool = _bool("QUANT_ALLOW_UNVERIFIED", False)
    crypto_settlement_blackout_seconds: float = _float(
        "CRYPTO_SETTLEMENT_BLACKOUT_SECONDS", 90.0
    )
    kelly_enabled: bool = _bool("KELLY_ENABLED", True)
    kelly_fraction: float = _float("KELLY_FRACTION", 0.25)


@dataclass
class ArbitrageConfig:
    enabled: bool = _bool("ARB_ENABLED", True)
    min_profit_cents: float = _float("ARB_MIN_PROFIT_CENTS", 1.0)
    max_pairs: int = _int("ARB_MAX_PAIRS", 100)


@dataclass
class TelegramConfig:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    timeout_seconds: float = _float("TELEGRAM_TIMEOUT_SECONDS", 10.0)
    queue_maxsize: int = _int("TELEGRAM_QUEUE_MAXSIZE", 200)
    min_interval_seconds: float = _float("TELEGRAM_MIN_INTERVAL_SECONDS", 1.0)
    max_backoff_seconds: float = _float("TELEGRAM_MAX_BACKOFF_SECONDS", 30.0)
    throttle_seconds: float = _float("TELEGRAM_THROTTLE_SECONDS", 900.0)
    kill_switch_throttle_seconds: float = _float(
        "TELEGRAM_KILL_SWITCH_THROTTLE_SECONDS", 21_600.0
    )
    stall_throttle_seconds: float = _float("TELEGRAM_STALL_THROTTLE_SECONDS", 1_800.0)
    flush_timeout_seconds: float = _float("TELEGRAM_FLUSH_TIMEOUT_SECONDS", 5.0)
    alert_edge_move_threshold: float = _float("ALERT_EDGE_MOVE_THRESHOLD", 0.05)
    alert_price_move_cents: float = _float("ALERT_PRICE_MOVE_CENTS", 5.0)
    alert_reminder_seconds: float = _float("ALERT_REMINDER_SECONDS", 3600.0)
    notify_trades: bool = _bool("TELEGRAM_NOTIFY_TRADES", True)
    daily_summary_hour_utc: int = _int("TELEGRAM_DAILY_SUMMARY_HOUR_UTC", -1)
    stall_alert_seconds: float = _float("TELEGRAM_STALL_ALERT_SECONDS", 900.0)
    #: Alert when the exchange balance moves by at least this much, in either
    #: direction. $49.98 left the account and nothing said so; a deposit
    #: arriving is equally worth knowing, because it is what re-enables
    #: trading after risk has been refusing everything on a $0 bankroll.
    balance_alert_threshold_usd: float = _float("BALANCE_ALERT_THRESHOLD_USD", 1.0)
    balance_alert_throttle_seconds: float = _float(
        "TELEGRAM_BALANCE_ALERT_THROTTLE_SECONDS", 300.0
    )


@dataclass
class AppConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    scout_categories: list = field(
        default_factory=lambda: os.getenv(
            "SCOUT_CATEGORIES", "Sports,Crypto,Weather,Finance"
        ).split(",")
    )
    # Families Scout reports on individually every pass, and asks Kalshi for
    # by name via the targeted per-series fetch. A zero count is a reportable
    # answer, which is the whole point — but only if the name is one Kalshi
    # still uses.
    #
    # KXPGATOUR, not PGATOUR. VERIFIED AGAINST PRODUCTION, 2026-08-23: the
    # targeted fetch for series "PGATOUR" ran every pass, succeeded, and
    # returned zero markets, so the census reported "family absent from the
    # scanned catalog" and no golf market ever reached the pipeline. Kalshi's
    # golf series is KXPGATOUR (their own market URLs read
    # kalshi.com/markets/kxpgatour/...), and tests/test_categories.py has
    # asserted that spelling since the taxonomy was ported. The census watch
    # list was the one place still on the pre-KX name.
    #
    # Both names are carried deliberately. KXPGATOUR could not be confirmed
    # against the live API from the session that found this — the evidence is
    # Kalshi's public URLs plus our own tests — so the legacy name stays until
    # a pass logs real KXPGATOUR markets. A series that does not exist costs
    # one bounded, empty request per pass; guessing wrong and dropping the
    # only working name costs the whole category.
    scout_census_families: list = field(
        default_factory=lambda: os.getenv(
            "SCOUT_CENSUS_FAMILIES",
            "KXBTC15M,KXETH,KXBTCD,KXBTC,KXETHD,KXPGATOUR,PGATOUR,"
            "KXHIGHNY,KXHIGHCHI,KXWTI",
        ).split(",")
    )
    llm_reasoning_categories: set = field(
        default_factory=lambda: {
            c.strip().lower() for c in os.getenv(
                "LLM_REASONING_CATEGORIES", "sports,weather,finance"
            ).split(",") if c.strip()
        }
    )
    priority_keywords: list = field(
        default_factory=lambda: [
            k.strip().lower() for k in os.getenv(
                "PRIORITY_KEYWORDS",
                "golf,pga,masters,liv,btc,bitcoin,eth,high,temperature,rain,"
                "wti,oil,gas,gold,silver,fed,cpi",
            ).split(",") if k.strip()
        ]
    )
    priority_categories: set = field(
        default_factory=lambda: {
            c.strip().lower() for c in os.getenv(
                "PRIORITY_CATEGORIES", "weather,crypto,finance"
            ).split(",") if c.strip()
        }
    )
    # Fewer calls/pass → less timeout pressure on Moonshot.
    max_llm_calls_per_pass: int = _int("MAX_LLM_CALLS_PER_PASS", 25)
    max_llm_calls_per_event: int = _int("MAX_LLM_CALLS_PER_EVENT", 2)
    max_fallback_llm_calls_per_pass: int = _int("MAX_FALLBACK_LLM_CALLS_PER_PASS", 8)
    model_failure_threshold: int = _int("MODEL_FAILURE_THRESHOLD", 5)
    startup_failure_hold_seconds: int = _int("STARTUP_FAILURE_HOLD_SECONDS", 60)
    scout_poll_seconds: int = _int("SCOUT_POLL_SECONDS", 180)
    scout_max_pages: int = _int("SCOUT_MAX_PAGES", 400)
    ledger_db_path: str = os.getenv("LEDGER_DB_PATH", "/data/daemon_kalshi.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    httpx_log_level: str = os.getenv("HTTPX_LOG_LEVEL", "WARNING")


CONFIG = AppConfig()

_log = logging.getLogger("daemon_kalshi.config")
_log.info(
    "Config scope categories=%s sports=%s llm_cats=%s moonshot_key=%s model=%s timeouts=%.0fs",
    CONFIG.scout_categories,
    CONFIG.risk.scout_sports_categories,
    sorted(CONFIG.llm_reasoning_categories),
    "set" if CONFIG.models.moonshot_api_key else "MISSING",
    CONFIG.models.moonshot_model,
    CONFIG.models.maker_timeout_seconds,
)
