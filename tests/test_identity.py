from __future__ import annotations

import pytest

from uefactory.core.identity import validate_asset_id


def test_validate_asset_id_accepts_canonical_snake_case() -> None:
    assert validate_asset_id("khronos_avocado") == "khronos_avocado"


@pytest.mark.parametrize("value", ["1asset", "Asset", "asset__one", "asset_", "asset-id"])
def test_validate_asset_id_rejects_noncanonical_value(value: str) -> None:
    with pytest.raises(ValueError, match="snake_case"):
        validate_asset_id(value)
