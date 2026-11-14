# Test discovery and execution

Read [foundation routing and safety](foundation.md) first. Only one test operation may be active for the
project. Before connected or top-level execution, confirm the exact project, compilation success, stopped
PlayMode, and that every open scene is clean. Save authorized owned scenes or refuse before submission; a
batch-mode save dialog can block the dispatcher.

## Discovery and execution

Live discovery is `test list --mode editor|playmode`: one `{FullName, Mode, Assembly, Categories, Explicit}`
record per test. Preserve those fields; a new test must appear after compilation and disappear after owned
deletion and reload.

Use the public host gate for an exact method, exact class, exact assembly, or their intersection. Omitting
all selectors runs the entire requested mode; omitting `--mode` selects EditMode.

```text
python3 unity-agent-harness/scripts/unity_workflow.py test --project <exact-project> --route connected \
  --test-class <namespace.class> --assembly <exact-assembly> --source <file.cs> --output <new-report.json>
```

Use `--test-name <exact-FullName>` for one discovered leaf, `--test-class <namespace.class>` for every discovered method
in that class, and `--assembly <exact-assembly>` for an assembly. A whole-assembly run needs no `--source`: the
helper takes the `.asmdef`, `.asmref` and `.cs` files under the folder of the one `.asmdef` in `Assets` or `Packages`
that declares the assembly (leaving out assemblies nested inside it) as its freshness inputs; an assembly from a
registry package is not found there, so pass `--source` for it. Name and class are mutually exclusive;
either can combine with assembly. Each selector may occur only once; repeated selectors refuse.
For a parameterized test, copy the complete case fullname including its arguments from discovery;
the method name alone does not select a group. Repeat `--source` for each explicit source input,
or use the [JSON source list](editor.md#compile-gate). The connected route
compiles and freezes a nonzero native discovery set, then submits one asynchronous native suite and polls to
fresh terminal status. The SDK accepts one case-insensitive partial name or assembly filter. The gate refuses
any selection that this filter would widen, including identical fullnames in separate assemblies: the native
SDK passes only fullnames to its runner. Use offline execution for such a selection.

Prepare a bound discovery plan while the exact project Editor is connected, then stop that owned Editor
before offline execution:

```text
python3 unity-agent-harness/scripts/unity_workflow.py test-plan --project <exact-project> \
  --test-name <exact-FullName> --assembly <exact-assembly> --source <file.cs> --output <new-plan.json>
python3 unity-agent-harness/scripts/unity_workflow.py test --project <exact-project> --route offline \
  --test-name <exact-FullName> --assembly <exact-assembly> --discovery <new-plan.json> \
  --source <file.cs> --output <new-report.xml>
```

The plan freezes native rows, exact selected assembly/fullname pairs, project, mode, Editor identity/version,
source hashes, and the source/assembly/DLL inventory. Offline execution requires matching selectors, project,
mode, version, and unchanged inputs; a raw discovery response cannot substitute for a plan. Offline class,
assembly, combined, and whole-mode selections require this plan. An offline single exact fullname can run
without a plan and validates the sole exact leaf. Native offline names use anchored escaped regex filters;
assembly selection forwards `-assemblyNames` after the CLI's `--` separator. Whole-mode runs pass no filter.
The offline runner performs its own compile step, so no separate connected compile proof is claimed.

Both routes require the complete nonzero frozen selection, consistent summary counts, and every selected
leaf passed, with no failed, skipped, or inconclusive leaves. A connected run is `test run --async-tests`
followed by `test status` polls until `status` is `completed`; the reply is camelCase: `summary.total`,
`summary.passed|failed|skipped|inconclusive`, and `results[]` of `fullName`, `status`, `duration`, `message`,
`stackTrace`. A failed run's final poll exits nonzero but still carries that payload, which the helper
publishes. A synchronous `test run` reply has no summary, so the helper never uses it. Offline XML identifies
each leaf through its Assembly parent suite; distinct assemblies with the same fullname remain distinct
identities. Missing, extra, or duplicate identities fail even when a failing report is retained. Starting a run
is not completion. Explicit connected tests require `--include-explicit`; the offline CLI exposes no inclusion
switch and refuses that option.

Choose a report path in a persistent directory outside the project's `Assets`, `Packages`,
`Library`, and `Temp` trees. A regular file already at that path is an older report: the helper removes it before
the run, so it is never read back as this run's, and writes the new report there; a symlink or directory at the
path is refused. The helper stages native output in a private sibling directory and publishes exclusively. Connected output is sanitized JSON. Offline output is the raw NUnit XML,
which may contain test messages, stack traces, and project-local paths; treat it as local diagnostic data.
Connected reports record the helper's final `ok/error`, the raw terminal status, source hashes
and available postflight observations. Refusals preserve partial evidence when publication is possible.
`--format summary` emits canonical counts, the five slowest classes and tests, and the nonpassing leaves once, each
failure's message and stack trace cut to its first line, with concise error fields and artifact references; full
JSON remains the default. Offline files retain native NUnit XML,
whose native pass result alone cannot prove the helper accepted its source, identity and lifecycle checks. Do not use Unity's temporary report location or select a
newest report from a shared directory.

Root `duration` and each leaf `duration` are validated as finite, nonnegative metrics.

Pipeline lists a test's explicit status from each leaf. An inherited NUnit `[Explicit]` can be omitted from
that metadata even though execution honors it, so a default suite can refuse on a skipped or missing expected
leaf. Inspect the test and its fixture attributes; use `--include-explicit` only when intentionally authorizing
those tests. Never treat skipped leaves as passed.

Connected tests have no run id. Freshness rests on the exact Editor and source checks and a cooperating
per-project host lock; before submitting, the helper refuses when `test status` already reports `running`.

A connected run that is still going at the helper's `--timeout` is not a failure. The call writes the report
with `"state": "running"` (its path is `statusPath`), prints that state and exits 4, and the run keeps going: the
helper never cancels it (`unity test cancel` stops it, on request). Run the same `test` command again to collect
it: the helper finds the run through a marker (`<tmp>/unity-session-<uid>/<sha256 of the project path>.testrun`,
written before the run is triggered) and does not start a second run (starting one stops the first). It collects
that run once, and only if the project's `.cs`, `.asmdef`, `.asmref` and `Packages/manifest.json` are unchanged since
the marker was written; after any change, or with no marker, it compiles and starts a fresh run (a change while the run is still going is refused until it ends, so the compile cannot fail it). A different request
cannot collect it and is refused while the run is `running`. A raw `unity test cancel` leaves the marker; the next
helper call that sees the run `cancelled` drops it.

The change check covers only those files. It does not see `*.rsp`, precompiled `.dll` files, scripting defines in
`ProjectSettings/ProjectSettings.asset`, `Packages/packages-lock.json`, `file:` packages outside the project, symlinked
folders, or data changes to prefabs, scenes and ScriptableObjects made outside the Editor: after a change there, start a
fresh run instead of collecting the old one.

While a run is `running` the Editor answers no other call without timing out, and Pipeline logs an error that NUnit
counts as an unhandled log, failing the running test. So until the run ends, every other helper call for that
project (`compile`, `console`, `unity_probe.py`, any native command through the helper) is refused with
`TEST_RUN_IN_PROGRESS` and sent nothing but one `test status` read, which Pipeline answers off the main thread. The
marker is removed as soon as any call sees a finished, cancelled or gone run (or another Editor process). When the status
cannot be read and the marker is past its run's deadline, the call proceeds and reports the Editor's real state instead. The native exit codes are named: `EDITOR_UNAVAILABLE` (17),
`EDITOR_AWAITING_INPUT` (18), `EDITOR_NOT_RUNNING` (12), `NATIVE_TIMEOUT` (14). Offline execution is the
top-level `unity test` and reserves time for Unity's native timeout and cleanup before the host deadline; if
the host itself times out, Editor cleanup remains unverified.

Parse only the caller-owned report path with a standard XML parser. Return its requested mode, counts,
failures, start/end time, duration, and exact test identities. Missing or malformed XML fails.

## Diagnose simulated-physics variation

For a physics test that varies with execution history, measure fixture-owned bodies, shared registries,
physics settings and initial body state before blaming a predecessor. Trace the first divergent fixed
step in repeated and predecessor/target runs. Compare the same fixture in a separately owned physics
scene; assert that its `PhysicsScene` is valid and differs from the default world, move every owned
collider/body into it, simulate it explicitly and close it in teardown. EditMode preview scenes require
`EditorSceneManager.ClosePreviewScene`; `SceneManager.CreateScene` with local physics is a PlayMode route.
Confirm that the bodies actually move and the original assertions still hold. Identical traces in one
Editor/version establish that comparison only, not cross-platform determinism or the cause of a
historical failure. Keep the original failure threshold while investigating.
See [PhysicsScene.Simulate](https://docs.unity.com/en-us/engine/6000.5/script-reference/unityengine/physicsscene/simulate)
and [preview-scene cleanup](https://docs.unity.com/en-us/engine/6000.3/script-reference/unityeditor/scenemanagement/editorscenemanager/newpreviewscene).
