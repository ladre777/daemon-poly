"""ESPN live-data scout, generalized across sports.

Each sport supplies one or more ESPN site-API league paths (e.g. "soccer/fifa.world",
"baseball/mlb", or both "tennis/atp" + "tennis/wta"). Functions take a sport config
dict (from sports_config.py) so the same parsing serves every sport.
"""

import requests
from datetime import datetime

ESPN_HOST = "https://site.api.espn.com/apis/site/v2/sports"

LIVE_STATUSES = (
    "STATUS_IN_PROGRESS",
    "STATUS_HALFTIME",
    "STATUS_END_PERIOD",
    "STATUS_FIRST_HALF",
    "STATUS_SECOND_HALF",
)


def _fetch_scoreboard(path: str) -> list:
    resp = requests.get(f"{ESPN_HOST}/{path}/scoreboard", timeout=10)
    resp.raise_for_status()
    return resp.json().get("events", [])


def _competitor_name(c: dict) -> str:
    team = c.get("team", {}) or {}
    name = team.get("displayName") or team.get("name")
    if not name:
        ath = c.get("athlete", {}) or {}
        name = ath.get("displayName") or ath.get("shortName", "")
    return name or ""


def _parse_event(event: dict, sport_key: str) -> dict:
    status      = event.get("status", {})
    comp        = event.get("competitions", [{}])[0]
    competitors = comp.get("competitors", [])

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    # Tennis (and some feeds) carry no homeAway flags — fall back to order.
    if home is None or away is None:
        home = home or (competitors[0] if len(competitors) > 0 else {})
        away = away or (competitors[1] if len(competitors) > 1 else {})

    return {
        "sport":         sport_key,
        "id":            event.get("id"),
        "name":          event.get("name") or event.get("shortName", ""),
        "date":          event.get("date"),
        "status_type":   status.get("type", {}).get("name", "unknown"),
        "status_detail": status.get("type", {}).get("detail", ""),
        "clock":         status.get("displayClock", "0:00"),
        "period":        status.get("period", 0),
        "home_team":     _competitor_name(home),
        "home_score":    home.get("score", "0"),
        "away_team":     _competitor_name(away),
        "away_score":    away.get("score", "0"),
        "venue":         comp.get("venue", {}).get("fullName", ""),
    }


def _parse_golf_event(event: dict, sport_key: str) -> dict:
    """Golf is a leaderboard sport — return the event with a top-15 leaderboard
    instead of home/away teams."""
    status = event.get("status", {})
    comp   = event.get("competitions", [{}])[0]
    board  = []
    for c in sorted(comp.get("competitors", []), key=lambda x: x.get("order", 999))[:15]:
        ath = c.get("athlete", {}) or {}
        ls  = c.get("linescores", []) or []
        board.append({
            "pos":    c.get("order"),
            "player": ath.get("displayName", "?"),
            "score":  c.get("score"),
            "rounds": [l.get("value") for l in ls if l.get("value") is not None],
        })
    return {
        "sport":         sport_key,
        "id":            event.get("id"),
        "name":          event.get("name") or event.get("shortName", ""),
        "date":          event.get("date"),
        "status_type":   status.get("type", {}).get("name", "unknown"),
        "status_detail": status.get("type", {}).get("detail", ""),
        "period":        status.get("period", 0),   # round number for golf
        "leaderboard":   board,
    }


def get_all_matches(sport_cfg: dict) -> list:
    """Every scheduled/live event for a sport across all its ESPN paths."""
    out = []
    is_golf     = "golf" in "".join(sport_cfg.get("espn_paths", []))
    event_match = sport_cfg.get("event_match")
    for path in sport_cfg.get("espn_paths", []):
        try:
            for ev in _fetch_scoreboard(path):
                # HARD event filter — some feeds (golf) list concurrent
                # tournaments; only keep events the config explicitly matches.
                if event_match:
                    name = ev.get("name", "") or ""
                    if not any(m.lower() in name.lower() for m in event_match):
                        continue
                if is_golf:
                    out.append(_parse_golf_event(ev, sport_cfg["key"]))
                else:
                    out.append(_parse_event(ev, sport_cfg["key"]))
        except Exception as e:
            print(f"[scout] {sport_cfg.get('key')} {path} error: {e}")
    return out


def get_live_matches(sport_cfg: dict) -> list:
    return [m for m in get_all_matches(sport_cfg) if m.get("status_type") in LIVE_STATUSES]


def get_match_detail(sport_cfg: dict, event_id: str) -> dict:
    for path in sport_cfg.get("espn_paths", []):
        try:
            resp = requests.get(
                f"{ESPN_HOST}/{path}/summary",
                params={"event": event_id},
                timeout=10,
            )
            resp.raise_for_status()
            data     = resp.json()
            boxscore = data.get("boxscore", {})
            stats    = {}
            for team in boxscore.get("teams", []):
                name = team.get("team", {}).get("displayName", "unknown")
                stats[name] = {
                    s.get("name", ""): s.get("displayValue", "")
                    for s in team.get("statistics", [])
                }
            return {
                "event_id": event_id,
                "stats":    stats,
                "plays":    data.get("plays", [])[-5:],
            }
        except Exception:
            continue
    return {"event_id": event_id, "stats": {}}


def is_in_play_window(sport_cfg: dict, match: dict) -> bool:
    """Sport-specific 'tradeable live window'. Conservative by design — the
    SIGNAL model + gates + CHECKER still decide whether anything is actionable."""
    kind   = sport_cfg.get("in_play_type")
    period = match.get("period", 0) or 0

    if kind == "soccer":
        try:
            minutes = int(str(match.get("clock", "0:00")).split(":")[0])
        except Exception:
            return False
        if period == 1 and 24 <= minutes <= 31:
            return True
        if period == 2 and 69 <= minutes <= 76:
            return True
        return False

    if kind == "baseball":
        return period >= 7            # 7th inning or later

    if kind == "basketball":
        return period >= 4            # 4th quarter / overtime

    if kind == "tennis":
        return match.get("status_type") == "STATUS_IN_PROGRESS"

    return False
