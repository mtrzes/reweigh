"""Check, before a Codex peer starts, that Codex still honours every name the bridge's command depends on.

Codex accepts an unknown `-c` key silently, so a renamed key would drop a restriction without a sound. The check runs
Codex in an empty temporary home with only PATH and HOME set, through the consultation's own guarded runner, and
never changes the command. A lost restriction raises BridgeError; a check that cannot reach a verdict warns, and the
peer starts. Claude Code is checked by test_contracts in the release run, by hand.
"""

import os
import tempfile
from pathlib import Path

from consultation import (
    CODEX_FEATURE_PURPOSE, CODEX_RESTRICTION_KEYS, RESTRICTION_LOST, UNVERIFIED, BridgeError, codex_feature_names,
    codex_feature_rows, codex_key_names, safe_path,
)

NAME = "Codex"
CHECK_SECONDS = 20
# Codex exits 2 for an unknown option or value and 1 for configuration it cannot load; any other status, such as a
# signal, says nothing about the names.
REJECTED = (1, 2)
# Wrong-type values: Codex refuses each while it knows the key, and accepts it silently once it does not.
CANARIES = {"mcp_servers.enabled": ("-c", 'mcp_servers.peer-bridge-canary={command="/usr/bin/true"}',
                                    "-c", "mcp_servers.peer-bridge-canary.enabled=1")}


class Inconclusive(Exception):
    """A check command could not run, or gave an answer the check cannot read."""


def lost(setting, purpose):
    return BridgeError(RESTRICTION_LOST.format(name=NAME, setting=setting, purpose=purpose))


async def codex_check(runner, command, home):
    """Raise BridgeError when a restriction's name is gone, Inconclusive when the check cannot tell."""
    environment = {"PATH": safe_path(os.environ.get("PATH")), "HOME": str(home), "CODEX_HOME": str(home)}

    async def accepted(*arguments):
        try:
            code, output, _ = await runner([command[0], *arguments], None, Path(home), CHECK_SECONDS, environment)
        except Exception as exc:  # a check command that fails to run never stops the consultation by itself
            raise Inconclusive("Codex did not run") from exc
        if code != 0 and code not in REJECTED:
            raise Inconclusive(f"a check ended with status {code}")
        return code == 0, output.decode("utf-8", errors="replace")

    flags = command[1:command.index("exec")]
    ok, listing = await accepted(*flags, "features", "list")
    if not ok:
        if (await accepted("features", "list"))[0]:
            raise lost("one of the command's options", "restrict the peer or set the chosen model and effort")
        raise Inconclusive("codex features list failed without the bridge's options too")
    rows = codex_feature_rows(listing)
    unread = not any(state in ("true", "false") for _, state in rows.values())
    for feature in codex_feature_names(flags):
        stage, state = rows.get(feature, (None, None))
        # Removed or on is a loss in any listing; absent only in one the check can read.
        if stage == "removed" or state == "true" or (stage is None and not unread):
            raise lost(f"the feature {feature}", CODEX_FEATURE_PURPOSE)
        unread = unread or state != "false"
    # The MCP key is checked whether or not this command names a server: a renamed table finds none to switch off.
    for key in dict.fromkeys([*codex_key_names(flags), "mcp_servers.enabled"]):
        purpose = CODEX_RESTRICTION_KEYS.get(key)
        if purpose and (await accepted(*CANARIES.get(key, ("-c", f"{key}=1")), "features", "list"))[0]:
            raise lost(f"-c {key}", purpose)
    if unread:  # only now: every other probe has had its chance to prove a restriction lost
        raise Inconclusive("codex features list printed rows the check cannot read")


async def preflight(provider, command, runner):
    """The warnings for a consultation's command; raises BridgeError when a Codex restriction's name is gone."""
    if provider != "codex":
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="peer-bridge-check-") as home:
            await codex_check(runner, command, home)
    except (Inconclusive, OSError, ValueError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, Inconclusive) else "the check failed"
        return [UNVERIFIED.format(name=NAME, reason=reason)]
    return []
