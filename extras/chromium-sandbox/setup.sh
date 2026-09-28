#!/usr/bin/env bash
# Lets Chromium-family browsers launch on Ubuntu 24.04+ (the DGX Spark ships it).
#
#   ./setup.sh              # install the sysctl + apply it now (survives reboot)
#   ./setup.sh --uninstall  # restore Ubuntu's stock restriction
#
# Why: Ubuntu 24.04 sets kernel.apparmor_restrict_unprivileged_userns=1, which
# blocks the unprivileged user namespaces Chromium needs to start its sandbox.
# Symptom on this box: gstack /browse died at launch with
#   FATAL ... No usable sandbox!
# for every Playwright/Chromium process (gstack browse, puppeteer-style tools,
# headless screenshots). The sysctl below is the whole fix; nothing in any
# skill's or app's code can substitute for it, and it must be root-installed.
#
# Trade-off, stated plainly: value=0 loosens an AppArmor confinement on ALL
# unprivileged user namespaces on the machine, not just Chromium's. On a
# single-user personal workstation that is the trade almost everyone makes;
# on a shared or hardened box, weigh it before installing.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/sysctl.d/99-chromium-userns.conf
KEY=kernel.apparmor_restrict_unprivileged_userns
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  sudo rm -f "$CONF"
  sudo sysctl -w "$KEY=1" 2>/dev/null || true
  echo "removed $CONF; restriction restored (Chromium may fail again with 'No usable sandbox!')"
  exit 0
fi
[ -z "${1:-}" ] || die "unknown flag: $1 (only --uninstall is accepted)"

if ! sysctl -n "$KEY" >/dev/null 2>&1; then
  echo "kernel has no $KEY (not Ubuntu 24.04+ with AppArmor) - nothing to fix here"
  exit 0
fi
NOW=$(sysctl -n "$KEY")
echo "current $KEY = $NOW (Ubuntu 24.04 default: 1 = restricted)"
if [ "$NOW" = "0" ] && [ -f "$CONF" ]; then
  echo "already installed and active: $CONF"
  exit 0
fi

sudo install -m 644 "$DIR/99-chromium-userns.conf" "$CONF"
sudo sysctl -p "$CONF" >/dev/null
[ "$(sysctl -n "$KEY")" = "0" ] || die "applied but verify failed: $KEY is still $(sysctl -n "$KEY")"
echo "installed $CONF (boot-persistent)."
echo "Verify: run gstack /browse, e.g. \$B goto https://example.com && \$B screenshot /tmp/verify.png"
