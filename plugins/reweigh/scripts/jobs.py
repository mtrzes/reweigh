"""Background consultations: a job store under the state directory and a detached runner per job.

Two locks per job keep its files consistent across runners, servers and crashes:

- runner.lock is held by the job's runner, and only by it, for as long as the runner lives. The kernel releases it
  when the runner dies, so whether a runner is present is read from it, never from a process ID.
- state.lock is a short mutex taken for every read-decide-write of the job's files. Whoever probes runner.lock does
  so while holding state.lock, so two probes never mistake each other for a runner.

Cancelling never signals a process: it leaves a marker that the runner checks before it consults and watches for
while it does. A peer never outlives its runner, because each peer runs under peer_guard.py. A job's outcome, once
written, is final; a lost runner's verdict is written too, so a runner that starts late honours it. The request,
which holds the prompt and context, is deleted when the runner claims it, and before any other final outcome.

Each operation's lock wait is set here, not by its caller. A start that is cancelled before its first view returns
cancels the job it published, since its caller never learns the ID. That cancel waits for the state lock no longer
than any other: if another process holds the lock for longer, the job runs to its end or its timeout, and nothing
reports its ID.
"""

import asyncio
import fcntl
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from consultation import (
    CANCELLED_JOB, JOB_RETENTION_SECONDS, PROVIDERS, BridgeError, job_expired, job_outcome, job_state, job_view,
    new_job_record, stopped_outcome, valid_job_outcome, valid_job_record, validate_session_id,
)
from state_files import check_private, ensure_private, open_regular, read_json, write_json

UNKNOWN_JOB = "No consultation job with that ID."
UNREADABLE = "The consultation job's files are unreadable."
LOCK_BUSY = "The consultation job is busy. Try again."
UNEXPECTED = "The consultation stopped on an unexpected error."
STORE_FAILED = "Cannot read or write the consultation job files."
RUNNER_FAILED = "Cannot start the consultation runner."
START_UNCONFIRMED = (
    "Cannot start the runner of consultation job {job_id}, and its cancellation could not be confirmed. Cancel it "
    "with that job ID."
)
CANCEL_POLL_SECONDS = 0.2
LOCK_POLL_SECONDS = 0.05
STATE_LOCK_TIMEOUT_SECONDS = 1.0
# A job folder is built under STAGING_PREFIX and renamed into place whole; an expired one is renamed under
# DELETING_PREFIX before it is removed, so an interrupted removal is finished later, whatever is left in it.
STAGING_PREFIX = ".staging-"
DELETING_PREFIX = ".deleting-"
STAGING_RETENTION_SECONDS = 3600
BRIDGE_SCRIPT = Path(__file__).resolve().with_name("bridge.py")


# The request a job's runner consults with, as start writes it to request.json.
REQUEST_TEXT_FIELDS = ("prompt", "working_directory", "context")
REQUEST_OPTIONAL_FIELDS = ("conversation_id", "ref", "seed_thread", "model", "reasoning_effort")
REQUEST_LIST_FIELDS = ("extra_directories",)


def job_request(provider, prompt, working_directory, context="", conversation_id=None, ref=None, seed_thread=None,
                model=None, reasoning_effort=None, extra_directories=()):
    """The request to persist for a checked consultation; empty optional values are stored as null."""
    request = {"provider": provider, "prompt": prompt, "working_directory": str(working_directory), "context": context}
    values = (conversation_id, ref, seed_thread, model, reasoning_effort)
    request |= {key: value or None for key, value in zip(REQUEST_OPTIONAL_FIELDS, values)}
    return request | {"extra_directories": [str(directory) for directory in extra_directories] or None}


def read_job_request(saved):
    """The consultation's keyword arguments from a persisted request, or None when it is not one this bridge writes."""
    if not isinstance(saved, dict) or saved.get("provider") not in PROVIDERS:
        return None
    if (any(not isinstance(saved.get(key), str) for key in REQUEST_TEXT_FIELDS)
            or any(key not in saved or not isinstance(saved[key], (str, type(None)))
                   for key in REQUEST_OPTIONAL_FIELDS)
            or any(key not in saved or saved[key] is not None and (
                       not isinstance(saved[key], list) or not all(isinstance(item, str) for item in saved[key]))
                   for key in REQUEST_LIST_FIELDS)):
        return None
    return {key: saved[key] for key in ("provider", *REQUEST_TEXT_FIELDS, *REQUEST_OPTIONAL_FIELDS,
                                        *REQUEST_LIST_FIELDS)}


