"""The parallel runner, driven as a command on small throwaway suites: its verdict and what it names."""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "run_tests.py"
DEADLINE_SECONDS = 30


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="run-tests-")
        self.addCleanup(temporary.cleanup)
        self.suite = Path(temporary.name)

    def module(self, name, source):
        (self.suite / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")

    def run_suite(self, *arguments):
        completed = subprocess.run([sys.executable, str(RUNNER), "-s", str(self.suite), *arguments],
                                   capture_output=True, text=True, timeout=DEADLINE_SECONDS)
        return completed.returncode, completed.stdout, completed.stderr

    def test_a_green_run_prints_one_line_and_runs_every_test_once(self):
        self.module("test_sample", """\
            import os, unittest
            class First(unittest.TestCase):
                def test_a(self): print(os.getpid(), file=open(os.environ["RAN"], "a"))
                def test_b(self): pass
            class Second(unittest.TestCase):
                def test_c(self): pass
                @unittest.skip("not here")
                def test_d(self): pass
        """)
        ran = self.suite / "ran"
        os.environ["RAN"] = str(ran)
        self.addCleanup(os.environ.pop, "RAN")
        code, stdout, stderr = self.run_suite("-j", "2")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertTrue(stdout.startswith("Ran 4 of 4 tests in "), stdout)
        self.assertTrue(stdout.endswith(" on 2 workers: OK (skipped=1)\n"), stdout)
        self.assertEqual(len(ran.read_text().splitlines()), 1)

    def test_a_failing_test_fails_the_run_and_shows_its_traceback(self):
        self.module("test_sample", """\
            import unittest
            class Cases(unittest.TestCase):
                def test_passes(self): pass
                def test_fails(self): self.assertEqual(1, 2)
        """)
        code, stdout, stderr = self.run_suite("-j", "2")
        self.assertEqual(code, 1)
        self.assertIn("test_sample.Cases.test_fails", stderr)
        self.assertIn("AssertionError: 1 != 2", stderr)
        self.assertTrue(stdout.endswith(": FAILED (failures=1)\n"), stdout)

    def test_a_module_that_cannot_be_imported_fails_before_any_worker_starts(self):
        self.module("test_sample", "import unittest\nclass Cases(unittest.TestCase):\n    def test_a(self): pass\n")
        self.module("test_broken", "import a_module_that_does_not_exist\n")
        code, stdout, stderr = self.run_suite()
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("a_module_that_does_not_exist", stderr)

    def test_a_worker_that_dies_names_the_running_test_and_those_that_did_not_run(self):
        self.module("test_sample", """\
            import os, unittest
            class Cases(unittest.TestCase):
                def test_1_passes(self): pass
                def test_2_exits(self): os._exit(3)
                def test_3_never_runs(self): pass
        """)
        code, stdout, stderr = self.run_suite("-j", "1")
        self.assertEqual(code, 1)
        self.assertIn("exited with status 3 before reporting, while running: test_sample.Cases.test_2_exits", stderr)
        self.assertIn("Tests that did not run:\n  test_sample.Cases.test_2_exits\n  test_sample.Cases.test_3_never_runs",
                      stderr)
        self.assertTrue(stdout.startswith("Ran 1 of 3 tests in "), stdout)

    def test_a_hung_worker_is_stopped_with_its_children_at_the_deadline(self):
        pid_file = self.suite / "child.pid"
        self.module("test_sample", f"""\
            import subprocess, sys, time, unittest
            class Cases(unittest.TestCase):
                def test_hangs(self):
                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    open({str(pid_file)!r}, "w").write(str(child.pid))
                    time.sleep(60)
        """)
        began = time.monotonic()
        code, _, stderr = self.run_suite("--deadline", "2")
        self.assertLess(time.monotonic() - began, DEADLINE_SECONDS)
        self.assertEqual(code, 1)
        self.assertIn("deadline while running: test_sample.Cases.test_hangs", stderr)
        child = int(pid_file.read_text())
        deadline = time.monotonic() + DEADLINE_SECONDS
        while self.alive(child):
            self.assertLess(time.monotonic(), deadline, "The hung test's child outlived its worker.")
            time.sleep(0.01)

    @staticmethod
    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=DEADLINE_SECONDS).stdout.strip()
        return bool(state) and not state.startswith("Z")


if __name__ == "__main__":
    unittest.main()
