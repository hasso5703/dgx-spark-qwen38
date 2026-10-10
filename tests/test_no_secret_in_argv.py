#!/usr/bin/env python3
"""Secrets do not ride on a command line, where any local user reads them in /proc.

The download's docker run carried `-e HF_TOKEN=<token>`, and the smoke test's curl carried
`-H "Authorization: Bearer <key>"` (found in review, 2026-09-24). The token is passed by
name now (docker reads it from its own environment), and the key as a header file."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = {name: (REPO / name).read_text() for name in ("install.sh", "switch-model.sh")}


class NoSecretOnACommandLine(unittest.TestCase):
    def test_no_value_is_spelled_on_a_docker_or_curl_line(self):
        for name, text in SCRIPTS.items():
            self.assertNotIn('-e HF_TOKEN="$HF_TOKEN"', text, name)
            self.assertNotIn('-H "Authorization: Bearer $', text, name)

    def test_the_token_reaches_docker_by_name(self):
        for name, text in SCRIPTS.items():
            i = text.index("DL_TOKEN_ARGS=()")
            block = text[i:text.index("\n", text.index("DL_TOKEN_ARGS=(-e HF_TOKEN)", i)) + 1]
            d = pathlib.Path(tempfile.mkdtemp(prefix="argv-"))
            (d / "docker").write_text('#!/bin/sh\necho "ARGV $*"\necho "ENV ${HF_TOKEN:-unset}"\n')
            (d / "docker").chmod(0o755)
            script = "set -euo pipefail\n" + block + 'docker run "${DL_TOKEN_ARGS[@]}" image\n'
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                                 env={"PATH": f"{d}:/usr/bin:/bin", "HF_TOKEN": "hf_secret"}).stdout
            self.assertIn("ARGV run -e HF_TOKEN image", out, name)
            self.assertNotIn("hf_secret", out.split("ENV")[0], f"{name}: the token is in docker's argv")
            self.assertIn("ENV hf_secret", out, f"{name}: docker does not see the token")


# The same mistake outside the installer (found 2026-10-10): needle.sh asked /v1/models with
# -H "Authorization: Bearer $KEY", the cockpit's smoke test logged in with the key in -d, and
# the commands the docs and the Decide view give to paste spelled it into curl's or docker's
# arguments. These are the shapes of that mistake, looked for in every tracked file but the
# Python tests and fixtures (which replay old launchers) and the CHANGELOG (which tells their
# history); the tests' own shell and browser scripts are looked at too.
ARGV_SECRET = re.compile(
    r'-H\s+\\?"Authorization: Bearer \$'          # curl -H "Authorization: Bearer $KEY" (and in JS)
    r'|-e\s+[A-Z_]*(?:KEY|TOKEN)=\\?"?\$'         # docker run -e OPENAI_API_KEY="$KEY"
    r'|\\?"key\\?"\s*:\s*\\?"\$')                 # curl -d "{\"key\":\"$KEY\"}"


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, timeout=30)
    if out.returncode == 0 and out.stdout:
        names = [n for n in out.stdout.decode().split("\0") if n]
    else:                                          # an export without .git: walk the tree
        names = [str(p.relative_to(REPO)) for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [n for n in names if n != "CHANGELOG.md" and not (
        n.startswith(("tests/", "dashboard/tests/")) and (n.endswith(".py") or "/fixtures/" in n))]


class NoSecretOnACommandLineAnywhere(unittest.TestCase):
    def test_no_tracked_file_spells_the_key_into_an_argument(self):
        found = []
        for name in tracked_files():
            path = REPO / name
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if ARGV_SECRET.search(line) and not (name == "check-pins.sh" and "Bearer $token" in line):
                    # check-pins.sh's $token is the anonymous pull token auth.docker.io hands
                    # anyone for a public repo, valid minutes: no secret of this box
                    found.append(f"{name}:{n}: {line.strip()[:100]}")
        self.assertEqual(found, [])

    def test_the_shapes_are_the_ones_that_were_there(self):
        # each shape as it stood in the repo before 2026-10-10, so a pattern that stopped
        # matching would not leave the scan above green and blind
        for line in ('  MODEL="$(curl -s -m 5 -H "Authorization: Bearer $KEY" "$BASE/v1/models" \\',
                     '"curl -s http://127.0.0.1:30001/v1/systemone \\\\\\n  -H \\"Authorization: Bearer $(cat ~/.config/qwen38/api-key)\\" \\\\\\n"',
                     'docker run --rm --network host -e OPENAI_API_KEY="$KEY" \\',
                     """ck "login bonne cle" 200 "$(code -c "$J" -X POST "$BASE/api/login" -H 'Content-Type: application/json' -d "{\\"key\\":\\"$KEY\\"}")\""""):
            self.assertTrue(ARGV_SECRET.search(line), line)
        for line in ("""  -H @<(printf 'Authorization: Bearer %s\\n' "$(cat ~/.config/qwen38/api-key)") \\""",
                     'OPENAI_API_KEY="$KEY" docker run --rm --network host -e OPENAI_API_KEY \\',
                     """--data-binary @<(printf '{"key":"%s"}' "$KEY")"""):
            self.assertFalse(ARGV_SECRET.search(line), line)


class NeedleAsksForTheModelWithTheKeyOffItsCommandLine(unittest.TestCase):
    """needle.sh without --model asks /v1/models first; its curl carried the key as an
    argument. A curl on PATH that records its arguments and the header files it is given."""

    def test_the_key_goes_in_a_header_file(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="needle-argv-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / ".config" / "qwen38").mkdir(parents=True)
        (d / ".config" / "qwen38" / "api-key").write_text("sk-argv-probe-0123456789\n")
        (d / "bin").mkdir()
        (d / "bin" / "curl").write_text(
            '#!/bin/bash\nfor a in "$@"; do printf "%s\\n" "$a" >> "$ARGS_OUT"; '
            'case "$a" in @*) cat "${a#@}" >> "$HEADERS_OUT";; esac; done\n'
            'echo \'{"data": [{"id": "m"}]}\'\n')
        (d / "bin" / "curl").chmod(0o755)
        env = {"PATH": f"{d / 'bin'}:/usr/bin:/bin", "HOME": str(d), "PORT": "9",
               "ARGS_OUT": str(d / "args.txt"), "HEADERS_OUT": str(d / "headers.txt")}
        subprocess.run(["bash", str(REPO / "needle.sh"), "--trials", "1", "--depths", "1000", "--no-flush"],
                       capture_output=True, text=True, timeout=120, env=env)
        args = (d / "args.txt").read_text()
        self.assertIn("/v1/models", args, "needle.sh did not ask for the model")
        self.assertNotIn("sk-argv-probe-0123456789", args)
        self.assertIn("Authorization: Bearer sk-argv-probe-0123456789",
                      (d / "headers.txt").read_text() if (d / "headers.txt").exists() else "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
