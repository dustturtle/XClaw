"""The live verifier must not turn nonempty truncated output into acceptance."""

import pytest

from scripts.verify_long_tasks import verify_complete_response
from scripts.verify_powercore import verify_memory_reference, verify_quote_continuation


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens"])
def test_live_verifier_rejects_known_truncated_completion(reason):
    reply = "An otherwise substantive answer. " * 20
    with pytest.raises(AssertionError, match="truncated"):
        verify_complete_response({"type": "run.completed", "response": reply, "finish_reason": reason}, reply)


def test_live_verifier_accepts_complete_matching_response():
    reply = "A complete analytical answer. " * 20
    verify_complete_response({"type": "run.completed", "response": reply, "finish_reason": "stop"}, reply)


@pytest.mark.parametrize("terminal,reply", [
    ({"type": "run.failed"}, "x" * 200),
    ({"type": "run.completed", "response": "x" * 200}, "y" * 200),
    ({"type": "run.completed", "response": "ok"}, "ok"),
])
def test_live_verifier_rejects_incomplete_or_mismatched_evidence(terminal, reply):
    with pytest.raises(AssertionError):
        verify_complete_response(terminal, reply)


def test_continuation_requires_actual_prior_tool_numbers_without_new_tools():
    source = "昨收: 1316.01\n涨跌幅: -0.30%"
    reply = "贵州茅台600519，昨收1316.01，涨跌幅-0.30%。"
    assert verify_quote_continuation(source, reply, []) == {"昨收": "1316.01", "涨跌幅": "-0.30%"}
    with pytest.raises(AssertionError):
        verify_quote_continuation(source, reply.replace("1316.01", "1300.00"), [])
    with pytest.raises(AssertionError):
        verify_quote_continuation(source, reply, [{"type": "tool.started"}])
    with pytest.raises(AssertionError):
        verify_quote_continuation("昨收: N/A\n涨跌幅: N/A", reply, [])


def test_memory_acceptance_checks_evidence_and_identity_not_punctuation():
    marker = "preference-4c340822bc"
    verify_memory_reference(marker, marker, "preference (ID: 4c340822bc)")
    verify_memory_reference(marker, marker, marker)
    with pytest.raises(AssertionError):
        verify_memory_reference(marker, "no actual memory", marker)
    with pytest.raises(AssertionError):
        verify_memory_reference(marker, marker, "preference (ID: unrelated)")
    with pytest.raises(AssertionError):
        verify_memory_reference(marker, marker, "unrelated (ID: 4c340822bc)")
