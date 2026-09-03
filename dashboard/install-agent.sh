#!/usr/bin/env bash
# Install the cockpit's Agent tab: opencode's web interface, behind the cockpit
# login. Opt-in, idempotent, never run by install.sh. Two pieces land:
#
#   opencode-web.service      `opencode serve` on 127.0.0.1:OPENCODE_PORT (default
#                             4096), as you, with Basic credentials generated once
#                             into ~/.config/qwen38/opencode-web.env (mode 0600).
#                             Nothing but the relay below ever reaches it.
#   qwen38-dashboard.service  re-installed with the agent relay on
#                             AGENT_BIND:AGENT_PORT (default: the tailnet address,
#                             port 30091). The relay accepts a request only with a
#                             valid cockpit session cookie and adds the credentials.
#                             The cockpit's own bind and port are kept as they are.
#
# The browser has to reach the cockpit and the relay under ONE host name (the
# session cookie is per host, ports do not matter): with the cockpit on 0.0.0.0
# or on the tailnet address, open it through the tailnet address. A cockpit bound
# to 127.0.0.1 only gets a loopback relay, usable on the box itself.
#
# Requires: the cockpit installed (dashboard/install-dashboard.sh) and opencode
# 1.18 or newer on your PATH (https://opencode.ai). Re-run after `opencode
# upgrade` is not needed: the unit points at the opencode command as found on
# your PATH (a symlink stays a symlink), the cockpit's Restart button serves the
# new version. Variables: OPENCODE_PORT, AGENT_PORT, AGENT_BIND (an address, or
# "tailscale"), AGENT_OUTPUT_TOKEN_MAX, AGENT_PATH (the PATH the service gets).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=opencode-web.service
DASH_UNIT_PATH=/etc/systemd/system/qwen38-dashboard.service
ENV_FILE="$HOME/.config/qwen38/opencode-web.env"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
AGENT_PORT="${AGENT_PORT:-30091}"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
note(){ printf 'NOTE: %s\n' "$*"; }

[[ "$OPENCODE_PORT" =~ ^[0-9]+$ ]] || die "OPENCODE_PORT must be a number"
[[ "$AGENT_PORT" =~ ^[0-9]+$ ]] || die "AGENT_PORT must be a number"
[ -f "$DASH_UNIT_PATH" ] || die "install the cockpit first: dashboard/install-dashboard.sh"
command -v python3 >/dev/null || die "python3 required"
command -v curl >/dev/null || die "curl required"

# ── opencode ────────────────────────────────────────────────────────────────
OPENCODE_BIN="${OPENCODE_BIN:-$(command -v opencode || true)}"
[ -n "$OPENCODE_BIN" ] && [ -x "$OPENCODE_BIN" ] || die "opencode not found on your PATH (install it from https://opencode.ai, or set OPENCODE_BIN=/path/to/opencode)"
# (captured, not piped into grep -q: under pipefail an early grep exit turns a
# perfectly good help text into a failure)
SERVE_HELP="$("$OPENCODE_BIN" serve --help 2>&1 || true)"
case "$SERVE_HELP" in *--hostname*) ;; *) die "this opencode has no 'serve --hostname' option; upgrade it (opencode upgrade)" ;; esac
OC_VERSION="$("$OPENCODE_BIN" --version 2>/dev/null || true)"; OC_VERSION="${OC_VERSION##*$'\n'}"

# ── where the cockpit listens decides where the relay may listen ───────────
COCKPIT_BIND="$({ grep -m1 -E '^Environment=COCKPIT_BIND=' "$DASH_UNIT_PATH" || true; } | cut -d= -f3-)"
COCKPIT_PORT="$({ grep -m1 -E '^Environment=COCKPIT_PORT=' "$DASH_UNIT_PATH" || true; } | cut -d= -f3-)"
COCKPIT_BIND="${COCKPIT_BIND:-127.0.0.1}"; COCKPIT_PORT="${COCKPIT_PORT:-30090}"
[ "$AGENT_PORT" != "$COCKPIT_PORT" ] || die "AGENT_PORT $AGENT_PORT is the cockpit's own port"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "${AGENT_BIND:-}" ]; then
  case "$COCKPIT_BIND" in
    127.0.0.1|localhost|::1)
      AGENT_BIND=127.0.0.1
      note "the cockpit listens on loopback only, so the relay does too: the Agent tab works on this box."
      note "to use it from your laptop: DASH_BIND=0.0.0.0 dashboard/install-dashboard.sh (or DASH_BIND=\$(tailscale ip -4)), then re-run this script." ;;
    0.0.0.0|tailscale|"$TS_IP")
      AGENT_BIND=tailscale ;;
    *)
      AGENT_BIND="$COCKPIT_BIND"
      note "the relay binds the cockpit's address $COCKPIT_BIND." ;;
  esac
fi
if [ "$AGENT_BIND" = "tailscale" ] && [ -z "$TS_IP" ]; then
  note "no tailnet address right now; the cockpit binds the relay as soon as tailscale is up."
fi
case "$AGENT_BIND" in *'|'*|*' '*) die "AGENT_BIND must be an address or 'tailscale'" ;; esac

