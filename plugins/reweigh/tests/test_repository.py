"""Checks of the repository's own files: documents read as fixed Markdown structure, code parsed with ast. Nothing here
starts a process. Each check reads exact tokens and structure, never wording (ADR 0002); whether a document's claims are
true is left to review."""

import ast
import json
import os
import shlex
import sys
import tomllib
import unicodedata
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

# Lets a single test file run on its own, as well as through discover.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import SCRIPTS, TESTS, bridge, consultation  # noqa: E402

PLUGIN = TESTS.parent
ROOT = PLUGIN.parents[1]
SUITES = (TESTS, ROOT / "dev" / "tests")
# The modules that hold every rule, and so import no shipped module but each other (ADR 0005).
PURE_MODULES = {"consultation", "thread_parsing"}
ADR_SECTIONS = ["Context", "Decision", "Consequences", "Reconsider if"]
ADR_STATUSES = {"Proposed", "Accepted"}  # a replaced decision is deleted, not marked: history is in git
PINNED_LINK = "https://github.com/mtrzes/reweigh/blob/"
BRIDGE_SCRIPT = "scripts/bridge.py"  # relative to the plugin folder
# Characters that end a URL in Markdown, JSON or code: a link's closing bracket, a quote, a space, an anchor.
URL_END = set(')]>"\'` \t\n#')


def imported_modules(path):
    """The top-level names of every absolute import in a script, including those inside functions."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module.partition(".")[0])
    return names


def test_files():
    """Every test module and fixture module of both suites."""
    return sorted(path for suite in SUITES for path in suite.glob("*.py"))


def plugin_version():
    return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def markdown_files():
    """The repository's Markdown, without dot-directories (git, the venv, other worktrees) or the local backlog."""
    found = []
    for directory, folders, files in os.walk(ROOT):
        folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
        found += [Path(directory, name) for name in files if name.endswith(".md") and name != "TODO.md"]
    return sorted(found)


def prose_lines(text):
    """(line number, line) outside fenced code blocks, with inline code spans removed."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            parts = line.split("`")
            yield number, "".join(parts[0::2])


def headings(text):
    """The text of every Markdown heading outside fenced code, code spans kept."""
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            marks, _, title = line.partition(" ")
            if set(marks) == {"#"}:
                yield title.strip()


def handled(heading):
    """Whether github_anchors computes this heading's anchor as GitHub does: no markup outside code spans."""
    outside = "".join(heading.split("`")[0::2])
    return heading.count("`") % 2 == 0 and not set(outside) & set("[]<>*_\\&")


def github_anchors(text):
    """GitHub's heading anchors: lower case, letters, marks, numbers, '_', '-' and spaces kept, spaces made hyphens,
    and a repeated anchor numbered -1, -2 and so on."""
    seen, anchors = Counter(), set()
    for heading in headings(text):
        kept = "".join(character for character in heading.lower()
                       if unicodedata.category(character)[0] in "LMN" or unicodedata.category(character) == "Pc"
                       or character in " -")
        anchor = kept.replace(" ", "-")
        anchors.add(anchor if not seen[anchor] else f"{anchor}-{seen[anchor]}")
        seen[anchor] += 1
    return anchors


def link_targets(line):
    """The targets of the inline links and images on a line: what stands between '](' and the next ')' or space."""
    targets, start = [], line.find("](")
    while start != -1:
        rest = line[start + 2:]
        end = min((index for index in (rest.find(")"), rest.find(" ")) if index != -1), default=len(rest))
        targets.append(rest[:end])
        start = line.find("](", start + 2)
    return targets


def fenced_commands(text):
    """The lines of the fenced code blocks, each backslash-continued command joined into one."""
    commands, fenced, pending = [], False, ""
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced, pending = not fenced, ""
        elif fenced:
            pending += line.removesuffix("\\")
            if not line.endswith("\\"):
                commands.append(pending)
                pending = ""
    return commands


