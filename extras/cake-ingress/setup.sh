#!/usr/bin/env bash
# Installs the cake-ingress anti-bufferbloat service (opt-in extra, boot-persistent).
#
#   BANDWIDTH=950Mbit ./setup.sh                 # ~95 % of your MEASURED downlink
#   IFACE=enP7s7 BANDWIDTH=2350Mbit ./setup.sh   # explicit interface
#   ./setup.sh --uninstall                       # remove service + script, restore defaults
#
# BANDWIDTH is deliberately required: it must be ~95 % of the downlink you
# MEASURE (speedtest), never the NIC speed. Too high is the one mistake that
# silently does nothing (the queue stays in the ISP box); a bit too low only
# costs a few percent of throughput and improves latency further.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  sudo systemctl disable --now cake-ingress.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/cake-ingress.service /usr/local/sbin/cake-ingress
  sudo systemctl daemon-reload
  echo "cake-ingress removed; the interface is back on its default qdisc"
  exit 0
fi
[ -z "${1:-}" ] || die "unknown flag: $1 (only --uninstall is accepted; configuration goes through BANDWIDTH= and IFACE=)"

IFACE="${IFACE:-$(ip route show default 2>/dev/null | awk '/^default/ {print $5; exit}')}"
[ -n "$IFACE" ] || die "no default-route interface found; pass IFACE=<iface>"
[ -e "/sys/class/net/$IFACE" ] || die "interface $IFACE does not exist (see: ip -br link)"
BANDWIDTH="${BANDWIDTH:-}"
if [ -z "$BANDWIDTH" ]; then
  die "BANDWIDTH is required: ~95 % of your MEASURED downlink (speedtest), not the NIC speed.
  examples: 500 Mb/s link -> BANDWIDTH=475Mbit ; 1 Gb/s -> BANDWIDTH=950Mbit ; 2.5 Gb/s -> BANDWIDTH=2350Mbit"
fi
echo "$BANDWIDTH" | grep -qEi '^[0-9]+(\.[0-9]+)?(k|m|g)bit$' \
  || die "BANDWIDTH must look like 950Mbit, 2350Mbit or 2.35Gbit (got: $BANDWIDTH)"

sudo install -m 755 "$DIR/cake-ingress" /usr/local/sbin/cake-ingress
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
sed -e "s|__IFACE__|$IFACE|g" -e "s|__BANDWIDTH__|$BANDWIDTH|g" \
    "$DIR/cake-ingress.service.template" > "$TMP"
sudo install -m 644 "$TMP" /etc/systemd/system/cake-ingress.service
sudo systemctl daemon-reload
sudo systemctl enable --now cake-ingress.service
sudo /usr/local/sbin/cake-ingress status || die "installed, but the status check failed (see above)"
echo
echo "cake-ingress active: $IFACE shaped at $BANDWIDTH."
echo "Verify it works: keep a 'ping 1.1.1.1' running while a big download saturates"
echo "the link; latency should stay in the tens of milliseconds instead of seconds."
echo "Tune later by re-running with another BANDWIDTH=; remove with ./setup.sh --uninstall"
