# When an engine stops: what the box does, measured

An engine that dies is started again by systemd, and a boot takes minutes: about 7.5 on the
27B lane, 11 on the flash lane. This page says what happens in that time, to the box and to
the clients, and what v1.21.0 added so that a restart can neither freeze the machine nor end
an agent's session. Every number here was measured on the reference box (DGX Spark, driver
580.178.04, kernel 6.17.0-1032) on 2026-10-01, for [issue #26](https://github.com/hasso5703/dgx-spark-qwen38/issues/26).

## The timeline of a crash

An emulated crash of the 27B 1M lane, the sequence of the issue (SIGQUIT to the server, its
scheduler dead within the second):

| time | what |
|---|---|
| 0 s | the scheduler dies; SGLang's server gets SIGQUIT |
| 2 to 5 s | the 92 GB the engine held are back (MemFree 2 GiB, then 98 GiB) |
| 5 s | SGLang kills its process tree; the request in flight gets its answer: a `500 Internal Server Error` before any token, a stream cut short after some |
| 6 s | the container is gone, the unit "Deactivated successfully" |
| 21 s | systemd starts the unit again (`RestartSec=15`) |
| about 7.5 min later | "The server is fired up and ready to roll!" |

The memory is back about 15 s before the next start: on a healthy driver the restart is in
no window at all. [sglang#40948](https://github.com/sgl-project/sglang/issues/40948) froze a
DGX Spark for 9.5 h in another case: the driver kept the dead engine's memory (still held five
minutes later), the next start piled its allocations on top of it, and the driver's lock
seized. A longer delay does not help there; not starting does, which is what the guard below
does.

## The start guard (`engine-preflight.sh`)

Every engine unit (27B, flash, image, video) runs it before its engine. It waits up to 60 s
and refuses the start while it finds:

- **GPU memory still held by what is left of an engine**: an SGLang process (its title is
  `sglang::<part>` or its command line names sglang), or a process the driver still lists and
  the system no longer has, holding more than 2 GiB;
- **memory the driver keeps that no process accounts for**: on this unified-memory machine a
  GPU allocation is in none of the kernel's own categories, so MemTotal minus all of them is
  what the driver holds. Measured: 1.1 GiB with no engine and a desktop session, the engine's
  size plus 1 to 3 GiB with one. The guard refuses past 16 GiB;
- **a driver that does not answer `nvidia-smi`** (each call cut off after 10 s).

Nothing else blocks a start, another program's GPU memory included: the journal names it,
and the engine sizes its pool to what it finds, as it always did. A refused start fails the
attempt; systemd tries again 15 s later, so the wait goes on across attempts, and the lane
starts by itself once the memory is back (one attempt fits in the 90 s systemd gives a start
step). Its lines start with `preflight:`, in the unit's journal:

```
preflight: qwen38-sglang.service waits: what is left of an engine still holds 16 GiB of GPU memory: 1756018 sglang::stuck 16578 MiB
preflight: NOT starting qwen38-sglang.service: what is left of an engine still holds 16 GiB ..., still after 60 s. ...
preflight: qwen38-sglang.service starts: no GPU memory held from before, 115.7 GiB available (0 s)
```

The cockpit reads them: a lane the guard holds shows **waiting for GPU memory** with the
reason, one it refused **not starting: GPU memory held**, never "starting" or "keeps
crashing". If nothing holds the memory and the driver still keeps it, only a reboot frees it.
A new version of the guard never restarts a running engine (it is no input of the engine).

Settings, for a test or a box that needs them: `QWEN38_PREFLIGHT_WAIT_S` (60),
`QWEN38_PREFLIGHT_HELD_MIB` (2048), `QWEN38_PREFLIGHT_ORPHAN_GIB` (16), set in a drop-in.

## Requests during a restart (proxy v6.28)

Before v1.21.0 every request of the boot window got a 503 at once. Measured against fake
engines, with the real clients:

| client | a 503 with `Retry-After: 30`, forever | a stream that only carries keepalives, 400 s, then the answer | a held stream that ends in an error event |
|---|---|---|---|
| opencode 1.18.32 | 6 attempts, 30 s apart, then the session fails at 151 s | waits, takes the answer, 1 request | sends it again 2.3 s later |
| Claude Code 2.1.286 (`claude-qwen`) | 11 attempts, 30 to 38 s apart, then fails at 325 s | waits, takes the answer, 1 request | sends it again without streaming |

So every engine restart ended every session it caught, in both clients. Now a request that finds the
engine not answering, while `qwen38-sglang.service` or `qwen38-flash.service` is starting,
waiting for its next attempt, up, or in the stop half of a restart (the unit "deactivating"
under a `restart` job), waits for it (`PROXY_HOLD_UNITS`, at most `PROXY_HOLD_MAX_S`, 1200 s,
both in the proxy's unit):

- a **streamed** request gets its `200` and the keepalives at once, the ones the proxy sends
  during a long prefill (an authentic empty chunk on the OpenAI dialect, the `ping` event on
  the Anthropic one), and its answer once the engine is back;
- a **non-streamed** one waits in silence;
- one that still fails after the wait (the engine not back in time, a prompt too long for
  the lane that came back) ends its stream with an error event, which both clients answer by
  sending the request again, to an engine that now answers;
- the request **in flight** when the engine dies gets the engine's own answer: the 500 before
  any token, or a stream cut short, which the proxy passes on as a cut (no closing chunk, no
  event in the engine's place). Both clients send it again at once, as a stream, and that
  retry is held. An error event in place of the cut, which earlier versions sent on the
  Anthropic dialect, made Claude Code send it again without a stream: held in silence, it
  gave up after about 330 s, again and again, and ended without an answer (measured on the
  reference box, below).

Not held: a lane stopped on purpose (its unit inactive, or stopping under a `stop` job), the
image and video lanes, `GET` routes, a proxy run by hand (no `PROXY_HOLD_UNITS`), and an engine that answers and still
fails the request (it is here, not on its way back; once a hold began, a boot not quite done
gets the request again 10 times at most, each after a pause). The journal says
`holding: ...` when a wait begins and `the engine answers again after N s held` when it ends.

## A server left without its scheduler

SGLang v0.5.19 can end up with its server process alive and no scheduler: a SIGQUIT that
reaches the server while its scheduler still runs raises its exit inside an asyncio task,
which swallows it (reproduced on the reference box). The flash lane's image ends there too,
a minute later: it first collects its crash diagnostics (60 s), then kills its scheduler and
keeps its server process, 196 MiB of GPU memory and no HTTP. Nothing answers, the unit stays active,
and nothing restarted it: the cockpit read it "degraded" for as long as it lasted. The
cockpit now restarts that one case itself, once it has been so for 2 min, at most once per
half hour per lane, through the start guard. `COCKPIT_ZOMBIE_RESTART=0` leaves it to the
operator. The general autoheal (`COCKPIT_AUTOHEAL`) stays off by default.

## Tested end to end

On the reference box, with the flash lane serving and the real opencode 1.18.32 and Claude
Code 2.1.286 each streaming a long answer through the proxy, its server process was sent
SIGQUIT (2026-10-01). Twice; each run found what the unit tests could not, and the fixes are
in this release:

- **Run 1.** The engine collected its crash diagnostics for 60 s, then killed its scheduler
  and kept its server: a zombie. The cockpit as first written never saw it: it asked
  `docker top` for the `comm` column alone, which docker refuses without the PID column, so
  its probe never answered. With the probe fixed, the cockpit named the zombie, restarted the
  lane 120 s later, the guard let it start ("no GPU memory held from before, 113.9 GiB
  available"), and opencode's request, held 632 s, got its whole answer. Two more faults:
  the restart's stop half ended the waits (the unit "deactivating", now read with its
  `restart` job), and Claude Code, given an error event in place of the cut, sent its
  request again without a stream, gave up on it after 329, 337 and 360 s, and ended with no
  answer (the cut is now passed on as a cut).
- **Run 2,** the proxy as released. Both streams were cut, both clients sent their request
  again at once as a stream, and both were held. The cockpit read the lane "capturing
  graphs" instead of "degraded": it had watched this boot, and the last stage it saw then
  came back when the crash's tracebacks left no milestone in the log it reads (now an
  activation that served counts as fired up). Restarted by hand: its stop took 16 s and no
  wait ended, the guard let it start, the boot took 11 min, and both held requests went on
  after 1,066 and 1,068 s. opencode wrote its whole answer (2,410 words); Claude Code's came
  whole too, and then stopped on the 4,000-token output cap of the test's own settings (the
  same request with no crash stops on it as well; `claude-qwen` sets 128,000).
- **The installed release** (v1.21.0, by the one-liner), the same crash: the cockpit named
  the zombie 96 s after the SIGQUIT (its 60 s of diagnostics included) and restarted the
  lane 120 s later, the guard let it start ("116.0 GiB available"), no wait ended, and
  opencode's answer came whole after 801 s held. Claude Code's never came: run with this
  box's Claude Code settings, its `max_tokens` (250,000, sized for the 27B in 1M mode) passed the
  flash lane's window, so each of its streamed requests was refused inside its stream, it
  sent them again without a stream, and it gave up on each of those after about 6 minutes
  (the pair for the flash lane is in docs/clients.md).

Against a fake engine that cuts a stream the way the real one did (`IncompleteRead`) and
then stays away 900 s, both clients sent their request again once, as a stream, waited the
900 s and took the answer.

## The NVRM line that looks like a cause

`NVRM: nvCheckOkFailedNoLog: Check failed: Out of memory [NV_ERR_NO_MEMORY] (0x00000051)
returned from _memdescAllocInternal(pMemDesc) @ mem_desc.c:1359` is routine on GB10: 4,286
of them on the reference box in three weeks, two thirds during ordinary engine boots, the rest
when another GPU client (a headless Chromium, for one) creates a context while an engine
holds the pool. While an engine serves, the truly free memory is about 1 GiB with no free
block of 2 MiB or more, and a new CUDA context fails there with `CUDA_ERROR_OUT_OF_MEMORY`
while MemAvailable reads 15 to 19 GiB. The engine's own allocations still succeed. The cockpit
shows these lines as "GPU driver refused N allocations at the edge of memory": no action.

## If an engine of yours dies

The class of failure behind issue #26 (`CUDA error: operation not permitted` after one to
three days) has been reported on driver 580.159.03 every time we could find a report:
sglang#40948, vllm#52877 (two crashes, and a third on a dual-Spark box in its comments), and
issue #26. The reference box runs 580.178.04 and has not
seen it, which is weak evidence (its engines rarely run more than a day in one go). If you
update the driver, do it through the DGX Dashboard's OTA: an `apt upgrade` alone to 580.173.02
left GPUs unusable on OTA2607 (driver and GSP firmware not paired). A report that can be acted
on carries the OTA version, `cat /etc/dgx-release`, `nvidia-smi -q | grep -iE "driver
version|gsp"`, the machine's uptime at the crash, and the unit's journal around it.
