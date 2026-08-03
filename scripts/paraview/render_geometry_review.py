"""Render deterministic, headless geometry-review images through ``pvbatch``.

This file intentionally imports ParaView and must only be interpreted by
``pvbatch``.  It does not open a GUI or modify the input geometry dataset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import traceback

from paraview import simple


VIEW_NAMES = ("isometric", "XY", "XZ", "YZ")


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--groups", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    return parser.parse_args()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, payload):
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _paraview_version():
    value = simple.GetParaViewVersion()
    if isinstance(value, (tuple, list)):
        return ".".join(str(component) for component in value)
    return str(value)


def _update_pipeline(proxy):
    try:
        simple.UpdatePipeline(proxy=proxy)
    except TypeError:
        method = getattr(proxy, "UpdatePipeline", None)
        if method is None:
            raise RuntimeError("this ParaView build exposes no UpdatePipeline API")
        method()


def _dataset_type(data_information):
    for method_name in ("GetPrettyDataTypeString", "GetDataSetTypeAsString"):
        method = getattr(data_information, method_name, None)
        if method is None:
            continue
        value = method()
        if value:
            return str(value)
    return None


def _finite_bounds(data_information):
    values = data_information.GetBounds()
    if values is None or len(values) != 6:
        raise RuntimeError("ParaView returned no six-component dataset bounds")
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise RuntimeError("ParaView returned non-finite dataset bounds")
    return result


def _cell_array_names(data_information):
    attributes = data_information.GetCellDataInformation()
    if attributes is None:
        return []
    names = []
    for index in range(max(0, int(attributes.GetNumberOfArrays()))):
        information = attributes.GetArrayInformation(index)
        if information is None:
            continue
        name = information.GetName()
        if name is not None and str(name).strip():
            names.append(str(name))
    return names


def _select_tag_array(names):
    by_lower = {name.lower(): name for name in names}
    for exact in ("gmsh:geometrical", "gmsh:physical"):
        if exact in by_lower:
            return by_lower[exact]
    for name in names:
        if "entity" in name.lower():
            return name
    raise RuntimeError(
        "no cell entity-tag array found; expected gmsh:geometrical, "
        "gmsh:physical, or an array containing 'entity'; available arrays: {0}".format(
            names
        )
    )


def _load_groups(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("groups JSON root must be an object")
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, (dict, list)):
        raise ValueError("groups JSON needs a non-empty groups object or list")
    entries = []
    if isinstance(raw_groups, dict):
        entries.extend(raw_groups.items())
    else:
        for index, item in enumerate(raw_groups):
            if not isinstance(item, dict):
                raise ValueError("groups entry {0} must be an object".format(index))
            entries.append((item.get("name"), item))
    groups = []
    names = set()
    for raw_name, specification in entries:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("every geometry group needs a non-empty name")
        name = raw_name.strip()
        if name.lower() == "all" or name in names:
            raise ValueError("geometry group name is reserved or duplicated: {0}".format(name))
        names.add(name)
        if isinstance(specification, dict):
            tags = specification.get("surface_tags")
            if tags is None:
                tags = specification.get("entity_tags")
        else:
            tags = specification
        if not isinstance(tags, list) or not tags:
            raise ValueError(
                "geometry group {0!r} needs non-empty surface_tags".format(name)
            )
        normalized_tags = []
        for tag in tags:
            if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
                raise ValueError(
                    "geometry group {0!r} has invalid tag {1!r}".format(name, tag)
                )
            if tag not in normalized_tags:
                normalized_tags.append(tag)
        groups.append({"name": name, "surface_tags": normalized_tags})
    if not groups:
        raise ValueError("groups JSON contains no geometry groups")
    return groups


def _threshold_for_tag(source, tag_array, tag):
    constructor = getattr(simple, "Threshold", None)
    if constructor is None:
        raise RuntimeError("this ParaView build does not provide Threshold")
    try:
        threshold = constructor(Input=source)
    except TypeError:
        threshold = constructor(source)
    if threshold is None:
        raise RuntimeError("ParaView could not create Threshold for tag {0}".format(tag))
    try:
        threshold.Scalars = ["CELLS", tag_array]
    except Exception as error:
        simple.Delete(threshold)
        raise RuntimeError(
            "this ParaView build cannot select cell array {0!r}: {1}".format(
                tag_array, error
            )
        )
    try:
        threshold.LowerThreshold = float(tag)
        threshold.UpperThreshold = float(tag)
    except Exception:
        try:
            threshold.ThresholdRange = [float(tag), float(tag)]
        except Exception as error:
            simple.Delete(threshold)
            raise RuntimeError(
                "this ParaView build exposes no compatible exact Threshold range API: {0}".format(
                    error
                )
            )
    _update_pipeline(threshold)
    return threshold


def _group_proxy(source, tag_array, tags):
    thresholds = [_threshold_for_tag(source, tag_array, tag) for tag in tags]
    if len(thresholds) == 1:
        return thresholds[0], thresholds
    constructor = getattr(simple, "AppendDatasets", None)
    if constructor is None:
        for proxy in reversed(thresholds):
            simple.Delete(proxy)
        raise RuntimeError(
            "this ParaView build does not provide AppendDatasets for multi-tag groups"
        )
    try:
        combined = constructor(Input=thresholds)
    except TypeError:
        combined = constructor(thresholds)
    if combined is None:
        for proxy in reversed(thresholds):
            simple.Delete(proxy)
        raise RuntimeError("ParaView could not combine a multi-tag geometry group")
    _update_pipeline(combined)
    return combined, [combined] + thresholds


def _safe_filename(value):
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return normalized or "group"


def _create_render_view():
    constructor = getattr(simple, "CreateView", None)
    if constructor is not None:
        view = constructor("RenderView")
    else:
        fallback = getattr(simple, "GetActiveViewOrCreate", None)
        if fallback is None:
            raise RuntimeError("this ParaView build exposes no RenderView creation API")
        view = fallback("RenderView")
    if view is None:
        raise RuntimeError("ParaView could not create a RenderView")
    return view


def _set_camera(view, bounds, view_name):
    center = [
        0.5 * (bounds[0] + bounds[1]),
        0.5 * (bounds[2] + bounds[3]),
        0.5 * (bounds[4] + bounds[5]),
    ]
    span = max(
        bounds[1] - bounds[0],
        bounds[3] - bounds[2],
        bounds[5] - bounds[4],
        1.0e-9,
    )
    distance = 3.0 * span
    cameras = {
        "isometric": ([distance, distance, distance], [0.0, 0.0, 1.0]),
        "XY": ([0.0, 0.0, distance], [0.0, 1.0, 0.0]),
        "XZ": ([0.0, -distance, 0.0], [0.0, 0.0, 1.0]),
        "YZ": ([distance, 0.0, 0.0], [0.0, 0.0, 1.0]),
    }
    offset, view_up = cameras[view_name]
    values = {
        "CameraFocalPoint": center,
        "CameraPosition": [center[index] + offset[index] for index in range(3)],
        "CameraViewUp": view_up,
        "CameraParallelProjection": 1,
    }
    for property_name, value in values.items():
        try:
            setattr(view, property_name, value)
        except Exception as error:
            raise RuntimeError(
                "this ParaView build cannot set RenderView property {0}: {1}".format(
                    property_name, error
                )
            )


def _render_group(proxy, group_name, output_directory, bounds):
    view = _create_render_view()
    display = None
    records = []
    try:
        display = simple.Show(proxy, view)
        if display is None:
            raise RuntimeError(
                "ParaView could not show geometry group {0!r}".format(group_name)
            )
        if hasattr(display, "Representation"):
            display.Representation = "Surface With Edges"
        reset = getattr(simple, "ResetCamera", None)
        if reset is not None:
            try:
                reset(view)
            except TypeError:
                reset()
        elif hasattr(view, "ResetCamera"):
            view.ResetCamera()
        else:
            raise RuntimeError("this ParaView build exposes no ResetCamera API")

        for view_name in VIEW_NAMES:
            _set_camera(view, bounds, view_name)
            renderer = getattr(simple, "Render", None)
            if renderer is not None:
                try:
                    renderer(view)
                except TypeError:
                    renderer()
            filename = "{0}__{1}.png".format(_safe_filename(group_name), view_name)
            image_path = output_directory / filename
            try:
                simple.SaveScreenshot(
                    str(image_path),
                    view,
                    ImageResolution=[1200, 900],
                    TransparentBackground=0,
                )
            except TypeError:
                simple.SaveScreenshot(str(image_path), view)
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                raise RuntimeError(
                    "ParaView did not create non-empty screenshot {0}".format(image_path)
                )
            records.append(
                {
                    "group": group_name,
                    "view": view_name,
                    "path": str(image_path),
                    "size_bytes": image_path.stat().st_size,
                    "sha256": _sha256(image_path),
                }
            )
    finally:
        if display is not None:
            hide = getattr(simple, "Hide", None)
            if hide is not None:
                try:
                    hide(proxy, view)
                except Exception:
                    pass
        simple.Delete(view)
    return records


def main():
    args = _parse_args()
    input_path = args.input.expanduser().resolve(strict=True)
    groups_path = args.groups.expanduser().resolve(strict=True)
    output_directory = args.output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = output_directory / "geometry_render_manifest.json"
    payload = {
        "status": "FAIL",
        "paraview_version": None,
        "input_file": str(input_path),
        "input_sha256": None,
        "groups_file": str(groups_path),
        "groups_sha256": None,
        "dataset_type": None,
        "number_of_points": None,
        "number_of_cells": None,
        "bounds": None,
        "available_cell_arrays": [],
        "tag_array": None,
        "groups": [],
        "images": [],
        "error": None,
    }
    source = None
    created_proxies = []
    try:
        if not input_path.is_file() or input_path.suffix.lower() != ".vtk":
            raise ValueError("geometry input must be an existing .vtk file")
        if not groups_path.is_file() or groups_path.suffix.lower() != ".json":
            raise ValueError("geometry groups must be an existing JSON file")
        if input_path in {groups_path, manifest_path} or groups_path == manifest_path:
            raise ValueError("geometry review outputs must not overwrite inputs")
        groups = _load_groups(groups_path)
        payload["input_sha256"] = _sha256(input_path)
        payload["groups_sha256"] = _sha256(groups_path)
        payload["paraview_version"] = _paraview_version()

        source = simple.OpenDataFile(str(input_path))
        if source is None:
            raise RuntimeError(
                "ParaView could not create a reader for {0}".format(input_path)
            )
        _update_pipeline(source)
        information = source.GetDataInformation()
        if information is None:
            raise RuntimeError("ParaView returned no geometry data information")
        points = int(information.GetNumberOfPoints())
        cells = int(information.GetNumberOfCells())
        if points <= 0 or cells <= 0:
            raise RuntimeError(
                "geometry dataset must contain positive point and cell counts; "
                "got points={0}, cells={1}".format(points, cells)
            )
        bounds = _finite_bounds(information)
        cell_arrays = _cell_array_names(information)
        tag_array = _select_tag_array(cell_arrays)
        payload.update(
            {
                "dataset_type": _dataset_type(information),
                "number_of_points": points,
                "number_of_cells": cells,
                "bounds": bounds,
                "available_cell_arrays": cell_arrays,
                "tag_array": tag_array,
            }
        )

        all_tags = sorted(
            {tag for group in groups for tag in group["surface_tags"]}
        )
        group_records = [{"name": "all", "surface_tags": all_tags}] + groups
        payload["groups"] = group_records
        payload["images"].extend(
            _render_group(source, "all", output_directory, bounds)
        )
        for group in groups:
            proxy, proxies = _group_proxy(
                source, tag_array, group["surface_tags"]
            )
            created_proxies.extend(proxies)
            proxy_information = proxy.GetDataInformation()
            if proxy_information is None:
                raise RuntimeError(
                    "ParaView returned no data information for group {0!r}".format(
                        group["name"]
                    )
                )
            if int(proxy_information.GetNumberOfCells()) <= 0:
                raise RuntimeError(
                    "geometry group {0!r} selected zero cells for tags {1}".format(
                        group["name"], group["surface_tags"]
                    )
                )
            group_bounds = _finite_bounds(proxy_information)
            payload["images"].extend(
                _render_group(
                    proxy,
                    group["name"],
                    output_directory,
                    group_bounds,
                )
            )
        payload["status"] = "PASS"
        payload["error"] = None
        _write_json(manifest_path, payload)
        return 0
    except Exception as error:
        payload["status"] = "FAIL"
        payload["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
        _write_json(manifest_path, payload)
        raise
    finally:
        for proxy in reversed(created_proxies):
            try:
                simple.Delete(proxy)
            except Exception:
                pass
        if source is not None:
            try:
                simple.Delete(source)
            except Exception:
                pass


def _entry_point():
    try:
        return main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
