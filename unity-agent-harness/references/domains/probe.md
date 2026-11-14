# C# probe execution

Use `scripts/unity_probe.py` to run one project C# source through `script run` and require an exact current
console completion message. Read the [foundation guide](foundation.md) first. `script run` takes the file
(project-relative or absolute), an optional `--entry` (`Type.Method`), `--timeout` in seconds and `--dry-run`.
The helper also uses `console get`, `editor status` and, for play ownership, `editor.playmode start|stop`.

```bash
python3 scripts/unity_probe.py --project "$PROJECT" \
  --source AgentScripts/Probe.cs --entry Probe.Run \
  --completion-text 'PROBE_DONE {run_id}' \
  --output "$REPORT_DIR/probe.json" --timeout 120
```

The exact completion template must appear literally in the C# source, such as
`UnityEngine.Debug.Log("PROBE_DONE {run_id}");`. The helper generates a unique run
ID and replaces that entire template in a private source copy. Other `{run_id}`
occurrences remain unchanged. Project source stays untouched. The staged path is
converted for the selected native CLI host.

A plain literal completion message is also accepted. Its evidence correlates the
message with the current console sequence after invocation; it cannot establish
exclusive authorship when other scripts can emit the same message. Prefer the
run ID template for probes that need invocation-specific completion evidence.

A stopped Editor runs the probe in edit mode (no play, nothing started); a playing Editor runs it in play.
`--enter-play` instead authorizes a stopped-to-playing transition owned by this helper. Dirty scenes refuse before
that request. The helper reserves time to restore even if the play request crosses the probe
action deadline; an uncertain request remains owned for cleanup. It restores
stopped state only for that transition and only while the leased Editor identity
remains current. Existing playing state remains caller-owned. A paused Editor is refused
(`PROBE_EDITOR_PAUSED`): unpause or stop it first.
The helper never launches or exits the Editor and never clears the console.

Edit-mode probes must not change scenes. The helper reads the open scenes' unsaved state before and
after the run; a scene the probe made dirty fails the run with `PROBE_CHANGED_SCENE` (scenes already
dirty before are not blamed on the probe). Nothing is reverted or saved.

Use compile-only checks without an entry point or completion marker:

```bash
python3 scripts/unity_probe.py --project "$PROJECT" \
  --source AgentScripts/Probe.cs --dry-run \
  --output "$REPORT_DIR/compile-check.json"
```

Compile-only mode runs `script run --dry-run`: the source compiles against the Editor's loaded references and
defines, nothing is loaded or executed, and the reply must show no assembly and zero execution time. It
preserves the current play state. A failed compile exits 6 and its JSON reply carries no diagnostics, so the
helper repeats the same dry run in human format and keeps that compiler output (`compilerOutput`,
`compilerErrors`) in the refusal as `PROBE_COMPILE_FAILED`. A dry run does not resolve `--entry`; an entry that
does not exist fails only on a real run. Project compilation is owned by the [compile gate](editor.md).

The report path must be absent and its parent directory must exist. Reports belong
outside `Assets`, `Packages`, `Library`, and `Temp`. Each report retains complete
native command responses, source and staged hashes, exact Editor identity, fresh
console cursor, run ID, correlation mode, completion entry, and restoration status.
Successful stdout includes the report path and SHA-256. Failure returns a nonzero
exit status with a retained report when report publication is possible.

The helper pins every call to the leased Editor's PID and project, and refuses an Editor whose
`editor status` names another project, malformed or failed command results, console reset or dropped
history, Error/Assert/Exception entries after the initial cursor, source hash changes, Editor replacement,
play-state changes, and deadline expiry. A failed run is named by its exit code: `PROBE_ENTRY_FAILED` (the file
or entry was not found), `PROBE_RUNTIME_FAILED` (the entry threw), `PROBE_COMPILE_FAILED`, and for 12, 17, 18
and 14 `EDITOR_NOT_RUNNING`, `EDITOR_UNAVAILABLE`, `EDITOR_AWAITING_INPUT` and `NATIVE_TIMEOUT`. A stale,
seeded, partial, or wrong completion message cannot establish success. Each poll
uses the shared [console validator](console.md). A current unseeded message containing the
marker with additional text refuses promptly as `PROBE_COMPLETION_MISMATCH` and retains
`nearestLine`; copy the complete emitted message or emit the exact template. A restoration
failure is recorded separately and does not replace the original probe failure. Console evidence remains bounded
by the native buffer; a reported gap prevents acceptance. Probe code must emit
completion only after its own assertions and asynchronous work finish. Exceptions
or messages produced after that terminal boundary require a separate observation.

Python callers use `ProbeWorkflow(project, session).run(source, entry,
completion_text, output, enter_play=False)` or `.check(source, output)` with a
`SessionController`. The session timeout bounds the operation. `ProbeRefusal`
provides a structured code and retained artifact details.

## Compile several probes

Run the active probe for normal iteration: its execution includes compilation. To check probes that will not
execute, run the single-file dry run once per source; each keeps its own report. Do it once at final
integration, when changed APIs or references could break other probes. Repeated checks add no evidence when
source, references and Editor identity have not changed.
