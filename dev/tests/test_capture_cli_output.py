"""The CLI recapture's pure rules: what the scrub keeps."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "dev"), str(ROOT / "plugins" / "reweigh" / "scripts")]
import capture_cli_output as capture  # noqa: E402

THREAD = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


class ScrubTests(unittest.TestCase):
    def test_only_the_reply_kinds_and_numbers_survive_and_equal_originals_stay_equal(self):
        scrub = capture.Scrubber()
        output = "\n".join(map(json.dumps, (
            {"type": "thread.started", "thread_id": THREAD},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "PEER-BRIDGE-CAPTURE-OK",
                                                "cwd": "/Users/me/private", "tokens": 3, "done": True}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "/Users/me/private"}},
        ))) + "\nnot json\n"
        self.assertEqual(scrub.json_lines(output).split("\n"), [
            '{"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000001"}',
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "PEER-BRIDGE-CAPTURE-OK", '
            '"cwd": "TEXT-1", "tokens": 3, "done": true}}',
            '{"type": "item.completed", "item": {"type": "reasoning", "text": "TEXT-1"}}',
            "TEXT-2", "",
        ])
        # The same scrubber carries the mapping on, so a resumed thread keeps the fresh one's ID.
        self.assertEqual(scrub.json_lines(json.dumps({"session_id": THREAD.upper()}) + "\n"),
                         '{"session_id": "00000000-0000-0000-0000-000000000001"}\n')

    def test_stderr_keeps_only_the_tag_the_bridge_reads(self):
        tag = "[claude-code:unrecognized_model]"
        self.assertEqual(capture.Scrubber().errors(f"warning /Users/me\n{tag} model x\n x {tag}\n"),
                         f"TEXT-1\n{tag} TEXT-2\nTEXT-3\n")

    def test_private_tokens_are_found_by_exact_text(self):
        self.assertEqual(capture.private_tokens_in('{"a": "/Users/me/x", "b": "me@host"}',
                                                   {"/Users/me", "me@host", "absent", ""}), ["/Users/me", "me@host"])


if __name__ == "__main__":
    unittest.main()
