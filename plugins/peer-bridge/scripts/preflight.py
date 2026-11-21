"""Check, before a peer starts, that its CLI still honours every name the bridge's command depends on.

Each check runs the peer CLI in an empty temporary home with only PATH and HOME set, so it reads none of the user's
configuration and cannot run their hooks or MCP servers. It runs through the consultation's own guarded runner, so
its processes stop with the consultation. A conclusive verdict is kept for the life of the server process, per CLI
executable, version and command shape; a check that could not reach one is repeated next time.

A check returns its warnings and the command to run: a lost restriction raises BridgeError, a lost capability is
dropped from the command with a warning, and a check that cannot reach a verdict warns and keeps the command.
"""

import json
import os
import shlex
import tempfile
from pathlib import Path

from consultation import (
    CAPABILITY_LOST, CLAUDE_CANARY_MARKERS, CLAUDE_CANARY_MODEL, CLAUDE_PROJECT_SETTINGS, CLAUDE_TOOLS_PURPOSE,
    CODEX_CAPABILITY_KEYS, CODEX_EXEC_PURPOSE, CODEX_FEATURE_PURPOSE, CODEX_RESTRICTION_KEYS, PROVIDER_NAMES,
    RESTRICTION_LOST, UNVERIFIED, BridgeError, claude_tool_names, codex_feature_names, codex_feature_off,
    codex_feature_rows, codex_key_names, missing_claude_tools, safe_path, unexpected_claude_tools, without_option,
    without_setting,
)

CHECK_SECONDS = 20
VERSION_SECONDS = 5
# Wrong-type values: Codex refuses each while it knows the key, and accepts it silently once it does not.
CODEX_CANARIES = {
    "mcp_servers.enabled": ("-c", 'mcp_servers.peer-bridge-canary={command="/usr/bin/true"}',
                            "-c", "mcp_servers.peer-bridge-canary.enabled=1"),
}


class Inconclusive(Exception):
    """A check command could not run, or gave no answer the check can read."""


class Checker:
    """Runs check commands through runner(command, stdin, directory, timeout, environment), the consultation's
    guarded runner, which returns the exit status, standard output and standard error as bytes."""

    def __init__(self, runner):
        self.runner = runner

    async def __call__(self, command, home, cwd, stdin=None, timeout=CHECK_SECONDS):
        """Exit status and text output of a check command; Inconclusive when it could not run or timed out."""
        environment = {"PATH": safe_path(os.environ.get("PATH")), "HOME": str(home), "CODEX_HOME": str(home)}
        try:
            code, output, _ = await self.runner(command, stdin, Path(cwd), timeout, environment)
        except Exception as exc:  # a check command that fails to run never stops the consultation by itself
            raise Inconclusive(f"{Path(command[0]).name} did not run") from exc
        return code, output.decode("utf-8", errors="replace")


async def version(run, executable):
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:
        code, output = await run([executable, "--version"], home, home, timeout=VERSION_SECONDS)
    if code or not output.strip():
        raise Inconclusive("its version could not be read")
    return output.strip()


async def codex_check(run, command):
    """The warnings and the command to run for a Codex consultation."""
    executable, name = command[0], PROVIDER_NAMES["codex"]
    exec_at = command.index("exec")
    warnings = []
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:

        async def accepted(*arguments):
            code, output = await run([executable, *arguments], home, home)
            return code == 0, output

        flags = command[1:exec_at]
        ok, listing = await accepted(*flags, "features", "list")
        if not ok:
            # Codex refuses an unknown --disable name, or a value it does not take: find which, one at a time.
            for index, word in enumerate(flags[:-1]):
                if word not in ("--disable", "-c"):
                    continue
                if (await accepted(word, flags[index + 1], "features", "list"))[0]:
                    continue
                key = flags[index + 1].partition("=")[0]
                if word == "-c" and key in CODEX_CAPABILITY_KEYS:
                    command = without_setting(command, key)
                    warnings.append(CAPABILITY_LOST.format(name=name, capability=CODEX_CAPABILITY_KEYS[key]))
                    continue
                raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"{word} {flags[index + 1]}",
                                                          purpose="restrict the peer"))
            exec_at = command.index("exec")
            flags = command[1:exec_at]
            ok, listing = await accepted(*flags, "features", "list")
            if not ok:
                raise Inconclusive("codex refused the bridge's flags together but each alone")
        rows = codex_feature_rows(listing)
        for feature in codex_feature_names(flags):
            if not codex_feature_off(feature, rows):
                raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"the feature {feature}",
                                                          purpose=CODEX_FEATURE_PURPOSE))
        for key in codex_key_names(flags):
            restriction, capability = CODEX_RESTRICTION_KEYS.get(key), CODEX_CAPABILITY_KEYS.get(key)
            if not (restriction or capability):
                continue
            if (await accepted(*CODEX_CANARIES.get(key, ("-c", f"{key}=1")), "features", "list"))[0]:
                if restriction:
                    raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"-c {key}", purpose=restriction))
                command = without_setting(command, key)
                warnings.append(CAPABILITY_LOST.format(name=name, capability=capability))
        # The exec options, without a resumed session's ID or the prompt, followed by --help, which fails on an
        # unknown option.
        options = [word for word in command[command.index("exec") + 1:] if word.startswith("--")]
        if not (await accepted("exec", *options, "--help"))[0]:
            raise BridgeError(RESTRICTION_LOST.format(name=name, setting="codex exec " + " ".join(options),
                                                      purpose=CODEX_EXEC_PURPOSE))
    return warnings, command


