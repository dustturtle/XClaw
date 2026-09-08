# Long-task optimization acceptance

Date: 2026-09-08. Local functional acceptance completed; performance and provider
reliability limits below remain explicit. This is not an all-load success guarantee.

Post-acceptance correction: the actual Chrome conversation later failed in
`run_b9d61e48f61c480a9fa63355c9c30a68` without concurrent test Runs. Its 600s
budget was active, both history queries succeeded, but finalization produced no
visible text despite 8192 output tokens and failed completion requirements after
593.5s. The user's retained-history/retry path is NOT covered by a successful
fresh-chat replay; stable long-answer delivery remains unresolved. The subsequent
`/reset` false UI failure is independently fixed and verified in
[the reset delivery report](reset-direct-reply-fix.md). The historical results
below must not be read as proof that this real-user path was resolved.

## Delivered and running

- XClaw runs at http://127.0.0.1:8080, PowerCore at 18080, PostgreSQL at 55439.
  Server and Worker use the same final 12:03 build, deployed at 12:06. Their hashes
  and changed PowerCore source files were checked against the frozen build.
- Actual/default model remains `glm-5-3-flash-260826`, thinking enabled,
  temperature 0.1, max_tokens 8192, existing Ark endpoint and private key unchanged.
- Standard/analysis run budgets are 180/600 seconds. The dedicated PowerCore model
  request cap is 600 seconds; finalization reserves 120 seconds WITHIN the run
  budget. Native/tool-internal `timeout=240` is unchanged.
- No commit, push, proxy change, arbitrary shell/Python permission, or user-state
  reset was performed. Test chats are separate from the original conversations.
- Isolation/test services on 8081/18081 are stopped. Test PostgreSQL containers on
  55440/55441 are stopped with all data retained. Only the 55439 application
  database remains running.

Implementation follows [the local plan](long-task-optimization-plan.md):

1. PowerCore admits and persists authoritative budgets, preserves active time
   across recovery, reserves finalization, and reports actual terminal failures.
   Precision, receipt replay, legacy migration and deadline-classification bugs
   found by real database tests were corrected without relaxing authority.
2. Empty or known truncated final candidates share at most one tool-disabled
   replacement attempt. Custom completion policies cannot bypass validity;
   CompletionGate and turn/token/cost/deadline limits remain authoritative.
3. Private Session history retains bounded complete tool exchanges separately
   from the public transcript. Prior evidence never becomes current-Run receipts.
   Fork ownership/cutoffs and recovery snapshots remain enforced. Tool evidence
   and image/dialogue budgets are separate; omissions are explicit.
4. XClaw persists submission/input/cancellation intent, reconciles exact
   idempotency keys, reconnects observers without resubmission, and deduplicates
   final replies. The page shows real progress, cancellation and recoverable history.
5. Reviewed independent reads are eligible for parallel execution. History
   queries use bounded coalescing/cache/failover and real source-worker limits.
   Search returns bounded webpage results and distinguishes empty/error outcomes.
6. Shutdown closes observers, scheduler and SQLite even on cancellation.
   Legacy terminal durations display as unrecorded rather than time since creation.

## Final verification

| Requirement | Authoritative evidence |
|---|---|
| Full XClaw suite | **611 passed**, one pre-existing pandas-ta warning, 20.71s; `xclaw.data/logs/long-task-final-tests.log` |
| Rust full workspace | **366 passed, 13 ignored** in workspace invocation; `../PowerCore/.runtime/session-history-workspace.log` |
| All real PostgreSQL tests | **13 passed separately**, each in a fresh 55441 database; `session-history-pg-final-*.log` in PowerCore/.runtime |
| Rust total | **379 distinct passing tests**, not double-counting focused or ignored tests |
| Remaining PowerCore gates | fmt, clippy with -D warnings, Python unittest 4 passed, fixture doctor, Server/Worker build, diff check all exit 0 |
| Budget admission/recovery/receipts | Unit and real-PG tests cover clamp, idempotency, fractional precision, lease/fencing, queue/pause migration, deadline persistence and no duplicate effects |
| Truncated/empty completion | 13 truncation regressions cover shared repair allowance, custom/contract policies, completed writes, checkpoint restore and hard budgets |
| Cross-turn evidence | 6 new PG tests plus 2 context tests cover exact numbers, multi-turn/fork, privacy, no inherited receipt credit, recovery, damage/bounds and large-image compatibility |
| XClaw recovery and UI contract | Full suite covers durable intents, accepted-input crash windows, bounded background recovery, cancellation scope, sanitation, reply cursor/dedup, page polling and chat switching |
| Data/search/tool bounds | Full suite covers cross-loop sharing, cancellation, TTL/LRU, source admission/cooldown, actual stuck-thread slots/teardown, search deadlines/parsing/error sanitation and independence declarations |
| Static checks | compileall and both repositories' git diff --check pass |
| Final build identity | `shasum -a 256 -c .runtime/session-history-freeze-sha256.log` passes for Server, Worker, service test binary and final changed source/contract files |

