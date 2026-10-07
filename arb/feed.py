"""Angel One SmartAPI market feed.

The websocket callback is the hottest code in the process. It does exactly three
things: normalise the best-5 packet, store it into preallocated arrays by integer
slot, and signal the scanner. No allocation, no logging, no I/O, no locks.
"""
import logging
import re
import threading
import time

import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from arb.book import QuoteStore, normalise_best5, PAISE

NSE_CM, NSE_FO, BSE_CM, BSE_FO = 1, 2, 3, 4


class _RedactSecrets(logging.Filter):
    """SmartApi logs the whole request on any HTTP error - PIN, live TOTP, API
    key and bearer token included - so a pasted log line is a credential leak.
    Scrub those fields before the record reaches any handler."""
    PATTERNS = [
        (re.compile(r"('password':\s*')[^']*(')"), r"\1***\2"),
        (re.compile(r"('totp':\s*')[^']*(')"), r"\1***\2"),
        (re.compile(r"('X-PrivateKey':\s*')[^']*(')"), r"\1***\2"),
        (re.compile(r"('Authorization':\s*'Bearer )[^']*(')"), r"\1***\2"),
        (re.compile(r"('jwtToken':\s*')[^']*(')"), r"\1***\2"),
        (re.compile(r"('refreshToken':\s*')[^']*(')"), r"\1***\2"),
        (re.compile(r"('feedToken':\s*')[^']*(')"), r"\1***\2"),
    ]

    def filter(self, record):
        try:
            msg = record.getMessage()
            for pat, rep in self.PATTERNS:
                msg = pat.sub(rep, msg)
            record.msg, record.args = msg, ()
        except Exception:
            pass
        return True


def _install_redaction():
    try:
        from logzero import logger as lz
        if not any(isinstance(f, _RedactSecrets) for f in lz.filters):
            lz.addFilter(_RedactSecrets())
        for h in lz.handlers:
            if not any(isinstance(f, _RedactSecrets) for f in h.filters):
                h.addFilter(_RedactSecrets())
    except Exception:
        pass


# Network faults that are worth waiting out at login: DNS down, no route,
# connection reset, timeouts. Anything else (a wrong PIN, a bad TOTP seed,
# Angel rejecting the session) is raised immediately - retrying it would only
# lock the account.
_NETWORK_ERRORS = ("NameResolutionError", "MaxRetryError", "ConnectionError",
                   "ConnectionResetError", "ConnectTimeout", "ReadTimeout",
                   "NewConnectionError", "Name or service not known",
                   "Connection aborted", "Temporary failure in name resolution")


class AngelSession:
    def __init__(self, creds):
        self.creds = creds
        self.api = None
        self.jwt = None
        self.refresh = None
        self.feed_token = None
        _install_redaction()

    def login(self, retry_for_sec=0.0, log=None):
        """Log in; with ``retry_for_sec`` > 0, keep trying through network
        outages for up to that long (backing off 15s -> 60s) before giving up."""
        deadline = time.time() + retry_for_sec
        wait = 15.0
        while True:
            try:
                return self._login_once()
            except Exception as e:
                text = f"{type(e).__name__}: {e}"
                network = any(k in text for k in _NETWORK_ERRORS)
                if not network or time.time() + wait > deadline:
                    raise
                if log:
                    log(f"login: network unavailable ({type(e).__name__}); "
                        f"retrying in {wait:.0f}s")
                time.sleep(wait)
                wait = min(60.0, wait * 2)

    def _login_once(self):
        # Re-login reuses the same SmartConnect so every holder of ``session.api``
        # (margin service, candle store, live broker) sees the renewed tokens.
        if self.api is None:
            self.api = SmartConnect(api_key=self.creds.api_key)
        totp = pyotp.TOTP(self.creds.totp_secret).now()
        data = self.api.generateSession(self.creds.client_id, self.creds.pin, totp)
        if not (data and data.get("status")):
            msg = (data or {}).get("message", data)
            # SmartApi returns None (and logs the request) on transport failures;
            # surface that as a network error so the retry loop can catch it.
            if data is None:
                raise ConnectionError("Angel login: no response (ConnectionError)")
            raise RuntimeError(f"Angel login failed: {msg}")
        self.jwt = data["data"]["jwtToken"]
        self.refresh = data["data"]["refreshToken"]
        self.feed_token = self.api.getfeedToken()
        return self

    def spot_ltp(self, token="99926000", symbol="NIFTY", exchange="NSE"):
        r = self.api.ltpData(exchange, symbol, token)
        if r and r.get("data"):
            return float(r["data"].get("ltp") or 0.0)
        return 0.0


