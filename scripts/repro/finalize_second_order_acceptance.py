#!/usr/bin/env python3
"""Finalize an accepted second-order checkpoint lineage and evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--accepted-segment",
        required=True,
        action="append",
        type=Path,
        help="Accepted lineage segment in chronological order; repeat for each segment",
    )
    parser.add_argument("--initial-restart-sha256", required=True)
    parser.add_argument("--excluded-segment", action="append", default=[], type=Path)
    parser.add_argument("--grid-label", required=True, choices=("coarse", "medium"))
    parser.add_argument("--mpi-ranks", required=True, type=int)
    args = parser.parse_args()
    final, output = args.final.resolve(strict=True), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((final / "checkpoint_manifest.json").read_text())
    diagnostics = json.loads((final / "paraview/rans_diagnostics.json").read_text())
    if manifest["status"] != "PASS" or manifest["consecutive_passing_windows"] < 3:
        raise ValueError("final checkpoint is not a three-window PASS")
    accepted = [path.resolve(strict=True) for path in args.accepted_segment]
    if not accepted or accepted[-1] != final:
        raise ValueError("accepted lineage must end at --final")
    lineage = []
    prior_hash = args.initial_restart_sha256
    if len(prior_hash) != 64:
        raise ValueError("initial restart SHA-256 must contain 64 hexadecimal characters")
    try:
        int(prior_hash, 16)
    except ValueError as error:
        raise ValueError("initial restart SHA-256 is invalid") from error
    for segment in accepted:
        current = json.loads((segment / "checkpoint_manifest.json").read_text())
        if current["provenance"]["restart_input_sha256"] != prior_hash:
            raise ValueError(f"restart lineage mismatch: {segment}")
        lineage.append({"path": str(segment), "input_restart_sha256": prior_hash,
                        "output_restart_sha256": current["restart"]["sha256"],
                        "status": current["status"], "transition": current.get("transition", False)})
        prior_hash = current["restart"]["sha256"]
    excluded = [path.resolve(strict=True) for path in args.excluded_segment]
    (output / "restart_lineage.json").write_text(json.dumps({
        "schema": "cfdpipe.restart_lineage.v1", "accepted": lineage,
        "excluded_failed_segments": [{"path": str(path), "reason": json.loads((path / "checkpoint_manifest.json").read_text())["decision_reasons"]} for path in excluded],
        "final_restart_sha256": prior_hash,
    }, indent=2) + "\n")
    history_out = output / "history_second_order.csv"
    with history_out.open("w", newline="", encoding="utf-8") as target:
        writer = None
        for segment in accepted:
            with (segment / "history.csv").open(newline="", encoding="utf-8") as source:
                reader = csv.reader(source)
                header = next(reader)
                if writer is None:
                    writer = csv.writer(target); writer.writerow(["segment", *header])
                for row in reader:
                    writer.writerow([segment.name, *row])
    loads = diagnostics["loads"]
    with (output / "forces_moments.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream); writer.writerow(["quantity", "value"])
        for name, item in loads["coefficients"].items(): writer.writerow([name, item["final"]])
        for name, value in loads["dimensional_loads"].items(): writer.writerow([name, value])
    measurement = diagnostics["measurement_surface"]
    with (output / "section_integrals.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = [key for key, value in measurement.items() if isinstance(value, (int, float, str))]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerow({key: measurement[key] for key in fields})
    for name, data in (("mass_balance.json", diagnostics["outlets_and_mass_balance"]["mass_balance"]),
                       ("yplus_summary.json", diagnostics["yplus"]),
                       ("rear_outlet_report.json", {key: diagnostics["outlets_and_mass_balance"]["markers"][key] for key in ("rear_outlet_1", "rear_outlet_2")})):
        (output / name).write_text(json.dumps(data, indent=2) + "\n")
    (output / "resource_usage.json").write_text(json.dumps({
        "mpi_ranks": args.mpi_ranks,
        "cuda_enabled": False,
        "note": "per-segment peak RSS must be retained in segment evidence when available",
    }, indent=2) + "\n")
    final_report = {
        "schema": "cfdpipe.second_order_acceptance.v1", "status": "PASS",
        "consecutive_passing_windows": manifest["consecutive_passing_windows"],
        "final_checkpoint_manifest": str(final / "checkpoint_manifest.json"),
        "first_order_restart_sha256": lineage[0]["input_restart_sha256"],
        "final_restart": {"path": str(final / "restart.dat"), "sha256": digest(final / "restart.dat")},
        "final_solution": {"path": str(final / "solution.vtu"), "sha256": digest(final / "solution.vtu")},
        "mass_balance": diagnostics["outlets_and_mass_balance"]["mass_balance"],
        "yplus": diagnostics["yplus"], "measurement_surface": measurement,
        "rear_outlets": {key: diagnostics["outlets_and_mass_balance"]["markers"][key] for key in ("rear_outlet_1", "rear_outlet_2")},
        "diagnostic_status": diagnostics["status"], "spatial_order": diagnostics["spatial_order"],
    }
    (output / "second_order_acceptance.final.json").write_text(json.dumps(final_report, indent=2) + "\n")
    (output / "second_order_acceptance.final.md").write_text(
        f"# {args.grid_label.capitalize()}-grid second-order SST RANS acceptance\n\n"
        f"- Status: **PASS**\n- Consecutive full windows: {manifest['consecutive_passing_windows']}\n"
        f"- Final restart SHA-256: `{final_report['final_restart']['sha256']}`\n"
        f"- Relative mass imbalance: `{final_report['mass_balance']['relative_global_imbalance']}`\n"
        f"- Maximum y+: `{final_report['yplus']['maximum']}`\n"
        f"- Rear outlet minimum normal Mach: `{final_report['rear_outlets']['rear_outlet_1']['normal_mach_face_centroid_min']}`, `{final_report['rear_outlets']['rear_outlet_2']['normal_mach_face_centroid_min']}`\n"
    )
    print(json.dumps({"status": "PASS", "output": str(output), "restart_sha256": final_report["final_restart"]["sha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
