"""Fail-closed local boundary-layer trial on one real project wall patch.

This module deliberately does not create a solver-ready volume mesh.  It
imports the hash-bound, read-only project BREP, resolves one configured wall
surface from its stable geometry fingerprint, removes the original fluid
volumes in-memory and extrudes a small prism-only patch into the adjacent
fluid side.  The result is evidence for the boundary-layer meshing method; it
is never eligible for SU2, ParaView or production use.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tomllib
import traceback
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .atmosphere import us_standard_atmosphere_1976
from .geometry_repair import _collect_model
from .topology_smoke_mesh import _normalize_contract, _resolve_entities


PathLike = str | os.PathLike[str]
_SCHEMA = "cfdpipe.boundary_layer_trial.v1"
_CONFIG_SCHEMA = "cfdpipe.boundary_layer_trial.v1"
_MAX_ELEMENT_CAP = 300_000
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class BoundaryLayerTrialError(RuntimeError):
    """Raised after a local boundary-layer trial fails closed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerTrialError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerTrialError(f"{label} must be numeric") from error
    if not math.isfinite(result):
        raise BoundaryLayerTrialError(f"{label} contains NaN or Inf")
    return result


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerTrialError(f"{label} must be a positive integer")
    return value


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _has_reparse_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_reparse(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _snapshot(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "read_only": _is_read_only(path),
    }


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _write_new_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def _load_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BoundaryLayerTrialError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise BoundaryLayerTrialError(f"{label} root must be a table")
    return value


def _case_rows(path: Path) -> list[dict[str, str]]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise BoundaryLayerTrialError(f"cannot read cases CSV: {error}") from error
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise BoundaryLayerTrialError("cases CSV has no header")
        return [dict(row) for row in reader]


def _required_record_hash(
    records: Mapping[str, Mapping[str, Any]], key: str
) -> str:
    record = records.get(key)
    value = record.get("sha256") if isinstance(record, Mapping) else None
    if not _is_sha256(value):
        raise BoundaryLayerTrialError(f"input record {key!r} has no SHA-256")
    return str(value).casefold()


def _wall_member(
    marker_config: Mapping[str, Any], fingerprint_id: str
) -> tuple[str, dict[str, Any], float]:
    matches: list[tuple[str, dict[str, Any]]] = []
    wall_area = 0.0
    markers = marker_config.get("solver_markers")
    if not isinstance(markers, list):
        raise BoundaryLayerTrialError("marker config has no solver_markers list")
    for raw_marker in markers:
        if not isinstance(raw_marker, Mapping):
            raise BoundaryLayerTrialError("marker config contains an invalid marker")
        if raw_marker.get("kind") != "wall":
            continue
        name = str(raw_marker.get("physical_name", ""))
        members = raw_marker.get("members")
        if not name or not isinstance(members, list) or not members:
            raise BoundaryLayerTrialError("wall marker is incomplete")
        for raw_member in members:
            if not isinstance(raw_member, Mapping):
                raise BoundaryLayerTrialError("wall marker member is invalid")
            area = _finite(raw_member.get("area_m2"), "wall member area")
            if area <= 0.0:
                raise BoundaryLayerTrialError("wall member area must be positive")
            wall_area += area
            if str(raw_member.get("fingerprint_id", "")).casefold() == fingerprint_id:
                matches.append((name, dict(raw_member)))
    if len(matches) != 1:
        raise BoundaryLayerTrialError(
            "configured boundary-layer surface fingerprint must match exactly one wall member"
        )
    return matches[0][0], matches[0][1], wall_area


def normalize_boundary_layer_trial_config(
    document: Mapping[str, Any],
    *,
    project: Mapping[str, Any],
    selected_case: Mapping[str, Any],
    marker_config: Mapping[str, Any],
    input_records: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate and normalize the hash-bound local trial contract."""

    expected_root = {
        "schema",
        "status",
        "policy",
        "provenance",
        "selection",
        "estimator",
        "mesh",
    }
    if set(document) != expected_root:
        raise BoundaryLayerTrialError(
            "boundary-layer trial config has unexpected or missing root keys"
        )
    if document.get("schema") != _CONFIG_SCHEMA or document.get("status") != "CONFIGURED":
        raise BoundaryLayerTrialError("boundary-layer trial config schema/status is invalid")

    policy = document.get("policy")
    expected_policy = {
        "local_wall_patch_only",
        "production_mesh_eligible",
        "run_su2",
        "run_paraview",
        "runtime_tags_present",
        "max_3d_elements",
    }
    if not isinstance(policy, Mapping) or set(policy) != expected_policy:
        raise BoundaryLayerTrialError("boundary-layer trial policy is incomplete")
    if (
        policy.get("local_wall_patch_only") is not True
        or policy.get("production_mesh_eligible") is not False
        or policy.get("run_su2") is not False
        or policy.get("run_paraview") is not False
        or policy.get("runtime_tags_present") is not False
    ):
        raise BoundaryLayerTrialError("boundary-layer trial policy is unsafe")
    max_elements = _positive_integer(
        policy.get("max_3d_elements"), "policy.max_3d_elements"
    )
    if max_elements > _MAX_ELEMENT_CAP:
        raise BoundaryLayerTrialError(
            f"boundary-layer trial element cap exceeds {_MAX_ELEMENT_CAP}"
        )

    provenance = document.get("provenance")
    expected_provenance = {
        "project_sha256": "project",
        "cases_sha256": "cases",
        "markers_sha256": "markers",
        "pipeline_brep_sha256": "pipeline_geometry",
    }
    if not isinstance(provenance, Mapping) or set(provenance) != set(expected_provenance):
        raise BoundaryLayerTrialError("boundary-layer trial provenance is incomplete")
    for field, record_name in expected_provenance.items():
        if str(provenance.get(field, "")).casefold() != _required_record_hash(
            input_records, record_name
        ):
            raise BoundaryLayerTrialError(
                f"boundary-layer trial provenance {field} is stale"
            )

    selection = document.get("selection")
    expected_selection = {
        "case_id",
        "surface_fingerprint_id",
        "required_marker_kind",
    }
    if not isinstance(selection, Mapping) or set(selection) != expected_selection:
        raise BoundaryLayerTrialError("boundary-layer trial selection is incomplete")
    case_id = str(selection.get("case_id", "")).strip()
    selected_case_id = str(selected_case.get("case_id", "")).strip()
    production = project.get("production")
    design_case = (
        str(production.get("design_case", "")).strip()
        if isinstance(production, Mapping)
        else ""
    )
    if not case_id or case_id != selected_case_id or case_id != design_case:
        raise BoundaryLayerTrialError(
            "boundary-layer trial must use the selected project design case"
        )
    fingerprint_id = str(selection.get("surface_fingerprint_id", "")).casefold()
    if not _is_sha256(fingerprint_id) or selection.get("required_marker_kind") != "wall":
        raise BoundaryLayerTrialError("boundary-layer trial wall selection is invalid")
    wall_name, member, whole_wall_area = _wall_member(marker_config, fingerprint_id)

    estimator = document.get("estimator")
    expected_estimator = {
        "skin_friction_correlation",
        "height_safety_factor",
        "target_yplus_source",
        "reference_length_source",
    }
    if not isinstance(estimator, Mapping) or set(estimator) != expected_estimator:
        raise BoundaryLayerTrialError("boundary-layer trial estimator is incomplete")
    if (
        estimator.get("skin_friction_correlation")
        != "turbulent_flat_plate_cf_0p026_re_x_minus_one_seventh"
        or estimator.get("target_yplus_source")
        != "project.physics.target_yplus"
        or estimator.get("reference_length_source")
        != "project.reference.length_m"
    ):
        raise BoundaryLayerTrialError("boundary-layer trial estimator is unsupported")
    safety_factor = _finite(
        estimator.get("height_safety_factor"), "estimator.height_safety_factor"
    )
    if not 0.1 <= safety_factor <= 1.0:
        raise BoundaryLayerTrialError("height_safety_factor must be between 0.1 and 1")

    mesh = document.get("mesh")
    expected_mesh = {
        "surface_size_m",
        "layer_count_source",
        "growth_ratio_source",
        "element_order",
        "recombine",
    }
    if not isinstance(mesh, Mapping) or set(mesh) != expected_mesh:
        raise BoundaryLayerTrialError("boundary-layer trial mesh settings are incomplete")
    surface_size = _finite(mesh.get("surface_size_m"), "mesh.surface_size_m")
    if not 0.005 <= surface_size <= 0.25:
        raise BoundaryLayerTrialError("surface_size_m must be between 0.005 and 0.25 m")
    if (
        mesh.get("layer_count_source") != "project.mesh.minimum_prism_layers"
        or mesh.get("growth_ratio_source")
        != "project.mesh.initial_growth_ratio"
        or mesh.get("element_order") != 1
        or mesh.get("recombine") is not True
    ):
        raise BoundaryLayerTrialError("boundary-layer trial mesh policy is unsafe")

    physics = project.get("physics")
    project_mesh = project.get("mesh")
    reference = project.get("reference")
    atmosphere = project.get("atmosphere")
    if not all(
        isinstance(value, Mapping)
        for value in (physics, project_mesh, reference, atmosphere)
    ):
        raise BoundaryLayerTrialError("project physical/mesh tables are incomplete")
    if physics.get("solver") != "RANS" or physics.get("turbulence_model") != "SST":
        raise BoundaryLayerTrialError("project baseline must remain RANS/SST")
    target_yplus = _finite(physics.get("target_yplus"), "project target_yplus")
    maximum_yplus = _finite(physics.get("maximum_yplus"), "project maximum_yplus")
    reference_length = _finite(reference.get("length_m"), "project reference length")
    layer_count = _positive_integer(
        project_mesh.get("minimum_prism_layers"), "project minimum_prism_layers"
    )
    growth_ratio = _finite(
        project_mesh.get("initial_growth_ratio"), "project initial_growth_ratio"
    )
    if (
        not 0.0 < target_yplus <= maximum_yplus <= 1.0
        or reference_length <= 0.0
        or layer_count < 15
        or not 1.0 < growth_ratio <= 1.5
    ):
        raise BoundaryLayerTrialError("project y+/layer policy is unsafe")

    return {
        "case_id": case_id,
        "max_3d_elements": max_elements,
        "surface_fingerprint_id": fingerprint_id,
        "wall_physical_name": wall_name,
        "selected_member": member,
        "selected_area_m2": _finite(member["area_m2"], "selected wall area"),
        "whole_wall_area_m2": whole_wall_area,
        "skin_friction_correlation": str(estimator["skin_friction_correlation"]),
        "height_safety_factor": safety_factor,
        "surface_size_m": surface_size,
        "target_yplus": target_yplus,
        "maximum_yplus": maximum_yplus,
        "reference_length_m": reference_length,
        "layer_count": layer_count,
        "growth_ratio": growth_ratio,
        "atmosphere": dict(atmosphere),
    }


def calculate_boundary_layer_schedule(
    normalized: Mapping[str, Any], selected_case: Mapping[str, Any]
) -> dict[str, Any]:
    """Calculate a conservative first-cell height and cumulative layer heights."""

    mach = _finite(selected_case.get("mach"), "case Mach")
    altitude_km = _finite(selected_case.get("altitude_km"), "case altitude_km")
    if mach <= 0.0 or altitude_km < 0.0:
        raise BoundaryLayerTrialError("case Mach/altitude is invalid")
    atmosphere = normalized["atmosphere"]
    state = us_standard_atmosphere_1976(
        altitude_km * 1000.0,
        model=str(atmosphere.get("model", "")),
        altitude_kind=str(atmosphere.get("altitude_kind", "")),
        viscosity_model=str(atmosphere.get("viscosity_model", "")),
    )
    velocity = mach * state.speed_of_sound_m_s
    reynolds = (
        state.density_kg_m3
        * velocity
        * float(normalized["reference_length_m"])
        / state.dynamic_viscosity_pa_s
    )
    if not math.isfinite(reynolds) or reynolds <= 1.0:
        raise BoundaryLayerTrialError("reference Reynolds number is invalid")
    skin_friction = 0.026 * reynolds ** (-1.0 / 7.0)
    wall_shear = 0.5 * state.density_kg_m3 * velocity * velocity * skin_friction
    friction_velocity = math.sqrt(wall_shear / state.density_kg_m3)
    estimated_first_height = (
        float(normalized["target_yplus"])
        * state.dynamic_viscosity_pa_s
        / (state.density_kg_m3 * friction_velocity)
    )
    first_height = estimated_first_height * float(normalized["height_safety_factor"])
    if not math.isfinite(first_height) or first_height <= 0.0:
        raise BoundaryLayerTrialError("first-layer height calculation is invalid")
    layer_count = int(normalized["layer_count"])
    growth_ratio = float(normalized["growth_ratio"])
    thicknesses = [first_height * growth_ratio**index for index in range(layer_count)]
    cumulative: list[float] = []
    running = 0.0
    for value in thicknesses:
        running += value
        cumulative.append(running)
    estimated_yplus = (
        first_height
        * state.density_kg_m3
        * friction_velocity
        / state.dynamic_viscosity_pa_s
    )
    values = (
        velocity,
        reynolds,
        skin_friction,
        wall_shear,
        friction_velocity,
        estimated_first_height,
        first_height,
        running,
        estimated_yplus,
        *thicknesses,
        *cumulative,
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise BoundaryLayerTrialError("boundary-layer schedule contains invalid values")
    if estimated_yplus > float(normalized["maximum_yplus"]):
        raise BoundaryLayerTrialError("estimated first-cell y+ exceeds project maximum")
    return {
        "method": str(normalized["skin_friction_correlation"]),
        "planning_estimate_only": True,
        "requires_solution_based_yplus_validation": True,
        "case_id": str(normalized["case_id"]),
        "mach": mach,
        "altitude_km": altitude_km,
        "atmosphere": state.as_dict(),
        "freestream_velocity_m_s": velocity,
        "reference_length_m": float(normalized["reference_length_m"]),
        "reference_reynolds_number": reynolds,
        "estimated_skin_friction_coefficient": skin_friction,
        "estimated_wall_shear_pa": wall_shear,
        "estimated_friction_velocity_m_s": friction_velocity,
        "project_target_yplus": float(normalized["target_yplus"]),
        "project_maximum_yplus": float(normalized["maximum_yplus"]),
        "height_safety_factor": float(normalized["height_safety_factor"]),
        "unsafetied_first_layer_height_m": estimated_first_height,
        "first_layer_height_m": first_height,
        "estimated_yplus_after_safety_factor": estimated_yplus,
        "layer_count": layer_count,
        "growth_ratio": growth_ratio,
        "layer_thicknesses_m": thicknesses,
        "cumulative_heights_m": cumulative,
        "total_thickness_m": running,
    }


def determine_inward_height_sign(
    gmsh: Any,
    catalog: Mapping[str, Any],
    surface_tag: int,
    *,
    probe_distance_m: float,
) -> dict[str, Any]:
    """Select the only normal sign proved to enter the adjacent fluid.

    The oriented CAD boundary tag is retained as audit evidence only.  It is
    not a direction rule: a real-project audit found both signs among wall
    surfaces even though their local inward extrusion sign was the same.
    """

    surfaces = catalog.get("surfaces")
    volumes = catalog.get("volumes")
    if not isinstance(surfaces, list) or not isinstance(volumes, list):
        raise BoundaryLayerTrialError("fresh geometry catalog is incomplete")
    matches = [
        item
        for item in surfaces
        if isinstance(item, Mapping) and int(item.get("entity_tag", -1)) == surface_tag
    ]
    if len(matches) != 1:
        raise BoundaryLayerTrialError("selected runtime surface is not unique")
    adjacent = matches[0].get("adjacent_volumes")
    if not isinstance(adjacent, list) or len(adjacent) != 1:
        raise BoundaryLayerTrialError("selected wall surface is not external to one fluid")
    volume_tag = int(adjacent[0])
    volume_matches = [
        item
        for item in volumes
        if isinstance(item, Mapping) and int(item.get("entity_tag", -1)) == volume_tag
    ]
    if len(volume_matches) != 1:
        raise BoundaryLayerTrialError("selected wall adjacent volume is not unique")
    oriented = volume_matches[0].get("oriented_boundary_surfaces")
    if not isinstance(oriented, list):
        raise BoundaryLayerTrialError("adjacent volume has no orientation audit")
    orientation_matches = [
        item
        for item in oriented
        if isinstance(item, Mapping) and int(item.get("surface_tag", -1)) == surface_tag
    ]
    if len(orientation_matches) != 1:
        raise BoundaryLayerTrialError("selected wall has no unique volume orientation")
    orientation = int(orientation_matches[0].get("orientation", 0))
    if orientation not in {-1, 1}:
        raise BoundaryLayerTrialError("selected wall orientation is invalid")
    attempts: dict[str, Any] = {}
    passing: list[tuple[int, dict[str, Any]]] = []
    for candidate_sign in (-1, 1):
        try:
            evidence = probe_inward_direction(
                gmsh,
                surface_tag=surface_tag,
                volume_tag=volume_tag,
                height_sign=candidate_sign,
                probe_distance_m=probe_distance_m,
            )
        except BoundaryLayerTrialError as error:
            attempts[str(candidate_sign)] = {
                "status": "FAIL",
                "message": str(error),
            }
        else:
            attempts[str(candidate_sign)] = evidence
            passing.append((candidate_sign, evidence))
    if len(passing) != 1:
        raise BoundaryLayerTrialError(
            "bilateral isInside sampling did not identify exactly one inward sign"
        )
    selected_sign, selected_evidence = passing[0]
    return {
        "surface_tag_audit": surface_tag,
        "adjacent_volume_tag_audit": volume_tag,
        "oriented_boundary_sign_audit": orientation,
        "oriented_boundary_sign_used_for_direction": False,
        "inward_height_sign": selected_sign,
        "inside_probe": selected_evidence,
        "bilateral_sign_attempts": attempts,
    }


def probe_inward_direction(
    gmsh: Any,
    *,
    surface_tag: int,
    volume_tag: int,
    height_sign: int,
    probe_distance_m: float,
) -> dict[str, Any]:
    """Prove the proposed normal direction lies inside the adjacent fluid.

    A volume centroid is not a safe direction proxy for this non-convex fluid
    domain.  The decision therefore uses several points on the trimmed wall and
    checks both sides directly with ``gmsh.model.isInside``.
    """

    if height_sign not in {-1, 1}:
        raise BoundaryLayerTrialError("height_sign must be -1 or 1")
    distance = _finite(probe_distance_m, "direction probe distance")
    if distance <= 0.0:
        raise BoundaryLayerTrialError("direction probe distance must be positive")
    minimum_raw, maximum_raw = gmsh.model.getParametrizationBounds(2, surface_tag)
    minimum = [_finite(value, "surface parametrization minimum") for value in minimum_raw]
    maximum = [_finite(value, "surface parametrization maximum") for value in maximum_raw]
    if len(minimum) != 2 or len(maximum) != 2:
        raise BoundaryLayerTrialError("surface parametrization bounds are invalid")

    samples: list[dict[str, Any]] = []
    contradictory = 0
    decisive = 0
    for u_fraction, v_fraction in (
        (0.20, 0.20),
        (0.35, 0.50),
        (0.50, 0.50),
        (0.65, 0.50),
        (0.80, 0.80),
    ):
        parameters = [
            minimum[0] + u_fraction * (maximum[0] - minimum[0]),
            minimum[1] + v_fraction * (maximum[1] - minimum[1]),
        ]
        on_trimmed_surface = int(
            gmsh.model.isInside(2, surface_tag, parameters, True)
        )
        if on_trimmed_surface <= 0:
            samples.append(
                {
                    "parametric_coordinates": parameters,
                    "on_trimmed_surface": False,
                    "decisive": False,
                }
            )
            continue
        point = [
            _finite(value, "surface sample coordinate")
            for value in gmsh.model.getValue(2, surface_tag, parameters)
        ]
        normal = [
            _finite(value, "surface sample normal")
            for value in gmsh.model.getNormal(surface_tag, parameters)
        ]
        if len(point) != 3 or len(normal) != 3:
            raise BoundaryLayerTrialError("surface direction sample is invalid")
        magnitude = math.sqrt(sum(value * value for value in normal))
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            raise BoundaryLayerTrialError("surface direction sample has zero normal")
        unit_normal = [value / magnitude for value in normal]
        proposed = [
            point[index] + height_sign * distance * unit_normal[index]
            for index in range(3)
        ]
        opposite = [
            point[index] - height_sign * distance * unit_normal[index]
            for index in range(3)
        ]
        proposed_inside = int(gmsh.model.isInside(3, volume_tag, proposed))
        opposite_inside = int(gmsh.model.isInside(3, volume_tag, opposite))
        is_decisive = proposed_inside > 0 and opposite_inside == 0
        is_contradictory = proposed_inside == 0 and opposite_inside > 0
        decisive += int(is_decisive)
        contradictory += int(is_contradictory)
        samples.append(
            {
                "parametric_coordinates": parameters,
                "on_trimmed_surface": True,
                "point_m": point,
                "unit_normal": unit_normal,
                "proposed_point_inside_count": proposed_inside,
                "opposite_point_inside_count": opposite_inside,
                "decisive": is_decisive,
                "contradictory": is_contradictory,
            }
        )
    if contradictory or decisive < 3:
        raise BoundaryLayerTrialError(
            "local isInside samples do not prove extrusion into the adjacent fluid"
        )
    return {
        "method": "trimmed-surface bilateral gmsh.model.isInside sampling",
        "probe_distance_m": distance,
        "sample_count": len(samples),
        "decisive_inside_sample_count": decisive,
        "contradictory_sample_count": contradictory,
        "status": "PASS",
        "samples": samples,
    }


def _linear_element_blocks(gmsh: Any, dimension: int, tag: int = -1) -> list[dict[str, Any]]:
    element_types, element_tags, node_tags = gmsh.model.mesh.getElements(dimension, tag)
    if not (len(element_types) == len(element_tags) == len(node_tags)):
        raise BoundaryLayerTrialError("Gmsh returned inconsistent element blocks")
    blocks: list[dict[str, Any]] = []
    for raw_type, raw_tags, raw_nodes in zip(element_types, element_tags, node_tags):
        element_type = int(raw_type)
        tags = [int(value) for value in raw_tags]
        nodes = [int(value) for value in raw_nodes]
        properties = gmsh.model.mesh.getElementProperties(element_type)
        name = str(properties[0])
        element_dimension = int(properties[1])
        order = int(properties[2])
        node_count = int(properties[3])
        primary_count = int(properties[5])
        if element_dimension != dimension or order != 1 or node_count != primary_count:
            raise BoundaryLayerTrialError(
                f"boundary-layer trial contains a non-linear {name}"
            )
        if len(nodes) != len(tags) * node_count:
            raise BoundaryLayerTrialError(f"invalid {name} connectivity")
        blocks.append(
            {
                "element_type": element_type,
                "name": name,
                "node_count": node_count,
                "element_tags": tags,
                "node_tags": nodes,
            }
        )
    return blocks


def inspect_prism_layer_mesh(
    gmsh: Any,
    *,
    base_surface_tag: int,
    requested_layers: int,
    max_elements: int,
) -> dict[str, Any]:
    """Audit a prism-only extrusion and prove the requested column coverage."""

    node_tags_raw, coordinates_raw, _ = gmsh.model.mesh.getNodes()
    node_tags = [int(value) for value in node_tags_raw]
    coordinates = [_finite(value, "mesh node coordinate") for value in coordinates_raw]
    if (
        not node_tags
        or len(node_tags) != len(set(node_tags))
        or len(coordinates) != 3 * len(node_tags)
    ):
        raise BoundaryLayerTrialError("boundary-layer mesh nodes are inconsistent")

    base_blocks = _linear_element_blocks(gmsh, 2, base_surface_tag)
    if not base_blocks or any(
        block["name"] != "Triangle 3" or block["node_count"] != 3
        for block in base_blocks
    ):
        raise BoundaryLayerTrialError("trial wall base must contain only Triangle 3")
    base_triangles = sum(len(block["element_tags"]) for block in base_blocks)
    if base_triangles <= 0:
        raise BoundaryLayerTrialError("trial wall base has no triangles")

    volume_blocks = _linear_element_blocks(gmsh, 3)
    if not volume_blocks or any(
        block["name"] != "Prism 6" or block["node_count"] != 6
        for block in volume_blocks
    ):
        names = sorted({str(block["name"]) for block in volume_blocks})
        raise BoundaryLayerTrialError(
            f"local boundary layer must contain only Prism 6; got {names}"
        )
    element_tags = [
        int(value) for block in volume_blocks for value in block["element_tags"]
    ]
    if len(element_tags) != len(set(element_tags)):
        raise BoundaryLayerTrialError("boundary-layer volume repeats an element tag")
    if len(element_tags) > max_elements:
        raise BoundaryLayerTrialError(
            f"3D element count {len(element_tags)} exceeds cap {max_elements}"
        )
    expected = base_triangles * requested_layers
    if len(element_tags) != expected:
        raise BoundaryLayerTrialError(
            "prism count does not prove one complete column per selected base triangle"
        )

    metrics: dict[str, list[float]] = {}
    for name in ("minDetJac", "minSJ", "minSIGE", "minSICN", "volume"):
        values = [
            _finite(value, f"boundary-layer quality {name}")
            for value in gmsh.model.mesh.getElementQualities(element_tags, name)
        ]
        if len(values) != len(element_tags):
            raise BoundaryLayerTrialError(f"quality {name} count is inconsistent")
        metrics[name] = values
    nonpositive = {
        name: sum(value <= 0.0 for value in values)
        for name, values in metrics.items()
    }
    if any(nonpositive.values()):
        raise BoundaryLayerTrialError(
            f"boundary-layer mesh has non-positive quality/volume: {nonpositive}"
        )
    return {
        "node_count": len(node_tags),
        "base_triangle_count": base_triangles,
        "element_count_3d": len(element_tags),
        "element_types_3d": {"Prism 6": len(element_tags)},
        "requested_layer_count": requested_layers,
        "actual_layer_count": len(element_tags) // base_triangles,
        "selected_patch_coverage_fraction": len(element_tags) / expected,
        "minimum_determinant_jacobian": min(metrics["minDetJac"]),
        "minimum_scaled_jacobian": min(metrics["minSJ"]),
        "minimum_signed_inverse_gradient_error": min(metrics["minSIGE"]),
        "minimum_signed_inverse_condition_number": min(metrics["minSICN"]),
        "minimum_element_volume_m3": min(metrics["volume"]),
        "nonpositive_determinant_jacobian_count": nonpositive["minDetJac"],
        "nonpositive_scaled_jacobian_count": nonpositive["minSJ"],
        "nonpositive_signed_inverse_gradient_error_count": nonpositive["minSIGE"],
        "nonpositive_signed_quality_count": nonpositive["minSICN"],
        "nonpositive_volume_count": nonpositive["volume"],
    }


def reconcile_serialized_mesh_audit(
    generation_audit: Mapping[str, Any],
    serialized_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Make the fresh readback of the written MSH the authoritative audit.

    Gmsh can retain nodes belonging to removed CAD entities in memory while
    ``Mesh.SaveAll=0`` correctly omits those unreferenced nodes from the MSH.
    The output manifest must therefore report the node count observed after a
    fresh read of the actual file, while also proving that the prism topology
    and requested layer coverage were preserved by serialization.
    """

    structural_fields = (
        "base_triangle_count",
        "element_count_3d",
        "element_types_3d",
        "requested_layer_count",
        "actual_layer_count",
        "selected_patch_coverage_fraction",
        "nonpositive_determinant_jacobian_count",
        "nonpositive_scaled_jacobian_count",
        "nonpositive_signed_inverse_gradient_error_count",
        "nonpositive_signed_quality_count",
        "nonpositive_volume_count",
    )
    for field in structural_fields:
        if generation_audit.get(field) != serialized_audit.get(field):
            raise BoundaryLayerTrialError(
                f"written MSH changed audited mesh field {field!r}"
            )
    generation_nodes = _positive_integer(
        generation_audit.get("node_count"), "in-memory mesh node count"
    )
    serialized_nodes = _positive_integer(
        serialized_audit.get("node_count"), "written MSH node count"
    )
    if serialized_nodes > generation_nodes:
        raise BoundaryLayerTrialError(
            "written MSH unexpectedly contains more nodes than the in-memory mesh"
        )
    result = dict(serialized_audit)
    result.update(
        {
            "node_count_source": "fresh_gmsh_readback_of_written_msh",
            "serialized_file_readback": True,
            "writer_preserved_prism_topology": True,
            "generation_node_count_before_write": generation_nodes,
            "node_count_delta_from_in_memory_model": (
                generation_nodes - serialized_nodes
            ),
        }
    )
    return result


def build_boundary_layer_trial(
    brep_path: PathLike,
    output_directory: PathLike,
    *,
    project: Mapping[str, Any],
    selected_case: Mapping[str, Any],
    marker_config: Mapping[str, Any],
    trial_config_path: PathLike,
    input_records: Mapping[str, Mapping[str, Any]],
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Build and audit one local prism patch on the real project wall."""

    output = Path(output_directory).expanduser()
    if ".." in output.parts:
        raise BoundaryLayerTrialError("output directory must not contain '..'")
    output = Path(os.path.abspath(output))
    if output.exists():
        raise BoundaryLayerTrialError(f"refusing to overwrite output directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if _has_reparse_component(output.parent):
        raise BoundaryLayerTrialError("output directory has a reparse ancestor")
    staging = output.parent / f".boundary-layer-trial-{os.getpid()}-{uuid4().hex}"
    staging.mkdir()

    started = _utc_now()
    log_lines = [f"[{started}] boundary-layer trial started"]
    manifest: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL",
        "started_at_utc": started,
        "ended_at_utc": None,
        "scope": {
            "local_wall_patch_only": True,
            "complete_fluid_domain": False,
            "core_filled": False,
            "solver_mesh_eligible": False,
            "production_mesh_eligible": False,
            "su2_run_allowed": False,
            "su2_called": False,
            "paraview_run_allowed": False,
            "paraview_called": False,
            "global_wall_coverage_not_claimed": True,
            "yplus_verified": False,
            "physical_interpretation_allowed": False,
        },
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_initialize_called": False,
        "gmsh_finalize_called": False,
        "logger_messages": [],
        "source_before": None,
        "source_after": None,
        "source_unchanged": None,
        "trial_config": None,
        "inputs": {key: dict(value) for key, value in input_records.items()},
        "schedule": None,
        "selection": None,
        "mesh": None,
        "outputs": {},
        "error": None,
        "secondary_errors": [],
    }
    gmsh: Any | None = None
    initialized = False
    initialization_attempted = False
    logger_started = False
    source: Path | None = None
    primary: BaseException | None = None
    primary_traceback = ""
    result_payload: dict[str, Any] | None = None

    def secondary(error: BaseException, context: str) -> None:
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        manifest["secondary_errors"].append(
            {
                "context": context,
                "type": type(error).__name__,
                "message": str(error),
                "traceback": rendered,
            }
        )
        log_lines.append(rendered)

    try:
        config_path = Path(trial_config_path).expanduser().resolve(strict=True)
        if not config_path.is_file() or _has_reparse_component(config_path):
            raise BoundaryLayerTrialError("trial config must be an ordinary non-reparse file")
        config_document = _load_toml(config_path, "boundary-layer trial config")
        normalized = normalize_boundary_layer_trial_config(
            config_document,
            project=project,
            selected_case=selected_case,
            marker_config=marker_config,
            input_records=input_records,
        )
        manifest["trial_config"] = {
            "path": str(config_path),
            "sha256": _sha256(config_path),
            "normalized": dict(normalized),
        }
        schedule = calculate_boundary_layer_schedule(normalized, selected_case)
        manifest["schedule"] = schedule

        candidate = Path(brep_path).expanduser()
        if not candidate.exists() or _has_reparse_component(candidate):
            raise BoundaryLayerTrialError("pipeline BREP must be an existing non-reparse file")
        source = candidate.resolve(strict=True)
        if not source.is_file() or source.suffix.casefold() != ".brep" or not _is_read_only(source):
            raise BoundaryLayerTrialError("pipeline BREP must be a read-only BREP file")
        before = _snapshot(source)
        manifest["source_before"] = before
        if before["sha256"] != _required_record_hash(input_records, "pipeline_geometry"):
            raise BoundaryLayerTrialError("pipeline BREP SHA-256 differs from input evidence")

        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_file = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_file).resolve()) if module_file else "unknown"
        )
        is_initialized = getattr(gmsh, "isInitialized", None)
        if callable(is_initialized) and bool(is_initialized()):
            raise BoundaryLayerTrialError("boundary-layer trial requires a fresh Gmsh session")
        initialization_attempted = True
        gmsh.initialize(readConfigFiles=False)
        initialized = True
        manifest["gmsh_initialize_called"] = True
        logger = getattr(gmsh, "logger", None)
        if logger is not None:
            logger.start()
            logger_started = True

        gmsh.model.add("cfdpipe_boundary_layer_trial")
        gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        imported = gmsh.model.occ.importShapes(str(source))
        gmsh.model.occ.synchronize()
        if not imported:
            raise BoundaryLayerTrialError("Gmsh imported no BREP entities")
        catalog = _collect_model(gmsh, require_normals=False)
        resolved = _resolve_entities(catalog, _normalize_contract(marker_config))
        diagnostic_index = resolved.get("surface_diagnostic_index")
        if not isinstance(diagnostic_index, Mapping):
            raise BoundaryLayerTrialError("fresh surface diagnostic index is missing")
        requested_fingerprint = str(normalized["surface_fingerprint_id"])
        matching_tags = [
            int(raw_tag)
            for raw_tag, record in diagnostic_index.items()
            if isinstance(record, Mapping)
            and str(record.get("fingerprint_id", "")).casefold()
            == requested_fingerprint
            and str(record.get("physical_name", ""))
            == str(normalized["wall_physical_name"])
        ]
        if len(matching_tags) != 1:
            raise BoundaryLayerTrialError(
                "configured wall fingerprint did not uniquely resolve in fresh BREP"
            )
        surface_tag = matching_tags[0]
        direction = determine_inward_height_sign(
            gmsh,
            catalog,
            surface_tag,
            probe_distance_m=max(
                float(schedule["total_thickness_m"]),
                float(schedule["first_layer_height_m"]) * 4.0,
            ),
        )
        signed_heights = [
            direction["inward_height_sign"] * float(value)
            for value in schedule["cumulative_heights_m"]
        ]

        all_volumes = list(gmsh.model.getEntities(3))
        gmsh.model.occ.remove(all_volumes, recursive=False)
        unselected_surfaces = [
            item for item in gmsh.model.getEntities(2) if int(item[1]) != surface_tag
        ]
        if unselected_surfaces:
            gmsh.model.occ.remove(unselected_surfaces, recursive=False)
        gmsh.model.occ.synchronize()
        if list(gmsh.model.getEntities(3)) or list(gmsh.model.getEntities(2)) != [
            (2, surface_tag)
        ]:
            raise BoundaryLayerTrialError(
                "in-memory local patch isolation did not leave exactly one wall surface"
            )

        fixed_options = {
            "General.NumThreads": 1.0,
            "Geometry.ExtrudeReturnLateralEntities": 1.0,
            "Mesh.ElementOrder": 1.0,
            "Mesh.RecombineAll": 0.0,
            "Mesh.MeshSizeFromCurvature": 0.0,
            "Mesh.MeshSizeFromPoints": 0.0,
            "Mesh.MeshSizeExtendFromBoundary": 0.0,
            "Mesh.MeshSizeMin": float(normalized["surface_size_m"]),
            "Mesh.MeshSizeMax": float(normalized["surface_size_m"]),
            "Mesh.SaveAll": 0.0,
            "Mesh.Binary": 0.0,
            "Mesh.MshFileVersion": 4.1,
            "Mesh.Algorithm": 6.0,
        }
        for name, value in fixed_options.items():
            gmsh.option.setNumber(name, value)

        layer_count = int(schedule["layer_count"])
        extruded = gmsh.model.geo.extrudeBoundaryLayer(
            [(2, surface_tag)],
            [1] * layer_count,
            signed_heights,
            True,
        )
        volume_tags = [int(tag) for dimension, tag in extruded if int(dimension) == 3]
        top_surface_tags: list[int] = []
        for index, (dimension, _) in enumerate(extruded):
            if int(dimension) == 3 and index > 0 and int(extruded[index - 1][0]) == 2:
                top_surface_tags.append(int(extruded[index - 1][1]))
        returned_surfaces = [
            int(tag) for dimension, tag in extruded if int(dimension) == 2
        ]
        lateral_surface_tags = sorted(set(returned_surfaces) - set(top_surface_tags))
        if len(volume_tags) != 1 or len(top_surface_tags) != 1 or not lateral_surface_tags:
            raise BoundaryLayerTrialError("Gmsh returned an incomplete boundary-layer extrusion")
        gmsh.model.geo.synchronize()

        physical_groups = (
            (2, [surface_tag], "trial_wall_base"),
            (2, top_surface_tags, "trial_outer_surface"),
            (2, lateral_surface_tags, "trial_lateral_surfaces"),
            (3, volume_tags, "trial_prism_volume"),
        )
        for dimension, tags, name in physical_groups:
            physical_tag = gmsh.model.addPhysicalGroup(dimension, tags)
            gmsh.model.setPhysicalName(dimension, physical_tag, name)

        gmsh.model.mesh.generate(3)
        generation_mesh = inspect_prism_layer_mesh(
            gmsh,
            base_surface_tag=surface_tag,
            requested_layers=layer_count,
            max_elements=int(normalized["max_3d_elements"]),
        )
        msh_stage = staging / "boundary_layer_trial.msh"
        gmsh.write(str(msh_stage))
        if not msh_stage.is_file() or msh_stage.stat().st_size <= 0:
            raise BoundaryLayerTrialError("Gmsh did not write a nonempty trial MSH")

        # Re-open the exact bytes that will be published.  This intentionally
        # replaces the CAD model in the owned Gmsh session: all geometry work
        # is complete, and the source BREP remains an independently snapshotted
        # read-only file.
        gmsh.clear()
        gmsh.open(str(msh_stage))
        serialized_mesh = inspect_prism_layer_mesh(
            gmsh,
            base_surface_tag=surface_tag,
            requested_layers=layer_count,
            max_elements=int(normalized["max_3d_elements"]),
        )
        mesh = reconcile_serialized_mesh_audit(generation_mesh, serialized_mesh)
        selected_area = float(normalized["selected_area_m2"])
        whole_wall_area = float(normalized["whole_wall_area_m2"])
        mesh["selected_patch_area_m2"] = selected_area
        mesh["whole_wall_area_m2"] = whole_wall_area
        mesh["whole_wall_area_fraction"] = selected_area / whole_wall_area
        mesh["global_wall_coverage_not_claimed"] = True
        mesh["surface_size_m"] = float(normalized["surface_size_m"])
        mesh["complete_fluid_domain"] = False
        mesh["core_filled"] = False
        mesh["solver_mesh_eligible"] = False
        result_payload = {
            "selection": {
                "surface_fingerprint_id": requested_fingerprint,
                "wall_physical_name": str(normalized["wall_physical_name"]),
                "surface_tag_audit": surface_tag,
                "runtime_tag_is_matching_criterion": False,
                "geometry": dict(normalized["selected_member"]),
                "direction": direction,
                "signed_cumulative_heights_m": signed_heights,
                "extruded_volume_tags_audit": volume_tags,
                "top_surface_tags_audit": top_surface_tags,
                "lateral_surface_tags_audit": lateral_surface_tags,
            },
            "mesh": mesh,
            "outputs": {
                "boundary_layer_trial.msh": {
                    "path": str(output / "boundary_layer_trial.msh"),
                    "size_bytes": msh_stage.stat().st_size,
                    "sha256": _sha256(msh_stage),
                }
            },
        }
    except BaseException as error:
        primary = error
        primary_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        log_lines.append(primary_traceback)
    finally:
        if gmsh is not None and logger_started:
            try:
                messages = [str(value) for value in gmsh.logger.get()]
                manifest["logger_messages"] = messages
                log_lines.extend(messages)
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    secondary(error, "gmsh.logger.get")
            try:
                gmsh.logger.stop()
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    secondary(error, "gmsh.logger.stop")
        if gmsh is not None and initialized:
            try:
                gmsh.finalize()
                manifest["gmsh_finalize_called"] = True
            except BaseException as error:
                if primary is None:
                    primary = error
                    primary_traceback = "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    )
                else:
                    secondary(error, "gmsh.finalize")
        elif gmsh is not None and initialization_attempted:
            is_initialized = getattr(gmsh, "isInitialized", None)
            if callable(is_initialized):
                try:
                    if bool(is_initialized()):
                        gmsh.finalize()
                        manifest["gmsh_finalize_called"] = True
                except BaseException as error:
                    if primary is None:
                        primary = error
                        primary_traceback = "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        )
                    else:
                        secondary(error, "gmsh.finalize-after-initialize-error")

    try:
        if source is not None and source.exists():
            after = _snapshot(source)
            manifest["source_after"] = after
            manifest["source_unchanged"] = after == manifest["source_before"]
            if manifest["source_unchanged"] is not True and primary is None:
                raise BoundaryLayerTrialError("pipeline BREP changed during local trial")
    except BaseException as error:
        if primary is None:
            primary = error
            primary_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
        else:
            secondary(error, "source-after-snapshot")

    manifest["ended_at_utc"] = _utc_now()
    if primary is None and result_payload is not None:
        manifest.update(result_payload)
        manifest["status"] = "PASS"
        manifest["error"] = None
        log_lines.append(f"[{manifest['ended_at_utc']}] status=PASS")
        try:
            _write_new_text(
                staging / "gmsh_boundary_layer_trial.log", "\n".join(log_lines) + "\n"
            )
            _write_new_json(staging / "boundary_layer_trial_manifest.json", manifest)
            staging.rename(output)
        except BaseException as error:
            primary = error
            primary_traceback = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
        else:
            return manifest

    manifest["status"] = "FAIL"
    manifest["error"] = {
        "type": type(primary).__name__ if primary is not None else "UnknownError",
        "message": str(primary) if primary is not None else "unknown failure",
        "traceback": primary_traceback,
    }
    log_lines.append(f"[{manifest['ended_at_utc']}] status=FAIL")
    try:
        if staging.exists():
            resolved_staging = staging.resolve(strict=True)
            resolved_parent = output.parent.resolve(strict=True)
            if resolved_staging.parent != resolved_parent or not staging.name.startswith(
                ".boundary-layer-trial-"
            ):
                raise BoundaryLayerTrialError("refusing to clean unowned staging path")
            shutil.rmtree(staging)
        output.mkdir()
        _write_new_text(
            output / "gmsh_boundary_layer_trial.log", "\n".join(log_lines) + "\n"
        )
        _write_new_json(output / "boundary_layer_trial_manifest.json", manifest)
    except BaseException as publication_error:
        raise BoundaryLayerTrialError(
            "boundary-layer trial failed and failure evidence could not be published: "
            f"{publication_error}"
        ) from primary
    if isinstance(primary, (KeyboardInterrupt, SystemExit)):
        raise primary
    raise BoundaryLayerTrialError(
        f"boundary-layer trial failed: {manifest['error']['message']}"
    ) from primary


__all__ = [
    "BoundaryLayerTrialError",
    "build_boundary_layer_trial",
    "calculate_boundary_layer_schedule",
    "determine_inward_height_sign",
    "inspect_prism_layer_mesh",
    "reconcile_serialized_mesh_audit",
    "normalize_boundary_layer_trial_config",
    "probe_inward_direction",
]
