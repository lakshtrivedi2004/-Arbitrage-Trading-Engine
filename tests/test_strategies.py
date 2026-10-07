import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from arb.strategies import (RateBand, scan_vertical, scan_box, scan_butterfly,
                            scan_pcp_futures, implied_box_rate)
from tests.synth import make_chain

RATES = RateBand(lend=0.055, borrow=0.085)
# top_k=None so the suite sees every violation. The live engine caps results at
# the best few, which is a separate concern tested at the end.
ARGS = dict(cost_points=0.60, qty=75, max_age_ms=400.0,
            min_depth_mult=1.0, min_edge_points=0.10, top_k=None)
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' | ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(name)


def scan_all(cv, **over):
    a = dict(ARGS); a.update(over)
    return (scan_vertical(cv, RATES, **a) + scan_box(cv, RATES, **a)
            + scan_butterfly(cv, RATES, **a) + scan_pcp_futures(cv, RATES, **a))


print("\n[1] Clean arbitrage-free chain must yield nothing")
for vol in (0.09, 0.13, 0.22, 0.40):
    for T in (1/365, 5/365, 30/365, 90/365):
        cv = make_chain(T=T, vol=vol)
        opps = scan_all(cv)
        if opps:
            best = max(opps, key=lambda o: o.gross_edge_points)
            check(f"vol={vol} T={T*365:.0f}d clean", False,
                  f"{len(opps)} false positives, worst {best.strategy} "
                  f"{best.gross_edge_points:+.3f}pt")
        else:
            check(f"vol={vol} T={T*365:.0f}d clean", True)

print("\n[2] Zero-cost, zero-threshold: still nothing on a clean chain")
cv = make_chain()
check("no free lunch at cost=0", not scan_all(cv, cost_points=0.0, min_edge_points=1e-9))

print("\n[3] Planted vertical violation (call spread sold for a credit)")
cv = make_chain()
# A higher-strike call bid above a lower-strike call ask: buying the spread
# (buy K3, sell K8) pays a credit for a payoff that can never be negative.
cv.ce_bid[8] = cv.ce_ask[3] + 3.0
cv.ce_ask[8] = cv.ce_bid[8] + 0.50
opps = [o for o in scan_all(cv) if o.strategy.startswith("VERTICAL")]
check("detected", len(opps) >= 1)
if opps:
    o = max(opps, key=lambda x: x.gross_edge_points)
    buy = [l for l in o.legs if l["side"] == "BUY"][0]
    sell = [l for l in o.legs if l["side"] == "SELL"][0]
    expected = sell["price"] - buy["price"]
    check("edge == sell_bid - buy_ask", abs(o.gross_edge_points - expected) < 1e-9,
          f"{o.gross_edge_points:.4f} vs {expected:.4f}")
    check("buys the lower-priced leg", buy["price"] < sell["price"])
    check("2 legs only", len(o.legs) == 2)

print("\n[4] Planted butterfly convexity violation")
cv = make_chain()
cv.ce_bid[6] = cv.ce_bid[6] + 6.0   # body far too rich -> sell 2 bodies for a credit
cv.ce_ask[6] = cv.ce_bid[6] + 0.5
flies = [o for o in scan_all(cv) if o.strategy.startswith("BUTTERFLY")]
check("detected", len(flies) >= 1)
if flies:
    o = max(flies, key=lambda x: x.gross_edge_points)
    lo, mid, hi = o.legs
    check("body is sold at 2x qty", mid["side"] == "SELL" and mid["qty"] == 2 * lo["qty"])
    check("wings bought", lo["side"] == "BUY" and hi["side"] == "BUY")
    exp = 2 * mid["price"] - lo["price"] - hi["price"]
    check("edge == 2*body_bid - wings_ask", abs(o.gross_edge_points - exp) < 1e-9,
          f"{o.gross_edge_points:.4f} vs {exp:.4f}")

