# Domestic Commodity Futures Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal, read-only domestic commodity futures slice to XClaw so quote, history, indicators, and gap analysis tools can answer `asset_type=future` queries with public data sources.

**Architecture:** Keep the current investment skill and tool names stable, and extend the existing stock-oriented tools with a new `asset_type=future` branch. Implement futures-specific normalization and provider failover in a dedicated datasource module so the tools only consume normalized frames and quote payloads.

**Tech Stack:** Python 3.11, pandas, akshare, pytest, pytest-asyncio

---

### Task 1: Add Futures Regression Tests First

**Files:**
- Modify: `tests/test_stock_tools.py`

- [ ] Add a failing test for `stock_quote` with `asset_type=future` that patches a new datasource function and asserts the output includes contract name, symbol, latest price, and source.
- [ ] Run: `pytest tests/test_stock_tools.py -k "future and quote" -v`
- [ ] Confirm the test fails because futures support is not implemented yet.

### Task 2: Add Futures Datasource Module

**Files:**
- Create: `xclaw/datasources/futures_cn.py`

- [ ] Implement symbol normalization for domestic commodity futures input such as `rb2410`, `RB2410`, and `rb0`.
- [ ] Implement a normalized realtime quote fetcher with provider fallback built on `akshare` futures interfaces.
- [ ] Implement a normalized history fetcher with provider fallback that returns the same core columns the tools already expect: `日期`, `开盘`, `收盘`, `最高`, `最低`, `成交量`.
- [ ] Keep provider-specific parsing inside the datasource module and expose only normalized return values.

### Task 3: Extend Tools To Read Futures Data

**Files:**
- Modify: `xclaw/tools/stock_quote.py`
- Modify: `xclaw/tools/stock_history.py`
- Modify: `xclaw/tools/stock_indicators.py`
- Modify: `xclaw/tools/stock_gap_analysis.py`

- [ ] Add `asset_type=future` to tool schemas where needed.
- [ ] Route futures requests to the new datasource module.
- [ ] Update human-readable descriptions and validation messages from stock-only wording to generic instrument/contract wording where those tools now support futures.
- [ ] Leave portfolio, watchlist, and backtest behavior unchanged in this iteration.

### Task 4: Teach The Agent About Futures Queries

**Files:**
- Modify: `xclaw/agent_engine.py`

- [ ] Update the system prompt so the model knows the same tools can be used for domestic commodity futures queries.
- [ ] Keep the prompt concise and aligned with the tool surface; do not promise unsupported trading or backtesting behavior for futures.

### Task 5: Fill Out Tool Regression Coverage

**Files:**
- Modify: `tests/test_stock_tools.py`

- [ ] Add failing tests for futures history, indicators, and gap analysis using mocked datasource responses.
- [ ] Add at least one normalization-focused test that proves lowercase futures symbols normalize correctly before fetch.
- [ ] Run targeted tool tests until the new futures cases pass.

### Task 6: Verify End-To-End Slice

**Files:**
- No code changes required unless verification reveals a defect.

- [ ] Run: `pytest tests/test_stock_tools.py -v`
- [ ] Run: `pytest tests/test_phase5.py -k "stock_backtest or tool_names" -v`
- [ ] Run: `pytest tests/test_llm_types.py tests/test_config.py -v`
- [ ] If any failure is caused by the futures changes, fix it with another red-green cycle before claiming completion.

### Task 7: Document Final Scope

**Files:**
- Modify: `README.md`

- [ ] Add one short note that the read-only investment tools now cover domestic commodity futures queries.
- [ ] Keep the scope explicit: quote, history, indicators, and gap analysis only.
