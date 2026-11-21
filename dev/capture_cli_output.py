#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Recapture the installed Codex and Claude Code CLIs' own output, the input the bridge's parsers read, as fixtures.

Run before each release, from the repository root, with both CLIs signed in. It calls the real CLIs on the
maintainer's account: four short model calls with a fixed prompt, two calls naming a model that does not exist, and
the sign-in probes. Each consultation runs the bridge's own command
(consultation.build_command) in a new, empty temporary git repository. The Codex and Claude Code calls leave a session
each in the account's history, as a consultation does.

Every string in the output is replaced by TEXT-<n>, and every UUID by a fixed one, except the fixed reply and the
string values of the keys in KEPT, which name kinds of things, never content; the same original always maps to the
same replacement. A stderr line keeps only the tag the bridge reads. The capture is refused when an exact private
token (the home directory, the host name, the git identity, the account's email) is still in it. Each fixture records
the CLI's version and exit status. Read the diff before committing it; the tests in test_core's CapturedOutputTests
show which parser a changed capture breaks.

    python3 dev/capture_cli_output.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "plugins" / "reweigh" / "tests" / "fixtures" / "cli"
sys.path.insert(0, str(ROOT / "plugins" / "reweigh" / "scripts"))
import consultation as c  # noqa: E402

REPLY = "PEER-BRIDGE-CAPTURE-OK"
PROMPT = f"Reply with exactly {REPLY} and nothing else. Do not read files or run commands."
FOLLOW_UP = f"Reply with exactly {REPLY} again, and nothing else."
UNKNOWN_MODEL = "reweigh-no-such-model"
# bridge.py's SIGN_IN_PROBES; bridge.py needs the MCP package, which this tool does without.
SIGN_IN_PROBES = {"claude": ("auth", "status", "--json"), "codex": ("login", "status")}
KEPT = {"type", "subtype", "stop_reason", "visibility", "slug", "effort", "default_reasoning_level"}
TIMEOUT_SECONDS = 300


class Refused(Exception):
    """A capture would carry private text, or a CLI could not be run."""


# Pure rules


class Scrubber:
    """Replaces every string but the fixed reply and KEPT keys' values; equal originals get equal replacements."""

    def __init__(self):
        self.texts, self.ids = {}, {}

    def text(self, value):
        return self.texts.setdefault(value, f"TEXT-{len(self.texts) + 1}")

    def string(self, value, key):
        if value == REPLY or key in KEPT:
            return value
        try:
            original = uuid.UUID(value)
        except ValueError:
            return self.text(value)
        return self.ids.setdefault(original, str(uuid.UUID(int=len(self.ids) + 1)))

    def value(self, value, key=None):
        if isinstance(value, dict):
            return {name: self.value(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self.value(item, key) for item in value]
        return self.string(value, key) if isinstance(value, str) else value

    def json_lines(self, text):
        """JSON output, one document or JSON Lines, with each record scrubbed; a line that is not JSON is replaced
        whole."""
        try:
            document = json.loads(text)
        except ValueError:
            pass
        else:
            return json.dumps(self.value(document), ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
        lines = []
        for line in text.split("\n"):
            try:
                record = json.loads(line)
            except ValueError:
                lines.append(self.text(line) if line else line)
                continue
            lines.append(json.dumps(self.value(record), ensure_ascii=False))
        return "\n".join(lines)

    def errors(self, text):
        """Stderr with only the tag the bridge reads kept, at the start of its line."""
        tag = c.CLAUDE_UNRECOGNIZED_MODEL_TAG
        return "\n".join(
            line if not line else f"{tag} {self.text(line[len(tag):])}" if line.startswith(tag) else self.text(line)
            for line in text.split("\n"))


def private_tokens_in(text, tokens):
    """The exact private tokens a capture still holds."""
    return sorted(token for token in tokens if token and token in text)


# Running the CLIs


def run(command, cwd, environment, prompt=None):
    try:
        result = subprocess.run(command, input=prompt, cwd=cwd, env=environment, capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"{Path(command[0]).name} {' '.join(command[1:3])} did not finish: {exc}") from exc
    return result.returncode, result.stdout, result.stderr


def executable(name):
    found = shutil.which(name)
    if found is None:
        raise Refused(f"{name} is not on PATH.")
    return found


def empty_repository(parent):
    folder = Path(tempfile.mkdtemp(dir=parent)).resolve()
    subprocess.run(["git", "init", "-q", str(folder)], check=True, env=c.git_environment(os.environ))
    return folder


def git_identity():
    values = []
    for key in ("user.email", "user.name"):
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
        values.append(result.stdout.strip())
    return values


def raw_captures(parent):
    """Each capture's (CLI, exit status, stdout, stderr), as the CLIs printed them."""
    codex, claude = executable("codex"), executable("claude")
    environment = c.peer_environment(os.environ)
    raw = {}

    def consult(name, provider, session=None, model=None, prompt=PROMPT):
        folder = empty_repository(parent)
        directories = (str(folder),) if provider == "claude" else ()
        command = c.build_command(provider, codex if provider == "codex" else claude, str(uuid.uuid4()),
                                  backend_session_id=session, model=model, directories=directories)
        raw[name] = (provider, *run(command, folder, environment, c.build_handoff(provider, prompt)))

    def session_of(name, provider):
        try:
            return c.parse_response(provider, raw[name][2])[0]
        except c.BridgeError:
            return None  # no resume to capture; the fresh capture's tests show why

    for provider, kind in (("codex", "exec"), ("claude", "json")):
        consult(f"{provider}-{kind}", provider)
        if session := session_of(f"{provider}-{kind}", provider):
            consult(f"{provider}-{kind}-resume", provider, session=session, prompt=FOLLOW_UP)
        consult(f"{provider}-{kind}-unknown-model", provider, model=UNKNOWN_MODEL)

    # The sign-in probes, in an empty folder as the bridge runs them; signed out in an empty home.
    for provider, command in (("claude", claude), ("codex", codex)):
        probe = [command, *SIGN_IN_PROBES[provider]]
        raw[f"{provider}-{'auth' if provider == 'claude' else 'login'}-status-signed-in"] = (
            provider, *run(probe, empty_repository(parent), environment))
        home = Path(tempfile.mkdtemp(dir=parent))
        bare = {"PATH": c.safe_path(os.environ.get("PATH")), "HOME": str(home), "CODEX_HOME": str(home)}
        raw[f"{provider}-{'auth' if provider == 'claude' else 'login'}-status-signed-out"] = (
            provider, *run(probe, home, bare))

    cache = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
    if cache.is_file():
        raw["codex-models-cache"] = ("codex", 0, cache.read_text(encoding="utf-8"), "")
    return raw


def main():
    versions = {name: run([executable(name), "--version"], ROOT, os.environ)[1].strip() for name in ("codex", "claude")}
    scrub = Scrubber()
    with tempfile.TemporaryDirectory(prefix="reweigh-capture-") as parent:
        raw = raw_captures(parent)
    signed_in = raw.get("claude-auth-status-signed-in", (None, None, "", ""))[2]
    try:
        email = json.loads(signed_in).get("email")
    except (ValueError, AttributeError):
        email = None
    tokens = {str(Path.home()), socket.gethostname(), email if isinstance(email, str) else "", *git_identity()}
    captures = {}
    for name, (provider, status, stdout, stderr) in sorted(raw.items()):
        captures[name] = {"cli_version": versions[provider], "exit_status": status, "stdout":
                          scrub.json_lines(stdout).split("\n"), "stderr": scrub.errors(stderr).split("\n")}
    files = {name: json.dumps(capture, indent=2, ensure_ascii=False) + "\n" for name, capture in captures.items()}
    leaks = {name: private_tokens_in(text, tokens) for name, text in files.items()}
    if any(leaks.values()):
        names = ", ".join(name for name, found in leaks.items() if found)
        raise Refused(f"Private text survived the scrub in {names}; nothing was written.")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for old in FIXTURES.glob("*.json"):
        old.unlink()
    for name, text in files.items():
        (FIXTURES / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"Wrote {len(files)} captures to {FIXTURES.relative_to(ROOT)} ({versions['codex']}, {versions['claude']}).")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as exc:
        sys.exit(str(exc))
