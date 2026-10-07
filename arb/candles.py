"""Index OHLC candles from Angel's historical API, cached off the hot path.

Fetched on a background thread and served from memory. The trading engine never
waits on this - a chart that fails to load must not delay an order.
"""
import json
import os
import threading
import time
from datetime import timedelta

from arb.instruments import IST, now_ist

INTERVALS = {"1m": "ONE_MINUTE", "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE",
             "1h": "ONE_HOUR", "1d": "ONE_DAY"}
SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


class CandleStore:
    def __init__(self, api, underlyings, interval="5m", lookback_days=5,
                 refresh_sec=300.0, cache_dir=None, log=lambda *_: None):
        self.api = api
        self.underlyings = {u.name: u for u in underlyings}
        self.interval = interval
        self.lookback_days = lookback_days
        self.refresh_sec = refresh_sec
        self.log = log
        self.data = {}                 # name -> [[iso, o, h, l, c, v], ...]
        self.fetched_at = {}
        # Angel's historical endpoint is rate limited hard and five-minute bars do
        # not change quickly, so results are cached to disk. A restart then paints
        # the chart immediately from cache instead of spending a request.
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".cache", "candles")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._load_cache()
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # Live bar, built from the spot index tick. The historical endpoint only
        # returns completed bars, so without this the chart is always one full
        # interval behind the market and the last candle never moves.
        self._store = None
        self._spot_slots = {}
        self.live = {}                 # name -> [iso, o, h, l, c, v] for the open bucket
        self._live_thread = None

    def attach_feed(self, store, spot_slots):
        """Give the store access to the live spot ticks, keyed by index name."""
        self._store = store
        self._spot_slots = dict(spot_slots)
        return self

    def _bucket_iso(self, ts):
        secs = SECONDS.get(self.interval, 300)
        start = int(ts // secs) * secs
        from datetime import datetime
        return datetime.fromtimestamp(start, IST).strftime("%Y-%m-%dT%H:%M:%S+05:30")

    def _sample_live(self):
        st = self._store
        if st is None:
            return
        now = time.time()
        iso = self._bucket_iso(now)
        for name, slot in self._spot_slots.items():
            px = float(st.ltp[slot])
            if px <= 0:
                continue
            bar = self.live.get(name)
            if bar is None or bar[0] != iso:
                # A new interval opened: the previous live bar is now complete and
                # can stand in for history until the next historical refresh.
                if bar is not None:
                    with self._lock:
                        rows = self.data.get(name) or []
                        if not rows or rows[-1][0] < bar[0]:
                            rows.append(bar)
                            self.data[name] = rows
                self.live[name] = [iso, px, px, px, px, 0.0]
            else:
                if px > bar[2]:
                    bar[2] = px
                if px < bar[3]:
                    bar[3] = px
                bar[4] = px

    def _run_live(self):
        while not self._stop.is_set():
            try:
                self._sample_live()
            except Exception:
                pass
            self._stop.wait(0.25)

    def _cache_path(self, name):
        return os.path.join(self.cache_dir, f"{name}_{self.interval}.json")

    def _load_cache(self):
        for name in self.underlyings:
            try:
                path = self._cache_path(name)
                if os.path.exists(path):
                    with open(path) as fh:
                        self.data[name] = json.load(fh)
                    self.fetched_at[name] = os.path.getmtime(path)
            except Exception:
                continue

    def _save_cache(self, name, rows):
        try:
            tmp = self._cache_path(name) + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(rows, fh)
            os.replace(tmp, self._cache_path(name))
        except Exception:
            pass

    def _fetch(self, u):
        end = now_ist()
        start = end - timedelta(days=self.lookback_days)
        params = {
            "exchange": u.spot_exchange,
            "symboltoken": u.spot_token,
            "interval": INTERVALS.get(self.interval, "FIVE_MINUTE"),
            "fromdate": start.strftime("%Y-%m-%d 09:15"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        r = self.api.getCandleData(params)
        rows = (r or {}).get("data") or []
        return [[str(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                 float(x[5] or 0)] for x in rows if x and len(x) >= 5]

    def refresh_once(self, force=False):
        for name, u in self.underlyings.items():
            fresh = time.time() - self.fetched_at.get(name, 0)
            if not force and fresh < self.refresh_sec * 0.9:
                continue                      # cache still current; spend nothing
            try:
                rows = self._fetch(u)
                if rows:
                    with self._lock:
                        self.data[name] = rows
                        self.fetched_at[name] = time.time()
                    self._save_cache(name, rows)
            except Exception as e:
                self.log(f"[candles] {name}: {e}")
            # The historical endpoint is rate limited; space the calls generously.
            time.sleep(2.5)

    def _run(self):
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(self.refresh_sec)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="candles", daemon=True)
        self._thread.start()
        if self._store is not None:
            self._live_thread = threading.Thread(target=self._run_live,
                                                 name="candles-live", daemon=True)
            self._live_thread.start()
        return self

    def stop(self):
        self._stop.set()

    def get(self, name, limit=120):
        with self._lock:
            rows = list(self.data.get(name) or [])
        live = self.live.get(name)
        if live:
            # Replace the bucket the live bar belongs to, or append it. History
            # wins on ties only if it is newer, which it never is for the open bar.
            if rows and rows[-1][0] == live[0]:
                rows[-1] = list(live)
            elif not rows or rows[-1][0] < live[0]:
                rows.append(list(live))
        return rows[-limit:]

    def all(self, limit=120):
        with self._lock:
            names = list(self.data)
        return {n: self.get(n, limit) for n in names}
