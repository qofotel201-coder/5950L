"""One-layer Gmsh count projection for bounded coarse-mesh calibration.

This module deliberately does *not* build a calibration mesh artifact.  It
uses the real-project boundary-layer strategy only far enough to generate the
temporary one-layer Prism6 shell and the two tetrahedral cores in memory.  The
observed source-column and core counts are then projected to the requested
layer count.  No mesh is written and no mesh-quality, calibration, solver or
post-processing PASS is authorized by this evidence.
"""

from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import stat
import time
import traceback
from typing import Any, Mapping, Sequence

from .boundary_layer_smoke import (
    RealProjectBoundaryLayerStrategy,
    _ALLOWED_3D_TYPES,
    _element_records_from_gmsh,
    _surface_faces_from_gmsh,
)
from .coarse_mesh import CoarseMeshError, make_coarse_strategy_config


_SCHEMA = "cfdpipe.coarse_mesh_projection.v1"
_STRATEGY_MODE = "coarse_projection_only"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseMeshProjectionError(RuntimeError):
    """Raised when one-layer projection evidence is incomplete or unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    return not bool(metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CoarseMeshProjectionError(f"{label} must be a positive integer")
    return value


def _finite_positive(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CoarseMeshProjectionError(f"{label} must be finite and positive") from error
    if not math.isfinite(result) or result <= 0.0:
        raise CoarseMeshProjectionError(f"{label} must be finite and positive")
    return result


def make_projection_strategy_config(
    contract: Mapping[str, Any], characteristic_length_m: float
) -> dict[str, Any]:
    """Derive the safe one-layer strategy configuration from a coarse contract.

    The requested final layer count remains the count selected by the coarse
    boundary-layer design.  Crucially, the temporary Gmsh construction height
    is restored from the authoritative smoke contract; the much larger planned
    60-layer total thickness is never sent to ``extrudeBoundaryLayer`` here.
    """

    try:
        strategy = make_coarse_strategy_config(
            contract,
            characteristic_length_m,
            projection_only=True,
        )
    except CoarseMeshError as error:
        raise CoarseMeshProjectionError(str(error)) from error
    base = contract.get("base_smoke_contract")
    design = contract.get("boundary_layer_design")
    if not isinstance(base, Mapping) or not isinstance(design, Mapping):
        raise CoarseMeshProjectionError(
            "coarse projection requires base smoke and boundary-layer design contracts"
        )
    construction_height = _finite_positive(
        base.get("construction_first_layer_height_m"),
        "base smoke construction first-layer height",
    )
    requested_layers = _positive_integer(
        design.get("layer_count"), "requested projection layer count"
    )
    projection_cap = _positive_integer(
        base.get("max_3d_elements"), "projection element cap"
    )

    strategy = copy.deepcopy(strategy)
    strategy.pop("normalized_config_sha256", None)
    strategy.update(
        {
            "contract_mode": _STRATEGY_MODE,
            "projection_only": True,
            "calibration_only": True,
            "production_mesh_eligible": False,
            "construction_first_layer_height_m": construction_height,
            "layer_count": requested_layers,
            "max_3d_elements": projection_cap,
            "projection_maximum_3d_elements": projection_cap,
            "projection_construction_height_source": (
                "contract.base_smoke_contract."
                "construction_first_layer_height_m"
            ),
        }
    )
    strategy["normalized_config_sha256"] = _canonical_hash(strategy)
    return strategy


def evaluate_projected_element_count(
    *,
    source_prism_count: int,
    nonprism_type_counts: Mapping[str, int],
    requested_layer_count: int,
    maximum_projected_3d_elements: int,
) -> dict[str, Any]:
    """Apply the strict ``projected < cap`` count gate without meshing layers."""

    prisms = _positive_integer(source_prism_count, "source prism count")
    layers = _positive_integer(requested_layer_count, "requested layer count")
    cap = _positive_integer(
        maximum_projected_3d_elements, "maximum projected 3D elements"
    )
    counts: dict[str, int] = {}
    for raw_name, raw_count in nonprism_type_counts.items():
        name = str(raw_name)
        if name not in _ALLOWED_3D_TYPES or name == "Prism 6":
            raise CoarseMeshProjectionError(
                "projection non-prism inventory contains an invalid element type"
            )
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise CoarseMeshProjectionError(
                "projection non-prism inventory contains an invalid count"
            )
        if name in counts:
            raise CoarseMeshProjectionError(
                "projection non-prism inventory contains duplicate normalized types"
            )
        counts[name] = raw_count
    nonprism_count = sum(counts.values())
    if nonprism_count <= 0:
        raise CoarseMeshProjectionError("projection has no core elements")
    projected = nonprism_count + prisms * layers
    passed = projected < cap
    return {
        "status": "PASS" if passed else "FAIL",
        "formula": (
            "nonprism_count + source_prism_count * requested_layer_count"
        ),
        "source_prism_count": prisms,
        "nonprism_type_counts": dict(sorted(counts.items())),
        "nonprism_count": nonprism_count,
        "requested_layer_count": layers,
        "projected_3d_elements": projected,
        "maximum_projected_3d_elements_exclusive": cap,
        "strictly_below_cap": passed,
        "headroom_elements": cap - projected,
        "failure_reason": None
        if passed
        else "PROJECTED_3D_ELEMENT_COUNT_NOT_STRICTLY_BELOW_CONTRACT_CAP",
    }


def summarize_one_layer_projection(
    gmsh: Any,
    *,
    wall_fingerprint_by_tag: Mapping[int, str],
    prism_by_wall: Mapping[int, int],
    core_volume_tags: Sequence[int],
    normalized_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Count the generated one-layer topology and extrapolate it in memory."""

    if (
        normalized_config.get("projection_only") is not True
        or normalized_config.get("contract_mode") != _STRATEGY_MODE
        or normalized_config.get("calibration_only") is not True
        or normalized_config.get("production_mesh_eligible") is not False
    ):
        raise CoarseMeshProjectionError("projection-only strategy scope is invalid")
    expected_fingerprints = {
        str(value) for value in normalized_config.get("wall_surface_fingerprints", [])
    }
    observed_fingerprint_list = [
        str(value) for value in wall_fingerprint_by_tag.values()
    ]
    observed_fingerprints = set(observed_fingerprint_list)
    prism_volume_tags = [int(tag) for tag in prism_by_wall.values()]
    if (
        not expected_fingerprints
        or observed_fingerprints != expected_fingerprints
        or len(observed_fingerprint_list) != len(expected_fingerprints)
        or set(int(tag) for tag in prism_by_wall)
        != set(int(tag) for tag in wall_fingerprint_by_tag)
        or len(prism_volume_tags) != len(set(prism_volume_tags))
    ):
        raise CoarseMeshProjectionError(
            "projection wall/prism entities do not match the stable wall contract"
        )
    if len(core_volume_tags) != 2 or len(set(int(tag) for tag in core_volume_tags)) != 2:
        raise CoarseMeshProjectionError("projection requires exactly two distinct cores")
    if set(int(tag) for tag in core_volume_tags) & set(prism_volume_tags):
        raise CoarseMeshProjectionError("projection core and prism volume tags overlap")

    per_surface: list[dict[str, Any]] = []
    source_prism_total = 0
    seen_element_tags: set[int] = set()
    for wall_tag in sorted(wall_fingerprint_by_tag):
        fingerprint = str(wall_fingerprint_by_tag[wall_tag])
        prism_tag = int(prism_by_wall[wall_tag])
        wall_faces = _surface_faces_from_gmsh(gmsh, [int(wall_tag)])
        if not wall_faces or any(len(face) != 3 for face in wall_faces):
            raise CoarseMeshProjectionError(
                f"wall {fingerprint} does not have an all-triangle source mesh"
            )
        prism_records = _element_records_from_gmsh(gmsh, 3, prism_tag)
        if not prism_records or any(
            str(record.get("type")) != "Prism 6" for record in prism_records
        ):
            raise CoarseMeshProjectionError(
                f"wall {fingerprint} does not map to an all-Prism6 one-layer volume"
            )
        prism_element_tags = {int(record["tag"]) for record in prism_records}
        if (
            len(prism_element_tags) != len(prism_records)
            or seen_element_tags & prism_element_tags
        ):
            raise CoarseMeshProjectionError(
                "one-layer prism element tags are duplicated across wall volumes"
            )
        seen_element_tags.update(prism_element_tags)
        source_triangles = len(wall_faces)
        one_layer_prisms = len(prism_records)
        if source_triangles != one_layer_prisms:
            raise CoarseMeshProjectionError(
                f"wall {fingerprint} source-column and one-layer counts differ"
            )
        source_prism_total += one_layer_prisms
        per_surface.append(
            {
                "wall_surface_fingerprint": fingerprint,
                "runtime_wall_surface_tag_audit": int(wall_tag),
                "runtime_prism_volume_tag_audit": prism_tag,
                "source_triangle_column_count": source_triangles,
                "one_layer_prism_count": one_layer_prisms,
                "counts_match": True,
            }
        )
    per_surface.sort(key=lambda item: item["wall_surface_fingerprint"])

    core_records: list[dict[str, Any]] = []
    core_inventory: list[dict[str, Any]] = []
    for index, core_tag in enumerate(
        (int(value) for value in core_volume_tags), start=1
    ):
        records = _element_records_from_gmsh(gmsh, 3, core_tag)
        if not records:
            raise CoarseMeshProjectionError(f"projection core {index} is empty")
        counts = Counter(str(record.get("type")) for record in records)
        if any(name not in _ALLOWED_3D_TYPES for name in counts) or counts.get(
            "Prism 6", 0
        ):
            raise CoarseMeshProjectionError(
                f"projection core {index} contains a disallowed or Prism6 element"
            )
        core_element_tags = {int(record["tag"]) for record in records}
        if (
            len(core_element_tags) != len(records)
            or seen_element_tags & core_element_tags
        ):
            raise CoarseMeshProjectionError(
                "projection core element tags are duplicated across volumes"
            )
        seen_element_tags.update(core_element_tags)
        core_records.extend(records)
        core_inventory.append(
            {
                "core_label": f"fluid_core_{index}",
                "runtime_volume_tag_audit": core_tag,
                "element_count": len(records),
                "element_type_counts": dict(sorted(counts.items())),
            }
        )
    nonprism_counts = Counter(str(record["type"]) for record in core_records)
    gate = evaluate_projected_element_count(
        source_prism_count=source_prism_total,
        nonprism_type_counts=dict(nonprism_counts),
        requested_layer_count=_positive_integer(
            normalized_config.get("layer_count"), "requested projection layer count"
        ),
        maximum_projected_3d_elements=_positive_integer(
            normalized_config.get("projection_maximum_3d_elements"),
            "projection element cap",
        ),
    )
    actual_one_layer_counts = Counter(nonprism_counts)
    actual_one_layer_counts["Prism 6"] = source_prism_total
    return {
        "schema": _SCHEMA,
        "status": gate["status"],
        "scope": {
            "projection_only": True,
            "calibration_PASS_authorized": False,
            "production": False,
            "mesh_quality_PASS_authorized": False,
        },
        "temporary_construction": {
            "source": normalized_config[
                "projection_construction_height_source"
            ],
            "height_m": _finite_positive(
                normalized_config.get("construction_first_layer_height_m"),
                "projection construction height",
            ),
            "one_prism_layer_generated": True,
        },
        "wall_surface_count": len(per_surface),
        "wall_source_column_count": source_prism_total,
        "one_layer_prism_count": source_prism_total,
        "per_wall_surface_columns": per_surface,
        "core_inventories": core_inventory,
        "one_layer_3d_element_type_counts": dict(
            sorted(actual_one_layer_counts.items())
        ),
        "one_layer_3d_element_count": sum(actual_one_layer_counts.values()),
        "projection_gate": gate,
        "full_layer_subdivision_performed": False,
        "mesh_generated_in_memory": True,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "formal_quality_conclusion": "NOT_EVALUATED_OR_AUTHORIZED",
    }


