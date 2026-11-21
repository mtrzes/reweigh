"""The Codex pre-launch check: what it reads from Codex's answers, and what it does when a name the bridge depends on
is gone. An in-process fake Codex acts out each rename; test_contracts runs the check against the installed Codex."""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import consultation  # noqa: E402  (the bridge's own modules, found through its script directory)
import preflight  # noqa: E402

c = consultation


def fake_codex(arguments, settings):
    """Codex's exit status and output for `codex <arguments>`: it knows every name the bridge passes, except as
    settings says. `features list` exits 2 on a value in rejected, or when every value in together is passed; it
    refuses a wrong-type canary with 1 unless its key is in renamed; it lists each disabled feature as `stable false`
    unless rows says otherwise (None leaves the row out); it exits 1 with broken, lists nothing with empty_listing
    and only a header with header_only; and it exits with a status on an argument named in crash."""
    for argument, status in settings.get("crash", {}).items():
        if argument in arguments:
            return status, ""
    if arguments[-2:] != ["features", "list"] or settings.get("broken"):
        return (1 if settings.get("broken") else 2), ""
    if all(value in arguments for value in settings.get("together", [None])):
        return 2, ""
    values = [arguments[i + 1] for i, word in enumerate(arguments[:-1])
              if word in ("-c", "--disable", "--sandbox", "--ask-for-approval", "--model")]
    if any(value in settings.get("rejected", []) for value in values):
        return 2, ""
    canary = False
    for value in values:
        if value.endswith("=1"):
            canary = True
            if value[:-2] not in settings.get("renamed", []):
                return 1, ""
    if settings.get("header_only") and not canary:
        return 0, "FEATURE STAGE ENABLED\n"
    if settings.get("empty_listing"):
        return 0, ""
    names = [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word == "--disable"]
    names += [v[len("features."):].partition("=")[0] for v in values if v.startswith("features.")]
    rows = settings.get("rows", {})
    lines = [f"image_generation {rows.get('image_generation', 'stable true')}"]
    lines += [f"{name} {rows.get(name, 'stable false')}" for name in names if rows.get(name, "") is not None]
    return 0, "\n".join(lines) + "\n"


def runner_for(settings, seen=None):
    """The check's runner, answering from fake_codex; seen collects each call's command, environment and home
    entries."""
    async def run(command, stdin, directory, timeout, environment):
        if seen is not None:
            seen.append((command, environment, sorted(directory.iterdir())))
        if command[0] == "missing":
            raise FileNotFoundError(command[0])
        code, output = fake_codex(command[1:], settings)
        return code, output.encode(), b""
    return run


