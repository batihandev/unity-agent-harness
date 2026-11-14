#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import pathlib
import os
import stat
import sys
from typing import Sequence, TextIO

from unity_cli.compile import CompileWorkflow, WorkflowRefusal
from unity_cli.lifecycle import SessionController, SessionRefusal
from unity_cli.platforms import PlatformAdapter, current_platform
from unity_cli.transport import CliTransport, _redact_value
from unity_cli.testing import TestWorkflow, WorkflowTestRefusal, assembly_sources
from unity_cli.console import ConsoleWorkflow, WorkflowConsoleRefusal
from unity_cli.summaries import workflow_summary


class InputRefusal(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def as_dict(self):
        return {"ok": False, "error": {"code": self.code, "message": str(self), "details": self.details}}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InputRefusal("INVALID_ARGUMENT", message)


class SingleSelection(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise InputRefusal("INVALID_ARGUMENT", f"Supply {option_string} only once; use one exact selection per run")
        setattr(namespace, self.dest, value)


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Run native Unity compile, exact-test, and console workflows.")
    parser.add_argument("action", choices=("compile", "test", "test-plan", "console"))
    parser.add_argument("--project", required=True, metavar="PATH")
    parser.add_argument("--source", action="append", metavar="FILE", help="Explicit project-relative or absolute source file to include in the stable input snapshot; repeat as needed.")
    parser.add_argument("--sources-file", action=SingleSelection, metavar="JSON", help="JSON array of explicit source paths; can combine with --source.")
    parser.add_argument("--require-compiled", action="store_true", help="Refuse an up_to_date no-op; require an actual compile.")
    parser.add_argument("--route", choices=("connected", "offline"), help="Test route: connected exact Editor or top-level offline Unity CLI.")
    parser.add_argument("--mode", choices=("EditMode", "PlayMode"))
    parser.add_argument("--test-name", action=SingleSelection, metavar="FULLNAME", help="Exact NUnit FullName.")
    parser.add_argument("--test-class", action=SingleSelection, metavar="CLASS", help="Exact namespace-qualified test class.")
    parser.add_argument("--assembly", action=SingleSelection, metavar="ASSEMBLY", help="Exact test assembly; can combine with a name or class.")
    parser.add_argument("--discovery", metavar="PLAN-JSON", help="Bound test-plan report required for offline suite selection.")
    parser.add_argument("--output", metavar="FILE", help="Initially absent persistent report path outside project-owned data directories.")
    parser.add_argument("--editor-version", metavar="VERSION", help="Offline Editor version override; defaults to ProjectVersion.txt.")
    parser.add_argument("--include-explicit", action="store_true", help="Deliberately include an explicit connected test.")
    parser.add_argument("--level", choices=("log", "warn", "error"), help="Minimum native console severity.")
    parser.add_argument("--tail", type=int, default=100, metavar="1..1000", help="Maximum number of most recent entries returned to stdout (default 100).")
    parser.add_argument("--since", type=int, help="Native console cursor from a prior response; supply with --since-session.")
    parser.add_argument("--since-session", metavar="SESSION", help="Native source session belonging to --since.")
    parser.add_argument("--cli", metavar="PATH")
    parser.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS")
    parser.add_argument("--format", choices=("json", "summary"), default="json", help="Full JSON response or concise JSON summary; saved reports retain complete evidence.")
    return parser


def _validate(args: argparse.Namespace) -> pathlib.Path:
    project = pathlib.Path(args.project).expanduser()
    if not project.is_dir():
        raise InputRefusal("PROJECT_NOT_FOUND", "The project directory does not exist", {"project": str(project)})
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise InputRefusal("INVALID_TIMEOUT", "--timeout must be finite and greater than zero")
    if args.sources_file is not None:
        if args.action not in {"compile", "test", "test-plan"}:
            raise InputRefusal("INVALID_ARGUMENT", "--sources-file applies only to compile, test, and test-plan")
        fd = None
        try:
            path = pathlib.Path(args.sources_file).expanduser()
            if path.is_symlink():
                raise ValueError("The source list must be a plain file")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("The source list must be a plain file")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("The source list exceeds one MiB")
            values = json.loads(content.decode("utf-8"))
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError("Supply a nonempty JSON array of nonempty source paths")
            args.source = [*(args.source or []), *values]
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise InputRefusal("SOURCE_INPUT_INVALID", str(exc)) from exc
        finally:
            if fd is not None:
                os.close(fd)
    if args.action == "compile" and not args.source:
        raise InputRefusal("INVALID_ARGUMENT", "Compile requires at least one explicit --source FILE")
    if args.action not in {"test", "test-plan"} and (args.test_class or args.assembly or args.discovery):
        raise InputRefusal("INVALID_ARGUMENT", "Test selection options apply only to test and test-plan")
    if args.test_class and args.test_name:
        raise InputRefusal("INVALID_ARGUMENT", "Choose --test-name or --test-class")
    if args.action == "test-plan" and (args.route or args.discovery):
        raise InputRefusal("INVALID_ARGUMENT", "test-plan uses connected discovery and accepts no --route or --discovery")
    if args.action in {"test", "test-plan"}:
        required = [("--output", args.output)]
        if args.action == "test":
            required.append(("--route", args.route))
        missing = [name for name, value in required if not value]
        if missing:
            raise InputRefusal("INVALID_ARGUMENT", "Test action is missing required arguments", {"missing": missing})
        if not args.source and args.assembly:
            try:
                args.source = assembly_sources(project, args.assembly)
            except WorkflowTestRefusal as exc:
                raise InputRefusal(exc.code, str(exc), exc.details) from exc
        if not args.source:
            raise InputRefusal("SOURCE_INPUT_REQUIRED", "Test requires --assembly or at least one explicit --source FILE")
        if args.require_compiled:
            raise InputRefusal("INVALID_ARGUMENT", "--require-compiled applies only to compile")
    if args.action == "console":
        if not args.output:
            raise InputRefusal("INVALID_ARGUMENT", "Console requires an explicit --output FILE")
        if args.source or args.require_compiled or args.route or args.mode or args.test_name or args.include_explicit or args.editor_version:
            raise InputRefusal("INVALID_ARGUMENT", "Compile and test arguments cannot be combined with console")
    return project.resolve()


RUNNING_EXIT = 4


def _exit_code(code: str) -> int:
    if code in {"PROJECT_NOT_FOUND", "INVALID_TIMEOUT", "INVALID_ARGUMENT", "SOURCE_INPUT_REQUIRED", "SOURCE_INPUT_OUTSIDE_PROJECT", "SOURCE_INPUT_MISSING"}:
        return 2
    if code.endswith("TIMEOUT"):
        return 5
    if code in {"CLI_NOT_FOUND", "SESSION_NOT_RUNNING", "PROCESS_INVENTORY_UNKNOWN"}:
        return 3
    return 6


def main(argv: Sequence[str] | None = None, *, stdout: TextIO = sys.stdout, platform: PlatformAdapter | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        project = _validate(args)
        adapter = platform or current_platform()
        if type(adapter) is PlatformAdapter:
            raise InputRefusal("PLATFORM_UNSUPPORTED", "This operating system has no session adapter")
        transport = CliTransport(adapter, cli_override=args.cli, command_timeout=args.timeout)
        session = SessionController(str(project), transport, adapter, timeout=args.timeout)
        if args.action == "compile":
            workflow = CompileWorkflow(project, session)
            result = workflow.run(args.source, require_compiled=args.require_compiled, **({"output": args.output} if args.output is not None else {}))
        else:
            if args.action in {"test", "test-plan"}:
                workflow = TestWorkflow(project, session)
                selection = {"mode": args.mode or "EditMode", "test_name": args.test_name,
                             "source_paths": args.source, "output": args.output, "include_explicit": args.include_explicit}
                if args.test_class is not None:
                    selection["test_class"] = args.test_class
                if args.assembly is not None:
                    selection["assembly"] = args.assembly
                if args.action == "test-plan":
                    result = workflow.plan(**selection)
                else:
                    if args.discovery is not None:
                        selection["discovery"] = args.discovery
                    result = workflow.run(route=args.route, editor_version=args.editor_version, **selection)
            elif args.action == "console":
                workflow = ConsoleWorkflow(project, session)
                result = workflow.run(output=args.output, level=args.level, tail=args.tail, since=args.since, since_session=args.since_session)
        print(json.dumps(_redact_value(workflow_summary(result) if args.format == "summary" else result), sort_keys=True), file=stdout)
        # A run still going is neither a pass nor a failure: exit 4, run the same command again to collect it.
        return RUNNING_EXIT if isinstance(result, dict) and result.get("state") == "running" else 0
    except (InputRefusal, SessionRefusal, WorkflowRefusal, WorkflowTestRefusal, WorkflowConsoleRefusal) as exc:
        result = exc.as_dict()
        if "args" in locals() and args.format == "summary":
            result = workflow_summary(result)
        print(json.dumps(_redact_value(result), sort_keys=True), file=stdout)
        return _exit_code(exc.code)


if __name__ == "__main__":
    raise SystemExit(main())
