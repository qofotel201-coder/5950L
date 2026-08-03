"""Audit one bounded SU2 SST RANS result inside ``pvbatch`` only."""

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
    map_mesh_points_to_solution,
    parse_topology_smoke_su2,
)
from cfdpipe.rans_diagnostics import (  # noqa: E402
    RANSDiagnosticError,
    summarize_measurement_slice,
    summarize_rans_history,
    summarize_wall_yplus,
)


SUPPORTED_SUFFIXES = {".vtk", ".vtu", ".pvtu", ".vtm"}


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid {0}: {1}: {2}".format(label, path, error))
    if not isinstance(value, dict):
        raise RuntimeError("{0} root must be an object".format(label))
    return value


def _update_pipeline(proxy):
    try:
        simple.UpdatePipeline(proxy=proxy)
    except TypeError:
        proxy.UpdatePipeline()


def _version():
    value = simple.GetParaViewVersion()
    if isinstance(value, (tuple, list)):
        return ".".join(str(component) for component in value)
    return str(value)


def _point_array(dataset, name, components):
    array = dataset.GetPointData().GetArray(name)
    if array is None:
        raise RANSDiagnosticError("solution lacks point array {0}".format(name))
    if int(array.GetNumberOfComponents()) != components:
        raise RANSDiagnosticError(
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
            raise RANSDiagnosticError(
                "point array {0} contains non-finite data".format(name)
            )
        values.append(value)
    return values


def _array_inventory(dataset):
    arrays = []
    attributes = dataset.GetPointData()
    for index in range(attributes.GetNumberOfArrays()):
        array = attributes.GetArray(index)
        if array is None:
            continue
        arrays.append(
            {
                "name": str(array.GetName()),
                "components": int(array.GetNumberOfComponents()),
                "tuples": int(array.GetNumberOfTuples()),
            }
        )
    return arrays


def _norm(vector):
    return math.sqrt(sum(component * component for component in vector))


def _measurement_diagnostics(source, contract):
    measurement = contract.get("measurement")
    if not isinstance(measurement, dict):
        raise RANSDiagnosticError("diagnostic contract lacks measurement")
    coordinate = float(measurement["plane_coordinate_m"])
    normal = tuple(float(value) for value in measurement["normal_unit"])

    slice_filter = simple.Slice(Input=source)
    try:
        slice_filter.SliceType = "Plane"
        slice_filter.SliceType.Origin = [coordinate, 0.0, 0.0]
        slice_filter.SliceType.Normal = list(normal)
        if hasattr(slice_filter, "Triangulatetheslice"):
            slice_filter.Triangulatetheslice = 1
        _update_pipeline(slice_filter)
        sliced = servermanager.Fetch(slice_filter)
        if sliced is None or not bool(sliced.IsA("vtkDataSet")):
            raise RANSDiagnosticError("measurement Slice did not produce a vtkDataSet")

        arrays = {}
        for name, components in (
            ("Density", 1),
            ("Momentum", 3),
            ("Mach", 1),
            ("Pressure", 1),
            ("Temperature", 1),
        ):
            arrays[name] = _point_array(sliced, name, components)
        slice_points = [
            tuple(float(value) for value in sliced.GetPoint(index))
            for index in range(sliced.GetNumberOfPoints())
        ]
        slice_cells = []
        for cell_index in range(sliced.GetNumberOfCells()):
            cell = sliced.GetCell(cell_index)
            slice_cells.append(
                tuple(
                    int(cell.GetPointId(index))
                    for index in range(cell.GetNumberOfPoints())
                )
            )
        return summarize_measurement_slice(
            points=slice_points,
            cells=slice_cells,
            density=arrays["Density"],
            momentum=arrays["Momentum"],
            mach=arrays["Mach"],
            pressure=arrays["Pressure"],
            temperature=arrays["Temperature"],
            name=measurement["name"],
            plane_coordinate_m=coordinate,
            normal_unit=normal,
            bounds_m=measurement["bounds_m"],
            expected_area_m2=measurement["area_m2"],
            maximum_relative_area_error=measurement["maximum_relative_area_error"],
            minimum_selected_cell_count=measurement["minimum_selected_cell_count"],
            specific_heat_ratio=contract["gas"]["specific_heat_ratio"],
            freestream_static_pressure_pa=contract["freestream"]["static_pressure_pa"],
            freestream_mach=contract["freestream"]["mach"],
        )
    finally:
        simple.Delete(slice_filter)


def _skin_friction_summary(mesh, solution_points, vectors, wall_marker, dynamic_pressure):
    mapping, evidence = map_mesh_points_to_solution(mesh.points, solution_points)
    wall_nodes = sorted({node for face in mesh.markers[wall_marker] for node in face})
    magnitudes = []
    for node in wall_nodes:
        index = mapping[node]
        vector = vectors[index]
        magnitude = _norm(vector)
        if not math.isfinite(magnitude):
            raise RANSDiagnosticError("skin-friction coefficient is non-finite")
        magnitudes.append(magnitude)
    if not magnitudes:
        raise RANSDiagnosticError("wall has no skin-friction samples")
    if not any(value > 0.0 for value in magnitudes):
        raise RANSDiagnosticError("all wall skin-friction samples are zero")
    return {
        "status": "PASS",
        "array_name": "Skin_Friction_Coefficient",
        "components": 3,
        "wall_unique_point_count": len(magnitudes),
        "coefficient_magnitude_minimum": min(magnitudes),
        "coefficient_magnitude_maximum": max(magnitudes),
        "coefficient_magnitude_mean": sum(magnitudes) / len(magnitudes),
        "wall_shear_magnitude_maximum_pa": max(magnitudes) * dynamic_pressure,
        "coordinate_mapping": evidence,
    }


def main():
    args = _parse_args()
    input_path = args.input.expanduser().resolve(strict=True)
    mesh_path = args.mesh.expanduser().resolve(strict=True)
    history_path = args.history.expanduser().resolve(strict=True)
    contract_path = args.contract.expanduser().resolve(strict=True)
    output_path = args.output.expanduser().resolve()
    output_root = Path.cwd().resolve()
    if output_path == output_root or output_root not in output_path.parents:
        raise ValueError("output JSON must stay below the pvbatch cwd")
    if input_path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("unsupported solution extension {0}".format(input_path.suffix))
    contract = _load_json(contract_path, "RANS diagnostic contract")
    expected = contract.get("inputs")
    if not isinstance(expected, dict):
        raise RANSDiagnosticError("diagnostic contract lacks inputs")
    for label, path in (("solution", input_path), ("mesh", mesh_path), ("history", history_path)):
        record = expected.get(label)
        if not isinstance(record, dict) or record.get("sha256") != _sha256(path):
            raise RANSDiagnosticError("{0} hash differs from diagnostic contract".format(label))

    source = simple.OpenDataFile(str(input_path))
    if source is None:
        raise RuntimeError("ParaView could not create a reader for {0}".format(input_path))
    dataset = None
    payload = None
    try:
        _update_pipeline(source)
        dataset = servermanager.Fetch(source)
        if dataset is None or not bool(dataset.IsA("vtkDataSet")):
            raise RANSDiagnosticError("RANS diagnostics require one vtkDataSet")
        expected_points = int(contract["mesh"]["point_count"])
        expected_cells = int(contract["mesh"]["cell_count"])
        if (
            int(dataset.GetNumberOfPoints()) != expected_points
            or int(dataset.GetNumberOfCells()) != expected_cells
        ):
            raise RANSDiagnosticError("solution point/cell count differs from mesh contract")
        solution_points = [
            tuple(float(value) for value in dataset.GetPoint(index))
            for index in range(dataset.GetNumberOfPoints())
        ]
        density = _point_array(dataset, "Density", 1)
        momentum = _point_array(dataset, "Momentum", 3)
        velocity = _point_array(dataset, "Velocity", 3)
        mach = _point_array(dataset, "Mach", 1)
        _point_array(dataset, "Pressure", 1)
        _point_array(dataset, "Temperature", 1)
        yplus = _point_array(dataset, "Y_Plus", 1)
        skin_vectors = _point_array(dataset, "Skin_Friction_Coefficient", 3)
        mesh = parse_topology_smoke_su2(mesh_path)
        markers = contract["markers"]
        yplus_summary = summarize_wall_yplus(
            mesh,
            solution_points=solution_points,
            yplus=yplus,
            wall_marker=markers["wall"],
            target=float(contract["yplus"]["target"]),
            maximum=float(contract["yplus"]["maximum"]),
        )
        outlet_summary = analyze_outlets(
            mesh,
            solution_points=solution_points,
            density=density,
            momentum=momentum,
            velocity=velocity,
            mach=mach,
            outlet_markers=tuple(markers["rear_outlets"]),
            farfield_marker=markers["farfield"],
            wall_marker=markers["wall"],
        )
        freestream = contract["freestream"]
        reference = contract["reference"]
        history_summary = summarize_rans_history(
            history_path,
            freestream_density_kg_m3=float(freestream["density_kg_m3"]),
            freestream_velocity_m_s=float(freestream["velocity_m_s"]),
            reference_area_m2=float(reference["area_m2"]),
            reference_length_m=float(reference["length_m"]),
            moment_origin_m=reference["moment_origin_m"],
        )
        dynamic_pressure = history_summary["reference"]["dynamic_pressure_pa"]
        skin_summary = _skin_friction_summary(
            mesh, solution_points, skin_vectors, markers["wall"], dynamic_pressure
        )
        measurement_summary = _measurement_diagnostics(source, contract)
        status = "PASS" if yplus_summary["status"] == "PASS" else "FAIL"
        run_scope = contract.get("run_scope", {})
        if not isinstance(run_scope, dict):
            raise RANSDiagnosticError("diagnostic contract run_scope must be an object")
        spatial_order = int(run_scope.get("spatial_order", 1))
        restart_used = bool(run_scope.get("restart_used", False))
        if spatial_order not in (1, 2) or (spatial_order == 2 and not restart_used):
            raise RANSDiagnosticError("diagnostic contract has invalid numerical scope")
        order_label = "second-order restart" if spatial_order == 2 else "first-order"
        payload = {
            "status": status,
            "diagnostic_only": True,
            "production_eligible": False,
            "convergence_claimed": False,
            "spatial_order": spatial_order,
            "restart_used": restart_used,
            "paraview_version": _version(),
            "input_solution": {
                "path": str(input_path),
                "sha256": _sha256(input_path),
                "point_count": int(dataset.GetNumberOfPoints()),
                "cell_count": int(dataset.GetNumberOfCells()),
            },
            "point_arrays": _array_inventory(dataset),
            "yplus": yplus_summary,
            "skin_friction": skin_summary,
            "loads": history_summary,
            "outlets_and_mass_balance": outlet_summary,
            "measurement_surface": measurement_summary,
            "limitations": [
                "bounded {0} startup/diagnostic only".format(order_label),
                "no physical convergence or production boundary claim",
                "measurement integration uses the smoke-grid planar slice",
            ],
        }
    finally:
        simple.Delete(source)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0 if payload["status"] == "PASS" else 2


def _entry_point():
    try:
        return main()
    except (OutletDiagnosticError, RANSDiagnosticError, Exception):
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
