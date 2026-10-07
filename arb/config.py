"""Configuration: credentials, cost model, risk limits, engine tuning."""
import json
import os
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file(path):
    """Parse a .env file leniently (the project's file has stray JSON/comments in it)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("{"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'\"")
            if key.isidentifier() and val:
                out[key] = val
    return out


@dataclass
class Credentials:
    api_key: str = ""
    api_secret: str = ""
    client_id: str = ""
    pin: str = ""
    totp_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def load(cls, env_path=None):
        env_path = env_path or os.path.join(ROOT, ".angel.env")
        e = _load_env_file(env_path)
        e.update({k: v for k, v in os.environ.items() if k.startswith(("ANGEL", "ANGLE", "TELEGRAM"))})

        def pick(*names):
            for n in names:
                if e.get(n):
                    return e[n]
            return ""

        return cls(
            api_key=pick("ANGEL_API_KEY", "ANGLE_ONE_API"),
            api_secret=pick("ANGEL_API_SECRET", "ANGLE_ONE_API_SECRET"),
            client_id=pick("ANGEL_CLIENT_ID", "ANGLE_ONE_ID"),
            pin=pick("ANGEL_PIN", "ANGEL_PASSWORD", "ANGLE_ONE_PIN"),
            totp_secret=pick("ANGEL_TOTP_SECRET", "ANGLE_ONE_TOTP"),
            telegram_bot_token=pick("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=pick("TELEGRAM_CHAT_ID"),
        )

    def missing(self):
        req = {"api_key": self.api_key, "client_id": self.client_id,
               "pin": self.pin, "totp_secret": self.totp_secret}
        return [k for k, v in req.items() if not v]


@dataclass
class CostModel:
    """Indian F&O statutory costs. Every rate is a fraction of turnover unless noted.

    Defaults follow .agents/AGENTS.md. They are deliberately on the conservative
    (expensive) side: an overstated cost can only make us skip a trade, while an
    understated one manufactures edge that is not there.
    """
    brokerage_per_order: float = 20.0
    opt_stt_sell: float = 0.00125
    opt_exchange_txn: float = 0.0005
    opt_stamp_buy: float = 0.00003
    fut_stt_sell: float = 0.0002
    fut_exchange_txn: float = 0.000019
    fut_stamp_buy: float = 0.00002
    sebi_fee: float = 0.000001
    gst_rate: float = 0.18
    ipft: float = 0.000005
    # Execution friction, in price points per leg, charged on top of the quoted
    # price we cross. Calibrated from live fills by research.py.
    slippage_ticks_per_leg: float = 1.0
    tick_size: float = 0.05

    @classmethod
    def load(cls, path=None):
        path = path or os.path.join(ROOT, "config", "costs.json")
        if os.path.exists(path):
            with open(path) as fh:
                return cls(**json.load(fh))
        return cls()

    def save(self, path=None):
        path = path or os.path.join(ROOT, "config", "costs.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.__dict__, fh, indent=2)

    def slippage_points(self):
        return self.slippage_ticks_per_leg * self.tick_size


@dataclass
class RiskLimits:
    # The trading book. Capital deployed per structure is derived from its own
    # payoff (see arb/capital.py), never assumed.
    total_capital: float = 1_000_000.0        # Rs 10 lakh
    # The trading band, in percent PER YEAR of contract value: the structure's
    # net edge, expressed as an annualised excess yield on the index notional it
    # controls (S x lot). For a box or parity trade this is exactly how far the
    # market's implied financing rate sits from fair - the number the rulebook
    # calls "implied carry vs repo". A per-trade 2-5% on margin is unreachable
    # on liquid index options (it needs a 26-point edge on a weekly box, at the
    # plausibility cap); 2-5% per year is a real dislocation and still rare.
    # The upper bound is a sanity cap, not a preference: a deviation above it is
    # a bad print or a frozen quote far more often than free money.
    min_excess_return_pa: float = 2.0
    max_excess_return_pa: float = 5.0
    # Annualising a two-hour tenor turns a tick into a triple-digit yield, so the
    # tenor used for the band is floored here: near expiry the edge must be worth
    # at least what a full day of carry at the band would pay.
    min_tenor_days: float = 1.0
    # The stake: the same Rs 1 lakh of broker margin on every trade, and never
    # more (owner's rule, set 11 Sep 2026, cut to Rs 1 lakh 18 Sep 2026). Lots
    # are the most whole lots whose margin
    # fits inside it, then capped by the depth actually displayed at the touch -
    # never by what we wish were there. A structure whose single lot needs more
    # than the stake is not traded at all. Each day's profit is also reported as
    # a return on this stake, since it is the amount actually put at work.
    stake_per_trade: float = 100_000.0
    max_lots_per_trade: int = 12
    max_open_structures: int = 3
    # Coarse sanity guard only; capital utilisation is the real control. A
    # single futures leg on NIFTY already carries ~Rs 15 lakh of notional.
    max_gross_notional: float = 8_000_000.0
    max_daily_loss: float = 25_000.0
    max_trades_per_day: int = 40
    trade_cooldown_sec: float = 2.0
    # A dislocation that is still on the screen after we traded it is usually the
    # same one, not a second opportunity. Re-arming on the identical set of legs
    # is how a single stale quote turns into a run of duplicate trades.
    structure_cooldown_sec: float = 45.0
    # Several distinct structures can all lean on one dislocated contract. Each
    # is genuinely an arbitrage if the quote is real - and all of them lose
    # together if it was a bad print. Trade one and stand down on that contract.
    instrument_cooldown_sec: float = 20.0
    # A leg is only tradable if the displayed size covers this multiple of ours.
    min_depth_multiple: float = 1.0
    # Every leg of a structure must have been quoted within this window, else the
    # structure is stale and the "edge" is an artifact of asynchronous prices.
    max_quote_age_ms: float = 400.0
    # Reject absurd edges outright: on liquid index options an edge this large is
    # far more likely to be a bad print or a frozen quote than free money.
    max_plausible_edge_points: float = 25.0
    # Absolute floor, secondary to the return band above. A structure tying up
    # Rs 3,250 hits 2% at Rs 65, so a Rs 150 floor would silently raise the real
    # minimum to 4.6% and make the configured band a fiction.
    require_positive_net_inr: float = 50.0
    # Never deploy more than this share of the book at once.
    max_capital_utilisation: float = 0.60
    # "Invested" means what Angel One actually blocks, taken from its own margin
    # calculator (SPAN + exposure, net of hedge benefit) - not the structure's
    # theoretical settlement obligation. A one-lot NIFTY box that owes Rs 13,000
    # at expiry has Rs 83,656 of margin blocked against it (measured 11 Sep 2026).
    # The scanner uses a per-chain table calibrated from the API so there is no
    # HTTP call in the detection path; the exact figure for the legs actually
    # traded is fetched right after the fill and recorded on the trade.
    margin_product: str = "CARRYFORWARD"      # NRML: structures are held to expiry
    margin_refresh_sec: float = 600.0         # re-calibrate the per-chain table
    margin_call_spacing_sec: float = 1.5      # the margin endpoint is rate limited
    # Until the table is calibrated (or if the API is down), assume this share of
    # notional per short option unit on a hedged structure. Angel's figure for a
    # fully hedged NIFTY box came to ~2.75% per short unit; 3% is on the safe side.
    margin_fallback_pct_per_short_unit: float = 0.03


@dataclass
class Underlying:
    """One index and where its contracts live.

    The three differ in more than name: SENSEX options trade on BSE's BFO segment
    (websocket exchange type 4) while the NSE indices use NFO (type 2), and lot
    size and strike step differ per index. Nothing may assume NIFTY's values.
    """
    name: str
    spot_token: str
    spot_exchange: str = "NSE"          # for the REST LTP call
    spot_exchange_type: int = 1         # websocket: 1 NSE_CM, 3 BSE_CM
    segment: str = "NFO"                # scrip master exch_seg, and the order exchange
    option_exchange_type: int = 2       # websocket: 2 NSE_FO, 4 BSE_FO
    num_expiries: int = 3
    strikes_each_side: int = 10
    enabled: bool = True


DEFAULT_UNDERLYINGS = [
    # NIFTY: weekly Tuesday expiries, lot 65, 50-point strikes. Only its monthly
    # carries a future, so put-call parity is limited to that expiry.
    dict(name="NIFTY", spot_token="99926000", spot_exchange="NSE",
         spot_exchange_type=1, segment="NFO", option_exchange_type=2),
    # BANKNIFTY: monthly only since weeklies were withdrawn, lot 30, 100-point
    # strikes. Every option expiry has a matching future, so parity works on all.
    dict(name="BANKNIFTY", spot_token="99926009", spot_exchange="NSE",
         spot_exchange_type=1, segment="NFO", option_exchange_type=2,
         num_expiries=2),
    # SENSEX: BSE, weekly Thursday expiries, lot 20, 100-point strikes.
    dict(name="SENSEX", spot_token="99919000", spot_exchange="BSE",
         spot_exchange_type=3, segment="BFO", option_exchange_type=4),
]


@dataclass
class EngineConfig:
    underlyings: list = field(default_factory=lambda:
                              [dict(u) for u in DEFAULT_UNDERLYINGS])
    num_strikes_each_side: int = 10
    # Skip contracts in their final minutes. The arithmetic still holds at expiry,
    # but a partial fill there cannot be unwound before settlement, so the
    # "risk-free" part stops being true exactly when it matters.
    min_hours_to_expiry: float = 1.0
    risk_free_rate: float = 0.065
    # Use the market's own cross-sectional implied box rate instead of a fixed
    # repo assumption; falls back to risk_free_rate until enough boxes are seen.
    use_implied_rate: bool = True
    # Plausible band for a market-implied financing rate. A reading outside this
    # is treated as a broken measurement, not a discovery, and the engine falls
    # back to risk_free_rate.
    implied_rate_floor: float = 0.02
    implied_rate_cap: float = 0.15
    scan_interval_ms: float = 0.0
    trading_mode: str = "paper"   # paper | live
    strategies: list = field(default_factory=lambda: [
        "vertical", "box", "butterfly", "pcp_futures",
    ])
    record_ticks: bool = True
    session_start: str = "09:15"
    session_end: str = "15:15"

    @classmethod
    def load(cls, path=None):
        # ARB_ENGINE_CONFIG lets a test point the whole process (daemon and its
        # children alike) at a config with a shortened session clock.
        path = path or os.environ.get("ARB_ENGINE_CONFIG") \
            or os.path.join(ROOT, "config", "engine.json")
        if os.path.exists(path):
            with open(path) as fh:
                data = json.load(fh)
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})

    def active_underlyings(self):
        out = []
        for u in self.underlyings:
            d = dict(u)
            if not d.pop("enabled", True):
                continue
            d.setdefault("strikes_each_side", self.num_strikes_each_side)
            out.append(Underlying(**d))
        return out
        return cls()

    def save(self, path=None):
        path = path or os.path.join(ROOT, "config", "engine.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.__dict__, fh, indent=2)
