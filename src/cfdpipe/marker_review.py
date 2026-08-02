"""Conservative, evidence-backed review of candidate CFD boundary markers.

The review is intentionally not a marker generator.  It joins already-created
read-only geometry evidence, reports what can and cannot be established, and
never writes ``config/markers.toml``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import tomllib
from typing import Any


PathLike = str | os.PathLike[str]
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_STATUSES = frozenset({"CONFIRMED", "CANDIDATE", "AMBIGUOUS", "MISSING"})
_SHA256_LENGTH = 64


class MarkerReviewError(ValueError):
    """Raised when marker-review evidence is unsafe, stale or inconsistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        information = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(information, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_allowed_root(path: PathLike) -> Path:
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir() or _is_reparse(root):
        raise MarkerReviewError(f"allowed output root is not a safe directory: {root}")
    return root


def _validate_file(
    path: PathLike,
    *,
    label: str,
    allowed_root: Path,
    allow_project_config: bool = False,
) -> tuple[Path, dict[str, Any]]:
    candidate = Path(path).expanduser().resolve(strict=True)
    config_root = (REPOSITORY_ROOT / "config").resolve()
    allowed = _is_within(candidate, allowed_root)
    if allow_project_config:
        allowed = allowed or _is_within(candidate, config_root)
    if not allowed:
        raise MarkerReviewError(f"{label} is outside its allowed root: {candidate}")
    if not candidate.is_file() or _is_reparse(candidate):
        raise MarkerReviewError(f"{label} is not a safe regular file: {candidate}")
    return candidate, {
        "path": str(candidate),
        "size_bytes": candidate.stat().st_size,
        "sha256": _sha256(candidate),
    }


def _validate_output_path(
    output_directory: PathLike, allowed_root: Path
) -> Path:
    output = Path(output_directory).expanduser().resolve()
    if not _is_within(output, allowed_root):
        raise MarkerReviewError(f"output directory escapes allowed root: {output}")
    current = output
    while _is_within(current, allowed_root):
        if current.exists() and _is_reparse(current):
            raise MarkerReviewError(f"output path contains a reparse point: {current}")
        if current == allowed_root:
            break
        current = current.parent
    return output


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MarkerReviewError(f"cannot parse {label}: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise MarkerReviewError(f"{label} must contain a JSON object")
    return payload


def _require_upstream_pass(payload: Mapping[str, Any], label: str) -> None:
    if payload.get("status") != "PASS":
        raise MarkerReviewError(
            f"{label} is not PASS: declared status={payload.get('status')!r}"
        )


def _json_integer_list(value: str, *, field: str, entity_tag: int) -> list[int]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise MarkerReviewError(
            f"surface {entity_tag} has invalid {field} JSON"
        ) from error
    if not isinstance(parsed, list):
        raise MarkerReviewError(f"surface {entity_tag} {field} must be a JSON list")
    result: list[int] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise MarkerReviewError(
                f"surface {entity_tag} {field} contains invalid tag {item!r}"
            )
        result.append(item)
    if len(result) != len(set(result)):
        raise MarkerReviewError(f"surface {entity_tag} {field} contains duplicates")
    return sorted(result)


def _json_float_list(value: str, *, field: str, entity_tag: int) -> list[float]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise MarkerReviewError(
            f"surface {entity_tag} has invalid {field} JSON"
        ) from error
    if not isinstance(parsed, list):
        raise MarkerReviewError(f"surface {entity_tag} {field} must be a JSON list")
    result: list[float] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise MarkerReviewError(
                f"surface {entity_tag} {field} contains a non-number"
            )
        number = float(item)
        if not math.isfinite(number):
            raise MarkerReviewError(
                f"surface {entity_tag} {field} contains a non-finite number"
            )
        result.append(number)
    return result


def _load_surface_catalog(path: Path) -> list[dict[str, Any]]:
    required = {
        "entity_tag",
        "step_name",
        "area_m2",
        "centroid_m",
        "bounding_box_m",
        "normal_samples",
        "adjacent_volumes",
        "boundary_curves",
    }
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or ()))
            raise MarkerReviewError(f"surface catalog is missing columns: {missing}")
        surfaces: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row_number, row in enumerate(reader, start=2):
            try:
                entity_tag = int(row["entity_tag"])
                area = float(row["area_m2"])
            except (TypeError, ValueError) as error:
                raise MarkerReviewError(
                    f"surface catalog row {row_number} has invalid numeric fields"
                ) from error
            if entity_tag <= 0 or entity_tag in seen:
                raise MarkerReviewError(
                    f"surface catalog has invalid or duplicate tag {entity_tag}"
                )
            if not math.isfinite(area) or area <= 0.0:
                raise MarkerReviewError(f"surface {entity_tag} has invalid area")
            seen.add(entity_tag)
            centroid = _json_float_list(
                row["centroid_m"], field="centroid_m", entity_tag=entity_tag
            )
            bounds = _json_float_list(
                row["bounding_box_m"],
                field="bounding_box_m",
                entity_tag=entity_tag,
            )
            if len(centroid) != 3 or len(bounds) != 6:
                raise MarkerReviewError(
                    f"surface {entity_tag} centroid/bounding-box dimensions are invalid"
                )
            surfaces.append(
                {
                    "entity_tag": entity_tag,
                    "step_name": str(row.get("step_name") or "").strip(),
                    "area_m2": area,
                    "centroid_m": centroid,
                    "bounding_box_m": bounds,
                    "normal_samples": row["normal_samples"],
                    "adjacent_volumes": _json_integer_list(
                        row["adjacent_volumes"],
                        field="adjacent_volumes",
                        entity_tag=entity_tag,
                    ),
                    "boundary_curves": _json_integer_list(
                        row["boundary_curves"],
                        field="boundary_curves",
                        entity_tag=entity_tag,
                    ),
                }
            )
    if not surfaces:
        raise MarkerReviewError("surface catalog contains no surfaces")
    return sorted(surfaces, key=lambda item: item["entity_tag"])


