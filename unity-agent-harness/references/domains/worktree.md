# Worktree Editors on a hidden desktop

Reader: an operator who wants several Editors on one project at once, one per git worktree, without any of them
taking the owner's screen. Windows Editors driven from WSL; Unity CLI 1.0.0, Pipeline 1.0.0.

```bash
python3 scripts/unity_worktree.py create NAME [--visible] [--scene Assets/Scenes/X.unity]
python3 scripts/unity_worktree.py open NAME [--visible] [--scene PATH]
python3 scripts/unity_worktree.py close NAME
python3 scripts/unity_worktree.py list
python3 scripts/unity_worktree.py dialogs NAME
python3 scripts/unity_worktree.py press NAME DIALOG BUTTON [--visible]
python3 scripts/unity_worktree.py remove NAME [--force]
```

Run it from the main project or any of its worktrees, or pass `--project PATH` before the action.

## Settings

`unity-worktrees.json` at the main checkout root:

```json
{"prefix": "MyGame", "base_port": 7950, "names": ["wt1", "wt2"], "seed_paths": ["Captures"], "desktop": "UnityCliWorktrees"}
```

- NAME must be one of `names`. Its worktree is the folder `../<prefix>-NAME` on a new branch NAME, and its Editor serves
  Pipeline on `base_port + index of NAME`. Pick a `base_port` range that nothing else listens on.
- `seed_paths` (optional): git-ignored folders of the main checkout that a new worktree needs and a checkout lacks.
- `desktop` (optional): the hidden desktop's name; `UnityCliWorktrees` by default.
- The Editor is the installed one named by `ProjectSettings/ProjectVersion.txt`, found in `unity editors --installed`. To override it, set
  `UNITY_EDITOR` to the full Windows path of `Unity.exe`.
- A worktree Editor needs about 8 GB of free RAM; `create` refuses below that.

## What each action does

- `create` makes the worktree (`git worktree add`), pins its Pipeline port in the worktree's own
  `UserSettings/Packages/com.unity.pipeline/EditorPipelineConfig.json` (`m_Port`; per user and git-ignored, so
  nothing is committed), copies `seed_paths`, copies the main `Library` without `Library/Pipeline` (that folder holds
  the main Editor's registration; a copy makes the new Editor look like the main one), then opens the Editor.
  The main Editor must be closed, or ready (not importing or compiling), while its Library is copied; `create` checks this and refuses otherwise.
  `unity vcs git worktree add` is not used: it skips the Library seed while the source Editor runs, which is when a
  second Editor is wanted.
- `open` first pins the worktree's port in that same settings file when it is missing or holds another port (never the
  main checkout's file), then starts the worktree's Editor, waits until `ready`, then with `--scene` opens that scene. Unsaved changes in an
  open scene refuse the scene open and nothing is discarded.
- `close` refuses in two cases, and either way nothing is saved, discarded or forced; fix it in the Editor, then close
  again. Unsaved scenes: `unity close --if-saved` refuses and lists them. Unsaved assets: before closing, the tool
  asks the Editor (`script eval`) for dirty persistent objects under `Assets/` (ScriptableObjects, materials,
  prefabs, an open prefab stage) and refuses with `UNSAVED_ASSETS` listing their paths, because `close` itself exits
  without saving them.
- `list` shows each worktree with its Editor's state, PID and port from `unity status`.
- `remove` closes the Editor, then `git worktree remove`; the branch stays. Modified or untracked files in the
  worktree refuse it and are listed; `--force` removes the worktree with them, after listing them. A project that
  commits its `.meta` files and ignores `Library/`, `Temp/`, `Logs/`, `UserSettings/` and `obj/` needs no `--force`.

## Why a hidden desktop

A windowed Editor brings its main window to the front each time play starts, and Windows follows a window to the
front, even onto another virtual desktop. On a desktop of its own that is never switched to, an Editor is still a whole
windowed Editor (play, the Game view, screen captures), and it cannot take the screen. `--visible` starts one on the
owner's desktop instead, to watch or record it.

## Dialogs nobody sees

A dialog an Editor raises on the hidden desktop is seen by nobody, and it holds the Editor's main thread. While a modal
is open Pipeline 1.0 does not answer: `editor status` exits 14 (or 12 for an Editor still starting, such as one that
opened into `Enter Safe Mode?` after a compile error) and `unity status` shows the Editor `unreachable`.

`unity_session.py ready` and `recover` look for it: when the Editor does not answer they list its windows by PID (checked
against its start time) on the hidden desktop and on the owner's, and count only a modal dialog (a dialog window whose owner
is disabled; the startup splash and the "Hold on..." progress windows of an import or reload are not), and report `AWAITING_INPUT` with each dialog's `title`, `buttons` and
`desktop: hidden|visible`. Then:

```bash
python3 scripts/unity_worktree.py dialogs NAME
python3 scripts/unity_worktree.py press NAME "Enter Safe Mode?" Quit   # add --visible for a dialog with desktop: visible
```

`press` clicks one button on the Editor's one dialog with that exact title; zero or several matches are refused. Choose
the button yourself from the dialog's text: Quit for a startup Safe Mode prompt (fix the compile error, then `open`
again), and for a scene modified externally the answer that keeps the owner's work. Never pick Ignore or Enter Safe Mode
without a reason. `recover --quit-safe-mode` and its scene-modal cancel act on the visible desktop only.
