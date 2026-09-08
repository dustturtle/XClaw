# Durable chat receipts and lifecycle diagnostics

## Deployment boundary

The local launcher now owns three children: PowerCore server, PowerCore worker,
and the XClaw runtime. The launcher remains outside the runtime and records its
exit code/signal, including SIGKILL. It checks readiness every 10 seconds and logs
health transitions. Shutdown cleans up its children.

No launchd job, automatic restart, failover or login-start service is installed.
If the launcher itself is killed with SIGKILL, or the machine loses power, it
cannot record that event in-process. These logs improve future diagnosis; they
do not retroactively establish why the earlier PID 15020 exited.

## Diagnostics

For the current local data directory:

- xclaw.data/powercore-glm53flash/logs/lifecycle.jsonl
- xclaw.data/powercore-glm53flash/logs/fatal-<runtime-boot-id>.log

The journal records UTC timestamps, boot IDs, PID/PPID, child PID, startup/stop,
signals, exit codes, health transitions, exception types and stack locations.
Exception messages, source-line text, local variables, chat text, image bodies
and credentials are not included. Request events use opaque server receipt IDs,
with a Run ID once known. Journal failures do not abort a request.

GET /health is liveness. GET /health/ready checks SQLite and PowerCore readiness
and returns 503 without private dependency errors if unavailable.

## Receipt API

POST /api/chat accepts optional request_id: 32 lowercase hexadecimal characters.
With that field, admission is persisted before returning HTTP 202:

    {"request_id": "...", "status": "accepted"}

This is acceptance, not task completion. Query:

    GET /api/chat/receipts/<request_id>?chat_id=<same-chat>

The response is scoped to the effective chat and bearer credentials. An unknown
or differently owned ID returns 404 / not_found. Reusing an ID with a different
payload returns 409. Without request_id the legacy synchronous API is retained;
the streaming endpoint rejects request IDs rather than silently ignoring them.

Receipt states:

- processing: admitted/preparing or the linked PowerCore submission/Run is active.
- completed: a durable reply is available (including a direct operation such as /reset).
- failed: a known rejection or failed/cancelled Run; preserve the user's draft.
- interrupted: execution cannot be established safely after interruption.

The SQLite uniqueness constraint prevents redispatch of a repeated submission,
including concurrent retries and retries after restart. Payload hashes include
text, mode and attachments; image bodies are not stored in the receipt table.
Existing PowerCore durable submissions retain their own payload and idempotency
key as before. Their creation links the receipt through a SQLite trigger.
Completed receipt results are immutable, not projections of whichever Run happens
to be latest in the chat.

This is at-most-once handler dispatch plus recovery of recorded PowerCore work,
not a claim of exactly-once arbitrary external side effects. If a process dies
before an effect can be correlated, the handler is not automatically replayed.
The current local runtime is single-worker; distributed worker ownership/liveness
coordination is not introduced by this change.

## Browser behavior

The browser saves the submission ID before POST, queries that exact receipt after
disconnect/reload and does not automatically POST again. Manual retry of unchanged
content reuses the same ID. An unresolved ID cannot silently become a new request
when its content changes. The settings panel has an explicit confirmation action
to end receipt reconciliation; this does not cancel previous effects.

Only ID/hash/mode are retained in localStorage for recovery across page sessions.
Text drafts remain in the existing session-scoped storage; images remain in memory
and must be reselected after a full reload when retrying an unreceived upload.

## Verification

- Concurrent identical submissions invoke the handler once; changed content conflicts.
- Credentials/chat isolation, absent receipts, direct operations and rejected work tested.
- Restart replays the recorded reply without rerunning the handler.
- The PowerCore adapter links the exact Run; previous waiting-input replies are excluded.
- Browser script tests cover 202-not-completion, lost POST, same-ID retry, refresh,
  a newer draft and changed-payload blocking.
- A real child killed by SIGKILL produces exit_code=-9 and signal=9 in the parent journal.
- Live SIGTERM shutdown recorded signal=15 and stopped all three children.
- Live GLM request 664c87eba8924ef4a81855b8de089ff8 completed as
  run_c4416c2edc8d47a7a9ec60b2993e8b73. After a full stack restart, reposting the same
  ID returned its receipt and the same reply. SQLite readback: one Run, one user message.
- Live refresh recovery: request 223c6f836f154eaab15194d5c5f539ee recovered
  run_56d6611facc84fbe98c816c366ad1680 and displayed its reply exactly once;
  draft cleared, send enabled, one user message in SQLite. Direct /reset also
  completed through a receipt without creating a model Run.
- Browser testing exposed and closed two regressions: the admission-to-dispatch
  timing gap was briefly reported as interruption; the restored-history flag
  was serialized as an ID instead of a Boolean. Both now have regression tests.
- Final full suite: 655 passed, one existing pandas-ta warning. Compileall and
  git diff --check passed. Logs: xclaw.data/logs/receipts-lifecycle-full.log.
