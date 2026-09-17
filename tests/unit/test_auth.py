"""Unit tests for JWT authentication."""

from sdlc_manager.dashboard.auth import (
    create_access_token,
    decode_access_token,
)


def test_create_and_decode_jwt():
    token = create_access_token(
        user_email="test@company.com",
        name="Test User",
        role="developer",
        expires_delta_hours=2,
    )
    assert token is not None

    session = decode_access_token(token)
    assert session is not None
    assert session.email == "test@company.com"
    assert session.name == "Test User"
    assert session.role == "developer"


def test_decode_invalid_jwt():
    session = decode_access_token("invalid.token.string")
    assert session is None
