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


@dataclass
class KalshiConfig:
    env: str = os.getenv("KALSHI_ENV", "demo")  # "demo" or "prod"
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    # Prefer a mounted file locally; on Railway store the PEM contents
    # directly in an env var since there's no persistent filesystem to mount.
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    private_key_pem: str = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")

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

    # Grounding data sources (not LLMs, but live here alongside the other
    # external-service keys for a single place to look).
    fred_api_key: str = os.getenv("FRED_API_KEY", "")


@dataclass
class RiskConfig:
    # Per-trade and account-level guardrails. Tune these to your real
    # PF-04/PF-09/PF-10 numbers — these are conservative placeholders.
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.05)       # 5% of bankroll per position
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.10)    # kill switch trigger
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
    order_strategy: str = os.getenv("ORDER_STRATEGY", "taker")


@dataclass
class AppConfig:
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    # These are Kalshi's own category names (case-insensitive match in
    # scout.py), not a guessed heuristic — call Scout.list_available_categories()
    # once against your account to confirm current spelling before relying on it.
    # NOTE: your own screenshots show Commodities as a category distinct
    # from Crypto (with Oil & Gas / Metals / Tech sub-tabs) — an earlier
    # version of this default conflated them. Verified taxonomy > my guess;
    # run list_available_categories() and treat this as a starting point.
    scout_categories: list = field(
        default_factory=lambda: os.getenv(
            "SCOUT_CATEGORIES", "Sports,Crypto,Politics,Economics,Climate,Culture"
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
    llm_reasoning_categories: set = field(
        default_factory=lambda: {
            c.strip().lower() for c in os.getenv(
                "LLM_REASONING_CATEGORIES", "sports,politics,economics,climate,culture"
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
    # Caps Maker (Kimi) LLM calls per scan pass so a broad category list
    # doesn't dilute spend/rate-limit budget away from priority markets.
    # Quant-path markets (crypto/commodities) aren't affected — no LLM call
    # to cap there. 0 = unlimited.
    max_llm_calls_per_pass: int = _int("MAX_LLM_CALLS_PER_PASS", 40)
    scout_poll_seconds: int = _int("SCOUT_POLL_SECONDS", 30)
    ledger_db_path: str = os.getenv("LEDGER_DB_PATH", "/data/daemon_kalshi.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


CONFIG = AppConfig()