class PreflightTests(unittest.TestCase):
    @staticmethod
    def command(session=None, **options):
        return c.build_command("codex", "codex", "conv", session, reasoning_effort="high", **options)

    @staticmethod
    def check(command, provider="codex", **settings):
        return asyncio.run(preflight.preflight(provider, command, runner_for(settings)))

    def test_a_codex_that_knows_every_name_passes(self):
        for session, options in ((None, {}), (None, {"subagents": False}), ("session", {})):
            with self.subTest(session=session, options=options):
                self.assertEqual(self.check(self.command(session, **options)), [])

    def test_a_claude_command_is_not_checked_at_runtime(self):
        self.assertEqual(self.check(["missing", "--print"], provider="claude"), [])

    def test_a_lost_restriction_refuses_the_call_and_names_it(self):
        mcp = ["-c", 'mcp_servers."docs".enabled=false']
        rejected = "Codex no longer accepts one of the command's options, which the bridge passes to restrict the " \
                   "peer or set the chosen model and effort"
        cases = (
            ({"rejected": ["hooks"]}, (), rejected),
            ({"rejected": ["read-only"]}, (), rejected),
            ({"together": ["hooks", "plugins"]}, (), rejected),
            ({"renamed": ["shell_environment_policy.inherit"]}, (), "Codex no longer accepts -c "
             "shell_environment_policy.inherit, which the bridge passes to keep the peer's commands to the core"),
            ({"renamed": ["web_search"]}, (), "Codex no longer accepts -c web_search, which the bridge passes to "
             "limit the peer's web search to OpenAI's index"),
            ({"renamed": ["features.multi_agent_v2.disable_native_tools"]}, (),
             "Codex no longer accepts -c features.multi_agent_v2.disable_native_tools"),
            ({"renamed": ["mcp_servers.peer-bridge-canary.enabled"]}, mcp, "-c mcp_servers.enabled"),
            ({"renamed": ["mcp_servers.peer-bridge-canary.enabled"]}, (), "-c mcp_servers.enabled"),
            ({"rows": {"hooks": "stable true"}}, (), "Codex no longer accepts the feature hooks"),
            ({"rows": {"apps": "removed false"}}, (), "Codex no longer accepts the feature apps"),
            ({"rows": {"plugins": None}}, (), "Codex no longer accepts the feature plugins"),
            # An unreadable row or listing does not hide a restriction another probe proves lost.
            ({"rows": {"hooks": "false"}, "renamed": ["web_search"]}, (), "Codex no longer accepts -c web_search"),
            ({"header_only": True, "renamed": ["web_search"]}, (), "Codex no longer accepts -c web_search"),
            # A removed feature is lost whatever its state column says, even in a listing with no other readable row.
            ({"rows": {"apps": "removed n/a"}}, (), "Codex no longer accepts the feature apps"),
            ({"rows": {**{name: None for name in (*c.CODEX_DISABLED_FEATURES, *c.CODEX_EXTRA_DISABLED_FEATURES,
                                                  "multi_agent", "multi_agent_v2.disable_native_tools")},
                       "apps": "removed n/a", "image_generation": "stable on"}}, (),
             "Codex no longer accepts the feature apps"),
            # A confirmed loss refuses even beside a row of a feature the command does not use, in a new shape.
            ({"rows": {"hooks": "stable true", "image_generation": "stable maybe"}}, (),
             "Codex no longer accepts the feature hooks"),
        )
        for settings, extra, message in cases:
            command = self.command(subagents=False)
            command[command.index("exec"):command.index("exec")] = list(extra)
            with self.subTest(message=message, settings=settings):
                with self.assertRaises(c.BridgeError) as refused:
                    self.check(command, **settings)
                self.assertIn(message, str(refused.exception))
                self.assertIn("The consultation was not started", str(refused.exception))

    def test_the_check_runs_codex_in_an_empty_home_with_only_path(self):
        seen = []
        with unittest.mock.patch.dict("os.environ", {"OPENAI_API_KEY": "secret", "PATH": "relative:/usr/bin:/bin"}):
            asyncio.run(preflight.preflight("codex", self.command(), runner_for({}, seen)))
        self.assertTrue(seen)
        for _, environment, entries in seen:
            self.assertEqual(sorted(environment), ["CODEX_HOME", "HOME", "PATH"])
            self.assertEqual(environment["PATH"], "/usr/bin:/bin")
            self.assertEqual(environment["HOME"], environment["CODEX_HOME"])
            self.assertEqual(entries, [])

    def test_the_first_probe_carries_every_option_before_exec_even_a_model_named_exec(self):
        seen = []
        command = c.build_command("codex", "codex", "conv", None, model="exec", reasoning_effort="high")
        self.assertEqual(asyncio.run(preflight.preflight("codex", command, runner_for({}, seen))), [])
        self.assertEqual(seen[0][0], [*command[:command.index("exec", command.index("--model") + 2)],
                                      "features", "list"])

    def test_a_check_without_a_verdict_warns_and_leaves_the_command_alone(self):
        unread = ("Codex's restrictions could not be checked before the start (", "); the peer ran unchecked.")
        listing = "codex features list printed rows the check cannot read"
        cases = (
            ("missing", {}, "Codex did not run"),
            ("codex", {"crash": {"web_search=1": 139}}, "a check ended with status 139"),
            ("codex", {"empty_listing": True}, listing),
            ("codex", {"header_only": True}, listing),
            ("codex", {"rows": {"hooks": "stable maybe"}}, listing),
            ("codex", {"rows": {"hooks": "false"}}, listing),
            # Codex failing without the bridge's options too says nothing about the options.
            ("codex", {"broken": True}, "codex features list failed without the bridge's options too"),
        )
        for executable, settings, reason in cases:
            command = self.command()
            command[0] = executable
            before = list(command)
            with self.subTest(reason=reason, settings=settings):
                self.assertEqual(self.check(command, **settings), [reason.join(unread)])
                self.assertEqual(command, before)


class PreflightRuleTests(unittest.TestCase):
    def test_feature_rows_keep_the_stage_and_the_state(self):
        listing = "apps     stable     false\nmulti_agent  deprecated  true\nodd under development false\nshort x\n"
        self.assertEqual(c.codex_feature_rows(listing), {
            "apps": ("stable", "false"), "multi_agent": ("deprecated", "true"), "odd": ("under development", "false"),
            "short": ("", "")})

    def test_the_names_a_command_passes(self):
        flags = ["--no-daemon", "--sandbox", "read-only", "--disable", "hooks",
                 "-c", "features.skill_mcp_dependency_install=false",
                 "-c", "features.multi_agent_v2.disable_native_tools=true", "-c", 'mcp_servers."a".enabled=false',
                 "-c", 'mcp_servers."b".enabled=false', "-c", 'web_search="cached"']
        self.assertEqual(c.codex_feature_names(flags), ["hooks", "skill_mcp_dependency_install"])
        self.assertEqual(c.codex_key_names(flags), ["features.skill_mcp_dependency_install",
                                                    "features.multi_agent_v2.disable_native_tools",
                                                    "mcp_servers.enabled", "web_search"])


if __name__ == "__main__":
    unittest.main()
