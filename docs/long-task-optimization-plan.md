# Long-task execution optimization

Date: 2026-09-08

## Goal and observed failure

The 7,857-character strategy review failed after 180 seconds, during its third
model call. Its first two model calls and 13 tools completed. A model request
timeout of 240 seconds did not override the worker's 180-second run deadline.
This work must fix the execution boundary, not truncate the user's document or
substitute a shorter acceptance prompt.

## Chosen architecture

Keep the existing conversational `/runs` and session continuation API. Reuse
PowerCore's budget/checkpoint/completion mechanisms rather than moving chat into
the separate `/tasks` business-contract workflow or adding a second agent loop.

1. PowerCore owns an admitted, persisted run wall-time budget and finalization
   reserve. Callers may narrow server policy, never expand authority. Effective
   budgets are observable. Idempotency, continuation and recovery preserve the
   appropriate budget semantics. Hard deadline and completion gates remain.
2. XClaw exposes explicit standard/analysis execution modes, initially 180/600
   seconds. Single model timeout remains independent. Waiting uses the admitted
   budget, not the previous fixed 270-second outer limit. Old servers without
   the budget contract fail explicitly rather than silently accepting a request.
3. XClaw projects actual execution state into the page, supports cancellation of
   a specifically bound Run, and can observe an existing Run after a browser
   refresh/disconnect. Observation is not a new execution. Raw reasoning, tool
   arguments, secrets and provider diagnostics are not public progress.
4. Application instructions separate analysis of supplied material from external
   factual verification. Tools expose explicit independence metadata. Audited
   independent reads use PowerCore's existing parallel executor; stateful tools
   remain serial. Data providers get bounded coalescing/cache/failover behavior.
   Search must distinguish useful web results, empty results and transport errors.

No key changes, model switch, blanket thinking disable, proxy changes, commits or
remote publication are part of this work. Existing dirty PowerCore changes are
preserved. Tests may use separate chats/data/ports; user state is not reset.

## Acceptance gates

- Deterministic budget admission, idempotency, clamp/rejection, finalization and
  hard-deadline tests, including checkpoint/recovery and no duplicate effects.
- XClaw budget payload/effective-budget handling, timeout classification,
  progress sanitation, cancellation scope, observation reconnection and message
  deduplication tests. REST/image/native compatibility remains covered.
- Tool coalescing/cache isolation, bounded fallback, independence metadata and
  search parser/error tests. No unbounded background SDK work is introduced.
- Full XClaw pytest and PowerCore AGENTS.md gates; appropriate PostgreSQL smoke.
- Real GLM-5.3-Flash execution of the original long input, plus long multi-tool
  execution and browser progress/cancel/reconnect checks. Verify actual terminal
  events, useful final content and state, not just HTTP 200 or a nonempty error.
- Known provider output truncation (`finish_reason=length/max_tokens`) is not a
  complete answer. Reuse bounded final-answer-only repair, never repeat tools or
  extend the run deadline to hide it. Repeated truncation must fail explicitly.
- Session continuation must retain bounded private tool evidence, not only the
  public final-answer summary. Verify a follow-up can recover prior quote fields
  omitted from the first answer without new tool calls or fabricated numbers;
  public transcripts and current-Run completion receipts remain isolated.
- Record exact commands, outcomes and limitations in a local acceptance report.

## Implementation ownership

- Main agent: XClaw client, runtime, persistence, instructions, launcher, plan and
  integration/acceptance.
- PowerCore agent: Rust/API/config/contract/schema changes and Rust gates.
- Datasource agent: data/tool implementation and focused tests.
- Web agent: page/endpoints and focused web tests.

Local functional acceptance is complete. The [acceptance report](long-task-optimization-report.md)
maps these gates to tests, real Runs, browser checks and preserved data. It retains
the failed concurrent original-input replay and explicitly does not promise
successful completion at every load level or certify financial conclusions.
