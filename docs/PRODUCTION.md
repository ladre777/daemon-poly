# DÆMON-KALSHI — Production Readiness

Last updated: 2026-08-21

## What the research says actually makes money

From Becker's 72.1M-trade Kalshi study and live open-source bots:

| Edge type | Evidence | Our status |
|-----------|----------|------------|
| **Be the Maker, not the Taker** | Takers avg −1.12%, Makers +1.12% | Still taker-only (maker mode blocked until order lifecycle) |
| **Avoid longshots** | 1¢ contracts win ~0.43% vs 1% implied | Guard already on |
| **15m crypto microstructure** | Binance moves; Kalshi book lags 3–7s | Quant path exists; not a latency sniper |
| **Locked YES+NO arb** | Buy both when ask_yes+ask_no+fees < 100¢ | Detection only (`ARB_ENABLED`) |
| **Weather model lag** | NWS/ensemble update before market reprices | NOAA wired; error-band fix shipped |
| **Fee-aware net edge** | 4pp raw can be negative after fees | Already required |
| **Quarter-Kelly sizing** | Standard among careful bots | Already on |

Honest conclusion: pure directional LLM edges are the weakest of the above.
Our strongest *current* path is **short-dated crypto quant (KXBTC15M / hourly)**
plus **same-day weather**, paper-traded until calibration is positive.

## What we shipped 2026-08-21

1. Checker → Moonshot/Kimi (stop Claude burn)
2. Volatility spike refusal (no phantom edge after moves)
3. Weather forecast-error guidance (~1–2°F same-day, not 3–4°F)
4. Slash Golf wired into ContextEnricher

## Production checklist (do in order)

### A. Infra (must)
- [ ] Railway Volume mounted at `/data`
- [ ] `LEDGER_DB_PATH=/data/daemon_kalshi.db`
- [ ] `PERSIST_VOL_HISTORY=true`
- [ ] `KALSHI_ENV=demo` until paper funnel shows real approvals
- [ ] `DRY_RUN=true` until calibration Brier is better than base rate
- [ ] `MOONSHOT_API_KEY` set; `CHECKER_LLM_PROVIDER=moonshot`
- [ ] `SLASH_GOLF_API_KEY` set if trading golf
- [ ] `RTI_FEED_ENABLED=true` for crypto

### B. Scope (recommended)
```bash
SCOUT_CATEGORIES=Sports,Crypto,Weather
PRIORITY_KEYWORDS=golf,pga,btc,bitcoin,eth,high,temperature
PRIORITY_CATEGORIES=weather,crypto
LLM_REASONING_CATEGORIES=sports,weather
SCOUT_SPORTS_CATEGORIES=Golf
SKIP_MULTI_EVENT_SHARDS=true
```

### C. Go-live gate (do not skip)
Only flip `DRY_RUN=false` and `KALSHI_ENV=prod` when:
1. Pass funnel shows `approved > 0` in paper for several days
2. Forecast calibration (post-regime-split) is not worse than base rate
3. No kill-switch trips from bugs
4. Bankroll is money you can lose ($300 is fine; size stays small via Kelly)

### D. After first live fills
1. Keep daily loss cap at 10%
2. Do not enable maker mode until resting-order cancel/reconcile exists
3. Re-read Checker reject reasons weekly — do not lower confidence blindly

## Next engineering priorities (profit-oriented, fail-closed)

1. **Order lifecycle → maker mode** — structural Becker edge
2. **Atomic YES+NO arb execution** — only when both legs can fill
3. **Faster 15m crypto path** — RTI/spot lag signal inside the 15m window
4. **Weather normal-CDF path** — same math as quant crypto, NWS high + σ(horizon)
5. **Resolution near-certainty filter** — extreme prices near settle with confirmed data

## What we will not do

- Blindly lower `CHECKER_MIN_CONFIDENCE` or coherence gates
- Trade unverified gold/silver unit-mismatched families
- Run live without durable `/data` storage
- Claim X-style miracle equity curves without settled PnL
