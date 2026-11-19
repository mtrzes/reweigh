"""Exercise peer_guard.py directly: status reporting, lifeline shutdown and its failure paths."""

import os
import select
import selectors
import resource
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import DEADLINE_SECONDS, SCRIPTS, eventually, module_view, running
import peer_guard  # noqa: E402  (support adds the plugin's scripts to the path)

GUARD = SCRIPTS / "peer_guard.py"
# A peer that starts a SIGTERM-ignoring descendant, records both PIDs, then behaves as MODE says.
PEER = textwrap.dedent("""\
    import os, signal, subprocess, sys, time
    folder, mode = sys.argv[1], sys.argv[2]
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    open(os.path.join(folder, "pids"), "w").write(f"{os.getpid()} {child.pid}")
    if mode == "exit":
        sys.exit(7)
    time.sleep(60)
""")


class GuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-guard-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.peer = self.folder / "peer.py"
        self.peer.write_text(PEER)

    def guard(self, mode, lifeline_fd=None, command=None, status_unread=False):
        lifeline_read, lifeline_write = os.pipe()
        status_read, status_write = os.pipe()
        if status_unread:  # closed before the guard starts, so its status write cannot win a race with the close
            os.close(status_read)
            status_read = None
        if lifeline_fd is not None:
            os.dup2(lifeline_read, lifeline_fd)
            os.close(lifeline_read)
            lifeline_read = lifeline_fd
        command = command or [sys.executable, str(self.peer), str(self.folder), mode]
        process = subprocess.Popen(
            [sys.executable, "-I", str(GUARD), str(lifeline_read), str(status_write), "--", *command],
            pass_fds=(lifeline_read, status_write), start_new_session=True,
        )
        os.close(lifeline_read)
        os.close(status_write)
        self.addCleanup(self.close_quietly, lifeline_write, *([] if status_read is None else [status_read]))
        return process, lifeline_write, status_read

    @staticmethod
    def close_quietly(*descriptors):
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def read_status(self, status):
        """The guard's status line, or a failure at the deadline instead of a hang."""
        with selectors.DefaultSelector() as selector:
            selector.register(status, selectors.EVENT_READ)
            if not selector.select(DEADLINE_SECONDS):
                self.fail("The guard reported no status.")
        return os.read(status, 64)

    def pids(self):
        path = self.folder / "pids"
        eventually(lambda: path.exists() and len(path.read_text().split()) == 2, "The peer did not start.")
        return [int(pid) for pid in path.read_text().split()]

    @staticmethod
    def assert_stopped(pids):
        eventually(lambda: not any(running(pid) for pid in pids), "A peer process outlived its guard.")

    def test_the_peer_exit_status_is_reported_and_closing_the_lifeline_stops_the_group(self):
        process, lifeline, status = self.guard("exit")
        pids = self.pids()
        self.assertEqual(self.read_status(status), b"7\n")
        self.assertTrue(running(pids[1]))  # the descendant lives until the lifeline closes
        os.close(lifeline)
        process.wait(timeout=10)
        self.assert_stopped(pids)

    def test_a_status_nobody_reads_still_ends_in_stopping_the_group(self):
        process, lifeline, _ = self.guard("exit", status_unread=True)  # the bridge died; its end is gone
        pids = self.pids()
        process.wait(timeout=10)  # the lifeline is still open: only the failed status write can stop the guard
        self.assert_stopped(pids)
        os.close(lifeline)

    def test_a_high_numbered_lifeline_still_works(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 2000
        if hard != resource.RLIM_INFINITY and hard <= target:
            self.skipTest("The descriptor limit is too low for this test.")
        if soft != resource.RLIM_INFINITY and soft <= target:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target + 1, hard))
            except (OSError, ValueError):
                self.skipTest("The descriptor limit cannot be raised for this test.")
            self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        process, lifeline, status = self.guard("exit", lifeline_fd=target)
        pids = self.pids()
        # The status is written from inside the wait on the lifeline, which select() could not have watched.
        self.assertEqual(self.read_status(status), b"7\n")
        os.close(lifeline)
        process.wait(timeout=10)
        self.assert_stopped(pids)

    def test_a_peer_that_cannot_start_is_reported_by_token(self):
        process, lifeline, status = self.guard("exit", command=[str(self.folder / "missing-peer")])
        self.assertEqual(self.read_status(status), b"start-failed\n")
        os.close(lifeline)
        process.wait(timeout=10)



def timer(*arguments):
    raise AssertionError("The guard waited on a timer.")


class UntimedSelector(selectors.DefaultSelector):
    """A selector whose wait may not time out: a guard that checked the peer on a timer fails with it."""

    def select(self, timeout=None):
        if timeout is not None:
            timer()
        return super().select()


class GuardWaitTests(unittest.TestCase):
    """The guard's waits, run in this process, wake on the peer's exit: a timer between checks would add its interval
    to every command the bridge runs."""

    def setUp(self):
        self.addCleanup(signal.signal, signal.SIGCHLD, signal.getsignal(signal.SIGCHLD))
        previous = signal.set_wakeup_fd(-1)
        self.exits = peer_guard.child_exits()
        # Restoring the previous wakeup descriptor returns the guard's, which is closed with the one it wakes.
        self.addCleanup(lambda: GuardTests.close_quietly(self.exits, signal.set_wakeup_fd(previous)))

    def peer(self, source):
        peer = subprocess.Popen([sys.executable, "-c", source])
        self.addCleanup(lambda: peer.poll() is None and (peer.kill(), peer.wait()))
        return peer

    def test_the_peer_exit_is_reported_without_a_timer(self):
        peer = self.peer("raise SystemExit(7)")
        lifeline, lifeline_write = os.pipe()
        status_read, status = os.pipe()
        self.addCleanup(GuardTests.close_quietly, lifeline, status_read, status)
        reported = []

        def bridge():
            # Reads the status as the bridge does, then closes the lifeline, its only owner; at the deadline it closes
            # it anyway.
            if select.select([status_read], [], [], DEADLINE_SECONDS)[0]:
                reported.append(os.read(status_read, 64))
            os.close(lifeline_write)

        thread = threading.Thread(target=bridge)
        thread.start()
        self.addCleanup(thread.join, DEADLINE_SECONDS)
        with patch.object(peer_guard, "selectors", module_view(selectors, DefaultSelector=UntimedSelector)):
            peer_guard.wait_for_lifeline(lifeline, peer, status, self.exits)
        self.assertEqual(reported, [b"7\n"])

    def test_the_group_is_killed_as_soon_as_the_peer_stops(self):
        peer = self.peer("import time; time.sleep(60)")
        signals = []

        class Selector(selectors.DefaultSelector):
            def select(self, timeout=None):
                # The peer stops only once the guard waits, so only its exit can end the wait before the grace period.
                peer.terminate()
                return super().select(timeout)

        started = time.monotonic()
        with (
            patch.object(peer_guard, "os", module_view(os, killpg=lambda group, number: signals.append(number))),
            patch.object(peer_guard, "selectors", module_view(selectors, DefaultSelector=Selector)),
            patch.object(peer_guard, "time", module_view(time, sleep=timer)),
            patch.object(peer_guard, "GRACE_SECONDS", DEADLINE_SECONDS),
        ):
            peer_guard.stop_group(peer, self.exits)
        self.assertLess(time.monotonic() - started, DEADLINE_SECONDS)  # it did not wait out the grace period
        self.assertEqual((signals, peer.returncode), ([signal.SIGTERM, signal.SIGKILL], -signal.SIGTERM))

if __name__ == "__main__":
    unittest.main()
