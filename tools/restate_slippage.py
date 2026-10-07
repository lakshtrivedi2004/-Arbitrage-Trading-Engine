"""Charge modelled slippage against past paper trades' locked edge.

Until 11 Sep 2026 the journal's net_inr was gross edge at the quotes less
statutory costs; the paper broker's own slippage (fills 0.05 past the quote on
every leg) was recorded as a separate figure but never deducted, so each trade
was overstated by ~Rs 13. This recomputes realised_slippage_inr as
sum(|fill - quote| x qty), deducts it, and re-derives every field that depends
on net: return on margin, annualised excess yield, stake percentages and the
day's running P&L. JSONL rewritten in place (.bak kept), CSV regenerated.

    venv/bin/python tools/restate_slippage.py 2026-09-10 2026-09-11
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb.config import RiskLimits
from arb.instruments import now_ist
from arb.journal import Journal


def main(days):
    L = RiskLimits()
    for day in days:
        path = os.path.join("journal", f"trades_{day}.jsonl")
        if not os.path.exists(path):
            print(f"{day}: no journal")
            continue
        shutil.copy(path, path + ".bak2")
        with open(path) as fh:
            fills = [json.loads(l) for l in fh if l.strip()]
        run = 0.0
        for f in fills:
            legs = [l for l in f["legs"] if l.get("filled", True)]
            slip = sum(abs(float(l.get("fill_price", 0)) - float(l.get("quote_price", l.get("price", 0))))
                       * float(l["qty"]) for l in legs)
            before = f["net_inr"]
            f["realised_slippage_inr"] = round(slip, 2)
            f["net_inr"] = f["gross_inr"] - slip - f["cost_inr"] if f.get("all_filled", True) \
                else -slip - f["cost_inr"]
            cap = f.get("capital_inr", 0.0)
            f["return_pct"] = round(100.0 * f["net_inr"] / cap, 3) if cap > 1 else 0.0
            cv = f.get("contract_value")
            if cv:
                days_t = max(f.get("tenor_days", 0.0), L.min_tenor_days)
                f["excess_return_pa"] = round(100.0 * f["net_inr"] / cv * 365.0 / days_t, 3)
            run += f["net_inr"]
            f["session_pnl"] = run
            f["equity"] = f.get("total_capital", L.total_capital) + run
            f["stake_inr"] = L.stake_per_trade
            f["trade_pct_of_stake"] = round(100.0 * f["net_inr"] / L.stake_per_trade, 3)
            f["day_pct_of_stake"] = round(100.0 * run / L.stake_per_trade, 3)
            f["slippage_restated_at"] = now_ist().strftime("%Y-%m-%d %H:%M")
            print(f"  {day} #{f['trade_id']} slippage ₹{slip:7.2f}   net {before:8.2f} -> {f['net_inr']:8.2f}")
        with open(path, "w") as fh:
            for f in fills:
                fh.write(json.dumps(f, default=str) + "\n")
        n = Journal("journal", day=day).rebuild_csv()
        print(f"{day}: {n} trade(s) restated, day P&L ₹{run:,.2f}; CSV rebuilt")


if __name__ == "__main__":
    main(sys.argv[1:] or [now_ist().strftime("%Y-%m-%d")])
