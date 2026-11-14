from __future__ import annotations

import hashlib
import json
import math
import pathlib
import time
import subprocess
from typing import Any

from .lifecycle import SessionRefusal
from .reports import ReportTarget, ReportPathError
from .model import NativeResult
from .transport import _redact_value


GROUND_TRUTH_MAX_AGE_MS = 2000
RETRYABLE_STATUSES = {"UNAVAILABLE", "TIMEOUT"}
EDITOR_BUSY_STATUSES = {"loading", "compiling", "reloading", "importing"}


class WorkflowRefusal(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "details": self.details}}


def native_failure(result: NativeResult, command: str) -> WorkflowRefusal:
    """Name why a native call did not succeed from its exit code: the Editor said busy, blocked, absent, late or failed."""
    if result.code == "TEST_RUN_IN_PROGRESS":
        return WorkflowRefusal("TEST_RUN_IN_PROGRESS", result.message or "A test run is in progress", {"command": command, **result.evidence()})
    codes = {"UNAVAILABLE": "EDITOR_UNAVAILABLE", "AWAITING_INPUT": "EDITOR_AWAITING_INPUT", "NO_EDITOR": "EDITOR_NOT_RUNNING",
             "TIMEOUT": "NATIVE_TIMEOUT", "UNKNOWN_COMMAND": "NATIVE_COMMAND_UNKNOWN"}
    return WorkflowRefusal(codes.get(result.status, "NATIVE_COMMAND_FAILED"), result.message or f"Unity {command} did not succeed",
                           {"command": command, **result.evidence()})


