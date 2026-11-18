"""Compare the consult-peer skill's steelman and double-crux modes with the plain skill.

Each run builds a fixture repository from cases/<id> (a base commit, then the change commit under review), exports
one arm's plugin at a commit, and runs Claude Code as the lead with only that plugin, read-only tools and no other
MCP server. The lead consults Codex through the bridge and ends its reply with fixed VERDICT and EVIDENCE lines.

Grading reads exact tokens only: the VERDICT value, EVIDENCE paths and line numbers, tool names and inputs in the
stream-json events, and Codex's token counts from its session files. Whether a mode fired is the lead's Read of the
mode's file. Nothing here interprets prose.

    python3 dev/evals/modes/modes_eval.py run --work DIR [--cases s1-rates,d2-retry-keyed] [--reps 1]
    python3 dev/evals/modes/modes_eval.py summary --work DIR

DIR must be outside the repository. Every run spends API or subscription quota on both CLIs.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CASES = HERE / "cases"
SERVER_TOOLS = "mcp__plugin_peer-bridge_peer-bridge"
CONSULT_TOOLS = (f"{SERVER_TOOLS}__ask_codex", f"{SERVER_TOOLS}__start_codex_consultation")
MODE_FILES = {"steelman": "skills/consult-peer/steelman.md", "double-crux": "skills/consult-peer/double-crux.md"}
# The mode each kind of case is meant to exercise.
TARGET_MODE = {"steelman": "steelman", "double-crux": "double-crux", "staged": "double-crux"}
VERDICTS = {"steelman": ("accept", "reject"), "double-crux": ("fix", "no-fix"), "staged": ("fix", "no-fix")}
UNRESOLVED = "unresolved"
MAX_EVIDENCE = 3

ASK = {
    "steelman": 'Commit HEAD in this repository proposes: "{message}". Its diff is below. Decide whether to merge it. '
    "Load the consult-peer skill, then consult Codex through peer-bridge before you decide.",
    "double-crux": 'A prior review of commit HEAD in this repository (diff below) flagged: "{claim}" Decide whether to '
    "fix it before merging. Load the consult-peer skill, then consult Codex through peer-bridge before you decide.",
    "staged": 'Earlier in this review you concluded about commit HEAD in this repository (diff below): "{claim}" '
    "I will act on that unless it is wrong. Load the consult-peer skill, then consult Codex through peer-bridge, then give me your final call.",
}
CONTRACT = (
    "Do not change any file. End your reply with these lines, exactly in this form:\n"
    "VERDICT: {first}|{second}|unresolved\n"
    "EVIDENCE: <path>:<line>\n"
    "Give one to three EVIDENCE lines, each a path relative to the repository root and the line of code that decides "
    "the call."
)


# ---- pure rules ------------------------------------------------------------------------------------------------


def build_prompt(case: dict, diff: str) -> str:
    first, second = VERDICTS[case["kind"]]
    ask = ASK[case["kind"]].format(message=case["message"], claim=case.get("claim", ""))
    return f"{ask}\n\n```diff\n{diff.rstrip()}\n```\n\n{CONTRACT.format(first=first, second=second)}"


def parse_answer(text: str, kind: str) -> dict:
    """Read the last VERDICT line and every EVIDENCE line; anything else in the reply is ignored."""
    verdict, evidence, malformed = None, [], 0
    allowed = (*VERDICTS[kind], UNRESOLVED)
    for line in text.splitlines():
        line = line.strip().strip("`*").strip()
        if line.startswith("VERDICT: "):
            value = line[len("VERDICT: "):].strip()
            verdict = value if value in allowed else None
            malformed += value not in allowed
        elif line.startswith("EVIDENCE: "):
            path, sep, number = line[len("EVIDENCE: "):].strip().rpartition(":")
            if sep and path and number.isdigit():
                evidence.append((path, int(number)))
            else:
                malformed += 1
    return {"verdict": verdict, "evidence": evidence[:MAX_EVIDENCE], "format_ok": verdict is not None and bool(evidence) and not malformed}


def relative_path(path: str, roots: list[str]) -> str:
    """Make an evidence path relative to the fixture: strip a leading ./ or one of the checkout roots."""
    if path.startswith("./"):
        return path[2:]
    for root in roots:
        if path.startswith(root.rstrip("/") + "/"):
            return path[len(root.rstrip("/")) + 1:]
    return path


def score(case: dict, answer: dict, roots: list[str]) -> dict:
    hit = any(
        relative_path(path, roots) == truth["path"] and truth["first"] <= line <= truth["last"]
        for path, line in answer["evidence"]
        for truth in case["evidence"]
    )
    return {
        "verdict": answer["verdict"],
        "correct": answer["verdict"] == case["verdict"],
        "unresolved": answer["verdict"] == UNRESOLVED,
        "evidence_hit": hit,
        "format_ok": answer["format_ok"],
    }


def scan_events(events: list[dict]) -> dict:
    """Count consultations, follow-ups, failures and mode-file reads from stream-json events."""
    calls, failed_ids, follow_ups, modes_read, conversations, skill_loaded = {}, set(), 0, set(), set(), False
    denied = []
    result = {}
    for event in events:
        if event.get("type") == "result":
            result = event
        if event.get("subtype") == "permission_denied":
            denied.append(event.get("tool_name"))
        message = event.get("message")
        # A system event carries its message as plain text, not as content blocks.
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                name, args = block.get("name"), block.get("input") or {}
                if name in CONSULT_TOOLS:
                    calls[block.get("id")] = name
                    follow_ups += bool(args.get("conversation_id"))
                if name == "Skill" and args.get("skill") == "peer-bridge:consult-peer":
                    skill_loaded = True
                if name == "Read":
                    for mode, suffix in MODE_FILES.items():
                        if str(args.get("file_path", "")).endswith("/" + suffix):
                            modes_read.add(mode)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                if block.get("is_error"):
                    failed_ids.add(block.get("tool_use_id"))
                content = block.get("content")
                texts = [c.get("text", "") for c in content] if isinstance(content, list) else [str(content or "")]
                for text in texts:
                    try:
                        conversation = json.loads(text).get("conversation_id")
                    except (ValueError, AttributeError):
                        conversation = None
                    if conversation:
                        conversations.add(conversation)
    usage = result.get("usage") or {}
    return {
        "consultations": len(calls),
        "failed": len(failed_ids),
        "follow_ups": follow_ups,
        "skill_loaded": skill_loaded,
        "modes_read": sorted(modes_read),
        "denied": denied,
        "conversations": sorted(conversations),
        "text": result.get("result") or "",
        "lead_cost_usd": result.get("total_cost_usd") or 0.0,
        "duration_ms": result.get("duration_ms") or 0,
        "turns": result.get("num_turns") or 0,
        "lead_tokens": {k: usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")},
        "is_error": bool(result.get("is_error")) or not result,
    }


def codex_cost(tokens: dict, rates: dict) -> float:
    cached = tokens.get("cached_input_tokens", 0)
    uncached = tokens.get("input_tokens", 0) - cached
    return (uncached * rates["input"] + cached * rates["cached"] + tokens.get("output_tokens", 0) * rates["output"]) / 1e6


# ---- processes and files ---------------------------------------------------------------------------------------


def git(cwd: Path, *args: str, text: bool = True) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@example.invalid",
           "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=text).stdout


def build_fixture(case_id: str, case: dict, repo: Path) -> str:
    shutil.copytree(CASES / case_id / "base", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Initial state")
    shutil.copytree(CASES / case_id / "change", repo, dirs_exist_ok=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", case["message"])
    return git(repo, "show", "--format=", "HEAD")


def export_plugin(commit: str, dest: Path) -> Path:
    if not (dest / "plugins" / "peer-bridge").is_dir():
        data = subprocess.run(["git", "archive", commit, "plugins/peer-bridge"], cwd=ROOT, check=True, capture_output=True).stdout
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(dest, filter="data")
    return dest / "plugins" / "peer-bridge"


def codex_sessions(since: float, run_dir: Path) -> tuple[dict, list[Path]]:
    """Sum Codex token counts over session files started under this run's directory."""
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    totals, files, prefix = {}, [], os.path.realpath(run_dir)
    for path in root.rglob("rollout-*.jsonl"):
        if path.stat().st_mtime < since:
            continue
        lines = path.read_text(errors="replace").splitlines()
        try:
            cwd = json.loads(lines[0]).get("payload", {}).get("cwd", "")
        except (ValueError, IndexError):
            continue
        if not os.path.realpath(cwd).startswith(prefix):
            continue
        files.append(path)
        last = {}
        for line in lines:
            try:
                payload = json.loads(line).get("payload") or {}
            except ValueError:
                continue
            if payload.get("type") == "token_count" and payload.get("info"):
                last = payload["info"].get("total_token_usage") or {}
        for key, value in last.items():
            totals[key] = totals.get(key, 0) + value
    return totals, files


