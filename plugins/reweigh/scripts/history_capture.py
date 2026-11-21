"""Capture the other app's history files: open them safely and serve only the bytes they held when opened.

Each read uses one open descriptor and never reads past the extent captured when it was opened, so what a reader
decides and what it returns come from the same bytes. A file that holds fewer bytes than that extent is refused as
changed.
"""

import io
import os
import stat
import sys
from contextlib import contextmanager

from thread_parsing import TailRecords, ThreadUnavailable, parse_line, rollout_format

# The most recent decoded content a compressed capture keeps, and so the largest tail window any capture serves.
MAX_TAIL_BYTES = 32 * 1024 * 1024
# A Codex session_meta line carries the base instructions, so the first line can be large.
MAX_FIRST_LINE_BYTES = 4 * 1024 * 1024
COMPRESSED_SLICE_BYTES = 1024
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
# A compressed rollout is refused if either stored bytes or decoded bytes exceed their budget.
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
# Bound header and index work even if nothing in the file decodes to usable metadata.
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_INDEX_LINE_BYTES = 64 * 1024
# What must stay the same while a history file is read for it to count as the same, unchanged file.
IDENTITY_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
CHANGED_WHILE_READ = "The thread's history file changed while it was read."


def checkpoint(point, *details):
    """Does nothing. Reads call it at fixed points, "header_read", "opened" and "attempted", with the file still open,
    so a test can change the file there deterministically."""


