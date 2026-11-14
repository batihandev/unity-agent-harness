---
name: unity-agent-harness
description: Use when driving a Windows Unity Editor from WSL; finding, opening, readying or recovering the exact Editor for a project or git worktree; running several Editors in git worktrees on a hidden desktop; handling a hung or unresponsive Editor or a dialog nobody can see; or running compile and test runs whose verdict must be trusted.
---

# Unity Agent Harness

Always target the intended project explicitly and treat its live command catalog as authoritative. Do not
invent command names, parameters, safety semantics, or result shapes from memory. Installing this skill never
authorizes a Unity package or manifest change.

The host workflow helpers require Python 3.10 or newer and no optional Python packages. Discover the CLI and
Editor versions for the selected host, or pass explicit executable paths and version values when discovery is
ambiguous. The verified route is a graphics-enabled Windows Editor controlled from a WSL/Linux shell.
Native Windows, Linux, and macOS routes are expected-unverified. Supported versions: Unity CLI 1.0.0,
`com.unity.pipeline` 1.0.0, Editor 6000.6.2f1, verified route WSL → Windows Editor. Do not assume other
cross-OS combinations.

Read [foundation routing and safety](references/domains/foundation.md) before choosing or invoking a route.

- Session discovery, opening, readiness, closing, restart, recovery: [session lifecycle guide](references/domains/session.md) and
  `python3 scripts/unity_session.py ACTION --project PATH`.
- Several Editors, one per git worktree, on a desktop the owner never sees, and the dialogs they raise there:
  [worktree guide](references/domains/worktree.md) and `python3 scripts/unity_worktree.py ACTION NAME`.
- Compile, connected or offline tests, console capture: `python3 scripts/unity_workflow.py` with the
  [editor](references/domains/editor.md), [test](references/domains/test.md) and [console](references/domains/console.md) guides.
- Ephemeral C# probes and compile-only checks: [probe guide](references/domains/probe.md) and
  `python3 scripts/unity_probe.py`. Require current-sequence completion and preserve its raw report.

Do not duplicate host process, identity, or session recovery logic in a command. For everything else, use Unity's
own CLI and Pipeline commands and their live catalog.
