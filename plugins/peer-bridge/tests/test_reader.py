"""Exercise the thread sources over synthetic session files: same-repository listing, agent-only reads and refusals.

Most tests give ThreadReader a stand-in for git's common-directory lookup; one runs real git through a worktree.
"""

import asyncio
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (
    PRIVATE_MARKERS, bridge, claude_prompt, claude_reply, claude_session, claude_tool_result, codex_event,
    codex_thread, codex_turn, git, git_repository, isolate,
)
import thread_reader  # found through the script directory support.py adds
from thread_reader import ClaudeSessionsSource, CodexFilesSource, ThreadReader, ThreadUnavailable


class ReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-threads-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        isolate(self, self.root)
        self.codex_home, self.claude_home = self.root / "codex-home", self.root / "claude-home"
        self.project, self.worktree, self.other = (self.root / name for name in ("project", "worktree", "other"))
        repositories = {self.project: "repository", self.worktree: "repository", self.other: "other"}
        for directory in repositories:
            directory.mkdir()
        self.common_dir = lambda directory: repositories.get(Path(directory).resolve())

    def reader(self, provider):
        return ThreadReader(CodexFilesSource() if provider == "codex" else ClaudeSessionsSource(), self.common_dir)

    def threads(self, provider, directory=None):
        return self.reader(provider).list(directory or self.project, 20)

    def read(self, provider, thread, max_messages=3):
        return self.reader(provider).read(self.project, thread, max_messages)

    def assert_private_text_absent(self, result):
        serialised = json.dumps(result)
        for marker in PRIVATE_MARKERS:
            self.assertNotIn(marker, serialised)

    def test_codex_threads_are_listed_only_for_the_same_git_repository(self):
        project = git_repository(self.root / "repository")
        linked = self.root / "repository-worktree"
        git(project, "worktree", "add", "--quiet", "--detach", str(linked))
        elsewhere = git_repository(self.root / "elsewhere")
        home = self.codex_home
        older, _ = codex_thread(home, project, codex_turn("t1", "Done."), name="Older review", mtime=1_000)
        newer, _ = codex_thread(home, linked / ".", codex_turn("t1", "Done."), name="Worktree review", mtime=2_000)
        codex_thread(home, elsewhere, codex_turn("t1", "Done."), name="Other repository")
        codex_thread(home, self.root / "deleted-worktree", codex_turn("t1", "Done."), name="Deleted worktree")
        codex_thread(home, project, codex_turn("t1", "Done."), source={"subagent": {"other": "guardian"}})
        codex_thread(home, project, codex_turn("t1", "Done."), source="exec")
        codex_thread(home, project, codex_turn("t1", "Done."), suffix=".jsonl.gz")
        reader = ThreadReader(CodexFilesSource())
        listing = reader.list(linked, 20)
        self.assertEqual([thread["id"] for thread in listing["threads"]], [newer, older])
        self.assertEqual([thread["name"] for thread in listing["threads"]], ["Worktree review", "Older review"])
        self.assertEqual(listing["unsupported_threads_machine_wide"], 1)
        with self.assertRaisesRegex(ThreadUnavailable, "inside a git repository"):
            reader.list(self.root, 20)

    def test_codex_thread_read_returns_agent_messages_and_the_latest_turn_state(self):
        home = self.codex_home
        codex_thread(home, self.project, codex_turn("t1", "First answer."), [codex_event("task_started", "orphan")],
                     codex_turn("t2", "Looking at the diff.", "Verdict: ship it."), name="Review",
                     tail='{"type": "event_msg", "payload": {"type": "task_sta')
        result = self.read("codex", "Review")
        self.assertEqual(result["messages"], [
            {"text": "First answer.", "phase": "final_answer"},
            {"text": "Looking at the diff.", "phase": "commentary"},
            {"text": "Verdict: ship it.", "phase": "final_answer"},
        ])
        self.assertEqual(result["latest_turn"], {"status": "completed", "finished": True, "liveness": None})
        self.assert_private_text_absent(result)
        # An unfinished turn's liveness comes from the writer-lock file.
        running, _ = codex_thread(home, self.project, codex_turn("t1", "Working.", outcome=None))
        (home / "thread-writer-locks").mkdir()
        (home / "thread-writer-locks" / f"{running}.lock").touch()
        abandoned, _ = codex_thread(home, self.project, codex_turn("t1", outcome=None))
        for thread, liveness in ((running, "unknown"), (abandoned, "abandoned")):
            with self.subTest(liveness=liveness):
                self.assertEqual(self.read("codex", thread, max_messages=1)["latest_turn"],
                                 {"status": "inProgress", "finished": False, "liveness": liveness})

    def test_codex_reader_refuses_history_it_cannot_parse(self):
        home = self.codex_home
        _, broken = codex_thread(home, self.project, codex_turn("t1", "Done."), name="Malformed")
        lines = broken.read_text(encoding="utf-8").splitlines(keepends=True)
        broken.write_text("".join(lines[:2] + ["{not json\n"] + lines[2:]), encoding="utf-8")
        mismatched = codex_turn("t1", "Shown answer.")
        mismatched[-1]["payload"]["last_agent_message"] = "Recorded answer."
        codex_thread(home, self.project, mismatched, name="Mismatched")
        codex_thread(home, self.project, codex_turn("t1", "Done."), name="Forked", meta={"forked_from_id": "x"})
        cases = (("Malformed", "malformed line"), ("Mismatched", "does not match its turn record"),
                 ("Forked", "Forked Codex threads"), ("Missing", "No readable thread with that ID"))
        for thread, expected in cases:
            with self.subTest(thread=thread), self.assertRaisesRegex(ThreadUnavailable, expected):
                self.read("codex", thread)

    def test_unsupported_histories_are_counted_machine_wide_and_read_like_missing_threads(self):
        home = self.codex_home
        codex_thread(home, self.project, codex_turn("t1", "Done."), name="Readable")
        foreign, _ = codex_thread(home, self.other, codex_turn("t1", "Done."))
        # The reader cannot tell which repository a history in an unknown format belongs to.
        compressed = []
        for cwd in (self.project, self.other):
            identifier, path = codex_thread(home, cwd, codex_turn("t1", "Done."), suffix=".jsonl.gz")
            path.write_bytes(b"\x1f\x8b\x08\x00")
            compressed.append(identifier)
        for directory in (self.project, self.other):
            with self.subTest(directory=directory):
                self.assertEqual(self.threads("codex", directory)["unsupported_threads_machine_wide"], 2)
        errors = set()
        for thread in (*compressed, foreign, str(uuid.uuid4())):
            with self.subTest(thread=thread), self.assertRaises(ThreadUnavailable) as raised:
                self.read("codex", thread)
            errors.add(str(raised.exception))
        self.assertEqual(errors, {"No readable thread with that ID or name belongs to this repository."})

    def test_history_with_unexpected_value_types_is_refused_with_a_reason(self):
        codex_thread(self.codex_home, self.project, codex_turn(["t1"], "Done."), name="Codex types")
        claude_session(self.claude_home, self.project, claude_prompt(5), claude_reply("Done."), title="Claude types")
        claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."), entrypoint=5)
        # The bridge turns a value of the wrong type, which the parser meets as a TypeError, into a named refusal.
        client = bridge.Bridge(readers={provider: self.reader(provider) for provider in ("claude", "codex")})
        for provider, name in (("codex", "Codex types"), ("claude", "Claude types")):
            with self.subTest(provider=provider), self.assertRaisesRegex(bridge.BridgeError, "malformed line"):
                asyncio.run(client.thread(provider, str(self.project), name))
        self.assertEqual([thread["name"] for thread in self.threads("claude")["threads"]], ["Claude types"])

    def test_compressed_rollouts_are_listed_and_read_like_plain_ones(self):
        turns = (codex_turn("t1", "First answer."), codex_turn("t2", "Second answer."))
        codex_thread(self.codex_home, self.project, *turns, name="Plain", mtime=1_000)
        compressed, _ = codex_thread(self.codex_home, self.project, *turns, name="Compressed", mtime=2_000,
                                     suffix=".jsonl.zst", frames=3)
        listing = self.threads("codex")
        self.assertEqual([thread["name"] for thread in listing["threads"]], ["Compressed", "Plain"])
        self.assertNotIn("unsupported_threads_machine_wide", listing)
        plain, packed = self.read("codex", "Plain"), self.read("codex", compressed)
        for result in (plain, packed):
            for key in ("id", "name", "updated_at"):
                result.pop(key)
        self.assertEqual(packed, plain)
        self.assertEqual([message["text"] for message in packed["messages"]], ["First answer.", "Second answer."])

    def test_a_compressed_rollout_keeps_at_most_its_limit_of_content(self):
        noise = [codex_event("token_count", "t1", info="x" * 200) for _ in range(200)]
        _, path = codex_thread(self.codex_home, self.project, codex_turn("t0", "Old answer."), noise,
                               codex_turn("t1", "Answer."), suffix=".jsonl.zst")
        whole, from_start = thread_reader.compressed_tail(path)
        self.assertTrue(from_start)
        tail, from_start = thread_reader.compressed_tail(path, limit=4096)
        self.assertEqual((len(tail), from_start), (4096, False))
        self.assertTrue(whole.endswith(tail))

    def test_cut_off_or_damaged_compressed_rollouts_are_refused_with_a_reason(self):
        # Several zstd blocks of hard-to-compress text, so a file cut in half still decodes its first lines.
        turns = [codex_turn(f"t{number}", os.urandom(300).hex()) for number in range(800)]
        damages = (("Cut off", lambda data: data[:len(data) // 2]), ("Trailing bytes", lambda data: data + b"\x01\x02"),
                   ("Header only", lambda data: data[:12]))
        for name, damage in damages:
            _, path = codex_thread(self.codex_home, self.project, *turns, name=name, suffix=".jsonl.zst")
            path.write_bytes(damage(path.read_bytes()))
        listing = self.threads("codex")
        self.assertEqual(sorted(thread["name"] for thread in listing["threads"]), ["Cut off", "Trailing bytes"])
        for name, reason in (("Cut off", "compressed history file is incomplete"),
                             ("Trailing bytes", "compressed history file is damaged"),
                             ("Header only", "No readable thread")):
            with self.subTest(name=name), self.assertRaisesRegex(ThreadUnavailable, reason):
                self.read("codex", name)

    def test_compressed_rollout_must_end_with_a_whole_record_and_stay_unchanged(self):
        later_turn = json.dumps(codex_event("task_started", "t2"))[:-5]
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Mid record",
                     suffix=".jsonl.zst", tail="\n" + later_turn)
        with self.assertRaisesRegex(ThreadUnavailable, "ends inside a record"):
            self.read("codex", "Mid record")
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Moving", suffix=".jsonl.zst")
        identities = iter([(1, 1, 1), (1, 1, 2), (1, 1, 2)])
        with (
            patch.object(thread_reader, "file_identity", lambda _: next(identities)),
            self.assertRaisesRegex(ThreadUnavailable, "changed while it was read"),
        ):
            self.read("codex", "Moving")

    def test_only_a_missing_zstandard_package_is_reported_as_missing(self):
        with patch.dict(sys.modules, {"zstandard": None}):
            with self.assertRaisesRegex(ThreadUnavailable, "needs the zstandard package"):
                thread_reader.zstandard_module()
        real_import = __import__

        def broken(name, *arguments, **keywords):
            if name == "zstandard":
                raise ImportError("dlopen failed: incompatible architecture")
            return real_import(name, *arguments, **keywords)

        with patch.dict(sys.modules), patch("builtins.__import__", broken):
            sys.modules.pop("zstandard", None)
            with self.assertRaisesRegex(ImportError, "incompatible architecture"):
                thread_reader.zstandard_module()

    def test_codex_tail_window_grows_until_it_reaches_a_turn(self):
        noise = [codex_event("token_count", "t1", info="x" * 200) for _ in range(200)]
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Early answer."), noise, name="Long")
        with patch.object(thread_reader, "INITIAL_TAIL_BYTES", 1024):
            result = self.read("codex", "Long")
        self.assertEqual(result["messages"], [{"text": "Early answer.", "phase": "final_answer"}])
        self.assertTrue(result["latest_turn"]["finished"])

    def test_claude_sessions_are_listed_and_read_by_the_same_rules(self):
        home = self.claude_home
        finished, _ = claude_session(home, self.project, claude_prompt(), claude_reply("Checking.", "tool_use"),
                                     claude_tool_result(), claude_reply("All good."), title="Payments review",
                                     mtime=2_000)
        working, _ = claude_session(home, self.worktree, claude_prompt(), claude_reply("Running tests.", "tool_use"),
                                    project="worktree", mtime=1_000)
        stopped, _ = claude_session(home, self.project, claude_prompt(), claude_reply("Starting.", "tool_use"),
                                    claude_prompt("[Request interrupted by user]"))
        claude_session(home, self.project, claude_prompt(), claude_reply("Consulted."), entrypoint="sdk-cli")
        claude_session(home, self.other, claude_prompt(), claude_reply("Elsewhere."), project="other")
        subagents = home / "projects" / "project" / finished / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-helper.jsonl").write_text(json.dumps(claude_reply("SUBAGENT TRAFFIC")) + "\n")
        listing = self.threads("claude")
        self.assertEqual([thread["id"] for thread in listing["threads"]], [stopped, finished, working])
        self.assertEqual([thread["name"] for thread in listing["threads"]], [None, "Payments review", None])
        result = self.read("claude", "Payments review")
        self.assertEqual([message["text"] for message in result["messages"]], ["Checking.", "All good."])
        self.assertEqual(result["latest_turn"]["status"], "completed")
        self.assert_private_text_absent(result)
        self.assertEqual(self.read("claude", working)["latest_turn"],
                         {"status": "inProgress", "finished": False, "liveness": "unknown"})
        self.assertEqual(self.read("claude", stopped)["latest_turn"]["status"], "interrupted")

    def test_a_claude_session_belongs_where_its_latest_records_are(self):
        home = self.claude_home
        moved_in, path = claude_session(home, self.other, claude_prompt(), claude_reply("Started elsewhere."),
                                        project="other")
        with path.open("a", encoding="utf-8") as history:
            history.write(json.dumps({**claude_prompt(), "cwd": str(self.project)}) + "\n")
        moved_out, path = claude_session(home, self.project, claude_prompt(), claude_reply("Started here."))
        with path.open("a", encoding="utf-8") as history:
            history.write(json.dumps({**claude_prompt(), "cwd": str(self.other)}) + "\n")
        claude_session(home, "", claude_prompt(), claude_reply("No directory."))
        claude_session(home, self.project, claude_prompt(), claude_reply("No entrypoint."), entrypoint=None)
        # A FIFO in a session's place is counted, without blocking the listing.
        os.mkfifo(home / "projects" / "project" / f"{uuid.uuid4()}.jsonl")
        listing = self.threads("claude")
        self.assertEqual([thread["id"] for thread in listing["threads"]], [moved_in])
        self.assertEqual(listing["unsupported_threads_machine_wide"], 1)
        self.assertNotIn(moved_out, json.dumps(listing))

    def fixture(self, name):
        """A real session file with every text replaced (dev/scrub_fixture.py), placed in this test's project."""
        text = (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")
        return text.replace("{CWD}", json.dumps(str(self.project))[1:-1])

    def test_real_histories_of_both_apps_are_read(self):
        # Synthetic records prove each rule; these prove the parsers still fit what the apps actually write.
        identifier = str(uuid.uuid4())
        rollout = self.codex_home / "sessions" / "2026" / "04" / "16" / f"rollout-2026-04-16T12-22-13-{identifier}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(self.fixture("codex-desktop-rollout.jsonl"), encoding="utf-8")
        events = [json.loads(line)["payload"] for line in rollout.read_text(encoding="utf-8").splitlines()]
        final = [event["last_agent_message"] for event in events if event.get("type") == "task_complete"][-1]
        result = self.read("codex", identifier)
        self.assertEqual((result["latest_turn"]["status"], result["messages"][-1]["text"]), ("completed", final))
        session = self.claude_home / "projects" / "project" / f"{uuid.uuid4()}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(self.fixture("claude-desktop-session.jsonl"), encoding="utf-8")
        result = self.read("claude", session.stem)
        self.assertEqual(result["latest_turn"]["status"], "completed")
        self.assertTrue(result["messages"])
        self.assertTrue(all(message["text"].startswith("TEXT-") for message in result["messages"]))

    def test_reading_starts_no_process_and_leaves_both_history_stores_untouched(self):
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Codex review")
        claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."), title="Claude review")

        def snapshot():
            return {path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode))
                    for home in (self.codex_home, self.claude_home) for path in home.rglob("*") if path.is_file()}

        before = snapshot()
        frozen = [path for home in (self.codex_home, self.claude_home) for path in (home, *home.rglob("*"))]
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in frozen}
        self.addCleanup(lambda: [path.chmod(mode) for path, mode in modes.items()])
        for path in frozen:
            path.chmod(modes[path] & ~0o222)
        refuse = AssertionError("the thread sources start no process and open no database")
        with patch.object(thread_reader.subprocess, "run", side_effect=refuse), \
                patch.object(sqlite3, "connect", side_effect=refuse):
            for provider, name in (("codex", "Codex review"), ("claude", "Claude review")):
                self.threads(provider)
                self.read(provider, name)
        for path, mode in modes.items():
            path.chmod(mode)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
