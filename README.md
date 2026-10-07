# NIFTY Options Arbitrage Engine

Pure Python. Event-driven. Finds model-free arbitrage in the NIFTY option chain,
trades it, and sends a Telegram message for every trade taken.

```bash
./run.sh            # start in paper mode and open the dashboard
./run.sh status     # connectivity + universe check
./run.sh logs       # follow the session log
./run.sh stop
./run.sh test       # strategy correctness suite
```

`run.sh` will not start live mode; the 30-day trial is paper only. Live needs a
deliberate `venv/bin/python run_bot.py --mode live --yes`.

## Deploying on a VM

```bash
./deploy.sh install   # venv + requirements (first time), then fill in .angel.env
./deploy.sh start     # daemon under nohup: one paper session per trading day, forever
./deploy.sh status    # daemon, session process, dashboard, next session
./deploy.sh logs      # follow the daemon log; ./deploy.sh session follows today's
./deploy.sh stop      # graceful: the session writes its summary first
./deploy.sh update    # git pull, reinstall, restart
```

The daemon (`run_bot.py --daemon`, in `arb/daemon.py`) survives the SSH session
ending and follows the exchange clock (IST, regardless of the VM's timezone):

| IST | what happens |
|---|---|
| 09:05 | a fresh session process logs in, downloads the scrip master, builds the day's universe, subscribes, calibrates margin |
| **09:15:00** | scanning starts on the opening second (`--wait-open` sleeps to the exact instant) |
| **15:15:00** | scanning stops on the closing second; no trade fires after it |
| 15:30 (expiry days only) | the spot feed is held to pin the settlement level, then released |
| 15:15 → next 09:05 | the day's dashboard stays up for review; the process is replaced before the next open |

One process per day, always fresh: Angel One's tokens, the scrip master and
the strike window all belong to a single day. Weekends and the dates in
`config/holidays.txt` are skipped; an unlisted holiday costs nothing but a
session that sees no ticks on a connected socket and stops itself after thirty
minutes. A session process that dies mid-day is relaunched with back-off (the
login itself already rides out a network outage), and Telegram is told.
`deploy.sh` refuses live mode, like `run.sh`; the two also refuse to run at
the same time, since both want the dashboard port.

Restarts are safe. On start the engine re-reads today's journal and restores the
trade count, P&L, capital deployed and trade-id sequence, so the daily loss limit
and the per-day trade cap cannot be reset by a crash or a restart — and the
dashboard's "today" means the calendar day, not the process uptime.

It trades three indices. They are not interchangeable, and the code assumes
nothing shared between them:

| Index | Segment | Lot | Step | Expiries | Futures |
|---|---|---|---|---|---|
| NIFTY | NFO (NSE) | 65 | 50 | weekly Tuesday | monthly only |
| BANKNIFTY | NFO (NSE) | 30 | 100 | monthly only | **every expiry** |
| SENSEX | BFO (BSE) | 20 | 100 | weekly Thursday | monthly only |

BANKNIFTY is the one worth watching for put-call parity: every option expiry has
a matching future, so the strategy is live on all its chains, where NIFTY only
gets it on the monthly. SENSEX orders route to BFO, not NFO - each leg carries its
own exchange.

The dashboard is a text-mode trading terminal, in the idiom of a DOS-era market
screen: an IBM VGA 9×16 bitmap font on a deep-blue field, the CGA/EGA sixteen-
colour palette, and a one-character grid so everything stays pixel-crisp at
integer zoom. Nine numbered panels tile the screen — **CHAIN** (the option-chain
ladder, CE │ strike │ PE with per-quote ages and the futures basis), **DISTANCE
TO PROFITABLE**, **SCAN**, **EXECUTIONS**, **MARKET**, **RATES / HEALTH**,
**SIGNALS / LOG**, **ENGINE**, and **P&L / CAPITAL**. Press `1`–`9` to blow a
panel up full-screen, `0` for the grid, `L` for a portrait stack, `F` to filter
to one index, `+`/`-` to zoom, `C` for the old page. Green is bid, red is ask,
yellow marks our resting quotes and stale ages — the same colour grammar the
book uses. The look is lifted from the `HFTENGINE/` reference dashboard and
re-pointed from crypto to the NSE/BSE index options this engine trades; the
bitmap fonts are served locally from `arb/static/`, so it renders identically
with no network. The previous Wikipedia-idiom page is kept at **`/classic`**,
where the deep analytics live — the mathematics write-ups, the month of paper-run
history, and the density/CDF charts.

While it runs, the live dashboard is at **http://127.0.0.1:8088** (`--port` to
move it, `--no-dashboard` to skip it). It shows spot, tick rate, scan latency,
trades with their legs, the live chain with per-quote ages, and the panel that
matters most - **distance to profitable**: the best violation currently on screen
for each strategy against what that structure costs to trade. When the engine is
quiet, that panel tells you whether the market was two rupees away or nowhere
near. It reads engine state from outside the scan loop and never touches the hot
path.

