#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["zstandard==0.25.0"]
# ///
"""Turn a real Codex rollout or Claude Code session into a test fixture with every piece of text replaced.

The fixture keeps the records' structure: every key, every number and boolean, and the string values of the keys in
KEPT, which name kinds of things, never content. Every other string becomes TEXT-<n>, the same original always
mapping to the same replacement, so equal values stay equal (a turn's ID, its final message and the message itself).
The working directory becomes {CWD}, which the tests fill in. Read the output before committing it.

    uv run --script dev/scrub_fixture.py <session file> <fixture.jsonl>
"""

import io
import json
import sys
from pathlib import Path

KEPT = {"type", "subtype", "entrypoint", "stop_reason", "role", "phase", "source", "status", "timestamp",
        "userType", "visibility"}


def records(path):
    data = path.read_bytes()
    if path.name.endswith(".zst"):
        import zstandard
        data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True).read()
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def scrubber():
    replacements = {}

    def replace(value):
        return replacements.setdefault(value, f"TEXT-{len(replacements) + 1}")

    def scrub(value, key=None):
        if isinstance(value, dict):
            return {name: scrub(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [scrub(item, key) for item in value]
        if not isinstance(value, str):
            return value
        if key == "cwd":
            return "{CWD}"
        if key in KEPT:
            return value
        return replace(value)

    return scrub


def main():
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    scrub = scrubber()
    target.write_text("".join(json.dumps(scrub(record)) + "\n" for record in records(source)), encoding="utf-8")


if __name__ == "__main__":
    main()
