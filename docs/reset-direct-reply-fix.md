# Reset response delivery fix

Date: 2026-09-08. Scope: XClaw only; no PowerCore source/model/budget change.

## Root cause

The user's Chrome chat `web-70dc2grt` successfully ran `/reset`. SQLite contained
the confirmation (messages 128/129) and no PowerCore session binding remained.
The page nevertheless required a changed Run id for every successful HTTP reply,
so idle progress after reset became a false submission failure.

The backend now returns a trusted, str-compatible `DirectReply` for this local
operation. `/api/chat` serializes it with `delivery: direct`; ordinary string
replies retain the original response shape. The page settles the direct reply,
invalidates old in-flight polls, preserves any newly edited draft and refreshes
the authoritative baseline before enabling the next request. It does not infer
success from `/reset` text or response wording. Reset failures are not marked direct.

## Verification

- Before the fix: REST regression and two browser-script regressions failed;
  the negative test for plain reset-like response text already passed.
- Full suite: **616 passed**, one existing pandas-ta warning, 20.59s.
  Command: `.venv/bin/python -m pytest -q`.
  Log: `xclaw.data/logs/reset-direct-full.log`.
- Browser-script tests cover idle-before-response, stale pre-reset polls,
  subsequent Run admission, new-draft preservation and failed reset handling.
- Real Chrome flow used the same 8080 service through `localhost` to avoid
  changing the user's `127.0.0.1` browser storage. Test chat:
  `reset-browser-20260908-1402-a`.
- Before reset: `run_1c47eb0c9ea64fb19d48285e7af7f2dd`, completed,
  `session_7f26054861f442e6a3995d8d07834b70`.
- After clicking Send with `/reset`: confirmation visible, composer cleared,
  Send restored, zero session bindings and still exactly one Run.
- Next message: `run_d5980a4f048b4bdfa90eb3c8c147bc20`, completed,
  `session_766ea61f9f724aa0a82bca2752dfe9de`. Thus a new Session was actually used.
- The user's original Chrome tab was refreshed after checking no files were
  selected. Its `/reset` draft was verified unchanged, the existing successful
  reset confirmation is now visible, and the false UI failure is gone. No reset
  or model request was resubmitted for the user's original chat.
- Compileall and git diff --check pass. Service was restarted only after checking
  there were no active Runs. No commit or push was performed.

This fixes reset delivery/UI classification only. It does not resolve the separate
long-input finalization failure in `run_b9d61e48f61c480a9fa63355c9c30a68`.
