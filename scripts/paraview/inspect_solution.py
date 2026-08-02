"""Inspect a SU2 visualization dataset from a headless ``pvbatch`` process.

This module intentionally imports ParaView at module scope: it must only be
interpreted by ``pvbatch`` and must never be imported by the controlling
Python process.
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
    parser.add_argument("--output", required=True, type=Path)
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


def _array_metadata(attribute_information):
    if attribute_information is None:
        return []

    arrays = []
    try:
        count = int(attribute_information.GetNumberOfArrays())
    except Exception:
        return arrays

    for index in range(max(0, count)):
        try:
            information = attribute_information.GetArrayInformation(index)
        except Exception:
            information = None
        if information is None:
            continue

        try:
            name = information.GetName()
        except Exception:
            name = None
        try:
            components = int(information.GetNumberOfComponents())
        except Exception:
            components = 0

        entry = {
            "name": None if name is None else str(name),
            "number_of_components": max(0, components),
        }
        component_ranges = []
        for component in range(max(0, components)):
            component_entry = {"component": component}
            try:
                component_name = information.GetComponentName(component)
            except Exception:
                component_name = None
            if component_name:
                component_entry["name"] = str(component_name)
            value_range = _finite_range(information, component)
            if value_range is not None:
                component_entry["range"] = value_range
            component_ranges.append(component_entry)
        if component_ranges:
            entry["component_ranges"] = component_ranges

        # ParaView exposes a vector-magnitude range through component -1 on
        # versions that support it.  Omit it when it is unavailable.
        if components > 1:
            magnitude_range = _finite_range(information, -1)
            if magnitude_range is not None:
                entry["magnitude_range"] = magnitude_range
        arrays.append(entry)
    return arrays


def _attribute_information(data_information, method_name):
    method = getattr(data_information, method_name, None)
    if method is None:
        return None
    try:
        return method()
    except Exception:
        return None


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


def _block_count(data_information, depth=0):
    """Count leaf blocks without fetching the server-side dataset."""

    if depth > 64:
        raise RuntimeError("composite dataset nesting exceeds 64 levels")
    method = getattr(data_information, "GetCompositeDataInformation", None)
    if method is None:
        return 1
    try:
        composite = method()
    except Exception:
        return 1
    if composite is None:
        return 1

    is_composite_method = getattr(composite, "GetDataIsComposite", None)
    if is_composite_method is not None:
        try:
            if not bool(is_composite_method()):
                return 1
        except Exception:
            pass

    children_method = getattr(composite, "GetNumberOfChildren", None)
    child_info_method = getattr(composite, "GetDataInformation", None)
    if children_method is None or child_info_method is None:
        return 1
    try:
        children = int(children_method())
    except Exception:
        return 1
    if children <= 0:
        return 0

    count = 0
    for index in range(children):
        try:
            child_information = child_info_method(index)
        except Exception:
            child_information = None
        if child_information is not None:
            count += _block_count(child_information, depth + 1)
    return count


def _update_pipeline(source):
    try:
        simple.UpdatePipeline(proxy=source)
    except TypeError:
        # Older ParaView releases expose UpdatePipeline only on the proxy.
        source.UpdatePipeline()


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
    output_path = args.output.expanduser().resolve()
    if output_path == input_path:
        raise ValueError("output JSON must not overwrite the input dataset")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source = simple.OpenDataFile(str(input_path))
    if source is None:
        raise RuntimeError("ParaView could not create a reader for {0}".format(input_path))

    try:
        _update_pipeline(source)
        data_information = source.GetDataInformation()
        if data_information is None:
            raise RuntimeError("ParaView returned no data information for {0}".format(input_path))

        point_arrays = _array_metadata(
            _attribute_information(data_information, "GetPointDataInformation")
        )
        cell_arrays = _array_metadata(
            _attribute_information(data_information, "GetCellDataInformation")
        )
        field_arrays = _array_metadata(
            _attribute_information(data_information, "GetFieldDataInformation")
        )
        payload = {
            "status": "PASS",
            "paraview_version": _paraview_version(),
            "input_file": str(input_path),
            "dataset_type": _dataset_type(data_information),
            "block_count": _block_count(data_information),
            "number_of_points": int(data_information.GetNumberOfPoints()),
            "number_of_cells": int(data_information.GetNumberOfCells()),
            "bounds": _bounds(data_information),
            "point_data_array_names": [entry["name"] for entry in point_arrays],
            "cell_data_array_names": [entry["name"] for entry in cell_arrays],
            "field_data_array_names": [entry["name"] for entry in field_arrays],
            "point_data_arrays": point_arrays,
            "cell_data_arrays": cell_arrays,
            "field_data_arrays": field_arrays,
        }
    finally:
        simple.Delete(source)

    _write_json(output_path, payload)
    return 0


def _entry_point():
    try:
        return main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
