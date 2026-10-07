"""Vectorised no-arbitrage scanners.

Every scanner is model-free: it prices a structure whose payoff is bounded by
arithmetic alone, never by an option model. No Black-Scholes, no implied vol, no
assumption about where the market "should" be - only relations that must hold or
money is lying on the table.

All scanners share one contract:
    inputs  - a ChainView of gathered numpy arrays plus a RateBand
    output  - a list of Opportunity, each with an exact leg list, the gross edge
              in points per unit, and the worst-case quote age of its legs.

Everything is computed over whole strike grids at once. The per-candidate Python
cost is paid only for the handful that survive the numpy filter.
"""
import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class RateBand:
    """Financing rates. Conservatism in a box is directional, so the two sides of
    a structure are discounted at different rates: cash we must fund is charged
    the borrow rate, cash we receive earns only the lend rate. Using one repo
    number for both, as the old bot did, invents edge on whichever side is wrong.
    """
    lend: float = 0.055
    borrow: float = 0.085

    def df_asset(self, T):
        """Discount factor for money we will *receive* at expiry (conservative:
        discount it hard, so we do not overpay for it today)."""
        return math.exp(-self.borrow * T)

    def df_liability(self, T):
        """Discount factor for money we will *owe* at expiry (conservative:
        discount it lightly, so we do not undercharge for taking it on)."""
        return math.exp(-self.lend * T)


