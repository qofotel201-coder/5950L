"""Prove when growth-only schedule changes cannot satisfy the prism gate."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping

from .coarse_repair_audit import validate_coarse_repair_discovery


REPORT_SCHEMA = "cfdpipe.coarse_schedule_invariant_report.v1"
CONCLUSION = "SCHEDULE_ONLY_PROVEN_INFEASIBLE"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CoarseScheduleInvariantError(RuntimeError):
    """The source evidence cannot support the fixed-first-layer proof."""


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def prove_growth_only_infeasible(
    manifest: Mapping[str, Any],
    *,
    source_manifest_path: str,
    source_manifest_sha256: str,
    discovery_validator: Callable[[Any], dict[str, Any]] = (
        validate_coarse_repair_discovery
    ),
) -> dict[str, Any]:
    """Return a hash-bound proof for the fixed first-layer schedule family.

    The proof is deliberately narrow.  It applies only while topology, column
    origins, column directions, the first-cell height and layer count remain
    fixed and growth changes begin with layer two.  It does not claim that a
    geometry-aware direction or topology repair is impossible.
    """

    if not isinstance(manifest, Mapping):
        raise CoarseScheduleInvariantError("repair audit manifest is not a mapping")
    manifest_sha256 = str(source_manifest_sha256).casefold()
    if _SHA256.fullmatch(manifest_sha256) is None or not str(source_manifest_path):
        raise CoarseScheduleInvariantError("source manifest identity is invalid")
    session = manifest.get("gmsh_session")
    write_guard = session.get("write_guard") if isinstance(session, Mapping) else None
    diagnostic = (
        session.get("diagnostic_audit") if isinstance(session, Mapping) else None
    )
    if (
        manifest.get("schema") != "cfdpipe.coarse_repair_audit_manifest.v1"
        or manifest.get("status") != "INCOMPLETE"
        or manifest.get("audit_only") is not True
        or manifest.get("calibration_PASS_authorized") is not False
        or manifest.get("production_mesh_eligible") is not False
        or manifest.get("mesh_written") is not False
        or manifest.get("su2_called") is not False
        or manifest.get("paraview_called") is not False
        or manifest.get("external_commands") != []
        or manifest.get("source_unchanged") is not True
        or not isinstance(session, Mapping)
        or session.get("initialize_called") is not True
        or session.get("clear_called") is not True
        or session.get("finalize_called") is not True
        or session.get("cleanup_errors") != []
        or not isinstance(write_guard, Mapping)
        or write_guard.get("installed") is not True
        or write_guard.get("write_attempt_count") != 0
        or write_guard.get("blocked_paths") != []
        or not isinstance(diagnostic, Mapping)
        or diagnostic.get("status") != "PASS"
        or diagnostic.get("fatal_messages") != []
    ):
        raise CoarseScheduleInvariantError(
            "repair audit lifecycle cannot support a schedule invariant proof"
        )

    discovery = discovery_validator(manifest.get("repair_discovery"))
    full = discovery.get("full_layer_detection")
    binding = manifest.get("local_schedule_binding")
    strategy = manifest.get("strategy_config")
    quality = strategy.get("quality_improvement") if isinstance(strategy, Mapping) else None
    if (
        discovery.get("status") != "INCOMPLETE"
        or not isinstance(full, Mapping)
        or not isinstance(binding, Mapping)
        or binding.get("schema")
        != "cfdpipe.boundary_layer_local_schedule_binding.v1"
        or binding.get("status") != "PASS"
        or not isinstance(strategy, Mapping)
        or strategy.get("contract_mode") != "coarse_repair_audit_only"
        or strategy.get("repair_audit_only") is not True
        or not isinstance(quality, Mapping)
    ):
        raise CoarseScheduleInvariantError(
            "repair audit schedule/strategy lineage is incomplete"
        )
    threshold = quality.get("minimum_prism_scaled_jacobian")
    detected_threshold = full.get("minimum_scaled_jacobian_for_repair")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or float(threshold) <= 0.0
        or isinstance(detected_threshold, bool)
        or not isinstance(detected_threshold, (int, float))
        or not math.isclose(
            float(threshold),
            float(detected_threshold),
            rel_tol=1.0e-12,
            abs_tol=0.0,
        )
    ):
        raise CoarseScheduleInvariantError("prism quality threshold is stale")
    threshold_value = float(threshold)

    first_height = binding.get("first_layer_height_m")
    layer_count = binding.get("layer_count")
    schedules = binding.get("surface_schedules")
    if (
        isinstance(first_height, bool)
        or not isinstance(first_height, (int, float))
        or not math.isfinite(float(first_height))
        or float(first_height) <= 0.0
        or isinstance(layer_count, bool)
        or not isinstance(layer_count, int)
        or layer_count <= 0
        or not isinstance(schedules, Mapping)
        or len(schedules) != 48
    ):
        raise CoarseScheduleInvariantError("bound physical schedule is incomplete")
    normalized_fingerprints = [str(value).casefold() for value in schedules]
    if (
        len(normalized_fingerprints) != len(set(normalized_fingerprints))
        or any(_SHA256.fullmatch(value) is None for value in normalized_fingerprints)
    ):
        raise CoarseScheduleInvariantError("bound wall inventory is invalid")
    for raw_fingerprint, record in schedules.items():
        fingerprint = str(raw_fingerprint).casefold()
        cumulative = record.get("cumulative_heights_m") if isinstance(record, Mapping) else None
        if (
            not isinstance(record, Mapping)
            or str(record.get("surface_fingerprint_id", "")).casefold()
            != fingerprint
            or not isinstance(cumulative, list)
            or len(cumulative) != layer_count
            or isinstance(cumulative[0], bool)
            or not isinstance(cumulative[0], (int, float))
            or not math.isclose(
                float(cumulative[0]),
                float(first_height),
                rel_tol=1.0e-12,
                abs_tol=0.0,
            )
        ):
            raise CoarseScheduleInvariantError(
                "one wall schedule does not preserve the common first height"
            )

    triangle_records = full.get("triangle_evidence")
    bad_records = full.get("bad_records")
    if not isinstance(triangle_records, list) or not isinstance(bad_records, list):
        raise CoarseScheduleInvariantError("full-layer records are missing")
    triangle_to_wall: dict[str, str] = {}
    for record in triangle_records:
        if not isinstance(record, Mapping):
            raise CoarseScheduleInvariantError(
                "full-layer triangle identity is invalid or duplicated"
            )
        triangle = str(record.get("source_triangle_sha256", "")).casefold()
        wall = str(record.get("wall_surface_fingerprint", "")).casefold()
        if (
            _SHA256.fullmatch(triangle) is None
            or wall not in normalized_fingerprints
            or triangle in triangle_to_wall
        ):
            raise CoarseScheduleInvariantError(
                "full-layer triangle identity is invalid or duplicated"
            )
        triangle_to_wall[triangle] = wall

    first_layer_failures: list[dict[str, Any]] = []
    for record in bad_records:
        if not isinstance(record, Mapping) or record.get("layer") != 1:
            continue
        triangle = str(record.get("source_triangle_sha256", "")).casefold()
        raw_min_sj = record.get("min_sj")
        if (
            triangle not in triangle_to_wall
            or isinstance(raw_min_sj, bool)
            or not isinstance(raw_min_sj, (int, float))
            or not math.isfinite(float(raw_min_sj))
            or record.get("below_configured_min_sj") is not True
            or not float(raw_min_sj) < threshold_value
        ):
            raise CoarseScheduleInvariantError(
                "one first-layer failure is inconsistent with the quality gate"
            )
        first_layer_failures.append(
            {
                "source_triangle_sha256": triangle,
                "wall_surface_fingerprint": triangle_to_wall[triangle],
                "minimum_scaled_jacobian": float(raw_min_sj),
                "strict_nonpositive_quality": record.get(
                    "strict_nonpositive_quality"
                ),
            }
        )
    first_layer_failures.sort(
        key=lambda item: (
            item["wall_surface_fingerprint"], item["source_triangle_sha256"]
        )
    )
    if not first_layer_failures:
        raise CoarseScheduleInvariantError(
            "no first-layer quality failure proves growth-only infeasibility"
        )
    stable_pairs = [
        (record["wall_surface_fingerprint"], record["source_triangle_sha256"])
        for record in first_layer_failures
    ]
    if len(stable_pairs) != len(set(stable_pairs)):
        raise CoarseScheduleInvariantError("first-layer stable failures are duplicated")

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "PASS",
        "conclusion": CONCLUSION,
        "source_manifest": {
            "path": str(source_manifest_path),
            "sha256": manifest_sha256,
            "schema": manifest.get("schema"),
            "status": manifest.get("status"),
        },
        "coarse_contract_sha256": manifest.get("coarse_contract_sha256"),
        "baseline_binding_sha256": binding.get("binding_sha256"),
        "proof_scope": {
            "fixed_topology": True,
            "fixed_column_origins": True,
            "fixed_column_directions": True,
            "fixed_first_layer_height_m": float(first_height),
            "fixed_layer_count": layer_count,
            "growth_changes_begin_after_layer_one": True,
            "geometry_aware_direction_or_topology_change_in_scope": False,
        },
        "quality_gate": {
            "minimum_prism_scaled_jacobian": threshold_value,
            "maximum_prism_below_threshold_element_count": 0,
        },
        "first_layer_failure_count": len(first_layer_failures),
        "first_layer_minimum_scaled_jacobian": min(
            record["minimum_scaled_jacobian"] for record in first_layer_failures
        ),
        "first_layer_failures": first_layer_failures,
        "logical_implication": (
            "Every growth-only candidate preserves the failing first-layer "
            "coordinates; therefore no member can satisfy the zero-below-threshold gate."
        ),
        "required_next_stage": "GEOMETRY_AWARE_DIRECTION_OR_TOPOLOGY_AUDIT",
        "mesh_written": False,
        "su2_called": False,
        "paraview_called": False,
        "calibration_PASS_authorized": False,
        "production_mesh_eligible": False,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report
