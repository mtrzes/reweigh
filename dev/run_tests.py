"""Run a unittest suite in parallel processes, as a faster local alternative to `python -m unittest discover`.

A developer tool, not part of the shipped plugin. The serial `discover` commands in the README stay the reference
and what CI runs; this one gives the same verdict sooner. It discovers the suite once, deals the test IDs round robin
to worker processes, and fails unless every discovered test ran exactly once and passed. Tests share no process, so
a test that leaks state into the next one in its process still shows, in the order its worker runs them.

    .venv/bin/python dev/run_tests.py -s plugins/reweigh/tests
    .venv/bin/python dev/run_tests.py -s dev/tests -t dev/tests

A green run prints one line. A red one prints each failing worker's tracebacks and names any test that did not run,
ran twice or was still running when its worker died or reached the deadline.
"""

import argparse
import collections
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

DEFAULT_WORKERS = min(8, os.cpu_count() or 1)
DEADLINE_SECONDS = 600  # for the whole run; CI stops a serial run at 15 minutes


def discovered(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from discovered(item)
        else:
            yield item


def record(report, **event):
    report.write(json.dumps(event) + "\n")
    report.flush()


class RecordingResult(unittest.TextTestResult):
    """Writes each test's start and end to the report as it happens, so a worker that dies still says where."""

    def __init__(self, stream, descriptions, verbosity, *, report):
        super().__init__(stream, descriptions, verbosity)
        self.report = report

    def startTest(self, test):
        record(self.report, started=test.id())
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        record(self.report, finished=test.id())


def work(top, report_path):
    """Worker: run the test IDs read from stdin and report on report_path; the exit status is the verdict."""
    sys.path.insert(0, top)
    ids = json.load(sys.stdin)
    stream = io.StringIO()
    with open(report_path, "w", encoding="utf-8") as report:
        runner = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=lambda *a: RecordingResult(
            *a, report=report))
        result = runner.run(unittest.defaultTestLoader.loadTestsFromNames(ids))
        record(report, output=stream.getvalue(), failures=len(result.failures), errors=len(result.errors),
               skipped=len(result.skipped), unexpected_successes=len(result.unexpectedSuccesses))
    return 0 if result.wasSuccessful() else 1


def read_report(path):
    started, finished, summary = [], [], None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:  # a worker killed mid-write leaves a partial last line
            continue
        if "started" in event:
            started.append(event["started"])
        elif "finished" in event:
            finished.append(event["finished"])
        elif "output" in event:
            summary = event
    return started, finished, summary


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(start, top, workers, deadline):
    loader = unittest.TestLoader()
    sys.path.insert(0, top)
    tests = list(discovered(loader.discover(start, top_level_dir=top)))
    if loader.errors:
        print("Discovery failed:", *loader.errors, sep="\n", file=sys.stderr)
        return 1
    ids = [test.id() for test in tests]
    shards = [ids[k::workers] for k in range(workers) if ids[k::workers]]
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="run-tests-") as folder:
        processes = []
        try:
            for number, shard in enumerate(shards):
                report, log = (os.path.join(folder, f"{number}.{kind}") for kind in ("jsonl", "log"))
                with open(log, "w", encoding="utf-8") as written:  # a file, so a chatty worker never blocks on a pipe
                    process = subprocess.Popen(
                        [sys.executable, os.path.abspath(__file__), "--worker", top, report], stdin=subprocess.PIPE,
                        stdout=written, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                process.stdin.write(json.dumps(shard))
                process.stdin.close()
                processes.append((process, report, log))
            outcomes = []
            for process, report, log in processes:
                try:
                    process.wait(timeout=max(0, deadline - (time.monotonic() - began)))
                    timed_out = False
                except subprocess.TimeoutExpired:
                    stop(process)
                    timed_out = True
                output = Path(log).read_text(encoding="utf-8", errors="replace")
                outcomes.append((process.returncode, timed_out, output, *read_report(report)))
        finally:
            for process, *_ in processes:
                if process.poll() is None:
                    stop(process)
    return verdict(ids, outcomes, workers, time.monotonic() - began, deadline)


def verdict(ids, outcomes, workers, elapsed, deadline):
    failed = False
    ran = collections.Counter()
    totals = collections.Counter()
    for code, timed_out, output, started, finished, summary in outcomes:
        ran.update(finished)
        unfinished = collections.Counter(started) - collections.Counter(finished)
        if output:  # what the tests themselves wrote, such as warnings: shown as a serial run would show it
            sys.stderr.write(output)
        if summary:
            totals.update({key: summary[key] for key in ("failures", "errors", "skipped", "unexpected_successes")})
        if timed_out:
            failed = True
            print(f"A worker reached the {deadline:g} s deadline while running: {', '.join(unfinished) or '-'}",
                  file=sys.stderr)
        elif summary is None:
            failed = True
            print(f"A worker exited with status {code} before reporting, while running: "
                  f"{', '.join(unfinished) or '-'}", file=sys.stderr)
        elif code != 0:
            failed = True
            sys.stderr.write(summary["output"])
    missing = collections.Counter(ids) - ran
    repeated = ran - collections.Counter(ids)
    if missing or repeated:
        failed = True
        for name, items in (("did not run", missing), ("ran more than once", repeated)):
            if items:
                print(f"Tests that {name}:", *sorted(items), sep="\n  ", file=sys.stderr)
    counts = ", ".join(f"{key.replace('_', ' ')}={value}" for key, value in sorted(totals.items()) if value)
    status = "FAILED" if failed else "OK"
    print(f"Ran {sum(ran.values())} of {len(ids)} tests in {elapsed:.1f}s on {len(outcomes)} workers: {status}"
          + (f" ({counts})" if counts else ""))
    return 1 if failed else 0


def main(argv):
    if argv[:1] == ["--worker"]:
        return work(*argv[1:3])
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-s", "--start-directory", default=".", help="directory to start discovery in")
    parser.add_argument("-t", "--top-level-directory", help="top-level directory of the project (default: start)")
    parser.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS, help="number of worker processes")
    parser.add_argument("--deadline", type=float, default=DEADLINE_SECONDS, help="seconds for the whole run")
    arguments = parser.parse_args(argv)
    if arguments.workers < 1:
        parser.error("--workers must be at least 1")
    start = os.path.abspath(arguments.start_directory)
    top = os.path.abspath(arguments.top_level_directory or start)
    return run(start, top, arguments.workers, arguments.deadline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
