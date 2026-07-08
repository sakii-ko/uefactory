from __future__ import annotations

import json
import os
import time
from pathlib import Path

import unreal

_STATE = {"started_at": 0.0, "job": {}}
_PIPELINE_QUEUE = None


@unreal.uclass()
class UEFRenderJobRuntimeExecutor(unreal.MoviePipelinePythonHostExecutor):
    active_movie_pipeline = unreal.uproperty(unreal.MoviePipeline)

    def _post_init(self):
        self.active_movie_pipeline = None

    @unreal.ufunction(override=True)
    def execute_delayed(self, in_pipeline_queue):
        global _PIPELINE_QUEUE
        del in_pipeline_queue
        _STATE["started_at"] = time.monotonic()
        _STATE["job"] = _load_job()
        job = _STATE["job"]
        out_dir = Path(job["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)

        camera = job["camera"]
        width, height = camera["resolution"]
        frames = int(job["frames"])
        sequence_path = str(job["sequence_path"])
        unreal.log(
            f"[UEF-RENDER-JOB-RUNTIME] render start out={out_dir} "
            f"frames={frames} size={width}x{height} sequence={sequence_path}"
        )

        _PIPELINE_QUEUE = unreal.new_object(unreal.MoviePipelineQueue, outer=self)
        pipeline_job = _PIPELINE_QUEUE.allocate_new_job(unreal.MoviePipelineExecutorJob)
        pipeline_job.sequence = unreal.SoftObjectPath(sequence_path)
        pipeline_job.author = "UEFactory render job"

        output_setting = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineOutputSetting
        )
        output_setting.output_directory = unreal.DirectoryPath(str(out_dir))
        output_setting.output_resolution = unreal.IntPoint(int(width), int(height))
        output_setting.file_name_format = "frame_{frame_number}"
        output_setting.zero_pad_frame_numbers = 4
        output_setting.output_frame_step = 1
        output_setting.use_custom_playback_range = True
        output_setting.custom_start_frame = 0
        output_setting.custom_end_frame = frames
        output_setting.flush_disk_writes_per_shot = True

        render_pass = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineDeferredPassBase
        )
        render_pass.disable_multisample_effects = True
        color_setting = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineColorSetting
        )
        color_setting.set_editor_property("disable_tone_curve", True)
        _configure_identity_ocio(color_setting)
        anti_aliasing = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineAntiAliasingSetting
        )
        anti_aliasing.set_editor_property("spatial_sample_count", 1)
        anti_aliasing.set_editor_property("temporal_sample_count", 1)
        anti_aliasing.set_editor_property("override_anti_aliasing", True)
        anti_aliasing.set_editor_property(
            "anti_aliasing_method", unreal.AntiAliasingMethod.AAM_NONE
        )
        anti_aliasing.set_editor_property("render_warm_up_count", 0)
        anti_aliasing.set_editor_property("engine_warm_up_count", 0)
        anti_aliasing.set_editor_property("render_warm_up_frames", False)
        _add_determinism_cvars(pipeline_job)
        pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineImageSequenceOutput_PNG
        )
        pipeline_job.get_configuration().initialize_transient_settings()

        self.active_movie_pipeline = unreal.new_object(
            self.target_pipeline_class,
            outer=self.get_last_loaded_world(),
            base_type=unreal.MoviePipeline,
        )
        self.active_movie_pipeline.on_movie_pipeline_work_finished_delegate.add_function_unique(
            self,
            "on_movie_pipeline_finished",
        )
        self.active_movie_pipeline.initialize(pipeline_job)

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
        success = bool(results.success)
        job = _STATE["job"]
        out_dir = Path(job["out_dir"])
        _write_manifest(
            out_dir,
            job,
            time.monotonic() - float(_STATE["started_at"]),
            success,
        )
        unreal.log(f"[UEF-RENDER-JOB-RUNTIME] render finished success={success}")
        global _PIPELINE_QUEUE
        self.active_movie_pipeline = None
        _PIPELINE_QUEUE = None
        self.on_executor_finished_impl()


def _load_job() -> dict:
    job_file = os.environ.get("UEF_JOB_FILE")
    if not job_file:
        raise RuntimeError("UEF_JOB_FILE is not set")
    with Path(job_file).open("r", encoding="utf-8") as file:
        return json.load(file)


def _configure_identity_ocio(color_setting) -> None:
    config = unreal.new_object(unreal.OpenColorIOConfiguration, outer=color_setting)
    config_path = unreal.FilePath()
    config_path.set_editor_property(
        "file_path",
        "{Engine}/Plugins/Compositing/OpenColorIO/Content/OCIO/simple.config.ocio",
    )
    config.set_editor_property("configuration_file", config_path)
    config.reload_existing_colorspaces(True)

    source = _ocio_color_space("Utility - Linear - sRGB", 2, "Utility")
    destination = _ocio_color_space("Utility - Linear - sRGB", 2, "Utility")
    color_configuration = color_setting.ocio_configuration.color_configuration
    color_configuration.set_editor_property("configuration_source", config)
    color_configuration.set_editor_property("source_color_space", source)
    color_configuration.set_editor_property("destination_color_space", destination)
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
    ]:
        cvars.add_or_update_console_variable(name, float(value))


def _write_manifest(out_dir: Path, job: dict, duration_sec: float, success: bool) -> None:
    frame_paths = sorted(path for path in out_dir.glob("*.png"))
    payload = {
        "schema_version": 2,
        "status": "ok" if success and len(frame_paths) == int(job["frames"]) else "failed",
        "render_kind": "job",
        "asset_id": job["asset_id"],
        "pass": job["passes"][0],
        "job_id": job["run_id"],
        "job": job["job"],
        "camera": job["camera"],
        "lighting": job["lighting"],
        "frames_expected": int(job["frames"]),
        "frames_found": len(frame_paths),
        "duration_sec": round(duration_sec, 3),
        "frame_paths": [str(path) for path in frame_paths],
        "executor": "UEFRenderJobRuntimeExecutor",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    unreal.log(f"[UEF-RENDER-JOB-RUNTIME] manifest={out_dir / 'manifest.json'}")