# ── credentials: generated once, never printed ─────────────────────────────
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -s "$ENV_FILE" ]; then
  PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
  ( umask 077; printf 'OPENCODE_SERVER_USERNAME=cockpit\nOPENCODE_SERVER_PASSWORD=%s\n' "$PW" > "$ENV_FILE" )
  echo "generated $ENV_FILE (mode 0600)"
fi
chmod 600 "$ENV_FILE"
grep -qE '^OPENCODE_SERVER_PASSWORD=.+' "$ENV_FILE" || die "$ENV_FILE has no OPENCODE_SERVER_PASSWORD line"
CRED_USER="$({ grep -m1 -E '^OPENCODE_SERVER_USERNAME=' "$ENV_FILE" || true; } | cut -d= -f2-)"; CRED_USER="${CRED_USER:-opencode}"
CRED_PW="$({ grep -m1 -E '^OPENCODE_SERVER_PASSWORD=' "$ENV_FILE" || true; } | cut -d= -f2-)"

# ── the output cap: the oc launcher's, else this repo's 27B default ────────
OUT_MAX="${AGENT_OUTPUT_TOKEN_MAX:-}"
if [ -z "$OUT_MAX" ] && grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  OUT_MAX="$({ grep -m1 -oE 'OUTPUT_TOKEN_MAX[^0-9]*[0-9]{4,7}' "$HOME/.local/bin/oc" || true; } | grep -oE '[0-9]+$' || true)"
fi
OUT_MAX="${OUT_MAX:-160000}"
[[ "$OUT_MAX" =~ ^[0-9]+$ ]] || die "AGENT_OUTPUT_TOKEN_MAX must be a number"

# ── PATH for the service: yours, deduplicated, the opencode dir first ──────
# (AGENT_PATH= overrides it, for an install run from a wrapper or an odd shell)
SVC_PATH="${AGENT_PATH:-$(printf '%s:%s' "$(dirname "$OPENCODE_BIN")" "$PATH" | tr ':' '\n' | awk 'NF && !seen[$0]++' | paste -sd: -)}"
case "$SVC_PATH" in *'|'*|*' '*) die "PATH contains a space or a | character; set PATH to something plain and re-run" ;; esac

# ── render and install the unit ────────────────────────────────────────────
TMP_UNIT="$(mktemp)"
sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn)|g" -e "s|__HOME__|$HOME|g" \
    -e "s|__OPENCODE_BIN__|$OPENCODE_BIN|g" -e "s|__OPENCODE_PORT__|$OPENCODE_PORT|g" \
    -e "s|__PATH__|$SVC_PATH|g" -e "s|__OUTPUT_TOKEN_MAX__|$OUT_MAX|g" \
    "$HERE/opencode-web.service.template" > "$TMP_UNIT"
grep -q '__[A-Z_]*__' "$TMP_UNIT" && die "unsubstituted placeholder in the unit render"
sudo install -m 644 "$TMP_UNIT" "/etc/systemd/system/$UNIT"; rm -f "$TMP_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
sudo systemctl try-restart "$UNIT"      # a re-run with a new port, cap or binary must take effect

# ── the server answers with the credentials (never on the command line) ────
health(){
  printf 'user = "%s:%s"\nsilent\nmax-time = 3\nurl = "http://127.0.0.1:%s/global/health"\n' \
    "$CRED_USER" "$CRED_PW" "$OPENCODE_PORT" | curl -K - 2>/dev/null || true
}
HEALTHY=""
for _ in $(seq 1 30); do
  case "$(health)" in *'"healthy":true'*) HEALTHY=1; break ;; esac
  sleep 1
done
[ -n "$HEALTHY" ] || die "opencode did not answer on 127.0.0.1:$OPENCODE_PORT within 30 s (journalctl -u $UNIT -n 40)"
echo "opencode-web.service: opencode ${OC_VERSION:-?} serving on 127.0.0.1:$OPENCODE_PORT (loopback only)"

# ── the cockpit gets the relay; its own bind and port are kept ─────────────
DASH_AGENT_PORT="$AGENT_PORT" DASH_AGENT_BIND="$AGENT_BIND" \
  DASH_AGENT_UPSTREAM="http://127.0.0.1:$OPENCODE_PORT" "$HERE/install-dashboard.sh"

# ── the relay refuses a request without a session (that is the proof it is up)
RELAY_HOST="$AGENT_BIND"; [ "$RELAY_HOST" = "tailscale" ] && RELAY_HOST="$TS_IP"
if [ -n "$RELAY_HOST" ]; then
  code=""
  for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://$RELAY_HOST:$AGENT_PORT/global/health" || true)"
    [ "$code" = "401" ] && break
    sleep 1
  done
  [ "$code" = "401" ] || die "the relay did not come up on $RELAY_HOST:$AGENT_PORT (got '$code'; journalctl -u qwen38-dashboard -n 30)"
  echo "agent relay: http://$RELAY_HOST:$AGENT_PORT refuses without a cockpit session (401), as it should"
  echo
  echo "Agent tab: http://$RELAY_HOST:$COCKPIT_PORT/#agent   (log in with the API key; the tab needs no second login)"
else
  echo "agent relay: binds the tailnet address when tailscale is up; then open http://<tailnet address>:$COCKPIT_PORT/#agent"
fi
echo "Remove with: sudo systemctl disable --now $UNIT; sudo rm -f /etc/systemd/system/$UNIT; DASH_AGENT_PORT=0 dashboard/install-dashboard.sh"
