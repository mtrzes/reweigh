#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["zstandard==0.25.0"]
# ///
"""A shadow Codex thread source over `codex app-server`, and the parity check that decides whether it may replace the
file reader. A developer tool, not part of the shipped plugin; it runs only on a copied Codex home, on macOS:

    uv run --no-project --script dev/codex_app_server.py [--fresh-copy]

ADR 0001 sets the conditions this module enforces: it runs the desktop's own Codex executable, sends only the
allowlisted requests, rejects every request from the server, and leaves the copied home's rollouts and databases
unchanged. A probe also showed that an app-server start deletes other Codex processes' temporary folders under the
home, so this source is never pointed at a live Codex home.
"""

import argparse
import fcntl
import hashlib
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent),
                str(Path(__file__).resolve().parents[1] / "plugins" / "peer-bridge" / "scripts")]
from consultation import state_directory  # noqa: E402
from parity_rules import (  # noqa: E402
    app_server_agent_messages, app_server_snapshot, app_server_summary, parity_differences, parity_passed,
    parity_verdict,
)
from thread_parsing import ThreadListing, ThreadUnavailable, rollout_format, rollout_identifier  # noqa: E402
from thread_reader import CodexFilesSource  # noqa: E402

ALLOWED_REQUESTS = ("initialize", "thread/list", "thread/read", "thread/turns/list", "thread/items/list")
ALLOWED_NOTIFICATIONS = ("initialized",)
REQUEST_TIMEOUT_SECONDS = 60
PAGE_SIZE = 100
# Files a sandboxed app-server must not change. SQLite rewrites a WAL database's -shm index whenever it is opened, even
# by a reader, so it is neither cloned nor counted as a change to the database.
DURABLE_FOLDERS = ("sessions", "archived_sessions")
DURABLE_FILES = ("session_index.jsonl",)
DURABLE_DATABASE_SUFFIXES = (".sqlite", ".sqlite-wal")
# Databases that hold thread state; any other database, such as Codex's own log database, is reported apart.
THREAD_DATABASE_PREFIXES = ("state_", "thread_history_")
# The database names used by the supported desktop app-server for thread/list and turn history.
STATE_DATABASE = "state_5.sqlite"
THREAD_DATABASES = (STATE_DATABASE, "thread_history_1.sqlite")
COPY_RECORD = "copy.json"
SYSTEM_ENVIRONMENT = {"PATH": "/usr/bin:/bin"}
DESKTOP_APP = Path("/Applications/ChatGPT.app")
BUNDLED_DESKTOP_CODEX = DESKTOP_APP / "Contents" / "Resources" / "codex-cli" / "bin" / "codex"
# The Apple team that signs ChatGPT.app; its seal covers the bundled Codex executable.
DESKTOP_TEAM = "2DC432GLL2"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


REFUSAL = {"code": -32601, "message": "Rejected by peer-bridge."}


class AppServerError(Exception):
    """The app-server could not be used safely or did not answer."""


def check_outgoing(message):
    """Raise unless message is an allowlisted request, an allowlisted notification, or the fixed refusal.

    Every message the client writes passes through here, so no call site can send anything else.
    """
    keys = set(message) if isinstance(message, dict) else set()
    if keys == {"id", "method", "params"} and type(message["id"]) is int and isinstance(message["params"], dict):
        method, params = message["method"], message["params"]
        if method not in ALLOWED_REQUESTS:
            raise AppServerError(f"{method} is not an allowed request.")
        if method == "thread/list" and params.get("useStateDbOnly") is not True:
            raise AppServerError("thread/list must set useStateDbOnly.")
        if method == "thread/read" and params.get("includeTurns"):
            raise AppServerError("thread/read must not include turns.")
        return
    if keys == {"method"}:
        if message["method"] not in ALLOWED_NOTIFICATIONS:
            raise AppServerError(f"{message['method']} is not an allowed notification.")
        return
    if keys == {"id", "error"} and message["error"] == REFUSAL:
        return
    raise AppServerError("Only allowlisted requests, notifications and the fixed refusal may be sent.")


