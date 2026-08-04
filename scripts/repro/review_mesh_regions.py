#!/usr/bin/env python3
"""Write the strict production mesh-region review report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cfdpipe.mesh_region_review import (  # noqa: E402
    load_json,
    review_mesh_regions,
    sha256_file,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family-plan", type=Path, required=True)
    parser.add_argument("--coarse-result", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()
    plan_path = args.family_plan.resolve(strict=True)
    result_path = args.coarse_result.resolve(strict=True)
    project_path = args.project.resolve(strict=True)
    report = review_mesh_regions(
        plan=load_json(plan_path),
        coarse_result=load_json(result_path),
        project=tomllib.loads(project_path.read_text(encoding="utf-8")),
        plan_sha256=sha256_file(plan_path),
        coarse_result_sha256=sha256_file(result_path),
    )
    for output in (args.output_json, args.output_md):
        resolved_parent = output.resolve(strict=False).parent
        if resolved_parent != (ROOT / "reports").resolve(strict=True):
            raise ValueError("review outputs must be direct children of reports/")
        if output.exists():
            raise FileExistsError(f"review output already exists: {output}")
    args.output_json.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    medium = report["medium_authorization"]
    regions = report["regions"]
    markdown = [
        "# Production mesh region review",
        "",
        "Status: **PASS**. The coarse partition is frozen and the medium mesh is authorized but not built.",
        "",
        "| Region | Coarse size (m) | Medium size (m) | Transition (m) |",
        "|---|---:|---:|---:|",
        *[
            f"| {item['name']} | {item['coarse_size_m']:.17g} | {item['medium_size_m']:.17g} | {item['transition_width_m']:.17g} |"
            for item in regions
        ],
        "",
        "The size order is strictly farfield > transition > near-body > internal passage. Bounds are nested, transitions are linear and positive, and the volume callback composes overlaps by minimum size. The frozen wall retains 60 layers, 0.4 m tangential triangulation, first-layer height, growth ratio, and Pilot repair parameters.",
        "",
        f"Medium target: {medium['target_cells']:,} cells; allowed range: {medium['minimum_cells']:,}–{medium['maximum_cells']:,}; cubic projection from the measured coarse core: {medium['cubic_projection_cells']:,}.",
        "",
        "RANS and fine mesh remain prohibited until the generated medium mesh independently passes every production quality and readback gate.",
    ]
    args.output_md.write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "output": str(args.output_json)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
