"""What Angel One actually blocks for a structure, from its own margin calculator.

The settlement model in ``arb/capital.py`` says a one-lot NIFTY short box ties up
Rs 13,000 - the most it can owe at expiry. Angel One blocks Rs 83,656 against the
same four legs (measured 11 Sep 2026: SPAN + exposure on the two short options,
net of the hedge benefit). "Invested" has to mean the second number, because that
is the cash the account must hold, and the return band has to be judged on it.

Two paths, because the margin endpoint is an HTTP round trip and the detection
loop cannot afford one:

* ``estimate(legs)`` - a per-chain table of per-lot margin for each *shape* of
  structure (spread, box, conversion, reversal), calibrated from the API on a
  background thread. Pure dictionary lookup on the hot path.
* ``exact(legs)`` - the API's figure for the legs actually traded, fetched right
  after the fill and written on the trade record.

Until the table is calibrated the estimate falls back to a fixed share of
notional per short option unit; the fallback is deliberately a little above
what Angel returned for a hedged box, so it can only under-size, never over.
"""
import threading
import time

from arb.capital import cash_today


def _units(legs):
    """Per-lot shape of a structure: how many option and futures units it is
    short and long, with one unit = the smallest leg quantity. A butterfly's
    doubled middle leg counts as two; that is what the exchange margins."""
    qtys = [abs(int(l["qty"])) for l in legs if l.get("qty")]
    if not qtys:
        return 0, 0, 0, 0
    base = min(qtys)
    so = lo = sf = lf = 0
    for l in legs:
        n = max(1, int(round(abs(int(l["qty"])) / base)))
        if l.get("opt") == "FUT":
            if l["side"] == "SELL":
                sf += n
            else:
                lf += n
        elif l["side"] == "SELL":
            so += n
        else:
            lo += n
    return so, lo, sf, lf


