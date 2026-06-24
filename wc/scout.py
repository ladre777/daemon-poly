import requests
import json
from datetime import datetime

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"


def get_live_scoreboard() -> dict:
    try:
        resp = requests.get(f"{ESPN_BASE}/scoreboard", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])

        matches = []
        for event in events:
            status = event.get("status", {})
            competitors = event.get("competitions", [{}])[0].get("competitors", [])

            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            match = {
                "id":             event.get("id"),
                "name":           event.get("name"),
                "date":           event.get("date"),
                "status_type":    status.get("type", {}).get("name", "unknown"),
                "status_detail":  status.get("type", {}).get("detail", ""),
                "clock":          status.get("displayClock", "0:00"),
                "period":         status.get("period", 0),
                "home_team":      home.get("team", {}).get("displayName", ""),
                "home_score":     home.get("score", "0"),
                "home_record":    home.get("records", [{}])[0].get("summary", ""),
                "away_team":      away.get("team", {}).get("displayName", ""),
                "away_score":     away.get("score", "0"),
                "away_record":    away.get("records", [{}])[0].get("summary", ""),
                "venue":          event.get("competitions", [{}])[0].get("venue", {}).get("fullName", ""),
            }
            matches.append(match)

        return {
            "timestamp":   datetime.utcnow().isoformat(),
            "match_count": len(matches),
            "matches":     matches,
        }
    except Exception as e:
        return {"error": str(e), "matches": []}


def get_match_detail(event_id: str) -> dict:
    try:
        resp = requests.get(
            f"{ESPN_BASE}/summary",
            params={"event": event_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        boxscore = data.get("boxscore", {})
        teams = boxscore.get("teams", [])

        stats = {}
        for team in teams:
            name = team.get("team", {}).get("displayName", "unknown")
            team_stats = {}
            for stat in team.get("statistics", []):
                team_stats[stat.get("name", "")] = stat.get("displayValue", "")
            stats[name] = team_stats

        return {
            "event_id": event_id,
            "stats":    stats,
            "plays":    data.get("plays", [])[-5:],
        }
    except Exception as e:
        return {"error": str(e), "stats": {}}


def get_live_matches() -> list:
    board = get_live_scoreboard()
    return [
        m for m in board.get("matches", [])
        if m.get("status_type") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
    ]


def get_all_matches() -> list:
    board = get_live_scoreboard()
    return board.get("matches", [])


def is_hydration_break_window(match: dict) -> bool:
    clock = match.get("clock", "0:00")
    try:
        parts   = clock.split(":")
        minutes = int(parts[0])
        period  = match.get("period", 1)
        if period == 1 and 24 <= minutes <= 31:
            return True
        if period == 2 and 69 <= minutes <= 76:
            return True
    except Exception:
        pass
    return False


def get_standings() -> dict:
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}
