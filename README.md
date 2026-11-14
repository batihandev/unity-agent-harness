# unity-agent-harness

Run and manage Unity Editor instances for coding agents: the right Editor per project or worktree, hidden Editors, dialogs, and test runs you can trust. Tested from WSL on Windows; macOS is planned.

Unity's own CLI and its `unity-cli` skill now cover almost everything else; install that skill alongside this one.

Looking for the Unity MCP skills pack? It is on the `mcp-final` tag.

## Tested setup

WSL driving a Windows Unity Editor today (Unity CLI 1.0.0, `com.unity.pipeline` 1.0.0). macOS is planned, not yet tested.

## Install

1. Unity CLI 1.0.0 (see Unity's CLI installation instructions), then check it with `unity --version`. From WSL the
   CLI is the Windows one and exists only as `unity.exe`: use `unity.exe` wherever `unity` appears below, or
   `alias unity=unity.exe`. The helpers find the CLI themselves; `--cli PATH` points them at another one.
2. Unity's `unity-cli` skill, in the environment where your agent runs:

   ```sh
   unity skill install --list
   unity skill install <agent>
   ```

3. This skill, for every agent found on the machine (Claude Code, Codex, Cursor, Antigravity and others), with the
   installer's telemetry off; then start a new agent session:

   ```sh
   DO_NOT_TRACK=1 npx skills add batihandev/unity-agent-harness -g
   ```

   `-a <agent>` limits it to one agent; `DO_NOT_TRACK=1 npx skills update` updates it. Without npm, clone the repository
   and copy (or link) the `unity-agent-harness/` folder into your agent's skills directory:

   ```sh
   git clone https://github.com/batihandev/unity-agent-harness.git
   ```

4. In the Unity project, `com.unity.pipeline` 1.0.0 in `Packages/manifest.json`.

Python 3.10+ is needed for the helpers; they use no extra packages.

## Use

Ask the agent to use `unity-agent-harness` with your project. The helpers it runs, from the Unity project folder, with the
path to the script (`SKILLS` is the skills directory you copied the folder into):

```sh
SKILLS=~/.claude/skills   # wherever unity-agent-harness/ was copied
cd /path/to/MyUnityProject
python3 $SKILLS/unity-agent-harness/scripts/unity_session.py open --hidden --project "$PWD"   # start the Editor off the owner's screen
python3 $SKILLS/unity-agent-harness/scripts/unity_session.py ready --project "$PWD"    # wait for the exact Editor to answer; it must already run (open starts it)
python3 $SKILLS/unity-agent-harness/scripts/unity_workflow.py compile --project "$PWD" --source Assets/Scripts/Player.cs
python3 $SKILLS/unity-agent-harness/scripts/unity_workflow.py test --project "$PWD" --route connected \
  --test-class Ns.MyTests --assembly MyGame.Tests --output /tmp/tests.json
python3 $SKILLS/unity-agent-harness/scripts/unity_probe.py --project "$PWD" --source AgentScripts/Probe.cs \
  --entry Probe.Run --completion-text "PROBE_DONE" --output /tmp/probe.json
python3 $SKILLS/unity-agent-harness/scripts/unity_worktree.py create wt1               # a worktree with its own Editor
```

`--output` names a new report file (an existing one is refused). A test run needs `--test-class`, `--test-name` or
`--assembly`; `--source` adds files that decide whether a run is fresh.

- `unity_session.py open` starts the project's Editor on the visible desktop (it is normally the owner's), and
  `open --hidden` on the hidden desktop, so an agent can work without taking the screen. `ready` never opens an Editor.
- The `--assembly` name is the test project's `.asmdef` name. To see what a selection resolves to, run
  `unity_workflow.py test-plan --project "$PWD" --assembly MyGame.Tests --output /tmp/plan.json` (its `expectedTests` lists them).
- `compile --source` takes the files whose change you care about (repeat it, or use `--sources-file`). `"compiled": false`
  with `noCompilationReason: up-to-date` means nothing needed compiling: success, not a skipped check.

Worktrees are set up by `unity-worktrees.json` at the project root:

```json
{"prefix": "MyGame", "base_port": 7950, "names": ["wt1", "wt2"], "seed_paths": ["Captures"], "desktop": "UnityCliWorktrees"}
```

`seed_paths` and `desktop` are optional. `unity_worktree.py` also has `open`, `list`, `close`, `remove`, `dialogs` and
`press`. Commit the project's `.meta` files and ignore `Library/`, `Temp/`, `Logs/`, `UserSettings/` and `obj/` (a
standard Unity `.gitignore`); a fresh repo has no `.meta` files until Unity first opens it, so open it once and commit them. Otherwise Unity's new files in a worktree are untracked and `remove` needs `--force`. Details are in [`unity-agent-harness/SKILL.md`](unity-agent-harness/SKILL.md) and
[`unity-agent-harness/references/domains/`](unity-agent-harness/references/domains/).

## Tested setup

Unity CLI 1.0.0, `com.unity.pipeline` 1.0.0, Unity Editor 6000.6.2f1, WSL driving Windows Editors.

Untested elsewhere. Worktree handling and dialog detection are WSL to Windows only: they call `wslpath`, `cmd.exe`,
`powershell.exe` and `robocopy`, and hidden desktops are Windows-only. Only the strict test verdicts and session handling
may work with a native macOS, Linux or Windows setup.

## License

[MIT License](LICENSE).
