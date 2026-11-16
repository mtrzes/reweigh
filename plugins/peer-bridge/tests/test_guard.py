"""Exercise peer_guard.py directly: status reporting, lifeline shutdown and its failure paths."""

import os
import selectors
import resource
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import DEADLINE_SECONDS, SCRIPTS, eventually, running

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

    def guard(self, mode, lifeline_fd=None, command=None):
        lifeline_read, lifeline_write = os.pipe()
        status_read, status_write = os.pipe()
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
        self.addCleanup(self.close_quietly, lifeline_write, status_read)
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
        process, lifeline, status = self.guard("exit")
        os.close(status)  # the bridge died; its end of the status pipe is gone
        pids = self.pids()
        process.wait(timeout=10)  # the lifeline is still open: only the failed status write can stop the guard
        self.assert_stopped(pids)
        os.close(lifeline)

    def test_a_high_numbered_lifeline_still_works(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 2000
        if hard != resource.RLIM_INFINITY and hard <= target:
            self.skipTest("The descriptor limit is too low for this test.")
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, target + 10), hard))
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


if __name__ == "__main__":
    unittest.main()