Detailed Rust commands, test-first failures, database names and preservation checks:
[budget/precision report](../../PowerCore/.runtime/long-task-budget-p1-fix-report.md),
[truncation report](../../PowerCore/.runtime/long-task-truncation-report.md),
[private history report](../../PowerCore/.runtime/session-history-report.md).

### Real final-build acceptance

All successful long cases below use the original 7,857-character message 35,
not a shortened replacement. The tools case appends explicit read-only queries.

| Case | Run | Result |
|---|---|---|
| Original, independent replay | `run_adef168a8b7a467dbcdcba6da98710f5` | **completed / stop, 252.24s, 2879 characters** |
| Original plus real quote/history/correlation | `run_13ea15c2601b46e78667f2b4cf8743b9` | **completed / stop, 592.12s, 4291 characters** |
| Cancel | `run_7e3feabf90054291bebfccf261b5fcec` | **cancelled, 2.03s** |
| Browser standard-mode submission | `run_ed8dd1991de44ea0b3b31ad670e978ca` | **completed / stop**, admitted 180000ms, about 5s, no tools |
| Complete business/image regression | 14 distinct Runs | **passed**, including exact audited continuation numbers and post-reset persisted state |

Long scripts exited 0. Final text was manually inspected for a complete conclusion;
terminal response equals the delivered reply and restored history has one matching
assistant reply. This verifies delivery, not financial correctness.

Final artifacts, relative to XClaw root:

- `xclaw.data/powercore-glm53flash/powercore/long-task-acceptance-57a364e149.json`
- `xclaw.data/powercore-glm53flash/powercore/long-task-acceptance-d490bd2bea.json`
- `xclaw.data/powercore-glm53flash/powercore/acceptance-250b9c6265.json`

The business artifact covers 13 foreground cases, one actually fired scheduled Run
and one child Run: 14 distinct Runs because the question and answer share one Run.
It verifies random image content and image continuation, quotes and prior-tool
numeric recall without new tools, memory/watchlist/paper holdings, isolation,
foreground question preservation during scheduled work, same-Run answer,
sub-Agent execution, exact 20-row/MA5/MA20 transcription, persisted report, and
reset preserving business state. No failed terminal event occurred in that suite.

Browser checks used the actual page: real cancellation followed by confirmed
terminal state, refresh observing the same Run, unique reply restoration, standard
budget selection and Send recovery. Long conversation scrolling keeps controls
visible. Desktop and 390x844 mobile checks passed with document width 390 and
cancel control inside the viewport. Final page shows the completed independent
original case; analysis mode was restored after the standard-mode test.

## Data preservation and cleanup

Before schema 1 -> 2 migration, no active user Run existed. Both old components
were stopped; old idle PIDs that failed graceful teardown were individually
terminated. Updated launchers subsequently exited 0 on SIGINT, including scheduler,
SQLite and child-process cleanup. Later history/truncation changes added no schema.

Backups: `xclaw.data/backups/long-task-20260908/` (directory 0700; database dumps
0600), including before-migration, before-truncation and before-history snapshots.
SQLite integrity is ok; all original 36 messages match by id/chat/role/content.
Original watchlist, holdings, memory, schedules and reports remain present.
All 49 original PostgreSQL Run ids/statuses matched after migration. An actual
pg_restore into disposable `pc_xclaw_backup_verify_20260908` succeeded. Ordered
full-row digests match all 6643 original events and 80 tool receipts.

The original config is untracked and ignored, as are runtime data/backups.
The configured model key was not found in nonignored source. No publication
was performed; this does not claim an audit of every historical Git commit.

