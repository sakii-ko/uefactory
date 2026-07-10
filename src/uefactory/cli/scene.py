from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from uefactory.cli._common import settings_from_context
from uefactory.scenes import (
    SceneBuildError,
    SceneSpecError,
    build_scene,
    load_scene_spec,
    thumbnail_catalog_scene,
)

scene_app = typer.Typer(
    help="Validate and build persistent multi-object Unreal levels.",
    no_args_is_help=True,
)


@scene_app.command("validate")
def validate_scene(
    spec_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Strict SceneSpec YAML or JSON file.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the canonical validated SceneSpec."),
    ] = False,
) -> None:
    try:
        spec = load_scene_spec(spec_path)
    except SceneSpecError as exc:
        typer.echo(f"Invalid scene spec {spec_path}: {exc}", err=True)
        raise typer.Exit(2) from exc
    payload = {**spec.as_dict(), "spec_sha256": spec.digest}
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"SceneSpec valid: {spec.scene_id}")
    typer.echo(f"Map: {spec.build.map_path}")
    typer.echo(f"License: {spec.source.license} ({spec.source.license_tier})")
    typer.echo(f"Digest: {spec.digest}")


@scene_app.command("build")
def build_scene_level(
    ctx: typer.Context,
    spec_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Strict SceneSpec YAML or JSON file.",
        ),
    ],
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Optional scene build run root."),
    ] = None,
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable build results."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    out_root = out
    if out_root is not None and not out_root.is_absolute():
        out_root = settings.project_root / out_root
    try:
        result = build_scene(
            settings=settings,
            spec_path=spec_path,
            database_path=database,
            out_root=out_root,
            timeout_sec=timeout_sec,
        )
    except SceneSpecError as exc:
        typer.echo(f"Invalid scene spec {spec_path}: {exc}", err=True)
        raise typer.Exit(2) from exc
    except SceneBuildError as exc:
        typer.echo(
            f"Scene build failed ({exc.cause_type}): {exc}; manifest={exc.manifest_path}",
            err=True,
        )
        raise typer.Exit(1) from exc
    payload = {
        "status": "built",
        "scene": result.scene.as_dict(),
        "run_dir": str(result.run_dir),
        "manifest": str(result.manifest_path),
        "catalog": str(result.catalog_path),
        "inventory_sha256": result.inventory_sha256,
        "packages": list(result.packages),
        "package_bundle_sha256": result.package_bundle_sha256,
        "build_sha256": result.build_sha256,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Scene built: {result.scene.scene_id}")
    typer.echo(f"Map: {result.scene.map_path}")
    typer.echo(
        f"Actors: {result.scene.actor_count}; meshes: {result.scene.static_mesh_count}; "
        f"triangles: {result.scene.triangle_count}"
    )
    typer.echo(f"Manifest: {result.manifest_path}")
    typer.echo(f"Build digest: {result.build_sha256}")
    typer.echo(f"Package bundle: {result.package_bundle_sha256}")
    typer.echo(f"Catalog: {result.catalog_path}")


@scene_app.command("thumbnail")
def thumbnail_scene_level(
    ctx: typer.Context,
    scene_id: Annotated[str, typer.Argument(help="Built catalog scene id.")],
    database: Annotated[
        Path | None,
        typer.Option("--database", "--db", help="Catalog database (default: data/catalog.db)."),
    ] = None,
    timeout_sec: Annotated[
        int,
        typer.Option("--timeout-sec", min=1, help="Timeout for each UE process."),
    ] = 1800,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable thumbnail results."),
    ] = False,
) -> None:
    settings = settings_from_context(ctx)
    result = thumbnail_catalog_scene(
        settings=settings,
        scene_id=scene_id,
        database_path=database,
        timeout_sec=timeout_sec,
    )
    payload = {
        "status": "render_ok",
        "scene_id": result.scene.scene_id,
        "thumbnail": str(result.thumbnail_path),
        "subject_mask": str(result.subject_mask_path),
        "render_manifest": str(result.render.manifest_path),
        "contact_sheet": str(result.render.artifacts.contact_sheet)
        if result.render.artifacts is not None
        else None,
        "catalog": str(result.catalog_path),
        "artifact_ids": list(result.artifact_ids),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"Scene thumbnail ready: {result.thumbnail_path}")
    typer.echo(f"Subject mask: {result.subject_mask_path}")
    typer.echo(f"Catalog: {result.catalog_path}")


__all__ = ["scene_app"]
