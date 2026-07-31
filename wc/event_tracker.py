"""Automatic tournament/event discovery and switching for DÆMON-POLY.

Golf rotates through 30+ PGA Tour events per season — the config must update
automatically as each tournament concludes and the next begins.  This module:

  1. Detects when the currently tracked event is done (all ESPN entries final
     or the event name no longer appears on the scoreboard).
  2. Discovers the next active/upcoming PGA Tour event from ESPN.
  3. Searches Polymarket US for the corresponding winner market and slug.
  4. Mutates SPORT_CONFIGS["golf"] in-place with the new event details.
  5. Persists the override to state["event_overrides"] so a bot restart
     automatically resumes the correct tournament without re-discovering.
  6. Sends a Telegram notification on every switch.

Scope: event-selection logic ONLY.  Gates, caps, and signal thresholds are
not touched.
"""

import requests
from datetime import datetime, timezone
from typing import Optional

from gates import load_state, save_state, STATE_LOCK, reset_phase_for_event_change
from telegram_ops import send_status

ESPN_HOST = "https://site.api.espn.com/apis/site/v2/sports"

# How many consecutive cycles a PM slug search is retried before giving up
# and leaving golf in alert-only mode until the next tournament switch.
_PM_RETRY_MAX = 4
_pm_retry_counts: dict = {}   # sport_key → retry attempt count


# ── State persistence ────────────────────────────────────────────────────────

def _get_overrides() -> dict:
    return load_state().get("event_overrides", {})


def _save_override(sport_key: str, override: dict) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("event_overrides", {})[sport_key] = override
        save_state(state)


def apply_event_overrides(sport_configs: dict) -> None:
    """Mutate SPORT_CONFIGS in-place with any persisted event overrides.
    Call once at bot startup so it immediately resumes the last known event
    without waiting for the first discovery cycle."""
    for sport_key, override in _get_overrides().items():
        if sport_key in sport_configs:
            _apply_to_cfg(sport_configs[sport_key], override)
            print(f"[event_tracker] Resumed override for {sport_key}: "
                  f"{override.get('label', sport_key)}")


def _apply_to_cfg(cfg: dict, override: dict) -> None:
    """Apply override fields to a sport config dict in-place."""
    for field in ("label", "event_match", "us_search", "us_title_match", "futures"):
        if override.get(field) is not None:
            cfg[field] = override[field]


# ── ESPN golf scoreboard helpers ─────────────────────────────────────────────

def _fetch_golf_events() -> list:
    """Return all events currently on the ESPN PGA Tour scoreboard."""
    try:
        resp = requests.get(f"{ESPN_HOST}/golf/pga/scoreboard", timeout=12)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        print(f"[event_tracker] ESPN golf fetch error: {e}")
        return []


def _is_final(event: dict) -> bool:
    """True when an ESPN event is fully completed."""
    st = event.get("status", {}).get("type", {})
    return st.get("completed", False) or st.get("name", "") in (
        "STATUS_FINAL", "STATUS_FINAL_ATS", "STATUS_POSTPONED", "STATUS_CANCELED",
    )


def _event_sort_key(event: dict) -> int:
    """Sort priority: in-progress (0) > scheduled (1) > other non-final (2) > final (9)."""
    name = event.get("status", {}).get("type", {}).get("name", "")
    if name == "STATUS_IN_PROGRESS":
        return 0
    if name in ("STATUS_SCHEDULED", "STATUS_PRE_TIMED"):
        return 1
    if not _is_final(event):
        return 2
    return 9


def _event_name(event: dict) -> str:
    return (event.get("name") or event.get("shortName") or "").strip()


def _short_name(full: str) -> str:
    """Strip verbose suffixes to get a cleaner display name."""
    for suffix in (" Golf Tournament", " Invitational Classic", " Classic"):
        full = full.replace(suffix, "")
    return full.strip()


# ── Polymarket slug discovery ────────────────────────────────────────────────

def _find_pm_golf_market(tournament_name: str) -> tuple:
    """Search Polymarket US for a winner market for the given PGA tournament.

    Returns (us_search, us_title_match, futures_dict) where futures_dict maps
    "{name} Winner" → slug.  All empty when no matching market is found.
    """
    try:
        from pm_us import get_pm_client
        client = get_pm_client()
        if not client:
            return [], [], {}

        short = _short_name(tournament_name)
        # Build a few search queries from broadest to most specific
        queries = [
            f"{short} golf winner",
            f"{short} winner",
            short,
            tournament_name,
        ]
        seen = set()
        for q in queries:
            try:
                resp = client.search.query({"query": q})
            except Exception:
                continue
            events = resp.get("events", []) if isinstance(resp, dict) else []
            for ev in events:
                title = (ev.get("title") or "").strip()
                if title in seen:
                    continue
                seen.add(title)
                tl = title.lower()
                # Title must mention key words from the tournament AND "winner"/"champion"
                key_words = [w for w in short.lower().split() if len(w) > 3]
                if not key_words:
                    continue
                if not any(w in tl for w in key_words):
                    continue
                if "winner" not in tl and "champion" not in tl:
                    continue
                # Extract first open, active market slug
                for m in ev.get("markets", []):
                    if m.get("closed") or m.get("active") is False:
                        continue
                    slug = (m.get("slug") or "").strip()
                    if not slug:
                        continue
                    futures_key = f"{short} Winner"
                    return (
                        [short.lower()],
                        [title],
                        {futures_key: slug},
                    )
    except Exception as e:
        print(f"[event_tracker] PM slug search error '{tournament_name}': {e}")
    return [], [], {}


