"""Strict pure-Python derivation of clearance evidence under a stable contract.

This module never imports or launches Gmsh, SU2 or ParaView.  It exists only
to revalidate an immutable, completed clearance audit after unrelated coarse
mesh resource policy changed.  The original report remains untouched and the
derived report retains a hash-bound lineage to it.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tomllib
import traceback
from typing import Any, Mapping, Sequence

from .boundary_layer_local_schedule import (
    BoundaryLayerLocalScheduleError,
    _load_strict_json,
    _validate_clearance_report,
    _validated_contract,
)


_SCHEMA = "cfdpipe.boundary_layer_clearance_revalidation.v1"
_REPORT_SCHEMA = "cfdpipe.boundary_layer_clearance.v1"
_SHA256_LENGTH = 64


class BoundaryLayerClearanceRevalidationError(RuntimeError):
    """Raised when legacy clearance evidence cannot be strictly revalidated."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise BoundaryLayerClearanceRevalidationError(
            "revalidated clearance evidence is not canonical JSON data"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    junction = getattr(path, "is_junction", None)
    return (
        path.is_symlink()
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        or (bool(junction()) if callable(junction) else False)
    )


def _has_link_component(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_link_like(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _snapshot(path: Path, *, include_read_only: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }
    if include_read_only:
        result["read_only"] = _is_read_only(path)
    return result


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} must be finite numeric data"
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} must be finite numeric data"
        ) from error
    if not math.isfinite(number) or (positive and number <= 0.0):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} must be finite{' and positive' if positive else ''}"
        )
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} must be a positive integer"
        )
    return value


def _same_float(left: object, right: object, label: str) -> float:
    actual = _finite(left, label)
    expected = _finite(right, f"expected {label}")
    if not math.isclose(actual, expected, rel_tol=1.0e-11, abs_tol=1.0e-14):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} is internally inconsistent"
        )
    return actual


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} is not a UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} is not parseable"
        ) from error
    if parsed.tzinfo is None:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} has no timezone"
        )
    return parsed


