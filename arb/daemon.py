"""Run one trading session per trading day, forever.

This is what ``deploy.sh`` keeps alive under nohup on the VM. It never touches
the market itself: ten minutes before each open it launches ``run_bot.py
--wait-open --hold`` as a child, which logs in, builds the day's universe,
subscribes, and starts scanning on the opening second (09:15:00 IST). The
child stops scanning at the close (15:15), pins settlement levels on expiry
days, and then keeps the dashboard serving the day's record until this
supervisor replaces it before the next open.

One child per day, always fresh: Angel One's session tokens, the scrip master
and the strike window centred on spot all belong to a single day, and a
process that has run for ten hours is not one to trust with the next ten.

If the child dies inside a session it is relaunched with back-off (30s,
60s, ... 5 min; at most a dozen times a day) - the login retry inside the
child already rides out a network outage, so a relaunch here means something
went wrong in the code, and Telegram is told.
"""
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

from arb.config import Credentials, EngineConfig
from arb.instruments import next_session, now_ist

PIDFILE = ".engine.pid"           # the child's, so run.sh stop still works
MAX_RESTARTS_PER_DAY = 12
LOGDIR = "logs"


def _log(msg):
    print(f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class _Telegram:
    def __init__(self, enabled):
        self.n = None
        if not enabled:
            return
        try:
            from arb.telegram import TelegramNotifier
            c = Credentials.load()
            if c.telegram_bot_token and c.telegram_chat_id:
                self.n = TelegramNotifier(c.telegram_bot_token, c.telegram_chat_id).start()
        except Exception as e:
            _log(f"telegram unavailable to the daemon: {e}")

    def send(self, text):
        if self.n:
            try:
                self.n.send(text)
            except Exception:
                pass

    def stop(self):
        if self.n:
            try:
                self.n.flush()
                self.n.stop()
            except Exception:
                pass


def _child_cmd(args):
    cmd = [sys.executable, "run_bot.py", "--mode", "paper", "--port", str(args.port),
           "--wait-open", "--hold", "--lots", str(args.lots),
           "--min-edge", str(args.min_edge), "--candle-interval", args.candle_interval]
    if args.no_telegram:
        cmd.append("--no-telegram")
    if args.no_dashboard:
        cmd.append("--no-dashboard")
    return cmd


def _spawn(args):
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, f"session_{now_ist().strftime('%Y%m%d_%H%M%S')}.log")
    fh = open(path, "ab", buffering=0)
    p = subprocess.Popen(_child_cmd(args), stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    _log(f"launched session process pid {p.pid} -> {path}")
    return p, path


def _terminate(p, why, grace=90.0):
    """SIGTERM and wait: the child writes its end-of-day summary, flushes
    Telegram and pins settlement before it exits, so give it time."""
    if p is None or p.poll() is not None:
        return
    _log(f"stopping session process pid {p.pid} ({why})")
    try:
        p.terminate()
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _log(f"pid {p.pid} did not exit in {grace:.0f}s; killing")
        p.kill()
        try:
            p.wait(timeout=10)
        except Exception:
            pass
    except Exception:
        pass


def _sleep_until(when, stop, child=None):
    """Sleep in one-second ticks until ``when`` or a stop request. Notes a
    child that dies in the meantime but does not relaunch it - outside a
    session the child only serves the dashboard."""
    noted = False
    while not stop["v"] and now_ist() < when:
        if child is not None and not noted and child.poll() is not None:
            _log(f"dashboard process exited (code {child.returncode}) outside the session; "
                 f"it will be relaunched before the open")
            noted = True
        time.sleep(1.0)


def run_daemon(args):
    if args.mode == "live":
        raise SystemExit("the daemon runs paper mode only; live is a deliberate hand start")
    # Fail now, at deploy time, not at 09:05 tomorrow: a half-filled .angel.env
    # would otherwise be discovered by a session that crash-loops at the open.
    missing = Credentials.load().missing()
    if missing:
        raise SystemExit(f"cannot start: missing credentials in .angel.env: {missing}")
    cfg = EngineConfig.load()
    tg = _Telegram(not args.no_telegram)
    stop = {"v": False}

    def on_signal(signum, *_):
        stop["v"] = True
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    with open(".daemon.pid", "w") as f:
        f.write(str(os.getpid()))
    _log(f"daemon up, pid {os.getpid()}: one paper session per trading day, "
         f"{cfg.session_start}-{cfg.session_end} IST, prepared {args.prep_minutes:g} min early")
    tg.send(f"🟦 <b>DAEMON UP</b>\n\nOne paper session per trading day, "
            f"<code>{cfg.session_start}</code>–<code>{cfg.session_end}</code> IST, "
            f"prepared {args.prep_minutes:g} min before the open.")

    child = None
    try:
        while not stop["v"]:
            open_dt, close_dt = next_session(start=cfg.session_start, end=cfg.session_end)
            prep_dt = open_dt - timedelta(minutes=args.prep_minutes)
            now = now_ist()
            if now < prep_dt:
                gap = prep_dt - now
                _log(f"next session {open_dt.strftime('%a %d %b %H:%M')} IST; launching at "
                     f"{prep_dt.strftime('%H:%M')} ({gap.total_seconds() / 3600:.1f}h from now)"
                     + ("; last session's dashboard stays up meanwhile"
                        if child is not None and child.poll() is None else
                        "; the dashboard comes up then"))
                _sleep_until(prep_dt, stop, child)
                if stop["v"]:
                    break

            # Prep window or mid-session: a fresh process for this session.
            _terminate(child, "new session")
            restarts = 0
            child, path = _spawn(args)
            while not stop["v"]:
                time.sleep(1.0)
                if child.poll() is None:
                    if now_ist() >= close_dt:
                        # Session done; the child stays up serving the day's
                        # record until it is replaced before the next open.
                        _log(f"session closed at {close_dt.strftime('%H:%M')}; "
                             f"dashboard held by pid {child.pid}")
                        break
                    continue
                code = child.returncode
                if now_ist() >= close_dt:
                    _log(f"session process exited (code {code}) after the close")
                    break
                restarts += 1
                if restarts > MAX_RESTARTS_PER_DAY:
                    _log(f"session process died {restarts - 1} times today; giving up on "
                         f"{open_dt.strftime('%d %b')}, will try again next session")
                    tg.send(f"🔴 <b>SESSION ABANDONED</b>\n\nThe session process died "
                            f"{restarts - 1} times today. See <code>{path}</code>. "
                            f"Will try again next trading day.")
                    break
                back = min(300.0, 30.0 * (2 ** (restarts - 1)))
                _log(f"session process died (code {code}) inside the session; "
                     f"relaunch #{restarts} in {back:.0f}s")
                tg.send(f"🟠 <b>SESSION RELAUNCH #{restarts}</b>\n\nProcess exited with code "
                        f"<code>{code}</code> at {now_ist().strftime('%H:%M:%S')}; "
                        f"relaunching in {back:.0f}s. Log: <code>{path}</code>")
                end = time.time() + back
                while not stop["v"] and time.time() < end:
                    time.sleep(1.0)
                if stop["v"]:
                    break
                child, path = _spawn(args)
            # Whether the session ended or was abandoned, wait for the next one;
            # a living child keeps the dashboard up until then.
            if not stop["v"] and now_ist() < close_dt:
                _sleep_until(close_dt, stop, child)
    finally:
        _terminate(child, "daemon stopping")
        _log("daemon stopped")
        tg.send("⬛ <b>DAEMON STOPPED</b>")
        tg.stop()
        for f in (".daemon.pid",):
            try:
                os.remove(f)
            except OSError:
                pass
    return 0