class AppServerClient:
    """A JSON-lines client for `codex app-server` that can send only the allowlisted messages."""

    def __init__(self, executable, codex_home, sandbox_home):
        sandbox_home = Path(sandbox_home)
        (sandbox_home / "tmp").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.environment = {**SYSTEM_ENVIRONMENT, "HOME": str(sandbox_home), "CODEX_HOME": str(codex_home),
                            "TMPDIR": str(sandbox_home / "tmp")}
        self.executable = executable
        self.server_requests, self.notifications = [], []
        self.process = None
        self.buffer = b""
        self.next_id = 0

    def __enter__(self):
        self.stderr = open_private(Path(self.environment["HOME"]) / "app-server.log", append=True)
        self.process = subprocess.Popen(
            [self.executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            env=self.environment, start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.request("initialize", {"clientInfo": {"name": "peer-bridge", "version": "parity"},
                                        "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
        except BaseException:
            self.__exit__(None, None, None)  # a failed handshake must not leave the server running
            raise
        return self

    def __exit__(self, *exc):
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=10)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        finally:
            self.selector.close()
            for stream in (self.process.stdin, self.process.stdout, self.stderr):
                try:
                    stream.close()
                except BrokenPipeError:
                    pass

    def write(self, message):
        check_outgoing(message)
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AppServerError("The app-server closed its input.") from exc

    def notify(self, method):
        self.write({"method": method})

    def request(self, method, params):
        check_outgoing({"id": self.next_id + 1, "method": method, "params": params})
        self.next_id += 1
        self.write({"id": self.next_id, "method": method, "params": params})
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            message = self.receive(deadline)
            if "method" in message and "id" in message:
                # The source never answers a server request with anything but a refusal.
                self.server_requests.append(message["method"])
                self.write({"id": message["id"], "error": REFUSAL})
            elif "method" in message:
                self.notifications.append(message["method"])
            elif message.get("id") == self.next_id:
                if "error" in message:
                    raise AppServerError(f"{method} failed with code {message['error'].get('code')}.")
                return message.get("result")

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            if not self.selector.select(timeout=max(0, deadline - time.monotonic())):
                raise AppServerError("The app-server did not answer in time.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AppServerError("The app-server exited.")
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                raise AppServerError("The app-server sent a message larger than the limit.")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise AppServerError("The app-server sent a malformed message.") from exc
        if not isinstance(message, dict):
            raise AppServerError("The app-server sent a malformed message.")
        return message

    def pages(self, method, params):
        cursor = None
        while True:
            result = self.request(method, {**params, "cursor": cursor} if cursor else params)
            yield from result.get("data") or []
            cursor = result.get("nextCursor")
            if not cursor:
                return


class AppServerSource:
    """Codex Desktop and interactive CLI threads through an open AppServerClient. Shadow only; see the module doc."""

    provider = "codex"

    def __init__(self, client):
        self.client = client

    def list_threads(self, include):
        threads = []
        for archived in (False, True):
            params = {"useStateDbOnly": True, "archived": archived, "sourceKinds": ["cli", "vscode"],
                      "limit": PAGE_SIZE}
            for thread in self.client.pages("thread/list", params):
                summary = app_server_summary(thread, archived)
                if summary and include(summary.cwd):
                    threads.append(summary)
        return ThreadListing(threads)

    def read_thread(self, summary, max_messages, include=None):
        """include is the file reader's repository check, which the app-server answers for itself."""
        if summary.forked_from_id:
            raise ThreadUnavailable("Forked Codex threads are not supported yet.")
        turns = self.client.request("thread/turns/list", {
            "threadId": summary.id, "sortDirection": "desc", "limit": max_messages, "itemsView": "notLoaded",
        }).get("data") or []
        messages = {}
        for turn in turns:
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                entries = self.client.pages("thread/items/list", {"threadId": summary.id, "turnId": turn["id"],
                                                                  "limit": PAGE_SIZE})
                messages[turn["id"]] = app_server_agent_messages(entries)
        return app_server_snapshot(summary, turns, messages, max_messages)


def signed_desktop_app():
    """Whether ChatGPT.app's seal is intact and its signing team is OpenAI's, read from codesign's exact fields."""
    verified = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(DESKTOP_APP)],
                              capture_output=True, env=SYSTEM_ENVIRONMENT)
    if verified.returncode:
        return False
    details = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=2", str(DESKTOP_APP)],
                             capture_output=True, text=True, env=SYSTEM_ENVIRONMENT)
    return details.returncode == 0 and f"TeamIdentifier={DESKTOP_TEAM}" in details.stderr.splitlines()


def desktop_codex_executable():
    """The Codex executable the desktop runs: ChatGPT.app's own, sealed by its signature, never any other process
    that names itself after it. Says whether a desktop app-server is running it."""
    bundled = os.path.realpath(BUNDLED_DESKTOP_CODEX)
    if not Path(bundled).is_file() or not signed_desktop_app():
        raise AppServerError("Cannot find a ChatGPT.app whose signature covers its Codex executable.")
    listing = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                             env=SYSTEM_ENVIRONMENT).stdout
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if os.path.realpath(command) != bundled:
            continue
        arguments = subprocess.run(["/bin/ps", "-o", "args=", "-p", pid], capture_output=True, text=True,
                                   env=SYSTEM_ENVIRONMENT).stdout.split()
        if "app-server" in arguments[1:2]:
            return bundled, "running desktop app-server"
    return bundled, "ChatGPT.app's bundled Codex (no desktop app-server running)"


