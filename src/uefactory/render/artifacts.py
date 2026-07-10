from __future__ import annotations

import html
import math
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class RenderArtifacts:
    contact_sheet: Path
    index_html: Path
    turntable_mp4: Path | None

    def manifest_payload(self, *, run_dir: Path) -> dict[str, str | None]:
        return {
            "contact_sheet": _relative_run_path(self.contact_sheet, run_dir),
            "index_html": _relative_run_path(self.index_html, run_dir),
            "turntable_mp4": (
                _relative_run_path(self.turntable_mp4, run_dir)
                if self.turntable_mp4 is not None
                else None
            ),
        }


def create_render_artifacts(
    *,
    run_dir: Path,
    frame_paths: dict[str, list[Path]],
    manifest_path: Path,
) -> RenderArtifacts:
    contact_sheet = run_dir / "contact_sheet.png"
    index_html = run_dir / "index.html"
    beauty_frames = frame_paths.get("beauty_lit")
    turntable_mp4: Path | None = None
    create_contact_sheet(frame_paths=frame_paths, output_path=contact_sheet)
    if beauty_frames:
        turntable_mp4 = run_dir / "turntable.mp4"
        create_turntable(frame_paths=beauty_frames, output_path=turntable_mp4)
    create_index_html(
        run_dir=run_dir,
        manifest_path=manifest_path,
        frame_paths=frame_paths,
        contact_sheet=contact_sheet,
        turntable_mp4=turntable_mp4,
        output_path=index_html,
    )
    return RenderArtifacts(
        contact_sheet=contact_sheet,
        index_html=index_html,
        turntable_mp4=turntable_mp4,
    )


