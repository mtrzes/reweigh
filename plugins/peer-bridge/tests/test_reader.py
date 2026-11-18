"""Exercise the thread sources over synthetic session files: same-repository listing, agent-only reads and refusals.

Most tests give ThreadReader a stand-in for git's common-directory lookup; one runs real git through a worktree.
"""

import asyncio
import errno
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
import history_capture  # found through the script directory support.py adds
import thread_reader
from thread_reader import ClaudeSessionsSource, CodexFilesSource, ThreadReader, ThreadUnavailable


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

        def archive_after_header(point, read_path, *details):
            if (point, read_path) == ("header_read", path):
                path.rename(archive / path.name)

        with patch.object(history_capture, "checkpoint", archive_after_header):
            rows = self.threads("codex")["threads"]
        self.assertEqual((path.exists(), (archive / path.name).exists()), (False, True))
        self.assertEqual([row["id"] for row in rows], [other, moving])
        self.assertEqual(rows[1]["updated_at"], "1970-01-01T00:16:40+00:00")

    def test_codex_index_only_reads_recent_complete_lines_within_its_budget(self):
        older, _ = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Older")
        newer, _ = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), name="Newer")
        index = self.codex_home / "session_index.jsonl"
        first, second = index.read_bytes().splitlines(keepends=True)
        index.write_bytes(first + b"x" * 1024 + b"\n" + second)
        with patch.object(history_capture, "MAX_METADATA_BYTES", len(second) + 10):
            names = {row["id"]: row["name"] for row in self.threads("codex")["threads"]}
        self.assertEqual(names, {older: None, newer: "Newer"})

    def test_a_session_refused_after_its_repository_matched_is_not_counted(self):
        # The unsupported count is machine-wide: it must not depend on which repository the listing asks about.
        broken, _ = claude_session(self.claude_home, self.project, claude_prompt() | {"parentUuid": 5},
                                   claude_reply("Done."), title="Broken ancestry")
        for directory, rows in ((self.project, [(broken, None)]), (self.other, [])):
            with self.subTest(directory=directory):
                listing = self.threads("claude", directory)
                self.assertEqual([(row["id"], row["name"]) for row in listing["threads"]], rows)
                self.assertNotIn("unsupported_threads_machine_wide", listing)

    def test_a_malformed_prompt_leaves_the_session_listed(self):
        claude_session(self.claude_home, self.project, claude_prompt(5), claude_reply("Done."), title="Claude types")
        claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."), entrypoint=5)
        # The listing reads only titles; the read refuses the prompt (test_core).
        self.assertEqual([thread["name"] for thread in self.threads("claude")["threads"]], ["Claude types"])

    def test_a_reader_fault_is_not_reported_as_a_damaged_file(self):
        class Faulty:
            def read(self, *arguments):
                raise AttributeError("a fault in the reader")

        client = bridge.Bridge(readers={"claude": Faulty()})
        with self.assertRaisesRegex(bridge.BridgeError, "^The thread reader failed unexpectedly.$"):
            asyncio.run(client.thread("claude", str(self.project), "Claude types"))

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
        def decompressed():
            with history_capture.open_history(path) as source:
                return history_capture.decompress_history(source)

        head, whole, from_start = decompressed()
        self.assertTrue(from_start)
        self.assertEqual(json.loads(head.split(b"\n")[0])["type"], "session_meta")
        with patch.object(history_capture, "MAX_TAIL_BYTES", 4096):
            head_again, tail, from_start = decompressed()
        self.assertEqual((head_again, len(tail), from_start), (head, 4096, False))
        self.assertTrue(whole.endswith(tail))

    def test_a_compressed_rollout_is_refused_past_its_decoded_budget(self):
        identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."),
                                        suffix=".jsonl.zst", frames=3)
        with history_capture.open_history(path) as source:
            decoded = len(history_capture.decompress_history(source)[1])
        # Each frame fits the budget; together they do not.
        with patch.object(history_capture, "MAX_DECOMPRESSED_BYTES", decoded - 1):
            with self.assertRaisesRegex(ThreadUnavailable, "compressed history is too large"):
                self.read("codex", identifier)
        with patch.object(history_capture, "MAX_DECOMPRESSED_BYTES", decoded):
            self.assertEqual(self.read("codex", identifier)["messages"][0]["text"], "Done.")

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
        with patch.object(history_capture, "MAX_METADATA_BYTES", 1024):
            self.assertEqual(self.threads("codex")["threads"], [])
        path.write_bytes(content + skippable)
        self.assertEqual([row["id"] for row in self.threads("codex")["threads"]], [identifier])
        with patch.object(history_capture, "MAX_COMPRESSED_BYTES", 1024):
            with self.assertRaisesRegex(ThreadUnavailable, "compressed history is too large"):
                self.read("codex", identifier)

    def test_each_compressed_read_attempt_uses_its_open_extent(self):
        import zstandard

        identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "First."),
                                        suffix=".jsonl.zst")
        records = "".join(json.dumps(record) + "\n" for record in codex_turn("t2", "Second.")).encode()
        frame = zstandard.ZstdCompressor().compress(records)
        decoded = []

        def append_after_first_open(point, read_path, attempt=None, history=None):
            if (point, attempt) == ("opened", 0):
                with path.open("ab") as output:
                    output.write(frame)
            elif point == "attempted":
                decoded.append(history.tail(history_capture.MAX_TAIL_BYTES).data)

        with patch.object(history_capture, "checkpoint", append_after_first_open):
            self.assertEqual(self.read("codex", identifier)["messages"][-1]["text"], "Second.")
        self.assertEqual(len(decoded), 2)
        self.assertFalse(decoded[0].endswith(records))
        self.assertEqual(decoded[1], decoded[0] + records)

    def test_a_history_that_changes_while_it_is_read_is_read_once_more_then_refused(self):
        import zstandard

        record = (json.dumps(codex_event("token_count", "t1")) + "\n").encode()
        for suffix, extra in ((".jsonl", record), (".jsonl.zst", zstandard.ZstdCompressor().compress(record))):
            identifier, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."), suffix=suffix)
            # Each change grows the file, so it is seen by its size whatever the clock's resolution.
            for change, steps, expected in (
                ("refused while damaged, then changed", {("opened", 0): "damage", ("attempted", 0): "restore"}, None),
                ("changed once", {("opened", 0): "append"}, None),
                ("changed twice", {("opened", 0): "append", ("opened", 1): "append"},
                 history_capture.CHANGED_WHILE_READ),
            ):
                content = path.read_bytes()
                actions = {"damage": lambda: path.write_bytes(b"\x00" * len(content)),
                           "restore": lambda: path.write_bytes(content + extra),
                           "append": lambda: path.write_bytes(path.read_bytes() + extra)}

                def change_at(point, read_path, attempt=None, history=None):
                    if (point, attempt) in steps:
                        actions[steps[point, attempt]]()

                with self.subTest(suffix=suffix, change=change), patch.object(history_capture, "checkpoint", change_at):
                    if expected:
                        with self.assertRaisesRegex(ThreadUnavailable, f"^{expected}$"):
                            self.read("codex", identifier)
                    else:
                        self.assertEqual(self.read("codex", identifier)["messages"][0]["text"], "Done.")

    def test_a_path_that_names_another_file_after_it_was_opened_is_read_again(self):
        thread, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Opened."))
        _, replacement = codex_thread(self.root / "elsewhere-codex", self.project, codex_turn("t1", "Replaced."),
                                      meta={"id": thread})
        moved = self.root / "moved-day"

        def replace_directory_after_open(point, read_path, attempt=None, history=None):
            if (point, attempt) == ("opened", 0):
                # Moving the enclosing directory leaves the open file's own metadata untouched: only the path check
                # sees that the path now names another file.
                path.parent.rename(moved)
                path.parent.mkdir()
                os.replace(replacement, path)

        with patch.object(history_capture, "checkpoint", replace_directory_after_open):
            self.assertEqual(self.read("codex", thread)["messages"][0]["text"], "Replaced.")

    def test_a_history_below_a_symlinked_directory_is_not_opened(self):
        _, path = claude_session(self.claude_home, self.project, claude_prompt(), claude_reply("Done."))
        moved = self.root / "moved-project"
        path.parent.rename(moved)
        path.parent.symlink_to(moved)
        self.assertTrue(path.is_file())
        with self.assertRaises(OSError):
            history_capture.open_history(path, self.claude_home)

    def test_an_io_error_in_the_identity_check_is_not_reported_as_a_change(self):
        _, path = codex_thread(self.codex_home, self.project, codex_turn("t1", "Done."))
        # Only the identity check uses lstat: the history is opened through directory descriptors.
        with patch.object(Path, "lstat", side_effect=OSError(errno.EIO, "Input/output error")):
            with self.assertRaises(OSError) as raised:
                history_capture.read_stable(self.codex_home, lambda: path, lambda history: history.first_line())
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_only_a_missing_zstandard_package_is_reported_as_missing(self):
        with patch.dict(sys.modules, {"zstandard": None}):
            with self.assertRaisesRegex(ThreadUnavailable, "needs the zstandard package"):
                history_capture.zstandard_module()
        real_import = __import__

        def broken(name, *arguments, **keywords):
            if name == "zstandard":
                raise ImportError("dlopen failed: incompatible architecture")
            return real_import(name, *arguments, **keywords)

        with patch.dict(sys.modules), patch("builtins.__import__", broken):
            sys.modules.pop("zstandard", None)
            with self.assertRaisesRegex(ImportError, "incompatible architecture"):
                history_capture.zstandard_module()

    def test_a_claude_read_takes_its_title_from_the_bytes_it_reads(self):
        # The last replies fit the first tail window; the title lies further back, where only the title search looks.
        replies = [entry for _ in range(60) for entry in (claude_prompt(), claude_reply("x" * 5000))]
        _, path = claude_session(self.claude_home, self.project, claude_prompt(),
                                 {"type": "custom-title", "customTitle": "Early title"}, *replies)
        real_match = thread_reader.match_thread

        def retitle_after_selection(threads, thread):
            summary = real_match(threads, thread)
            path.write_text(path.read_text(encoding="utf-8").replace("Early title", "Later title"), encoding="utf-8")
            return summary

        with patch.object(thread_reader, "match_thread", retitle_after_selection):
            self.assertEqual(self.read("claude", "Early title")["name"], "Later title")

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
        # The read decides again from the bytes it returns: each row runs in homes of its own.
        elsewhere = {"cwd": str(self.other)}

        def append(path, *records):
            with path.open("a", encoding="utf-8") as history:
                history.write("".join(json.dumps(record) + "\n" for record in records))

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
                moved_in_after_a_foreign_answer)
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
