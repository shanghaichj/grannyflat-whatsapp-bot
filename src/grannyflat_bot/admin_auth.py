"""Admin console auth: login cookie (weekly) + optional HTTP Basic fallback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from grannyflat_bot.config import get_settings

ADMIN_COOKIE_NAME = "granny_admin_session"
security = HTTPBasic(auto_error=False)


def _b64url_decode(data: str) -> bytes:
    pad = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _session_secret() -> str:
    settings = get_settings()
    if settings.admin_session_secret:
        return settings.admin_session_secret
    if settings.admin_password:
        return settings.admin_password
    return ""


def verify_admin_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    if not settings.admin_password:
        return False
    username = username.strip()
    if not username or not password:
        return False
    user_ok = secrets.compare_digest(
        username.encode("utf-8"),
        settings.admin_username.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    return user_ok and pass_ok


def create_session_token(username: str) -> str:
    settings = get_settings()
    secret = _session_secret()
    if not secret:
        raise ValueError("Admin session secret is not configured")
    expires_at = int(time.time()) + settings.admin_session_days * 86400
    payload = {"u": username, "exp": expires_at}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session_token(token: str) -> str | None:
    secret = _session_secret()
    if not secret or not token or "." not in token:
        return None
    body, sig = token.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    username = str(payload.get("u") or "").strip()
    return username or None


def get_session_user(request: Request) -> str | None:
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        return None
    return verify_session_token(token)


def session_status(request: Request) -> dict[str, Any]:
    settings = get_settings()
    if not settings.admin_auth_enabled:
        return {
            "authenticated": False,
            "auth_required": False,
            "admin_open": True,
            "username": None,
            "expires_at": None,
            "session_days": settings.admin_session_days,
        }
    user = get_session_user(request)
    if not user:
        return {
            "authenticated": False,
            "auth_required": True,
            "username": None,
            "expires_at": None,
            "session_days": settings.admin_session_days,
        }
    token = request.cookies.get(ADMIN_COOKIE_NAME) or ""
    expires_at = None
    if "." in token:
        body = token.split(".", 1)[0]
        try:
            payload = json.loads(_b64url_decode(body).decode("utf-8"))
            expires_at = payload.get("exp")
        except (ValueError, json.JSONDecodeError):
            expires_at = None
    return {
        "authenticated": True,
        "auth_required": True,
        "username": user,
        "expires_at": expires_at,
        "session_days": settings.admin_session_days,
    }


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    """Protect /admin APIs. Cookie session first; Basic Auth as fallback."""
    settings = get_settings()
    if not settings.admin_password:
        return "open"

    user = get_session_user(request)
    if user:
        return user

    if credentials is not None and credentials.username and credentials.password:
        if verify_admin_credentials(credentials.username, credentials.password):
            return credentials.username

    raise _unauthorized()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Admin login required",
        headers={"WWW-Authenticate": 'Basic realm="Granny Flat Admin"'},
    )
