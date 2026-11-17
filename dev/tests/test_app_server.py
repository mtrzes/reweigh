"""Exercise the shadow app-server source and the parity check against a stand-in `codex app-server`."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# Lets a single test file run on its own, as well as through discover.
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "dev"), str(ROOT / "plugins" / "peer-bridge" / "tests")]
from support import codex_thread, codex_turn  # noqa: E402  (support adds the plugin's scripts to the path)
import codex_app_server as app  # noqa: E402
from thread_reader import CodexFilesSource  # noqa: E402

# Answers from FAKE_SERVER_DATA; before its first thread/list answer it sends one server request.
FAKE_SERVER = textwrap.dedent('''\
    import json, os, sys
    data = json.load(open(os.environ["FAKE_SERVER_DATA"]))
    log = open(os.environ["FAKE_SERVER_LOG"], "a")
    asked = False
    def send(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        message = json.loads(line)
        log.write(json.dumps(message) + "\\n")
        log.flush()
        method, params = message.get("method"), message.get("params") or {}
        if "id" not in message or method is None:
            continue
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "thread/list":
            if not asked:
                asked = True
                send({"id": "server-1", "method": "item/commandExecution/requestApproval", "params": {}})
                send({"method": "thread/started", "params": {}})
            result = {"data": [t for t in data["threads"] if t.get("archived", False) == params["archived"]]}
        elif method == "thread/turns/list":
            result = {"data": data["turns"].get(params["threadId"], [])[:params["limit"]]}
        elif method == "thread/items/list":
            result = {"data": data["items"].get(params["turnId"], [])}
        else:
            send({"id": message["id"], "error": {"code": -32601, "message": "unknown"}})
            continue
        send({"id": message["id"], "result": result})
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

    def add_thread(self, identifier, turns, cwd="/repo", **fields):
        self.data["threads"].append({"id": identifier, "source": "vscode", "cwd": cwd, "updatedAt": 1, **fields})
        self.data["turns"][identifier] = [{"id": turn, "status": status} for turn, status, _ in reversed(turns)]
        for turn, _, text in turns:
            self.data["items"][turn] = [
                {"turnId": turn, "item": {"type": "userMessage", "content": []}},
                {"turnId": turn, "item": {"type": "agentMessage", "text": text, "phase": "final_answer"}},
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

    def test_parity_compares_latest_turn_status_and_final_message(self):
        same, _ = codex_thread(self.home, "/repo", codex_turn("t1", "Draft.", "First."), codex_turn("t2", "Final."))
        changed, _ = codex_thread(self.home, "/repo", codex_turn("t3", "Final."))
        missing, _ = codex_thread(self.home, "/repo", codex_turn("t4", "Final."))
        codex_thread(self.home, "/repo", codex_turn("t5", "Final."), source="exec")
        self.add_thread(same, [("t1", "completed", "First."), ("t2", "completed", "Final.")])
        self.add_thread(changed, [("t3", "interrupted", "Something else.")], archived=True)
        self.add_thread("app-only", [("t9", "completed", "Final.")])
        with self.client() as client:
            result = app.compare(CodexFilesSource(self.home), app.AppServerSource(client))
        self.assertEqual(result["threads_compared"], 3)
        self.assertEqual(result["missing"], [missing])
        self.assertEqual(result["mismatches"], [{"id": changed, "fields": ["status", "final_agent_message"]}])
        self.assertEqual(result["file_unreadable"], {})

    def test_only_the_signed_desktop_codex_is_run(self):
        # Any local user can start a process whose path names ChatGPT.app; only the sealed bundle's own executable runs.
        bundled = self.root.resolve() / "ChatGPT.app" / "codex"
        bundled.parent.mkdir()
        bundled.write_text("")
        impostor = self.root / "elsewhere" / "ChatGPT.app" / "codex"

        def ps(arguments, **_):
            output = f"41 {impostor}\n42 {bundled}\n" if "-axo" in arguments else (
                f"{impostor} app-server" if "41" in arguments else f"{bundled} other")
            return SimpleNamespace(stdout=output)

        with patch.object(app, "BUNDLED_DESKTOP_CODEX", bundled), patch.object(app.subprocess, "run", ps):
            with patch.object(app, "signed_desktop_app", lambda: True):
                self.assertEqual(app.desktop_codex_executable(),
                                 (str(bundled), "ChatGPT.app's bundled Codex (no desktop app-server running)"))
            with patch.object(app, "signed_desktop_app", lambda: False), \
                    self.assertRaisesRegex(app.AppServerError, "signature"):
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
