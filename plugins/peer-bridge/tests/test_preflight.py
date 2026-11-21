"""The Codex pre-launch check: what it reads from Codex's answers, and what it does when a name the bridge depends on
is gone. A fake Codex acts out each rename; test_contracts runs the check against the installed Codex."""

import asyncio
import json
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
    """The check's runner without the guard: enough for a fake Codex that exits at once."""
    result = await asyncio.to_thread(subprocess.run, command, cwd=directory, env=environment, capture_output=True,
                                     timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# A Codex that knows every name the bridge passes, except as SETTINGS says: `features list` exits 2 on a value in
# rejected; it lists each disabled feature as `stable false` unless rows says otherwise; it refuses a wrong-type
# canary with exit 1 unless its key is in renamed; `exec ... --help` exits 2 on an option in exec_gone; it lists
# nothing with empty_listing, and exits with a status on an argument named in crash.
FAKE = textwrap.dedent('''\
    import json, sys
    settings = json.loads({settings!r})
    with open(settings["calls"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    arguments = sys.argv[1:]
    for argument, status in settings.get("crash", {{}}).items():
        if argument in arguments:
            sys.exit(status)
    if arguments == ["--version"]:
        print(settings.get("version", "codex-cli 1.0")); sys.exit(0)
    if arguments[-2:] == ["features", "list"]:
        values = [arguments[i + 1] for i, word in enumerate(arguments[:-1])
                  if word in ("-c", "--disable", "--sandbox", "--ask-for-approval", "--model")]
        if any(value in settings.get("rejected", []) for value in values):
            sys.exit(2)
        for value in values:
            if value.endswith("=1") and value[:-2] not in settings.get("renamed", []):
                sys.exit(1)
        names = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word == "--disable"]
        names += [v[len("features."):].partition("=")[0] for v in values if v.startswith("features.")]
        if settings.get("empty_listing"):
            sys.exit(0)
        rows = settings.get("rows", {{}})
        print("image_generation  stable  true")
        for name in names:
            row = rows.get(name, "stable false")
            if row is not None:
                print(name, row)
        sys.exit(0)
    if arguments[:1] == ["exec"] and arguments[-1] == "--help":
        sys.exit(2 if any(option in settings.get("exec_gone", []) for option in arguments) else 0)
    sys.exit(2)
''')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fake(self, **settings):
        executable = self.root / f"codex-{len(list(self.root.glob('codex-*')))}"
        self.calls = self.root / f"{executable.name}.calls"
        self.calls.touch()
        self.write(executable, {"calls": str(self.calls), **settings})
        return str(executable)

    @staticmethod
    def write(executable, settings):
        Path(executable).write_text(f"#!{sys.executable}\n" + FAKE.format(settings=json.dumps(settings)))
        Path(executable).chmod(0o700)

    def command(self, executable, session=None, **options):
        return c.build_command("codex", executable, "conv", session, reasoning_effort="high", **options)

    def check(self, command, gate=None, provider="codex", runner=plain_runner):
        return asyncio.run((gate or preflight.Preflight())(provider, command, runner))

    def test_a_codex_that_knows_every_name_passes(self):
        for options in ({}, {"subagents": False}):
            with self.subTest(options=options):
                self.assertEqual(self.check(self.command(self.fake(), **options)), [])
        self.assertEqual(self.check(self.command(self.fake(), "session")), [])  # a follow-up's resume options

    def test_a_claude_command_is_not_checked_at_runtime(self):
        self.assertEqual(self.check(["/nonexistent/claude", "--print"], provider="claude"), [])

    def test_a_lost_restriction_refuses_the_call_and_names_it(self):
        mcp = ["-c", 'mcp_servers."docs".enabled=false']
        cases = (
            ({"rejected": ["hooks"]}, (), "Codex no longer accepts --disable hooks"),
            ({"rejected": ["read-only"]}, (), "Codex no longer accepts --sandbox read-only"),
            ({"rejected": ["features.multi_agent_v2.disable_native_tools=true"]}, (),
             "Codex no longer accepts -c features.multi_agent_v2.disable_native_tools=true"),
            ({"renamed": ["shell_environment_policy.inherit"]}, (), "Codex no longer accepts -c "
             "shell_environment_policy.inherit, which the bridge passes to keep the peer's commands to the core"),
            ({"renamed": ["web_search"]}, (), "Codex no longer accepts -c web_search, which the bridge passes to "
             "limit the peer's web search to OpenAI's index"),
            ({"renamed": ["mcp_servers.peer-bridge-canary.enabled"]}, mcp, "-c mcp_servers.enabled"),
            ({"rows": {"hooks": "stable true"}}, (), "Codex no longer accepts the feature hooks"),
            ({"rows": {"apps": "removed false"}}, (), "Codex no longer accepts the feature apps"),
            ({"rows": {"plugins": None}}, (), "Codex no longer accepts the feature plugins"),
            ({"exec_gone": ["--ignore-rules"]}, (), "Codex no longer accepts codex exec --json "
             "--ignore-user-config --ignore-rules"),
        )
        for settings, extra, message in cases:
            command = self.command(self.fake(**settings), subagents=False)
            command[command.index("exec"):command.index("exec")] = list(extra)
            with self.subTest(message=message):
                with self.assertRaises(c.BridgeError) as refused:
                    self.check(command)
                self.assertIn(message, str(refused.exception))
                self.assertIn("The consultation was not started", str(refused.exception))

    def test_a_lost_capability_warns_and_leaves_the_command_alone(self):
        command = self.command(self.fake(renamed=["model_reasoning_effort"]))
        before = list(command)
        self.assertEqual(self.check(command), [
            "Codex no longer accepts the chosen reasoning effort under that name; the peer ran without it."])
        self.assertEqual(command, before)

    def test_a_check_without_a_verdict_warns_and_is_tried_again(self):
        unread = ("Codex's restrictions could not be checked before the start (", "); the peer ran unchecked.")
        cases = (
            (str(self.root / "missing"), "missing did not run"),
            (self.fake(crash={"web_search=1": 139}), "a check ended with status 139"),
            (self.fake(empty_listing=True), "codex features list printed no rows the check can read"),
            (self.fake(crash={"--version": 1}), "its version could not be read"),
        )
        for executable, reason in cases:
            gate = preflight.Preflight()
            with self.subTest(reason=reason):
                for _ in range(2):
                    self.assertEqual(self.check(self.command(executable), gate), [reason.join(unread)])
                self.assertEqual(gate.verdicts, {})

    def test_a_verdict_is_reused_for_one_version_and_command_shape_only(self):
        settings = {"rejected": ["hooks"]}
        gate, executable = preflight.Preflight(), self.fake(**settings)

        def checks():
            return len([line for line in self.calls.read_text().splitlines() if line != "--version"])

        counts = []
        for _ in range(2):
            with self.assertRaises(c.BridgeError):
                self.check(self.command(executable), gate)
            counts.append(checks())
        self.assertEqual(counts[0], counts[1])  # the second call only read the version
        with self.assertRaises(c.BridgeError):
            self.check(self.command(executable, "session"), gate)  # a follow-up has its own shape
        self.assertGreater(checks(), counts[1])
        # Another version at the same path is checked afresh.
        before = checks()
        self.write(executable, {"calls": str(self.calls), **settings, "version": "codex-cli 2.0"})
        with self.assertRaises(c.BridgeError):
            self.check(self.command(executable), gate)
        self.assertGreater(checks(), before)


class PreflightRuleTests(unittest.TestCase):
    def test_feature_rows_keep_the_stage_and_the_state(self):
        listing = "apps     stable     false\nmulti_agent  deprecated  true\nodd under development false\n"
        rows = c.codex_feature_rows(listing)
        self.assertEqual(rows, {"apps": ("stable", "false"), "multi_agent": ("deprecated", "true"),
                                "odd": ("under development", "false")})
        self.assertTrue(c.codex_feature_off("apps", rows))
        self.assertFalse(c.codex_feature_off("multi_agent", rows))
        self.assertFalse(c.codex_feature_off("absent", rows))
        self.assertFalse(c.codex_feature_off("x", {"x": ("removed", "false")}))

    def test_the_names_and_options_a_command_passes(self):
        flags = ["--no-daemon", "--sandbox", "read-only", "--disable", "hooks",
                 "-c", "features.skill_mcp_dependency_install=false",
                 "-c", "features.multi_agent_v2.disable_native_tools=true", "-c", 'mcp_servers."a".enabled=false',
                 "-c", 'mcp_servers."b".enabled=false', "-c", 'web_search="cached"']
        self.assertEqual(c.codex_feature_names(flags), ["hooks", "skill_mcp_dependency_install"])
        self.assertEqual(c.codex_key_names(flags), ["features.skill_mcp_dependency_install",
                                                    "features.multi_agent_v2.disable_native_tools",
                                                    "mcp_servers.enabled", "web_search"])
        self.assertEqual(c.codex_options(flags)[:3], [("--no-daemon",), ("--sandbox", "read-only"),
                                                      ("--disable", "hooks")])
