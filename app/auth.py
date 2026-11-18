"""Small demo-identity boundary for HomeShop.

This assignment intentionally has no account or password system. The selected
browser identity is sent in ``X-HomeShop-User`` and is enforced by the API so
one demo seller cannot manage another seller's inventory. It is not production
authentication: a real deployment would replace this with verified sessions.
"""
import re

from fastapi import Header, HTTPException


USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def validate_user_id(user_id: str | None) -> str:
    """Normalize and validate the constrained HomeShop demo identity."""
    normalized = (user_id or "alice").strip().lower()
    if not USER_ID_RE.fullmatch(normalized):
        raise ValueError("User ID must be 1-32 lowercase letters, numbers, _ or -")
    return normalized


def current_user(x_homeshop_user: str | None = Header(default=None)) -> str:
    """Return a constrained, stable demo identity, defaulting to ``alice``."""
    try:
        return validate_user_id(x_homeshop_user)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error


def require_same_user(actor: str, user_id: str) -> None:
    """Prevent an API caller from operating on another customer's cart."""
    if actor != user_id.lower():
        raise HTTPException(status_code=403, detail="You cannot access another user's cart or orders")