print("\n[5] Planted box violation")
cv = make_chain()
i, j = 2, 8
width = cv.strikes[j] - cv.strikes[i]
# Make the long box purchasable 8 points under its discounted payoff.
target_debit = width * RATES.df_asset(cv.T) - 8.0
cur = (cv.ce_ask[i] - cv.ce_bid[j]) + (cv.pe_ask[j] - cv.pe_bid[i])
delta = cur - target_debit
cv.ce_ask[i] -= delta          # keep the quote uncrossed while cheapening it
cv.ce_bid[i] -= delta
boxes = [o for o in scan_all(cv) if o.strategy.startswith("BOX")]
check("detected", len(boxes) >= 1)
planted = [o for o in boxes
           if {l["strike"] for l in o.legs} == {cv.strikes[i], cv.strikes[j]}]
check("planted pair reported", len(planted) == 1)
if planted:
    o = planted[0]
    check("edge ~= 8 points", abs(o.gross_edge_points - 8.0) < 0.02,
          f"{o.gross_edge_points:.4f}")
    check("4 legs", len(o.legs) == 4)
    sides = {(l["opt"], l["side"]) for l in o.legs}
    check("long box shape", o.strategy == "BOX_LONG" and
          sides == {("CE", "BUY"), ("CE", "SELL"), ("PE", "BUY"), ("PE", "SELL")})

print("\n[6] Planted put-call-parity violation vs the real future")
cv = make_chain()
k = 5
cv.ce_ask[k] -= 10.0   # synthetic long far too cheap -> reversal
cv.ce_bid[k] = min(cv.ce_bid[k], cv.ce_ask[k] - 0.05)
pcp = [o for o in scan_all(cv) if o.strategy.startswith("PCP")]
check("detected", len(pcp) >= 1)
if pcp:
    o = max(pcp, key=lambda x: x.gross_edge_points)
    check("has a futures leg", any(l["instrument"] == "FUT" for l in o.legs))
    check("3 legs", len(o.legs) == 3)
    check("reversal sells the future",
          [l for l in o.legs if l["instrument"] == "FUT"][0]["side"] == "SELL")

print("\n[7] PCP refuses to fire without a matched-expiry future")
cv = make_chain(with_future=False)
cv.ce_ask[5] -= 10.0
check("no futures -> no PCP", not [o for o in scan_all(cv) if o.strategy.startswith("PCP")])

print("\n[8] Staleness gate")
def planted():
    c = make_chain()
    c.ce_bid[8] = c.ce_ask[3] + 3.0
    c.ce_ask[8] = c.ce_bid[8] + 0.50
    return c

check("fresh quotes fire", len(scan_all(planted())) >= 1)
stale = planted()
stale.ce_age[8] = 900.0            # a new snapshot, as the engine builds each pass
check("stale leg blocks the trade",
      not [o for o in scan_all(stale) if o.strategy.startswith("VERTICAL")])
frozen = planted().freeze()
scan_all(frozen)
try:
    frozen.ce_age[8] = 900.0
    check("a frozen snapshot refuses mutation", False, "mutation silently accepted")
except ValueError:
    check("a frozen snapshot refuses mutation", True)

print("\n[9] Depth gate")
cv = make_chain(depth=1500.0)
cv.ce_bid[8] = cv.ce_ask[3] + 3.0
cv.ce_ask[8] = cv.ce_bid[8] + 0.50
check("deep book fires", len(scan_all(cv, qty=75)) >= 1)
check("size beyond displayed depth is refused", not scan_all(cv, qty=5000))

print("\n[10] Implied financing rate recovered from the chain")
for true_r in (0.05, 0.065, 0.09):
    cv = make_chain(r=true_r, T=30/365, half_spread=0.10)
    got = implied_box_rate(cv)
    check(f"r={true_r} recovered", got is not None and abs(got - true_r) < 0.02,
          f"got {got if got is None else round(got, 4)}")

