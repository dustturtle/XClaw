from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from xclaw.channels.web import create_web_app, _build_chat_page_html
from xclaw.config import Settings

def test_prepare_control_is_opt_in_and_has_explicit_budget():
    base=dict(auth_enabled=False,model_name='t',provider_name='t')
    assert '<option value="prepare">' not in _build_chat_page_html(**base)
    assert '预览操作，确认后执行 · 3 分钟' in _build_chat_page_html(**base,supports_prepare=True)

def test_unsupported_prepare_is_rejected_before_handler():
    for backend in ['native','powercore']:
        handler=AsyncMock(return_value='reply')
        app=create_web_app(handler,settings=Settings(agent_backend=backend,openui_inline_stream=False))
        with TestClient(app) as client:
            assert client.post('/api/chat',json={'message':'test','execution_mode':'prepare'}).status_code==400
        handler.assert_not_called()
