"""Fail-closed orchestration for real-STEP geometry evidence collection.

This is deliberately separate from the production pipeline.  It creates only
surface diagnostics and screenshots, never a volume mesh or an SU2 file.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import traceback
from typing import Any

from .bridges import GmshBridge
from .bridges.geometry_review_paraview import run_geometry_review
from .geometry_review import build_diagnostic_surface_mesh
from .process import CommandRunner
from .step_topology import map_step_solids_to_gmsh_volumes, parse_step_topology
from .toolchain import Toolchain


class GeometryEvidenceReviewError(RuntimeError):
    """Raised when a geometry-evidence stage fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise GeometryEvidenceReviewError(f"evidence file is missing: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _surface_components(
    surfaces: Sequence[Mapping[str, Any]],
    volumes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_tag = {int(item["entity_tag"]): item for item in surfaces}
    result: list[dict[str, Any]] = []
    for volume in sorted(volumes, key=lambda item: int(item["entity_tag"])):
        volume_tag = int(volume["entity_tag"])
        boundary = sorted(int(tag) for tag in volume["boundary_surfaces"])
        unseen = set(boundary)
        components: list[list[int]] = []
        while unseen:
            seed = min(unseen)
            unseen.remove(seed)
            pending = [seed]
            component: list[int] = []
            while pending:
                current = pending.pop()
                component.append(current)
                curves = {
                    int(value) for value in by_tag[current].get("boundary_curves", [])
                }
                connected = sorted(
                    candidate
                    for candidate in unseen
                    if curves
                    & {
                        int(value)
                        for value in by_tag[candidate].get("boundary_curves", [])
                    }
                )
                for candidate in connected:
                    unseen.remove(candidate)
                    pending.append(candidate)
            components.append(sorted(component))
        for index, component in enumerate(
            sorted(components, key=lambda tags: (len(tags), tags)), start=1
        ):
            result.append(
                {
                    "volume_entity_tag": volume_tag,
                    "component_index": index,
                    "surface_tags": component,
                    "face_count": len(component),
                }
            )
    return result


def _shell_component_mapping(
    topology: Mapping[str, Any],
    solid_volume_mapping: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    solids = {int(item["id"]): item for item in topology.get("solids", [])}
    volume_to_solid = {
        int(item["volume_entity_tag"]): int(item["solid_id"])
        for item in solid_volume_mapping.get("matches", [])
    }
    records: list[dict[str, Any]] = []
    for volume_tag, solid_id in sorted(volume_to_solid.items()):
        solid = solids[solid_id]
        geometric = [
            item for item in components if int(item["volume_entity_tag"]) == volume_tag
        ]
        step_components = list(solid.get("shell_components", []))
        for component in geometric:
            candidates = [
                item
                for item in step_components
                if int(item.get("face_count", -1)) == int(component["face_count"])
            ]
            reverse_candidates = [
                item
                for item in geometric
                if int(item["face_count"]) == int(component["face_count"])
            ]
            matched = len(candidates) == 1 and len(reverse_candidates) == 1
            record = {
                "volume_entity_tag": volume_tag,
                "solid_id": solid_id,
                "surface_tags": list(component["surface_tags"]),
                "face_count": int(component["face_count"]),
                "status": "MATCHED" if matched else "AMBIGUOUS",
                "candidate_root_shell_ids": [
                    int(item["root_shell_id"]) for item in candidates
                ],
            }
            if matched:
                record.update(
                    {
                        "root_shell_id": int(candidates[0]["root_shell_id"]),
                        "role_in_solid": str(candidates[0]["role_in_solid"]),
                    }
                )
            records.append(record)
    return records


def _strong_contact_pairs(mesh_manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = mesh_manifest.get("distance_evidence")
    if not isinstance(evidence, Mapping):
        return []
    selected: list[dict[str, Any]] = []
    for item in evidence.get("surface_pairs", []):
        if not isinstance(item, Mapping):
            continue
        prescreen = item.get("prescreen")
        if not isinstance(prescreen, Mapping):
            continue
        try:
            relative_area = float(prescreen["relative_area_difference"])
            centroid_distance = float(prescreen["centroid_distance_m"])
            bbox_gap = float(prescreen["bbox_gap_m"])
            distance = float(item["distance_m"])
            first = int(prescreen["surface_a"])
            second = int(prescreen["surface_b"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            all(
                math.isfinite(value)
                for value in (relative_area, centroid_distance, bbox_gap, distance)
            )
            and relative_area <= 1.0e-4
            and centroid_distance <= 1.0e-3
            and bbox_gap <= 1.0e-6
            and distance <= 1.0e-9
        ):
            selected.append(
                {
                    "surface_tags": [first, second],
                    "relative_area_difference": relative_area,
                    "centroid_distance_m": centroid_distance,
                    "bbox_gap_m": bbox_gap,
                    "occ_minimum_distance_m": distance,
                    "classification": "strong_near_coincident_contact_candidate",
                    "full_surface_coincidence_proven": False,
                }
            )
    return sorted(selected, key=lambda item: item["surface_tags"])


def _diagnostic_groups(
    surfaces: Sequence[Mapping[str, Any]],
    volumes: Sequence[Mapping[str, Any]],
    components: Sequence[Mapping[str, Any]],
    contacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, dict[str, list[int]]] = {}
    for volume in sorted(volumes, key=lambda item: int(item["entity_tag"])):
        tag = int(volume["entity_tag"])
        groups[f"volume_{tag}_all"] = {
            "surface_tags": sorted(int(value) for value in volume["boundary_surfaces"])
        }
    for component in components:
        volume_tag = int(component["volume_entity_tag"])
        count = int(component["face_count"])
        index = int(component["component_index"])
        groups[f"volume_{volume_tag}_shell_component_{index}_{count}_faces"] = {
            "surface_tags": list(component["surface_tags"])
        }
    contact_tags: set[int] = set()
    for index, contact in enumerate(contacts, start=1):
        tags = [int(value) for value in contact["surface_tags"]]
        contact_tags.update(tags)
        groups[f"contact_candidate_{index}"] = {"surface_tags": tags}

    axial: list[int] = []
    for surface in surfaces:
        bounds = [float(value) for value in surface["bounds_m"]]
        if bounds[3] - bounds[0] <= 1.0e-5:
            axial.append(int(surface["entity_tag"]))
    if axial:
        groups["axial_planar_candidates"] = {"surface_tags": sorted(axial)}

    exposed = sorted(
        int(surface["entity_tag"])
        for surface in surfaces
        if int(surface["entity_tag"]) not in contact_tags
    )
    if exposed:
        groups["assembly_exposed_surface_candidates"] = {"surface_tags": exposed}

    ranked = sorted(
        surfaces,
        key=lambda item: (-float(item["area_m2"]), int(item["entity_tag"])),
    )
    individual_tags = set(axial) | contact_tags | {
        int(item["entity_tag"]) for item in ranked[:12]
    }
    for tag in sorted(individual_tags):
        groups[f"surface_{tag}"] = {"surface_tags": [tag]}
    return {"groups": groups}


def run_geometry_evidence_review(
    *,
    step_path: Path,
    project_path: Path,
    output_directory: Path,
    allowed_output_root: Path,
    toolchain: Toolchain,
    timeout_seconds: float = 300.0,
    live_output: bool = True,
    mesh_size_m: float = 0.20,
    max_triangles: int = 100_000,
    controller_argv: Sequence[str] = (),
) -> dict[str, Any]:
    """Collect topology, surface-mesh and headless render evidence."""

    output = output_directory.resolve(strict=False)
    allowed = allowed_output_root.resolve(strict=False)
    if output != allowed:
        raise ValueError(f"geometry review output must exactly equal {allowed}: {output}")
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "geometry_evidence_manifest.json"
    started_at = _utc_now()
    manifest: dict[str, Any] = {
        "status": "FAIL",
        "classification_status": "NOT_RUN",
        "marker_configuration_ready": False,
        "next_stage_allowed": False,
        "started_at": started_at,
        "ended_at": None,
        "controller_argv": list(controller_argv),
        "step_path": str(step_path),
        "project_path": str(project_path),
        "output_directory": str(output),
        "stages": {},
        "outputs": {},
        "error": None,
    }
    try:
        source = step_path.resolve(strict=True)
        project = project_path.resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in {".step", ".stp"}:
            raise GeometryEvidenceReviewError(f"invalid STEP source: {source}")
        if not project.is_file() or project.suffix.lower() != ".toml":
            raise GeometryEvidenceReviewError(f"invalid project configuration: {project}")

        topology = parse_step_topology(source)
        catalog_manifest = GmshBridge().inspect_step_geometry(
            source,
            output,
            allowed_output_root=allowed,
        )
        if catalog_manifest.get("status") != "PASS":
            raise GeometryEvidenceReviewError("Gmsh STEP catalog did not pass")

        mesh_manifest = build_diagnostic_surface_mesh(
            source,
            output,
            allowed_output_root=allowed,
            mesh_size_m=mesh_size_m,
            max_triangles=max_triangles,
        )
        if mesh_manifest.get("status") != "PASS":
            raise GeometryEvidenceReviewError("diagnostic Gmsh surface mesh did not pass")
        expected_hash = str(topology["source_sha256"]).lower()
        observed_hashes = {
            str(catalog_manifest.get("source_sha256_after", "")).lower(),
            str(mesh_manifest.get("source_sha256_after", "")).lower(),
        }
        if observed_hashes != {expected_hash}:
            raise GeometryEvidenceReviewError(
                "STEP hash changed or disagreed between topology and Gmsh stages"
            )

        volumes = list(mesh_manifest.get("volumes", []))
        surfaces = list(mesh_manifest.get("surfaces", []))
        solid_mapping = map_step_solids_to_gmsh_volumes(topology, volumes)
        if solid_mapping.get("status") != "MATCHED":
            raise GeometryEvidenceReviewError(
                "STEP solids could not be uniquely matched to Gmsh volumes"
            )
        components = _surface_components(surfaces, volumes)
        shell_mapping = _shell_component_mapping(
            topology, solid_mapping, components
        )
        contacts = _strong_contact_pairs(mesh_manifest)
        topology_evidence = {
            **topology,
            "solid_volume_mapping": solid_mapping,
            "surface_shell_components": components,
            "shell_component_mapping": shell_mapping,
            "strong_contact_candidates": contacts,
        }
        topology_path = output / "step_topology.json"
        _write_json(topology_path, topology_evidence)

        groups_path = output / "geometry_groups.json"
        _write_json(
            groups_path,
            _diagnostic_groups(surfaces, volumes, components, contacts),
        )
        pvbatch = toolchain.resolve("pvbatch")
        renders = output / "renders"
        runner = CommandRunner(
            renders,
            live_output=live_output,
            allow_mnt_c_executables=toolchain.allow_mnt_c_executables,
        )
        render_manifest = run_geometry_review(
            runner,
            pvbatch.path,
            output / "diagnostic_surfaces.vtk",
            groups_path,
            renders,
            allowed_output_root=allowed,
            timeout_seconds=timeout_seconds,
            live_output=live_output,
        )

        from .marker_review import build_marker_review

        marker_review = build_marker_review(
            project,
            output / "surface_catalog.csv",
            output / "surface_types.json",
            topology_evidence,
            solid_mapping,
            renders / "geometry_render_manifest.json",
            output,
            allowed_output_root=allowed,
        )
        manifest["stages"] = {
            "step_topology": {
                "status": "PASS",
                "record_count": topology["record_count"],
                "solid_count": topology["solid_count"],
                "solid_volume_mapping": solid_mapping["status"],
            },
            "gmsh_catalog": {
                "status": catalog_manifest["status"],
                "surface_count": catalog_manifest["surface_count"],
                "volume_count": catalog_manifest["volume_count"],
            },
            "gmsh_diagnostic_surface_mesh": {
                "status": mesh_manifest["status"],
                "quality_status": mesh_manifest["diagnostic_quality_status"],
                "warnings": list(mesh_manifest["warnings"]),
                "mesh": dict(mesh_manifest["mesh"]),
            },
            "pvbatch_render": {
                "status": render_manifest["status"],
                "pvbatch_path": str(pvbatch.path),
                "paraview_version": render_manifest["paraview_version"],
                "number_of_points": render_manifest["number_of_points"],
                "number_of_cells": render_manifest["number_of_cells"],
                "image_count": len(render_manifest["images"]),
            },
            "marker_review": {
                "status": marker_review.get("status"),
                "marker_configuration_ready": bool(
                    marker_review.get("marker_configuration_ready")
                ),
            },
        }
        manifest["status"] = "PASS"
        manifest["classification_status"] = (
            "READY"
            if marker_review.get("marker_configuration_ready")
            else "REVIEW_REQUIRED"
        )
        manifest["marker_configuration_ready"] = bool(
            marker_review.get("marker_configuration_ready")
        )
        manifest["next_stage_allowed"] = bool(
            marker_review.get("marker_configuration_ready")
        )
        for path in (
            topology_path,
            groups_path,
            output / "surface_catalog.csv",
            output / "volume_catalog.csv",
            output / "surface_types.json",
            output / "diagnostic_surfaces.msh",
            output / "diagnostic_surfaces.vtk",
            output / "gmsh_geometry.log",
            output / "gmsh_review.log",
            renders / "geometry_render_manifest.json",
            renders / "pvbatch.command.json",
            renders / "pvbatch.stdout.log",
            renders / "pvbatch.stderr.log",
            output / "marker_review.json",
            output / "marker_review.md",
        ):
            manifest["outputs"][path.name] = _file_record(path)
    except BaseException as error:
        manifest["status"] = "FAIL"
        manifest["classification_status"] = "FAILED"
        manifest["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        }
        manifest["ended_at"] = _utc_now()
        _write_json(manifest_path, manifest)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise GeometryEvidenceReviewError(str(error)) from error

    manifest["ended_at"] = _utc_now()
    _write_json(manifest_path, manifest)
    return manifest


__all__ = ["GeometryEvidenceReviewError", "run_geometry_evidence_review"]