## Failure ledger and limits

Failures are retained, not relabeled or hidden behind later successes.

| Evidence | Finding and disposition |
|---|---|
| Original pre-optimization case | Third model call hit the 180-second total deadline despite 240-second model timeout; earlier calls/tools had succeeded |
| `long-task-acceptance-26dda94e16.json` (isolated) | Internal checkpoint failure at ~555.5s with 600s admitted; real PG tests identified fractional deadline serialization mismatch, subsequently fixed |
| `long-task-acceptance-1ce010c8b4.json` (isolated) | Correct deadline_exceeded at client 603.2s; 240s model retries and 45s reserve informed 600s cap/120s reserve calibration |
| `long-task-acceptance-2f45d89485.json` (isolated) | Original completed in 366.53s; tools case initially labeled PASS but ended with length and cut-off text. That complete-answer PASS was withdrawn; verifier and Harness were strengthened |
| `acceptance-c0bc734365.json` | Raw quote had 1316.01/-0.30%, successor had only public summary and asked to requery. Private history boundary fixed and audited continuation subsequently passed |
| `acceptance-4c340822bc.json` | Final reset assertion rejected a hyphen reformatted as '(ID: ...)'. DB and audited tool evidence were intact. Verifier now checks actual persisted state, source content and displayed identity; failed artifact stays failed |
| `long-task-acceptance-63d5cb29b5.json` | Final-build original replay failed during concurrent long/business runs: exploration 479958ms + finalization 119966ms, worker 599994ms, client 601.1s, deadline_exceeded. No model turn/usage receipt completed. Concurrency is context, NOT a proven root cause |
| `long-task-acceptance-57a364e149.json` | Same frozen code, model, settings and unchanged original input passed independent replay in 252.24s after all other model runs ended |

Unprefixed artifact names in the table are under the corresponding ignored data
directory's `powercore/`; isolated means `powercore-long-tasks`, otherwise
`powercore-glm53flash`. Earlier successful finalization evidence is retained in
`long-task-acceptance-4893269c3a.json`: 569.19s, complete stop, one real
run.finalizing event observed in the browser.

- These changes fix execution integrity and the reproduced integration gaps;
  they do not guarantee fast replies or successful completion at every concurrency
  level. Long results still take minutes and can exhaust their finite budget.
  No usage receipt does not mean no provider-side cost.
- Financial conclusions, external search snippets, source timestamps, missing
  dates and cross-source inconsistencies are not independently certified here.
  Analysis may identify missing futures/ETF inputs rather than complete a backtest.
- History is bounded and can omit older/oversized evidence with explicit markers.
  Existing checkpointed Runs keep their original snapshot, not retroactive backfill.
- Known finish-reason markers are checked; this is not a semantic answer-quality
  guarantee. Independent ResultComposer/native tool-internal model behavior is
  not rewritten by the AgentLoop repair.
- This is single-process local Web acceptance, not production authentication,
  multi-replica coordination, external IM, released-image or arbitrary-code
  sandbox acceptance. No new general code-execution permission was enabled.

## Reproduction commands

These commands use new acceptance chats, consume real model/data calls, and retain
evidence under the ignored data directory. Run separately for independent latency
checks; do not interpret a concurrent run as an isolated benchmark.

```sh
.venv/bin/python -m pytest -q

.venv/bin/python scripts/verify_long_tasks.py \
  --source-db xclaw.data/powercore-glm53flash/xclaw.db --message-id 35 \
  --data-dir xclaw.data/powercore-glm53flash --case original

.venv/bin/python scripts/verify_long_tasks.py \
  --source-db xclaw.data/powercore-glm53flash/xclaw.db --message-id 35 \
  --data-dir xclaw.data/powercore-glm53flash --case tools --cancel

.venv/bin/python scripts/verify_powercore.py \
  --data-dir xclaw.data/powercore-glm53flash \
  --expected-model glm-5-3-flash-260826 --multimodal --numeric-transcription
```

The business verifier requires protected audit access. Exact final live logs:
`long-task-final-original-independent.log`, `long-task-final-tools.log`,
`long-task-final-business-audited.log` under `xclaw.data/logs/`.
Service log: `long-task-8080-stack.log`.
