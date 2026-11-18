#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.30.0", "zstandard==0.25.0"]
# ///
"""Consult Claude Code or Codex through dedicated, resumable CLI sessions."""

import argparse
import asyncio
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import consultation
from consultation import (
    ACTIVE_ENV, CLOSED_INPUT, BridgeError, FailureFacts, PeerFailure, build_command, build_handoff,
    check_limit, check_prompt, check_provider, check_thread_request, codex_mcp_server_names, completed_record,
    consultation_outcome, consultation_result, consultation_timeout, consulted_ref, executable_variable,
    failure_message, filter_overrides, git_environment, new_record, output_signals, peer_environment, resumed_record,
    THREADS_OFF, safe_path, seed_context, threads_enabled, signed_in_from, validate_commit, validate_session_id, with_seed,
)
from consultation import (
    FINISHED_JOB_STATES, catalogue_models, check_against_catalogue, check_codex_choices, check_wait, codex_choices,
    git_entry_found,
)
from cleanup import run_to_completion
from jobs import JobStore, job_request, job_store_errors, run_job, spawn_runner, start_job
from state_files import PRIVATE, check_private, ensure_private, open_regular, read_json, write_json
import peer_guard
from thread_reader import ClaudeSessionsSource, CodexFilesSource, ThreadReader, ThreadUnavailable

UNKNOWN_CONVERSATION = "Unknown or unreadable conversation. Start a new consultation."
START_UNCONFIRMED = (
    "Consultation job {job_id} was started but cannot be read, and its cancellation could not be confirmed. Cancel it "
    "with that job ID."
)
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
# On macOS, ChatGPT.app bundles the Codex CLI without putting it on PATH.
MACOS_APPLICATION_DIRECTORIES = ("/Applications", "~/Applications")
BUNDLED_CODEX_PATH = Path("ChatGPT.app", "Contents", "Resources", "codex-cli", "bin", "codex")
# A pinned checkout is prepared within one budget, every git command in it sharing what is left; removing it, after the
# consultation or when a later one sweeps it up, has an allowance of its own.
CHECKOUT_PREPARATION_SECONDS = 300
CHECKOUT_REMOVAL_SECONDS = 120
GIT_LATE = "git did not finish in time while preparing the pinned ref."
SIGN_IN_PROBE_SECONDS = 30
SIGN_IN_PROBES = {"claude": ("auth", "status", "--json"), "codex": ("login", "status")}
GUARD_SCRIPT = Path(__file__).resolve().with_name("peer_guard.py")
# How long past the guard's own grace period to wait for it to stop the peer's group.
GUARD_MARGIN_SECONDS = 3
# How long to keep reading a peer's pipes after it exits, for output still buffered in them.
PIPE_DRAIN_SECONDS = 1
SEED_MESSAGES = 3
# How often a caller waiting on a background consultation rereads its state.
JOB_POLL_SECONDS = 0.5


class Budget:
    """A time allowance shared by successive commands: each may run for what is left of it."""

    def __init__(self, seconds, clock):
        self.clock = clock
        self.deadline = clock() + seconds

    def left(self):
        return self.deadline - self.clock()


class CommandTimeout(BridgeError):
    """A command the bridge ran did not finish within its timeout."""


class InputClosed(Exception):
    """The peer exited before reading the whole handoff; its output may still explain why."""

    def __init__(self, output, errors):
        super().__init__("The peer CLI closed its input.")
        self.output, self.errors = output, errors


def is_conversation_id(name):
    try:
        return validate_session_id(name) == name
    except BridgeError:
        return False


def state_directory():
    return consultation.state_directory(os.environ, Path.home())


class SessionStore:
    """Keep bridge-owned sessions bound to their provider and project.

    clock and runner, time.monotonic and run_command unless given, are what every conversation scope opened here times
    its budgets with and runs its guarded commands through.
    """

    def __init__(self, directory, clock=None, runner=None):
        self.directory = Path(directory).absolute()
        self.clock, self.runner = clock, runner

    def checkout_location(self, identifier):
        """Where a conversation's pinned checkout lives: fixed, because Claude resumes a session only from the directory
        it ran in."""
        return self.directory / "worktrees" / identifier

    @contextmanager
    def open(self, provider, working_directory, conversation_id):
        """Open a conversation, new or resumed: yields its Conversation scope, holding its lock and record."""
        identifier = validate_session_id(conversation_id) if conversation_id else str(uuid.uuid4())
        try:
            ensure_private(self.directory)
        except OSError as exc:
            raise BridgeError("Cannot create the bridge's state directory.") from exc
        with self.holding(identifier) as held:
            if held is None:
                raise BridgeError("This conversation is busy. Wait for its current consultation to finish.")
            path = self.directory / f"{identifier}.json"
            record = new_record(provider, working_directory)
            if conversation_id:
                saved = read_json(path, unreadable=UNKNOWN_CONVERSATION)
                if saved is None:
                    raise BridgeError(UNKNOWN_CONVERSATION)
                record = resumed_record(saved, provider, working_directory)
            yield Conversation(held, identifier, record, path)

    def idle_worktrees(self):
        """The conversations with a pinned checkout folder, whatever their state; holding() tells which are idle.

        Only real folders named as this bridge names them, a conversation's canonical UUID, in a private worktrees
        folder checked before it is listed: cleanup must never reach through a symlink.
        """
        worktrees = self.directory / "worktrees"
        if not check_private(self.directory, worktrees):
            return []
        return [entry.name for entry in worktrees.iterdir()
                if is_conversation_id(entry.name) and entry.is_dir() and not entry.is_symlink()]

    @contextmanager
    def holding(self, identifier):
        """Hold a conversation's lock while it is free: yields lock-bound execution on it, a ConversationLock, or None
        when a consultation has it. It opens no record; open() builds on it.

        A guard given the descriptor keeps the lock after this context ends, for as long as the guard lives, so this
        never unlocks explicitly: flock belongs to the open file description the guard shares, and LOCK_UN would free
        the conversation while the guard is still stopping its group (test_jobs
        test_a_killed_runners_conversation_stays_locked_until_its_guard_has_stopped_the_peer). Closing only this
        process's copy is what lets the guard keep it. JobStore.flock, whose descriptor no process inherits, unlocks
        explicitly; the two are not interchangeable.
        """
        try:
            descriptor = open_regular(self.directory / f"{identifier}.lock", os.O_CREAT | os.O_RDWR)
        except OSError as exc:
            raise BridgeError("Cannot lock the conversation's state files.") from exc
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            yield ConversationLock(lock.fileno(), self.checkout_location(identifier), self.clock or time.monotonic,
                                   self.runner or run_command)


