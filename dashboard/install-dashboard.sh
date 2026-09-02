#!/usr/bin/env bash
# Install Spark Cockpit as a systemd service. Opt-in: install.sh never runs this.
# Idempotent. Installs: qwen38-dashboard.service (127.0.0.1:__PORT__) and the
# narrow sudoers allowlist for the unit start/stop/restart buttons.
# The sudoers file is validated with visudo -c BEFORE it lands, from a temp
# path, so a broken render can never brick sudo.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$HERE")"
PORT="${DASH_PORT:-30090}"
UNIT=qwen38-dashboard.service
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 required"
python3 - "$HERE/cockpit.py" <<'EOF' || die "cockpit.py does not parse"
import ast, sys
ast.parse(open(sys.argv[1]).read())
EOF

TMP_UNIT="$(mktemp)"
sed -e "s|__PORT__|$PORT|g" -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$HERE/qwen38-dashboard.service.template" > "$TMP_UNIT"
grep -q '__[A-Z_]*__' "$TMP_UNIT" && die "unsubstituted placeholder in unit"
sudo install -m 644 "$TMP_UNIT" "/etc/systemd/system/$UNIT"; rm -f "$TMP_UNIT"

TMP_SUDO="$(mktemp)"
# read-only forensics wrapper (scheduler stack dump), referenced by the sudoers line below
sudo install -m 755 "$HERE/pyspy-scheduler.sh" /usr/local/bin/qwen38-pyspy-scheduler
sed -e "s|__USER__|$(id -un)|g" "$HERE/sudoers-cockpit.template" > "$TMP_SUDO"
sudo visudo -c -f "$TMP_SUDO" >/dev/null || die "sudoers render failed visudo check, NOT installed"
sudo install -m 440 "$TMP_SUDO" /etc/sudoers.d/qwen38-cockpit; rm -f "$TMP_SUDO"

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
for _ in $(seq 1 15); do
  curl -s -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -s -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 \
  || die "cockpit did not come up (journalctl -u $UNIT -n 30)"
echo "Spark Cockpit: http://127.0.0.1:$PORT (login = the API key)"
echo "Remove with: sudo systemctl disable --now $UNIT; sudo rm -f /etc/systemd/system/$UNIT /etc/sudoers.d/qwen38-cockpit /usr/local/bin/qwen38-pyspy-scheduler"
