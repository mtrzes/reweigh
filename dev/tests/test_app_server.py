"""Exercise the shadow app-server source and the parity check against a stand-in `codex app-server`."""

import fcntl
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


# Lets a single test file run on its own, as well as through discover.
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "dev"), str(ROOT / "plugins" / "peer-bridge" / "tests")]
from support import codex_thread, codex_turn  # noqa: E402  (support adds the plugin's scripts to the path)
import codex_app_server as app  # noqa: E402
from thread_reader import CodexFilesSource  # noqa: E402

# Answers from FAKE_SERVER_DATA, sending one server request before the first list response by default.
FAKE_SERVER = textwrap.dedent('''\
    import json, os, sys
    data = json.load(open(os.environ["FAKE_SERVER_DATA"]))
    log = open(os.environ["FAKE_SERVER_LOG"], "a")
    ask_last = data.get("request_after_last_list")
    asked = False
    def send(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        message = json.loads(line)
        log.write(json.dumps(message) + "\\n")
        log.flush()
        method, params = message.get("method"), message.get("params") or {}
        if ask_last and message.get("id") == "server-1":
            break
        if "id" not in message or method is None:
            continue
        if method == data.get("fail_method"):
            send({"id": message["id"], "error": {"code": -32603, "message": "fixture"}})
            continue
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "thread/list":
            if not asked and not ask_last and not data.get("no_server_requests"):
                asked = True
                send({"id": "server-1", "method": "item/commandExecution/requestApproval", "params": {}})
                send({"method": "thread/started", "params": {}})
            result = {"data": [t for t in data["threads"] if t.get("archived", False) == params["archived"]]}
        elif method == "thread/turns/list":
            turns = data["turns"].get(params["threadId"], [])
            start = int(params.get("cursor", 0))
            end = start + params["limit"]
            result = {"data": turns[start:end], "nextCursor": str(end) if end < len(turns) else None}
        elif method == "thread/items/list":
            result = {"data": data["items"].get(params["turnId"], [])}
        else:
            send({"id": message["id"], "error": {"code": -32601, "message": "unknown"}})
            continue
        response = {"id": message["id"], "result": result}
        if ask_last and method == "thread/list" and params["archived"]:
            request = {"id": "server-1", "method": "item/commandExecution/requestApproval", "params": {}}
            os.write(sys.stdout.fileno(), (json.dumps(response) + "\\n" + json.dumps(request) + "\\n").encode())
        else:
            send(response)
''')


class AppServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-app-server-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
        self.sandbox = self.root / "sandbox"
        self.data_path, self.log_path = self.root / "data.json", self.root / "log.jsonl"
        self.server = self.root / "codex"
        self.server.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
        self.server.chmod(0o700)
        self.data = {"threads": [], "turns": {}, "items": {}}

    def client(self):
        self.data_path.write_text(json.dumps(self.data))
        client = app.AppServerClient(str(self.server), self.home, self.sandbox)
        client.environment |= {"FAKE_SERVER_DATA": str(self.data_path), "FAKE_SERVER_LOG": str(self.log_path)}
        return client

    def sent(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def parity_run(self, version, **kwargs):
        with patch.dict(os.environ, CODEX_HOME=str(self.home.resolve())), \
                patch.object(app, "desktop_codex_executable", return_value=(str(self.server), "fake")), \
                patch.object(app, "codex_version", return_value=version), \
                patch.object(app, "AppServerClient", return_value=self.client()):
            return app.run_parity(self.root.resolve() / "state", **kwargs)

    def existing_parity_copy(self):
        root = self.root.resolve() / "state" / "parity"
        copy = root / "codex-home"
        copy.mkdir(parents=True)
        (copy / "copy.json").write_text(json.dumps({"copy_id": "c1", "created_at": "2026-11-01"}))
        return root

    def add_thread(self, identifier, turns, cwd="/repo", **fields):
        self.data["threads"].append({"id": identifier, "source": "vscode", "cwd": cwd, "updatedAt": 1, **fields})
        self.data["turns"][identifier] = [{"id": turn, "status": status} for turn, status, _ in reversed(turns)]
        for turn, _, texts in turns:
            if isinstance(texts, str):
                texts = (texts,)
            self.data["items"][turn] = [
                {"turnId": turn, "item": {"type": "userMessage", "content": []}},
                {"turnId": turn, "item": {"type": "commandExecution"}},
                *({"turnId": turn, "item": {"type": "agentMessage", "text": text,
                                           "phase": "final_answer" if i == len(texts) - 1 else "commentary"}}
                  for i, text in enumerate(texts)),
            ]

    def test_only_allowlisted_messages_are_sent_and_server_requests_are_refused(self):
        with self.client() as client:
            for method, params, reason in (
                ("turn/start", {}, "not an allowed request"),
                ("thread/list", {"useStateDbOnly": False}, "must set useStateDbOnly"),
                ("thread/read", {"threadId": "x", "includeTurns": True}, "must not include turns"),
            ):
                with self.subTest(method=method), self.assertRaisesRegex(app.AppServerError, reason):
                    client.request(method, params)
            with self.assertRaisesRegex(app.AppServerError, "not an allowed notification"):
                client.notify("thread/archive")
            listing = app.AppServerSource(client).list_threads(lambda cwd: True)
        self.assertEqual(listing.threads, [])
        self.assertEqual(client.server_requests, ["item/commandExecution/requestApproval"])
        self.assertEqual(client.notifications, ["thread/started"])
        sent = self.sent()
        self.assertEqual([message.get("method") for message in sent if "method" in message],
                         ["initialize", "initialized", "thread/list", "thread/list"])
        refusal = next(message for message in sent if message.get("id") == "server-1")
        self.assertEqual(refusal["error"]["code"], -32601)
        self.assertTrue(all(message["params"]["useStateDbOnly"] for message in sent
                            if message.get("method") == "thread/list"))

    def test_every_written_message_passes_the_gate(self):
        allowed = ({"id": 1, "method": "thread/items/list", "params": {"threadId": "x"}}, {"method": "initialized"},
                   {"id": "server-1", "error": app.REFUSAL})
        for message in allowed:
            with self.subTest(message=message):
                app.check_outgoing(message)
        refused = (
            {"id": 1, "method": "turn/start", "params": {}},
            {"id": True, "method": "thread/list", "params": {"useStateDbOnly": True}},
            {"id": 1, "method": "thread/list", "params": {"useStateDbOnly": True}, "extra": 1},
            {"id": 1, "method": "thread/read", "params": "x"},
            {"id": "server-1", "result": {"decision": "approved"}},
            {"id": "server-1", "error": {"code": -32601, "message": "other"}},
            {"method": "initialized", "params": {}},
            {"method": "thread/archive"},
            [],
        )
        for message in refused:
            with self.subTest(message=message), self.assertRaises(app.AppServerError):
                app.check_outgoing(message)
        with self.client() as client:
            with self.assertRaises(app.AppServerError):
                client.write({"id": 99, "method": "turn/start", "params": {}})
            with self.assertRaises(app.AppServerError):
                client.write({"id": "server-1", "result": {}})
        self.assertNotIn("turn/start", self.log_path.read_text())

    def test_parity_records_a_request_buffered_after_the_last_response(self):
        self.existing_parity_copy()
        self.data["request_after_last_list"] = True

        def compare(_, source):
            source.list_threads(lambda cwd: True)
            return {"threads_compared": 1, "missing": [], "mismatches": []}

        with patch.object(app, "compare", compare):
            report = self.parity_run("codex-cli 1.2.3")
        self.assertEqual(report["server_requests"], ["item/commandExecution/requestApproval"])
        self.assertFalse(report["passed"])
        self.assertIn({"id": "server-1", "error": app.REFUSAL}, self.sent())

    def test_receive_stops_at_the_deadline_even_when_stdout_is_ready(self):
        client = self.client()
        client.selector, client.process = Mock(), Mock()
        now = 0

        def read(*args):
            nonlocal now
            now += 2
            return b"{" if now == 2 else b"}\n"

        with patch.object(app.time, "monotonic", lambda: now), patch.object(app.os, "read", read), \
                self.assertRaisesRegex(app.AppServerError, "in time"):
            client.receive(1)

    def test_write_stops_at_the_deadline_even_when_stdin_accepts_partial_writes(self):
        client = self.client()
        client.process = Mock()
        now = 0

        def write(fd, data):
            nonlocal now
            now += 2
            return 1

        with patch.object(app, "REQUEST_TIMEOUT_SECONDS", 1), patch.object(app.time, "monotonic", lambda: now), \
                patch.object(app.os, "write", write), self.assertRaisesRegex(app.AppServerError, "in time"):
            client.write({"method": "initialized"})

    def test_pagination_rejects_a_cursor_seen_on_an_earlier_page(self):
        client = self.client()
        pages = [{"data": [], "nextCursor": cursor} for cursor in ("a", "b", "a", None)]
        with patch.object(client, "request", side_effect=pages), \
                self.assertRaisesRegex(app.AppServerError, "repeated cursor"):
            list(client.pages("thread/items/list", {}))

    def test_teardown_sends_sigkill_to_the_group_even_when_the_leader_exits(self):
        client = self.client()
        client.selector, client.stderr = Mock(), Mock()
        client.process = Mock(pid=42)
        calls = Mock()
        calls.attach_mock(client.process.wait, "wait")
        with patch.object(app.os, "killpg", calls.killpg):
            client.__exit__(app.AppServerError, None, None)
        self.assertEqual([call.args for call in calls.mock_calls],
                         [(42, app.signal.SIGTERM), (), (42, app.signal.SIGKILL), ()])

    def test_failed_handshakes_and_rpcs_break_the_release_streak(self):
        root = self.existing_parity_copy()
        identifier, _ = codex_thread(root / "codex-home", "/repo", codex_turn("t1", "Done."))
        self.add_thread(identifier, [("t1", "completed", "Done.")])
        self.data["no_server_requests"] = True
        methods = (None, "initialize", None, "thread/turns/list", None)
        for patch_number, method in enumerate(methods):
            version = f"codex-cli 1.2.{patch_number}"
            with self.subTest(version=version, method=method):
                self.data["fail_method"] = method
                report = self.parity_run(version)
                self.assertEqual(report["codex_version"], version)
                self.assertEqual(report["passed"], method is None)
                self.assertFalse(report["ready_to_replace_file_reader"])
                if method:
                    self.assertEqual(report["error"], f"{method} failed with code -32603.")
        self.assertEqual(len(list((root / "reports").glob("*.json"))), len(methods))

    def test_a_run_interrupted_at_startup_already_has_an_atomic_failure_report(self):
        root = self.existing_parity_copy()
        version = "codex-cli 1.2.3"
        first_report = {}
        replace = os.replace

        def publish(staging, destination):
            self.assertEqual(list((root / "reports").glob("*.json")), [])
            first_report.update(json.loads(staging.read_text()))
            replace(staging, destination)

        with patch.object(app.os, "replace", publish), \
                patch.object(app.AppServerClient, "__enter__", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.parity_run(version)
        (saved,) = (root / "reports").iterdir()
        self.assertEqual(json.loads(saved.read_text()), first_report)
        self.assertEqual(first_report["codex_version"], version)
        self.assertFalse(first_report["passed"])

    def test_sandbox_paths_and_symlinks_cannot_reach_the_live_home(self):
        root = self.root.resolve()
        live = root / "live-home"
        (live / "plugins").mkdir(parents=True)
        copy = root / "copy"
        (copy / "plugins").mkdir(parents=True)
        (copy / "plugins" / "latest").symlink_to(live / "plugins")
        (copy / "node_modules").symlink_to(root)
        (copy / "plugins" / "file").write_text("kept")
        self.assertEqual(app.remove_symlinks(copy), 2)
        self.assertEqual(sorted(path.name for path in copy.rglob("*")), ["file", "plugins"])
        app.check_isolated(root / "parity" / "home", live)
        (root / "alias").symlink_to(root / "parity", target_is_directory=True)
        for path, reason in ((live / "parity", "overlaps"), (root, "overlaps"), (live, "overlaps"),
                             (root / "alias" / "home", "symlink")):
            with self.subTest(path=path), self.assertRaisesRegex(app.AppServerError, reason):
                app.check_isolated(path, live)

    def test_parity_refuses_before_writing_anything_near_the_live_home(self):
        root = self.root.resolve()
        live = root / "live-home"
        (live / "sessions").mkdir(parents=True)
        (live / "auth.json").write_text("secret")
        with patch.dict(os.environ, {"CODEX_HOME": str(live)}):
            with self.assertRaisesRegex(app.AppServerError, "overlaps"):
                app.run_parity(live / "state")
        self.assertFalse((live / "state").exists())
        linked = root / "linked-home"
        linked.symlink_to(live, target_is_directory=True)
        # Pretends to be macOS so the check before any copy runs on every system; cp must never start.
        with patch.object(app.sys, "platform", "darwin"), \
                patch.object(app.subprocess, "run", side_effect=AssertionError("cp ran")), \
                self.assertRaisesRegex(app.AppServerError, "resolved directory"):
            app.clone_home(linked, root / "copy")
        self.assertTrue((live / "auth.json").exists())
        self.assertFalse((root / "copy").exists())

    def test_sandbox_files_are_written_without_following_symlinks(self):
        root = self.root.resolve()
        target = root / "elsewhere.log"
        target.write_text("untouched")
        (root / "app-server.log").symlink_to(target)
        with self.assertRaisesRegex(app.AppServerError, "may be a symlink"):
            app.open_private(root / "app-server.log", append=True)
        self.assertEqual(target.read_text(), "untouched")
        with app.open_private(root / "report.json") as output:
            output.write("{}")
        self.assertEqual((root / "report.json").stat().st_mode & 0o777, 0o600)

    @staticmethod
    def copy_input(arguments, **kwargs):
        source, target = map(Path, arguments[-2:])
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        return SimpleNamespace(returncode=0)

    def test_clone_passes_only_allowlisted_inputs_to_cp(self):
        live, copy = self.home.resolve(), self.root.resolve() / "copy"
        _, rollout = codex_thread(live, "/repo", name="One")
        _, compressed = codex_thread(live, "/repo", suffix=".jsonl.zst")
        archives = live / "archived_sessions"
        archives.mkdir()
        archived = compressed.rename(archives / compressed.name)
        inputs = [rollout, archived, live / "session_index.jsonl"]
        for name in ("state_5.sqlite", "state_5.sqlite-wal", "thread_history_1.sqlite", "thread_history_1.sqlite-wal"):
            path = live / name
            path.write_bytes(name.encode())
            inputs.append(path)
        for path in (live / "auth.json", live / "config.toml", live / ".credentials.json", live / ".env",
                     live / "state_other.sqlite", live / "logs_2.sqlite", live / "state_5.sqlite-shm",
                     rollout.parent / ".env"):
            path.write_text("private")
        (archives / rollout.name).symlink_to(live / "auth.json")
        with patch.object(app.sys, "platform", "darwin"), \
                patch.object(app.subprocess, "run", side_effect=self.copy_input) as cp:
            app.clone_home(live, copy)
        self.assertCountEqual([Path(call.args[0][-2]) for call in cp.call_args_list], inputs)
        self.assertEqual({str(path.relative_to(copy)) for path in copy.rglob("*") if path.is_file()},
                         {"copy.json", *(str(path.relative_to(live)) for path in inputs)})
        for call in cp.call_args_list:
            self.assertEqual(call.args[0][0], "/bin/cp")
            self.assertEqual(call.kwargs["env"], {"PATH": "/usr/bin:/bin"})

    def test_failed_copy_is_never_published(self):
        live, copy = self.home.resolve(), self.root.resolve() / "copy"
        codex_thread(live, "/repo")
        (live / "state_5.sqlite").touch()

        def failed_copy(arguments, **kwargs):
            self.copy_input(arguments, **kwargs)
            return SimpleNamespace(returncode=1)

        with patch.object(app.sys, "platform", "darwin"), patch.object(app.subprocess, "run", failed_copy), \
                self.assertRaises(app.AppServerError):
            app.clone_home(live, copy)
        self.assertFalse(copy.exists())
        self.assertFalse(copy.with_suffix(".partial").exists())

    def test_clone_identity_exists_when_the_copy_is_published(self):
        live, copy = self.home.resolve(), self.root.resolve() / "copy"
        codex_thread(live, "/repo")
        (live / "state_5.sqlite").touch()
        rename = os.rename
        published = []

        def publish(staging, destination):
            published.append(json.loads((staging / "copy.json").read_text()))
            rename(staging, destination)

        with patch.object(app.sys, "platform", "darwin"), patch.object(app.subprocess, "run", self.copy_input), \
                patch.object(app.os, "rename", publish):
            app.clone_home(live, copy)
        self.assertEqual(published, [json.loads((copy / "copy.json").read_text())])
        self.assertTrue(published[0]["copy_id"])

    def test_run_rebuilds_an_orphaned_copy_and_holds_the_lock_through_reporting(self):
        live, state = self.home.resolve(), self.root.resolve() / "state"
        _, rollout = codex_thread(live, "/repo")
        (live / "state_5.sqlite").touch()
        root = state / "parity"
        orphan = root / "codex-home"
        orphan.mkdir(parents=True)
        (orphan / ".credentials.json").touch()
        verdict = app.parity_verdict

        def assert_locked():
            with (root / "parity.lock").open("ab") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def copy_input(arguments, **kwargs):
            assert_locked()
            return self.copy_input(arguments, **kwargs)

        def read_reports(reports):
            assert_locked()
            self.assertEqual(len(reports), 1)
            return verdict(reports)

        client = self.client()
        with patch.dict(os.environ, CODEX_HOME=str(live)), patch.object(app.sys, "platform", "darwin"), \
                patch.object(app.subprocess, "run", copy_input), patch.object(app, "AppServerClient", return_value=client), \
                patch.object(app, "desktop_codex_executable", return_value=(str(self.server), "fake")), \
                patch.object(app, "codex_version", return_value="codex-cli 1.2.3"), \
                patch.object(app, "parity_verdict", read_reports):
            report = app.run_parity(state)
        self.assertEqual(report["copy_id"], json.loads((orphan / "copy.json").read_text())["copy_id"])
        self.assertTrue((orphan / rollout.relative_to(live)).is_file())
        self.assertFalse((orphan / ".credentials.json").exists())

    def test_parity_lists_missing_readable_threads(self):
        same, _ = codex_thread(self.home, "/repo", codex_turn("t1", "Draft.", "First."), codex_turn("t2", "Final."))
        missing, _ = codex_thread(self.home, "/repo", codex_turn("t4", "Final."))
        codex_thread(self.home, "/repo", codex_turn("t5", "Final."), source="exec")
        self.add_thread(same, [("t1", "completed", ("Draft.", "First.")), ("t2", "completed", "Final.")], archived=True)
        self.add_thread("app-only", [("t9", "completed", "Final.")])
        with self.client() as client:
            result = app.compare(CodexFilesSource(self.home), app.AppServerSource(client))
        self.assertEqual(result["threads_compared"], 2)
        self.assertEqual(result["missing"], [missing])
        self.assertEqual(result["mismatches"], [])
        self.assertEqual(result["file_unreadable"], {})
        self.assertEqual(result["file_unsupported"], [])
        # A reader fault is not a refusal: it stops the run instead of leaving the thread out of the comparison.
        with self.client() as client, patch.object(CodexFilesSource, "read_thread", side_effect=AttributeError):
            with self.assertRaisesRegex(app.AppServerError, "file reader failed"):
                app.compare(CodexFilesSource(self.home), app.AppServerSource(client))

    def test_shadow_reader_pages_past_turns_with_no_agent_messages(self):
        self.add_thread("id", [
            ("t0", "completed", "Earlier."),
            ("t1", "completed", ("First.", "Second.", "Third.")),
            ("t2", "interrupted", ()),
            ("t3", "completed", ()),
            ("t4", "interrupted", ()),
        ])
        with self.client() as client:
            source = app.AppServerSource(client)
            summary = app.app_server_summary(self.data["threads"][0], False)
            snapshot = source.read_thread(summary, 3)
        self.assertEqual(snapshot.latest_turn.turn_id, "t4")
        self.assertEqual([message.text for message in snapshot.messages], ["First.", "Second.", "Third."])
        self.assertEqual([message["params"]["turnId"] for message in self.sent()
                          if message.get("method") == "thread/items/list"], ["t4", "t3", "t2", "t1"])

    def test_only_the_signed_desktop_codex_is_run(self):
        # Any local user can start a process whose path names ChatGPT.app; only the sealed bundle's own executable runs.
        bundled = self.root.resolve() / "ChatGPT.app" / "codex"
        bundled.parent.mkdir()
        bundled.write_text("")
        impostor = self.root / "elsewhere" / "ChatGPT.app" / "codex"

        team = app.DESKTOP_TEAM

        def system_tool(arguments, *, env=None, **kwargs):
            self.assertEqual(env, {"PATH": "/usr/bin:/bin"})
            if arguments[0] == "/usr/bin/codesign":
                return SimpleNamespace(returncode=0, stderr=f"TeamIdentifier={team}\n")
            self.assertEqual(arguments[0], "/bin/ps")
            output = f"41 {impostor}\n42 {bundled}\n" if arguments[1] == "-axo" else f"{bundled} other"
            return SimpleNamespace(stdout=output)

        with patch.object(app, "BUNDLED_DESKTOP_CODEX", bundled), patch.object(app.subprocess, "run", system_tool):
            self.assertEqual(app.desktop_codex_executable(),
                             (str(bundled), "ChatGPT.app's bundled Codex (no desktop app-server running)"))
            team = f"{app.DESKTOP_TEAM}0"
            with self.assertRaisesRegex(app.AppServerError, "signature"):
                app.desktop_codex_executable()

    def test_fingerprint_hashes_content_and_counts_only_an_empty_wal_as_absent(self):
        self.home.mkdir()
        (self.home / "sessions").mkdir()
        rollout = self.home / "sessions" / "rollout-x.jsonl"
        rollout.write_text("{\"a\": 1}\n")
        (self.home / "state_5.sqlite").write_bytes(b"state")
        before = app.fingerprint(self.home)
        (self.home / "state_5.sqlite-wal").write_bytes(b"")
        (self.home / "state_5.sqlite-shm").write_bytes(b"index")
        self.assertEqual(app.changed_paths(before, app.fingerprint(self.home)), [])
        stat = rollout.stat()
        rollout.write_text("{\"a\": 2}\n")
        os.utime(rollout, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        (self.home / "memories_1.sqlite").write_bytes(b"")
        (self.home / "logs_2.sqlite-wal").write_bytes(b"rows")
        changes = app.changed_paths(before, app.fingerprint(self.home))
        self.assertEqual(changes, ["logs_2.sqlite-wal", "memories_1.sqlite", "sessions/rollout-x.jsonl"])
        self.assertEqual([path for path in changes if app.is_thread_data(path)], ["sessions/rollout-x.jsonl"])
        for path, expected in (("state_5.sqlite", True), ("thread_history_1.sqlite-wal", True),
                               ("session_index.jsonl", True), ("logs_2.sqlite", False), ("memories_1.sqlite", False)):
            with self.subTest(path=path):
                self.assertEqual(app.is_thread_data(path), expected)


if __name__ == "__main__":
    unittest.main()
