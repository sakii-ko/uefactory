from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from uefactory.core.config import Settings, load_settings
from uefactory.render.job import RenderJobResult, compare_job_outputs, render_job
from uefactory.render.jobspec import load_jobspec
from uefactory.render.passes import (
    PASS_FORMATS,
    PassFrameStats,
    PassValidation,
    stable_validation_payload,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORBIT8_JOB = PROJECT_ROOT / "examples/orbit8.yaml"
EXPECTED_PASSES = (
    "beauty_lit",
    "beauty_unlit",
    "depth",
    "normal",
    "basecolor",
    "object_mask",
)
PNG_PASSES = frozenset({"beauty_lit", "beauty_unlit", "normal", "basecolor"})
EXR_PASSES = frozenset({"depth", "object_mask"})


@pytest.fixture(scope="module")
def ue_settings() -> Settings:
    return load_settings(project_root=PROJECT_ROOT)


@pytest.mark.ue
def test_orbit8_render_job_end_to_end(ue_settings: Settings) -> None:
    result = render_job(settings=ue_settings, job_path=ORBIT8_JOB, timeout_sec=1800)

    assert result.spec.passes == EXPECTED_PASSES
    assert result.spec.frame_count == 8
    assert set(result.frame_paths) == set(EXPECTED_PASSES)
    assert set(result.pass_validations) == set(EXPECTED_PASSES)

    for pass_name in EXPECTED_PASSES:
        paths = result.frame_paths[pass_name]
        validation = result.pass_validations[pass_name]
        expected_format = PASS_FORMATS[pass_name]

        assert len(paths) == 8
        assert validation.frame_count == 8
        assert validation.resolution == (640, 360)
        assert validation.format == expected_format
        assert tuple(frame.frame for frame in validation.frames) == tuple(
            path.name for path in paths
        )
        for path, frame in zip(paths, validation.frames, strict=True):
            assert path.is_file()
            assert path.suffix == expected_format.extension
            assert len(frame.pixel_sha256) == 64
            int(frame.pixel_sha256, 16)

            if pass_name in PNG_PASSES:
                with Image.open(path) as image:
                    image.load()
                    assert image.format == "PNG"
                    assert image.mode == "RGB"
                    assert image.getbands() == ("R", "G", "B")

        if pass_name in PNG_PASSES:
            assert validation.format.bit_depth == 8
            assert validation.format.pixel_type == "uint8"
            assert validation.format.channels == 3
        else:
            assert pass_name in EXR_PASSES
            assert validation.format.bit_depth == 16
            assert validation.format.pixel_type == "float16"
            assert validation.format.channels == 4

    # render_job only returns after validate_render_pass has also applied the
    # repeated OCIO INVALID-overlay guard to every RGB frame.
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    expected_engine = json.loads(
        ue_settings.ue_root.joinpath("Engine/Build/Build.version").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ok"
    assert manifest["render_kind"] == "job"
    assert manifest["engine"] == expected_engine
    assert manifest["passes"] == stable_validation_payload(result.pass_validations)
    assert manifest["asset_cleanup"]["status"] == "ok"

    assert set(manifest["frame_paths"]) == set(EXPECTED_PASSES)
    for pass_name in EXPECTED_PASSES:
        relative_paths = manifest["frame_paths"][pass_name]
        assert len(relative_paths) == 8
        for relative_path, actual_path in zip(
            relative_paths, result.frame_paths[pass_name], strict=True
        ):
            path = Path(relative_path)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert result.run_dir / path == actual_path

    cleanup_path = Path(manifest["asset_cleanup"]["path"])
    assert not cleanup_path.is_absolute()
    assert not ue_settings.project_root.joinpath(cleanup_path).exists()

    assert result.artifacts is not None
    assert result.artifacts.turntable_mp4 is not None
    artifacts = result.artifacts.manifest_payload(run_dir=result.run_dir)
    assert artifacts == {
        "contact_sheet": "contact_sheet.png",
        "index_html": "index.html",
        "turntable_mp4": "turntable.mp4",
    }
    assert manifest["artifacts"] == artifacts
    for relative_path in artifacts.values():
        assert relative_path is not None
        assert not Path(relative_path).is_absolute()
        assert result.run_dir.joinpath(relative_path).is_file()

    with Image.open(result.artifacts.contact_sheet) as contact_sheet:
        contact_sheet.load()
        assert contact_sheet.format == "PNG"
        assert contact_sheet.mode == "RGB"
        assert contact_sheet.width > 0
        assert contact_sheet.height > 0

    index_html = result.artifacts.index_html.read_text(encoding="utf-8")
    assert str(result.run_dir) not in index_html
    for pass_name in EXPECTED_PASSES:
        assert pass_name in index_html
        for relative_path in manifest["frame_paths"][pass_name]:
            assert f'href="{relative_path}"' in index_html

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(result.artifacts.turntable_mp4),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert float(probe.stdout.strip()) == pytest.approx(4.0, abs=0.05)


def test_compare_job_outputs_rejects_pixel_hash_difference() -> None:
    first_frame = PassFrameStats(
        frame="frame_0000.png",
        pixel_sha256="0" * 64,
        mean=(80.0, 100.0, 120.0),
        min=(0.0, 0.0, 0.0),
        max=(255.0, 255.0, 255.0),
        stddev=(20.0, 20.0, 20.0),
    )
    second_frame = replace(first_frame, pixel_sha256="1" * 64)
    first = _comparison_result(first_frame)
    second = _comparison_result(second_frame)

    with pytest.raises(RuntimeError, match="decoded pass output mismatch"):
        compare_job_outputs(first, second)


def _comparison_result(frame: PassFrameStats) -> RenderJobResult:
    validation = PassValidation(
        pass_name="beauty_lit",
        format=PASS_FORMATS["beauty_lit"],
        resolution=(640, 360),
        frame_count=1,
        frames=(frame,),
    )
    return RenderJobResult(
        run_dir=PROJECT_ROOT / "out/test-comparison",
        manifest_path=PROJECT_ROOT / "out/test-comparison/manifest.json",
        ue_log_path=PROJECT_ROOT / "out/test-comparison/ue.log",
        setup_log_path=PROJECT_ROOT / "out/test-comparison/ue_setup.log",
        frame_paths={"beauty_lit": []},
        pass_validations={"beauty_lit": validation},
        spec=load_jobspec(ORBIT8_JOB),
    )
