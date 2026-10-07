#!/usr/bin/env bash
# Deploy the arbitrage engine on a VM: one paper session per trading day,
# forever, under nohup. Survives the SSH session ending.
#
#   ./deploy.sh install      create venv, install requirements (first time)
#   ./deploy.sh start        start the daemon under nohup (idempotent)
#   ./deploy.sh stop         stop the daemon and today's session process
#   ./deploy.sh restart
#   ./deploy.sh check        log in to Angel One and build the universe, then exit
#                            (proves .angel.env before the daemon needs it)
#   ./deploy.sh status       daemon / session / dashboard / next session
#   ./deploy.sh logs         follow the daemon log
#   ./deploy.sh session      follow the current session log
#   ./deploy.sh update       git pull, reinstall requirements, restart
#
# Timetable (IST, from config/engine.json session_start/session_end):
#   09:05  log in, download the scrip master, build the universe, subscribe
#   09:15  scanning starts on the opening second
#   15:15  scanning stops; on an expiry day the spot feed is held to 15:30
#          to pin the settlement level, then the dashboard stays up with the
#          day's record until the next morning's relaunch
#
# Paper mode only. Live trading is not started from here.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PY=venv/bin/python
PORT="${PORT:-8088}"
LOGDIR=logs
DAEMON_PID=.daemon.pid
ENGINE_PID=.engine.pid
DLOG="$LOGDIR/daemon.log"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

daemon_pid() {
  if [ -f "$DAEMON_PID" ]; then
    local pid; pid=$(cat "$DAEMON_PID")
    if alive "$pid" && grep -q -- "--daemon" /proc/"$pid"/cmdline 2>/dev/null; then
      echo "$pid"; return 0
    fi
    rm -f "$DAEMON_PID"
  fi
  pgrep -f "^[^ ]*python[0-9.]* run_bot\.py .*--daemon" | head -n1 || true
}
engine_pid() {
  if [ -f "$ENGINE_PID" ]; then
    local pid; pid=$(cat "$ENGINE_PID")
    alive "$pid" && grep -q run_bot /proc/"$pid"/cmdline 2>/dev/null && { echo "$pid"; return 0; }
  fi
  # Any engine process that is not the daemon itself (the daemon's child, or
  # one started by hand with run.sh).
  for p in $(pgrep -f "^[^ ]*python[0-9.]* run_bot\.py" || true); do
    grep -q -- "--daemon" /proc/"$p"/cmdline 2>/dev/null || { echo "$p"; return 0; }
  done
  true
}

need_env() {
  [ -x "$PY" ] || { red "venv missing — run: ./deploy.sh install"; exit 1; }
  [ -f .angel.env ] || { red ".angel.env missing — copy .angel.env.example and fill it in"; exit 1; }
}

do_install() {
  command -v python3 >/dev/null || { red "python3 not found"; exit 1; }
  [ -d venv ] || python3 -m venv venv
  venv/bin/pip install --upgrade pip >/dev/null
  venv/bin/pip install -r requirements.txt
  mkdir -p "$LOGDIR" journal .cache
  green "installed. Next: copy .angel.env.example to .angel.env, fill it in, then ./deploy.sh start"
}

do_start() {
  need_env
  local pid; pid=$(daemon_pid)
  if [ -n "$pid" ]; then dim "daemon already running (pid $pid)"; do_status; return 0; fi
  # A hand-started engine (run.sh) would fight the daemon for the port.
  local e; e=$(engine_pid)
  if [ -n "$e" ]; then dim "stopping hand-started engine (pid $e) first"; kill "$e" || true; sleep 3; fi
  mkdir -p "$LOGDIR"
  # setsid + nohup: a new session, immune to SIGHUP when the terminal goes.
  setsid nohup "$PY" run_bot.py --daemon --mode paper --port "$PORT" "$@" >>"$DLOG" 2>&1 < /dev/null &
  sleep 2
  pid=$(daemon_pid)
  if [ -z "$pid" ]; then red "daemon failed to start — last lines of $DLOG:"; tail -n 20 "$DLOG"; exit 1; fi
  green "daemon running (pid $pid)"
  dim  "log        $DLOG"
  dim  "dashboard  http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT  (during and after each session)"
  dim  "status     ./deploy.sh status   ·  follow  ./deploy.sh logs"
  tail -n 3 "$DLOG" | sed 's/^/  /'
}