def _strict_lifecycle_and_geometry_checks(
    report: Mapping[str, Any],
    contract: Mapping[str, Any],
    source_report_path: Path,
) -> dict[str, bool]:
    session = report.get("gmsh_session")
    if (
        not isinstance(session, Mapping)
        or session.get("fresh_session_required") is not True
        or session.get("initialize_attempted") is not True
        or session.get("initialize_called") is not True
        or session.get("finalize_attempted") is not True
        or session.get("finalize_called") is not True
        or session.get("cleanup_errors") != []
    ):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report lacks a complete clean Gmsh lifecycle"
        )
    started = _parse_time(report.get("started_at_utc"), "legacy started_at_utc")
    ended = _parse_time(report.get("ended_at_utc"), "legacy ended_at_utc")
    if ended < started:
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report end time precedes its start time"
        )
    if not str(report.get("gmsh_version", "")).strip() or not str(
        report.get("gmsh_module_path", "")
    ).strip():
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report lacks Gmsh version/module evidence"
        )

    source = Path(contract["pipeline_brep_path"])
    current_source = _snapshot(source, include_read_only=True)
    before = report.get("source_before")
    after = report.get("source_after")
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or dict(before) != dict(after)
        or dict(before) != current_source
        or report.get("source_unchanged") is not True
        or current_source["read_only"] is not True
        or _has_link_component(source)
    ):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report does not match the current read-only pipeline BREP"
        )

    recorded_report_path = report.get("report_path")
    if not isinstance(recorded_report_path, str):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report does not retain its report_path"
        )
    try:
        if Path(recorded_report_path).resolve(strict=True) != source_report_path:
            raise BoundaryLayerClearanceRevalidationError(
                "legacy report_path differs from the selected evidence file"
            )
    except OSError as error:
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report_path no longer exists"
        ) from error

    absolute_tolerance = _finite(
        report.get("absolute_tolerance_m"),
        "legacy absolute clearance tolerance",
        positive=True,
    )
    required = float(contract["global_total_thickness_m"])
    maximum_allowed_tolerance = required * 1.0e-6
    if absolute_tolerance > maximum_allowed_tolerance * (1.0 + 1.0e-12):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy absolute clearance tolerance exceeds the bound contract"
        )

    surfaces = report.get("surfaces")
    if not isinstance(surfaces, list):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy clearance surfaces are missing"
        )
    observed_minimum: tuple[float, str, str] | None = None
    for surface_index, surface in enumerate(surfaces):
        if not isinstance(surface, Mapping):
            raise BoundaryLayerClearanceRevalidationError(
                f"legacy clearance surface {surface_index} is invalid"
            )
        _positive_int(surface.get("surface_tag_audit"), "surface audit tag")
        _positive_int(
            surface.get("adjacent_volume_tag_audit"),
            "adjacent volume audit tag",
        )
        direction = surface.get("direction_audit")
        if (
            not isinstance(direction, Mapping)
            or direction.get("status") != "PASS"
            or not isinstance(direction.get("samples"), list)
            or len(direction["samples"]) != 5
        ):
            raise BoundaryLayerClearanceRevalidationError(
                "legacy surface lacks the five-sample inward-direction PASS"
            )
        samples = surface.get("samples")
        if not isinstance(samples, list):
            raise BoundaryLayerClearanceRevalidationError(
                "legacy surface clearance samples are missing"
            )
        for sample_index, sample in enumerate(samples):
            if not isinstance(sample, Mapping):
                raise BoundaryLayerClearanceRevalidationError(
                    f"legacy sample {surface_index}:{sample_index} is invalid"
                )
            if (
                sample.get("status") != "PASS"
                or sample.get("method")
                != "doubling_then_bisection_first_detected_inside_to_outside"
                or sample.get("first_exit_guaranteed_for_nonconvex_volume")
                is not False
            ):
                raise BoundaryLayerClearanceRevalidationError(
                    "legacy sample search semantics are missing or changed"
                )
            lower = _finite(
                sample.get("clearance_lower_bound_m"),
                "sample clearance lower bound",
                positive=True,
            )
            upper = _finite(
                sample.get("clearance_upper_bound_m"),
                "sample clearance upper bound",
                positive=True,
            )
            width = _finite(
                sample.get("bracket_width_m"),
                "sample clearance bracket width",
                positive=True,
            )
            _same_float(width, upper - lower, "sample clearance bracket width")
            if width > absolute_tolerance * (1.0 + 1.0e-9):
                raise BoundaryLayerClearanceRevalidationError(
                    "legacy sample bracket exceeds the recorded audit tolerance"
                )
            label = str(sample.get("label", "")).strip()
            fingerprint = str(
                sample.get("surface_fingerprint_id", "")
            ).casefold()
            if not label:
                raise BoundaryLayerClearanceRevalidationError(
                    "legacy sample label is missing"
                )
            candidate = (lower, fingerprint, label)
            if observed_minimum is None or candidate[0] < observed_minimum[0]:
                observed_minimum = candidate
    if observed_minimum is None:
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report contains no clearance samples"
        )
    location = report.get("minimum_clearance_location")
    if (
        not isinstance(location, Mapping)
        or str(location.get("surface_fingerprint_id", "")).casefold()
        != observed_minimum[1]
        or str(location.get("sample_label", "")) != observed_minimum[2]
    ):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy global minimum-clearance location is inconsistent"
        )

    command_inputs = report.get("command_inputs")
    marker_identity = contract["clearance_evidence_contract"]["marker_config"]
    if (
        not isinstance(command_inputs, Mapping)
        or command_inputs.get("markers_sha256") != marker_identity["sha256"]
    ):
        raise BoundaryLayerClearanceRevalidationError(
            "legacy report is not bound to the current marker definition"
        )
    return {
        "strict_utf8_json_and_all_numbers_finite": True,
        "original_sha256_caller_bound": True,
        "current_pipeline_brep_path_sha256_size_read_only": True,
        "source_before_after_identical": True,
        "gmsh_initialize_finalize_clean": True,
        "audit_scope_no_mesh_no_write_no_downstream": True,
        "wall_fingerprints_48_of_48": True,
        "clearance_samples_240_of_240": True,
        "required_total_thickness_exact": True,
        "sample_brackets_and_global_minimum_consistent": True,
        "current_marker_config_sha256": True,
    }