def open_history(path, root=None):
    """Open a regular history file without following symlinks below the app's configured home."""
    root = path.parent if root is None else root
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    parent = os.open(root, directory_flags)
    try:
        for part in path.relative_to(root).parts[:-1]:
            child = os.open(part, directory_flags | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ThreadUnavailable("The thread's history file is not a regular file.")
        os.set_blocking(descriptor, True)
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def zstandard_module():
    try:
        import zstandard  # declared in bridge.py's script dependencies
    except ModuleNotFoundError as exc:
        # Only the package's absence; a broken installation keeps its own error.
        if exc.name != "zstandard":
            raise
        raise ThreadUnavailable("Reading compressed Codex history needs the zstandard package.") from exc
    return zstandard


@contextmanager
def decompression_errors():
    """Report damaged compressed history as a named refusal."""
    try:
        yield
    except Exception as exc:
        # Only a zstd error is translated; zstandard is loaded by then if a compressed file was being read.
        zstandard = sys.modules.get("zstandard")
        if zstandard is not None and isinstance(exc, zstandard.ZstdError):
            raise ThreadUnavailable("The thread's compressed history file is damaged.") from exc
        raise


def first_record(line):
    if not line.endswith(b"\n"):
        raise ThreadUnavailable("The thread's history file has no complete first line.")
    return parse_line(line)


class HistoryChanged(Exception):
    """The history file holds fewer bytes than when it was opened."""


@contextmanager
def changes_refused():
    try:
        yield
    except HistoryChanged as exc:
        raise ThreadUnavailable(CHANGED_WHILE_READ) from exc


class BoundedInput:
    """Serve compressed bytes only from the captured extent or metadata budget."""

    def __init__(self, source, length):
        self.source, self.remaining = source, length

    def read(self, length):
        length = min(length, self.remaining)
        block = self.source.read(length)
        if len(block) != length:
            raise HistoryChanged
        self.remaining -= length
        return block


class PlainHistory:
    """A plain history file read through one open descriptor, only up to the size it had when it was opened."""

    def __init__(self, source, opened):
        self.source, self.end, self.recency = source, opened.st_size, opened.st_mtime

    def read_at(self, start, length):
        self.source.seek(start)
        data = self.source.read(length)
        if len(data) != length:
            raise HistoryChanged
        return data

    def lines(self, limit, *, start=0, end):
        """Yield whole lines from the given extent, skipping oversized lines and partial edges."""
        self.source.seek(start)
        dropping = start > 0
        while start < end:
            line = self.source.readline(min(limit, end - start))
            if not line:
                raise HistoryChanged
            start += len(line)
            complete = line.endswith(b"\n")
            if complete and not dropping:
                yield line
            dropping = not complete

    def head_lines(self):
        """The whole lines at the start of the file, within the metadata budget."""
        return self.lines(MAX_FIRST_LINE_BYTES, end=min(self.end, MAX_METADATA_BYTES))

    def suffix_lines(self):
        """The whole index lines at the end of the file, within the metadata budget."""
        return self.lines(MAX_INDEX_LINE_BYTES, start=max(0, self.end - MAX_METADATA_BYTES), end=self.end)

    def first_line(self):
        self.source.seek(0)
        return first_record(self.source.readline(min(MAX_FIRST_LINE_BYTES, self.end)))

    def tail(self, window):
        """Return the complete records in the last window bytes, and whether the window reached the start."""
        start = max(0, self.end - window)
        return TailRecords(self.read_at(start, self.end - start), start == 0)


class CompressedHistory:
    """A zstd rollout's first line and last content, from one decompression of one open descriptor."""

    def __init__(self, source, opened):
        if opened.st_size > MAX_COMPRESSED_BYTES:
            raise ThreadUnavailable("The thread's compressed history is too large to read.")
        self.recency = opened.st_mtime
        self.head, self.content, self.from_start = decompress_history(BoundedInput(source, opened.st_size))

    def first_line(self):
        end = self.head.find(b"\n")
        return first_record(self.head[:end + 1] if end >= 0 else self.head)

    def tail(self, window):
        if window >= len(self.content):
            return TailRecords(self.content, self.from_start)
        return TailRecords(self.content[-window:], False)


def decompress_history(raw):
    """Decompress a zstd rollout, keeping its content's first line, its last MAX_TAIL_BYTES and whether those start at
    the beginning.

    A compressed file cannot be read from its end, so the whole stream is decompressed once. The decompressor returns
    a cut-off file's partial content without an error, so the file is refused unless its last frame ends and its
    content ends with a complete record: otherwise its last surviving turn need not be the thread's latest.
    """
    limit = MAX_TAIL_BYTES
    zstandard = zstandard_module()
    head, tail, total, frame_open = bytearray(), bytearray(), 0, False
    decompressor = zstandard.ZstdDecompressor().decompressobj()
    with decompression_errors():
        while block := raw.read(1024 * 1024):
            # A zstd block holds at most 128 KiB and needs at least 3 bytes, so one call on a slice of
            # COMPRESSED_SLICE_BYTES returns a bounded amount of content however compressible the file is.
            for start in range(0, len(block), COMPRESSED_SLICE_BYTES):
                piece = block[start:start + COMPRESSED_SLICE_BYTES]
                while piece:
                    content = decompressor.decompress(piece)
                    frame_open = True
                    total += len(content)
                    if total > MAX_DECOMPRESSED_BYTES:
                        raise ThreadUnavailable("The thread's compressed history is too large to read.")
                    if len(head) < MAX_FIRST_LINE_BYTES and b"\n" not in head:
                        head += content[:MAX_FIRST_LINE_BYTES - len(head)]
                    tail += content
                    if len(tail) > limit:
                        del tail[:len(tail) - limit]
                    if decompressor.eof:
                        # Codex may write several frames; whatever follows a frame's end starts the next one.
                        piece, frame_open = decompressor.unused_data, False
                        decompressor = zstandard.ZstdDecompressor().decompressobj()
                    else:
                        piece = b""
    if frame_open:
        raise ThreadUnavailable("The thread's compressed history file is incomplete.")
    if tail and not tail.endswith(b"\n"):
        # Codex compresses a rollout it has finished writing, so its last record must be whole.
        raise ThreadUnavailable("The thread's compressed history file ends inside a record.")
    return bytes(head), bytes(tail), total <= limit


def captured(source, path, opened):
    """The capture of an open history file in its format, which the file name's exact suffix names."""
    history_type = CompressedHistory if rollout_format(path.name) == "zstd" else PlainHistory
    return history_type(source, opened)


def read_header(path, root):
    """Return the history's first record and its modification time, from a bounded prefix of one open descriptor.

    The time comes from the descriptor, so a move of the file after it was opened, such as Codex archiving it, cannot
    lose it. A compressed header is decoded only from the stored bytes within the metadata budget.
    """
    with open_history(path, root) as raw, changes_refused(), decompression_errors():
        opened = os.fstat(raw.fileno())
        if rollout_format(path.name) == "zstd":
            content = zstandard_module().ZstdDecompressor().stream_reader(
                BoundedInput(raw, min(opened.st_size, MAX_METADATA_BYTES)), read_across_frames=True, closefd=False)
            with io.BufferedReader(content) as source:
                line = source.readline(MAX_FIRST_LINE_BYTES)
        else:
            line = raw.readline(min(MAX_FIRST_LINE_BYTES, opened.st_size))
        record = first_record(line)
        checkpoint("header_read", path)
        return record, opened.st_mtime


def read_once(path, root, read):
    """Return read(history) for one capture of the file, without checking that the file stayed the same."""
    with open_history(path, root) as source, changes_refused():
        return read(captured(source, path, os.fstat(source.fileno())))


def file_identity(stat_result):
    return tuple(getattr(stat_result, name) for name in IDENTITY_FIELDS)


def path_identity(path):
    """The identity of what the path names now, or None once nothing is there. Any other error is a fault to report,
    not a sign that the file changed."""
    try:
        return file_identity(path.lstat())
    except (FileNotFoundError, NotADirectoryError):
        return None


def read_stable(root, locate, read):
    """Return read(history) for the file locate() names, opened once, if it stayed the same file throughout.

    The decision on which repository a thread belongs to and the messages returned come from the same descriptor and
    the same extent. The file must keep its identity, size and times while it is read, and its path must still name it
    afterwards. A file that changes is read once more, also when the changed attempt was refused, then refused.
    """
    for attempt in range(2):
        path = locate()
        with open_history(path, root) as source:
            opened = os.fstat(source.fileno())
            checkpoint("opened", path, attempt)
            history = failure = None
            changed = False
            try:
                history = captured(source, path, opened)
                result = read(history)
            except HistoryChanged:
                changed = True
            except ThreadUnavailable as exc:
                failure = exc
            checkpoint("attempted", path, attempt, history)
            identities = {file_identity(opened), file_identity(os.fstat(source.fileno())), path_identity(path)}
        if not changed and len(identities) == 1:
            if failure is not None:
                raise failure
            return result
    raise ThreadUnavailable(CHANGED_WHILE_READ)