def _project_roles(project_path: Path) -> tuple[list[dict[str, str]], bool]:
    try:
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise MarkerReviewError(f"cannot parse project TOML: {error}") from error
    boundaries = project.get("boundaries")
    if not isinstance(boundaries, Mapping):
        raise MarkerReviewError("project.toml is missing [boundaries]")

    def required_string(key: str) -> str:
        value = boundaries.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MarkerReviewError(f"project boundary {key!r} must be non-empty")
        return value.strip()

    farfield = required_string("farfield_role")
    wall = required_string("wall_role")
    measurement = required_string("measurement_surface_role")
    raw_rear = boundaries.get("rear_outlet_roles")
    if not isinstance(raw_rear, list) or not raw_rear:
        raise MarkerReviewError("rear_outlet_roles must be a non-empty TOML array")
    rear: list[str] = []
    for value in raw_rear:
        if not isinstance(value, str) or not value.strip():
            raise MarkerReviewError("rear outlet role names must be non-empty")
        rear.append(value.strip())
    roles = [
        {"role": farfield, "kind": "farfield"},
        {"role": wall, "kind": "wall"},
        *({"role": value, "kind": "rear_outlet"} for value in rear),
        {"role": measurement, "kind": "measurement_surface"},
    ]
    names = [item["role"].casefold() for item in roles]
    if len(names) != len(set(names)):
        raise MarkerReviewError("required project boundary roles are not unique")
    measurement_is_boundary = boundaries.get(
        "measurement_surface_is_solver_boundary", False
    )
    if not isinstance(measurement_is_boundary, bool):
        raise MarkerReviewError(
            "measurement_surface_is_solver_boundary must be boolean"
        )
    return roles, measurement_is_boundary


