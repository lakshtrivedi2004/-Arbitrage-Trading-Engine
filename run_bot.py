#!/usr/bin/env python3
"""NIFTY options arbitrage bot - live runner.

    python run_bot.py                      # paper mode against the live feed
    python run_bot.py --mode live --yes    # send real orders
    python run_bot.py --status             # one-shot connectivity + universe check

Paper mode is the default and is the only mode that runs without an explicit
--yes. Nothing about the detection path differs between the two: live mode swaps
the broker and nothing else, so what paper trades is exactly what live trades.
"""
import argparse
import signal
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from arb.config import Credentials, CostModel, EngineConfig, RiskLimits
from arb.candles import CandleStore
from arb.dashboard import Dashboard, DashboardState
from arb.engine import ArbEngine, ChainBinding
from arb.execution import ExecutionEngine, LiveBroker, PaperBroker
from arb.feed import NSE_CM, NSE_FO, AngelSession, MarketFeed
from arb.instruments import (build_universe, live_futures, load_scrip_master, market_phase,
                             next_session, now_ist, years_to_expiry)
from arb.journal import Journal
from arb.recorder import TickRecorder
from arb.margin import MarginService
from arb.risk import RiskManager
from arb.settlement import SettlementBook
from arb.telegram import TelegramNotifier, _inr


def log(msg):
    print(f"[{now_ist().strftime('%H:%M:%S')}] {msg}", flush=True)


