"""Keep a peer CLI's process group from outliving the consultation that started it.

The bridge starts this guard as the leader of a new session. The guard runs the peer as its child in the same process
group, with the standard streams it was given, and reports the peer's exit status on its status descriptor. It then
waits for its lifeline descriptor to close. The lifeline closes when the bridge is done with the peer, and also when
the bridge's process dies for any reason, because the kernel closes it. Either way the guard stops every process in
its group: SIGTERM, a grace period, then SIGKILL, which ends the guard too. Whatever goes wrong inside the guard after
the peer starts, including a status descriptor nobody reads any more, ends in that same stop.

The guard stays a live member of its group until that last signal, so the group's ID cannot pass to another process
while it is being signalled. It keeps its status descriptor open until it exits, so the bridge reads the end of that
descriptor as the guard's exit without reaping it, and can still signal the group safely afterwards.

Usage: peer_guard.py LIFELINE_FD STATUS_FD -- COMMAND...  (standard library only; run with python -I)
"""

import os
import selectors
import signal
import subprocess
import sys
import time

GRACE_SECONDS = 2
POLL_SECONDS = 0.05
# Exact tokens on the status descriptor: the peer's exit status as an integer, or this when it could not start.
START_FAILED = b"start-failed"


def ignore(signal_number, frame):
    """The guard's own group signal must not stop it early. A handler, unlike SIG_IGN, is reset for the peer."""


def release_standard_streams():
    """Let go of the peer's pipes, so only the peer holds them: the bridge then sees a broken input pipe or the end
    of output exactly when the peer, not the guard, stops using them."""
    null = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)
    os.close(null)


def report(status, token):
    """Write a status token, keeping the descriptor open until the guard exits; False when the bridge no longer reads
    it, which the guard treats as a shutdown."""
    try:
        os.write(status, token + b"\n")
        return True
    except OSError:
        return False


def stop_group(peer):
    os.killpg(0, signal.SIGTERM)
    deadline = time.monotonic() + GRACE_SECONDS
    while peer is not None and peer.poll() is None and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
    os.killpg(0, signal.SIGKILL)


def wait_for_lifeline(lifeline, peer, status):
    """Report the peer's exit status when it exits, until the lifeline closes or the status cannot be reported.

    A selector, unlike select(), accepts any descriptor number the bridge passes.
    """
    reported = peer is None
    with selectors.DefaultSelector() as selector:
        selector.register(lifeline, selectors.EVENT_READ)
        while True:
            if selector.select(POLL_SECONDS) and not os.read(lifeline, 1):
                return
            if not reported and peer.poll() is not None:
                reported = True
                if not report(status, b"%d" % peer.returncode):
                    return


def main():
    lifeline, status = int(sys.argv[1]), int(sys.argv[2])
    if sys.argv[3] != "--" or len(sys.argv) < 5:
        sys.exit(2)
    command = sys.argv[4:]
    for signal_number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signal_number, ignore)
    peer = None
    try:
        try:
            # The lifeline and status descriptors are not inherited: close_fds keeps only the standard streams.
            peer = subprocess.Popen(command)
        except OSError:
            report(status, START_FAILED)
        release_standard_streams()
        wait_for_lifeline(lifeline, peer, status)
    finally:
        stop_group(peer)


if __name__ == "__main__":
    main()
