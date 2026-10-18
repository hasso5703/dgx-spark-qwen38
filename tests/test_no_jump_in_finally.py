#!/usr/bin/env python3
"""No return, break or continue leaves a finally block: it would swallow what is raising.

The proxy's pump thread ended its finally with a return when it had drained an abandoned
answer (keepalive-proxy.py, the relay's pump): a return there discards the exception that
brought the thread into the finally, and Python 3.14 says so when it compiles the file
(PEP 765, "SyntaxWarning: 'return' in a 'finally' block", seen with 3.14.6 in a cloud
review, 2026-10-18). The box runs 3.12, which says nothing. This walks the syntax tree of
every tracked Python file instead of waiting for a compiler to say it, and is stricter
than 3.14.6, which says nothing of a return in a loop of the finally: that swallows the
same.
"""
import ast
import pathlib
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".claude", ".venv", ".venv-test", ".hypothesis", "node_modules", "__pycache__"}


def tracked_python():
    """The tracked Python files, or, outside a git checkout, the same walk with the
    working directories of tools left out."""
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", "*.py"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0 and r.stdout:
        return [REPO / p for p in r.stdout.split("\0") if p]
    return sorted(p for p in REPO.rglob("*.py") if not SKIP_DIRS & set(p.relative_to(REPO).parts))


def leaving(node, in_loop=False):
    """The return, break and continue under node that leave the finally node sits in."""
    if isinstance(node, ast.Return) or (isinstance(node, (ast.Break, ast.Continue)) and not in_loop):
        yield node
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        return                                   # a scope of its own: its return is its own
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        for child in node.body:                  # a loop of the finally keeps its break
            yield from leaving(child, True)
        for child in node.orelse:                # but its else is not in that loop
            yield from leaving(child, in_loop)
    else:
        for child in ast.iter_child_nodes(node):
            yield from leaving(child, in_loop)


def jumps_out_of_finally(source, name="<shape>"):
    """(line, keyword) of each jump out of a finally block of source, each said once."""
    found = set()
    for node in ast.walk(ast.parse(source, name)):
        if isinstance(node, (ast.Try, ast.TryStar)):
            for stmt in node.finalbody:
                found.update((j.lineno, type(j).__name__.lower()) for j in leaving(stmt))
    return sorted(found)


class NoJumpLeavesAFinally(unittest.TestCase):
    def test_no_tracked_python_file_has_one(self):
        found = []
        for path in tracked_python():
            name = str(path.relative_to(REPO))
            found += [f"{name}:{line}: {kw}"
                      for line, kw in jumps_out_of_finally(path.read_text(encoding="utf-8"), name)]
        self.assertEqual(found, [])

    def test_the_shape_that_was_there_is_seen(self):
        # the pump's finally as it stood before 2026-10-18, cut down, so a walk that
        # stopped seeing it would not leave the scan above green and blind; the breaks
        # of its own loop stay in the finally
        shape = ("def pump():\n"
                 "    try:\n"
                 "        work()\n"
                 "    finally:\n"
                 "        if drain.is_set():\n"
                 "            resp.close()\n"
                 "            return\n"
                 "        while True:\n"
                 "            try: q.put(end); break\n"
                 "            except queue.Full:\n"
                 "                if stop.is_set(): break\n")
        self.assertEqual(jumps_out_of_finally(shape), [(7, "return")])

    def test_what_leaves_and_what_stays(self):
        shapes = {
            # a loop around the try: its break and its continue leave the finally
            "for x in xs:\n    try:\n        pass\n    finally:\n        break\n": [(5, "break")],
            "for x in xs:\n    try:\n        pass\n    finally:\n        continue\n": [(5, "continue")],
            # a return in a loop of the finally leaves it all the same (3.14.6 is silent here)
            "def f(xs):\n    try:\n        pass\n    finally:\n        for x in xs:\n            return x\n":
                [(6, "return")],
            # the else of a loop of the finally is not in that loop: its break is the outer one's
            "while a:\n    try:\n        pass\n    finally:\n        for x in xs:\n            pass\n"
            "        else:\n            break\n": [(8, "break")],
            # a try in the finally: its return leaves both, and is said once
            "def f():\n    try:\n        pass\n    finally:\n        try:\n            pass\n"
            "        finally:\n            return 1\n": [(8, "return")],
            # a function or a lambda of the finally has a return of its own
            "try:\n    pass\nfinally:\n    def g():\n        return 1\n    h = lambda: 2\n": [],
            # the try and its except may return: the finally runs after, nothing is swallowed
            "def f():\n    try:\n        return 1\n    except ValueError:\n        return 2\n"
            "    finally:\n        log()\n": [],
        }
        for source, expected in shapes.items():
            self.assertEqual(jumps_out_of_finally(source), expected, source)


if __name__ == "__main__":
    unittest.main()