def bridge_launches(command):
    """The arguments after each `uv` in a command that runs the bridge script: `uv … run … --script …/bridge.py`."""
    if "uv" not in command.split():
        return []
    tokens = shlex.split(command)
    launches = []
    for index, token in enumerate(tokens):
        arguments = tokens[index + 1:]
        if (token == "uv" and "run" in arguments and "--script" in arguments[:-1]
                and arguments[arguments.index("--script") + 1].endswith("/" + BRIDGE_SCRIPT)):
            launches.append(arguments)
    return launches


class ReleaseTests(unittest.TestCase):
    def test_both_manifests_and_the_newest_release_in_the_changelog_name_one_version(self):
        versions = {json.loads((PLUGIN / folder / "plugin.json").read_text(encoding="utf-8"))["version"]
                    for folder in (".claude-plugin", ".codex-plugin")}
        headings_ = [line for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
                     if line.startswith("## ")]
        # Changes waiting for a release sit under "## Unreleased" above it; the release renames that heading.
        if headings_[:1] == ["## Unreleased"]:
            headings_ = headings_[1:]
        self.assertTrue(headings_, "CHANGELOG.md has no release heading.")
        self.assertEqual(len(versions), 1, versions)
        # The whole heading, so trailing text cannot hide a mismatch.
        self.assertEqual(headings_[0], f"## {versions.pop()}")

    def test_the_readmes_carry_the_version_only_in_pinned_links(self):
        # A version written into prose goes stale at the next release; links pinned to the release tag are checked
        # against the manifest instead.
        version = plugin_version()
        for readme in (ROOT / "README.md", PLUGIN / "README.md"):
            with self.subTest(readme=readme.relative_to(ROOT)):
                text = readme.read_text(encoding="utf-8").replace(f"/blob/v{version}/", "/blob/<pinned>/")
                self.assertNotIn(version, text)


class CodexPluginTests(unittest.TestCase):
    def test_the_codex_plugin_forwards_the_variables_its_claude_server_reads(self):
        # Codex passes an MCP server only the variables it names; the nesting mark must reach the bridge it starts.
        server = json.loads((PLUGIN / "codex-mcp.json").read_text(encoding="utf-8"))
        forwarded = set(server["mcpServers"]["reweigh"]["env_vars"])
        for name in ("REWEIGH_ACTIVE", "REWEIGH_THREADS", "REWEIGH_TIMEOUT_SECONDS",
                     "REWEIGH_STATE_DIR", "REWEIGH_CLAUDE_BIN", "REWEIGH_SUBAGENTS",
                     "REWEIGH_CLAUDE_MODEL",
                     "REWEIGH_CLAUDE_REASONING_EFFORT", "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR"):
            with self.subTest(name=name):
                self.assertIn(name, forwarded)

    def test_the_registered_tool_timeout_outlasts_a_pinned_consultation_at_the_longest_timeout(self):
        # Past the peer's timeout a blocking call can spend a pinned checkout's preparation and removal, a guard's stop
        # after each of the three deadlines, and the .git probe; a host that drops the call loses a finished reply.
        stop = bridge.peer_guard.GRACE_SECONDS + bridge.GUARD_MARGIN_SECONDS
        longest = (consultation.MAX_TIMEOUT_SECONDS + bridge.CHECKOUT_PREPARATION_SECONDS
                   + bridge.CHECKOUT_REMOVAL_SECONDS + 3 * stop + bridge.GIT_ENTRY_PROBE_SECONDS)
        seconds = json.loads((PLUGIN / "codex-mcp.json").read_text(encoding="utf-8"))["mcpServers"]["reweigh"][
            "tool_timeout_sec"]
        self.assertGreater(seconds, longest)
        readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
        for setting in (f"`tool_timeout_sec = {seconds}`", f"`MCP_TOOL_TIMEOUT={seconds * 1000}`"):
            with self.subTest(setting=setting):
                self.assertIn(setting, readme)


