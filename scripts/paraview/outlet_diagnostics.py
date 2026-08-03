"""Compute marker-resolved outlet diagnostics inside ``pvbatch`` only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import traceback

from paraview import servermanager, simple


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cfdpipe.outlet_diagnostics import (  # noqa: E402
    OutletDiagnosticError,
    analyze_outlets,
    parse_topology_smoke_su2,
)


SUPPORTED_SUFFIXES = {".vtk", ".vtu", ".pvtu", ".vtm"}


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--outlet-marker", required=True, action="append")
    parser.add_argument("--farfield-marker", required=True)
    parser.add_argument("--wall-marker", required=True)
    return parser.parse_args()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _update_pipeline(proxy):
    try:
        simple.UpdatePipeline(proxy=proxy)
    except TypeError:
        proxy.UpdatePipeline()


def _array(dataset, name, components):
    array = dataset.GetPointData().GetArray(name)
    if array is None:
        raise OutletDiagnosticError("solution lacks point array {0}".format(name))
    if int(array.GetNumberOfComponents()) != components:
        raise OutletDiagnosticError(
            "point array {0} has {1} components, expected {2}".format(
                name, array.GetNumberOfComponents(), components
            )
        )
    values = []
    for index in range(dataset.GetNumberOfPoints()):
        raw = array.GetTuple(index)
        value = float(raw[0]) if components == 1 else tuple(float(item) for item in raw)
        scalars = (value,) if components == 1 else value
        if any(not math.isfinite(item) for item in scalars):
            raise OutletDiagnosticError(
                "point array {0} contains non-finite data".format(name)
            )
        values.append(value)
    return values


def _version():
    value = simple.GetParaViewVersion()
    if isinstance(value, (tuple, list)):
        return ".".join(str(component) for component in value)
    return str(value)


def main():
    args = _parse_args()
    input_path = args.input.expanduser().resolve(strict=True)
    mesh_path = args.mesh.expanduser().resolve(strict=True)
    output_path = args.output.expanduser().resolve()
    if input_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("unsupported solution extension {0}".format(input_path.suffix))
    if len(args.outlet_marker) != 2 or len(set(args.outlet_marker)) != 2:
        raise ValueError("exactly two distinct --outlet-marker values are required")
    if output_path in (input_path, mesh_path):
        raise ValueError("output JSON must not overwrite an input")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source = simple.OpenDataFile(str(input_path))
    if source is None:
        raise RuntimeError("ParaView could not create a reader for {0}".format(input_path))
    dataset = None
    try:
        _update_pipeline(source)
        dataset = servermanager.Fetch(source)
        if dataset is None or not bool(dataset.IsA("vtkDataSet")):
            raise OutletDiagnosticError("outlet diagnostics require one vtkDataSet")
        solution_points = [
            tuple(float(value) for value in dataset.GetPoint(index))
            for index in range(dataset.GetNumberOfPoints())
        ]
        mesh = parse_topology_smoke_su2(mesh_path)
        payload = analyze_outlets(
            mesh,
            solution_points=solution_points,
            density=_array(dataset, "Density", 1),
            momentum=_array(dataset, "Momentum", 3),
            velocity=_array(dataset, "Velocity", 3),
            mach=_array(dataset, "Mach", 1),
            outlet_markers=tuple(args.outlet_marker),
            farfield_marker=args.farfield_marker,
            wall_marker=args.wall_marker,
        )
        payload.update(
            {
                "status": "PASS",
                "paraview_version": _version(),
                "input_solution": {
                    "path": str(input_path),
                    "sha256": _sha256(input_path),
                    "point_count": int(dataset.GetNumberOfPoints()),
                    "cell_count": int(dataset.GetNumberOfCells()),
                },
            }
        )
    finally:
        simple.Delete(source)

    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0


def _entry_point():
    try:
        return main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
