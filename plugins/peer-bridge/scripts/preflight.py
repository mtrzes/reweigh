"""Check, before a Codex peer starts, that Codex still honours every name the bridge's command depends on.

Codex accepts an unknown `-c` key silently, so a renamed key would drop a restriction without a sound. The check runs
Codex in an empty temporary home with only PATH and HOME set, so it reads none of the user's configuration, through
the consultation's own guarded runner, so its processes stop with the consultation. It never changes the command. A
lost restriction raises BridgeError; a lost capability, and a check that cannot reach a verdict, become warnings. A
conclusive verdict is kept for the life of the server process, per executable, version and command shape.

Claude Code is checked at release only, by test_contracts: its check needs a signed-out canary run of two seconds.
"""

import os
import tempfile
from pathlib import Path

from consultation import (
    CAPABILITY_LOST, CODEX_CAPABILITY_KEYS, CODEX_EXEC_PURPOSE, CODEX_FEATURE_PURPOSE, CODEX_RESTRICTION_KEYS,
    RESTRICTION_LOST, UNVERIFIED, BridgeError, codex_feature_names, codex_feature_off, codex_feature_rows,
    codex_key_names, codex_options, safe_path,
)

NAME = "Codex"
CHECK_SECONDS = 20
VERSION_SECONDS = 5
# Codex exits 2 for an unknown option or value and 1 for configuration it cannot load; any other failure, such as a
# signal, says nothing about the names.
REJECTED = (1, 2)
# Wrong-type values: Codex refuses each while it knows the key, and accepts it silently once it does not.
CODEX_CANARIES = {
    "mcp_servers.enabled": ("-c", 'mcp_servers.peer-bridge-canary={command="/usr/bin/true"}',
                            "-c", "mcp_servers.peer-bridge-canary.enabled=1"),
}


class Inconclusive(Exception):
    """A check command could not run, or gave an answer the check cannot read."""


async def answer(runner, command, home, timeout=CHECK_SECONDS):
    """Whether Codex accepted a check command (exit 0) or rejected it (REJECTED), and its output."""
    environment = {"PATH": safe_path(os.environ.get("PATH")), "HOME": str(home), "CODEX_HOME": str(home)}
    try:
        code, output, _ = await runner(command, None, Path(home), timeout, environment)
    except Exception as exc:  # a check command that fails to run never stops the consultation by itself
        raise Inconclusive(f"{Path(command[0]).name} did not run") from exc
    if code != 0 and code not in REJECTED:
        raise Inconclusive(f"a check ended with status {code}")
    return code == 0, output.decode("utf-8", errors="replace")


async def codex_check(runner, command, home):
    """The warnings for a Codex command; raises BridgeError when a restriction's name is gone."""
    executable, exec_at = command[0], command.index("exec")
    flags = command[1:exec_at]

    async def accepted(*arguments):
        return await answer(runner, [executable, *arguments], home)

    ok, listing = await accepted(*flags, "features", "list")
    if not ok:
        # Codex refused an option or value: find which, one at a time.
        for option in codex_options(flags):
            if not (await accepted(*option, "features", "list"))[0]:
                raise BridgeError(RESTRICTION_LOST.format(name=NAME, setting=" ".join(option),
                                                          purpose="restrict the peer"))
        raise Inconclusive("Codex refused the bridge's options together but each alone")
    rows = codex_feature_rows(listing)
    if not rows or any(state not in ("true", "false") for _, state in rows.values()):
        raise Inconclusive("codex features list printed no rows the check can read")
    for feature in codex_feature_names(flags):
        if not codex_feature_off(feature, rows):
            raise BridgeError(RESTRICTION_LOST.format(name=NAME, setting=f"the feature {feature}",
                                                      purpose=CODEX_FEATURE_PURPOSE))
    warnings = []
    for key in codex_key_names(flags):
        restriction, capability = CODEX_RESTRICTION_KEYS.get(key), CODEX_CAPABILITY_KEYS.get(key)
        if (restriction or capability) and (await accepted(*CODEX_CANARIES.get(key, ("-c", f"{key}=1")),
                                                           "features", "list"))[0]:
            if restriction:
                raise BridgeError(RESTRICTION_LOST.format(name=NAME, setting=f"-c {key}", purpose=restriction))
            warnings.append(CAPABILITY_LOST.format(name=NAME, capability=capability))
    # The exec options, in their subcommand, with --help, which fails on an unknown one.
    suffix = command[exec_at:]
    probe = [word for word in suffix if word.startswith("--") or word in ("exec", "resume")]
    if not (await accepted(*probe, "--help"))[0]:
        raise BridgeError(RESTRICTION_LOST.format(name=NAME, setting="codex " + " ".join(probe),
                                                  purpose=CODEX_EXEC_PURPOSE))
    return warnings


def shape(command):
    """What decides a verdict: every option and value, and whether it resumes, without a session's ID or the prompt."""
    exec_at = command.index("exec")
    return (*command[1:exec_at], *(word for word in command[exec_at:] if word.startswith("--") or word == "resume"))


class Preflight:
    """Cached pre-launch checks of Codex commands; a Claude command is not checked at runtime."""

    def __init__(self):
        self.verdicts = {}

    async def __call__(self, provider, command, runner):
        """The warnings for the command; raises BridgeError when a restriction's name is gone."""
        if provider != "codex":
            return []
        try:
            with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:
                ok, version = await answer(runner, [command[0], "--version"], home, VERSION_SECONDS)
                if not ok or not version.strip():
                    raise Inconclusive("its version could not be read")
                key = (command[0], version.strip(), shape(command))
                if key not in self.verdicts:
                    try:
                        self.verdicts[key] = (None, await codex_check(runner, command, home))
                    except BridgeError as exc:
                        self.verdicts[key] = (str(exc), [])
        except (Inconclusive, OSError, ValueError, UnicodeError) as exc:
            reason = str(exc) if isinstance(exc, Inconclusive) else "the check failed"
            return [UNVERIFIED.format(name=NAME, reason=reason)]
        refusal, warnings = self.verdicts[key]
        if refusal:
            raise BridgeError(refusal)
        return list(warnings)