class PackagingTests(unittest.TestCase):
    def test_the_marketplaces_plugins_and_mcp_servers_share_one_name(self):
        # A rename that misses one of these fails here, not at install: the MCP key names every tool's prefix. The
        # server's handshake name is checked against the plugin folder in test_contracts, where the server runs.
        def load(*parts):
            return json.loads(ROOT.joinpath(*parts).read_text(encoding="utf-8"))

        def only_server(config):
            [name] = config["mcpServers"]
            return name

        claude_market = load(".claude-plugin", "marketplace.json")
        codex_market = load(".agents", "plugins", "marketplace.json")
        codex_plugin = load("plugins", PLUGIN.name, ".codex-plugin", "plugin.json")
        names = {
            "plugin folder": PLUGIN.name,
            "Claude marketplace": claude_market["name"],
            "Claude marketplace entry": claude_market["plugins"][0]["name"],
            "Codex marketplace": codex_market["name"],
            "Codex marketplace entry": codex_market["plugins"][0]["name"],
            "Claude plugin": load("plugins", PLUGIN.name, ".claude-plugin", "plugin.json")["name"],
            "Codex plugin": codex_plugin["name"],
            "Codex display name": codex_plugin["interface"]["displayName"],
            "Claude MCP server": only_server(load("plugins", PLUGIN.name, ".mcp.json")),
            # The file the Codex manifest points at, so a dangling pointer fails too.
            "Codex MCP server": only_server(load("plugins", PLUGIN.name, codex_plugin["mcpServers"])),
        }
        self.assertEqual(len(set(names.values())), 1, names)
        self.assertEqual(claude_market["plugins"][0]["source"], f"./plugins/{PLUGIN.name}")
        self.assertEqual(codex_market["plugins"][0]["source"]["path"], f"./plugins/{PLUGIN.name}")


class ScriptImportTests(unittest.TestCase):
    """What the shipped scripts import, read from their source: the packages they declare and the way they depend."""

    def setUp(self):
        self.imports = {path.stem: imported_modules(path) for path in SCRIPTS.glob("*.py")}

    def test_the_scripts_declare_every_package_they_import(self):
        # A package installed only because another one needs it can vanish with that package's next release.
        lock = tomllib.loads((SCRIPTS / "bridge.py.lock").read_text(encoding="utf-8"))
        declared = {requirement["name"] for requirement in lock["manifest"]["requirements"]}
        external = set().union(*self.imports.values()) - set(sys.stdlib_module_names) - set(self.imports)
        self.assertEqual(external, declared)

    def test_imports_point_from_the_adapters_to_the_rules(self):
        local = set(self.imports)
        self.assertLessEqual(PURE_MODULES, local)
        for module in PURE_MODULES:
            with self.subTest(module=module):
                self.assertLessEqual(self.imports[module] & local, PURE_MODULES)
        # The guard outlives the bridge's failures and runs under python -I: the standard library only.
        self.assertLessEqual(self.imports["peer_guard"], set(sys.stdlib_module_names))
        # bridge.py is the entry point: jobs reaches it only by starting `bridge.py run-job` as a separate process.
        self.assertEqual({name for name, imports in self.imports.items() if "bridge" in imports}, set())


class TestImportTests(unittest.TestCase):
    def test_no_test_module_imports_another(self):
        # Shared fixtures live in each suite's support module. dev/tests also imports the plugin suite's support,
        # which is the one edge between the suites; support modules are not in this set.
        test_modules = {path.stem for path in test_files() if path.stem.startswith("test_")}
        for path in test_files():
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.assertEqual(imported_modules(path) & test_modules, set())


