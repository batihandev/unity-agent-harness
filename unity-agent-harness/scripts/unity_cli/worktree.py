"""Several Unity Editors on one project: one git worktree per Editor, beside the main checkout, each on a hidden desktop.

Driven by `unity_worktree.py`. Settings live in `unity-worktrees.json` at the main checkout root:

    {"prefix": "MyGame", "base_port": 7950, "names": ["wt1", "wt2"], "seed_paths": ["Captures"], "desktop": "UnityCliWorktrees"}

A worktree NAME is a folder `../<prefix>-NAME` on a new branch NAME, and its Editor serves Pipeline on
`base_port + index of NAME in names`. `seed_paths` (optional) are git-ignored folders copied into a new worktree;
`desktop` (optional) names the hidden desktop.

Design choices, each measured:

- Each Editor needs its own Pipeline port. Pipeline 1.0 keeps the Editor server's settings per user, in
  `UserSettings/Packages/com.unity.pipeline/EditorPipelineConfig.json` (key `m_Port`), a git-ignored file, so a worktree
  sets its port there and nothing is committed.
- The Library copy leaves out `Library/Pipeline`, which holds the main Editor's registration (pid, port, token): a copied
  one makes the new Editor look like the main one. `unity vcs git worktree add` was not used: it skips the Library seed
  while the source Editor is running, which is exactly when a second Editor is wanted.
- A worktree Editor runs on a desktop of its own that is never shown (`hidden_desktop.ps1`): a windowed Editor brings its
  main window to the front each time play starts, and Windows follows a window to the front, even onto another virtual
  desktop. On its own desktop it is a whole windowed Editor: play, the Game view and screen captures all work. A dialog it
  raises shows there too, where nobody sees it, and while a modal is open Pipeline does not answer at all, so `dialogs`
  and `press` are how it is found and answered. `--visible` starts an Editor on the owner's own desktop instead.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from .model import SessionIdentity
from .platforms import PlatformAdapter, current_platform
from .transport import CliTransport

CONFIG_FILE = "unity-worktrees.json"
DEFAULT_DESKTOP = "UnityCliWorktrees"
PORT_FILE = "UserSettings/Packages/com.unity.pipeline/EditorPipelineConfig.json"
HIDDEN_DESKTOP = pathlib.Path(__file__).with_name("hidden_desktop.ps1")
SESSION = pathlib.Path(__file__).resolve().parents[1] / "unity_session.py"
# An idle Editor with its two import workers holds about 6-7 GB; play mode adds to that.
MIN_FREE_GB = 8


class WorktreeError(RuntimeError):
    """A refusal to act, with the reason to show."""


@dataclass(frozen=True)
class Config:
    main: pathlib.Path
    prefix: str
    base_port: int
    names: tuple[str, ...]
    seed_paths: tuple[str, ...] = ()
    desktop: str = DEFAULT_DESKTOP

    def __post_init__(self) -> None:
        # A name that resolves to the main checkout itself would aim `close`, `dialogs` and `press` at the owner's Editor.
        for name in self.names:
            if (self.main.parent / f"{self.prefix}-{name}").resolve() == self.main.resolve():
                raise WorktreeError(f"{CONFIG_FILE}: `{self.prefix}-{name}` is the main checkout's own folder; change `prefix` or the name")

    def path(self, name: str) -> pathlib.Path:
        self.index(name)
        return self.main.parent / f"{self.prefix}-{name}"

    def index(self, name: str) -> int:
        if name not in self.names:
            raise WorktreeError(f"{name!r} is not in `names` of {CONFIG_FILE}: {', '.join(self.names)}")
        return self.names.index(name)

    def port(self, name: str) -> int:
        return self.base_port + self.index(name)


def run(args: list[str], cwd: pathlib.Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def main_checkout(start: pathlib.Path) -> pathlib.Path:
    """The main checkout, whichever worktree `start` is in: worktrees share its .git."""
    out = run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"], check=False)
    if out.returncode != 0:
        raise WorktreeError(f"{start} is not inside a git repository")
    return pathlib.Path(out.stdout.strip()).parent


def load_config(start: pathlib.Path) -> Config:
    main = main_checkout(start)
    try:
        raw = json.loads((main / CONFIG_FILE).read_text(encoding="utf-8"))
        names = tuple(raw["names"])
        return Config(main, str(raw["prefix"]), int(raw["base_port"]), names,
                      tuple(raw.get("seed_paths", ())), str(raw.get("desktop") or DEFAULT_DESKTOP))
    except OSError as exc:
        raise WorktreeError(f"{main / CONFIG_FILE} is missing: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise WorktreeError(f'{main / CONFIG_FILE} must be {{"prefix": str, "base_port": int, "names": [str, ...]}}: {exc!r}') from exc


def desktop_name(project: str | pathlib.Path) -> str:
    """The hidden desktop a project's Editors run on: the config's `desktop`, else the default."""
    try:
        return load_config(pathlib.Path(project)).desktop
    except (WorktreeError, OSError):
        return DEFAULT_DESKTOP


def set_port(path: pathlib.Path, port: int) -> None:
    """Pin the worktree Editor's Pipeline port in its own per-user settings file; other keys stay as they are."""
    file = path / PORT_FILE
    try:
        data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
    except ValueError as exc:
        raise WorktreeError(f"{file} is not valid JSON ({exc}); fix or delete it, then run the command again") from None
    if not isinstance(data, dict):
        raise WorktreeError(f"{file} is not a JSON object; fix or delete it, then run the command again")
    data["m_Port"] = port
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ensure_port(config: Config, name: str, path: pathlib.Path) -> None:
    """`open` pins the worktree's port the way `create` does, when its settings file lacks it or holds another.
    Only the worktree's own file is written, never the main checkout's."""
    if path.resolve() == config.main.resolve():
        raise WorktreeError(f"{path} is the main checkout; its Editor's port is not set here")
    port = config.port(name)
    file = path / PORT_FILE
    try:
        current = json.loads(file.read_text(encoding="utf-8")).get("m_Port") if file.is_file() else None
    except (OSError, ValueError, AttributeError):
        current = None
    if current != port:
        set_port(path, port)


