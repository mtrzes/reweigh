"""Private files under the bridge's state directory: how they are created, read and replaced.

Every file is opened without following a symlink and without blocking, and must be a regular file; every directory
is created private, level by level. A process running as the same user can still read and rewrite these files, so
that is not a boundary these checks guard.
"""

import json
import os
import stat
import tempfile
from pathlib import Path

from consultation import BridgeError

PRIVATE = "State directory allows group or other access. Choose a dedicated private directory."
UNREADABLE = "The bridge's state files are unreadable."


def ensure_private(*directories):
    """Create each directory, in order, private; mkdir(parents=True) would give the parents default permissions."""
    directories[0].parent.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        directory.mkdir(exist_ok=True, mode=0o700)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise BridgeError(PRIVATE)


def open_regular(path, flags, mode=0o600, unreadable=UNREADABLE):
    """Open a regular file, never through a symlink and without waiting: O_NONBLOCK keeps a FIFO in its place from
    blocking the open, and the type is checked before anything else."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise BridgeError(unreadable)
    os.set_blocking(descriptor, True)
    return descriptor


def read_json(path, unreadable=UNREADABLE):
    """Parse a regular JSON file, never through a symlink; None when it does not exist."""
    try:
        descriptor = open_regular(path, os.O_RDONLY, unreadable=unreadable)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError(unreadable) from exc
    with os.fdopen(descriptor, "rb") as source:
        data = source.read()
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise BridgeError(unreadable) from exc


def write_json(path, data):
    """Replace a file atomically; os.replace swaps the name itself, never what a symlink there points to."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