def _validate_surface_types(
    payload: Mapping[str, Any], surfaces: Sequence[Mapping[str, Any]]
) -> dict[int, dict[str, Any]]:
    raw_surfaces = payload.get("surfaces")
    if not isinstance(raw_surfaces, list):
        raise MarkerReviewError("surface_types.surfaces must be a list")
    by_tag: dict[int, dict[str, Any]] = {}
    catalog_by_tag = {int(item["entity_tag"]): item for item in surfaces}
    for index, item in enumerate(raw_surfaces):
        if not isinstance(item, Mapping):
            raise MarkerReviewError(f"surface_types.surfaces[{index}] is invalid")
        tag = item.get("entity_tag")
        if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
            raise MarkerReviewError("surface_types contains an invalid entity tag")
        if tag in by_tag:
            raise MarkerReviewError(f"surface_types contains duplicate tag {tag}")
        adjacent = item.get("adjacent_volumes")
        if not isinstance(adjacent, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in adjacent
        ):
            raise MarkerReviewError(
                f"surface_types surface {tag} has invalid adjacent_volumes"
            )
        by_tag[tag] = dict(item)
    if set(by_tag) != set(catalog_by_tag):
        raise MarkerReviewError("surface_types and surface_catalog entity tags differ")
    for tag, item in by_tag.items():
        if sorted(set(item["adjacent_volumes"])) != catalog_by_tag[tag][
            "adjacent_volumes"
        ]:
            raise MarkerReviewError(
                f"surface_types and catalog disagree on surface {tag} adjacency"
            )
    return by_tag


def _build_shell_components(
    surfaces: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    volumes = sorted(
        {
            int(volume)
            for surface in surfaces
            for volume in surface["adjacent_volumes"]
        }
    )
    components: list[dict[str, Any]] = []
    component_number = 0
    for volume in volumes:
        owned = {
            int(surface["entity_tag"]): surface
            for surface in surfaces
            if volume in surface["adjacent_volumes"]
        }
        graph = {tag: set() for tag in owned}
        curves: dict[int, list[int]] = defaultdict(list)
        for tag, surface in owned.items():
            for curve in surface["boundary_curves"]:
                curves[int(curve)].append(tag)
        for tags in curves.values():
            for first in tags:
                graph[first].update(tag for tag in tags if tag != first)

        unseen = set(graph)
        volume_component_index = 0
        while unseen:
            seed = min(unseen)
            stack = [seed]
            found: set[int] = set()
            while stack:
                tag = stack.pop()
                if tag in found:
                    continue
                found.add(tag)
                unseen.discard(tag)
                stack.extend(sorted(graph[tag] - found, reverse=True))
            volume_component_index += 1
            component_number += 1
            tags = sorted(found)
            names = sorted(
                {
                    str(owned[tag]["step_name"])
                    for tag in tags
                    if str(owned[tag]["step_name"])
                }
            )
            components.append(
                {
                    "component_id": f"volume_{volume}_shell_{volume_component_index}",
                    "component_index": component_number,
                    "volume_entity_tag": volume,
                    "surface_tags": tags,
                    "surface_count": len(tags),
                    "boundary_curve_tags": sorted(
                        {
                            int(curve)
                            for tag in tags
                            for curve in owned[tag]["boundary_curves"]
                        }
                    ),
                    "persistent_step_names": names,
                    "exposed_surface_tags": [
                        tag
                        for tag in tags
                        if len(owned[tag]["adjacent_volumes"]) == 1
                    ],
                    "internal_surface_tags": [
                        tag
                        for tag in tags
                        if len(owned[tag]["adjacent_volumes"]) >= 2
                    ],
                }
            )
    return components


def _match_step_shell_components(
    topology: Mapping[str, Any],
    mapping: Mapping[str, Any],
    catalog_components: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    solids = {
        int(item["id"]): item
        for item in topology.get("solids", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), int)
    }
    components_by_volume: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for component in catalog_components:
        components_by_volume[int(component["volume_entity_tag"])].append(component)

    evidence: list[dict[str, Any]] = []
    for match in mapping.get("matches", []):
        if not isinstance(match, Mapping):
            raise MarkerReviewError("solid-volume mapping contains an invalid match")
        solid_id = match.get("solid_id")
        volume_tag = match.get("volume_entity_tag")
        if not isinstance(solid_id, int) or not isinstance(volume_tag, int):
            raise MarkerReviewError("solid-volume match ids must be integers")
        solid = solids.get(solid_id)
        if solid is None:
            raise MarkerReviewError(
                f"solid-volume mapping references unknown STEP solid {solid_id}"
            )
        shell_components = solid.get("shell_components")
        if not isinstance(shell_components, list) or not shell_components:
            raise MarkerReviewError(
                f"STEP solid {solid_id} lacks root shell component evidence"
            )
        step_by_count: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        catalog_by_count: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for item in shell_components:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("face_count"), int
            ):
                raise MarkerReviewError(
                    f"STEP solid {solid_id} has invalid shell component evidence"
                )
            step_by_count[int(item["face_count"])].append(item)
        for item in components_by_volume.get(volume_tag, []):
            catalog_by_count[int(item["surface_count"])].append(item)
        for count in sorted(set(step_by_count) | set(catalog_by_count)):
            step_items = step_by_count.get(count, [])
            catalog_items = catalog_by_count.get(count, [])
            if len(step_items) == len(catalog_items) == 1:
                status = "CONFIRMED"
            elif step_items and catalog_items:
                status = "AMBIGUOUS"
            else:
                status = "MISSING"
            if step_items:
                for item in step_items:
                    evidence.append(
                        {
                            "status": status,
                            "solid_id": solid_id,
                            "volume_entity_tag": volume_tag,
                            "root_shell_id": item.get("root_shell_id"),
                            "step_role_in_solid": item.get("role_in_solid"),
                            "face_count": count,
                            "candidate_catalog_component_ids": [
                                candidate["component_id"]
                                for candidate in catalog_items
                            ],
                            "cfd_role_inference": "none",
                            "basis": (
                                "unique topology face-count correspondence; STEP shell "
                                "semantics are not CFD boundary roles"
                            ),
                        }
                    )
            else:
                evidence.append(
                    {
                        "status": "MISSING",
                        "solid_id": solid_id,
                        "volume_entity_tag": volume_tag,
                        "root_shell_id": None,
                        "step_role_in_solid": None,
                        "face_count": count,
                        "candidate_catalog_component_ids": [
                            candidate["component_id"] for candidate in catalog_items
                        ],
                        "cfd_role_inference": "none",
                        "basis": "catalog component has no STEP root-shell count peer",
                    }
                )
    return evidence


