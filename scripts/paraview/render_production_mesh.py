"""Render an accepted production mesh through headless ``pvbatch`` only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import traceback

from paraview import simple


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save(view, path):
    simple.Render(view)
    try:
        simple.SaveScreenshot(
            str(path),
            view,
            ImageResolution=[1600, 1000],
            TransparentBackground=0,
        )
    except TypeError:
        simple.SaveScreenshot(str(path), view)
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("pvbatch did not create a non-empty image: {0}".format(path))
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _camera(view, bounds, direction, up):
    center = [
        0.5 * (bounds[0] + bounds[1]),
        0.5 * (bounds[2] + bounds[3]),
        0.5 * (bounds[4] + bounds[5]),
    ]
    span = max(
        bounds[1] - bounds[0],
        bounds[3] - bounds[2],
        bounds[5] - bounds[4],
    )
    view.CameraFocalPoint = center
    view.CameraPosition = [center[i] + 2.5 * span * direction[i] for i in range(3)]
    view.CameraViewUp = up
    view.CameraParallelProjection = 1
    view.ResetCamera()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mesh", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    mesh = args.mesh.expanduser().resolve(strict=True)
    output = args.output_directory.expanduser().resolve(strict=False)
    if mesh.suffix.lower() not in {".msh", ".vtk"} or not mesh.is_file() or mesh.is_symlink():
        raise ValueError("mesh input must be a regular non-link .msh or .vtk file")
    if output.exists():
        raise ValueError("render output directory already exists")
    output.mkdir(parents=True)
    manifest_path = output / "mesh_render_manifest.json"
    manifest = {
        "schema": "cfdpipe.production_mesh_render.v1",
        "status": "FAIL",
        "mesh": str(mesh),
        "mesh_sha256": _sha256(mesh),
        "paraview_version": None,
        "number_of_points": None,
        "number_of_cells": None,
        "bounds": None,
        "images": [],
        "error": None,
    }
    source = None
    merged = None
    slice_proxy = None
    view = None
    try:
        source = simple.OpenDataFile(str(mesh))
        if source is None:
            raise RuntimeError("ParaView could not open the Gmsh MSH")
        simple.UpdatePipeline(proxy=source)
        information = source.GetDataInformation()
        render_source = source
        if int(information.GetNumberOfPoints()) <= 0:
            merge_constructor = getattr(simple, "MergeBlocks", None)
            if merge_constructor is None:
                raise RuntimeError("ParaView provides no MergeBlocks filter for composite data")
            merged = merge_constructor(Input=source)
            simple.UpdatePipeline(proxy=merged)
            information = merged.GetDataInformation()
            render_source = merged
        bounds = [float(value) for value in information.GetBounds()]
        if len(bounds) != 6 or not all(math.isfinite(value) for value in bounds):
            raise RuntimeError("ParaView returned invalid mesh bounds")
        points = int(information.GetNumberOfPoints())
        cells = int(information.GetNumberOfCells())
        if points <= 0 or cells <= 0:
            raise RuntimeError("ParaView returned an empty production mesh")
        manifest.update(
            {
                "paraview_version": str(simple.GetParaViewVersion()),
                "number_of_points": points,
                "number_of_cells": cells,
                "bounds": bounds,
            }
        )
        view = simple.CreateView("RenderView")
        view.Background = [1.0, 1.0, 1.0]
        display = simple.Show(render_source, view)
        display.Representation = "Surface With Edges"
        display.DiffuseColor = [0.72, 0.78, 0.88]
        display.EdgeColor = [0.08, 0.08, 0.08]
        _camera(view, bounds, [1.0, -1.0, 0.7], [0.0, 0.0, 1.0])
        manifest["images"].append(_save(view, output / "medium_mesh_isometric.png"))
        simple.Hide(render_source, view)

        slice_proxy = simple.Slice(Input=render_source)
        slice_proxy.SliceType = "Plane"
        slice_proxy.SliceType.Origin = [
            0.5 * (bounds[0] + bounds[1]),
            0.5 * (bounds[2] + bounds[3]),
            0.0,
        ]
        slice_proxy.SliceType.Normal = [0.0, 0.0, 1.0]
        simple.UpdatePipeline(proxy=slice_proxy)
        slice_display = simple.Show(slice_proxy, view)
        slice_display.Representation = "Surface With Edges"
        slice_display.DiffuseColor = [0.85, 0.88, 0.94]
        slice_display.EdgeColor = [0.05, 0.05, 0.05]
        _camera(view, bounds, [0.0, 0.0, 1.0], [0.0, 1.0, 0.0])
        manifest["images"].append(_save(view, output / "medium_mesh_center_slice.png"))
        manifest["status"] = "PASS"
    except Exception:
        manifest["error"] = traceback.format_exc()
    finally:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        for proxy in (slice_proxy, merged, source, view):
            if proxy is not None:
                try:
                    simple.Delete(proxy)
                except Exception:
                    pass
    print(json.dumps({"status": manifest["status"], "manifest": str(manifest_path)}))
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