class ChainGeometry:
    """Index grids that depend only on the strike ladder.

    The ladder is fixed for a session, so every pair and triple index, every
    spread width and every equal-spacing test is computed once here instead of
    being rebuilt on each pass. On small arrays numpy's per-call dispatch cost
    dominates the arithmetic, so hoisting these out is most of the scan budget.
    """
    __slots__ = ("n", "strikes", "pair_i", "pair_j", "pair_width", "flies")

    def __init__(self, strikes, max_wing=4):
        self.strikes = np.asarray(strikes, dtype=float)
        self.n = n = len(self.strikes)
        self.pair_i, self.pair_j = np.triu_indices(n, k=1)
        self.pair_width = self.strikes[self.pair_j] - self.strikes[self.pair_i]
        self.flies = []
        for k in range(1, min(max_wing, max(1, (n - 1) // 2)) + 1):
            lo = np.arange(0, n - 2 * k)
            if lo.size == 0:
                continue
            mid, hi = lo + k, lo + 2 * k
            spaced = np.isclose(self.strikes[mid] - self.strikes[lo],
                                self.strikes[hi] - self.strikes[mid])
            if not spaced.any():
                continue
            # Keep only the equally spaced triples, so the mask never has to be
            # applied again at scan time.
            lo, mid, hi = lo[spaced], mid[spaced], hi[spaced]
            self.flies.append((lo, mid, hi))


@dataclass
class ChainView:
    """One expiry's option grid, gathered into contiguous arrays."""
    expiry: str
    T: float
    strikes: np.ndarray
    lot_size: int
    ce_bid: np.ndarray
    ce_ask: np.ndarray
    ce_bidq: np.ndarray
    ce_askq: np.ndarray
    pe_bid: np.ndarray
    pe_ask: np.ndarray
    pe_bidq: np.ndarray
    pe_askq: np.ndarray
    ce_age: np.ndarray
    pe_age: np.ndarray
    ce_symbol: list
    pe_symbol: list
    ce_token: list
    pe_token: list
    fut_bid: float = 0.0
    fut_ask: float = 0.0
    fut_bidq: float = 0.0
    fut_askq: float = 0.0
    fut_age: float = 1e9
    fut_symbol: str = ""
    fut_token: str = ""
    has_future: bool = False
    underlying: str = "NIFTY"
    segment: str = "NFO"
    ce_exch: np.ndarray = None
    pe_exch: np.ndarray = None
    fut_exch: int = 0
    # Wall-clock epoch seconds at which this snapshot was gathered. Each leg's
    # quote time is this minus that leg's age, so a structure carries the actual
    # instant every one of its prices was observed - the only way to tell after
    # the fact whether the legs were really simultaneous.
    now_wall: float = 0.0
    geom: "ChainGeometry" = None
    _masks: dict = field(default_factory=dict, repr=False)

    def geometry(self):
        if self.geom is None:
            self.geom = ChainGeometry(self.strikes)
        return self.geom

    def mask(self, kind, qty, max_age_ms, min_depth_mult):
        """Tradability mask for one option side, memoised for this snapshot.

        Every scanner needs the same masks, and building them costs more than the
        arithmetic that follows, so they are computed once per pass and shared.
        ``kind`` is 'CE' or 'PE'; ``qty`` differs for a butterfly body.
        """
        key = (kind, qty, max_age_ms, min_depth_mult)
        m = self._masks.get(key)
        if m is None:
            if kind == "CE":
                m = _tradable(self.ce_bid, self.ce_ask, self.ce_bidq, self.ce_askq,
                              self.ce_age, max_age_ms, qty, min_depth_mult)
            else:
                m = _tradable(self.pe_bid, self.pe_ask, self.pe_bidq, self.pe_askq,
                              self.pe_age, max_age_ms, qty, min_depth_mult)
            self._masks[key] = m
        return m

    def freeze(self):
        """Mark this snapshot immutable.

        The mask cache above is only sound if the view never changes after it is
        first scanned. Rather than rely on that by convention, the arrays are made
        read-only so a stray mutation raises instead of silently returning a mask
        that no longer matches the data - which, for the staleness mask, would
        mean trading on quotes the engine believed it had rejected.
        """
        for a in (self.ce_bid, self.ce_ask, self.ce_bidq, self.ce_askq,
                  self.pe_bid, self.pe_ask, self.pe_bidq, self.pe_askq,
                  self.ce_age, self.pe_age):
            a.setflags(write=False)
        return self

    def any_tradable(self, qty, max_age_ms, min_depth_mult):
        """Cheap early-out: nothing here can trade unless two strikes are live."""
        return (self.mask("CE", qty, max_age_ms, min_depth_mult).sum() >= 2
                or self.mask("PE", qty, max_age_ms, min_depth_mult).sum() >= 2)


@dataclass
class Opportunity:
    strategy: str
    expiry: str
    description: str
    legs: list
    gross_edge_points: float          # per unit, before costs
    est_cost_points: float            # per unit, approximate
    qty: int
    max_age_ms: float
    min_depth_ratio: float
    detail: dict = field(default_factory=dict)
    T: float = 0.0                    # years to expiry, for annualising the edge

    @property
    def leg_spread_ms(self):
        """Milliseconds between the oldest and newest leg quote.

        This is the synchronicity of the structure, and it matters more than any
        single leg's age: an arbitrage priced from legs observed seconds apart is
        not an arbitrage, it is four unrelated prices. The old bot's 4-leg boxes
        were assembled from REST polls seconds apart and this number would have
        exposed that immediately.
        """
        ages = [l.get("age_ms", 0.0) for l in self.legs]
        return (max(ages) - min(ages)) if ages else 0.0

    @property
    def gross_inr(self):
        return self.gross_edge_points * self.qty

    @property
    def est_net_inr(self):
        return (self.gross_edge_points - self.est_cost_points) * self.qty


def _tradable(bid, ask, bidq, askq, age, max_age_ms, need_qty, min_depth_mult):
    """A quote is usable only if it is two-sided, uncrossed, fresh and deep enough."""
    need = need_qty * min_depth_mult
    return ((bid > 0) & (ask > 0) & (ask >= bid) & (age <= max_age_ms)
            & (bidq >= need) & (askq >= need))


def _top(cand, net, top_k):
    """Indices of the best candidates, most profitable first.

    Without a cap, a badly dislocated chain (one frozen quote can violate a bound
    against every other strike) would materialise thousands of Opportunity objects
    on the scan thread. We only ever act on the best few, so the rest are pure
    allocation in the latency path.
    """
    idx = np.flatnonzero(cand)
    if top_k and idx.size > top_k:
        idx = idx[np.argpartition(-net[idx], top_k - 1)[:top_k]]
    return idx


def _pair_grid(n):
    """Upper-triangular index grids for all i<j strike pairs."""
    i, j = np.triu_indices(n, k=1)
    return i, j


# ─────────────────────────────────────────────────────────────────────────────
# S1. Vertical spread bounds  (2 legs - cheapest and fastest to execute)
# ─────────────────────────────────────────────────────────────────────────────
def scan_vertical(cv: ChainView, rates: RateBand, cost_points, qty,
                  max_age_ms, min_depth_mult, min_edge_points, top_k=8):
    """A call spread must be worth between 0 and PV(K2-K1); a put spread likewise.

    Two violations are tradable:
      LOW  - the spread can be bought for a *credit*. Payoff is never negative,
             so being paid to hold it is free money.
      HIGH - the spread can be sold for more than the largest amount it can ever
             cost to settle.

    This is the highest-frequency family in the book: only two legs, so half the
    execution risk and half the fixed cost of a box.
    """
    out = []
    n = len(cv.strikes)
    if n < 2:
        return out
    g = cv.geometry()
    i, j, width = g.pair_i, g.pair_j, g.pair_width
    pv_asset = width * rates.df_asset(cv.T)
    pv_liab = width * rates.df_liability(cv.T)

    for kind, bid, ask, bidq, askq, age, syms, toks in (
        ("CE", cv.ce_bid, cv.ce_ask, cv.ce_bidq, cv.ce_askq, cv.ce_age, cv.ce_symbol, cv.ce_token),
        ("PE", cv.pe_bid, cv.pe_ask, cv.pe_bidq, cv.pe_askq, cv.pe_age, cv.pe_symbol, cv.pe_token),
    ):
        ok = cv.mask(kind, qty, max_age_ms, min_depth_mult)
        pair_ok = ok[i] & ok[j]
        if not pair_ok.any():
            continue

        # For calls the low strike is the expensive one; for puts it is the high
        # strike. `lo`/`hi` name the leg we are long in the debit direction.
        if kind == "CE":
            long_leg, short_leg = i, j          # buy K_i call, sell K_j call
        else:
            long_leg, short_leg = j, i          # buy K_j put,  sell K_i put

        # LOW violation: buying the spread yields a credit.
        credit_to_own = bid[short_leg] - ask[long_leg]
        edge_low = credit_to_own
        # HIGH violation: selling the spread pays more than its worst settlement.
        credit_to_sell = bid[long_leg] - ask[short_leg]
        edge_high = credit_to_sell - pv_liab

        for tag, edge, buy_idx, sell_idx in (
            ("LOW", edge_low, long_leg, short_leg),
            ("HIGH", edge_high, short_leg, long_leg),
        ):
            net = edge - cost_points
            cand = pair_ok & (net >= min_edge_points) & np.isfinite(net)
            if not cand.any():
                continue
            for c in _top(cand, net, top_k):
                bi, si = int(buy_idx[c]), int(sell_idx[c])
                exch = cv.ce_exch if kind == "CE" else cv.pe_exch
                legs = [
                    {"symbol": syms[bi], "token": toks[bi], "side": "BUY",
                     "price": float(ask[bi]), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                     "strike": float(cv.strikes[bi]), "opt": kind,
                     "depth": float(askq[bi]), "age_ms": float(age[bi]),
                     "quote_ts": cv.now_wall - float(age[bi]) / 1000.0,
                     "quote_exch_ms": int(exch[bi]) if exch is not None else 0},
                    {"symbol": syms[si], "token": toks[si], "side": "SELL",
                     "price": float(bid[si]), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                     "strike": float(cv.strikes[si]), "opt": kind,
                     "depth": float(bidq[si]), "age_ms": float(age[si]),
                     "quote_ts": cv.now_wall - float(age[si]) / 1000.0,
                     "quote_exch_ms": int(exch[si]) if exch is not None else 0},
                ]
                out.append(Opportunity(
                    strategy=f"VERTICAL_{kind}_{tag}",
                    expiry=cv.expiry,
                    T=cv.T,
                    description=(f"{kind} vertical {cv.strikes[bi]:.0f}/{cv.strikes[si]:.0f} "
                                 f"bound violated ({tag})"),
                    legs=legs,
                    gross_edge_points=float(edge[c]),
                    est_cost_points=cost_points,
                    qty=qty,
                    max_age_ms=float(max(age[bi], age[si])),
                    min_depth_ratio=float(min(askq[bi], bidq[si]) / qty),
                    detail={"width": float(width[c]), "pv_bound": float(pv_asset[c])},
                ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# S2. Box spread  (4 legs - the classic, and the one most often faked)
# ─────────────────────────────────────────────────────────────────────────────
def scan_box(cv: ChainView, rates: RateBand, cost_points, qty,
             max_age_ms, min_depth_mult, min_edge_points, top_k=8):
    out = []
    n = len(cv.strikes)
    if n < 2:
        return out
    ok = (cv.mask("CE", qty, max_age_ms, min_depth_mult)
          & cv.mask("PE", qty, max_age_ms, min_depth_mult))
    if ok.sum() < 2:
        return out

    g = cv.geometry()
    i, j, width = g.pair_i, g.pair_j, g.pair_width
    pair_ok = ok[i] & ok[j]

    # Long box: pay a debit now, collect `width` at expiry -> the payoff is an
    # asset, discounted at the borrow rate.
    debit = (cv.ce_ask[i] - cv.ce_bid[j]) + (cv.pe_ask[j] - cv.pe_bid[i])
    edge_long = width * rates.df_asset(cv.T) - debit

    # Short box: take a credit now, owe `width` at expiry -> a liability,
    # discounted at the lend rate.
    credit = (cv.ce_bid[i] - cv.ce_ask[j]) + (cv.pe_bid[j] - cv.pe_ask[i])
    edge_short = credit - width * rates.df_liability(cv.T)

    for tag, edge, sides in (
        ("LONG", edge_long, ("BUY", "SELL", "SELL", "BUY")),
        ("SHORT", edge_short, ("SELL", "BUY", "BUY", "SELL")),
    ):
        net = edge - cost_points
        cand = pair_ok & (net >= min_edge_points) & np.isfinite(net)
        if not cand.any():
            continue
        for c in _top(cand, net, top_k):
            a, b = int(i[c]), int(j[c])
            s_ce_a, s_ce_b, s_pe_a, s_pe_b = sides
            spec = [
                (cv.ce_symbol[a], cv.ce_token[a], s_ce_a, "CE", a),
                (cv.ce_symbol[b], cv.ce_token[b], s_ce_b, "CE", b),
                (cv.pe_symbol[a], cv.pe_token[a], s_pe_a, "PE", a),
                (cv.pe_symbol[b], cv.pe_token[b], s_pe_b, "PE", b),
            ]
            legs, depths = [], []
            for sym, tok, side, kind, idx in spec:
                if kind == "CE":
                    px = cv.ce_ask[idx] if side == "BUY" else cv.ce_bid[idx]
                    dq = cv.ce_askq[idx] if side == "BUY" else cv.ce_bidq[idx]
                else:
                    px = cv.pe_ask[idx] if side == "BUY" else cv.pe_bid[idx]
                    dq = cv.pe_askq[idx] if side == "BUY" else cv.pe_bidq[idx]
                depths.append(float(dq))
                lage = float(cv.ce_age[idx] if kind == "CE" else cv.pe_age[idx])
                lex = cv.ce_exch if kind == "CE" else cv.pe_exch
                legs.append({"symbol": sym, "token": tok, "side": side,
                             "price": float(px), "qty": qty, "instrument": "OPT",
                             "exchange": cv.segment, "underlying": cv.underlying,
                             "strike": float(cv.strikes[idx]), "opt": kind,
                             "depth": float(dq), "age_ms": lage,
                             "quote_ts": cv.now_wall - lage / 1000.0,
                             "quote_exch_ms": int(lex[idx]) if lex is not None else 0})
            out.append(Opportunity(
                strategy=f"BOX_{tag}",
                expiry=cv.expiry,
                T=cv.T,
                description=f"{tag.title()} box {cv.strikes[a]:.0f}/{cv.strikes[b]:.0f}",
                legs=legs,
                gross_edge_points=float(edge[c]),
                est_cost_points=cost_points,
                qty=qty,
                max_age_ms=float(max(cv.ce_age[a], cv.ce_age[b], cv.pe_age[a], cv.pe_age[b])),
                min_depth_ratio=min(depths) / qty,
                detail={"width": float(width[c]),
                        "debit": float(debit[c]), "credit": float(credit[c])},
            ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# S3. Butterfly convexity  (3 legs - option prices must be convex in strike)
# ─────────────────────────────────────────────────────────────────────────────
def scan_butterfly(cv: ChainView, rates: RateBand, cost_points, qty,
                   max_age_ms, min_depth_mult, min_edge_points, top_k=8, max_wing=4):
    """C(K) must be convex in K. If an equally spaced butterfly can be bought for
    a credit, convexity is violated and the payoff - which can never be negative
    - has been handed over for free. Needs no rate assumption at all.
    """
    out = []
    n = len(cv.strikes)
    if n < 3:
        return out
    for kind, bid, ask, bidq, askq, age, syms, toks in (
        ("CE", cv.ce_bid, cv.ce_ask, cv.ce_bidq, cv.ce_askq, cv.ce_age, cv.ce_symbol, cv.ce_token),
        ("PE", cv.pe_bid, cv.pe_ask, cv.pe_bidq, cv.pe_askq, cv.pe_age, cv.pe_symbol, cv.pe_token),
    ):
        ok = cv.mask(kind, qty, max_age_ms, min_depth_mult)
        ok_body = cv.mask(kind, 2 * qty, max_age_ms, min_depth_mult)
        for lo, mid, hi in cv.geometry().flies:
            valid = ok[lo] & ok_body[mid] & ok[hi]
            if not valid.any():
                continue
            # Buy 1 lo, sell 2 mid, buy 1 hi. Negative cost == credit == arbitrage.
            cost = ask[lo] + ask[hi] - 2.0 * bid[mid]
            edge = -cost
            net = edge - cost_points
            cand = valid & (net >= min_edge_points) & np.isfinite(net)
            if not cand.any():
                continue
            for c in _top(cand, net, top_k):
                a, m, b = int(lo[c]), int(mid[c]), int(hi[c])
                exch = cv.ce_exch if kind == "CE" else cv.pe_exch
                _t = lambda i: {"age_ms": float(age[i]),
                                "quote_ts": cv.now_wall - float(age[i]) / 1000.0,
                                "quote_exch_ms": int(exch[i]) if exch is not None else 0}
                legs = [
                    {"symbol": syms[a], "token": toks[a], "side": "BUY",
                     "price": float(ask[a]), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                     "strike": float(cv.strikes[a]), "opt": kind, "depth": float(askq[a]), **_t(a)},
                    {"symbol": syms[m], "token": toks[m], "side": "SELL",
                     "price": float(bid[m]), "qty": 2 * qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                     "strike": float(cv.strikes[m]), "opt": kind, "depth": float(bidq[m]), **_t(m)},
                    {"symbol": syms[b], "token": toks[b], "side": "BUY",
                     "price": float(ask[b]), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                     "strike": float(cv.strikes[b]), "opt": kind, "depth": float(askq[b]), **_t(b)},
                ]
                out.append(Opportunity(
                    strategy=f"BUTTERFLY_{kind}",
                    expiry=cv.expiry,
                    T=cv.T,
                    description=(f"{kind} fly {cv.strikes[a]:.0f}/{cv.strikes[m]:.0f}/"
                                 f"{cv.strikes[b]:.0f} convexity violated"),
                    legs=legs,
                    gross_edge_points=float(edge[c]),
                    est_cost_points=cost_points,
                    qty=qty,
                    max_age_ms=float(max(age[a], age[m], age[b])),
                    min_depth_ratio=float(min(askq[a], bidq[m] / 2.0, askq[b]) / qty),
                    detail={"wing": float(cv.strikes[m] - cv.strikes[a])},
                ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# S4. Put-call parity against the *actual* future (3 legs, matched expiry only)
# ─────────────────────────────────────────────────────────────────────────────
def scan_pcp_futures(cv: ChainView, rates: RateBand, cost_points, qty,
                     max_age_ms, min_depth_mult, min_edge_points, top_k=8):
    """C - P = F - K*DF must hold against a future of the *same expiry*.

    Without the futures leg this is not arbitrage but a naked synthetic; without
    matched expiry it is a calendar bet. Both are refused here.
    """
    out = []
    if not cv.has_future or cv.fut_bid <= 0 or cv.fut_ask <= 0:
        return out
    if cv.fut_age > max_age_ms:
        return out
    if min(cv.fut_bidq, cv.fut_askq) < qty * min_depth_mult:
        return out

    ok = (cv.mask("CE", qty, max_age_ms, min_depth_mult)
          & cv.mask("PE", qty, max_age_ms, min_depth_mult))
    if not ok.any():
        return out

    K = cv.strikes
    dfa, dfl = rates.df_asset(cv.T), rates.df_liability(cv.T)

    def pv(amount):
        """Discount a cash flow received at expiry. The sign decides the rate:
        an inflow is discounted at the borrow rate (worth less today), an
        obligation at the lend rate (costs more today). Either way the answer
        moves against us, which is the only safe direction."""
        return amount * np.where(amount > 0.0, dfa, dfl)

    # Reversal: buy the synthetic (buy call, sell put), sell the future. The
    # combined payoff is a *single* cash flow of (F - K) at expiry, so the whole
    # amount is discounted - discounting only the strike, as the old bot did,
    # leaves (1-DF)*F of phantom edge, ~131 points at 30 days on NIFTY.
    synth_cost = cv.ce_ask - cv.pe_bid
    edge_rev = pv(cv.fut_bid - K) - synth_cost
    # Conversion: sell the synthetic, buy the future -> (K - F) at expiry.
    synth_credit = cv.ce_bid - cv.pe_ask
    edge_conv = synth_credit + pv(K - cv.fut_ask)

    for tag, edge, ce_side, pe_side, fut_side in (
        ("REVERSAL", edge_rev, "BUY", "SELL", "SELL"),
        ("CONVERSION", edge_conv, "SELL", "BUY", "BUY"),
    ):
        net = edge - cost_points
        cand = ok & (net >= min_edge_points) & np.isfinite(net)
        if not cand.any():
            continue
        for c in _top(cand, net, top_k):
            ce_px = cv.ce_ask[c] if ce_side == "BUY" else cv.ce_bid[c]
            pe_px = cv.pe_ask[c] if pe_side == "BUY" else cv.pe_bid[c]
            fut_px = cv.fut_ask if fut_side == "BUY" else cv.fut_bid
            ce_dq = cv.ce_askq[c] if ce_side == "BUY" else cv.ce_bidq[c]
            pe_dq = cv.pe_askq[c] if pe_side == "BUY" else cv.pe_bidq[c]
            fut_dq = cv.fut_askq if fut_side == "BUY" else cv.fut_bidq
            legs = [
                {"symbol": cv.ce_symbol[c], "token": cv.ce_token[c], "side": ce_side,
                 "price": float(ce_px), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                 "strike": float(K[c]), "opt": "CE", "depth": float(ce_dq),
                 "age_ms": float(cv.ce_age[c]),
                 "quote_ts": cv.now_wall - float(cv.ce_age[c]) / 1000.0,
                 "quote_exch_ms": int(cv.ce_exch[c]) if cv.ce_exch is not None else 0},
                {"symbol": cv.pe_symbol[c], "token": cv.pe_token[c], "side": pe_side,
                 "price": float(pe_px), "qty": qty, "instrument": "OPT", "exchange": cv.segment, "underlying": cv.underlying,
                 "strike": float(K[c]), "opt": "PE", "depth": float(pe_dq),
                 "age_ms": float(cv.pe_age[c]),
                 "quote_ts": cv.now_wall - float(cv.pe_age[c]) / 1000.0,
                 "quote_exch_ms": int(cv.pe_exch[c]) if cv.pe_exch is not None else 0},
                {"symbol": cv.fut_symbol, "token": cv.fut_token, "side": fut_side,
                 "price": float(fut_px), "qty": qty, "instrument": "FUT", "exchange": cv.segment, "underlying": cv.underlying,
                 "strike": 0.0, "opt": "FUT", "depth": float(fut_dq),
                 "age_ms": float(cv.fut_age),
                 "quote_ts": cv.now_wall - float(cv.fut_age) / 1000.0,
                 "quote_exch_ms": int(cv.fut_exch)},
            ]
            out.append(Opportunity(
                strategy=f"PCP_{tag}",
                expiry=cv.expiry,
                T=cv.T,
                description=f"Put-call parity {tag.lower()} at {K[c]:.0f} vs {cv.fut_symbol}",
                legs=legs,
                gross_edge_points=float(edge[c]),
                est_cost_points=cost_points,
                qty=qty,
                max_age_ms=float(max(cv.ce_age[c], cv.pe_age[c], cv.fut_age)),
                min_depth_ratio=float(min(ce_dq, pe_dq, fut_dq) / qty),
                detail={"strike": float(K[c]), "fut_bid": cv.fut_bid, "fut_ask": cv.fut_ask},
            ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Market-implied financing rate  (data-derived, replaces the repo guess)
# ─────────────────────────────────────────────────────────────────────────────
def implied_box_rate(cv: ChainView, max_age_ms=2000.0, min_pairs=6,
                     rate_floor=0.0, rate_cap=0.15):
    """The rate the option market itself is financing at, read off the boxes.

    Each strike pair implies a rate from its mid-price box. The cross-sectional
    median is the market's consensus; centring the rate band on it means we stop
    manufacturing edge out of a stale repo assumption, and a pair that deviates
    from the consensus is a genuine relative-value signal.

    Returns None when the chain is too thin to trust.
    """
    n = len(cv.strikes)
    if n < 2 or cv.T <= 0:
        return None
    ok = ((cv.ce_bid > 0) & (cv.ce_ask > 0) & (cv.pe_bid > 0) & (cv.pe_ask > 0)
          & (cv.ce_age <= max_age_ms) & (cv.pe_age <= max_age_ms))
    if ok.sum() < 2:
        return None
    ce_mid = 0.5 * (cv.ce_bid + cv.ce_ask)
    pe_mid = 0.5 * (cv.pe_bid + cv.pe_ask)
    g = cv.geometry()
    i, j = g.pair_i, g.pair_j
    sel = ok[i] & ok[j]
    if sel.sum() < min_pairs:
        return None
    width = g.pair_width[sel]
    i, j = i[sel], j[sel]
    box_mid = (ce_mid[i] - ce_mid[j]) + (pe_mid[j] - pe_mid[i])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = box_mid / width
        rate = -np.log(np.clip(ratio, 1e-6, None)) / cv.T
    # Wider boxes carry more rate information and less quantisation noise; weight
    # by width and clip to a sane band before taking the weighted median.
    # A box mid more than a few percent away from its width is a broken quote,
    # not a financing signal - at a 14-day tenor even a 15% rate moves the ratio
    # by well under 1%, so anything wider is noise being read as information.
    good = (np.isfinite(rate) & (ratio > 0.90) & (ratio < 1.02) & (width > 0)
            & (rate > rate_floor - 0.10) & (rate < rate_cap + 0.20))
    if good.sum() < min_pairs:
        return None
    r, w = rate[good], width[good]
    order = np.argsort(r)
    r, w = r[order], w[order]
    cw = np.cumsum(w)
    med = float(r[np.searchsorted(cw, cw[-1] / 2.0)])
    # Outside a plausible band the measurement is not usable. Returning None here
    # makes the engine fall back to its configured rate; accepting, say, 27% would
    # mis-discount a 2400-point SENSEX box by ~19 points - more than that box's
    # entire cost line, i.e. pure manufactured edge.
    return med if rate_floor <= med <= rate_cap else None


SCANNERS = {
    "vertical": scan_vertical,
    "box": scan_box,
    "butterfly": scan_butterfly,
    "pcp_futures": scan_pcp_futures,
}
