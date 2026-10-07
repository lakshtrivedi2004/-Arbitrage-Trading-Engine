"""Scrip master handling and universe selection."""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
# Index derivatives stop trading at 15:15 and settle out of the closing auction.
EXPIRY_CUTOFF = (15, 30)


def now_ist():
    return datetime.now(IST)


def parse_expiry(s):
    """'16SEP2026' -> aware datetime at the expiry-day settlement time."""
    d = datetime.strptime(s.upper(), "%d%b%Y")
    return d.replace(hour=EXPIRY_CUTOFF[0], minute=EXPIRY_CUTOFF[1], tzinfo=IST)


def years_to_expiry(expiry_str, ref=None):
    ref = ref or now_ist()
    secs = (parse_expiry(expiry_str) - ref).total_seconds()
    return max(secs, 60.0) / (365.0 * 24 * 3600)


def load_scrip_master(max_age_hours=12, force=False):
    """Download the scrip master, caching it for the day. Contract definitions
    change only overnight, so re-downloading 8 MB mid-session is pure latency."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "scrip_master.json")
    if not force and os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 3600.0
        if age < max_age_hours:
            with open(path) as fh:
                return json.load(fh), f"cache ({age:.1f}h old)"
    req = urllib.request.Request(SCRIP_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return data, "downloaded"


def _strike(scrip):
    return float(scrip.get("strike", 0)) / 100.0


def build_universe(scrip_master, underlying="NIFTY", spot=None,
                   num_expiries=2, strikes_each_side=12, segment="NFO",
                   min_hours_to_expiry=1.0):
    """Pick the option and futures contracts we will subscribe to.

    Returns (expiries, options_by_expiry, futures_by_expiry). Futures are matched
    to option expiries by date so put-call parity only ever runs against a future
    that settles on the same day.
    """
    opts, futs = [], {}
    for s in scrip_master:
        if s.get("name") != underlying:
            continue
        seg, itype = s.get("exch_seg"), s.get("instrumenttype")
        if seg != segment:
            continue
        if itype == "OPTIDX":
            sym = s.get("symbol", "")
            if not (sym.endswith("CE") or sym.endswith("PE")):
                continue
            opts.append(s)
        elif itype == "FUTIDX":
            futs[s.get("expiry", "")] = s

    live = []
    ref = now_ist() + timedelta(hours=min_hours_to_expiry)
    for s in opts:
        try:
            if parse_expiry(s["expiry"]) > ref:
                live.append(s)
        except Exception:
            continue
    if not live:
        return [], {}, {}

    expiries = sorted({s["expiry"] for s in live}, key=parse_expiry)[:num_expiries]

    if spot is None:
        strikes = sorted({_strike(s) for s in live if s["expiry"] == expiries[0]})
        spot = strikes[len(strikes) // 2] if strikes else 0.0

    by_expiry, fut_by_expiry = {}, {}
    for exp in expiries:
        chain = [s for s in live if s["expiry"] == exp]
        grid = sorted({_strike(s) for s in chain if _strike(s) > 0})
        if not grid:
            continue
        # Strike step is whatever this expiry actually uses (50 for weeklies,
        # sometimes 100 further out) - read it off the grid instead of assuming.
        atm_i = min(range(len(grid)), key=lambda i: abs(grid[i] - spot))
        lo = max(0, atm_i - strikes_each_side)
        hi = min(len(grid), atm_i + strikes_each_side + 1)
        keep = set(grid[lo:hi])

        pairs = {}
        for s in chain:
            k = _strike(s)
            if k not in keep:
                continue
            side = "CE" if s["symbol"].endswith("CE") else "PE"
            pairs.setdefault(k, {})[side] = {
                "token": s["token"], "symbol": s["symbol"], "strike": k,
                "expiry": exp, "lotsize": int(float(s.get("lotsize", 75))),
                "type": side, "tick": float(s.get("tick_size", 5)) / 100.0,
                "segment": segment,
            }
        # Only strikes with both a call and a put are usable: every strategy here
        # needs at least one of each, or a matched pair across strikes.
        by_expiry[exp] = {k: v for k, v in sorted(pairs.items()) if "CE" in v and "PE" in v}
        f = futs.get(exp)
        if f:
            fut_by_expiry[exp] = {
                "token": f["token"], "symbol": f["symbol"], "expiry": exp,
                "lotsize": int(float(f.get("lotsize", 75))),
                "tick": float(f.get("tick_size", 5)) / 100.0,
                "segment": segment,
            }
    return expiries, by_expiry, fut_by_expiry


def live_futures(scrip_master, underlying="NIFTY", segment="NFO",
                 min_hours_to_expiry=1.0, n=3):
    """The nearest ``n`` index futures on this underlying, nearest first.

    Independent of the option chains: the term structure of futures is its own
    object (calendar carry between expiries), and NIFTY's weekly option expiries
    only ever match one of them.
    """
    ref = now_ist() + timedelta(hours=min_hours_to_expiry)
    out = []
    for s in scrip_master:
        if s.get("name") != underlying or s.get("exch_seg") != segment \
                or s.get("instrumenttype") != "FUTIDX":
            continue
        try:
            if parse_expiry(s["expiry"]) <= ref:
                continue
        except Exception:
            continue
        out.append({"token": s["token"], "symbol": s["symbol"], "expiry": s["expiry"],
                    "lotsize": int(float(s.get("lotsize", 75))), "segment": segment})
    out.sort(key=lambda f: parse_expiry(f["expiry"]))
    return out[:n]


HOLIDAYS_FILE = os.path.join("config", "holidays.txt")


def load_holidays(path=HOLIDAYS_FILE):
    """Exchange holidays as a set of dates, one YYYY-MM-DD per line in
    config/holidays.txt (blank lines and # comments ignored). Missing file =
    no holidays known; an unlisted holiday only costs a session that sees no
    ticks and stops itself."""
    out = set()
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                try:
                    out.add(datetime.strptime(line, "%Y-%m-%d").date())
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out


def is_trading_day(day, holidays=None):
    holidays = load_holidays() if holidays is None else holidays
    return day.weekday() < 5 and day not in holidays


def session_bounds(day, start="09:15", end="15:15"):
    """(open, close) aware IST datetimes of the trading session on ``day``."""
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    o = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    c = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    return o, c


def next_session(ref=None, start="09:15", end="15:15", holidays=None):
    """(open, close) of the session in progress or the next one to come.

    If ``ref`` falls on a trading day before that day's close, the answer is
    that day's session - even if its open is already past, which is how a
    caller tells "we are inside a session" (open <= ref < close) from "waiting
    for one" (ref < open)."""
    ref = ref or now_ist()
    holidays = load_holidays() if holidays is None else holidays
    day = ref.date()
    for _ in range(31):
        if is_trading_day(day, holidays):
            o, c = session_bounds(day, start, end)
            if ref < c:
                return o, c
        day += timedelta(days=1)
    raise RuntimeError("no trading day in the next month - check config/holidays.txt")


def market_phase(ref=None, start="09:15", end="15:15"):
    """'preopen' | 'open' | 'closed' | 'weekend' for the given IST time."""
    ref = ref or now_ist()
    if ref.weekday() >= 5:
        return "weekend"
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    t = ref.hour * 60 + ref.minute
    if t < 9 * 60:
        return "closed"
    if t < sh * 60 + sm:
        return "preopen"
    return "open" if t < eh * 60 + em else "closed"
