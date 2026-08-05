#!/usr/bin/env python3
"""Evaluate one bounded first-order formal-RANS checkpoint and its evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


COMPONENTS = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz")
RESIDUALS = ("rms[Rho]", "rms[RhoU]", "rms[RhoV]", "rms[RhoW]", "rms[RhoE]", "rms[k]", "rms[w]")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(path: Path) -> str:
    for root in (path, *path.parents):
        dot_git = root / ".git"
        if dot_git.is_file():
            marker = dot_git.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir: "):
                continue
            git_dir = (root / marker.removeprefix("gitdir: ")).resolve(strict=True)
        elif dot_git.is_dir():
            git_dir = dot_git
        else:
            continue
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ")
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith(("#", "^")):
                    value, name = line.split(" ", 1)
                    if name == ref:
                        return value
        raise ValueError(f"cannot resolve Git ref {ref}")
    raise ValueError("checkpoint is not contained in a Git working tree")


def load_history(path: Path) -> list[dict[str, float]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        headers = [value.strip().strip('"') for value in next(reader)]
        rows = []
        for index, values in enumerate(reader, start=1):
            row = {key: float(value) for key, value in zip(headers, values)}
            if any(not math.isfinite(value) for value in row.values()):
                raise ValueError(f"history row {index} contains a non-finite value")
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment", required=True, type=Path)
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument(
        "--independent-initial",
        action="store_true",
        help="Record a freestream initialization with no restart input",
    )
    parser.add_argument("--index", required=True, type=int)
    parser.add_argument("--prior", type=Path)
    parser.add_argument(
        "--diagnostic-prior",
        type=Path,
        help="Most recent full-diagnostic checkpoint when light checkpoints intervene",
    )
    parser.add_argument("--total-iterations", required=True, type=int)
    parser.add_argument("--maximum-total-iterations", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.segment.resolve(strict=True)
    mesh = args.mesh.resolve(strict=True)
    if not mesh.is_file() or mesh.is_symlink():
        raise ValueError("mesh must be a non-symlink regular file")
    history = root / "history.csv"
    restart = root / "restart.dat"
    diagnostics_path = root / "paraview/rans_diagnostics.json"
    returncode_path = root / "returncode.txt"
    config_path = root / "case.cfg"
    restart_input = root / "restart_input.dat"
    stdout_path = root / "solver.stdout.log"
    stderr_path = root / "solver.stderr.log"
    if not returncode_path.is_file() or returncode_path.read_text().strip() != "0":
        raise ValueError("solver return code is missing or nonzero")
    rows = load_history(history)
    if not 300 <= len(rows) <= 500:
        raise ValueError("checkpoint history must contain 300 to 500 rows")
    if not restart.is_file() or restart.is_symlink() or restart.stat().st_size <= 0:
        raise ValueError("checkpoint restart must be a non-empty regular file")
    if args.independent_initial:
        if args.prior is not None or restart_input.exists():
            raise ValueError("independent initial checkpoint cannot have a prior restart")
    elif not restart_input.is_file() or restart_input.is_symlink():
        raise ValueError("continued checkpoint requires a non-symlink restart input")
    diagnostics = (
        json.loads(diagnostics_path.read_text(encoding="utf-8"))
        if diagnostics_path.is_file()
        else None
    )
    window = rows[-300:]
    components = {}
    window_pass = True
    prior = json.loads(args.prior.read_text(encoding="utf-8")) if args.prior else None
    diagnostic_prior = (
        json.loads(args.diagnostic_prior.read_text(encoding="utf-8"))
        if args.diagnostic_prior
        else prior
    )
    window_means = {
        name: sum(row[name] for row in window) / len(window) for name in COMPONENTS
    }
    group_scales = {
        "force": max(abs(window_means[name]) for name in COMPONENTS if name.startswith("CF")),
        "moment": max(abs(window_means[name]) for name in COMPONENTS if name.startswith("CM")),
    }
    for name in COMPONENTS:
        values = [row[name] for row in window]
        mean = window_means[name]
        span = max(values) - min(values)
        center = (len(values) - 1) / 2.0
        denominator = sum((index - center) ** 2 for index in range(len(values)))
        slope = sum(
            (index - center) * (value - mean)
            for index, value in enumerate(values)
        ) / denominator
        tolerance = 0.02 if name.startswith("CF") else 0.03
        group = "force" if name.startswith("CF") else "moment"
        required_for_convergence = abs(mean) >= 0.1 * group_scales[group]
        passed = span <= max(2.0e-4, tolerance * abs(mean))
        prior_component = (
            prior.get("window", {}).get("loads_and_moments", {}).get(name)
            if prior else None
        )
        cross_window_delta = (
            abs(mean - prior_component["mean"]) if prior_component else None
        )
        cross_window_pass = bool(prior_component) and cross_window_delta <= max(
            2.0e-4,
            tolerance * max(abs(mean), abs(prior_component["mean"])),
        )
        passed = passed and cross_window_pass
        if required_for_convergence:
            window_pass &= passed
        components[name] = {
            "mean": mean,
            "minimum": min(values),
            "maximum": max(values),
            "range": span,
            "relative_range": span / max(abs(mean), 1.0e-30),
            "least_squares_slope_per_iteration": slope,
            "window_endpoint_delta": values[-1] - values[0],
            "prior_window_mean": prior_component["mean"] if prior_component else None,
            "cross_window_mean_delta": cross_window_delta,
            "cross_window_stability": "PASS" if cross_window_pass else "FAIL",
            "relative_tolerance": tolerance,
            "absolute_tolerance": 2.0e-4,
            "required_for_convergence": required_for_convergence,
            "status": (
                "PASS" if passed else "FAIL"
            ) if required_for_convergence else "MONITOR",
        }
    mass = diagnostics["outlets_and_mass_balance"]["mass_balance"] if diagnostics else None
    mass_pass = bool(mass and mass["relative_global_imbalance"] <= 5.0e-3)
    residuals = {}
    residual_pass = True
    for name in RESIDUALS:
        values = [row[name] for row in window]
        center = (len(values) - 1) / 2.0
        mean = sum(values) / len(values)
        denominator = sum((index - center) ** 2 for index in range(len(values)))
        slope = sum((index - center) * (value - mean) for index, value in enumerate(values)) / denominator
        endpoint_delta = values[-1] - values[0]
        # SU2 stores log10 residuals.  A simultaneous rise exceeding 0.1 decade
        # with a positive fitted trend is treated as sustained deterioration.
        worsening = slope > 0.0 and endpoint_delta > 0.1
        residual_pass &= not worsening
        residuals[name] = {
            "least_squares_slope_per_iteration": slope,
            "window_endpoint_delta_decades": endpoint_delta,
            "status": "FAIL" if worsening else "PASS",
        }
    measurement = diagnostics["measurement_surface"] if diagnostics else None
    flow_pass = False
    if diagnostic_prior and measurement is not None:
        old = diagnostic_prior["window"].get("measurement")
        fields = ("net_mass_flow_kg_s", "total_pressure_recovery")
        flow_pass = bool(old) and all(
            abs(measurement[field] - old[field])
            <= 0.005 * max(abs(measurement[field]), abs(old[field]), 1.0e-30)
            for field in fields
        )
    outlets = diagnostics["outlets_and_mass_balance"]["markers"] if diagnostics else {}
    outlet_pass = bool(diagnostics) and all(
        outlets[name]["backflow_area_fraction"] == 0.0
        and outlets[name]["reverse_mass_flow_kg_s"] == 0.0
        and outlets[name]["normal_mach_face_centroid_min"] > 1.0
        for name in ("rear_outlet_1", "rear_outlet_2")
    )
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    fatal_log_tokens = ("nan detected", "divergence detected", "negative density", "negative pressure")
    solver_log_pass = not any(token in stderr_text.lower() for token in fatal_log_tokens)
    physical_pass = solver_log_pass and (diagnostics is None or diagnostics.get("status") == "PASS")
    # First-order acceptance establishes a robust restart field.  Measurement
    # flow and total-pressure stability remain recorded for provenance, but are
    # final second-order gates rather than first-order hard gates.
    this_pass = window_pass and residual_pass and mass_pass and outlet_pass and physical_pass
    prior_consecutive = (
        int(diagnostic_prior["consecutive_passing_windows"])
        if diagnostic_prior else 0
    )
    consecutive = prior_consecutive + 1 if this_pass else 0
    if not physical_pass:
        status = "FAIL"
    elif consecutive >= 2:
        status = "PASS"
    elif args.total_iterations >= args.maximum_total_iterations:
        status = "FAIL"
    else:
        status = "PENDING"
    reasons = []
    for passed, reason in (
        (window_pass, "recent_300_load_or_moment_window_not_stable"),
        (residual_pass, "recent_300_residuals_show_sustained_deterioration"),
        (physical_pass, "nonphysical_or_invalid_diagnostic_state"),
    ):
        if not passed:
            reasons.append(reason)
    if diagnostics is None:
        reasons.append("full_pvbatch_diagnostics_deferred_until_candidate_window")
    else:
        if not mass_pass:
            reasons.append("relative_global_mass_imbalance_exceeds_5e-3")
        if not outlet_pass:
            reasons.append("rear_outlet_gate_failed")
    manifest = {
        "schema": "cfdpipe.rans_checkpoint_manifest.v1",
        "status": status,
        "segment_index": args.index,
        "segment_iteration_count": len(rows),
        "maximum_segment_iterations": 500,
        "recent_window_size": 300,
        "total_accepted_iterations": args.total_iterations,
        "maximum_total_iterations": args.maximum_total_iterations,
        "consecutive_passing_windows": consecutive,
        "required_consecutive_passing_windows": 2,
        "convergence_contract": {
            "major_component_minimum_fraction_of_group_maximum": 0.1,
            "major_force_relative_span_maximum": 0.02,
            "major_moment_relative_span_maximum": 0.03,
            "relative_mass_imbalance_maximum": 5.0e-3,
            "measurement_stability_required": False,
        },
        "decision_reasons": reasons,
        "restart": {"path": str(restart), "size_bytes": restart.stat().st_size, "sha256": sha256(restart)},
        "provenance": {
            "git_sha": git_head(root),
            "mesh_path": str(mesh),
            "mesh_sha256": sha256(mesh),
            "config_sha256": sha256(config_path),
            "restart_initialization": (
                "FREESTREAM_INDEPENDENT" if args.independent_initial else "MANIFEST_BOUND_RESTART"
            ),
            "restart_input_sha256": (
                None if args.independent_initial else sha256(restart_input)
            ),
            "solver_return_code": 0,
        },
        "window": {
            "loads_and_moments": components,
            "residuals": residuals,
            "measurement": measurement,
            "mass_balance": mass,
            "flow_stability": "PASS" if flow_pass else "FAIL",
            "pass": this_pass,
        },
        "outputs": {
            "history_sha256": sha256(history),
            "diagnostics_sha256": sha256(diagnostics_path) if diagnostics else None,
            "solver_stdout_sha256": sha256(stdout_path),
            "solver_stderr_sha256": sha256(stderr_path),
        },
    }
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "consecutive": consecutive, "reasons": reasons, "restart_sha256": manifest["restart"]["sha256"]}, indent=2))
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