print("\n[11] The old bot's method, applied to a chain with no arbitrage in it")
# angel_arb_bot.py never saw a book. It polled ltpData() one contract at a time
# with time.sleep(0.1) between calls, then synthesised bid = ltp - 0.15% and
# ask = ltp + 0.15%. Two separate errors compound:
#   1. LTP is the last *trade*, so it sits on one side of the book at random.
#   2. The legs of a 4-leg box were priced up to seconds apart, while NIFTY moved.
# Reproduce both on a chain that is arbitrage-free at every instant.
DRIFT_PTS_PER_SEC = 3.0
POLL_INTERVAL_SEC = 0.15          # time.sleep(0.1) plus REST round trip
rng = np.random.default_rng(7)
base = make_chain()
n = len(base.strikes)
cv = make_chain()
for k in range(n):                # poll order: strike by strike, CE then PE
    for off, (dst_b, dst_a) in enumerate(((cv.ce_bid, cv.ce_ask), (cv.pe_bid, cv.pe_ask))):
        drift = DRIFT_PTS_PER_SEC * POLL_INTERVAL_SEC * (2 * k + off)
        moved = make_chain(spot=24500.0 + drift)      # market has moved on
        src_b = moved.ce_bid if off == 0 else moved.pe_bid
        src_a = moved.ce_ask if off == 0 else moved.pe_ask
        ltp = src_a[k] if rng.integers(0, 2) else src_b[k]
        dst_b[k] = ltp * (1 - 0.0015)
        dst_a[k] = ltp * (1 + 0.0015)
fake_opps = scan_all(cv)
gross = sum(o.gross_inr for o in fake_opps)
print(f"       -> {len(fake_opps)} 'opportunities', "
      f"Rs.{gross:,.0f} of 'profit' that is not there")
check("reproduces the old bot's false positives", len(fake_opps) > 0)
check("the same chain quoted honestly and synchronously yields nothing",
      not scan_all(make_chain()))

print("\n[12] Result cap keeps a dislocated chain off the allocation path")
cv = make_chain()
cv.ce_bid[8] = cv.ce_ask[3] + 3.0      # one bad quote violates against many strikes
cv.ce_ask[8] = cv.ce_bid[8] + 0.50
uncapped = scan_box(cv, RATES, 0.60, 75, 400.0, 1.0, 0.10, top_k=None)
capped = scan_box(cv, RATES, 0.60, 75, 400.0, 1.0, 0.10, top_k=4)
# The cap applies per direction (long box and short box are scanned separately),
# so a cap of 4 admits at most 8 in total.
check("cap limits how many are built",
      len(capped) < len(uncapped) and len(capped) <= 8,
      f"{len(uncapped)} uncapped -> {len(capped)} capped")
if capped and uncapped:
    check("cap keeps the best one",
          abs(max(o.gross_edge_points for o in capped)
              - max(o.gross_edge_points for o in uncapped)) < 1e-9)

print("\n[13] The band: annualised excess yield on contract value, 2-5% per year")
from arb.capital import structure_capital, return_pct
from arb.config import CostModel, RiskLimits
from arb.risk import RiskManager
from types import SimpleNamespace

Q = 65
mk = lambda sym, side, px, opt, k: dict(symbol=sym, side=side, price=px, qty=Q,
                                        opt=opt, strike=k, instrument="OPT")
vert = [mk("a", "BUY", 120, "CE", 23500), mk("b", "SELL", 70, "CE", 23600)]
cap = structure_capital(vert)
check("settlement capital of a debit spread is the debit paid", abs(cap - 50 * Q) < 1e-6, f"₹{cap:,.0f}")
check("return on that capital is arithmetic", abs(return_pct(100, vert) - 3.077) < 0.01)

