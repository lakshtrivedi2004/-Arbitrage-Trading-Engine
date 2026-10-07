"""Re-state past paper trades on the margin Angel One actually blocks.

Trades journalled before 11 Sep 2026 recorded "invested" as the structure's
settlement obligation (Rs 13,000 for a one-lot NIFTY box). The account would
have needed Rs 83,656 of margin for the same legs. This asks Angel One's margin
calculator for each past trade's exact legs and rewrites capital_inr,
return_pct and the deployed-capital running total, marking every touched record
as backfilled. The JSONL is rewritten in place (a .bak copy is kept) and the CSV
regenerated from it.

    venv/bin/python tools/backfill_margin.py 2026-09-10 2026-09-11
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb.capital import structure_capital
from arb.config import Credentials, RiskLimits
from arb.feed import AngelSession
from arb.instruments import now_ist
from arb.journal import Journal
from arb.margin import MarginService


def main(days):
    creds = Credentials.load()
    session = AngelSession(creds).login()
    limits = RiskLimits()
    ms = MarginService(session.api, [], limits, log=print)
    stamp = now_ist().strftime("%Y-%m-%d %H:%M")

    for day in days:
        path = os.path.join("journal", f"trades_{day}.jsonl")
        if not os.path.exists(path):
            print(f"{day}: no journal")
            continue
        shutil.copy(path, path + ".bak")
        with open(path) as fh:
            fills = [json.loads(l) for l in fh if l.strip()]
        deployed = 0.0
        for f in fills:
            legs = [l for l in f["legs"] if l.get("filled", True)]
            for l in legs:
                l.setdefault("exchange", "NFO")
            settlement = structure_capital(legs) if legs else 0.0
            ex = ms.exact(legs) if legs else None
            before = f.get("capital_inr", 0.0)
            if ex is None:
                print(f"  {day} #{f['trade_id']}: margin API gave no answer "
                      f"({ms.last_error}); left at {before:,.2f}")
                deployed += before
                continue
            cap = max(settlement, ex["total"])
            f["settlement_inr"] = settlement
            f["margin_inr"] = ex["total"]
            f["margin_components"] = ex["components"]
            f["margin_product"] = ex["product"]
            f["capital_inr"] = cap
            f["capital_source"] = "angel_one"
            f["capital_backfilled_at"] = stamp
            f["return_pct"] = round(100.0 * f["net_inr"] / cap, 3) if cap > 1 else 0.0
            f["free_carry"] = cap <= 1.0
            deployed += cap
            f["capital_deployed"] = deployed
            f.setdefault("opportunity", {}).setdefault("detail", {})["capital_inr"] = cap
            print(f"  {day} #{f['trade_id']} {f['opportunity']['description']:<26} "
                  f"invested {before:>10,.2f} -> {cap:>10,.2f}   "
                  f"return {f['return_pct']:.2f}%")
        with open(path, "w") as fh:
            for f in fills:
                fh.write(json.dumps(f, default=str) + "\n")
        n = Journal("journal", day=day).rebuild_csv()
        print(f"{day}: {n} trade(s) restated; CSV rebuilt")


if __name__ == "__main__":
    main(sys.argv[1:] or [now_ist().strftime("%Y-%m-%d")])
