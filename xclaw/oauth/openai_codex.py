"""OpenAI Codex OAuth flow management for XClaw."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import httpx

from xclaw.oauth.store import AuthProfileStore


AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
DEFAULT_PROFILE_ID = "openai-codex:default"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"


class OAuthNotAuthenticatedError(RuntimeError):
    """Raised when no usable OAuth credential is available."""


class OAuthLoginNotFoundError(RuntimeError):
    """Raised when a login session is missing or expired."""


@dataclass
class LoginSession:
    login_id: str
    state: str
    code_verifier: str
    authorize_url: str
    created_at: float
    expires_at: float
    mode: str
    status: str = "pending"
    error: str | None = None
    email: str | None = None
    display_name: str | None = None
    account_id: str | None = None


class OpenAICodexOAuthManager:
    """Manage OpenAI Codex OAuth login, refresh and storage."""

    def __init__(
        self,
        *,
        store: AuthProfileStore,
        base_url: str = DEFAULT_BASE_URL,
        session_ttl_seconds: int = 600,
        callback_host: str = "127.0.0.1",
        callback_port: int = 1455,
        originator: str = "xclaw",
        request_timeout: float = 20.0,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.session_ttl_seconds = session_ttl_seconds
        self.callback_host = callback_host
        self.callback_port = callback_port
        self.originator = originator
        self._client = httpx.AsyncClient(timeout=request_timeout, follow_redirects=False)
        self._refresh_lock = asyncio.Lock()
        self._sessions: dict[str, LoginSession] = {}
        self._sessions_by_state: dict[str, str] = {}
        self._callback_server: ThreadingHTTPServer | None = None
        self._callback_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def close(self) -> None:
        await self._client.aclose()
        if self._callback_server is not None:
            self._callback_server.shutdown()
            self._callback_server.server_close()
        if self._callback_thread is not None and self._callback_thread.is_alive():
            self._callback_thread.join(timeout=1)

    async def start_login(self) -> dict[str, Any]:
        self._cleanup_expired_sessions()
        await self.run_tls_preflight()
        self._loop = asyncio.get_running_loop()
        login_id = secrets.token_urlsafe(18)
        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = self._build_code_challenge(code_verifier)
        listener_ready = self._ensure_callback_listener()
        mode = "auto_callback" if listener_ready else "manual"
        authorize_url = self._build_authorize_url(state=state, code_challenge=code_challenge)
        session = LoginSession(
            login_id=login_id,
            state=state,
            code_verifier=code_verifier,
            authorize_url=authorize_url,
            created_at=time.time(),
            expires_at=time.time() + self.session_ttl_seconds,
            mode=mode,
        )
        self._sessions[login_id] = session
        self._sessions_by_state[state] = login_id
        return self._session_payload(session)

    async def get_login_status(self, login_id: str) -> dict[str, Any]:
        session = self._get_session(login_id)
        return self._session_payload(session)

    async def complete_login(self, login_id: str, redirect_url_or_code: str) -> dict[str, Any]:
        session = self._get_session(login_id)
        code, state = self._parse_authorization_input(redirect_url_or_code)
        if state and state != session.state:
            session.status = "failed"
            session.error = "OAuth state mismatch."
            raise ValueError(session.error)
        if not code:
            raise ValueError("No authorization code found.")
        return await self._finalize_login(session, code)

    async def logout(self) -> None:
        self.store.delete_profile(DEFAULT_PROFILE_ID)

    async def get_session_payload(self) -> dict[str, Any]:
        profile = self.store.get_profile(DEFAULT_PROFILE_ID)
        if not profile:
            return {
                "provider": "openai-codex",
                "authenticated": False,
                "email": None,
                "display_name": None,
                "account_id": None,
                "expires_at": None,
            }
        return {
            "provider": "openai-codex",
            "authenticated": True,
            "email": profile.get("email"),
            "display_name": profile.get("display_name"),
            "account_id": profile.get("account_id"),
            "expires_at": self._iso_timestamp(profile.get("expires_at")),
        }

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        profile = self.store.get_profile(DEFAULT_PROFILE_ID)
        if not profile:
            raise OAuthNotAuthenticatedError("OpenAI Codex OAuth is not authenticated.")
        expires_at = int(profile.get("expires_at", 0) or 0)
        now = int(time.time())
        if not force_refresh and expires_at > now + 60:
            return str(profile["access"])

        async with self._refresh_lock:
            current = self.store.get_profile(DEFAULT_PROFILE_ID)
            if not current:
                raise OAuthNotAuthenticatedError("OpenAI Codex OAuth is not authenticated.")
            current_expires = int(current.get("expires_at", 0) or 0)
            if not force_refresh and current_expires > int(time.time()) + 60:
                return str(current["access"])
            refresh_token = str(current.get("refresh") or "")
            if not refresh_token:
                raise OAuthNotAuthenticatedError("OpenAI Codex refresh token is missing.")
            refreshed = await self._refresh_tokens(refresh_token)
            updated = self._build_profile_from_tokens(
                refreshed["access_token"],
                refreshed["refresh_token"],
                int(refreshed.get("expires_in", 3600)),
            )
            self.store.set_profile(DEFAULT_PROFILE_ID, updated)
            return str(updated["access"])

    async def run_tls_preflight(self) -> None:
        try:
            await self._client.get(
                AUTHORIZE_URL,
                params={
                    "response_type": "code",
                    "client_id": "xclaw-preflight",
                    "redirect_uri": REDIRECT_URI,
                    "scope": "openid profile email",
                },
            )
        except httpx.ConnectError as exc:
            message = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in message or "certificate verify failed" in message:
                raise RuntimeError(
                    "OpenAI OAuth TLS preflight failed due to local certificate verification. "
                    "Please install/update your system CA certificates and retry."
                ) from exc
            raise RuntimeError(f"OpenAI OAuth preflight failed: {message}") from exc

    def _ensure_callback_listener(self) -> bool:
        if self._callback_server is not None:
            return True

        manager = self

        class _CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/auth/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                code = params.get("code", [""])[0]
                state = params.get("state", [""])[0]
                error = params.get("error", [""])[0]
                if manager._loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        manager._handle_callback(code=code, state=state, error=error or None),
                        manager._loop,
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = "<html><body><h2>Authentication completed</h2><p>You can return to XClaw.</p></body></html>"
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        try:
            server = ThreadingHTTPServer((self.callback_host, self.callback_port), _CallbackHandler)
        except OSError:
            return False

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._callback_server = server
        self._callback_thread = thread
        return True

    async def _handle_callback(self, *, code: str, state: str, error: str | None) -> None:
        login_id = self._sessions_by_state.get(state)
        if not login_id:
            return
        session = self._sessions.get(login_id)
        if session is None:
            return
        if error:
            session.status = "failed"
            session.error = error
            return
        if not code:
            session.status = "failed"
            session.error = "Missing authorization code."
            return
        try:
            await self._finalize_login(session, code)
        except Exception as exc:  # pragma: no cover - exercised through status
            session.status = "failed"
            session.error = str(exc)

    async def _finalize_login(self, session: LoginSession, code: str) -> dict[str, Any]:
        try:
            exchanged = await self._exchange_code_for_tokens(code, session.code_verifier)
        except Exception as exc:
            session.status = "failed"
            session.error = str(exc)
            raise
        profile = self._build_profile_from_tokens(
            str(exchanged["access_token"]),
            str(exchanged["refresh_token"]),
            int(exchanged.get("expires_in", 3600)),
        )
        self.store.set_profile(DEFAULT_PROFILE_ID, profile)
        session.status = "completed"
        session.email = profile.get("email")
        session.display_name = profile.get("display_name")
        session.account_id = profile.get("account_id")
        return await self.get_session_payload()

    async def _exchange_code_for_tokens(self, code: str, verifier: str) -> dict[str, Any]:
        response = await self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()

    async def _refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        response = await self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()

    def _build_authorize_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": state,
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "originator": self.originator,
            },
            quote_via=quote_plus,
        )
        return f"{AUTHORIZE_URL}?{query}"

    @staticmethod
    def _build_code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    @staticmethod
    def _parse_authorization_input(raw: str) -> tuple[str, str]:
        text = raw.strip()
        if "://" not in text:
            return text, ""
        parsed = urlparse(text)
        params = parse_qs(parsed.query)
        return params.get("code", [""])[0], params.get("state", [""])[0]

    def _build_profile_from_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ) -> dict[str, Any]:
        identity = self._decode_access_token(access_token)
        expires_at = int(time.time()) + max(expires_in, 1)
        return {
            "provider": "openai-codex",
            "type": "oauth",
            "access": access_token,
            "refresh": refresh_token,
            "expires_at": expires_at,
            "account_id": identity.get("account_id"),
            "email": identity.get("email"),
            "display_name": identity.get("display_name"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _decode_access_token(token: str) -> dict[str, str]:
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return {}
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception:
            return {}
        return {
            "account_id": str(data.get("sub") or ""),
            "email": str(data.get("email") or ""),
            "display_name": str(
                data.get("name")
                or data.get("nickname")
                or data.get("preferred_username")
                or ""
            ),
        }

    def _get_session(self, login_id: str) -> LoginSession:
        self._cleanup_expired_sessions()
        session = self._sessions.get(login_id)
        if session is None:
            raise OAuthLoginNotFoundError("OAuth login session was not found.")
        if session.status not in {"completed", "failed"} and session.expires_at <= time.time():
            session.status = "expired"
        return session

    def _cleanup_expired_sessions(self) -> None:
        expired: list[str] = []
        now = time.time()
        for login_id, session in self._sessions.items():
            if session.status in {"completed", "failed"}:
                continue
            if session.expires_at <= now:
                session.status = "expired"
                expired.append(login_id)
        for login_id in expired:
            session = self._sessions.get(login_id)
            if session is not None:
                self._sessions_by_state.pop(session.state, None)

    def _session_payload(self, session: LoginSession) -> dict[str, Any]:
        return {
            "login_id": session.login_id,
            "status": session.status,
            "mode": session.mode,
            "authorize_url": session.authorize_url,
            "expires_at": self._iso_timestamp(session.expires_at),
            "authenticated": session.status == "completed",
            "error": session.error,
            "email": session.email,
            "display_name": session.display_name,
            "account_id": session.account_id,
        }

    @staticmethod
    def _iso_timestamp(value: Any) -> str | None:
        if not value:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except Exception:
            return None
