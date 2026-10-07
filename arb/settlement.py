"""Final settlement levels and the mark on every leg.

A leg's exit price is one of two things. Before expiry it is the current mid
quote - what the position is worth now. At and after expiry it is the intrinsic
value at the index's final settlement level, which is what an arbitrage held to
expiry actually receives. The engine records that level itself: on an expiry day
it writes the last spot it saw at the close into ``journal/settlements.json``, so
a leg's exit stays reviewable for the whole paper run without another API call.
"""
import json
import os
import threading
import time

from arb.instruments import IST, now_ist, parse_expiry


class SettlementBook:
    def __init__(self, path="journal/settlements.json"):
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        try:
            with open(path) as fh:
                self._data = json.load(fh)
        except Exception:
            self._data = {}

    @staticmethod
    def _key(underlying, expiry):
        return f"{underlying} {expiry}"

    def get(self, underlying, expiry):
        return self._data.get(self._key(underlying, expiry))

    def record(self, underlying, expiry, level, source="spot at close"):
        if not level or level <= 0:
            return False
        with self._lock:
            self._data[self._key(underlying, expiry)] = {
                "level": round(float(level), 2), "source": source,
                "at": now_ist().isoformat(timespec="seconds")}
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        return True

    def record_todays_expiries(self, pairs, spot_of):
        """On an expiry day, once the index has settled (15:30), pin the
        settlement level of every (underlying, expiry) in ``pairs`` that expires
        today to the last spot seen. Idempotent."""
        today = now_ist().date()
        done = []
        for underlying, expiry in sorted(pairs):
            try:
                exp = parse_expiry(expiry)
            except Exception:
                continue
            if exp.date() != today or now_ist() < exp:
                continue
            if self.get(underlying, expiry):
                continue
            level = spot_of(underlying)
            if level and self.record(underlying, expiry, level, "last spot at close (approx.)"):
                done.append((underlying, expiry, level))
        return done

    def level_from_candles(self, candles, underlying, expiry):
        """Fallback: the close of the last five-minute bar on the expiry date."""
        if candles is None:
            return None
        try:
            day = parse_expiry(expiry).strftime("%Y-%m-%d")
            rows = [r for r in candles.get(underlying, 100000) if str(r[0]).startswith(day)]
            return float(rows[-1][4]) if rows else None
        except Exception:
            return None


def intrinsic(leg, level):
    kind = leg.get("opt", "")
    k = float(leg.get("strike") or 0.0)
    if kind == "CE":
        return max(0.0, level - k)
    if kind == "PE":
        return max(0.0, k - level)
    if kind == "FUT":
        return level
    return None


class LegMarker:
    """Attach exit price and P&L to the legs of a journalled trade."""

    def __init__(self, store, settlements: SettlementBook, candles=None):
        self.store = store
        self.settlements = settlements
        self.candles = candles

    def _settlement_level(self, underlying, expiry):
        s = self.settlements.get(underlying, expiry)
        if s:
            return s["level"], s["source"]
        lvl = self.settlements.level_from_candles(self.candles, underlying, expiry)
        return (lvl, "index close from candles") if lvl else (None, None)

    def _mark(self, leg):
        slot = self.store.slot_of_token.get(str(leg.get("token", "")))
        if slot is None:
            return None
        bid, ask = float(self.store.bid[slot]), float(self.store.ask[slot])
        if bid > 0 and ask > 0:
            return 0.5 * (bid + ask)
        ltp = float(self.store.ltp[slot])
        return ltp if ltp > 0 else None

    def enrich(self, trade):
        """Mutates and returns ``trade`` (a history/day_trades row or a live
        dashboard row) with per-leg exit, per-leg P&L and the trade's
        mark-to-market or settled P&L."""
        expiry = trade.get("expiry", "")
        und = trade.get("underlying", "")
        try:
            expired = now_ist() >= parse_expiry(expiry)
        except Exception:
            expired = False
        level = source = None
        if expired:
            level, source = self._settlement_level(und, expiry)
        legs_pnl = 0.0
        complete = True
        for l in trade.get("legs", []):
            entry = float(l.get("price") or 0.0)
            qty = float(l.get("qty") or 0.0)
            sign = 1.0 if l.get("side") == "BUY" else -1.0
            exit_px, kind = None, "—"
            if not l.get("filled", True):
                kind = "unfilled"
            elif expired:
                if level is not None:
                    exit_px, kind = intrinsic(l, level), "settled"
                else:
                    kind = "awaiting settlement"
            else:
                exit_px = self._mark(l)
                kind = "mark" if exit_px is not None else "no quote"
            l["entry"] = round(entry, 2)
            l["exit"] = None if exit_px is None else round(exit_px, 2)
            l["exit_kind"] = kind
            if exit_px is None:
                l["pnl"] = None
                if l.get("filled", True):
                    complete = False
            else:
                l["pnl"] = round(sign * (exit_px - entry) * qty, 2)
                legs_pnl += l["pnl"]
        cost = float(trade.get("cost") or 0.0)
        trade["legs_pnl"] = round(legs_pnl, 2) if complete else None
        trade["mtm_pnl"] = round(legs_pnl - cost, 2) if complete else None
        trade["mtm_kind"] = ("settled" if expired and level is not None
                             else "awaiting settlement" if expired
                             else "mark-to-market" if complete else "partial marks")
        trade["settlement_level"] = level
        trade["settlement_source"] = source
        return trade