---

## Why the previous bot found arbitrage that was not there

`angel_arb_bot.py` reported 154 winning trades in one session. None of them were
real, for four independent reasons:

1. **It never saw an order book.** It polled `ltpData()` and *invented* the
   quotes: `spread = max(0.50, ltp * 0.003)`, `bid = ltp - spread/2`. LTP is the
   last *trade*, which sits on one side of the book, so these "mids" were offset
   by half a spread in a random direction per contract.
2. **Legs were priced seconds apart.** `time.sleep(0.1)` per contract plus
   `time.sleep(10)` per cycle meant a 4-leg box was assembled from prices taken
   at four different moments while the index moved.
3. **Put-call parity had no futures leg** and priced against `futures = spot + 35.0`,
   a hardcoded constant. A 2-leg synthetic booked as arbitrage is a naked
   directional position.
4. **Fills were assumed.** The theoretical edge was added straight to cash. No
   depth check, no slippage, no partial fills.

`tests/test_strategies.py` case 11 reproduces exactly that method against a chain
that is arbitrage-free by construction. It reports **76 opportunities worth
₹24,349 that do not exist**. The same chain, quoted honestly, yields nothing.

---

## What this engine does instead

| | Old | New |
|---|---|---|
| Quotes | LTP ± 0.3%, invented | Real best-5 book, `SNAP_QUOTE` websocket |
| Data flow | REST poll every 10 s | Push; scan wakes on the tick |
| Leg synchronisation | none | Every leg's age checked; stale structures refused |
| Detection | Python loops over strike pairs | Vectorised numpy, **109 µs** for all four strategies |
| Put-call parity | 2 legs vs `spot + 35` | 3 legs vs the real matched-expiry future |
| Discounting | one repo guess both ways | borrow/lend band, centred on the market's own implied rate |
| Depth | ignored | every leg must show size ≥ our own |
| Fills | assumed | modelled; partial fills unwind and book the loss |
| Threshold | "4% ROI" | measured from recorded book data |

---

## Strategies

All four are **model-free**: their payoff is bounded by arithmetic, not by
Black-Scholes. No implied vol, no fair-value estimate, nothing to be wrong about.

**`vertical`** — a call spread must be worth between 0 and `PV(K₂−K₁)`. If it can
be *bought for a credit*, you are paid to hold a payoff that is never negative.
Two legs, so half the execution risk and half the fixed cost of a box. Highest
frequency family.

**`box`** — buy the call spread, sell the put spread at the same two strikes.
Guaranteed `K₂−K₁` at expiry. Four legs.

**`butterfly`** — option prices must be convex in strike. If a butterfly can be
bought for a credit, convexity is violated. Needs no interest-rate assumption at
all.

**`pcp_futures`** — `C − P = DF·(F − K)` against a future of the *same* expiry.
Refuses to fire without a matched future, which on NIFTY means the monthly only;
the weeklies have no future, so the strategy is correctly disabled there.

### Discounting is directional

