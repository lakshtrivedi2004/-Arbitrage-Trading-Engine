"""Live web dashboard.

A threaded HTTP server that reads engine state from outside the scan loop. It
never writes, never locks, and never touches the hot path - the scanner does not
know it exists. State is assembled per request; the probe that computes
distance-to-profitable is throttled so a refreshing browser cannot drive it.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arb import history as hist
from arb.instruments import market_phase, now_ist, years_to_expiry
from arb.mathview import chain_math, term_structure
from arb.settlement import LegMarker, SettlementBook

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dashboard.html")
CLASSIC = os.path.join(HERE, "dashboard_classic.html")
STATIC = os.path.join(HERE, "static")

# Fonts and their content types. The terminal dashboard uses the IBM VGA/EGA
# bitmap faces; the classic page used DepartureMono. All are served locally so
# the page renders identically with no network.
_FONT_CT = {".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain"}


class DashboardState:
    """Assembles a JSON snapshot of everything the page shows."""

    def __init__(self, engine, feed, risk, execution, cfg, mode, journal,
                 candles=None, margin=None, settlements=None):
        self.engine, self.feed, self.risk = engine, feed, risk
        self.execution, self.cfg, self.mode = execution, cfg, mode
        self.journal = journal
        self.candles = candles
        self.margin = margin
        self.settlements = settlements or SettlementBook()
        # Exit price and P&L per leg: live mid before expiry, intrinsic at the
        # recorded settlement level after it.
        self.marker = LegMarker(feed.store, self.settlements, candles)
        self._hist = None
        self._hist_at = 0.0
        self.started = time.time()
        self.spots = {}
        self.trades = []
        self._probe = {}
        self._probe_at = 0.0
        self._math = None
        self._math_at = 0.0
        self._lock = threading.Lock()

    def live_spot(self, name):
        """Index level from the live feed, falling back to the session's opening
        fetch when the spot slot has not ticked yet."""
        slots = getattr(self.feed, "spot_slots", {}) or {}
        s = slots.get(name)
        if s is not None:
            try:
                ltp = float(self.feed.store.ltp[s])
                if ltp > 0:
                    return ltp
            except Exception:
                pass
        return float(self.spots.get(name, 0.0))

    def mathematics(self):
        """Every chain evaluated against the pricing integrals, plus each index's
        futures curve. Throttled: a page polling once a second must not make the
        server redo eight chains of finite differences each time."""
        now = time.time()
        if self._math is not None and now - self._math_at < 2.0:
            return self._math
        self._math_at = now
        store = self.feed.store
        now_ns = time.perf_counter_ns()
        chains, curves = [], []
        for b in self.engine.bindings:
            spot = self.live_spot(b.underlying)
            rate = self.engine.implied_rates.get(f"{b.underlying} {b.expiry}",
                                                 self.cfg.risk_free_rate)
            try:
                cm = chain_math(b, store, spot, rate, self.risk.limits, now_ns)
            except Exception as e:
                cm = {"underlying": b.underlying, "expiry": b.expiry, "error": str(e)}
            if cm:
                chains.append(cm)
        futs = getattr(self.feed, "future_slots", {}) or {}
        for name, rows in futs.items():
            spot = self.live_spot(name)
            box_rates = {k.split(" ", 1)[1]: v for k, v in self.engine.implied_rates.items()
                         if k.startswith(name + " ")}
            try:
                curves.append(term_structure(name, rows, store, spot,
                                             self.cfg.risk_free_rate, box_rates,
                                             years_to_expiry))
            except Exception as e:
                curves.append({"underlying": name, "error": str(e), "futures": [], "pairs": []})
        self._math = {"now": now_ist().strftime("%H:%M:%S"), "chains": chains,
                      "curves": curves,
                      "band": [self.risk.limits.min_excess_return_pa,
                               self.risk.limits.max_excess_return_pa],
                      "min_tenor_days": self.risk.limits.min_tenor_days,
                      "stake": self.risk.limits.stake_per_trade,
                      "costs": self.risk.cm.__dict__}
        return self._math

    def record_trade(self, fill):
        o = fill["opportunity"]
        with self._lock:
            self.trades.insert(0, {
                "id": fill["trade_id"],
                "time": fill.get("executed_at", fill["ts_ist"][11:19]),
                "leg_spread_ms": fill.get("leg_spread_ms", 0.0),
                "strategy": o["strategy"],
                "underlying": o.get("underlying", ""),
                "description": o["description"],
                "expiry": o["expiry"],
                "lots": fill["lots"],
                "gross": round(fill["gross_inr"], 2),
                "cost": round(fill["cost_inr"], 2),
                "slip": round(fill.get("realised_slippage_inr", 0.0), 2),
                "net": round(fill["net_inr"], 2),
                "capital": round(fill.get("capital_inr", 0.0), 2),
                "capital_source": fill.get("capital_source", ""),
                "margin_inr": round(fill.get("margin_inr", 0.0) or 0.0, 2),
                "settlement_inr": round(fill.get("settlement_inr", 0.0) or 0.0, 2),
                "return_pct": fill.get("return_pct", 0.0),
                "filled": fill["all_filled"],
                "detect_ms": round(fill["latency_ms"]["detect_to_order"], 2),
                "legs": [{"side": l["side"], "symbol": l["symbol"],
                          "token": str(l.get("token", "")), "strike": l.get("strike", 0.0),
                          "qty": l["qty"], "price": round(l["fill_price"], 2),
                          "filled": bool(l.get("filled")),
                          "opt": l.get("opt", ""),
                          "quoted_at": l.get("quoted_at", ""),
                          "ms_before": round(l.get("ms_before_fire", 0.0))}
                         for l in fill["legs"]],
            })
            del self.trades[40:]

    def history(self, days=30):
        """Rolled-up paper-run history. Re-read from disk on a slow cadence -
        the files only change when a trade lands, and a browser refreshing once a
        second must not re-parse a month of journals each time."""
        now = time.time()
        if self._hist is None or now - self._hist_at > 3.0:
            self._hist_at = now
            try:
                trades = hist.load_trades(days=days)
                summary = hist.summarise(trades, self.risk.limits.total_capital,
                                         self.risk.limits.stake_per_trade)
                today = now_ist().strftime("%Y-%m-%d")
                dates = [d["date"] for d in summary["days"]]
                summary["intraday"] = hist.intraday_curve(trades, today)
                summary["dates"] = dates
                summary["by_day"] = {d: [self.marker.enrich(t) for t in hist.day_trades(trades, d)]
                                     for d in dates[-7:]}
                self._hist = summary
            except Exception as e:
                self._hist = {"error": str(e), "days": [], "equity_curve": [],
                              "totals": {}, "intraday": [], "dates": [], "by_day": {}}
        return self._hist

    def probe(self):
        now = time.time()
        if now - self._probe_at > 1.5:
            self._probe_at = now
            try:
                self._probe = self.engine.probe()
            except Exception:
                pass
        return self._probe

    def chain(self, limit=7):
        """Strikes nearest the money for each expiry, with quote ages."""
        store = self.feed.store
        now_ns = time.perf_counter_ns()
        out = []
        for b in self.engine.bindings:
            n = len(b.strikes)
            mid = n // 2
            spot = self.spots.get(b.underlying, 0.0)
            if spot:
                mid = min(range(n), key=lambda i: abs(b.strikes[i] - spot))
            lo = max(0, mid - limit // 2)
            rows = []
            for i in range(lo, min(n, lo + limit)):
                ce, pe = int(b.ce_slots[i]), int(b.pe_slots[i])
                rows.append({
                    "strike": float(b.strikes[i]),
                    "ce_bid": round(float(store.bid[ce]), 2),
                    "ce_ask": round(float(store.ask[ce]), 2),
                    "ce_age": round((now_ns - int(store.recv_ns[ce])) / 1e6),
                    "pe_bid": round(float(store.bid[pe]), 2),
                    "pe_ask": round(float(store.ask[pe]), 2),
                    "pe_age": round((now_ns - int(store.recv_ns[pe])) / 1e6),
                    "atm": i == mid,
                })
            fut = None
            if b.fut_slot is not None:
                fs = b.fut_slot
                fut = {"symbol": b.fut_symbol,
                       "bid": round(float(store.bid[fs]), 2),
                       "ask": round(float(store.ask[fs]), 2)}
            out.append({"underlying": b.underlying, "segment": b.segment,
                        "expiry": b.expiry, "lot": b.lot_size,
                        "rows": rows, "future": fut,
                        "implied_rate": self.engine.implied_rates.get(
                            f"{b.underlying} {b.expiry}")})
        return out

    def snapshot(self):
        e, f, r = self.engine, self.feed, self.risk
        st = e.stats.summary()
        h = e.quote_health
        legs = max(1, h["legs_seen"])
        up = time.time() - self.started
        return {
            "now": now_ist().strftime("%H:%M:%S"),
            "phase": market_phase(start=self.cfg.session_start, end=self.cfg.session_end),
            "mode": self.mode,
            "connected": bool(f.connected.is_set()) if hasattr(f, "connected") else True,
            "uptime_s": int(up),
            "spots": {k: round(self.live_spot(k), 2) for k in self.spots},
            "indices": sorted({b.underlying for b in self.engine.bindings}),
            "chains": len(self.engine.bindings),
            "ticks": f.ticks,
            "dropped": f.dropped,
            "reconnects": getattr(f, "reconnects", 0),
            "stalls": getattr(f, "stalls", 0),
            "quiet_s": round((time.perf_counter_ns() - f.last_tick_ns) / 1e9, 1)
                       if f.last_tick_ns else None,
            "tick_rate": round(f.ticks / up, 1) if up > 1 else 0.0,
            "passes": st["passes"],
            "chains_scanned": e.chains_scanned,
            "chains_skipped": e.chains_skipped,
            "scan_p50_us": st["scan_us_p50"],
            "scan_p99_us": st["scan_us_p99"],
            "quote_health": {k: round(100.0 * h[k] / legs, 1)
                             for k in ("tradable", "stale", "thin", "one_sided")},
            "legs_seen": h["legs_seen"],
            "implied_rates": {k: round(v, 4) for k, v in e.implied_rates.items()},
            "signals": e.signals_by_strategy,
            "rejects": r.state.rejects,
            "trades_today": r.state.trades_today,
            "session_pnl": round(r.state.session_pnl, 2),
            "total_capital": r.limits.total_capital,
            "equity": round(r.equity, 2),
            "capital_deployed": round(r.state.capital_deployed, 2),
            "capital_free": round(r.capital_free, 2),
            "day_pct": round(100.0 * r.state.session_pnl / r.limits.total_capital, 3)
                       if r.limits.total_capital else 0.0,
            "stake": r.limits.stake_per_trade,
            "stake_pct": round(100.0 * r.state.session_pnl / r.limits.stake_per_trade, 3)
                         if r.limits.stake_per_trade else 0.0,
            "return_band": [r.limits.min_excess_return_pa, r.limits.max_excess_return_pa],
            "halted": r.state.halted,
            "halt_reason": r.state.halt_reason,
            "probe": self.probe(),
            "chain": self.chain(),
            "recent_trades": [self.marker.enrich(dict(t, legs=[dict(l) for l in t["legs"]]))
                              for t in self.trades[:12]],
            "strategies": list(self.cfg.strategies),
            "margin": self.margin.snapshot() if self.margin is not None else None,
        }


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                                   # never spam the trading console

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path.startswith("/api/state"):
                    self._send(json.dumps(state.snapshot()).encode(),
                               "application/json")
                elif self.path.startswith("/api/history"):
                    self._send(json.dumps(state.history()).encode(),
                               "application/json")
                elif self.path.startswith("/api/math"):
                    self._send(json.dumps(state.mathematics()).encode(),
                               "application/json")
                elif self.path.startswith("/api/candles"):
                    rows = state.candles.all(180) if state.candles else {}
                    self._send(json.dumps(rows).encode(), "application/json")
                elif self.path.startswith("/static/"):
                    # Fonts are served locally rather than from a CDN so the
                    # dashboard renders identically with no network.
                    name = os.path.basename(self.path)
                    fp = os.path.join(STATIC, name)
                    if os.path.isfile(fp) and os.path.dirname(fp) == STATIC:
                        ct = _FONT_CT.get(os.path.splitext(name)[1],
                                          "application/octet-stream")
                        with open(fp, "rb") as fh:
                            body = fh.read()
                        self.send_response(200)
                        self.send_header("Content-Type", ct)
                        self.send_header("Cache-Control", "public, max-age=604800")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.send_response(404); self.end_headers()
                elif self.path.startswith("/classic"):
                    # The previous Wikipedia-idiom page, kept reachable: the deep
                    # analytics (mathematics essays, month history, density/CDF
                    # charts) live here while the terminal grid is the default face.
                    page = CLASSIC if os.path.isfile(CLASSIC) else PAGE
                    with open(page, "rb") as fh:
                        self._send(fh.read(), "text/html; charset=utf-8")
                elif self.path in ("/", "/index.html"):
                    with open(PAGE, "rb") as fh:
                        self._send(fh.read(), "text/html; charset=utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                try:
                    self._send(json.dumps({"error": str(exc)}).encode(),
                               "application/json")
                except Exception:
                    pass
    return Handler


class Dashboard:
    def __init__(self, state, port=8088, host="127.0.0.1"):
        self.state = state
        self.port, self.host = port, host
        self.httpd = None
        self._thread = None

    def start(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port),
                                         make_handler(self.state))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="dashboard", daemon=True)
        self._thread.start()
        return self

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
