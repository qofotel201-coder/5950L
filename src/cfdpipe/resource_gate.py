"""Conservative, side-effect-free resource projection for a coarse CFD grid.

The gate consumes caller-supplied calibration measurements.  It does not
inspect the host, launch a tool, generate a mesh, or run a solver.  Every
decision and projection is returned as JSON-serializable evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


REPORT_SCHEMA = "cfdpipe.coarse_resource_gate.v1"
OS_RESERVE_BYTES = 4 * 1024**3
SAFETY_FACTOR = 1.3
MINIMUM_CALIBRATION_SAMPLES = 2

_SAMPLE_FIELDS = (
    "elements",
    "peak_working_set_bytes",
    "elapsed_seconds",
    "output_bytes",
)


def _reason(
    report: dict[str, Any], code: str, message: str, *, field: str | None = None
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        item["field"] = field
    report["reasons"].append(item)


def _warning(report: dict[str, Any], code: str, message: str) -> None:
    report["warnings"].append({"code": code, "message": message})


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _safe_input(value: Any) -> int | float | str | bool | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return repr(value)


def _base_report(
    *,
    target_elements: Any,
    physical_total_bytes: Any,
    physical_available_bytes: Any,
    virtual_available_bytes: Any,
    disk_free_bytes: Any,
    maximum_elapsed_seconds: Any,
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": "FAIL",
        "target_elements": _safe_input(target_elements),
        "policy": {
            "minimum_calibration_samples": MINIMUM_CALIBRATION_SAMPLES,
            "os_reserve_bytes": OS_RESERVE_BYTES,
            "safety_factor": SAFETY_FACTOR,
            "pagefile_can_substitute_physical": False,
            "projection_method": "max(linear_through_origin,power_envelope)",
            "maximum_elapsed_seconds": _safe_input(maximum_elapsed_seconds),
        },
        "inputs": {
            "calibration_samples": [],
            "system_resources": {
                "physical_total_bytes": _safe_input(physical_total_bytes),
                "physical_available_bytes": _safe_input(physical_available_bytes),
                "virtual_available_bytes": _safe_input(virtual_available_bytes),
                "disk_free_bytes": _safe_input(disk_free_bytes),
            },
        },
        "projections": {},
        "requirements": {
            "projected_peak_with_safety_bytes": None,
            "physical_bytes_including_os_reserve": None,
            "virtual_available_bytes": None,
            "disk_free_bytes": None,
            "elapsed_seconds": None,
        },
        "checks": {
            "calibration_sample_count": False,
            "calibration_scale_count": False,
            "calibration_values": False,
            "target_elements": False,
            "target_not_below_calibration_max": False,
            "physical_total_input": False,
            "physical_available_input": False,
            "virtual_available_input": False,
            "disk_free_input": False,
            "physical_total": False,
            "physical_available": False,
            "virtual_available": False,
            "disk_free": False,
            "elapsed_budget": False,
        },
        "time_gate_status": "FAIL",
        "reasons": [],
        "warnings": [],
    }


def _normalize_samples(report: dict[str, Any], samples: Any) -> list[dict[str, Any]]:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes, bytearray)):
        _reason(
            report,
            "CALIBRATION_SAMPLE_COUNT",
            "calibration samples must be a sequence containing at least two records",
            field="samples",
        )
        return []

    report["checks"]["calibration_sample_count"] = (
        len(samples) >= MINIMUM_CALIBRATION_SAMPLES
    )
    if not report["checks"]["calibration_sample_count"]:
        _reason(
            report,
            "CALIBRATION_SAMPLE_COUNT",
            "at least two calibration samples are required",
            field="samples",
        )

    normalized: list[dict[str, Any]] = []
    all_values_valid = True
    for index, raw in enumerate(samples):
        location = f"samples[{index}]"
        if not isinstance(raw, Mapping):
            all_values_valid = False
            _reason(
                report,
                "CALIBRATION_VALUE_INVALID",
                f"{location} must be an object",
                field=location,
            )
            normalized.append({field: None for field in _SAMPLE_FIELDS})
            continue
        record = {field: _safe_input(raw.get(field)) for field in _SAMPLE_FIELDS}
        normalized.append(record)
        validity = {
            "elements": _positive_int(raw.get("elements")),
            "peak_working_set_bytes": _positive_int(
                raw.get("peak_working_set_bytes")
            ),
            "elapsed_seconds": _positive_finite(raw.get("elapsed_seconds")),
            "output_bytes": _positive_int(raw.get("output_bytes")),
        }
        for field, valid in validity.items():
            if not valid:
                all_values_valid = False
                _reason(
                    report,
                    "CALIBRATION_VALUE_INVALID",
                    f"{location}.{field} must be a positive finite value"
                    if field == "elapsed_seconds"
                    else f"{location}.{field} must be a positive integer",
                    field=f"{location}.{field}",
                )
                record[field] = None

    report["inputs"]["calibration_samples"] = sorted(
        normalized,
        key=lambda item: item.get("elements")
        if isinstance(item.get("elements"), int)
        else -1,
    )
    report["checks"]["calibration_values"] = all_values_valid
    valid_scales = {
        item["elements"]
        for item in normalized
        if isinstance(item.get("elements"), int) and item["elements"] > 0
    }
    report["checks"]["calibration_scale_count"] = len(valid_scales) >= 2
    if len(samples) >= MINIMUM_CALIBRATION_SAMPLES and len(valid_scales) < 2:
        _reason(
            report,
            "CALIBRATION_SCALE_COUNT",
            "at least two distinct positive element counts are required",
            field="samples.elements",
        )
    return normalized


def _project_metric(
    samples: list[dict[str, Any]], target_elements: int, metric: str
) -> dict[str, Any]:
    points = sorted(
        (int(item["elements"]), float(item[metric])) for item in samples
    )
    pairwise_exponents: list[float] = []
    for left_index, (left_elements, left_value) in enumerate(points):
        for right_elements, right_value in points[left_index + 1 :]:
            if right_elements == left_elements:
                continue
            exponent = math.log(right_value / left_value) / math.log(
                right_elements / left_elements
            )
            if math.isfinite(exponent):
                pairwise_exponents.append(exponent)
    power_exponent = max([1.0, *pairwise_exponents])
    linear_values = [
        value * target_elements / elements for elements, value in points
    ]
    power_values: list[float] = []
    for elements, value in points:
        projected = value * math.exp(
            power_exponent * math.log(target_elements / elements)
        )
        if not math.isfinite(projected):
            raise OverflowError(f"non-finite {metric} power projection")
        power_values.append(projected)
    linear_upper = max(linear_values)
    power_upper = max(power_values)
    upper = max(linear_upper, power_upper)
    if not all(math.isfinite(value) for value in (linear_upper, power_upper, upper)):
        raise OverflowError(f"non-finite {metric} projection")
    is_bytes = metric.endswith("_bytes")
    convert = math.ceil if is_bytes else float
    return {
        "metric": metric,
        "method": "max(linear_through_origin,power_envelope)",
        "calibration_points": [
            {"elements": elements, "value": math.ceil(value) if is_bytes else value}
            for elements, value in points
        ],
        "observed_pairwise_exponents": pairwise_exponents,
        "power_exponent": power_exponent,
        "linear_upper": convert(linear_upper),
        "power_upper": convert(power_upper),
        "upper_bound": convert(upper),
    }


def _validate_system_inputs(
    report: dict[str, Any],
    *,
    physical_total_bytes: Any,
    physical_available_bytes: Any,
    virtual_available_bytes: Any,
    disk_free_bytes: Any,
) -> None:
    total_valid = _positive_int(physical_total_bytes)
    available_valid = _nonnegative_int(physical_available_bytes)
    if available_valid and total_valid and physical_available_bytes > physical_total_bytes:
        available_valid = False
    virtual_valid = _nonnegative_int(virtual_available_bytes)
    disk_valid = _nonnegative_int(disk_free_bytes)
    report["checks"]["physical_total_input"] = total_valid
    report["checks"]["physical_available_input"] = available_valid
    report["checks"]["virtual_available_input"] = virtual_valid
    report["checks"]["disk_free_input"] = disk_valid
    validations = (
        (
            total_valid,
            "PHYSICAL_TOTAL_INVALID",
            "physical_total_bytes must be a positive integer",
            "physical_total_bytes",
        ),
        (
            available_valid,
            "PHYSICAL_AVAILABLE_INVALID",
            "physical_available_bytes must be a non-negative integer no greater than physical_total_bytes",
            "physical_available_bytes",
        ),
        (
            virtual_valid,
            "VIRTUAL_AVAILABLE_INVALID",
            "virtual_available_bytes must be a non-negative integer",
            "virtual_available_bytes",
        ),
        (
            disk_valid,
            "DISK_FREE_INVALID",
            "disk_free_bytes must be a non-negative integer",
            "disk_free_bytes",
        ),
    )
    for valid, code, message, field in validations:
        if not valid:
            _reason(report, code, message, field=field)


def evaluate_coarse_resource_gate(
    samples: Sequence[Mapping[str, Any]],
    *,
    target_elements: int,
    physical_total_bytes: int,
    physical_available_bytes: int,
    virtual_available_bytes: int,
    disk_free_bytes: int,
    maximum_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Project a target mesh conservatively and evaluate a physical resource gate.

    The upper bound for each metric is the larger of a through-origin linear
    envelope and a power-law envelope using the largest observed pairwise
    exponent (never less than one).  Pagefile-backed virtual memory is checked,
    but it can never compensate for failed physical-memory checks.
    """

    report = _base_report(
        target_elements=target_elements,
        physical_total_bytes=physical_total_bytes,
        physical_available_bytes=physical_available_bytes,
        virtual_available_bytes=virtual_available_bytes,
        disk_free_bytes=disk_free_bytes,
        maximum_elapsed_seconds=maximum_elapsed_seconds,
    )
    normalized = _normalize_samples(report, samples)
    _validate_system_inputs(
        report,
        physical_total_bytes=physical_total_bytes,
        physical_available_bytes=physical_available_bytes,
        virtual_available_bytes=virtual_available_bytes,
        disk_free_bytes=disk_free_bytes,
    )

    target_valid = _positive_int(target_elements)
    report["checks"]["target_elements"] = target_valid
    if not target_valid:
        _reason(
            report,
            "TARGET_ELEMENTS_INVALID",
            "target_elements must be a positive integer",
            field="target_elements",
        )
    valid_element_counts = [
        item["elements"]
        for item in normalized
        if isinstance(item.get("elements"), int) and item["elements"] > 0
    ]
    target_is_extrapolation = bool(
        target_valid
        and valid_element_counts
        and target_elements >= max(valid_element_counts)
    )
    report["checks"]["target_not_below_calibration_max"] = target_is_extrapolation
    if target_valid and valid_element_counts and not target_is_extrapolation:
        _reason(
            report,
            "TARGET_BELOW_CALIBRATION_MAX",
            "target_elements must not be below the largest calibration sample",
            field="target_elements",
        )

    if maximum_elapsed_seconds is None:
        report["checks"]["elapsed_budget"] = None
        report["time_gate_status"] = "NOT_GATED"
        _warning(
            report,
            "TIME_NOT_GATED",
            "no maximum_elapsed_seconds was supplied; elapsed time is projected but not a PASS/FAIL gate",
        )
        time_input_valid = True
    else:
        time_input_valid = _positive_finite(maximum_elapsed_seconds)
        if not time_input_valid:
            _reason(
                report,
                "ELAPSED_BUDGET_INVALID",
                "maximum_elapsed_seconds must be a positive finite number or None",
                field="maximum_elapsed_seconds",
            )

    can_project = (
        report["checks"]["calibration_sample_count"]
        and report["checks"]["calibration_scale_count"]
        and report["checks"]["calibration_values"]
        and target_is_extrapolation
    )
    if can_project:
        try:
            report["projections"] = {
                metric: _project_metric(normalized, target_elements, metric)
                for metric in (
                    "peak_working_set_bytes",
                    "elapsed_seconds",
                    "output_bytes",
                )
            }
        except (OverflowError, ValueError) as exc:
            _reason(report, "PROJECTION_NONFINITE", str(exc))
            report["projections"] = {}
            can_project = False

    if can_project:
        projected_peak = int(
            report["projections"]["peak_working_set_bytes"]["upper_bound"]
        )
        projected_output = int(report["projections"]["output_bytes"]["upper_bound"])
        projected_elapsed = float(
            report["projections"]["elapsed_seconds"]["upper_bound"]
        )
        safe_peak = math.ceil(projected_peak * SAFETY_FACTOR)
        safe_elapsed = projected_elapsed * SAFETY_FACTOR
        physical_required = safe_peak + OS_RESERVE_BYTES
        virtual_required = safe_peak
        disk_required = math.ceil(projected_output * SAFETY_FACTOR)
        report["requirements"] = {
            "projected_peak_with_safety_bytes": safe_peak,
            "physical_bytes_including_os_reserve": physical_required,
            "virtual_available_bytes": virtual_required,
            "disk_free_bytes": disk_required,
            "elapsed_seconds": safe_elapsed,
        }

        if report["checks"]["physical_total_input"]:
            passed = physical_total_bytes >= physical_required
            report["checks"]["physical_total"] = passed
            if not passed:
                _reason(
                    report,
                    "PHYSICAL_TOTAL_INSUFFICIENT",
                    f"physical total {physical_total_bytes} is below required {physical_required}",
                    field="physical_total_bytes",
                )
        if report["checks"]["physical_available_input"]:
            passed = physical_available_bytes >= physical_required
            report["checks"]["physical_available"] = passed
            if not passed:
                _reason(
                    report,
                    "PHYSICAL_AVAILABLE_INSUFFICIENT",
                    f"physical available {physical_available_bytes} is below required {physical_required}",
                    field="physical_available_bytes",
                )
        if report["checks"]["virtual_available_input"]:
            passed = virtual_available_bytes >= virtual_required
            report["checks"]["virtual_available"] = passed
            if not passed:
                _reason(
                    report,
                    "VIRTUAL_AVAILABLE_INSUFFICIENT",
                    f"virtual available {virtual_available_bytes} is below required {virtual_required}",
                    field="virtual_available_bytes",
                )
        if report["checks"]["disk_free_input"]:
            passed = disk_free_bytes >= disk_required
            report["checks"]["disk_free"] = passed
            if not passed:
                _reason(
                    report,
                    "DISK_FREE_INSUFFICIENT",
                    f"disk free {disk_free_bytes} is below required {disk_required}",
                    field="disk_free_bytes",
                )
        if maximum_elapsed_seconds is not None and time_input_valid:
            passed = safe_elapsed <= float(maximum_elapsed_seconds)
            report["checks"]["elapsed_budget"] = passed
            report["time_gate_status"] = "PASS" if passed else "FAIL"
            if not passed:
                _reason(
                    report,
                    "ELAPSED_BUDGET_EXCEEDED",
                    f"projected elapsed with safety factor {safe_elapsed} exceeds budget {maximum_elapsed_seconds}",
                    field="maximum_elapsed_seconds",
                )

        physical_failed = (
            report["checks"]["physical_total"] is False
            or report["checks"]["physical_available"] is False
        )
        if physical_failed and report["checks"]["virtual_available"] is True:
            _reason(
                report,
                "PAGEFILE_NOT_ACCEPTED",
                "virtual memory is sufficient but cannot substitute for failed physical-memory gates",
            )

    report["status"] = "PASS" if not report["reasons"] else "FAIL"
    return report


__all__ = [
    "MINIMUM_CALIBRATION_SAMPLES",
    "OS_RESERVE_BYTES",
    "REPORT_SCHEMA",
    "SAFETY_FACTOR",
    "evaluate_coarse_resource_gate",
]
