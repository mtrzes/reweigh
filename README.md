# peer-bridge

[![Tests](https://github.com/mtrzes/peer-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/mtrzes/peer-bridge/actions/workflows/tests.yml)

Ask Codex for a second opinion from Claude Code, or ask Claude Code from Codex. The peer only reads: it inspects the
code and returns advice to the app you asked from. You decide what to change.

Version `1.0.0-beta.2` · [Apache-2.0](LICENSE)

## Try it

In Claude Code, ask:

> Ask Codex to review the latest commit, pinned to `HEAD`. Which existing callers, if any, will behave differently?
> Cite the relevant files and lines.

In Codex, ask Claude Code the same question. A pinned review runs in a separate checkout of that commit and returns its
hash, so you can keep editing meanwhile. You can ask a follow-up in the same consultation, or run a long review in the
background and collect it later.

Already have a review open in the other app? The experimental thread reader, off unless you set
`PEER_BRIDGE_THREADS=1`, brings that thread's latest agent replies into a new consultation. It reads interactive
threads from the same Git repository. The bridge never posts into the original chat.

## What the peer can read

- **Codex** can read any file your user account can read, including files in your home directory.
- **Claude Code** can read the directory you ask it to inspect, its own temporary folder, and paths your Claude
  settings allow.

Neither peer can edit files, search the web or start another consultation. Pinning selects the checkout where
the peer starts; the read permissions above still apply. The bridge runs locally, but your question and whatever the
peer reads go to that peer's model provider, and its CLI keeps a transcript. [Security details](SECURITY.md)

## Install

You need macOS or Linux, Python 3.11 or later, [`uv`](https://docs.astral.sh/uv/), and both CLIs installed and signed
in. Install the plugin in the app you want to ask from; install it in both for both directions.

**From Claude Code to Codex**: enter this in Claude Code 2.1.275 or later.

```text
/plugin install peer-bridge --marketplace mtrzes/peer-bridge
```

If the server is missing from `/mcp`, restart Claude Code.
[Earlier Claude Code versions](plugins/peer-bridge/README.md#claude-code-to-codex)

**From Codex to Claude Code**: run these in your shell, then restart Codex.

```sh
codex plugin marketplace add mtrzes/peer-bridge
```

```sh
codex plugin add peer-bridge@peer-bridge
```

## Documentation

- [Plugin reference](plugins/peer-bridge/README.md): every tool, argument and setting, and how to run the tests.
- [How it works](plugins/peer-bridge/HOW-IT-WORKS.md): the bridge's parts and the paths a request takes.
- [Security](SECURITY.md): what the bridge enforces, what it does not, and how to report a problem.
- [Changelog](CHANGELOG.md) and [design decisions](docs/adr/).