class TestSourceTests(unittest.TestCase):
    """The test code itself, parsed: shapes that make a test silently check nothing."""

    def test_no_class_defines_a_test_method_twice(self):
        # Python keeps the last definition of a name, so the earlier test never runs.
        for path in test_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ClassDef):
                    names = Counter(item.name for item in node.body
                                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
                    with self.subTest(test_class=f"{path.relative_to(ROOT)}:{node.lineno} {node.name}"):
                        self.assertEqual([name for name, count in names.items() if count > 1], [])

    def test_no_patch_creates_the_attribute_it_replaces(self):
        # patch(create=True) succeeds on a name that does not exist, so after a rename the test patches nothing and
        # still passes. Without it, a missing name raises AttributeError, which shows the target is wrong.
        found = []
        for path in test_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                patching = (isinstance(function, ast.Name) and function.id == "patch") or (
                    isinstance(function, ast.Attribute) and (
                        function.attr == "patch"
                        or (function.attr in ("object", "multiple") and isinstance(function.value, ast.Name)
                            and function.value.id == "patch")))
                creating = any(keyword.arg == "create" and isinstance(keyword.value, ast.Constant)
                               and keyword.value.value is True for keyword in node.keywords)
                if patching and creating:
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])


class AdrTests(unittest.TestCase):
    def setUp(self):
        self.adrs = sorted((ROOT / "docs" / "adr").glob("*.md"))
        self.assertTrue(self.adrs)

    def test_every_adr_has_the_fields_and_sections_in_order(self):
        for path in self.adrs:
            with self.subTest(adr=path.name):
                lines = path.read_text(encoding="utf-8").splitlines()
                number = path.name.partition("-")[0]
                self.assertTrue(lines[0].startswith(f"# ADR-{number}: "), lines[0])
                fields = [line.partition(":")[0] for line in lines if line.startswith("- ") and ":" in line
                          and line.partition(":")[0] in ("- Status", "- Enforced by")]
                self.assertEqual(fields[:2], ["- Status", "- Enforced by"])
                status = next(line for line in lines if line.startswith("- Status:")).partition(":")[2].strip()
                self.assertIn(status, ADR_STATUSES)
                sections = [line[3:] for _, line in prose_lines("\n".join(lines)) if line.startswith("## ")]
                self.assertEqual(sections, ADR_SECTIONS)

    def test_every_test_an_adr_names_exists_once(self):
        defined = Counter()
        for path in test_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    defined[node.name] += 1
        for path in self.adrs:
            lines = path.read_text(encoding="utf-8").splitlines()
            start = next(index for index, line in enumerate(lines) if line.startswith("- Enforced by:"))
            field = [lines[start]]
            for line in lines[start + 1:]:
                if not line.startswith("  "):  # the field ends where its indented continuation lines do
                    break
                field.append(line)
            spans = " ".join(field).split("`")[1::2]
            for name in (span for span in spans if span.startswith("test_") and span.isidentifier()):
                with self.subTest(adr=path.name, test=name):
                    self.assertEqual(defined[name], 1, "missing" if not defined[name] else "defined more than once")


class GuideTests(unittest.TestCase):
    """HOW-IT-WORKS.md's two inventories, read as tables: a row per tool and per job state, nothing else."""

    def setUp(self):
        self.lines = (PLUGIN / "HOW-IT-WORKS.md").read_text(encoding="utf-8").splitlines()

    def first_cells(self, start):
        """The first cell of each row of the table that begins at or after start, each a single code span."""
        while not self.lines[start].startswith("|"):
            start += 1
        rows = []
        for line in self.lines[start + 2:]:  # past the header and its divider
            if not line.startswith("|"):
                break
            cell = line.split("|")[1].strip()
            self.assertTrue(cell.startswith("`") and cell.endswith("`") and cell.count("`") == 2, line)
            rows.append(cell[1:-1])
        return rows

    def test_the_tools_table_lists_each_tool_the_server_offers_once(self):
        schemas = json.loads((TESTS / "fixtures" / "mcp_input_schemas.json").read_text(encoding="utf-8"))
        tools = self.first_cells(self.lines.index("## Tools"))
        self.assertEqual(sorted(tools), sorted(schemas))

    def test_the_job_state_table_lists_each_state_once(self):
        states = {*consultation.FINISHED_JOB_STATES, *consultation.UNFINISHED_JOB_STATES, "cancelling"}
        self.assertEqual(sorted(self.first_cells(self.lines.index("| State | Meaning |"))), sorted(states))


