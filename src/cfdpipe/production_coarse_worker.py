"""Build one production coarse mesh from a hash-bound frozen Pilot PASS."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping

from .boundary_layer_smoke import RealProjectBoundaryLayerStrategy
from .coarse_mesh import (
    build_coarse_calibration,
    make_production_coarse_strategy_config,
    normalize_coarse_mesh_config,
)


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    expected = str(expected_sha256).casefold()
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or len(expected) != 64
        or _sha256(resolved) != expected
    ):
        raise ValueError(f"{label} path or SHA-256 is invalid")
    value = json.loads(
        resolved.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"{label} contains {token}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _level_contract(plan: Mapping[str, Any], mesh_level: str) -> dict[str, Any]:
    levels = plan.get("levels")
    if mesh_level not in {"coarse", "medium"} or not isinstance(levels, Mapping):
        raise ValueError("production mesh level is invalid")
    raw = levels.get(mesh_level)
    if not isinstance(raw, Mapping) or raw.get("build_authorized") is not True:
        raise ValueError(f"production {mesh_level} mesh is not authorized")
    values = {
        key: raw.get(key)
        for key in ("target_cells", "minimum_cells", "maximum_cells")
    }
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values.values()):
        raise ValueError(f"production {mesh_level} cell contract is incomplete")
    if not 0 < values["minimum_cells"] <= values["target_cells"] <= values["maximum_cells"]:
        raise ValueError(f"production {mesh_level} cell contract is invalid")
    return values


def _regions(
    plan: Mapping[str, Any], scale: float, mesh_level: str = "coarse"
) -> list[dict[str, Any]]:
    if not math.isfinite(scale) or not 0.5 <= scale <= 2.0:
        raise ValueError("volume scale must be finite and in [0.5, 2.0]")
    family = plan.get("mesh_family")
    raw = plan.get("physical_regions")
    if (
        plan.get("schema") != "cfdpipe.production_mesh_family_plan.v1"
        or plan.get("status") not in {
            "CALIBRATION",
            "FROZEN",
            "COARSE_REGIONS_FROZEN_MEDIUM_AUTHORIZED",
        }
        or not isinstance(family, Mapping)
        or family.get("boundary_layer_count") != 60
        or family.get("level_size_factors") != [1.0, 0.794, 0.63]
        or not isinstance(raw, list)
        or [item.get("name") for item in raw if isinstance(item, Mapping)]
        != ["farfield", "transition", "near_body_core", "internal_passage"]
    ):
        raise ValueError("production mesh family plan is invalid")
    _level_contract(plan, mesh_level)
    level_factor = {"coarse": 1.0, "medium": 0.794}[mesh_level]
    result = []
    for item in raw:
        bounds = item.get("bounds_m")
        size = item.get("coarse_size_m")
        transition = item.get("transition_width_m")
        if (
            not isinstance(bounds, list)
            or len(bounds) != 6
            or isinstance(size, bool)
            or not isinstance(size, (int, float))
            or isinstance(transition, bool)
            or not isinstance(transition, (int, float))
            or float(transition) <= 0.0
        ):
            raise ValueError("production physical region is incomplete")
        result.append(
            {
                "name": str(item["name"]),
                "bounds_m": [float(value) for value in bounds],
                "size_m": float(size) * level_factor * scale,
                "transition_width_m": float(item["transition_width_m"]),
            }
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pilot-manifest", type=Path, required=True)
    parser.add_argument("--pilot-sha256", required=True)
    parser.add_argument("--family-plan", type=Path, required=True)
    parser.add_argument("--family-plan-sha256", required=True)
    parser.add_argument("--volume-scale", type=float, required=True)
    parser.add_argument(
        "--mesh-level", choices=("coarse", "medium"), default="coarse"
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = args.config.expanduser().resolve(strict=True)
    if config != (ROOT / "config" / "coarse_mesh.toml").resolve(strict=True):
        raise ValueError("production worker requires config/coarse_mesh.toml")
    contract = normalize_coarse_mesh_config(
        tomllib.loads(config.read_text(encoding="utf-8")), repository_root=ROOT
    )
    pilot = _strict_json(args.pilot_manifest, args.pilot_sha256, "Pilot manifest")
    plan = _strict_json(args.family_plan, args.family_plan_sha256, "family plan")
    level_contract = _level_contract(plan, args.mesh_level)
    volume_regions = _regions(plan, float(args.volume_scale), args.mesh_level)
    target_range = (
        int(level_contract["minimum_cells"]),
        int(level_contract["maximum_cells"]),
    )
    strategy_config = make_production_coarse_strategy_config(
        contract,
        pilot,
        volume_regions=volume_regions,
        maximum_3d_elements=target_range[1] + 250_000,
    )
    strategy_config.pop("normalized_config_sha256", None)
    strategy_config["production_mesh_level"] = args.mesh_level
    strategy_config["production_target_element_count"] = int(
        level_contract["target_cells"]
    )
    strategy_config["normalized_config_sha256"] = _canonical_hash(strategy_config)
    markers = tomllib.loads((ROOT / "config" / "markers.toml").read_text("utf-8"))
    topology = tomllib.loads(
        (ROOT / "config" / "topology_smoke.toml").read_text("utf-8")
    )
    strategy = RealProjectBoundaryLayerStrategy(
        markers,
        topology,
        marker_config_sha256=contract["provenance"]["markers_sha256"],
        topology_smoke_config_sha256=contract["provenance"][
            "topology_smoke_sha256"
        ],
    )
    manifest = build_coarse_calibration(
        contract=contract,
        characteristic_length_m=0.4,
        output_directory=args.output,
        allowed_output_root=ROOT / "runs" / "mesh" / args.mesh_level,
        strategy=strategy,
        local_schedule_binding=None,
        projection_evidence=None,
        controller_argv=list(argv or []),
        production_strategy_config=strategy_config,
        production_target_range=target_range,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "output": str(args.output),
                "element_count_3d": manifest["generation_audit"][
                    "element_count_3d"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
