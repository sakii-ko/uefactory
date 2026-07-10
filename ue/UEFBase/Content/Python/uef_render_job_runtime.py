from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from pathlib import Path

import unreal

_STATE = {"started_at": 0.0, "job": {}, "job_index": 0, "success": True}
_PIPELINE_QUEUE = None
_WORLD_DEPTH_MATERIAL = (
    "/MovieRenderPipeline/Materials/MovieRenderQueue_WorldDepth.MovieRenderQueue_WorldDepth"
)

_PASS_OUTPUTS = {
    "beauty_lit": {
        "kind": "deferred",
        "render_pass": "FinalImage",
        "extension": "png",
        "output": unreal.MoviePipelineImageSequenceOutput_PNG,
    },
    "beauty_unlit": {
        "kind": "unlit",
        "render_pass": "Unlit",
        "extension": "png",
        "output": unreal.MoviePipelineImageSequenceOutput_PNG,
    },
    "depth": {
        "kind": "material",
        "render_pass": "FinalImageMovieRenderQueue_WorldDepth",
        "extension": "exr",
        "output": unreal.MoviePipelineImageSequenceOutput_EXR,
        "material": _WORLD_DEPTH_MATERIAL,
        "high_precision": False,
    },
    "normal": {
        "kind": "material",
        "render_pass": "FinalImageUEF_Normal",
        "extension": "png",
        "output": unreal.MoviePipelineImageSequenceOutput_PNG,
        "material": "/Engine/BufferVisualization/WorldNormal.WorldNormal",
        "high_precision": False,
    },
    "basecolor": {
        "kind": "material",
        "render_pass": "FinalImageUEF_BaseColor",
        "extension": "png",
        "output": unreal.MoviePipelineImageSequenceOutput_PNG,
        "material": "/Engine/BufferVisualization/BaseColor.BaseColor",
        "high_precision": False,
    },
    "object_mask": {
        "kind": "material",
        "render_pass": "FinalImageUEF_ObjectMask",
        "extension": "exr",
        "output": unreal.MoviePipelineImageSequenceOutput_EXR,
        "material": "{job_package}/UEF_ObjectMask_Mat.UEF_ObjectMask_Mat",
        "high_precision": False,
    },
}


