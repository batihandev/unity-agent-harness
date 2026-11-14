from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import subprocess
import tempfile
import time
import shlex
from dataclasses import replace
from typing import Any

from .model import Deadline, CommandResult, NativeResult, RegistryObservation, SessionIdentity


NATIVE_CLEANUP_RESERVE_SECONDS = 15


class CliMissing(RuntimeError):
    pass


class RunMarker:
    """The record of a connected test run that outlived its caller, kept beside the helper's other per-project state.

    While a run is `running` the Editor answers no other call without timing out, and Pipeline logs an error that NUnit counts as an unhandled log, failing the running test.
    The marker lets a later call see the run, and lets the same `test` request collect it."""

    def __init__(self, project: str | pathlib.Path, root: str | pathlib.Path | None = None):
        if root is None:
            who = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
            root = pathlib.Path(tempfile.gettempdir()) / f"unity-session-{who}"
        stem = hashlib.sha256(os.path.normcase(str(pathlib.Path(project).resolve())).encode("utf-8")).hexdigest()
        self.path = pathlib.Path(root) / f"{stem}.testrun"

    def read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        partial.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(partial, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class CliTransport:
    def __init__(self, platform, cli_override: str | None = None, command_runner=subprocess.run, command_timeout: float = 30, launcher=subprocess.Popen, diagnostic_root: str | None = None, state_root: str | None = None):
        self.platform = platform
        self.state_root = state_root
        self.cli_override = cli_override
        self._run = command_runner
        self._launch = launcher
        self.diagnostic_root = diagnostic_root
        self._launchers: dict[int, tuple[Any, pathlib.Path]] = {}
        self.command_timeout = command_timeout
        self.cli_path: str | None = None
        self._project_paths: dict[tuple[str, str], str] = {}

    def _budget(self, timeout: float | None, deadline: Deadline | None) -> Deadline:
        if deadline is not None:
            return deadline.capped(self.command_timeout if timeout is None else min(timeout, self.command_timeout))
        limit = self.command_timeout if timeout is None else min(timeout, self.command_timeout)
        return Deadline.after(limit)

    def ensure_cli(self, timeout: float | None = None, *, deadline: Deadline | None = None) -> str:
        deadline = self._budget(timeout, deadline)
        deadline.require_remaining("Unity CLI discovery")
        if self.cli_path:
            return self.cli_path
        candidate = self.cli_override or self.platform.discover_cli(timeout=deadline.remaining(), deadline=deadline)
        if not candidate:
            raise CliMissing("Unity CLI was not found; install it or pass --cli PATH")
        if self.cli_override and not pathlib.Path(candidate).is_file():
            raise CliMissing(f"Unity CLI override does not exist: {candidate}")
        self.cli_path = candidate
        return candidate

    def _invoke(self, args: list[str], timeout: float | None = None, *, deadline: Deadline | None = None) -> CommandResult:
        deadline = self._budget(timeout, deadline)
        try:
            cli = self.ensure_cli(deadline=deadline)
            argv = [cli, *args]
            framework_args = args[:args.index("--")] if "--" in args else args
            timeout_index = argv.index("--timeout") + 1 if "--timeout" in framework_args else None
            requested = float(argv[timeout_index]) if timeout_index is not None else None
            limit = deadline.require_remaining("Unity CLI command")
            if args and args[0] == "test" and timeout_index is not None:
                native_timeout = int(limit) - NATIVE_CLEANUP_RESERVE_SECONDS
                if native_timeout < 1:
                    return CommandResult.failed("TEST_TIMEOUT_BUDGET_TOO_SMALL", "The host deadline cannot reserve time for Unity's native Editor cleanup")
                argv[timeout_index] = str(min(int(requested), native_timeout))
                args[timeout_index - 1] = argv[timeout_index]
            completed = self._run(
                argv, capture_output=True, text=True,
                timeout=limit, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult.failed("CLI_TIMEOUT", "Unity CLI command reached its deadline", stderr=_bounded(exc.stderr))
        except OSError as exc:
            return CommandResult.failed("CLI_EXEC_FAILED", f"Unity CLI could not be executed: {exc}")
        payload = None
        if completed.stdout.strip():
            try:
                payload = json.loads(completed.stdout)
            except (ValueError, RecursionError):
                if completed.returncode != 0:
                    return CommandResult.failed("CLI_PROCESS_FAILED", "Unity CLI refused the call without a command envelope",
                        exitCode=completed.returncode, stdout=_bounded(completed.stdout), stderr=_bounded(completed.stderr))
                return CommandResult.failed(
                    "CLI_INVALID_JSON", "Unity CLI returned invalid JSON",
                    exitCode=completed.returncode, stdout=_bounded(completed.stdout), stderr=_bounded(completed.stderr),
                )
        diagnostics = {
            "exitCode": completed.returncode,
            "stderr": _bounded(completed.stderr),
        }
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool) or (payload["success"] and not isinstance(payload.get("data"), dict)):
            return CommandResult.failed("CLI_PROCESS_FAILED" if completed.returncode != 0 else "CLI_INVALID_ENVELOPE",
                "Unity CLI refused the call without a command envelope" if completed.returncode != 0 else "Unity CLI returned a malformed command envelope", stdout=_bounded(completed.stdout), **diagnostics)
        if completed.returncode != 0 or (isinstance(payload, dict) and payload.get("success") is False):
            code, message = _envelope_error(payload)
            if completed.returncode != 0 and not (code or message):
                return CommandResult.failed("CLI_PROCESS_FAILED", "Unity CLI refused the call without error details",
                    payload=_redact_value(payload), **diagnostics)
            return CommandResult.failed(_bounded(code or "CLI_COMMAND_FAILED", 500), _bounded(message or "Unity CLI command failed", 500), payload=_redact_value(payload), **diagnostics)
        return CommandResult.ok(payload, **diagnostics)

    def _project(self, project: str, timeout: float | None = None, *, deadline: Deadline | None = None, cache: bool = True) -> str:
        deadline = self._budget(timeout, deadline)
        cli = self.ensure_cli(deadline=deadline)
        remaining = deadline.require_remaining("Unity CLI preparation")
        if not cache:
            target = self.platform.cli_project_path(project, cli, timeout=remaining, deadline=deadline)
            deadline.require_remaining("Unity CLI preparation")
            return target
        canonicalize = getattr(self.platform, "canonicalize_project", None)
        canonical = canonicalize(project) if canonicalize is not None else project
        key = (canonical, cli)
        if key not in self._project_paths:
            target = self.platform.cli_project_path(project, cli, timeout=remaining, deadline=deadline)
            deadline.require_remaining("Unity CLI preparation")
            self._project_paths[key] = target
        target = self._project_paths[key]
        deadline.require_remaining("Unity CLI preparation")
        return target

    def _project_budget(self, project: str, timeout: float | None, *, deadline: Deadline | None = None) -> tuple[str, float]:
        deadline = self._budget(timeout, deadline)
        try:
            target = self._project(project, deadline=deadline)
        except subprocess.TimeoutExpired:
            return project, 0
        return target, deadline.remaining()

    def _test_run_block(self, project: str, pid: int | None, timeout: float | None, deadline: Deadline | None) -> NativeResult | None:
        """A refusal while a test run of this project is `running`, else None. A marker whose run is gone or finished is dropped."""
        marker = RunMarker(project, self.state_root)
        held = marker.read()
        if held is None:
            return None
        if pid is not None and held.get("pid") not in (None, pid):
            marker.clear()
            return None
        status = self.native("test", "status", {"detail": "summary"}, project=project, pid=pid, timeout=timeout, deadline=deadline, result_only=False)
        state = status.value.get("status") if isinstance(status.value, dict) else None
        if state == "running":
            return self._in_progress(status)
        if state is None and status.status != "NO_EDITOR":
            if time.time() < held.get("deadlineAt", float("inf")):
                return self._in_progress(status)
            # The run's own deadline is long past and the Editor will not say: let the call meet the Editor's real state (unavailable, awaiting input, unreachable).
            return None
        marker.clear()
        return None

    def test_run_refusal(self, project: str, pid: int | None = None, timeout: float | None = 10) -> NativeResult | None:
        """TEST_RUN_IN_PROGRESS while a test run of the project is going, else None: the check every other helper call gets, for a caller that talks to `unity.exe` itself."""
        return self._test_run_block(project, pid, timeout, None)

    @staticmethod
    def _in_progress(status: NativeResult) -> NativeResult:
        return NativeResult(6, code="TEST_RUN_IN_PROGRESS", argv=status.argv,
                            message="A test run is still running; any other call would time out and fail it. Run the same `test` request to collect it, or `unity test cancel`.")

    def native(self, group: str, name: str, params: dict[str, Any] | None = None, *, project: str,
               pid: int | None = None, timeout: float | None = None, deadline: Deadline | None = None, human: bool = False, result_only: bool = True) -> NativeResult:
        """Run one Pipeline command as `unity command <group> <name> --kebab-param value` and return its exit code and result.

        `--result-only` replaces a failing command's reply with an error envelope. A command whose failure still carries a payload (`test status` of a red run, exit 6) is run with `result_only=False`: its stdout is then the bare payload."""
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*", group) \
                or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            return NativeResult(2, code="INVALID_COMMAND", message="The Pipeline command name is invalid")
        if group != "test":
            blocked = self._test_run_block(project, pid, timeout, deadline)
            if blocked is not None:
                return blocked
        flags: list[str] = []
        for key, value in (params or {}).items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
                return NativeResult(2, code="INVALID_ARGUMENTS", message="A Pipeline argument name is invalid")
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    flags.append(flag)
            elif isinstance(value, (str, int, float)):
                flags.extend((flag, str(value)))
            elif isinstance(value, (list, dict)):
                flags.extend((flag, json.dumps(value, separators=(",", ":"))))
            else:
                return NativeResult(2, code="INVALID_ARGUMENTS", message=f"Pipeline argument {key!r} has an unsupported type")
        budget = self.command_timeout if timeout is None else min(timeout, self.command_timeout)
        if not 0 < budget < float("inf") or not math.isfinite(budget * 1000):
            return NativeResult(14, code="CLI_TIMEOUT", message="Unity CLI command reached its deadline")
        try:
            deadline = self._budget(timeout, deadline)
            target_project = self._project(project, deadline=deadline)
            cli = self.ensure_cli(deadline=deadline)
            seconds = int(deadline.require_remaining("Unity CLI command"))
        except subprocess.TimeoutExpired:
            return NativeResult(14, code="CLI_TIMEOUT", message="Unity CLI command reached its deadline")
        except CliMissing as exc:
            return NativeResult(4, code="CLI_NOT_FOUND", message=str(exc))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return NativeResult(1, code="CLI_EXEC_FAILED", message=_bounded(str(exc), 500))
        if seconds < 1:
            return NativeResult(14, code="CLI_TIMEOUT", message="Unity CLI command cannot fit a positive timeout within its deadline")
        argv = [cli, "command", "--project-path", target_project]
        if pid is not None:
            argv.extend(("--pid", str(pid)))
        argv.extend(("--timeout", str(seconds), group, name, *flags) if human else ("--format", "json", *(("--result-only",) if result_only else ()), "--timeout", str(seconds), group, name, *flags))
        result = self._run_native(argv, deadline, parse=not human)
        if group == "test" and name == "cancel" and result.ok:
            RunMarker(project, self.state_root).clear()
        return result

    def recompile(self, project: str, *, pid: int | None = None, timeout: float | None = None, deadline: Deadline | None = None) -> NativeResult:
        """Run `unity recompile`, the CLI's one-shot compile gate; its value is the reply's `data`."""
        blocked = self._test_run_block(project, pid, timeout, deadline)
        if blocked is not None:
            return blocked
        try:
            deadline = self._budget(timeout, deadline)
            target_project = self._project(project, deadline=deadline)
            cli = self.ensure_cli(deadline=deadline)
            seconds = int(deadline.require_remaining("Unity CLI recompile"))
        except subprocess.TimeoutExpired:
            return NativeResult(14, code="CLI_TIMEOUT", message="Unity CLI recompile reached its deadline")
        except CliMissing as exc:
            return NativeResult(4, code="CLI_NOT_FOUND", message=str(exc))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return NativeResult(1, code="CLI_EXEC_FAILED", message=_bounded(str(exc), 500))
        if seconds < 1:
            return NativeResult(14, code="CLI_TIMEOUT", message="Unity CLI recompile cannot fit a positive timeout within its deadline")
        argv = [cli, "recompile", "--project-path", target_project, "--json", "--timeout", str(seconds)]
        result = self._run_native(argv, deadline)
        envelope = result.value
        if result.exit_code == 0 and isinstance(envelope, dict) and isinstance(envelope.get("data"), dict):
            return replace(result, value=envelope["data"])
        return result

    def _run_native(self, argv: list[str], deadline: Deadline, parse: bool = True) -> NativeResult:
        try:
            limit = deadline.require_remaining("Unity CLI command")
            index = argv.index("--timeout") + 1
            seconds = int(min(float(argv[index]), limit))
            if seconds < 1:
                raise subprocess.TimeoutExpired("Unity CLI command", limit)
            argv = [*argv[:index], str(seconds), *argv[index + 1:]]
            completed = self._run(argv, capture_output=True, text=True, timeout=limit, check=False)
        except subprocess.TimeoutExpired as exc:
            return NativeResult(14, argv=tuple(argv), code="CLI_TIMEOUT", message="Unity CLI command reached its deadline", stderr=_bounded(exc.stderr))
        except OSError as exc:
            return NativeResult(1, argv=tuple(argv), code="CLI_EXEC_FAILED", message=_bounded(f"Unity CLI could not be executed: {exc}", 500))
        stdout, stderr = _bounded(completed.stdout, 20000), _bounded(completed.stderr)
        value = None
        if parse and completed.stdout.strip():
            try:
                value = json.loads(completed.stdout)
            except (ValueError, RecursionError):
                if completed.returncode == 0:
                    return NativeResult(1, argv=tuple(argv), stdout=stdout, stderr=stderr, code="CLI_INVALID_JSON", message="Unity CLI returned invalid JSON")
        code = message = None
        if completed.returncode != 0:
            code, message = _envelope_error(value)
            if message is None:
                message = _bounded(completed.stderr.strip().splitlines()[0], 500) if completed.stderr.strip() else None
        return NativeResult(completed.returncode, value, stdout, stderr, tuple(argv), code, message)

    def invoke_native_test(self, project: str, mode: str, filter_value: str | None, output: str, editor_version: str, timeout: float, *, assembly: str | None = None, deadline: Deadline | None = None) -> CommandResult:
        """Run a top-level native suite and retain invocation evidence."""
        if mode not in {"EditMode", "PlayMode"}:
            return CommandResult.failed("INVALID_ARGUMENT", "Test mode must be EditMode or PlayMode")
        if filter_value is not None and (not isinstance(filter_value, str) or not filter_value):
            return CommandResult.failed("INVALID_ARGUMENT", "Test filter must be omitted or nonempty")
        if assembly is not None and (not isinstance(assembly, str) or not assembly or ";" in assembly or assembly.startswith("-")):
            return CommandResult.failed("INVALID_ARGUMENT", "One exact assembly name is required")
        try:
            deadline = self._budget(timeout, deadline)
            initial_budget = deadline.remaining()
            target_project = self._project(project, initial_budget, deadline=deadline)
            remaining = deadline.remaining()
            target_output = self._project(output, remaining, deadline=deadline, cache=False)
            remaining = deadline.remaining()
            host_budget = min(remaining, self.command_timeout)
            native_timeout = int(host_budget) - NATIVE_CLEANUP_RESERVE_SECONDS
            if native_timeout < 1:
                return CommandResult.failed("TEST_TIMEOUT_BUDGET_TOO_SMALL", "The host deadline cannot reserve time for Unity's native Editor cleanup")
            args = ["test", target_project, "--mode", mode, "--output", target_output,
                    "--editor-version", editor_version, "--timeout", str(native_timeout), "--format", "json"]
            if filter_value is not None:
                args.extend(("--filter", filter_value))
            if assembly is not None:
                args.extend(("--", "-assemblyNames", assembly))
            cli = self.ensure_cli(deadline=deadline)
        except subprocess.TimeoutExpired as exc:
            return CommandResult.failed("CLI_TIMEOUT", "Unity CLI preparation reached its deadline", stderr=_bounded(exc.stderr))
        except (CliMissing, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            code = "CLI_NOT_FOUND" if isinstance(exc, CliMissing) else "CLI_EXEC_FAILED"
            return CommandResult.failed(code, str(exc), invocation={"command": "test", "project": project, "output": output})
        result = self._invoke(args, deadline=deadline)
        argv = [cli, *args]
        invocation = {"argv": argv, "command": "test", "mode": mode, "filter": filter_value, "project": project, "output": output, "editorVersion": editor_version, "assembly": assembly}
        diagnostics = dict(result.diagnostics)
        diagnostics["invocation"] = invocation
        if result.success:
            return CommandResult.ok(result.data, **diagnostics)
        return CommandResult.failed(result.code or "CLI_COMMAND_FAILED", result.message or "Unity test command failed", **diagnostics)

    def registry(self, project: str, timeout: float | None = None, *, deadline: Deadline | None = None) -> CommandResult:
        deadline = self._budget(timeout, deadline)
        result = self._invoke(["editors", "running", "--format", "json"], timeout, deadline=deadline)
        if not result.success:
            return result
        envelope = result.data.get("data") if isinstance(result.data, dict) else None
        instances = envelope.get("instances") if isinstance(envelope, dict) else None
        if not isinstance(instances, list):
            return CommandResult.failed("REGISTRY_INVALID", "Unity CLI editor inventory omitted data.instances")
        observations: list[RegistryObservation] = []
        for instance in instances:
            if not isinstance(instance, dict):
                return CommandResult.failed("REGISTRY_INVALID", "Unity CLI editor inventory contained a malformed instance")
            raw_project = instance.get("project") or instance.get("projectPath")
            pid = instance.get("pid") or instance.get("processId")
            if not isinstance(raw_project, str) or not raw_project or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return CommandResult.failed("REGISTRY_INVALID", "Unity CLI editor inventory contained a malformed instance")
            try:
                canonical = self.platform.canonicalize_external_project(raw_project, deadline=deadline)
            except subprocess.TimeoutExpired as exc:
                return CommandResult.failed("CLI_TIMEOUT", "Editor registry path conversion reached its deadline", stderr=_bounded(exc.stderr))
            port = instance.get("port")
            pipeline_port = port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else None
            observations.append(RegistryObservation(
                canonical, pid,
                _optional_string(instance.get("startedAt") or instance.get("startTime")), "editors-running", pipeline_port,
            ))
        return CommandResult.ok(observations, **result.diagnostics)

    def editor_status(self, identity: SessionIdentity, timeout: float | None = None, *, deadline: Deadline | None = None) -> NativeResult:
        return self.native("editor", "status", project=identity.project, pid=identity.pid, timeout=timeout, deadline=deadline)

    def dirty_scenes(self, identity: SessionIdentity, timeout: float | None = None, *, deadline: Deadline | None = None) -> CommandResult:
        result = self.native("scene", "list_open", project=identity.project, pid=identity.pid, timeout=timeout, deadline=deadline)
        if not result.ok:
            return CommandResult.failed(result.code or result.status, result.message or "Dirty-scene inspection failed", exitCode=result.exit_code)
        if not isinstance(result.value, list) or any(not isinstance(row, dict) for row in result.value):
            return CommandResult.failed("DIRTY_STATE_INVALID", "Dirty-scene inspection returned an unexpected result", payload=_redact_value(result.value))
        return CommandResult.ok([{"path": row.get("path"), "name": row.get("name"), "saved": bool(row.get("path"))}
                                 for row in result.value if row.get("isDirty") is True])

    def request_editor_exit(self, identity: SessionIdentity, timeout: float | None = None, *, deadline: Deadline | None = None) -> CommandResult:
        """Quit the exact Editor; the Editor itself refuses while a scene or prefab has unsaved changes."""
        result = self.native("editor", "quit", project=identity.project, pid=identity.pid, timeout=timeout, deadline=deadline)
        if result.ok:
            return CommandResult.ok({"requested": True}, transport={"exitCode": 0})
        reasons = {"UNAVAILABLE": "EDITOR_UNAVAILABLE", "AWAITING_INPUT": "EDITOR_AWAITING_INPUT", "NO_EDITOR": "PROCESS_IDENTITY_CHANGED", "TIMEOUT": "CLI_TIMEOUT"}
        code = reasons.get(result.status, "DIRTY_EDITOR_STATE" if result.exit_code == 6 else "CLOSE_NOT_ACCEPTED")
        return CommandResult.failed(code, "The exact Editor refused the close request", stderr=result.stderr, exitCode=result.exit_code)

    def open(self, project: str, editor_version: str | None, timeout: float | None = None, batch_mode: bool = False, *, deadline: Deadline | None = None) -> CommandResult:
        deadline = self._budget(timeout, deadline)
        target_project, timeout = self._project_budget(project, timeout, deadline=deadline)
        if timeout is not None and timeout <= 0:
            return CommandResult.failed("CLI_TIMEOUT", "The open action reached its deadline before launch")
        args = ["open", target_project, "--format", "json"]
        if editor_version:
            args.extend(["--editor-version", editor_version])
        owned_root: pathlib.Path | None = None
        try:
            if self.diagnostic_root:
                root = pathlib.Path(self.diagnostic_root)
            else:
                owned_root = pathlib.Path(tempfile.mkdtemp(prefix="unity-session-launch-"))
                root = owned_root
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, log_path = tempfile.mkstemp(prefix="open-", suffix=".log", dir=root)
        except OSError as exc:
            if owned_root is not None:
                try:
                    owned_root.rmdir()
                except OSError:
                    pass
            return CommandResult.failed("DIAGNOSTIC_STORAGE_FAILED", _bounded(f"The CLI launch diagnostic file could not be created: {exc}", 500))
        editor_log_path = root / f"editor-{pathlib.Path(log_path).stem}.log"
        try:
            native_editor_log = self._project(str(editor_log_path), deadline=deadline, cache=False)
        except subprocess.TimeoutExpired:
            os.close(fd)
            pathlib.Path(log_path).unlink(missing_ok=True)
            return CommandResult.failed("CLI_TIMEOUT", "The open action reached its deadline before launch")
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            os.close(fd)
            pathlib.Path(log_path).unlink(missing_ok=True)
            return CommandResult.failed("EDITOR_LOG_PATH_UNAVAILABLE", _bounded(f"The Editor log path could not be converted: {exc}", 500))
        if deadline.remaining() <= 0:
            os.close(fd)
            pathlib.Path(log_path).unlink(missing_ok=True)
            return CommandResult.failed("CLI_TIMEOUT", "The open action reached its deadline before launch")
        editor_args = ["-logFile", native_editor_log]
        if batch_mode:
            editor_args.insert(0, "-batchmode")
        args.extend(["--args", subprocess.list2cmdline(editor_args) if self.platform.name in {"windows", "wsl"} else shlex.join(editor_args)])
        try:
            with os.fdopen(fd, "wb") as log:
                options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "close_fds": True}
                if os.name == "nt":
                    options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    options["start_new_session"] = True
                cli = self.ensure_cli(deadline=deadline)
                argv = [cli, *args]
                deadline.require_remaining("Unity CLI open")
                process = self._launch(argv, **options)
        except subprocess.TimeoutExpired:
            pathlib.Path(log_path).unlink(missing_ok=True)
            return CommandResult.failed("CLI_TIMEOUT", "The open action reached its deadline before launch")
        except OSError as exc:
            pathlib.Path(log_path).unlink(missing_ok=True)
            return CommandResult.failed("CLI_EXEC_FAILED", _bounded(f"Unity CLI could not be launched: {exc}", 500))
        self._launchers[process.pid] = (process, pathlib.Path(log_path))
        return CommandResult.ok({"launcherPid": process.pid, "diagnosticPath": log_path, "editorLogPath": str(editor_log_path)}, detached=True)

    def launch_status(self, launcher_pid: int, timeout: float | None = None, *, deadline: Deadline | None = None) -> CommandResult:
        launcher = self._launchers.get(launcher_pid)
        if launcher is None:
            return CommandResult.failed("LAUNCHER_UNKNOWN", "The launcher is not owned by this helper process")
        process, log_path = launcher
        exit_code = process.poll()
        try:
            with log_path.open("rb") as log:
                log.seek(max(0, log.seek(0, os.SEEK_END) - 8192))
                output = log.read().decode("utf-8", "replace")
        except OSError:
            output = ""
        payload = None
        for line in reversed(output.splitlines()):
            try:
                parsed = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(parsed, dict) and "success" in parsed:
                payload = parsed
                break
        if isinstance(payload, dict) and payload.get("success") is False:
            code, message = _envelope_error(payload)
            return CommandResult.failed(_bounded(code or "CLI_COMMAND_FAILED", 500), _bounded(message or "Unity CLI rejected open", 500), exitCode=exit_code, output=_bounded(output), diagnosticPath=str(log_path))
        if exit_code is not None and exit_code != 0:
            return CommandResult.failed("CLI_COMMAND_FAILED", "Unity CLI open launcher exited before the Editor appeared", exitCode=exit_code, output=_bounded(output), diagnosticPath=str(log_path))
        return CommandResult.ok({"state": "running" if exit_code is None else "completed", "exitCode": exit_code}, diagnosticPath=str(log_path))

    def diagnose_logs(self, project: str, log_file: str | None = None, timeout: float | None = None, *, deadline: Deadline | None = None) -> dict[str, Any]:
        deadline = self._budget(timeout, deadline)
        candidates: list[tuple[pathlib.Path, str]] = []
        if log_file and log_file != "-":
            if self.platform.name == "wsl" and (re.match(r"^[A-Za-z]:[\\/]", log_file) or log_file.startswith("\\\\")):
                try:
                    log_file = self.platform.canonicalize_external_project(log_file, deadline=deadline)
                except subprocess.TimeoutExpired:
                    return {"source": "editor-log", "code": "CLI_TIMEOUT", "error": "Editor log path conversion reached its deadline", "matches": []}
            custom = pathlib.Path(log_file)
            candidates.append((custom if custom.is_absolute() else pathlib.Path(project) / custom, "launch-argument"))
        candidates.append((pathlib.Path(project) / "Logs" / "Editor.log", "unverified"))
        if self.platform.name == "macos":
            candidates.append((pathlib.Path.home() / "Library" / "Logs" / "Unity" / "Editor.log", "unverified"))
        elif self.platform.name == "linux":
            candidates.append((pathlib.Path.home() / ".config" / "unity3d" / "Editor.log", "unverified"))
        elif self.platform.name == "wsl":
            finder = getattr(self.platform, "default_editor_log", None)
            try:
                default = finder(timeout=timeout, deadline=deadline) if finder else None
            except subprocess.TimeoutExpired:
                return {"source": "editor-log", "code": "CLI_TIMEOUT", "error": "Default Editor log discovery reached its deadline", "matches": []}
            if default:
                candidates.append((pathlib.Path(default), "unverified"))
        elif self.platform.name == "windows" and os.environ.get("LOCALAPPDATA"):
            candidates.append((pathlib.Path(os.environ["LOCALAPPDATA"]) / "Unity" / "Editor" / "Editor.log", "unverified"))
        for path, attribution in candidates:
            try:
                if not path.is_file():
                    continue
                matches: list[dict[str, Any]] = []
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    categories = []
                    if re.search(r"error CS\d{4}|Scripts have compiler errors", line, re.IGNORECASE):
                        categories.append("compile-error")
                    if re.search(r"Scene\(s\) Have Been Modified|modal|dialog", line, re.IGNORECASE):
                        categories.append("modal")
                    if categories:
                        matches.append({"line": number, "categories": categories, "text": _bounded(line, 500)})
                return {"source": "editor-log", "attribution": attribution, "matches": matches[-40:]}
            except OSError as exc:
                return {"source": "project-log", "error": str(exc), "matches": []}
        return {"source": "project-log", "error": "project Editor log was not found", "matches": []}


def _envelope_error(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return _optional_string(errors[0].get("code")), _optional_string(errors[0].get("message"))
    return None, None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _bounded(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    redacted = _redact(str(value))
    sanitized = "".join(character for character in redacted if character == "\t" or ord(character) >= 32)
    return sanitized[:limit]


def _redact(value: str) -> str:
    value = re.sub(r"(?i)(--?access[-_]?token|accessToken|licensingIpc|hubIPC)(?:=|\s+)\S+", r"\1=<redacted>", value)
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1<redacted>", value)
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str): return _redact(value)
    if isinstance(value, list): return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            sensitive_key = isinstance(key, str) and re.search(r"(?i)(token|secret|password|authorization|api[_-]?key|licensingipc|hubipc)", key)
            safe_key = _redact(key) if isinstance(key, str) else key
            redacted[safe_key] = "<redacted>" if sensitive_key else _redact_value(item)
        return redacted
    return value
