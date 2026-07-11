from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from uefactory.acquire.polyhaven import PolyHavenAcquireError
from uefactory.acquire.polyhaven_resources import (
    parse_polyhaven_resource_files,
    parse_polyhaven_resource_listing,
    resource_storage_root,
    revisioned_resource_id,
)

HDRI_REVISION = "d69ec09a43016714fd0dda163b3b0c585c968f56"
PBR_REVISION = "cdf3c8f091b3589407bdf0697a2deb2c6b40650d"
SECOND_REVISION = "d69ec09a43016714fd0dda163b3b0c585c968f57"


def _listing_entry(
    *,
    kind: str,
    name: str = "Fixture Resource",
    revision: str = HDRI_REVISION,
    published: int = 10,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": 0 if kind == "hdri" else 1,
        "name": name,
        "date_published": published,
        "files_hash": revision,
        "authors": {"Fixture Author": "All"},
        "categories": ["outdoor", "outdoor"],
        "tags": ["asphalt", "ground"],
    }
    if kind == "pbr_texture_set":
        result["dimensions"] = [30_000, 25_000.5]
    return result


def _entry(url: str, *, size: int = 17, md5: str = "a" * 32) -> dict[str, Any]:
    return {"url": url, "size": size, "md5": md5}


def _hdri_files(source_id: str = "studio-small_03") -> dict[str, Any]:
    return {
        "hdri": {
            "1k": {
                "hdr": _entry(
                    f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/{source_id}_1k.hdr"
                ),
                "exr": _entry(
                    f"https://dl.polyhaven.org/file/ph-assets/HDRIs/exr/1k/{source_id}_1k.exr"
                ),
            },
            "2k": {
                "hdr": _entry(
                    f"https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/{source_id}_2k.hdr"
                )
            },
        }
    }


def _pbr_files(source_id: str = "aerial_asphalt_01") -> dict[str, Any]:
    base = "https://dl.polyhaven.org/file/ph-assets/Textures/jpg/1k"
    return {
        "Diffuse": {
            "1k": {
                "png": _entry(f"{base}/{source_id}_diff_1k.png", size=101, md5="1" * 32),
                "jpg": _entry(f"{base}/{source_id}_diff_1k.jpg"),
            }
        },
        "nor_dx": {
            "1k": {"png": _entry(f"{base}/{source_id}_nor_dx_1k.png", size=102, md5="2" * 32)}
        },
        "arm": {"1k": {"png": _entry(f"{base}/{source_id}_arm_1k.png", size=103, md5="3" * 32)}},
        "nor_gl": {"1k": {"png": _entry(f"{base}/{source_id}_nor_gl_1k.png")}},
    }


def test_parse_hdri_listing_preserves_hyphenated_source_and_metadata() -> None:
    payload = {
        "later_hdri": _listing_entry(kind="hdri", published=20),
        "Studio-small_03": _listing_entry(kind="hdri", name="Studio Small 03", published=10),
    }

    resources = parse_polyhaven_resource_listing(payload, "hdri")

    assert [item.source_id for item in resources] == ["Studio-small_03", "later_hdri"]
    resource = resources[0]
    assert resource.kind == "hdri"
    assert resource.name == "Studio Small 03"
    assert resource.revision == HDRI_REVISION
    assert resource.authors == (("Fixture Author", "All"),)
    assert resource.categories == ("outdoor",)
    assert resource.physical_size_mm is None
    assert resource.profile == "radiance_hdr_v1"
    assert resource.resource_id("1k") == revisioned_resource_id(
        "hdri", "Studio-small_03", HDRI_REVISION, "1k"
    )


def test_parse_pbr_listing_requires_and_normalizes_physical_dimensions() -> None:
    (resource,) = parse_polyhaven_resource_listing(
        {
            "aerial_asphalt_01": _listing_entry(
                kind="pbr_texture_set", name="Aerial Asphalt 01", revision=PBR_REVISION
            )
        },
        "pbr_texture_set",
    )

    assert resource.profile == "ue_pbr_png_v1"
    assert resource.physical_size_mm == (30_000.0, 25_000.5)


def test_listing_canonicalizes_live_provider_author_whitespace() -> None:
    entry = _listing_entry(kind="pbr_texture_set")
    entry["name"] = "Brushed Concrete "
    entry["authors"] = {"Dario Barresi": "Processing "}

    (resource,) = parse_polyhaven_resource_listing({"asphalt_01": entry}, "pbr_texture_set")

    assert resource.name == "Brushed Concrete"
    assert resource.authors == (("Dario Barresi", "Processing"),)


