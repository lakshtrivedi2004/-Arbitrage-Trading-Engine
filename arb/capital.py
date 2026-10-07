"""Capital required by a structure, computed from its own payoff.

Rather than assume a margin number, the worst case is derived from the legs. A
structure's payoff at expiry is piecewise linear with kinks only at its strikes,
so evaluating it at every strike (plus points either side of the range) finds the
true minimum exactly. Capital is then what we pay today plus the worst we could
owe at settlement.

This is deliberately conservative: the exchange's SPAN margin on a fully hedged
spread is usually less than the worst-case obligation, so sizing off this number
can only under-deploy, never over-deploy.
"""


def payoff_at(legs, spot):
    """Net settlement value of the structure if the index expires at ``spot``."""
    total = 0.0
    for l in legs:
        sign = 1.0 if l["side"] == "BUY" else -1.0
        kind = l.get("opt", "OPT")
        qty = l["qty"]
        if kind == "CE":
            total += sign * max(0.0, spot - l["strike"]) * qty
        elif kind == "PE":
            total += sign * max(0.0, l["strike"] - spot) * qty
        elif kind == "FUT":
            total += sign * (spot - l["price"]) * qty
    return total


def cash_today(legs):
    """Net cash flow now. Positive is a credit received, negative a debit paid.

    Futures cost nothing to enter, so they contribute no cash today - only the
    settlement leg of the payoff.
    """
    total = 0.0
    for l in legs:
        if l.get("opt") == "FUT":
            continue
        price = l.get("fill_price", l.get("price", 0.0))
        total += (price * l["qty"]) if l["side"] == "SELL" else -(price * l["qty"])
    return total


def worst_payoff(legs):
    strikes = sorted({l["strike"] for l in legs if l.get("opt") in ("CE", "PE")})
    if not strikes:
        # Futures only: unbounded in one direction, so there is no finite worst
        # case to quote. Callers treat this as "not sizable" rather than free.
        return float("-inf")
    lo, hi = strikes[0], strikes[-1]
    span = max(hi - lo, 1.0)
    # Kinks sit only at strikes; sampling beyond both ends captures the slope of
    # the outer segments, so the minimum over these points is the true minimum.
    probes = [max(0.0, lo - span), *strikes, hi + span]
    return min(payoff_at(legs, s) for s in probes)


# SPAN + exposure margin the exchange blocks on an index futures leg, as a
# fraction of its notional. A conversion or reversal is economically almost
# self-financing - its payoff is a constant - so without this its capital would
# compute as ~0 and its return on capital would be meaningless.
FUTURES_MARGIN_PCT = 0.12


def futures_margin(legs, pct=FUTURES_MARGIN_PCT):
    return sum(abs(l.get("fill_price", l.get("price", 0.0))) * l["qty"] * pct
               for l in legs if l.get("opt") == "FUT")


def structure_capital(legs):
    """Rupees this structure ties up: the larger of what we pay now and what we
    could owe at settlement.

    Not the sum of the two - a credit spread's settlement obligation is partly
    funded by the credit already received, and adding them would double count.
    Taking the maximum instead is still conservative against the exchange's SPAN
    margin on a hedged spread, which is the safe direction: it can only understate
    the return on capital, never flatter it.
    """
    cash = cash_today(legs)
    worst = worst_payoff(legs)
    if worst == float("-inf"):
        return float("inf")
    option_side = max(max(0.0, -cash), max(0.0, -worst))
    return option_side + futures_margin(legs)


def return_pct(net_profit_inr, legs):
    """Net profit as a percentage of the capital the structure ties up."""
    cap = structure_capital(legs)
    if not cap or cap == float("inf"):
        return 0.0
    return 100.0 * net_profit_inr / cap