@unreal.uclass()
class UEFRenderJobRuntimeExecutor(unreal.MoviePipelinePythonHostExecutor):
    active_movie_pipeline = unreal.uproperty(unreal.MoviePipeline)

    def _post_init(self):
        self.active_movie_pipeline = None

    @unreal.ufunction(override=True)
    def execute_delayed(self, in_pipeline_queue):
        global _PIPELINE_QUEUE
        del in_pipeline_queue
        try:
            _STATE["started_at"] = time.monotonic()
            _STATE["job"] = _load_job()
            _STATE["job_index"] = 0
            _STATE["success"] = True
            job = _STATE["job"]
            out_dir = Path(job["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)

            camera = job["camera"]
            width, height = camera["resolution"]
            frames = int(job["frames"])
            sequence_path = str(job["sequence_path"])
            passes = list(job["passes"])
            unreal.log(
                f"[UEF-RENDER-JOB-RUNTIME] render start out={out_dir} "
                f"passes={passes} frames={frames} size={width}x{height} "
                f"sequence={sequence_path}"
            )

            _PIPELINE_QUEUE = unreal.new_object(unreal.MoviePipelineQueue, outer=self)
            for pass_name in passes:
                _configure_pipeline_job(_PIPELINE_QUEUE, job, pass_name)

            _start_next_pipeline(self)
        except Exception as exc:
            _finish_executor_failure(self, context="initialization", error=exc)

    @unreal.ufunction(override=True)
    def on_begin_frame(self):
        super().on_begin_frame()

    @unreal.ufunction(override=True)
    def on_map_load(self, in_world):
        del in_world

    @unreal.ufunction(override=True)
    def is_rendering(self):
        return self.active_movie_pipeline is not None

    @unreal.ufunction(ret=None, params=[unreal.MoviePipelineOutputData])
    def on_movie_pipeline_finished(self, results):
        _STATE["success"] = bool(_STATE["success"]) and bool(results.success)
        job = _STATE["job"]
        self.active_movie_pipeline = None
        _STATE["job_index"] = int(_STATE["job_index"]) + 1
        try:
            if _start_next_pipeline(self):
                return
        except Exception as exc:
            _finish_executor_failure(self, context="starting next pass", error=exc)
            return

        out_dir = Path(job["out_dir"])
        error: str | None = None
        try:
            _normalize_outputs(job)
        except Exception as exc:
            _STATE["success"] = False
            error = f"Output normalization failed: {type(exc).__name__}: {exc}"
            unreal.log_error(f"[UEF-RENDER-JOB-RUNTIME] {error}\n{traceback.format_exc()}")
        if error is None and not bool(_STATE["success"]):
            error = "Movie Render Queue reported unsuccessful output"
        try:
            _write_manifest(
                out_dir,
                job,
                time.monotonic() - float(_STATE["started_at"]),
                bool(_STATE["success"]),
                error=error,
            )
        except Exception as exc:
            unreal.log_error(
                "[UEF-RENDER-JOB-RUNTIME] Failed to write final manifest: "
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
        finally:
            unreal.log(
                f"[UEF-RENDER-JOB-RUNTIME] render finished success={bool(_STATE['success'])}"
            )
            global _PIPELINE_QUEUE
            _PIPELINE_QUEUE = None
            self.on_executor_finished_impl()


def _finish_executor_failure(executor, *, context: str, error: Exception) -> None:
    global _PIPELINE_QUEUE
    _STATE["success"] = False
    message = f"Runtime {context} failed: {type(error).__name__}: {error}"
    unreal.log_error(f"[UEF-RENDER-JOB-RUNTIME] {message}\n{traceback.format_exc()}")
    job = _STATE.get("job")
    if isinstance(job, dict) and job.get("out_dir"):
        try:
            _write_manifest(
                Path(job["out_dir"]),
                job,
                time.monotonic() - float(_STATE["started_at"]),
                False,
                error=message,
            )
        except Exception as manifest_error:
            unreal.log_error(
                "[UEF-RENDER-JOB-RUNTIME] Failed to write failure manifest: "
                f"{type(manifest_error).__name__}: {manifest_error}"
            )
    executor.active_movie_pipeline = None
    _PIPELINE_QUEUE = None
    executor.on_executor_finished_impl()


def _start_next_pipeline(executor) -> bool:
    jobs = _PIPELINE_QUEUE.get_jobs()
    job_index = int(_STATE["job_index"])
    if job_index >= len(jobs):
        return False
    world = executor.get_last_loaded_world()
    unreal.log(f"[UEF-RENDER-JOB-RUNTIME] starting MRQ subjob {job_index + 1}/{len(jobs)}")
    executor.active_movie_pipeline = unreal.new_object(
        executor.target_pipeline_class,
        outer=world,
        base_type=unreal.MoviePipeline,
    )
    executor.active_movie_pipeline.on_movie_pipeline_work_finished_delegate.add_function_unique(
        executor,
        "on_movie_pipeline_finished",
    )
    executor.active_movie_pipeline.initialize(jobs[job_index])
    return True


def _configure_pipeline_job(queue, job: dict, pass_name: str):
    if pass_name not in _PASS_OUTPUTS:
        raise RuntimeError(f"Unsupported render pass: {pass_name}")
    pass_config = _PASS_OUTPUTS[pass_name]
    out_dir = Path(job["out_dir"])
    raw_dir = out_dir / "_mrq" / pass_name
    raw_dir.mkdir(parents=True, exist_ok=True)
    camera = job["camera"]
    width, height = camera["resolution"]
    frames = int(job["frames"])

    pipeline_job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    sequence_path = (
        job.get("beauty_sequence_path", job["sequence_path"])
        if pass_name in {"beauty_lit", "beauty_unlit"}
        else job["sequence_path"]
    )
    pipeline_job.sequence = unreal.SoftObjectPath(str(sequence_path))
    pipeline_job.author = "UEFactory render job"
    pipeline_job.job_name = f"UEF_{job['run_id']}_{pass_name}"

    config = pipeline_job.get_configuration()
    output_setting = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    output_setting.output_directory = unreal.DirectoryPath(str(raw_dir))
    output_setting.output_resolution = unreal.IntPoint(int(width), int(height))
    output_setting.file_name_format = "{render_pass}.frame_{frame_number}"
    output_setting.zero_pad_frame_numbers = 4
    output_setting.output_frame_step = 1
    output_setting.use_custom_playback_range = True
    output_setting.custom_start_frame = 0
    output_setting.custom_end_frame = frames
    output_setting.flush_disk_writes_per_shot = True

    _configure_render_pass(config, pass_name, pass_config)
    color_setting = config.find_or_add_setting_by_class(unreal.MoviePipelineColorSetting)
    color_setting.set_editor_property("disable_tone_curve", True)
    if pass_name in {"beauty_lit", "beauty_unlit"}:
        _configure_display_ocio(color_setting)
    elif pass_name in {"normal", "basecolor"}:
        _configure_linear_ocio(color_setting)
    else:
        color_setting.ocio_configuration.set_editor_property("is_enabled", False)
    anti_aliasing = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    anti_aliasing.set_editor_property("spatial_sample_count", 1)
    anti_aliasing.set_editor_property("temporal_sample_count", 1)
    anti_aliasing.set_editor_property("override_anti_aliasing", True)
    anti_aliasing.set_editor_property("anti_aliasing_method", unreal.AntiAliasingMethod.AAM_NONE)
    anti_aliasing.set_editor_property("render_warm_up_count", 0)
    anti_aliasing.set_editor_property("engine_warm_up_count", 0)
    anti_aliasing.set_editor_property("render_warm_up_frames", False)
    _add_determinism_cvars(pipeline_job)
    output = config.find_or_add_setting_by_class(pass_config["output"])
    if pass_config["output"] == unreal.MoviePipelineImageSequenceOutput_EXR:
        output.set_editor_property("multilayer", False)
    config.initialize_transient_settings()
    return pipeline_job


def _configure_render_pass(config, pass_name: str, pass_config: dict) -> None:
    if pass_config["kind"] == "unlit":
        render_pass = config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPass_Unlit)
        render_pass.set_editor_property("disable_multisample_effects", True)
        return

    render_pass = config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    render_pass.set_editor_property("disable_multisample_effects", True)
    if pass_config["kind"] == "material":
        _enable_post_process_material(
            render_pass,
            name=_material_pass_name(pass_name),
            material_path=_resolve_material_path(pass_config["material"]),
            high_precision=bool(pass_config["high_precision"]),
        )


def _resolve_material_path(material_path: str) -> str:
    if "{job_package}" not in material_path:
        return material_path
    job = _STATE["job"]
    run_id = str(job["run_id"])
    return material_path.replace("{job_package}", f"/Game/UEF/RenderJobs/{run_id}")


def _enable_post_process_material(
    render_pass,
    *,
    name: str,
    material_path: str,
    high_precision: bool,
) -> None:
    material = unreal.load_asset(material_path)
    if material is None:
        raise RuntimeError(f"Could not load render pass material: {material_path}")
    passes = render_pass.get_editor_property("additional_post_process_materials")
    for pass_item in passes:
        pass_item.set_editor_property("enabled", False)
    item = unreal.MoviePipelinePostProcessPass()
    item.set_editor_property("enabled", True)
    item.set_editor_property("name", name)
    item.set_editor_property("material", material)
    item.set_editor_property("high_precision_output", bool(high_precision))
    passes.append(item)
    render_pass.set_editor_property("additional_post_process_materials", passes)


def _material_pass_name(pass_name: str) -> str:
    return {
        "depth": "UEF_Depth",
        "normal": "UEF_Normal",
        "basecolor": "UEF_BaseColor",
        "object_mask": "UEF_ObjectMask",
    }[pass_name]


def _normalize_outputs(job: dict) -> None:
    out_dir = Path(job["out_dir"])
    for pass_name in job["passes"]:
        pass_config = _PASS_OUTPUTS[pass_name]
        raw_dir = out_dir / "_mrq" / pass_name
        final_dir = out_dir / pass_name
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        expected_prefix = pass_config["render_pass"]
        extension = pass_config["extension"]
        raw_frames = sorted(raw_dir.glob(f"{expected_prefix}.frame_*.{extension}"))
        if len(raw_frames) != int(job["frames"]):
            raise RuntimeError(
                f"{pass_name}: expected {job['frames']} raw frames in {raw_dir}, "
                f"found {len(raw_frames)}"
            )
        for index, raw_frame in enumerate(raw_frames):
            shutil.copy2(raw_frame, final_dir / f"frame_{index:04d}.{extension}")
    shutil.rmtree(out_dir / "_mrq")


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _configure_display_ocio(color_setting) -> None:
    _configure_ocio_transform(
        color_setting,
        destination_name="Output - sRGB Monitor - UE Emulation",
        destination_index=7,
        destination_family="Output",
    )


def _configure_linear_ocio(color_setting) -> None:
    _configure_ocio_transform(
        color_setting,
        destination_name="Utility - Reference",
        destination_index=0,
        destination_family="Utility",
    )


def _configure_ocio_transform(
    color_setting,
    *,
    destination_name: str,
    destination_index: int,
    destination_family: str,
) -> None:
    config = unreal.new_object(unreal.OpenColorIOConfiguration, outer=color_setting)
    config_path = unreal.FilePath()
    config_path.set_editor_property(
        "file_path",
        "{Engine}/Plugins/Compositing/OpenColorIO/Content/OCIO/simple.config.ocio",
    )
    config.set_editor_property("configuration_file", config_path)

    source_name = "Utility - Linear - sRGB"
    source = _ocio_color_space(source_name, 2, "Utility")
    destination = _ocio_color_space(
        destination_name,
        destination_index,
        destination_family,
    )
    # UE creates transforms only between distinct entries in DesiredColorSpaces.
    # A same-space "identity" leaves the conversion invalid and burns a yellow
    # OCIO INVALID diagnostic into every output frame. Valid display/data transforms
    # also bypass UE's regular sRGB 8-bit quantizer, which adds random dither.
    config.set_editor_property("desired_color_spaces", [source, destination])
    config.reload_existing_colorspaces(True)

    desired = {
        item.get_editor_property("color_space_name"): item
        for item in config.get_editor_property("desired_color_spaces")
    }
    missing = {source_name, destination_name} - desired.keys()
    if missing:
        raise RuntimeError(f"OCIO config is missing required color spaces: {sorted(missing)}")

    color_configuration = color_setting.ocio_configuration.color_configuration
    color_configuration.set_editor_property("configuration_source", config)
    color_configuration.set_editor_property("source_color_space", desired[source_name])
    color_configuration.set_editor_property("destination_color_space", desired[destination_name])
    color_setting.ocio_configuration.set_editor_property(
        "color_configuration",
        color_configuration,
    )
    color_setting.ocio_configuration.set_editor_property("is_enabled", True)


def _ocio_color_space(name: str, index: int, family: str):
    color_space = unreal.OpenColorIOColorSpace()
    color_space.set_editor_property("color_space_name", name)
    color_space.set_editor_property("color_space_index", index)
    color_space.set_editor_property("family_name", family)
    return color_space


def _add_determinism_cvars(pipeline_job) -> None:
    cvars = pipeline_job.get_configuration().find_or_add_setting_by_class(
        unreal.MoviePipelineConsoleVariableSetting
    )
    for name, value in [
        ("r.AntiAliasingMethod", 0),
        ("r.ScreenPercentage", 100),
        ("r.MotionBlurQuality", 0),
        ("r.DefaultFeature.MotionBlur", 0),
        ("r.EyeAdaptationQuality", 0),
        ("r.DefaultFeature.AutoExposure", 0),
        ("r.BloomQuality", 0),
        ("r.DefaultFeature.Bloom", 0),
        ("r.AmbientOcclusionLevels", 0),
        ("r.DefaultFeature.AmbientOcclusion", 0),
        ("r.Tonemapper.Quality", 0),
        ("r.Tonemapper.Sharpen", 0),
        ("r.SceneColorFringeQuality", 0),
        ("r.DepthOfFieldQuality", 0),
        ("r.Lumen.DiffuseIndirect.Allow", 0),
        ("r.Lumen.Reflections.Allow", 0),
        ("r.SSGI.Quality", 0),
        ("r.SSR.Quality", 0),
        ("r.ContactShadows", 0),
        ("r.Shadow.Virtual.Enable", 0),
        ("r.ShadowQuality", 1),
        ("r.Shadow.CSM.MaxCascades", 1),
        ("r.Shadow.MaxResolution", 512),
        ("r.Shadow.MaxCSMResolution", 512),
        ("r.CustomDepth", 3),
    ]:
        cvars.add_or_update_console_variable(name, float(value))


def _write_manifest(
    out_dir: Path,
    job: dict,
    duration_sec: float,
    success: bool,
    *,
    error: str | None = None,
) -> None:
    pass_frames = {
        pass_name: sorted(str(path) for path in (out_dir / pass_name).glob("frame_*.*"))
        for pass_name in job["passes"]
    }
    payload = {
        "schema_version": 2,
        "status": (
            "ok"
            if success and all(len(paths) == int(job["frames"]) for paths in pass_frames.values())
            else "failed"
        ),
        "render_kind": "job",
        "asset_id": job["asset_id"],
        "job_id": job["run_id"],
        "job": job["job"],
        "camera": job["camera"],
        "lighting": job["lighting"],
        "requested_passes": job["passes"],
        "frames_expected": int(job["frames"]),
        "frames_found": {pass_name: len(paths) for pass_name, paths in pass_frames.items()},
        "duration_sec": round(duration_sec, 3),
        "frame_paths": pass_frames,
        "executor": "UEFRenderJobRuntimeExecutor",
    }
    if error is not None:
        payload["error"] = error
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    unreal.log(f"[UEF-RENDER-JOB-RUNTIME] manifest={out_dir / 'manifest.json'}")
