# peer-bridge

Lets Claude Code consult Codex, and Codex consult Claude Code, through restricted, resumable sessions, and lets each
read what the other app's agent last said in the same repository. It installs as a plugin in both apps.

- [How it works](plugins/peer-bridge/HOW-IT-WORKS.md): its shape, the paths a request takes and its scope.
- [README](plugins/peer-bridge/README.md): setup in Claude Code and Codex, every argument and setting, and the tests.
- [Security](SECURITY.md): what the bridge enforces, what it does not protect against, and how to report a problem.
- [Changelog](CHANGELOG.md) and [decisions](docs/adr/): what changed, and why the bridge is built the way it is.

Licensed under [Apache-2.0](LICENSE).
