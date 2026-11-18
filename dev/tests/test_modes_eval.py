"""The modes eval's grading rules, table-driven, with no process or file."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "dev" / "evals" / "modes")]
import modes_eval as ev  # noqa: E402

CASE = {"kind": "double-crux", "verdict": "no-fix", "evidence": [{"path": "gateway.py", "first": 16, "last": 20}]}


def tool_use(id_, name, **args):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": id_, "name": name, "input": args}]}}


def tool_result(id_, payload, is_error=False):
    block = {"type": "tool_result", "tool_use_id": id_, "is_error": is_error,
             "content": [{"type": "text", "text": json.dumps(payload)}]}
    return {"type": "user", "message": {"content": [block]}}


class ParseAnswerTest(unittest.TestCase):
    def test_reads_only_exact_lines(self):
        rows = [
            ("VERDICT: no-fix\nEVIDENCE: gateway.py:17", "no-fix", [("gateway.py", 17)], True),
            ("**VERDICT: fix**\n`EVIDENCE: a.py:3`", "fix", [("a.py", 3)], True),
            ("VERDICT: unresolved\nEVIDENCE: a.py:3", "unresolved", [("a.py", 3)], True),
            ("VERDICT: accept\nEVIDENCE: a.py:3", None, [("a.py", 3)], False),  # not a value for this kind
            ("Verdict: fix\nEVIDENCE: a.py:3", None, [("a.py", 3)], False),
            ("VERDICT: fix\nEVIDENCE: a.py", "fix", [], False),
            ("VERDICT: fix\nVERDICT: no-fix\nEVIDENCE: a.py:1", "no-fix", [("a.py", 1)], True),
            ("VERDICT: fix\n" + "EVIDENCE: a.py:1\n" * 5, "fix", [("a.py", 1)] * 3, True),
        ]
        for text, verdict, evidence, ok in rows:
            with self.subTest(text=text):
                answer = ev.parse_answer(text, "double-crux")
                self.assertEqual((answer["verdict"], answer["evidence"], answer["format_ok"]), (verdict, evidence, ok))


class ScoreTest(unittest.TestCase):
    def test_evidence_matches_path_and_range(self):
        rows = [
            ([("gateway.py", 16)], [], True),
            ([("gateway.py", 21)], [], False),
            ([("./gateway.py", 20)], [], True),
            ([("/w/state/worktrees/c1/gateway.py", 18)], ["/w/state/worktrees/c1"], True),
            ([("payments.py", 7), ("gateway.py", 19)], [], True),
            ([("other/gateway.py", 18)], [], False),
        ]
        for evidence, roots, hit in rows:
            with self.subTest(evidence=evidence):
                result = ev.score(CASE, {"verdict": "no-fix", "evidence": evidence, "format_ok": True}, roots)
                self.assertEqual(result["evidence_hit"], hit)
                self.assertTrue(result["correct"])


class ScanEventsTest(unittest.TestCase):
    def test_counts_consultations_follow_ups_failures_and_mode_reads(self):
        ask, start = ev.CONSULT_TOOLS
        events = [
            tool_use("k1", "Skill", skill="peer-bridge:consult-peer"),
            tool_use("r1", "Read", file_path="/p/plugins/peer-bridge/skills/consult-peer/double-crux.md"),
            tool_use("r2", "Read", file_path="/p/notes/double-crux.md"),
            tool_use("a1", ask, prompt="q"),
            tool_result("a1", {"conversation_id": "c1", "reply": "r"}),
            tool_use("a2", ask, prompt="q", conversation_id="c1"),
            tool_result("a2", {"error": "timeout"}, is_error=True),
            tool_use("a3", start, prompt="q"),
            {"type": "system", "subtype": "permission_denied", "tool_name": "Read", "message": "denied"},
            {"type": "result", "result": "VERDICT: fix", "total_cost_usd": 0.5, "num_turns": 4, "usage": {}},
        ]
        scan = ev.scan_events(events)
        self.assertEqual((scan["consultations"], scan["follow_ups"], scan["failed"]), (3, 1, 1))
        self.assertEqual(scan["modes_read"], ["double-crux"])
        self.assertTrue(scan["skill_loaded"])
        self.assertEqual(scan["conversations"], ["c1"])
        self.assertEqual(scan["denied"], ["Read"])
        self.assertFalse(scan["is_error"])

    def test_a_mode_read_counts_after_reply_only_once_a_peer_reply_came_before_it(self):
        ask, start = ev.CONSULT_TOOLS
        mode = "/p/plugins/peer-bridge/skills/consult-peer/double-crux.md"
        rows = [
            ([tool_use("r1", "Read", file_path=mode), tool_use("a1", ask, prompt="q"),
              tool_result("a1", {"conversation_id": "c1", "reply": "r"})], []),
            ([tool_use("a1", ask, prompt="q"), tool_result("a1", {"conversation_id": "c1", "reply": "r"}),
              tool_use("r1", "Read", file_path=mode)], ["double-crux"]),
            ([tool_use("a1", ask, prompt="q"), tool_result("a1", {"error": "timeout"}, is_error=True),
              tool_use("r1", "Read", file_path=mode)], []),
            ([tool_use("s1", start, prompt="q"), tool_result("s1", {"job_id": "j", "state": "queued"}),
              tool_use("r1", "Read", file_path=mode)], []),
            ([tool_use("s1", start, prompt="q"), tool_result("s1", {"job_id": "j", "state": "queued"}),
              tool_use("g1", ev.POLL_TOOL, job_id="j"), tool_result("g1", {"state": "completed", "reply": "r"}),
              tool_use("r1", "Read", file_path=mode)], ["double-crux"]),
        ]
        for events, after in rows:
            with self.subTest(events=[e["message"]["content"][0].get("name") or "result" for e in events]):
                self.assertEqual(ev.scan_events(events)["modes_read_after_reply"], after)

    def test_missing_result_is_an_error(self):
        self.assertTrue(ev.scan_events([])["is_error"])


class BuildPromptTest(unittest.TestCase):
    def test_a_dispute_prompt_carries_the_fact_and_the_contract(self):
        prompt = ev.build_prompt({"kind": "dispute", "message": "m", "fact": "THE-FACT"}, "+x")
        self.assertIn('"THE-FACT"', prompt)
        self.assertIn("VERDICT: fix|no-fix|unresolved", prompt)


class CodexCostTest(unittest.TestCase):
    def test_cached_input_is_part_of_input(self):
        tokens = {"input_tokens": 1_000_000, "cached_input_tokens": 600_000, "output_tokens": 10_000}
        self.assertAlmostEqual(ev.codex_cost(tokens, {"input": 10, "cached": 1, "output": 50}), 4.0 + 0.6 + 0.5)


if __name__ == "__main__":
    unittest.main()
