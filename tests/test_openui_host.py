"""Integration boundaries for the actual shipped chat host and package assets."""
import subprocess
import shutil
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from xclaw.channels.web import _build_chat_page_html, create_web_app
from xclaw.config import Settings

STATIC = Path(__file__).resolve().parents[1] / 'xclaw/channels/static'

@pytest.mark.parametrize('enabled', [False, True])
def test_generated_inline_javascript_is_valid_and_opt_in(enabled):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for shipped JavaScript syntax verification')
    doc = BeautifulSoup(_build_chat_page_html(auth_enabled=True, model_name='test',
        provider_name='test', supports_openui=enabled, supports_progress=True,
        supports_receipts=True), 'html.parser')
    code = '\n'.join(s.text for s in doc.select('script:not([src])'))
    result = subprocess.run([node, '--check'], input=code, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert f'const SUPPORTS_OPENUI = {str(enabled).lower()};' in code
    assert bool(doc.select('script[src="/chat-assets/openui.js"]')) is enabled
    assert 'item.status === "failed"' in code
    assert 'appendMessage(item.role, item.content, [], true, false)' in code
    assert 'else appendMessage(item.role, item.content, [], true, true, item)' in code
    assert 'XClawOpenUIHost?.clear()' in code
    assert Settings(_env_file=None).openui_enabled is False


def test_actual_host_is_valid_and_assets_are_public_not_prompt():
    node = shutil.which('node')
    if node:
        result = subprocess.run([node, '--check', str(STATIC/'openui-host.js')], capture_output=True)
        assert result.returncode == 0
    client = TestClient(create_web_app(AsyncMock(return_value='hello')))
    for name in ('openui.js','openui.css','openui-host.js'):
        response = client.get('/chat-assets/'+name)
        assert response.status_code == 200
        assert response.content == (STATIC/name).read_bytes()
        assert response.headers['x-content-type-options'] == 'nosniff'
    assert client.get('/chat-assets/openui-prompt.txt').status_code == 404
    assert client.get('/api/openui/capabilities').status_code == 404


def test_agent_ui_hint_is_scoped_to_enabled_web_only():
    from types import SimpleNamespace
    from xclaw.agent_engine import _build_system_prompt
    def context(channel, enabled):
        return SimpleNamespace(channel=channel, settings=SimpleNamespace(openui_enabled=enabled))
    assert "OpenUI 展示阶段" in _build_system_prompt(context("web", True))
    assert "OpenUI 展示阶段" not in _build_system_prompt(context("web", False))
    assert "OpenUI 展示阶段" not in _build_system_prompt(context("wechat", True))


def test_empty_outcomes_restore_and_organizing_only_after_admission():
    host=(STATIC/'openui-host.js').read_text()
    assert "panel.className = 'openui-presentation'; panel.hidden = true" in host
    assert "if (event === 'status') { panel.hidden = false; stop.hidden = false; status.hidden = false" in host
    assert 'showEmpty(cached)' in host
    assert "showEmpty(saved.source || 'root = Col([])')" in host
    assert "p.version === 'xclaw-empty-1'" in host
