# KIMI BUILD PROMPT — DAEMON-POLY TRADING BOT
# Copy and paste this entire prompt to Kimi

---

Build me a fully autonomous golf prediction market trading bot with the following exact specifications. Do not deviate from the strategy rules. Do not summarize or simplify the pre-flight gates. Implement everything exactly as written.

---

## WHAT TO BUILD

A Python bot that:
1. Monitors live golf leaderboards via Slash Golf API (RapidAPI)
2. Monitors live Polymarket odds via Polymarket CLOB API
3. Runs signal analysis via Claude API (claude-sonnet-4-6)
4. Executes trades automatically on Polymarket when pre-flight checks pass
5. Communicates all alerts, signals, reports, and confirmations via Telegram
6. Runs 24/7 on Railway.app as a background worker

---

## DEPLOYMENT TARGET

Railway.app — deploy as a Python background worker. No web server needed. Just a continuous polling loop. Use environment variables for all credentials (never hardcode keys).

---

## REQUIRED ENVIRONMENT VARIABLES

SLASH_GOLF_KEY         → RapidAPI key for Slash Golf live golf data
TELEGRAM_TOKEN         → Telegram bot token from BotFather
TELEGRAM_CHAT_ID       → 8486909237
POLYMARKET_API_KEY     → Polymarket CLOB API key
POLYMARKET_PK          → Polymarket wallet private key
POLYMARKET_WALLET      → Polymarket wallet address
ANTHROPIC_API_KEY      → Claude API key
SESSION_BANKROLL       → Starting bankroll in USD

---

## API INTEGRATIONS

### Slash Golf (via RapidAPI)
Host: live-golf-data.p.rapidapi.com
Endpoints needed:
- /leaderboard — live leaderboard, positions, scores, thru
- /scorecard — hole-by-hole data for specific players
Poll every 15 minutes during active rounds only.

### Polymarket CLOB API
Host: https://clob.polymarket.com
Endpoints needed:
- GET /markets — search active US Open Winner markets, pull current YES prices per player
- POST /order — place market order (YES or SELL)
Poll odds every 10 minutes during active rounds only.

### Claude API
Model: claude-sonnet-4-6
Max tokens: 600
Used for: signal scan analysis when SCAN is triggered
Pass full leaderboard + odds + active positions as user message
System prompt is provided below in SYSTEM PROMPT section.

### Telegram Bot API
Used for: receiving operator commands, sending all alerts and reports
Use long polling (getUpdates) in a background thread
Send plain text only — no markdown, no HTML. Mobile-readable.
Confirm every operator command with one line before acting.

---

## PRE-FLIGHT GATES (HARD STOPS — ALL MUST PASS OR TRADE IS BLOCKED)

Implement these as a function that returns (bool, reason_string).
Every single trade must pass ALL gates before any API call to Polymarket.

PF-01: Round gate — current round must be R2 or later. R4 requires edge >15%.
PF-02: Position size gate — trade size must be ≤5% of current bankroll. This is the most important gate. Never allow a trade that exceeds 5% of bankroll.
PF-03: Position count — fewer than 4 open positions total
PF-04: Stack gate — player has fewer than 2 open positions already
PF-05: Leaderboard gate — player within 6 shots of lead (4 shots in R4)
PF-06: Cooldown gate — no active cooldown (triggered by 3 consecutive losses)
PF-07: Data freshness — leaderboard data must be under 2 hours old
PF-08: Confidence gate — Claude signal confidence must be High or Medium. Never Low.
PF-09: Loss position gate — if operator already has a position on this player and it is down more than 30% from entry price, block any additional entries on that player
PF-10: Saturday cap — maximum 2 trades total on Saturday (R3). Bot tracks Saturday trade count and blocks any trade after count reaches 2.

If any gate fails: log the failure, send Telegram alert with reason, do not place the order, move to next candidate.

---

## TRADE SIZING

Calculate size based on edge and confidence AFTER all pre-flight gates pass.
Size is always a percentage of current bankroll.
Cap at 5% regardless of calculation result (PF-02 enforces this).

High confidence + edge >15% = 5% of bankroll
High confidence + edge 8-15% = 3% of bankroll
Medium confidence + edge >12% = 2% of bankroll
Medium confidence + edge 8-12% = 1% of bankroll

Also enforce total exposure cap: sum of all open position sizes must not exceed 10% of bankroll. If adding the new trade would breach this, reduce size to fit or block entirely.