@pytest.mark.parametrize(
    ("source_id", "message"),
    [
        ("-leading", "alphanumeric boundaries"),
        ("trailing_", "alphanumeric boundaries"),
        ("../escape", "safe Poly Haven identifier"),
        ("bad.name", "safe Poly Haven identifier"),
        ("a" * 65, "up to 64 characters"),
    ],
)
def test_listing_rejects_source_ids_outside_strict_boundaries(source_id: str, message: str) -> None:
    with pytest.raises(PolyHavenAcquireError, match=message):
        parse_polyhaven_resource_listing(
            {source_id: _listing_entry(kind="hdri")},
            "hdri",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("empty", "listing is empty"),
        ("bool_type", "type must be integer 0"),
        ("wrong_type", "type must be integer 0"),
        ("uppercase_revision", "lowercase 40-character SHA-1"),
        ("empty_authors", "authors is empty"),
        ("casefold_collision", "collide after casefold normalization"),
    ],
)
def test_listing_rejects_schema_and_portable_identity_violations(
    mutation: str, message: str
) -> None:
    payload = {"Fixture-01": _listing_entry(kind="hdri")}
    if mutation == "empty":
        payload.clear()
    elif mutation == "bool_type":
        payload["Fixture-01"]["type"] = False
    elif mutation == "wrong_type":
        payload["Fixture-01"]["type"] = 1
    elif mutation == "uppercase_revision":
        payload["Fixture-01"]["files_hash"] = "A" * 40
    elif mutation == "empty_authors":
        payload["Fixture-01"]["authors"] = {}
    elif mutation == "casefold_collision":
        payload["fixture-01"] = _listing_entry(kind="hdri")

    with pytest.raises(PolyHavenAcquireError, match=message):
        parse_polyhaven_resource_listing(payload, "hdri")


@pytest.mark.parametrize("dimensions", [None, [1], [1, 2, 3], [True, 2], [0, 2], [1, float("inf")]])
def test_pbr_listing_rejects_non_positive_or_non_finite_dimensions(
    dimensions: Any,
) -> None:
    entry = _listing_entry(kind="pbr_texture_set")
    entry["dimensions"] = dimensions

    with pytest.raises(PolyHavenAcquireError, match="dimensions"):
        parse_polyhaven_resource_listing({"texture_01": entry}, "pbr_texture_set")


def test_hdri_files_select_only_exact_hdr_resolution() -> None:
    package = parse_polyhaven_resource_files("studio-small_03", _hdri_files(), "hdri", "1k")

    assert package.kind == "hdri"
    assert package.profile == "radiance_hdr_v1"
    assert package.resolution == "1k"
    assert len(package.files) == 1
    assert package.files[0].provider_role == "hdri"
    assert package.files[0].relative_path == Path("studio-small_03_1k.hdr")


def test_pbr_files_select_exact_diffuse_directx_normal_and_arm_png_cohort() -> None:
    package = parse_polyhaven_resource_files(
        "aerial_asphalt_01", _pbr_files(), "pbr_texture_set", "1k"
    )

    assert package.profile == "ue_pbr_png_v1"
    assert [item.provider_role for item in package.files] == ["Diffuse", "nor_dx", "arm"]
    assert [item.relative_path.name for item in package.files] == [
        "aerial_asphalt_01_diff_1k.png",
        "aerial_asphalt_01_nor_dx_1k.png",
        "aerial_asphalt_01_arm_1k.png",
    ]
    assert [item.bytes for item in package.files] == [101, 102, 103]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_dx", "no exact 1k ue_pbr_png_v1 cohort"),
        ("jpg_only", "no exact 1k ue_pbr_png_v1 cohort"),
        ("wrong_host", "unapproved Poly Haven download URL"),
        ("http", "unapproved Poly Haven download URL"),
        ("port", "unapproved Poly Haven download URL"),
        ("query", "unapproved Poly Haven download URL"),
        ("wrong_name", "filename does not match"),
        ("uppercase_md5", "lowercase 32-character MD5"),
        ("bool_size", "must be a positive integer"),
        ("oversize", "32 GiB safety limit"),
        ("extra", "unsupported key"),
    ],
)
def test_pbr_files_fail_closed_without_format_or_role_fallback(mutation: str, message: str) -> None:
    payload = _pbr_files()
    entry = payload["nor_dx"]["1k"]["png"]
    if mutation == "missing_dx":
        del payload["nor_dx"]
    elif mutation == "jpg_only":
        payload["nor_dx"]["1k"] = {"jpg": entry}
    elif mutation == "wrong_host":
        entry["url"] = entry["url"].replace("dl.polyhaven.org", "example.test")
    elif mutation == "http":
        entry["url"] = entry["url"].replace("https://", "http://")
    elif mutation == "port":
        entry["url"] = entry["url"].replace("dl.polyhaven.org", "dl.polyhaven.org:443")
    elif mutation == "query":
        entry["url"] += "?token=unsafe"
    elif mutation == "wrong_name":
        entry["url"] = entry["url"].replace("nor_dx", "nor_gl")
    elif mutation == "uppercase_md5":
        entry["md5"] = "A" * 32
    elif mutation == "bool_size":
        entry["size"] = True
    elif mutation == "oversize":
        entry["size"] = 32 * 1024 * 1024 * 1024 + 1
    elif mutation == "extra":
        entry["unexpected"] = "value"

    with pytest.raises(PolyHavenAcquireError, match=message):
        parse_polyhaven_resource_files("aerial_asphalt_01", payload, "pbr_texture_set", "1k")


