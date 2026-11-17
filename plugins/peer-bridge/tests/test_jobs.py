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

    @staticmethod
    async def never(request):
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

        def runner_gone():  # the runner publishes its outcome before it releases its lock
            with self.client.jobs.state_lock(folder):
                return not self.client.jobs.runner_present(folder)

        await eventually_async(runner_gone, "The cancelled runner still holds its job.")
        self.client.jobs.prune(time.time() + LATER)
        self.assertFalse(folder.exists())

    async def test_a_killed_runners_conversation_stays_locked_until_its_guard_has_stopped_the_peer(self):
        # The runner's death frees its own lock at once, but the guard is still stopping a peer that ignores SIGTERM
        # in a pinned checkout; until it has, no other consultation may take the conversation or sweep the checkout.
        git_repository(self.project)
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang", "SIMULATED_PEER_IGNORES_SIGTERM": "1"}):
            job_id = (await self.start(ref="HEAD"))["job_id"]
            await self.wait_for_child()
        [checkout] = (self.state / "worktrees").iterdir()
        folder = self.folder(job_id)
        os.kill(json.loads((folder / "job.json").read_text())["pid"], signal.SIGKILL)

        def runner_gone():
            with self.client.jobs.state_lock(folder):
                return not self.client.jobs.runner_present(folder)

        await eventually_async(runner_gone, "The killed runner still holds its job.")
        with self.client.store.holding(checkout.name) as lock:
            self.assertIsNone(lock)  # the guard's grace period for the SIGTERM-ignoring peer has not ended
        await bridge.sweep_worktrees(self.client.store)
        self.assertTrue(checkout.is_dir())
        await self.assert_processes_stopped()

        def free():
            with self.client.store.holding(checkout.name) as lock:
                return lock is not None

        await eventually_async(free, "The conversation stayed locked after its guard stopped.")
        await bridge.sweep_worktrees(self.client.store)
        self.assertFalse(checkout.exists())

    async def test_a_killed_runner_takes_its_peer_with_it_and_its_job_is_lost(self):
        with patch.dict(os.environ, {"SIMULATED_PEER_MODE": "spawn_hang"}):
            job_id = (await self.start())["job_id"]
            await self.wait_for_child()
        folder = self.folder(job_id)
        os.kill(json.loads((folder / "job.json").read_text())["pid"], signal.SIGKILL)

        def runner_gone():
            with self.client.jobs.state_lock(folder):
                return not self.client.jobs.runner_present(folder)

        await eventually_async(runner_gone, "The killed runner still holds its job.")
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

    async def test_a_runner_that_cannot_start_leaves_its_job_cancelled(self):
        def fail(job_id):
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        self.client.spawn = fail
        with self.assertRaisesRegex(bridge.BridgeError, "Cannot start the consultation runner"):
            await self.start(prompt="PRIVATE QUESTION")
        [folder] = (self.state / "jobs").iterdir()
        self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")
        self.assertFalse((folder / "request.json").exists())

    async def test_a_start_cancelled_during_creation_leaves_no_runnable_job(self):
        entered, release = threading.Event(), threading.Event()
        real_create, spawned = self.client.jobs.create, []

        def held_create(*arguments):
            entered.set()
            release.wait(DEADLINE_SECONDS)  # the barrier: creation is inside its worker thread
            return real_create(*arguments)

        self.client.jobs.create = held_create
        self.client.spawn = spawned.append
        task = asyncio.create_task(self.start(prompt="PRIVATE QUESTION"))
        self.assertTrue(await asyncio.to_thread(entered.wait, DEADLINE_SECONDS))
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        [folder] = (self.state / "jobs").iterdir()
        self.assertEqual(spawned, [])
        self.assertEqual((await self.client.job(folder.name, CLAUDE))["state"], "cancelled")
        self.assertFalse((folder / "request.json").exists())

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
        self.assertEqual((view["state"], view["error"]), ("cancelled", jobs.CANCELLED))

    async def test_a_queued_job_cancelled_before_its_runner_starts_never_runs(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelled")
        self.assertFalse((self.folder(job_id) / "request.json").exists())
        await jobs.run_job(self.client.jobs, job_id, self.never)
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelled")

    async def test_a_cancel_marker_left_before_the_runner_starts_stops_it(self):
        job_id = await self.queued()
        (self.folder(job_id) / "cancel.requested").touch()
        await jobs.run_job(self.client.jobs, job_id, self.never)
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelled")
        self.assertFalse((self.folder(job_id) / "request.json").exists())

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

    async def test_cancelling_an_overdue_queued_job_stores_it_as_lost(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        later = time.time() + consultation.JOB_START_GRACE_SECONDS + 1
        cancelled = self.client.jobs.cancel(job_id, CLAUDE, now=later)
        self.assertEqual((cancelled["state"], cancelled["error"]), ("lost", consultation.LOST_JOB))
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "lost")
        self.assertFalse((self.folder(job_id) / "request.json").exists())

    async def test_a_job_reads_cancelling_until_its_runner_stops(self):
        job_id = await self.queued()
        store = self.client.jobs
        with store.runner_lock(self.folder(job_id)):  # a runner that has not seen the marker yet
            self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "cancelling")
            self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelling")
        await jobs.run_job(self.client.jobs, job_id, self.never)  # the runner then honours the marker
        self.assertEqual((await self.client.job(job_id, CLAUDE))["state"], "cancelled")

    async def test_prune_never_waits_for_a_job_another_process_holds(self):
        held, free = await self.queued(), await self.queued()
        other = jobs.JobStore(self.state)
        with other.state_lock(self.folder(held)):
            await asyncio.wait_for(asyncio.to_thread(self.client.jobs.prune, time.time() + LATER), DEADLINE_SECONDS)
        self.assertEqual((self.folder(held).exists(), self.folder(free).exists()), (True, False))

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

    async def test_a_job_carries_its_codex_model_and_effort_to_the_runner(self):
        job_id = await self.queued(provider="codex", model="gpt-6-astra", reasoning_effort="xhigh")
        received = []

        async def consult(request):
            received.append((request["model"], request["reasoning_effort"]))
            return {"conversation_id": str(uuid.uuid4()), "reply": "done", "model": request["model"],
                    "reasoning_effort": request["reasoning_effort"]}

        await jobs.run_job(self.client.jobs, job_id, consult)
        self.assertEqual(received, [("gpt-6-astra", "xhigh")])
        done = await self.client.job(job_id, {"codex"})
        self.assertEqual((done["model"], done["reasoning_effort"]), ("gpt-6-astra", "xhigh"))
        with self.assertRaisesRegex(bridge.BridgeError, "Codex consultations only"):
            await self.queued(provider="claude", reasoning_effort="xhigh")

    async def test_a_cancel_after_the_reply_but_before_its_outcome_keeps_the_result(self):
        job_id = await self.queued(prompt="PRIVATE QUESTION")
        store, other, seen = jobs.JobStore(self.state), jobs.JobStore(self.state), {}
        record = store.record_outcome

        def at_the_outcome(folder, outcome):
            # The reply is in and its outcome not yet written; the runner still holds the job.
            seen["request kept"] = (folder / "request.json").exists()
            seen["view"] = other.view(job_id, CLAUDE)["state"]
            seen["cancel"] = other.cancel(job_id, CLAUDE)["state"]
            record(folder, outcome)

        store.record_outcome = at_the_outcome

        async def consult(request):
            return {"conversation_id": "conv", "reply": "kept"}

        await jobs.run_job(store, job_id, consult)
        self.assertEqual(seen, {"request kept": False, "view": "running", "cancel": "cancelling"})
        done = await self.client.job(job_id, CLAUDE)
        self.assertEqual((done["state"], done["reply"]), ("completed", "kept"))
        self.assertEqual((await self.client.cancel(job_id, CLAUDE))["state"], "completed")

    # Files and retention

    async def test_a_job_appears_whole_or_not_at_all(self):
        def crash(path, data):
            if path.name == "job.json":
                raise OSError(errno.ENOSPC, "No space left on device")
            jobs.JobStore.write(path, data)

        with patch.object(self.client.jobs, "write", crash):
            with self.assertRaisesRegex(bridge.BridgeError, "Cannot read or write the consultation job files"):
                await self.start(prompt="PRIVATE QUESTION")
        self.assertEqual(list((self.state / "jobs").iterdir()), [])
        stale = self.state / "jobs" / f"{jobs.STAGING_PREFIX}leftover"
        stale.mkdir()
        (stale / "request.json").write_text("PRIVATE QUESTION")
        self.client.jobs.prune(time.time() + jobs.STAGING_RETENTION_SECONDS + 60)
        self.assertFalse(stale.exists())

    async def test_prune_skips_a_staging_folder_published_meanwhile(self):
        await self.queued()
        vanishing = self.state / "jobs" / f"{jobs.STAGING_PREFIX}being-published"
        vanishing.mkdir()
        published = self.state / "jobs" / "published-meanwhile"
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