class ConversationLock:
    """Lock-bound execution on one conversation: every command it runs, peer, sign-in probe or git, runs under the
    guard, which inherits the conversation's locked descriptor, so the conversation and its checkout stay busy until
    the command's process group has stopped. Its clock times the budgets those commands share."""

    def __init__(self, descriptor, worktree, clock, runner):
        self.descriptor, self.worktree, self.clock, self.runner = descriptor, worktree, clock, runner

    def budget(self, seconds):
        return Budget(seconds, self.clock)

    def preparation_budget(self):
        return self.budget(CHECKOUT_PREPARATION_SECONDS)

    def removal_budget(self):
        return self.budget(CHECKOUT_REMOVAL_SECONDS)

    async def run(self, command, prompt, directory, timeout, environment=None):
        """run_command, with the guard holding this conversation's lock."""
        return await self.runner(command, prompt, directory, timeout, environment=environment, lock=self.descriptor)

    async def git(self, directory, *arguments, failure, timeout, config=(), allowed=()):
        """Run git under the guard, isolated from the repository's hooks, monitor and transports and from inherited
        GIT_* variables, plus the settings in config; exit statuses in allowed count as success. A command with no
        time left is not started.

        The output keeps git's exact bytes, decoded as file names are, without its one terminating newline.
        """
        if timeout <= 0:
            raise BridgeError(GIT_LATE)
        try:
            return_code, output, _ = await self.run(["git", "-C", str(directory), *arguments], None, None, timeout,
                                                    environment=git_environment(os.environ, config))
        except OSError as exc:
            raise BridgeError("git is unavailable. Install it to pin a consultation to a ref.") from exc
        except CommandTimeout as exc:
            raise BridgeError(GIT_LATE) from exc
        except BridgeError as exc:
            raise BridgeError(failure) from exc
        if return_code and return_code not in allowed:
            raise BridgeError(failure)
        return os.fsdecode(output).removesuffix("\n")


class Conversation(ConversationLock):
    """An open conversation: its lock, its record and where its pinned checkout lives. It saves the record only once
    the peer's result is validated against it."""

    def __init__(self, held, identifier, record, path):
        super().__init__(held.descriptor, held.worktree, held.clock, held.runner)
        self.identifier, self.record, self.path = identifier, record, path

    def save(self, backend_session_id, commit):
        record = completed_record(self.record, backend_session_id, commit)
        try:
            write_json(self.path, record)
        except OSError as exc:
            raise BridgeError("Cannot save the conversation's state files.") from exc