def _contact_candidates(
    surface_types: Mapping[str, Any],
    surfaces_by_tag: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    distance = surface_types.get("distance_evidence")
    if not isinstance(distance, Mapping):
        return []
    pairs = distance.get("surface_pairs")
    if not isinstance(pairs, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in pairs:
        if not isinstance(item, Mapping):
            continue
        raw_a = item.get("entity_a")
        raw_b = item.get("entity_b")
        if isinstance(raw_a, list) and len(raw_a) == 2:
            tag_a = raw_a[1]
        else:
            tag_a = item.get("surface_a")
        if isinstance(raw_b, list) and len(raw_b) == 2:
            tag_b = raw_b[1]
        else:
            tag_b = item.get("surface_b")
        if not isinstance(tag_a, int) or not isinstance(tag_b, int):
            continue
        first = surfaces_by_tag.get(tag_a)
        second = surfaces_by_tag.get(tag_b)
        if first is None or second is None:
            raise MarkerReviewError("distance evidence references an unknown surface")
        owners_a = set(first["adjacent_volumes"])
        owners_b = set(second["adjacent_volumes"])
        if not owners_a or not owners_b or owners_a & owners_b:
            continue
        raw_distance = item.get("distance_m", item.get("distance"))
        if not isinstance(raw_distance, (int, float)) or isinstance(raw_distance, bool):
            raise MarkerReviewError("contact candidate has an invalid distance")
        distance_m = float(raw_distance)
        if not math.isfinite(distance_m) or distance_m < 0.0:
            raise MarkerReviewError("contact candidate distance is not finite/nonnegative")
        prescreen = item.get("prescreen")
        if not isinstance(prescreen, Mapping):
            # A zero OCC distance alone also describes surfaces that merely meet
            # along an edge.  It is therefore insufficient evidence for a
            # full-surface contact candidate.
            continue
        try:
            relative_area = float(prescreen["relative_area_difference"])
            centroid_distance = float(prescreen["centroid_distance_m"])
            bbox_gap = float(prescreen["bbox_gap_m"])
        except (KeyError, TypeError, ValueError) as error:
            raise MarkerReviewError(
                "contact candidate has incomplete geometric prescreen evidence"
            ) from error
        if not all(
            math.isfinite(value)
            for value in (relative_area, centroid_distance, bbox_gap)
        ):
            raise MarkerReviewError("contact candidate prescreen is not finite")
        if not (
            relative_area <= 1.0e-4
            and centroid_distance <= 1.0e-3
            and bbox_gap <= 1.0e-6
            and distance_m <= 1.0e-9
        ):
            continue
        candidates.append(
            {
                "status": "CANDIDATE",
                "surface_a": tag_a,
                "surface_b": tag_b,
                "volume_a": sorted(owners_a),
                "volume_b": sorted(owners_b),
                "distance_m": distance_m,
                "relative_area_difference": relative_area,
                "centroid_distance_m": centroid_distance,
                "bbox_gap_m": bbox_gap,
                "full_surface_coincidence_proven": False,
                "basis": (
                    "cross-volume near-coincident area, centroid, bounding-box and "
                    "minimum-distance evidence; not a CFD marker assignment"
                ),
            }
        )
    return sorted(candidates, key=lambda item: (item["surface_a"], item["surface_b"]))


def _axial_plane_candidates(
    surfaces: Sequence[Mapping[str, Any]],
    surface_types_by_tag: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for surface in surfaces:
        tag = int(surface["entity_tag"])
        type_name = str(surface_types_by_tag[tag].get("entity_type", ""))
        if len(surface["adjacent_volumes"]) != 1:
            continue
        bounds = surface["bounding_box_m"]
        spans = [abs(bounds[index + 3] - bounds[index]) for index in range(3)]
        transverse = max(spans[1], spans[2], 1.0)
        if spans[0] > max(1.0e-9, 1.0e-6 * transverse):
            continue
        candidates.append(
            {
                "surface_tag": tag,
                "step_name": surface["step_name"],
                "volume_entity_tag": surface["adjacent_volumes"][0],
                "x_m": surface["centroid_m"][0],
                "entity_type": type_name,
                "x_span_m": spans[0],
                "basis": (
                    "geometrically axial exposed surface with negligible X "
                    "thickness; kernel surface type alone is not decisive"
                ),
            }
        )
    return sorted(candidates, key=lambda item: (item["x_m"], item["surface_tag"]))


def _role_reviews(
    required_roles: Sequence[Mapping[str, str]],
    surfaces: Sequence[Mapping[str, Any]],
    components: Sequence[Mapping[str, Any]],
    axial_planes: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    surfaces_by_name: dict[str, list[int]] = defaultdict(list)
    for surface in surfaces:
        name = str(surface["step_name"])
        if name:
            surfaces_by_name[name.casefold()].append(int(surface["entity_tag"]))
    exposed_components = [
        component for component in components if component["exposed_surface_tags"]
    ]
    internal_tags = sorted(
        int(surface["entity_tag"])
        for surface in surfaces
        if len(surface["adjacent_volumes"]) >= 2
    )
    rear_roles = [
        item["role"] for item in required_roles if item["kind"] == "rear_outlet"
    ]
    reviews: dict[str, dict[str, Any]] = {}

    for item in required_roles:
        role = item["role"]
        kind = item["kind"]
        named_tags = sorted(surfaces_by_name.get(role.casefold(), []))
        if named_tags:
            reviews[role] = {
                "status": "CONFIRMED",
                "kind": kind,
                "surface_tags": named_tags,
                "candidate_component_ids": [],
                "basis": "unique exact non-empty persistent STEP entity name",
                "persistent_step_name": role,
            }
            continue

        if kind == "measurement_surface":
            if not internal_tags:
                status = "MISSING"
                basis = "no surface has two or more adjacent volumes"
            elif len(internal_tags) == 1:
                status = "CANDIDATE"
                basis = "one internal surface exists but has no confirming STEP name"
            else:
                status = "AMBIGUOUS"
                basis = "multiple internal surfaces exist without confirming STEP names"
            reviews[role] = {
                "status": status,
                "kind": kind,
                "surface_tags": internal_tags,
                "candidate_component_ids": [],
                "basis": basis,
                "persistent_step_name": None,
            }
            continue

        if kind == "rear_outlet":
            tags = [int(candidate["surface_tag"]) for candidate in axial_planes]
            if not tags:
                status = "MISSING"
                basis = "no exposed axial-plane candidate was found"
            elif len(rear_roles) > 1 or len(tags) > 1:
                status = "AMBIGUOUS"
                basis = (
                    "axial-plane candidates cannot be assigned to numbered rear "
                    "outlet roles without persistent names"
                )
            else:
                status = "CANDIDATE"
                basis = "one axial-plane candidate exists without a persistent role name"
            reviews[role] = {
                "status": status,
                "kind": kind,
                "surface_tags": tags,
                "candidate_component_ids": [],
                "basis": basis,
                "persistent_step_name": None,
            }
            continue

        component_ids = [
            str(component["component_id"]) for component in exposed_components
        ]
        candidate_tags = sorted(
            {
                int(tag)
                for component in exposed_components
                for tag in component["exposed_surface_tags"]
            }
        )
        if not component_ids:
            status = "MISSING"
            basis = "no exposed shell component is available"
        elif len(component_ids) == 1:
            status = "CANDIDATE"
            basis = "one exposed shell exists but has no confirming STEP role name"
        else:
            status = "AMBIGUOUS"
            basis = "multiple exposed shells exist without confirming STEP role names"
        reviews[role] = {
            "status": status,
            "kind": kind,
            "surface_tags": candidate_tags,
            "candidate_component_ids": component_ids,
            "basis": basis,
            "persistent_step_name": None,
        }
    return reviews


def _validate_render_dependencies(
    render: Mapping[str, Any], allowed_root: Path
) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for label, path_key, hash_key in (
        ("render input", "input_file", "input_sha256"),
        ("render groups", "groups_file", "groups_sha256"),
    ):
        raw_path = render.get(path_key)
        expected = render.get(hash_key)
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            raise MarkerReviewError(f"render manifest lacks {path_key}/{hash_key}")
        path, evidence = _validate_file(
            raw_path, label=label, allowed_root=allowed_root
        )
        if evidence["sha256"].casefold() != expected.casefold():
            raise MarkerReviewError(f"{label} SHA256 does not match render manifest")
        dependencies.append({"kind": label, **evidence})

    images = render.get("images", [])
    if not isinstance(images, list):
        raise MarkerReviewError("render manifest images must be a list")
    for index, image in enumerate(images):
        if not isinstance(image, Mapping):
            raise MarkerReviewError(f"render image record {index} is invalid")
        raw_path = image.get("path")
        expected = image.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            raise MarkerReviewError(f"render image record {index} lacks path/hash")
        _, evidence = _validate_file(
            raw_path, label=f"render image {index}", allowed_root=allowed_root
        )
        if evidence["sha256"].casefold() != expected.casefold():
            raise MarkerReviewError(f"render image {index} SHA256 mismatch")
        dependencies.append({"kind": "render image", **evidence})
    return dependencies


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Marker review",
        "",
        f"- Review status: `{report['status']}`",
        f"- Marker configuration ready: `{str(report['marker_configuration_ready']).lower()}`",
        "- This document is evidence for human review; it is not `config/markers.toml`.",
        "",
        "## Required roles",
        "",
        "| Role | Kind | Status | Candidate surfaces | Basis |",
        "|---|---|---|---|---|",
    ]
    for role in report["required_roles"]:
        review = report["roles"][role["role"]]
        tags = ", ".join(str(tag) for tag in review["surface_tags"]) or "—"
        basis = str(review["basis"]).replace("|", "\\|")
        lines.append(
            f"| {role['role']} | {role['kind']} | {review['status']} | "
            f"{tags} | {basis} |"
        )
    lines.extend(
        [
            "",
            "## Shell evidence",
            "",
            f"- Catalog shell components: {len(report['shell_components'])}",
            f"- STEP-to-catalog shell correspondences: {len(report['step_shell_correspondence'])}",
            "- STEP outer/void/boundary-shell semantics are not interpreted as CFD roles.",
            "",
            "## Contact candidates",
            "",
            f"- Candidate pairs: {len(report['contact_candidates'])}",
            "- Zero or near-distance pairs remain candidates only.",
            "",
            "## Uncovered exposed shells",
            "",
        ]
    )
    if report["uncovered_exposed_shells"]:
        for item in report["uncovered_exposed_shells"]:
            lines.append(
                f"- `{item['component_id']}`: surfaces "
                + ", ".join(str(tag) for tag in item["uncovered_surface_tags"])
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Input evidence", ""])
    for name, item in report["inputs"].items():
        if isinstance(item, Mapping) and "sha256" in item:
            lines.append(f"- `{name}`: `{item['sha256']}` — {item.get('path', 'in-memory')}")
    return "\n".join(lines).rstrip() + "\n"


def build_marker_review(
    project_path: PathLike,
    surface_catalog_path: PathLike,
    surface_types_path: PathLike,
    step_topology: Mapping[str, Any],
    solid_volume_mapping: Mapping[str, Any],
    render_manifest_path: PathLike,
    output_directory: PathLike,
    allowed_output_root: PathLike,
) -> dict[str, Any]:
    """Build a conservative marker review from validated local evidence."""

    started_at = _utc_now()
    allowed_root = _validate_allowed_root(allowed_output_root)
    output = _validate_output_path(output_directory, allowed_root)
    project, project_evidence = _validate_file(
        project_path,
        label="project configuration",
        allowed_root=allowed_root,
        allow_project_config=True,
    )
    catalog_path, catalog_evidence = _validate_file(
        surface_catalog_path,
        label="surface catalog",
        allowed_root=allowed_root,
    )
    types_path, types_evidence = _validate_file(
        surface_types_path,
        label="surface types",
        allowed_root=allowed_root,
    )
    render_path, render_evidence = _validate_file(
        render_manifest_path,
        label="render manifest",
        allowed_root=allowed_root,
    )

    surface_types = _load_json(types_path, "surface types")
    render = _load_json(render_path, "render manifest")
    _require_upstream_pass(surface_types, "surface types")
    _require_upstream_pass(render, "render manifest")
    if not isinstance(step_topology, Mapping):
        raise TypeError("step_topology must be a mapping")
    if not isinstance(solid_volume_mapping, Mapping):
        raise TypeError("solid_volume_mapping must be a mapping")
    topology_hash = step_topology.get("source_sha256")
    mapping_hash = solid_volume_mapping.get("source_sha256")
    if (
        not isinstance(topology_hash, str)
        or len(topology_hash) != _SHA256_LENGTH
        or any(character not in "0123456789abcdefABCDEF" for character in topology_hash)
    ):
        raise MarkerReviewError("step_topology lacks a valid source SHA256")
    if not isinstance(mapping_hash, str) or mapping_hash.casefold() != topology_hash.casefold():
        raise MarkerReviewError("solid-volume mapping source SHA256 does not match topology")
    if solid_volume_mapping.get("status") != "MATCHED":
        raise MarkerReviewError(
            "solid-volume mapping must be uniquely MATCHED before marker review"
        )
    raw_solids = step_topology.get("solids")
    raw_matches = solid_volume_mapping.get("matches")
    if not isinstance(raw_solids, list) or not raw_solids:
        raise MarkerReviewError("step_topology contains no solids")
    if not isinstance(raw_matches, list) or len(raw_matches) != len(raw_solids):
        raise MarkerReviewError("solid-volume mapping does not cover every STEP solid")

    surfaces = _load_surface_catalog(catalog_path)
    surface_types_by_tag = _validate_surface_types(surface_types, surfaces)
    declared_catalog_hash = surface_types.get("surface_catalog_sha256")
    if declared_catalog_hash is not None and (
        not isinstance(declared_catalog_hash, str)
        or declared_catalog_hash.casefold() != catalog_evidence["sha256"].casefold()
    ):
        raise MarkerReviewError("surface_types surface_catalog_sha256 mismatch")
    render_dependencies = _validate_render_dependencies(render, allowed_root)
    required_roles, measurement_is_boundary = _project_roles(project)

    components = _build_shell_components(surfaces)
    shell_correspondence = _match_step_shell_components(
        step_topology, solid_volume_mapping, components
    )
    surfaces_by_tag = {int(item["entity_tag"]): item for item in surfaces}
    contacts = _contact_candidates(surface_types, surfaces_by_tag)
    axial_planes = _axial_plane_candidates(surfaces, surface_types_by_tag)
    roles = _role_reviews(required_roles, surfaces, components, axial_planes)
    if any(review["status"] not in _REVIEW_STATUSES for review in roles.values()):
        raise AssertionError("internal marker review generated an invalid status")

    confirmed_surfaces = {
        int(tag)
        for review in roles.values()
        if review["status"] == "CONFIRMED"
        for tag in review["surface_tags"]
    }
    uncovered: list[dict[str, Any]] = []
    for component in components:
        missing = sorted(
            set(component["exposed_surface_tags"]) - confirmed_surfaces
        )
        if missing:
            uncovered.append(
                {
                    "status": "MISSING",
                    "component_id": component["component_id"],
                    "volume_entity_tag": component["volume_entity_tag"],
                    "uncovered_surface_tags": missing,
                    "basis": "exposed shell surfaces are not covered by a CONFIRMED role",
                }
            )

    all_confirmed = all(
        review["status"] == "CONFIRMED" and review["persistent_step_name"]
        for review in roles.values()
    )
    confirmed_role_sets = [
        set(review["surface_tags"])
        for review in roles.values()
        if review["status"] == "CONFIRMED"
    ]
    no_overlap = sum(len(item) for item in confirmed_role_sets) == len(
        set().union(*confirmed_role_sets) if confirmed_role_sets else set()
    )
    marker_ready = bool(all_confirmed and no_overlap and not uncovered)
    role_statuses = {review["status"] for review in roles.values()}
    if marker_ready:
        overall_status = "CONFIRMED"
    elif "AMBIGUOUS" in role_statuses:
        overall_status = "AMBIGUOUS"
    elif "MISSING" in role_statuses or uncovered:
        overall_status = "MISSING"
    else:
        overall_status = "CANDIDATE"

    inputs = {
        "project": project_evidence,
        "surface_catalog": catalog_evidence,
        "surface_types": {
            **types_evidence,
            "declared_status": surface_types["status"],
        },
        "render_manifest": {
            **render_evidence,
            "declared_status": render["status"],
        },
        "render_dependencies": render_dependencies,
        "step_topology": {
            "path": step_topology.get("source_path", "in-memory"),
            "sha256": _payload_sha256(step_topology),
            "source_sha256": topology_hash,
            "solid_count": len(raw_solids),
        },
        "solid_volume_mapping": {
            "path": "in-memory",
            "sha256": _payload_sha256(solid_volume_mapping),
            "source_sha256": mapping_hash,
            "declared_status": solid_volume_mapping["status"],
        },
    }
    report: dict[str, Any] = {
        "status": overall_status,
        "marker_configuration_ready": marker_ready,
        "markers_toml_written": False,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "allowed_output_root": str(allowed_root),
        "output_directory": str(output),
        "required_roles": required_roles,
        "measurement_surface_is_solver_boundary": measurement_is_boundary,
        "roles": roles,
        "shell_components": components,
        "step_shell_correspondence": shell_correspondence,
        "contact_candidates": contacts,
        "axial_plane_candidates": axial_planes,
        "uncovered_exposed_shells": uncovered,
        "inputs": inputs,
        "policy": {
            "confirmed_requires_exact_nonempty_persistent_step_name": True,
            "contact_candidates_are_not_markers": True,
            "step_shell_semantics_are_not_cfd_roles": True,
            "rear_outlet_numbering_is_not_inferred": True,
            "uncovered_exposed_shells_block_readiness": True,
        },
    }

    output.mkdir(parents=True, exist_ok=True)
    if _is_reparse(output):
        raise MarkerReviewError(f"output directory became a reparse point: {output}")
    json_path = output / "marker_review.json"
    markdown_path = output / "marker_review.md"
    if (output / "markers.toml").exists():
        raise MarkerReviewError("refusing to operate beside an unexpected markers.toml")
    markdown = _render_markdown(report)
    _atomic_write_text(markdown_path, markdown)
    report["outputs"] = {
        "marker_review.md": {
            "path": str(markdown_path),
            "size_bytes": markdown_path.stat().st_size,
            "sha256": _sha256(markdown_path),
        }
    }
    _atomic_write_text(
        json_path,
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
    )
    result = dict(report)
    result["outputs"] = {
        **report["outputs"],
        "marker_review.json": {
            "path": str(json_path),
            "size_bytes": json_path.stat().st_size,
            "sha256": _sha256(json_path),
        },
    }
    return result


__all__ = ["MarkerReviewError", "build_marker_review"]
