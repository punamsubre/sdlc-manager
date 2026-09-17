"""Authentication and session management via Google OAuth 2.0 and JWT."""

import os
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

security = HTTPBearer(auto_error=False)
JWT_ALGORITHM = "HS256"

# Logout revocation: token-hash -> expiry. In-memory (per instance) —
# same accepted scope as the rate limiter at max 2 instances.
_REVOKED_TOKENS: dict[str, float] = {}


def revoke_token(token: str, ttl_seconds: float = 8 * 3600) -> None:
    import hashlib

    now = time.time()
    for k, exp in list(_REVOKED_TOKENS.items()):
        if exp < now:
            _REVOKED_TOKENS.pop(k, None)
    _REVOKED_TOKENS[hashlib.sha256(token.encode()).hexdigest()] = now + ttl_seconds


def _is_revoked(token: str) -> bool:
    import hashlib

    entry = _REVOKED_TOKENS.get(hashlib.sha256(token.encode()).hexdigest())
    return entry is not None and entry > time.time()

# Development-only fallback. Production deployments must provide JWT_SECRET_KEY
# (Secret Manager on Cloud Run); assert_production_auth_config() enforces this.
_DEV_FALLBACK_SIGNING_KEY = "change-this-to-a-secure-random-key-in-production"


def _jwt_secret() -> str:
    return os.getenv("JWT_SECRET_KEY") or _DEV_FALLBACK_SIGNING_KEY


def assert_production_auth_config() -> None:
    """Refuse to run with authentication enabled but no real signing key."""
    auth_enabled = os.getenv("AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
    if auth_enabled and _jwt_secret() == _DEV_FALLBACK_SIGNING_KEY:
        raise RuntimeError(
            "AUTH_ENABLED is true but JWT_SECRET_KEY is not set. "
            "Provide a strong random key (e.g. via Secret Manager) before serving."
        )


class UserSession(BaseModel):
    """Authenticated user profile."""

    email: str
    name: str | None = None
    picture: str | None = None
    role: str = "developer"


def create_access_token(
    user_email: str,
    name: str | None = None,
    picture: str | None = None,
    role: str = "developer",
    expires_delta_hours: int = 8,
) -> str:
    """Create signed JWT session token."""
    secret_key = _jwt_secret()
    expires = time.time() + (expires_delta_hours * 3600)
    payload = {
        "sub": user_email,
        "name": name,
        "picture": picture,
        "role": role,
        "exp": expires,
    }
    encoded = jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM)
    return str(encoded)


def decode_access_token(token: str) -> UserSession | None:
    """Validate and decode JWT token into UserSession."""
    if _is_revoked(token):
        return None
    secret_key = _jwt_secret()
    try:
        payload = jwt.decode(token, secret_key, algorithms=[JWT_ALGORITHM])
        # A session token must never satisfy a purpose-bound check and
        # vice versa (e.g. the GitHub OAuth state parameter).
        if payload.get("purpose"):
            return None
        email = payload.get("sub")
        if not email:
            return None
        return UserSession(
            email=email,
            name=payload.get("name"),
            picture=payload.get("picture"),
            role=payload.get("role", "developer"),
        )
    except JWTError:
        return None


def create_state_token(user_email: str, nonce: str) -> str:
    """Short-lived, purpose-bound OAuth state: only verify_state_token
    accepts it, and only when the browser presents the matching nonce
    cookie — a plain session JWT can no longer stand in as state."""
    payload = {
        "sub": user_email,
        "purpose": "gh-oauth-state",
        "nonce": nonce,
        "exp": time.time() + 600,
    }
    return str(jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM))


def verify_state_token(state: str, nonce: str | None) -> bool:
    try:
        payload = jwt.decode(state, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError:
        return False
    return bool(
        payload.get("purpose") == "gh-oauth-state"
        and nonce
        and payload.get("nonce") == nonce
    )


# --- Per-developer GitHub session (OAuth token carried in a signed cookie) ---

GITHUB_COOKIE = "gh_session"


def _cookie_cipher():
    """AES-GCM keyed from the JWT secret: a JWT is only SIGNED, so the
    GitHub token must be encrypted before it rides in a cookie."""
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(hashlib.sha256(_jwt_secret().encode()).digest())


def _encrypt_value(value: str) -> str:
    import base64
    import os as _os

    nonce = _os.urandom(12)
    sealed = _cookie_cipher().encrypt(nonce, value.encode(), None)
    return base64.urlsafe_b64encode(nonce + sealed).decode()


def _decrypt_value(blob: str) -> str | None:
    import base64

    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        return _cookie_cipher().decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        return None


def create_github_session_token(gh_token: str, login: str, days: int = 30) -> str:
    """Wrap a developer's GitHub OAuth token (encrypted) in a signed JWT."""
    secret_key = _jwt_secret()
    payload = {
        "sub": f"github:{login}",
        "ghe": _encrypt_value(gh_token),
        "login": login,
        "exp": time.time() + days * 86400,
    }
    return str(jwt.encode(payload, secret_key, algorithm=JWT_ALGORITHM))


def read_github_session(request: Request) -> tuple[str | None, str | None]:
    """Return (github_token, github_login) from the signed cookie, if present and valid."""
    raw = request.cookies.get(GITHUB_COOKIE)
    if not raw:
        return None, None
    secret_key = _jwt_secret()
    try:
        payload = jwt.decode(raw, secret_key, algorithms=[JWT_ALGORITHM])
        sealed = payload.get("ghe")
        token = _decrypt_value(sealed) if sealed else None
        # Legacy plaintext cookies are rejected, not honored — the user
        # simply reconnects GitHub once.
        return token, payload.get("login") if token else None
    except JWTError:
        return None, None


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(security),
) -> UserSession:
    """Extract authenticated user from Bearer header or session cookie."""
    auth_enabled = os.getenv("AUTH_ENABLED", "false").lower() in ("true", "1", "yes")
    if not auth_enabled:
        return UserSession(
            email="developer@local.dev",
            name="Local Developer",
            role="admin",
        )

    token = None
    if creds and creds.credentials:
        token = creds.credentials
    elif "session_token" in request.cookies:
        token = request.cookies.get("session_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    user = decode_access_token(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )

    return user