@contextmanager
def job_store_errors():
    """Report filesystem failures in the job store as fixed messages; a job that vanished meanwhile is unknown."""
    try:
        yield
    except FileNotFoundError as exc:
        raise BridgeError(UNKNOWN_JOB) from exc
    except OSError as exc:
        raise BridgeError(STORE_FAILED) from exc


def cancel_requested(folder):
    try:
        return stat.S_ISREG((folder / "cancel.requested").lstat().st_mode)
    except FileNotFoundError:
        return False


class JobStore:
    def __init__(self, state_directory):
        self.directory = Path(state_directory).absolute() / "jobs"

    # Files

    def ensure(self):
        ensure_private(self.directory.parent, self.directory)

    def folder(self, job_id):
        """The job's folder, refusing anything but a real directory directly inside a real jobs directory.

        A process running as the same user could still swap a checked directory for a symlink before it is used;
        such a process can read and rewrite these files directly anyway, so that is not a boundary this guards.
        """
        try:
            job_id = validate_session_id(job_id)
        except BridgeError as exc:
            raise BridgeError(UNKNOWN_JOB) from exc
        if not check_private(self.directory.parent, self.directory):
            raise BridgeError(UNKNOWN_JOB)
        folder = self.directory / job_id
        if folder.is_symlink() or not folder.is_dir():
            raise BridgeError(UNKNOWN_JOB)
        return folder

    @staticmethod
    def open_regular(path, flags):
        return open_regular(path, flags, unreadable=UNREADABLE)

    @staticmethod
    def read(path):
        return read_json(path, unreadable=UNREADABLE)

    @classmethod
    def mark(cls, path):
        try:
            os.close(cls.open_regular(path, os.O_CREAT | os.O_WRONLY))
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise BridgeError(UNREADABLE) from exc

    @contextmanager
    def flock(self, path):
        """Hold an exclusive lock on path, or yield False at once if another process holds it."""
        try:
            descriptor = self.open_regular(path, os.O_CREAT | os.O_RDWR)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise BridgeError(UNREADABLE) from exc
        with os.fdopen(descriptor, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def state_lock(self, folder):
        """Hold the job's short state lock, or report the job busy after a bounded wait."""
        deadline = time.monotonic() + STATE_LOCK_TIMEOUT_SECONDS
        while True:
            with self.flock(folder / "state.lock") as acquired:
                if acquired:
                    yield
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(LOCK_POLL_SECONDS, remaining))
            if time.monotonic() >= deadline:
                break
        raise BridgeError(LOCK_BUSY)

    @asynccontextmanager
    async def runner_file_lock(self, path, *, retry=True):
        """Retry on the event loop so the runner can be cancelled while waiting."""
        while True:
            with self.flock(path) as acquired:
                if acquired or not retry:
                    yield acquired
                    return
            await asyncio.sleep(LOCK_POLL_SECONDS)

    def runner_present(self, folder):
        """Whether a runner holds the job. Call only while holding the job's state lock."""
        with self.flock(folder / "runner.lock") as acquired:
            return not acquired

    def load(self, folder, providers):
        """The job's record and outcome, for a job of one of the providers this caller may see."""
        record = self.read(folder / "job.json")
        if not valid_job_record(record) or record["provider"] not in providers:
            raise BridgeError(UNKNOWN_JOB)
        outcome = self.read(folder / "outcome.json")
        if outcome is not None and not valid_job_outcome(outcome):
            raise BridgeError(UNREADABLE)
        return record, outcome

    def settle(self, folder, record, outcome, *, cancelling=False, runner_present=None):
        """Finish a job that has stopped and return its view. Hold the state lock.

        A lost or cancelled verdict is stored, so a runner that starts later finds it and does not run.
        """
        if outcome is not None:
            # A crash may have left the request behind a final outcome; it is not kept.
            (folder / "request.json").unlink(missing_ok=True)
            return job_view(folder.name, record, outcome, outcome["state"])
        now = time.time()
        if runner_present is None:
            runner_present = self.runner_present(folder)
        state = job_state(record, None, runner_present, now, cancel_requested=cancelling or cancel_requested(folder))
        if cancelling and state != "lost":
            # The runner checks this marker before it consults and watches for it while it does.
            self.mark(folder / "cancel.requested")
        outcome = stopped_outcome(state, now)
        if outcome is not None:
            self.finish(folder, outcome)
        return job_view(folder.name, record, outcome, state)

    def finish(self, folder, outcome):
        """Write a final outcome, after deleting the request, unless one is already there. Hold the state lock."""
        (folder / "request.json").unlink(missing_ok=True)
        if self.read(folder / "outcome.json") is None:
            write_json(folder / "outcome.json", outcome)

    # Operations

    def create(self, provider, working_directory, request):
        self.ensure()
        job_id = str(uuid.uuid4())
        staging = self.directory / f"{STAGING_PREFIX}{job_id}"
        staging.mkdir(mode=0o700)
        try:
            write_json(staging / "request.json", request)
            # Stamped at publication, so time spent before it does not count against the start-up grace.
            write_json(staging / "job.json", new_job_record(provider, working_directory, time.time()))
            # The job appears with both files or not at all.
            os.rename(staging, self.directory / job_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return job_id

    def view(self, job_id, providers):
        folder = self.folder(job_id)
        with self.state_lock(folder):
            return self.settle(folder, *self.load(folder, providers))

    def cancel(self, job_id, providers):
        """Stop a running job, or mark one cancelled that no runner holds."""
        folder = self.folder(job_id)
        with self.state_lock(folder):
            return self.settle(folder, *self.load(folder, providers), cancelling=True)

    def prune(self, now):
        """Remove jobs no runner holds that finished, or were created, longer ago than the retention period, and
        folders a crash left in staging or mid-removal. One unreadable job never stops the others' removal."""
        if not check_private(self.directory.parent, self.directory):
            return
        for entry in list(self.directory.iterdir()):
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                prefix = next((prefix for prefix in (STAGING_PREFIX, DELETING_PREFIX) if entry.name.startswith(prefix)),
                              "")
                job_id = entry.name.removeprefix(prefix)
                if validate_session_id(job_id) != job_id:
                    continue
                if prefix == DELETING_PREFIX:
                    shutil.rmtree(entry, ignore_errors=True)
                    continue
                if prefix == STAGING_PREFIX:
                    # A concurrent start may publish this folder at any moment; a vanished one is skipped.
                    if now - entry.stat().st_mtime > STAGING_RETENTION_SECONDS:
                        shutil.rmtree(entry, ignore_errors=True)
                    continue
                tombstone = None
                # Without waiting: a process stopped while holding one job's lock must not stall every start.
                with self.flock(entry / "state.lock") as held:
                    if not held or self.runner_present(entry):
                        continue
                    if self.expired(entry, now):
                        tombstone = self.directory / f"{DELETING_PREFIX}{job_id}"
                        os.rename(entry, tombstone)
                if tombstone is not None:
                    shutil.rmtree(tombstone, ignore_errors=True)
            except (BridgeError, OSError):
                continue

    def expired(self, folder, now):
        try:
            record, outcome = self.read(folder / "job.json"), self.read(folder / "outcome.json")
        except BridgeError:
            record = outcome = None
        if valid_job_record(record) and (outcome is None or valid_job_outcome(outcome)):
            return job_expired(record, outcome, now)
        # A job whose files this bridge cannot read is kept no longer than any other.
        return now - folder.stat().st_mtime > JOB_RETENTION_SECONDS

    # Runner side

    async def claim(self, folder):
        """Take the job's request for this runner, as the consultation's keyword arguments, or None when the job must
        not run. Hold the runner lock."""
        async with self.runner_file_lock(folder / "state.lock"):
            record = self.read(folder / "job.json")
            if record is None:
                return None  # removed while this runner waited
            if self.read(folder / "outcome.json") is not None:
                # Its record may be one this bridge cannot read, so no view is built from it.
                (folder / "request.json").unlink(missing_ok=True)
                return None  # cancelled, or judged lost, before the runner started
            if not valid_job_record(record):
                self.finish(folder, job_outcome("failed", time.time(), error=UNREADABLE))
                return None
            # This runner holds runner.lock, but has yet to claim the job: it does not count as the job's runner.
            if self.settle(folder, record, None, runner_present=False)["state"] != "queued":
                return None
            try:
                request = read_job_request(self.read(folder / "request.json"))
            except BridgeError:
                request = None
            if request is None:
                self.finish(folder, job_outcome("failed", time.time(), error=UNREADABLE))
                return None
            (folder / "request.json").unlink(missing_ok=True)
            # The PID is recorded for diagnosis only; nothing signals it.
            write_json(folder / "job.json", {**record, "state": "running", "pid": os.getpid(),
                                             "started_at": time.time()})
            return request

    async def record_outcome(self, folder, outcome, *, retry=True):
        async with self.runner_file_lock(folder / "state.lock", retry=retry) as acquired:
            if acquired:
                self.finish(folder, outcome)


async def start_job(store, request, spawn):
    """Publish a job for a job_request(), launch its runner with spawn(job_id), and return its first view, the only way
    its caller learns the ID; a view that cannot be read, for a busy lock or any other reason, is returned as `queued`.
    Nothing is awaited from publication until the runner is launched. A cancellation requested meanwhile arrives at the
    read of that view, and one that arrives there cancels the job, unless the state lock stays busy for the cancel's
    whole wait: the job then runs to its end or its timeout, and nothing reports its ID."""
    provider = request["provider"]
    with job_store_errors():
        await asyncio.to_thread(store.prune, time.time())
        job_id = store.create(provider, request["working_directory"], request)
        try:
            spawn(job_id)
        except Exception as exc:
            # The runner may have started anyway, such as when only its reaper thread failed: the job is cancelled, or
            # the error names its ID, the only way left to it.
            try:
                store.cancel(job_id, {provider})
            except (BridgeError, OSError):
                raise BridgeError(START_UNCONFIRMED.format(job_id=job_id)) from exc
            raise BridgeError(RUNNER_FAILED) from exc
        try:
            return await asyncio.to_thread(store.view, job_id, {provider})
        except asyncio.CancelledError:
            # Nobody will learn the ID. A cancel that fails does not turn the interruption into an ordinary failure:
            # nobody is left to read either. It fails when another process holds the state lock for longer than the
            # one-second wait, and the runner already launched then keeps the job.
            with suppress(BridgeError, OSError):
                store.cancel(job_id, {provider})
            raise
        except (BridgeError, OSError):
            # Busy, as when the runner holds the state lock to claim the job, or unreadable: the caller still needs
            # the ID, and a later read reports what is wrong.
            return job_view(job_id, {"provider": provider}, None, "queued")


def spawn_runner(job_id):
    """Start the job's runner in its own session, detached from the caller, so it outlives an MCP server restart."""
    process = subprocess.Popen(
        [sys.executable, str(BRIDGE_SCRIPT), "run-job", "--job-id", job_id],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    # Reap the runner when it exits, so it never lingers as a zombie of a long-lived server.
    threading.Thread(target=process.wait, daemon=True).start()


async def watch_for_cancel(folder, task):
    """Cancel the consultation task once the job's cancel marker appears."""
    while not cancel_requested(folder):
        await asyncio.sleep(CANCEL_POLL_SECONDS)
    task.cancel()


async def run_job(store, job_id, consult):
    """Run one job to its outcome while holding its runner lock. consult(request) performs the consultation.

    The caller installs the SIGTERM handler before this starts, so a signal also arrives as CancelledError.
    """
    try:
        folder = store.folder(job_id)
        async with store.runner_file_lock(folder / "runner.lock"):
            request = await store.claim(folder)
            if request is not None:
                await consult_and_record(store, folder, request, consult)
    except (BridgeError, OSError):
        # The job is gone, its files cannot be trusted, or they cannot be written; a reader then finds it lost.
        return


async def consult_and_record(store, folder, request, consult):
    consulting = asyncio.ensure_future(consult(request))
    watcher = asyncio.ensure_future(watch_for_cancel(folder, consulting))
    try:
        result = await consulting
        # Stamped once the reply is in, so the retention period counts from completion.
        outcome = job_outcome("completed", time.time(), result=result)
    except BridgeError as exc:
        outcome = job_outcome("failed", time.time(), error=str(exc))
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Cancelling consult() has already stopped the peer's guard and removed a pinned checkout.
        marked = (not asyncio.current_task().cancelling()
                  and watcher.done() and not watcher.cancelled() and watcher.exception() is None)
        # A signalled runner must exit even if another process holds the state lock.
        await store.record_outcome(folder, job_outcome("cancelled", time.time(), error=CANCELLED_JOB), retry=marked)
        if marked:
            return  # cancelled through the marker: the job ends normally
        raise
    except Exception:
        outcome = job_outcome("failed", time.time(), error=UNEXPECTED)
    finally:
        watcher.cancel()
    await store.record_outcome(folder, outcome)
