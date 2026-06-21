---
name: golf-data-sources
description: Live golf leaderboard data sources for DAEMON-POLY bot (ESPN primary, Claude fallback) and their traps
---

# Golf leaderboard data sources (DAEMON-POLY)

Primary source is the **ESPN public golf API** — free, no key, genuinely live (in-play
position, total + today to-par, holes thru, completion, cut). Slash Golf (RapidAPI) is
the legacy source but its monthly quota is exhausted.

**Why:** RapidAPI ran out mid-tournament; needed a reliable live feed for real-money R4 trading.

**How to apply:**
- Endpoint: `https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard`. The
  PGA-specific path (`/golf/pga/leaderboard`) returns 404 — use the generic golf path.
- Shape: `events[].competitions[0].competitors[]`; per player: `athlete.displayName`,
  `status.position.displayName` ("T6"), `score.displayValue` (total to par, "E"/"+5"),
  `linescores[]` (find entry where `period == competition status.period` for today's
  round score), `status.thru`, `status.type` (state "post"/`completed` ⇒ round done;
  name contains CUT/WD/DQ ⇒ cut).
- **Hard-require the exact event** ("u.s. open" in `events[].name`). NEVER fall back to
  `events[0]` or "any in-progress event" — that silently trades on the WRONG tournament.
  If not matched, raise → Claude fallback → `data_stale=True` (PF-07 blocks trading).
- Compute `all_groups_finished` from ESPN completion state (reliable), not from `thru=="F"`.

## Claude web-search fallback — known limitation
Claude (api.anthropic.com, web_search_20250305 tool) **cannot get live in-play scores**.
Web searches only surface **start-of-round standings**, and Claude often mislabels them
as `thru "F"` (would trigger false auto-settlement — so force `all_groups_finished=False`
in that path). Each web-search call burns ~52k input tokens. Use only as a degraded
fallback when ESPN fails; it will mostly sit idle rather than trade.
