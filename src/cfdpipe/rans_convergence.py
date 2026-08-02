"""Deterministic convergence and steady-oscillation gate for formal RANS runs.

The evaluator in this module is intentionally independent of SU2, ParaView,
the filesystem, and wall-clock time.  A pipeline supplies ordered checkpoint
records plus an external criteria mapping and receives a JSON-safe report.
No engineering threshold is embedded in the implementation.

The input checkpoints are expected to be samples from one numerical stage
(normally the second-order restart stage).  ``window_size`` therefore counts
checkpoints, not solver iterations.  Formal orchestration is responsible for
creating those checkpoints and for binding them to solver/post-processing
manifests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


REPORT_SCHEMA = "cfdpipe.rans_convergence_report.v1"

CONVERGED = "CONVERGED"
MAX_ITER_NOT_CONVERGED = "MAX_ITER_NOT_CONVERGED"
URANS_REVIEW_REQUIRED = "URANS_REVIEW_REQUIRED"
FAIL = "FAIL"

LOAD_COMPONENTS = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz")
MEASUREMENT_SCALARS = (
    "net_mass_flow_kg_s",
    "forward_mass_flow_kg_s",
    "mass_weighted_mach",
    "mass_weighted_static_pressure_pa",
    "mass_weighted_static_temperature_k",
    "mass_weighted_total_pressure_pa",
    "total_pressure_recovery",
    "area_m2",
    "backflow_mass_fraction",
)

_SERIES_SPEC_KEYS = {
    "absolute_tolerance",
    "relative_tolerance",
    "minimum",
    "maximum",
}
_OUTLET_VALUE_KEYS = (
    "minimum_normal_mach_margin",
    "subsonic_area_fraction",
    "nonpositive_area_fraction",
    "backflow_area_fraction",
    "reverse_mass_flow_kg_s",
)


class RANSConvergenceError(ValueError):
    """Raised internally when a criteria or checkpoint contract is invalid."""


def _base_report() -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "status": FAIL,
        "convergence_claimed": False,
        "sample_count": 0,
        "latest_iteration": None,
        "maximum_iterations": None,
        "maximum_iterations_reached": False,
        "window": {},
        "checks": {
            "input": "FAIL",
            "hard_gates": "NOT_EVALUATED",
            "joint_stability": "NOT_EVALUATED",
            "residuals": "NOT_EVALUATED",
            "oscillation": "NOT_EVALUATED",
        },
        "series": {},
        "residuals": {},
        "hard_gates": {},
        "oscillation": {
            "detected": False,
            "detected_signals": [],
            "signals": {},
        },
        "failures": [],
    }


def _failure(
    report: dict[str, Any],
    code: str,
    message: str,
    **evidence: Any,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    item.update(evidence)
    report["failures"].append(item)


def _finite_number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RANSConvergenceError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise RANSConvergenceError(f"{label} must be finite")
    return number


def _nonnegative(value: Any, *, label: str) -> float:
    number = _finite_number(value, label=label)
    if number < 0.0:
        raise RANSConvergenceError(f"{label} must be non-negative")
    return number


def _positive_int(value: Any, *, label: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RANSConvergenceError(
            f"{label} must be an integer greater than or equal to {minimum}"
        )
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RANSConvergenceError(f"{label} must be a mapping")
    return value


def _required(mapping: Mapping[str, Any], key: str, *, label: str) -> Any:
    if key not in mapping:
        raise RANSConvergenceError(f"{label} lacks required key {key!r}")
    return mapping[key]


def _find_nonfinite(value: Any, location: str = "$") -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return location
    if isinstance(value, Mapping):
        for key, child in value.items():
            found = _find_nonfinite(child, f"{location}.{key}")
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            found = _find_nonfinite(child, f"{location}[{index}]")
            if found is not None:
                return found
    return None


def _normalize_series_spec(value: Any, *, label: str) -> dict[str, float | None]:
    raw = _mapping(value, label=label)
    unknown = sorted(set(raw) - _SERIES_SPEC_KEYS)
    if unknown:
        raise RANSConvergenceError(
            f"{label} has unsupported keys: {', '.join(unknown)}"
        )
    absolute = _nonnegative(
        _required(raw, "absolute_tolerance", label=label),
        label=f"{label}.absolute_tolerance",
    )
    relative = _nonnegative(
        _required(raw, "relative_tolerance", label=label),
        label=f"{label}.relative_tolerance",
    )
    minimum = (
        None
        if "minimum" not in raw
        else _finite_number(raw["minimum"], label=f"{label}.minimum")
    )
    maximum = (
        None
        if "maximum" not in raw
        else _finite_number(raw["maximum"], label=f"{label}.maximum")
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise RANSConvergenceError(f"{label} minimum exceeds maximum")
    return {
        "absolute_tolerance": absolute,
        "relative_tolerance": relative,
        "minimum": minimum,
        "maximum": maximum,
    }


def _normalize_named_series(
    value: Any,
    *,
    label: str,
    required_names: Sequence[str],
) -> dict[str, dict[str, float | None]]:
    raw = _mapping(value, label=label)
    expected = set(required_names)
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise RANSConvergenceError(f"{label} fields differ: {'; '.join(details)}")
    return {
        name: _normalize_series_spec(raw[name], label=f"{label}.{name}")
        for name in required_names
    }


def _normalize_criteria(criteria: Any) -> dict[str, Any]:
    raw = _mapping(criteria, label="criteria")
    required_root = {
        "maximum_iterations",
        "window_size",
        "required_consecutive_windows",
        "loads",
        "measurement",
        "mass_balance",
        "outlets",
        "yplus",
        "residuals",
        "oscillation",
    }
    missing = sorted(required_root - set(raw))
    if missing:
        raise RANSConvergenceError(
            "criteria lacks required sections: " + ", ".join(missing)
        )

    maximum_iterations = _positive_int(
        raw["maximum_iterations"], label="criteria.maximum_iterations"
    )
    window_size = _positive_int(
        raw["window_size"], label="criteria.window_size", minimum=2
    )
    required_windows = _positive_int(
        raw["required_consecutive_windows"],
        label="criteria.required_consecutive_windows",
        minimum=2,
    )
    loads = _normalize_named_series(
        raw["loads"], label="criteria.loads", required_names=LOAD_COMPONENTS
    )
    measurement = _normalize_named_series(
        raw["measurement"],
        label="criteria.measurement",
        required_names=MEASUREMENT_SCALARS,
    )

    mass_balance = _mapping(raw["mass_balance"], label="criteria.mass_balance")
    maximum_mass_imbalance = _nonnegative(
        _required(
            mass_balance, "maximum_relative_imbalance", label="criteria.mass_balance"
        ),
        label="criteria.mass_balance.maximum_relative_imbalance",
    )

    outlet = _mapping(raw["outlets"], label="criteria.outlets")
    names = _required(outlet, "names", label="criteria.outlets")
    if (
        isinstance(names, (str, bytes))
        or not isinstance(names, Sequence)
        or not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        raise RANSConvergenceError(
            "criteria.outlets.names must contain unique non-empty strings"
        )
    outlet_names = tuple(names)
    outlet_thresholds = {
        "minimum_normal_mach_margin": _nonnegative(
            _required(
                outlet, "minimum_normal_mach_margin", label="criteria.outlets"
            ),
            label="criteria.outlets.minimum_normal_mach_margin",
        ),
        "maximum_subsonic_area_fraction": _nonnegative(
            _required(
                outlet, "maximum_subsonic_area_fraction", label="criteria.outlets"
            ),
            label="criteria.outlets.maximum_subsonic_area_fraction",
        ),
        "maximum_nonpositive_area_fraction": _nonnegative(
            _required(
                outlet,
                "maximum_nonpositive_area_fraction",
                label="criteria.outlets",
            ),
            label="criteria.outlets.maximum_nonpositive_area_fraction",
        ),
        "maximum_backflow_area_fraction": _nonnegative(
            _required(
                outlet, "maximum_backflow_area_fraction", label="criteria.outlets"
            ),
            label="criteria.outlets.maximum_backflow_area_fraction",
        ),
        "maximum_reverse_mass_flow_kg_s": _nonnegative(
            _required(
                outlet,
                "maximum_reverse_mass_flow_kg_s",
                label="criteria.outlets",
            ),
            label="criteria.outlets.maximum_reverse_mass_flow_kg_s",
        ),
    }
    for field in (
        "maximum_subsonic_area_fraction",
        "maximum_nonpositive_area_fraction",
        "maximum_backflow_area_fraction",
    ):
        if outlet_thresholds[field] > 1.0:
            raise RANSConvergenceError(
                f"criteria.outlets.{field} must not exceed one"
            )

    yplus = _mapping(raw["yplus"], label="criteria.yplus")
    yplus_thresholds = {
        "maximum": _nonnegative(
            _required(yplus, "maximum", label="criteria.yplus"),
            label="criteria.yplus.maximum",
        ),
        "maximum_exceedance_area_fraction": _nonnegative(
            _required(
                yplus,
                "maximum_exceedance_area_fraction",
                label="criteria.yplus",
            ),
            label="criteria.yplus.maximum_exceedance_area_fraction",
        ),
    }
    if yplus_thresholds["maximum"] <= 0.0:
        raise RANSConvergenceError("criteria.yplus.maximum must be positive")
    if yplus_thresholds["maximum_exceedance_area_fraction"] > 1.0:
        raise RANSConvergenceError(
            "criteria.yplus.maximum_exceedance_area_fraction must not exceed one"
        )

    residual_raw = _mapping(raw["residuals"], label="criteria.residuals")
    if not residual_raw:
        raise RANSConvergenceError("criteria.residuals must not be empty")
    residuals: dict[str, dict[str, float]] = {}
    for name, value in residual_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise RANSConvergenceError("criteria residual names must be non-empty")
        spec = _mapping(value, label=f"criteria.residuals.{name}")
        residuals[name] = {
            "maximum_final": _finite_number(
                _required(spec, "maximum_final", label=f"criteria.residuals.{name}"),
                label=f"criteria.residuals.{name}.maximum_final",
            ),
            "minimum_reduction": _nonnegative(
                _required(
                    spec, "minimum_reduction", label=f"criteria.residuals.{name}"
                ),
                label=f"criteria.residuals.{name}.minimum_reduction",
            ),
        }

    oscillation = _mapping(raw["oscillation"], label="criteria.oscillation")
    minimum_samples = _positive_int(
        _required(
            oscillation, "minimum_samples", label="criteria.oscillation"
        ),
        label="criteria.oscillation.minimum_samples",
        minimum=6,
    )
    minimum_cycles = _positive_int(
        _required(oscillation, "minimum_cycles", label="criteria.oscillation"),
        label="criteria.oscillation.minimum_cycles",
        minimum=2,
    )
    minimum_period = _positive_int(
        _required(
            oscillation, "minimum_period_samples", label="criteria.oscillation"
        ),
        label="criteria.oscillation.minimum_period_samples",
        minimum=2,
    )
    maximum_period = _positive_int(
        _required(
            oscillation, "maximum_period_samples", label="criteria.oscillation"
        ),
        label="criteria.oscillation.maximum_period_samples",
        minimum=2,
    )
    if minimum_period > maximum_period:
        raise RANSConvergenceError(
            "criteria oscillation minimum period exceeds maximum period"
        )
    if minimum_samples < maximum_period * minimum_cycles:
        raise RANSConvergenceError(
            "criteria oscillation sample count cannot contain the requested cycles"
        )
    minimum_autocorrelation = _finite_number(
        _required(
            oscillation, "minimum_autocorrelation", label="criteria.oscillation"
        ),
        label="criteria.oscillation.minimum_autocorrelation",
    )
    if not 0.0 < minimum_autocorrelation <= 1.0:
        raise RANSConvergenceError(
            "criteria.oscillation.minimum_autocorrelation must be in (0, 1]"
        )
    retention = _finite_number(
        _required(
            oscillation,
            "minimum_amplitude_retention_ratio",
            label="criteria.oscillation",
        ),
        label="criteria.oscillation.minimum_amplitude_retention_ratio",
    )
    if not 0.0 <= retention <= 1.0:
        raise RANSConvergenceError(
            "criteria oscillation amplitude retention must be in [0, 1]"
        )

    return {
        "maximum_iterations": maximum_iterations,
        "window_size": window_size,
        "required_consecutive_windows": required_windows,
        "loads": loads,
        "measurement": measurement,
        "mass_balance": {
            "maximum_relative_imbalance": maximum_mass_imbalance
        },
        "outlets": {"names": outlet_names, **outlet_thresholds},
        "yplus": yplus_thresholds,
        "residuals": residuals,
        "oscillation": {
            "minimum_samples": minimum_samples,
            "minimum_cycles": minimum_cycles,
            "minimum_period_samples": minimum_period,
            "maximum_period_samples": maximum_period,
            "minimum_autocorrelation": minimum_autocorrelation,
            "minimum_amplitude_retention_ratio": retention,
        },
    }


def _checkpoint_number(
    mapping: Mapping[str, Any], key: str, *, label: str
) -> float:
    return _finite_number(
        _required(mapping, key, label=label), label=f"{label}.{key}"
    )


def _normalize_checkpoints(
    checkpoints: Any, criteria: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if (
        isinstance(checkpoints, (str, bytes))
        or not isinstance(checkpoints, Sequence)
        or not checkpoints
    ):
        raise RANSConvergenceError("checkpoints must be a non-empty sequence")
    nonfinite = _find_nonfinite(checkpoints, "checkpoints")
    if nonfinite is not None:
        raise RANSConvergenceError(f"checkpoint data contains NaN or Inf at {nonfinite}")

    expected_outlets = set(criteria["outlets"]["names"])
    residual_names = tuple(criteria["residuals"])
    normalized: list[dict[str, Any]] = []
    previous_iteration = 0
    for index, value in enumerate(checkpoints):
        label = f"checkpoints[{index}]"
        raw = _mapping(value, label=label)
        iteration = _positive_int(
            _required(raw, "iteration", label=label), label=f"{label}.iteration"
        )
        if iteration <= previous_iteration:
            raise RANSConvergenceError("checkpoint iterations must be strictly increasing")
        if iteration > criteria["maximum_iterations"]:
            raise RANSConvergenceError(
                "checkpoint iteration exceeds criteria.maximum_iterations"
            )
        previous_iteration = iteration

        raw_loads = _mapping(_required(raw, "loads", label=label), label=f"{label}.loads")
        missing_loads = sorted(set(LOAD_COMPONENTS) - set(raw_loads))
        if missing_loads:
            raise RANSConvergenceError(
                f"{label}.loads lacks six-component values: {', '.join(missing_loads)}"
            )
        loads = {
            name: _checkpoint_number(raw_loads, name, label=f"{label}.loads")
            for name in LOAD_COMPONENTS
        }

        raw_measurement = _mapping(
            _required(raw, "measurement", label=label),
            label=f"{label}.measurement",
        )
        missing_measurement = sorted(
            set(MEASUREMENT_SCALARS) - set(raw_measurement)
        )
        if missing_measurement:
            raise RANSConvergenceError(
                f"{label}.measurement lacks scalars: {', '.join(missing_measurement)}"
            )
        measurement = {
            name: _checkpoint_number(
                raw_measurement, name, label=f"{label}.measurement"
            )
            for name in MEASUREMENT_SCALARS
        }

        raw_mass = _mapping(
            _required(raw, "mass_balance", label=label),
            label=f"{label}.mass_balance",
        )
        mass_balance = {
            "relative_global_imbalance": _checkpoint_number(
                raw_mass,
                "relative_global_imbalance",
                label=f"{label}.mass_balance",
            )
        }
        if mass_balance["relative_global_imbalance"] < 0.0:
            raise RANSConvergenceError(
                f"{label}.mass_balance.relative_global_imbalance is negative"
            )

        raw_outlets = _mapping(
            _required(raw, "outlets", label=label), label=f"{label}.outlets"
        )
        if set(raw_outlets) != expected_outlets:
            raise RANSConvergenceError(
                f"{label}.outlets differs from the configured outlet names"
            )
        outlets: dict[str, dict[str, float]] = {}
        for name in criteria["outlets"]["names"]:
            raw_outlet = _mapping(raw_outlets[name], label=f"{label}.outlets.{name}")
            outlets[name] = {
                field: _checkpoint_number(
                    raw_outlet, field, label=f"{label}.outlets.{name}"
                )
                for field in _OUTLET_VALUE_KEYS
            }
            for field in (
                "subsonic_area_fraction",
                "nonpositive_area_fraction",
                "backflow_area_fraction",
            ):
                if not 0.0 <= outlets[name][field] <= 1.0:
                    raise RANSConvergenceError(
                        f"{label}.outlets.{name}.{field} is outside [0, 1]"
                    )
            if outlets[name]["reverse_mass_flow_kg_s"] < 0.0:
                raise RANSConvergenceError(
                    f"{label}.outlets.{name}.reverse_mass_flow_kg_s is negative"
                )

        raw_yplus = _mapping(
            _required(raw, "yplus", label=label), label=f"{label}.yplus"
        )
        all_finite = _required(raw_yplus, "all_finite", label=f"{label}.yplus")
        if not isinstance(all_finite, bool):
            raise RANSConvergenceError(f"{label}.yplus.all_finite must be boolean")
        yplus = {
            "all_finite": all_finite,
            "maximum": _checkpoint_number(
                raw_yplus, "maximum", label=f"{label}.yplus"
            ),
            "above_maximum_area_fraction": _checkpoint_number(
                raw_yplus,
                "above_maximum_area_fraction",
                label=f"{label}.yplus",
            ),
        }
        if yplus["maximum"] < 0.0:
            raise RANSConvergenceError(f"{label}.yplus.maximum is negative")
        if not 0.0 <= yplus["above_maximum_area_fraction"] <= 1.0:
            raise RANSConvergenceError(
                f"{label}.yplus.above_maximum_area_fraction is outside [0, 1]"
            )

        raw_residuals = _mapping(
            _required(raw, "residuals", label=label), label=f"{label}.residuals"
        )
        missing_residuals = sorted(set(residual_names) - set(raw_residuals))
        if missing_residuals:
            raise RANSConvergenceError(
                f"{label}.residuals lacks fields: {', '.join(missing_residuals)}"
            )
        residuals = {
            name: _checkpoint_number(
                raw_residuals, name, label=f"{label}.residuals"
            )
            for name in residual_names
        }
        normalized.append(
            {
                "iteration": iteration,
                "loads": loads,
                "measurement": measurement,
                "mass_balance": mass_balance,
                "outlets": outlets,
                "yplus": yplus,
                "residuals": residuals,
            }
        )
    return normalized


def _allowed_variation(mean: float, spec: Mapping[str, float | None]) -> tuple[float, bool]:
    absolute = float(spec["absolute_tolerance"])
    relative = float(spec["relative_tolerance"]) * abs(mean)
    return max(absolute, relative), absolute >= relative


def _series_summary(
    values: Sequence[float],
    spec: Mapping[str, float | None],
    *,
    window_size: int,
    required_windows: int,
) -> dict[str, Any]:
    required_samples = window_size * required_windows
    minimum = spec["minimum"]
    maximum = spec["maximum"]
    limit_values = (
        list(values[-required_samples:])
        if len(values) >= required_samples
        else list(values)
    )
    limit_offset = len(values) - len(limit_values)
    limit_failures = []
    for local_index, value in enumerate(limit_values):
        index = limit_offset + local_index
        if minimum is not None and value < float(minimum):
            limit_failures.append(
                {"sample_index": index, "value": value, "limit": "minimum"}
            )
        if maximum is not None and value > float(maximum):
            limit_failures.append(
                {"sample_index": index, "value": value, "limit": "maximum"}
            )
    if len(values) < required_samples:
        return {
            "status": "INSUFFICIENT_DATA",
            "sample_count": len(values),
            "required_sample_count": required_samples,
            "limits_passed": not limit_failures,
            "limit_failures": limit_failures,
            "windows": [],
            "window_mean_drifts": [],
        }

    tail = list(values[-required_samples:])
    windows = []
    means = []
    ranges_passed = True
    for window_index in range(required_windows):
        start = window_index * window_size
        window = tail[start : start + window_size]
        mean = sum(window) / len(window)
        observed_range = max(window) - min(window)
        allowed, absolute_controls = _allowed_variation(mean, spec)
        passed = observed_range <= allowed
        means.append(mean)
        ranges_passed = ranges_passed and passed
        windows.append(
            {
                "index": window_index,
                "mean": mean,
                "minimum": min(window),
                "maximum": max(window),
                "range": observed_range,
                "allowed_variation": allowed,
                "absolute_gate_controls": absolute_controls,
                "status": "PASS" if passed else "FAIL",
            }
        )
    drifts = []
    drifts_passed = True
    for index in range(1, len(means)):
        previous = means[index - 1]
        current = means[index]
        drift = abs(current - previous)
        allowed, absolute_controls = _allowed_variation(
            max((previous, current), key=abs), spec
        )
        passed = drift <= allowed
        drifts_passed = drifts_passed and passed
        drifts.append(
            {
                "from_window": index - 1,
                "to_window": index,
                "absolute_drift": drift,
                "allowed_variation": allowed,
                "absolute_gate_controls": absolute_controls,
                "status": "PASS" if passed else "FAIL",
            }
        )
    passed = ranges_passed and drifts_passed and not limit_failures
    return {
        "status": "PASS" if passed else "FAIL",
        "sample_count": len(values),
        "required_sample_count": required_samples,
        "limits_passed": not limit_failures,
        "limit_failures": limit_failures,
        "windows": windows,
        "window_mean_drifts": drifts,
    }


def _linear_detrend(values: Sequence[float]) -> list[float]:
    count = len(values)
    center = 0.5 * (count - 1)
    mean = sum(values) / count
    denominator = sum((index - center) ** 2 for index in range(count))
    slope = (
        0.0
        if denominator == 0.0
        else sum(
            (index - center) * (value - mean)
            for index, value in enumerate(values)
        )
        / denominator
    )
    return [
        value - (mean + slope * (index - center))
        for index, value in enumerate(values)
    ]


def _autocorrelation(values: Sequence[float], lag: int) -> float | None:
    left = values[:-lag]
    right = values[lag:]
    left_energy = sum(value * value for value in left)
    right_energy = sum(value * value for value in right)
    if left_energy == 0.0 or right_energy == 0.0:
        return None
    return sum(a * b for a, b in zip(left, right, strict=True)) / math.sqrt(
        left_energy * right_energy
    )


def _rms(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values))


def _oscillation_summary(
    values: Sequence[float],
    spec: Mapping[str, float | None],
    criteria: Mapping[str, Any],
) -> dict[str, Any]:
    minimum_samples = int(criteria["minimum_samples"])
    if len(values) < minimum_samples:
        return {
            "status": "INSUFFICIENT_DATA",
            "sample_count": len(values),
            "required_sample_count": minimum_samples,
            "persistent": False,
        }
    samples = list(values[-minimum_samples:])
    detrended = _linear_detrend(samples)
    peak_to_peak = max(detrended) - min(detrended)
    mean = sum(samples) / len(samples)
    amplitude_threshold, absolute_controls = _allowed_variation(mean, spec)
    significant = peak_to_peak > amplitude_threshold

    split = len(detrended) // 2
    early_rms = _rms(detrended[:split])
    late_rms = _rms(detrended[split:])
    retention = None if early_rms == 0.0 else late_rms / early_rms

    best_period = None
    best_correlation = None
    for lag in range(
        int(criteria["minimum_period_samples"]),
        int(criteria["maximum_period_samples"]) + 1,
    ):
        if len(detrended) < lag * int(criteria["minimum_cycles"]):
            continue
        correlation = _autocorrelation(detrended, lag)
        if correlation is not None and (
            best_correlation is None or correlation > best_correlation
        ):
            best_period = lag
            best_correlation = correlation
    correlation_passed = (
        best_correlation is not None
        and best_correlation >= float(criteria["minimum_autocorrelation"])
    )
    retention_passed = (
        retention is not None
        and retention
        >= float(criteria["minimum_amplitude_retention_ratio"])
    )
    persistent = significant and correlation_passed and retention_passed
    return {
        "status": "PERSISTENT_PERIODIC" if persistent else "NOT_PERSISTENT",
        "sample_count": len(samples),
        "required_sample_count": minimum_samples,
        "detrended_peak_to_peak": peak_to_peak,
        "significant_amplitude_threshold": amplitude_threshold,
        "absolute_gate_controls": absolute_controls,
        "amplitude_significant": significant,
        "early_detrended_rms": early_rms,
        "late_detrended_rms": late_rms,
        "amplitude_retention_ratio": retention,
        "minimum_amplitude_retention_ratio": criteria[
            "minimum_amplitude_retention_ratio"
        ],
        "best_period_samples": best_period,
        "best_autocorrelation": best_correlation,
        "minimum_autocorrelation": criteria["minimum_autocorrelation"],
        "persistent": persistent,
    }


def _hard_gate_failures(
    report: dict[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    criteria: Mapping[str, Any],
) -> None:
    maximum_mass = float(criteria["mass_balance"]["maximum_relative_imbalance"])
    outlet_criteria = criteria["outlets"]
    yplus_criteria = criteria["yplus"]
    for checkpoint in checkpoints:
        iteration = checkpoint["iteration"]
        mass = checkpoint["mass_balance"]["relative_global_imbalance"]
        if mass > maximum_mass:
            _failure(
                report,
                "GLOBAL_MASS_IMBALANCE",
                "global relative mass imbalance exceeds its configured maximum",
                iteration=iteration,
                observed=mass,
                allowed=maximum_mass,
            )
        for name in outlet_criteria["names"]:
            outlet = checkpoint["outlets"][name]
            if (
                outlet["minimum_normal_mach_margin"]
                < outlet_criteria["minimum_normal_mach_margin"]
            ):
                _failure(
                    report,
                    "OUTLET_MACH_MARGIN",
                    "outlet normal-Mach margin is below its configured minimum",
                    iteration=iteration,
                    outlet=name,
                    observed=outlet["minimum_normal_mach_margin"],
                    allowed=outlet_criteria["minimum_normal_mach_margin"],
                )
            for field, threshold, code in (
                (
                    "subsonic_area_fraction",
                    "maximum_subsonic_area_fraction",
                    "OUTLET_SUBSONIC_AREA",
                ),
                (
                    "nonpositive_area_fraction",
                    "maximum_nonpositive_area_fraction",
                    "OUTLET_NONPOSITIVE_AREA",
                ),
                (
                    "backflow_area_fraction",
                    "maximum_backflow_area_fraction",
                    "OUTLET_BACKFLOW",
                ),
                (
                    "reverse_mass_flow_kg_s",
                    "maximum_reverse_mass_flow_kg_s",
                    "OUTLET_REVERSE_MASS_FLOW",
                ),
            ):
                if outlet[field] > outlet_criteria[threshold]:
                    _failure(
                        report,
                        code,
                        f"outlet {field} exceeds its configured maximum",
                        iteration=iteration,
                        outlet=name,
                        observed=outlet[field],
                        allowed=outlet_criteria[threshold],
                    )
        yplus = checkpoint["yplus"]
        if not yplus["all_finite"]:
            _failure(
                report,
                "YPLUS_NONFINITE",
                "wall y+ evidence is not entirely finite",
                iteration=iteration,
            )
        if yplus["maximum"] >= yplus_criteria["maximum"]:
            _failure(
                report,
                "YPLUS_MAXIMUM",
                "wall y+ maximum does not remain strictly below its configured limit",
                iteration=iteration,
                observed=yplus["maximum"],
                allowed=yplus_criteria["maximum"],
            )
        if (
            yplus["above_maximum_area_fraction"]
            > yplus_criteria["maximum_exceedance_area_fraction"]
        ):
            _failure(
                report,
                "YPLUS_EXCEEDANCE_AREA",
                "wall y+ exceedance area exceeds its configured maximum",
                iteration=iteration,
                observed=yplus["above_maximum_area_fraction"],
                allowed=yplus_criteria["maximum_exceedance_area_fraction"],
            )


def evaluate_rans_convergence(
    checkpoints: Sequence[Mapping[str, Any]],
    criteria: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one ordered checkpoint series against external criteria.

    The function never mutates its arguments.  Malformed/non-finite evidence,
    hard physical-gate failures, or premature termination return ``FAIL``.
    Finite evidence that reaches the configured iteration limit without joint
    stability returns ``MAX_ITER_NOT_CONVERGED``.  Significant, detrended,
    autocorrelated oscillation whose amplitude does not decay returns
    ``URANS_REVIEW_REQUIRED``.  Only every joint gate passing returns
    ``CONVERGED`` and sets ``convergence_claimed`` true.
    """

    report = _base_report()
    try:
        normalized_criteria = _normalize_criteria(criteria)
    except RANSConvergenceError as error:
        _failure(report, "CONFIG_ERROR", str(error))
        return report

    report["maximum_iterations"] = normalized_criteria["maximum_iterations"]
    report["window"] = {
        "size": normalized_criteria["window_size"],
        "required_consecutive": normalized_criteria[
            "required_consecutive_windows"
        ],
        "required_sample_count": normalized_criteria["window_size"]
        * normalized_criteria["required_consecutive_windows"],
    }
    try:
        normalized_checkpoints = _normalize_checkpoints(
            checkpoints, normalized_criteria
        )
    except RANSConvergenceError as error:
        _failure(report, "DATA_ERROR", str(error))
        return report

    report["checks"]["input"] = "PASS"
    report["sample_count"] = len(normalized_checkpoints)
    report["latest_iteration"] = normalized_checkpoints[-1]["iteration"]
    at_limit = (
        normalized_checkpoints[-1]["iteration"]
        == normalized_criteria["maximum_iterations"]
    )
    report["maximum_iterations_reached"] = at_limit
    required_samples = report["window"]["required_sample_count"]
    tail = normalized_checkpoints[-required_samples:]

    stability_passed = len(normalized_checkpoints) >= required_samples
    for group, names in (
        ("loads", LOAD_COMPONENTS),
        ("measurement", MEASUREMENT_SCALARS),
    ):
        for name in names:
            values = [checkpoint[group][name] for checkpoint in normalized_checkpoints]
            summary = _series_summary(
                values,
                normalized_criteria[group][name],
                window_size=normalized_criteria["window_size"],
                required_windows=normalized_criteria[
                    "required_consecutive_windows"
                ],
            )
            signal = f"{group}.{name}"
            report["series"][signal] = summary
            stability_passed = stability_passed and summary["status"] == "PASS"
            for failure in summary["limit_failures"]:
                _failure(
                    report,
                    "SERIES_LIMIT",
                    f"{signal} violates a caller-configured hard limit",
                    signal=signal,
                    **failure,
                )
    if len(normalized_checkpoints) < required_samples:
        report["checks"]["joint_stability"] = "INSUFFICIENT_DATA"
    else:
        report["checks"]["joint_stability"] = (
            "PASS" if stability_passed else "FAIL"
        )

    residuals_passed = True
    for name, spec in normalized_criteria["residuals"].items():
        values = [checkpoint["residuals"][name] for checkpoint in normalized_checkpoints]
        initial = values[0]
        final = values[-1]
        reduction = initial - final
        final_passed = final <= spec["maximum_final"]
        reduction_passed = reduction >= spec["minimum_reduction"]
        passed = final_passed and reduction_passed
        residuals_passed = residuals_passed and passed
        report["residuals"][name] = {
            "status": "PASS" if passed else "FAIL",
            "all_finite": True,
            "initial": initial,
            "final": final,
            "reduction": reduction,
            "maximum_final": spec["maximum_final"],
            "minimum_reduction": spec["minimum_reduction"],
            "final_passed": final_passed,
            "reduction_passed": reduction_passed,
        }
    report["checks"]["residuals"] = "PASS" if residuals_passed else "FAIL"

    hard_failure_start = len(report["failures"])
    _hard_gate_failures(report, tail, normalized_criteria)
    hard_passed = len(report["failures"]) == hard_failure_start
    report["hard_gates"] = {
        "evaluated_sample_count": len(tail),
        "maximum_relative_global_mass_imbalance": normalized_criteria[
            "mass_balance"
        ]["maximum_relative_imbalance"],
        "outlets": {
            key: value
            for key, value in normalized_criteria["outlets"].items()
            if key != "names"
        },
        "outlet_names": list(normalized_criteria["outlets"]["names"]),
        "yplus": dict(normalized_criteria["yplus"]),
    }
    report["checks"]["hard_gates"] = "PASS" if hard_passed else "FAIL"

    oscillation_detected = False
    detected_signals = []
    oscillation_data_sufficient = (
        len(normalized_checkpoints)
        >= normalized_criteria["oscillation"]["minimum_samples"]
    )
    for group, names in (
        ("loads", LOAD_COMPONENTS),
        ("measurement", MEASUREMENT_SCALARS),
    ):
        for name in names:
            signal = f"{group}.{name}"
            values = [checkpoint[group][name] for checkpoint in normalized_checkpoints]
            summary = _oscillation_summary(
                values,
                normalized_criteria[group][name],
                normalized_criteria["oscillation"],
            )
            report["oscillation"]["signals"][signal] = summary
            if summary["persistent"]:
                oscillation_detected = True
                detected_signals.append(signal)
    report["oscillation"]["detected"] = oscillation_detected
    report["oscillation"]["detected_signals"] = detected_signals
    report["oscillation"]["criteria"] = dict(
        normalized_criteria["oscillation"]
    )
    report["checks"]["oscillation"] = (
        "INSUFFICIENT_DATA"
        if not oscillation_data_sufficient
        else ("FAIL" if oscillation_detected else "PASS")
    )

    if not hard_passed or any(
        failure["code"] == "SERIES_LIMIT" for failure in report["failures"]
    ):
        report["status"] = FAIL
    elif oscillation_detected:
        report["status"] = URANS_REVIEW_REQUIRED
        _failure(
            report,
            "PERSISTENT_PERIODIC_OSCILLATION",
            "steady checkpoint quantities contain sustained periodic oscillation",
            signals=list(detected_signals),
        )
    elif stability_passed and residuals_passed and oscillation_data_sufficient:
        report["status"] = CONVERGED
        report["convergence_claimed"] = True
    elif at_limit:
        report["status"] = MAX_ITER_NOT_CONVERGED
        _failure(
            report,
            "MAX_ITERATIONS_WITHOUT_CONVERGENCE",
            "configured maximum iteration was reached without every convergence gate",
            iteration=report["latest_iteration"],
        )
    else:
        report["status"] = FAIL
        _failure(
            report,
            "RUN_ENDED_BEFORE_CONVERGENCE_OR_LIMIT",
            "checkpoint series ended before convergence or the configured iteration limit",
            iteration=report["latest_iteration"],
        )
    return report


__all__ = [
    "CONVERGED",
    "FAIL",
    "LOAD_COMPONENTS",
    "MAX_ITER_NOT_CONVERGED",
    "MEASUREMENT_SCALARS",
    "RANSConvergenceError",
    "REPORT_SCHEMA",
    "URANS_REVIEW_REQUIRED",
    "evaluate_rans_convergence",
]
