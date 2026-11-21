"""Exercise the bridge as its clients see it: the MCP server's tools and envelopes, and the command line's JSON and exit
statuses. Each test starts real bridge processes; what they do inside is tested in test_bridge.py and test_jobs.py.
Checks that read the repository's files only are in test_repository.py; those that run the installed Codex and
Claude Code, in an empty home, are here.

Codex consultations here are refused before the bridge reads Codex configuration, so no bridge process reads the
machine's /etc/codex/config.toml.
"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (
    BRIDGE_PATH, DEADLINE_SECONDS, TESTS, BridgeTestCase, bridge, claude_prompt, claude_reply, claude_session,
    codex_thread, codex_turn, consultation, git_repository, isolate,
)
import preflight  # noqa: E402  (found through the script directory support.py adds)

DIFFERENT_PEER = "This conversation belongs to a different provider or working directory."


def canonical_json(value):
    """JSON text with sorted keys: equal only when the JSON is, so false and 0 differ, and a diff reads line by line."""
    return json.dumps(value, indent=2, sort_keys=True)




class InstalledCliTests(unittest.TestCase):
    """The bridge's commands against the CLIs installed here, each in an empty temporary home: Codex through the
    pre-launch check the bridge runs, Claude Code signed out, so no model is called, through a canary of its own."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-installed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def installed(self, name):
        executable = shutil.which(name)
        if executable is None:
            # The run started by hand on a release commit installs both CLIs; elsewhere a missing one skips.
            self.assertNotEqual(os.environ.get("GITHUB_EVENT_NAME"), "workflow_dispatch",
                                f"The release run must install {name}.")
            self.skipTest(f"{name} is not on PATH.")
        return executable

    def test_the_installed_codex_knows_every_name_the_bridge_passes(self):
        async def runner(command, stdin, directory, timeout, environment):
            result = await asyncio.to_thread(subprocess.run, command, cwd=directory, env=environment,
                                             capture_output=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr

        executable = self.installed("codex")
        for session, subagents in ((None, True), (None, False), (str(uuid.uuid4()), True)):
            command = consultation.build_command("codex", executable, "conv", session, reasoning_effort="high",
                                                 subagents=subagents)
            with self.subTest(resumed=bool(session), subagents=subagents):
                self.assertEqual(asyncio.run(preflight.Preflight()("codex", command, runner)), [])

    def claude_canary(self, command):
        """Run a Claude command signed out in an empty home, beside a user hook and a user MCP server that each touch
        a marker if they run, in a repository whose settings name a model; return the markers written, the model
        and the tools system/init reports."""
        home, project = self.root / "home", self.root / "project"
        for folder in (home / ".claude", project / ".claude"):
            folder.mkdir(parents=True, exist_ok=True)
        for marker in ("user-hook", "mcp"):
            (self.root / marker).unlink(missing_ok=True)
        hook = {"type": "command", "command": f"/usr/bin/touch {shlex.quote(str(self.root / 'user-hook'))}"}
        (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [hook]}]}}))
        (home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"canary": {"command": "/usr/bin/touch", "args": [str(self.root / "mcp")]}}}))
        (project / ".claude" / "settings.json").write_text(json.dumps({"model": "peer-bridge-canary-model"}))
        environment = {"PATH": os.environ["PATH"], "HOME": str(home)}
        status = subprocess.run([command[0], "auth", "status", "--json"], cwd=project, env=environment,
                                capture_output=True, text=True, timeout=DEADLINE_SECONDS)
        signed_in = json.loads(status.stdout).get("loggedIn")  # an unreadable answer fails the test, never skips it
        self.assertIsInstance(signed_in, bool, "claude auth status --json gave no loggedIn")
        if signed_in:
            self.skipTest("Claude Code read the empty home as signed in; the canary would call a model.")
        result = subprocess.run(command, input="check", cwd=project, env=environment, capture_output=True,
                                text=True, timeout=DEADLINE_SECONDS)
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        [init] = [record for record in records if record.get("subtype") == "init"]
        return ({marker for marker in ("user-hook", "mcp") if (self.root / marker).exists()}, init.get("model"),
                sorted(init.get("tools", [])))

    def test_the_installed_claude_keeps_every_restriction_and_offers_every_tool(self):
        executable = self.installed("claude")
        streamed = ["--output-format", "stream-json", "--verbose"]
        # Control: without the bridge's restrictions, each planted signal fires.
        control = [executable, "--print", *streamed, "--tools", "Read", "--setting-sources", "user,project"]
        self.assertEqual(self.claude_canary(control), ({"user-hook", "mcp"}, "peer-bridge-canary-model", ["Read"]))
        # The bridge's command, as it runs a consultation, with streamed output: none fires, and every tool is there,
        # Agent under the name Task.
        command = consultation.build_command("claude", executable, "conv", directories=(str(self.root / "project"),))
        at = command.index("--output-format")
        command[at:at + 2] = streamed
        markers, model, tools = self.claude_canary(command)
        self.assertEqual(markers, set())
        self.assertNotEqual(model, "peer-bridge-canary-model")
        self.assertEqual(tools, ["Glob", "Grep", "Read", "Skill", "Task", "WebSearch"])


class McpResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_result_with_warnings_passes_the_tool_s_output_schema(self):
        # A consultation's result carries the pre-launch check's warnings, a list, beside its text fields.
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-result-")
        self.addCleanup(temporary.cleanup)
        isolate(self, Path(temporary.name))

        async def consulted(bridge_self, provider, *arguments, **options):
            return {"provider": provider, "conversation_id": "conv", "reply": "Answer.", "warnings": ["Checked."]}

        with patch.object(bridge.Bridge, "consult", consulted):
            for peer, tool in (("codex", "ask_codex"), ("claude", "ask_claude")):
                with self.subTest(tool=tool):
                    server = bridge.create_server(peer)
                    _, result = await server.call_tool(tool, {"prompt": "Q", "working_directory": "/"})
                    self.assertEqual(result, {"provider": peer, "conversation_id": "conv", "reply": "Answer.",
                                              "warnings": ["Checked."]})


class McpTests(BridgeTestCase):
    @asynccontextmanager
    async def session(self, peer):
        parameters = StdioServerParameters(command=sys.executable, args=[str(BRIDGE_PATH), "--peer", peer],
                                           env=dict(os.environ))
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=2 * DEADLINE_SECONDS)) as session,
        ):
            session.started = await session.initialize()
            yield session

    @staticmethod
    async def call(session, tool, arguments):
        """The tool's JSON result, or its error message, which the envelope prefixes with the tool name."""
        response = await session.call_tool(tool, arguments)
        text = response.content[0].text
        if response.isError:
            return True, text.removeprefix(f"Error executing tool {tool}: ")
        return False, json.loads(text)

    async def test_tools_their_annotations_and_envelopes(self):
        def tools(provider):
            return {f"ask_{provider}", f"list_{provider}_threads", f"read_{provider}_thread",
                    f"start_{provider}_consultation"}

        jobs = {"get_consultation", "cancel_consultation"}
        expected = {"claude": tools("claude") | jobs, "codex": tools("codex") | jobs | {"list_codex_models"},
                    "both": tools("claude") | tools("codex") | jobs | {"list_codex_models"}}
        # The Claude-peer server, which Codex loads, tells Codex never to drive the Claude app instead.
        instructions = {"claude": [bridge.CLAUDE_PEER_INSTRUCTIONS], "codex": [bridge.CODEX_PEER_INSTRUCTIONS]}
        # The arguments each tool accepts are its public interface: a change to one is a reviewed fixture diff.
        schemas = json.loads((TESTS / "fixtures" / "mcp_input_schemas.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schemas), expected["both"])

        def offered(listing, peer):
            self.maxDiff = None  # show the changed lines of the schema, not a truncated summary
            self.assertEqual(canonical_json({tool.name: tool.inputSchema for tool in listing.tools}),
                             canonical_json({name: schemas[name] for name in expected[peer]}))

        for peer in ("claude", "codex"):
            with self.subTest(peer=peer):
                async with self.session(peer) as session:
                    listing = await session.list_tools()
                    given = session.started.instructions
                offered(listing, peer)
                for part in instructions[peer]:
                    self.assertIn(part, given)
        await asyncio.to_thread(git_repository, self.project)
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Codex answer."), name="Codex review")
        claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Claude answer."),
                       title="Claude review")
        request = {"prompt": "Explain the query.", "working_directory": str(self.project)}
        async with self.session("both") as session:
            listing = await session.list_tools()
            offered(listing, "both")
            # One subtest per tool or tool group, so a broken route does not hide the ones after it.
            with self.subTest(part="annotations"):
                for tool in listing.tools:
                    # Consulting sends content to the other provider and cancelling stops a job: only lookups are
                    # read-only.
                    self.assertEqual(tool.annotations.readOnlyHint, tool.name.startswith(("list_", "read_", "get_")))
                    self.assertFalse(tool.annotations.destructiveHint)

            conversation = {"conversation_id": "unset"}
            with self.subTest(tool="ask_claude"):
                response = await session.call_tool("ask_claude", request)
                self.assertFalse(response.isError)
                asked = response.structuredContent
                self.assertEqual((asked["provider"], asked["reply"]),
                                 ("claude", "claude consulted: Explain the query."))
                conversation = {"conversation_id": asked["conversation_id"]}
                failed, resumed = await self.call(session, "ask_claude", {**request, **conversation, "model": "opus",
                                                                          "reasoning_effort": "max"})
                self.assertEqual((failed, resumed["conversation_id"]), (False, asked["conversation_id"]))
                self.assertEqual((resumed["model"], resumed["reasoning_effort"]), ("opus", "max"))
                arguments = self.calls()[-1]["arguments"]
                self.assertEqual([arguments[arguments.index(flag) + 1] for flag in ("--model", "--effort")],
                                 ["opus", "max"])
                self.assertEqual(await self.call(session, "ask_claude", {**request, "prompt": " "}),
                                 (True, "A consultation question is required."))
            with self.subTest(tool="ask_codex"):
                # Each Codex tool reaches Codex: it refuses the Claude conversation.
                self.assertEqual(await self.call(session, "ask_codex", {**request, **conversation}),
                                 (True, DIFFERENT_PEER))

            waiting = {"wait_seconds": DEADLINE_SECONDS}
            with self.subTest(tool="start_claude_consultation, get_consultation, cancel_consultation"):
                extra = self.root / "extra"
                extra.mkdir()
                _, started = await self.call(session, "start_claude_consultation", {
                    **request, "model": "opus", "reasoning_effort": "max", "extra_directories": [str(extra)]})
                _, done = await self.call(session, "get_consultation", {**waiting, "job_id": started["job_id"]})
                self.assertEqual((done["state"], done["reply"], done["model"], done["reasoning_effort"]),
                                 ("completed", "claude consulted: Explain the query.", "opus", "max"))
                self.assertIn(str(extra.resolve()), self.calls()[-1]["arguments"])
                _, cancelled = await self.call(session, "cancel_consultation", {"job_id": started["job_id"]})
                self.assertEqual(cancelled["state"], "completed")
            with self.subTest(tool="start_codex_consultation"):
                _, started = await self.call(session, "start_codex_consultation", {**request, **conversation})
                _, done = await self.call(session, "get_consultation", {**waiting, "job_id": started["job_id"]})
                self.assertEqual((done["provider"], done["state"], done["error"]), ("codex", "failed", DIFFERENT_PEER))
            with self.subTest(tool="get_consultation"):
                self.assertEqual(await self.call(session, "get_consultation", {"job_id": "not-a-job"}),
                                 (True, "No consultation job with that ID."))

            for provider, name in (("claude", "Claude review"), ("codex", "Codex review")):
                with self.subTest(provider=provider):
                    _, threads = await self.call(session, f"list_{provider}_threads",
                                                 {"working_directory": str(self.project)})
                    self.assertEqual([thread["name"] for thread in threads["threads"]], [name])
                    _, thread = await self.call(session, f"read_{provider}_thread",
                                                {"working_directory": str(self.project), "thread": name})
                    self.assertEqual(thread["messages"][0]["text"], f"{name.split()[0]} answer.")


class CommandLineTests(BridgeTestCase):
    def run_cli(self, *arguments, prompt=b""):
        completed = subprocess.run([sys.executable, str(BRIDGE_PATH), *arguments], input=prompt,
                                   capture_output=True, timeout=DEADLINE_SECONDS)
        try:
            return completed.returncode, json.loads(completed.stdout)
        except ValueError:  # a crash before the result: its traceback is on stderr
            pass
        self.fail(f"The CLI printed no JSON result (exit {completed.returncode}). Its stderr:\n"
                  + completed.stderr.decode("utf-8", errors="replace"))

    def test_ask_prints_the_tool_result_resumes_and_reports_errors_as_json(self):
        target = ("ask", "--provider", "claude", "--working-directory", str(self.project))
        context = self.root / "context.md"
        context.write_text("Date filtering is relevant.", encoding="utf-8")
        extra = [self.root / "extra-a", self.root / "extra-b"]
        for folder in extra:
            folder.mkdir()
        code, first = self.run_cli(*target, "--context-file", str(context), "--extra-directory", str(extra[0]),
                                   "--extra-directory", str(extra[1]), prompt=b"Explain the query.")
        self.assertEqual((code, set(first)), (0, {"provider", "conversation_id", "reply"}))
        arguments = self.calls()[0]["arguments"]
        added = [arguments[index + 1] for index, word in enumerate(arguments) if word == "--add-dir"]
        self.assertEqual(added, [str(self.project.resolve()), *(str(folder.resolve()) for folder in extra)])
        self.assertIn("Date filtering is relevant.", first["reply"])
        code, second = self.run_cli(*target, "--conversation-id", first["conversation_id"], prompt=b"Null dates?")
        self.assertEqual((code, second["conversation_id"]), (0, first["conversation_id"]))
        self.assertIn("Null dates?", second["reply"])
        for extra, prompt, expected in (
            (("--context-file", str(self.root / "missing.md")), b"Explain.", "Cannot read the context file"),
            ((), b"Explain \xff the query.", "Cannot read the prompt from standard input as UTF-8"),
            (("--extra-directory", str(context)), b"Explain.", "which is not a directory"),
        ):
            with self.subTest(expected=expected):
                code, result = self.run_cli(*target, *extra, prompt=prompt)
                self.assertEqual((code, list(result)), (1, ["error"]))
                self.assertIn(expected, result["error"])
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["session"], calls[1]["session"])

    def test_start_job_and_cancel_print_the_job(self):
        code, started = self.run_cli("start", "--provider", "claude", "--working-directory", str(self.project),
                                     prompt=b"CLI question?")
        self.assertEqual(code, 0)
        code, done = self.run_cli("job", "--job-id", started["job_id"], "--wait", str(DEADLINE_SECONDS))
        self.assertEqual((code, done["state"], done["reply"]), (0, "completed", "claude consulted: CLI question?"))
        code, cancelled = self.run_cli("cancel", "--job-id", started["job_id"])
        self.assertEqual((code, cancelled["state"]), (0, "completed"))
        self.assertEqual(self.run_cli("job", "--job-id", "not-a-job"),
                         (1, {"error": "No consultation job with that ID."}))

    def test_signalling_cli_start_after_publication_cancels_the_job(self):
        script = f"""\
import os, signal, sys, threading
from pathlib import Path

sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).parent))
import bridge
import jobs

discarding = threading.Event()
create = jobs.JobStore.create
discard = jobs.abandon_created

def create_and_signal(store, *arguments):
    job_id = create(store, *arguments)
    os.kill(os.getpid(), signal.SIGTERM)
    # Keep creation pending until the CLI's task has entered its cancellation cleanup.
    assert discarding.wait({DEADLINE_SECONDS})
    return job_id

async def abandon_created(*arguments):
    discarding.set()
    await discard(*arguments)

jobs.JobStore.create = create_and_signal
jobs.abandon_created = abandon_created
bridge.main()
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(BRIDGE_PATH), "start", "--provider", "claude",
             "--working-directory", str(self.project)],
            input="PRIVATE QUESTION", capture_output=True, text=True, timeout=DEADLINE_SECONDS,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"error": "The consultation was cancelled."})
        [folder] = (self.state / "jobs").iterdir()
        self.assertFalse((folder / "request.json").exists())
        code, job = self.run_cli("job", "--job-id", folder.name)
        self.assertEqual((code, job["state"]), (1, "cancelled"))

    def test_threads_and_read_thread_print_the_tool_result(self):
        git_repository(self.project)
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Codex review")
        target = ("--provider", "codex", "--working-directory", str(self.project))
        code, listing = self.run_cli("threads", *target)
        self.assertEqual((code, [thread["name"] for thread in listing["threads"]]), (0, ["Codex review"]))
        code, missing = self.run_cli("read-thread", *target, "--thread", "Missing")
        self.assertEqual(code, 1)
        self.assertIn("No readable thread", missing["error"])


if __name__ == "__main__":
    unittest.main()
