# DÆMON-KALSHI

Fork of DÆMON-POLY's six-worker architecture, rebuilt for Kalshi's CFTC-regulated
market surface instead of Polymarket. Same operating philosophy — Scout finds edge,
Maker proposes, Checker verifies, Risk Guardrail gates, Execution fires, Ledger
remembers — adapted for Kalshi's RSA-signed REST/WebSocket API and its much wider
market catalog (golf, sports, crypto, econ, weather, etc. all live under one venue).

## Why this isn't a copy-paste of DÆMON-POLY

Kalshi and Polymarket differ in ways that touch almost every worker:

| | Polymarket | Kalshi |
|---|---|---|
| Auth | Wallet signing (EIP-712) across 3 APIs (Gamma/CLOB/Data) | Single unified REST+WS API, RSA-PSS signature per request |
| Settlement | USDC on-chain | Fiat, CFTC-regulated |
| Pricing | Cents, integer | Dollar strings, sub-penny ticks on some markets |
| Rate limits | Per-endpoint | Tiered: Basic 20r/10w per sec → Prime 400/400 (volume-gated) |
| Market ID | Condition ID / token ID | Ticker strings like `KXBTC-26MAR14-100000` |

This build hand-rolls the Kalshi client rather than depending on the official
`kalshi_python_sync`/`kalshi_python_async` SDKs. Kalshi's own docs recommend this
for production use — the SDKs are auto-generated from spec and can lag. RSA-PSS
signing is ~30 lines; owning it means no dependency risk on money-moving code.

## What I could and couldn't verify from here

I have no network access in this build environment and no GitHub connector
connected in this chat, so:
- **Not tested live.** Every request-signing and order-placement code path is
  written to Kalshi's documented spec, but has not been run against the demo
  or production API. Run it against `demo-api.kalshi.co` first.
- **PF-04 / PF-09 / PF-10 are stubs.** I don't have the actual rule logic from
  your DÆMON-POLY repo in this conversation — only the names. `risk_guardrail.py`
  has placeholder implementations with docstrings describing what a rule *named*
  that typically does in this system. Replace with your real logic, or paste it
  to me and I'll port it exactly.
- **Kimi/Moonshot Maker + Claude Checker prompts are scaffolded, not tuned.**
  The actual edge-detection prompts you refined for golf/sports on Polymarket
  aren't in my memory in enough detail to reproduce verbatim — I've built the
  calling infrastructure and a starting prompt; you'll want to port your tuned
  prompts over.
- **ESPN event matching is a heuristic, not solved.** `context.py` guesses
  which ESPN tournament/game corresponds to a Kalshi market from keywords in
  the title — there's no shared ID between the two systems to join on
  cleanly. Good enough to get a leaderboard/scoreboard in front of Maker;
  needs a real tuning pass once you're looking at live concurrent tickers.
- **Weather station mapping covers ~17 cities, verify before trusting.**
  `weather_client.py` hardcodes city -> NWS station (the gotchas that matter:
  Chicago settles on Midway not O'Hare, Houston on Hobby not Bush, NYC on
  Central Park not LaGuardia). Sourced from public trading-guide references,
  not Kalshi's own docs directly — cross-check each city against the
  specific market's stated settlement source before trusting the mapping
  with size.
- **The Kalshi category is called "Climate", not "Weather".** Fixed after
  seeing real screenshots of the app (tabs read Commodities / Finance /
  Climate / Tech and Science) — an earlier version of every config default
  in this repo said "Weather" and would have silently filtered out every
  climate-category market with no error to notice it by. Also fixed:
  market titles abbreviate cities ("LA", "NYC") rather than spelling them
  out, which the city-matching logic didn't originally handle either. If
  you spot other categories in the app that don't match what's hardcoded
  here (Elections vs. Politics, for instance), assume the screenshot is
  right and this repo is wrong until proven otherwise — that's now happened
  twice from the same root cause: guessed category names instead of
  verified ones.
- **FRED gives you the last published number, not a forecast.** Useful as
  context for Maker ("here's the current trend"), not as a prediction of an
  unreleased CPI/jobs print — don't mistake it for more than that.

## How "getting smarter" actually works here

Two separate mechanisms, don't conflate them:

