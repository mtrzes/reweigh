"""Exercise background consultations: the job store in-process with stand-in consultations, and real detached runners
where the process boundary is the point (a reply, a cancellation, a killed runner).

Interleavings are produced, not hoped for: a hook runs the competing call at the exact point, and a lock is checked
through a second open file, which flock treats as another holder.
"""

import asyncio
import errno
import json
import os
import shutil
import signal
import threading
import sys
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import DEADLINE_SECONDS, BridgeTestCase, bridge, consultation, eventually_async, git_repository
import jobs  # found through the script directory support.py adds

CLAUDE = {"claude"}
LATER = consultation.JOB_RETENTION_SECONDS + 60


class RequestFormatTests(unittest.TestCase):
    """The persisted request, written by start and read by the runner: pure, no files."""

    def test_job_request_round_trips_and_another_shape_is_refused(self):
        written = jobs.job_request("claude", "Why?", Path("/project"), "", "", None, "Review", "opus", "max",
                                   (Path("/other"),))
        self.assertEqual(written, {"provider": "claude", "prompt": "Why?", "working_directory": "/project",
                                   "context": "", "conversation_id": None, "ref": None, "seed_thread": "Review",
                                   "model": "opus", "reasoning_effort": "max", "extra_directories": ["/other"]})
        for name, saved, expected in (
            ("written", written, written),
            ("no extra directories", written | {"extra_directories": None}, written | {"extra_directories": None}),
            ("missing extra directories",
             {key: value for key, value in written.items() if key != "extra_directories"}, None),
            ("directory as text", written | {"extra_directories": "/other"}, None),
            ("number as directory", written | {"extra_directories": [1]}, None),
            ("extra field", written | {"later": 1}, written),
            ("not an object", ["Why?"], None),
            ("unknown provider", written | {"provider": "other"}, None),
            ("missing prompt", {key: value for key, value in written.items() if key != "prompt"}, None),
            ("missing ref", {key: value for key, value in written.items() if key != "ref"}, None),
            ("number as text", written | {"context": 1}, None),
            ("number as choice", written | {"model": 1}, None),
        ):
            with self.subTest(request=name):
                self.assertEqual(jobs.read_job_request(saved), expected)


class JobTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        poll = patch.object(bridge, "JOB_POLL_SECONDS", 0.02)
        poll.start()
        self.addCleanup(poll.stop)

    async def start(self, provider="claude", **arguments):
        request = {"prompt": "Explain the query.", "working_directory": str(self.project), **arguments}
        return await self.client.start(provider, **request)

    async def finished(self, job_id):
        return await self.client.job(job_id, CLAUDE, wait_seconds=DEADLINE_SECONDS)

    def folder(self, job_id):
        return self.state / "jobs" / job_id

    async def queued(self, **arguments):
        """Start a job whose runner never starts, for a test that runs it in-process or not at all."""
        self.client.spawn = lambda job_id: None
        return (await self.start(**arguments))["job_id"]

    async def run_in_process(self, job_id, reply="done"):
        async def consult(request):
            return {"conversation_id": str(uuid.uuid4()), "reply": reply}

        await jobs.run_job(self.client.jobs, job_id, consult)

    def runner_gone(self, folder):
        """Whether the job's runner has released its lock, probed under the state lock as the store probes it."""
        with self.client.jobs.state_lock(folder):
            return not self.client.jobs.runner_present(folder)

    async def never(self, request):
        """A consultation that must not happen. run_job records any exception as `failed`, so a call is also
        checked after the test, whatever the test asserts."""
        if not hasattr(self, "consulted"):
            self.consulted = []
            self.addCleanup(lambda: self.assertEqual(self.consulted, [], "A job that must not run was consulted."))
        self.consulted.append(request)
        raise AssertionError("This job must not run.")

    # Real runners

    async def test_a_started_job_runs_detached_and_returns_the_reply(self):
        started = await self.start()
        if started["state"] != "completed":  # a fast runner may already have finished before the first view
            self.assertIn(started["state"], {"queued", "running"})
            self.assertEqual(set(started), {"job_id", "provider", "state"})
        done = await self.finished(started["job_id"])
        self.assertEqual((done["state"], done["reply"]), ("completed", "claude consulted: Explain the query."))
        self.assertFalse((self.folder(started["job_id"]) / "request.json").exists())
        followup = await self.finished((await self.start(conversation_id=done["conversation_id"],
                                                         prompt="And then?"))["job_id"])
        self.assertEqual(followup["conversation_id"], done["conversation_id"])
        calls = self.calls()
        self.assertEqual(calls[1]["session"], calls[0]["session"])

    async def test_cancel_stops_the_runner_and_its_peer_without_signalling_them(self):
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang"}):
            job_id = (await self.start())["job_id"]
            await self.wait_for_child()
        self.client.jobs.prune(time.time() + LATER)
        self.assertTrue((self.folder(job_id) / "job.json").exists())  # its runner holds it
        with patch("os.kill") as kill, patch("os.killpg") as killpg:
            self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelling")
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual((await self.finished(job_id))["state"], "cancelled")
        await self.assert_processes_stopped()
        self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelled")
        folder = self.folder(job_id)
        # The runner publishes its outcome before it releases its lock.
        await eventually_async(lambda: self.runner_gone(folder), "The cancelled runner still holds its job.")
        self.client.jobs.prune(time.time() + LATER)
        self.assertFalse(folder.exists())

    async def test_a_killed_runners_conversation_stays_locked_until_its_guard_has_stopped_the_peer(self):
        await asyncio.to_thread(git_repository, self.project)
        real_git = shutil.which("git")
        command = self.root / "blocking-peer"
        command.write_text(f"""#!{sys.executable}
import os
import subprocess
import sys
import time
from pathlib import Path

if Path(sys.argv[0]).name == "git":
    git = {real_git!r}
    if sys.argv[4:6] != ["read-tree", "--reset"]:
        os.execv(git, [git, *sys.argv[1:]])
    subprocess.run([git, *sys.argv[1:]], check=True)

root = Path({str(self.root)!r})
(root / "guard.pid").write_text(str(os.getppid()))
temporary = root / "command.pid.tmp"
temporary.write_text(str(os.getpid()))
temporary.replace(root / "command.pid")
time.sleep({4 * DEADLINE_SECONDS})
""")
        command.chmod(0o700)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shutil.copy2(command, bin_dir / "git")
        ready = self.root / "command.pid"

        for name, settings in (
            ("peer", {"REWEIGH_CLAUDE_BIN": str(command)}),
            ("read-tree", {"PATH": os.pathsep.join((str(bin_dir), os.environ["PATH"]))}),
        ):
            with self.subTest(command=name):
                ready.unlink(missing_ok=True)
                with patch.dict(os.environ, settings):
                    job_id = (await self.start(ref="HEAD"))["job_id"]
                await eventually_async(ready.exists, f"The guarded {name} command did not start.")
                [checkout] = (self.state / "worktrees").iterdir()
                folder = self.folder(job_id)
                runner = json.loads((folder / "job.json").read_text())["pid"]
                guard = int((self.root / "guard.pid").read_text())

                def free():
                    with self.client.store.holding(checkout.name) as lock:
                        return lock is not None

                # Pause the guard before the lifeline closes. Only the test can let it begin shutdown.
                os.kill(guard, signal.SIGSTOP)
                try:
                    os.kill(runner, signal.SIGKILL)
                    await eventually_async(lambda: self.runner_gone(folder), "The killed runner still holds its job.")
                    self.assertFalse(free())
                    await bridge.sweep_worktrees(self.client.store)
                    self.assertTrue(checkout.is_dir())
                finally:
                    os.kill(guard, signal.SIGCONT)
                    await self.assert_processes_stopped(("command.pid",))

                await eventually_async(free, "The conversation stayed locked after its guard stopped.")
                await bridge.sweep_worktrees(self.client.store)
                self.assertFalse(checkout.exists())

    async def test_a_runner_that_cannot_start_leaves_its_job_cancelled_even_behind_a_busy_state_lock(self):
        # A status read gives up on a busy lock; cancelling a job whose start failed, which nobody will learn of, waits.
        held, release = threading.Event(), threading.Event()
        store, other = self.client.jobs, jobs.JobStore(self.state)
        real_flock = store.flock

        def flock(path, blocking):
            if held.is_set() and path.name == "state.lock" and blocking:
                release.set()  # the cleanup waits for the holder, so let it go
            return real_flock(path, blocking)

        def hold(job_id):
            with other.flock(self.folder(job_id) / "state.lock", blocking=False) as acquired:
                if acquired:
                    held.set()
                    release.wait(DEADLINE_SECONDS)

        def fail(job_id):
            holder = threading.Thread(target=hold, args=(job_id,), daemon=True)
            holder.start()
            self.assertTrue(held.wait(DEADLINE_SECONDS))
            self.addCleanup(holder.join, DEADLINE_SECONDS)
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        self.client.spawn = fail
        with patch.object(store, "flock", flock), patch.object(jobs, "STATE_LOCK_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(bridge.BridgeError, "Cannot start the consultation runner"):
                await self.start(prompt="PRIVATE QUESTION")
        self.assertTrue(release.is_set())
        [folder] = (self.state / "jobs").iterdir()
        self.assertFalse((folder / "request.json").exists())
        self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")

    async def test_a_runner_launch_that_fails_any_way_cancels_its_job_or_names_it(self):
        # spawn can fail after the runner started, such as when only its reaper thread cannot start.
        def started_then_failed(job_id):
            raise RuntimeError("can't start new thread")

        self.client.spawn = started_then_failed
        with self.assertRaisesRegex(bridge.BridgeError, "Cannot start the consultation runner"):
            await self.start(prompt="PRIVATE QUESTION")
        [folder] = (self.state / "jobs").iterdir()
        self.assertFalse((folder / "request.json").exists())
        self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")

    async def test_a_start_returns_its_job_id_while_the_runner_holds_the_job(self):
        # The runner may already hold the state lock to claim its job when the start reads it.
        spawned = []
        self.client.spawn = spawned.append
        with patch.object(self.client.jobs, "view", side_effect=jobs.JobBusy(jobs.LOCK_BUSY)):
            started = await self.start()
        self.assertEqual(started, {"job_id": spawned[0], "provider": "claude", "state": "queued"})
        self.assertTrue((self.folder(spawned[0]) / "request.json").exists())  # left for its runner, not cancelled
        self.assertEqual((await self.client.job(spawned[0], CLAUDE))["state"], "queued")

    async def test_a_killed_runner_takes_its_peer_with_it_and_its_job_is_lost(self):
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang"}):
            job_id = (await self.start())["job_id"]
            await self.wait_for_child()
        folder = self.folder(job_id)
        os.kill(json.loads((folder / "job.json").read_text())["pid"], signal.SIGKILL)
        await eventually_async(lambda: self.runner_gone(folder), "The killed runner still holds its job.")
        # Cancelled while the guard may still be stopping the peer: no clean cancellation, so lost.
        cancelled = await self.client.cancel(job_id, CLAUDE)
        self.assertEqual((cancelled["state"], cancelled["error"]), ("lost", consultation.LOST_JOB))
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "lost")
        await self.assert_processes_stopped()

    # The store, with the runner in-process

    async def test_a_first_job_creates_a_private_state_directory(self):
        self.assertFalse(self.state.exists())
        await self.queued()
        for directory in (self.state, self.state / "jobs"):
            with self.subTest(directory=directory):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    async def test_a_start_whose_first_view_fails_cancels_its_job_or_names_it(self):
        # The caller learns a job's ID only from that view, so a job it cannot read is cancelled rather than left to
        # run; if cancelling fails too, the failure carries the ID.
        self.client.spawn = lambda job_id: None
        jobs_folder = self.state / "jobs"
        for failure, expected in (
            (bridge.BridgeError("A fixed message."), "A fixed message."),
            (OSError("disk"), "Cannot read or write the consultation job files."),
        ):
            with self.subTest(failure=failure):
                before = set(jobs_folder.iterdir()) if jobs_folder.exists() else set()
                with (
                    patch.object(self.client.jobs, "view", side_effect=failure),
                    self.assertRaises(bridge.BridgeError) as raised,
                ):
                    await self.start(prompt="PRIVATE QUESTION")
                self.assertEqual(str(raised.exception), expected)
                [folder] = set(jobs_folder.iterdir()) - before
                self.assertFalse((folder / "request.json").exists())  # read first: a status read deletes it too
                self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")
        before = set(jobs_folder.iterdir())
        with (
            patch.object(self.client.jobs, "view", side_effect=OSError("disk")),
            patch.object(self.client.jobs, "abandon", side_effect=OSError("disk")),
            self.assertRaises(bridge.BridgeError) as raised,
        ):
            await self.start()
        [folder] = set(jobs_folder.iterdir()) - before
        self.assertIn(folder.name, str(raised.exception))
        self.assertIn("cancellation could not be confirmed", str(raised.exception))

    async def test_an_interrupted_start_stays_cancelled_when_its_cleanup_fails(self):
        # The caller is interrupted during the first view, or during the cleanup that follows a failed one.
        self.client.spawn = lambda job_id: None
        for interrupted in ("view", "abandon"):
            entered, release = threading.Event(), threading.Event()

            def held(*arguments, entered=entered, release=release, **options):
                entered.set()
                release.wait(DEADLINE_SECONDS)
                raise OSError("disk")

            hooks = {"view": OSError("disk"), "abandon": OSError("disk"), interrupted: held}
            with (
                self.subTest(interrupted=interrupted),
                patch.object(self.client.jobs, "view", side_effect=hooks["view"]),
                patch.object(self.client.jobs, "abandon", side_effect=hooks["abandon"]),
            ):
                task = asyncio.create_task(self.start())
                self.assertTrue(await asyncio.to_thread(entered.wait, DEADLINE_SECONDS))
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_a_start_cancelled_during_creation_leaves_no_runnable_job(self):
        real_create, real_abandon, spawned = self.client.jobs.create, self.client.jobs.abandon, []
        self.client.spawn = spawned.append
        # The second time, cancelling the published job fails too: the start still ends as cancelled.
        for abandon in (real_abandon, OSError("disk")):
            entered, release = threading.Event(), threading.Event()

            def held_create(*arguments, entered=entered, release=release):
                entered.set()
                release.wait(DEADLINE_SECONDS)  # the barrier: creation is inside its worker thread
                return real_create(*arguments)

            before = set((self.state / "jobs").iterdir()) if (self.state / "jobs").exists() else set()
            with (
                self.subTest(abandon=abandon),
                patch.object(self.client.jobs, "create", side_effect=held_create),
                patch.object(self.client.jobs, "abandon", side_effect=abandon),
            ):
                task = asyncio.create_task(self.start(prompt="PRIVATE QUESTION"))
                self.assertTrue(await asyncio.to_thread(entered.wait, DEADLINE_SECONDS))
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(spawned, [])
            if abandon is real_abandon:
                [folder] = set((self.state / "jobs").iterdir()) - before
                self.assertFalse((folder / "request.json").exists())
                self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")

    async def test_a_failed_consultation_is_recorded_with_a_fixed_message(self):
        async def refused(request):
            raise bridge.BridgeError("A fixed message.")

        async def broken(request):
            raise RuntimeError("PRIVATE DETAIL")

        for consult, expected in ((refused, "A fixed message."), (broken, jobs.UNEXPECTED)):
            with self.subTest(expected=expected):
                job_id = await self.queued()
                await jobs.run_job(self.client.jobs, job_id, consult)
                view = await self.client.job(job_id, CLAUDE)
                self.assertEqual((view["state"], view["error"]), ("failed", expected))

    async def test_completion_is_stamped_when_the_reply_arrives(self):
        job_id = await self.queued()
        clock = SimpleNamespace(now=1000.0)

        async def consult(request):
            clock.now = 5000.0
            return {"conversation_id": "conv", "reply": "late"}

        with patch.object(jobs, "time", SimpleNamespace(time=lambda: clock.now)):
            await jobs.run_job(self.client.jobs, job_id, consult)
        self.assertEqual(json.loads((self.folder(job_id) / "outcome.json").read_text())["finished_at"], 5000.0)

    async def test_a_runner_stopped_by_a_signal_records_its_job_cancelled(self):
        # The runner's SIGTERM handler cancels its task, which is what this cancel does.
        job_id = await self.queued()
        consulting = asyncio.Event()

        async def consult(request):
            consulting.set()
            await asyncio.Future()

        runner = asyncio.create_task(jobs.run_job(self.client.jobs, job_id, consult))
        await asyncio.wait_for(consulting.wait(), DEADLINE_SECONDS)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        view = await self.client.job(job_id, CLAUDE)
        self.assertEqual((view["state"], view["error"]), ("cancelled", consultation.CANCELLED_JOB))

    async def test_the_runner_can_be_cancelled_while_its_state_lock_is_held(self):
        for phase in ("claim", "reply", "cancel"):
            with self.subTest(phase=phase):
                job_id = await self.queued()
                store = self.client.jobs
                folder = self.folder(job_id)
                state_lock = folder / "state.lock"
                attempted, consulting, reply = asyncio.Event(), asyncio.Event(), asyncio.Event()
                real_flock = store.flock
                held = False

                def flock(path, blocking):
                    if path == state_lock and held:
                        attempted.set()
                        self.assertFalse(blocking)
                    return real_flock(path, blocking)

                async def consult(request):
                    consulting.set()
                    await reply.wait()
                    return {"conversation_id": "conv", "reply": "done"}

                with patch.object(store, "flock", flock):
                    runner = asyncio.create_task(jobs.run_job(store, job_id, consult))
                    try:
                        if phase != "claim":
                            await asyncio.wait_for(consulting.wait(), DEADLINE_SECONDS)
                        with real_flock(state_lock, blocking=False) as held:
                            self.assertTrue(held)
                            if phase == "reply":
                                reply.set()
                            if phase != "cancel":
                                await asyncio.wait_for(attempted.wait(), DEADLINE_SECONDS)
                            runner.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(runner, DEADLINE_SECONDS)
                    finally:
                        if not runner.done():
                            runner.cancel()
                            await asyncio.gather(runner, return_exceptions=True)
                with real_flock(folder / "runner.lock", blocking=False) as free:
                    self.assertTrue(free)

    async def test_a_queued_job_cancelled_before_its_runner_starts_never_runs(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelled")
        self.assertFalse((self.folder(job_id) / "request.json").exists())
        await jobs.run_job(self.client.jobs, job_id, self.never)
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelled")

    async def test_only_regular_files_request_cancellation(self):
        target = self.root / "marker-target"
        target.touch()

        checked, real_cancel_requested = asyncio.Event(), jobs.cancel_requested

        def watched(folder):
            requested = real_cancel_requested(folder)
            checked.set()
            return requested

        async def consult(request):
            # The claim reads the marker too; the reply waits for the watcher's first look, which follows this start.
            checked.clear()
            await asyncio.wait_for(checked.wait(), DEADLINE_SECONDS)
            return {"conversation_id": "conv", "reply": "done"}

        for marker_type in ("symlink", "fifo"):
            with self.subTest(marker_type=marker_type):
                job_id = await self.queued()
                marker = self.folder(job_id) / "cancel.requested"
                if marker_type == "symlink":
                    marker.symlink_to(target)
                else:
                    os.mkfifo(marker)
                before = await self.client.job(job_id, CLAUDE)
                with patch.object(jobs, "cancel_requested", watched):
                    await jobs.run_job(self.client.jobs, job_id, consult)
                after = await self.client.job(job_id, CLAUDE)
                self.assertEqual((before["state"], after["state"]), ("queued", "completed"))

    async def test_a_lost_verdict_is_stored_and_a_late_runner_honours_it(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "queued")
        later = time.time() + consultation.JOB_START_GRACE_SECONDS + 1
        self.assertEqual(self.client.jobs.view(job_id, CLAUDE, now=later)["state"], "lost")
        self.assertFalse((self.folder(job_id) / "request.json").exists())
        await jobs.run_job(self.client.jobs, job_id, self.never)
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "lost")
        # Nobody read the job before its runner started too late: the runner itself refuses it.
        job_id = await self.queued()
        record = json.loads((self.folder(job_id) / "job.json").read_text(encoding="utf-8"))
        record["created_at"] -= consultation.JOB_START_GRACE_SECONDS + 1
        (self.folder(job_id) / "job.json").write_text(json.dumps(record), encoding="utf-8")
        (self.folder(job_id) / "cancel.requested").touch()  # interrupted cancel
        await jobs.run_job(self.client.jobs, job_id, self.never)
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "lost")

    async def test_a_job_reads_cancelling_until_its_runner_stops(self):
        job_id = await self.queued()
        store = self.client.jobs
        with store.flock(self.folder(job_id) / "runner.lock", blocking=False) as acquired:
            self.assertTrue(acquired)  # a runner that has not seen the marker yet
            self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelling")
            self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelling")
        await jobs.run_job(self.client.jobs, job_id, self.never)  # the runner then honours the marker
        self.assertFalse((self.folder(job_id) / "request.json").exists())  # before any read, which deletes it too
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelled")

    async def test_prune_never_waits_for_a_job_another_process_holds(self):
        held, free = await self.queued(), await self.queued()
        other = jobs.JobStore(self.state)
        with other.state_lock(self.folder(held)):
            await asyncio.wait_for(asyncio.to_thread(self.client.jobs.prune, time.time() + LATER), DEADLINE_SECONDS)
        self.assertEqual((self.folder(held).exists(), self.folder(free).exists()), (True, False))

    async def test_job_apis_stop_waiting_for_a_busy_state_lock(self):
        job_id = await self.queued()
        state_lock = self.folder(job_id) / "state.lock"

        def sleep(seconds):
            clock.now += seconds

        clock = SimpleNamespace(now=1000.0, monotonic=lambda: clock.now, sleep=sleep)
        for call in (self.client.job, self.client.cancel):
            clock.now = 1000.0
            with self.subTest(call=call.__name__), patch.object(jobs, "time", clock):
                with self.client.jobs.flock(state_lock, blocking=False) as acquired:
                    self.assertTrue(acquired)
                    # JobBusy in particular: a start tells the busy runner apart from a failure by it.
                    with self.assertRaises(jobs.JobBusy) as error:
                        await asyncio.wait_for(call(job_id, CLAUDE), timeout=1)
                self.assertEqual(str(error.exception), "The consultation job is busy. Try again.")
                self.assertEqual(clock.now, 1001.0)

    async def test_the_runner_lock_is_probed_only_under_the_state_lock(self):
        # A probe outside the state lock could meet another probe and take it for a runner.
        job_id = await self.queued()
        store, other = jobs.JobStore(self.state), jobs.JobStore(self.state)
        real_flock, guarded = store.flock, []

        @contextmanager
        def flock(path, blocking):
            if path.name == "runner.lock":
                with other.flock(path.with_name("state.lock"), blocking=False) as free:
                    guarded.append(not free)
            with real_flock(path, blocking) as acquired:
                yield acquired

        store.flock = flock
        self.assertEqual(store.view(job_id, CLAUDE)["state"], "queued")
        self.assertEqual(store.cancel(job_id, CLAUDE)["state"], "cancelled")
        store.prune(time.time() + LATER)
        self.assertEqual(guarded, [True, True, True])
        self.assertFalse(self.folder(job_id).exists())

    async def test_a_claude_jobs_extra_directories_reach_the_runner_resolved(self):
        # Its warnings come back from the result.
        other = self.root / "other"
        other.mkdir()
        job_id = await self.queued(provider="claude", extra_directories=[str(other)])
        received = []

        async def claude(request):
            received.append(request["extra_directories"])
            return {"conversation_id": str(uuid.uuid4()), "reply": "done", "warnings": ["w"]}

        await jobs.run_job(self.client.jobs, job_id, claude)
        self.assertEqual(received, [[str(other.resolve())]])
        self.assertEqual((await self.client.job(job_id, {"claude"}))["warnings"], ["w"])

    async def test_a_cancel_after_the_reply_but_before_its_outcome_keeps_the_result(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        store, other, seen = jobs.JobStore(self.state), jobs.JobStore(self.state), {}
        record = store.record_outcome

        async def at_the_outcome(folder, outcome):
            # The reply is in and its outcome not yet written; the runner still holds the job.
            seen["request kept"] = (folder / "request.json").exists()
            seen["view"] = other.view(job_id, CLAUDE)["state"]
            seen["cancel"] = other.cancel(job_id, CLAUDE)["state"]
            await record(folder, outcome)

        store.record_outcome = at_the_outcome

        async def consult(request):
            return {"conversation_id": "conv", "reply": "kept"}

        await jobs.run_job(store, job_id, consult)
        self.assertEqual(seen, {"request kept": False, "view": "running", "cancel": "cancelling"})
        done = await self.client.job(job_id, CLAUDE)
        self.assertEqual((done["state"], done["reply"]), ("completed", "kept"))
        self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "completed")

    # Files and retention

    async def test_claim_removes_an_unreadable_request_and_records_failure(self):
        for name, content in (("malformed", "{\"prompt\": \"PRIVATE QUESTION\""),
                              ("another shape", json.dumps({"prompt": "PRIVATE QUESTION"}))):
            with self.subTest(request=name):
                job_id = await self.queued()
                request = self.folder(job_id) / "request.json"
                request.write_text(content)
                await jobs.run_job(self.client.jobs, job_id, self.never)
                self.assertFalse(request.exists())
                view = await self.client.job(job_id, CLAUDE)
                self.assertEqual((view["state"], view["error"]), ("failed", jobs.UNREADABLE))

    async def test_prune_uses_the_folder_age_when_reading_the_record_fails(self):
        job_id = await self.queued()
        folder = self.folder(job_id)
        record = (folder / "job.json").stat()
        old = time.time() - LATER
        os.utime(folder, (old, old))
        fdopen = os.fdopen

        def fail_read():
            raise OSError(errno.EIO, "PRIVATE DETAIL")

        @contextmanager
        def open_with_failed_read(descriptor, *args, **kwargs):
            with fdopen(descriptor, *args, **kwargs) as handle:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == (record.st_dev, record.st_ino):
                    yield SimpleNamespace(read=fail_read)
                else:
                    yield handle

        with patch("state_files.os.fdopen", open_with_failed_read):
            self.client.jobs.prune(time.time())
        self.assertFalse(folder.exists())

    async def test_a_job_appears_whole_or_not_at_all(self):
        def crash(path, data):
            if path.name == "job.json":
                raise OSError(errno.ENOSPC, "No space left on device")
            jobs.JobStore.write(path, data)

        with patch.object(self.client.jobs, "write", crash):
            with self.assertRaisesRegex(bridge.BridgeError, "Cannot read or write the consultation job files"):
                await self.start(prompt="PRIVATE QUESTION")
        self.assertEqual(list((self.state / "jobs").iterdir()), [])
        stale = self.state / "jobs" / f"{jobs.STAGING_PREFIX}{uuid.uuid4()}"
        stale.mkdir()
        (stale / "request.json").write_text("PRIVATE QUESTION")
        self.client.jobs.prune(time.time() + jobs.STAGING_RETENTION_SECONDS + 60)
        self.assertFalse(stale.exists())

    async def test_prune_skips_a_staging_folder_published_meanwhile(self):
        await self.queued()
        published = self.state / "jobs" / str(uuid.uuid4())
        vanishing = published.with_name(f"{jobs.STAGING_PREFIX}{published.name}")
        vanishing.mkdir()
        real_is_dir = Path.is_dir

        def is_dir(path, *arguments, **keywords):
            found = real_is_dir(path, *arguments, **keywords)
            if path == vanishing and found:
                vanishing.rename(published)  # prune saw the folder; then a concurrent start renamed it into place
            return found

        with patch.object(Path, "is_dir", is_dir):
            self.client.jobs.prune(time.time())
        self.assertFalse(vanishing.exists())
        self.assertTrue(published.exists())

    async def test_a_runner_started_after_its_job_was_pruned_does_not_run(self):
        job_id = await self.queued()
        self.client.jobs.prune(time.time() + LATER)
        self.assertEqual(list((self.state / "jobs").iterdir()), [])
        await jobs.run_job(self.client.jobs, job_id, self.never)

    async def test_an_interrupted_removal_is_finished_by_the_next_prune(self):
        await self.queued()
        tombstone = self.state / "jobs" / f"{jobs.DELETING_PREFIX}{uuid.uuid4()}"
        tombstone.mkdir()
        (tombstone / "outcome.json").write_text(json.dumps({"result": {"reply": "PRIVATE REPLY"}}))
        self.client.jobs.prune(time.time())
        self.assertFalse(tombstone.exists())

    async def test_prune_leaves_prefixed_directories_without_a_canonical_job_id(self):
        await self.queued()
        made = []
        for prefix in (jobs.STAGING_PREFIX, jobs.DELETING_PREFIX):
            for suffix in ("keep", "AAAAAAAA-AAAA-4AAA-AAAA-AAAAAAAAAAAA"):
                entry = self.state / "jobs" / f"{prefix}{suffix}"
                entry.mkdir()
                made.append(entry)
        self.client.jobs.prune(time.time() + LATER)
        for entry in made:
            with self.subTest(entry=entry.name):
                self.assertTrue(entry.is_dir())

    async def test_unreadable_jobs_are_refused_with_a_reason_and_never_stop_pruning(self):
        broken_outcome = await self.queued()
        (self.folder(broken_outcome) / "outcome.json").write_text("{}")
        broken_record = await self.queued()
        record = json.loads((self.folder(broken_record) / "job.json").read_text())
        (self.folder(broken_record) / "job.json").write_text(json.dumps(record | {"created_at": "yesterday"}))
        unreadable_record = await self.queued()
        (self.folder(unreadable_record) / "job.json").unlink()
        os.mkfifo(self.folder(unreadable_record) / "job.json")
        unlockable = await self.queued()
        (self.folder(unlockable) / "state.lock").unlink(missing_ok=True)
        os.mkfifo(self.folder(unlockable) / "state.lock")
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            await self.client.job(broken_outcome, CLAUDE)
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            await self.client.cancel(broken_outcome, CLAUDE)
        with self.assertRaisesRegex(bridge.BridgeError, "No consultation job"):
            await self.client.job(broken_record, CLAUDE)
        healthy = await self.queued()
        old = time.time() - LATER
        for job_id in (broken_outcome, broken_record, unreadable_record, unlockable):
            os.utime(self.folder(job_id), (old, old))
        self.client.jobs.prune(time.time())
        # A job whose lock cannot be taken is kept; every other unreadable one is removed like an expired job.
        self.assertEqual(sorted(path.name for path in (self.state / "jobs").iterdir()), sorted([healthy, unlockable]))

    async def test_symlinked_job_folders_and_files_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text(json.dumps({"state": "completed", "finished_at": 1}))
        job_id = await self.queued()
        linked = str(uuid.uuid4())
        (self.state / "jobs" / linked).symlink_to(outside, target_is_directory=True)
        for call in (self.client.job, self.client.cancel):
            with self.subTest(call=call.__name__), self.assertRaisesRegex(bridge.BridgeError, "No consultation job"):
                await call(linked, CLAUDE)
        (self.folder(job_id) / "cancel.requested").symlink_to(outside / "marker")
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            await self.client.cancel(job_id, CLAUDE)
        (self.folder(job_id) / "outcome.json").symlink_to(outside / "secret.json")
        with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
            await self.client.job(job_id, CLAUDE)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["secret.json"])

    @contextmanager
    def unblocked_on_exit(self, fifo):
        """If the code under test is stuck opening the FIFO, open its other end on the way out, so the worker thread
        finishes and the test fails at its deadline instead of hanging at shutdown."""
        try:
            yield
        finally:
            for flags in (os.O_RDONLY | os.O_NONBLOCK, os.O_WRONLY | os.O_NONBLOCK):
                try:
                    os.close(os.open(fifo, flags))
                except OSError:
                    pass

    async def test_a_fifo_in_place_of_a_job_file_is_refused_without_blocking(self):
        job_id = await self.queued()
        for name, call in (("outcome.json", self.client.job), ("cancel.requested", self.client.cancel)):
            fifo = self.folder(job_id) / name
            os.mkfifo(fifo)
            with self.subTest(name=name), self.unblocked_on_exit(fifo):
                with self.assertRaisesRegex(bridge.BridgeError, "unreadable"):
                    await asyncio.wait_for(call(job_id, CLAUDE), timeout=DEADLINE_SECONDS)
            fifo.unlink()

    async def test_a_job_removed_meanwhile_reads_as_unknown(self):
        real_folder = jobs.JobStore.folder

        def vanish(store, identifier):
            folder = real_folder(store, identifier)
            shutil.rmtree(folder)  # removed by a concurrent prune after the check
            return folder

        for name in ("job", "cancel"):
            job_id = await self.queued()
            with self.subTest(call=name), patch.object(jobs.JobStore, "folder", vanish):
                with self.assertRaisesRegex(bridge.BridgeError, "No consultation job"):
                    await getattr(self.client, name)(job_id, CLAUDE)

    async def test_jobs_are_visible_only_to_servers_exposing_their_peer(self):
        job_id = await self.queued()
        for job, providers in ((job_id, {"codex"}), ("not-a-uuid", CLAUDE), (job_id.replace("-", "")[:8], CLAUDE)):
            with self.subTest(job=job, providers=providers):
                with self.assertRaisesRegex(bridge.BridgeError, "No consultation job with that ID"):
                    await self.client.job(job, providers)
                with self.assertRaisesRegex(bridge.BridgeError, "No consultation job with that ID"):
                    await self.client.cancel(job, providers)
        with self.assertRaisesRegex(bridge.BridgeError, "wait_seconds"):
            await self.client.job(job_id, CLAUDE, 61)

    async def test_finished_and_queued_jobs_are_pruned_after_the_retention_period(self):
        finished = await self.queued()
        await self.run_in_process(finished)
        queued = await self.queued()
        self.client.jobs.prune(time.time() + consultation.JOB_RETENTION_SECONDS - 60)
        self.assertTrue(self.folder(finished).exists())
        self.client.jobs.prune(time.time() + LATER)
        self.assertEqual(list((self.state / "jobs").iterdir()), [])
        self.assertFalse(self.folder(queued).exists())


if __name__ == "__main__":
    unittest.main()
