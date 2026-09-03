#!/usr/bin/env bash
# Install Spark Cockpit as a systemd service. Opt-in: install.sh never runs this.
# Idempotent. Installs: qwen38-dashboard.service (DASH_BIND:DASH_PORT) and the
# narrow sudoers allowlist for the unit start/stop/restart buttons.
# DASH_BIND defaults to 127.0.0.1 on a first install. Set it to reach the cockpit
# from another machine, e.g. DASH_BIND=0.0.0.0 (every interface) or
# DASH_BIND=<tailscale ip> (that interface only). The API key is the only gate, so
# keep it on a private network: a Tailscale tailnet or a LAN you trust, never the
# open internet.
# A re-run keeps what the installed unit says (bind, port, agent relay) unless the
# variable is set again: an upgrade must never flip a cockpit back to loopback.
# The Agent tab's relay (DASH_AGENT_PORT, DASH_AGENT_BIND, DASH_AGENT_UPSTREAM) is
# normally set by dashboard/install-agent.sh, which calls this script.
# The sudoers file is validated with visudo -c BEFORE it lands, from a temp
# path, so a broken render can never brick sudo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$HERE")"
UNIT=qwen38-dashboard.service
INSTALLED="/etc/systemd/system/$UNIT"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# What the installed unit says, so a re-run without variables changes nothing.
installed(){ { grep -m1 -E "^Environment=$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f3-; }
PORT="${DASH_PORT:-$(installed COCKPIT_PORT)}"; PORT="${PORT:-30090}"
BIND="${DASH_BIND:-$(installed COCKPIT_BIND)}"; BIND="${BIND:-127.0.0.1}"
AGENT_PORT="${DASH_AGENT_PORT:-$(installed COCKPIT_AGENT_PORT)}"; AGENT_PORT="${AGENT_PORT:-0}"
AGENT_BIND="${DASH_AGENT_BIND:-$(installed COCKPIT_AGENT_BIND)}"; AGENT_BIND="${AGENT_BIND:-tailscale}"
AGENT_UPSTREAM="${DASH_AGENT_UPSTREAM:-$(installed COCKPIT_AGENT_UPSTREAM)}"; AGENT_UPSTREAM="${AGENT_UPSTREAM:-http://127.0.0.1:4096}"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "DASH_PORT must be a number (got '$PORT')"
[[ "$AGENT_PORT" =~ ^[0-9]+$ ]] || die "DASH_AGENT_PORT must be a number (got '$AGENT_PORT')"
[ "$AGENT_PORT" != "$PORT" ] || die "the agent relay cannot share the cockpit port $PORT"
case "$BIND$AGENT_BIND$AGENT_UPSTREAM" in *'|'*|*' '*) die "bind and upstream values must not contain spaces or |" ;; esac

# The health probe below needs an address to dial, and 0.0.0.0 is not one.
case "$BIND" in
  0.0.0.0) PROBE=127.0.0.1 ;;
  ::|'[::]') PROBE='[::1]' ;;
  *) PROBE="$BIND" ;;
esac

command -v python3 >/dev/null || die "python3 required"
for f in cockpit.py agent_relay.py; do
  python3 - "$HERE/$f" <<'EOF' || die "$f does not parse"
import ast, sys
ast.parse(open(sys.argv[1]).read())
EOF
done

TMP_UNIT="$(mktemp)"
sed -e "s|__PORT__|$PORT|g" -e "s|__BIND__|$BIND|g" -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__AGENT_PORT__|$AGENT_PORT|g" -e "s|__AGENT_BIND__|$AGENT_BIND|g" \
    -e "s|__AGENT_UPSTREAM__|$AGENT_UPSTREAM|g" \
    "$HERE/qwen38-dashboard.service.template" > "$TMP_UNIT"
grep -q '__[A-Z_]*__' "$TMP_UNIT" && die "unsubstituted placeholder in unit"
sudo install -m 644 "$TMP_UNIT" "$INSTALLED"; rm -f "$TMP_UNIT"

TMP_SUDO="$(mktemp)"
# read-only forensics wrapper (scheduler stack dump), referenced by the sudoers line below
sudo install -m 755 "$HERE/pyspy-scheduler.sh" /usr/local/bin/qwen38-pyspy-scheduler
sed -e "s|__USER__|$(id -un)|g" "$HERE/sudoers-cockpit.template" > "$TMP_SUDO"
sudo visudo -c -f "$TMP_SUDO" >/dev/null || die "sudoers render failed visudo check, NOT installed"
sudo install -m 440 "$TMP_SUDO" /etc/sudoers.d/qwen38-cockpit; rm -f "$TMP_SUDO"

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
# enable --now leaves an already running unit alone, so a re-run with a changed
# port or bind would report success while the old process kept the old socket.
sudo systemctl try-restart "$UNIT"
for _ in $(seq 1 15); do
  curl -s -m 2 "http://$PROBE:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -s -m 2 "http://$PROBE:$PORT/api/health" >/dev/null 2>&1 \
  || die "cockpit did not come up (journalctl -u $UNIT -n 30)"
echo "Spark Cockpit: http://$PROBE:$PORT (login = the API key)"
if [ "$BIND" != "127.0.0.1" ] && [ "$BIND" != "localhost" ] && [ "$BIND" != "::1" ]; then
  echo "Bound to $BIND: reachable from other machines. The API key is the only gate."
  [ -s "$HOME/.config/qwen38/api-key" ] \
    || echo "WARNING: $HOME/.config/qwen38/api-key is missing or empty, so nobody can log in."
fi
if [ "$AGENT_PORT" != "0" ]; then
  echo "Agent relay: $AGENT_BIND:$AGENT_PORT -> $AGENT_UPSTREAM (Agent tab; a cockpit session is required)"
fi
REMOVE="sudo systemctl disable --now $UNIT; sudo rm -f $INSTALLED /etc/sudoers.d/qwen38-cockpit /usr/local/bin/qwen38-pyspy-scheduler"
[ -f /etc/systemd/system/opencode-web.service ] && REMOVE="$REMOVE; sudo systemctl disable --now opencode-web.service; sudo rm -f /etc/systemd/system/opencode-web.service"
echo "Remove with: $REMOVE"
