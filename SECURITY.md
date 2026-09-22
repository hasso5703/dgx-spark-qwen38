# Security policy

What this box trusts, what it does not, and how to report a hole in that line.

## What this project is, security-wise

A serving stack one operator installs on a machine they own and administer:
a Docker engine behind a Python proxy, a cockpit web UI that reaches systemd
through an exact-argv sudo allowlist, and an opt-in relay that puts opencode's
web interface behind the cockpit login. There is no telemetry and no
phone-home: every byte that leaves the box is a download the installer names
(Hugging Face, the image registry) or a request you sent. One exception, added in
v1.15.2 and named here because it is one: the cockpit asks GitHub's public releases
endpoint, at most once every six hours, whether a newer release exists, so that a box
running an old version is told rather than left to find out. It sends nothing but the
cockpit's version in a `User-Agent`, it reads a public URL that needs no credential, and
`COCKPIT_UPDATE_CHECK=0` turns it off, after which the cockpit says nothing about
releases at all. A check that fails backs off instead of retrying. That includes the typed
decisions route (`POST /v1/systemone`, v6.19): it speaks the wire contract of a hosted
service, and it is answered by the engine on this box and nothing else; the proxy
calls no address but its upstream.

Two things the typed-decisions route is careful about, both found by a review of it on
2026-09-19 and both fixed before it shipped. The proxy decodes the request path once,
before any check reads it, because the engine routes on the decoded path: matching the raw
string let `/%76%31/chat/completions` walk past the per-client identity wall and past the
oversize guard with the engine's own key attached. And the `state` of a typed decision,
which is third-party text by design, is fenced with a token drawn at start-up: without it
a state could write the framing of the question itself and be answered instead of judged.

## Reporting

Report privately, not as a public issue (a public issue is a disclosure with
no fix window). Email `basbunarhasan@gmail.com` with as much of this as you
can: the version (`git rev-parse HEAD` or the release tag), the component
(proxy, cockpit, relay, installer, sudoers surface), what you sent, what came
back, and what you believe the boundary break is. Expected first response:
7 days, with patches landing as ordinary commits once the fix is public.
GitHub's private vulnerability reporting tab is an acceptable channel once
enabled for the repository.

## The trust boundaries, as they actually exist

**The keepalive proxy (`keepalive-proxy.py`, :30001)** is the only component
that reads bytes it does not control: every client request. Its guards are
its contract: bodies over `MAX_BODY_BYTES` (256 MiB) get 413 before a byte is
read, a lying `Content-Length` cannot decide an allocation, tool schemas are
sanitized at bounded depth and only for patterns Python cannot compile,
image parts are priced from their own headers, and the corruption tripwire
aborts a decode that emits runs of token id 0. Fuzzed and state-machine
tested (see TESTING.md); a crash you can reproduce in the proxy is a bug
worth a private report.

Several of those guards exist because the engine behind it does not survive
the request, and a denial of service that costs one HTTP call is worth naming
as such. A prompt past the KV pool wedges the scheduler rather than being
refused (sglang#36333). A logprob request past the vocabulary raises
`selected index k out of range` inside the scheduler and the server is gone
for every client until it is restarted, about nine minutes here
(sglang#40076, open). A `stop_token_ids` or `input_ids` entry past the
vocabulary indexes a `scatter_add_` or the embedding out of bounds, which on
CUDA is a device-side assert with the same blast radius, and `n` expands a
list before scheduling with no bound at all (sglang#31597, whose two fixes
were closed without being merged). Every one of those fields is declared
unconstrained in the served release, checked in the image, and all of them
are reachable from an ordinary chat request.

The proxy refuses all of them with a 400. **That protects the clients that go
through it, and nothing else.** On this box the engine binds `0.0.0.0:30000`
with no firewall in front of it, so anything on the LAN or the tailnet that
holds the serving API key can reach the engine directly and end it, exactly
as it could before these guards existed. The key is the boundary there, not
the proxy. **Since v1.17 the engine binds `127.0.0.1` by default**, so on a fresh
install the proxy's guards are the only door and the engine is not on the
network at all. Nothing is lost by it: the proxy is a full pass-through of
every route the engine serves, so a client that used `:30000` from another
machine uses `:30001` and gets the guards with it. `ENGINE_BIND=0.0.0.0`
restores the old behaviour, `PROXY_BIND` does the same for the proxy, and an
installed choice is kept across updates in both directions, so neither a
hardened box nor one that deliberately exposed its engine is changed by an
update nobody read about.

**The cockpit (`dashboard/`, :30090)** reaches root through exactly one
surface: the NOPASSWD sudoers lines rendered from
`dashboard/sudoers-cockpit.template`. The allowlist is exact argv with no
wildcards on purpose (a glob on `sed` arguments is still root), and two CI
steps hold it: no `*` on any allowlist line, and every privileged call in
`switch-model.sh` cross-checked against the template. The cockpit process
runs unprivileged; login is the serving API key; a per-session HMAC cookie
gates everything else; the diagnostic bundle masks the API key in every
member, and a test builds a real tarball and greps it rather than trusting
the masking code.

**The agent relay (`dashboard/agent_relay.py`, :30091 by default)** is the
only thing that reaches `opencode serve`: it requires a valid cockpit session
cookie, refuses foreign origins, strips caller headers the relay owns, and
forwards over HTTP with Basic credentials that never leave the machine
(opencode binds 127.0.0.1). Its boundary properties (origin gate, cookie
names, header hygiene) are property-tested.

**The installer (`install.sh`, `dashboard/install-*.sh`)** runs things you
sudo. Every pinned byte is hash-verified: images by digest, checkpoints by
pinned revision, vendored overlay files by sha256 manifest (a modified
overlay refuses to build). The engine enforces `--api-key` (the file is
created 0600) and the chat template and launcher are regenerated from the
repo on every install.

## Assumptions that are design choices, not vulnerabilities

Read these before reporting; a report against one of them is a design
discussion, not a disclosure.

- **Plain HTTP.** The proxy, cockpit and relay serve HTTP because they are
  meant for loopback or a tailnet, and the cockpit warns when a non-loopback
  bind has no key. TLS in front is the operator's reverse proxy. If you need
  to expose any of it to a LAN you do not control, that is the design's
  stated edge, not a hole.
- **One API key, one trust realm.** Every client that holds the key is the
  same principal: same quota, same ceiling, same lane. Per-client identity
  does not exist yet.
- **`AGENT_AUTO=1` is deliberate escalation.** It sets opencode to allow
  every tool call in the Agent tab, behind the cockpit login, on this
  machine, by the operator's own choice. The explicit deny rules of your
  own `opencode.json` still apply.
- **The abliterated targets are a policy choice.** `uncensored`,
  `uncensored-fp8` and `flash-uncensored` exist as pinned checkpoints
  because the operator asked for them; nothing in the serving stack filters
  or vets what they answer.
- **`/metrics` is open on the engine port.** Since the Prometheus flag ships on
  every lane, request rates and queue depths are readable by anything that can
  reach the engine port. That is the port's existing trust model (a trusted
  network by design, see the plain HTTP edge above); the metrics endpoint adds
  counters to it, not a new surface to authenticate against.
- **The cockpit assumes one admin user.** The sudo allowlist covers this
  repo's argv, but a hostile local user with your shell can do what you can
  do: this box is yours, and it is not a multi-tenant host.

## Out of scope

SGLang and the serving images themselves (report upstream, tell us too if it
affects a pinned digest), model behavior and prompt injection into agents
(opencode's permission model is the boundary), and anything reachable only
with root or physical access.
