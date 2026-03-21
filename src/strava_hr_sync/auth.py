"""OAuth2 authentication for Strava and Fitbit APIs."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

CONFIG_DIR = Path.home() / ".config" / "strava-hr-sync"
CALLBACK_PORT = 8089
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

# Strava OAuth endpoints
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_SCOPES = "activity:read_all,activity:write"

# Fitbit OAuth endpoints
FITBIT_AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_SCOPES = "activity heartrate"


def _token_path(service: str) -> Path:
    return CONFIG_DIR / f"{service}.tokens.json"


def _save_tokens(service: str, tokens: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _token_path(service)
    path.write_text(json.dumps(tokens, indent=2))
    path.chmod(0o600)


def load_tokens(service: str) -> dict[str, Any] | None:
    path = _token_path(service)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _wait_for_auth_code() -> str:
    """Start a local HTTP server to capture the OAuth callback code."""
    code_holder: dict[str, str] = {}
    error_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                error_holder["error"] = params["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authorization failed.</h1><p>You can close this tab.</p>")
            elif "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h1>Authorization successful!</h1><p>You can close this tab.</p>"
                )
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Unexpected request.</h1>")

        def log_message(self, format, *args):
            pass  # Suppress server logs

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), Handler)
    server.timeout = 120

    # Handle just one request
    server.handle_request()
    server.server_close()

    if "error" in error_holder:
        raise RuntimeError(f"OAuth authorization error: {error_holder['error']}")
    if "code" not in code_holder:
        raise RuntimeError("No authorization code received (timed out?).")
    return code_holder["code"]


def authenticate_strava(client_id: str, client_secret: str) -> dict[str, Any]:
    """Run Strava OAuth2 flow. Opens browser, captures callback, returns tokens."""
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": STRAVA_SCOPES,
        "approval_prompt": "force",
    }
    auth_url = f"{STRAVA_AUTH_URL}?{urllib.parse.urlencode(params)}"

    print(f"Opening browser for Strava authorization...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")

    # Open browser in background thread to not block
    threading.Thread(target=webbrowser.open, args=(auth_url,), daemon=True).start()

    code = _wait_for_auth_code()

    # Exchange code for tokens
    resp = httpx.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    tokens = resp.json()
    tokens["client_id"] = client_id
    tokens["client_secret"] = client_secret
    _save_tokens("strava", tokens)
    print("Strava authentication successful!")
    return tokens


def authenticate_fitbit(client_id: str, client_secret: str) -> dict[str, Any]:
    """Run Fitbit OAuth2 PKCE flow. Opens browser, captures callback, returns tokens."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": FITBIT_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{FITBIT_AUTH_URL}?{urllib.parse.urlencode(params)}"

    print(f"Opening browser for Fitbit authorization...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")

    threading.Thread(target=webbrowser.open, args=(auth_url,), daemon=True).start()

    code = _wait_for_auth_code()

    # Exchange code for tokens
    resp = httpx.post(
        FITBIT_TOKEN_URL,
        data={
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        auth=(client_id, client_secret),
    )
    resp.raise_for_status()
    tokens = resp.json()
    tokens["client_id"] = client_id
    tokens["client_secret"] = client_secret
    _save_tokens("fitbit", tokens)
    print("Fitbit authentication successful!")
    return tokens


def refresh_strava_token(tokens: dict[str, Any]) -> dict[str, Any]:
    """Refresh Strava access token using refresh_token."""
    resp = httpx.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": tokens["client_id"],
            "client_secret": tokens["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    new_tokens["client_id"] = tokens["client_id"]
    new_tokens["client_secret"] = tokens["client_secret"]
    _save_tokens("strava", new_tokens)
    return new_tokens


def refresh_fitbit_token(tokens: dict[str, Any]) -> dict[str, Any]:
    """Refresh Fitbit access token using refresh_token."""
    resp = httpx.post(
        FITBIT_TOKEN_URL,
        data={
            "client_id": tokens["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        auth=(tokens["client_id"], tokens["client_secret"]),
    )
    resp.raise_for_status()
    new_tokens = resp.json()
    new_tokens["client_id"] = tokens["client_id"]
    new_tokens["client_secret"] = tokens["client_secret"]
    _save_tokens("fitbit", new_tokens)
    return new_tokens


def get_fitbit_client() -> httpx.Client:
    """Create an httpx Client with Fitbit auth and auto-refresh on 401."""
    tokens = load_tokens("fitbit")
    if tokens is None:
        raise RuntimeError("Not authenticated with Fitbit. Run: strava-hr-sync auth fitbit")

    transport = httpx.HTTPTransport(retries=1)
    client = httpx.Client(
        base_url="https://api.fitbit.com",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        transport=transport,
        timeout=30.0,
    )
    # Attach tokens for refresh logic
    client._tokens = tokens  # type: ignore[attr-defined]
    return client


def get_strava_client() -> httpx.Client:
    """Create an httpx Client with Strava auth and auto-refresh."""
    tokens = load_tokens("strava")
    if tokens is None:
        raise RuntimeError("Not authenticated with Strava. Run: strava-hr-sync auth strava")

    if tokens.get("expires_at", 0) < time.time():
        tokens = refresh_strava_token(tokens)

    transport = httpx.HTTPTransport(retries=1)
    client = httpx.Client(
        base_url="https://www.strava.com/api/v3",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        transport=transport,
        timeout=30.0,
    )
    client._tokens = tokens  # type: ignore[attr-defined]
    return client
