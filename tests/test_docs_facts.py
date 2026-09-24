#!/usr/bin/env python3
"""The documents say what the code does, where a check can tell.

Three drifts found in review (2026-09-24), each of which a reader could not see
from the page itself:
- a code block of README.md was never closed, so on GitHub the paragraph after
  it, the heading of the cockpit section and its introduction rendered as code;
- install.sh's closing summary sent people to a cockpit tab named Benchmarks,
  which never existed, and the README's tab table promised a bench, a canary
  battery and a key regeneration no tab has;
- the README's disk requirements were not the preflight's (84 GB announced for
  a 27B target where the preflight asks 45 on one disk and 40 on another, 225
  for a flash target where it asks 230 and 35), so a box sized from the README
  was refused.
"""
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".claude", ".venv", ".venv-test", ".hypothesis", "node_modules", "__pycache__"}


def tracked(*patterns):
    """The tracked files matching the patterns, or, outside a git checkout, the same
    walk with the working directories of tools left out."""
    r = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--", *patterns],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout:
        return [REPO / p for p in r.stdout.split("\0") if p]
    found = set()
    for pat in patterns:
        for p in REPO.rglob(pat):
            if not SKIP_DIRS & set(p.relative_to(REPO).parts):
                found.add(p)
    return sorted(found)


FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def fence_problems(text):
    """CommonMark's rule, as far as this repo's pages need it: a block opens on a
    fence of three or more backticks or tildes, and only a fence of the same
    character, at least as long and with nothing after it, closes it. So a fence
    with an info string inside an open block cannot close it, and is the mark of a
    block left open above (README.md's Operations block ran into the next
    ```bash that way)."""
    problems, open_at = [], None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if not m:
            continue
        fence, rest = m.group(1), m.group(2).strip()
        if open_at is None:
            open_at = (n, fence)
        elif fence[0] == open_at[1][0] and len(fence) >= len(open_at[1]) and not rest:
            open_at = None
        elif rest:
            problems.append(f"line {n}: a fence with an info string inside the block opened at line {open_at[0]}")
    if open_at is not None:
        problems.append(f"line {open_at[0]}: a code block that never closes")
    return problems


class TheCodeBlocksClose(unittest.TestCase):
    def test_every_tracked_page(self):
        pages = tracked("*.md")
        self.assertGreater(len(pages), 20, "the page list came back nearly empty")
        bad = {str(p.relative_to(REPO)): fence_problems(p.read_text(encoding="utf-8")) for p in pages}
        bad = {k: v for k, v in bad.items() if v}
        self.assertEqual(bad, {})

    def test_the_rule_sees_the_block_that_swallowed_a_heading(self):
        # the shape README.md had: a block that runs into the next opening fence
        text = "## Operations\n\n```bash\nls\n\nprose\n\n## The cockpit\n\n```bash\nx\n```\n"
        self.assertEqual(len(fence_problems(text)), 1)
        self.assertEqual(fence_problems("```\na\n```\n\n~~~~\n```\n~~~~\n"), [])
        self.assertEqual(fence_problems("```python\nx\n"), ["line 1: a code block that never closes"])


def nav_labels():
    html = (REPO / "dashboard/static/index.html").read_text()
    labels = re.findall(r'<button class="nav" data-tab="[a-z]+"[^>]*>.*?<span class="lab">([^<]+)</span>', html)
    return set(labels)


# "<Name> tab" in prose and messages; a leading word that only introduces the name
# ("The Agent tab", "Every tab") is not part of it
TAB_REF = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?) tab\b")
NOT_A_NAME = {"The", "Every", "One", "Each", "This", "That", "A", "An", "Its", "Any", "Which"}


class TheTabsTheDocsNameExist(unittest.TestCase):
    def test_the_page_has_its_tabs(self):
        self.assertGreaterEqual(len(nav_labels()), 10, "the nav markup moved: fix the pattern")

    def test_every_tab_named_anywhere_is_one_of_the_page(self):
        labels = nav_labels()
        unknown = []
        # the changelog is history: a tab it names may have been renamed since
        files = [p for p in tracked("*.md", "*.sh", "*.py", "*.js", "*.html") if p.name != "CHANGELOG.md"]
        for p in files:
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for m in TAB_REF.finditer(line):
                    words = m.group(1).split()
                    while words and words[0] in NOT_A_NAME:
                        words.pop(0)
                    if words and " ".join(words) not in labels:
                        unknown.append(f"{p.relative_to(REPO)}:{n}: {m.group(0)}")
        self.assertEqual(unknown, [], f"tabs the page does not have (it has {sorted(labels)})")

    def test_the_readme_table_lists_the_tabs_of_the_page(self):
        readme = (REPO / "README.md").read_text()
        section = readme[readme.index("## The cockpit"):]
        section = section[:section.index("\n## ", 1)]
        rows = re.findall(r"^\| \*\*([^*]+)\*\* \|", section, re.M)
        self.assertEqual(sorted(rows), sorted(nav_labels()))


class TheDiskNeedsAreThePreflights(unittest.TestCase):
    def test_the_readme_states_what_the_installer_checks(self):
        sh = (REPO / "install.sh").read_text()
        pairs = [tuple(map(int, m)) for m in re.findall(r"NEED_GB=(\d+); DOCKER_NEED_GB=(\d+); IMG_LABEL=", sh)]
        self.assertEqual(len(pairs), 2, "the preflight's two needs moved: fix the pattern")
        (b_hf, b_docker), (f_hf, f_docker) = pairs
        ple = re.findall(r"NEED_GB=\$\(\(NEED_GB \+ (\d+)\)\)", sh)
        self.assertEqual(len(ple), 1, "the PLE table's share moved: fix the pattern")
        f_hf += int(ple[0])
        labels = re.findall(r'IMG_LABEL="(\d+) GB Docker image"', sh)
        readme = (REPO / "README.md").read_text()
        req = next(line for line in readme.splitlines() if line.startswith("Requirements:"))
        for gb in (b_hf, b_docker, f_hf, f_docker, b_hf + b_docker, f_hf + f_docker):
            self.assertIn(f"**{gb} GB**", req, f"install.sh checks {gb} GB somewhere the README does not say")
        for gb in labels:
            self.assertIn(f"its {gb} GB image", req, f"install.sh names a {gb} GB image the README does not")


if __name__ == "__main__":
    unittest.main(verbosity=2)
