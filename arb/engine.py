"""The scanning engine.

One thread owns the scan loop. It sleeps on the feed's tick event, so it wakes
within tens of microseconds of a quote change and never burns the GIL that the
websocket thread needs to deliver the next tick.

Per pass it gathers each expiry's grid into contiguous arrays and runs every
scanner over the whole grid at once. Detection is O(strikes^2) inside numpy, not
in Python, so a 25-strike chain is scanned end to end in well under a millisecond.
"""
import statistics
import threading
import time
from collections import deque

import numpy as np

from arb.costs import per_unit_cost_points
from arb.instruments import years_to_expiry
from arb.strategies import (SCANNERS, ChainGeometry, ChainView, RateBand,
                            implied_box_rate)


class ChainBinding:
    """Precomputed slot indices for one expiry - built once, reused every pass."""

    def __init__(self, expiry, strikes, ce_slots, pe_slots, ce_sym, pe_sym,
                 ce_tok, pe_tok, lot_size, fut_slot=None, fut_sym="", fut_tok="",
                 underlying="NIFTY", segment="NFO"):
        self.underlying = underlying
        self.segment = segment
        self.expiry = expiry
        self.strikes = np.asarray(strikes, dtype=float)
        self.ce_slots = np.asarray(ce_slots, dtype=np.intp)
        self.pe_slots = np.asarray(pe_slots, dtype=np.intp)
        self.ce_symbol, self.pe_symbol = ce_sym, pe_sym
        self.ce_token, self.pe_token = ce_tok, pe_tok
        self.lot_size = lot_size
        self.fut_slot = fut_slot
        self.fut_symbol, self.fut_token = fut_sym, fut_tok
        self.T = years_to_expiry(expiry)
        self._T_refreshed = time.time()
        # The strike ladder is fixed for the session, so its index grids are
        # built once here and shared by every scan pass.
        self.geom = ChainGeometry(self.strikes)

    def refresh_T(self, every=30.0):
        now = time.time()
        if now - self._T_refreshed >= every:
            self.T = years_to_expiry(self.expiry)
            self._T_refreshed = now

    def view(self, store, now_ns, now_wall=None):
        """Gather a consistent snapshot of this expiry into a ChainView.

        Prices are read before receive-times in the writer, so a torn read can
        only ever make a quote look *older* than it is - which errs toward
        rejecting the trade.
        """
        ce, pe = self.ce_slots, self.pe_slots
        age = (now_ns - store.recv_ns) / 1e6
        if now_wall is None:
            now_wall = time.time()
        cv = ChainView(
            expiry=self.expiry, T=self.T, strikes=self.strikes, lot_size=self.lot_size,
            ce_bid=store.bid[ce], ce_ask=store.ask[ce],
            ce_bidq=store.bid_qty[ce], ce_askq=store.ask_qty[ce],
            pe_bid=store.bid[pe], pe_ask=store.ask[pe],
            pe_bidq=store.bid_qty[pe], pe_askq=store.ask_qty[pe],
            ce_age=age[ce], pe_age=age[pe],
            ce_exch=store.exch_ms[ce], pe_exch=store.exch_ms[pe],
            now_wall=now_wall,
            ce_symbol=self.ce_symbol, pe_symbol=self.pe_symbol,
            ce_token=self.ce_token, pe_token=self.pe_token, geom=self.geom,
            underlying=self.underlying, segment=self.segment,
        )
        if self.fut_slot is not None:
            s = self.fut_slot
            cv.fut_bid = float(store.bid[s]); cv.fut_ask = float(store.ask[s])
            cv.fut_bidq = float(store.bid_qty[s]); cv.fut_askq = float(store.ask_qty[s])
            cv.fut_age = float(age[s])
            cv.fut_exch = int(store.exch_ms[s])
            cv.fut_symbol, cv.fut_token = self.fut_symbol, self.fut_token
            cv.has_future = cv.fut_bid > 0 and cv.fut_ask > 0
        return cv.freeze()


