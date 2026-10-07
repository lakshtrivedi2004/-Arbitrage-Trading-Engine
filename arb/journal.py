"""Append-only trade journal: JSONL for machines, CSV for spreadsheets.

Written from the execution thread after the order is away, so journalling is
never in the latency path of a trade.
"""
import csv
import json
import os
import threading
from datetime import datetime

from arb.instruments import now_ist

CSV_COLUMNS = [
    "trade_id", "ts_ist", "executed_at", "mode", "underlying", "strategy",
    "expiry", "description", "lots",
    "leg_no", "symbol", "token", "opt", "side", "qty", "quote_price", "fill_price",
    "filled", "quoted_at", "leg_age_ms", "ms_before_fire", "quote_exch_ms",
    "gross_points", "gross_inr", "slippage_inr", "cost_inr", "net_inr", "capital_inr", "return_pct",
    "margin_inr", "settlement_inr", "capital_source",
    "stake_inr", "trade_pct_of_stake", "day_pct_of_stake",
    "contract_value", "tenor_days", "excess_return_pa",
    "tick_to_detect_ms", "detect_to_order_ms", "max_leg_age_ms", "leg_spread_ms",
    "session_pnl", "session_trades",
]


class Journal:
    def __init__(self, directory="journal", day=None):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        day = day or now_ist().strftime("%Y-%m-%d")
        self.jsonl_path = os.path.join(self.dir, f"trades_{day}.jsonl")
        self.csv_path = os.path.join(self.dir, f"trades_{day}.csv")
        self.rejects_path = os.path.join(self.dir, f"rejects_{day}.jsonl")
        self._lock = threading.Lock()
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="") as fh:
                csv.writer(fh).writerow(CSV_COLUMNS)

    def record_trade(self, fill):
        with self._lock:
            with open(self.jsonl_path, "a") as fh:
                fh.write(json.dumps(fill, default=str) + "\n")
        self._append_csv(fill)

    def _append_csv(self, fill):
        o = fill["opportunity"]
        with self._lock:
            with open(self.csv_path, "a", newline="") as fh:
                w = csv.writer(fh)
                for n, leg in enumerate(fill["legs"], 1):
                    w.writerow([
                        fill["trade_id"], fill["ts_ist"], fill.get("executed_at", ""),
                        fill["mode"], o.get("underlying", ""), o["strategy"],
                        o["expiry"], o["description"], fill["lots"], n,
                        leg["symbol"], leg["token"], leg.get("opt", ""),
                        leg["side"], leg["qty"],
                        f"{leg['quote_price']:.2f}", f"{leg['fill_price']:.2f}",
                        int(bool(leg.get("filled", True))),
                        leg.get("quoted_at", ""),
                        f"{leg.get('age_ms', 0):.1f}",
                        f"{leg.get('ms_before_fire', 0):.1f}",
                        leg.get("quote_exch_ms", 0),
                        f"{o['gross_edge_points']:.4f}", f"{fill['gross_inr']:.2f}",
                        f"{fill.get('realised_slippage_inr', 0.0):.2f}",
                        f"{fill['cost_inr']:.2f}", f"{fill['net_inr']:.2f}",
                        f"{fill.get('capital_inr', 0):.2f}",
                        f"{fill.get('return_pct', 0):.3f}",
                        f"{fill.get('margin_inr', 0) or 0:.2f}",
                        f"{fill.get('settlement_inr', 0) or 0:.2f}",
                        fill.get("capital_source", ""),
                        f"{fill.get('stake_inr', 0) or 0:.2f}",
                        f"{fill.get('trade_pct_of_stake', 0) or 0:.3f}",
                        f"{fill.get('day_pct_of_stake', 0) or 0:.3f}",
                        f"{fill.get('contract_value', 0) or 0:.2f}",
                        f"{fill.get('tenor_days', 0) or 0:.3f}",
                        f"{fill.get('excess_return_pa', 0) or 0:.3f}",
                        f"{fill['latency_ms']['tick_to_detect']:.4f}",
                        f"{fill['latency_ms']['detect_to_order']:.4f}",
                        f"{o['max_age_ms']:.1f}",
                        f"{fill.get('leg_spread_ms', 0):.1f}",
                        f"{fill['session_pnl']:.2f}", fill["session_trades"],
                    ])

    def rebuild_csv(self):
        """Regenerate this day's CSV from its JSONL (after a schema change or a
        backfill). The JSONL is the record; the CSV is a view of it."""
        if not os.path.exists(self.jsonl_path):
            return 0
        with self._lock:
            with open(self.jsonl_path) as fh:
                fills = [json.loads(line) for line in fh if line.strip()]
            with open(self.csv_path, "w", newline="") as fh:
                csv.writer(fh).writerow(CSV_COLUMNS)
        for f in fills:
            self._append_csv(f)
        return len(fills)

    def record_reject(self, opp_dict, reason):
        """Every signal that was found but not traded, and why. This is the file
        that tells you whether the engine is too tight or the market is simply
        efficient - the old bot logged nothing, so it could not tell."""
        with self._lock:
            with open(self.rejects_path, "a") as fh:
                fh.write(json.dumps(
                    {"ts_ist": now_ist().isoformat(), "reason": reason,
                     "opportunity": opp_dict}, default=str) + "\n")
