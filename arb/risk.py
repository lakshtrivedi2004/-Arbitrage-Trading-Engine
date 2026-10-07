"""Pre-trade risk gate.

Every check here can only stop a trade. They run in order of cost: the cheap
arithmetic guards fire before anything that touches shared state.
"""
import time
from dataclasses import dataclass, field

from arb.capital import structure_capital
from arb.config import RiskLimits
from arb.costs import structure_cost


@dataclass
class RiskState:
    trades_today: int = 0
    open_structures: int = 0
    session_pnl: float = 0.0
    gross_notional: float = 0.0
    capital_deployed: float = 0.0
    last_trade_ts: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    rejects: dict = field(default_factory=dict)


class RiskManager:
    def __init__(self, limits: RiskLimits, cost_model, state: RiskState = None,
                 margin=None):
        self.limits = limits
        self.cm = cost_model
        self.state = state or RiskState()
        # arb.margin.MarginService, when the broker is reachable. Without it
        # capital falls back to the structure's own settlement obligation.
        self.margin = margin

    def capital_for(self, legs, expiry=None):
        """Rupees the account must hold for these legs at these quantities:
        the larger of the settlement obligation and what the broker blocks.

        Returns (capital, settlement, margin_estimate, source). ``capital`` is
        inf when the structure has no finite worst case and no margin figure.
        """
        settlement = structure_capital(legs)
        m_est, src = (0.0, "settlement")
        if self.margin is not None and legs:
            m_est, src = self.margin.estimate(legs, expiry)
        if settlement == float("inf"):
            return (m_est if m_est > 0 else settlement), settlement, m_est, src
        if m_est > settlement:
            return m_est, settlement, m_est, src
        return settlement, settlement, m_est, "settlement"

    def _reject(self, reason):
        self.state.rejects[reason] = self.state.rejects.get(reason, 0) + 1
        return False, reason

    def check(self, opp, now=None):
        """Return (ok, reason). ``opp`` is an Opportunity; its exact cost is
        computed here because this is the last point before an order goes out."""
        L, S = self.limits, self.state
        now = now or time.time()

        if S.halted:
            return self._reject(f"halted: {S.halt_reason}")
        if S.trades_today >= L.max_trades_per_day:
            return self._reject("max_trades_per_day")
        if S.open_structures >= L.max_open_structures:
            return self._reject("max_open_structures")
        if now - S.last_trade_ts < L.trade_cooldown_sec:
            return self._reject("cooldown")
        if S.session_pnl <= -abs(L.max_daily_loss):
            S.halted = True
            S.halt_reason = "daily loss limit"
            return self._reject("daily_loss_limit")

        if opp.max_age_ms > L.max_quote_age_ms:
            return self._reject("stale_quotes")
        if opp.min_depth_ratio < L.min_depth_multiple:
            return self._reject("insufficient_depth")
        if opp.gross_edge_points > L.max_plausible_edge_points:
            # On liquid index options an edge this size is a bad print or a
            # frozen quote far more often than it is free money.
            return self._reject("implausible_edge")
        if opp.gross_edge_points <= 0:
            return self._reject("non_positive_edge")

        notional = sum(abs(l["price"]) * l["qty"] for l in opp.legs)
        if S.gross_notional + notional > L.max_gross_notional:
            return self._reject("max_gross_notional")

        exact_cost = structure_cost(opp.legs, self.cm)
        net = opp.gross_inr - exact_cost
        if net < L.require_positive_net_inr:
            return self._reject("net_below_floor")

        capital, settlement, m_est, src = self.capital_for(opp.legs, opp.expiry)
        if capital == float("inf"):
            # No finite worst case (a futures-only structure): not sizable.
            return self._reject("capital_unbounded")
        if S.capital_deployed + capital > L.total_capital * L.max_capital_utilisation:
            return self._reject("capital_utilisation")

        # The band: annualised excess yield on the contract value the structure
        # controls. rho = net / (S q) x 365 / T. For a box this is the gap between
        # the market's implied financing rate and fair; for parity, the futures
        # basis mispricing in rate terms. It is judged on contract value, not on
        # margin, because margin is a broker parameter and a return on it is not
        # a statement about the market.
        contract_value, T_days = self.contract_value(opp)
        rho = self.excess_return_pa(net, contract_value, T_days)
        if rho < L.min_excess_return_pa:
            return self._reject("return_below_band")
        if rho > L.max_excess_return_pa:
            return self._reject("return_above_band")

        # Reported alongside, never gated on: what the trade earns on the margin
        # Angel One blocks. Zero-capital structures ("free carry") have no such
        # ratio, and are flagged instead.
        free_carry = capital <= 1.0
        ret = float("inf") if free_carry else 100.0 * net / capital

        opp.detail["exact_cost_inr"] = exact_cost
        opp.detail["exact_net_inr"] = net
        opp.detail["notional"] = notional
        opp.detail["contract_value"] = contract_value
        opp.detail["tenor_days"] = T_days
        opp.detail["excess_return_pa"] = rho
        opp.detail["capital_inr"] = capital
        opp.detail["settlement_inr"] = settlement
        opp.detail["margin_est_inr"] = m_est
        opp.detail["capital_source"] = src
        opp.detail["return_pct"] = ret
        opp.detail["free_carry"] = free_carry
        return True, "ok"

    @staticmethod
    def contract_value(opp, legs=None):
        """(index notional in rupees, tenor in days) for a structure.

        Contract value is the index level times the units controlled: the
        futures price when the structure has a futures leg, else the mean strike
        of its option legs (which brackets spot). Units are one lot's worth,
        i.e. the smallest leg quantity, so a butterfly's doubled middle does not
        double the notional.
        """
        legs = legs if legs is not None else opp.legs
        if not legs:
            return 0.0, 0.0
        base = min(abs(int(l["qty"])) for l in legs)
        futs = [float(l.get("fill_price") or l.get("price") or 0.0)
                for l in legs if l.get("opt") == "FUT"]
        if futs:
            level = sum(futs) / len(futs)
        else:
            ks = [float(l["strike"]) for l in legs if l.get("strike")]
            level = sum(ks) / len(ks) if ks else 0.0
        T = getattr(opp, "T", 0.0) or 0.0
        if T <= 0 and getattr(opp, "expiry", ""):
            from arb.instruments import years_to_expiry
            T = years_to_expiry(opp.expiry)
        return level * base, T * 365.0

    def excess_return_pa(self, net_inr, contract_value, tenor_days):
        if contract_value <= 0:
            return 0.0
        days = max(tenor_days, self.limits.min_tenor_days)
        return 100.0 * net_inr / contract_value * 365.0 / days

    def on_trade(self, net_inr, notional, capital=0.0, now=None):
        S = self.state
        S.trades_today += 1
        S.session_pnl += net_inr
        S.gross_notional += notional
        S.capital_deployed += capital
        S.last_trade_ts = now or time.time()

    def adjust_capital(self, delta):
        """Correct deployed capital once the broker's exact margin is known."""
        self.state.capital_deployed += delta

    @property
    def equity(self):
        return self.limits.total_capital + self.state.session_pnl

    @property
    def capital_free(self):
        return (self.limits.total_capital * self.limits.max_capital_utilisation
                - self.state.capital_deployed)

    def halt(self, reason):
        self.state.halted = True
        self.state.halt_reason = reason

    def restore_today(self, journal_dir=None, day=None):
        """Seed today's counters from the journal on disk.

        RiskState lives in memory, so without this every restart forgot the
        day: the dashboard showed Rs 0 after two real trades, and - far worse -
        max_trades_per_day, the daily loss limit and the capital-utilisation cap
        all reset to zero. A bot that crash-looped could have blown through its
        daily loss limit once per restart. The journal is the source of truth for
        what happened today; the process is not.
        """
        from arb import history
        from arb.instruments import now_ist
        day = day or now_ist().strftime("%Y-%m-%d")
        trades = [t for t in history.load_trades(journal_dir or history.JOURNAL, days=3)
                  if t.get("_day") == day]
        if not trades:
            return 0
        S = self.state
        S.trades_today = len(trades)
        S.session_pnl = sum(t.get("net_inr", 0.0) for t in trades)
        S.capital_deployed = sum(t.get("capital_inr", 0.0) for t in trades)
        S.gross_notional = sum(
            abs(l.get("fill_price", 0.0)) * l.get("qty", 0)
            for t in trades for l in t.get("legs", []) if l.get("filled", True))
        try:
            S.last_trade_ts = max(
                __import__("datetime").datetime.fromisoformat(t["ts_ist"]).timestamp()
                for t in trades if t.get("ts_ist"))
        except Exception:
            pass
        if S.session_pnl <= -abs(self.limits.max_daily_loss):
            S.halted = True
            S.halt_reason = "daily loss limit (restored from journal)"
        return max(int(t.get("trade_id", 0)) for t in trades)