class CompileWorkflow:
    """Host-side gate around the Unity CLI recompile command: a clean compile, fresh console ground truth and unchanged sources."""

    def __init__(self, project: str | pathlib.Path, session, poll_interval: float = 0.25, clock=time):
        self.project = pathlib.Path(project).resolve()
        self.session = session
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive")
        self.poll_interval = poll_interval
        self.clock = clock

    def run(self, source_paths, require_compiled: bool = False, *, output=None) -> dict[str, Any]:
        if not isinstance(require_compiled, bool):
            raise WorkflowRefusal("INVALID_ARGUMENT", "require_compiled must be Boolean")
        target = None
        started = self.clock.monotonic()
        failure = None
        try:
            if output is not None:
                target = ReportTarget(self.project, output)
            try:
                with self.session.workflow_session() as lease:
                    report = self.run_with_lease(source_paths, lease, require_compiled=require_compiled)
            except SessionRefusal as exc:
                code = "COMPILE_TIMEOUT" if exc.code in {"ACTION_TIMEOUT", "READY_TIMEOUT"} else exc.code
                failure = WorkflowRefusal(code, str(exc), exc.details)
                report = {"schemaVersion": 1, "action": "compile", "project": str(self.project), **failure.as_dict()}
            except WorkflowRefusal as exc:
                failure = exc
                report = {"schemaVersion": 1, "action": "compile", "project": str(self.project), **exc.as_dict()}
                source_inputs = exc.details.get("observations", {}).get("sourceInputs")
                if source_inputs is not None:
                    report["sourceInputs"] = source_inputs
            report["wallSeconds"] = self.clock.monotonic() - started
            if target is not None:
                payload = json.dumps(_redact_value(report), sort_keys=True, separators=(",", ":")).encode("utf-8")
                try:
                    target.publish(payload)
                except ReportPathError as exc:
                    details = {**exc.details, "workflowReport": _redact_value(report)}
                    if failure is not None:
                        details["originalWorkflowError"] = _redact_value(failure.as_dict()["error"])
                    raise WorkflowRefusal(exc.code, str(exc), details) from exc
                artifact = {"reportPath": str(target.path), "reportSha256": hashlib.sha256(payload).hexdigest()}
                if failure is not None:
                    failure.details.update(artifact, reportPreserved=True)
                else:
                    report.update(artifact)
            if failure is not None:
                raise failure
            return report
        except ReportPathError as exc:
            raise WorkflowRefusal(exc.code, str(exc), exc.details) from exc
        finally:
            if target is not None:
                target.close()

    def run_with_lease(self, source_paths, lease, require_compiled: bool = False) -> dict[str, Any]:
        """Run the compile gate under a caller-owned project workflow lease."""
        if not isinstance(require_compiled, bool):
            raise WorkflowRefusal("INVALID_ARGUMENT", "require_compiled must be Boolean")
        phase = "source-validation"
        observations = {}
        try:
            sources = self._sources(source_paths)
            before_sources = self._hash_sources(sources, check_budget=lease.remaining)
            observations["sourceInputs"] = before_sources
            observations["editorIdentity"] = lease.identity.as_dict()
            phase = "editor-readiness"
            self.session.ready(expected_identity=lease.identity, deadline=lease.deadline)
            preflight, preflight_invocation, preflight_attempts = self._editor_state(lease, observations=observations, observation_key="editorBefore")
            phase = "compile"
            lease.verify_current()
            trigger = self.session.transport.recompile(str(self.project), pid=lease.identity.pid, timeout=lease.remaining(), deadline=lease.transport_deadline)
            lease.verify_current()
            observations["recompile"] = trigger.evidence()
            compile_data = trigger.value if isinstance(trigger.value, dict) else None
            if not trigger.ok:
                if trigger.exit_code == 6 and compile_data is not None:
                    raise WorkflowRefusal("COMPILATION_FAILED", "Unity reports a failed compilation", {"recompile": compile_data})
                raise native_failure(trigger, "recompile")
            state = self._validate_status(compile_data)
            if state["failed"] or state["errors"]:
                raise WorkflowRefusal("COMPILATION_FAILED", "Unity reports compile errors", {"recompile": compile_data})
            if state["status"] not in {"completed", "up_to_date"}:
                raise WorkflowRefusal("COMPILE_STATUS_INVALID", "Unity returned an unsupported compile status", {"status": state["status"]})
            if require_compiled and state["status"] == "up_to_date":
                raise WorkflowRefusal("COMPILATION_NOT_RUN", "Unity reports nothing to compile; this request requires an actual compile cycle", {"recompile": compile_data})
            phase = "source-postflight"
            after_sources = self._hash_sources(sources, check_budget=lease.remaining)
            if before_sources != after_sources:
                raise WorkflowRefusal("SOURCE_INPUT_CHANGED", "An explicit source input changed during the compile workflow", {"before": before_sources, "after": after_sources})
            phase = "console-ground-truth"
            console, ground_truth, console_invocation, console_attempts = self._fresh_ground_truth(lease, observations=observations)
            phase = "editor-postflight"
            final_editor, final_editor_invocation, final_editor_attempts = self._editor_state(lease, observations=observations, observation_key="editorAfter")
            lease.verify_current()
            return {
                "ok": True,
                "schemaVersion": 1,
                "action": "compile",
                "project": str(self.project),
                "editorIdentity": lease.identity.as_dict(),
                "compiled": state["status"] == "completed",
                "noCompilationReason": "up-to-date" if state["status"] == "up_to_date" else None,
                "sourceInputs": before_sources,
                "recompile": compile_data,
                "freshness": {
                    "groundTruth": ground_truth,
                    "editorBefore": {"result": preflight, "invocation": preflight_invocation, "retries": preflight_attempts},
                    "editorAfter": {"result": final_editor, "invocation": final_editor_invocation, "retries": final_editor_attempts},
                },
                "consoleObservation": {"result": console, "invocation": console_invocation, "groundTruth": ground_truth, "retries": console_attempts},
            }
        except WorkflowRefusal as exc:
            exc.details.update(lastPhase=phase, observations=observations)
            raise
        except SessionRefusal as exc:
            code = "COMPILE_TIMEOUT" if exc.code in {"ACTION_TIMEOUT", "READY_TIMEOUT"} else exc.code
            raise WorkflowRefusal(code, str(exc), {**exc.details, "lastPhase": phase, "observations": observations}) from exc

    def _sources(self, source_paths) -> list[pathlib.Path]:
        if isinstance(source_paths, (str, pathlib.Path)) or source_paths is None:
            source_paths = [] if source_paths is None else [source_paths]
        elif not isinstance(source_paths, (list, tuple)):
            raise WorkflowRefusal("SOURCE_INPUT_INVALID", "Source inputs must be a list of explicit paths")
        paths = list(source_paths)
        if not paths:
            raise WorkflowRefusal("SOURCE_INPUT_REQUIRED", "At least one explicit source input is required")
        resolved = []
        for item in paths:
            if not isinstance(item, (str, pathlib.Path)):
                raise WorkflowRefusal("SOURCE_INPUT_INVALID", "Each source input must be a path string", {"valueType": type(item).__name__})
            try:
                path = pathlib.Path(item)
                path = (self.project / path).resolve() if not path.is_absolute() else path.resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise WorkflowRefusal("SOURCE_INPUT_INVALID", "A source input path could not be resolved", {"reason": str(exc)}) from exc
            try:
                path.relative_to(self.project)
            except ValueError as exc:
                raise WorkflowRefusal("SOURCE_INPUT_OUTSIDE_PROJECT", "Source inputs must remain inside the explicit project", {"path": str(path)}) from exc
            if not path.is_file():
                raise WorkflowRefusal("SOURCE_INPUT_MISSING", "An explicit source input is not a regular file", {"path": str(path)})
            resolved.append(path)
        if len(set(resolved)) != len(resolved):
            raise WorkflowRefusal("SOURCE_INPUT_DUPLICATE", "Source inputs must not contain duplicate paths")
        return sorted(resolved)

    @staticmethod
    def _hash_sources(paths, *, check_budget=None):
        values = []
        budget = check_budget if check_budget is not None else lambda: None
        for path in paths:
            budget()
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    while True:
                        budget()
                        block = stream.read(1024 * 1024)
                        budget()
                        if not block:
                            break
                        digest.update(block)
            except OSError as exc:
                raise WorkflowRefusal("SOURCE_INPUT_UNREADABLE", "A source input could not be hashed", {"path": str(path), "reason": str(exc)}) from exc
            values.append({"path": str(path), "sha256": digest.hexdigest()})
        return values

    @staticmethod
    def _validate_status(payload):
        """The reply of `unity recompile`: its status, whether it failed and the errors it lists."""
        if not isinstance(payload, dict):
            raise WorkflowRefusal("COMPILE_STATUS_INVALID", "Compile status payload must be an object")
        required = {"status": str, "failed": bool, "errors": list}
        for key, kind in required.items():
            if key not in payload or not isinstance(payload[key], kind):
                raise WorkflowRefusal("COMPILE_STATUS_INVALID", "Compile status payload is missing a correctly typed required field", {"field": key})
        return {key: payload[key] for key in required}

    def _invoke(self, lease, group, name, params=None):
        lease.verify_current()
        result = self.session.transport.native(group, name, params, project=str(self.project), pid=lease.identity.pid,
                                               timeout=lease.remaining(), deadline=lease.transport_deadline)
        lease.verify_current()
        return result

    def _editor_state(self, lease, *, observations=None, observation_key="editorStatus"):
        """Read `editor status` until the Editor is ready and stopped; a compiling, reloading or loading Editor is waited for until the deadline."""
        while True:
            result, retries = self._invoke_readonly(lease, "editor", "status")
            invocation = {"argv": list(result.argv)}
            if observations is not None:
                observations[observation_key] = {"result": result.value, "invocation": invocation, "retries": retries}
            state = result.value
            if not isinstance(state, dict):
                raise WorkflowRefusal("EDITOR_STATUS_INVALID", "Editor status result must be an object")
            project = state.get("projectPath")
            if not isinstance(project, str):
                raise WorkflowRefusal("EDITOR_STATUS_INVALID", "Editor status omitted its project path")
            lease.remaining()
            try:
                canonical = self.session.platform.canonicalize_external_project(project, deadline=lease.transport_deadline)
            except subprocess.TimeoutExpired as exc:
                lease.remaining()
                raise WorkflowRefusal("CLI_TIMEOUT", "Editor status path conversion reached its local deadline") from exc
            except (OSError, RuntimeError, TypeError, ValueError):
                canonical = None
            if state.get("status") == "ready" and state.get("playMode") == "stopped" and canonical == lease.identity.project \
                    and state.get("compiling") is not True and state.get("domainReloadInProgress") is not True:
                return state, invocation, retries
            busy = state.get("compiling") is True or state.get("domainReloadInProgress") is True or state.get("status") in EDITOR_BUSY_STATUSES
            if not busy or canonical != lease.identity.project:
                raise WorkflowRefusal("EDITOR_NOT_READY", "Exact Editor must be ready and stopped", {"editorStatus": state})
            try:
                self._sleep(lease)
            except SessionRefusal as exc:
                raise WorkflowRefusal("EDITOR_NOT_READY", "The Editor was still busy when the workflow deadline came", {"editorStatus": state}) from exc

    def _invoke_readonly(self, lease, group, name, params=None):
        """Read from the Editor, retrying while it reports busy or a call times out; any other failure refuses."""
        retries = []
        while True:
            result = self._invoke(lease, group, name, params)
            if result.ok:
                return result, retries
            retries.append(result.evidence())
            if result.status not in RETRYABLE_STATUSES:
                raise native_failure(result, f"{group} {name}")
            try:
                self._sleep(lease)
            except SessionRefusal as exc:
                raise WorkflowRefusal("COMPILE_TIMEOUT", f"Could not read Pipeline {group} {name} state before the workflow deadline", {"retries": retries}) from exc

    def _fresh_ground_truth(self, lease, *, observations=None):
        retries = []
        while True:
            try:
                result, attempt_retries = self._invoke_readonly(lease, "console", "status")
                retries.extend(attempt_retries)
            except SessionRefusal as exc:
                raise WorkflowRefusal("GROUND_TRUTH_STALE", "A fresh, idle Editor groundTruth sample did not arrive before the workflow deadline", {"retries": retries}) from exc
            data = result.value
            if observations is not None:
                observations["consoleObservation"] = {"result": result.value, "invocation": {"argv": list(result.argv)}, "retries": list(retries)}
            if not isinstance(data, dict):
                raise WorkflowRefusal("GROUND_TRUTH_INVALID", "Console status result must be an object")
            ground_truth = data.get("groundTruth")
            if "groundTruth" in data and ground_truth is None:
                retries.append({"code": "GROUND_TRUTH_PENDING", "message": "Editor groundTruth sample is pending after assembly reload",
                                "invocation": {"argv": list(result.argv)}})
                try:
                    self._sleep(lease)
                except SessionRefusal as exc:
                    raise WorkflowRefusal("GROUND_TRUTH_STALE", "A fresh, idle Editor groundTruth sample did not arrive before the workflow deadline",
                                          {"retries": retries}) from exc
                continue
            if not isinstance(ground_truth, dict):
                raise WorkflowRefusal("GROUND_TRUTH_INVALID", "Console status omitted its Editor groundTruth sample")
            age = ground_truth.get("ageMs")
            if not isinstance(age, int) or isinstance(age, bool) or age < 0:
                raise WorkflowRefusal("GROUND_TRUTH_INVALID", "Editor groundTruth ageMs must be a nonnegative integer")
            if not isinstance(ground_truth.get("compiling"), bool) or not isinstance(ground_truth.get("compilationFailed"), bool):
                raise WorkflowRefusal("GROUND_TRUTH_INVALID", "Editor groundTruth compiling and compilationFailed must be Boolean fields")
            if ground_truth.get("compilationFailed") is True:
                raise WorkflowRefusal("COMPILATION_FAILED", "Fresh Editor groundTruth reports a compile failure")
            if age <= GROUND_TRUTH_MAX_AGE_MS and ground_truth.get("compiling") is False:
                if ground_truth.get("compilationFailed") is not False:
                    raise WorkflowRefusal("GROUND_TRUTH_INVALID", "Editor groundTruth compilationFailed must be Boolean false")
                return data, ground_truth, {"argv": list(result.argv)}, retries
            try:
                self._sleep(lease)
            except SessionRefusal as exc:
                code = "GROUND_TRUTH_STALE" if age > GROUND_TRUTH_MAX_AGE_MS else "EDITOR_NOT_READY"
                raise WorkflowRefusal(code, "A fresh, idle Editor groundTruth sample did not arrive before the workflow deadline", {"ageMs": age, "compiling": ground_truth.get("compiling")}) from exc

    def _sleep(self, lease):
        remaining = lease.remaining()
        self.clock.sleep(min(self.poll_interval, remaining))