rm0 = RiskManager(RiskLimits(), CostModel())
today_box = [mk("s1", "SELL", 94.95, "CE", 23350), mk("b1", "BUY", 29.15, "CE", 23550),
             mk("b2", "BUY", 119.65, "PE", 23350), mk("s2", "SELL", 262.0, "PE", 23550)]
opp4d = SimpleNamespace(legs=today_box, T=4.0 / 365.0, expiry="15SEP2026")
cv, tdays = rm0.contract_value(opp4d)
check("contract value is index level x units (mean strike x lot)",
      abs(cv - 23450 * Q) < 1e-6 and abs(tdays - 4.0) < 1e-9, f"₹{cv:,.0f} over {tdays:.1f}d")
rho = rm0.excess_return_pa(392.62, cv, tdays)
check("today's Rs 392.62 box over 4 days is ~2.35% p.a. excess yield -> in band",
      2.0 <= rho <= 5.0, f"{rho:.2f}% p.a.")
check("the same rupees over 2 hours are annualised as one day, not 2 hours",
      abs(rm0.excess_return_pa(392.62, cv, 2 / 24) - rm0.excess_return_pa(392.62, cv, 1.0)) < 1e-9)
check("Rs 100 on the box is below band", rm0.excess_return_pa(100, cv, tdays) < 2.0)
check("Rs 1,000 on the box is above band (bad print territory)",
      rm0.excess_return_pa(1000, cv, tdays) > 5.0, f"{rm0.excess_return_pa(1000, cv, tdays):.2f}%")
fut = [mk("c", "BUY", 120, "CE", 23500), mk("p", "SELL", 118, "PE", 23500),
       dict(symbol="F", side="SELL", price=23502, qty=Q, opt="FUT", strike=0.0)]
cvf, _ = rm0.contract_value(SimpleNamespace(legs=fut, T=0.05, expiry="29SEP2026"))
check("a futures leg sets the contract value at the futures price", abs(cvf - 23502 * Q) < 1e-6)
fly = [mk("a", "BUY", 100, "CE", 23400), dict(mk("b", "SELL", 60, "CE", 23450), qty=2 * Q),
       mk("c", "BUY", 30, "CE", 23500)]
cvb, _ = rm0.contract_value(SimpleNamespace(legs=fly, T=0.01, expiry="15SEP2026"))
check("a butterfly's doubled middle does not double the notional", abs(cvb - 23450 * Q) < 1e-6)

opp = [o for o in scan_vertical(planted(), RATES, 0.6, 65, 400.0, 1.0, 0.1,
                                top_k=None)][0]
ok, why = rm0.check(opp)
check("a paid-to-hold spread is not refused for being too profitable",
      why != "capital_unbounded", why)
check("zero-capital arbitrage is flagged free carry",
      opp.detail.get("free_carry") in (True, None), f"ok={ok} why={why}")
check("the gate recorded the annualised figure it judged",
      why != "ok" or "excess_return_pa" in opp.detail, why)

print("\n[14] Invested = what the broker blocks, from Angel One's margin calculator")
from arb.margin import MarginService, _units


class FakeAngel:
    """Answers like the real endpoint did on 11 Sep 2026: Rs 83,656 for a
    one-lot NIFTY box, ~Rs 1.75 lakh for a naked short, linear in lots."""
    def __init__(self):
        self.calls = []

    def getMarginApi(self, params):
        pos = params["positions"]
        self.calls.append(pos)
        shorts = sum(p["qty"] for p in pos if p["tradeType"] == "SELL")
        longs = sum(p["qty"] for p in pos if p["tradeType"] == "BUY")
        per_unit = 83656.30 / (2 * 65) if longs else 174652.66 / 65
        return {"status": True, "message": "SUCCESS",
                "data": {"totalMarginRequired": round(per_unit * shorts, 2),
                         "marginComponents": {"spanMargin": 0.0}}}