---

## TOURNAMENT SCHEDULE (US Open reference — update tournament ID per event)

R1 Thursday — monitor only, zero trades, send leaderboard summary when all groups show F
R2 Friday — first entry window. Trigger Scan 1 when Slash Golf shows all groups as F. Scan 2 at T+35 minutes. Run profit cycle check after Scan 2.
R3 Saturday — pre-round check at 8am ET on open positions. Scan 1 when all groups F. Scan 2 at T+35 minutes. Saturday night at 9pm ET: R4 preview, exit any position on player outside 4-shot gate.
R4 Sunday — pre-round check 8am ET. Mid-round scans at 11am, 2pm, 5pm ET. Final scan at 6pm ET when leaders on 15-18. Full session summary after last group holes out.

All scan times should trigger based on Slash Golf showing all groups as F, not fixed clock times, except the mid-round R4 monitors.

---

## LIVE MOVEMENT TRIGGERS (Active During R2, R3, R4 Only)

Run these checks every time a new poll completes. Compare to previous snapshot.
Maximum 3 unscheduled scans per hour — queue extras and batch at next 20-minute mark.

T1 ODDS COLLAPSE: Any open YES position drops 8+ percentage points in one 10-minute polling window → send EXIT EVALUATION alert to Telegram, run unscheduled scan

T2 MISPRICING: Any player gains 2+ leaderboard positions AND their Polymarket odds have moved less than 3 percentage points upward in same window → run immediate unscheduled scan, execute if edge clears pre-flight

T3 LEADER SEPARATION: Any player moves 3+ shots clear of field AND priced below 35% → flag and scan

T4 PROFIT TARGET: Any open YES position increases 50%+ from entry price → send EXIT HALF / EXIT FULL / HOLD alert to Telegram. EXIT HALF sells 50% of position at market price.

T5 CONDITIONS SHIFT: Field scoring average swings 1.5+ strokes in one hour → pause all unscheduled entries, send alert, wait for next scheduled scan

Always send a one-line Telegram confirmation before acting on any trigger.

---

## PROFIT CYCLING (Between Rounds Only — Never Mid-Round)

Triggers at round break when CYCLE PROFIT command is sent or autopilot auto-triggers it.

Step 1: Calculate realized profit = sum of all settled winning positions minus settled losses this session. If realized profit is zero or negative, send NO PROFIT TO CYCLE and stop.

Step 2: Lock 50% of realized profit permanently. Add to banked_profit state variable. This amount is never re-risked.

Step 3: Remaining 50% becomes cycle_pool for this round only.

Step 4: Screen candidates for cycle pool using these requirements (stricter than standard):
- Edge must be >10% (not the standard 8%)
- Confidence must be High only (not Medium)
- Must be a NEW position — never add to existing open position
- Player must be within shot gate (6 shots standard, 4 shots R4)
- Position must not already be blocked by PF-09

If no candidate qualifies: hold cycle pool, send CYCLE POOL HELD alert, carry to next round break.

Step 5: If locked bank total does not grow for 2 consecutive round breaks, suspend cycling for rest of tournament. Send CYCLING PAUSED alert.

---

## AUTO-SUSPENSION TRIGGERS

Immediately suspend all trade execution and alert Telegram if:
- Bankroll drops more than 20% from session starting value
- 3 consecutive filled trades result in losses (triggers cooldown — PF-06)
- Saturday trade count reaches 2 (PF-10)
- Polymarket API returns auth error or rate limit
- Leaderboard data becomes stale (>2 hours since last successful fetch)
- Next trade would push total open exposure above 10% of bankroll

Operator must send AUTOPILOT: RESUME to restart after any suspension.

---

## OPERATOR TELEGRAM COMMANDS

Parse these from incoming Telegram messages. Confirm each with one line before acting.

AUTOPILOT: ON          → activate trade execution
AUTOPILOT: OFF         → signal mode only, no trades placed
AUTOPILOT: RESUME      → restart after any suspension or cooldown
STATUS                 → send current positions, exposure, P&L, bankroll
SCAN                   → run manual signal scan immediately
BANKROLL: $[X]         → set or update session bankroll
COOLDOWN: RESET        → manually clear cooldown lock
CYCLE PROFIT           → manually trigger profit cycling at round break
CYCLE: OFF             → disable profit cycling for rest of session
EXIT HALF [player]     → sell 50% of named player's position at market price
EXIT FULL [player]     → close 100% of named player's position at market price