# ── Main entry-point: called each analysis cycle for golf ────────────────────

def check_and_update_golf(sport_cfg: dict) -> bool:
    """Detect if the tracked golf tournament is done and switch to the next one.

    Mutates sport_cfg in-place, persists to state, and notifies Telegram.
    Returns True if a tournament switch occurred this call.
    Safe to call every 3-12 min — returns immediately when no change is needed.
    """
    current_match = sport_cfg.get("event_match", [])
    all_events    = _fetch_golf_events()
    if not all_events:
        return False   # ESPN unavailable — leave config unchanged

    # ── Check 1: Is current tournament still active? ─────────────────────────
    if current_match:
        matching = [
            ev for ev in all_events
            if any(m.lower() in _event_name(ev).lower() for m in current_match)
        ]
        if matching and not all(_is_final(ev) for ev in matching):
            # Tournament is still running — check whether we're missing a PM slug
            # (happens when the bot switched before the market was listed).
            if not sport_cfg.get("futures"):
                _retry_pm_slug(sport_cfg, current_match[0] if current_match else "")
            return False   # No tournament switch needed

    # ── Check 2: Find next active/upcoming tournament ────────────────────────
    candidates = [ev for ev in all_events if _event_sort_key(ev) < 9]
    if not candidates:
        print("[event_tracker] Golf: no upcoming events on ESPN — off-season or between events")
        return False

    best     = min(candidates, key=_event_sort_key)
    new_name = _event_name(best)
    old_name = current_match[0] if current_match else sport_cfg.get("label", "?")

    if new_name.lower() == old_name.lower():
        # Same event, but all-final — might be a brief scoring delay; don't switch yet.
        return False

    print(f"[event_tracker] Golf: '{old_name}' done → switching to '{new_name}'")

    # ── Find Polymarket market for the new tournament ─────────────────────────
    us_search, us_title_match, futures = _find_pm_golf_market(new_name)
    short = _short_name(new_name)
    _pm_retry_counts["golf"] = 0  # reset retry counter on a new switch

    override = {
        "label":          f"Golf — {short}",
        "event_match":    [new_name],
        "us_search":      us_search      or [short.lower()],
        "us_title_match": us_title_match or [f"{short} Winner"],
        "futures":        futures,
    }

    old_label = sport_cfg.get("label", old_name)
    _apply_to_cfg(sport_cfg, override)
    _save_override("golf", override)

    # Auto-reset phase count for the new tournament — the old count
    # belongs to the previous event and must not carry forward to block
    # signal generation in the new one.  Keyed off the new label so
    # restarts never re-trigger the reset (epoch is persisted in state).
    reset_phase_for_event_change("golf", override["label"])

    pm_note = (
        f"Polymarket: {next(iter(futures.values()))} ✅"
        if futures else
        "⚠️ No PM market found yet — running alert-only; will retry automatically each cycle."
    )

    send_status(
        f"⛳ GOLF TOURNAMENT SWITCH\n"
        f"──────────────────────\n"
        f"Concluded: {old_label}\n"
        f"Now tracking: {override['label']}\n"
        f"ESPN: {new_name}\n"
        f"{pm_note}\n"
        f"Switched automatically — no operator action needed."
    )
    return True


def _retry_pm_slug(sport_cfg: dict, tournament_name: str) -> None:
    """Called when the tracked tournament is active but futures dict is empty
    (PM market wasn't live at switch time).  Retries slug search up to
    _PM_RETRY_MAX times before giving up until the next switch."""
    count = _pm_retry_counts.get("golf", 0)
    if count >= _PM_RETRY_MAX:
        return
    _pm_retry_counts["golf"] = count + 1

    us_search, us_title_match, futures = _find_pm_golf_market(tournament_name)
    if not futures:
        print(f"[event_tracker] Golf PM slug retry {count+1}/{_PM_RETRY_MAX}: "
              f"no market yet for '{tournament_name}'")
        return

    short    = _short_name(tournament_name)
    override = {
        "label":          f"Golf — {short}",
        "event_match":    [tournament_name],
        "us_search":      us_search,
        "us_title_match": us_title_match,
        "futures":        futures,
    }
    _apply_to_cfg(sport_cfg, override)
    _save_override("golf", override)
    _pm_retry_counts["golf"] = _PM_RETRY_MAX  # stop retrying — we found it

    slug = next(iter(futures.values()))
    print(f"[event_tracker] Golf PM slug found on retry: {slug}")
    send_status(
        f"⛳ Golf PM market connected: {sport_cfg.get('label')}\n"
        f"Slug: {slug}\n"
        f"Bot will now auto-execute on this market."
    )