def open_worktree(config: Config, name: str, visible: bool, scene: str | None) -> bool:
    path = existing(config, name)
    ensure_port(config, name, path)
    return open_editor(config, path, visible, scene)


def windows(path: pathlib.Path | str) -> str:
    return run(["wslpath", "-w", str(path)]).stdout.strip()


def unity_cli() -> str:
    cli = os.environ.get("UNITY_CLI") or shutil.which("unity.exe") or shutil.which("unity")
    if not cli:
        raise WorktreeError("Unity CLI not found: put unity.exe on PATH or set UNITY_CLI")
    return cli


def session(action: str, project: pathlib.Path, timeout: int = 300) -> dict[str, Any]:
    args = [sys.executable, str(SESSION), action, "--project", str(project), "--timeout", str(timeout)]
    if os.environ.get("UNITY_CLI"):
        args += ["--cli", os.environ["UNITY_CLI"]]
    out = run(args, check=False)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        raise WorktreeError(f"unity_session {action} gave no result: {out.stdout}{out.stderr}") from None


def discover(project: pathlib.Path) -> dict[str, Any]:
    """The session helper's discovery of the exact Editor on `project`. A refusal (inventory unknown, endpoint collision,
    ...) is an error, never "no Editor running": only `state == "stopped"` says nothing is running."""
    found = session("discover", project)
    if not found.get("ok"):
        error = found.get("error") or {}
        raise WorktreeError(f"Editor discovery refused for {project}: {error.get('code', 'unknown')}: {error.get('message', found)}")
    return found


def worktrees(main: pathlib.Path) -> list[pathlib.Path]:
    out = run(["git", "worktree", "list", "--porcelain"], cwd=main).stdout
    return [pathlib.Path(line[len("worktree "):]) for line in out.splitlines() if line.startswith("worktree ")]


def listening_ports() -> set[int]:
    out = run(["cmd.exe", "/c", "netstat -ano -p tcp"], check=False).stdout
    return {int(m) for m in re.findall(r"127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING", out)}


def free_gb() -> float:
    out = run(["powershell.exe", "-NoProfile", "-Command",
               "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"], check=False).stdout
    try:
        return int(out.strip()) / 1024 / 1024
    except ValueError:
        raise WorktreeError(f"could not read the free RAM from Windows (got {out.strip()[:80]!r}); is powershell.exe reachable?") from None


