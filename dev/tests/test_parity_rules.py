"""The parity check's pure rules: the app-server's records, the comparison and the verdict."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "dev"), str(ROOT / "plugins" / "peer-bridge" / "scripts")]
import parity_rules as p  # noqa: E402
import thread_parsing as t  # noqa: E402


class AppServerParsingTests(unittest.TestCase):
    SUMMARY = t.ThreadSummary("id-1", None, "/repo", 0.0)

    def test_app_server_summary(self):
        thread = {"id": "id-1", "source": "vscode", "cwd": "/repo", "name": "Review", "recencyAt": 60,
                  "updatedAt": 30, "forkedFromId": None}
        self.assertEqual(p.app_server_summary(thread, True), t.ThreadSummary(
            "id-1", "Review", "/repo", 60.0, archived=True, source_kind="vscode"))
        self.assertEqual(p.app_server_summary(thread | {"recencyAt": None}, False).recency_at, 30.0)
        self.assertEqual(p.app_server_summary(thread | {"forkedFromId": "p"}, False).forked_from_id, "p")
        for broken in (thread | {"source": "exec"}, thread | {"source": {"subAgent": {}}}, thread | {"cwd": None},
                       thread | {"id": 3}, "thread"):
            with self.subTest(broken=broken):
                self.assertIsNone(p.app_server_summary(broken, False))

    def test_agent_messages(self):
        entries = [
            {"turnId": "t1", "item": {"type": "userMessage", "content": []}},
            {"turnId": "t1", "item": {"type": "agentMessage", "text": "Thinking.", "phase": "commentary"}},
            {"turnId": "t1", "item": {"type": "agentMessage", "text": " "}},
            {"turnId": "t1", "item": {"type": "agentMessage", "text": 3}},
            {"turnId": "t1", "item": {"type": "agentMessage", "text": "Done.", "phase": 7}},
            "broken",
        ]
        self.assertEqual(p.app_server_agent_messages(entries), [t.AgentMessage("t1", "Thinking.", "commentary"),
                                                                t.AgentMessage("t1", "Done.", None)])

    def test_snapshot_and_parity(self):
        turns = [{"id": "t2", "status": "completed"}, {"id": "t1", "status": "completed"}]
        messages = {"t1": [t.AgentMessage("t1", "One.")], "t2": [t.AgentMessage("t2", "Two."),
                                                                  t.AgentMessage("t2", "Final.")]}
        app = p.app_server_snapshot(self.SUMMARY, turns, messages, 2)
        self.assertEqual(app.latest_turn, t.TurnState("t2", "completed"))
        self.assertEqual([message.text for message in app.messages], ["Two.", "Final."])
        same = t.ThreadSnapshot(self.SUMMARY, t.TurnState("t2", "completed"), messages["t2"])
        self.assertEqual(p.parity_differences(same, app), [])
        for sequence in (app.messages[1:], app.messages[::-1],
                         [t.AgentMessage("t2", "Two.", "commentary"), app.messages[-1]],
                         [t.AgentMessage("t1", "Two."), app.messages[-1]]):
            with self.subTest(sequence=sequence):
                changed = t.ThreadSnapshot(self.SUMMARY, app.latest_turn, sequence)
                self.assertEqual(p.parity_differences(changed, app), ["messages"])
        other = t.ThreadSnapshot(self.SUMMARY, t.TurnState("t3", "interrupted", "abandoned"),
                                 [t.AgentMessage("t3", "Else.")])
        self.assertEqual(p.parity_differences(other, app), ["latest_turn", "status", "messages"])
        for turns_, reason in (([], "No turn"), ([{"id": "t1", "status": "paused"}], "does not know")):
            with self.subTest(turns=turns_), self.assertRaisesRegex(t.ThreadUnavailable, reason):
                p.app_server_snapshot(self.SUMMARY, turns_, {}, 3)

    def test_parity_verdict(self):
        a, b, c = "codex-cli 0.167.0-alpha.4.1", "codex-cli 0.167.0", "codex-cli 0.168.0-alpha.1"

        def report(at, version, passed=True, copy="c1"):
            return {"ran_at": at, "codex_version": version, "passed": passed, "copy_id": copy}
        failed_before = "did not pass on the previous"
        cases = (
            ([], False, "No parity report"),
            ([report("1", a)], False, "also pass on the previous"),
            ([report("1", a), report("2", a)], False, "also pass on the previous"),
            ([report("1", a), report("2", b)], True, "two consecutive"),
            ([report("1", a), report("2", b, passed=False)], False, "latest parity report did not pass"),
            ([report("1", a, passed=False), report("2", b)], False, failed_before),
            ([report("1", a), report("2", b, passed=False), report("3", c)], False, failed_before),
            ([report("1", a), report("2", a, passed=False), report("3", b)], False, failed_before),
            ([report("1", a), report("2", b, passed=False), report("3", b)], True, "two consecutive"),
            ([report("2", b), report("1", a)], True, "two consecutive"),
            ([report("1", a, copy="c0"), report("2", b)], False, "also pass on the previous"),
            ([report("1", b), report("2", a)], False, "not an upgrade"),
            ([report("1", a), report("2", "codex 9")], False, "cannot be ordered"),
        )
        for reports, ready, reason in cases:
            with self.subTest(reports=reports):
                verdict = p.parity_verdict(reports)
                self.assertEqual(verdict[0], ready)
                self.assertIn(reason, verdict[1])

    def test_parity_passed(self):
        clean = {"threads_compared": 2, "missing": [], "mismatches": [], "thread_data_changes": [],
                 "other_database_changes": [], "server_requests": []}
        self.assertTrue(p.parity_passed(clean))
        for field, value in (("threads_compared", 0), ("missing", ["id"]), ("mismatches", [{"id": "id", "fields": ["status"]}]),
                             ("thread_data_changes", ["sessions/x.jsonl"]),
                             ("other_database_changes", ["logs_2.sqlite"]),
                             ("server_requests", ["item/commandExecution/requestApproval"])):
            with self.subTest(field=field):
                self.assertFalse(p.parity_passed(clean | {field: value}))

    def test_release_key_orders_by_semantic_versioning(self):
        ordered = ["codex-cli 0.167.0-alpha.2", "codex-cli 0.167.0-alpha.4.1", "codex-cli 0.167.0-alpha.10",
                   "codex-cli 0.167.0-beta", "codex-cli 0.167.0", "codex-cli 0.168.0", "codex-cli 1.0.0"]
        keys = [p.release_key(version) for version in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))
        for text in ("0.167.0", "codex-cli 0.167", "codex-cli 0.167.0 (beta)", "codex-cli 0.167.0-alpha..1",
                     "codex-cli 0.167.0-alpha.01", "codex-cli 00.167.0", "", None):
            with self.subTest(text=text):
                self.assertIsNone(p.release_key(text))


if __name__ == "__main__":
    unittest.main()
