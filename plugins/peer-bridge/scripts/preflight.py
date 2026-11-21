"""Check, before a peer starts, that its CLI still knows every name the bridge's command depends on.

Each check runs the peer CLI in an empty temporary home with only PATH and HOME set, so it reads none of the user's
configuration and cannot run their hooks or MCP servers. It runs through the consultation's own guarded runner, so
its processes stop with the consultation. Verdicts are cached per CLI version, so the cost falls on the
first consultation after an upgrade: about 0.2 seconds for Codex and 2 seconds for Claude Code.
"""

import json
import os
import tempfile
from pathlib import Path

from consultation import (
    CAPABILITY_LOST, CLAUDE_CANARY_MARKERS, CODEX_CAPABILITY_KEYS, CODEX_FEATURE_PURPOSE, CODEX_RESTRICTION_KEYS,
    PROVIDER_NAMES, RESTRICTION_LOST, UNVERIFIED, BridgeError, claude_tool_names, codex_feature_names,
    codex_feature_stages, codex_key_names, missing_claude_tools, safe_path,
)

CHECK_SECONDS = 20
VERSION_SECONDS = 5


def bare_environment(home):
    return {"PATH": safe_path(os.environ.get("PATH")), "HOME": str(home)}


class Checker:
    """Runs check commands through runner(command, stdin, directory, timeout, environment), the consultation's
    guarded runner, which returns the exit status, standard output and standard error as bytes."""

    def __init__(self, runner):
        self.runner = runner

    async def __call__(self, command, home, cwd, stdin=None, timeout=CHECK_SECONDS):
        """Exit status and text output of a check command, or None when it could not run or timed out."""
        environment = bare_environment(home) | {"CODEX_HOME": str(home)}
        try:
            code, output, _ = await self.runner(command, stdin, Path(cwd), timeout, environment)
        except Exception:  # a check that cannot run never stops the consultation by itself  # noqa: BLE001
            return None
        return code, output.decode("utf-8", errors="replace")


async def version(run, executable):
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:
        result = await run([executable, "--version"], home, home, timeout=VERSION_SECONDS)
    return result[1].strip() if result and result[0] == 0 else None


async def codex_check(run, command):
    """The warnings for a Codex command; raises BridgeError when a restriction's name is gone."""
    executable, name = command[0], PROVIDER_NAMES["codex"]
    flags = command[1:command.index("exec")]
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:
        listed = await run([executable, *flags, "features", "list"], home, home)
        if listed is None:
            return [UNVERIFIED.format(name=name, reason="codex features list did not run")]
        if listed[0]:
            # Codex refuses an unknown --disable name or features key: find which, one at a time.
            for index, word in enumerate(flags[:-1]):
                if word in ("--disable", "-c"):
                    alone = await run([executable, word, flags[index + 1], "features", "list"], home, home)
                    if alone and alone[0]:
                        raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"{word} {flags[index + 1]}",
                                                                  purpose="restrict the peer"))
            return [UNVERIFIED.format(name=name, reason="codex refused the bridge's flags")]
        stages = codex_feature_stages(listed[1])
        for feature in codex_feature_names(flags):
            if feature not in stages or stages[feature] == "removed":
                raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"the feature {feature}",
                                                          purpose=CODEX_FEATURE_PURPOSE))
        warnings = []
        for key in codex_key_names(flags):
            restriction, capability = CODEX_RESTRICTION_KEYS.get(key), CODEX_CAPABILITY_KEYS.get(key)
            if not (restriction or capability):
                continue
            # A wrong type, which Codex refuses while it knows the key and accepts once it does not.
            canary = await run([executable, "-c", f"{key}=1", "features", "list"], home, home)
            if canary is None or canary[0]:
                continue
            if restriction:
                raise BridgeError(RESTRICTION_LOST.format(name=name, setting=f"-c {key}", purpose=restriction))
            warnings.append(CAPABILITY_LOST.format(name=name, capability=capability))
    return warnings


async def claude_check(run, command):
    """The warnings for a Claude command; raises BridgeError when a protection fails in a planted canary.

    Signed out in an empty home, Claude Code runs hooks and MCP servers and reports its tools before it stops for want
    of sign-in, with no model call. Each canary writes a marker only if its protection fails."""
    executable, name = command[0], PROVIDER_NAMES["claude"]
    with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as root:
        root = Path(root)
        home, project = root / "home", root / "project"
        (home / ".claude").mkdir(parents=True)
        (project / ".claude").mkdir(parents=True)

        def hook(marker):
            return {"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": f"/usr/bin/touch {root / marker}"}]}]}}

        (home / ".claude" / "settings.json").write_text(json.dumps(hook("user-hook")))
        (project / ".claude" / "settings.json").write_text(json.dumps(hook("project-hook")))
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"canary": {"command": "/usr/bin/touch", "args": [str(root / "mcp")]}}}))
        status = await run([executable, "auth", "status", "--json"], home, project)
        try:
            signed_out = status is not None and json.loads(status[1]).get("loggedIn") is False
        except (ValueError, AttributeError):
            signed_out = False
        if not signed_out:
            return [UNVERIFIED.format(name=name, reason="an empty home did not read as signed out")]
        canary = canary_command(command, project)
        result = await run(canary, home, project, stdin="check")
        for marker, (setting, purpose) in CLAUDE_CANARY_MARKERS.items():
            if (root / marker).exists():
                raise BridgeError(RESTRICTION_LOST.format(name=name, setting=setting, purpose=purpose))
        reported = init_tools(result[1] if result else "")
        if reported is None:
            return [UNVERIFIED.format(name=name, reason="Claude Code reported no tools")]
    return [CAPABILITY_LOST.format(name=name, capability=f"the {tool} tool")
            for tool in missing_claude_tools(claude_tool_names(command), reported)]


def canary_command(command, project):
    """The Claude command with streamed output, the canary project as its only directory, and no session to resume."""
    canary, skip = [], 0
    for index, word in enumerate(command):
        if skip:
            skip -= 1
            continue
        if word in ("--resume", "--name", "--add-dir", "--model", "--effort"):
            skip = 1
            continue
        if word == "--output-format":
            canary += ["--output-format", "stream-json", "--verbose"]
            skip = 1
            continue
        canary.append(word)
    return [*canary, "--add-dir", str(project)]


def init_tools(output):
    """The tools list of the system/init record in Claude Code's streamed output, or None without one."""
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "system" and record.get("subtype") == "init":
            tools = record.get("tools")
            return tools if isinstance(tools, list) else None
    return None


class Preflight:
    """Cached pre-launch checks: one verdict per CLI executable, version and command shape."""

    def __init__(self):
        self.verdicts = {}

    async def __call__(self, provider, command, runner):
        run = Checker(runner)
        shape = tuple(word for word in command[1:] if word.startswith("-") or "=" in word)
        key = (provider, command[0], await version(run, command[0]), shape)
        if key[2] is None:
            return [UNVERIFIED.format(name=PROVIDER_NAMES[provider], reason="its version could not be read")]
        if key not in self.verdicts:
            check = claude_check if provider == "claude" else codex_check
            try:
                self.verdicts[key] = ("ok", await check(run, command))
            except BridgeError as exc:
                self.verdicts[key] = ("refused", str(exc))
        kind, value = self.verdicts[key]
        if kind == "refused":
            raise BridgeError(value)
        return list(value)
