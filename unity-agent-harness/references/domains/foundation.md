# Foundation routing and safety

Targets Unity `6000.6.2f1`, Unity CLI `1.0.0`, `com.unity.pipeline` `1.0.0`. Live discovery wins when a
later installed surface differs.

## Exact project targeting

Pass `--project-path` on every `unity command` invocation: it picks its project from the working
directory otherwise. From WSL, keep a Windows Editor's project on a drive-backed path (`D:\...`) and pass
the Windows form; opening it from `\\wsl.localhost\...` crashes the Editor during import. Stop before a risky
transition when the target is ambiguous, importing, compiling, modal-blocked, or dirty in a way that could
discard work. `--pid` pins one Editor when several share a project; the helpers pin the exact PID they leased.

## Live discovery

Read a command's parameters from the connected Editor before calling it:

```bash
unity commands --include-editor --json          # the whole catalog (rejects --project-path)
unity command --project-path "$PROJECT" test run --help
```

Prefer a Pipeline command that owns the behavior. For anything else, `script eval` runs a C# snippet and
`script run` runs one static method of a `.cs` file. `script eval --dry-run` and `script run --dry-run` compile
only.

## Calling commands

```bash
unity command --project-path "$PROJECT" --format json --result-only editor status
```

Parameters are kebab-case flags (`--async-tests`, `--filter-type`). The group is the first word
(`editor.playmode start`). With `--result-only --format json` a successful call prints the bare result: an
object, an array for commands that return records, or a JSON value (`script eval` of `return "pong";` prints
`"pong"`). A failure prints its reason on stderr, or an envelope with `errors[0].code` for a call the CLI itself
refuses. `--result-only` also replaces the payload of a failing call with that envelope, so `test status` is read without it: a red run exits 6 and its bare payload stays on stdout. Timeouts: `--timeout` is seconds, on the request and on `script eval`/`script run`.

## Exit codes

Branch on the exit code before the text.

| Code | Name | Meaning |
|---|---|---|
| 0 | `SUCCESS` | |
| 6 | `COMMAND_FAILED` | the command failed, including a failed `script eval` and failed tests |
| 11 / 13 | `NOT_FOUND` / `REMOTE_COMMAND_FAILED` | a missing project, or no such Editor command |
| 12 | `EDITOR_UNREACHABLE` | no Editor answered (none running, none matches, Safe Mode); also `unity recompile` |
| 14 | `TIMEOUT` | the request ran out of time |
| 17 | `UNAVAILABLE` | the Editor is loading, compiling, reloading or in Safe Mode: retry later |
| 18 | `AWAITING_INPUT` | a modal dialog blocks it: the owner answers it, then retry |

The helpers read 17 and 18 from the commands that return them instead of guessing from timeouts. `editor status` is exempt from both: it exits 0 and reports `compiling`, `reloading` or `blocked_by_dialog` in its `status` field, so `ready` and `recover` classify from that field. A timeout or a dialog is
reported, never worked around with a longer timeout or a retry loop. A client timeout does not prove a
mutation stopped: inspect the operation's own postconditions before resubmitting.

## Safety

A successful reply is not proof of the resulting Editor state: read it back. Save authorized owned scene
changes or refuse before tests, play or an Editor exit; never trigger a save dialog in batch mode. Cancellation is not
rollback. For long work, `--detach` returns a job id for `unity job status|wait|cancel`.
