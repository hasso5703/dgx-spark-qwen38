---
name: Bug report
about: Something this stack did that it should not have done
labels: ["bug"]
---

<!--
Anything that breaks a security boundary or leaks the key goes to the
private channel in SECURITY.md first, not here.
For everything else: a report this repo can act on names the version,
reproduces, and shows the bytes. "It stopped working" is not a repro; the
unit's journal usually is, pasted in full with the key masked (SGLang
prints its server_args, api_key included, at every engine start):
  journalctl -u <unit> --since ... --until ... | sed -E "s/(api_key(': |=)')[^']*/\1<masked>/g"
For a CUDA error, the kernel's lines from the same minutes too: an Xid is
logged there, not in the unit's journal.
  sudo journalctl -k --since ... --until ...
-->

## Version
<!-- git rev-parse HEAD or the release tag; the proxy prints its version in
the journal banner, the cockpit shows its drift fingerprint. -->

## What happened, and what should have happened instead

## Reproduction
```
<!-- smallest command that reproduces; for proxy behavior the raw curl with
the body shape matters more than the client's UI message. -->
```

## Evidence
```
<!-- journalctl / docker logs excerpt with the key masked as above (the
same sed masks docker logs), the kernel's lines for a CUDA error,
/tmp/pins.txt for pin failures, the cockpit diagnostic bundle for lane
mysteries (it masks the key). -->
```

## Your configuration
<!-- lane, target, context mode, tier, ports, anything hand-tuned in the
unit; the cockpit drift panel says what differs from the repo's recipe. -->
