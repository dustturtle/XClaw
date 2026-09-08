# Chat UI reading layout

Verified locally on 2026-09-08.

## Scope

- Full-height conversation layout; no permanent explanatory sidebar.
- Session ID, token, execution mode and clear-display control live in a
  closed-by-default settings panel. Opening it does not resize the conversation.
- Compact composer retains image attachment, send, cancel and resume controls.
  Execution details are available on demand above the composer.
- Assistant replies and restored history use the same local Markdown renderer:
  headings, emphasis, lists, quotes, tables and fenced code.
- User messages over 1200 characters are collapsible without changing the
  submitted message or persisted history.
- New replies start at their beginning when following the conversation.
  Reading older content is not forcibly scrolled away.
- No changes to PowerCore, model selection, execution budgets or completion rules.

## Safety and packaging

The vendored markdown-it 15.0.1 bundle runs locally. Raw HTML is disabled;
unsafe link schemes are rejected by the parser. Markdown images are disabled
to avoid unsolicited remote image loads; uploaded image attachments still work.
Links open with noopener/noreferrer. No CDN or remote Markdown rendering service
is used. Asset URLs are allowlisted and included in the Python wheel.

## Verification

- Full regression: 634 passed, one existing pandas-ta deprecation warning.
  Command: .venv/bin/python -m pytest -q
- Compileall and git diff --check passed.
- Wheel build passed; CSS, renderer, parser bundle and license files are included.
- Actual Chrome viewport 1856 x 845: message area grew from 294px to 617px;
  composer shrank from 411.5px to 166px. Existing real replies rendered correctly.
- Responsive viewport 390 x 844: message area 626px; no page-level horizontal
  overflow. Wide tables scroll inside their own container. Settings remain
  within the viewport and do not shrink the reading area.
- Isolated browser chat chat-ui-20260908-1451 completed with GLM-5.3-Flash,
  rendering a heading, bold text, table and Python code block.
- Browser reload preserved an unsent multiline draft and rendered history.
  Sending /reset returned its direct success reply and cleared the submitted draft.
- Existing user chat was not reset or resubmitted. Its empty draft/image state
  was checked before refreshing the page.

This verifies the UI change, not a new acceptance of complex investment tasks.
