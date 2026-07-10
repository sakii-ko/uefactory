from __future__ import annotations

import json
import os
import time
from pathlib import Path

import unreal

_STATE = {"started_at": 0.0, "job": {}}
_PIPELINE_QUEUE = None


@unreal.uclass()
class UEFMRQSpikeRuntimeExecutor(unreal.MoviePipelinePythonHostExecutor):
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

        frames = int(job.get("frames", 8))
        width = int(job.get("width", 640))
        height = int(job.get("height", 360))
        sequence_path = str(
            job.get(
                "sequence_path",
                "/Game/UEF/MRQSpike/UEF_MRQ_Spike.UEF_MRQ_Spike",
            )
        )
        unreal.log(
            f"[UEF-MRQ-SPIKE-RUNTIME] render start out={out_dir} "
            f"frames={frames} size={width}x{height} sequence={sequence_path}"
        )

        _PIPELINE_QUEUE = unreal.new_object(unreal.MoviePipelineQueue, outer=self)
        pipeline_job = _PIPELINE_QUEUE.allocate_new_job(unreal.MoviePipelineExecutorJob)
        pipeline_job.sequence = unreal.SoftObjectPath(sequence_path)
        pipeline_job.author = "UEFactory T1.2 MRQ spike"

        output_setting = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineOutputSetting
        )
        output_setting.output_directory = unreal.DirectoryPath(str(out_dir))
        output_setting.output_resolution = unreal.IntPoint(width, height)
        output_setting.file_name_format = "frame_{frame_number}"
        output_setting.zero_pad_frame_numbers = 4
        output_setting.output_frame_step = 1
        output_setting.use_custom_playback_range = True
        output_setting.custom_start_frame = 0
        output_setting.custom_end_frame = frames
        output_setting.flush_disk_writes_per_shot = True

        render_pass = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineDeferredPass_Unlit
        )
        render_pass.disable_multisample_effects = True
        color_setting = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineColorSetting
        )
        color_setting.set_editor_property("disable_tone_curve", True)
        color_setting.ocio_configuration.set_editor_property("is_enabled", True)
        anti_aliasing = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineAntiAliasingSetting
        )
        anti_aliasing.set_editor_property("spatial_sample_count", 1)
        anti_aliasing.set_editor_property("temporal_sample_count", 1)
        anti_aliasing.set_editor_property("override_anti_aliasing", True)
        anti_aliasing.set_editor_property(
            "anti_aliasing_method",
            unreal.AntiAliasingMethod.AAM_NONE,
        )
        anti_aliasing.set_editor_property("render_warm_up_count", 0)
        anti_aliasing.set_editor_property("engine_warm_up_count", 0)
        anti_aliasing.set_editor_property("render_warm_up_frames", False)
        cvars = pipeline_job.get_configuration().find_or_add_setting_by_class(
            unreal.MoviePipelineConsoleVariableSetting
        )
        for name, value in [
            ("r.AntiAliasingMethod", 0),
            ("r.ScreenPercentage", 100),
            ("r.MotionBlurQuality", 0),
            ("r.DefaultFeature.MotionBlur", 0),
            ("r.EyeAdaptationQuality", 0),
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
            ("r.SkyAtmosphere.FastSkyLUT", 0),
        ]:
            cvars.add_or_update_console_variable(name, float(value))
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
        frames = int(job.get("frames", 8))
        _write_manifest(
            out_dir,
            frames,
            time.monotonic() - float(_STATE["started_at"]),
            success,
        )
        unreal.log(f"[UEF-MRQ-SPIKE-RUNTIME] render finished success={success}")
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


def _write_manifest(out_dir: Path, frames: int, duration_sec: float, success: bool) -> None:
    frame_paths = sorted(path for path in out_dir.glob("*.png"))
    payload = {
        "schema_version": 1,
        "status": "ok" if success and len(frame_paths) == frames else "failed",
        "render_kind": "mrq_spike",
        "frames_expected": frames,
        "frames_found": len(frame_paths),
        "duration_sec": round(duration_sec, 3),
        "frame_paths": [str(path) for path in frame_paths],
        "executor": "UEFMRQSpikeRuntimeExecutor",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    unreal.log(f"[UEF-MRQ-SPIKE-RUNTIME] manifest={out_dir / 'manifest.json'}")
