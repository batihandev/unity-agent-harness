# Console capture

Read [foundation routing and safety](foundation.md) first. The Pipeline buffer and the Editor's live Console
have different owners; keep their counts separate.

```text
python3 unity-agent-harness/scripts/unity_workflow.py console --project <exact-project> \
  --output <new-persistent-json-path> [--level log|warn|error] [--tail 1..1000] \
  [--since <cursor> --since-session <source-session>]
```

This runs `console get` once. Without cursor arguments it is a snapshot. For a follow read pass both the prior
`cursor` and `sourceSession` from the result; keep that Pipeline source session apart from the Editor's OS
process identity. The call always captures up to the 2,000-entry buffer; `--tail` only limits what stdout
shows, while the artifact keeps the full sanitized reply. `dropped`/`bufferGap` and `reset` describe native
cursor history (a cursor from another session returns the tail with both true); `displayTruncated` describes
only stdout clipping.

The buffer is bounded and can miss entries across overflow or reload: never claim complete history. Buffer
`counts` and `groundTruth` totals have different owners; ground truth can be `unknown`, `stale` or `fresh` and
is diagnostic, not a zero count. The read does not clear or mutate either console; use `console clear` only on
an explicit request.

The report path must be new, persistent, and outside `Assets`, `Packages`, `Library` and `Temp`. The artifact
may hold full messages, stack traces and local paths. Never pick a newest report from a shared directory.

To log a message use `console write --message TEXT [--level info|warning|error]` and verify the unique
message through the retained session and cursor. To force a reload use `unity recompile` (see the
[compile gate](editor.md)).
