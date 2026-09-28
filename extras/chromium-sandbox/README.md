# Chromium won't start on Ubuntu 24.04 ("No usable sandbox!")

One root-owned sysctl, installed once, persistent across reboots and across every
`gstack-upgrade`. This is the fix that took the reference box from "browse is dead on
the Spark" to the screenshots you see in the docs.

## The symptom

Any Playwright/Chromium launch dies immediately, e.g. gstack `/browse`:

```
FATAL:playwright ... No usable sandbox!
```

Chromium builds its sandbox inside an unprivileged user namespace. Ubuntu 24.04
(what the DGX Spark ships) sets `kernel.apparmor_restrict_unprivileged_userns=1`,
which refuses exactly those namespaces to unprivileged processes. The browser never
starts, so the error points at the browser and at any skill or tool that drives it.
It is a kernel-posture choice, not an ARM problem and not an aarch64 build problem:
the same Ubuntu 24.04 on x86 fails the same way.

## Why no app-level fix counts

It is tempting to patch the launcher to pass `--no-sandbox` (gstack issue #2671
circulates exactly such a patch). On the reference box we wrote that patch, rebuilt
the bundle, and the crashes continued: a patch that is not compiled into the
executed artifact protects nothing, and `--no-sandbox` throws away the sandbox for
every page the tool visits. One sysctl keeps the sandbox and fixes every Chromium
on the box at once (gstack browse, puppeteer-style tooling, headless renderers).

## Install

```bash
extras/chromium-sandbox/setup.sh              # needs sudo; applies now, persists on reboot
extras/chromium-sandbox/setup.sh --uninstall  # back to Ubuntu's stock restriction
```

What it writes: `/etc/sysctl.d/99-chromium-userns.conf` containing
`kernel.apparmor_restrict_unprivileged_userns=0`.

Verify:

```bash
sysctl kernel.apparmor_restrict_unprivileged_userns   # must print 0
# then, with gstack installed:
~/.config/opencode/skills/gstack/browse/dist/browse goto https://example.com
~/.config/opencode/skills/gstack/browse/dist/browse screenshot /tmp/verify.png
```

## The trade-off, stated plainly

Value `0` loosens that AppArmor restriction for **all** unprivileged user namespaces
on the machine, not just Chromium's. AppArmor still confines everything else it is
configured to confine. On a single-user personal workstation this is the trade the
distro's own docs expect you to weigh; on a shared or hardened machine, don't take
it on our word. Uninstall restores the stock value.