---

## SYSTEM PROMPT FOR CLAUDE (pass as system parameter on every API call)

You are a golf prediction market trading analyst. Evaluate live tournament data and generate trade signals for Polymarket golf markets. Output must be concise and Telegram-readable — plain text, no markdown.

CORE STRATEGY:
Entry mid-tournament only (R2-R3). Never pre-tournament. Target players 3-6 shots off lead with ascending trajectory. YES positions where market probability lags performance. Avoid leaders priced above 40%. Max 3-4 active positions. Never stack more than 2 on same player.

EDGE THRESHOLDS:
Fair value exceeds market by 5 cents or more: STRONG BUY
Fair value exceeds market by 2-5 cents: BUY
Within 2 cents either way: HOLD
Market exceeds fair value by 2-5 cents: SELL
Market exceeds fair value by 5 cents or more: STRONG SELL

COURSE EDGE (update per tournament):
At US Open / Shinnecock-style courses: fade bombers, reward accuracy and low ball-flight. SG:APP is the number one differentiator. Public over-bets favorites and Grand Slam narratives. R2 movers gaining 3+ shots still outside top 10 are primary targets. R4 field historically goes over par.

FOR EACH PLAYER OUTPUT EXACTLY THIS FORMAT:
PLAYER: [Name]
MARKET: [%]
FAIR VALUE: [%]
EDGE: [+/- %]
SIGNAL: [ENTER / HOLD / AVOID]
CONFIDENCE: [High / Medium / Low]
WHY: [1 sentence max]

Rank by edge descending. Mark top pick with a star symbol.
If no position has edge above 8 percent: output NO TRADE — WAIT
R4 rule: only signal ENTER if edge above 15% and player within 4 shots of lead.
Respond concisely. No filler. No explanations beyond the format above.

---

## TELEGRAM REPORT FORMATS

### Autopilot Cycle Report (send after every execution cycle)
--- CYCLE REPORT ---
[TIME] | R[X] | Bankroll: $[AMT]

EXECUTED:
[Player] YES $[SIZE] @ [%] | ID: [ORDER_ID]

BLOCKED:
[Player] — PF-[XX] failed: [reason]

OPEN ([X]/4):
[Player] in @ [%] now [%] P&L: [+/-$]

EXPOSURE: $[AMT] ([X]% of bankroll)
Saturday trades used: [X]/2
Banked profit: $[AMT]
--------------------

### Session Final Report (send after R4 completes)
--- FINAL REPORT ---
Starting Bankroll: $[AMT]
Ending Bankroll: $[AMT]
Banked Profit: $[AMT]
Total P&L: $[+/-AMT]
Win Rate: [X]% ([W]W/[L]L)
Trades Placed: [X]
Avg Edge on Entries: [%]
Best Trade: [Player] +$[AMT]
Worst Trade: [Player] -$[AMT]
--------------------

---

## STATE VARIABLES TO TRACK

autopilot: bool
bankroll: float
starting_bankroll: float
open_positions: list of dicts (player, market_id, side, size, entry_pct, order_id, round_entered)
closed_positions: list of dicts (same + pnl, exit_pct)
banked_profit: float
cycle_pool: float
consecutive_losses: int
cooldown_active: bool
cycling_active: bool
cycle_no_growth_count: int
current_round: int (1-4)
saturday_trade_count: int (reset each Saturday, hard cap at 2)
last_leaderboard: dict
last_odds: dict
prev_leaderboard: dict (for movement trigger comparison)
prev_odds: dict
suspended: bool
data_last_fetched: datetime
total_wins: int
total_losses: int

---

## BUG HANDLING

If any API call fails: log the error, send brief Telegram alert, do not retry more than 2 times, continue to next scheduled action. Never crash the main loop on a single API failure. Wrap all external calls in try/except.

If Polymarket order returns an error: log it, alert Telegram, do not retry automatically, flag for operator review.

If Claude API fails: log it, send CLAUDE API ERROR to Telegram, skip that scan, resume on next scheduled trigger.

---

## DELIVERABLES FROM KIMI

1. Complete working Python bot (single file or organized package)
2. requirements.txt with all dependencies
3. railway.toml or Procfile configured for Railway deployment
4. README with exact Railway setup steps and environment variable list
5. Test the Telegram connection and confirm bot sends startup message before delivering

