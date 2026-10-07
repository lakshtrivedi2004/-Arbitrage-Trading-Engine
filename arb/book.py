"""In-memory quote store.

Every instrument is assigned a dense integer slot at startup. Ticks write into
preallocated numpy arrays by slot, so the websocket callback does no allocation,
no dict resizing and no string work - it is a handful of array stores.

Prices arrive from Angel in paise (x100); everything here is in rupees.
"""
import time
import numpy as np

PAISE = 0.01


class QuoteStore:
    def __init__(self, capacity):
        self.capacity = capacity
        z = lambda: np.zeros(capacity, dtype=np.float64)
        self.bid = z()
        self.ask = z()
        self.bid_qty = z()
        self.ask_qty = z()
        self.ltp = z()
        self.oi = z()
        self.volume = z()
        # Monotonic receive time per slot, in nanoseconds. Staleness is judged
        # against this, never against exchange timestamps (clock skew).
        self.recv_ns = np.zeros(capacity, dtype=np.int64)
        self.exch_ms = np.zeros(capacity, dtype=np.int64)
        self.tick_count = np.zeros(capacity, dtype=np.int64)
        self.token_of_slot = [""] * capacity
        self.slot_of_token = {}
        self._next = 0

    def assign(self, token):
        slot = self.slot_of_token.get(token)
        if slot is None:
            if self._next >= self.capacity:
                raise RuntimeError("QuoteStore capacity exceeded")
            slot = self._next
            self._next += 1
            self.slot_of_token[token] = slot
            self.token_of_slot[slot] = token
        return slot

    def update_from_tick(self, slot, bid, ask, bid_qty, ask_qty, ltp,
                         oi=0.0, volume=0.0, exch_ms=0, now_ns=None):
        self.bid[slot] = bid
        self.ask[slot] = ask
        self.bid_qty[slot] = bid_qty
        self.ask_qty[slot] = ask_qty
        self.ltp[slot] = ltp
        self.oi[slot] = oi
        self.volume[slot] = volume
        self.exch_ms[slot] = exch_ms
        self.recv_ns[slot] = now_ns if now_ns is not None else time.perf_counter_ns()
        self.tick_count[slot] += 1

    def age_ms(self, now_ns=None):
        now = now_ns if now_ns is not None else time.perf_counter_ns()
        return (now - self.recv_ns) / 1e6

    def snapshot(self, slot):
        return {
            "token": self.token_of_slot[slot],
            "bid": float(self.bid[slot]), "ask": float(self.ask[slot]),
            "bid_qty": float(self.bid_qty[slot]), "ask_qty": float(self.ask_qty[slot]),
            "ltp": float(self.ltp[slot]), "oi": float(self.oi[slot]),
            "ticks": int(self.tick_count[slot]),
        }


def normalise_best5(buy_list, sell_list):
    """Return (bid, bid_qty, ask, ask_qty) in rupees from Angel's best-5 packets.

    SmartWebSocketV2 swaps the two arrays when it assigns them, and the swap has
    flipped between releases, so the side labels are not trustworthy. Both arrays
    are re-derived here from the only invariant that always holds: the bid side is
    the one whose best price is the lower of the two.
    """
    def clean(rows):
        out = []
        for r in rows or ():
            p = r.get("price", 0)
            q = r.get("quantity", 0)
            if p and p > 0 and q and q > 0:
                out.append((p * PAISE, float(q)))
        return out

    a, b = clean(buy_list), clean(sell_list)
    if not a and not b:
        return 0.0, 0.0, 0.0, 0.0
    if not a or not b:
        rows = a or b
        # Only one side present: cannot form a two-sided quote, report nothing
        # rather than guessing - a one-sided book is not tradable both ways.
        best = max(rows, key=lambda x: x[0])
        return (best[0], best[1], 0.0, 0.0) if rows is a else (0.0, 0.0, best[0], best[1])

    # Candidate A: `a` is the buy side -> bid = max(a), ask = min(b)
    bid_a, bidq_a = max(a, key=lambda x: x[0])
    ask_a, askq_a = min(b, key=lambda x: x[0])
    # Candidate B: the other way round
    bid_b, bidq_b = max(b, key=lambda x: x[0])
    ask_b, askq_b = min(a, key=lambda x: x[0])

    a_ok = bid_a < ask_a
    b_ok = bid_b < ask_b
    if a_ok and not b_ok:
        return bid_a, bidq_a, ask_a, askq_a
    if b_ok and not a_ok:
        return bid_b, bidq_b, ask_b, askq_b
    if a_ok and b_ok:
        # Both orientations are internally consistent (crossed/locked book);
        # prefer the tighter spread.
        return (bid_a, bidq_a, ask_a, askq_a) if (ask_a - bid_a) <= (ask_b - bid_b) \
            else (bid_b, bidq_b, ask_b, askq_b)
    return 0.0, 0.0, 0.0, 0.0
