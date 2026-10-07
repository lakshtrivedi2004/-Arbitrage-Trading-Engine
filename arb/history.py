"""Paper-trading history across days.

The journal writes one file per day; this reads them back so a 30-day paper run
can be reviewed as a whole - yesterday's trades, the equity curve, which strategy
and which index actually produced the money.

Nothing here is derived from live state, so it survives restarts and can be read
after the fact by anything, including arb.research.
"""
import glob
import json
import os
from collections import defaultdict

from arb.instruments import now_ist

JOURNAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "journal")


def _day_of(path):
    return os.path.basename(path).replace("trades_", "").replace(".jsonl", "")


def load_trades(journal_dir=JOURNAL, days=30):
    """Every recorded trade, newest day first, capped at ``days`` files."""
    files = sorted(glob.glob(os.path.join(journal_dir, "trades_*.jsonl")), reverse=True)
    out = []
    for path in files[:days]:
        day = _day_of(path)
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        f = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    f["_day"] = day
                    out.append(f)
        except OSError:
            continue
    return out


def summarise(trades, total_capital=1_000_000.0, stake=100_000.0):
    """Per-day rollup plus the cumulative equity curve.

    Each day's profit is shown two ways: on the whole book, and on the fixed
    stake put to work on every trade - the owner's own yardstick.
    """
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["_day"]].append(t)

    days = []
    for day in sorted(by_day):
        ts = by_day[day]
        net = sum(t.get("net_inr", 0.0) for t in ts)
        gross = sum(t.get("gross_inr", 0.0) for t in ts)
        cost = sum(t.get("cost_inr", 0.0) for t in ts)
        cap = sum(t.get("capital_inr", 0.0) for t in ts)
        wins = sum(1 for t in ts if t.get("net_inr", 0.0) > 0)
        strat, idx = defaultdict(float), defaultdict(float)
        for t in ts:
            o = t.get("opportunity", {})
            strat[o.get("strategy", "?")] += t.get("net_inr", 0.0)
            idx[o.get("underlying", "?") or "?"] += t.get("net_inr", 0.0)
        days.append({
            "date": day,
            "trades": len(ts),
            "gross": round(gross, 2),
            "cost": round(cost, 2),
            "net": round(net, 2),
            "capital_used": round(cap, 2),
            # Return on the book for the day, which is what a 30-day paper run is
            # actually being judged on.
            "day_pct": round(100.0 * net / total_capital, 4) if total_capital else 0.0,
            "stake": stake,
            "stake_pct": round(100.0 * net / stake, 4) if stake else 0.0,
            "wins": wins,
            "losses": len(ts) - wins,
            "win_rate": round(100.0 * wins / len(ts), 1) if ts else 0.0,
            "by_strategy": {k: round(v, 2) for k, v in sorted(strat.items())},
            "by_index": {k: round(v, 2) for k, v in sorted(idx.items())},
        })

    equity, run = [], total_capital
    for d in days:
        run += d["net"]
        equity.append({"date": d["date"], "equity": round(run, 2),
                       "net": d["net"], "cum": round(run - total_capital, 2)})

    tot_net = sum(d["net"] for d in days)
    tot_trades = sum(d["trades"] for d in days)
    tot_wins = sum(d["wins"] for d in days)
    best = max(days, key=lambda d: d["net"]) if days else None
    worst = min(days, key=lambda d: d["net"]) if days else None
    return {
        "days": days,
        "equity_curve": equity,
        "totals": {
            "sessions": len(days),
            "trades": tot_trades,
            "net": round(tot_net, 2),
            "return_pct": round(100.0 * tot_net / total_capital, 4)
                          if total_capital else 0.0,
            "stake": stake,
            "stake_pct": round(100.0 * tot_net / stake, 4) if stake else 0.0,
            "avg_stake_pct": round(sum(d["stake_pct"] for d in days) / len(days), 4)
                             if days else 0.0,
            "win_rate": round(100.0 * tot_wins / tot_trades, 1) if tot_trades else 0.0,
            "avg_per_trade": round(tot_net / tot_trades, 2) if tot_trades else 0.0,
            "best_day": best["date"] if best else None,
            "best_net": best["net"] if best else 0.0,
            "worst_day": worst["date"] if worst else None,
            "worst_net": worst["net"] if worst else 0.0,
            "green_days": sum(1 for d in days if d["net"] > 0),
            "red_days": sum(1 for d in days if d["net"] < 0),
        },
    }


def day_trades(trades, day):
    """Flattened trades for one date, newest first - the 'what happened' view."""
    out = []
    for t in trades:
        if t["_day"] != day:
            continue
        o = t.get("opportunity", {})
        out.append({
            "id": t.get("trade_id"),
            "time": t.get("executed_at", t.get("ts_ist", "")[11:19]),
            "underlying": o.get("underlying", ""),
            "strategy": o.get("strategy", ""),
            "description": o.get("description", ""),
            "expiry": o.get("expiry", ""),
            "lots": t.get("lots", 1),
            "capital": round(t.get("capital_inr", 0.0), 2),
            "capital_source": t.get("capital_source", ""),
            "margin_inr": round(t.get("margin_inr", 0.0) or 0.0, 2),
            "settlement_inr": round(t.get("settlement_inr", 0.0) or 0.0, 2),
            "gross": round(t.get("gross_inr", 0.0), 2),
            "cost": round(t.get("cost_inr", 0.0), 2),
            "slip": round(t.get("realised_slippage_inr", 0.0), 2),
            "net": round(t.get("net_inr", 0.0), 2),
            "return_pct": t.get("return_pct", 0.0),
            "excess_return_pa": t.get("excess_return_pa"),
            "filled": t.get("all_filled", True),
            "detect_ms": round(t.get("latency_ms", {}).get("detect_to_order", 0.0), 2),
            "leg_spread_ms": t.get("leg_spread_ms", 0.0),
            "legs": [{"side": l.get("side"), "symbol": l.get("symbol"),
                      "token": str(l.get("token", "")), "strike": l.get("strike", 0.0),
                      "qty": l.get("qty"), "price": round(l.get("fill_price", 0.0), 2),
                      "opt": l.get("opt", ""), "quoted_at": l.get("quoted_at", ""),
                      "ms_before": round(l.get("ms_before_fire", 0.0)),
                      "filled": bool(l.get("filled", True))}
                     for l in t.get("legs", [])],
        })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


def intraday_curve(trades, day=None):
    """Cumulative P&L through a single session, one point per trade."""
    day = day or now_ist().strftime("%Y-%m-%d")
    ts = sorted((t for t in trades if t["_day"] == day),
                key=lambda t: t.get("ts_ist", ""))
    run, pts = 0.0, []
    for t in ts:
        run += t.get("net_inr", 0.0)
        pts.append({"time": t.get("executed_at", t.get("ts_ist", "")[11:19]),
                    "cum": round(run, 2), "net": round(t.get("net_inr", 0.0), 2),
                    "strategy": t.get("opportunity", {}).get("strategy", "")})
    return pts