def installed_editors() -> list[dict[str, Any]]:
    """The CLI's list of installed Editors (`unity editors --installed --json`); raises WorktreeError when unreadable."""
    out = run([unity_cli(), "editors", "--installed", "--json", "--no-banner"], check=False)
    try:
        items = json.loads(out.stdout)["data"]["items"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise WorktreeError(f"the installed Editors could not be read from `unity editors --installed --json` (exit {out.returncode}): {(out.stderr or out.stdout).strip()[:200]}") from None
    return [item for item in items if isinstance(item, dict)]


def editor_executable(path: pathlib.Path, version: str | None = None) -> str:
    """The installed Editor this project asks for, found in the CLI's install list; `UNITY_EDITOR` overrides it with a
    full path to Unity.exe. Only when that list cannot be read does it assume the Hub's default install folder."""
    if os.environ.get("UNITY_EDITOR"):
        return os.environ["UNITY_EDITOR"]
    if not version:
        match = re.search(r"m_EditorVersion: (\S+)", (path / "ProjectSettings/ProjectVersion.txt").read_text())
        if not match:
            raise WorktreeError(f"{path}/ProjectSettings/ProjectVersion.txt names no Editor version")
        version = match.group(1)
    try:
        editors = installed_editors()
    except WorktreeError as unreadable:
        files = run(["cmd.exe", "/c", "echo %ProgramFiles%"], check=False).stdout.strip() or "C:\\Program Files"
        guess = f"{files}\\Unity\\Hub\\Editor\\{version}\\Editor\\Unity.exe"
        print(f"warning: {unreadable}; assuming the Hub default {guess}", file=sys.stderr)
        return guess
    for editor in editors:
        if editor.get("version") == version and editor.get("location"):
            return editor["location"]
    raise WorktreeError(f"Editor {version} is not installed; run `unity install {version}`")


def start_on_desktop(path: pathlib.Path, desktop: str, editor_version: str | None = None) -> None:
    """Starts the project's Editor on the named desktop (`Default` is the owner's own) through the hidden-desktop launcher.
    It only starts the process; the caller waits for the Editor to claim the project."""
    (path / "Logs").mkdir(exist_ok=True)
    log = windows(path / "Logs" / f"Editor-{time.strftime('%Y%m%d-%H%M%S')}.log")
    command = f'"{editor_executable(path, editor_version)}" -projectPath "{windows(path)}" -logFile "{log}"'
    started = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windows(HIDDEN_DESKTOP),
                   "-CommandLine", command, "-Desktop", desktop], check=False)
    if started.returncode != 0:
        raise WorktreeError(f"not started: {started.stdout.strip()}{started.stderr.strip()}")


def launch(config: Config, path: pathlib.Path, visible: bool) -> None:
    """Starts the worktree's Editor on the hidden desktop, or on the owner's own desktop (Default) when visible.
    The session helper's open then finds the Editor running and verifies it."""
    if discover(path).get("state") == "running":
        return
    start_on_desktop(path, "Default" if visible else config.desktop)
    for _ in range(60):
        if discover(path).get("state") == "running":
            return
        time.sleep(1)
    raise WorktreeError("the Editor started did not claim the project within 60 s")


def editor_command(path: pathlib.Path, pid: int, *args: str) -> subprocess.CompletedProcess:
    return run([unity_cli(), "command", *args, "--project-path", windows(path), "--pid", str(pid), "--result-only"], check=False)


