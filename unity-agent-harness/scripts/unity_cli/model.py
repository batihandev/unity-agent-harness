from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
import math
import subprocess
import time


@dataclass(frozen=True)
class Deadline:
    """Immutable expiry measured by its owning monotonic clock."""

    expiry: float
    clock: Any = time

    @classmethod
    def after(cls, timeout: float, clock: Any = time) -> "Deadline":
        now = clock.monotonic()
        return cls(now + timeout if math.isfinite(timeout) and timeout > 0 else now, clock)

    def remaining(self, cap: float | None = None) -> float:
        remaining = max(0.0, self.expiry - self.clock.monotonic())
        return remaining if cap is None else min(remaining, cap)

    def capped(self, timeout: float) -> "Deadline":
        expiry = self.clock.monotonic() + timeout
        return self if expiry >= self.expiry else Deadline(expiry, self.clock)

    def require_remaining(self, operation: str, cap: float | None = None) -> float:
        remaining = self.remaining(cap)
        if not 0 < remaining < float("inf"):
            raise subprocess.TimeoutExpired(operation, remaining)
        return remaining


@dataclass(frozen=True)
class ProcessRecord:
    project: str
    pid: int
    started_at: str
    executable: str
    role: str = "editor"
    parent_pid: int | None = None
    log_file: str | None = None

    def identity_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "pid": self.pid,
            "startedAt": self.started_at,
        }


@dataclass(frozen=True)
class RegistryObservation:
    project: str
    pid: int
    started_at: str | None
    source: str
    pipeline_port: int | None = None


@dataclass(frozen=True)
class InventorySnapshot:
    known: bool
    records: tuple[ProcessRecord, ...]
    error: str | None = None
    code: Literal["PROCESS_INVENTORY_TIMEOUT"] | None = None


@dataclass(frozen=True)
class CommandResult:
    success: bool
    data: Any = None
    code: str | None = None
    message: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any = None, **diagnostics: Any) -> "CommandResult":
        return cls(True, data=data, diagnostics=diagnostics)

    @classmethod
    def failed(cls, code: str, message: str, **diagnostics: Any) -> "CommandResult":
        return cls(False, code=code, message=message, diagnostics=diagnostics)


@dataclass(frozen=True)
class SessionIdentity:
    project: str
    pid: int
    started_at: str
    executable: str
    log_file: str | None = None

    @classmethod
    def from_process(cls, record: ProcessRecord) -> "SessionIdentity":
        return cls(record.project, record.pid, record.started_at, record.executable, record.log_file)

    def as_dict(self) -> dict[str, Any]:
        return {"project": self.project, "pid": self.pid, "startedAt": self.started_at}


EXIT_STATUS = {0: "OK", 2: "BAD_ARGS", 6: "FAILED", 11: "NOT_FOUND", 12: "NO_EDITOR", 13: "UNKNOWN_COMMAND",
               14: "TIMEOUT", 17: "UNAVAILABLE", 18: "AWAITING_INPUT"}


@dataclass(frozen=True)
class NativeResult:
    """One Unity CLI call: its exit code, the parsed JSON it printed and what it said on failure."""

    exit_code: int
    value: Any = None
    stdout: str = ""
    stderr: str = ""
    argv: tuple[str, ...] = ()
    code: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def status(self) -> str:
        """OK, UNAVAILABLE (loading, compiling, reloading), AWAITING_INPUT (a modal blocks), TIMEOUT, NO_EDITOR, FAILED or ERROR."""
        return EXIT_STATUS.get(self.exit_code, "ERROR")

    def evidence(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "exitCode": self.exit_code, "status": self.status, "code": self.code,
                "message": self.message, "stdout": self.stdout, "stderr": self.stderr}
