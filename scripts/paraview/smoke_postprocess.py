"""Run a minimal, non-interpretive ParaView integration smoke check.

The script is a ``pvbatch`` entry point.  It opens an existing SU2
visualization result, updates the pipeline, applies ``IntegrateVariables``,
and writes only the two explicitly requested connection-test artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import traceback

from paraview import simple


SUPPORTED_SUFFIXES = {".vtk", ".vtu", ".pvtu", ".vtm"}


def _paraview_version():
    value = simple.GetParaViewVersion()
    if isinstance(value, (tuple, list)):
        return ".".join(str(component) for component in value)
    return str(value)


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    return parser.parse_args()


def _resolve_input(path):
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError("input dataset is not a file: {0}".format(resolved))
    if resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            "unsupported dataset extension {0!r}; expected one of {1}".format(
                resolved.suffix,
                ", ".join(sorted(SUPPORTED_SUFFIXES)),
            )
        )
    return resolved


def _finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _finite_range(array_information, component):
    try:
        values = array_information.GetComponentRange(component)
        if values is None or len(values) != 2:
            return None
        minimum = _finite_number(values[0])
        maximum = _finite_number(values[1])
    except Exception:
        return None
    if minimum is None or maximum is None:
        return None
    return [minimum, maximum]


def _array_descriptors(data_information):
    descriptors = []
    associations = (
        ("point", "GetPointDataInformation"),
        ("cell", "GetCellDataInformation"),
        ("field", "GetFieldDataInformation"),
    )
    for association, method_name in associations:
        method = getattr(data_information, method_name, None)
        if method is None:
            continue
        try:
            attribute_information = method()
            count = int(attribute_information.GetNumberOfArrays())
        except Exception:
            continue
        for index in range(max(0, count)):
            try:
                information = attribute_information.GetArrayInformation(index)
            except Exception:
                information = None
            if information is None:
                continue
            try:
                raw_name = information.GetName()
            except Exception:
                raw_name = None
            try:
                components = max(0, int(information.GetNumberOfComponents()))
            except Exception:
                components = 0
            try:
                data_type = information.GetDataTypeAsString()
            except Exception:
                data_type = None

            ranges = []
            for component in range(components):
                value_range = _finite_range(information, component)
                if value_range is not None:
                    ranges.append(
                        {"component": component, "range": value_range}
                    )
            descriptor = {
                "association": association,
                "name": None if raw_name is None else str(raw_name),
                "number_of_components": components,
            }
            if data_type:
                descriptor["data_type"] = str(data_type)
            if ranges:
                descriptor["component_ranges"] = ranges
            descriptors.append(descriptor)
    return descriptors


def _is_integrable(descriptor):
    if descriptor["number_of_components"] <= 0:
        return False
    data_type = str(descriptor.get("data_type", "")).lower()
    if any(token in data_type for token in ("string", "unicode", "variant")):
        return False
    # A finite component range is direct evidence of numeric data.  When a
    # release omits ranges, a known non-string VTK data type is sufficient.
    return bool(descriptor.get("component_ranges") or data_type)


def _dataset_type(data_information):
    for method_name in ("GetPrettyDataTypeString", "GetDataSetTypeAsString"):
        method = getattr(data_information, method_name, None)
        if method is None:
            continue
        try:
            value = method()
        except Exception:
            continue
        if value:
            return str(value)
    return None


def _bounds(data_information):
    try:
        raw_bounds = data_information.GetBounds()
    except Exception:
        return None
    if raw_bounds is None or len(raw_bounds) != 6:
        return None
    values = [_finite_number(value) for value in raw_bounds]
    if any(value is None for value in values):
        return None
    return values


def _update_pipeline(proxy):
    try:
        simple.UpdatePipeline(proxy=proxy)
    except TypeError:
        proxy.UpdatePipeline()


def _write_json(path, payload):
    serialized = json.dumps(
        payload,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )
    path.write_text(serialized + "\n", encoding="utf-8")


def main():
    args = _parse_args()
    input_path = _resolve_input(args.input)
    output_directory = args.output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path = output_directory / "smoke_integral.csv"
    json_path = output_directory / "smoke_postprocess.json"
    if input_path in (csv_path, json_path):
        raise ValueError("post-processing output must not overwrite the input dataset")

    source = simple.OpenDataFile(str(input_path))
    if source is None:
        raise RuntimeError("ParaView could not create a reader for {0}".format(input_path))

    integrated = None
    try:
        _update_pipeline(source)
        source_information = source.GetDataInformation()
        if source_information is None:
            raise RuntimeError("ParaView returned no data information for {0}".format(input_path))

        input_arrays = _array_descriptors(source_information)
        integrate_filter = getattr(simple, "IntegrateVariables", None)
        if integrate_filter is None:
            raise RuntimeError("this ParaView build does not provide IntegrateVariables")
        try:
            integrated = integrate_filter(Input=source)
        except TypeError:
            integrated = integrate_filter(source)
        if integrated is None:
            raise RuntimeError("ParaView could not create the IntegrateVariables filter")
        _update_pipeline(integrated)

        integrated_information = integrated.GetDataInformation()
        if integrated_information is None:
            raise RuntimeError("IntegrateVariables returned no data information")

        simple.SaveData(str(csv_path), proxy=integrated)
        if not csv_path.is_file() or csv_path.stat().st_size <= 0:
            raise RuntimeError("ParaView did not create a non-empty integral CSV")

        payload = {
            "status": "PASS",
            "paraview_version": _paraview_version(),
            "input_file": str(input_path),
            "dataset_type": _dataset_type(source_information),
            "total_points": int(source_information.GetNumberOfPoints()),
            "total_cells": int(source_information.GetNumberOfCells()),
            "bounds": _bounds(source_information),
            "integrable_arrays": [
                descriptor for descriptor in input_arrays if _is_integrable(descriptor)
            ],
            "integrated_result_arrays": _array_descriptors(integrated_information),
            "integration_filter": "IntegrateVariables",
            "integral_csv": str(csv_path),
        }
    finally:
        if integrated is not None:
            simple.Delete(integrated)
        simple.Delete(source)

    _write_json(json_path, payload)
    return 0


def _entry_point():
    try:
        return main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