class B:                                   # the slice of ChainBinding margin uses
    underlying, expiry, segment, lot_size = "NIFTY", "15SEP2026", "NFO", 65
    strikes = [23300.0, 23350.0, 23400.0, 23450.0]
    ce_token = ["1", "2", "3", "4"]; pe_token = ["5", "6", "7", "8"]
    fut_token = "9"


lim = RiskLimits(margin_call_spacing_sec=0.0)
fake = FakeAngel()
ms = MarginService(fake, [B()], lim, log=lambda *a: None)
box = [dict(mk("s1", "SELL", 94.95, "CE", 23350), token="2", exchange="NFO", underlying="NIFTY"),
       dict(mk("b1", "BUY", 29.15, "CE", 23550), token="4", exchange="NFO", underlying="NIFTY"),
       dict(mk("b2", "BUY", 119.65, "PE", 23350), token="6", exchange="NFO", underlying="NIFTY"),
       dict(mk("s2", "SELL", 262.0, "PE", 23550), token="8", exchange="NFO", underlying="NIFTY")]
check("shape of a box is 2 short / 2 long option units", _units(box) == (2, 2, 0, 0))
fly = [dict(box[0], qty=Q, side="BUY"), dict(box[1], qty=2 * Q, side="SELL"),
       dict(box[2], qty=Q, side="BUY", opt="CE")]
check("butterfly's doubled middle counts as two short units", _units(fly) == (2, 2, 0, 0),
      str(_units(fly)))

est0, src0 = ms.estimate(box, "15SEP2026")
check("uncalibrated estimate is the notional fallback, not Rs 13,000",
      src0 == "fallback" and est0 > 50_000, f"{src0} ₹{est0:,.0f}")

ms._running.set()
n, missed = ms.calibrate_once()
check("calibration asks for spread, box, conversion and reversal", (n, missed) == (4, 0), str((n, missed)))
check("calibration used MARKET orders with no price", all(p["orderType"] == "MARKET"
      for call in fake.calls for p in call))
est, src = ms.estimate(box, "15SEP2026")
check("box now sized off Angel's Rs 83,656 per lot", abs(est - 83656.30) < 1.0 and
      src == "angel_one:table", f"{src} ₹{est:,.2f}")
two = [dict(l, qty=2 * Q) for l in box]
check("two lots scale linearly", abs(ms.estimate(two, "15SEP2026")[0] - 2 * 83656.30) < 1.0)

rm2 = RiskManager(lim, CostModel(), margin=ms)
capm, settle, m_est, csrc = rm2.capital_for(box, "15SEP2026")
check("capital is the broker margin when it exceeds settlement",
      abs(capm - 83656.30) < 1.0 and abs(settle - 13000.0) < 1.0 and csrc == "angel_one:table",
      f"capital ₹{capm:,.0f} settlement ₹{settle:,.0f} ({csrc})")
check("today's Rs 392 box is 0.47% on real margin (reported, not gated)",
      abs(100.0 * 392.62 / capm - 0.469) < 0.01, f"{100.0 * 392.62 / capm:.2f}%")
debit = [mk("a", "BUY", 120, "CE", 23500), mk("b", "SELL", 70, "CE", 23600)]
for l in debit:
    l.update(token="3", exchange="NFO", underlying="NIFTY")
capd, settled, _, srcd = rm2.capital_for(debit, "15SEP2026")
check("settlement wins when it is the larger figure", capd >= settled and capd >= 3250)

ex = ms.exact(two)
check("exact() returns the broker's figure for the traded quantity",
      ex is not None and abs(ex["total"] - 2 * 83656.30) < 1.0, str(ex))
fake.getMarginApi = lambda params: {"status": False, "message": "AB1004 rate limited"}
check("a refused margin call is None, with the reason kept", ms.exact(box) is None and
      "AB1004" in ms.last_error, ms.last_error)
snap = ms.snapshot()
check("snapshot names the shapes for the dashboard",
      {r["shape"] for r in snap["rows"]} == {"spread", "box / butterfly", "conversion", "reversal"},
      str(sorted({r["shape"] for r in snap["rows"]})))

