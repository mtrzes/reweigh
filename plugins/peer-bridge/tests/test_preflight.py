"""The pre-launch check: what it reads from each CLI's answers, and what it does when a name the bridge depends on is
gone. Fake CLIs here act out a rename; test_contracts runs the check against the installed CLIs."""

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import consultation  # noqa: E402  (the bridge's own modules, found through its script directory)
import preflight  # noqa: E402

c = consultation


async def plain_runner(command, stdin, directory, timeout, environment):
    """The check's runner without the guard: enough for fake CLIs that exit at once."""
    result = await asyncio.to_thread(subprocess.run, command, input=stdin.encode() if stdin else None, cwd=directory,
                                     env=environment, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# A Codex that knows every name the bridge passes except those in GONE, and accepts a wrong-type value for the keys
# in ACCEPTED; a Claude that reports the requested tools except those in DROPPED and may write a canary marker.
FAKE = textwrap.dedent('''\
    import json, sys
    from pathlib import Path
    GONE, ACCEPTED, DROPPED, MARKER, SIGNED_IN = {gone!r}, {accepted!r}, {dropped!r}, {marker!r}, {signed_in!r}
    calls = Path({calls!r})
    with calls.open("a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("fake 1.0"); sys.exit(0)
    if arguments[:2] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": SIGNED_IN}})); sys.exit(0)
    if arguments[-2:] == ["features", "list"]:
        values = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word in ("-c", "--disable")]
        if any(value in GONE for value in values):
            sys.exit(1)
        for value in values:
            if value.endswith("=1") and value[:-2] not in ACCEPTED:
                sys.exit(1)
        names = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word == "--disable"]
        names += [v[len("features."):].partition("=")[0] for v in values if v.startswith("features.")]
        for name in names:
            print(name, "stable", "false")
        sys.exit(0)
    if "--print" in arguments:
        if MARKER:
            Path(Path.cwd().parent / MARKER).touch()
        tools = [t for t in arguments[arguments.index("--tools") + 1].split(",") if t not in DROPPED]
        reported = ["Task" if t == "Agent" else t for t in tools]
        print(json.dumps({{"type": "system", "subtype": "init", "tools": reported}}))
        sys.exit(1)
    sys.exit(2)
''')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fake(self, gone=(), accepted=(), dropped=(), marker=None, signed_in=False):
        executable = self.root / f"fake-{len(list(self.root.iterdir()))}"
        self.calls = self.root / f"{executable.name}.calls"
        executable.write_text(f"#!{sys.executable}\n" + FAKE.format(
            gone=list(gone), accepted=list(accepted), dropped=list(dropped), marker=marker, signed_in=signed_in,
            calls=str(self.calls)))
        executable.chmod(0o700)
        return str(executable)

    def check(self, provider, command, gate=None):
        return asyncio.run((gate or preflight.Preflight())(provider, command, plain_runner))

    def codex(self, executable, **options):
        return c.build_command("codex", executable, "conv", reasoning_effort="high", **options)

    def claude(self, executable):
        return c.build_command("claude", executable, "conv", directories=("/repo",))

    def test_a_cli_that_knows_every_name_passes_without_warnings(self):
        self.assertEqual(self.check("codex", self.codex(self.fake())), [])
        self.assertEqual(self.check("codex", self.codex(self.fake(), subagents=False)), [])
        self.assertEqual(self.check("claude", self.claude(self.fake())), [])

    def test_a_lost_restriction_refuses_the_call_and_names_the_setting(self):
        cases = (
            ("codex", self.fake(gone=["hooks"]), "--disable hooks"),
            ("codex", self.fake(gone=["features.multi_agent_v2.disable_native_tools=true"]),
             "features.multi_agent_v2.disable_native_tools"),
            ("codex", self.fake(accepted=["shell_environment_policy.inherit"]), "shell_environment_policy.inherit"),
            ("claude", self.fake(marker="user-hook"), "disableAllHooks"),
            ("claude", self.fake(marker="project-hook"), "--setting-sources user"),
            ("claude", self.fake(marker="mcp"), "--strict-mcp-config"),
        )
        for provider, executable, setting in cases:
            command = self.codex(executable, subagents=False) if provider == "codex" else self.claude(executable)
            with self.subTest(setting=setting), self.assertRaisesRegex(c.BridgeError, "no longer accepts .*" + setting):
                self.check(provider, command)

    def test_a_lost_capability_only_warns(self):
        self.assertEqual(self.check("codex", self.codex(self.fake(accepted=["web_search", "model_reasoning_effort"]))),
                         [c.CAPABILITY_LOST.format(name="Codex", capability="cached web search"),
                          c.CAPABILITY_LOST.format(name="Codex", capability="the chosen reasoning effort")])
        self.assertEqual(self.check("claude", self.claude(self.fake(dropped=["WebSearch", "Agent"]))),
                         [c.CAPABILITY_LOST.format(name="Claude Code", capability=f"the {tool} tool")
                          for tool in ("WebSearch", "Agent")])

    def test_a_check_that_cannot_run_warns_instead_of_stopping(self):
        [warning] = self.check("claude", self.claude(self.fake(signed_in=True)))
        self.assertIn("could not be checked", warning)
        [warning] = self.check("codex", self.codex(str(self.root / "missing")))
        self.assertIn("could not be checked", warning)

    def test_a_verdict_is_reused_for_the_same_version_and_command_shape(self):
        gate, executable = preflight.Preflight(), self.fake(gone=["hooks"])
        counts = []
        for _ in range(2):
            with self.assertRaises(c.BridgeError):
                self.check("codex", self.codex(executable), gate)
            counts.append(len([line for line in self.calls.read_text().splitlines() if line != "--version"]))
        self.assertEqual(counts[0], counts[1])  # the second call only read the version

    def test_the_canary_command_keeps_the_restrictions_and_drops_the_session(self):
        command = c.build_command("claude", "/bin/claude", "conv", "session", model="opus", reasoning_effort="max",
                                  directories=("/repo",))
        canary = preflight.canary_command(command, Path("/check/project"))
        for kept in ("--settings", "--setting-sources", "--strict-mcp-config", "--tools", "--disallowed-tools"):
            self.assertIn(kept, canary)
        for dropped in ("--resume", "--name", "--model", "--effort", "/repo"):
            self.assertNotIn(dropped, canary)
        self.assertEqual(canary[-2:], ["--add-dir", "/check/project"])
        self.assertEqual(canary[canary.index("--output-format") + 1:canary.index("--output-format") + 3],
                         ["stream-json", "--verbose"])
