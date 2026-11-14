# Editor state and compile gate

Read [foundation routing and safety](foundation.md) first. A successful call is not proof of the resulting
Editor state: read it back with `editor status` or the owner of the postcondition.

## State

`unity command editor status` returns `status`, `compiling`, `domainReloadInProgress`, `playMode`
(`stopped`, `playing`, `paused`), `projectPath`, `unityVersion` and `role`. Exit 17 means the Editor is
loading, compiling or reloading; exit 18 means a dialog blocks it. `scene list_open` lists each open scene
with `isDirty`. Play mode is `editor.playmode start|stop|pause`; before starting, every open scene must be
clean, and always stop play afterwards and observe `stopped`. Unpersisted play-mode changes are lost.

## Compile gate

```bash
python3 scripts/unity_workflow.py compile --project PATH --source FILE [--source FILE ...] \
  [--output new-report.json] [--require-compiled] [--format summary]
```

Pass every intended project source file; the gate does not guess a glob. It hashes those files, takes the
shared project lock, waits until the exact Editor is `ready`, requires it stopped, and runs `unity recompile`
(exit 12 means no Editor answered). It accepts a clean `completed` or `up_to_date` reply with no errors,
unchanged source hashes, a fresh `console status` ground truth (`compilationFailed` false, sample no older
than 2 s) and a ready, stopped final `editor status`. `up_to_date` is a successful no-op and reports
`compiled: false`; `--require-compiled` refuses it.

`--sources-file sources.json` takes a plain UTF-8 JSON array of explicit paths (at most 1 MiB) and combines
with `--source`. Paths are confined to the project and must exist; keep deleted paths out.

`--output` keeps the verdict, source hashes, native evidence, wall time and last phase, including on refusal.
The report path must be new, with an existing plain parent outside the project's data trees. `--format
summary` prints concise JSON while the report keeps the full evidence.

Refusals carry the native exit code by name: `EDITOR_NOT_RUNNING` (12), `EDITOR_UNAVAILABLE` (17),
`EDITOR_AWAITING_INPUT` (18), `NATIVE_TIMEOUT` (14), `COMPILATION_FAILED`. Compiling while playing is refused.
Compile flags and runtime exceptions are separate observations: an exception burst does not by itself show a
compiler failure. Safe Mode (compile errors at startup) never loads Pipeline: use `unity status` for its
errors and `unity_session.py recover`.