1. **QuantMaker** (`workers/quant_maker.py`) handles high-frequency numeric
   markets — 15-min/hourly crypto and commodities — with a digital-option
   probability model instead of an LLM call per trade. This is what makes
   markets like Kalshi's 15-minute GLD/SLV/BTC contracts actually tradeable;
   an LLM reasoning call is too slow and too expensive for something that
   resolves in 15 minutes, and there's nothing to reason about — it's a
   math problem (spot price, time to expiry, volatility), not an
   information problem. It gets *literally* smarter as the bot runs longer,
   since its volatility estimate is built from an in-memory rolling price
   history that starts empty and improves with more observations — this is
   the one part of "smarter" that's a real statistical improvement, not a
   metaphor.
2. **Reflection** (`workers/reflect.py`) runs periodically (every N passes,
   configurable), hands Claude a batch of settled trades — reasoning and
   outcomes both — and asks for concrete patterns. That gets saved to
   `playbook.md` and prepended to future Maker/Checker prompts. Be honest
   with yourself about what this is: it changes what the LLMs are told, not
   the models themselves. It's a real mechanism, not theater, but it can
   also drift or overfit to a short losing streak the same way a person
   can — worth glancing at the playbook occasionally rather than trusting
   it to compound unsupervised forever.

Calibration tracking (`edge_store.calibration_by_category`) now computes
Brier score, not just PnL, split by *(category, source)* — so "did DÆMON
lose money" and "was DÆMON's stated probability actually honest" are
answered separately. A category can win money on a lucky trade with a bad
probability estimate; Brier score can't be fooled that way, which is why
PF-09 checks both.

## Don't trade markets you have no edge in

It's tempting to point Scout at every category Kalshi offers — more markets,
more chances to find edge. Resist that. A market only belongs in rotation if
one of two things is true: QuantMaker has a real live price feed for it, or
Maker has actual grounding data / genuine qualitative-reasoning advantage
(ESPN, NOAA, FRED, or domain knowledge like golf). Otherwise an LLM call on
it isn't a smaller edge, it's speculation with extra steps and real fees.

`main.py`'s routing enforces this: candidates that fail both
`QuantMaker.can_handle()` and `category in LLM_REASONING_CATEGORIES` get
skipped outright, logged but never sent to Maker. Kalshi's Commodities/Tech
markets (NVIDIA H100/A100 compute pricing, for example) are the clearest
case — there's no free live feed for GPU rental rates, and no principled
reasoning edge either, so they're excluded by default rather than getting
an LLM's best guess dressed up as an estimate.

`PRIORITY_KEYWORDS` (default: `golf,pga`) makes sure the proven category
always gets processed first each pass and is exempt from
`MAX_LLM_CALLS_PER_PASS` — so as you cautiously add categories, they can't
crowd out or rate-limit-starve the one with an actual track record.

## Research-backed additions (from Jonathan Becker's Kalshi microstructure analysis)

