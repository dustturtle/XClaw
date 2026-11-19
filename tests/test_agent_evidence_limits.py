import json
from xclaw.agent_engine import _finalize_evidence_limits
from xclaw.llm_types import Message
from xclaw.responses import RejectedReply

def evidence(verified=False,error=False):
    return [{'name':'cn_fund_nav','content':json.dumps({'identity_verified':verified}), 'is_error':error}]

def test_unverified_final_and_session_same():
    messages=[Message(role='user',content='test'),Message(role='assistant',content='净值1.234')]
    result=_finalize_evidence_limits('净值1.234',messages,evidence())
    assert result.startswith('净值1.234\n\n数据限制：')
    assert '身份未独立核验' in result
    assert messages[-1].content==result

def test_rejected_unchanged():
    r=RejectedReply('失败');m=[]
    assert _finalize_evidence_limits(r,m,evidence()) is r
    assert m==[]

def test_only_current_success_exact_false():
    for data in [[],evidence(True),evidence(False,True),[{'name':'other','content':'{"identity_verified":false}'}], [{'name':'cn_fund_nav','content':'bad'}]]:
        m=[Message(role='assistant',content='answer')]
        assert _finalize_evidence_limits('answer',m,data)=='answer'
        assert m[-1].content=='answer'

def test_existing_model_caveat_does_not_suppress_standard_notice():
    m=[Message(role='assistant',content='身份可能未核验')]
    assert '数据限制：' in _finalize_evidence_limits('身份可能未核验',m,evidence())
