"""The chain as mathematics, for the dashboard.

Everything the scanner enforces is a consequence of one identity: an option's
price is the discounted integral of its payoff against the risk-neutral density,

    C(K) = DF(T) * integral_K^inf (S - K) q(S) dS,
    P(K) = DF(T) * integral_0^K (K - S) q(S) dS,        DF(T) = exp(-integral_0^T r dt).

Differentiating twice in K (Breeden & Litzenberger, 1978) gives DF * q(K) as the
second difference of call prices - so a butterfly is a density estimate and must
be non-negative; the first difference is DF * (Q(K) - 1), so a vertical spread is
a probability and must lie in [0, DF*(K2-K1)]; and integrating the whole density
gives the box, DF*(K2-K1), and parity, C - P = DF*(F - K). This module evaluates
those integrals from live mid prices so the page can show the density the market
is quoting, where it goes negative, and what the band demands on each chain.
"""
import math

import numpy as np


def _mid(bid, ask):
    """Midpoints where both sides are quoted, NaN elsewhere."""
    bid = np.asarray(bid, dtype=float)
    ask = np.asarray(ask, dtype=float)
    ok = (bid > 0) & (ask > 0) & (ask >= bid)
    out = np.full(bid.shape, np.nan)
    out[ok] = 0.5 * (bid[ok] + ask[ok])
    return out


