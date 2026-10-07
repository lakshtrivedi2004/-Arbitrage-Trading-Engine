"""Tick recorder.

Writes a compact binary snapshot of the whole book on a fixed cadence, on its own
thread, so nothing here can slow detection. This is the raw material the research
module turns into calibrated thresholds - without it, every parameter in the
system is a guess, which is what the old bot's 4% ROI target was.

Layout: one .npy-style row per snapshot per instrument, plus a JSON sidecar with
the instrument map so a session can be replayed exactly.
"""
import json
import os
import threading
import time

import numpy as np

from arb.instruments import now_ist

COLUMNS = ["slot", "bid", "ask", "bid_qty", "ask_qty", "ltp", "oi", "age_ms"]


class TickRecorder:
    def __init__(self, store, bindings, directory="data", hz=4.0):
        self.store = store
        self.bindings = bindings
        self.dir = directory
        self.period = 1.0 / hz
        os.makedirs(self.dir, exist_ok=True)
        stamp = now_ist().strftime("%Y%m%d_%H%M%S")
        self.bin_path = os.path.join(self.dir, f"book_{stamp}.f4")
        self.meta_path = os.path.join(self.dir, f"book_{stamp}.meta.json")
        self._fh = None
        self._stop = threading.Event()
        self._thread = None
        self.snapshots = 0
        self._write_meta()

    def _write_meta(self):
        meta = {
            "started_ist": now_ist().isoformat(),
            "columns": COLUMNS,
            "dtype": "float32",
            "n_slots": self.store._next,
            "expiries": [],
        }
        for b in self.bindings:
            meta["expiries"].append({
                "expiry": b.expiry, "lot_size": b.lot_size,
                "strikes": [float(k) for k in b.strikes],
                "ce_slots": [int(x) for x in b.ce_slots],
                "pe_slots": [int(x) for x in b.pe_slots],
                "ce_symbol": list(b.ce_symbol), "pe_symbol": list(b.pe_symbol),
                "fut_slot": int(b.fut_slot) if b.fut_slot is not None else None,
                "fut_symbol": b.fut_symbol,
            })
        with open(self.meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)

    def _snapshot(self):
        n = self.store._next
        now_ns = time.perf_counter_ns()
        row = np.empty((n, len(COLUMNS)), dtype=np.float32)
        row[:, 0] = np.arange(n)
        row[:, 1] = self.store.bid[:n]
        row[:, 2] = self.store.ask[:n]
        row[:, 3] = self.store.bid_qty[:n]
        row[:, 4] = self.store.ask_qty[:n]
        row[:, 5] = self.store.ltp[:n]
        row[:, 6] = self.store.oi[:n]
        row[:, 7] = (now_ns - self.store.recv_ns[:n]) / 1e6
        return row

    def _run(self):
        with open(self.bin_path, "ab", buffering=1 << 20) as fh:
            self._fh = fh
            while not self._stop.is_set():
                fh.write(self._snapshot().tobytes())
                self.snapshots += 1
                time.sleep(self.period)
            fh.flush()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)


def load_session(meta_path):
    """Replay a recorded session: returns (meta, array[snapshots, slots, columns])."""
    with open(meta_path) as fh:
        meta = json.load(fh)
    bin_path = meta_path.replace(".meta.json", ".f4")
    n_slots, n_cols = meta["n_slots"], len(meta["columns"])
    raw = np.fromfile(bin_path, dtype=np.float32)
    frames = raw.size // (n_slots * n_cols)
    return meta, raw[:frames * n_slots * n_cols].reshape(frames, n_slots, n_cols)