async def claude_check(run, command):
    """The warnings and the command to run for a Claude consultation.

    Signed out in an empty home, Claude Code runs hooks and MCP servers and reports system/init before it stops for
    want of sign-in, with no model call. A planted user hook and stdio MCP server each write a marker only if their
    protection fails; the planted repository's settings name a model that system/init reports only if they load."""
    executable, name = command[0], PROVIDER_NAMES["claude"]
    warnings = []
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as root:
        root = Path(root)
        home, project = root / "home", root / "project"
        (home / ".claude").mkdir(parents=True)
        (project / ".claude").mkdir(parents=True)
        touch = {marker: f"/usr/bin/touch {shlex.quote(str(root / marker))}" for marker in CLAUDE_CANARY_MARKERS}
        (home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": touch["user-hook"]}]}]}}))
        (project / ".claude" / "settings.json").write_text(json.dumps({"model": CLAUDE_CANARY_MODEL}))
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"canary": {"command": "/usr/bin/touch", "args": [str(root / "mcp")]}}}))
        _, status = await run([executable, "auth", "status", "--json"], home, project)
        if signed_in(status) is not False:
            raise Inconclusive("an empty home did not read as signed out")

        async def init(canary):
            _, output = await run(canary, home, project, stdin="check")
            for marker, (setting, purpose) in CLAUDE_CANARY_MARKERS.items():
                if (root / marker).exists():
                    raise BridgeError(RESTRICTION_LOST.format(name=name, setting=setting, purpose=purpose))
            return init_record(output)

        record = await init(canary_command(command, project, model=False))
        if record is None and "--effort" in command:
            record = await init(canary_command(without_option(command, "--effort"), project, model=False))
            if record is not None:
                command = without_option(command, "--effort")
                warnings.append(CAPABILITY_LOST.format(name=name, capability="the chosen reasoning effort"))
        if record is None:
            raise Inconclusive("Claude Code reported no system/init record")
        if record.get("model") == CLAUDE_CANARY_MODEL:
            raise BridgeError(RESTRICTION_LOST.format(name=name, setting=CLAUDE_PROJECT_SETTINGS[0],
                                                      purpose=CLAUDE_PROJECT_SETTINGS[1]))
        reported = record.get("tools")
        if not isinstance(reported, list) or not all(isinstance(tool, str) for tool in reported):
            raise Inconclusive("Claude Code's system/init record lists no tools")
        requested = claude_tool_names(command)
        if unexpected_claude_tools(requested, reported):
            raise BridgeError(RESTRICTION_LOST.format(name=name, setting="--tools", purpose=CLAUDE_TOOLS_PURPOSE))
        warnings += [CAPABILITY_LOST.format(name=name, capability=f"the {tool} tool")
                     for tool in missing_claude_tools(requested, reported)]
        if "--model" in command and await init(canary_command(command, project, model=True)) is None:
            command = without_option(command, "--model")
            warnings.append(CAPABILITY_LOST.format(name=name, capability="the chosen model"))
    return warnings, command


def signed_in(output):
    """The exact loggedIn value of `claude auth status --json`, or None when it is not a boolean."""
    try:
        value = json.loads(output).get("loggedIn")
    except (ValueError, AttributeError, RecursionError):
        return None
    return value if isinstance(value, bool) else None


def canary_command(command, project, model):
    """The Claude command with streamed output, the canary repository as its only directory, no session to resume,
    and its --model only when model is true: a --model would hide which model the repository's settings chose."""
    canary, skip = [], 0
    for word in command:
        if skip:
            skip -= 1
            continue
        if word in ("--resume", "--name", "--add-dir") or (word == "--model" and not model):
            skip = 1
            continue
        if word == "--output-format":
            canary += ["--output-format", "stream-json", "--verbose"]
            skip = 1
            continue
        canary.append(word)
    return [*canary, "--add-dir", str(project)]


def init_record(output):
    """The system/init record in Claude Code's streamed output, or None without one."""
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
            return record
    return None


def shape(provider, command):
    """What decides a verdict: every option and its value, without a session's ID, the conversation's name, the
    consulted directories or the handoff."""
    if provider == "claude":
        return tuple(canary_command(command, Path("/"), model=True))
    exec_at = command.index("exec")
    return (*command[1:exec_at], *(word for word in command[exec_at:] if word.startswith("--")))


class Preflight:
    """Cached pre-launch checks."""

    def __init__(self):
        self.verdicts = {}

    async def __call__(self, provider, command, runner):
        """The warnings and the command to run; raises BridgeError when a restriction's name is gone."""
        run, name = Checker(runner), PROVIDER_NAMES[provider]
        try:
            key = (provider, command[0], await version(run, command[0]), shape(provider, command))
            if key not in self.verdicts:
                check = claude_check if provider == "claude" else codex_check
                try:
                    warnings, checked = await check(run, command)
                    self.verdicts[key] = ("checked", warnings, checked)
                except BridgeError as exc:
                    self.verdicts[key] = ("refused", str(exc), None)
        except (Inconclusive, OSError, ValueError, UnicodeError, RecursionError) as exc:
            reason = str(exc) if isinstance(exc, Inconclusive) else "the check failed"
            return [UNVERIFIED.format(name=name, reason=reason)], command
        kind, value, checked = self.verdicts[key]
        if kind == "refused":
            raise BridgeError(value)
        # The checked command differs from this one only in what shape() leaves out: drop what the check dropped.
        dropped = [option for option in ("--model", "--effort") if option in command and option not in checked]
        dropped_keys = [word for word in command if word.partition("=")[0] in CODEX_CAPABILITY_KEYS
                        and word not in checked]
        for option in dropped:
            command = without_option(command, option)
        for setting in dropped_keys:
            command = without_setting(command, setting.partition("=")[0])
        return list(value), command