def build(cfg, creds, args):
    """Log in, pick the universe across every enabled index, wire the feed."""
    log(f"Angel One login as {creds.client_id}")
    # Wait out a network outage at start (DNS down, no route) for up to ten
    # minutes rather than dying: on 15 Sep 2026 the machine's DNS was out at
    # 13:55 and the bot stayed dead until it was restarted by hand at 14:48.
    session = AngelSession(creds).login(retry_for_sec=600.0, log=log)
    log("login ok")

    sm, src = load_scrip_master()
    log(f"scrip master: {len(sm):,} rows from {src}")

    underlyings = cfg.active_underlyings()
    specs, spots = [], {}
    for u in underlyings:
        spot = 0.0
        try:
            spot = session.spot_ltp(u.spot_token, u.name, u.spot_exchange)
        except Exception as e:
            log(f"  {u.name}: spot fetch failed ({e}); using chain midpoint")
        spots[u.name] = spot
        expiries, chains, futures = build_universe(
            sm, u.name, spot or None, u.num_expiries, u.strikes_each_side,
            u.segment, cfg.min_hours_to_expiry)
        if not expiries:
            log(f"  {u.name}: no live expiries, skipped")
            continue
        specs.append((u, expiries, chains, futures))

    if not specs:
        raise SystemExit("no tradable index found in the scrip master")

    n_tokens = sum(len(c) * 2 for _, _, ch, _ in specs for c in ch.values()) \
        + sum(len(f) for _, _, _, f in specs) + len(specs)
    feed = MarketFeed(
        session, capacity=n_tokens + 256, log=log,
        market_open=lambda: market_phase(start=cfg.session_start,
                                         end=cfg.session_end) == "open")

    bindings = []
    spot_slots = {}
    future_slots = {}
    for u, expiries, chains, futures in specs:
        spot_slots[u.name] = feed.register(u.spot_token, u.spot_exchange_type)
        log(f"{u.name}  spot {spots[u.name]:,.2f}  ({u.segment})")
        # The futures term structure, for calendar-carry analytics on the
        # dashboard. Futures already tied to a chain share the same slot.
        future_slots[u.name] = []
        for f in live_futures(sm, u.name, u.segment, cfg.min_hours_to_expiry):
            known = feed.store.slot_of_token.get(str(f["token"]))
            slot = known if known is not None else \
                feed.register(f["token"], u.option_exchange_type)
            future_slots[u.name].append(dict(f, slot=slot))
        for exp in expiries:
            chain = chains.get(exp) or {}
            if len(chain) < 2:
                log(f"    {exp}: too few paired strikes, skipped")
                continue
            strikes = sorted(chain)
            gi = len(bindings)
            ce_slots, pe_slots = [], []
            ce_sym, pe_sym, ce_tok, pe_tok = [], [], [], []
            for k in strikes:
                ce, pe = chain[k]["CE"], chain[k]["PE"]
                ce_slots.append(feed.register(ce["token"], u.option_exchange_type, gi))
                pe_slots.append(feed.register(pe["token"], u.option_exchange_type, gi))
                ce_sym.append(ce["symbol"]); pe_sym.append(pe["symbol"])
                ce_tok.append(ce["token"]); pe_tok.append(pe["token"])
            fut = futures.get(exp)
            fut_slot = feed.register(fut["token"], u.option_exchange_type, gi) \
                if fut else None
            bindings.append(ChainBinding(
                exp, strikes, ce_slots, pe_slots, ce_sym, pe_sym, ce_tok, pe_tok,
                lot_size=chain[strikes[0]]["CE"]["lotsize"],
                fut_slot=fut_slot,
                fut_sym=fut["symbol"] if fut else "",
                fut_tok=fut["token"] if fut else "",
                underlying=u.name, segment=u.segment))
            step = (strikes[1] - strikes[0]) if len(strikes) > 1 else 0
            log(f"    {exp}: {len(strikes)} strikes {strikes[0]:.0f}-{strikes[-1]:.0f} "
                f"(step {step:.0f}), lot {bindings[-1].lot_size}, "
                f"T={years_to_expiry(exp) * 365:.2f}d, "
                f"future {fut['symbol'] if fut else 'NONE (parity off)'}")
    if not bindings:
        raise SystemExit("no tradable chains")
    log(f"{len(bindings)} chains across {len(specs)} indices; "
        f"subscribing to {feed.store._next} tokens in SNAP_QUOTE mode")
    feed.spot_slots = spot_slots
    feed.future_slots = future_slots
    return session, feed, bindings, spots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("paper", "live"), default=None)
    ap.add_argument("--yes", action="store_true", help="required to arm live orders")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="minimum net edge in points per unit to consider")
    ap.add_argument("--status", action="store_true", help="check and exit")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until close")
    ap.add_argument("--port", type=int, default=8088, help="dashboard port")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--candle-interval", default="5m",
                    choices=("1m", "5m", "15m", "1h", "1d"))
    ap.add_argument("--wait-open", action="store_true",
                    help="build now, start scanning exactly at the session open "
                         "(09:15 IST) and stop at the close (15:15)")
    ap.add_argument("--hold", action="store_true",
                    help="after the session, keep the dashboard serving until SIGTERM")
    ap.add_argument("--daemon", action="store_true",
                    help="supervisor: launch one session per trading day, forever "
                         "(what deploy.sh runs under nohup)")
    ap.add_argument("--prep-minutes", type=float, default=10.0,
                    help="daemon: minutes before the open to log in and subscribe")
    args = ap.parse_args()

    if args.daemon:
        from arb.daemon import run_daemon
        return run_daemon(args)

    cfg = EngineConfig.load()
    cm = CostModel.load()
    limits = RiskLimits()
    creds = Credentials.load()
    if creds.missing():
        raise SystemExit(f"missing credentials in .angel.env: {creds.missing()}")

    mode = args.mode or cfg.trading_mode
    if mode == "live" and not args.yes:
        raise SystemExit(
            "live mode places real orders with real money.\n"
            "Re-run with --mode live --yes once you have watched paper mode "
            "for a full session and agree with what it did.")

    phase = market_phase(start=cfg.session_start, end=cfg.session_end)
    log(f"market phase: {phase} ({now_ist().strftime('%a %d %b %H:%M IST')})")
    # The session this process is for. With --wait-open, a start before the
    # open arms the scanner for exactly 09:15:00; a start with no session left
    # today (evening, weekend, holiday) only serves the dashboard.
    open_dt, close_dt = next_session(start=cfg.session_start, end=cfg.session_end)
    session_today = open_dt.date() == now_ist().date()
    if args.wait_open and not session_today:
        log(f"no session left today; next is {open_dt.strftime('%a %d %b %H:%M IST')}. "
            f"Holding the dashboard only - the daemon relaunches before the open.")

    session, feed, bindings, spots = build(cfg, creds, args)
    if args.status:
        log("status check complete")
        return

    notifier = None
    if not args.no_telegram:
        notifier = TelegramNotifier(creds.telegram_bot_token,
                                    creds.telegram_chat_id).start()

    journal = Journal()
    # "Invested" is what Angel One blocks, from its own margin calculator, not
    # the structure's theoretical settlement obligation (Rs 83,656 versus
    # Rs 13,000 for a one-lot NIFTY box). Calibrated per chain in the background
    # so the scan loop never waits on HTTP; exact figure fetched after each fill.
    margin = MarginService(session.api, bindings, limits, log=log, spots=spots).start()
    risk = RiskManager(limits, cm, margin=margin)
    # Pick the day back up where it left off. Counters, limits and the trade-id
    # sequence all come from the journal, so a restart is invisible to the
    # risk gate and to the dashboard.
    last_id = risk.restore_today()
    if risk.state.trades_today:
        log(f"restored today's state from journal: {risk.state.trades_today} trade(s), "
            f"P&L {_inr(risk.state.session_pnl)}, "
            f"deployed {_inr(risk.state.capital_deployed)}"
            + (f" — HALTED: {risk.state.halt_reason}" if risk.state.halted else ""))
    broker = (LiveBroker(session.api, cm) if mode == "live" else PaperBroker(cm))
    lot = bindings[0].lot_size
    execution = ExecutionEngine(broker, cm, risk, journal, notifier, lot,
                                total_capital=limits.total_capital, margin=margin)
    execution.trade_id = last_id          # continue numbering, never reuse an id
    engine = ArbEngine(feed, bindings, cfg, cm, limits, risk, execution,
                       journal, notifier, log=log)

    # Final settlement levels, pinned from the live spot at the close of an
    # expiry day so every leg's exit price stays reviewable after the contract
    # is gone.
    settlements = SettlementBook()

    def expiring_pairs(bindings):
        """(underlying, expiry) pairs that settle today: today's chains, plus
        the expiries of every open paper position in the journal - an expiring
        chain leaves the universe an hour before it settles, but the positions
        in it still need their settlement level."""
        from arb import history as _hist
        from arb.instruments import parse_expiry
        today = now_ist().date()
        pairs = {(b.underlying, b.expiry) for b in bindings}
        try:
            for t in _hist.load_trades(days=30):
                o = t.get("opportunity") or {}
                if o.get("expiry") and o.get("underlying"):
                    pairs.add((o["underlying"], o["expiry"]))
        except Exception:
            pass
        out = set()
        for u, e in pairs:
            try:
                if parse_expiry(e).date() == today:
                    out.add((u, e))
            except Exception:
                continue
        return out

    def spot_of(name):
        slot = (getattr(feed, "spot_slots", {}) or {}).get(name)
        if slot is not None and float(feed.store.ltp[slot]) > 0:
            return float(feed.store.ltp[slot])
        return float(spots.get(name, 0.0))

    dash = None
    if not args.no_dashboard:
        candles = CandleStore(session.api, cfg.active_underlyings(),
                              interval=args.candle_interval, log=log)
        candles.attach_feed(feed.store, getattr(feed, "spot_slots", {})).start()
        dstate = DashboardState(engine, feed, risk, execution, cfg, mode, journal,
                                candles=candles, margin=margin, settlements=settlements)
        dash = Dashboard(dstate, port=args.port).start()
        # Mirror every finished trade into the dashboard: the execution engine
        # publishes each record once the broker's margin figure is on it.
        execution.subscribers.append(dstate.record_trade)
        log(f"dashboard   {dash.url}")

    # Open positions from earlier sessions whose contracts fell outside today's
    # strike window (the universe re-centres on spot each start) still need a
    # live mark for their exit price. Subscribe to those legs too; they carry
    # no chain group, so the scanner never sees them.
    extra = 0
    try:
        from arb import history as _hist
        from arb.feed import BSE_FO, NSE_FO
        from arb.instruments import parse_expiry
        for t in _hist.load_trades(days=30):
            exp = (t.get("opportunity") or {}).get("expiry", "")
            try:
                if not exp or now_ist() >= parse_expiry(exp):
                    continue
            except Exception:
                continue
            for l in t.get("legs", []):
                tok = str(l.get("token", ""))
                if tok and tok not in feed.store.slot_of_token:
                    feed.register(tok, BSE_FO if l.get("exchange") == "BFO" else NSE_FO)
                    extra += 1
    except Exception as e:
        log(f"open-position subscription skipped: {e}")
    if extra:
        log(f"subscribed {extra} extra contract(s) held in open paper positions, for marks")

    feed.start()
    log("websocket connecting...")
    if not feed.connected.wait(timeout=30):
        log("websocket did not connect in 30s; continuing to wait in background")
    if dash:
        dash.state.spots = dict(spots)
    slots = list(range(feed.store._next))
    ready = feed.wait_for_quotes(slots, timeout=25, need=max(4, len(slots) // 4))
    log(f"{ready}/{len(slots)} instruments quoting")

    def announce(title, extra=""):
        if not notifier:
            return
        notifier.send(
            f"{title} · <code>{mode.upper()}</code>\n\n"
            f"Account <code>{creds.client_id}</code>\n"
            f"Market <code>{market_phase(start=cfg.session_start, end=cfg.session_end)}</code>"
            f" · {now_ist().strftime('%d %b %H:%M:%S IST')}{extra}\n"
            f"Indices <code>"
            f"{', '.join(sorted({b.underlying for b in bindings}))}</code>\n"
            f"Chains <code>{len(bindings)}</code>\n"
            f"Instruments <code>{ready}/{len(slots)} quoting</code>\n"
            f"Strategies <code>{', '.join(cfg.strategies)}</code>\n"
            f"Size <code>{args.lots} lot (per-index lot size)</code>\n"
            f"Capital <code>{_inr(limits.total_capital)}</code> · "
            f"stake <code>{_inr(limits.stake_per_trade)}</code> per trade, fixed\n"
            f"Take only <code>{limits.min_excess_return_pa:g}%-{limits.max_excess_return_pa:g}% p.a.</code> "
            f"excess yield on contract value\n\n"
            f"<i>A message follows for every trade taken, with invested amount, "
            f"profit and the day's P&amp;L.</i>")

    recorder = None
    if cfg.record_ticks:
        recorder = TickRecorder(feed.store, bindings).start()
        log(f"recording book snapshots to {recorder.bin_path}")

    stop_at = time.time() + args.duration if args.duration else None

    running = {"v": True}
    scheduled = {"v": False}      # the scanner is bound to today's session clock

    def should_stop():
        if stop_at and time.time() > stop_at:
            return True
        # Bound to the session clock: stop at the close, exactly. A dev start
        # after hours (scheduled False) runs until Ctrl+C or --duration.
        return scheduled["v"] and now_ist() >= close_dt

    def on_signal(*_):
        running["v"] = False
        engine.stop()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    def hold_dashboard(reason):
        if not (args.hold and dash):
            return
        log(f"{reason}; dashboard stays up at {dash.url} until stopped")
        while running["v"]:
            time.sleep(1.0)

    if args.wait_open and not session_today:
        # Nothing to trade today. Keep the page up for review, then leave.
        hold_dashboard("no session today")
        feed.stop()
        margin.stop()
        if dash:
            if getattr(dash.state, "candles", None):
                dash.state.candles.stop()
            dash.stop()
        if notifier:
            notifier.stop()
        return

    now = now_ist()
    if args.wait_open and now < open_dt:
        wait = (open_dt - now).total_seconds()
        log(f"armed: scanning starts at {open_dt.strftime('%H:%M:%S')} IST, "
            f"in {wait / 60:.1f} min; stop at {close_dt.strftime('%H:%M')}")
        announce("🟡 <b>ARMED</b>",
                 f"\nScanning starts at <code>{open_dt.strftime('%H:%M:%S')} IST</code>, "
                 f"stops at <code>{close_dt.strftime('%H:%M')}</code>")
        # Coarse 1s ticks so a SIGTERM is honoured, then one fine sleep so the
        # first scan lands on the opening second, not up to a second late.
        while running["v"]:
            left = (open_dt - now_ist()).total_seconds()
            if left <= 1.0:
                if left > 0:
                    time.sleep(left)
                break
            time.sleep(1.0)
        if not running["v"]:
            return
    scheduled["v"] = args.wait_open or now_ist() < close_dt
    if scheduled["v"] and now_ist() >= close_dt:
        # Started in the evening without --wait-open: nothing to schedule.
        scheduled["v"] = False

    engine.start(lots=args.lots, min_edge_points=args.min_edge,
                 stop_predicate=should_stop)
    started_at = now_ist()
    log(f"scanning ({mode}) from {started_at.strftime('%H:%M:%S.%f')[:-3]} IST"
        + (f"; stops at {close_dt.strftime('%H:%M')}" if scheduled["v"] else "; Ctrl+C to stop"))
    announce("🟢 <b>ARBITRAGE ENGINE UP</b>",
             f"\nScanning from <code>{started_at.strftime('%H:%M:%S')}</code>"
             + (f", stops at <code>{close_dt.strftime('%H:%M')}</code>" if scheduled["v"] else ""))

    last_report = 0.0
    holiday = False
    warned_quiet = False
    try:
        while running["v"] and engine._running.is_set():
            # Wake on the closing second exactly, not up to a second after it.
            nap = 1.0
            if scheduled["v"]:
                nap = max(0.0, min(1.0, (close_dt - now_ist()).total_seconds()))
            time.sleep(nap)
            if scheduled["v"] and now_ist() >= close_dt:
                log(f"close {close_dt.strftime('%H:%M')} IST: scanning stopped")
                engine.stop()
                break
            # An exchange holiday that is not in config/holidays.txt: the open
            # passes, the socket is up, and nothing ticks. Warn at ten minutes,
            # stop the day at thirty. A socket that never connected is an
            # outage, not a holiday, and is left to the feed's reconnects.
            if scheduled["v"] and not holiday and feed.ticks == 0 and feed.connected.is_set():
                quiet = (now_ist() - open_dt).total_seconds()
                if quiet > 600 and not warned_quiet:
                    warned_quiet = True
                    log("no ticks 10 minutes after the open on a connected socket - "
                        "exchange holiday? Will stop for the day at 30 minutes.")
                if quiet > 1800:
                    holiday = True
                    log("no ticks 30 minutes after the open - exchange holiday. "
                        "Stopping for the day; add the date to config/holidays.txt.")
                    if notifier:
                        notifier.send("⚪ <b>NO MARKET DATA</b>\n\nNo ticks 30 minutes after "
                                      "the open on a connected socket. Exchange holiday? "
                                      "Stopped for the day - add the date to "
                                      "<code>config/holidays.txt</code>.")
                    engine.stop()
                    break
            if dash:
                st = feed.store
                seen = set()
                for b in bindings:
                    if b.underlying in seen:
                        continue
                    mid = len(b.strikes) // 2
                    ce, pe = int(b.ce_slots[mid]), int(b.pe_slots[mid])
                    # Synthetic spot from the ATM call/put mid and the strike:
                    # fresher than another REST poll, and it only feeds the display.
                    if st.bid[ce] > 0 and st.bid[pe] > 0:
                        dash.state.spots[b.underlying] = (
                            float(b.strikes[mid])
                            + 0.5 * (st.bid[ce] + st.ask[ce])
                            - 0.5 * (st.bid[pe] + st.ask[pe]))
                        seen.add(b.underlying)
            if time.time() - last_report >= 60:
                last_report = time.time()
                for und, exp, lvl in settlements.record_todays_expiries(
                        expiring_pairs(bindings), spot_of):
                    log(f"settlement pinned: {und} {exp} at {lvl:,.2f}")
                s = engine.stats.summary()
                log(f"ticks={feed.ticks:,} passes={s['passes']:,} "
                    f"scan p50={s['scan_us_p50']}µs "
                    f"trades={risk.state.trades_today} "
                    f"pnl={_inr(risk.state.session_pnl)} "
                    f"equity={_inr(risk.equity)}")
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        # On an expiry day the scanner stops at session_end (15:15) but the
        # index settles at 15:30. If the stop was the scheduled one - not a
        # Ctrl+C - stay up on the still-live spot feed until then, so the
        # settlement level is pinned from the close itself.
        pairs = expiring_pairs(bindings)
        if pairs and running["v"] and not holiday:
            from arb.instruments import parse_expiry
            latest = max(parse_expiry(e) for _, e in pairs)
            wait = (latest - now_ist()).total_seconds() + 45.0
            if 0 < wait < 45 * 60:
                log(f"expiry day: holding {wait / 60:.1f} min for the 15:30 close to pin "
                    f"{', '.join(f'{u} {e}' for u, e in sorted(pairs))}")
                end = time.time() + wait
                while running["v"] and time.time() < end:
                    time.sleep(1.0)
        for und, exp, lvl in settlements.record_todays_expiries(pairs, spot_of):
            log(f"settlement pinned: {und} {exp} at {lvl:,.2f}")
        # Let a trade that fired in the last second finish its margin lookup and
        # reach the journal and Telegram before anything else is torn down.
        execution.drain()
        margin.stop()
        feed.stop()
        if dash and getattr(dash.state, "candles", None):
            dash.state.candles.stop()
        if recorder:
            recorder.stop()
            log(f"recorded {recorder.snapshots:,} snapshots -> {recorder.meta_path}")
            log(f"calibrate with: venv/bin/python -m arb.research {recorder.meta_path}")
        s = engine.stats.summary()
        log("─" * 60)
        log(f"ticks received     {feed.ticks:,} (dropped {feed.dropped})")
        log(f"scan passes        {s['passes']:,}")
        log(f"scan latency       p50 {s['scan_us_p50']}µs · p99 {s['scan_us_p99']}µs")
        log(f"tick→detect        p50 {s['tick_to_detect_us_p50']}µs · "
            f"p99 {s['tick_to_detect_us_p99']}µs")
        log(f"chains scanned     {engine.chains_scanned:,} "
            f"(skipped {engine.chains_skipped:,} unchanged)")
        log(f"quote health       {engine.quote_health_summary()}")
        log(f"implied rates      "
            f"{ {k: round(v, 4) for k, v in engine.implied_rates.items()} or 'none'}")
        log(f"signals            {engine.signals_by_strategy or 'none'}")
        log(f"rejected           {risk.state.rejects or 'none'}")
        log(f"trades             {risk.state.trades_today}")
        log(f"capital            {_inr(limits.total_capital)} "
            f"({limits.min_excess_return_pa:g}-{limits.max_excess_return_pa:g}% p.a. band)")
        log(f"session P&L        {_inr(risk.state.session_pnl)}  "
            f"({100.0 * risk.state.session_pnl / limits.stake_per_trade:+.3f}% of the "
            f"{_inr(limits.stake_per_trade)} stake)")
        log(f"equity             {_inr(risk.equity)}")
        if engine.best_seen:
            b = engine.best_seen
            log(f"best signal seen   {b.strategy} {b.gross_edge_points:+.2f}pt "
                f"({b.description})")
        if notifier:
            notifier.send(
                f"🔴 <b>ENGINE STOPPED</b>\n\n"
                f"Ticks <code>{feed.ticks:,}</code> · passes <code>{s['passes']:,}</code>\n"
                f"Scan p50 <code>{s['scan_us_p50']}µs</code> / "
                f"p99 <code>{s['scan_us_p99']}µs</code>\n"
                f"Trades <code>{risk.state.trades_today}</code>\n"
                f"Session P&amp;L <code>{_inr(risk.state.session_pnl)}</code> "
                f"(<code>{100.0 * risk.state.session_pnl / limits.stake_per_trade:+.3f}%</code> "
                f"of the {_inr(limits.stake_per_trade)} stake)\n"
                f"Signals <code>{engine.signals_by_strategy or 'none'}</code>\n"
                f"Rejects <code>{risk.state.rejects or 'none'}</code>")
            notifier.flush()
            notifier.stop()
            log(f"telegram           {notifier.sent} sent, {notifier.failed} failed")
        # The day's record stays reviewable on the page until the daemon
        # relaunches for the next session (or someone stops this process).
        hold_dashboard("session over")
        if dash:
            dash.stop()


if __name__ == "__main__":
    main()
