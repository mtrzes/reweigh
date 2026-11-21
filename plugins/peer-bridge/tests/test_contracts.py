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
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (
    BRIDGE_PATH, DEADLINE_SECONDS, TESTS, BridgeTestCase, bridge, claude_prompt, claude_reply, claude_session,
    codex_thread, codex_turn, consultation, git_repository,
)

DIFFERENT_PEER = "This conversation belongs to a different provider or working directory."


def canonical_json(value):
    """JSON text with sorted keys: equal only when the JSON is, so false and 0 differ, and a diff reads line by line."""
    return json.dumps(value, indent=2, sort_keys=True)


class CodexContractTests(unittest.TestCase):
    def test_codex_knows_every_feature_the_bridge_disables(self):
        # A name Codex drops stops every consultation (--disable) or silently loses a layer (configuration), so the
        # release run installs Codex and fails here first. Elsewhere it runs when codex is on PATH, in an empty home,
        # so Codex reads none of the user's configuration or history.
        executable = shutil.which("codex")
        if executable is None:
            self.assertNotEqual(os.environ.get("GITHUB_ACTIONS"), "true", "The release run must install Codex.")
            self.skipTest("codex is not on PATH.")
        with tempfile.TemporaryDirectory() as home:
            listing = subprocess.run(
                [executable, "features", "list"], cwd=home, env={"PATH": os.environ["PATH"], "HOME": home,
                                                                  "CODEX_HOME": home},
                capture_output=True, text=True, timeout=DEADLINE_SECONDS, check=True,
            ).stdout
        # Each row is a name, a stage of one or more words, and the effective state.
        stages = {words[0]: " ".join(words[1:-1]) for words in map(str.split, listing.splitlines()) if len(words) >= 3}
        for feature in (*consultation.CODEX_DISABLED_FEATURES, *consultation.CODEX_EXTRA_DISABLED_FEATURES):
            with self.subTest(feature=feature):
                self.assertTrue(feature in stages, "Codex no longer knows this feature; find what replaced it.")
                self.assertNotEqual(stages[feature], "removed", "Codex removed this feature; find what replaced it.")
        # Codex refuses to start on a key it does not know, so the sub-agent switch fails closed; this finds a rename
        # before a release instead of at an operator's next consultation.
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [executable, *consultation.CODEX_NO_SUBAGENTS, "features", "list"], cwd=home,
                env={"PATH": os.environ["PATH"], "HOME": home, "CODEX_HOME": home},
                capture_output=True, text=True, timeout=DEADLINE_SECONDS,
            )
        self.assertEqual(result.returncode, 0, "Codex no longer accepts the sub-agent switch: " + result.stderr[-300:])


class InstalledCliTests(unittest.TestCase):
    """The pre-launch check against the CLIs installed here: the bridge's real commands pass with no warning. Each
    check runs in an empty temporary home; Claude's runs signed out, so no model is called."""

    def check(self, provider, command):
        import asyncio
        import preflight

        async def runner(command, stdin, directory, timeout, environment):
            result = await asyncio.to_thread(subprocess.run, command, input=stdin.encode() if stdin else None,
                                             cwd=directory, env=environment, capture_output=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr

        return asyncio.run(preflight.Preflight()(provider, command, runner))

    def test_the_installed_codex_knows_every_name_the_bridge_passes(self):
        executable = shutil.which("codex")
        if executable is None:
            self.assertNotEqual(os.environ.get("GITHUB_ACTIONS"), "true", "The release run must install Codex.")
            self.skipTest("codex is not on PATH.")
        for subagents in (True, False):
            command = consultation.build_command("codex", executable, "conv", reasoning_effort="high",
                                                 subagents=subagents)
            with self.subTest(subagents=subagents):
                self.assertEqual(self.check("codex", command), [])

    def test_the_installed_claude_keeps_every_restriction_and_offers_every_tool(self):
        executable = shutil.which("claude")
        if executable is None:
            self.skipTest("claude is not on PATH.")
        command = consultation.build_command("claude", executable, "conv", directories=("/",))
        self.assertEqual(self.check("claude", command), [])


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
                failed, resumed = await self.call(session, "ask_claude", {**request, **conversation})
                self.assertEqual((failed, resumed["conversation_id"]), (False, asked["conversation_id"]))
                self.assertEqual(await self.call(session, "ask_claude", {**request, "prompt": " "}),
                                 (True, "A consultation question is required."))
            with self.subTest(tool="ask_codex"):
                # Each Codex tool reaches Codex: it refuses the Claude conversation.
                self.assertEqual(await self.call(session, "ask_codex", {**request, **conversation}),
                                 (True, DIFFERENT_PEER))

            waiting = {"wait_seconds": DEADLINE_SECONDS}
            with self.subTest(tool="start_claude_consultation, get_consultation, cancel_consultation"):
                _, started = await self.call(session, "start_claude_consultation", request)
                _, done = await self.call(session, "get_consultation", {**waiting, "job_id": started["job_id"]})
                self.assertEqual((done["state"], done["reply"]),
                                 ("completed", "claude consulted: Explain the query."))
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
        code, first = self.run_cli(*target, "--context-file", str(context), prompt=b"Explain the query.")
        self.assertEqual((code, set(first)), (0, {"provider", "conversation_id", "reply"}))
        self.assertIn("Date filtering is relevant.", first["reply"])
        code, second = self.run_cli(*target, "--conversation-id", first["conversation_id"], prompt=b"Null dates?")
        self.assertEqual((code, second["conversation_id"]), (0, first["conversation_id"]))
        self.assertIn("Null dates?", second["reply"])
        for extra, prompt, expected in (
            (("--context-file", str(self.root / "missing.md")), b"Explain.", "Cannot read the context file"),
            ((), b"Explain \xff the query.", "Cannot read the prompt from standard input as UTF-8"),
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
