from __future__ import annotations

from typing import Any

SLOWEST = 5
LINE_LIMIT = 300


def _first_line(value: Any) -> Any:
    """A failure message is its first nonempty line, so one failure never costs kilobytes of trace."""
    if not isinstance(value, str):
        return value
    for line in value.splitlines():
        if line.strip():
            return line.strip()[:LINE_LIMIT]
    return ""


def _short(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _first_line(value) if key in {"message", "stackTrace"} else value for key, value in item.items()}


def _class_of(full_name: str) -> str:
    from .testing import TestWorkflow
    return TestWorkflow._test_class(full_name) or full_name


def _slowest(results: list[Any]) -> dict[str, Any]:
    timed = [item for item in results if isinstance(item, dict) and isinstance(item.get("fullName"), str)
             and type(item.get("duration")) in {int, float}]
    classes: dict[str, float] = {}
    for item in timed:
        name = _class_of(item["fullName"])
        classes[name] = classes.get(name, 0.0) + item["duration"]
    return {"slowestClasses": [{"class": name, "duration": round(total, 3)} for name, total in sorted(classes.items(), key=lambda row: -row[1])[:SLOWEST]],
            "slowestTests": [{"fullName": item["fullName"], "duration": item["duration"]} for item in sorted(timed, key=lambda item: -item["duration"])[:SLOWEST]]}


def workflow_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Summarize the workflow verdict and canonical leaves without counting raw echoes."""
    summary = {"ok": report.get("ok")}
    for key in ("action", "route", "mode", "state", "statusPath", "next", "project", "compiled", "noCompilationReason",
                "reportPath", "reportSha256", "summary", "duration", "wallSeconds"):
        if key in report:
            summary[key] = report[key]
    error = report.get("error")
    results = report.get("results")
    if isinstance(error, dict):
        concise = {key: error[key] for key in ("code", "message") if key in error}
        details = error.get("details")
        if isinstance(details, dict):
            if error.get("code") == "OFFLINE_TEST_FAILED":
                if "summary" not in summary and isinstance(details.get("summary"), dict):
                    summary["summary"] = details["summary"]
                if results is None:
                    results = details.get("results")
            concise["details"] = {key: value for key, value in details.items() if value is None or type(value) in {str, int, float, bool}}
            original = details.get("originalWorkflowError")
            if isinstance(original, dict):
                concise["details"]["originalWorkflowError"] = {key: original[key] for key in ("code", "message") if key in original}
        summary["error"] = concise
    if isinstance(results, list):
        summary["nonpassingTests"] = [
            _short(item) for item in results
            if isinstance(item, dict) and item.get("status") != "Passed"
        ]
        summary.update(_slowest(results))
    return summary
