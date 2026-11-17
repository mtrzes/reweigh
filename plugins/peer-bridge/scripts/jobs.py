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
from contextlib import contextmanager
from pathlib import Path

from consultation import (
    JOB_RETENTION_SECONDS, LOST_JOB, BridgeError, job_expired, job_outcome, job_state, job_view, new_job_record,
    valid_job_outcome, valid_job_record, validate_session_id,
)
from state_files import PRIVATE, check_private, ensure_private, open_regular, read_json, write_json

UNKNOWN_JOB = "No consultation job with that ID."
UNREADABLE = "The consultation job's files are unreadable."
CANCELLED = "The consultation was cancelled."
UNEXPECTED = "The consultation stopped on an unexpected error."
STOPPED_JOB_ERRORS = {"lost": LOST_JOB, "cancelled": CANCELLED}
CANCEL_POLL_SECONDS = 0.2
# A job folder is built under STAGING_PREFIX and renamed into place whole; an expired one is renamed under
# DELETING_PREFIX before it is removed, so an interrupted removal is finished later, whatever is left in it.
STAGING_PREFIX = ".staging-"
DELETING_PREFIX = ".deleting-"
STAGING_RETENTION_SECONDS = 3600
BRIDGE_SCRIPT = Path(__file__).resolve().with_name("bridge.py")


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
    def open_regular(path, flags, mode=0o600):
        return open_regular(path, flags, mode, unreadable=UNREADABLE)

    @staticmethod
    def read(path):
        return read_json(path, unreadable=UNREADABLE)

    @staticmethod
    def write(path, data):
        write_json(path, data)

    @classmethod
    def mark(cls, path):
        try:
            os.close(cls.open_regular(path, os.O_CREAT | os.O_WRONLY))
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise BridgeError(UNREADABLE) from exc

    @contextmanager
    def flock(self, path, blocking):
        """Hold an exclusive lock on path; with blocking False, yield False at once if another process holds it."""
        try:
            descriptor = self.open_regular(path, os.O_CREAT | os.O_RDWR)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise BridgeError(UNREADABLE) from exc
        with os.fdopen(descriptor, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def state_lock(self, folder):
        return self.flock(folder / "state.lock", blocking=True)

    def runner_lock(self, folder):
        return self.flock(folder / "runner.lock", blocking=True)

    def runner_present(self, folder):
        """Whether a runner holds the job. Call only while holding the job's state lock."""
        with self.flock(folder / "runner.lock", blocking=False) as acquired:
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

    def finish(self, folder, outcome):
        """Write a final outcome, after deleting the request, unless one is already there. Hold the state lock."""
        (folder / "request.json").unlink(missing_ok=True)
        if self.read(folder / "outcome.json") is None:
            self.write(folder / "outcome.json", outcome)

    # Operations

    def create(self, provider, working_directory, request):
        self.ensure()
        job_id = str(uuid.uuid4())
        staging = self.directory / f"{STAGING_PREFIX}{job_id}"
        staging.mkdir(mode=0o700)
        try:
            self.write(staging / "request.json", request)
            # Stamped at publication, so time spent before it does not count against the start-up grace.
            self.write(staging / "job.json", new_job_record(provider, working_directory, time.time()))
            # The job appears with both files or not at all.
            os.rename(staging, self.directory / job_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return job_id

    def view(self, job_id, providers, now=None):
        folder = self.folder(job_id)
        with self.state_lock(folder):
            record, outcome = self.load(folder, providers)
            if outcome is None:
                now = time.time() if now is None else now
                state = job_state(record, None, self.runner_present(folder), now,
                                  cancel_requested=cancel_requested(folder))
                if state not in STOPPED_JOB_ERRORS:
                    return job_view(job_id, record, None, state)
                # Stored, so a runner that starts later finds it and does not run.
                outcome = job_outcome(state, now, error=STOPPED_JOB_ERRORS[state])
                self.finish(folder, outcome)
            else:
                # A crash may have left the request behind a final outcome; it is not kept.
                (folder / "request.json").unlink(missing_ok=True)
            return job_view(job_id, record, outcome, outcome["state"])

    def cancel(self, job_id, providers, now=None):
        """Stop a running job, or mark one cancelled that no runner holds."""
        folder = self.folder(job_id)
        with self.state_lock(folder):
            record, outcome = self.load(folder, providers)
            if outcome is not None:
                (folder / "request.json").unlink(missing_ok=True)
                return job_view(job_id, record, outcome, outcome["state"])
            now = time.time() if now is None else now
            state = job_state(record, None, self.runner_present(folder), now, cancel_requested=True)
            if state != "lost":
                # The runner checks this marker before it consults and watches for it while it does.
                self.mark(folder / "cancel.requested")
            if state == "cancelling":
                return job_view(job_id, record, None, state)
            outcome = job_outcome(state, now, error=STOPPED_JOB_ERRORS[state])
            self.finish(folder, outcome)
            return job_view(job_id, record, outcome, outcome["state"])

    def prune(self, now):
        """Remove jobs no runner holds that finished, or were created, longer ago than the retention period, and
        folders a crash left in staging or mid-removal. One unreadable job never stops the others' removal."""
        if not check_private(self.directory.parent, self.directory):
            return
        for entry in list(self.directory.iterdir()):
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if entry.name.startswith(DELETING_PREFIX):
                    shutil.rmtree(entry, ignore_errors=True)
                    continue
                if entry.name.startswith(STAGING_PREFIX):
                    # A concurrent start may publish this folder at any moment; a vanished one is skipped.
                    if now - entry.stat().st_mtime > STAGING_RETENTION_SECONDS:
                        shutil.rmtree(entry, ignore_errors=True)
                    continue
                tombstone = None
                # Without waiting: a process stopped while holding one job's lock must not stall every start.
                with self.flock(entry / "state.lock", blocking=False) as held:
                    if not held or self.runner_present(entry):
                        continue
                    if self.expired(entry, now):
                        tombstone = self.directory / f"{DELETING_PREFIX}{entry.name}"
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

    def claim(self, folder):
        """Take the job's request for this runner, or None when the job must not run. Hold the runner lock."""
        with self.state_lock(folder):
            record = self.read(folder / "job.json")
            if record is None:
                return None  # removed while this runner waited
            if self.read(folder / "outcome.json") is not None:
                (folder / "request.json").unlink(missing_ok=True)
                return None  # cancelled, or judged lost, before the runner started
            now = time.time()
            if not valid_job_record(record):
                self.finish(folder, job_outcome("failed", now, error=UNREADABLE))
                return None
            # This runner holds runner.lock, but has yet to claim the job.
            state = job_state(record, None, False, now, cancel_requested=cancel_requested(folder))
            if state in STOPPED_JOB_ERRORS:
                self.finish(folder, job_outcome(state, now, error=STOPPED_JOB_ERRORS[state]))
                return None
            try:
                request = self.read(folder / "request.json")
            except BridgeError:
                request = None
            if not isinstance(request, dict):
                self.finish(folder, job_outcome("failed", time.time(), error=UNREADABLE))
                return None
            (folder / "request.json").unlink(missing_ok=True)
            # The PID is recorded for diagnosis only; nothing signals it.
            self.write(folder / "job.json", {**record, "state": "running", "pid": os.getpid(),
                                             "started_at": time.time()})
            return request

    def record_outcome(self, folder, outcome):
        with self.state_lock(folder):
            self.finish(folder, outcome)


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
        with store.runner_lock(folder):
            request = store.claim(folder)
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
        store.record_outcome(folder, job_outcome("cancelled", time.time(), error=CANCELLED))
        if watcher.done() and not watcher.cancelled():
            return  # cancelled through the marker: the job ends normally
        raise
    except Exception:
        outcome = job_outcome("failed", time.time(), error=UNEXPECTED)
    finally:
        watcher.cancel()
    store.record_outcome(folder, outcome)