class LatencyStats:
    def __init__(self, maxlen=20000):
        self.scan_us = deque(maxlen=maxlen)
        self.tick_to_detect_us = deque(maxlen=maxlen)
        self.passes = 0
        self.signals = 0

    def add_scan(self, us):
        self.scan_us.append(us)
        self.passes += 1

    def add_detect(self, us):
        self.tick_to_detect_us.append(us)
        self.signals += 1

    @staticmethod
    def _pct(d, p):
        if not d:
            return 0.0
        s = sorted(d)
        return s[min(len(s) - 1, int(len(s) * p))]

    def summary(self):
        return {
            "passes": self.passes,
            "signals": self.signals,
            "scan_us_p50": round(self._pct(self.scan_us, 0.50), 1),
            "scan_us_p99": round(self._pct(self.scan_us, 0.99), 1),
            "scan_us_max": round(max(self.scan_us), 1) if self.scan_us else 0.0,
            "tick_to_detect_us_p50": round(self._pct(self.tick_to_detect_us, 0.50), 1),
            "tick_to_detect_us_p99": round(self._pct(self.tick_to_detect_us, 0.99), 1),
        }


class ArbEngine:
    def __init__(self, feed, bindings, cfg, cost_model, limits, risk,
                 execution, journal, notifier=None, log=print):
        self.feed = feed
        self.bindings = bindings
        self.cfg = cfg
        self.cm = cost_model
        self.limits = limits
        self.risk = risk
        self.execution = execution
        self.journal = journal
        self.notifier = notifier
        self.log = log
        self.stats = LatencyStats()
        self.default_rates = RateBand(lend=cfg.risk_free_rate - 0.010,
                                      borrow=cfg.risk_free_rate + 0.020)
        # One band per expiry. Live NIFTY boxes imply 5.2% at the front weekly and
        # 10.4% three weeks out; collapsing that term structure to a single median
        # misprices every expiry but one. On a 1200-point box at 5 days a 3% rate
        # error is 0.5 points - larger than the median violation we are hunting,
        # so a flat rate would manufacture signals rather than find them.
        self.rates_by_expiry = {}
        self.implied_rates = {}
        self._running = threading.Event()
        self._thread = None
        self.best_seen = None
        self.signals_by_strategy = {}
        # Without these, "signals: none" is ambiguous - it cannot distinguish an
        # efficient market from a gate that silently blocked every quote. The
        # tradability masks live inside numpy, so nothing they drop would ever
        # reach the reject journal.
        self.quote_health = {"legs_seen": 0, "one_sided": 0, "stale": 0,
                             "thin": 0, "tradable": 0}
        self._seen_seq = [-1] * len(bindings)
        self.chains_scanned = 0
        self.chains_skipped = 0
        self._last_rate_update = 0.0
        self._recent_structures = {}
        self._recent_instruments = {}
        self._scanners = [(n, SCANNERS[n]) for n in cfg.strategies if n in SCANNERS]
        # Cost per unit is dominated by fixed brokerage amortised over the lot, so
        # precompute one bound per (strategy, legs) shape instead of per candidate.
        self._cost_cache = {}

    def _cost_points(self, n_opt, n_fut, qty, price_scale):
        key = (n_opt, n_fut, qty, round(price_scale, -1))
        c = self._cost_cache.get(key)
        if c is None:
            c = per_unit_cost_points(price_scale, n_opt, n_fut, qty, self.cm)
            self._cost_cache[key] = c
        return c

    def _refresh_rates(self, views):
        """Re-centre the financing band on the market's own implied box rate."""
        if not self.cfg.use_implied_rate:
            return
        now = time.time()
        if now - self._last_rate_update < 5.0:
            return
        self._last_rate_update = now
        for cv in views:
            # Calibrate only from quotes fresh enough that we would trade on
            # them. A closed or thin market then yields no reading at all and the
            # engine keeps its configured rate, rather than fitting a discount
            # curve to prices it would have rejected as stale.
            r = implied_box_rate(cv, max_age_ms=self.limits.max_quote_age_ms,
                                 rate_floor=self.cfg.implied_rate_floor,
                                 rate_cap=self.cfg.implied_rate_cap)
            if r is not None:
                self.implied_rates[f"{cv.underlying} {cv.expiry}"] = r
                # Keep a spread around this expiry's own consensus for the
                # borrow/lend asymmetry. Keyed by index too: NIFTY and BANKNIFTY
                # can share an expiry date but not a financing rate.
                self.rates_by_expiry[(cv.underlying, cv.expiry)] = \
                    RateBand(lend=r - 0.010, borrow=r + 0.020)

    def scan_once(self, min_edge_points):
        """One full pass over every expiry. Returns candidate opportunities."""
        t0 = time.perf_counter_ns()
        t0_wall = time.time()
        seqs = getattr(self.feed, "group_seq", None)
        views = []
        for gi, b in enumerate(self.bindings):
            # Skip a chain whose quotes have not moved since we last looked at it.
            # Rebuilding and rescanning an unchanged grid cannot produce a new
            # signal, and on a three-expiry universe most ticks touch only one.
            if seqs is not None and gi < len(seqs):
                if seqs[gi] == self._seen_seq[gi]:
                    self.chains_skipped += 1
                    continue
                self._seen_seq[gi] = seqs[gi]
            self.chains_scanned += 1
            b.refresh_T()
            views.append(b.view(self.feed.store, t0, t0_wall))
        if not views:
            self.stats.add_scan((time.perf_counter_ns() - t0) / 1000.0)
            return [], t0
        self._refresh_rates(views)

        out = []
        for cv in views:
            # Whole-chain early-out: at the open, at the close, and on any expiry
            # that has stopped quoting, this skips all four scanners outright.
            qty = cv.lot_size
            self._record_quote_health(cv, qty)
            if not cv.any_tradable(qty, self.limits.max_quote_age_ms,
                                   self.limits.min_depth_multiple):
                continue
            price_scale = float(np.nanmax(cv.ce_ask)) if cv.ce_ask.size else 200.0
            if not np.isfinite(price_scale) or price_scale <= 0:
                price_scale = 200.0
            rates = self.rates_by_expiry.get((cv.underlying, cv.expiry), self.default_rates)
            for name, fn in self._scanners:
                n_opt = {"vertical": 2, "box": 4, "butterfly": 4, "pcp_futures": 2}[name]
                n_fut = 1 if name == "pcp_futures" else 0
                cp = self._cost_points(n_opt, n_fut, qty, price_scale)
                try:
                    out.extend(fn(cv, rates, cp, qty,
                                  self.limits.max_quote_age_ms,
                                  self.limits.min_depth_multiple, min_edge_points))
                except Exception as e:
                    self.log(f"[engine] scanner {name} failed: {e}")
        self.stats.add_scan((time.perf_counter_ns() - t0) / 1000.0)
        return out, t0

    def run(self, lots=1, min_edge_points=0.05, stop_predicate=None):
        self._running.set()
        store = self.feed.store
        while self._running.is_set():
            if stop_predicate and stop_predicate():
                break
            # Wake on the next quote change; the timeout only bounds idle periods.
            self.feed.tick_event.wait(timeout=0.25)
            self.feed.tick_event.clear()
            tick_ns = self.feed.last_tick_ns

            opps, t0 = self.scan_once(min_edge_points)
            if not opps:
                continue

            detect_ns = time.perf_counter_ns()
            if tick_ns:
                self.stats.add_detect((detect_ns - tick_ns) / 1000.0)

            opps.sort(key=lambda o: o.gross_edge_points - o.est_cost_points, reverse=True)
            for o in opps:
                self.signals_by_strategy[o.strategy] = \
                    self.signals_by_strategy.get(o.strategy, 0) + 1
            if self.best_seen is None or \
                    opps[0].gross_edge_points > self.best_seen.gross_edge_points:
                self.best_seen = opps[0]

            now = time.time()
            for opp in opps[:5]:
                key = (opp.strategy, opp.expiry,
                       tuple(sorted(l["symbol"] for l in opp.legs)))
                last = self._recent_structures.get(key)
                if last is not None and now - last < self.limits.structure_cooldown_sec:
                    self._note_reject(opp, "structure_cooldown")
                    continue
                busy = [l["symbol"] for l in opp.legs
                        if now - self._recent_instruments.get(l["symbol"], -1e9)
                        < self.limits.instrument_cooldown_sec]
                if busy:
                    self._note_reject(opp, "instrument_cooldown")
                    continue
                ok, reason = self.risk.check(opp)
                if not ok:
                    self.journal.record_reject(self._opp_dict(opp), reason)
                    continue
                n_lots = self.size_lots(opp, lots)
                if n_lots < 1:
                    # One lot of this structure needs more margin than the stake.
                    self._note_reject(opp, "exceeds_stake")
                    continue
                per_lot = opp.detail.get("sizing", {}).get("per_lot_capital") or 0.0
                self.log(f"[engine] TAKE {opp.strategy} {opp.description} "
                         f"edge={opp.gross_edge_points:+.2f}pt "
                         f"net=₹{opp.detail['exact_net_inr'] * n_lots:,.0f} "
                         f"x{n_lots} lot(s), "
                         f"₹{per_lot * n_lots:,.0f} margin "
                         f"({opp.detail.get('capital_source', '?')})")
                self._recent_structures[key] = now
                for l in opp.legs:
                    self._recent_instruments[l["symbol"]] = now
                if len(self._recent_structures) > 4096:
                    cutoff = now - self.limits.structure_cooldown_sec
                    self._recent_structures = {k: v for k, v
                                               in self._recent_structures.items()
                                               if v >= cutoff}
                try:
                    self.execution.execute(opp, n_lots, tick_ns, detect_ns)
                except Exception as e:
                    # One bad trade record must never take the scanner down
                    # with it; the cooldowns above already stop a re-fire.
                    self.log(f"[engine] execute failed: {e!r}")
                    self._note_reject(opp, "execute_error")
                break
        self._running.clear()

    def probe(self):
        """Best *raw* violation currently on screen per strategy, ignoring cost.

        The scan loop only reports structures that clear their cost line, so when
        it says nothing it cannot tell you whether the market was two rupees away
        or nowhere near. This runs off the hot path, at whatever rate the caller
        asks, purely so the dashboard can show the distance to profitable.
        """
        now_ns = time.perf_counter_ns()
        now_wall = time.time()
        out = {}
        for b in self.bindings:
            cv = b.view(self.feed.store, now_ns, now_wall)
            qty = cv.lot_size
            rates = self.rates_by_expiry.get((cv.underlying, cv.expiry), self.default_rates)
            price_scale = float(np.nanmax(cv.ce_ask)) if cv.ce_ask.size else 200.0
            if not np.isfinite(price_scale) or price_scale <= 0:
                price_scale = 200.0
            for name, fn in self._scanners:
                n_opt = {"vertical": 2, "box": 4, "butterfly": 4, "pcp_futures": 2}[name]
                n_fut = 1 if name == "pcp_futures" else 0
                cost = self._cost_points(n_opt, n_fut, qty, price_scale)
                try:
                    # Zero cost, no edge floor, generous staleness: show the raw
                    # dislocation even when it is unreachable, so "nothing" is
                    # distinguishable from "not quite".
                    found = fn(cv, rates, 0.0, qty, 1e9, 0.0, -1e9, top_k=1)
                except Exception:
                    continue
                for o in found:
                    key = f"{b.underlying}·{o.strategy}"
                    prev = out.get(key)
                    if prev is None or o.gross_edge_points > prev["edge"]:
                        out[key] = {
                            "underlying": b.underlying,
                            "strategy": o.strategy,
                            "edge": round(o.gross_edge_points, 3),
                            "cost": round(cost, 3),
                            "expiry": o.expiry,
                            "description": o.description,
                            "max_age_ms": round(o.max_age_ms, 0),
                            "tradable": bool(o.gross_edge_points > cost),
                            # The probe deliberately ignores staleness and depth so
                            # it can show raw dislocations. This flag says whether
                            # the live path would actually have accepted the quotes.
                            "fresh": bool(o.max_age_ms <= self.limits.max_quote_age_ms),
                        }
        return out

    def _record_quote_health(self, cv, qty):
        """Attribute every untradable leg to a cause, so a silent session can be
        explained: too few two-sided quotes, quotes arriving too slowly, or a
        book too thin for our size."""
        h = self.quote_health
        need = qty * self.limits.min_depth_multiple
        age_cap = self.limits.max_quote_age_ms
        for bid, ask, bq, aq, age in ((cv.ce_bid, cv.ce_ask, cv.ce_bidq, cv.ce_askq,
                                       cv.ce_age),
                                      (cv.pe_bid, cv.pe_ask, cv.pe_bidq, cv.pe_askq,
                                       cv.pe_age)):
            h["legs_seen"] += bid.size
            two = (bid > 0) & (ask > 0) & (ask >= bid)
            h["one_sided"] += int((~two).sum())
            fresh = two & (age <= age_cap)
            h["stale"] += int((two & ~fresh).sum())
            deep = fresh & (bq >= need) & (aq >= need)
            h["thin"] += int((fresh & ~deep).sum())
            h["tradable"] += int(deep.sum())

    def quote_health_summary(self):
        h = self.quote_health
        n = max(1, h["legs_seen"])
        return {k: f"{100.0 * v / n:.1f}%" for k, v in h.items() if k != "legs_seen"} \
            | {"legs_seen": h["legs_seen"]}

    def size_lots(self, opp, requested):
        """Lots for this structure: enough to deploy the target capital, no more
        than the book shows, no more than the per-trade cap.

        The depth cap is the honest part. Displayed size is the only evidence a
        fill exists; asking for eight lots against a three-lot touch would be the
        old bot's mistake in a new form.
        """
        L = self.limits
        # Per-lot capital as the broker sees it: what Angel One blocks, or the
        # settlement obligation if that is larger. Sizing off the theoretical
        # number alone put 7 lots against Rs 13k each when the account would
        # have needed Rs 5.9 lakh of margin for them.
        per_lot = self.risk.capital_for(opp.legs, opp.expiry)[0]
        by_depth = int(opp.min_depth_ratio)          # whole lots visible at the touch
        if per_lot <= 0 or per_lot == float("inf"):
            # Free carry (nothing tied up) or unsizable: trade the requested
            # size, still bounded by what the book shows.
            lots = max(1, min(requested, by_depth, L.max_lots_per_trade))
            opp.detail["sizing"] = {"per_lot_capital": 0.0 if per_lot <= 0 else None,
                                    "stake": L.stake_per_trade, "stake_lots": requested,
                                    "depth_lots": by_depth, "lots": lots}
            return lots
        # The stake is a ceiling, never a target to be exceeded: the most whole
        # lots whose margin fits in Rs 1 lakh. Zero lots means the structure
        # is too big for the stake and is not traded.
        fit = int(L.stake_per_trade // per_lot)
        lots = min(fit, by_depth, L.max_lots_per_trade)
        room = (L.total_capital * L.max_capital_utilisation
                - self.risk.state.capital_deployed)
        if room > 0:
            lots = min(lots, int(room // per_lot))
        lots = max(0, lots)
        opp.detail["sizing"] = {"per_lot_capital": round(per_lot, 2),
                                "stake": L.stake_per_trade, "stake_lots": fit,
                                "depth_lots": by_depth, "lots": lots,
                                "stake_used": round(per_lot * lots, 2)}
        return lots

    def _note_reject(self, opp, reason):
        self.journal.record_reject(self._opp_dict(opp), reason)
        self.risk.state.rejects[reason] = self.risk.state.rejects.get(reason, 0) + 1

    @staticmethod
    def _opp_dict(o):
        return {"strategy": o.strategy, "expiry": o.expiry, "description": o.description,
                "gross_edge_points": o.gross_edge_points,
                "est_cost_points": o.est_cost_points, "qty": o.qty,
                "max_age_ms": o.max_age_ms, "min_depth_ratio": o.min_depth_ratio,
                "legs": [{k: l[k] for k in ("symbol", "side", "price", "qty")}
                         for l in o.legs],
                "detail": o.detail}

    def start(self, **kw):
        self._thread = threading.Thread(target=self.run, kwargs=kw,
                                        name="scanner", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._running.clear()
        self.feed.tick_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