def _validate_projection_source(contract: Mapping[str, Any]) -> Path:
    raw = contract.get("pipeline_brep_path")
    if not isinstance(raw, str) or not raw.strip():
        raise CoarseMeshProjectionError("projection source BREP path is missing")
    source = Path(raw).expanduser().resolve(strict=True)
    provenance = contract.get("provenance")
    expected = (
        str(provenance.get("pipeline_brep_sha256", "")).casefold()
        if isinstance(provenance, Mapping)
        else ""
    )
    if (
        not source.is_file()
        or source.suffix.casefold() != ".brep"
        or _SHA256.fullmatch(expected) is None
        or _sha256(source) != expected
        or not _is_read_only(source)
    ):
        raise CoarseMeshProjectionError(
            "projection source BREP is not the hash-bound read-only input"
        )
    return source


def run_coarse_mesh_projection(
    *,
    contract: Mapping[str, Any],
    characteristic_length_m: float,
    strategy: RealProjectBoundaryLayerStrategy,
    gmsh_module: Any | None = None,
) -> dict[str, Any]:
    """Run one fresh, in-memory Gmsh projection session and always finalize.

    Failures are returned as a strict in-memory manifest with the original
    traceback.  The caller may serialize that evidence in an already-approved
    output layer; this function itself performs no write operation.
    """

    started_at = _utc_now()
    started_monotonic = time.monotonic()
    manifest: dict[str, Any] = {
        "schema": "cfdpipe.coarse_mesh_projection_manifest.v1",
        "status": "FAIL",
        "projection_only": True,
        "calibration_PASS_authorized": False,
        "production": False,
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "external_commands": [],
        "started_at_utc": started_at,
        "ended_at_utc": None,
        "elapsed_seconds": None,
        "gmsh_version": None,
        "gmsh_module_path": None,
        "gmsh_session": {
            "initialize_attempted": False,
            "initialize_called": False,
            "logger_started": False,
            "logger_messages": [],
            "clear_called": False,
            "finalize_attempted": False,
            "finalize_called": False,
            "cleanup_errors": [],
        },
        "strategy_config": None,
        "strategy_config_sha256": None,
        "source_before": None,
        "source_after": None,
        "source_unchanged": None,
        "projection": None,
        "error": None,
    }
    gmsh: Any | None = None
    initialized = False
    logger_started = False
    primary: BaseException | None = None

    def record_cleanup(stage: str, error: BaseException) -> None:
        manifest["gmsh_session"]["cleanup_errors"].append(
            {
                "stage": stage,
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }
        )

    try:
        strategy_config = make_projection_strategy_config(
            contract, characteristic_length_m
        )
        source = _validate_projection_source(contract)
        source_before = {
            "path": str(source),
            "sha256": _sha256(source),
            "size_bytes": source.stat().st_size,
            "read_only": _is_read_only(source),
        }
        manifest["strategy_config"] = strategy_config
        manifest["strategy_config_sha256"] = strategy_config[
            "normalized_config_sha256"
        ]
        manifest["source_before"] = source_before
        gmsh = gmsh_module or importlib.import_module("gmsh")
        manifest["gmsh_version"] = str(getattr(gmsh, "__version__", "unknown"))
        module_path = getattr(gmsh, "__file__", None)
        manifest["gmsh_module_path"] = (
            str(Path(module_path).resolve()) if module_path else "unknown"
        )
        checker = getattr(gmsh, "isInitialized", None)
        if callable(checker) and bool(checker()):
            raise CoarseMeshProjectionError(
                "projection requires a fresh uninitialized Gmsh session"
            )
        manifest["gmsh_session"]["initialize_attempted"] = True
        gmsh.initialize(readConfigFiles=False)
        initialized = True
        manifest["gmsh_session"]["initialize_called"] = True
        logger = getattr(gmsh, "logger", None)
        if logger is not None and callable(getattr(logger, "start", None)):
            logger.start()
            logger_started = True
            manifest["gmsh_session"]["logger_started"] = True
        gmsh.model.add("cfdpipe_coarse_mesh_projection")
        projection = strategy.build(gmsh, source, strategy_config, source.parent)
        if (
            not isinstance(projection, Mapping)
            or projection.get("schema") != _SCHEMA
            or projection.get("scope", {}).get("projection_only") is not True
            or projection.get("scope", {}).get("calibration_PASS_authorized")
            is not False
            or projection.get("mesh_written") is not False
            or projection.get("full_layer_subdivision_performed") is not False
        ):
            raise CoarseMeshProjectionError(
                "strategy did not return strict projection-only evidence"
            )
        manifest["projection"] = dict(projection)
        manifest["status"] = str(projection.get("status", "FAIL"))
    except BaseException as error:
        primary = error
        manifest["status"] = "FAIL"
        manifest["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        }
    finally:
        if gmsh is not None and logger_started:
            logger = getattr(gmsh, "logger", None)
            try:
                getter = getattr(logger, "get", None)
                if callable(getter):
                    manifest["gmsh_session"]["logger_messages"] = [
                        str(value) for value in getter()
                    ]
            except BaseException as error:
                record_cleanup("gmsh.logger.get", error)
            try:
                stopper = getattr(logger, "stop", None)
                if callable(stopper):
                    stopper()
            except BaseException as error:
                record_cleanup("gmsh.logger.stop", error)

        should_finalize = initialized
        if not should_finalize and gmsh is not None and manifest["gmsh_session"][
            "initialize_attempted"
        ]:
            try:
                checker = getattr(gmsh, "isInitialized", None)
                should_finalize = callable(checker) and bool(checker())
            except BaseException as error:
                record_cleanup("gmsh.isInitialized", error)
        if should_finalize and gmsh is not None:
            clearer = getattr(gmsh, "clear", None)
            if callable(clearer):
                try:
                    clearer()
                    manifest["gmsh_session"]["clear_called"] = True
                except BaseException as error:
                    record_cleanup("gmsh.clear", error)
            manifest["gmsh_session"]["finalize_attempted"] = True
            try:
                gmsh.finalize()
                manifest["gmsh_session"]["finalize_called"] = True
            except BaseException as error:
                record_cleanup("gmsh.finalize", error)

        before = manifest.get("source_before")
        if isinstance(before, Mapping):
            try:
                source = Path(str(before["path"]))
                after = {
                    "path": str(source),
                    "sha256": _sha256(source),
                    "size_bytes": source.stat().st_size,
                    "read_only": _is_read_only(source),
                }
                manifest["source_after"] = after
                manifest["source_unchanged"] = after == dict(before)
            except BaseException as error:
                record_cleanup("source_after", error)
                manifest["source_unchanged"] = False

        cleanup_errors = manifest["gmsh_session"]["cleanup_errors"]
        if (
            cleanup_errors
            or manifest.get("source_unchanged") is not True
            or (
                manifest["gmsh_session"]["initialize_called"] is True
                and manifest["gmsh_session"]["finalize_called"] is not True
            )
        ):
            manifest["status"] = "FAIL"
            if manifest["error"] is None:
                manifest["error"] = {
                    "type": "CoarseMeshProjectionCleanupError",
                    "message": "projection cleanup or source-integrity gate failed",
                    "traceback": "",
                }
        manifest["ended_at_utc"] = _utc_now()
        manifest["elapsed_seconds"] = time.monotonic() - started_monotonic
        if primary is None and manifest["status"] == "FAIL" and manifest["error"] is None:
            manifest["error"] = {
                "type": "CoarseMeshProjectionGateError",
                "message": str(
                    manifest.get("projection", {})
                    .get("projection_gate", {})
                    .get("failure_reason", "projection gate failed")
                ),
                "traceback": "",
            }
    return manifest