def test_revisioned_identity_binds_complete_tuple_and_avoids_slug_collisions() -> None:
    baseline = revisioned_resource_id("hdri", "source-a", HDRI_REVISION, "1k")
    variants = {
        revisioned_resource_id("hdri", "source_a", HDRI_REVISION, "1k"),
        revisioned_resource_id("hdri", "source-a", SECOND_REVISION, "1k"),
        revisioned_resource_id("hdri", "source-a", HDRI_REVISION, "2k"),
        revisioned_resource_id("pbr_texture_set", "source-a", HDRI_REVISION, "1k"),
    }

    assert baseline not in variants
    assert len(variants) == 4
    assert len(baseline) <= 64
    assert re.fullmatch(r"[a-z][a-z0-9_]*", baseline)
    assert "__" not in baseline


def test_long_source_ids_form_bounded_catalog_id_without_losing_identity() -> None:
    first = "A-" + "long_source-" * 5 + "x"
    second = "A_" + "long_source-" * 5 + "x"
    assert len(first) <= 64
    assert len(second) <= 64

    first_id = revisioned_resource_id("pbr_texture_set", first, PBR_REVISION, "1k")
    second_id = revisioned_resource_id("pbr_texture_set", second, PBR_REVISION, "1k")

    assert len(first_id) == 64
    assert len(second_id) == 64
    assert first_id != second_id


def test_storage_paths_bind_kind_source_full_revision_profile_and_resolution() -> None:
    package = parse_polyhaven_resource_files(
        "aerial_asphalt_01", _pbr_files(), "pbr_texture_set", "1k"
    )
    expected_root = Path(
        "acquire/polyhaven/resources/pbr_texture_set/aerial_asphalt_01/"
        f"{PBR_REVISION}/ue_pbr_png_v1/1k"
    )

    assert package.storage_root(PBR_REVISION) == expected_root
    assert (
        resource_storage_root("pbr_texture_set", "aerial_asphalt_01", PBR_REVISION, "1k")
        == expected_root
    )
    assert [item.relative_path.parent for item in package.storage_files(PBR_REVISION)] == [
        expected_root,
        expected_root,
        expected_root,
    ]
    assert package.storage_root(PBR_REVISION) != resource_storage_root(
        "pbr_texture_set", "aerial_asphalt_01", PBR_REVISION, "2k"
    )
    assert resource_storage_root("hdri", "source-a", HDRI_REVISION, "1k") != (
        resource_storage_root("hdri", "source_a", HDRI_REVISION, "1k")
    )
    assert not expected_root.is_absolute()
    assert ".." not in expected_root.parts


def test_resource_parsers_do_not_mutate_official_payloads() -> None:
    listing = {"aerial_asphalt_01": _listing_entry(kind="pbr_texture_set")}
    files = _pbr_files()
    listing_before = copy.deepcopy(listing)
    files_before = copy.deepcopy(files)

    parse_polyhaven_resource_listing(listing, "pbr_texture_set")
    parse_polyhaven_resource_files("aerial_asphalt_01", files, "pbr_texture_set", "1k")

    assert listing == listing_before
    assert files == files_before


@pytest.mark.parametrize("kind", ["models", "HDRI", "", None, True])
def test_resource_kind_is_closed(kind: Any) -> None:
    with pytest.raises(PolyHavenAcquireError, match="resource kind"):
        parse_polyhaven_resource_listing({"fixture": _listing_entry(kind="hdri")}, kind)