class MarginService:
    def __init__(self, api, bindings, limits, log=print, spots=None):
        self.api = api
        self.bindings = list(bindings or [])
        self.limits = limits
        self.log = log
        self.spots = spots or {}          # underlying -> spot, for the fallback
        self.product = limits.margin_product
        self.table = {}                   # (underlying, expiry, shape) -> Rs per lot
        self.table_at = {}                # same key -> wall time of calibration
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.last_calibrated = 0.0
        self._last_call = 0.0
        self._api_lock = threading.Lock()
        self._running = threading.Event()
        self._thread = None
        self._by_chain = {(b.underlying, b.expiry): b for b in self.bindings}

    # ── the API call ────────────────────────────────────────────────────────
    def _positions(self, legs, market=False):
        return [{
            "exchange": l.get("exchange", "NFO"),
            "qty": int(abs(l["qty"])),
            "price": 0 if market else round(float(l.get("fill_price") or l.get("price") or 0.0), 2),
            "productType": self.product,
            "token": str(l["token"]),
            "tradeType": l["side"],
            "orderType": "MARKET" if market else "LIMIT",
        } for l in legs]

    def _call(self, legs, market=False):
        """One margin request, serialised and spaced so the endpoint's rate limit
        is never tripped by calibration and a live trade at the same moment."""
        with self._api_lock:
            wait = self.limits.margin_call_spacing_sec - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            t0 = time.time()
            try:
                r = self.api.getMarginApi({"positions": self._positions(legs, market)})
            except Exception as e:
                r = {"status": False, "message": str(e)}
            self._last_call = time.time()
            self.calls += 1
        ms = (self._last_call - t0) * 1000.0
        if not (isinstance(r, dict) and r.get("status") and r.get("data")):
            self.failures += 1
            self.last_error = str((r or {}).get("message") if isinstance(r, dict) else r)[:200]
            return None
        d = r["data"]
        try:
            total = float(d.get("totalMarginRequired") or 0.0)
        except (TypeError, ValueError):
            self.failures += 1
            self.last_error = "unparseable totalMarginRequired"
            return None
        return {"total": total, "components": d.get("marginComponents") or {},
                "ms": round(ms, 1), "product": self.product}

    def exact(self, legs):
        """Angel's margin for exactly these legs at these quantities and prices,
        or None if the API did not answer. Blocking; never call on the scan thread."""
        legs = [l for l in legs if l.get("token")]
        return self._call(legs) if legs else None

    # ── the hot-path estimate ───────────────────────────────────────────────
    def estimate(self, legs, expiry=None):
        """(rupees, source) for these legs at their stated quantities.

        Looks up the calibrated per-lot figure for this chain and shape, scaled
        by how many lots the quantities represent. Falls back to a fixed share
        of notional per short unit when the table has nothing for this chain.
        """
        if not legs:
            return 0.0, "none"
        und = legs[0].get("underlying", "")
        shape = _units(legs)
        b = self._by_chain.get((und, expiry))
        lot = b.lot_size if b is not None else min(abs(int(l["qty"])) for l in legs)
        lots = max(1, int(round(min(abs(int(l["qty"])) for l in legs) / lot)))
        per_lot = self.table.get((und, expiry, shape))
        if per_lot is not None:
            return per_lot * lots, "angel_one:table"
        return self._fallback(legs), "fallback"

    def _fallback(self, legs):
        pct = self.limits.margin_fallback_pct_per_short_unit
        total = 0.0
        for l in legs:
            px = float(l.get("fill_price") or l.get("price") or 0.0)
            q = abs(int(l["qty"]))
            if l.get("opt") == "FUT":
                total += 0.12 * px * q
            elif l["side"] == "SELL":
                ref = l.get("strike") or self.spots.get(l.get("underlying", ""), 0.0) or px
                total += pct * float(ref) * q
        # A net debit is cash out of the account on top of whatever is blocked.
        return total + max(0.0, -cash_today(legs))

    # ── calibration ─────────────────────────────────────────────────────────
    def _canonical(self, b):
        """Representative one-lot structures on this chain, near the middle of
        the strike grid (which was built around spot). Margin on a hedged
        structure barely depends on which strikes, so one of each shape is
        enough to size any structure of that shape on the chain."""
        n = len(b.strikes)
        if n < 2:
            return []
        i = n // 2 - 1
        j = i + 1
        lot = b.lot_size
        exch = b.segment

        def leg(side, tok, opt, strike):
            return {"side": side, "token": tok, "opt": opt, "strike": strike,
                    "qty": lot, "exchange": exch, "underlying": b.underlying}
        ki, kj = float(b.strikes[i]), float(b.strikes[j])
        ce_i, ce_j = leg("SELL", b.ce_token[i], "CE", ki), leg("BUY", b.ce_token[j], "CE", kj)
        pe_i, pe_j = leg("BUY", b.pe_token[i], "PE", ki), leg("SELL", b.pe_token[j], "PE", kj)
        out = [("spread", [ce_i, ce_j]),
               ("box", [ce_i, ce_j, pe_i, pe_j])]
        if getattr(b, "fut_token", ""):
            fut_s = leg("SELL", b.fut_token, "FUT", 0.0)
            fut_l = dict(fut_s, side="BUY")
            out.append(("conversion", [dict(ce_i, side="BUY"), dict(pe_i, side="SELL"), fut_s]))
            out.append(("reversal", [ce_i, pe_i, fut_l]))
        return out

    def calibrate_once(self):
        """Returns (shapes calibrated, shapes that got no answer)."""
        done = missed = 0
        for b in self.bindings:
            if not self._running.is_set():
                break
            for name, legs in self._canonical(b):
                r = self._call(legs, market=True)
                if r is None:
                    missed += 1
                    continue
                key = (b.underlying, b.expiry, _units(legs))
                self.table[key] = r["total"]
                self.table_at[key] = time.time()
                done += 1
        self.last_calibrated = time.time()
        return done, missed

    def _run(self):
        while self._running.is_set():
            missed = 0
            try:
                n, missed = self.calibrate_once()
                self.log(f"[margin] calibrated {n} structure shapes across "
                         f"{len(self.bindings)} chains from Angel One"
                         + (f"; {missed} unanswered ({self.last_error[:80]}), retrying in 60s"
                            if missed else ""))
            except Exception as e:
                self.log(f"[margin] calibration error: {e}")
            # A shape the API did not answer for is still on the fallback; do
            # not leave it there for the whole refresh interval.
            deadline = time.time() + (60.0 if missed else self.limits.margin_refresh_sec)
            while self._running.is_set() and time.time() < deadline:
                time.sleep(1.0)

    def start(self):
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="margin", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._running.clear()

    # ── reporting ───────────────────────────────────────────────────────────
    _SHAPE_NAMES = {(1, 1, 0, 0): "spread", (2, 2, 0, 0): "box / butterfly",
                    (1, 1, 1, 0): "conversion", (1, 1, 0, 1): "reversal"}

    def snapshot(self):
        rows = []
        for (und, exp, shape), rs in sorted(self.table.items()):
            rows.append({"underlying": und, "expiry": exp,
                         "shape": self._SHAPE_NAMES.get(shape, str(shape)),
                         "per_lot": round(rs, 2),
                         "at": self.table_at.get((und, exp, shape), 0.0)})
        return {"product": self.product, "calls": self.calls, "failures": self.failures,
                "last_error": self.last_error, "last_calibrated": self.last_calibrated,
                "rows": rows}
