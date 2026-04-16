"""Tests for OpenAI Codex OAuth flow management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xclaw.oauth.openai_codex import OpenAICodexOAuthManager
from xclaw.oauth.store import AuthProfileStore


@pytest.mark.asyncio
async def test_start_login_contains_expected_authorize_params(tmp_path: Path):
    store = AuthProfileStore(tmp_path / "auth_profiles.json")
    manager = OpenAICodexOAuthManager(store=store)
    manager._ensure_callback_listener = lambda: False  # type: ignore[method-assign]
    manager.run_tls_preflight = _noop  # type: ignore[method-assign]

    payload = await manager.start_login()

    assert payload["mode"] == "manual"
    url = payload["authorize_url"]
    assert "https://auth.openai.com/oauth/authorize" in url
    assert "response_type=code" in url
    assert "scope=openid+profile+email+offline_access" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback" in url
    assert "code_challenge_method=S256" in url
    assert "codex_cli_simplified_flow=true" in url


@pytest.mark.asyncio
async def test_complete_login_persists_profile_from_redirect_url(tmp_path: Path):
    store = AuthProfileStore(tmp_path / "auth_profiles.json")
    manager = OpenAICodexOAuthManager(store=store)
    manager._ensure_callback_listener = lambda: False  # type: ignore[method-assign]
    manager.run_tls_preflight = _noop  # type: ignore[method-assign]

    payload = await manager.start_login()
    login_id = payload["login_id"]
    session = manager._sessions[login_id]
    manager._exchange_code_for_tokens = _fake_exchange  # type: ignore[method-assign]

    result = await manager.complete_login(
        login_id,
        f"http://localhost:1455/auth/callback?code=test-code&state={session.state}",
    )

    assert result["authenticated"] is True
    profile = store.get_profile("openai-codex:default")
    assert profile is not None
    assert profile["provider"] == "openai-codex"
    assert profile["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_access_token_refreshes_expired_profile(tmp_path: Path):
    store = AuthProfileStore(tmp_path / "auth_profiles.json")
    manager = OpenAICodexOAuthManager(store=store)
    store.set_profile(
        "openai-codex:default",
        {
            "provider": "openai-codex",
            "type": "oauth",
            "access": _build_jwt(email="alice@example.com", exp=1),
            "refresh": "refresh-old",
            "expires_at": 1,
            "account_id": "acct-1",
            "email": "alice@example.com",
            "display_name": "Alice",
            "updated_at": "2026-04-16T00:00:00Z",
        },
    )
    manager._refresh_tokens = _fake_refresh  # type: ignore[method-assign]

    token = await manager.get_access_token()

    assert token == "access-refresh"
    profile = store.get_profile("openai-codex:default")
    assert profile is not None
    assert profile["refresh"] == "refresh-new"


def test_store_round_trip(tmp_path: Path):
    store = AuthProfileStore(tmp_path / "auth_profiles.json")
    store.set_profile(
        "openai-codex:default",
        {
            "provider": "openai-codex",
            "type": "oauth",
            "access": "access",
            "refresh": "refresh",
            "expires_at": 123,
            "updated_at": "2026-04-16T00:00:00Z",
        },
    )

    raw = json.loads((tmp_path / "auth_profiles.json").read_text(encoding="utf-8"))
    assert "openai-codex:default" in raw["profiles"]
    assert store.get_profile("openai-codex:default")["access"] == "access"


async def _fake_exchange(code: str, verifier: str) -> dict[str, object]:
    assert code == "test-code"
    assert verifier
    return {
        "access_token": _build_jwt(email="alice@example.com", exp=4_102_444_800),
        "refresh_token": "refresh-1",
        "expires_in": 3600,
    }


async def _fake_refresh(refresh_token: str) -> dict[str, object]:
    assert refresh_token == "refresh-old"
    return {
        "access_token": "access-refresh",
        "refresh_token": "refresh-new",
        "expires_in": 3600,
    }


def _build_jwt(*, email: str, exp: int) -> str:
    import base64

    def _part(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return ".".join(
        [
            _part({"alg": "none", "typ": "JWT"}),
            _part({"sub": "acct-1", "email": email, "name": "Alice", "exp": exp}),
            "sig",
        ]
    )


async def _noop() -> None:
    return None