print("\n[15] The chain as mathematics: density, distribution, parity, term structure")
from arb.mathview import chain_math, term_structure
from arb.book import QuoteStore


class MiniBinding:
    def __init__(self, cv, store):
        self.underlying, self.expiry, self.segment = "NIFTY", cv.expiry, "NFO"
        self.strikes, self.lot_size, self.T = cv.strikes, cv.lot_size, cv.T
        n = len(cv.strikes)
        self.ce_slots = np.arange(0, n); self.pe_slots = np.arange(n, 2 * n)
        self.fut_slot = 2 * n
        for i in range(n):
            store.bid[i], store.ask[i] = cv.ce_bid[i], cv.ce_ask[i]
            store.bid[n + i], store.ask[n + i] = cv.pe_bid[i], cv.pe_ask[i]
        store.bid[2 * n], store.ask[2 * n] = cv.fut_bid, cv.fut_ask


cvm = make_chain(spot=24500.0, n=25, step=50.0, T=10 / 365, r=0.065, vol=0.13, half_spread=0.05)
st = QuoteStore(64)
mb = MiniBinding(cvm, st)
mm = chain_math(mb, st, 24500.0, 0.065, RiskLimits(), 0)
dens = [v for v in mm["density"] if v is not None]
check("density from an arbitrage-free chain is non-negative everywhere",
      dens and min(dens) >= -1e-9 and not mm["negative_density_at"], f"min q̂ = {min(dens):.2e}")
# ±12 strikes of 50 is ±1.1 sigma for 13% vol over 10 days, so the grid should
# hold roughly three quarters of the mass and never more than one.
check("density integrates to ~0.75 over ±1.1σ, never above one",
      0.6 < mm["mass"] <= 1.02, f"mass {mm['mass']}")
cdf = [v for v in mm["cdf_mid"] if v is not None]
check("implied distribution function is monotone and within [0, 1]",
      all(b >= a - 1e-9 for a, b in zip(cdf, cdf[1:])) and min(cdf) >= -1e-6 and max(cdf) <= 1 + 1e-6,
      f"Q̂ from {min(cdf):.4f} to {max(cdf):.4f}")
lo_r, hi_r = mm["spread_ratio_range"]
check("every vertical spread sits inside its bound", lo_r >= -1e-6 and hi_r <= 1 + 1e-6, f"[{lo_r}, {hi_r}]")
check("box ratio on mids is 1 to within the tick", abs(mm["box_ratio_median"] - 1.0) < 2e-3, str(mm["box_ratio_median"]))
check("box-implied rate recovers the 6.5% the chain was built with",
      abs(mm["box_rate_median"] - 0.065) < 0.01, f"{mm['box_rate_median']:.4f}")
F_true = 24500.0 * math.exp(0.065 * 10 / 365)
check("parity forward matches the future to within the half spread",
      abs(mm["parity"]["F_hat"] - F_true) < 0.5 and abs(mm["parity"]["deviation_pts"]) < 0.5,
      f"F̂ {mm['parity']['F_hat']} vs F {F_true:.2f}")
need_lo, need_hi = mm["band"]["need_inr"]
check("2% p.a. on Rs 18.4 lakh for 10 days is about Rs 1,007 (the band in rupees)",
      abs(need_lo - 24500 * 75 * 0.02 * 10 / 365) < 1.0 and abs(need_hi / need_lo - 2.5) < 1e-3,
      f"₹{need_lo:,.0f} – ₹{need_hi:,.0f}")

st2 = QuoteStore(8)
S = 24500.0
for i, (T, r) in enumerate(((10 / 365, 0.065), (38 / 365, 0.065), (66 / 365, 0.09))):
    F = S * math.exp(r * T); st2.bid[i], st2.ask[i] = F - 0.5, F + 0.5