Conservatism in a box is not one number. Cash we must fund is charged the borrow
rate; cash we receive earns only the lend rate. Using a single repo rate for both
manufactures edge on whichever side is wrong. `RateBand` handles this, and with
`use_implied_rate` the band is centred on the financing rate **read off the boxes
themselves** rather than assumed.

---

## Latency

Measured, not claimed. `venv/bin/python tools/bench.py`:

```
25 strikes (300 pairs):
   butterfly          54.5 µs
   vertical           23.1 µs
   box                17.7 µs
   pcp_futures        14.0 µs
   ALL SCANNERS      109.3 µs
```

The scan is O(strikes²) inside numpy, not in Python, so it barely grows: 61
strikes costs ~124 µs. The engine sleeps on the feed's tick event, so it wakes
within tens of microseconds of a quote change and never holds the GIL the
websocket thread needs.

Every trade records, per leg, the wall-clock instant that leg's quote was seen and
how many milliseconds before the order it was — plus the millisecond fire time and
the **leg spread**: the gap between the oldest and newest leg quote.

That last number is the integrity check on the whole system. An arbitrage priced
from legs observed seconds apart is not an arbitrage, it is four unrelated prices.
The old bot's boxes were assembled from REST polls seconds apart, and a leg-spread
column would have exposed it on the first trade. All of it lands in the Telegram
message, the dashboard trade card, and the journal CSV (`executed_at`,
`quoted_at`, `leg_age_ms`, `ms_before_fire`, `leg_spread_ms`).

---

## Entry, exit and P&L per leg

The trade log's legs table shows, for every leg, the entry (the fill), the
instant its quote was seen, the **exit** and the leg's P&L, then a subtotal per
trade and an overall total. Exit is the live mid until expiry; from expiry on it
is the intrinsic value at the index's final settlement level, which the engine
pins itself into `journal/settlements.json` from the last spot it sees at the
close of an expiry day (`arb/settlement.py`; the close of the last five-minute
candle is the fallback). Contracts held in open paper positions are subscribed
even when today's strike window has moved past them, so no leg is ever left
without a mark. The trade's P&L is the legs less statutory costs and should
converge on the edge locked at entry as the structure settles - the difference
between the two columns is the test of whether the arbitrage was real.

The locked edge itself is now net of the paper broker's own slippage (fills
0.05 past the quote on each leg, ~Rs 13 a trade); `tools/restate_slippage.py`
applied this to the trades recorded before the change.

## The mathematics on the page

The dashboard has a *Mathematics* section, set in MathML the way Wikipedia sets
its formulas, that derives every rule the scanner enforces from one identity -
an option is the discounted integral of its payoff against the risk-neutral
density - and then evaluates the integrals on live mid prices every two seconds
(`arb/mathview.py`, `/api/math`):

- the **box** integrates the whole density, giving the discount factor and the
  market's own financing rate per expiry;
- **butterflies** are the second strike-derivative, i.e. the density itself
  (Breeden-Litzenberger); the page draws the implied density per chain and marks
  where it goes negative;
- **vertical spreads** are the first derivative, i.e. tail probabilities; the
  page draws the implied distribution function and flags steps that run backwards;
- **put-call parity** is the first moment, the forward; the parity-implied
  future is compared with the listed one;
- the **edge, the statutory cost per leg and the band** are written out, with a
  table of what 2% and 5% per year come to on each chain today;
- the **futures curve** of each index (three expiries, subscribed independently
  of the option chains) with the carry each future implies and the forward carry
  between consecutive expiries against the financing rate - calendar spreads as
  analytics only, since their payoff is not locked in at entry.

## The 30-day paper log

Every trade is journalled per day (`journal/trades_YYYY-MM-DD.jsonl` and `.csv`),
and `arb/history.py` reads the whole run back: per-day net, win rate, which
strategy and which index produced it, and the cumulative equity curve. The
dashboard's **Trade log** has a day picker, so yesterday's fills — every leg, its
price and the instant its quote was seen — stay reviewable for the whole trial.

Nothing there depends on the process staying up: it is read from disk, so a
restart, a crash, or a fresh session loses none of the record.

