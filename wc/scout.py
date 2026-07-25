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


def _team_record(competitor: dict) -> str:
    """Overall W-L record string from competitor.records, e.g. '60-43'."""
    recs = competitor.get("records", []) if competitor else []
    return recs[0].get("summary", "") if recs else ""


def _team_stats(competitor: dict) -> dict:
    """Key in-game stats dict from competitor.statistics."""
    return {
        x["name"]: x.get("displayValue")
        for x in (competitor.get("statistics", []) if competitor else [])
        if x.get("name") in ("hits", "runs", "ERA", "avg", "errors",
                              "assists", "rebounds", "fieldGoalsAttempted", "pointsInPaint")
    }


def _parse_event(event: dict, sport_key: str) -> dict:
    status      = event.get("status", {})
    comp        = event.get("competitions", [{}])[0]
    competitors = comp.get("competitors", [])
    situation   = comp.get("situation", {})

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    # Tennis (and some feeds) carry no homeAway flags — fall back to order.
    if home is None or away is None:
        home = home or (competitors[0] if len(competitors) > 0 else {})
        away = away or (competitors[1] if len(competitors) > 1 else {})

    result = {
        "sport":         sport_key,
        "id":            event.get("id"),
        "name":          event.get("name") or event.get("shortName", ""),
        "date":          event.get("date"),
        "status_type":   status.get("type", {}).get("name", "unknown"),
        "status_detail": status.get("type", {}).get("detail", ""),
        "clock":         status.get("displayClock", "0:00"),
        "period":        status.get("period", 0),
        "home_team":     _competitor_name(home),
        "home_record":   _team_record(home),
        "home_score":    (home.get("score", "0") if home else "0"),
        "home_stats":    _team_stats(home),
        "away_team":     _competitor_name(away),
        "away_record":   _team_record(away),
        "away_score":    (away.get("score", "0") if away else "0"),
        "away_stats":    _team_stats(away),
        "venue":         comp.get("venue", {}).get("fullName", ""),
    }

    # Baseball in-play: include base/out/count state so SIGNAL can assess
    # leverage index (how much this at-bat matters for win probability).
    if situation:
        pitcher_name = (situation.get("pitcher") or {}).get("athlete", {}).get("fullName", "")
        batter_name  = (situation.get("batter")  or {}).get("athlete", {}).get("fullName", "")
        result["situation"] = {
            "balls":     situation.get("balls"),
            "strikes":   situation.get("strikes"),
            "outs":      situation.get("outs"),
            "on_first":  situation.get("onFirst",  False),
            "on_second": situation.get("onSecond", False),
            "on_third":  situation.get("onThird",  False),
            "pitcher":   pitcher_name,
            "batter":    batter_name,
            "last_play": (situation.get("lastPlay") or {}).get("text", ""),
        }

    return result


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


def get_standings(sport_cfg: dict) -> str:
    """Fetch current division standings for a sport.

    Returns a compact text block injected into the signal prompt so the AI has
    win%, run/point differential, and playoff odds — the data it needs to spot
    futures mispricings. Returns '' if the sport has no standings_path or the
    fetch fails (never raises)."""
    path = sport_cfg.get("standings_path")
    if not path:
        return ""
    try:
        url  = f"https://site.web.api.espn.com/apis/v2/sports/{path}/standings?seasontype=2"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        d    = resp.json()
        rows = []
        for group in d.get("children", []):
            div_name = group.get("name", "")
            for entry in group.get("standings", {}).get("entries", []):
                t     = entry.get("team", {})
                stats = {x["name"]: x.get("displayValue") for x in entry.get("stats", [])}
                rows.append({
                    "div":        div_name,
                    "team":       t.get("displayName", "?"),
                    "record":     stats.get("overall", "?"),
                    "win_pct":    stats.get("winPercent", "?"),
                    "diff":       stats.get("pointDifferential") or stats.get("differential", "?"),
                    "last10":     stats.get("Last Ten Games", ""),
                    "playoff":    stats.get("playoffPercent", ""),
                    "seed":       stats.get("playoffSeed", ""),
                })
        if not rows:
            return ""
        lines = [f"STANDINGS ({sport_cfg.get('label', path)}) — fetched live:"]
        cur_div = None
        for r in rows:
            if r["div"] != cur_div:
                cur_div = r["div"]
                lines.append(f"  [{cur_div}]")
            pct  = f" playoff:{r['playoff']}" if r.get("playoff") else ""
            l10  = f" L10:{r['last10']}"      if r.get("last10")  else ""
            lines.append(
                f"    #{r['seed']:>2} {r['team']:<28} {r['record']:<7} "
                f"({r['win_pct']}) diff:{r['diff']}{l10}{pct}"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[scout] standings error {sport_cfg.get('key')}: {e}")
        return ""


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
        # Hydration-break windows + buffer: market often lags during these pauses.
        if period == 1 and 22 <= minutes <= 34:
            return True
        if period == 2 and 67 <= minutes <= 78:
            return True
        # Late drama window: red cards, penalties, desperate attack in final 10 min.
        if period == 2 and minutes >= 83:
            return True
        return False

    if kind == "baseball":
        return period >= 7            # 7th inning or later

    if kind == "basketball":
        return period >= 4            # 4th quarter / overtime

    if kind == "tennis":
        return match.get("status_type") == "STATUS_IN_PROGRESS"

    return False