futs = [{"expiry": "E1", "symbol": "F1", "lotsize": 75, "slot": 0},
        {"expiry": "E2", "symbol": "F2", "lotsize": 75, "slot": 1},
        {"expiry": "E3", "symbol": "F3", "lotsize": 75, "slot": 2}]
Ts = {"E1": 10 / 365, "E2": 38 / 365, "E3": 66 / 365}
ts = term_structure("NIFTY", futs, st2, S, 0.065, {"E1": 0.065}, lambda e: Ts[e])
check("each future's carry is read back", all(abs(f["carry"] - r) < 1e-3 for f, r in zip(ts["futures"], (0.065, 0.065, 0.09))))
p1, p2 = ts["pairs"]
check("a fairly priced calendar shows no edge", abs(p1["edge_pts"]) < 0.05 and abs(p1["deviation_pa"]) < 1e-3,
      f"{p1['edge_pts']} pts, {p1['deviation_pa']:+.4f}")
check("a far month carrying 9% against 6.5% is flagged rich by ~+12% forward carry",
      p2["rich"] == "far" and p2["deviation_pa"] > 0.05 and p2["edge_inr_per_lot"] > 1000,
      f"fwd {p2['forward_carry']:.4f}, edge ₹{p2['edge_inr_per_lot']:,.0f}/lot")

print("\n[16] Entry, exit and P&L per leg: live mark before expiry, settlement after")
import tempfile
from arb.settlement import LegMarker, SettlementBook, intrinsic

tmpd = tempfile.mkdtemp()
book = SettlementBook(os.path.join(tmpd, "settlements.json"))
st3 = QuoteStore(16)
for tok, (bid, ask) in {"11": (96.0, 97.0), "12": (28.0, 29.0), "13": (121.0, 122.0), "14": (259.0, 260.0)}.items():
    sl = st3.assign(tok); st3.bid[sl], st3.ask[sl] = bid, ask
mk2 = LegMarker(st3, book)
def trade(expiry):
    return {"id": 1, "expiry": expiry, "underlying": "NIFTY", "cost": 143.31, "net": 392.62, "gross": 535.94,
            "legs": [{"side": "SELL", "token": "11", "opt": "CE", "strike": 23350, "qty": 65, "price": 94.90},
                     {"side": "BUY",  "token": "12", "opt": "CE", "strike": 23550, "qty": 65, "price": 29.20},
                     {"side": "BUY",  "token": "13", "opt": "PE", "strike": 23350, "qty": 65, "price": 119.70},
                     {"side": "SELL", "token": "14", "opt": "PE", "strike": 23550, "qty": 65, "price": 261.95}]}
live = mk2.enrich(trade("31DEC2099"))
check("before expiry every leg is marked at the live mid", all(l["exit_kind"] == "mark" for l in live["legs"])
      and live["legs"][0]["exit"] == 96.5, str([l["exit"] for l in live["legs"]]))
check("leg P&L is signed by side: short CE 94.90 -> 96.50 loses 1.60 x 65",
      abs(live["legs"][0]["pnl"] + 1.60 * 65) < 1e-6, str(live["legs"][0]["pnl"]))
exp_legs = sum(l["pnl"] for l in live["legs"])
check("trade P&L now is legs less costs", abs(live["mtm_pnl"] - (exp_legs - 143.31)) < 1e-6
      and live["mtm_kind"] == "mark-to-market", f"{live['mtm_pnl']} ({live['mtm_kind']})")

old_t = mk2.enrich(trade("01JAN2020"))
check("an expired trade with no settlement level waits rather than guessing",
      old_t["mtm_pnl"] is None and old_t["mtm_kind"] == "awaiting settlement", old_t["mtm_kind"])
