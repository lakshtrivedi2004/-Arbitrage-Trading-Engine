"""Synthetic chain builder for testing the scanners.

Prices come from Black-Scholes on a forward, which is arbitrage-free by
construction, so any opportunity a scanner reports on an untouched chain is a bug
in the scanner.
"""
import math
import numpy as np
from arb.strategies import ChainView


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(F, K, T, vol, df, call=True):
    if T <= 0 or vol <= 0:
        intrinsic = max(0.0, F - K) if call else max(0.0, K - F)
        return df * intrinsic
    sq = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / sq
    d2 = d1 - sq
    if call:
        return df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def make_chain(spot=24500.0, n=13, step=50.0, T=5.0 / 365.0, r=0.065, vol=0.13,
               half_spread=0.25, depth=1500.0, age_ms=5.0, lot=75,
               with_future=True, tick=0.05):
    """Arbitrage-free NIFTY-like chain. Quotes are rounded to the real tick grid,
    outward from the true price, so rounding can never create edge."""
    df = math.exp(-r * T)
    F = spot / df
    atm = round(spot / step) * step
    strikes = np.array([atm + (i - n // 2) * step for i in range(n)], dtype=float)

    def quote(mid):
        bid = math.floor((mid - half_spread) / tick) * tick
        ask = math.ceil((mid + half_spread) / tick) * tick
        return max(0.05, bid), max(0.10, ask)

    ce_b, ce_a, pe_b, pe_a = [], [], [], []
    for K in strikes:
        cb, ca = quote(bs(F, K, T, vol, df, True))
        pb, pa = quote(bs(F, K, T, vol, df, False))
        ce_b.append(cb); ce_a.append(ca); pe_b.append(pb); pe_a.append(pa)

    z = lambda v: np.full(n, v, dtype=float)
    return ChainView(
        expiry="16SEP2026", T=T, strikes=strikes, lot_size=lot,
        ce_bid=np.array(ce_b), ce_ask=np.array(ce_a),
        ce_bidq=z(depth), ce_askq=z(depth),
        pe_bid=np.array(pe_b), pe_ask=np.array(pe_a),
        pe_bidq=z(depth), pe_askq=z(depth),
        ce_age=z(age_ms), pe_age=z(age_ms),
        ce_symbol=[f"NIFTY16SEP26{int(k)}CE" for k in strikes],
        pe_symbol=[f"NIFTY16SEP26{int(k)}PE" for k in strikes],
        ce_token=[f"C{i}" for i in range(n)], pe_token=[f"P{i}" for i in range(n)],
        fut_bid=round(F - half_spread, 2), fut_ask=round(F + half_spread, 2),
        fut_bidq=depth, fut_askq=depth, fut_age=age_ms,
        fut_symbol="NIFTY16SEP26FUT", fut_token="F1", has_future=with_future,
    )