def create_contact_sheet(
    *,
    frame_paths: dict[str, list[Path]],
    output_path: Path,
    thumb_width: int = 160,
) -> None:
    passes = list(frame_paths)
    if not passes:
        raise ValueError("Cannot build contact sheet without passes")
    frame_count = max(len(paths) for paths in frame_paths.values())
    if frame_count == 0:
        raise ValueError("Cannot build contact sheet without frames")

    previews: dict[str, list[Image.Image]] = {
        pass_name: [_frame_preview(pass_name, path) for path in paths]
        for pass_name, paths in frame_paths.items()
    }
    first = next(image for images in previews.values() for image in images)
    aspect = first.height / first.width
    thumb_height = max(1, int(round(thumb_width * aspect)))
    label_width = 132
    header_height = 26
    cell_padding = 6
    cell_width = thumb_width + cell_padding * 2
    cell_height = thumb_height + cell_padding * 2
    sheet = Image.new(
        "RGB",
        (label_width + frame_count * cell_width, header_height + len(passes) * cell_height),
        (18, 20, 24),
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for frame_index in range(frame_count):
        draw.text(
            (label_width + frame_index * cell_width + cell_padding, 7),
            f"view {frame_index:02d}",
            fill=(220, 224, 230),
            font=font,
        )
    for row_index, pass_name in enumerate(passes):
        y = header_height + row_index * cell_height
        draw.text((10, y + cell_padding + 4), pass_name, fill=(220, 224, 230), font=font)
        for frame_index, image in enumerate(previews[pass_name]):
            resized = image.resize((thumb_width, thumb_height), Image.Resampling.BILINEAR)
            x = label_width + frame_index * cell_width + cell_padding
            sheet.paste(resized, (x, y + cell_padding))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def create_turntable(
    *,
    frame_paths: list[Path],
    output_path: Path,
    framerate: int = 12,
    duration_sec: float = 4.0,
) -> None:
    if not frame_paths:
        raise ValueError("Cannot build turntable without frames")
    if framerate <= 0:
        raise ValueError("Turntable framerate must be positive")
    if duration_sec <= 0:
        raise ValueError("Turntable duration must be positive")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg not found; run `uef doctor` and install ffmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = max(4.0, duration_sec)
    input_framerate = len(frame_paths) / duration_sec
    output_frame_count = math.ceil(duration_sec * framerate)
    pattern = frame_paths[0].parent / "frame_%04d.png"
    command = [
        ffmpeg,
        "-y",
        "-stream_loop",
        "-1",
        "-framerate",
        f"{input_framerate:.6g}",
        "-i",
        str(pattern),
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r",
        str(framerate),
        "-frames:v",
        str(output_frame_count),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _output_tail(exc.stderr)
        raise RuntimeError(
            f"ffmpeg timed out while creating turntable {output_path} after {exc.timeout}s; "
            f"command: {shlex.join(command)}; stderr: {stderr or '<empty>'}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"ffmpeg could not create turntable {output_path}: {exc}; "
            f"command: {shlex.join(command)}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed while creating turntable {output_path} with exit code "
            f"{result.returncode}; command: {shlex.join(command)}; "
            f"stdout: {_output_tail(result.stdout) or '<empty>'}; "
            f"stderr: {_output_tail(result.stderr) or '<empty>'}"
        )
    if not output_path.is_file():
        raise RuntimeError(
            f"ffmpeg reported success but did not create turntable {output_path}; "
            f"command: {shlex.join(command)}"
        )


def create_index_html(
    *,
    run_dir: Path,
    manifest_path: Path,
    frame_paths: dict[str, list[Path]],
    contact_sheet: Path,
    turntable_mp4: Path | None,
    output_path: Path,
) -> None:
    rows = []
    for pass_name, paths in frame_paths.items():
        links = " ".join(
            f'<a href="{html.escape(_relative_run_path(path, run_dir))}">{index:02d}</a>'
            for index, path in enumerate(paths)
        )
        rows.append(f"<tr><th>{html.escape(pass_name)}</th><td>{links}</td></tr>")
    if turntable_mp4 is None:
        turntable_html = (
            '<p class="turntable-skipped">Turntable skipped: beauty_lit not rendered.</p>'
        )
    else:
        turntable_html = (
            '<p><video controls loop src="'
            f"{html.escape(_relative_run_path(turntable_mp4, run_dir))}"
            '"></video></p>'
        )
    output_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="en">',
                "<head>",
                '<meta charset="utf-8">',
                "<title>UEFactory Render Job</title>",
                "<style>",
                "body{font-family:sans-serif;margin:24px;background:#111;color:#eee}",
                "a{color:#8cc8ff} img{max-width:100%;height:auto;border:1px solid #444}",
                "table{border-collapse:collapse}"
                "th,td{padding:6px 10px;border-bottom:1px solid #333}",
                "</style>",
                "</head>",
                "<body>",
                "<h1>UEFactory Render Job</h1>",
                f'<p><a href="{html.escape(_relative_run_path(manifest_path, run_dir))}">'
                "manifest.json</a></p>",
                turntable_html,
                f'<p><img src="{html.escape(_relative_run_path(contact_sheet, run_dir))}" '
                'alt="contact sheet"></p>',
                "<table>",
                *rows,
                "</table>",
                "</body>",
                "</html>",
            ]
        ),
        encoding="utf-8",
    )


def _frame_preview(pass_name: str, frame_path: Path) -> Image.Image:
    if frame_path.suffix.lower() == ".png":
        with Image.open(frame_path) as image:
            image.load()
            return image.convert("RGB")
    return _exr_preview(pass_name, frame_path)


def _exr_preview(pass_name: str, frame_path: Path) -> Image.Image:
    OpenEXR = import_module("OpenEXR")
    np = import_module("numpy")
    file = OpenEXR.File(str(frame_path), separate_channels=False)
    pixels = next(iter(file.channels().values())).pixels
    values = np.asarray(pixels, dtype=np.float32)
    if values.ndim == 3:
        values = values[:, :, 0]
    values = values.copy()
    values[~np.isfinite(values)] = 0.0
    if pass_name == "object_mask":
        scaled = np.clip(values / (2.0 / 255.0), 0.0, 1.0) * 255.0
    else:
        min_value = float(np.min(values))
        max_value = float(np.max(values))
        if max_value <= min_value:
            scaled = np.zeros_like(values)
        else:
            scaled = (values - min_value) / (max_value - min_value) * 255.0
    return Image.fromarray(scaled.astype("uint8"), mode="L").convert("RGB")


def _relative_run_path(path: Path, run_dir: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"Artifact path must be inside run directory {run_dir}: {path}") from None


def _output_tail(output: str | bytes | None, limit: int = 1000) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return output[-limit:].strip()
