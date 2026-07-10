from __future__ import annotations

from typing import Any


def validate_snake_slug(
    value: Any,
    *,
    field: str,
    max_length: int = 64,
) -> str:
    """Return a canonical lowercase snake_case identifier or raise ValueError."""

    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field}: expected 1 to {max_length} characters")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz":
        raise ValueError(f"{field}: expected lowercase snake_case starting with a letter")
    if (
        any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)
        or "__" in value
        or value.endswith("_")
    ):
        raise ValueError(f"{field}: expected lowercase snake_case starting with a letter")
    return value


def validate_asset_id(value: Any, *, field: str = "asset_id") -> str:
    return validate_snake_slug(value, field=field, max_length=64)
