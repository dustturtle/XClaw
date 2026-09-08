# Chat submission recovery incident

## Observed on 2026-09-08

The user's last question began with "但是美股的势头不见得已经结束啊".
Chrome showed a locally appended question, a retained draft, "观察已暂停",
disabled send, and the previous task's completed state / 289-second duration.

At inspection, port 8080 had no listener and the XClaw launcher process was
absent. The PowerCore server and worker were still alive as orphan processes.
There was no row for this question in SQLite messages, no pending submission,
and no new PowerCore Run. The last persisted reply was the preceding question:
run_457165ee13d94d6eb00820edd0da43b8, completed at about 15:34:21 local time.

The stack log ends without shutdown or crash evidence. The exact signal or
external cause that terminated XClaw is not established. Do not attribute this
incident to the model or claim the last question was executing.

## Recovery and fix

- Stopped only the identified orphan PowerCore processes after confirming no
  active Runs; retained all databases, history and configuration.
- Restarted the stack with detached session and redirected standard streams.
  It is independent of the launching shell, not an installed auto-start service.
- Recorded POST transport failures separately from HTTP rejection.
- If successful progress reads repeatedly show only the previous stable state,
  stop indefinite waiting after four checks and expose an explicit unconfirmed
  submission state. Preserve the draft and images; never replay POST.
- A queued/reconnecting submission or a newly discovered Run remains observable.
  Failed progress queries reset the consecutive missing-submission counter.
- New submissions no longer display the previous Run's stage or elapsed time.
  Paused progress uses "状态待确认"; the disabled send label is "待确认".
- Renamed the user-facing control to "重新连接" and state to "进度更新已暂停".

An absent Run on a progress query is not a definitive rejection of a delayed
submission. The UI says "未确认", permits another status query, and never retries
the message automatically. Durable per-submission receipts/idempotency remain a
possible future enhancement, not something this patch implements.

## Verification

- Before the fix: regression tests reproduced both indefinite busy state and
  stale previous-task status (two failing tests).
- After the fix: 637 tests passed, one existing pandas-ta warning.
- Browser fixture: tests/fixtures/chat_disconnect_server.py on port 8082 drops
  the POST and four progress requests, then returns only the previous Run.
  Chrome showed pause, reconnect, explicit unconfirmed submission, retained
  draft and enabled send. Fixture readback: exactly one POST, zero Runs.
- Compileall and git diff --check passed.
- The user's original question was not resent and the conversation was not reset.
