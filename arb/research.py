"""Turn recorded book data into calibrated strategy parameters.

The point of this module is to replace guesses with measurements. The old bot's
central parameter was a hardcoded "trade at 4% net ROI", a number with no
relationship to anything the market was doing. Everything below is derived from
what the book actually did:

  * persistence  - how long a violation stays on the screen. This sets the
                   latency budget: if the median violation lives 40 ms, a scanner
                   that needs 200 ms cannot trade it, and no threshold fixes that.
  * edge         - the distribution of violation sizes per strategy, so the
                   trading threshold sits above the noise floor rather than at a
                   round number someone liked.
  * spread/depth - observed touch size per strike, which is what slippage should
                   be calibrated to.
  * implied rate - the financing rate the option market is actually using, which
                   the engine then centres its discounting on.

Output is config/calibration.json, which the engine reads on start.
"""
import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

import numpy as np

from arb.config import CostModel, ROOT
from arb.costs import per_unit_cost_points
from arb.instruments import years_to_expiry
from arb.recorder import load_session
from arb.strategies import (ChainGeometry, ChainView, RateBand, implied_box_rate,
                            scan_box, scan_butterfly, scan_pcp_futures, scan_vertical)

SCAN_FNS = {"vertical": scan_vertical, "box": scan_box,
            "butterfly": scan_butterfly, "pcp_futures": scan_pcp_futures}


def frame_to_view(meta_exp, frame, geom, T, snapshot_hz):
    """Rebuild a ChainView from one recorded snapshot."""
    ce = np.asarray(meta_exp["ce_slots"], dtype=np.intp)
    pe = np.asarray(meta_exp["pe_slots"], dtype=np.intp)
    strikes = np.asarray(meta_exp["strikes"], dtype=float)
    # Column order matches recorder.COLUMNS.
    B, A, BQ, AQ, AGE = 1, 2, 3, 4, 7
    cv = ChainView(
        expiry=meta_exp["expiry"], T=T, strikes=strikes,
        lot_size=meta_exp["lot_size"],
        ce_bid=frame[ce, B].astype(float), ce_ask=frame[ce, A].astype(float),
        ce_bidq=frame[ce, BQ].astype(float), ce_askq=frame[ce, AQ].astype(float),
        pe_bid=frame[pe, B].astype(float), pe_ask=frame[pe, A].astype(float),
        pe_bidq=frame[pe, BQ].astype(float), pe_askq=frame[pe, AQ].astype(float),
        ce_age=frame[ce, AGE].astype(float), pe_age=frame[pe, AGE].astype(float),
        ce_symbol=meta_exp["ce_symbol"], pe_symbol=meta_exp["pe_symbol"],
        ce_token=meta_exp["ce_symbol"], pe_token=meta_exp["pe_symbol"],
        geom=geom,
    )
    fs = meta_exp.get("fut_slot")
    if fs is not None:
        cv.fut_bid = float(frame[fs, B]); cv.fut_ask = float(frame[fs, A])
        cv.fut_bidq = float(frame[fs, BQ]); cv.fut_askq = float(frame[fs, AQ])
        cv.fut_age = float(frame[fs, AGE])
        cv.fut_symbol = meta_exp.get("fut_symbol", "")
        cv.fut_token = cv.fut_symbol
        cv.has_future = cv.fut_bid > 0 and cv.fut_ask > 0
    return cv


