#!/usr/bin/env python3
"""The auto-continue plugin relaunches a session only after an error a relaunch can fix.

A relaunch sends the session's whole history again, plus the plugin's own reminder. On an
error that a relaunch cannot fix, a prompt past what the lane serves or an image the engine
cannot decode, it could only fail again, a little longer each time: the reference box's log
of 2026-09-09 shows 210,159, then 210,210, then 210,261 tokens refused against a 200,000
ceiling, and 26 such errors kept for relaunch in all. opencode 1.18.32 compacts on an
overflow by itself (SessionProcessor.halt sets needsCompaction), so a relaunch there also
lands on a turn that was going to carry on. These load the plugin as written under node,
with a fake opencode client, and feed it the events opencode sends.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = REPO / "extras" / "opencode" / "auto-continue.js"

# One session: an error event, then the idle opencode sends when the turn stops. Prints
# the prompts the plugin sent back.
DRIVER = r"""
const plugin = (await import(process.env.PLUGIN_URL)).default;
const sent = [];
const client = { session: {
  promptAsync: async (req) => { sent.push(req.body.parts[0].text); },
  messages: async () => ({ data: [] }),
} };
const hooks = await plugin({ client });
const error = JSON.parse(process.env.ERROR);
await hooks.event({ event: { type: "session.error", properties: { sessionID: "ses_test0000001", error } } });
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "ses_test0000001" } } });
console.log(JSON.stringify(sent));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TheAutoContinuePlugin(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="auto-continue-"))

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def relaunches(self, name, message):
        env = dict(os.environ, PLUGIN_URL=PLUGIN.as_uri(), AC_LOG=str(self.t / "ac.log"),
                   AC_IDLE_DELAY_MS="0", AC_THROTTLE_MS="0",
                   ERROR=json.dumps({"name": name, "data": {"message": message}}))
        r = subprocess.run(["node", "--input-type=module", "-e", DRIVER], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return len(json.loads(r.stdout.strip().splitlines()[-1]))

    # what a relaunch fixes: the engine restarting, a malformed tool-call delta
    def test_an_engine_that_restarted_is_relaunched(self):
        self.assertEqual(self.relaunches("APIError", "keepalive-proxy: upstream http://127.0.0.1:30000 "
                                         "unreachable ([Errno 111] Connection refused)"), 1)

    def test_a_tool_call_without_id_is_relaunched(self):
        self.assertEqual(self.relaunches("UnknownError", "Expected 'id' to be a string."), 1)

    # what it cannot fix
    def test_an_overflow_is_left_to_opencode(self):
        self.assertEqual(self.relaunches(
            "ContextOverflowError", "keepalive-proxy: the prompt is too long for this lane. This request "
            "is 420237 prompt tokens (counted by the engine); this lane serves at most 250000 prompt "
            "tokens (KV pool 889412 tokens, one-prompt ceiling 250000) and the engine would hang instead "
            "of refusing it."), 0)

    def test_the_proxys_older_refusal_is_not_relaunched(self):
        # the wording opencode did not read as an overflow, word for word from the box's log
        self.assertEqual(self.relaunches("APIError", "keepalive-proxy: this request is 210159 prompt "
                                         "tokens (counted by the engine); this lane serves at most 200000"), 0)
        self.assertEqual(self.relaunches("APIError", "keepalive-proxy: this request is at least ~408841 "
                                         "tokens by size (the engine could not count it), more than the pool"), 0)

    def test_an_image_the_engine_cannot_decode_is_not_relaunched(self):
        self.assertEqual(self.relaunches("UnknownError", "ImageDecodeError: Image could not be decoded"), 0)

    def test_a_user_abort_is_still_not_relaunched(self):
        self.assertEqual(self.relaunches("MessageAbortedError", "Aborted"), 0)


if __name__ == "__main__":
    unittest.main()