class MarketFeed:
    """Owns the websocket and the quote store."""

    def __init__(self, session: AngelSession, capacity, on_tick=None,
                 correlation_id="arbfeed01", log=print, stale_sec=15.0,
                 market_open=None):
        self.session = session
        self.store = QuoteStore(capacity)
        self.on_tick = on_tick
        self.correlation_id = correlation_id
        self.log = log
        self.sws = None
        self.subscriptions = {NSE_CM: [], NSE_FO: [], BSE_CM: [], BSE_FO: []}
        self.tick_event = threading.Event()
        self.last_tick_ns = 0
        self.ticks = 0
        self.dropped = 0
        self.connected = threading.Event()
        self._thread = None
        # Reconnection is owned here, not by the library. Its retries fire only
        # from the error callback, give up after a fixed count, and never notice
        # a connection that simply goes quiet - which is what a network blip
        # leaves behind: on 11 Sep 2026 the socket went silent at 10:55 with no
        # error or close event and the engine scanned frozen quotes for the rest
        # of the morning. The watchdog treats "no tick for stale_sec while the
        # market is open" as dead and forces a fresh connection.
        self.stale_sec = stale_sec
        self.market_open = market_open or (lambda: True)
        self.reconnects = 0
        self.stalls = 0
        self.last_reconnect_at = 0.0
        self._stopping = threading.Event()
        self._watchdog = None
        self._consecutive_failures = 0
        self._slot_cache = self.store.slot_of_token
        # Which chain (expiry) each slot belongs to, and a monotonic per-chain
        # tick counter. A tick touches exactly one expiry, so the scanner can skip
        # the chains that have not changed instead of rebuilding all of them.
        self.slot_group = [-1] * capacity
        self.group_seq = []

    def register(self, token, exchange_type=NSE_FO, group=-1):
        slot = self.store.assign(str(token))
        self.subscriptions[exchange_type].append(str(token))
        if group >= 0:
            while len(self.group_seq) <= group:
                self.group_seq.append(0)
            self.slot_group[slot] = group
        return slot

    # ── hot path ────────────────────────────────────────────────────────────
    def _on_data(self, wsapp, message):
        try:
            slot = self._slot_cache.get(message["token"])
            if slot is None:
                return
            bid, bq, ask, aq = normalise_best5(
                message.get("best_5_buy_data"), message.get("best_5_sell_data"))
            now = time.perf_counter_ns()
            self.store.update_from_tick(
                slot, bid, ask, bq, aq,
                message.get("last_traded_price", 0) * PAISE,
                message.get("open_interest", 0),
                message.get("volume_trade_for_the_day", 0),
                message.get("exchange_timestamp", 0),
                now_ns=now,
            )
            g = self.slot_group[slot]
            if g >= 0:
                # Monotonic, never reset. A lost increment under the GIL only
                # delays that chain by one tick, and the next tick corrects it.
                self.group_seq[g] += 1
            self.ticks += 1
            self.last_tick_ns = now
            self.tick_event.set()
        except Exception:
            self.dropped += 1

    def _on_open(self, wsapp):
        token_list = [{"exchangeType": et, "tokens": toks}
                      for et, toks in self.subscriptions.items() if toks]
        if token_list:
            self.sws.subscribe(self.correlation_id, SmartWebSocketV2.SNAP_QUOTE, token_list)
        self.connected.set()

    def _on_error(self, wsapp, error):
        if self._stopping.is_set():
            return                      # our own close, not an error
        self.log(f"[feed] websocket error: {error}")

    def _on_close(self, wsapp, *a):
        self.connected.clear()
        self.log("[feed] websocket closed")

    def _new_socket(self):
        # max_retry_attempt=0: the library must not reconnect from inside its own
        # error callback (a nested run_forever); _run below does it instead.
        sws = SmartWebSocketV2(
            self.session.jwt, self.session.creds.api_key,
            self.session.creds.client_id, self.session.feed_token,
            max_retry_attempt=0)
        sws.on_data = self._on_data
        sws.on_open = self._on_open
        sws.on_error = self._on_error
        sws.on_close = self._on_close
        return sws

    def _run(self):
        """Connect, block until the socket dies, reconnect. Forever."""
        while not self._stopping.is_set():
            self.sws = self._new_socket()
            opened_at = time.time()
            try:
                self.sws.connect()             # returns only when the socket closes
            except Exception as e:
                self.log(f"[feed] connect failed: {e}")
            self.connected.clear()
            if self._stopping.is_set():
                break
            # A socket that lived less than a few seconds did not really connect.
            if time.time() - opened_at < 5.0:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 0
            self.reconnects += 1
            self.last_reconnect_at = time.time()
            delay = min(2.0 * self._consecutive_failures + 1.0, 20.0)
            if self._consecutive_failures and self._consecutive_failures % 3 == 0:
                # Three straight refusals usually mean the tokens are dead, not
                # the network. A fresh login renews jwt and feed token together.
                try:
                    self.session.login()
                    self.log("[feed] re-login ok; reconnecting with fresh tokens")
                except Exception as e:
                    self.log(f"[feed] re-login failed: {e}")
            self.log(f"[feed] reconnecting in {delay:.0f}s (reconnect #{self.reconnects})")
            self._stopping.wait(delay)

    def _watch(self):
        while not self._stopping.wait(1.0):
            if not self.connected.is_set() or not self.last_tick_ns:
                continue
            quiet = (time.perf_counter_ns() - self.last_tick_ns) / 1e9
            if quiet > self.stale_sec and self.market_open():
                self.stalls += 1
                self.log(f"[feed] no ticks for {quiet:.0f}s while market open; "
                         f"forcing reconnect (stall #{self.stalls})")
                self.last_tick_ns = 0            # one alarm per stall
                try:
                    self.sws.close_connection()  # run_forever returns; _run reconnects
                except Exception as e:
                    self.log(f"[feed] close on stall failed: {e}")

    def start(self, block=False):
        self._stopping.clear()
        if block:
            self._run()
            return self
        self._thread = threading.Thread(target=self._run, name="ws", daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, name="ws-watchdog", daemon=True)
        self._watchdog.start()
        return self

    def stop(self):
        self._stopping.set()
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception:
            pass

    def wait_for_quotes(self, slots, timeout=30.0, need=1):
        """Block until at least ``need`` of the given slots have two-sided quotes."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready = sum(1 for s in slots
                        if self.store.bid[s] > 0 and self.store.ask[s] > 0)
            if ready >= need:
                return ready
            time.sleep(0.1)
        return sum(1 for s in slots if self.store.bid[s] > 0 and self.store.ask[s] > 0)
