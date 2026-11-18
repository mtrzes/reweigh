"""Exercise the bridge's I/O shell with the stand-in peer: processes, session records, pinned checkouts and the facts a
failure is diagnosed from. The rules those rest on are tested in test_core.py; the client contracts in
test_contracts.py."""

import asyncio
import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio

# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (
    BRIDGE_PATH, DEADLINE_SECONDS, BridgeTestCase, PRIVATE_MARKERS, bridge, codex_thread, codex_turn, consultation,
    eventually_async, git, git_repository, read_pid, running,
)
import jobs  # noqa: E402  (support adds the plugin's scripts to the path)


class ConsultationTests(BridgeTestCase):
    async def test_both_peers_resume_their_own_conversation_with_context(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                first = await self.consult(provider, context="Date filtering is relevant.")
                identifier = first["conversation_id"]
                self.assertEqual(str(uuid.UUID(identifier)), identifier)
                self.assertEqual(first["provider"], provider)
                self.assertIn("Date filtering is relevant.", first["reply"])
                second = await self.consult(provider, prompt="What about null dates?", conversation_id=identifier)
                self.assertEqual(second["conversation_id"], identifier)
                self.assertIn("What about null dates?", second["reply"])
                calls = self.calls()[-2:]
                self.assertEqual(calls[0]["session"], calls[1]["session"])
                self.assertNotEqual(identifier, calls[0]["session"])
                self.assertEqual(calls[1]["cwd"], str(self.project))
                # The peer gets the consultation environment: marked, without the host session.
                self.assertEqual((calls[1]["active"], calls[1]["claudecode_present"]), ("1", False))
                self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((self.state / f"{identifier}.json").stat().st_mode), 0o600)

    async def test_codex_settings_and_declared_mcp_servers_reach_its_command(self):
        # Codex never loads a project's configuration here, and disabling a server it never saw breaks Codex.
        project_config = self.project / ".codex" / "config.toml"
        project_config.parent.mkdir()
        project_config.write_text('[mcp_servers.project-peer]\ncommand = "never-run-me"\n', encoding="utf-8")
        consultation.CODEX_SYSTEM_CONFIG.write_text('[mcp_servers."system peer"]\ncommand = "x"\n', encoding="utf-8")
        settings = {"PEER_BRIDGE_CODEX_MODEL": "gpt-x", "PEER_BRIDGE_CODEX_REASONING_EFFORT": "high"}
        with patch.dict(os.environ, settings):
            await self.consult("codex")
        arguments = self.calls()[-1]["arguments"]
        for setting in ('mcp_servers."system peer".enabled=false', 'model_reasoning_effort="high"'):
            self.assertIn(setting, arguments)
        self.assertFalse([argument for argument in arguments if "project-peer" in argument])
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-x")
        # A choice made for the call wins over the environment, and the reply says what ran.
        with patch.dict(os.environ, settings):
            answer = await self.consult("codex", model="gpt-6-astra", reasoning_effort="xhigh")
        arguments = self.calls()[-1]["arguments"]
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="xhigh"', arguments)
        self.assertEqual((answer["model"], answer["reasoning_effort"]), ("gpt-6-astra", "xhigh"))
        calls = len(self.calls())
        with self.assertRaisesRegex(bridge.BridgeError, "Codex consultations only"):
            await self.consult("claude", model="opus")
        # With Codex's catalogue readable, a value it does not list is refused before Codex starts.
        self.codex_home.mkdir(parents=True, exist_ok=True)
        (self.codex_home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-6-astra", "visibility": "list", "supported_reasoning_levels": [{"effort": "xhigh"}]}]}))
        with self.assertRaisesRegex(bridge.BridgeError, "takes reasoning_effort xhigh, not low"):
            await self.consult("codex", model="gpt-6-astra", reasoning_effort="low")
        with patch.dict(os.environ, settings), self.assertRaisesRegex(bridge.BridgeError, "Codex lists no model gpt-x"):
            await self.consult("codex")  # the environment's choice is checked too
        self.assertEqual(len(self.calls()), calls)
        # A system configuration it cannot read could declare a server it cannot disable, so nothing starts; one that
        # is not a regular file, such as a FIFO, is refused without blocking.
        system = consultation.CODEX_SYSTEM_CONFIG
        system.write_text("[mcp_servers.broken\n", encoding="utf-8")
        with self.assertRaisesRegex(bridge.BridgeError, "Cannot read Codex configuration"):
            await self.consult("codex")
        system.unlink()
        os.mkfifo(system)
        with self.assertRaisesRegex(bridge.BridgeError, "Cannot read Codex configuration"):
            await asyncio.wait_for(self.consult("codex"), DEADLINE_SECONDS)
        self.assertEqual(len(self.calls()), calls)  # no peer started

    def without_peer_on_path(self):
        empty = self.root / "empty-bin"
        empty.mkdir(exist_ok=True)
        return patch.dict(os.environ, {"PATH": str(empty)})

    async def test_codex_falls_back_to_the_cli_bundled_with_chatgpt_on_macos(self):
        applications = self.root / "Applications"
        bundled = applications / bridge.BUNDLED_CODEX_PATH
        bundled.parent.mkdir(parents=True)
        bundled.symlink_to(self.peer)
        directories = (str(self.root / "missing-applications"), str(applications))
        with (
            self.without_peer_on_path(),
            patch.object(bridge, "MACOS_APPLICATION_DIRECTORIES", directories),
            patch.object(bridge.sys, "platform", "darwin"),
        ):
            del os.environ["PEER_BRIDGE_CODEX_BIN"]
            await self.consult("codex")
        self.assertEqual([call["executable"] for call in self.calls()], [str(bundled)])

    async def test_only_absolute_path_entries_find_the_peer_and_what_its_launcher_runs(self):
        # Empty and relative PATH entries search the current directory, which is the consulted repository for the
        # peer and for a launcher script that finds its helper by name, as ChatGPT's bundled codex does.
        trusted = self.root / "trusted-bin"
        trusted.mkdir()
        for folder, name, body in ((trusted, "claude", "exec helper \"$@\""), (trusted, "helper", f'exec "{self.peer}" "$@"'),
                                   (self.project, "claude", f"touch {self.root}/ran-claude; exit 1"),
                                   (self.project, "helper", f"touch {self.root}/ran-helper; exit 1")):
            (folder / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            (folder / name).chmod(0o755)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.project)
        del os.environ["PEER_BRIDGE_CLAUDE_BIN"]
        with patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "bin", str(trusted)])}):
            await self.consult()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(sorted(path.name for path in self.root.glob("ran-*")), [])

    async def test_missing_peer_cli_is_reported_without_starting_a_peer(self):
        with self.without_peer_on_path(), patch.object(bridge, "MACOS_APPLICATION_DIRECTORIES", (str(self.root),)):
            os.environ["PEER_BRIDGE_CODEX_BIN"] = str(self.root / "missing-codex")
            with self.assertRaisesRegex(bridge.BridgeError, "PEER_BRIDGE_CODEX_BIN does not name an executable"):
                await self.consult("codex")
            del os.environ["PEER_BRIDGE_CODEX_BIN"]
            for platform, expected in (("darwin", "bundles it at /Applications/ChatGPT.app/"),
                                       ("linux", "Install it and sign in")):
                with (
                    self.subTest(platform=platform),
                    patch.object(bridge.sys, "platform", platform),
                    self.assertRaisesRegex(bridge.BridgeError, expected),
                ):
                    await self.consult("codex")
        self.assertEqual(self.calls(), [])

    async def test_a_peer_that_cannot_start_is_reported_by_the_guard(self):
        broken = self.root / "broken-peer"
        broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
        broken.chmod(0o700)
        with (
            patch.dict(os.environ, {"PEER_BRIDGE_CLAUDE_BIN": str(broken)}),
            self.assertRaisesRegex(bridge.BridgeError, "Cannot start claude"),
        ):
            await self.consult()

    async def test_existing_private_state_directory_permissions_are_preserved(self):
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o300)
        try:
            with self.store.open("claude", self.project, None):
                self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o300)
        finally:
            self.state.chmod(0o700)

    async def test_a_shared_state_directory_is_rejected_without_mutation_or_peer_execution(self):
        self.state.mkdir(mode=0o750)
        self.state.chmod(0o750)
        marker = self.state / "unrelated.txt"
        marker.write_text("Shared folder content", encoding="utf-8")
        spawned = []
        self.client.spawn = spawned.append
        request = {"prompt": "Explain the query.", "working_directory": str(self.project)}
        for call in (self.client.consult, self.client.start):
            with self.subTest(call=call.__name__), self.assertRaisesRegex(bridge.BridgeError, "private directory"):
                await call("claude", **request)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o750)
        self.assertEqual(list(self.state.iterdir()), [marker])
        self.assertEqual((self.calls(), spawned), ([], []))

    async def test_cleanup_never_reaches_through_a_state_directory_that_is_not_private(self):
        # Each layout hides something cleanup would delete (a leftover checkout, a half-removed job) behind a path
        # that is shared or a symlink; checks that ran only after cleanup would have let it go.
        elsewhere = self.root / "elsewhere"
        spawned = []
        self.client.spawn = spawned.append
        leftover = str(uuid.uuid4())

        def candidates(base):
            for folder in (base / "worktrees" / leftover, base / "jobs" / f"{jobs.DELETING_PREFIX}{uuid.uuid4()}"):
                folder.mkdir(parents=True, mode=0o700)
                (folder / "sentinel").write_text("keep", encoding="utf-8")

        def shared_root():
            self.state.mkdir(mode=0o700)
            candidates(self.state)
            self.state.chmod(0o750)

        def linked_root():
            elsewhere.mkdir(mode=0o700)
            candidates(elsewhere)
            self.state.symlink_to(elsewhere, target_is_directory=True)

        def linked_children():
            self.state.mkdir(mode=0o700)
            elsewhere.mkdir(mode=0o700)
            candidates(elsewhere)
            for name in ("worktrees", "jobs"):
                (self.state / name).symlink_to(elsewhere / name, target_is_directory=True)

        def linked_checkout():
            (self.state / "worktrees").mkdir(parents=True, mode=0o700)
            self.state.chmod(0o700)
            elsewhere.mkdir(mode=0o700)
            candidates(elsewhere)
            (self.state / "worktrees" / leftover).symlink_to(elsewhere / "worktrees" / leftover)

        request = {"prompt": "Explain the query.", "working_directory": str(self.project)}
        for layout, refused in ((shared_root, True), (linked_root, True), (linked_children, True),
                                (linked_checkout, False)):
            with self.subTest(layout=layout.__name__):
                layout()
                try:
                    for call in (self.client.consult, self.client.start):
                        if refused:
                            with self.assertRaisesRegex(bridge.BridgeError, "private directory"):
                                await call("claude", **request)
                        elif call == self.client.consult:
                            await call("claude", **request)
                    sentinels = sorted((self.state if layout is shared_root else elsewhere).rglob("sentinel"))
                    self.assertEqual(len(sentinels), 2)
                    self.assertEqual(spawned, [])
                finally:
                    if self.state.is_symlink():
                        self.state.unlink()
                    elif self.state.exists():
                        self.state.chmod(0o700)
                        shutil.rmtree(self.state)
                    shutil.rmtree(elsewhere, ignore_errors=True)

    def pinned_repository(self):
        git_repository(self.project)
        first, second = self.commit_marker("first"), self.commit_marker("second")
        (self.project / "pkg" / "marker.txt").write_text("uncommitted edit", encoding="utf-8")
        return first, second

    def commit_marker(self, content):
        package = self.project / "pkg"
        package.mkdir(exist_ok=True)
        (package / "marker.txt").write_text(content, encoding="utf-8")
        git(self.project, "add", "pkg")
        git(self.project, "commit", "--quiet", "-m", content)
        return git(self.project, "rev-parse", "HEAD")

    def registered_worktrees(self):
        listing = git(self.project, "worktree", "list", "--porcelain")
        return [line for line in listing.splitlines() if line.startswith("worktree ")]

    async def test_a_relative_store_directory_still_places_checkouts_in_it(self):
        first, _ = self.pinned_repository()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        self.client.store = bridge.SessionStore(self.state.name)
        pinned = await self.consult(ref=first)
        self.assertEqual(pinned["commit"], first)
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertFalse((self.project / self.state.name).exists())
        self.assertEqual(len(self.calls()), 1)

    async def test_followup_is_bound_to_provider_and_canonical_directory(self):
        identifier = (await self.consult())["conversation_id"]
        alias = self.root / "project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        resumed = await self.consult(working_directory=str(alias), conversation_id=identifier)
        self.assertEqual(resumed["conversation_id"], identifier)
        with self.assertRaisesRegex(bridge.BridgeError, "different provider or working directory"):
            await self.consult("codex", conversation_id=identifier)
        self.assertEqual(len(self.calls()), 2)

    async def test_unknown_and_raw_backend_identifiers_do_not_start_a_peer(self):
        first = await self.consult()
        raw_session = self.calls()[0]["session"]
        self.assertNotEqual(first["conversation_id"], raw_session)
        for identifier in (str(uuid.uuid4()), raw_session):
            with (
                self.subTest(identifier=identifier),
                self.assertRaisesRegex(bridge.BridgeError, "Unknown or unreadable conversation"),
            ):
                await self.consult(conversation_id=identifier)
        self.assertEqual(len(self.calls()), 1)

    async def test_concurrent_resume_fails_before_starting_another_peer(self):
        identifier = (await self.consult())["conversation_id"]
        with (
            self.store.open("claude", self.project, identifier),
            self.assertRaisesRegex(bridge.BridgeError, "conversation is busy"),
        ):
            await self.consult(conversation_id=identifier)
        self.assertEqual(len(self.calls()), 1)

    async def test_invalid_or_nested_requests_start_nothing(self):
        spawned = []
        self.client.spawn = spawned.append
        cases = (({"prompt": " "}, "question is required"), ({"working_directory": "relative"}, "absolute path"),
                 ({"working_directory": str(self.root / "missing")}, "absolute path"),
                 ({"conversation_id": "not-a-uuid"}, "Invalid conversation"))
        for call in (self.client.consult, self.client.start):
            for arguments, expected in cases:
                request = {"prompt": "Explain the query.", "working_directory": str(self.project), **arguments}
                with self.subTest(call=call.__name__, arguments=arguments):
                    with self.assertRaisesRegex(bridge.BridgeError, expected):
                        await call("claude", **request)
            with (
                patch.dict(os.environ, {bridge.ACTIVE_ENV: "1"}),
                self.assertRaisesRegex(bridge.BridgeError, "Nested peer consultations"),
            ):
                await call("claude", "Explain the query.", str(self.project))
        self.assertEqual((self.calls(), spawned), ([], []))
        self.assertFalse(self.state.exists())

    async def test_prompt_is_stdin_data_without_shell_execution(self):
        marker = self.root / "injected-file"
        prompt = f"$(touch '{marker}'); `touch '{marker}'`\n--resume injected-session"
        result = await self.consult(prompt=prompt, context="Literal supporting context.")
        call = self.calls()[0]
        self.assertIn(prompt, call["prompt"])
        self.assertIn("Literal supporting context.", call["prompt"])
        self.assertNotIn(prompt, call["arguments"])
        self.assertIn(prompt, result["reply"])
        self.assertFalse(marker.exists())

    # Failures

    async def test_cli_failure_does_not_expose_prompt_context_or_backend_output(self):
        with (
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "failure"}),
            self.assertRaises(bridge.BridgeError) as raised,
        ):
            await self.consult(prompt="PRIVATE QUESTION", context="PRIVATE LEAD CONTEXT")
        message = str(raised.exception)
        self.assertIn("exit 7", message)
        for private in ("PRIVATE QUESTION", "PRIVATE LEAD CONTEXT", "PRIVATE BACKEND CONTEXT"):
            self.assertNotIn(private, message)
        self.assertEqual(list(self.state.glob("*.json")), [])

    async def failure_message(self, provider, environment, **arguments):
        """Run a consultation expected to fail, and return its message after checking it stays private."""
        with (
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "known_failure", **environment}),
            self.assertRaises(bridge.BridgeError) as raised,
        ):
            await self.consult(provider, prompt="PRIVATE QUESTION", **arguments)
        message = str(raised.exception)
        self.assertNotIn("PRIVATE", message)
        return message

    async def test_cli_text_alone_never_names_a_cause(self):
        git_repository(self.project)
        misleading = ("thread already has an active writer; 401 Unauthorized; Not logged in; Please run /login; "
                      "model is not supported; No conversation found with session ID; Not inside a trusted directory; "
                      "note: [claude-code:unrecognized_model]")
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                message = await self.failure_message(provider, {"SIMULATED_FAILURE_TEXT": misleading})
                self.assertEqual(message, f"{provider} consultation failed (exit 1). Check CLI login, version, and "
                                          "permissions.")
        message = await self.failure_message("claude", {"SIMULATED_PEER_MODE": "error_result"})
        self.assertTrue(message.startswith("Claude did not complete the consultation."))
        self.assertEqual(list(self.state.glob("*.json")), [])

    async def test_failures_are_named_from_the_facts_the_bridge_gathers(self):
        git_repository(self.project)
        cases = (  # one row per fact: sign-in probes, the result's status field, the stderr tag, settings, .git
            ("claude", {"SIMULATED_SIGNED_IN": "0"}, {}, "Claude Code is not signed in"),
            ("codex", {"SIMULATED_SIGNED_IN": "0"}, {}, "Codex is not signed in"),
            ("claude", {"SIMULATED_PEER_MODE": "api_error", "SIMULATED_API_STATUS": "401"}, {},
             "Claude Code is not signed in"),
            ("claude", {"SIMULATED_FAILURE_TEXT": '[claude-code:unrecognized_model] {"model":"x"}'}, {},
             "Claude Code rejected the requested model."),
            ("codex", {"PEER_BRIDGE_CODEX_MODEL": "gpt-x"}, {}, "codex consultation failed (exit 1). Check CLI "
             "login, version, and permissions." + consultation.MODEL_HINT),
            ("codex", {}, {"reasoning_effort": "ultra"}, "codex consultation failed (exit 1). Check CLI "
             "login, version, and permissions." + consultation.MODEL_HINT),
        )
        for provider, environment, arguments, expected in cases:
            with self.subTest(provider=provider, environment=environment, arguments=arguments):
                message = await self.failure_message(provider, environment, **arguments)
                self.assertTrue(message.startswith(expected), message)
        message = await self.failure_message("codex", {}, working_directory=str(self.other_project))
        self.assertEqual(message, consultation.OUTSIDE_GIT)
        self.assertEqual(list(self.state.glob("*.json")), [])

    async def test_failure_probes_share_the_peers_deadline_and_conversation_lock(self):
        git_repository(self.project)
        self.client.timeout = 5
        sign_in = str(self.peer), 2
        for peer_seconds, sign_in_seconds, expected in (
            (3, 2, [sign_in]),
            (5, 0, []),
        ):
            with self.subTest(peer_seconds=peer_seconds, sign_in_seconds=sign_in_seconds):
                clock, guarded = SimpleNamespace(now=100.0), []

                async def recording(command, prompt, directory, timeout, environment=None, lock=None):
                    guarded.append((command[0], timeout, lock))
                    if prompt is not None:
                        clock.now += peer_seconds
                        return 7, b"", b""
                    if command[1:] == ["login", "status"]:
                        clock.now += sign_in_seconds
                    return 0, b"/project/.git\n", b""

                self.client.store = bridge.SessionStore(self.state, clock=lambda: clock.now, runner=recording)
                message = await self.failure_message("codex", {})
                self.assertEqual(message, "codex consultation failed (exit 7). Check CLI login, version, and "
                                 "permissions.")
                (_, _, lock), *probes = guarded
                self.assertIsInstance(lock, int)
                self.assertEqual(probes, [(executable, timeout, lock) for executable, timeout in expected])

    async def test_a_cancelled_sign_in_probe_is_stopped_before_the_cancellation_passes_on(self):
        # A failed run asks the CLI whether it is signed in; a job is reported cancelled only once that probe, and any
        # helper it started, has stopped.
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "failure", "SIMULATED_PROBE_HANGS": "1"}):
            task = asyncio.create_task(self.consult())
            await eventually_async((self.root / "probe-child.pid").exists, "The sign-in probe did not start.")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(running(read_pid(self.root / "probe-child.pid")))

    async def test_resumed_consultation_failures_check_the_peer_history_and_writer_lock(self):
        git_repository(self.project)
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                conversation = (await self.consult(provider))["conversation_id"]
                record = json.loads((self.state / f"{conversation}.json").read_text())
                session = record["backend_session_id"]
                message = await self.failure_message(provider, {}, conversation_id=conversation)
                self.assertIn("no longer has this conversation's history", message)
                if provider == "claude":
                    history = self.claude_home / "projects" / "-project" / f"{session}.jsonl"
                else:
                    history = self.codex_home / "sessions" / "2026" / f"rollout-2026-11-12T00-00-00-{session}.jsonl"
                history.parent.mkdir(parents=True)
                history.write_text("{}\n")
                message = await self.failure_message(provider, {}, conversation_id=conversation)
                self.assertTrue(message.startswith(f"{provider} consultation failed (exit 1)"))
                if provider == "codex":
                    lock = self.codex_home / "thread-writer-locks" / f"{session}.lock"
                    lock.parent.mkdir()
                    lock.touch()
                    message = await self.failure_message(provider, {}, conversation_id=conversation)
                    self.assertIn("open elsewhere", message)
                self.assertEqual(json.loads((self.state / f"{conversation}.json").read_text()), record)

    async def test_failure_before_the_peer_reads_its_input_is_still_diagnosed(self):
        git_repository(self.project)
        large_context = "x" * (2 * 1024 * 1024)
        for exit_status, signed_in, expected in (("1", "0", "Codex is not signed in"),
                                                 ("1", "1", "closed its input"), ("0", "1", "closed its input")):
            with (
                self.subTest(exit_status=exit_status, signed_in=signed_in),
                patch.dict(os.environ, {"SIMULATED_PEER_MODE": "early_exit", "SIMULATED_EXIT_STATUS": exit_status,
                                        "SIMULATED_SIGNED_IN": signed_in}),
                self.assertRaises(bridge.BridgeError) as raised,
            ):
                await self.consult("codex", context=large_context)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertEqual(list(self.state.glob("*.json")), [])

    async def test_unexpected_backend_session_change_preserves_previous_metadata(self):
        first = await self.consult()
        record = self.state / f"{first['conversation_id']}.json"
        previous = record.read_text(encoding="utf-8")
        with (
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "changed_session"}),
            self.assertRaisesRegex(bridge.BridgeError, "unexpectedly changed sessions"),
        ):
            await self.consult(conversation_id=first["conversation_id"])
        self.assertEqual(record.read_text(encoding="utf-8"), previous)

    # Process lifetime: every way a consultation ends stops the peer's whole process group.

    async def test_timeout_is_reported_without_saving_the_conversation(self):
        # The peer never answers, so the timeout fires whatever the machine's speed.
        self.client.timeout = 0.5
        with (
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "hang"}),
            self.assertRaisesRegex(bridge.BridgeError, "timed out"),
        ):
            await self.consult()
        self.assertEqual(list(self.state.glob("*.json")), [])

    async def test_oversized_output_stops_the_peer(self):
        with (
            patch.object(bridge, "MAX_OUTPUT_BYTES", 1024),
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "flood"}),
            self.assertRaisesRegex(bridge.BridgeError, "exceeded the size limit"),
        ):
            await self.consult()
        await self.assert_processes_stopped(("parent.pid",))

    async def test_reply_is_returned_while_a_descendant_still_holds_the_pipes(self):
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "lingering_descendant"}):
            result = await self.consult()
        self.assertIn("Explain the query.", result["reply"])
        await self.assert_processes_stopped()

    async def test_repeated_cancellation_still_kills_the_peer_and_removes_its_checkout(self):
        first, _ = self.pinned_repository()

        async def cancel_when_the_child_starts(scope):
            await self.wait_for_child()
            scope.cancel()

        # MCP requests run in an anyio cancel scope, which cancels again at every await once the request is cancelled.
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang", "SIMULATED_PEER_IGNORES_SIGTERM": "1"}):
            with anyio.CancelScope() as scope:
                canceller = asyncio.create_task(cancel_when_the_child_starts(scope))
                await self.consult(ref=first)
            await canceller
        self.assertTrue(scope.cancelled_caught)
        await self.assert_processes_stopped()
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertEqual(list((self.state / "worktrees").iterdir()), [])

    async def test_cli_repeated_termination_still_kills_the_peer_and_removes_its_checkout(self):
        first, _ = self.pinned_repository()
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang", "SIMULATED_PEER_IGNORES_SIGTERM": "1"}):
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(BRIDGE_PATH), "ask", "--provider", "claude", "--working-directory",
                str(self.project), "--ref", first,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            process.stdin.write(b"Explain the query.")
            await process.stdin.drain()
            process.stdin.close()
            await self.wait_for_child()
            process.send_signal(signal.SIGTERM)
            # The guard has signalled the peer, which ignores it: the bridge is now in its cleanup, and later
            # signals arrive during the guard's grace period.
            await eventually_async((self.root / "terminated").exists, "The guard never signalled the peer.")
            for signal_number in (signal.SIGHUP, signal.SIGTERM):
                try:
                    process.send_signal(signal_number)
                except ProcessLookupError:
                    break
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        self.assertEqual((process.returncode, json.loads(stdout)), (1, {"error": "The consultation was cancelled."}))
        await self.assert_processes_stopped()
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertEqual(list((self.state / "worktrees").iterdir()), [])

    async def test_the_last_group_signal_comes_before_the_guard_is_reaped(self):
        # Reaping frees the guard's ID, which is also its group's, so no signal may follow it, whether the guard exited
        # early (killed), exits once its lifeline closes, or stalls past the deadline.
        events = []

        class Guard:
            """Stands in for the guard's Popen: poll() reaps it, as only the bridge's own poll() does."""
            pid = 2 ** 22 + 7
            returncode = None

            def poll(self):
                if self.returncode is None:
                    events.append("reap")
                    self.returncode = -signal.SIGKILL
                return self.returncode

        loop = asyncio.get_running_loop()
        for case, margin in (("exited before", DEADLINE_SECONDS), ("exits on lifeline", DEADLINE_SECONDS),
                             ("stalled", 0.2)):
            events.clear()
            reading, lifeline = os.pipe()
            status, writer = os.pipe()
            os.set_blocking(status, False)
            self.addCleanup(self.close_quietly, reading, status)
            if case == "stalled":
                self.addCleanup(os.close, writer)
            elif case == "exited before":
                os.close(writer)  # the guard's end of its status pipe closes only when it exits
            elif case == "exits on lifeline":
                loop.add_reader(reading, lambda: (loop.remove_reader(reading), os.close(writer)))
            with (
                self.subTest(case=case),
                patch.object(bridge.os, "killpg", lambda pid, number: events.append(("signal", pid, number))),
                patch.object(bridge, "GUARD_MARGIN_SECONDS", margin),
                patch.object(bridge.peer_guard, "GRACE_SECONDS", 0),
            ):
                await bridge.stop_peer(Guard(), lifeline, status)
                self.assertEqual(events, [("signal", Guard.pid, signal.SIGKILL), "reap"])

    @staticmethod
    def close_quietly(*descriptors):
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    async def test_a_stalled_guard_is_killed_with_its_whole_group(self):
        # A guard that cannot act on its closed lifeline, here stopped, must not leave its peer behind. Only the
        # bridge's wait for the guard is shortened; the guard runs in its own process with its own grace period.
        with (
            patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang"}),
            patch.object(bridge, "GUARD_MARGIN_SECONDS", 0.5),
            patch.object(bridge.peer_guard, "GRACE_SECONDS", 0.5),
        ):
            task = asyncio.create_task(self.consult())
            await self.wait_for_child()
            peer = read_pid(self.root / "parent.pid")
            guard = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(peer)], capture_output=True,
                                       text=True).stdout)
            os.kill(guard, signal.SIGSTOP)
            self.addCleanup(lambda: running(guard) and os.kill(guard, signal.SIGKILL))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_processes_stopped()
        self.assertFalse(running(guard))

    async def test_a_guard_killed_by_a_signal_takes_its_peer_with_it(self):
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang"}):
            task = asyncio.create_task(self.consult())
            await self.wait_for_child()
            peer = read_pid(self.root / "parent.pid")
            guard = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(peer)], capture_output=True,
                                       text=True).stdout)
            os.kill(guard, signal.SIGKILL)
            with self.assertRaisesRegex(bridge.BridgeError, "stopped unexpectedly"):
                await task
        await self.assert_processes_stopped()

    async def test_a_failed_pipe_allocation_leaves_no_descriptor_open(self):
        real_pipe, calls = os.pipe, []

        def second_fails():
            if calls:
                calls.append(None)
                raise OSError(24, "Too many open files")
            calls.append(real_pipe())
            return calls[0]

        with patch.object(bridge.os, "pipe", second_fails), self.assertRaisesRegex(bridge.BridgeError, "Cannot start"):
            await self.consult()
        self.assertEqual(len(calls), 2)
        for descriptor in calls[0]:  # the first pipe's ends are closed, whatever else the process has open
            with self.subTest(descriptor=descriptor), self.assertRaises(OSError) as raised:
                os.fstat(descriptor)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    # Pinned checkouts

    async def test_pinned_ref_consults_a_detached_checkout_and_follow_ups_keep_its_directory(self):
        # Claude resumes a session only from the directory it ran in, so the checkout's path is fixed per conversation.
        first, second = self.pinned_repository()
        package = self.project / "pkg"
        pinned = await self.consult(working_directory=str(package), ref=first[:12])
        identifier = pinned["conversation_id"]
        self.assertEqual(pinned["commit"], first)
        same = await self.consult(working_directory=str(package), conversation_id=identifier)
        moved = await self.consult(working_directory=str(package), conversation_id=identifier, ref="HEAD")
        self.assertEqual((same["commit"], moved["commit"]), (first, second))
        calls = self.calls()
        self.assertEqual([call["marker"] for call in calls], ["first", "first", "second"])
        checkout = self.state / "worktrees" / identifier / "pkg"
        self.assertEqual({call["cwd"] for call in calls}, {str(checkout)})
        self.assertEqual(len({call["session"] for call in calls}), 1)
        self.assertFalse(checkout.parent.exists())
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertEqual((package / "marker.txt").read_text(encoding="utf-8"), "uncommitted edit")

    async def test_pinned_consultation_tells_the_peer_where_its_checkout_root_is(self):
        first, _ = self.pinned_repository()
        await self.consult(ref=first)
        await self.consult(working_directory=str(self.project / "pkg"), ref=first)
        await self.consult()
        *pinned_prompts, unpinned = (call["prompt"] for call in self.calls())
        for prompt, root in zip(pinned_prompts, ("the current directory", "`..`"), strict=True):
            self.assertIn(f"temporary checkout of commit {first}", prompt)
            self.assertIn(f"Read files only inside this checkout, whose root is {root},", prompt)
        self.assertNotIn("temporary checkout", unpinned)

    async def test_inherited_git_location_does_not_redirect_the_peer_from_its_checkout(self):
        first, _ = self.pinned_repository()
        other = git_repository(self.root / "other-checkout")
        other_head = git(other, "rev-parse", "HEAD")
        inherited = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
        with patch.dict(os.environ, inherited):
            pinned = await self.consult("codex", ref=first)
            await self.consult("claude")
            self.assertEqual({name: os.environ[name] for name in inherited}, inherited)
        pinned_call, plain_call = self.calls()
        self.assertEqual((pinned_call["git_location"], plain_call["git_location"]), ([], []))
        self.assertEqual(pinned_call["head"], pinned["commit"])
        self.assertNotEqual(plain_call["head"], other_head)

    async def test_invalid_or_unpinned_refs_do_not_start_a_peer(self):
        first, _ = self.pinned_repository()
        unpinned = await self.consult()
        (self.root / "not-a-repository").mkdir()
        injected = self.root / "injected"
        cases = (
            ({"ref": "no-such-ref"}, "does not name a commit"),
            ({"ref": f"--output={injected}"}, "does not name a commit"),
            ({"ref": first, "working_directory": str(self.root / "not-a-repository")}, "inside a git repository"),
            ({"ref": first, "conversation_id": unpinned["conversation_id"]}, "not pinned to a ref"),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(bridge.BridgeError, expected):
                await self.consult(**arguments)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.registered_worktrees()), 1)
        self.assertFalse(injected.exists())

    async def test_a_missing_directory_or_a_leftover_checkout_leaves_no_worktree_behind(self):
        first, _ = self.pinned_repository()
        (self.project / "later").mkdir()
        with self.assertRaisesRegex(bridge.BridgeError, "does not exist at that ref"):
            await self.consult(working_directory=str(self.project / "later"), ref=first)
        self.assertEqual(len(self.registered_worktrees()), 1)
        # A server killed mid-run leaves its checkout; the conversation's next consultation replaces it.
        identifier = (await self.consult(ref=first))["conversation_id"]
        leftover = self.state / "worktrees" / identifier
        leftover.mkdir()
        (leftover / "stale.txt").write_text("left by a killed server", encoding="utf-8")
        resumed = await self.consult(conversation_id=identifier)
        self.assertEqual((resumed["commit"], self.calls()[-1]["cwd"]), (first, str(leftover)))
        self.assertFalse(leftover.exists())
        self.assertEqual(len(self.registered_worktrees()), 1)
        # A checkout another conversation's dead run left, registered with git, goes at the next consultation; one
        # whose conversation is running stays.
        dead, running = (self.state / "worktrees" / str(uuid.uuid4()) for _ in range(2))
        for folder in (dead, running):
            git(self.project, "worktree", "add", "--detach", "--quiet", str(folder), first)
        with self.client.store.holding(running.name) as held:
            self.assertTrue(held)
            await self.consult()
        self.assertEqual((dead.exists(), running.exists()), (False, True))
        self.assertEqual(len(self.registered_worktrees()), 2)

    async def test_pinning_runs_none_of_the_repository_code(self):
        # Preparing a checkout must not run a hook, a filter or a symlink of the repository's: none is sandboxed.
        git_repository(self.project)
        ran = self.root / "ran"
        hooks = self.project / "hooks"
        hooks.mkdir()
        (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {ran}-hook\n", encoding="utf-8")
        (hooks / "post-checkout").chmod(0o755)
        # A driver name with "=" in it, which `-c filter.<name>.smudge=` would split at the wrong place.
        (self.project / ".gitattributes").write_text("*.txt filter=x=y\n", encoding="utf-8")
        git(self.project, "config", "filter.x=y.clean", "cat")
        (self.project / "notes.txt").write_text("notes\n", encoding="utf-8")
        package = self.project / "pkg"
        package.symlink_to(self.root)
        git(self.project, "add", ".")
        git(self.project, "commit", "--quiet", "-m", "linked")
        linked = git(self.project, "rev-parse", "HEAD")
        package.unlink()
        package.mkdir()
        (package / "marker.txt").write_text("real", encoding="utf-8")
        git(self.project, "add", "-A")
        git(self.project, "commit", "--quiet", "-m", "real")
        for setting, value in (("core.hooksPath", "hooks"), ("filter.x=y.smudge", f"touch {ran}-smudge; cat"),
                               ("filter.x=y.process", f"touch {ran}-process"), ("filter.x=y.required", "true")):
            git(self.project, "config", setting, value)
        pinned = await self.consult(ref="HEAD")
        self.assertEqual(pinned["commit"], git(self.project, "rev-parse", "HEAD"))
        with self.assertRaisesRegex(bridge.BridgeError, "does not exist at that ref"):
            await self.consult(working_directory=str(package), ref=linked)
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.name.startswith("ran")), [])
        self.assertEqual(len(self.calls()), 1)

    async def test_pinning_ignores_what_would_change_the_checkout_or_run_a_command(self):
        # Configuration seen only where the files are written, an inherited GIT_CONFIG, a replacement object and a
        # partial clone's lazy fetch each would run repository code or check out other content than the commit named.
        ran = self.root / "ran"
        with self.subTest("filter configured only for linked worktrees, with an inherited GIT_CONFIG"):
            git_repository(self.project)
            (self.project / ".gitattributes").write_text("*.txt filter=late\n", encoding="utf-8")
            (self.project / "marker.txt").write_text("stored\n", encoding="utf-8")
            git(self.project, "add", ".")
            git(self.project, "commit", "--quiet", "-m", "filtered")
            linked_only = self.root / "linked-only.config"
            linked_only.write_text(f'[filter "late"]\n\tsmudge = "touch {ran}-late; cat"\n\trequired = true\n',
                                   encoding="utf-8")
            git(self.project, "config", "includeIf.gitdir:**/worktrees/**.path", str(linked_only))
            empty = self.root / "empty.config"
            empty.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {"GIT_CONFIG": str(empty)}):
                pinned = await self.consult(ref="HEAD")
            self.assertEqual(self.calls()[-1]["marker"], "stored\n")
            self.assertFalse(ran.with_name("ran-late").exists())
        with self.subTest("replacement object"):
            original = pinned["commit"]
            (self.project / "marker.txt").write_text("replaced\n", encoding="utf-8")
            git(self.project, "commit", "--quiet", "-am", "replacement")
            git(self.project, "replace", original, git(self.project, "rev-parse", "HEAD"))
            replaced = await self.consult(ref=original)
            self.assertEqual((replaced["commit"], self.calls()[-1]["marker"]), (original, "stored\n"))
        with self.subTest("partial clone with a promisor remote"):
            git(self.project, "config", "uploadpack.allowFilter", "true")
            clone = self.root / "partial"
            git(self.root, "clone", "--quiet", "--filter=blob:none", "--no-checkout", f"file://{self.project}",
                str(clone))
            transport = self.root / "transport.sh"
            transport.write_text(f"#!/bin/sh\ntouch {ran}-transport\nexit 1\n", encoding="utf-8")
            transport.chmod(0o755)
            git(clone, "config", "remote.origin.url", f"ext::{transport}")
            git(clone, "config", "protocol.ext.allow", "always")
            calls = len(self.calls())
            with self.assertRaisesRegex(bridge.BridgeError, "Cannot check out the ref"):
                await self.consult(working_directory=str(clone), ref="HEAD")
            self.assertFalse(ran.with_name("ran-transport").exists())
            self.assertEqual(len(self.calls()), calls)

    async def test_removing_a_pinned_checkout_leaves_other_worktrees_registered(self):
        # A worktree on a drive that is not mounted looks just like a deleted one; only the bridge's own record goes.
        git_repository(self.project)
        unmounted = self.root / "unmounted"
        git(self.project, "worktree", "add", "--detach", "--quiet", str(unmounted))
        shutil.rmtree(unmounted)
        await self.consult(ref="HEAD")
        names = [Path(line.split(" ", 1)[1]).name for line in self.registered_worktrees()]
        self.assertEqual(names, [self.project.name, "unmounted"])

    async def test_checkout_preparation_shares_one_budget_and_removal_has_its_own(self):
        git_repository(self.project)
        clock = SimpleNamespace(now=1000.0)
        preparation, removal = bridge.CHECKOUT_PREPARATION_SECONDS, bridge.CHECKOUT_REMOVAL_SECONDS
        for step, late, expected in (
            (10, False, [("rev-parse", preparation), ("rev-parse", preparation - 10), ("worktree", preparation - 20),
                  ("worktree", preparation - 30), ("config", preparation - 40), ("read-tree", preparation - 50),
                  ("peer", self.client.timeout), ("worktree", removal)]),
            # Out of time before the checkout is added: nothing more is started, and the removal still runs.
            (preparation / 3, True, [("rev-parse", preparation), ("rev-parse", preparation * 2 / 3),
                               ("worktree", preparation / 3), ("worktree", removal)]),
        ):
            calls = []

            async def recording(command, prompt, directory, timeout, **options):
                name = "peer" if prompt is not None else command[3].removeprefix(f"--work-tree={command[2]}")
                calls.append((name or command[4], timeout))
                clock.now += step
                return await bridge.run_command(command, prompt, directory, timeout, **options)

            self.client.store = bridge.SessionStore(self.state, clock=lambda: clock.now, runner=recording)
            with self.subTest(step=step):
                if late:
                    with self.assertRaisesRegex(bridge.BridgeError, "git did not finish in time"):
                        await self.consult(ref="HEAD")
                else:
                    await self.consult(ref="HEAD")
            self.assertEqual(calls, expected)
            self.assertEqual(len(self.registered_worktrees()), 1)  # only the project's own

    async def test_thread_reading_and_seed_thread_are_off_unless_turned_on(self):
        git_repository(self.project)
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Verdict: ship it."), name="Desktop review")
        spawned = []
        self.client.spawn = spawned.append
        request = {"prompt": "Explain the query.", "working_directory": str(self.project), "seed_thread": "t1"}
        for value in (None, "true", "0"):
            with self.subTest(value=value), patch.dict(os.environ, {"PEER_BRIDGE_THREADS": value or ""}):
                if value is None:
                    del os.environ["PEER_BRIDGE_THREADS"]
                for call in (lambda: self.client.threads("codex", str(self.project)),
                             lambda: self.client.thread("codex", str(self.project), "t1"),
                             lambda: self.client.consult("codex", **request),
                             lambda: self.client.start("codex", **request)):
                    with self.assertRaisesRegex(bridge.BridgeError, "PEER_BRIDGE_THREADS=1"):
                        await call()
        self.assertEqual((self.calls(), spawned, list((self.state / "jobs").glob("*"))), ([], [], []))

    async def test_seed_thread_carries_recent_agent_messages_into_a_fresh_consultation(self):
        git_repository(self.project)
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Looking at it.", "Verdict: ship it."),
                     name="Desktop review")
        await self.consult("codex", context="Lead context.", seed_thread="Desktop review")
        with self.assertRaisesRegex(bridge.BridgeError, "No readable thread with that ID"):
            await self.consult("codex", seed_thread="Missing")
        self.assertEqual(len(self.calls()), 1)
        prompt = self.calls()[0]["prompt"]
        self.assertIn('Recent agent messages from the Codex thread "Desktop review"', prompt)
        self.assertLess(prompt.index("Verdict: ship it."), prompt.index("Lead context."))
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, prompt)


if __name__ == "__main__":
    unittest.main()
