"""Shared fixtures for the integration tests: an isolated environment, a stand-in peer CLI, synthetic thread histories
and waits bounded by a deadline. It needs no dependency beyond the standard library; test_core.py does not use it."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
BRIDGE_PATH = SCRIPTS / "bridge.py"
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402  (the bridge's own modules, found through its script directory)
import consultation  # noqa: E402

DEADLINE_SECONDS = 30
POLL_SECONDS = 0.01
PRIVATE_MARKERS = ("SUBAGENT TRAFFIC", "TOOL OUTPUT", "PRIVATE REASONING", "USER PROMPT")
# Inherited settings that would change what a test exercises.
UNSET = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "XDG_STATE_HOME", bridge.ACTIVE_ENV,
         "PEER_BRIDGE_CODEX_MODEL", "PEER_BRIDGE_CODEX_REASONING_EFFORT", "PEER_BRIDGE_TIMEOUT_SECONDS",
         "PEER_BRIDGE_THREADS")
GIT_ISOLATION = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_IDENTITY = ("-c", "user.name=Bridge Tests", "-c", "user.email=tests@example.invalid", "-c", "commit.gpgsign=false",
                "-c", "init.defaultBranch=main")


def isolate(test, root, **variables):
    """Point every home, state and configuration lookup of this process and its children inside root."""
    environment = {
        "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-home"), "PEER_BRIDGE_STATE_DIR": str(root / "state"),
        **GIT_ISOLATION, **variables,
    }
    patcher = patch.dict(os.environ, environment)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in set(UNSET) - set(environment):
        os.environ.pop(name, None)
    system_config = patch.object(consultation, "CODEX_SYSTEM_CONFIG", root / "system-codex-config.toml")
    system_config.start()
    test.addCleanup(system_config.stop)


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *GIT_IDENTITY, *arguments], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ISOLATION}).stdout.strip()


def git_repository(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet")
    git(path, "commit", "--quiet", "--allow-empty", "-m", "start")
    return path


# Waits


def eventually(predicate, message, timeout=DEADLINE_SECONDS):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(POLL_SECONDS)


async def eventually_async(predicate, message, timeout=DEADLINE_SECONDS):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(POLL_SECONDS)


def module_view(module, **replacements):
    """A module as one importer sees it: these replacements, and the module for everything else. Patching the
    importer's name for the module with it, unlike patching the module itself, reaches no other importer, including
    one that binds a name from the module while the patch is active."""
    view = ModuleType(module.__name__)
    view.__getattr__ = lambda name: getattr(module, name)
    vars(view).update(replacements)
    return view


def running(pid):
    """Whether a process exists and is not a zombie, which is gone for every purpose here."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def read_pid(path):
    """The PID in a file its process may still be writing, or None."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    return int(text) if text.strip() else None


# The stand-in peer CLI

PEER_SOURCE = textwrap.dedent('''\
    import json, os, signal, subprocess, sys, time, uuid
    from pathlib import Path

    arguments = sys.argv[1:]
    signed_in = os.environ.get("SIMULATED_SIGNED_IN", "1") == "1"
    if arguments[:2] in (["auth", "status"], ["login", "status"]) and os.environ.get("SIMULATED_PROBE_HANGS"):
        # A sign-in probe that starts a SIGTERM-ignoring helper, records its PID, then never answers.
        folder = os.environ["SIMULATED_PEER_PID_DIR"]
        subprocess.Popen([sys.executable, "-c", "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                          f"open(os.path.join({folder!r}, 'probe-child.pid.tmp'), 'w').write(str(os.getpid())); "
                          f"os.replace(os.path.join({folder!r}, 'probe-child.pid.tmp'), "
                          f"os.path.join({folder!r}, 'probe-child.pid')); time.sleep(60)"])
        time.sleep(60)
    if arguments[:2] == ["auth", "status"]:
        print(json.dumps({"loggedIn": signed_in, "authMethod": "claude.ai"}))
        sys.exit(0)
    if arguments[:2] == ["login", "status"]:
        print("PRIVATE BACKEND CONTEXT")
        sys.exit(0 if signed_in else 1)
    provider = "claude" if "--print" in arguments else "codex"
    mode = os.environ.get("SIMULATED_PEER_MODE", "success")
    pids = Path(os.environ["SIMULATED_PEER_PID_DIR"])
    if mode == "early_exit":
        print("PRIVATE BACKEND CONTEXT 401 Unauthorized", file=sys.stderr)
        sys.exit(int(os.environ["SIMULATED_EXIT_STATUS"]))
    prompt = sys.stdin.read()
    resumed = None
    for flag in ("--resume", "resume"):
        if flag in arguments:
            resumed = arguments[arguments.index(flag) + 1]
    session = resumed or str(uuid.uuid4())
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:  # no git on a PATH a test emptied
        head = None
    with open(os.environ["SIMULATED_PEER_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({
            "provider": provider, "executable": sys.argv[0], "arguments": arguments, "prompt": prompt,
            "cwd": os.getcwd(), "session": session, "active": os.environ.get("PEER_BRIDGE_ACTIVE"),
            "claudecode_present": "CLAUDECODE" in os.environ,
            "marker": Path("marker.txt").read_text() if Path("marker.txt").exists() else None,
            "git_location": sorted(name for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
                                   if name in os.environ),
            "head": head,
        }) + "\\n")
    if mode == "failure":
        print("PRIVATE BACKEND CONTEXT", file=sys.stderr)
        print(prompt)
        sys.exit(7)
    if mode == "known_failure":
        print("PRIVATE BACKEND CONTEXT", file=sys.stderr)
        print(os.environ.get("SIMULATED_FAILURE_TEXT", ""), file=sys.stderr)
        print(prompt)
        sys.exit(1)
    if mode == "api_error":
        print(json.dumps({"subtype": "success", "is_error": True, "session_id": session,
                          "api_error_status": int(os.environ["SIMULATED_API_STATUS"]),
                          "result": "PRIVATE BACKEND CONTEXT"}))
        sys.exit(1)
    if mode == "error_result":
        print(json.dumps({"subtype": "success", "is_error": True, "session_id": session,
                          "result": "PRIVATE BACKEND CONTEXT Not logged in · Please run /login"}))
        sys.exit(0)

    def record_pid():
        (pids / "parent.pid.tmp").write_text(str(os.getpid()))
        os.replace(pids / "parent.pid.tmp", pids / "parent.pid")

    def spawn_descendant():
        """Start a child that ignores SIGTERM and holds the inherited stdout and stderr open."""
        record_pid()
        child = ("import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "folder = Path(os.environ['SIMULATED_PEER_PID_DIR']); "
                 "(folder / 'child.pid.tmp').write_text(str(os.getpid())); "
                 "os.replace(folder / 'child.pid.tmp', folder / 'child.pid'); time.sleep(60)")
        subprocess.Popen([sys.executable, "-c", child])
        deadline = time.monotonic() + 30
        while not (pids / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    if mode == "hang":
        time.sleep(60)
    if mode == "spawn_hang":
        if os.environ.get("SIMULATED_PEER_IGNORES_SIGTERM"):
            # The guard's SIGTERM starts its grace period; the marker tells the test it has begun.
            signal.signal(signal.SIGTERM, lambda *_: (pids / "terminated").touch())
        spawn_descendant()
        time.sleep(60)
    if mode == "flood":
        record_pid()
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
        time.sleep(60)
    if mode == "lingering_descendant":
        spawn_descendant()
    if mode == "changed_session":
        session = str(uuid.uuid4())
    reply = provider + " consulted: " + prompt.split("Question:\\n", 1)[-1]
    if provider == "claude":
        print(json.dumps({"subtype": "success", "is_error": False, "session_id": session, "result": reply}))
    else:
        for event in ({"type": "thread.started", "thread_id": session},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": reply}},
                      {"type": "turn.completed", "usage": {}}):
            print(json.dumps(event), flush=True)
''')


class RecordingProbe:
    """The second adapter behind failure diagnosis: answers each observation from answers, None when it has none, and
    records what was asked. Pass it to Bridge as probe=RecordingProbe(answers)."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, provider, directory, scope, peer, source):
        return self

    async def observe(self, name, session):
        self.asked.append((name, session))
        return self.answers.get(name)


class BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    """A bridge whose peers are the stand-in CLI, with every home, state and git setting inside a temporary root."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-bridge-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project, self.other_project = self.root / "project", self.root / "other-project"
        for directory in (self.project, self.other_project):
            directory.mkdir()
        self.state, self.log = self.root / "state", self.root / "calls.jsonl"
        self.codex_home, self.claude_home = self.root / "codex-home", self.root / "claude-home"
        self.peer = self.root / "peer executable"
        self.peer.write_text(f"#!{sys.executable}\n{PEER_SOURCE}", encoding="utf-8")
        self.peer.chmod(0o700)
        isolate(self, self.root, **{
            "PEER_BRIDGE_CLAUDE_BIN": str(self.peer), "PEER_BRIDGE_CODEX_BIN": str(self.peer),
            "PEER_BRIDGE_TIMEOUT_SECONDS": str(DEADLINE_SECONDS), "SIMULATED_PEER_MODE": "success",
            "SIMULATED_PEER_LOG": str(self.log), "SIMULATED_PEER_PID_DIR": str(self.root),
            "CLAUDECODE": "test-parent-marker", "PEER_BRIDGE_THREADS": "1",
        })
        self.store = bridge.SessionStore(self.state)
        self.client = bridge.Bridge(store=self.store, timeout=DEADLINE_SECONDS)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    async def consult(self, provider="claude", **arguments):
        request = {"prompt": "Explain the query.", "working_directory": str(self.project), **arguments}
        return await self.client.consult(provider, **request)

    async def wait_for_child(self):
        await eventually_async(lambda: (self.root / "child.pid").exists(), "The simulated descendant did not start.")

    async def assert_processes_stopped(self, names=("parent.pid", "child.pid")):
        pids = [read_pid(self.root / name) for name in names]
        self.assertNotIn(None, pids)
        try:
            await eventually_async(lambda: not any(running(pid) for pid in pids),
                                   "A peer process or its descendant outlived the consultation.")
        finally:
            for pid in pids:
                if running(pid):
                    os.kill(pid, 9)


# Synthetic thread histories


def codex_event(kind, turn_id, **payload):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn_id, **payload}}


def codex_turn(turn_id, *texts, outcome="completed"):
    """A Codex turn with agent messages and the noise a real rollout carries around them."""
    events = [codex_event("task_started", turn_id),
              {"type": "response_item", "payload": {"type": "reasoning", "summary": "PRIVATE REASONING"}},
              {"type": "response_item", "payload": {"type": "agent_message", "author": "/root/helper",
                                                    "recipient": "/root", "content": "SUBAGENT TRAFFIC"}},
              codex_event("item_completed", turn_id, item={"type": "CommandExecution", "output": "TOOL OUTPUT"})]
    for index, text in enumerate(texts):
        phase = "final_answer" if index == len(texts) - 1 else "commentary"
        events.append(codex_event("item_completed", turn_id, item={
            "type": "AgentMessage", "phase": phase, "content": [{"type": "Text", "text": text}],
        }))
    if outcome == "completed":
        events.append(codex_event("task_complete", turn_id, last_agent_message=texts[-1] if texts else None))
    elif outcome == "failed":
        events.append(codex_event("task_complete", turn_id, last_agent_message=None, error={"message": "TOOL OUTPUT"}))
    elif outcome == "interrupted":
        events.append(codex_event("turn_aborted", turn_id, reason="interrupted"))
    return events


def codex_thread(home, cwd, *turns, name=None, source="vscode", suffix=".jsonl", meta=None, mtime=None, tail="",
                 frames=1):
    identifier = str(uuid.uuid4())
    folder = home / "sessions" / "2026" / "10" / "13"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-10-13T10-00-00-{identifier}{suffix}"
    meta = {"id": identifier, "cwd": str(cwd), "source": source, **(meta or {})}
    records = [{"type": "session_meta", "payload": meta}]
    records += [event for turn in turns for event in turn]
    content = ("".join(json.dumps(record) + "\n" for record in records) + tail).encode("utf-8")
    if suffix == ".jsonl.zst":
        import zstandard

        # Codex's compressed rollouts are zstd; several frames in one file must read as one stream.
        size = -(-len(content) // frames)
        compressor = zstandard.ZstdCompressor()
        content = b"".join(compressor.compress(content[i:i + size]) for i in range(0, len(content), size))
    path.write_bytes(content)
    if name:
        with (home / "session_index.jsonl").open("a", encoding="utf-8") as index:
            entry = {"id": identifier, "thread_name": name, "updated_at": "2026-10-13T10:00:00Z"}
            index.write(json.dumps(entry) + "\n")
    if mtime:
        os.utime(path, (mtime, mtime))
    return identifier, path


def claude_prompt(text="USER PROMPT"):
    return {"type": "user", "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": text}}


def claude_reply(text, stop_reason="end_turn"):
    content = [{"type": "thinking", "thinking": "PRIVATE REASONING"}, {"type": "text", "text": text}]
    if stop_reason == "tool_use":
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": "TOOL OUTPUT"}})
    return {"type": "assistant", "uuid": str(uuid.uuid4()), "message": {"content": content, "stop_reason": stop_reason}}


def claude_tool_result():
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "TOOL OUTPUT"}]}}


def claude_session(home, cwd, *entries, title=None, entrypoint="claude-desktop", project="project", mtime=None):
    identifier = str(uuid.uuid4())
    folder = home / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    base = {"cwd": str(cwd), "entrypoint": entrypoint, "sessionId": identifier, "isSidechain": False}
    records = [{**base, **entry} for entry in entries]
    if title:
        records.append({"type": "custom-title", "customTitle": title, "sessionId": identifier})
    path = folder / f"{identifier}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))
    return identifier, path