[github.com/Jon-Becker/prediction-market-analysis](https://github.com/Jon-Becker/prediction-market-analysis)
isn't a bot — it's a data-collection/analysis framework plus the largest
public dataset of Kalshi + Polymarket trades. Two things from it are worth
knowing about even though this repo doesn't (yet) directly depend on it:

1. **A real, verified Kalshi category taxonomy.** Its
   `src/analysis/kalshi/util/categories.py` module (`get_group()`,
   `get_hierarchy()`) maps actual Kalshi tickers to group/category/
   subcategory, built from real collected data — not the guessed strings
   this repo has had to correct twice already (Weather→Climate, the
   Commodities/Crypto split). I haven't vendored it in because I only saw
   its documented interface, not its actual source — pulling in someone
   else's file should mean reading their LICENSE first and grabbing the
   real content, not me reconstructing a ticker mapping I don't actually
   have. Worth doing via Claude Code once you have real repo access.
2. **A 72.1M-trade empirical basis for two changes already made here:**
   `risk_guardrail.py`'s longshot bias guard and `execution.py`'s
   maker/taker order strategy, both described above, come directly from
   this analysis rather than being guessed. The headline numbers: contracts
   priced under 20¢ systematically underperform their implied odds (a 5¢
   contract won 4.18% of the time, not 5%), while liquidity takers lost
   ~1.12% on average per trade to makers. Effect size varies a lot by
   category — Finance is close to efficient, World Events and Media show
   much larger gaps — which is one more argument for letting PF-09's
   calibration tracking, not assumption, decide which categories earn a
   bigger allocation over time.

The same repo's pre-collected historical dataset is also the obvious next
step before trusting any of this with real size: backtest QuantMaker's
pricing model and Maker/Checker's calibration against real historical Kalshi
trades before going live, rather than finding out the hard way. Not built
here — this repo has no historical data or backtest harness of its own yet
— but it's a natural next request if you want it.

## Repo layout

```
daemon-kalshi/
  core/
    kalshi_client.py     # REST client: RSA-PSS signing, rate-limit backoff
    account_state.py      # reconciled exposure: balance, positions, orders, fills
    order_state.py         # order lifecycle states + deterministic client order IDs
    kalshi_ws.py            # WebSocket: orderbook snapshot+delta, seq tracking, reconnect
    espn_client.py          # Free, keyless ESPN data (github.com/pseudo-r/Public-ESPN-API)
    weather_client.py        # Free, keyless NOAA/NWS data, mapped to Kalshi's settlement stations
    fred_client.py             # Free-key Federal Reserve economic data
    spot_price_client.py        # Free crypto (CoinGecko) + ETF (Yahoo) spot prices
  workers/
    scout.py               # market discovery via Kalshi's own category field
    maker.py                # Kimi/Moonshot signal proposer (+ ESPN context, + playbook)
    quant_maker.py            # Fast no-LLM digital-option pricing for 15-min/hourly numeric markets
    checker.py                 # Claude Sonnet second-opinion verifier
    risk_guardrail.py           # worst-case dollar exposure caps + kill switch
    execution.py                 # idempotent submission + fill reconciliation
    ledger.py                     # trade log, P&L, edge memory writeback
    context.py                     # ESPN/NOAA/FRED grounding for candidates
    reflect.py                      # turns settled trades into an evolving playbook
  memory/
    db.py                   # shared SQLite connection handling (WAL, busy timeout)
    edge_store.py            # SQLite-backed memory of past edges + outcomes
    order_store.py            # durable orders, fills and settlements
  tests/                     # pytest suite — see docs/SAFETY.md
  docs/SAFETY.md            # safety model, risk formulas, what is NOT verified
  main.py                    # orchestrator loop
  config.py                  # env-driven config
  requirements.txt
  requirements-dev.txt
  railway.toml
  Procfile
  .env.example
```

## Safety model

Read [`docs/SAFETY.md`](docs/SAFETY.md) before pointing this at a production
key. It covers the signal → order → fill → settlement state machine, the exact
risk formulas, the fail-closed conditions, and — most importantly — the list
of Kalshi API assumptions that have **not** been verified against a live
endpoint.

`DRY_RUN` defaults to `true`; a fresh checkout places no real orders. Run the
tests with:

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

## Setup

1. `openssl genrsa -out kalshi_private_key.pem 2048` (or reuse the key you
   already generated) — upload the matching public key in Kalshi > Settings >
   API Keys, get back a Key ID (UUID).
2. Copy `.env.example` to `.env`, fill in `KALSHI_API_KEY_ID`,
   `KALSHI_PRIVATE_KEY_PATH` (or `KALSHI_PRIVATE_KEY_PEM` for Railway, since
   Railway env vars are easier as a single string than a mounted file),
   `MOONSHOT_API_KEY`, `ANTHROPIC_API_KEY`.
3. `pip install -r requirements.txt`
4. Point `KALSHI_ENV=demo` first and run `python main.py --dry-run` against
   `demo-api.kalshi.co` before touching production.
5. Push to a new GitHub repo, connect it in Railway the same way
   `daemon-poly-wc` is connected, set the env vars in the Railway dashboard
   (never commit the private key), deploy.
6. **Attach a Railway Volume before you trust the memory system.** Railway's
   default filesystem is ephemeral — it's wiped on every redeploy. Without a
   Volume, every code push silently resets your entire edge memory, which
   defeats the point of PF-09's calibration check. One-time setup: in the
   Railway dashboard, open the service → Settings → Volumes → New Volume →
   mount path `/data`. `LEDGER_DB_PATH` already defaults to `/data/daemon_kalshi.db`
   to match. Redeploy once after adding it.

## Next steps once you push this

Tell me the repo name once it exists on GitHub and I'll take it from there on
the Railway side — connect the service, set build/start commands, and manage
redeploys through the Railway tools I already have.
