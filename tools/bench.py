#!/usr/bin/env python3
"""Latency benchmark for the detection path."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from arb.config import CostModel, RiskLimits
from arb.costs import per_unit_cost_points
from arb.strategies import (RateBand, scan_box, scan_butterfly, scan_pcp_futures,
                            scan_vertical, implied_box_rate)
from tests.synth import make_chain

R = RateBand(0.055, 0.085)
CM = CostModel()


def timeit(fn, n=400):
    fn()
    t = time.perf_counter_ns()
    for _ in range(n):
        fn()
    return (time.perf_counter_ns() - t) / n / 1000.0


def main():
    for n_strikes in (13, 25, 41, 61):
        cv = make_chain(n=n_strikes)
        cp = per_unit_cost_points(300.0, 4, 0, 65, CM)
        args = (cv, R, cp, 65, 400.0, 1.0, 0.05)
        parts = {
            "vertical": lambda: scan_vertical(*args),
            "box": lambda: scan_box(*args),
            "butterfly": lambda: scan_butterfly(*args),
            "pcp_futures": lambda: scan_pcp_futures(*args),
            "implied_rate": lambda: implied_box_rate(cv),
        }
        times = {k: timeit(v) for k, v in parts.items()}
        total = sum(v for k, v in times.items() if k != "implied_rate")
        print(f"\n{n_strikes} strikes ({n_strikes * (n_strikes - 1) // 2} pairs):")
        for k, v in sorted(times.items(), key=lambda x: -x[1]):
            print(f"   {k:<14} {v:8.1f} µs")
        print(f"   {'ALL SCANNERS':<14} {total:8.1f} µs")


def bench_engine():
    """Full ArbEngine.scan_once, the number that actually matters."""
    import cProfile, pstats, io
    from arb.book import QuoteStore
    from arb.config import EngineConfig, RiskLimits
    from arb.engine import ArbEngine, ChainBinding
    from arb.execution import ExecutionEngine, PaperBroker
    from arb.journal import Journal
    from arb.risk import RiskManager

    n = 25
    strikes = [24500 + (i - n // 2) * 50 for i in range(n)]
    store = QuoteStore(2 * n + 4)
    ce = [store.assign(f"CE{i}") for i in range(n)]
    pe = [store.assign(f"PE{i}") for i in range(n)]
    fut = store.assign("FUT")
    cv = make_chain(n=n)
    now = time.perf_counter_ns()
    for i in range(n):
        store.update_from_tick(ce[i], cv.ce_bid[i], cv.ce_ask[i], 2000, 2000,
                               cv.ce_bid[i], now_ns=now)
        store.update_from_tick(pe[i], cv.pe_bid[i], cv.pe_ask[i], 2000, 2000,
                               cv.pe_bid[i], now_ns=now)
    store.update_from_tick(fut, 24600, 24601, 2000, 2000, 24600, now_ns=now)

    class F:
        pass
    f = F(); f.store = store

    binding = ChainBinding("15SEP2026", strikes, ce, pe,
                           [f"C{i}" for i in range(n)], [f"P{i}" for i in range(n)],
                           [f"CE{i}" for i in range(n)], [f"PE{i}" for i in range(n)],
                           65, fut, "FUT", "FUT")
    cfg = EngineConfig()
    limits = RiskLimits()
    risk = RiskManager(limits, CM)
    j = Journal(directory="journal/bench")
    eng = ArbEngine(f, [binding], cfg, CM, limits, risk,
                    ExecutionEngine(PaperBroker(CM), CM, risk, j, None, 65),
                    j, None, log=lambda *_: None)

    fn = lambda: eng.scan_once(65, 0.05)
    print(f"\nArbEngine.scan_once (25 strikes, all scanners): {timeit(fn, 400):.1f} µs")

    pr = cProfile.Profile()
    pr.enable()
    for _ in range(400):
        fn()
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(12)
    print("\n".join(s.getvalue().splitlines()[4:22]))


if __name__ == "__main__":
    if "--engine" in sys.argv:
        bench_engine()
    else:
        main()
        bench_engine()
