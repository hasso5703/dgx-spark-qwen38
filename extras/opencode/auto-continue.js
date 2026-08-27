// auto-continue: automatically resumes an opencode session interrupted by a
// TECHNICAL error (tool-call delta without id, timeout, network reset...), OR
// left stuck right after a compaction (context compacted but the turn does not
// restart on its own), so a one-off incident no longer freezes a long run.
// Battle-tested on the reference box (errors 2026-08-24, compaction 2026-08-25),
// zero dependencies.
// Log: ~/.config/qwen38/auto-continue.log (override with AC_LOG).
// Install: cp auto-continue.js ~/.config/opencode/plugins/
// Plugins load at opencode startup only: a new session picks it up, a running
// one never does.
import { appendFile } from "node:fs/promises";

const num = (v, d) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
};

const LOG = process.env.AC_LOG || process.env.HOME + "/.config/qwen38/auto-continue.log";
const THROTTLE_MS = num(process.env.AC_THROTTLE_MS, 5000);   // minimum between two relaunches of one session
const IDLE_DELAY_MS = num(process.env.AC_IDLE_DELAY_MS, 1500); // breather after idle before relaunching
const MAX_CONSECUTIVE = num(process.env.AC_MAX_CONSECUTIVE, 25); // relaunches in a row without any progress
// never relaunch on these: a deliberate user action, or a problem that will
// not fix itself
const EXCLUDE = ["abort", "interrupt", "permission", "unauthorized", "auth", "payment", "quota"];

const RESUME_AFTER_ERROR = "<system-reminder>The previous turn was interrupted by a transient technical error, not by the user. Resume the task exactly where it stopped and continue through to completion. Do not apologize for or mention this interruption.</system-reminder>";
const RESUME_AFTER_COMPACTION = "<system-reminder>The context was just compacted and the turn did not resume on its own. Continue the task from the compaction summary, exactly where it left off, through to completion. Do not restart from scratch, and do not apologize for or mention this.</system-reminder>";

const state = new Map(); // sessionID -> {errPending, compactPending, lastPrompt, consecutive}

function getState(id) {
  let s = state.get(id);
  if (!s) {
    s = { errPending: false, compactPending: false, lastPrompt: 0, consecutive: 0 };
    state.set(id, s);
  }
  return s;
}

async function log(msg) {
  try { await appendFile(LOG, new Date().toISOString().slice(11, 19) + " " + msg + "\n"); } catch {}
}

function errText(error) {
  if (!error || typeof error !== "object") return String(error ?? "");
  const d = error.data && typeof error.data === "object" ? error.data : {};
  return `${error.name ?? ""}: ${d.message ?? error.message ?? JSON.stringify(error).slice(0, 200)}`;
}

// True only if the LAST message of the session is the compaction boundary,
// the sign that opencode compacted then stopped without relaunching the turn.
// A normally finished assistant turn (task actually done) returns false, so a
// completed task is never relaunched. Unknown shape or error => false.
async function stuckAfterCompaction(client, sessionID) {
  try {
    const res = await client.session.messages({ path: { id: sessionID } });
    const list = Array.isArray(res) ? res : res?.data ?? res?.messages ?? [];
    if (!Array.isArray(list) || list.length === 0) return false;
    const last = list[list.length - 1];
    if (last?.info?.summary === true || last?.summary === true) return true;
    const agent = last?.info?.agent ?? last?.agent;
    if (typeof agent === "string" && agent.toLowerCase().includes("compact")) return true;
    if (/"type"\s*:\s*"compaction"/.test(JSON.stringify(last))) return true;
    return false;
  } catch {
    return false;
  }
}

async function relaunch(client, sessionID, s, text, tag) {
  s.lastPrompt = Date.now();
  s.consecutive += 1;
  await new Promise((r) => setTimeout(r, IDLE_DELAY_MS));
  await log(`RELAUNCH ${tag} ${s.consecutive}/${MAX_CONSECUTIVE} (${sessionID.slice(0, 12)})`);
  await client.session.promptAsync({
    path: { id: sessionID },
    body: { parts: [{ type: "text", text }] },
  });
}

const plugin = async ({ client }) => {
  await log("auto-continue plugin loaded (v2: errors + compaction)");
  return {
    event: async ({ event }) => {
      try {
        if (event.type === "session.error") {
          const { sessionID, error } = event.properties ?? {};
          if (!sessionID) return;
          const txt = errText(error);
          if (EXCLUDE.some((p) => txt.toLowerCase().includes(p))) {
            await log(`error EXCLUDED (${sessionID.slice(0, 12)}): ${txt.slice(0, 120)}`);
            return;
          }
          getState(sessionID).errPending = true;
          await log(`error retained (${sessionID.slice(0, 12)}): ${txt.slice(0, 120)}`);
          return;
        }

        if (event.type === "session.compacted") {
          const sessionID = event.properties?.sessionID;
          if (!sessionID) return;
          getState(sessionID).compactPending = true;
          await log(`compaction detected (${sessionID.slice(0, 12)}): resume armed`);
          return;
        }

        if (event.type === "session.idle") {
          const sessionID = event.properties?.sessionID;
          if (!sessionID) return;
          const s = state.get(sessionID);
          if (!s) return;

          // nothing pending: a clean idle far from the last relaunch means real
          // progress (duplicate idles right after a relaunch do not count)
          if (!s.errPending && !s.compactPending) {
            if (Date.now() - s.lastPrompt > THROTTLE_MS) s.consecutive = 0;
            return;
          }
          if (Date.now() - s.lastPrompt < THROTTLE_MS) return;
          if (s.consecutive >= MAX_CONSECUTIVE) {
            await log(`STOP (${sessionID.slice(0, 12)}): ${MAX_CONSECUTIVE} relaunches without progress`);
            s.errPending = false;
            s.compactPending = false;
            return;
          }

          // the error takes priority (a hard break); otherwise a stuck compaction
          if (s.errPending) {
            s.errPending = false;
            await relaunch(client, sessionID, s, RESUME_AFTER_ERROR, "err");
            return;
          }
          if (s.compactPending) {
            s.compactPending = false;
            if (await stuckAfterCompaction(client, sessionID)) {
              await relaunch(client, sessionID, s, RESUME_AFTER_COMPACTION, "compact");
            } else {
              await log(`compaction OK, resumed on its own (${sessionID.slice(0, 12)}): no relaunch`);
            }
            return;
          }
        }
      } catch (e) {
        await log("plugin internal error: " + String(e).slice(0, 200));
      }
    },
  };
};

export default plugin;