def codex_version(executable, environment):
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, env=environment, timeout=30)
    return result.stdout.strip() or "unknown"


def check_isolated(path, live_home):
    """Refuse a sandbox path that is or passes through a symlink, or that overlaps the live Codex home."""
    path, live = Path(path).absolute(), Path(live_home).resolve()
    resolved = path.resolve()
    if resolved != path:
        raise AppServerError(f"{path} passes through a symlink; the parity sandbox must not.")
    if resolved == live or live in resolved.parents or resolved in live.parents:
        raise AppServerError(f"{path} overlaps the live Codex home; the parity sandbox must stay apart from it.")


def remove_symlinks(home):
    """Remove every symlink under home, so nothing in the copy reaches a live file. Returns how many were removed."""
    removed = 0
    for directory, folders, files in os.walk(home, followlinks=False):
        for name in (*folders, *files):
            path = Path(directory) / name
            if path.is_symlink():
                path.unlink()
                removed += 1
    return removed


def open_private(path, append=False):
    """Open a file for writing without following a symlink at its name, creating it readable by the owner only."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    try:
        return os.fdopen(os.open(path, flags, 0o600), "ab" if append else "w")
    except OSError as exc:
        raise AppServerError(f"Cannot write {path.name} in the parity sandbox; it may be a symlink.") from exc


def clone_inputs(source):
    """Select only regular rollouts, the session index and the supported thread databases and WAL files."""
    def rollouts(directory):
        for path in directory.iterdir():
            if path.is_symlink():
                continue
            if path.is_dir():
                yield from rollouts(path)
            elif path.name.startswith("rollout-") and rollout_identifier(path.name):
                if not rollout_format(path.name):
                    raise AppServerError("Cannot clone an unsupported Codex rollout format.")
                yield path

    state = source / STATE_DATABASE
    if state.is_symlink() or not state.is_file():
        raise AppServerError(f"Cannot clone the Codex home without {STATE_DATABASE}.")
    for name in (*DURABLE_FILES, *THREAD_DATABASES, *(database + "-wal" for database in THREAD_DATABASES)):
        path = source / name
        if path.exists() and not path.is_symlink():
            yield path
    for name in DURABLE_FOLDERS:
        directory = source / name
        if directory.is_dir() and not directory.is_symlink():
            yield from rollouts(directory)


def clone_home(source, destination):
    """Stage a macOS clone containing only parity inputs, then publish it with its copy identity."""
    if sys.platform != "darwin":
        raise AppServerError("The parity check clones the Codex home with APFS, which needs macOS.")
    if Path(source).resolve() != Path(source) or not Path(source).is_dir():
        raise AppServerError("The live Codex home must be given as a resolved directory.")
    partial = destination.with_name(destination.name + ".partial")
    for path in (destination, partial):
        check_isolated(path, source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    discard_partial(partial)
    partial.mkdir(mode=0o700)
    try:
        for path in clone_inputs(source):
            target = partial / path.relative_to(source)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise AppServerError(f"Cannot clone non-regular input {path.name}.")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = subprocess.run(["/bin/cp", "-c", "-p", "-P", str(path), str(target)],
                                    capture_output=True, env=SYSTEM_ENVIRONMENT)
            if result.returncode or target.is_symlink() or not target.is_file():
                raise AppServerError(f"Cannot clone {path.name}.")
        with open_private(partial / COPY_RECORD) as output:
            json.dump({"copy_id": str(uuid.uuid4()),
                       "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, output)
        os.rename(partial, destination)
    finally:
        discard_partial(partial)


def discard_partial(partial):
    if partial.is_symlink():
        partial.unlink()  # removes the link only, never its target
    elif partial.exists():
        shutil.rmtree(partial, ignore_errors=True)


def content_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(home):
    """Content hashes of the rollouts, the session index and the databases, keyed by path relative to the home."""
    paths = [path for folder in DURABLE_FOLDERS for path in (home / folder).rglob("*")]
    paths += [home / name for name in DURABLE_FILES]
    paths += [path for path in home.iterdir() if path.name.endswith(DURABLE_DATABASE_SUFFIXES)]
    prints = {}
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        # An empty WAL file holds no pages, so it is the same database state as no WAL file.
        if path.name.endswith(".sqlite-wal") and not path.stat().st_size:
            continue
        prints[str(path.relative_to(home))] = content_digest(path)
    return prints


def changed_paths(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def is_thread_data(path):
    """Whether a fingerprinted path holds thread data: a rollout, the session index or a thread-state database."""
    return path.startswith(tuple(f"{folder}/" for folder in DURABLE_FOLDERS)) or path in DURABLE_FILES or (
        path.startswith(THREAD_DATABASE_PREFIXES)
    )


def compare(file_source, app_source):
    """Compare the two sources on every Desktop and CLI thread the file reader can read, in any repository."""
    everything = lambda cwd: True  # noqa: E731  (the copy is compared machine-wide, not per repository)
    app_threads = {summary.id: summary for summary in app_source.list_threads(everything).threads}
    compared, unreadable, missing, mismatches = 0, {}, [], []
    for summary in file_source.list_threads(everything).threads:
        try:
            expected = file_source.read_thread(summary, 3, everything)
        except ThreadUnavailable as exc:
            unreadable[str(exc)] = unreadable.get(str(exc), 0) + 1
            continue
        except (OSError, TypeError, AttributeError):
            unreadable["The thread's history file could not be read."] = unreadable.get(
                "The thread's history file could not be read.", 0) + 1
            continue
        compared += 1
        if summary.id not in app_threads:
            missing.append(summary.id)
            continue
        try:
            actual = app_source.read_thread(app_threads[summary.id], 3)
        except ThreadUnavailable as exc:
            mismatches.append({"id": summary.id, "fields": ["read"], "reason": str(exc)})
            continue
        differences = parity_differences(expected, actual)
        if differences:
            mismatches.append({"id": summary.id, "fields": differences})
    return {"threads_compared": compared, "file_unreadable": unreadable, "missing": missing, "mismatches": mismatches}


def run_parity(state_directory, fresh_copy=False):
    """Run one parity check on the persistent copied home under the state directory and save its report."""
    live = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    root = Path(state_directory) / "parity"
    check_isolated(root, live)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open_private(root / "parity.lock", append=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_locked_parity(root, live, fresh_copy)


def run_locked_parity(root, live, fresh_copy):
    copy, sandbox, reports = root / "codex-home", root / "home", root / "reports"
    copy_record = copy / COPY_RECORD
    for path in (copy, sandbox, sandbox / "tmp", reports, copy_record):
        check_isolated(path, live)
    discard_partial(copy.with_name(copy.name + ".partial"))
    if sandbox.is_dir():
        remove_symlinks(sandbox)
    # Clones made before the record moved inside may contain files outside the allowlist. Rebuild them.
    if copy.is_dir() and (fresh_copy or not copy_record.is_file()):
        shutil.rmtree(copy)
    if not copy.is_dir():
        clone_home(live, copy)
    record = json.loads(copy_record.read_text())
    executable, executable_source = desktop_codex_executable()
    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "copy_id": record["copy_id"],
        "copy_created_at": record["created_at"], "executable": executable, "executable_source": executable_source,
    }
    # The app-server makes symlinks to its own executable on each start; none may survive into another run.
    report["symlinks_removed"] = remove_symlinks(copy)
    before = fingerprint(copy)
    with AppServerClient(executable, copy, sandbox) as client:
        report["codex_version"] = codex_version(executable, client.environment)
        report.update(compare(CodexFilesSource(copy), AppServerSource(client)))
        report["server_requests"] = sorted(set(client.server_requests))
    changes = changed_paths(before, fingerprint(copy))
    remove_symlinks(copy)
    report["thread_data_changes"] = [path for path in changes if is_thread_data(path)]
    report["other_database_changes"] = [path for path in changes if not is_thread_data(path)]
    report["passed"] = parity_passed(report)
    reports.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open_private(reports / f"{report['ran_at'].replace(':', '')}.json") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    saved = [json.loads(path.read_text()) for path in sorted(reports.glob("*.json"))]
    report["ready_to_replace_file_reader"], report["verdict"] = parity_verdict(saved)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fresh-copy", action="store_true",
                        help="discard the copied Codex home and clone the live one again")
    arguments = parser.parse_args()
    try:
        result = run_parity(state_directory(os.environ, Path.home()), arguments.fresh_copy)
    except AppServerError as exc:
        result = {"error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