def run_one(args, case_id: str, arm: str, plugin: Path, rep: int) -> dict:
    case = json.loads((CASES / case_id / "case.json").read_text())
    run_dir = Path(args.work) / "runs" / f"{case_id}.{arm}.{rep}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    state = run_dir / "state"
    state.mkdir(mode=0o700)
    repo = run_dir / "repo"
    diff = build_fixture(case_id, case, repo)
    prompt = build_prompt(case, diff)
    (run_dir / "prompt.txt").write_text(prompt)
    env = {**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "PEER_BRIDGE_STATE_DIR": str(state),
           "PEER_BRIDGE_CODEX_MODEL": args.codex_model, "PEER_BRIDGE_CODEX_REASONING_EFFORT": args.codex_effort}
    command = ["claude", "-p", "--plugin-dir", str(plugin), "--setting-sources", "project,local",
               # The mode files sit in the plugin, outside the run's cwd: dontAsk denies a Read there without this.
               "--add-dir", str(plugin),
               # Skill is a tool too: without it the lead can never load consult-peer, and both arms are the same.
               "--tools", "Read,Grep,Glob,Skill", "--allowedTools", f"{SERVER_TOOLS},Skill", "--permission-mode", "dontAsk",
               "--output-format", "stream-json", "--verbose", "--model", args.lead_model,
               "--no-session-persistence", "--max-budget-usd", str(args.budget_usd)]
    started = time.time()
    with open(run_dir / "stream.jsonl", "w") as out, open(run_dir / "stderr.txt", "w") as err:
        try:
            subprocess.run(command, cwd=repo, env=env, input=prompt, text=True, stdout=out, stderr=err, timeout=args.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    events = []
    for line in (run_dir / "stream.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    scan = scan_events(events)
    checkouts = list((state / "worktrees").glob("*")) if (state / "worktrees").is_dir() else []
    roots = [str(repo), os.path.realpath(repo), *(str(p) for p in checkouts), *(os.path.realpath(p) for p in checkouts)]
    tokens, files = codex_sessions(started - 5, run_dir)
    leaked = any(str(CASES) in text for text in [(run_dir / "stream.jsonl").read_text(), *(f.read_text(errors="replace") for f in files)])
    record = {"case": case_id, "kind": case["kind"], "arm": arm, "rep": rep, "timed_out": timed_out,
              **score(case, parse_answer(scan["text"], case["kind"]), roots),
              "target_mode_read": TARGET_MODE[case["kind"]] in scan["modes_read"],
              **{k: v for k, v in scan.items() if k != "text"},
              "codex_tokens": tokens, "codex_sessions": len(files), "truth_leak": leaked}
    (run_dir / "record.json").write_text(json.dumps(record, indent=2))
    return record


def run(args) -> None:
    work = Path(os.path.realpath(args.work))
    if str(work).startswith(str(ROOT) + "/") or work == ROOT:
        sys.exit("--work must be outside the repository")
    arms = dict(item.split("=", 1) for item in args.arms.split(","))
    plugins = {arm: export_plugin(commit, work / "arms" / arm) for arm, commit in arms.items()}
    cases = args.cases.split(",") if args.cases else sorted(p.name for p in CASES.iterdir() if (p / "case.json").exists())
    with open(work / "results.jsonl", "a") as results:
        for rep in range(1, args.reps + 1):
            for case_id in cases:
                for arm, plugin in plugins.items():
                    record = run_one(args, case_id, arm, plugin, rep)
                    results.write(json.dumps(record) + "\n")
                    results.flush()
                    print(f"{case_id} {arm} rep{rep}: verdict={record['verdict']} correct={record['correct']} "
                          f"evidence={record['evidence_hit']} mode_read={record['target_mode_read']} "
                          f"consults={record['consultations']} lead=${record['lead_cost_usd']:.2f}", flush=True)


def summary(args) -> None:
    rates = {"input": args.codex_input, "cached": args.codex_cached, "output": args.codex_output}
    records = [json.loads(line) for line in (Path(args.work) / "results.jsonl").read_text().splitlines() if line.strip()]
    for arm in sorted({r["arm"] for r in records}):
        rows = [r for r in records if r["arm"] == arm]
        n = len(rows)
        codex = sum(codex_cost(r["codex_tokens"], rates) for r in rows)
        lead = sum(r["lead_cost_usd"] for r in rows)
        print(f"{arm}: runs={n} correct={sum(r['correct'] for r in rows)} unresolved={sum(r['unresolved'] for r in rows)} "
              f"evidence_hit={sum(r['evidence_hit'] for r in rows)} format_ok={sum(r['format_ok'] for r in rows)} "
              f"skill_loaded={sum(r.get('skill_loaded', False) for r in rows)} "
              f"mode_read={sum(r['target_mode_read'] for r in rows)} consultations={sum(r['consultations'] for r in rows)} "
              f"follow_ups={sum(r['follow_ups'] for r in rows)} failed={sum(r['failed'] for r in rows)} "
              f"denied={sum(len(r.get('denied', [])) for r in rows)} leaks={sum(r['truth_leak'] for r in rows)} "
              f"lead=${lead:.2f} codex~${codex:.2f}")
        for r in rows:
            t = r["codex_tokens"]
            print(f"  {r['case']} rep{r['rep']}: verdict={r['verdict']} correct={r['correct']} evidence={r['evidence_hit']} "
                  f"modes_read={r['modes_read']} consults={r['consultations']} follow_ups={r['follow_ups']} "
                  f"turns={r['turns']} {r['duration_ms'] / 1000:.0f}s lead=${r['lead_cost_usd']:.2f} "
                  f"codex in={t.get('input_tokens', 0)} cached={t.get('cached_input_tokens', 0)} out={t.get('output_tokens', 0)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--work", required=True)
    r.add_argument("--cases", default="")
    r.add_argument("--arms", default="plain=5b51f39,modes=c0e9cba")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--lead-model", default="claude-opus-5-5")
    r.add_argument("--codex-model", default="gpt-6.2-sol")
    r.add_argument("--codex-effort", default="high")
    r.add_argument("--budget-usd", type=float, default=3.0)
    r.add_argument("--timeout", type=int, default=1500)
    s = sub.add_parser("summary")
    s.add_argument("--work", required=True)
    s.add_argument("--codex-input", type=float, default=2.0, help="$ per million uncached input tokens")
    s.add_argument("--codex-cached", type=float, default=0.10)
    s.add_argument("--codex-output", type=float, default=10.0)
    args = parser.parse_args()
    {"run": run, "summary": summary}[args.command](args)


if __name__ == "__main__":
    main()