def _f(x, nd=4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def chain_math(b, store, spot, rate, limits, now_ns):
    """One expiry's option grid evaluated against the no-arbitrage integrals.

    ``b`` is a ChainBinding, ``rate`` the financing rate used to discount
    (box-implied when the engine has one), ``spot`` the live index level.
    """
    K = np.asarray(b.strikes, dtype=float)
    n = len(K)
    if n < 3:
        return None
    h = float(np.median(np.diff(K)))
    T = max(float(b.T), 1e-6)
    DF = math.exp(-rate * T)
    C = _mid(store.bid[b.ce_slots], store.ask[b.ce_slots])
    P = _mid(store.bid[b.pe_slots], store.ask[b.pe_slots])

    # Second difference: DF q(K_i) h^2 = C_{i-1} - 2 C_i + C_{i+1}. Reported as a
    # density in 1/points, so integral q dK over the grid is a probability mass.
    qc = np.full(n, np.nan)
    qp = np.full(n, np.nan)
    qc[1:-1] = (C[:-2] - 2 * C[1:-1] + C[2:]) / (DF * h * h)
    qp[1:-1] = (P[:-2] - 2 * P[1:-1] + P[2:]) / (DF * h * h)
    both = np.isfinite(qc) & np.isfinite(qp)
    q = np.where(both, 0.5 * (qc + qp), np.where(np.isfinite(qc), qc, qp))

    # First difference: the implied distribution function at the midpoints,
    # Q(K_{i+1/2}) = 1 + (C_{i+1} - C_i) / (DF h). A vertical spread is DF times a
    # probability, so the ratio below must sit in [0, 1] for every pair.
    dC = (C[1:] - C[:-1]) / (DF * h)          # in [-1, 0]
    dP = (P[1:] - P[:-1]) / (DF * h)          # in [0, 1]
    Q_mid = 1.0 + dC
    call_ratio = -dC                          # (C_i - C_{i+1}) / (DF h)
    put_ratio = dP                            # (P_{i+1} - P_i) / (DF h)

    # The box: (C_i - C_{i+1}) + (P_{i+1} - P_i) = DF h exactly, so this ratio is
    # 1 in an arbitrage-free chain; its log gives the market's own rate.
    box_ratio = call_ratio + put_ratio
    with np.errstate(divide="ignore", invalid="ignore"):
        box_rate = -np.log(box_ratio * DF) / T   # r such that B = e^{-rT} h

    # Parity near the money: F_hat = K + (C - P) / DF, one estimate per strike.
    F_hat = K + (C - P) / DF
    atm = int(np.argmin(np.abs(K - spot))) if spot else n // 2
    lo, hi = max(0, atm - 2), min(n, atm + 3)
    F_near = F_hat[lo:hi]
    F_near = F_near[np.isfinite(F_near)]
    F_par = float(np.median(F_near)) if F_near.size else None
    fut_bid = float(store.bid[b.fut_slot]) if b.fut_slot is not None else 0.0
    fut_ask = float(store.ask[b.fut_slot]) if b.fut_slot is not None else 0.0

    # What the band asks of this chain today, in rupees and in points per unit.
    lot = int(b.lot_size)
    T_days = T * 365.0
    days = max(T_days, limits.min_tenor_days)
    level = spot or float(K[atm])
    contract = level * lot
    need_lo = limits.min_excess_return_pa / 100.0 * contract * days / 365.0
    need_hi = limits.max_excess_return_pa / 100.0 * contract * days / 365.0

    fin_q = q[np.isfinite(q)]
    mass = float(np.nansum(np.where(np.isfinite(q), q, 0.0)) * h) if fin_q.size else None
    neg = [float(K[i]) for i in range(n) if np.isfinite(q[i]) and q[i] < -1e-9]

    def series(vals):
        return [_f(v, 7) for v in vals]

    return {
        "underlying": b.underlying, "expiry": b.expiry, "segment": b.segment,
        "lot": lot, "step": h, "T_days": round(T_days, 3), "rate": round(rate, 5),
        "DF": round(DF, 6), "spot": round(level, 2),
        "strikes": [float(k) for k in K],
        "call_mid": series(C), "put_mid": series(P),
        "density": series(q), "density_call": series(qc), "density_put": series(qp),
        "cdf_mid": series(Q_mid),
        "call_spread_ratio": series(call_ratio), "put_spread_ratio": series(put_ratio),
        "box_ratio": series(box_ratio), "box_rate": series(box_rate),
        "mass": _f(mass, 4),
        "negative_density_at": neg,
        "spread_ratio_range": [_f(np.nanmin(np.concatenate([call_ratio, put_ratio])), 4),
                               _f(np.nanmax(np.concatenate([call_ratio, put_ratio])), 4)]
                              if np.isfinite(call_ratio).any() or np.isfinite(put_ratio).any()
                              else [None, None],
        "box_ratio_median": _f(np.nanmedian(box_ratio), 5) if np.isfinite(box_ratio).any() else None,
        "box_rate_median": _f(np.nanmedian(box_rate), 5) if np.isfinite(box_rate).any() else None,
        "parity": {
            "F_hat": _f(F_par, 2),
            "fut_bid": round(fut_bid, 2), "fut_ask": round(fut_ask, 2),
            "basis_rate": _f(math.log(F_par / level) / T, 5) if (F_par and level > 0) else None,
            "deviation_pts": _f(F_par - 0.5 * (fut_bid + fut_ask), 2)
                             if (F_par and fut_bid > 0 and fut_ask > 0) else None,
        },
        "band": {
            "contract_value": round(contract, 2),
            "tenor_days_used": round(days, 3),
            "need_inr": [round(need_lo, 2), round(need_hi, 2)],
            "need_pts": [round(need_lo / lot, 3), round(need_hi / lot, 3)],
        },
    }


def term_structure(underlying, futures, store, spot, ref_rate, box_rates, T_of):
    """The futures curve and the forward carry between consecutive expiries.

    F(T) = S e^{rT} in a dividend-free world, so ln(F/S)/T is the carry the
    market charges to that date and ln(F2/F1)/(T2-T1) the forward carry between
    two dates. A calendar spread is rich or cheap by the gap between that
    forward carry and the configured financing rate - shown as analytics only:
    unlike a box or a parity trade its payoff is not locked in at entry.
    """
    rows = []
    for f in futures:
        s = f["slot"]
        bid, ask = float(store.bid[s]), float(store.ask[s])
        mid = 0.5 * (bid + ask) if bid > 0 and ask > 0 else None
        T = max(T_of(f["expiry"]), 1e-6)
        rows.append({
            "expiry": f["expiry"], "symbol": f["symbol"], "lot": f["lotsize"],
            "bid": round(bid, 2), "ask": round(ask, 2), "mid": _f(mid, 2),
            "T_days": round(T * 365.0, 3),
            "carry": _f(math.log(mid / spot) / T, 5) if (mid and spot > 0) else None,
            "fair": _f(spot * math.exp(ref_rate * T), 2) if spot > 0 else None,
            "box_rate": _f(box_rates.get(f["expiry"]), 5),
        })
    pairs = []
    for a, c in zip(rows, rows[1:]):
        if not (a["mid"] and c["mid"]):
            continue
        dT = (c["T_days"] - a["T_days"]) / 365.0
        if dT <= 0:
            continue
        fwd = math.log(c["mid"] / a["mid"]) / dT
        # Judged against the configured financing rate, not the near expiry's
        # box-implied rate: BANKNIFTY boxes finance at 11-13% while its futures
        # carry 6-7%, and mixing the two would call every calendar "cheap".
        ref = ref_rate
        fair_spread = a["mid"] * (math.exp(ref * dT) - 1.0)
        actual = c["mid"] - a["mid"]
        pairs.append({
            "near": a["expiry"], "far": c["expiry"], "dT_days": round(dT * 365.0, 2),
            "actual_spread": round(actual, 2), "fair_spread": round(fair_spread, 2),
            "forward_carry": round(fwd, 5), "ref_rate": round(ref, 5),
            "deviation_pa": round(fwd - ref, 5),
            "edge_pts": round(actual - fair_spread, 2),
            "edge_inr_per_lot": round((actual - fair_spread) * a["lot"], 2),
            "edge_pct_contract": round(100.0 * (actual - fair_spread) / a["mid"], 4),
            "rich": "far" if actual > fair_spread else "near",
        })
    return {"underlying": underlying, "spot": round(spot, 2), "ref_rate": round(ref_rate, 5),
            "futures": rows, "pairs": pairs}
