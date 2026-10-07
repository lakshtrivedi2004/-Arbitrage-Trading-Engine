"""Telegram alerts on a background thread.

Nothing here ever runs on the scanning or execution path. Alerts are queued and
drained by a worker, so a slow or failing Telegram API cannot delay an order by
even a millisecond. Every executed trade is queued unconditionally - the queue is
unbounded for trade alerts and only heartbeat/status messages are rate limited.
"""
import json
import queue
import threading
import time
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, token, chat_id, enabled=True):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(enabled and token and chat_id)
        self.q = queue.Queue()
        self.sent = 0
        self.failed = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.enabled:
            return self
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=8.0):
        self._stop.set()
        self.q.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    def send(self, text, priority=False):
        """Queue a message. Returns immediately; never raises."""
        if not self.enabled:
            return False
        try:
            self.q.put((time.time(), text, priority))
            return True
        except Exception:
            return False

    def flush(self, timeout=10.0):
        deadline = time.time() + timeout
        while not self.q.empty() and time.time() < deadline:
            time.sleep(0.05)

    def _run(self):
        while not self._stop.is_set():
            item = self.q.get()
            if item is None:
                break
            _, text, _ = item
            self._deliver(text)

    def _deliver(self, text):
        url = API.format(token=self.token)
        payload = json.dumps({
            "chat_id": self.chat_id, "text": text[:4096],
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }).encode("utf-8")
        backoff = 1.0
        for _ in range(4):
            try:
                req = urllib.request.Request(
                    url, data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("ok"):
                        self.sent += 1
                        return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = 3.0
                    try:
                        body = json.loads(e.read().decode("utf-8"))
                        retry = float(body.get("parameters", {}).get("retry_after", 3))
                    except Exception:
                        pass
                    time.sleep(min(retry, 30))
                    continue
                if 400 <= e.code < 500:
                    break          # malformed or bad chat id: retrying will not help
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)
        self.failed += 1
        return False


def format_trade(fill, mode):
    """The message sent for every trade the bot takes."""
    tag = "PAPER" if mode == "paper" else "LIVE"
    o = fill["opportunity"]
    cap = fill.get("capital_inr", 0.0)
    ret = fill.get("return_pct", 0.0)
    total = fill.get("total_capital", 0.0)
    equity = fill.get("equity", total)
    deployed = fill.get("capital_deployed", 0.0)
    pnl = fill["session_pnl"]
    day_pct = (100.0 * pnl / total) if total else 0.0

    lines = [
        f"{'🧪' if mode == 'paper' else '⚡'} <b>TRADE TAKEN · {tag}</b>",
        "",
        f"<b>{o['strategy']}</b>",
        f"{o['description']}",
        f"Expiry <code>{o['expiry']}</code> · {fill['lots']} lot × {o['lot_size']}",
        "",
        f"⏱ <b>Executed</b>  <code>{fill.get('executed_at', '')}</code> IST",
        "",
        "<b>Legs</b>  <i>(price, and when that quote was seen)</i>",
    ]
    for leg in fill["legs"]:
        arrow = "🟢 BUY " if leg["side"] == "BUY" else "🔴 SELL"
        status = "" if leg.get("filled", True) else "  ⚠️ UNFILLED"
        kind = leg.get("opt", "")
        tick = f"{kind} " if kind in ("CE", "PE", "FUT") else ""
        qt = leg.get("quoted_at", "")
        ms = leg.get("ms_before_fire", 0.0)
        lines.append(
            f"{arrow} <code>{leg['symbol']}</code>{status}\n"
            f"     {leg['qty']} @ ₹{leg['fill_price']:,.2f}\n"
            f"     ⏲ {tick}quoted <code>{qt}</code> · {ms:.0f} ms before fire")

    src = fill.get("capital_source", "")
    if src == "angel_one":
        basis = "Angel One margin for these legs"
    elif src.startswith("angel_one"):
        basis = "est. from Angel One margin table"
    elif src == "fallback":
        basis = "estimated margin (broker table not yet calibrated)"
    else:
        basis = "settlement obligation"
    if fill.get("margin_error"):
        basis += f" · margin API: {fill['margin_error'][:60]}"
    settlement = fill.get("settlement_inr")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>Stake</b>          <code>{_inr(fill.get('stake_inr', 0.0))}</code> per trade, fixed",
        f"💰 <b>Invested</b>       <code>{_inr(cap)}</code>"
        + (f"  ({100.0 * cap / fill['stake_inr']:.0f}% of stake)" if fill.get("stake_inr") else ""),
        f"     <i>{basis}</i>"
        + (f"; owes at most {_inr(settlement)} at expiry"
           if settlement and abs(settlement - cap) > 1.0 else ""),
        f"📈 <b>Gross</b>          <code>{_inr(fill['gross_inr'])}</code>  at the quotes",
        f"↔️ <b>Slippage</b>       <code>{_inr(fill.get('realised_slippage_inr', 0.0))}</code>  fills vs quotes",
        f"🧾 <b>Costs</b>          <code>{_inr(fill['cost_inr'])}</code>  statutory + brokerage",
        f"✅ <b>PROFIT</b>         <b>{_inr(fill['net_inr'])}</b>",
        (f"📊 <b>Return</b>         <b>free carry</b> (no capital tied up)"
         if fill.get("free_carry") else
         f"📊 <b>Return</b>         <b>{ret:.2f}%</b> on margin"),
        f"📐 <b>Excess yield</b>   <b>{fill.get('excess_return_pa', 0.0):.2f}% p.a.</b> "
        f"on {_inr(fill.get('contract_value', 0.0))} contract value, "
        f"{fill.get('tenor_days', 0.0):.1f}d tenor",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 <b>TODAY'S P&amp;L</b>    <b>{_inr(pnl)}</b>  "
        f"({fill.get('day_pct_of_stake', 0.0):+.2f}% of stake · {day_pct:+.3f}% of book)",
        f"🏦 <b>Capital</b>        <code>{_inr(total)}</code>",
        f"💼 <b>Equity</b>         <code>{_inr(equity)}</code>",
        f"📌 <b>Deployed</b>       <code>{_inr(deployed)}</code>",
        f"🔢 <b>Trades today</b>   <code>{fill['session_trades']}</code>",
        "",
        f"⚡ Detect→order <code>{fill['latency_ms']['detect_to_order']:.2f} ms</code>",
        f"🕒 Oldest leg <code>{o['max_age_ms']:.0f} ms</code> · "
        f"legs spread over <code>{fill.get('leg_spread_ms', 0):.0f} ms</code>",
    ]
    if fill.get("unwound"):
        lines.append("\n⚠️ <b>Partial fill — unwind attempted, see journal</b>")
    return "\n".join(lines)


def _inr(x):
    """Indian digit grouping: 10,00,000 rather than 1,000,000."""
    neg = x < 0
    whole, frac = divmod(round(abs(x) * 100), 100)
    sd = str(whole)
    if len(sd) > 3:
        head, tail = sd[:-3], sd[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        sd = ",".join(parts) + "," + tail
    return f"{'-' if neg else ''}₹{sd}.{frac:02d}"
