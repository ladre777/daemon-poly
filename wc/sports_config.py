"""Sport definitions for DÆMON-POLY multi-sport trading.

Every sport routes through the SAME safety core — gates.py (caps, concurrency,
bankroll ceiling), the Opus CHECKER, and the 5% drawdown kill switch. Only the
data sources and edge descriptions differ per sport. Flip `active` to enable /
disable a sport without touching any other module.

Polymarket slugs below are EVENT slugs (verified live), not market slugs — the
market reader resolves them via /events?slug=.
"""

SPORT_CONFIGS = {
    "world_cup": {
        "key":          "world_cup",
        "label":        "World Cup",
        "emoji":        "⚽",
        "active":       True,
        "espn_paths":   ["soccer/fifa.world"],
        # Polymarket US (live execution venue) futures discovery.
        "us_search":     ["world cup winner"],
        "us_title_match": ["World Cup Winner"],
        "futures": {
            "Winner":       "world-cup-winner",
            "Golden Boot":  "world-cup-golden-boot-winner",
        },
        "in_play_type": "soccer",
        "settle_note":  "Match winner markets settle at 90-min regulation ONLY (no extra time/penalties).",
        "edges": (
            "EDGE CASCADE — Elimination reprice: a top team is eliminated and the surviving side is "
            "priced below updated bracket math. Wait >=2h after the result.\n"
            "EDGE BRACKET — A team's bracket path is confirmed but its Stage-of-Elimination market "
            "still prices an old opponent.\n"
            "EDGE IN_PLAY — Hydration-break window (~25-30' and ~70-75'); lead >=1 goal and the 90-min "
            "winner market hasn't moved >=15pp. Max hold 20 min.\n"
            "EDGE LADDER — Sum of a team's Stage-of-Elimination outcomes deviates >5% from 100%.\n"
            "EDGE PROP — Star-player Golden Boot collapse after their team is eliminated."
        ),
    },
    "mlb": {
        "key":          "mlb",
        "label":        "MLB",
        "emoji":        "⚾",
        "active":       True,
        "espn_paths":   ["baseball/mlb"],
        "us_search":     ["mlb world series"],
        "us_title_match": ["World Series Champion"],
        "futures": {
            "World Series": "mlb-world-series-champion-2026",
            "AL Champion":  "mlb-2026-american-league-champion",
            "NL Champion":  "mlb-2026-national-league-champion",
        },
        "in_play_type": "baseball",
        "settle_note":  "Moneyline markets settle at the final out (extra innings included).",
        "edges": (
            "EDGE FUTURES — Champion/pennant markets mispriced vs standings and run differential.\n"
            "EDGE IN_PLAY — Late-game (7th inning+) value when a close game's win market lags the "
            "score and base/out state.\n"
            "EDGE PROP — Award markets (MVP, Cy Young) mispriced vs season trajectory."
        ),
    },
    "wnba": {
        "key":          "wnba",
        "label":        "WNBA",
        "emoji":        "🏀",
        "active":       True,
        "espn_paths":   ["basketball/wnba"],
        "us_search":     ["wnba champion"],
        "us_title_match": ["WNBA Champion"],
        "futures": {
            "Champion": "wnba-2026-champion-464",
        },
        "in_play_type": "basketball",
        "settle_note":  "Moneyline markets settle at the final buzzer (overtime included).",
        "edges": (
            "EDGE FUTURES — Champion market mispriced vs standings and net rating.\n"
            "EDGE IN_PLAY — 4th-quarter value when a close game's win market lags the score/possession.\n"
            "EDGE PROP — MVP / Rookie-of-the-Year markets mispriced vs season trajectory."
        ),
    },
    "golf": {
        "key":          "golf",
        "label":        "Golf — The Open",
        "emoji":        "⛳",
        "active":       True,
        "espn_paths":   ["golf/pga"],
        # HARD event match — ESPN's golf scoreboard lists multiple concurrent
        # tournaments; only trade the one that matches (memory lesson: never
        # trade the wrong tournament).
        "event_match":  ["The Open"],
        "us_search":     ["open championship"],
        "us_title_match": ["Open Championship Winner"],
        "futures": {
            "The Open Winner": "2026-the-open-championship-winner",
        },
        "in_play_type": None,  # no separate in-play loop; 3-min main cycle IS live for golf
        "settle_note":  "Winner market settles on the official tournament champion (playoff included).",
        "edges": (
            "EDGE LEADERBOARD — Winner market lags the live leaderboard: a player holds/extends a "
            "multi-shot lead late (final round, back nine) but the market hasn't repriced. "
            "Weight holes-remaining and chaser quality.\n"
            "EDGE COLLAPSE — A leader's price stays elevated after consecutive dropped shots while "
            "an in-form chaser within 2 is still cheap.\n"
            "EDGE WEATHER/WAVE — Late-wave players face materially worse conditions than the market "
            "has priced into their winner odds."
        ),
    },
    "wimbledon": {
        "key":          "wimbledon",
        "label":        "Wimbledon",
        "emoji":        "🎾",
        "active":       True,
        "espn_paths":   ["tennis/atp", "tennis/wta"],
        # HARD event match — ESPN tennis feeds list whatever tour events are
        # running; without this the bot analyzes unrelated tournaments
        # (e.g. Gstaad/Estoril) as "Wimbledon" once the slam ends.
        "event_match":  ["Wimbledon"],
        "us_search":     ["wimbledon"],
        "us_title_match": ["Wimbledon Winner"],
        "futures": {
            "Men's Winner":   "2026-mens-wimbledon-winner",
            "Women's Winner": "2026-womens-wimbledon-winner",
        },
        "in_play_type": "tennis",
        "settle_note":  "Match markets settle on match completion (a retirement still resolves).",
        "edges": (
            "EDGE FUTURES — Tournament winner markets mispriced vs seeding and draw path.\n"
            "EDGE IN_PLAY — Live-match value when the winner market lags a set/break swing."
        ),
    },
}


def active_sports() -> list:
    """Configs for every sport with active=True, in display order."""
    return [c for c in SPORT_CONFIGS.values() if c.get("active")]


def get_sport(key: str) -> dict:
    return SPORT_CONFIGS.get(key, {})
