#!/usr/bin/env python3
"""Evaluate one bounded second-order RANS checkpoint."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

COMPONENTS = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz")
RESIDUALS = ("rms[Rho]", "rms[RhoU]", "rms[RhoV]", "rms[RhoW]", "rms[RhoE]", "rms[k]", "rms[w]")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


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


def history_rows(path: Path) -> list[dict[str, float]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.reader(stream)
        headings = [item.strip().strip('"') for item in next(reader)]
        rows = []
        for number, values in enumerate(reader, 1):
            row = {key: float(value) for key, value in zip(headings, values)}
            if any(not math.isfinite(value) for value in row.values()):
                raise ValueError(f"non-finite history value at row {number}")
            rows.append(row)
    if not 300 <= len(rows) <= 500:
        raise ValueError("history must contain 300 to 500 iterations")
    return rows


def stats(values: list[float]) -> dict[str, float]:
    mean = sum(values) / len(values)
    center = (len(values) - 1) / 2
    denominator = sum((index - center) ** 2 for index in range(len(values)))
    slope = sum((index - center) * (value - mean) for index, value in enumerate(values)) / denominator
    return {
        "minimum": min(values), "maximum": max(values), "mean": mean,
        "range": max(values) - min(values), "relative_range": (max(values) - min(values)) / max(abs(mean), 1e-30),
        "least_squares_slope_per_iteration": slope, "endpoint_delta": values[-1] - values[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segment", required=True, type=Path)
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument("--index", required=True, type=int)
    parser.add_argument("--prior", type=Path)
    parser.add_argument("--diagnostic-prior", type=Path)
    parser.add_argument("--total-iterations", required=True, type=int)
    parser.add_argument("--maximum-total-iterations", default=50000, type=int)
    parser.add_argument("--transition", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.segment.resolve(strict=True)
    mesh = args.mesh.resolve(strict=True)
    if not mesh.is_file() or mesh.is_symlink():
        raise ValueError("mesh must be a non-symlink regular file")
    paths = {name: root / name for name in ("case.cfg", "history.csv", "restart.dat", "restart_input.dat", "solver.stdout.log", "solver.stderr.log", "returncode.txt")}
    if paths["returncode.txt"].read_text().strip() != "0":
        raise ValueError("solver return code is not zero")
    for name in ("restart.dat", "restart_input.dat"):
        if not paths[name].is_file() or paths[name].is_symlink() or paths[name].stat().st_size == 0:
            raise ValueError(f"invalid {name}")
    rows = history_rows(paths["history.csv"])
    window = rows[-300:]
    prior = json.loads(args.prior.read_text()) if args.prior else None
    diagnostic_prior = json.loads(args.diagnostic_prior.read_text()) if args.diagnostic_prior else prior
    load_stats = {}
    load_pass = True
    group_scales = {
        "force": max(abs(sum(row[name] for row in window) / len(window)) for name in COMPONENTS[:3]),
        "moment": max(abs(sum(row[name] for row in window) / len(window)) for name in COMPONENTS[3:]),
    }
    for name in COMPONENTS:
        item = stats([row[name] for row in window])
        group = "force" if name.startswith("CF") else "moment"
        relative_limit = 0.005 if group == "force" else 0.01
        absolute_limit = 2e-4
        major = abs(item["mean"]) >= 0.1 * group_scales[group]
        within = item["range"] <= max(absolute_limit, relative_limit * abs(item["mean"]))
        old = prior.get("window", {}).get("loads_and_moments", {}).get(name) if prior else None
        cross_delta = abs(item["mean"] - old["mean"]) if old else None
        cross_pass = bool(old) and cross_delta <= max(absolute_limit, relative_limit * max(abs(item["mean"]), abs(old["mean"])))
        passed = within and cross_pass
        if major:
            load_pass &= passed
        item.update({"major": major, "relative_limit": relative_limit, "absolute_limit": absolute_limit,
                     "prior_mean": old["mean"] if old else None, "cross_window_delta": cross_delta,
                     "status": "PASS" if passed else ("FAIL" if major else "MONITOR")})
        load_stats[name] = item
    residual_stats = {}
    residual_pass = True
    for name in RESIDUALS:
        item = stats([row[name] for row in window])
        worsening = item["least_squares_slope_per_iteration"] > 0 and item["endpoint_delta"] > 0.1
        residual_pass &= not worsening
        item["status"] = "FAIL" if worsening else "PASS"
        residual_stats[name] = item
    diagnostics_path = root / "paraview/rans_diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text()) if diagnostics_path.is_file() else None
    hard = {"diagnostics_present": diagnostics is not None}
    flow_pass = mass_pass = outlet_pass = yplus_pass = physical_pass = False
    measurement = mass = outlets = yplus = None
    if diagnostics:
        measurement = diagnostics["measurement_surface"]
        mass = diagnostics["outlets_and_mass_balance"]["mass_balance"]
        outlets = diagnostics["outlets_and_mass_balance"]["markers"]
        yplus = diagnostics["yplus"]
        previous_measurement = diagnostic_prior.get("window", {}).get("measurement") if diagnostic_prior else None
        flow_pass = bool(previous_measurement) and all(
            abs(measurement[field] - previous_measurement[field]) <= 0.005 * max(abs(measurement[field]), abs(previous_measurement[field]), 1e-30)
            for field in ("net_mass_flow_kg_s", "total_pressure_recovery")
        )
        mass_pass = mass["relative_global_imbalance"] <= 1e-3
        outlet_pass = all(outlets[name]["backflow_area_fraction"] == 0 and outlets[name]["reverse_mass_flow_kg_s"] == 0 and outlets[name]["normal_mach_face_centroid_min"] > 1 for name in ("rear_outlet_1", "rear_outlet_2"))
        yplus_pass = yplus["maximum"] < 1 and yplus["above_maximum_area_fraction"] == 0
        physical_pass = diagnostics.get("status") == "PASS" and diagnostics.get("spatial_order") == 2
    stderr = paths["solver.stderr.log"].read_text(errors="replace").lower()
    log_pass = not any(token in stderr for token in ("nan detected", "divergence detected", "negative density", "negative pressure", "negative temperature"))
    hard.update({"solver_log": log_pass, "physical": physical_pass, "mass_balance": mass_pass,
                 "rear_outlets": outlet_pass, "yplus": yplus_pass, "measurement_stability": flow_pass})
    complete_window = load_pass and residual_pass and all(hard.values()) and not args.transition
    consecutive = (int(diagnostic_prior.get("consecutive_passing_windows", 0)) + 1) if complete_window and diagnostic_prior else 0
    if not log_pass or (diagnostics and not (physical_pass and mass_pass and outlet_pass and yplus_pass)):
        status = "FAIL"
    elif consecutive >= 3:
        status = "PASS"
    elif args.total_iterations >= args.maximum_total_iterations:
        status = "FAIL"
    else:
        status = "PENDING"
    reasons = [name for passed, name in ((load_pass, "load_or_moment_window"), (residual_pass, "residual_deterioration"),
               (diagnostics is not None, "full_diagnostics_deferred"), (flow_pass, "measurement_stability"),
               (mass_pass, "mass_balance"), (outlet_pass, "rear_outlets"), (yplus_pass, "yplus"),
               (physical_pass, "physical_diagnostics")) if not passed]
    if args.transition:
        reasons.append("muscl_transition_window_not_eligible")
    manifest = {
        "schema": "cfdpipe.second_order_checkpoint_manifest.v1", "status": status,
        "segment_index": args.index, "transition": args.transition, "segment_iteration_count": len(rows),
        "total_accepted_iterations": args.total_iterations, "maximum_total_iterations": args.maximum_total_iterations,
        "consecutive_passing_windows": consecutive, "required_consecutive_passing_windows": 3,
        "decision_reasons": reasons, "restart": {"path": str(paths["restart.dat"]), "size_bytes": paths["restart.dat"].stat().st_size, "sha256": digest(paths["restart.dat"])},
        "provenance": {
            "git_sha": git_head(root),
            "mesh_path": str(mesh),
            "mesh_sha256": digest(mesh),
            "config_sha256": digest(paths["case.cfg"]),
            "restart_input_sha256": digest(paths["restart_input.dat"]),
            "solver_return_code": 0,
        },
        "window": {"loads_and_moments": load_stats, "residuals": residual_stats, "measurement": measurement,
                   "mass_balance": mass, "outlets": outlets, "yplus": yplus, "hard_gates": hard, "pass": complete_window},
        "outputs": {"history_sha256": digest(paths["history.csv"]), "diagnostics_sha256": digest(diagnostics_path) if diagnostics else None,
                    "solver_stdout_sha256": digest(paths["solver.stdout.log"]), "solver_stderr_sha256": digest(paths["solver.stderr.log"])},
    }
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": status, "consecutive": consecutive, "reasons": reasons, "restart_sha256": manifest["restart"]["sha256"]}, indent=2))
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