def codex_mcp_servers():
    """The MCP servers the system Codex configuration declares, which a consultation disables.

    Read without blocking, as a regular file of bounded size: it decides what the consultation may start, so one that
    cannot be read refuses the consultation.
    """
    try:
        # A symlink is followed: administrators link system configuration; the target must still be a regular file.
        descriptor = os.open(consultation.CODEX_SYSTEM_CONFIG, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise BridgeError("Cannot read Codex configuration to disable consultation MCP servers.") from exc
    try:
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("not a regular file")
            data = source.read(consultation.MAX_CONFIG_BYTES + 1)
            if len(data) > consultation.MAX_CONFIG_BYTES:
                raise ValueError("too large")
            return codex_mcp_server_names(tomllib.loads(data.decode("utf-8")))
    except (OSError, ValueError) as exc:
        raise BridgeError("Cannot read Codex configuration to disable consultation MCP servers.") from exc


def bundled_codex_candidates():
    if sys.platform != "darwin":
        return []
    return [Path(directory).expanduser() / BUNDLED_CODEX_PATH for directory in MACOS_APPLICATION_DIRECTORIES]


def peer_executable(provider):
    """Find the peer CLI: the configured binary, then PATH, then the Codex CLI bundled with ChatGPT.app."""
    variable = executable_variable(provider)
    configured = os.environ.get(variable)
    if configured:
        # A bare name is searched on the same safe PATH; a path with a slash is taken as given.
        executable = shutil.which(configured, path=safe_path(os.environ.get("PATH")))
        if not executable:
            raise BridgeError(f"{variable} does not name an executable {provider} CLI.")
        # Absolute, because the peer starts in the consulted directory, where a relative path would resolve again.
        return os.path.abspath(executable)
    # The same PATH the peer is launched with, so the peer found is the peer that runs.
    executable = shutil.which(provider, path=safe_path(os.environ.get("PATH")))
    if executable:
        return os.path.abspath(executable)
    candidates = bundled_codex_candidates() if provider == "codex" else []
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    if candidates:
        raise BridgeError(
            "codex CLI is unavailable. Install it or ChatGPT.app, which bundles it at "
            f"/Applications/{BUNDLED_CODEX_PATH}, and sign in before consulting it."
        )
    raise BridgeError(f"{provider} CLI is unavailable. Install it and sign in before consulting it.")


async def signed_in(provider, directory, scope, peer):
    """Ask the peer CLI whether it is signed in: True, False, or None when it cannot tell. It runs within what is left
    of the peer's budget.

    The probe runs under the guard like the consultation, so a helper it starts cannot outlive a cancelled or timed
    out consultation, and a cancellation passes on only once the probe's group has stopped.
    """
    timeout = min(SIGN_IN_PROBE_SECONDS, peer.left())
    if timeout <= 0:
        return None
    try:
        command = [peer_executable(provider), *SIGN_IN_PROBES[provider]]
        return_code, output, _ = await scope.run(command, None, directory, timeout)
    except (BridgeError, OSError):
        return None
    return signed_in_from(provider, return_code, output.decode("utf-8", errors="replace"))


def codex_catalogue():
    """The models Codex's own catalogue lists, or an empty list when it has none or it cannot be read."""
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "models_cache.json"
    try:
        return catalogue_models(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def command_for(provider, working_directory, backend_session_id, conversation_id, model=None, reasoning_effort=None,
                pinned=False):
    executable = peer_executable(provider)
    if provider == "claude":
        return build_command(provider, executable, conversation_id, backend_session_id)
    return build_command(
        provider, executable, conversation_id, backend_session_id, model=model,
        mcp_servers=codex_mcp_servers(), reasoning_effort=reasoning_effort, pinned=pinned,
    )


async def read_output(stream, chunks):
    """Collect a pipe into chunks, so what arrived stays usable while a descendant holds the pipe open."""
    size = 0
    while chunk := await stream.read(65536):
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            raise BridgeError("Consultation output exceeded the size limit. Ask a narrower question.")
        chunks.append(chunk)


async def peer_exit_status(status):
    """Return the peer's exit status from the guard's status descriptor, as soon as the guard reports it."""
    data = b""
    while b"\n" not in data:
        try:
            chunk = os.read(status, 64)
        except BlockingIOError:
            await asyncio.sleep(0.05)
            continue
        if not chunk:
            raise BridgeError("The peer CLI stopped unexpectedly. Check its installation.")
        data += chunk
    token = data.split(b"\n", 1)[0]
    if token == peer_guard.START_FAILED:
        raise OSError("The peer CLI could not start.")
    try:
        return int(token)
    except ValueError as exc:
        raise BridgeError("The peer CLI stopped unexpectedly. Check its installation.") from exc


async def guard_exit_status(guard):
    """Reap the guard and return its exit status. Called only after the bridge's last signal to its group: reaping
    frees the group's ID for reuse.

    The guard is a subprocess.Popen, so nothing else reaps it: only Popen.poll(), called from this event loop's thread,
    does. (asyncio's child watcher would reap on another thread, ahead of what the bridge can see.)
    """
    while guard.poll() is None:
        await asyncio.sleep(0.05)
    return guard.returncode


async def status_closed(status, deadline):
    """Wait until the guard's status descriptor reaches its end, which happens only when the guard has exited, or
    until the deadline; True at the end. Reading it, unlike poll(), leaves the guard unreaped."""
    while time.monotonic() < deadline:
        try:
            if not os.read(status, 64):
                return True
        except BlockingIOError:
            await asyncio.sleep(0.05)
    return False


def kill_group(group):
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def stop_peer(guard, lifeline, status):
    """Close the guard's lifeline, which makes it stop the peer's whole group, wait for the guard to exit, then kill
    whatever is left of the group, and only then reap the guard.

    Until it is reaped the guard, even exited, holds its process ID, which is also its group's ID, so the last signal
    reaches only the peer's own group: whether the guard stopped the group, was killed before it could, or stalled
    past the deadline.
    """
    try:
        os.close(lifeline)
    except OSError:
        pass
    await status_closed(status, time.monotonic() + peer_guard.GRACE_SECONDS + GUARD_MARGIN_SECONDS)
    kill_group(guard.pid)
    await guard_exit_status(guard)


async def open_streams(guard):
    """Asyncio streams over the guard's pipes, which the peer inherited: two readers and a writer, or None for a
    command given no input."""
    loop = asyncio.get_running_loop()
    readers, transports = [], []
    for pipe in (guard.stdout, guard.stderr):
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(lambda reader=reader: asyncio.StreamReaderProtocol(reader), pipe)
        readers.append(reader)
        transports.append(transport)
    if guard.stdin is None:
        return readers, None, transports
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, guard.stdin)
    transports.append(transport)
    return readers, asyncio.StreamWriter(transport, protocol, None, loop), transports


async def run_command(command, prompt, working_directory, timeout, environment=None, lock=None):
    """Run a command under peer_guard and return its exit status, output and errors as bytes; prompt None gives it no
    input. The environment defaults to the peer's.

    Every process the bridge starts, peer, sign-in probe or git, runs here, so none outlives its consultation. lock, a
    conversation's locked descriptor, is held by the guard and not by the command: the conversation stays locked until
    the guard has exited, after its last signal to the group, even when the bridge's own process dies first.
    """
    lifeline_read, lifeline_write = os.pipe()
    try:
        status_read, status_write = os.pipe()
    except BaseException:
        os.close(lifeline_read)
        os.close(lifeline_write)
        raise
    os.set_blocking(status_read, False)
    try:
        guard = subprocess.Popen(
            [sys.executable, "-I", str(GUARD_SCRIPT), str(lifeline_read), str(status_write), "--", *command],
            cwd=working_directory, env=peer_environment(os.environ) if environment is None else environment,
            pass_fds=(lifeline_read, status_write, *(() if lock is None else (lock,))),
            stdin=subprocess.DEVNULL if prompt is None else subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except BaseException:
        os.close(lifeline_write)
        os.close(status_read)
        raise
    finally:
        os.close(lifeline_read)
        os.close(status_write)
    transports = []

    async def communicate():
        nonlocal transports
        (stdout, stderr), stdin, transports = await open_streams(guard)
        collected = ([], [])
        readers = [asyncio.create_task(read_output(stream, chunks))
                   for stream, chunks in zip((stdout, stderr), collected)]
        exited = asyncio.create_task(peer_exit_status(status_read))
        try:
            handed_off = True
            if stdin is not None:
                try:
                    stdin.write(prompt.encode("utf-8"))
                    await stdin.drain()
                    stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    handed_off = False
            # Wait for the peer, not for the end of its pipes. A reader that fails on oversized output ends the wait.
            pending = {exited, *readers}
            while not exited.done():
                finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            drained, _ = await asyncio.wait(readers, timeout=PIPE_DRAIN_SECONDS)
            for reader in drained:
                reader.result()
            output, errors = (b"".join(chunks) for chunks in collected)
            if not handed_off:
                raise InputClosed(output, errors)
            return exited.result(), output, errors
        finally:
            for task in (*readers, exited):
                task.cancel()
            await asyncio.gather(*readers, exited, return_exceptions=True)

    try:
        return await asyncio.wait_for(communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise CommandTimeout("Consultation timed out. Narrow the question or increase the configured timeout.") from exc
    finally:
        try:
            await run_to_completion(stop_peer(guard, lifeline_write, status_read))
        finally:
            os.close(status_read)
            for transport in transports:
                transport.close()
            for pipe in (guard.stdin, guard.stdout, guard.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()


async def registered(scope, repository, budget):
    """Whether git records the conversation's checkout as one of the repository's worktrees, read from its
    NUL-separated listing."""
    listing = await scope.git(repository, "worktree", "list", "--porcelain", "-z", failure="Cannot list the worktrees.",
                              timeout=budget.left())
    target = os.path.realpath(scope.worktree)
    return any(field.startswith("worktree ") and os.path.realpath(field[len("worktree "):]) == target
               for field in listing.split("\0"))


async def discard_worktree(scope, repository, budget):
    """Remove the conversation's pinned checkout and git's record of it, also when the folder has already gone, and
    touch no other worktree of the repository. A folder git does not record is deleted; one git records but will not
    remove, such as a locked one, is left alone."""
    worktree = scope.worktree
    try:
        await scope.git(repository, "worktree", "remove", "--force", "--", str(worktree.absolute()),
                        failure="Cannot remove the worktree.", timeout=budget.left())
    except BridgeError:
        if (worktree.is_dir() and not worktree.is_symlink()
                and not await registered(scope, repository, budget)):
            shutil.rmtree(worktree, ignore_errors=True)


async def sweep_worktrees(store):
    """Remove pinned checkouts left by runs that died, which nothing else would remove: those whose conversation is
    not running, read from its lock."""
    for identifier in await asyncio.to_thread(store.idle_worktrees):
        try:
            await sweep_worktree(store, identifier)
        except BridgeError:
            continue  # this conversation's lock cannot be taken, or git failed; the next sweep tries again


async def sweep_worktree(store, identifier):
    """Remove one idle conversation's checkout, bound to its lock, without opening or saving its record."""
    with store.holding(identifier) as scope:
        if scope is None:
            return
        worktree = scope.worktree
        removal = scope.removal_budget()
        try:
            repository = await scope.git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                         failure="Not a checkout.", timeout=removal.left())
        except BridgeError:
            # Without its .git link the folder is no checkout, only what an interrupted pin left; with one, git
            # could not answer now (a repository on an unmounted volume, say), so it is left for a later sweep.
            if not os.path.lexists(worktree / ".git"):
                shutil.rmtree(worktree, ignore_errors=True)
            return
        # The shared git directory itself, which is right whatever the repository's layout.
        await discard_worktree(scope, Path(repository), removal)


@asynccontextmanager
async def pinned_checkout(scope, directory, ref):
    """Yield the directory to consult in: the request's own, or the same subdirectory of the conversation's detached
    ref checkout.

    The checkout is registered without files first, so the filter drivers disabled are those configured where the
    files are written, which conditional includes and per-worktree configuration can make differ from the source's.
    Every git command until the checkout is yielded shares one preparation budget; its removal has its own allowance.
    """
    if ref is None:
        yield directory, None
        return
    worktree, preparation = scope.worktree, scope.preparation_budget()
    repository = Path(await scope.git(
        directory, "rev-parse", "--show-toplevel",
        failure="A ref needs a working_directory inside a git repository.", timeout=preparation.left(),
    )).resolve()
    commit = validate_commit(await scope.git(
        directory, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}",
        failure="The ref does not name a commit in this repository.", timeout=preparation.left(),
    ))
    try:
        ensure_private(worktree.parent)
    except OSError as exc:
        raise BridgeError("Cannot create the bridge's state directory.") from exc
    if worktree.is_symlink():
        raise BridgeError(PRIVATE)
    await discard_worktree(scope, repository, preparation)
    try:
        await scope.git(repository, "worktree", "add", "--detach", "--no-checkout", "--force", "--quiet", str(worktree),
                        commit, failure="Cannot check out the ref in a temporary worktree.", timeout=preparation.left())
        filters = filter_overrides(await scope.git(
            worktree, "config", "--null", "--get-regexp", r"^filter\.",
            failure="Cannot read the repository's git configuration.", allowed=(1,), timeout=preparation.left(),
        ))
        # An explicit work tree, so a configured core.worktree cannot send the files elsewhere.
        await scope.git(worktree, f"--work-tree={worktree}", "read-tree", "--reset", "-u", "--no-sparse-checkout",
                        "--no-recurse-submodules", commit, failure="Cannot check out the ref in a temporary worktree.",
                        config=filters, timeout=preparation.left())
        consulted = worktree / directory.relative_to(repository)
        # A symlink at the ref could lead the peer out of the checkout.
        if not consulted.is_dir() or not consulted.resolve().is_relative_to(worktree.resolve()):
            raise BridgeError("working_directory does not exist at that ref.")
        yield consulted, commit
    finally:
        await run_to_completion(discard_worktree(scope, repository, scope.removal_budget()))


class Bridge:
    def __init__(self, store=None, timeout=None, readers=None, jobs=None, spawn=None):
        self.store = store or SessionStore(state_directory())
        self.readers = readers or {
            "claude": ThreadReader(ClaudeSessionsSource()), "codex": ThreadReader(CodexFilesSource()),
        }
        self.timeout = consultation_timeout(os.environ, timeout)
        self.jobs = jobs or JobStore(self.store.directory)
        self.spawn = spawn or spawn_runner

    def checked_request(self, provider, prompt, working_directory, conversation_id, model=None, reasoning_effort=None):
        """Check what can be checked before a consultation starts; return the canonical project directory and the
        requested Codex model and reasoning effort."""
        if os.environ.get(ACTIVE_ENV):
            raise BridgeError("Nested peer consultations are disabled. Return your answer to the lead agent.")
        check_prompt(prompt)
        directory = self.project_directory(provider, working_directory)
        if conversation_id:
            validate_session_id(conversation_id)
        model, reasoning_effort = check_codex_choices(provider, model, reasoning_effort)
        if provider == "codex":
            # A model or effort Codex does not list would be ignored or refused only once Codex runs; refuse it now.
            check_against_catalogue(codex_catalogue(), *codex_choices(os.environ, model, reasoning_effort))
        return directory, model, reasoning_effort

    async def start(self, provider, prompt, working_directory, context="", conversation_id=None, ref=None,
                    seed_thread=None, model=None, reasoning_effort=None):
        """Start a consultation in a detached runner and return its job at once."""
        directory, model, reasoning_effort = self.checked_request(provider, prompt, working_directory, conversation_id,
                                                                  model, reasoning_effort)
        if seed_thread:
            self.check_threads_enabled()  # refused now, not once the runner reads the thread
        request = job_request(provider, prompt, directory, context, conversation_id, ref, seed_thread, model,
                              reasoning_effort)
        return await start_job(self.jobs, request, self.spawn)

    async def job(self, job_id, providers, wait_seconds=0):
        """Return a job's state and, once finished, its result; wait up to wait_seconds for it to finish."""
        deadline = time.monotonic() + check_wait(wait_seconds)
        while True:
            with job_store_errors():
                view = await asyncio.to_thread(self.jobs.view, job_id, providers)
            if view["state"] in FINISHED_JOB_STATES or time.monotonic() >= deadline:
                return view
            await asyncio.sleep(min(JOB_POLL_SECONDS, max(0, deadline - time.monotonic())))

    async def cancel(self, job_id, providers):
        with job_store_errors():
            return await asyncio.to_thread(self.jobs.cancel, job_id, providers)

    @staticmethod
    def project_directory(provider, working_directory):
        check_provider(provider)
        directory = Path(working_directory).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise BridgeError("working_directory must be an absolute path to an existing directory.")
        return directory.resolve()

    @staticmethod
    def check_threads_enabled():
        if not threads_enabled(os.environ):
            raise BridgeError(THREADS_OFF)

    async def threads(self, provider, working_directory, limit=20):
        """List the provider's recent threads that belong to the working directory's git repository."""
        self.check_threads_enabled()
        directory = self.project_directory(provider, working_directory)
        return await self.read_history(self.readers[provider].list, directory, check_limit(limit))

    async def thread(self, provider, working_directory, thread, max_messages=3):
        """Return one thread's latest agent messages and whether its latest turn has finished."""
        self.check_threads_enabled()
        directory = self.project_directory(provider, working_directory)
        thread = check_thread_request(thread, max_messages)
        return await self.read_history(self.readers[provider].read, directory, thread, max_messages)

    @staticmethod
    async def read_history(call, *arguments):
        try:
            return await asyncio.to_thread(call, *arguments)
        except ThreadUnavailable as exc:
            raise BridgeError(str(exc)) from exc
        except OSError as exc:
            raise BridgeError("Cannot read the thread history files.") from exc
        except (TypeError, AttributeError) as exc:
            # A record whose values have unexpected types, such as a number where text belongs.
            raise BridgeError("The thread's history file has a malformed line.") from exc

    async def consult(self, provider, prompt, working_directory, context="", conversation_id=None, ref=None,
                      seed_thread=None, model=None, reasoning_effort=None):
        directory, model, reasoning_effort = self.checked_request(provider, prompt, working_directory, conversation_id,
                                                                  model, reasoning_effort)
        if provider == "codex":
            model, reasoning_effort = codex_choices(os.environ, model, reasoning_effort)
        if seed_thread:
            seed = await self.thread(provider, directory, seed_thread, SEED_MESSAGES)
            context = with_seed(context, seed_context(seed))
        # A state directory that is not private stops the consultation before any cleanup inside it.
        check_private(self.store.directory)
        try:
            await sweep_worktrees(self.store)
        except OSError:
            pass  # best effort: a leftover checkout costs only disk space
        with self.store.open(provider, directory, conversation_id) as conversation:
            ref = consulted_ref(conversation.record, ref)
            async with pinned_checkout(conversation, directory, ref) as (consulted, commit):
                command = command_for(provider, consulted, conversation.record.get("backend_session_id"),
                                      conversation.identifier, model, reasoning_effort, pinned=commit is not None)
                depth = len(consulted.relative_to(conversation.worktree).parts) if commit else 0
                handoff = build_handoff(provider, prompt, context, commit, depth)
                failure = None
                # The failure probes share what is left of it.
                peer = conversation.budget(self.timeout)
                try:
                    return_code, output, errors = await conversation.run(
                        command, handoff, consulted, self.timeout,
                        environment=peer_environment(os.environ, pinned=commit is not None))
                except OSError as exc:
                    raise BridgeError(f"Cannot start {provider}. Check its executable and working directory.") from exc
                except InputClosed as exc:
                    # An incomplete handoff is never a success, whatever the exit status.
                    output, errors, failure = exc.output, exc.errors, PeerFailure(CLOSED_INPUT)
            output, errors = (text.decode("utf-8", errors="replace") for text in (output, errors))
            try:
                if failure:
                    raise failure
                backend_session_id, reply = consultation_outcome(provider, return_code, output)
            except PeerFailure as exc:
                facts = await self.failure_facts(provider, directory, conversation.record, output, errors,
                                                 scope=conversation, peer=peer,
                                                 chose_model=bool(model or reasoning_effort))
                raise BridgeError(failure_message(provider, facts, str(exc))) from exc
            conversation.save(backend_session_id, commit)
            return consultation_result(provider, conversation.identifier, reply, commit, model, reasoning_effort)

    async def failure_facts(self, provider, directory, record, output, errors, *, scope, peer, chose_model=False):
        """Establish what may explain a failed run, from exact fields and checks, never from the CLI's text."""
        session = record.get("backend_session_id")
        source = self.readers[provider].source

        def check(question, *arguments):
            try:
                return question(*arguments)
            except OSError:
                return None

        facts = {"resuming": bool(session), "signed_in": await signed_in(provider, directory, scope, peer),
                 **output_signals(provider, output, errors)}
        if session:
            facts["history_present"] = await asyncio.to_thread(check, source.has_history, session)
        if provider == "codex":
            if session:
                facts["writer_lock_present"] = check(source.writer_lock_present, session)
            facts["in_git_repository"] = await asyncio.to_thread(
                git_entry_found, Path(os.path.realpath(directory)), os.lstat)
            facts["model_setting"] = chose_model
        return FailureFacts(**facts)


# Shown to the agent by the host app. Codex was seen typing into the Claude desktop app's composer to reach a Claude
# session; that bypasses every restriction the bridge applies, so the instructions say what to do instead.
CLAUDE_PEER_INSTRUCTIONS = (
    "Reach Claude Code only through these tools: ask_claude, or start_claude_consultation for work that takes "
    "minutes. Never reach a Claude session by operating the Claude desktop app (computer use, accessibility, or "
    "typing into its composer): that bypasses the read-only restrictions the bridge applies. The bridge cannot write "
    "into an existing Claude session. To hand work to one, give the handoff as your reply, and tell the user that "
    "session can read this thread with read_codex_thread, or start from it with seed_thread."
)
CODEX_PEER_INSTRUCTIONS = (
    "Consult Codex with ask_codex, or start_codex_consultation for work that takes minutes. Choose the model and "
    "reasoning_effort per call when the task needs a particular one, such as gpt-6-astra at xhigh for a review; "
    "list_codex_models gives the values Codex offers. Otherwise leave them unset. Read a Codex Desktop thread with "
    "read_codex_thread; never operate the Codex app."
)


def models_result():
    models = codex_catalogue()
    if not models:
        raise BridgeError("Codex has no readable model catalogue yet. Start Codex once, then try again.")
    return {"provider": "codex", "models": models}


def server_instructions(peer):
    parts = {"claude": [CLAUDE_PEER_INSTRUCTIONS], "codex": [CODEX_PEER_INSTRUCTIONS],
             "both": [CLAUDE_PEER_INSTRUCTIONS, CODEX_PEER_INSTRUCTIONS]}[peer]
    return " ".join(parts + ["Replies are advice: check them against the task before acting."])


def create_server(peer):
    from typing import Any

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("peer-bridge", instructions=server_instructions(peer), log_level="WARNING")
    bridge = Bridge()
    consulting = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    reading = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    providers = {"claude", "codex"} if peer == "both" else {peer}
    starting = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
    stopping = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(annotations=reading)
    async def get_consultation(job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        """Return a background consultation's state (queued, running, cancelling, completed, failed, cancelled or
        lost) and, once completed, its conversation_id and reply; wait_seconds, at most 60, waits for it to finish."""
        return await bridge.job(job_id, providers, wait_seconds)

    @server.tool(annotations=stopping)
    async def cancel_consultation(job_id: str) -> dict[str, Any]:
        """Stop a background consultation; it stops the peer and removes a pinned checkout."""
        return await bridge.cancel(job_id, providers)

    if peer in {"claude", "both"}:
        @server.tool(annotations=starting)
        async def start_claude_consultation(
            prompt: str, working_directory: str, context: str = "", conversation_id: str | None = None,
            ref: str | None = None, seed_thread: str | None = None,
        ) -> dict[str, Any]:
            """Start an ask_claude consultation in the background and return its job_id at once; read it with
            get_consultation. Takes the same arguments as ask_claude; suits reviews that take minutes. Use this, never
            the Claude desktop app, to reach Claude."""
            return await bridge.start("claude", prompt, working_directory, context, conversation_id, ref,
                                      seed_thread)

        @server.tool(annotations=consulting)
        async def ask_claude(
            prompt: str, working_directory: str, context: str = "", conversation_id: str | None = None,
            ref: str | None = None, seed_thread: str | None = None,
        ) -> dict[str, str]:
            """Ask Claude Code a scoped question; reuse conversation_id for a follow-up; pin a commit with ref;
            seed_thread names a Claude Code session whose recent agent messages become context. Use this, never the
            Claude desktop app, to reach Claude; it starts a new consultation and cannot write into an existing
            session."""
            return await bridge.consult("claude", prompt, working_directory, context, conversation_id, ref,
                                        seed_thread)

        @server.tool(annotations=reading)
        async def list_claude_threads(working_directory: str, limit: int = 20) -> dict[str, Any]:
            """Experimental: reads the other app's internal history files, which may change. List recent Claude Code
            sessions in the same git repository as working_directory, newest first."""
            return await bridge.threads("claude", working_directory, limit)

        @server.tool(annotations=reading)
        async def read_claude_thread(working_directory: str, thread: str, max_messages: int = 3) -> dict[str, Any]:
            """Experimental: reads the other app's internal history files, which may change. Read a Claude Code
            session's latest agent messages, by ID or name, and whether its turn finished."""
            return await bridge.thread("claude", working_directory, thread, max_messages)

    if peer in {"codex", "both"}:
        @server.tool(annotations=starting)
        async def start_codex_consultation(
            prompt: str, working_directory: str, context: str = "", conversation_id: str | None = None,
            ref: str | None = None, seed_thread: str | None = None, model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> dict[str, Any]:
            """Start an ask_codex consultation in the background and return its job_id at once; read it with
            get_consultation. Takes the same arguments as ask_codex; suits reviews that take minutes."""
            return await bridge.start("codex", prompt, working_directory, context, conversation_id, ref,
                                      seed_thread, model, reasoning_effort)

        @server.tool(annotations=consulting)
        async def ask_codex(
            prompt: str, working_directory: str, context: str = "", conversation_id: str | None = None,
            ref: str | None = None, seed_thread: str | None = None, model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> dict[str, str]:
            """Ask Codex a scoped question; reuse conversation_id for a follow-up; pin a commit with ref;
            seed_thread names a Codex thread whose recent agent messages become context. model (such as
            gpt-6-astra) and reasoning_effort (such as low, medium, high or xhigh) choose what Codex runs for this
            call, over PEER_BRIDGE_CODEX_MODEL and PEER_BRIDGE_CODEX_REASONING_EFFORT. list_codex_models gives the
            values Codex offers, and a value it does not list is refused before Codex starts. The reply names the
            model and effort when one was chosen."""
            return await bridge.consult("codex", prompt, working_directory, context, conversation_id, ref,
                                        seed_thread, model, reasoning_effort)

        @server.tool(annotations=reading)
        async def list_codex_models() -> dict[str, Any]:
            """Experimental: list the models this Codex account offers, from Codex's own catalogue, with each one's
            reasoning efforts and default: the values ask_codex's model and reasoning_effort accept."""
            return await asyncio.to_thread(models_result)

        @server.tool(annotations=reading)
        async def list_codex_threads(working_directory: str, limit: int = 20) -> dict[str, Any]:
            """Experimental: reads the other app's internal history files, which may change. List recent Codex threads
            in the same git repository as working_directory, newest first."""
            return await bridge.threads("codex", working_directory, limit)

        @server.tool(annotations=reading)
        async def read_codex_thread(working_directory: str, thread: str, max_messages: int = 3) -> dict[str, Any]:
            """Experimental: reads the other app's internal history files, which may change. Read a Codex thread's
            latest agent messages, by ID or name, and whether its turn finished."""
            return await bridge.thread("codex", working_directory, thread, max_messages)

    return server


async def until_terminated(operation):
    # Convert termination signals to cancellation so the operation can clean up before the CLI exits.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for signal_number in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signal_number, task.cancel)
    return await operation()


def run_command_line(arguments):
    """Run one CLI command and print its JSON result, or {"error": ...} with exit status 1."""
    try:
        if arguments.command in {"ask", "start"}:
            context = ""
            if arguments.context_file:
                try:
                    context = Path(arguments.context_file).read_text(encoding="utf-8")
                except (OSError, ValueError) as exc:
                    raise BridgeError("Cannot read the context file as UTF-8 text.") from exc
            try:
                sys.stdin.reconfigure(encoding="utf-8", errors="strict")
                prompt = sys.stdin.read()
            except (OSError, ValueError) as exc:
                raise BridgeError("Cannot read the prompt from standard input as UTF-8 text.") from exc
            request = (arguments.provider, prompt, arguments.working_directory, context, arguments.conversation_id,
                       arguments.ref, arguments.seed_thread, arguments.model, arguments.reasoning_effort)
            if arguments.command == "start":
                result = asyncio.run(until_terminated(lambda: Bridge().start(*request)))
            else:
                result = asyncio.run(until_terminated(lambda: Bridge().consult(*request)))
        elif arguments.command == "run-job":
            store = JobStore(state_directory())

            async def consult(request):
                return await Bridge().consult(**request)

            with job_store_errors():
                asyncio.run(until_terminated(lambda: run_job(store, arguments.job_id, consult)))
            result = {"job_id": arguments.job_id}
        elif arguments.command == "codex-models":
            result = models_result()
        elif arguments.command == "job":
            result = asyncio.run(Bridge().job(arguments.job_id, {"claude", "codex"}, arguments.wait))
        elif arguments.command == "cancel":
            result = asyncio.run(Bridge().cancel(arguments.job_id, {"claude", "codex"}))
        elif arguments.command == "threads":
            result = asyncio.run(Bridge().threads(arguments.provider, arguments.working_directory, arguments.limit))
        else:
            result = asyncio.run(Bridge().thread(
                arguments.provider, arguments.working_directory, arguments.thread, arguments.max_messages,
            ))
    except BridgeError as exc:
        result = {"error": str(exc)}
    except (asyncio.CancelledError, KeyboardInterrupt):
        result = {"error": consultation.CANCELLED_JOB}
    print(json.dumps(result, ensure_ascii=False))
    return 1 if "error" in result else 0


def main():
    # An inherited SIG_IGN would let the kernel reap an exited guard at once, freeing its group's ID before the
    # bridge's last signal to that group.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", choices=("claude", "codex", "both"), default="both",
                        help="peer tools the MCP server exposes when no command is given")
    commands = parser.add_subparsers(dest="command", metavar="command")
    ask_parser = commands.add_parser("ask", help="run one consultation; the prompt is read from stdin")
    start_parser = commands.add_parser("start", help="start a consultation in the background and print its job")
    job_parser = commands.add_parser("job", help="print a background consultation's state and result")
    cancel_parser = commands.add_parser("cancel", help="stop a background consultation")
    runner_parser = commands.add_parser("run-job", help=argparse.SUPPRESS)
    commands.add_parser("codex-models", help="list Codex's models with their reasoning efforts")
    threads_parser = commands.add_parser("threads", help="list recent threads in the same git repository")
    read_parser = commands.add_parser("read-thread", help="read a thread's latest agent messages")
    for command in (ask_parser, start_parser, threads_parser, read_parser):
        command.add_argument("--provider", choices=("claude", "codex"), required=True)
        command.add_argument("--working-directory", required=True)
    for command in (ask_parser, start_parser):
        command.add_argument("--context-file", help="UTF-8 file with supporting context for the peer")
        command.add_argument("--conversation-id", help="bridge conversation ID from an earlier consultation")
        command.add_argument("--ref", help="consult a detached checkout of this commit instead of the working tree")
        command.add_argument("--seed-thread", help="thread ID or name whose recent agent messages become context")
        command.add_argument("--model", help="Codex only: the model to run, over PEER_BRIDGE_CODEX_MODEL")
        command.add_argument("--reasoning-effort",
                             help="Codex only: the reasoning effort, over PEER_BRIDGE_CODEX_REASONING_EFFORT")
    for command in (job_parser, cancel_parser, runner_parser):
        command.add_argument("--job-id", required=True)
    job_parser.add_argument("--wait", type=float, default=0, help="seconds to wait for the job to finish, at most 60")
    threads_parser.add_argument("--limit", type=int, default=20)
    read_parser.add_argument("--thread", required=True, help="thread ID or exact name")
    read_parser.add_argument("--max-messages", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.command:
        sys.exit(run_command_line(arguments))
    create_server(arguments.peer).run(transport="stdio")


if __name__ == "__main__":
    main()
