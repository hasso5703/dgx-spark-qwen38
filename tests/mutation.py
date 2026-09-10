#!/usr/bin/env python3
"""A small, dependency-free mutation tester for this repo's pure modules.

Mutation testing answers the only question that matters for a suite written
alongside the code it tests: if the code were WRONG, would the tests notice?
It edits one operator at a time in the module's AST, runs the suite, and counts
how many edits the suite catches. An edit nobody catches is a line the suite
covers but does not check.

mutmut and cosmic-ray are the usual tools; both want a pytest-shaped project and
a working directory they can rewrite. This repo runs unittest files directly, so
a 200-line walker that respects that is more honest than bending the repo to a
tool. Same operators either way (comparison, boundary, arithmetic, boolean,
constant, return).
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class Mutator(ast.NodeTransformer):
    """Applies exactly the nth mutation it finds, and reports what it was."""

    CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
           ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
           ast.In: ast.NotIn, ast.NotIn: ast.In,
           ast.Is: ast.IsNot, ast.IsNot: ast.Is}
    BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
           ast.Div: ast.Mult, ast.FloorDiv: ast.Mult}
    BOOL = {ast.And: ast.Or, ast.Or: ast.And}

    def __init__(self, target):
        self.target = target
        self.seen = 0
        self.applied = None

    def _hit(self, desc):
        self.seen += 1
        if self.seen - 1 == self.target:
            self.applied = desc
            return True
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in self.CMP:
            new = self.CMP[type(node.ops[0])]
            if self._hit(f"line {node.lineno}: {type(node.ops[0]).__name__} -> {new.__name__}"):
                node.ops = [new()]
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if type(node.op) in self.BIN:
            new = self.BIN[type(node.op)]
            if self._hit(f"line {node.lineno}: {type(node.op).__name__} -> {new.__name__}"):
                node.op = new()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if type(node.op) in self.BOOL:
            new = self.BOOL[type(node.op)]
            if self._hit(f"line {node.lineno}: {type(node.op).__name__} -> {new.__name__}"):
                node.op = new()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit(f"line {node.lineno}: dropped a 'not'"):
            return node.operand
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._hit(f"line {node.lineno}: {node.value} -> {not node.value}"):
                return ast.copy_location(ast.Constant(value=not node.value), node)
        elif isinstance(node.value, int) and -1000 <= node.value <= 10 ** 7:
            if self._hit(f"line {node.lineno}: {node.value} -> {node.value + 1}"):
                return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node


def count(src):
    tree = ast.parse(src)
    m = Mutator(-1)
    m.visit(tree)
    return m.seen


def mutate(src, n):
    tree = ast.parse(src)
    m = Mutator(n)
    tree = m.visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree), m.applied


def run_suite(cmds, cwd):
    """Run the suite against whatever is on disk right now.

    PYTHONDONTWRITEBYTECODE and the cache sweep are not hygiene, they are
    correctness: CPython validates a .pyc against its source mtime and SIZE, and
    two mutants written inside the same mtime granularity with the same length
    are indistinguishable to that check, so the interpreter silently re-ran the
    PREVIOUS mutant's bytecode. That reported a mutant as surviving when the
    suite kills it in one second by hand, which is the worst failure a tool like
    this can have: a number that is wrong in an unknown direction.
    """
    for cache in Path(cwd).rglob("__pycache__"):
        for f in cache.glob("*.pyc"):
            f.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=600, env=env)
        if r.returncode != 0:
            return False
    return True


# What each module's suite is, and the score its suite must reach. A floor is
# raised in a commit, never lowered quietly: that is the whole point of having
# one. The numbers are what was measured on 2026-09-10, minus a small margin for
# the equivalent mutants any walk of this kind produces.
TARGETS = {
    "dashboard/lifecycle.py": ("python3 dashboard/tests/test_lifecycle.py", 91.0),
    "dashboard/recipes.py": ("python3 dashboard/tests/test_recipes.py", 79.0),
}


def main():
    if len(sys.argv) == 1 or sys.argv[1] == "--all":
        worst = 0.0
        failed = []
        for mod, (cmd, floor) in TARGETS.items():
            score = one((REPO / mod).resolve(), [cmd.split()], 10 ** 6, quiet=True)
            mark = "ok" if score >= floor else "UNDER FLOOR"
            print(f"  {mark:11} {mod:28} {score:5.1f}%  (floor {floor:.0f}%)")
            if score < floor:
                failed.append(mod)
            worst = max(worst, floor - score)
        print(f"\nmutation score: {'all modules at or above their floor' if not failed else 'BELOW FLOOR: ' + ', '.join(failed)}")
        return 1 if failed else 0
    # resolve(): the walk mutates a COPY of the repo and locates the module by
    # its path relative to REPO, so a relative argv (dashboard/lifecycle.py, the
    # way anyone would type it) raised ValueError until this line existed.
    module = Path(sys.argv[1]).resolve()
    suite_cmds = [c.split() for c in sys.argv[2].split(";")]
    budget = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 6
    one(module, suite_cmds, budget)
    return 0


def one(module: Path, suite_cmds, budget, quiet=False) -> float:
    """Mutate and measure inside a COPY of the repo, never the working tree.

    Rewriting a source file in place has two failure modes that both bit this
    tool on the day it was written: anything else reading the repo at that
    instant sees a mutant (a parallel test run reported 46 errors and one
    baffling parse failure, which was this tool editing recipes.py underneath
    it), and a kill -9 between the write and the restore leaves a mutated file
    staged for commit. A throwaway copy has neither.
    """
    work = Path(tempfile.mkdtemp(prefix="mutation-"))
    root = work / "repo"
    shutil.copytree(REPO, root, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", ".venv-test", ".venv",
                                                  "__pycache__", ".hypothesis",
                                                  "*.pyc", ".coverage*"))
    try:
        return _walk(root / module.relative_to(REPO), suite_cmds, budget, root, quiet)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _walk(module: Path, suite_cmds, budget, root: Path, quiet=False) -> float:
    src = module.read_text()
    total = min(count(src), budget)
    if not quiet:
        print(f"{module.name}: {count(src)} mutation points, running {total}")
    if not run_suite(suite_cmds, root):
        print(f"{module.name}: the suite does not pass on unmutated code; aborting")
        raise SystemExit(2)
    killed, survived = 0, []
    try:
        for i in range(total):
            new, desc = mutate(src, i)
            if desc is None:
                continue
            module.write_text(new)
            try:
                caught = not run_suite(suite_cmds, root)
            except subprocess.TimeoutExpired:
                caught = True            # a hang is a detection: the suite noticed
            if caught:
                killed += 1
            else:
                survived.append(desc)
            if not quiet:
                print(f"\r  {i + 1}/{total} killed={killed} survived={len(survived)}",
                      end="", flush=True)
    finally:
        module.write_text(src)          # the copy, so this is belt and braces
    score = 100.0 * killed / max(1, killed + len(survived))
    if not quiet:
        print()
        print(f"mutation score: {score:.1f}% ({killed} killed, {len(survived)} survived)")
        for line in survived[:25]:
            print(f"  SURVIVED {line}")
    return score


if __name__ == "__main__":
    sys.exit(main())
