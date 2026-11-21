"""The pre-launch check: what it reads from each CLI's answers, and what it does when a name the bridge depends on is
gone. Fake CLIs here act out each rename; test_contracts runs the check against the installed CLIs."""

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
    """The check's runner without the guard: enough for fake CLIs that exit at once."""
    result = await asyncio.to_thread(subprocess.run, command, input=stdin.encode() if stdin else None, cwd=directory,
                                     env=environment, capture_output=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# A CLI that knows every name the bridge passes, except as SETTINGS says. Codex: `features list` fails on a value in
# rejected, lists each disabled feature as `stable false` unless rows says otherwise, and refuses a wrong-type canary
# unless its key is in renamed; `exec ... --help` fails on an option in exec_gone. Claude: reports the requested tools,
# without dropped and with extra, and no init at all when an option in no_init is passed; it may write a canary
# marker or report the repository's canary model. hang names an argument on which it never answers.
FAKE = textwrap.dedent('''\
    import json, sys, time
    from pathlib import Path
    settings = json.loads({settings!r})
    with open(settings["calls"], "a") as log:
        log.write(" ".join(sys.argv[1:]) + "\\n")
    arguments = sys.argv[1:]
    if settings.get("hang") in arguments:
        time.sleep(60)
    if arguments == ["--version"]:
        print(settings.get("version", "fake 1.0")); sys.exit(0)
    if arguments[:2] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": settings.get("signed_in", False)}})); sys.exit(0)
    if arguments[-2:] == ["features", "list"]:
        values = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word in ("-c", "--disable")]
        if any(value in settings.get("rejected", []) for value in values):
            sys.exit(1)
        for value in values:
            if value.endswith("=1") and value[:-2] not in settings.get("renamed", []):
                sys.exit(1)
        names = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word == "--disable"]
        names += [v[len("features."):].partition("=")[0] for v in values if v.startswith("features.")]
        rows = settings.get("rows", {{}})
        for name in names:
            row = rows.get(name, "stable false")
            if row is not None:
                print(name, row)
        sys.exit(0)
    if arguments[:1] == ["exec"] and arguments[-1] == "--help":
        sys.exit(2 if any(option in settings.get("exec_gone", []) for option in arguments) else 0)
    if "--print" in arguments:
        if any(option in arguments for option in settings.get("no_init", [])):
            sys.exit(1)
        if settings.get("marker"):
            (Path.cwd().parent / settings["marker"]).touch()  # beside the check's canary repository
        requested = arguments[arguments.index("--tools") + 1].split(",")
        tools = [t for t in requested if t not in settings.get("dropped", [])] + settings.get("extra", [])
        model = "peer-bridge-canary-model" if settings.get("project_model") else "fake-model"
        print(json.dumps({{"type": "system", "subtype": "init", "model": model,
                           "tools": ["Task" if t == "Agent" else t for t in tools]}}))
        sys.exit(1)
    sys.exit(2)
''')


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fake(self, **settings):
        executable = self.root / f"fake-{len(list(self.root.glob('fake-*')))}"
        self.calls = self.root / f"{executable.name}.calls"
        self.calls.touch()
        executable.write_text(f"#!{sys.executable}\n" + FAKE.format(
            settings=json.dumps({"calls": str(self.calls), **settings})))
        executable.chmod(0o700)
        return str(executable)

    def check(self, provider, command, gate=None, runner=plain_runner):
        return asyncio.run((gate or preflight.Preflight())(provider, command, runner))

    def codex(self, executable, **options):
        return c.build_command("codex", executable, "conv", reasoning_effort="high", **options)

    def claude(self, executable, **options):
        return c.build_command("claude", executable, "conv", directories=("/repo",), **options)

    def test_a_cli_that_knows_every_name_passes_unchanged(self):
        commands = (("codex", self.codex(self.fake())), ("codex", self.codex(self.fake(), subagents=False)),
                    ("claude", self.claude(self.fake(), model="opus", reasoning_effort="max")))
        for provider, command in commands:
            with self.subTest(command=command[1:4]):
                self.assertEqual(self.check(provider, command), ([], command))

    def test_a_lost_restriction_refuses_the_call_and_names_the_setting(self):
        mcp = ["-c", 'mcp_servers."docs".enabled=false']
        cases = (
            ("codex", self.fake(rejected=["hooks"]), (), "--disable hooks"),
            ("codex", self.fake(rejected=["features.multi_agent_v2.disable_native_tools=true"]), (),
             "features.multi_agent_v2.disable_native_tools"),
            ("codex", self.fake(renamed=["shell_environment_policy.inherit"]), (), "shell_environment_policy.inherit"),
            ("codex", self.fake(renamed=["mcp_servers.peer-bridge-canary.enabled"]), mcp, "mcp_servers.enabled"),
            ("codex", self.fake(rows={"hooks": "stable true"}), (), "the feature hooks"),
            ("codex", self.fake(rows={"apps": "removed false"}), (), "the feature apps"),
            ("codex", self.fake(rows={"plugins": None}), (), "the feature plugins"),
            ("codex", self.fake(exec_gone=["--ignore-rules"]), (), "codex exec"),
            ("claude", self.fake(marker="user-hook"), (), "disableAllHooks"),
            ("claude", self.fake(marker="mcp"), (), "--strict-mcp-config"),
            ("claude", self.fake(project_model=True), (), "--setting-sources user"),
            ("claude", self.fake(extra=["Bash"]), (), "--tools"),
        )
        for provider, executable, extra, setting in cases:
            if provider == "codex":
                command = self.codex(executable, subagents=False)
                command[command.index("exec"):command.index("exec")] = list(extra)
            else:
                command = self.claude(executable)
            with self.subTest(setting=setting), self.assertRaisesRegex(c.BridgeError, "no longer accepts .*" + setting):
                self.check(provider, command)

    def test_sub_agents_switched_off_refuse_a_reported_task_tool(self):
        command = self.claude(self.fake(extra=["Task"]), subagents=False)
        with self.assertRaisesRegex(c.BridgeError, "--tools"):
            self.check("claude", command)

    def test_a_lost_capability_is_dropped_with_a_warning(self):
        codex = self.codex(self.fake(renamed=["web_search", "model_reasoning_effort"]))
        warnings, command = self.check("codex", codex)
        self.assertEqual(warnings, [c.CAPABILITY_LOST.format(name="Codex", capability="cached web search"),
                                    c.CAPABILITY_LOST.format(name="Codex", capability="the chosen reasoning effort")])
        self.assertEqual(command, c.without_setting(c.without_setting(codex, "web_search"), "model_reasoning_effort"))
        # A known key whose value Codex no longer takes is dropped the same way.
        warnings, command = self.check("codex", self.codex(self.fake(rejected=['web_search="cached"'])))
        self.assertEqual(len(warnings), 1)
        self.assertNotIn('web_search="cached"', command)
        claude = self.claude(self.fake(dropped=["WebSearch", "Agent"], no_init=["--effort", "--model"]),
                             model="opus", reasoning_effort="max")
        warnings, command = self.check("claude", claude)
        self.assertEqual(warnings, [c.CAPABILITY_LOST.format(name="Claude Code", capability=name) for name in
                                    ("the chosen reasoning effort", "the WebSearch tool", "the Agent tool",
                                     "the chosen model")])
        self.assertEqual(command, c.without_option(c.without_option(claude, "--effort"), "--model"))

    def test_a_check_without_a_verdict_warns_and_is_tried_again(self):
        cases = (
            ("claude", self.claude(self.fake(signed_in=True))),
            ("codex", self.codex(str(self.root / "missing"))),
            ("codex", self.codex(self.fake(hang="web_search=1"))),
        )
        for provider, command in cases:
            gate = preflight.Preflight()

            async def quick(command, stdin, directory, timeout, environment):
                return await plain_runner(command, stdin, directory, min(timeout, 2), environment)

            with self.subTest(command=command[0]):
                for _ in range(2):
                    warnings, checked = self.check(provider, command, gate, quick)
                    self.assertEqual(len(warnings), 1)
                    self.assertIn("could not be checked", warnings[0])
                    self.assertEqual(checked, command)
                self.assertEqual(gate.verdicts, {})

    def test_a_verdict_is_reused_only_for_the_same_version_and_command_shape(self):
        gate, executable = preflight.Preflight(), self.fake(rejected=["hooks"])

        def checks():
            return len([line for line in self.calls.read_text().splitlines() if line != "--version"])

        counts = []
        for _ in range(2):
            with self.assertRaises(c.BridgeError):
                self.check("codex", self.codex(executable), gate)
            counts.append(checks())
        self.assertEqual(counts[0], counts[1])  # the second call only read the version
        claude = self.fake()
        self.check("claude", self.claude(claude), gate)
        before = checks()
        self.check("claude", self.claude(claude, subagents=False), gate)  # another tool set: checked again
        self.assertGreater(checks(), before)

    def test_the_canary_command_keeps_the_restrictions_and_drops_the_session(self):
        command = c.build_command("claude", "/bin/claude", "conv", "session", model="opus", reasoning_effort="max",
                                  directories=("/repo",))
        canary = preflight.canary_command(command, Path("/check/project"), model=False)
        for kept in ("--settings", "--setting-sources", "--strict-mcp-config", "--tools", "--disallowed-tools",
                     "--effort"):
            self.assertIn(kept, canary)
        for dropped in ("--resume", "--name", "--model", "/repo"):
            self.assertNotIn(dropped, canary)
        self.assertEqual(canary[-2:], ["--add-dir", "/check/project"])
        self.assertEqual(canary[canary.index("--output-format") + 1:canary.index("--output-format") + 3],
                         ["stream-json", "--verbose"])
        self.assertIn("--model", preflight.canary_command(command, Path("/p"), model=True))


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

    def test_the_names_a_command_passes(self):
        command = ["/bin/codex", "--disable", "hooks", "-c", "features.skill_mcp_dependency_install=false",
                   "-c", "features.multi_agent_v2.disable_native_tools=true", "-c", 'mcp_servers."a".enabled=false',
                   "-c", 'mcp_servers."b".enabled=false', "-c", 'web_search="cached"', "exec"]
        self.assertEqual(c.codex_feature_names(command), ["hooks", "skill_mcp_dependency_install"])
        self.assertEqual(c.codex_key_names(command), ["features.skill_mcp_dependency_install",
                                                      "features.multi_agent_v2.disable_native_tools",
                                                      "mcp_servers.enabled", "web_search"])
        self.assertEqual(c.without_setting(command, "web_search")[-1], "exec")
        self.assertNotIn('web_search="cached"', c.without_setting(command, "web_search"))

    def test_claude_tools_are_compared_through_their_reported_names(self):
        requested = ["Read", "Glob", "Grep", "WebSearch", "Skill", "Agent"]
        self.assertEqual(c.missing_claude_tools(requested, ["Read", "Glob", "Grep", "Task", "Skill"]), ["WebSearch"])
        self.assertEqual(c.unexpected_claude_tools(requested, ["Read", "Task", "Bash"]), ["Bash"])
        self.assertEqual(c.unexpected_claude_tools(requested[:-1], ["Read", "Task"]), ["Task"])