def derive_revalidated_clearance_report(
    *,
    coarse_contract: Mapping[str, Any],
    source_report_path: str | os.PathLike[str],
    source_report_sha256: str,
) -> dict[str, Any]:
    """Return a derived report bound to the clearance-only sub-contract.

    The caller must supply the expected SHA-256; selecting a report by a
    directory search is intentionally unsupported.  No files are modified.
    """

    expected_digest = str(source_report_sha256).casefold()
    if not _is_sha256(expected_digest):
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report SHA-256 is invalid"
        )
    candidate = Path(source_report_path).expanduser()
    try:
        if candidate.exists() and _is_link_like(candidate):
            raise BoundaryLayerClearanceRevalidationError(
                "source clearance report must not be a link"
            )
        source_report = candidate.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report does not exist"
        ) from error
    if not source_report.is_file() or _has_link_component(source_report):
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report must be an ordinary file without links"
        )
    before = _snapshot(source_report)
    if before["sha256"] != expected_digest:
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report SHA-256 does not match the selected file"
        )
    payload = source_report.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report changed while being read"
        )
    try:
        report = _load_strict_json(payload)
        contract = _validated_contract(coarse_contract)
    except BoundaryLayerLocalScheduleError as error:
        raise BoundaryLayerClearanceRevalidationError(str(error)) from error
    if report.get("schema") != _REPORT_SCHEMA:
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report schema is invalid"
        )
    original_contract_hash = report.get("coarse_contract_sha256")
    if not _is_sha256(original_contract_hash):
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report has no valid original contract hash"
        )
    try:
        validated_surfaces = _validate_clearance_report(
            report,
            contract,
            allowed_contract_hashes={str(original_contract_hash)},
        )
    except BoundaryLayerLocalScheduleError as error:
        raise BoundaryLayerClearanceRevalidationError(str(error)) from error
    checks = _strict_lifecycle_and_geometry_checks(
        report,
        contract,
        source_report,
    )
    after = _snapshot(source_report)
    if after != before:
        raise BoundaryLayerClearanceRevalidationError(
            "source clearance report changed during revalidation"
        )

    derived = copy.deepcopy(report)
    stable_hash = str(contract["clearance_evidence_contract_sha256"])
    derived["coarse_contract_sha256"] = stable_hash
    derived["evidence_kind"] = "strict_pure_python_derived_revalidation"
    derived["revalidation"] = {
        "schema": _SCHEMA,
        "status": "PASS",
        "mode": "pure_python_no_geometry_import_no_mesh_no_external_program",
        "revalidated_at_utc": _utc_now(),
        "original_report": before,
        "original_report_path": str(source_report),
        "original_report_sha256": expected_digest,
        "original_coarse_contract_sha256": str(original_contract_hash),
        "current_normalized_coarse_contract_sha256": contract[
            "normalized_config_sha256"
        ],
        "clearance_evidence_contract_sha256": stable_hash,
        "clearance_evidence_contract": copy.deepcopy(
            contract["clearance_evidence_contract"]
        ),
        "validated_surface_count": len(validated_surfaces),
        "validated_sample_count": sum(
            len(item["sample_clearance_lower_bounds_m"])
            for item in validated_surfaces
        ),
        "checks": checks,
        "gmsh_called_during_revalidation": False,
        "mesh_generated_during_revalidation": False,
        "su2_called_during_revalidation": False,
        "paraview_called_during_revalidation": False,
    }
    derived["report_path"] = None
    try:
        _validate_clearance_report(derived, contract)
    except BoundaryLayerLocalScheduleError as error:
        raise BoundaryLayerClearanceRevalidationError(
            f"derived clearance report failed self-validation: {error}"
        ) from error
    _canonical_sha256(derived)
    return derived


def _trusted_existing_file(
    value: str | Path,
    *,
    root: Path,
    allowed_parent: Path,
    suffix: str,
    label: str,
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} path must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    if _has_link_component(lexical):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} path must not use links"
        )
    try:
        path = lexical.resolve(strict=True)
        parent = allowed_parent.resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} does not exist"
        ) from error
    if (
        not path.is_file()
        or path.suffix.casefold() != suffix.casefold()
        or (path != parent and parent not in path.parents)
        or root not in path.parents
    ):
        raise BoundaryLayerClearanceRevalidationError(
            f"{label} is outside its authorized repository directory"
        )
    return path