```bash
venv/bin/python -c "import sys;sys.path.insert(0,'.');\
from arb.history import load_trades, summarise;\
import json;print(json.dumps(summarise(load_trades())['totals'], indent=2))"
```

## Data-driven calibration

Every session records the book to `data/book_*.f4`. Then:

```bash
venv/bin/python -m arb.research data/book_20260909_195431.meta.json
```

This measures, and derives settings from, what the market actually did:

- **violation lifetime** — sets the latency budget. If the median violation lives
  40 ms, no threshold will make a 200 ms scanner able to trade it.
- **violation size distribution** — the trading threshold becomes p90 of observed
  edge, floored at 1.5× that structure's own round-trip cost, instead of a round
  number.
- **touch spread** — calibrates slippage to what crossing actually costs.
- **touch depth** — caps size at what the book routinely shows.
- **box-implied financing rate** — measured, not assumed.

Output goes to `config/calibration.json`, with a written rationale for each
number so a later session can tell why a threshold is what it is.

---

## Safety

- **"Invested" is what Angel One blocks.** `arb/margin.py` asks Angel One's own
  margin calculator (`getMarginApi`, SPAN + exposure net of hedge benefit) what the
  account must hold. A one-lot NIFTY short box that can owe at most Rs 13,000 at
  expiry has about Rs 80,000 of margin blocked against it - so "invested", lot
  sizing and capital utilisation all use the margin, not the theoretical
  settlement figure, and the return on margin is reported on every trade. The
  detection loop
  never waits on HTTP: a per-chain table of per-lot margin for each structure shape
  (spread, box/butterfly, conversion, reversal) is calibrated in the background
  every 10 minutes, and the exact figure for the legs actually traded is fetched
  right after the fill and written on the trade record (`margin_inr`,
  `capital_source`). Until the table is calibrated a fixed share of notional per
  short unit is assumed, set a little above what Angel returned for a hedged box.
  `tools/backfill_margin.py` re-states past trades on the same basis.
- **The band is annualised.** A trade is taken only when its net edge is between
  `min_excess_return_pa` and `max_excess_return_pa` (2-5%) **per year of the
  contract value it controls** - the index level times the units, so for a box
  this is exactly how far the market's implied financing rate sits from fair. A
  per-trade 2-5% on margin is unreachable on liquid index options (it needs a
  26-point edge on a weekly NIFTY box, at the plausibility cap); 2-5% per year is
  a real dislocation and still rare. Tenor is floored at one day when annualising
  so an expiry-afternoon tick is not a triple-digit yield. The dashboard's
  Mathematics section shows what the band comes to on each chain today, in rupees
  and in points.
- **Sizing.** Every trade puts the same stake to work: `stake_per_trade`
  (Rs 1 lakh) of broker margin, and never more. Lots are the most whole lots
  whose margin fits inside the stake, then **capped by the depth displayed at the
  touch**; a structure whose single lot needs more than the stake is rejected
  (`exceeds_stake`). Displayed size is the only evidence a fill exists; sizing
  past it is the old bot's mistake in a new form. Each day's profit is reported
  as a return on the stake as well as on the book.
- **Feed watchdog.** Reconnection is owned by `MarketFeed`, not the SmartAPI
  library, whose retries fire only from the error callback and give up after a
  fixed count. A socket that goes quiet for 15 s while the market is open (what a
  network blip leaves behind - no error, no close event, just silence) is torn
  down and reconnected; three straight failed connects trigger a fresh login. The
  dashboard's Engine box shows the time since the last tick and the reconnect
  count.

**Paper is the default.** Live mode requires `--mode live --yes`. Nothing in the
  detection path differs between them — live swaps the broker and nothing else.
- Pre-trade gate: staleness, depth, implausible-edge, notional, daily loss,
  cooldowns per structure and per instrument, exact-cost net floor.
- `implausible_edge` exists because on liquid index options a 25-point edge is a
  bad print or a frozen quote far more often than it is free money.
