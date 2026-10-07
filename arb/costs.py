"""Exact and vectorised Indian F&O transaction costs.

Two entry points:
  * ``leg_cost`` / ``structure_cost`` - exact rupee cost, used to confirm a
    candidate before it is allowed to trade.
  * ``per_unit_cost_points`` - a linear per-unit approximation used inside the
    numpy hot loop so thousands of candidates can be filtered without a Python
    call per candidate. It is built to *overstate* cost slightly, so anything it
    rejects would also have been rejected by the exact model.
"""
from arb.config import CostModel


def leg_cost(price, qty, side, instrument="OPT", cm: CostModel = None):
    """Exact statutory + brokerage cost of one leg, in rupees."""
    cm = cm or CostModel()
    turnover = abs(price) * qty
    sell = side == "SELL"

    brokerage = cm.brokerage_per_order
    if instrument == "OPT":
        stt = turnover * cm.opt_stt_sell if sell else 0.0
        exch = turnover * cm.opt_exchange_txn
        stamp = 0.0 if sell else turnover * cm.opt_stamp_buy
    else:
        stt = turnover * cm.fut_stt_sell if sell else 0.0
        exch = turnover * cm.fut_exchange_txn
        stamp = 0.0 if sell else turnover * cm.fut_stamp_buy

    sebi = turnover * cm.sebi_fee
    ipft = turnover * cm.ipft
    gst = (brokerage + exch + sebi + ipft) * cm.gst_rate
    return brokerage + stt + exch + stamp + sebi + ipft + gst


def structure_cost(legs, cm: CostModel = None):
    """Exact cost of a multi-leg structure including modelled slippage.

    ``legs`` are dicts with price/qty/side/instrument. Slippage is charged as
    ``slippage_points`` of adverse price movement per leg per unit, on top of the
    quote we were already crossing.
    """
    cm = cm or CostModel()
    fees = sum(leg_cost(l["price"], l["qty"], l["side"], l.get("instrument", "OPT"), cm)
               for l in legs)
    slip = sum(cm.slippage_points() * l["qty"] for l in legs)
    return fees + slip


def per_unit_cost_points(price_scale, n_option_legs, n_future_legs=0,
                         qty=1, cm: CostModel = None):
    """Upper-bound cost of a structure expressed in *price points per unit*.

    ``price_scale`` is a representative per-unit premium for the option legs; it
    only needs to bound the turnover-proportional part, so the caller passes a
    generous value. Fixed brokerage is amortised over ``qty``.
    """
    cm = cm or CostModel()
    # Worst case: treat every option leg as a sell (STT applies) *and* charge the
    # buy-side stamp duty, which overstates cost and therefore never invents edge.
    per_opt = price_scale * (cm.opt_stt_sell + cm.opt_exchange_txn + cm.opt_stamp_buy
                             + cm.sebi_fee + cm.ipft)
    per_fut = price_scale * (cm.fut_stt_sell + cm.fut_exchange_txn + cm.fut_stamp_buy
                             + cm.sebi_fee + cm.ipft)
    gst_part = price_scale * (cm.opt_exchange_txn + cm.sebi_fee + cm.ipft) * cm.gst_rate
    variable = n_option_legs * (per_opt + gst_part) + n_future_legs * per_fut
    fixed = (n_option_legs + n_future_legs) * (
        cm.brokerage_per_order * (1.0 + cm.gst_rate)) / max(1, qty)
    slippage = (n_option_legs + n_future_legs) * cm.slippage_points()
    return variable + fixed + slippage
