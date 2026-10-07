#!/usr/bin/env python3
"""End-to-end pipeline test against a synthetic market.

Drives the *real* engine, risk manager, paper broker, journal and notifier from a
fake feed. The synthetic market is arbitrage-free except for violations injected
at known times and known sizes, so the run answers two questions at once:
does the pipeline trade what it should, and does it stay quiet when it should.
"""
import argparse
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from arb.book import QuoteStore
from arb.config import CostModel, EngineConfig, RiskLimits
from arb.engine import ArbEngine, ChainBinding
from arb.execution import ExecutionEngine, PaperBroker
from arb.journal import Journal
from arb.recorder import TickRecorder
from arb.risk import RiskManager
from tests.synth import bs


class FakeFeed:
    """Same surface the engine uses from MarketFeed: store, tick_event, counters."""

    def __init__(self, capacity):
        self.store = QuoteStore(capacity)
        self.tick_event = threading.Event()
        self.last_tick_ns = 0
        self.ticks = 0
        self.dropped = 0

    def register(self, token):
        return self.store.assign(str(token))


class SyntheticMarket:
    def __init__(self, feed, strikes, ce_slots, pe_slots, fut_slot,
                 spot=24500.0, T=6.0 / 365.0, r=0.065, vol=0.13,
                 half_spread=0.30, depth=2000.0, tick=0.05, seed=11):
        self.feed, self.strikes = feed, np.asarray(strikes, float)
        self.ce_slots, self.pe_slots, self.fut_slot = ce_slots, pe_slots, fut_slot
        self.spot, self.T, self.r, self.vol = spot, T, r, vol
        self.half_spread, self.depth, self.tick = half_spread, depth, tick
        self.rng = np.random.default_rng(seed)
        self.injections = []          # (slot_kind, index, points, expires_at)
        self.injected_total = 0
        self._stop = threading.Event()

    def _quote(self, mid):
        bid = math.floor((mid - self.half_spread) / self.tick) * self.tick
        ask = math.ceil((mid + self.half_spread) / self.tick) * self.tick
        return max(0.05, bid), max(0.10, ask)

    def inject_vertical(self, points=5.0, hold=0.6):
        """Lift one call bid above the adjacent lower strike's ask - a genuine,
        model-free, two-leg arbitrage.

        The injection goes into the far OTM wing, where adjacent strikes are only
        a rupee or so apart, so crossing the bound takes a small perturbation and
        does not simultaneously blow through every other bound in the chain. That
        is also where real violations actually occur: the illiquid wings, not the
        money."""
        # Rotate through the OTM wing so successive injections land on different
        # contracts, the way real dislocations do. Repeating one contract would
        # only be testing the instrument cooldown.
        wing = len(self.strikes) - 2
        hi = wing - 2 * (self.injected_total % 4)
        self.injections.append(("vertical", hi - 1, hi, points, time.time() + hold))
        self.injected_total += 1

    def injection_active(self):
        return any(x[4] > time.time() for x in self.injections)

    def run(self, hz=40.0, drift_vol=1.2):
        period = 1.0 / hz
        store = self.feed.store
        while not self._stop.is_set():
            self.spot += self.rng.normal(0, drift_vol)
            df = math.exp(-self.r * self.T)
            F = self.spot / df
            now = time.perf_counter_ns()
            ce_q, pe_q = [], []
            for K in self.strikes:
                ce_q.append(self._quote(bs(F, K, self.T, self.vol, df, True)))
                pe_q.append(self._quote(bs(F, K, self.T, self.vol, df, False)))

            live = [x for x in self.injections if x[4] > time.time()]
            self.injections = live
            for _, lo, hi, pts, _ in live:
                # High-strike call bid lifted above the low-strike call ask.
                bid = ce_q[lo][1] + pts
                ce_q[hi] = (bid, bid + 0.50)

            for i, slot in enumerate(self.ce_slots):
                b, a = ce_q[i]
                store.update_from_tick(slot, b, a, self.depth, self.depth,
                                       (b + a) / 2, now_ns=now)
            for i, slot in enumerate(self.pe_slots):
                b, a = pe_q[i]
                store.update_from_tick(slot, b, a, self.depth, self.depth,
                                       (b + a) / 2, now_ns=now)
            if self.fut_slot is not None:
                store.update_from_tick(self.fut_slot, F - 0.25, F + 0.25,
                                       self.depth, self.depth, F, now_ns=now)
            self.feed.ticks += 1
            self.feed.last_tick_ns = now
            self.feed.tick_event.set()
            time.sleep(period)

    def stop(self):
        self._stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--hz", type=float, default=40.0)
    ap.add_argument("--injections", type=int, default=6)
    ap.add_argument("--record", action="store_true",
                    help="write book snapshots for arb.research to calibrate from")
    ap.add_argument("--clean-seconds", type=float, default=8.0,
                    help="quiet phase used to prove the engine stays silent")
    ap.add_argument("--edge", type=float, default=5.0, help="injected edge, points")
    ap.add_argument("--strikes", type=int, default=25)
    args = ap.parse_args()

    n = args.strikes
    strikes = [24500 + (i - n // 2) * 50 for i in range(n)]
    feed = FakeFeed(capacity=2 * n + 4)
    ce_slots = [feed.register(f"CE{i}") for i in range(n)]
    pe_slots = [feed.register(f"PE{i}") for i in range(n)]
    fut_slot = feed.register("FUT")

    binding = ChainBinding(
        expiry="15SEP2026", strikes=strikes, ce_slots=ce_slots, pe_slots=pe_slots,
        ce_sym=[f"NIFTY15SEP26{int(k)}CE" for k in strikes],
        pe_sym=[f"NIFTY15SEP26{int(k)}PE" for k in strikes],
        ce_tok=[f"CE{i}" for i in range(n)], pe_tok=[f"PE{i}" for i in range(n)],
        lot_size=65, fut_slot=fut_slot, fut_sym="NIFTY15SEP26FUT", fut_tok="FUT")

    cfg = EngineConfig()
    cm = CostModel()
    # The simulation compresses a session into seconds, so the time-based
    # cooldowns are scaled down with it. Everything else is at production values.
    # The return band is widened here on purpose: this harness tests the
    # detect -> risk -> execute -> journal path, and its injected dislocations are
    # deliberately large. The 2-5% production band is tested directly in
    # tests/test_strategies.py instead.
    limits = RiskLimits(trade_cooldown_sec=0.5, max_trades_per_day=100,
                        structure_cooldown_sec=1.5, instrument_cooldown_sec=1.5,
                        min_excess_return_pa=0.0, max_excess_return_pa=1e9)
    risk = RiskManager(limits, cm)
    journal = Journal(directory="journal/sim")
    execution = ExecutionEngine(PaperBroker(cm), cm, risk, journal, None, 65)
    engine = ArbEngine(feed, [binding], cfg, cm, limits, risk, execution,
                       journal, None, log=lambda m: print("   " + m, flush=True))

    market = SyntheticMarket(feed, strikes, ce_slots, pe_slots, fut_slot)
    threading.Thread(target=market.run, kwargs={"hz": args.hz}, daemon=True).start()
    time.sleep(0.4)

    recorder = TickRecorder(feed.store, [binding], directory="data/sim",
                            hz=8.0).start() if args.record else None
    print(f"\nSynthetic market: {n} strikes, {args.hz:.0f} Hz\n")

    # ── Phase 1: a market with no arbitrage in it. The engine must stay silent.
    print(f"Phase 1 — clean market, {args.clean_seconds:.0f}s. Expect zero signals.")
    engine.start(lots=1, min_edge_points=0.05)
    time.sleep(args.clean_seconds)
    clean_signals = dict(engine.signals_by_strategy)
    clean_trades = risk.state.trades_today
    clean_stats = engine.stats.summary()
    print(f"   signals: {clean_signals or 'none'} · trades: {clean_trades}")

    # ── Phase 2: inject real, known-size arbitrages. Each must be traded.
    print(f"\nPhase 2 — {args.injections} injected {args.edge}pt arbitrages.")
    every = max(1.2, args.seconds / (args.injections + 1))
    for _ in range(args.injections):
        market.inject_vertical(points=args.edge)
        time.sleep(every)

    engine.stop()
    market.stop()
    if recorder:
        recorder.stop()
    time.sleep(0.2)

    s = engine.stats.summary()
    trades = risk.state.trades_today
    injected_trades = trades - clean_trades
    print("\n" + "─" * 68)
    print(f"  ticks generated       {feed.ticks:,}")
    print(f"  scan passes           {s['passes']:,}")
    print(f"  scan latency (clean)  p50 {clean_stats['scan_us_p50']:>7.1f} µs   "
          f"p99 {clean_stats['scan_us_p99']:>7.1f} µs")
    print(f"  scan latency (all)    p50 {s['scan_us_p50']:>7.1f} µs   "
          f"p99 {s['scan_us_p99']:>7.1f} µs   max {s['scan_us_max']:.1f} µs")
    print(f"  tick → detect         p50 {s['tick_to_detect_us_p50']:>7.1f} µs   "
          f"p99 {s['tick_to_detect_us_p99']:>7.1f} µs")
    print(f"  arbitrages injected   {market.injected_total}")
    print(f"  trades taken          {trades} ({injected_trades} after injections)")
    print(f"  session P&L           ₹{risk.state.session_pnl:,.2f}")
    print(f"  signals by strategy   {engine.signals_by_strategy or 'none'}")
    print(f"  rejects               {risk.state.rejects or 'none'}")
    if recorder:
        print(f"  recorded              {recorder.snapshots:,} snapshots "
              f"-> {recorder.meta_path}")
    print("─" * 68)

    ok = True
    if clean_signals:
        print(f"  FAIL engine signalled on an arbitrage-free market: {clean_signals}")
        ok = False
    else:
        print(f"  PASS silent for {args.clean_seconds:.0f}s of arbitrage-free market "
              f"({clean_stats['passes']:,} passes)")
    if injected_trades < market.injected_total:
        print(f"  FAIL missed {market.injected_total - injected_trades} "
              f"injected arbitrage(s)")
        ok = False
    else:
        print(f"  PASS traded all {market.injected_total} injected arbitrages")
    if risk.state.session_pnl <= 0 and trades:
        print("  FAIL traded at a loss on guaranteed arbitrage")
        ok = False
    elif trades:
        print(f"  PASS every trade net positive "
              f"(₹{risk.state.session_pnl / trades:,.0f} avg)")
    print("─" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