class LinkTests(unittest.TestCase):
    def test_relative_links_reach_a_file_and_a_heading_that_exist(self):
        for path in markdown_files():
            for number, line in prose_lines(path.read_text(encoding="utf-8")):
                for target in link_targets(line):
                    if target.startswith(("http://", "https://", "mailto:")):
                        continue
                    with self.subTest(link=f"{path.relative_to(ROOT)}:{number} {target}"):
                        location, _, anchor = target.partition("#")
                        linked = (path.parent / unquote(location)).resolve() if location else path
                        self.assertTrue(linked.exists(), "no such file")
                        if anchor:
                            self.assertEqual(linked.suffix, ".md", "an anchor into a file that has no headings")
                            text = linked.read_text(encoding="utf-8")
                            unhandled = [heading for heading in headings(text) if not handled(heading)]
                            self.assertEqual(unhandled, [], "headings whose GitHub anchor this check cannot compute")
                            self.assertIn(anchor, github_anchors(text))

    def test_shipped_links_to_the_repository_pin_this_release_and_reach_a_file(self):
        # A user's installed plugin reads its links from the release they installed, not from main. The tests are the
        # checks themselves, not something a user reads.
        version = plugin_version()
        for path in sorted(PLUGIN.rglob("*")):
            if not path.is_file() or TESTS in path.parents:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            start = text.find(PINNED_LINK)
            while start != -1:
                rest = text[start + len(PINNED_LINK):]
                ref, _, tail = rest.partition("/")
                linked = tail[:next((index for index, character in enumerate(tail) if character in URL_END),
                                    len(tail))]
                with self.subTest(link=f"{path.relative_to(ROOT)}: {ref}/{linked}"):
                    self.assertEqual(ref, f"v{version}")
                    self.assertTrue((ROOT / linked).exists(), "no such file")
                start = text.find(PINNED_LINK, start + 1)


class LauncherTests(unittest.TestCase):
    """Every way of starting the bridge points uv at the plugin folder, so uv never reads a project, a configuration
    or a Python environment from the folder the host starts it in, and installs only the locked dependencies."""

    def assert_launch(self, arguments, directory=None):
        self.assertEqual(arguments[0:1] + arguments[2:3], ["--directory", "run"], arguments)
        directory = directory or arguments[1]
        self.assertEqual(arguments[1], directory)
        options = arguments[3:arguments.index("--script")]
        self.assertIn("--locked", options)
        self.assertIn("--no-config", options)
        self.assertEqual(arguments[arguments.index("--script") + 1], f"{directory}/{BRIDGE_SCRIPT}")

    def test_the_plugin_configurations_launch_uv_from_the_plugin_folder(self):
        # Claude Code expands CLAUDE_PLUGIN_ROOT and sets no working directory; Codex resolves "." from the plugin.
        for name, directory, cwd in ((".mcp.json", "${CLAUDE_PLUGIN_ROOT}", None), ("codex-mcp.json", ".", ".")):
            with self.subTest(configuration=name):
                server = json.loads((PLUGIN / name).read_text(encoding="utf-8"))["mcpServers"]["reweigh"]
                self.assertEqual(server["command"], "uv")
                self.assert_launch(server["args"], directory)
                self.assertEqual(server.get("cwd"), cwd)

    def test_the_documented_launch_commands_launch_uv_from_the_plugin_folder(self):
        found = 0
        for path in markdown_files():
            for command in fenced_commands(path.read_text(encoding="utf-8")):
                for arguments in bridge_launches(command):
                    found += 1
                    with self.subTest(command=f"{path.relative_to(ROOT)}: {command.strip()[:80]}"):
                        self.assert_launch(arguments)
        self.assertGreater(found, 0, "no documented command launches the bridge, so nothing was checked")


if __name__ == "__main__":
    unittest.main()