- Partial fills unwind at market and are journalled as a loss, never as profit.
- Every signal that was *not* traded is written to `journal/rejects_*.jsonl` with
  its reason — so you can tell whether the engine is too tight or the market is
  simply efficient. The old bot logged nothing and could not tell.

## What the live market actually showed

Measured on 10 Sep 2026, 11:01-11:08 IST, 41,432 real ticks across 152 NIFTY
contracts on three expiries. This is the answer to "does this have edge", and it
came from the recorder and `arb.research`, not from an opinion.

**Violations exist, and they are far too small to trade.**

| Strategy | median violation | max seen | cost to trade | verdict |
|---|---|---|---|---|
| Box (4 legs) | 0.27 pts | 2.27 pts | ~4.73 pts | 17x too small |
| Put-call parity (3 legs) | 0.90 pts | 2.98 pts | ~3.06 pts | max is just under cost |
| Butterfly (4 legs) | 0.15 pts | 1.00 pts | ~4.73 pts | far too small |
| Vertical (2 legs) | *none observed* | — | ~3.06 pts | the bound never broke |

**They live 0 ms.** A violation appears in one 250 ms snapshot and is gone by the
next. Detection at ~1.3 ms is nowhere near the constraint; the broker round-trip
is.

**The book is healthy, the feed is slow.** Quote health over the session:
`one_sided 0.0%`, `thin 0.0%`, `stale 61.7%`, `tradable 38.2%`. The book is
two-sided and deep enough at every touch — the only thing failing is freshness.
Angel pushes roughly one update per second per instrument, and the median quote
is 647 ms old. For a 4-leg box needing all legs simultaneously fresh inside
400 ms, that is `0.382^4` ≈ **2%** of passes.

Loosening the freshness gate does not fix this. It would only trade the *stale*
prices, which is precisely how the old bot generated 154 imaginary wins. And it
would not matter anyway: at 0.27 points against 4.73 points of cost, the edge is
absent, not merely hard to reach.

**Financing is a term structure, not a number.** Live box-implied rates came out
at 7.4% / 9.3% / 9.5% across the three expiries. The engine now fits a separate
rate band per expiry. A single median across them would misprice a 1200-point box
by ~0.5 points at 5 days — larger than the median violation being hunted, so a
flat rate manufactures signals instead of finding them.

### So: is it worth running?

Run it as an instrument before running it as a trader. The recorder plus
`arb.research` answer "is there edge today" from data. Conditions worth measuring
before concluding anything: **expiry day** (Tuesday for NIFTY, when pin risk and
unwinds widen dislocations), the first and last fifteen minutes, and high-VIX
days. A session that reports zero trades is a correct result.

Do not switch to `--mode live` until `arb.research` shows a strategy whose
observed violations exceed its own cost line. Right now, none does.

---

## Layout

```
arb/
  config.py       credentials, cost model, risk limits, engine settings
  book.py         quote store; best-5 normalisation (robust to Angel's side swap)
  costs.py        exact + vectorised Indian F&O costs
  strategies.py   the four scanners, vectorised
  instruments.py  scrip master, expiry/strike universe, market phase
  feed.py         SmartAPI login + SNAP_QUOTE websocket
  engine.py       scan loop, geometry cache, rate calibration, dedupe
  risk.py         pre-trade gate
  execution.py    paper + live brokers, concurrent legs, unwind, reconcile
  telegram.py     background alert queue
  journal.py      trades + rejects
  recorder.py     book snapshots for research
  research.py     calibration from recorded data
run_bot.py        live runner
tools/bench.py    latency benchmark
tools/simulate.py end-to-end pipeline test against a synthetic market
tests/            strategy correctness suite
```

## Tests

```bash
venv/bin/python tests/test_strategies.py     # 40 correctness checks
venv/bin/python tools/simulate.py --record   # full pipeline, synthetic market
venv/bin/python tools/bench.py               # latency
```

The suite's central check is that an arbitrage-free chain produces **zero**
signals across four volatilities and four expiries, at zero cost and zero
threshold. Planted violations are then verified to be detected at exactly the
planted size.