def open_scene(path: pathlib.Path, pid: int, scene: str) -> str:
    """Makes `scene` the open scene in the worktree's ready Editor. Unsaved scene changes refuse it; nothing is discarded."""
    if not (path / scene).is_file():
        raise WorktreeError(f"{path} has no {scene}")
    listed = editor_command(path, pid, "scene", "list_open")
    try:
        before = json.loads(listed.stdout)
    except json.JSONDecodeError:
        raise WorktreeError(f"scene list_open gave no result: {listed.stdout}{listed.stderr}") from None
    if any(s.get("path") == scene and s.get("isActive") for s in before):
        return f"{scene} is already the active scene"
    dirty = [s.get("path") or "(unsaved)" for s in before if s.get("isDirty")]
    if dirty:
        raise WorktreeError(f"{scene} not opened: unsaved changes in {', '.join(dirty)}; save or revert them in the Editor, then open again")
    opened = editor_command(path, pid, "scene", "open", "--path", scene)
    if opened.returncode != 0:
        raise WorktreeError(f"{scene} not opened: {opened.stdout.strip()}{opened.stderr.strip()}")
    after = editor_command(path, pid, "scene", "list_open")
    try:
        active = [s.get("path") for s in json.loads(after.stdout) if s.get("isActive")]
    except json.JSONDecodeError:
        active = []
    if scene not in active:
        raise WorktreeError(f"{scene} not opened: it is not the active scene afterwards (active: {', '.join(map(str, active)) or 'none'})")
    return f"{scene} open"


def open_editor(config: Config, path: pathlib.Path, visible: bool = False, scene: str | None = None) -> bool:
    """Opens the worktree's Editor (hidden, or visible), waits until it is ready, then opens `scene`."""
    if scene and not (path / scene).is_file():
        raise WorktreeError(f"{path} has no {scene}")
    (path / "Logs").mkdir(exist_ok=True)
    launch(config, path, visible)
    opened = session("open", path)
    ready = session("ready", path, timeout=540) if opened.get("ok") else opened
    print(json.dumps(ready))
    if ready.get("ok") and scene:
        print(open_scene(path, ready["process"]["pid"], scene))
    return bool(ready.get("ok"))


def existing(config: Config, name: str) -> pathlib.Path:
    path = config.path(name)
    if path not in worktrees(config.main):
        raise WorktreeError(f"{path} is not a worktree of this project")
    return path


def create(config: Config, name: str, visible: bool, scene: str | None) -> bool:
    path = config.path(name)
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    port = config.port(name)
    if port in listening_ports():
        raise WorktreeError(f"port {port} is already listening; pick another base_port in {CONFIG_FILE}")
    free = free_gb()
    if free < MIN_FREE_GB:
        raise WorktreeError(f"{free:.1f} GB RAM free; another Editor needs about {MIN_FREE_GB} GB. Close one first.")
    # The Library copy is only consistent while the main Editor is idle (not importing or compiling).
    if discover(config.main).get("state") == "running" and not session("ready", config.main, timeout=60).get("ok"):
        raise WorktreeError("the main Editor is not ready; copying its Library now could copy a half-written database")
    # `remove` keeps the branch, so a later `create` of the same name reuses it.
    if run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=config.main, check=False).returncode == 0:
        print(f"branch {name} exists; the worktree reuses it")
        run(["git", "worktree", "add", str(path), name], cwd=config.main)
    else:
        run(["git", "worktree", "add", str(path), "-b", name], cwd=config.main)
    set_port(path, port)
    for seed in config.seed_paths:
        source = config.main / seed
        if source.is_dir():
            shutil.copytree(source, path / seed, dirs_exist_ok=True)
        elif source.is_file():
            (path / seed).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, path / seed)
    if (config.main / "Library").is_dir():
        # robocopy exits 0-7 on success; 8 and above is a failure.
        copy = run(["cmd.exe", "/c", "robocopy", windows(config.main / "Library"), windows(path / "Library"),
                    "/E", "/MT:16", "/NFL", "/NDL", "/NP", "/R:1", "/W:1", "/XD", windows(config.main / "Library/Pipeline")],
                   cwd=config.main, check=False)
        if copy.returncode >= 8:
            raise WorktreeError(f"Library copy failed ({copy.returncode}):\n{copy.stdout[-2000:]}")
    print(f"{path} on branch {name}, port {port}")
    return open_editor(config, path, visible, scene)


def editor_identity(path: pathlib.Path) -> tuple[SessionIdentity, PlatformAdapter]:
    """The running Editor as the session code identifies it (project, PID and start time)."""
    process = discover(path).get("process")
    if not process:
        raise WorktreeError(f"no Editor running on {path}")
    return SessionIdentity(process["project"], int(process["pid"]), process["startedAt"], ""), current_platform()


def hidden_script(*args: str) -> subprocess.CompletedProcess:
    return run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windows(HIDDEN_DESKTOP), *args], check=False)


