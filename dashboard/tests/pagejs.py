"""Run the cockpit's page scripts in node, against a small DOM built from index.html.

A test hands over JavaScript that runs in the page's own global scope once app.js has
loaded, drives it (renderers, clicks, fetch answers, virtual time) and ends with
report(value); run() returns that value. What this cannot see is layout: every box
measures 0 (fakedom.js says what it covers). node is in the CI image, which already
checks the opencode plugin's syntax with it, so a missing node fails there instead of
skipping a test that then counts as run.
"""
import html.parser
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

DASH = pathlib.Path(__file__).resolve().parents[1]
STATIC = DASH / "static"
FAKEDOM = pathlib.Path(__file__).with_name("fakedom.js")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}


class _Tree(html.parser.HTMLParser):
    """[tag, [[name, value], ...], children] per element, str per text node. The contents
    of <script> and <style> are dropped: the scripts run from their own files."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["#root", [], []]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = [tag, [[k, v if v is not None else ""] for k, v in attrs], []]
        self.stack[-1][2].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1][2].append([tag, [[k, v if v is not None else ""] for k, v in attrs], []])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.stack[-1][0] not in ("script", "style"):
            self.stack[-1][2].append(data)


def tree(markup: str):
    p = _Tree()
    p.feed(markup)
    p.close()
    return next(n for n in p.root[2] if isinstance(n, list))


def node_binary(test):
    node = shutil.which("node")
    if node:
        return node
    if os.environ.get("CI"):
        test.fail("node is needed for the page tests, and the CI image has it")
    test.skipTest("node is not installed here")


def run(test, body: str, *, setup: str = "", scripts=("app.js",), markup: str | None = None,
        opts: dict | None = None, timeout: float = 60.0):
    """Load the page, run `setup`, then the page scripts, then `body`; return what `body`
    passed to report(). A page script that throws while loading fails the test, and so
    does an exception in a timer callback."""
    node = node_binary(test)
    page = markup if markup is not None else (STATIC / "index.html").read_text()
    job = {"tree": tree(page), "opts": opts or {}, "setup": setup,
           "scripts": [str(STATIC / s) for s in scripts], "body": body}
    with tempfile.TemporaryDirectory(prefix="pagejs-") as d:
        path = pathlib.Path(d) / "job.json"
        path.write_text(json.dumps(job))
        p = subprocess.run([node, str(FAKEDOM), str(path)], capture_output=True, text=True,
                           timeout=timeout)
    out = p.stdout.rsplit("\n__RESULT__", 1)
    if p.returncode != 0 or len(out) != 2:
        err = p.stdout.rsplit("\n__ERROR__", 1)[-1] if "__ERROR__" in p.stdout else ""
        test.fail(f"node exited {p.returncode}: {err or p.stderr[-2000:] or p.stdout[-2000:]}")
    res = json.loads(out[1])
    if res["errors"]:
        test.fail("a timer callback threw: " + res["errors"][0][:1500])
    return res["result"]
