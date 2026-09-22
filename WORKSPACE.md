# Operator 1.2 workspace

## User path

Open the existing status card for the current goal, checkpoints, constraints and
decisions. New job starts a distinct objective in the same chat after the current
run ends. Previous jobs/results stay available. The job store supplements the
existing transcript/native-thread resume; it does not replace either.

Result cards distinguish **found**, **prepared**, and **confirmed**. The runner
records completed tool output as bounded evidence. The model must reference
server-issued evidence/file IDs; confirmed cards also need confirmation details.
This validates provenance, not the truth of every model interpretation. There is
no second model grader or mandatory extra verification turn.

Send a correction normally while Operator works. Codex uses `turn/steer` on the
current app-server turn. Claude's tool hook can deliver immediately after a tool;
other runtimes use the shared boundary queue. After three seconds without
delivery, fallback interrupts the owned process, waits for its exit, then resumes
with the correction and durable job context. Unknown native acknowledgements are
not replayed. An interrupted purchase/booking/message must be inspected before
retrying. Stop invalidates outstanding delivery and never starts a replacement.

Use the paperclip to add/download/remove chat files; desktop drops onto the
composer and mobile file pickers use the same endpoint. Completed Chrome
downloads are attributed by initiating frame and owned target, never foreground
tab. Uploads are staged to Windows-native paths before attaching to a file input.
Unknown downloads are canceled instead of being assigned to an arbitrary chat.
The metadata-only download connection outlives the viewer while runs or transfers
are active; background jobs do not require frame capture. Viewer reconnections
notify that owner to restore the managed download policy.
Failed/canceled transfers are visible in Files. Files persist until explicit file
or chat deletion; there is no silent age-based eviction.

Saved recipes retain variables and schedules and gain success criteria plus a
default **prepare, then confirm** policy. Bounded permissions require explicit
actions, exact sites/recipients, and (for purchases) a total amount/currency.
The allowance applies across follow-ups in the same recipe job. Changed proposals
invalidate pending/unclaimed approvals. Approval covers one exact proposal, not
whatever the model does next. Scheduled runs pause for a controller to approve.
This is an agent policy and audited approval protocol, **not** a hard sandbox for
arbitrary browser/command actions. Existing host permissions remain unchanged.

## Storage and configuration

The additive SQLite store defaults to
`~/.local/share/operator/workspace/workspace.sqlite3`, WAL mode, schema version 1.
Managed file blobs live in `files/`; generated working files in `artifacts/<run>`.
Credentials fence model updates to the active run; only their hash is stored.
Existing session/history/native stores are not migrated or reset. Service startup
marks interrupted workspace runs stale without deleting jobs or results.

Set `OPERATOR_WORKSPACE_DIR` for a separate private service instance. Never run
two independent service owners against the same workspace; they would share
startup recovery and retention. The public demo gets no workspace tools/routes.
Browser staging defaults to Windows `%TEMP%\Operator\files-v1`; use
`OPERATOR_BROWSER_STAGING_DIR` (WSL path) and `OPERATOR_BROWSER_STAGING_NATIVE`
(Windows path) together for an isolated instance or integration fixture.

| Setting | Default | Meaning |
| --- | --- | --- |
| `OPERATOR_FILE_LIMIT_MIB` | 100 | Each managed file |
| `OPERATOR_CHAT_LIMIT_MIB` | 1024 | All managed files in a chat |
| `OPERATOR_TOTAL_LIMIT_MIB` | 10240 | All managed files in this instance |
| `OPERATOR_CODEX_APP_SERVER` | 1 | Set 0 to restore the previous Codex transport |
| `OPERATOR_FILE_BRIDGE` | 1 | Set 0 to disable Chrome download collection |

Quota failures leave existing files intact. These limits govern managed uploads
and publication, not arbitrary shell writes outside the managed store. Delete a
chat only after its run stops: it removes its managed/staged files and generated
working directory, and releases its browser tabs using the existing owner map.

## Diagnostics

Settings → Diagnostics reveals one Health entry; off by default in each client.
Aggregate metadata collects without adding UI when hidden. Measurements cover
capture (including Chrome's combined capture/encode), sent frames/bytes,
unchanged frames, reconnects, applied viewport corrections, input and tool timing,
first action, run duration, steering, failures and reported native Codex tokens.
Provider-unreported token metrics are absent, not zero. Capture/feed measurements
are instance-wide; run timing/tool/native-token measurements carry a run ID.
Health supports last 24 hours and current-run detail.

Frame-path recording is memory-only with a bounded bucket map. A worker flushes
every ten seconds or at terminal transitions. Metrics retain 30 days. Optional
debug records auto-stop after 15 minutes and expire after 24 hours (also capped at
10,000 rows). The allowlist accepts numeric measurements and sanitized IDs/model
slugs, never prompts, page text, screenshots, credentials or full URLs. Busy/full
storage drops diagnostics rather than blocking a run. Timing is instrumentation,
not a new polling/viewport control loop.

## Release and recovery

Workspace-aware CDP helpers require Playwright 1.61 or newer (`no_defaults` /
`noDefaults` avoids replacing the persistent download owner's settings). The
control requirements pin 1.61. The managed job store uses only Python's standard
library; adding job/file tools does not require Flask in the MCP interpreter.

Deploy through host-app's immutable-release/drain guard. Before rollout, make
a consistent SQLite backup of existing session/history stores and retain the
previous release. The new workspace DB is additive; roll back the service release
without deleting it. Do not downgrade through a newer workspace schema: unknown
schema versions are refused. For subsequent upgrades, use SQLite's backup API
(not a copy of a live WAL database alone) and retain managed file blobs with it.
No npm package, public-demo publication, or automatic model-based acceptance run
is part of this release.

Verification is quota-free: workspace/route ownership fixtures, native wire
fixtures checked against the installed Codex CLI schema, correction race tests,
rendered Chromium cockpit tests, and an opt-in Windows Chrome upload/download
round trip using a new headless profile and random port:

```sh
PYTHONPATH=. pytest tests -q
OPERATOR_TEST_WINDOWS_FILES=1 PYTHONPATH=. pytest tests/test_windows_file_roundtrip.py -q
```

The Windows fixture never attaches to :9222, :9224 or a signed-in profile. Native
model behavior still needs observation during normal use; these tests deliberately
do not consume a real model turn. Existing capture/viewport logic is not retuned.
