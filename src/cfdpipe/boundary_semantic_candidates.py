"""Build conservative, tag-independent boundary-semantic candidates.

This module consumes the read-only STEP/OCC evidence already produced by the
geometry-review stage.  It does not import Gmsh, edit CAD, create physical
groups, or assert project business semantics.  Runtime entity tags are kept
only as audit evidence; stable candidate identities are hashes of source,
shell, geometry, orientation and connectivity fingerprints.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PathLike = str | os.PathLike[str]


class BoundarySemanticCandidateError(RuntimeError):
    """Raised when geometry evidence is incomplete or internally inconsistent."""


_SCHEMA_VERSION = "cfdpipe.boundary_semantic_candidates.v1"
_GROUP_SCHEMA = "cfdpipe.boundary_group_fingerprint.v1"
_SURFACE_SCHEMA = "cfdpipe.boundary_surface_fingerprint.v1"
_INTERFACE_SCHEMA = "cfdpipe.interface_pair_fingerprint.v1"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise BoundarySemanticCandidateError(f"{label} does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BoundarySemanticCandidateError(
            f"cannot read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise BoundarySemanticCandidateError(f"{label} must contain a JSON object")
    return value


def _load_project_axes(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise BoundarySemanticCandidateError(f"project config does not exist: {path}")
    try:
        raw = path.read_bytes()
        project = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise BoundarySemanticCandidateError(
            f"cannot read project config {path}: {error}"
        ) from error
    axes = project.get("axes")
    if not isinstance(axes, Mapping) or axes.get("x_positive") != "nose_to_tail":
        raise BoundarySemanticCandidateError(
            "project axes must explicitly declare x_positive='nose_to_tail'"
        )
    return hashlib.sha256(raw).hexdigest(), "nose_to_tail"


def _load_interface_completeness(
    path: Path,
    *,
    source_sha256: str,
    expected_pairs: Sequence[Mapping[str, Any]],
) -> dict[frozenset[int], dict[str, Any]]:
    document = _load_json(path, "interface completeness evidence")
    if document.get("status") != "PASS" or document.get("source_unchanged") is not True:
        raise BoundarySemanticCandidateError(
            "interface completeness evidence did not pass unchanged-source checks"
        )
    if str(document.get("source_step_sha256", "")).lower() != source_sha256:
        raise BoundarySemanticCandidateError(
            "interface completeness evidence source SHA256 is stale"
        )
    rows = document.get("pairs")
    if not isinstance(rows, list) or not rows:
        raise BoundarySemanticCandidateError(
            "interface completeness evidence has no pairs"
        )
    result: dict[frozenset[int], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise BoundarySemanticCandidateError(
                "invalid interface completeness pair"
            )
        tags = _json_integer_list(
            row.get("surface_tags"), "interface completeness surface_tags"
        )
        key = frozenset(tags)
        if len(key) != 2 or key in result:
            raise BoundarySemanticCandidateError(
                "interface completeness pairs must be unique two-surface sets"
            )
        if row.get("status") != "FULL_GEOMETRIC_COINCIDENCE":
            raise BoundarySemanticCandidateError(
                "every interface pair must prove FULL_GEOMETRIC_COINCIDENCE"
            )
        projections = row.get("bidirectional_surface_projection")
        boundary = row.get("boundary_curve_matching")
        normals = row.get("matched_parametric_normal_opposition")
        if not isinstance(projections, Mapping) or projections.get("direction_count") != 2:
            raise BoundarySemanticCandidateError(
                "interface pair lacks bidirectional surface projection evidence"
            )
        if _finite_float(
            projections.get("maximum_distance_m"),
            "interface maximum projected distance",
        ) > _finite_float(
            projections.get("distance_tolerance_m"),
            "interface projection tolerance",
        ):
            raise BoundarySemanticCandidateError(
                "interface surface projection exceeds its tolerance"
            )
        if not isinstance(boundary, Mapping) or boundary.get("bijective") is not True:
            raise BoundarySemanticCandidateError(
                "interface boundary curves are not bijectively matched"
            )
        if _finite_float(
            boundary.get("maximum_sample_hausdorff_m"),
            "interface boundary Hausdorff distance",
        ) > _finite_float(
            boundary.get("distance_tolerance_m"),
            "interface boundary tolerance",
        ):
            raise BoundarySemanticCandidateError(
                "interface boundary matching exceeds its tolerance"
            )
        if not isinstance(normals, Mapping) or normals.get("opposite") is not True:
            raise BoundarySemanticCandidateError(
                "interface matched parametric normals are not opposite"
            )
        result[key] = json.loads(
            json.dumps(row, ensure_ascii=False, allow_nan=False)
        )
    expected = {
        frozenset(
            _json_integer_list(item.get("surface_tags"), "strong contact surfaces")
        )
        for item in expected_pairs
    }
    if set(result) != expected:
        raise BoundarySemanticCandidateError(
            "interface completeness pairs do not match strong contact candidates"
        )
    return result


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise BoundarySemanticCandidateError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundarySemanticCandidateError(f"{label} must be numeric") from error
    if not math.isfinite(number):
        raise BoundarySemanticCandidateError(f"{label} must be finite")
    return number


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise BoundarySemanticCandidateError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise BoundarySemanticCandidateError(f"{label} must be an integer") from error
    if str(value).strip() not in {str(number), f"{number}.0"}:
        raise BoundarySemanticCandidateError(f"{label} must be an integer")
    return number


def _json_vector(value: object, *, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise BoundarySemanticCandidateError(
            f"{label} must be a JSON array of length {length}"
        )
    return [_finite_float(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _json_integer_list(value: object, label: str) -> list[int]:
    if not isinstance(value, list):
        raise BoundarySemanticCandidateError(f"{label} must be a JSON array")
    result = [_integer(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise BoundarySemanticCandidateError(f"{label} contains duplicate values")
    return result


def _parse_json_cell(row: Mapping[str, str], field: str, tag: int) -> object:
    raw = row.get(field)
    if raw is None:
        raise BoundarySemanticCandidateError(
            f"surface {tag} is missing CSV field {field!r}"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise BoundarySemanticCandidateError(
            f"surface {tag} field {field!r} is not valid JSON"
        ) from error


def _load_surface_catalog(path: Path) -> dict[int, dict[str, Any]]:
    if not path.is_file():
        raise BoundarySemanticCandidateError(f"surface catalog does not exist: {path}")
    try:
        # Geometry review CSVs intentionally use UTF-8 with a BOM so they also
        # open cleanly in spreadsheet tools on Windows.
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, UnicodeError, csv.Error) as error:
        raise BoundarySemanticCandidateError(
            f"cannot read surface catalog {path}: {error}"
        ) from error
    if not rows:
        raise BoundarySemanticCandidateError("surface catalog is empty")
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        tag = _integer(row.get("entity_tag"), "surface entity_tag")
        if tag in result:
            raise BoundarySemanticCandidateError(f"duplicate surface tag {tag}")
        normals_raw = _parse_json_cell(row, "normal_samples", tag)
        if not isinstance(normals_raw, list) or not normals_raw:
            raise BoundarySemanticCandidateError(
                f"surface {tag} must contain normal samples"
            )
        normals = [
            _json_vector(item, length=3, label=f"surface {tag} normal")
            for item in normals_raw
        ]
        result[tag] = {
            "tag": tag,
            "step_name": str(row.get("step_name", "")),
            "area_m2": _finite_float(row.get("area_m2"), f"surface {tag} area"),
            "centroid_m": _json_vector(
                _parse_json_cell(row, "centroid_m", tag),
                length=3,
                label=f"surface {tag} centroid",
            ),
            "bounding_box_m": _json_vector(
                _parse_json_cell(row, "bounding_box_m", tag),
                length=6,
                label=f"surface {tag} bounding box",
            ),
            "normal_samples": normals,
            "adjacent_volumes": _json_integer_list(
                _parse_json_cell(row, "adjacent_volumes", tag),
                f"surface {tag} adjacent volumes",
            ),
            "boundary_curves": _json_integer_list(
                _parse_json_cell(row, "boundary_curves", tag),
                f"surface {tag} boundary curves",
            ),
        }
    return result


def _load_surface_types(path: Path) -> dict[int, str]:
    document = _load_json(path, "surface type evidence")
    if document.get("status") != "PASS":
        raise BoundarySemanticCandidateError("surface type evidence status is not PASS")
    rows = document.get("surfaces")
    if not isinstance(rows, list) or not rows:
        raise BoundarySemanticCandidateError("surface type evidence has no surfaces")
    result: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise BoundarySemanticCandidateError("invalid surface type record")
        tag = _integer(row.get("entity_tag"), "surface type entity_tag")
        entity_type = row.get("entity_type")
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise BoundarySemanticCandidateError(
                f"surface {tag} has no entity type"
            )
        if tag in result:
            raise BoundarySemanticCandidateError(
                f"duplicate surface type record {tag}"
            )
        result[tag] = entity_type.strip()
    return result


def _connected_components(
    tags: Iterable[int], records: Mapping[int, Mapping[str, Any]]
) -> list[list[int]]:
    remaining = set(tags)
    components: list[list[int]] = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        component = {seed}
        queue = [seed]
        while queue:
            current = queue.pop()
            curves = set(records[current]["boundary_curves"])
            neighbours = {
                tag
                for tag in remaining
                if curves.intersection(records[tag]["boundary_curves"])
            }
            component.update(neighbours)
            remaining.difference_update(neighbours)
            queue.extend(sorted(neighbours))
        components.append(sorted(component))
    return components


def _surface_payload(
    *,
    source_sha256: str,
    project_sha256: str,
    shell: Mapping[str, Any],
    record: Mapping[str, Any],
    entity_type: str,
) -> dict[str, Any]:
    return {
        "schema": _SURFACE_SCHEMA,
        "source_step_sha256": source_sha256,
        "source_project_sha256": project_sha256,
        "solid_shell": {
            "step_solid_id": shell["solid_id"],
            "step_solid_type": shell["solid_type"],
            "step_solid_name": shell["solid_name"],
            "step_root_shell_id": shell["root_shell_id"],
            "step_shell_role": shell["role_in_solid"],
        },
        "entity_type": entity_type,
        "area_m2": record["area_m2"],
        "centroid_m": record["centroid_m"],
        "bounding_box_m": record["bounding_box_m"],
        "boundary_curve_count": len(record["boundary_curves"]),
    }


def _group_record(
    *,
    source_sha256: str,
    project_sha256: str,
    geometric_class: str,
    shell: Mapping[str, Any],
    tags: Sequence[int],
    records: Mapping[int, Mapping[str, Any]],
    entity_types: Mapping[int, str],
) -> dict[str, Any]:
    member_rows: list[tuple[str, int, dict[str, Any]]] = []
    for tag in tags:
        payload = _surface_payload(
            source_sha256=source_sha256,
            project_sha256=project_sha256,
            shell=shell,
            record=records[tag],
            entity_type=entity_types[tag],
        )
        member_rows.append((_sha256(payload), tag, payload))
    member_rows.sort(key=lambda item: item[0])
    member_ids = [item[0] for item in member_rows]
    if len(member_ids) != len(set(member_ids)):
        raise BoundarySemanticCandidateError(
            "group contains duplicate geometric surface fingerprints"
        )
    id_by_tag = {tag: fingerprint_id for fingerprint_id, tag, _ in member_rows}
    shared_edges: list[dict[str, Any]] = []
    for index, first in enumerate(tags):
        first_curves = set(records[first]["boundary_curves"])
        for second in tags[index + 1 :]:
            shared_count = len(
                first_curves.intersection(records[second]["boundary_curves"])
            )
            if shared_count:
                shared_edges.append(
                    {
                        "member_fingerprint_ids": sorted(
                            [id_by_tag[first], id_by_tag[second]]
                        ),
                        "shared_boundary_curve_count": shared_count,
                    }
                )
    shared_edges.sort(
        key=lambda item: tuple(item["member_fingerprint_ids"])
    )
    areas = [float(records[tag]["area_m2"]) for tag in tags]
    total_area = sum(areas)
    if total_area <= 0.0:
        raise BoundarySemanticCandidateError("group area must be positive")
    centroid = [
        sum(
            float(records[tag]["area_m2"])
            * float(records[tag]["centroid_m"][axis])
            for tag in tags
        )
        / total_area
        for axis in range(3)
    ]
    bounds = [
        min(float(records[tag]["bounding_box_m"][axis]) for tag in tags)
        for axis in range(3)
    ] + [
        max(float(records[tag]["bounding_box_m"][axis]) for tag in tags)
        for axis in range(3, 6)
    ]
    curve_counts = Counter(
        curve for tag in tags for curve in records[tag]["boundary_curves"]
    )
    parametric_normal_x = [
        float(normal[0])
        for tag in tags
        for normal in records[tag]["normal_samples"]
    ]
    shell_fingerprint = {
        "step_solid_id": shell["solid_id"],
        "step_solid_type": shell["solid_type"],
        "step_solid_name": shell["solid_name"],
        "step_root_shell_id": shell["root_shell_id"],
        "step_shell_role": shell["role_in_solid"],
    }
    payload = {
        "schema": _GROUP_SCHEMA,
        "source_step_sha256": source_sha256,
        "source_project_sha256": project_sha256,
        "geometric_class": geometric_class,
        "solid_shell": shell_fingerprint,
        "member_surface_fingerprint_ids": member_ids,
        "member_count": len(tags),
        "entity_type_histogram": dict(sorted(Counter(entity_types[tag] for tag in tags).items())),
        "total_area_m2": total_area,
        "area_weighted_centroid_m": centroid,
        "bounding_box_m": bounds,
        "connectivity_edges": shared_edges,
        "external_boundary_curve_count": sum(
            count == 1 for count in curve_counts.values()
        ),
        "internal_shared_curve_multiplicities": sorted(
            count for count in curve_counts.values() if count > 1
        ),
    }
    return {
        "geometry_status": "CONFIRMED_GEOMETRY",
        "business_role_status": "CANDIDATE_ROLE",
        "group_fingerprint_id": _sha256(payload),
        "fingerprint": payload,
        "members": [
            {
                "surface_fingerprint_id": fingerprint_id,
                "fingerprint": member_payload,
                "audit": {
                    "gmsh_surface_entity_tag": tag,
                    "tag_is_matching_criterion": False,
                    "boundary_curve_tags": list(records[tag]["boundary_curves"]),
                    "boundary_curve_tags_are_matching_criteria": False,
                },
            }
            for fingerprint_id, tag, member_payload in member_rows
        ],
        "audit": {
            "current_surface_tags": sorted(tags),
            "current_volume_entity_tag": shell["volume_entity_tag"],
            "gmsh_tags_are_matching_criteria": False,
            "parametric_normal_x_range": [
                min(parametric_normal_x),
                max(parametric_normal_x),
            ],
            "parametric_normals_are_outward_interpretation": False,
        },
    }


def _interface_record(
    *,
    source_sha256: str,
    project_sha256: str,
    pair: Mapping[str, Any],
    surface_payloads: Mapping[int, Mapping[str, Any]],
    completeness: Mapping[str, Any],
) -> dict[str, Any]:
    raw_tags = pair.get("surface_tags")
    if not isinstance(raw_tags, list) or len(raw_tags) != 2:
        raise BoundarySemanticCandidateError(
            "strong contact candidate must contain exactly two surfaces"
        )
    tags = [_integer(item, "interface surface tag") for item in raw_tags]
    if tags[0] == tags[1] or any(tag not in surface_payloads for tag in tags):
        raise BoundarySemanticCandidateError("interface candidate has invalid surfaces")
    member_ids = sorted(_sha256(surface_payloads[tag]) for tag in tags)
    payload = {
        "schema": _INTERFACE_SCHEMA,
        "source_step_sha256": source_sha256,
        "source_project_sha256": project_sha256,
        "member_surface_fingerprint_ids": member_ids,
        "relative_area_difference": _finite_float(
            pair.get("relative_area_difference"), "interface relative area difference"
        ),
        "centroid_distance_m": _finite_float(
            pair.get("centroid_distance_m"), "interface centroid distance"
        ),
        "bbox_gap_m": _finite_float(pair.get("bbox_gap_m"), "interface bbox gap"),
        "occ_minimum_distance_m": _finite_float(
            pair.get("occ_minimum_distance_m"), "interface OCC distance"
        ),
        "full_coincidence_evidence_sha256": _sha256(completeness),
    }
    return {
        "geometry_status": "CONFIRMED_FULL_GEOMETRIC_COINCIDENCE",
        "full_surface_coincidence_claimed": True,
        "interface_pair_fingerprint_id": _sha256(payload),
        "fingerprint": payload,
        "completeness_evidence": dict(completeness),
        "audit": {
            "current_surface_tags": sorted(tags),
            "gmsh_tags_are_matching_criteria": False,
        },
    }


def _destination(path: PathLike) -> Path:
    destination = Path(path).resolve()
    if destination.suffix.lower() != ".json":
        raise BoundarySemanticCandidateError("output path must end in .json")
    if any(part.casefold() == "config" for part in destination.parts):
        raise BoundarySemanticCandidateError(
            "boundary candidate evidence cannot be written under config"
        )
    if destination.exists():
        raise BoundarySemanticCandidateError(
            f"refusing to overwrite existing evidence: {destination}"
        )
    return destination


def _axial_surface_observations(
    *,
    records: Mapping[int, Mapping[str, Any]],
    entity_types: Mapping[int, str],
    shell_by_surface: Mapping[int, Mapping[str, Any]],
    coordinate_tolerance_m: float = 1.0e-5,
) -> list[dict[str, Any]]:
    """Describe, but do not semantically label, near-constant-X subgroups."""

    eligible = [
        tag
        for tag, record in records.items()
        if float(record["bounding_box_m"][3])
        - float(record["bounding_box_m"][0])
        <= coordinate_tolerance_m
    ]
    observations: list[dict[str, Any]] = []
    by_shell: dict[tuple[int, int], list[int]] = {}
    for tag in eligible:
        shell = shell_by_surface[tag]
        key = (int(shell["solid_id"]), int(shell["root_shell_id"]))
        by_shell.setdefault(key, []).append(tag)
    for tags in by_shell.values():
        for component in _connected_components(tags, records):
            shell = shell_by_surface[component[0]]
            areas = [float(records[tag]["area_m2"]) for tag in component]
            total_area = sum(areas)
            centroid = [
                sum(
                    float(records[tag]["area_m2"])
                    * float(records[tag]["centroid_m"][axis])
                    for tag in component
                )
                / total_area
                for axis in range(3)
            ]
            bounds = [
                min(float(records[tag]["bounding_box_m"][axis]) for tag in component)
                for axis in range(3)
            ] + [
                max(float(records[tag]["bounding_box_m"][axis]) for tag in component)
                for axis in range(3, 6)
            ]
            normal_x = [
                float(normal[0])
                for tag in component
                for normal in records[tag]["normal_samples"]
            ]
            observations.append(
                {
                    "geometry_status": "CONFIRMED_GEOMETRY",
                    "business_role_status": "UNASSIGNED",
                    "step_shell_role": shell["role_in_solid"],
                    "member_count": len(component),
                    "entity_type_histogram": dict(
                        sorted(Counter(entity_types[tag] for tag in component).items())
                    ),
                    "total_area_m2": total_area,
                    "area_weighted_centroid_m": centroid,
                    "bounding_box_m": bounds,
                    "parametric_normal_x_range": [min(normal_x), max(normal_x)],
                    "audit": {
                        "current_surface_tags": sorted(component),
                        "gmsh_tags_are_matching_criteria": False,
                        "parametric_normals_are_outward_interpretation": False,
                    },
                }
            )
    observations.sort(
        key=lambda item: (
            float(item["area_weighted_centroid_m"][0]),
            str(item["step_shell_role"]),
        )
    )
    return observations


def _write_new_json(destination: Path, document: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_boundary_semantic_candidates(
    surface_catalog_path: PathLike,
    surface_types_path: PathLike,
    step_topology_path: PathLike,
    output_path: PathLike | None = None,
    *,
    project_path: PathLike,
    interface_completeness_path: PathLike,
    coordinate_tolerance_m: float = 1.0e-5,
) -> dict[str, Any]:
    """Return conservative complete-shell boundary candidates.

    The only semantic output is a set of hypotheses requiring explicit human
    confirmation.  In particular, numbered rear outlets are never inferred
    from a member surface's centroid or sign of Z.
    """

    coordinate_tolerance = _finite_float(
        coordinate_tolerance_m, "coordinate_tolerance_m"
    )
    if coordinate_tolerance <= 0.0:
        raise BoundarySemanticCandidateError(
            "coordinate_tolerance_m must be positive"
        )
    catalog_path = Path(surface_catalog_path).resolve()
    types_path = Path(surface_types_path).resolve()
    topology_path = Path(step_topology_path).resolve()
    project_config_path = Path(project_path).resolve()
    interface_path = Path(interface_completeness_path).resolve()
    project_sha256, x_positive = _load_project_axes(project_config_path)
    records = _load_surface_catalog(catalog_path)
    entity_types = _load_surface_types(types_path)
    topology = _load_json(topology_path, "STEP topology evidence")
    source_sha256 = topology.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise BoundarySemanticCandidateError(
            "STEP topology evidence has no valid source_sha256"
        )
    source_sha256 = source_sha256.lower()
    if set(records) != set(entity_types):
        raise BoundarySemanticCandidateError(
            "surface catalog and type evidence contain different entity sets"
        )

    solids_raw = topology.get("solids")
    if not isinstance(solids_raw, list) or not solids_raw:
        raise BoundarySemanticCandidateError("STEP topology evidence has no solids")
    solids: dict[int, Mapping[str, Any]] = {}
    for item in solids_raw:
        if not isinstance(item, Mapping):
            raise BoundarySemanticCandidateError("invalid STEP solid record")
        solid_id = _integer(item.get("id"), "STEP solid id")
        solids[solid_id] = item

    shell_rows = topology.get("shell_component_mapping")
    if not isinstance(shell_rows, list) or not shell_rows:
        raise BoundarySemanticCandidateError(
            "STEP topology evidence has no shell component mapping"
        )
    shells: list[dict[str, Any]] = []
    shell_by_surface: dict[int, dict[str, Any]] = {}
    for raw in shell_rows:
        if not isinstance(raw, Mapping) or raw.get("status") != "MATCHED":
            raise BoundarySemanticCandidateError(
                "every STEP shell component must be uniquely MATCHED"
            )
        solid_id = _integer(raw.get("solid_id"), "shell solid id")
        solid = solids.get(solid_id)
        if solid is None:
            raise BoundarySemanticCandidateError(
                f"shell references unknown STEP solid {solid_id}"
            )
        tags = _json_integer_list(raw.get("surface_tags"), "shell surface_tags")
        shell = {
            "volume_entity_tag": _integer(
                raw.get("volume_entity_tag"), "shell volume entity tag"
            ),
            "solid_id": solid_id,
            "solid_type": str(solid.get("type", "")),
            "solid_name": str(solid.get("name", "")),
            "root_shell_id": _integer(raw.get("root_shell_id"), "root shell id"),
            "role_in_solid": str(raw.get("role_in_solid", "")),
            "surface_tags": tags,
        }
        if not shell["solid_type"] or not shell["role_in_solid"]:
            raise BoundarySemanticCandidateError("shell identity is incomplete")
        for tag in tags:
            if tag not in records:
                raise BoundarySemanticCandidateError(
                    f"shell references unknown surface {tag}"
                )
            if tag in shell_by_surface:
                raise BoundarySemanticCandidateError(
                    f"surface {tag} belongs to multiple shell components"
                )
            shell_by_surface[tag] = shell
        shells.append(shell)
    if set(shell_by_surface) != set(records):
        raise BoundarySemanticCandidateError(
            "shell component mapping does not cover every surface exactly once"
        )

    surface_payloads = {
        tag: _surface_payload(
            source_sha256=source_sha256,
            project_sha256=project_sha256,
            shell=shell_by_surface[tag],
            record=record,
            entity_type=entity_types[tag],
        )
        for tag, record in records.items()
    }
    contact_rows = topology.get("strong_contact_candidates")
    if not isinstance(contact_rows, list) or not contact_rows:
        raise BoundarySemanticCandidateError(
            "STEP topology evidence has no strong cross-volume contact candidates"
        )
    completeness_by_pair = _load_interface_completeness(
        interface_path,
        source_sha256=source_sha256,
        expected_pairs=[item for item in contact_rows if isinstance(item, Mapping)],
    )
    interfaces = [
        _interface_record(
            source_sha256=source_sha256,
            project_sha256=project_sha256,
            pair=item,
            surface_payloads=surface_payloads,
            completeness=completeness_by_pair[
                frozenset(
                    _json_integer_list(
                        item.get("surface_tags"), "strong contact surfaces"
                    )
                )
            ],
        )
        for item in contact_rows
        if isinstance(item, Mapping)
    ]
    if len(interfaces) != len(contact_rows):
        raise BoundarySemanticCandidateError("invalid strong contact candidate")
    interface_tags: set[int] = set()
    for item in interfaces:
        tags = set(item["audit"]["current_surface_tags"])
        if interface_tags.intersection(tags):
            raise BoundarySemanticCandidateError(
                "a surface appears in multiple interface candidates"
            )
        first, second = sorted(tags)
        if (
            shell_by_surface[first]["volume_entity_tag"]
            == shell_by_surface[second]["volume_entity_tag"]
        ):
            raise BoundarySemanticCandidateError(
                "interface candidate does not cross two volumes"
            )
        interface_tags.update(tags)

    upstream: list[dict[str, Any]] = []
    downstream: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    void_shell: list[dict[str, Any]] = []
    for shell in shells:
        exposed = [tag for tag in shell["surface_tags"] if tag not in interface_tags]
        if not exposed:
            continue
        if shell["role_in_solid"] == "void_shell":
            for component in _connected_components(exposed, records):
                void_shell.append(
                    _group_record(
                        source_sha256=source_sha256,
                        project_sha256=project_sha256,
                        geometric_class="void_shell_boundary",
                        shell=shell,
                        tags=component,
                        records=records,
                        entity_types=entity_types,
                    )
                )
            continue
        shell_interfaces = [
            tag for tag in shell["surface_tags"] if tag in interface_tags
        ]
        if not shell_interfaces:
            for component in _connected_components(exposed, records):
                unresolved.append(
                    _group_record(
                        source_sha256=source_sha256,
                        project_sha256=project_sha256,
                        geometric_class="unresolved_nonvoid_shell_without_interface",
                        shell=shell,
                        tags=component,
                        records=records,
                        entity_types=entity_types,
                    )
                )
            continue
        interface_x_min = min(
            float(records[tag]["bounding_box_m"][0])
            for tag in shell_interfaces
        )
        interface_x_max = max(
            float(records[tag]["bounding_box_m"][3])
            for tag in shell_interfaces
        )
        interface_curves = {
            curve
            for tag in shell_interfaces
            for curve in records[tag]["boundary_curves"]
        }
        continuation = [
            tag
            for tag in exposed
            if float(records[tag]["bounding_box_m"][0])
            >= interface_x_max - coordinate_tolerance
        ]
        remaining = [tag for tag in exposed if tag not in set(continuation)]
        interface_edge_closure = [
            tag
            for tag in remaining
            if interface_curves.intersection(records[tag]["boundary_curves"])
            and abs(
                float(records[tag]["bounding_box_m"][3]) - interface_x_max
            )
            <= coordinate_tolerance
        ]
        lower_x = [
            tag for tag in remaining if tag not in set(interface_edge_closure)
        ]
        for component in _connected_components(continuation, records):
            downstream.append(
                _group_record(
                    source_sha256=source_sha256,
                    project_sha256=project_sha256,
                    geometric_class="positive_x_continuation_from_interface",
                    shell=shell,
                    tags=component,
                    records=records,
                    entity_types=entity_types,
                )
            )
        for component in _connected_components(interface_edge_closure, records):
            downstream.append(
                _group_record(
                    source_sha256=source_sha256,
                    project_sha256=project_sha256,
                    geometric_class="maximum_x_interface_edge_closure",
                    shell=shell,
                    tags=component,
                    records=records,
                    entity_types=entity_types,
                )
            )
        for component in _connected_components(lower_x, records):
            component_x_min = min(
                float(records[tag]["bounding_box_m"][0]) for tag in component
            )
            component_x_max = max(
                float(records[tag]["bounding_box_m"][3]) for tag in component
            )
            target = upstream
            geometric_class = "lower_x_outer_shell_before_interface_edge"
            if not (
                component_x_min < interface_x_min - coordinate_tolerance
                and component_x_max <= interface_x_max + coordinate_tolerance
            ):
                target = unresolved
                geometric_class = "unresolved_outer_shell_relative_x_extent"
            target.append(
                _group_record(
                    source_sha256=source_sha256,
                    project_sha256=project_sha256,
                    geometric_class=geometric_class,
                    shell=shell,
                    tags=component,
                    records=records,
                    entity_types=entity_types,
                )
            )

    all_groups = [*upstream, *downstream, *unresolved, *void_shell]
    grouped_tags = [
        tag
        for group in all_groups
        for tag in group["audit"]["current_surface_tags"]
    ]
    assigned_tags = grouped_tags + sorted(interface_tags)
    if len(assigned_tags) != len(set(assigned_tags)) or set(assigned_tags) != set(records):
        raise BoundarySemanticCandidateError(
            "candidate groups and interfaces do not partition all surfaces exactly once"
        )

    downstream_ids = sorted(
        group["group_fingerprint_id"] for group in downstream
    )
    report: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "status": "PASS",
        "classification_status": "HUMAN_CONFIRMATION_REQUIRED",
        "marker_configuration_ready": False,
        "business_semantics_asserted": False,
        "source": {
            "step_sha256": source_sha256,
            "project_sha256": project_sha256,
            "x_positive": x_positive,
            "surface_catalog_path": str(catalog_path),
            "surface_types_path": str(types_path),
            "step_topology_path": str(topology_path),
            "project_path": str(project_config_path),
            "interface_completeness_path": str(interface_path),
        },
        "geometric_candidates": {
            "upstream_outer_groups": upstream,
            "downstream_outer_groups": downstream,
            "transverse_or_mixed_outer_groups": unresolved,
            "void_shell_boundary_groups": void_shell,
            "near_coincident_interface_pairs": interfaces,
        },
        "axial_surface_observations": _axial_surface_observations(
            records=records,
            entity_types=entity_types,
            shell_by_surface=shell_by_surface,
        ),
        "role_hypotheses": {
            "farfield": {
                "status": "CANDIDATE",
                "candidate_group_fingerprint_ids": sorted(
                    group["group_fingerprint_id"] for group in upstream
                ),
                "basis": "complete external non-void shell group at lower X, separated by interface-edge incidence under project +X nose-to-tail",
            },
            "wall": {
                "status": "CANDIDATE",
                "candidate_group_fingerprint_ids": sorted(
                    group["group_fingerprint_id"] for group in void_shell
                ),
                "basis": "STEP BREP_WITH_VOIDS void-shell membership; project role still requires confirmation",
            },
            "rear_outlets": {
                "status": "CANDIDATE_NUMBERING_PENDING",
                "candidate_group_fingerprint_ids": downstream_ids,
                "basis": "complete external non-void shell groups that either continue beyond interface maximum X or close its maximum-X edge",
            },
        },
        "rear_outlet_numbering_contract": {
            "status": "PENDING_HUMAN_CONFIRMATION",
            "strategy": "explicit_group_fingerprint_mapping",
            "required_role_count": 2,
            "candidate_group_fingerprint_ids": downstream_ids,
            "forbidden_inference": "single_surface_centroid_z",
            "requirements": [
                "Map each numbered rear outlet to one complete group fingerprint.",
                "Use each candidate group exactly once.",
                "Reject zero, multiple, stale, partial, duplicate or extra group matches.",
                "Do not infer numbering from Gmsh tags or member-surface centroid signs.",
            ],
        },
        "partition_audit": {
            "surface_count": len(records),
            "assigned_surface_count": len(assigned_tags),
            "all_surfaces_assigned_once": True,
            "current_interface_surface_tags": sorted(interface_tags),
            "current_group_surface_tags": sorted(grouped_tags),
            "gmsh_tags_are_matching_criteria": False,
            "parametric_normals_used_for_role_classification": False,
        },
        "policy": {
            "original_cad_modified": False,
            "gmsh_imported": False,
            "mesh_generated": False,
            "solver_run": False,
            "formal_markers_written": False,
            "geometry_class_is_not_business_role": True,
            "fail_closed_until_human_mapping": True,
            "parametric_gmsh_normals_are_not_treated_as_outward_normals": True,
            "classification_uses_shell_interface_curve_incidence_and_relative_x_extent": True,
        },
    }
    if output_path is not None:
        _write_new_json(_destination(output_path), report)
    return report


__all__ = [
    "BoundarySemanticCandidateError",
    "build_boundary_semantic_candidates",
]