do_stop() {
  local pid; pid=$(daemon_pid)
  if [ -n "$pid" ]; then
    kill "$pid"
    # The daemon stops its child gracefully (end-of-day summary, Telegram).
    for _ in $(seq 1 100); do alive "$pid" || break; sleep 1; done
    alive "$pid" && { red "daemon did not exit; killing"; kill -9 "$pid" || true; }
    green "daemon stopped"
  else dim "daemon not running"; fi
  local e; e=$(engine_pid)
  if [ -n "$e" ]; then kill "$e" 2>/dev/null || true; dim "stopped session process (pid $e)"; fi
  rm -f "$DAEMON_PID"
}

do_status() {
  local pid; pid=$(daemon_pid); local e; e=$(engine_pid)
  if [ -n "$pid" ]; then green "daemon    running (pid $pid)"; else red "daemon    not running"; fi
  if [ -n "$e" ]; then green "session   process pid $e"; else dim "session   no process (launches 10 min before the open)"; fi
  PORT="$PORT" "$PY" - <<'PYEOF'
import json, os, urllib.request
from datetime import timedelta
from arb.config import EngineConfig
from arb.instruments import next_session, now_ist
port = os.environ["PORT"]
try:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2))
    print(f"dashboard http://127.0.0.1:{port}  market {d['phase']}  {d['now']} IST  "
          f"feed {'live' if d['connected'] else 'down'}")
    print(f"today     {d['trades_today']} trade(s)  P&L Rs {d['session_pnl']:,.2f}  "
          f"({d['stake_pct']:+.3f}% of stake)")
except Exception:
    print(f"dashboard not serving on :{port}")
c = EngineConfig.load()
o, e = next_session(start=c.session_start, end=c.session_end)
now = now_ist()
if o <= now < e:
    print(f"now       inside today's session, closes {e.strftime('%H:%M')} IST")
else:
    print(f"next      {o.strftime('%a %d %b %H:%M')} IST, prepared from "
          f"{(o - timedelta(minutes=10)).strftime('%H:%M')}")
PYEOF
  # Only this daemon's lines: the log is appended across starts, and an old
  # traceback from a failed first attempt would otherwise sit here forever.
  [ -f "$DLOG" ] && { dim "last daemon lines:";
    awk '/daemon up, pid/{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' "$DLOG" \
      | tail -n 3 | sed 's/^/  /'; } || true
}

do_update() {
  git pull --ff-only
  venv/bin/pip install -r requirements.txt -q
  do_stop; do_start
}

cmd="${1:-start}"; shift || true
for a in "$@"; do [ "$a" = "live" ] && { red "live mode is not deployed from here — paper only"; exit 3; }; done
case "$cmd" in
  install) do_install ;;
  start)   do_start "$@" ;;
  stop)    do_stop ;;
  restart) do_stop; do_start "$@" ;;
  status)  need_env; do_status ;;
  check)   need_env; exec "$PY" run_bot.py --status --no-telegram ;;
  logs)    [ -f "$DLOG" ] || { red "no daemon log yet"; exit 1; }; exec tail -n 40 -f "$DLOG" ;;
  session)
    f=$(ls -t "$LOGDIR"/session_*.log 2>/dev/null | head -n1)
    [ -n "$f" ] || { red "no session logs yet"; exit 1; }
    exec tail -n 40 -f "$f" ;;
  update)  do_update ;;
  *) red "unknown command: $cmd"; sed -n '2,12p' "$0"; exit 2 ;;
esac
