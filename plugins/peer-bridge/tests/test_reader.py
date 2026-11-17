"""Exercise the thread sources over synthetic session files: same-repository listing, agent-only reads and refusals.

Most tests give ThreadReader a stand-in for git's common-directory lookup; one runs real git through a worktree.
"""

import asyncio
import json
import os
import sqlite3
import stat
import struct
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (
    PRIVATE_MARKERS, bridge, claude_prompt, claude_reply, claude_session, claude_tool_result, codex_event,
    codex_thread, codex_turn, git, git_repository, isolate,
)
import thread_reader  # found through the script directory support.py adds
from thread_reader import ClaudeSessionsSource, CodexFilesSource, ThreadReader, ThreadSummary, ThreadUnavailable


class ReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-threads-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        isolate(self, self.root, PEER_BRIDGE_THREADS="1")
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

    def test_history_entries_must_be_regular_files_with_the_recorded_id(self):
        for provider, make_history in (
            ("codex", lambda: codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."))),
            ("claude", lambda: claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."))),
        ):
            identifier, path = make_history()
            alternate_id = str(uuid.uuid4())
            copy = path.with_name(path.name.replace(identifier, alternate_id))
            copy.write_bytes(path.read_bytes())
            with self.subTest(provider=provider, entry="different recorded ID"):
                self.assertEqual([row["id"] for row in self.threads(provider)["threads"]], [identifier])
                with self.assertRaisesRegex(ThreadUnavailable, "^No readable thread"):
                    self.read(provider, alternate_id)
            copy.unlink()
            outside = self.root / path.name
            path.rename(outside)
            path.symlink_to(outside)
            with self.subTest(provider=provider, entry="symlink with matching ID"):
                self.assertEqual(self.threads(provider)["threads"], [])
                with self.assertRaisesRegex(ThreadUnavailable, "^No readable thread"):
                    self.read(provider, identifier)

    def test_archiving_a_rollout_after_its_header_is_read_keeps_the_listing(self):
        moving, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), mtime=1_000)
        other, _ = codex_thread(self.codex_home, self.project, codex_turn("t2", "Also done."), mtime=2_000)
        archive = self.codex_home / "archived_sessions"
        archive.mkdir()
        parse = thread_reader.parse_line

        def archive_after_header(line):
            record = parse(line)
            if record.get("payload", {}).get("id") == moving:
                path.rename(archive / path.name)
            return record

        with patch.object(thread_reader, "parse_line", archive_after_header):
            rows = self.threads("codex")["threads"]
        self.assertEqual([row["id"] for row in rows], [other, moving])
        self.assertEqual(rows[1]["updated_at"], "1970-01-01T00:16:40+00:00")

    def test_codex_index_only_reads_recent_complete_lines_within_its_budget(self):
        older, _ = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Older")
        newer, _ = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Newer")
        index = self.codex_home / "session_index.jsonl"
        first, second = index.read_bytes().splitlines(keepends=True)
        index.write_bytes(first + b"x" * 1024 + b"\n" + second)
        with patch.object(thread_reader, "MAX_METADATA_BYTES", len(second) + 10, create=True):
            names = {row["id"]: row["name"] for row in self.threads("codex")["threads"]}
        self.assertEqual(names, {older: None, newer: "Newer"})

    def test_history_with_unexpected_value_types_is_refused_with_a_reason(self):
        claude_session(self.claude_home, self.project, claude_prompt(5), claude_reply("Done."), title="Claude types")
        claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."), entrypoint=5)
        # The bridge turns a value of the wrong type, which the parser meets as a TypeError, into a named refusal.
        client = bridge.Bridge(readers={"claude": self.reader("claude")})
        with self.assertRaisesRegex(bridge.BridgeError, "malformed line"):
            asyncio.run(client.thread("claude", str(self.project), "Claude types"))
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
        def decompressed(limit=None):
            with thread_reader.open_history(path) as source:
                return thread_reader.decompress_history(source, limit)

        head, whole, from_start = decompressed()
        self.assertTrue(from_start)
        self.assertEqual(json.loads(head.split(b"\n")[0])["type"], "session_meta")
        head_again, tail, from_start = decompressed(limit=4096)
        self.assertEqual((head_again, len(tail), from_start), (head, 4096, False))
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

    def test_compressed_rollout_must_end_with_a_whole_record(self):
        later_turn = json.dumps(codex_event("task_started", "t2"))[:-5]
        codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Mid record",
                     suffix=".jsonl.zst", tail="\n" + later_turn)
        with self.assertRaisesRegex(ThreadUnavailable, "ends inside a record"):
            self.read("codex", "Mid record")

    def test_compressed_input_budgets_count_zstd_skippable_frames(self):
        identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."),
                                        suffix=".jsonl.zst")
        content = path.read_bytes()
        skippable = struct.pack("<II", 0x184D2A50, 4096) + bytes(4096)
        path.write_bytes(skippable + content)
        with patch.object(thread_reader, "MAX_METADATA_BYTES", 1024, create=True):
            self.assertEqual(self.threads("codex")["threads"], [])
        path.write_bytes(content + skippable)
        self.assertEqual([row["id"] for row in self.threads("codex")["threads"]], [identifier])
        with patch.object(thread_reader, "MAX_COMPRESSED_BYTES", 1024, create=True):
            with self.assertRaisesRegex(ThreadUnavailable, "compressed history is too large"):
                self.read("codex", identifier)

    def test_compressed_read_retries_a_parse_error_when_the_file_changed(self):
        identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."),
                                        suffix=".jsonl.zst")
        content = path.read_bytes()
        decompress = thread_reader.decompress_history
        attempts = []

        def change_during_decompression(source, limit=None):
            attempts.append(1)
            if len(attempts) == 1:
                path.write_bytes(b"\x00" * len(content))
                try:
                    return decompress(source, limit)
                finally:
                    path.write_bytes(content)
            return decompress(source, limit)

        with patch.object(thread_reader, "decompress_history", change_during_decompression):
            self.assertEqual(self.read("codex", identifier)["messages"][0]["text"], "Done.")
        self.assertEqual(len(attempts), 2)

    def test_each_compressed_read_attempt_uses_its_open_extent(self):
        import zstandard

        identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "First."),
                                        suffix=".jsonl.zst")
        records = "".join(json.dumps(record) + "\n" for record in codex_turn("t2", "Second.")).encode()
        frame = zstandard.ZstdCompressor().compress(records)
        decompress = thread_reader.decompress_history
        decoded = []

        def append_during_decompression(source, limit=None):
            if not decoded:
                with path.open("ab") as output:
                    output.write(frame)
            result = decompress(source, limit)
            decoded.append(result[1])
            return result

        with patch.object(thread_reader, "decompress_history", append_during_decompression):
            self.assertEqual(self.read("codex", identifier)["messages"][-1]["text"], "Second.")
        self.assertEqual(len(decoded), 2)
        self.assertFalse(decoded[0].endswith(records))
        self.assertEqual(decoded[1], decoded[0] + records)

    def test_a_history_that_changes_while_it_is_read_is_read_once_more_then_refused(self):
        _, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Moving")
        real_scan = thread_reader.scan_codex

        def scan_while_appending(times):
            def scan(records):
                if times:
                    times.pop()
                    with path.open("a", encoding="utf-8") as history:
                        history.write(json.dumps(codex_event("token_count", "t1")) + "\n")
                return real_scan(records)
            return scan

        with patch.object(thread_reader, "scan_codex", scan_while_appending([1])):
            self.assertEqual([message["text"] for message in self.read("codex", "Moving")["messages"]], ["Done."])
        with (
            patch.object(thread_reader, "scan_codex", scan_while_appending([1, 1])),
            self.assertRaisesRegex(ThreadUnavailable, "^The thread's history file changed while it was read.$"),
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

    def test_claude_read_expands_the_tail_to_resolve_a_rewind(self):
        records = [
            claude_prompt() | {"uuid": "u1", "parentUuid": None},
            claude_reply("Kept.") | {"uuid": "a1", "parentUuid": "u1"},
            {"type": "file-history-snapshot", "snapshot": "x" * 4096},
            claude_prompt() | {"uuid": "u2", "parentUuid": "a1"},
            claude_reply("Abandoned.") | {"uuid": "a2", "parentUuid": "u2"},
            claude_prompt() | {"uuid": "u3", "parentUuid": "a1"},
            claude_reply("After rewind.") | {"uuid": "a3", "parentUuid": "u3"},
        ]
        identifier, _ = claude_session(self.claude_home, self.project, *records)
        with patch.object(thread_reader, "INITIAL_TAIL_BYTES", 2048):
            self.assertEqual([m["text"] for m in self.read("claude", identifier, max_messages=1)["messages"]],
                             ["After rewind."])
            with patch.object(thread_reader, "MAX_TAIL_BYTES", 2048):
                with self.assertRaisesRegex(ThreadUnavailable, "active ancestry could not be resolved"):
                    self.read("claude", identifier, max_messages=1)

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
        # The read decides again from the bytes it returns: each row runs in homes of its own.
        elsewhere = {"cwd": str(self.other)}
        large_record = json.dumps({"type": "file-history-snapshot", "snapshot": "x" * 4096}) + "\n"

        def append(path, *records):
            with path.open("a", encoding="utf-8") as history:
                history.write("".join(record if isinstance(record, str) else json.dumps(record) + "\n"
                                      for record in records))

        def at_selection(action):
            """Run action after the listing has picked the thread and before the source reads it."""
            def selected(threads, thread):
                summary = real_match(threads, thread)
                action()
                return summary
            real_match = thread_reader.match_thread
            return patch.object(thread_reader, "match_thread", selected)

        def claude_append_after_selection(stack, codex, claude):
            thread, path = claude_session(claude, self.project, claude_prompt(), claude_reply("Here."))
            stack.enter_context(at_selection(lambda: append(
                path, {**claude_prompt(), **elsewhere}, {**claude_reply("Elsewhere."), **elsewhere})))
            return lambda: self.read("claude", thread), "^No readable thread with that ID"

        def cwd_symlink_retargeted_after_selection(stack, codex, claude):
            linked = claude.parent / "linked"
            linked.parent.mkdir()
            linked.symlink_to(self.worktree)
            thread, path = claude_session(claude, linked, claude_prompt(), claude_reply("Here."))

            def retarget():
                linked.unlink()
                linked.symlink_to(self.other)
                append(path, {**claude_reply("Elsewhere."), "cwd": str(linked)})

            stack.enter_context(at_selection(retarget))
            return lambda: self.read("claude", thread), "^No readable thread with that ID"

        def codex_replaced_after_selection(stack, codex, claude):
            thread, path = codex_thread(codex, self.project, codex_turn("t1", "Here."))
            _, foreign = codex_thread(codex.parent / "elsewhere-codex", self.other, codex_turn("t1", "Elsewhere."),
                                      meta={"id": thread})
            stack.enter_context(at_selection(lambda: os.replace(foreign, path)))
            return lambda: self.read("codex", thread), "^No readable thread with that ID"

        def move_hidden_beyond_the_first_window(stack, codex, claude):
            thread, path = claude_session(claude, self.project, claude_prompt(), claude_reply("Here."))
            append(path, {**claude_prompt(), **elsewhere}, {**claude_reply("Elsewhere."), **elsewhere}, large_record)
            stack.enter_context(patch.object(thread_reader, "INITIAL_TAIL_BYTES", 1024))
            return lambda: self.read("claude", thread), "^No readable thread with that ID"

        def directory_beyond_the_cap(stack, codex, claude):
            thread, path = claude_session(claude, self.project, claude_prompt(), claude_reply("Here."))
            append(path, large_record)
            stack.enter_context(patch.object(thread_reader, "INITIAL_TAIL_BYTES", 1024))
            stack.enter_context(patch.object(thread_reader, "MAX_TAIL_BYTES", 2048))
            include = self.reader("claude").same_repository(self.project)
            summary = ThreadSummary(thread, None, str(self.project), 0.0)
            return (lambda: ClaudeSessionsSource().read_thread(summary, 3, include),
                    "^The session's recent history does not name its working directory.$")

        def moved_in_after_a_foreign_answer(stack, codex, claude):
            thread, path = claude_session(claude, self.other, claude_prompt(), claude_reply("Started elsewhere."),
                                          {"type": "custom-title", "customTitle": "Elsewhere title"}, project="other")
            append(path, {**claude_prompt(), "cwd": str(self.project)})
            listing = self.threads("claude")["threads"]
            self.assertEqual([(entry["id"], entry["name"]) for entry in listing], [(thread, None)])
            with self.assertRaisesRegex(ThreadUnavailable, "^No readable thread with that ID"):
                self.read("claude", "Elsewhere title")
            return (lambda: self.read("claude", thread),
                    ([], {"status": "inProgress", "finished": False, "liveness": "unknown"}, None))

        rows = (claude_append_after_selection, cwd_symlink_retargeted_after_selection, codex_replaced_after_selection,
                move_hidden_beyond_the_first_window, directory_beyond_the_cap, moved_in_after_a_foreign_answer)
        for row in rows:
            homes = self.root / row.__name__
            codex, claude = homes / "codex-home", homes / "claude-home"
            with self.subTest(row.__name__), ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, CODEX_HOME=str(codex), CLAUDE_CONFIG_DIR=str(claude)))
                read, expected = row(stack, codex, claude)
                if isinstance(expected, str):
                    with self.assertRaisesRegex(ThreadUnavailable, expected):
                        read()
                else:
                    result = read()
                    self.assertEqual(([message["text"] for message in result["messages"]], result["latest_turn"],
                                      result["name"]), expected)

    def fixture(self, name):
        """A real session file with every text replaced (dev/scrub_fixture.py), placed in this test's project."""
        text = (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.replace("{CWD}", json.dumps(str(self.project))[1:-1]).splitlines()]

    def test_real_histories_of_both_apps_are_read(self):
        # Synthetic records prove each rule; these prove the parsers still fit what the apps actually write.
        identifier = str(uuid.uuid4())
        rollout = self.codex_home / "sessions" / "2026" / "04" / "16" / f"rollout-2026-04-16T12-22-13-{identifier}.jsonl"
        rollout.parent.mkdir(parents=True)
        records = self.fixture("codex-desktop-rollout.jsonl")
        records[0]["payload"]["id"] = identifier
        rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        events = [record["payload"] for record in records]
        final = [event["last_agent_message"] for event in events if event.get("type") == "task_complete"][-1]
        result = self.read("codex", identifier)
        self.assertEqual((result["latest_turn"]["status"], result["messages"][-1]["text"]), ("completed", final))
        session = self.claude_home / "projects" / "project" / f"{uuid.uuid4()}.jsonl"
        session.parent.mkdir(parents=True)
        records = self.fixture("claude-desktop-session.jsonl")
        for record in records:
            if "sessionId" in record:
                record["sessionId"] = session.stem
        session.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
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