def dialogs(config: Config, name: str) -> list[dict[str, Any]]:
    """Every window of the worktree's Editor with buttons, on the hidden desktop and on the owner's: a dialog there is
    seen by nobody and holds the Editor's main thread."""
    identity, platform = editor_identity(existing(config, name))
    return platform.list_windows(identity, [config.desktop, "Default"], 30)


def press(config: Config, name: str, dialog: str, button: str, visible: bool) -> None:
    """Press one named button on the Editor's one dialog with that exact title; anything ambiguous is refused."""
    identity, platform = editor_identity(existing(config, name))
    pid = identity.pid
    # The same check the session code makes: a PID the system has handed to another process since discovery is not pressed.
    if not platform.editor_is_current(identity, 30):
        raise WorktreeError(f"Editor {pid} is no longer the Editor discovered for {name}; run the command again")
    result = hidden_script("-PressPid", str(pid), "-Dialog", dialog, "-Button", button, "-Desktop", "Default" if visible else config.desktop)
    said, error = result.stdout.strip(), result.stderr.strip()
    print(f"Editor {pid}: {said}")
    if result.returncode != 0 or error or said != "pressed":
        raise WorktreeError(f"{button} not pressed on Editor {pid}: {said or error or 'the script printed nothing'} (exit {result.returncode})")


def refusal_text(closed: subprocess.CompletedProcess) -> str:
    """What `unity close --if-saved` said when it refused: its error messages, which list the unsaved scenes."""
    try:
        body = json.loads(closed.stdout)
        return "\n".join(f"{e.get('code', '')}: {e.get('message', '')}" for e in body.get("errors", [])) or closed.stdout.strip()
    except json.JSONDecodeError:
        return (closed.stdout + closed.stderr).strip()


# Persistent objects under Assets/ with unsaved changes, and an open prefab stage's asset when it is dirty. Scenes are
# left to `unity close --if-saved`.
DIRTY_ASSETS_CODE = """
var paths = new System.Collections.Generic.SortedSet<string>();
foreach (var o in UnityEngine.Resources.FindObjectsOfTypeAll<UnityEngine.Object>())
{
    if (o == null || !UnityEditor.EditorUtility.IsPersistent(o) || !UnityEditor.EditorUtility.IsDirty(o)) continue;
    var p = UnityEditor.AssetDatabase.GetAssetPath(o);
    if (p.StartsWith("Assets/") && !p.EndsWith(".unity")) paths.Add(p);
}
var stage = UnityEditor.SceneManagement.PrefabStageUtility.GetCurrentPrefabStage();
if (stage != null && stage.scene.isDirty && stage.assetPath.StartsWith("Assets/")) paths.Add(stage.assetPath);
return string.Join("\\n", paths);
"""


def dirty_assets(path: pathlib.Path, pid: int) -> list[str]:
    """Asset paths with unsaved changes in the worktree's Editor. An Editor that cannot say refuses the close."""
    asked = editor_command(path, pid, "script", "eval", "--code", DIRTY_ASSETS_CODE, "--timeout", "30")
    try:
        text = json.loads(asked.stdout)
    except json.JSONDecodeError:
        text = None
    if asked.returncode != 0 or not isinstance(text, str):
        raise WorktreeError(f"{path.name}: unsaved assets could not be checked, so nothing was closed: {asked.stdout.strip()}{asked.stderr.strip()}")
    return [line for line in text.split("\n") if line]


def close(config: Config, name: str) -> bool:
    path = existing(config, name)
    found = discover(path)
    if found.get("state") != "running":
        print(f"{name}: Editor not running, nothing to close")
        return True
    pid = int(found["process"]["pid"])
    blocked = CliTransport(current_platform(), cli_override=os.environ.get("UNITY_CLI")).test_run_refusal(str(path), pid)
    if blocked is not None:
        print(f"{name}: TEST_RUN_IN_PROGRESS, not closed: {blocked.message}")
        return False
    dirty = dirty_assets(path, pid)
    if dirty:
        print(f"{name}: UNSAVED_ASSETS, not closed (nothing was saved or cleared; save or revert them in the Editor, then close again):\n" + "\n".join(dirty))
        return False
    closed = run([unity_cli(), "close", windows(path), "--if-saved", "--timeout", "60", "--json"], check=False)
    if closed.returncode == 0:
        print(f"{name}: Editor closed")
        return True
    print(f"{name}: not closed (nothing was saved, discarded or forced; save or revert in the Editor, then close again):\n{refusal_text(closed)}")
    return False