def analyse(meta_paths, cm=None, max_age_ms=1e9, snapshot_hz=4.0, verbose=True):
    cm = cm or CostModel()
    rates = RateBand(0.055, 0.085)
    edges = defaultdict(list)
    spreads = defaultdict(list)
    depths = defaultdict(list)
    implied = defaultdict(list)
    # A violation is "the same one" while it keeps appearing on the same legs.
    open_runs = {}
    lifetimes = defaultdict(list)
    frames_seen = 0

    for mp in meta_paths:
        meta, data = load_session(mp)
        if verbose:
            print(f"  {os.path.basename(mp)}: {data.shape[0]} snapshots, "
                  f"{data.shape[1]} instruments")
        geoms = {e["expiry"]: ChainGeometry(np.asarray(e["strikes"], float))
                 for e in meta["expiries"]}
        Ts = {e["expiry"]: years_to_expiry(e["expiry"]) for e in meta["expiries"]}
        dt_ms = 1000.0 / snapshot_hz

        for f_i in range(data.shape[0]):
            frames_seen += 1
            frame = data[f_i]
            for e in meta["expiries"]:
                cv = frame_to_view(e, frame, geoms[e["expiry"]], Ts[e["expiry"]],
                                   snapshot_hz)
                two_sided = (cv.ce_bid > 0) & (cv.ce_ask > 0)
                if two_sided.any():
                    spreads["CE"].extend((cv.ce_ask - cv.ce_bid)[two_sided].tolist())
                    depths["CE"].extend(np.minimum(cv.ce_bidq, cv.ce_askq)[two_sided].tolist())
                ts = (cv.pe_bid > 0) & (cv.pe_ask > 0)
                if ts.any():
                    spreads["PE"].extend((cv.pe_ask - cv.pe_bid)[ts].tolist())
                    depths["PE"].extend(np.minimum(cv.pe_bidq, cv.pe_askq)[ts].tolist())

                r = implied_box_rate(cv, max_age_ms=max_age_ms)
                if r is not None:
                    implied[e["expiry"]].append(r)

                seen_now = set()
                for name, fn in SCAN_FNS.items():
                    n_opt = {"vertical": 2, "box": 4, "butterfly": 4, "pcp_futures": 2}[name]
                    n_fut = 1 if name == "pcp_futures" else 0
                    cp = per_unit_cost_points(200.0, n_opt, n_fut, cv.lot_size, cm)
                    # Measure raw violations: no cost floor, no minimum edge, so
                    # the distribution is the market's, not the filter's.
                    for o in fn(cv, rates, 0.0, cv.lot_size, max_age_ms, 0.0, -1e9):
                        if o.gross_edge_points <= 0:
                            continue
                        edges[o.strategy].append(o.gross_edge_points)
                        key = (o.strategy, tuple(sorted(l["symbol"] for l in o.legs)))
                        seen_now.add(key)
                        if key not in open_runs:
                            open_runs[key] = f_i
                for key in list(open_runs):
                    if key not in seen_now:
                        lifetimes[key[0]].append((f_i - open_runs.pop(key)) * dt_ms)

    for key, start in open_runs.items():
        lifetimes[key[0]].append((frames_seen - start) * dt_ms)

    def stats(v):
        if not v:
            return None
        v = sorted(v)
        return {
            "n": len(v), "min": round(v[0], 4),
            "p50": round(v[len(v) // 2], 4),
            "p90": round(v[int(len(v) * 0.90)], 4),
            "p99": round(v[int(len(v) * 0.99)], 4),
            "max": round(v[-1], 4), "mean": round(statistics.fmean(v), 4),
        }

    return {
        "frames": frames_seen,
        "edge_points": {k: stats(v) for k, v in sorted(edges.items())},
        "lifetime_ms": {k: stats(v) for k, v in sorted(lifetimes.items())},
        "spread_points": {k: stats(v) for k, v in sorted(spreads.items())},
        "touch_depth": {k: stats(v) for k, v in sorted(depths.items())},
        "implied_rate": {k: stats(v) for k, v in sorted(implied.items())},
    }


def calibrate(report, cm=None):
    """Derive engine settings from the measurements.

    Each number below is traceable to something observed, and the reasoning is
    recorded alongside it so a later session can tell why a threshold is what it is.
    """
    cm = cm or CostModel()
    out = {"derived_from_frames": report["frames"], "rationale": {}}

    # Slippage: half the observed touch spread is what crossing it actually costs.
    sp = [report["spread_points"].get(k) for k in ("CE", "PE")]
    sp = [s for s in sp if s]
    if sp:
        median_spread = statistics.fmean(s["p50"] for s in sp)
        ticks = max(1.0, round((median_spread / 2.0) / cm.tick_size, 1))
        out["slippage_ticks_per_leg"] = ticks
        out["rationale"]["slippage_ticks_per_leg"] = (
            f"median touch spread {median_spread:.2f} pts; crossing it costs half, "
            f"= {ticks} ticks of {cm.tick_size}")

    # Latency budget: we must detect and fire inside the median violation's life.
    lives = [v["p50"] for v in report["lifetime_ms"].values() if v]
    if lives:
        budget = min(lives)
        out["max_quote_age_ms"] = round(max(50.0, min(budget / 2.0, 1000.0)), 0)
        out["rationale"]["max_quote_age_ms"] = (
            f"shortest median violation lifetime {budget:.0f} ms; a quote older "
            f"than half of that is likely describing a book that has already moved")

    # Trading threshold per strategy: sit above the noise floor, not at a round
    # number. p90 of observed violations is the level that is rare enough to be
    # real but common enough to trade.
    thresholds = {}
    for strat, st in report["edge_points"].items():
        if not st:
            continue
        n_opt = 4 if ("BOX" in strat or "BUTTERFLY" in strat) else 2
        n_fut = 1 if strat.startswith("PCP") else 0
        cost = per_unit_cost_points(200.0, n_opt, n_fut, 65, cm)
        thresholds[strat] = round(max(st["p90"], cost * 1.5), 3)
    if thresholds:
        out["min_edge_points_by_strategy"] = thresholds
        out["rationale"]["min_edge_points_by_strategy"] = (
            "p90 of observed violation size for that strategy, floored at 1.5x its "
            "own round-trip cost")

    # Financing: centre the discount band on what the market itself implies.
    rates = [v["p50"] for v in report["implied_rate"].values() if v]
    if rates:
        mid = statistics.fmean(rates)
        out["risk_free_rate"] = round(mid, 5)
        out["rationale"]["risk_free_rate"] = (
            f"median box-implied financing rate across expiries = {mid:.4f}, "
            f"measured rather than assumed from the repo rate")

    # Size: never ask for more than the book routinely shows at the touch.
    dp = [report["touch_depth"].get(k) for k in ("CE", "PE")]
    dp = [d for d in dp if d]
    if dp:
        p50_depth = min(d["p50"] for d in dp)
        out["observed_touch_depth_p50"] = p50_depth
        out["rationale"]["observed_touch_depth_p50"] = (
            f"median size at the touch is {p50_depth:.0f}; sizing above this "
            f"will not fill at the quoted price")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meta", nargs="+", help="data/book_*.meta.json files")
    ap.add_argument("--hz", type=float, default=4.0, help="recorder snapshot rate")
    ap.add_argument("--out", default=os.path.join(ROOT, "config", "calibration.json"))
    ap.add_argument("--apply", action="store_true",
                    help="write the derived values into config/ as well")
    args = ap.parse_args()

    cm = CostModel.load()
    print(f"Analysing {len(args.meta)} recorded session(s)...")
    report = analyse(args.meta, cm, snapshot_hz=args.hz)
    cal = calibrate(report, cm)

    def show(title, block, unit=""):
        if not block:
            return
        print(f"\n{title}")
        for k, v in block.items():
            if not v:
                continue
            print(f"  {k:<22} n={v['n']:<7} p50={v['p50']:<10} p90={v['p90']:<10} "
                  f"max={v['max']}{unit}")

    print(f"\nFrames analysed: {report['frames']:,}")
    show("Violation size (points, before costs)", report["edge_points"])
    show("Violation lifetime (ms)", report["lifetime_ms"])
    show("Touch spread (points)", report["spread_points"])
    show("Touch depth (units)", report["touch_depth"])
    show("Box-implied financing rate", report["implied_rate"])

    print("\nDerived settings:")
    for k, v in cal.items():
        if k == "rationale":
            continue
        print(f"  {k} = {v}")
        if k in cal["rationale"]:
            print(f"      ↳ {cal['rationale'][k]}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"report": report, "calibration": cal}, fh, indent=2)
    print(f"\nWritten to {args.out}")

    if args.apply:
        if "slippage_ticks_per_leg" in cal:
            cm.slippage_ticks_per_leg = cal["slippage_ticks_per_leg"]
            cm.save()
            print("Applied slippage to config/costs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
