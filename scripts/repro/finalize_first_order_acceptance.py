#!/usr/bin/env python3
"""Audit two accepted full checkpoints and write first-order evidence reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    raise ValueError("evidence directory is not contained in a Git working tree")


def valid_consecutive_counts(first: int, second: int) -> bool:
    """Accept any adjacent pair in an already-established passing sequence."""
    return first >= 1 and second == first + 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--first-index", required=True, type=int)
    parser.add_argument("--second-index", required=True, type=int)
    parser.add_argument("--grid-label", required=True, choices=("coarse", "medium"))
    args = parser.parse_args()
    base = args.base.resolve(strict=True)
    indices = (args.first_index, args.second_index)
    if indices[1] != indices[0] + 1:
        raise ValueError("accepted checkpoints must be consecutive segments")

    artifact_names = (
        "case.cfg",
        "history.csv",
        "restart_input.dat",
        "restart.dat",
        "solution.vtu",
        "surface_solution.vtu",
        "solver.stdout.log",
        "solver.stderr.log",
        "returncode.txt",
        "paraview/rans_diagnostics.json",
        "paraview/pvbatch.stdout.log",
        "paraview/pvbatch.stderr.log",
        "paraview/returncode.txt",
        "checkpoint_manifest.json",
    )
    windows = []
    for index in indices:
        segment = base / f"segment_{index:04d}"
        manifest = load(segment / "checkpoint_manifest.json")
        diagnostics = load(segment / "paraview/rans_diagnostics.json")
        artifacts = {}
        for name in artifact_names:
            path = segment / name
            artifacts[name] = {
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        windows.append(
            {
                "segment_index": index,
                "manifest": str(segment / "checkpoint_manifest.json"),
                "status": manifest["status"],
                "consecutive": manifest["consecutive_passing_windows"],
                "decision_reasons": manifest["decision_reasons"],
                "window_pass": manifest["window"]["pass"],
                "loads_and_moments": manifest["window"]["loads_and_moments"],
                "residuals": manifest["window"]["residuals"],
                "mass_balance": manifest["window"]["mass_balance"],
                "measurement_flow_monitor": manifest["window"]["measurement"],
                "measurement_flow_stability_monitor": manifest["window"]["flow_stability"],
                "physical_state": diagnostics["physical_state"],
                "solver_return_code": int(
                    (segment / "returncode.txt").read_text(encoding="utf-8")
                ),
                "pvbatch_return_code": int(
                    (segment / "paraview/returncode.txt").read_text(encoding="utf-8")
                ),
                "yplus": diagnostics["yplus"],
                "rear_outlets": {
                    name: diagnostics["outlets_and_mass_balance"]["markers"][name]
                    for name in ("rear_outlet_1", "rear_outlet_2")
                },
                "artifacts": artifacts,
            }
        )

    prior_manifest = load(
        base / f"segment_{indices[0] - 1:04d}" / "checkpoint_manifest.json"
    )
    lineage = {
        "first_input_matches_prior_output": (
            windows[0]["artifacts"]["restart_input.dat"]["sha256"]
            == prior_manifest["restart"]["sha256"]
        ),
        "second_input_matches_first_output": (
            windows[1]["artifacts"]["restart_input.dat"]["sha256"]
            == windows[0]["artifacts"]["restart.dat"]["sha256"]
        ),
    }
    checks = []
    for window in windows:
        checks.extend(
            (
                window["solver_return_code"] == 0,
                window["pvbatch_return_code"] == 0,
                window["window_pass"],
                not window["decision_reasons"],
                all(value["status"] == "PASS" for value in window["residuals"].values()),
                window["mass_balance"]["relative_global_imbalance"] <= 5.0e-3,
                window["physical_state"]["status"] == "PASS",
                all(
                    outlet["backflow_area_fraction"] == 0.0
                    and outlet["reverse_mass_flow_kg_s"] == 0.0
                    and outlet["normal_mach_face_centroid_min"] > 1.0
                    for outlet in window["rear_outlets"].values()
                ),
            )
        )
    checks.extend(lineage.values())
    checks.extend(
        (
            valid_consecutive_counts(
                windows[0]["consecutive"], windows[1]["consecutive"]
            ),
            windows[1]["status"] == "PASS",
            windows[1]["consecutive"] >= 2,
        )
    )
    overall = "PASS" if all(checks) else "FAIL"
    git_sha = git_head(base)
    report = {
        "schema": "cfdpipe.first_order_acceptance.v1",
        "status": overall,
        "overall": overall,
        "git_sha": git_sha,
        "base_directory": str(base),
        "criteria": {
            "solver_return_code": 0,
            "pvbatch_return_code": 0,
            "nonfinite_count": 0,
            "nonpositive_density_count": 0,
            "nonpositive_pressure_count": 0,
            "nonpositive_temperature_count": 0,
            "residual_sustained_deterioration": False,
            "relative_mass_imbalance_maximum": 5.0e-3,
            "major_force_relative_span_maximum": 0.02,
            "major_moment_relative_span_maximum": 0.03,
            "required_consecutive_windows": 2,
            "measurement_stability_required_for_first_order": False,
        },
        "lineage": lineage,
        "windows": windows,
        "final_restart": {
            "path": str(base / f"segment_{indices[1]:04d}" / "restart.dat"),
            "sha256": windows[1]["artifacts"]["restart.dat"]["sha256"],
            "size_bytes": windows[1]["artifacts"]["restart.dat"]["size_bytes"],
        },
    }
    (base / "first_order_acceptance.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown = "\n".join(
        (
            f"# {args.grid_label.capitalize()}-grid first-order SST RANS acceptance",
            "",
            f"- Overall: `{overall}`",
            f"- Git SHA: `{git_sha}`",
            f"- Accepted windows: `segment_{indices[0]:04d}`, `segment_{indices[1]:04d}`",
            f"- Final restart SHA-256: `{report['final_restart']['sha256']}`",
            f"- Restart lineage: `{lineage}`",
            "",
            "Measurement-flow stability is retained as a monitor and is not a first-order hard gate under the frozen 2026-08-05 acceptance contract.",
            "",
        )
    )
    (base / "first_order_acceptance.md").write_text(markdown, encoding="utf-8")
    print(
        json.dumps(
            {
                "overall": overall,
                "report": str(base / "first_order_acceptance.json"),
                "final_restart": report["final_restart"],
                "lineage": lineage,
            },
            indent=2,
        )
    )
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
