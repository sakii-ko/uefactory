from __future__ import annotations

import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import OpenEXR
import pytest
import typer
from PIL import Image
from typer.testing import CliRunner

from uefactory.cli.render import render_app
from uefactory.core.config import Settings
from uefactory.render.showcase import ShowcaseError, _publish_directory, create_showcase

FIXED_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
FIXED_COMMIT = "a" * 40


def _app(tmp_path: Path) -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def root(ctx: typer.Context) -> None:
        ctx.obj = {"settings": Settings(project_root=tmp_path, data_dir=tmp_path / "data")}

    app.add_typer(render_app, name="render")
    return app


def _render_run(
    project_root: Path,
    *,
    frame_count: int = 72,
    resolution: tuple[int, int] = (1440, 1440),
    status: str = "ok",
    foreground_span: float = 0.6,
) -> Path:
    run_dir = (
        project_root / "out/showcase_source_renders/20260711T115000Z_fixture/scene_bm_player_home"
    )
    beauty_dir = run_dir / "beauty_lit"
    mask_dir = run_dir / "object_mask"
    beauty_dir.mkdir(parents=True)
    mask_dir.mkdir()
    width, height = resolution
    mask_values = np.zeros((height, width), dtype=np.float16)
    foreground_width = max(1, int(round(width * foreground_span)))
    foreground_height = max(1, int(round(height * foreground_span)))
    x_start = (width - foreground_width) // 2
    y_start = (height - foreground_height) // 2
    mask_values[
        y_start : y_start + foreground_height,
        x_start : x_start + foreground_width,
    ] = np.float16(1.0 / 255.0)
    mask_rgba = np.repeat(mask_values[:, :, np.newaxis], 4, axis=2)
    first_mask = mask_dir / "frame_0000.exr"
    OpenEXR.File(
        {"compression": OpenEXR.ZIP_COMPRESSION},
        {"RGBA": mask_rgba},
    ).write(str(first_mask))
    mask_bytes = first_mask.read_bytes()
    mask_pixel_sha256 = hashlib.sha256(np.ascontiguousarray(mask_rgba).tobytes()).hexdigest()
    relative_paths: list[str] = []
    mask_relative_paths: list[str] = []
    validation_frames: list[dict[str, Any]] = []
    mask_validation_frames: list[dict[str, Any]] = []
    frame_luma: list[float] = []
    for index in range(frame_count):
        source_image = Image.new(
            "RGB",
            resolution,
            (48 + index % 64, 96 + index % 32, 144 + index % 48),
        )
        image_buffer = io.BytesIO()
        source_image.save(image_buffer, format="PNG")
        frame_bytes = image_buffer.getvalue()
        pixel_sha256 = hashlib.sha256(source_image.tobytes()).hexdigest()
        name = f"frame_{index:04d}.png"
        (beauty_dir / name).write_bytes(frame_bytes)
        relative_paths.append(f"beauty_lit/{name}")
        validation_frames.append({"frame": name, "pixel_sha256": pixel_sha256})
        mask_name = f"frame_{index:04d}.exr"
        if index:
            (mask_dir / mask_name).write_bytes(mask_bytes)
        mask_relative_paths.append(f"object_mask/{mask_name}")
        mask_validation_frames.append({"frame": mask_name, "pixel_sha256": mask_pixel_sha256})
        frame_luma.append(round((48 + index % 64 + 96 + index % 32 + 144 + index % 48) / 3, 3))
    manifest = {
        "schema_version": 3,
        "status": status,
        "render_kind": "job",
        "asset_id": "scene:bm_player_home",
        "asset_cleanup": {"status": "ok", "removed": True},
        "camera": {
            "rig": "orbit",
            "views": frame_count,
            "resolution": list(resolution),
        },
        "asset": {
            "kind": "scene",
            "asset_id": "scene:bm_player_home",
            "scene_id": "bm_player_home",
            "source": "blackmyth_asset_library",
            "source_id": "player_home",
            "source_url": "https://example.test/player-home",
            "source_sha256": "b" * 64,
            "build_sha256": "c" * 64,
            "package_bundle_sha256": "d" * 64,
            "export": True,
            "license": "CC-BY-4.0",
            "license_tier": "open",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "Fixture Artist - Player Home",
        },
        "frame_paths": {
            "beauty_lit": relative_paths,
            "object_mask": mask_relative_paths,
        },
        "frames_expected": frame_count,
        "frames_found": {"beauty_lit": frame_count, "object_mask": frame_count},
        "frame_luma": frame_luma,
        "requested_passes": ["beauty_lit", "object_mask"],
        "passes": {
            "beauty_lit": {
                "frame_count": frame_count,
                "resolution": list(resolution),
                "format": {
                    "extension": ".png",
                    "bit_depth": 8,
                    "pixel_type": "uint8",
                    "channels": 3,
                },
                "frames": validation_frames,
            },
            "object_mask": {
                "frame_count": frame_count,
                "resolution": list(resolution),
                "format": {
                    "extension": ".exr",
                    "bit_depth": 16,
                    "pixel_type": "float16",
                    "channels": 4,
                },
                "frames": mask_validation_frames,
                "stencil_coverage": {
                    "observed_ids": [1],
                    "missing_ids": [],
                    "coverage_ratio": 1.0,
                },
            },
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return run_dir


def _mp4(*, faststart: bool = True) -> bytes:
    def box(kind: bytes) -> bytes:
        return (8).to_bytes(4, "big") + kind

    ordered = (b"ftyp", b"moov", b"mdat") if faststart else (b"ftyp", b"mdat", b"moov")
    return b"".join(box(kind) for kind in ordered)


def _install_fake_tools(
    monkeypatch: Any,
    *,
    calls: list[list[str]],
    ffmpeg_returncode: int = 0,
    probe_frames: int = 72,
    probe_resolution: tuple[int, int] = (1440, 1440),
    faststart: bool = True,
) -> None:
    monkeypatch.setattr(
        "uefactory.render.showcase._required_executable", lambda name: f"/fake/{name}"
    )

    def fake_run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        command = list(command)
        calls.append(command)
        if command[0] == "/fake/ffmpeg":
            if ffmpeg_returncode == 0:
                Path(command[-1]).write_bytes(_mp4(faststart=faststart))
            return subprocess.CompletedProcess(
                command,
                ffmpeg_returncode,
                stdout="",
                stderr="encode failed" if ffmpeg_returncode else "",
            )
        if command[0] == "/fake/ffprobe":
            video_path = Path(command[-1])
            payload = {
                "streams": [
                    {
                        "codec_name": "h264",
                        "codec_type": "video",
                        "width": probe_resolution[0],
                        "height": probe_resolution[1],
                        "pix_fmt": "yuv420p",
                        "r_frame_rate": "24/1",
                        "avg_frame_rate": "24/1",
                        "nb_frames": str(probe_frames),
                        "nb_read_frames": str(probe_frames),
                    }
                ],
                "format": {
                    "duration": f"{probe_frames / 24:.6f}",
                    "size": str(video_path.stat().st_size),
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                },
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[:5] == ["git", "-C", command[2], "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{FIXED_COMMIT}\n", stderr="")
        if command[0] == "git" and "status" in command:
            return subprocess.CompletedProcess(command, 0, stdout=" M PLAN.md\n", stderr="")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("uefactory.render.showcase._run", fake_run)


def _relax_showcase_policy(
    monkeypatch: Any,
    *,
    minimum_frames: int = 3,
    minimum_short_edge: int = 1,
) -> None:
    monkeypatch.setattr("uefactory.render.showcase.MIN_SHOWCASE_FRAMES", minimum_frames)
    monkeypatch.setattr("uefactory.render.showcase.MIN_SHOWCASE_SHORT_EDGE", minimum_short_edge)


def test_create_showcase_publishes_verified_24fps_video_and_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    run_dir = _render_run(tmp_path)
    calls: list[list[str]] = []
    _install_fake_tools(monkeypatch, calls=calls)

    result = create_showcase(
        project_root=tmp_path,
        render_run_dir=run_dir,
        stage="m3_scene_assets",
        clock=lambda: FIXED_NOW,
    )

    assert result.run_dir == (
        tmp_path / "out/showcases/m3_scene_assets/20260711T120000Z_scene_bm_player_home"
    )
    assert result.video_path.read_bytes() == _mp4()
    assert result.frame_count == 72
    assert result.resolution == (1440, 1440)
    assert result.duration_sec == 3.0
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["stage"] == "m3_scene_assets"
    assert manifest["source"] == {
        "run_dir": run_dir.relative_to(tmp_path).as_posix(),
        "manifest_path": run_dir.joinpath("manifest.json").relative_to(tmp_path).as_posix(),
        "archived_manifest_path": "source_render_manifest.json",
        "manifest_sha256": hashlib.sha256(
            run_dir.joinpath("manifest.json").read_bytes()
        ).hexdigest(),
        "manifest_size": run_dir.joinpath("manifest.json").stat().st_size,
        "render_schema_version": 3,
        "render_kind": "job",
        "render_status": "ok",
    }
    assert manifest["asset"]["asset_id"] == "scene:bm_player_home"
    assert manifest["asset"]["source_sha256"] == "b" * 64
    assert manifest["license"] == {
        "name": "CC-BY-4.0",
        "tier": "open",
        "url": "https://creativecommons.org/licenses/by/4.0/",
    }
    assert manifest["attribution"] == "Fixture Artist - Player Home"
    assert manifest["rights_provenance"] == "render_manifest"
    assert manifest["frames"]["count"] == 72
    assert manifest["frames"]["resolution"] == [1440, 1440]
    assert len(manifest["frames"]["items"]) == 72
    assert len(manifest["frames"]["aggregate_sha256"]) == 64
    assert manifest["frames"]["object_mask"]["count"] == 72
    assert manifest["frames"]["object_mask"]["policy"] == {
        "minimum_foreground_area_ratio": 0.1,
        "minimum_bbox_area_ratio": 0.18,
        "minimum_margin_ratio": 0.03,
        "background_stencil_atol": 0.0004,
    }
    framing = manifest["frames"]["object_mask"]["summary"]
    assert framing["foreground_area_ratio"]["minimum"] == pytest.approx(0.36, abs=0.002)
    assert framing["bbox_area_ratio"]["minimum"] == pytest.approx(0.36, abs=0.002)
    assert manifest["video"] == {
        "container": "mp4",
        "codec": "h264",
        "pixel_format": "yuv420p",
        "width": 1440,
        "height": 1440,
        "fps": 24,
        "frame_count": 72,
        "duration_sec": 3.0,
        "size": 24,
        "faststart": True,
        "path": "showcase.mp4",
        "sha256": hashlib.sha256(_mp4()).hexdigest(),
        "encoding": {
            "codec": "libx264",
            "crf": 16,
            "preset": "slow",
            "pixel_format": "yuv420p",
            "movflags": "+faststart",
            "audio": False,
            "command_sha256": manifest["video"]["encoding"]["command_sha256"],
        },
    }
    assert manifest["git_commit"] == FIXED_COMMIT
    assert manifest["git_dirty"] is True
    assert (
        result.run_dir.joinpath("source_render_manifest.json").read_bytes()
        == (run_dir / "manifest.json").read_bytes()
    )
    ffmpeg_command = next(command for command in calls if command[0] == "/fake/ffmpeg")
    assert ffmpeg_command[ffmpeg_command.index("-framerate") + 1] == "24"
    assert ffmpeg_command[ffmpeg_command.index("-frames:v") + 1] == "72"
    assert ffmpeg_command[ffmpeg_command.index("-crf") + 1] == "16"
    assert ffmpeg_command[ffmpeg_command.index("-pix_fmt") + 1] == "yuv420p"
    assert ffmpeg_command[ffmpeg_command.index("-movflags") + 1] == "+faststart"
    assert not list(result.run_dir.parent.glob("*.tmp"))


def test_create_showcase_rejects_failed_render_before_tools(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    run_dir = _render_run(tmp_path, frame_count=1, resolution=(16, 16), status="failed")
    monkeypatch.setattr(
        "uefactory.render.showcase._required_executable",
        lambda _name: (_ for _ in ()).throw(AssertionError("tools must not run")),
    )

    with pytest.raises(ShowcaseError, match="status must be 'ok'"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )

    assert not (tmp_path / "out/showcases").exists()


def test_create_showcase_rejects_noncontinuous_frame_manifest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(tmp_path, frame_count=3, resolution=(16, 16))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["frame_paths"]["beauty_lit"][1] = "beauty_lit/frame_0002.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ShowcaseError, match="continuous canonical sequence"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_symlink_frame(monkeypatch: Any, tmp_path: Path) -> None:
    _relax_showcase_policy(monkeypatch, minimum_frames=2)
    run_dir = _render_run(tmp_path, frame_count=2, resolution=(16, 16))
    frame = run_dir / "beauty_lit/frame_0000.png"
    target = run_dir / "beauty_lit/frame_0001.png"
    frame.unlink()
    frame.symlink_to(target.name)

    with pytest.raises(ShowcaseError, match="Symlink paths are not allowed"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_short_edge_below_1080(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch, minimum_frames=1, minimum_short_edge=1080)
    run_dir = _render_run(tmp_path, frame_count=1, resolution=(1080, 1078))

    with pytest.raises(ShowcaseError, match="short edge must be at least 1080"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_changed_decoded_frame(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(tmp_path, frame_count=3, resolution=(64, 64))
    Image.new("RGB", (64, 64), (255, 0, 0)).save(run_dir / "beauty_lit/frame_0001.png")

    with pytest.raises(ShowcaseError, match="decoded pixel hash mismatch"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_small_object_mask_subject(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(
        tmp_path,
        frame_count=3,
        resolution=(128, 128),
        foreground_span=0.22,
    )

    with pytest.raises(ShowcaseError, match="subject is too small"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )

    assert not (tmp_path / "out/showcases").exists()


def test_create_showcase_rejects_small_object_mask_bbox(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(
        tmp_path,
        frame_count=3,
        resolution=(128, 128),
        foreground_span=0.4,
    )

    with pytest.raises(ShowcaseError, match="subject bbox is too small"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_object_mask_crop_margin(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(
        tmp_path,
        frame_count=3,
        resolution=(128, 128),
        foreground_span=0.96,
    )

    with pytest.raises(ShowcaseError, match="violates frame margin"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
        )


def test_create_showcase_rejects_source_frame_changed_during_encoding(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(tmp_path, frame_count=3, resolution=(64, 64))
    calls: list[list[str]] = []
    _install_fake_tools(
        monkeypatch,
        calls=calls,
        probe_frames=3,
        probe_resolution=(64, 64),
    )

    def mutating_encode(**kwargs: Any) -> list[str]:
        frames = kwargs["frames"]
        output_path = kwargs["output_path"]
        output_path.write_bytes(_mp4())
        Image.new("RGB", (64, 64), (255, 0, 0)).save(frames.paths[0])
        return ["/fake/ffmpeg"]

    monkeypatch.setattr("uefactory.render.showcase._encode_showcase", mutating_encode)

    with pytest.raises(ShowcaseError, match="source frame changed during encoding"):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
            clock=lambda: FIXED_NOW,
        )

    stage_root = tmp_path / "out/showcases/m3_scene_assets"
    assert not (stage_root / "20260711T120000Z_scene_bm_player_home").exists()
    assert not list(stage_root.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("ffmpeg_returncode", "probe_frames", "faststart", "message"),
    [
        (9, 3, True, "ffmpeg failed"),
        (0, 2, True, "frame count mismatch"),
        (0, 3, False, "does not prove \\+faststart"),
    ],
)
def test_create_showcase_tool_failure_cleans_staging_and_never_publishes(
    monkeypatch: Any,
    tmp_path: Path,
    ffmpeg_returncode: int,
    probe_frames: int,
    faststart: bool,
    message: str,
) -> None:
    _relax_showcase_policy(monkeypatch)
    run_dir = _render_run(tmp_path, frame_count=3, resolution=(64, 64))
    calls: list[list[str]] = []
    _install_fake_tools(
        monkeypatch,
        calls=calls,
        ffmpeg_returncode=ffmpeg_returncode,
        probe_frames=probe_frames,
        probe_resolution=(64, 64),
        faststart=faststart,
    )

    with pytest.raises(ShowcaseError, match=message):
        create_showcase(
            project_root=tmp_path,
            render_run_dir=run_dir,
            stage="m3_scene_assets",
            clock=lambda: FIXED_NOW,
        )

    stage_root = tmp_path / "out/showcases/m3_scene_assets"
    assert not (stage_root / "20260711T120000Z_scene_bm_player_home").exists()
    assert not list(stage_root.glob(".*.tmp"))


def test_publish_rolls_back_if_parent_fsync_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    stage_root = tmp_path / "out/showcases/m3_scene_assets"
    staging_dir = stage_root / ".candidate.tmp"
    final_dir = stage_root / "20260711T120000Z_scene_bm_player_home"
    staging_dir.mkdir(parents=True)
    (staging_dir / "showcase.mp4").write_bytes(_mp4())
    calls = 0

    def fail_first_fsync(_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ShowcaseError("injected parent fsync failure")

    monkeypatch.setattr("uefactory.render.showcase._fsync_directory", fail_first_fsync)

    with pytest.raises(ShowcaseError, match="injected parent fsync failure"):
        _publish_directory(staging_dir=staging_dir, final_dir=final_dir)

    assert not final_dir.exists()
    assert staging_dir.joinpath("showcase.mp4").read_bytes() == _mp4()
    assert calls == 2


def test_showcase_cli_forwards_stage_and_prints_archive(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source_run = tmp_path / "out/showcase_source_renders/run/scene_bm_player_home"
    source_run.mkdir(parents=True)
    output_run = tmp_path / "out/showcases/m3_scene_assets/result"
    calls: list[dict[str, Any]] = []

    def fake_create_showcase(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            video_path=output_run / "showcase.mp4",
            manifest_path=output_run / "manifest.json",
            frame_count=72,
            resolution=(1440, 1440),
        )

    monkeypatch.setattr("uefactory.cli.render.create_showcase", fake_create_showcase)
    result = CliRunner().invoke(
        _app(tmp_path),
        ["render", "showcase", str(source_run), "--stage", "m3_scene_assets"],
    )

    assert result.exit_code == 0, result.output
    assert "Frames: 72 at 24 fps" in result.stdout
    assert "Resolution: 1440x1440" in result.stdout
    assert str(output_run / "showcase.mp4") in result.stdout
    assert calls == [
        {
            "project_root": tmp_path,
            "render_run_dir": source_run,
            "stage": "m3_scene_assets",
        }
    ]


def test_showcase_cli_reports_fail_closed_error(monkeypatch: Any, tmp_path: Path) -> None:
    source_run = tmp_path / "out/showcase_source_renders/run/scene_bm_player_home"
    source_run.mkdir(parents=True)
    monkeypatch.setattr(
        "uefactory.cli.render.create_showcase",
        lambda **_kwargs: (_ for _ in ()).throw(ShowcaseError("render manifest failed")),
    )

    result = CliRunner().invoke(
        _app(tmp_path),
        ["render", "showcase", str(source_run), "--stage", "m3_scene_assets"],
    )

    assert result.exit_code == 2
    assert "Showcase failed: render manifest failed" in result.stderr