def _prepare_output_directory(value: str | Path, *, root: Path) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise BoundaryLayerClearanceRevalidationError(
            "revalidation output path must not contain '..'"
        )
    lexical = Path(os.path.abspath(raw))
    allowed = (root / "runs" / "mesh" / "coarse").resolve(strict=False)
    allowed.mkdir(parents=True, exist_ok=True)
    if _has_link_component(allowed):
        raise BoundaryLayerClearanceRevalidationError(
            "revalidation output root must not use links"
        )
    allowed = allowed.resolve(strict=True)
    resolved = lexical.resolve(strict=False)
    if resolved == allowed or allowed not in resolved.parents:
        raise BoundaryLayerClearanceRevalidationError(
            "revalidation output must be a new child of runs/mesh/coarse"
        )
    if lexical.exists() or resolved.exists() or _has_link_component(lexical.parent):
        raise BoundaryLayerClearanceRevalidationError(
            "revalidation output directory must be new and link-free"
        )
    lexical.mkdir(parents=True, exist_ok=False)
    if lexical.resolve(strict=True) != resolved or _has_link_component(lexical):
        raise BoundaryLayerClearanceRevalidationError(
            "revalidation output directory changed during creation"
        )
    return lexical


def run_clearance_revalidation_command(
    *,
    config_path: str | Path,
    source_report_path: str | Path,
    source_report_sha256: str,
    output_directory: str | Path,
    repository_root: str | Path,
) -> dict[str, Any]:
    """Normalize current truth, derive evidence, and exclusively write JSON."""

    try:
        root = Path(repository_root).expanduser().resolve(strict=True)
    except OSError as error:
        raise BoundaryLayerClearanceRevalidationError(
            "repository root does not exist"
        ) from error
    if not root.is_dir():
        raise BoundaryLayerClearanceRevalidationError(
            "repository root is not a directory"
        )
    config = _trusted_existing_file(
        config_path,
        root=root,
        allowed_parent=root / "config",
        suffix=".toml",
        label="coarse mesh config",
    )
    source_report = _trusted_existing_file(
        source_report_path,
        root=root,
        allowed_parent=root / "runs" / "mesh" / "coarse",
        suffix=".json",
        label="source clearance report",
    )
    try:
        with config.open("rb") as stream:
            config_document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise BoundaryLayerClearanceRevalidationError(
            "coarse mesh config cannot be parsed"
        ) from error
    from .coarse_mesh import normalize_coarse_mesh_config

    try:
        contract = normalize_coarse_mesh_config(
            config_document,
            repository_root=root,
        )
    except Exception as error:
        raise BoundaryLayerClearanceRevalidationError(
            f"current coarse mesh contract is invalid: {error}"
        ) from error
    derived = derive_revalidated_clearance_report(
        coarse_contract=contract,
        source_report_path=source_report,
        source_report_sha256=source_report_sha256,
    )
    output = _prepare_output_directory(output_directory, root=root)
    report_path = output / "boundary_layer_clearance.json"
    derived["report_path"] = str(report_path)
    derived["revalidation"]["output_report_path"] = str(report_path)
    rendered = json.dumps(
        derived,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
    return {
        "status": "PASS",
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "original_report_path": str(source_report),
        "original_report_sha256": str(source_report_sha256).casefold(),
        "original_coarse_contract_sha256": derived["revalidation"][
            "original_coarse_contract_sha256"
        ],
        "clearance_evidence_contract_sha256": contract[
            "clearance_evidence_contract_sha256"
        ],
        "surface_count": derived["surface_count"],
        "sample_count": derived["sample_count"],
        "gmsh_called": False,
        "mesh_generated": False,
        "su2_called": False,
        "paraview_called": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strictly revalidate old clearance evidence without Gmsh"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_clearance_revalidation_command(
            config_path=args.config,
            source_report_path=args.source_report,
            source_report_sha256=args.source_sha256,
            output_directory=args.output,
            repository_root=args.repository_root,
        )
    except Exception as error:
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BoundaryLayerClearanceRevalidationError",
    "derive_revalidated_clearance_report",
    "run_clearance_revalidation_command",
]