def unclean_files(path: pathlib.Path) -> list[str]:
    """What git reports as modified or untracked in the worktree, one `status --porcelain` line each."""
    return run(["git", "status", "--porcelain"], cwd=path, check=False).stdout.splitlines()


def remove(config: Config, name: str, force: bool = False) -> bool:
    """Removes the worktree and keeps its branch. Files git reports as modified or untracked refuse it, listed, unless
    `force`, which removes the worktree with them (after listing them)."""
    path = existing(config, name)
    if not close(config, name):
        return False
    left = unclean_files(path)
    if left and not force:
        print(f"not removed: {path} has {len(left)} modified or untracked file(s); commit or discard them, or remove with --force:\n" + "\n".join(left))
        return False
    if left:
        print(f"removing {path} with {len(left)} modified or untracked file(s):\n" + "\n".join(left))
    result = run(["git", "worktree", "remove", *(["--force"] if force else []), str(path)], cwd=config.main, check=False)
    if result.returncode:
        print(f"not removed: {result.stderr.strip()}")
        return False
    print(f"removed {path}; branch {name} kept")
    return True


def list_all(config: Config) -> None:
    rows = {}
    status = run([unity_cli(), "status", "--json"], check=False)
    try:
        rows = {_norm(i["projectPath"]): i for i in json.loads(status.stdout)["data"]["instances"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    for path in worktrees(config.main):
        row = rows.get(_norm(windows(path)))
        branch = run(["git", "branch", "--show-current"], cwd=path).stdout.strip()
        state = f"Editor {row['state']} pid {row['pid']} port {row['port']}" if row else "no Editor on Pipeline"
        print(f"{path}  {branch}  {state}")


def _norm(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=".", help="the main project or any of its worktrees (default: the current directory)")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "open"):
        opening = sub.add_parser(action)
        opening.add_argument("name")
        opening.add_argument("--visible", action="store_true", help="on the owner's own desktop, to watch or record it")
        opening.add_argument("--scene", help="once ready, open this scene (an Assets/ path); unsaved scene changes refuse it")
    sub.add_parser("list")
    sub.add_parser("close", help="refuses on unsaved scenes (unity close --if-saved) and on unsaved assets under Assets/ (UNSAVED_ASSETS)").add_argument("name")
    removing = sub.add_parser("remove", help="keeps the branch; modified or untracked files refuse it unless --force")
    removing.add_argument("name")
    removing.add_argument("--force", action="store_true", help="remove the worktree with its modified or untracked files, listing them first")
    sub.add_parser("dialogs").add_argument("name")
    pressing = sub.add_parser("press")
    for argument in ("name", "dialog", "button"):
        pressing.add_argument(argument)
    pressing.add_argument("--visible", action="store_true", help="the Editor is on the owner's own desktop")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(pathlib.Path(args.project).resolve())
        if args.action == "create":
            return 0 if create(config, args.name, args.visible, args.scene) else 1
        if args.action == "open":
            return 0 if open_worktree(config, args.name, args.visible, args.scene) else 1
        if args.action == "list":
            list_all(config)
        elif args.action == "dialogs":
            found = dialogs(config, args.name)
            print(json.dumps(found, indent=2) if found else "no dialogs: nothing is waiting on a button")
        elif args.action == "press":
            press(config, args.name, args.dialog, args.button, args.visible)
        elif args.action == "close":
            return 0 if close(config, args.name) else 1
        elif args.action == "remove":
            return 0 if remove(config, args.name, args.force) else 1
        return 0
    except WorktreeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        said = (exc.stderr or exc.stdout or "").strip().splitlines()
        print(f"`{' '.join(map(str, exc.cmd))}` failed ({exc.returncode}): {said[-1] if said else 'no output'}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"{exc.filename or exc} was not found: worktree handling runs from WSL and drives Windows (wslpath, cmd.exe, powershell.exe, robocopy)", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