book.record("NIFTY", "01JAN2020", 23480.0, "test")
settled = mk2.enrich(trade("01JAN2020"))
check("after settlement each leg exits at intrinsic value at the recorded level",
      settled["legs"][0]["exit"] == 130.0 and settled["legs"][1]["exit"] == 0.0
      and settled["legs"][2]["exit"] == 0.0 and settled["legs"][3]["exit"] == 70.0,
      str([l["exit"] for l in settled["legs"]]))
# Short box: receive 208.15 credit less 0.20 slippage-adjusted = 207.95... settle pays 200 -> legs P&L = (207.95 - 200) x 65
credit = (94.90 - 29.20 - 119.70 + 261.95)
check("a short box settles to credit minus width whatever the level",
      abs(settled["legs_pnl"] - (credit - 200.0) * 65) < 1e-6, f"legs {settled['legs_pnl']} vs {(credit - 200) * 65:.2f}")
book.record("NIFTY", "01JAN2020", 23000.0, "test")
settled2 = mk2.enrich(trade("01JAN2020"))
check("...and the same at a very different settlement level", abs(settled2["legs_pnl"] - settled["legs_pnl"]) < 1e-6)
# Locked at entry = gross at quotes 535.94 - slippage 13.00 - costs 143.31 = 379.63.
# Settlement pays the credit at fills less the width, so the only gap left is the
# financing on the Rs 13,000 credit held to expiry (~Rs 6 over four days).
check("settled trade P&L converges on the edge locked at entry, up to financing on the credit",
      abs(settled["mtm_pnl"] - 379.63) < 8.0, f"settled {settled['mtm_pnl']} vs locked 379.63")
book2 = SettlementBook(book.path)
check("settlement levels persist to disk", book2.get("NIFTY", "01JAN2020")["level"] == 23000.0)
check("intrinsic values: CE, PE, FUT", intrinsic({"opt": "CE", "strike": 100}, 110) == 10 and
      intrinsic({"opt": "PE", "strike": 100}, 110) == 0 and intrinsic({"opt": "FUT"}, 110) == 110)

print("\n[17] The session clock: 09:15 open, 15:15 close, weekends and holidays skipped")
from datetime import datetime as _dt
from arb.instruments import IST, is_trading_day, next_session, session_bounds, load_holidays

H = {_dt(2026, 10, 2).date()}
at = lambda s: _dt.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
o, c = next_session(at("2026-09-22 08:00"), holidays=H)
check("before the open on a trading day, today's session", o == at("2026-09-22 09:15") and c == at("2026-09-22 15:15"))
o, c = next_session(at("2026-09-22 11:00"), holidays=H)
check("inside the session, today's session (open already past)", o == at("2026-09-22 09:15") and o <= at("2026-09-22 11:00") < c)
o, c = next_session(at("2026-09-22 15:15"), holidays=H)
check("at the close exactly, the next day's session", o == at("2026-09-23 09:15"))
o, c = next_session(at("2026-09-25 16:00"), holidays=H)
check("Friday evening rolls to Monday", o == at("2026-09-28 09:15"), o.strftime("%a %d %b"))
o, c = next_session(at("2026-10-01 20:00"), holidays=H)
check("a listed holiday (2 Oct) is skipped, and so is the weekend after it", o == at("2026-10-05 09:15"), o.strftime("%a %d %b"))
check("Saturday is not a trading day", not is_trading_day(_dt(2026, 9, 26).date(), H))
check("session bounds honour the configured clock", session_bounds(_dt(2026, 9, 22).date(), "09:20", "15:10")[1] == at("2026-09-22 15:10"))
import tempfile as _tf
with _tf.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
    fh.write("# comment\n2026-11-09   # Diwali (example)\n\nnot-a-date\n")
check("holiday file parsing ignores comments, blanks and junk", load_holidays(fh.name) == {_dt(2026, 11, 9).date()})

print("\n" + "=" * 64)
print(f"  {'ALL CHECKS PASSED' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
print("=" * 64)
sys.exit(1 if FAILS else 0)
