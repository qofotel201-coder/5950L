from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from cfdpipe.bridges import gmsh_bridge


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONNECTION_ROOT = (REPOSITORY_ROOT / "runs" / "connection").resolve()


VALID_SU2_TEXT = """\
NDIME= 2
NELEM= 2
5 0 1 2 0
5 0 2 3 1
NPOIN= 4
0.0 0.0 0
1.0 0.0 1
1.0 0.5 2
0.0 0.5 3
NMARK= 1
MARKER_TAG= farfield
MARKER_ELEMS= 4
3 0 1
3 1 2
3 2 3
3 3 0
"""


@contextlib.contextmanager
def connection_temporary_directory(prefix: str):
    """Create test-only scratch space under the mandated connection root."""

    CONNECTION_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=CONNECTION_ROOT) as directory:
        path = Path(directory).resolve()
        if CONNECTION_ROOT not in path.parents:
            raise AssertionError(f"temporary directory escaped {CONNECTION_ROOT}: {path}")
        yield path


def _large_valid_su2_text() -> str:
    """Return a small-on-disk but target-count smoke SU2 mesh fixture."""

    element_rows: list[str] = []
    element_id = 0
    for row in range(24):
        for column in range(24):
            lower_left = row * 25 + column
            lower_right = lower_left + 1
            upper_left = lower_left + 25
            upper_right = upper_left + 1
            element_rows.append(
                f"5 {lower_left} {lower_right} {upper_right} {element_id}"
            )
            element_id += 1
            element_rows.append(
                f"5 {lower_left} {upper_right} {upper_left} {element_id}"
            )
            element_id += 1
    element_lines = "\n".join(element_rows)
    point_lines = "\n".join(
        f"{(point_id % 25) / 24:.8f} "
        f"{(point_id // 25) / 48:.8f} {point_id}"
        for point_id in range(625)
    )
    boundary_edges = (
        [(index, index + 1) for index in range(24)]
        + [(row * 25 + 24, (row + 1) * 25 + 24) for row in range(24)]
        + [(600 + index + 1, 600 + index) for index in range(24)]
        + [((row + 1) * 25, row * 25) for row in range(23, -1, -1)]
    )
    boundary_lines = "\n".join(
        f"3 {first} {second}" for first, second in boundary_edges
    )
    return (
        "NDIME= 2\n"
        "NELEM= 1152\n"
        f"{element_lines}\n"
        "NPOIN= 625\n"
        f"{point_lines}\n"
        "NMARK= 1\n"
        "MARKER_TAG= farfield\n"
        "MARKER_ELEMS= 96\n"
        f"{boundary_lines}\n"
    )


def make_fake_gmsh(
    *,
    initialize_error: BaseException | None = None,
    generate_error: BaseException | None = None,
    write_failure_at: int | None = None,
) -> ModuleType:
    """Build a deterministic in-memory stand-in for the public Gmsh API."""

    gmsh = ModuleType("gmsh")
    gmsh.__version__ = "4.13.1"
    gmsh.__file__ = str((CONNECTION_ROOT / "fake_sdk" / "gmsh.py").resolve())
    gmsh.timeline = []

    gmsh.initialize = mock.Mock(side_effect=initialize_error)
    gmsh.finalize = mock.Mock()
    gmsh.fltk = SimpleNamespace(run=mock.Mock())

    def set_string(name, value):
        gmsh.timeline.append(("setString", name, value))

    gmsh.option = SimpleNamespace(
        setString=mock.Mock(side_effect=set_string),
        setNumber=mock.Mock(),
    )
    gmsh.logger = SimpleNamespace(
        start=mock.Mock(),
        get=mock.Mock(return_value=["Info : fake smoke mesh complete"]),
        stop=mock.Mock(),
    )

    def add_rectangle(x, y, z, length, height, *args, **kwargs):
        del args, kwargs
        gmsh.timeline.append(("addRectangle", x, y, z, length, height))
        return 17

    def import_shapes(path, *args, **kwargs):
        del args, kwargs
        gmsh.timeline.append(("importShapes", str(path)))
        return [(3, 101)]

    def occ_synchronize():
        gmsh.timeline.append(("occ.synchronize",))

    occ = SimpleNamespace(
        addRectangle=mock.Mock(side_effect=add_rectangle),
        importShapes=mock.Mock(side_effect=import_shapes),
        synchronize=mock.Mock(side_effect=occ_synchronize),
    )

    # The fake also exposes the built-in geometry kernel so the test constrains
    # behaviour (a rectangle plus synchronization), not a particular kernel.
    geo_point_tags = iter((1, 2, 3, 4))
    geo_line_tags = iter((11, 12, 13, 14))
    geo = SimpleNamespace(
        addPoint=mock.Mock(side_effect=lambda *args, **kwargs: next(geo_point_tags)),
        addLine=mock.Mock(side_effect=lambda *args, **kwargs: next(geo_line_tags)),
        addCurveLoop=mock.Mock(return_value=21),
        addPlaneSurface=mock.Mock(return_value=17),
        synchronize=mock.Mock(side_effect=lambda: gmsh.timeline.append(("geo.synchronize",))),
    )

    def get_elements(dim=-1, tag=-1):
        del tag
        triangle_tags = list(range(1, 1153))
        triangle_nodes = [node for _ in triangle_tags for node in (1, 2, 3)]
        boundary_tags = [1153, 1154, 1155, 1156]
        boundary_nodes = [1, 2, 2, 3, 3, 4, 4, 1]
        if dim == 1:
            return [1], [boundary_tags], [boundary_nodes]
        if dim == 2:
            return [2], [triangle_tags], [triangle_nodes]
        return (
            [1, 2],
            [boundary_tags, triangle_tags],
            [boundary_nodes, triangle_nodes],
        )

    mesh = SimpleNamespace(
        generate=mock.Mock(side_effect=generate_error),
        setSize=mock.Mock(),
        getNodes=mock.Mock(
            return_value=(
                list(range(1, 626)),
                [0.0] * (625 * 3),
                [],
            )
        ),
        getElements=mock.Mock(side_effect=get_elements),
        getElementProperties=mock.Mock(
            side_effect=lambda element_type: (
                ("Line", 1, 1, 2, [], 2)
                if element_type == 1
                else ("Triangle", 2, 1, 3, [], 3)
            )
        ),
    )

    def add_physical_group(dimension, entity_tags, *args, **kwargs):
        del args, kwargs
        group_tag = {1: 91, 2: 92, 3: 93}[dimension]
        gmsh.timeline.append(
            ("addPhysicalGroup", dimension, tuple(entity_tags), group_tag)
        )
        return group_tag

    def set_physical_name(dimension, group_tag, name):
        gmsh.timeline.append(
            ("setPhysicalName", dimension, group_tag, str(name))
        )

    model = SimpleNamespace(
        add=mock.Mock(),
        occ=occ,
        geo=geo,
        mesh=mesh,
        getBoundary=mock.Mock(
            return_value=[(1, 11), (1, 12), (1, 13), (1, 14)]
        ),
        getEntities=mock.Mock(
            side_effect=lambda dimension=-1: {
                0: [(0, 1), (0, 2), (0, 3), (0, 4)],
                1: [(1, 11), (1, 12), (1, 13), (1, 14)],
                2: [(2, 17), (2, 31), (2, 32)],
                3: [(3, 101)],
            }.get(dimension, [(2, 17)])
        ),
        addPhysicalGroup=mock.Mock(side_effect=add_physical_group),
        setPhysicalName=mock.Mock(side_effect=set_physical_name),
        getPhysicalGroups=mock.Mock(return_value=[(1, 91), (2, 92)]),
        getPhysicalName=mock.Mock(
            side_effect=lambda dimension, tag: {
                (1, 91): "farfield",
                (2, 92): "fluid",
            }.get((dimension, tag), "")
        ),
        getEntitiesForPhysicalGroup=mock.Mock(
            side_effect=lambda dimension, tag: {
                (1, 91): [11, 12, 13, 14],
                (2, 92): [17],
            }.get((dimension, tag), [])
        ),
    )
    gmsh.model = model

    write_count = 0

    def write_mesh(path_value):
        nonlocal write_count
        write_count += 1
        if write_failure_at == write_count:
            raise RuntimeError(f"write failure {write_count}")
        path = Path(path_value).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".su2":
            path.write_text(_large_valid_su2_text(), encoding="utf-8")
        else:
            path.write_text(
                "$MeshFormat\n4.1 0 8\n$EndMeshFormat\n",
                encoding="utf-8",
            )
        gmsh.timeline.append(("write", str(path)))

    gmsh.write = mock.Mock(side_effect=write_mesh)
    return gmsh


def make_bridge():
    """Return a bridge with deterministic mocked Gmsh CLI provenance."""

    cli_path = (CONNECTION_ROOT / "fake_sdk" / "gmsh.exe").resolve()
    toolchain = mock.Mock()
    toolchain.resolve.return_value = SimpleNamespace(
        name="gmsh", path=cli_path, source="test"
    )
    command_result = SimpleNamespace(
        stdout="4.13.1\n",
        stderr="",
        returncode=0,
        ok=True,
        as_metadata=lambda: {
            "executable": str(cli_path),
            "args": ["--version"],
            "command": [str(cli_path), "--version"],
            "returncode": 0,
        },
    )
    runner = mock.Mock()
    runner.run.return_value = command_result
    return gmsh_bridge.GmshBridge(runner=runner, toolchain=toolchain), runner, toolchain


def marker_names(marker_payload) -> set[str]:
    if isinstance(marker_payload, dict):
        return {str(name) for name in marker_payload}
    names = set()
    for marker in marker_payload:
        if isinstance(marker, str):
            names.add(marker)
        elif isinstance(marker, dict):
            names.add(str(marker.get("name", marker.get("tag"))))
    return names


class Su2MeshTextValidationTests(unittest.TestCase):
    def _write(self, directory: Path, content: str) -> Path:
        path = directory / "fixture.su2"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_mesh_parses_all_required_counts_and_farfield(self):
        with connection_temporary_directory("gmsh_su2_valid_") as directory:
            report = gmsh_bridge.validate_su2_mesh(
                self._write(directory, VALID_SU2_TEXT)
            )

        self.assertEqual(report["ndime"], 2)
        self.assertEqual(report["nelem"], 2)
        self.assertEqual(report["npoin"], 4)
        self.assertEqual(report["nmark"], 1)
        self.assertEqual(report["markers"]["farfield"], 4)
        quality = report["quality"]
        self.assertEqual(quality["status"], "PASS")
        self.assertEqual(quality["triangle_count"], 2)
        self.assertEqual(quality["negative_signed_area_count"], 0)
        self.assertEqual(quality["degenerate_triangle_count"], 0)
        self.assertEqual(quality["nonmanifold_edge_count"], 0)
        self.assertEqual(quality["farfield_edge_count"], 4)
        self.assertEqual(quality["boundary_component_count"], 1)
        self.assertAlmostEqual(quality["total_area_m2"], 0.5)
        self.assertGreater(quality["minimum_normalized_quality"], 0.0)
        self.assertGreater(quality["minimum_angle_deg"], 0.0)

    def test_invalid_triangle_geometry_or_boundary_is_rejected(self):
        invalid_cases = (
            VALID_SU2_TEXT.replace("1.0 0.5 2", "2.0 0.0 2"),
            VALID_SU2_TEXT.replace("5 0 1 2 0", "5 0 2 1 0"),
            VALID_SU2_TEXT.replace("5 0 1 2 0", "9 0 1 2 0"),
            VALID_SU2_TEXT.replace("3 0 1", "3 0 2"),
            VALID_SU2_TEXT.replace("1.0 0.5 2", "1.0 0.0 2"),
        )
        for index, content in enumerate(invalid_cases):
            with self.subTest(index=index):
                with connection_temporary_directory(
                    f"gmsh_su2_bad_quality_{index}_"
                ) as directory:
                    with self.assertRaises(gmsh_bridge.SU2MeshValidationError):
                        gmsh_bridge.validate_su2_mesh(
                            self._write(directory, content)
                        )

    def test_nan_or_inf_is_rejected(self):
        invalid_cases = (
            VALID_SU2_TEXT.replace("0.0 0.0 0", "NaN 0.0 0"),
            VALID_SU2_TEXT.replace("0.0 0.0 0", "Inf 0.0 0"),
        )
        for index, content in enumerate(invalid_cases):
            with self.subTest(index=index):
                with connection_temporary_directory(
                    f"gmsh_su2_nonfinite_{index}_"
                ) as directory:
                    with self.assertRaises(ValueError):
                        gmsh_bridge.validate_su2_mesh(
                            self._write(directory, content)
                        )

    def test_missing_farfield_marker_is_rejected(self):
        content = VALID_SU2_TEXT.replace(
            "MARKER_TAG= farfield", "MARKER_TAG= wall"
        )
        with connection_temporary_directory("gmsh_su2_no_farfield_") as directory:
            with self.assertRaises(ValueError):
                gmsh_bridge.validate_su2_mesh(self._write(directory, content))

    def test_unparseable_or_nonpositive_count_is_rejected(self):
        invalid_cases = (
            VALID_SU2_TEXT.replace("NELEM= 2", "NELEM= triangles"),
            VALID_SU2_TEXT.replace("NPOIN= 4", "NPOIN= 0"),
            VALID_SU2_TEXT.replace("MARKER_ELEMS= 4", "MARKER_ELEMS= nope"),
            VALID_SU2_TEXT.replace("MARKER_ELEMS= 4", "MARKER_ELEMS= 0"),
            VALID_SU2_TEXT.replace("NDIME= 2", "NDIME= 3"),
            "",
        )
        for index, content in enumerate(invalid_cases):
            with self.subTest(index=index):
                with connection_temporary_directory(
                    f"gmsh_su2_bad_count_{index}_"
                ) as directory:
                    with self.assertRaises(ValueError):
                        gmsh_bridge.validate_su2_mesh(
                            self._write(directory, content)
                        )


class GmshSmokeMeshTests(unittest.TestCase):
    def test_cli_probe_failure_stops_before_import_or_initialize(self):
        gmsh = make_fake_gmsh()
        bridge, runner, _ = make_bridge()
        runner.run.side_effect = RuntimeError("CLI probe sentinel")

        with connection_temporary_directory("gmsh_cli_failure_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                with self.assertRaisesRegex(RuntimeError, "CLI probe sentinel"):
                    bridge.build_smoke_mesh(directory)

            manifest = json.loads(
                (directory / "gmsh_manifest.json").read_text(encoding="utf-8")
            )
            log_text = (directory / "gmsh.log").read_text(encoding="utf-8")

        self.assertEqual(manifest["status"], "FAIL")
        self.assertEqual(manifest["error"]["message"], "CLI probe sentinel")
        self.assertIn("Traceback (most recent call last)", log_text)
        gmsh.initialize.assert_not_called()
        gmsh.finalize.assert_not_called()

    def test_smoke_uses_direct_import_and_always_closes_owned_session(self):
        gmsh = make_fake_gmsh()
        bridge, _, _ = make_bridge()
        imported_names = []
        real_import = builtins.__import__

        def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported_names.append(name)
            return real_import(name, globals, locals, fromlist, level)

        with connection_temporary_directory("gmsh_lifecycle_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                with mock.patch("builtins.__import__", new=tracking_import):
                    bridge.smoke(output_directory=directory, timeout=10)

        self.assertIn("gmsh", imported_names)
        gmsh.initialize.assert_called_once_with()
        gmsh.finalize.assert_called_once_with()
        gmsh.fltk.run.assert_not_called()

    def test_rectangle_is_synchronized_meshed_in_2d_and_written_twice(self):
        gmsh = make_fake_gmsh()
        bridge, _, _ = make_bridge()
        with connection_temporary_directory("gmsh_rectangle_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                bridge.build_smoke_mesh(
                    directory,
                    length_m=1.0,
                    height_m=0.5,
                    mesh_size_m=0.025,
                    timeout=10,
                )

            self.assertTrue(
                gmsh.model.occ.addRectangle.called or gmsh.model.geo.addPoint.called
            )
            self.assertTrue(
                gmsh.model.occ.synchronize.called
                or gmsh.model.geo.synchronize.called
            )
            gmsh.model.mesh.generate.assert_called_once_with(2)
            gmsh.option.setNumber.assert_any_call("Mesh.RecombineAll", 0)
            self.assertEqual(gmsh.write.call_count, 2)
            written_names = {
                Path(call.args[0]).name for call in gmsh.write.call_args_list
            }
            self.assertEqual(written_names, {"smoke.msh", "smoke.su2"})

    def test_farfield_and_fluid_physical_groups_are_named(self):
        gmsh = make_fake_gmsh()
        bridge, _, _ = make_bridge()
        with connection_temporary_directory("gmsh_groups_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                bridge.build_smoke_mesh(directory)

        group_calls = gmsh.model.addPhysicalGroup.call_args_list
        boundary_call = next(call for call in group_calls if call.args[0] == 1)
        fluid_call = next(call for call in group_calls if call.args[0] == 2)
        self.assertCountEqual(boundary_call.args[1], [11, 12, 13, 14])
        self.assertEqual(list(fluid_call.args[1]), [17])

        name_calls = {
            (call.args[0], call.args[2])
            for call in gmsh.model.setPhysicalName.call_args_list
        }
        self.assertIn((1, "farfield"), name_calls)
        self.assertIn((2, "fluid"), name_calls)

    def test_outputs_manifest_and_hashes_are_complete(self):
        gmsh = make_fake_gmsh()
        bridge, _, _ = make_bridge()
        with connection_temporary_directory("gmsh_outputs_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                bridge.build_smoke_mesh(directory)

            expected = {
                "smoke.msh",
                "smoke.su2",
                "gmsh_manifest.json",
                "gmsh.log",
            }
            self.assertTrue(expected.issubset({path.name for path in directory.iterdir()}))
            for name in expected:
                self.assertTrue((directory / name).is_file(), name)
                self.assertGreater((directory / name).stat().st_size, 0, name)

            manifest = json.loads(
                (directory / "gmsh_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "PASS")
            self.assertEqual(manifest["gmsh_version"], "4.13.1")
            self.assertEqual(
                Path(manifest["python_module_path"]), Path(gmsh.__file__).resolve()
            )
            self.assertEqual(manifest["gmsh_cli_version"], "4.13.1")
            self.assertTrue(Path(manifest["gmsh_cli_path"]).is_absolute())
            self.assertEqual(
                manifest["gmsh_cli_command"]["command"],
                [manifest["gmsh_cli_path"], "--version"],
            )
            self.assertEqual(manifest["inputs"]["length_m"], 1.0)
            self.assertEqual(manifest["inputs"]["height_m"], 0.5)
            self.assertGreater(manifest["node_count"], 0)
            self.assertGreaterEqual(manifest["element_count"], 1000)
            self.assertLessEqual(manifest["element_count"], 5000)
            self.assertTrue({"farfield", "fluid"}.issubset(marker_names(manifest["markers"])))

            hashes = manifest["output_sha256"]
            for name in ("smoke.msh", "smoke.su2"):
                expected_hash = hashlib.sha256((directory / name).read_bytes()).hexdigest()
                self.assertEqual(hashes[name], expected_hash)

    def test_generate_or_write_exception_still_finalizes(self):
        cases = (
            (make_fake_gmsh(generate_error=RuntimeError("generate failed")), "generate failed"),
            (make_fake_gmsh(write_failure_at=2), "write failure 2"),
        )
        for index, (gmsh, message) in enumerate(cases):
            with self.subTest(message=message):
                bridge, _, _ = make_bridge()
                with connection_temporary_directory(
                    f"gmsh_finalize_error_{index}_"
                ) as directory:
                    with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                        with self.assertRaisesRegex(RuntimeError, message):
                            bridge.build_smoke_mesh(directory)
                gmsh.finalize.assert_called_once_with()

    def test_initialize_failure_logs_full_traceback_and_module_path(self):
        gmsh = make_fake_gmsh(
            initialize_error=RuntimeError("initialization sentinel")
        )
        bridge, _, _ = make_bridge()
        with connection_temporary_directory("gmsh_init_failure_") as directory:
            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                with self.assertRaisesRegex(RuntimeError, "initialization sentinel"):
                    bridge.build_smoke_mesh(directory)

            log_path = directory / "gmsh.log"
            self.assertTrue(log_path.is_file())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("Traceback (most recent call last)", log_text)
            self.assertIn("RuntimeError: initialization sentinel", log_text)
            self.assertIn(str(Path(gmsh.__file__).resolve()), log_text)


class GmshStepEntryTests(unittest.TestCase):
    def test_step_entry_rejects_ambiguous_external_markers(self):
        ambiguous_mappings = (
            {
                "wall": {"dimension": 2, "entity_tags": [31]},
                " wall ": {"dimension": 2, "entity_tags": [32]},
            },
            {
                "wall": {"dimension": 2, "entity_tags": [31]},
                "other": {"dimension": 2, "entity_tags": [31]},
            },
        )

        with connection_temporary_directory("gmsh_step_ambiguous_") as directory:
            step_path = directory / "input.step"
            step_path.write_text("mock STEP fixture\n", encoding="utf-8")
            for marker_mapping in ambiguous_mappings:
                with self.subTest(marker_mapping=marker_mapping):
                    with self.assertRaises(ValueError):
                        gmsh_bridge.build_step_mesh(
                            step_path,
                            directory / "output",
                            marker_mapping,
                            {},
                        )

    def test_step_entry_sets_metres_before_import_and_uses_external_markers(self):
        self.assertTrue(callable(getattr(gmsh_bridge.GmshBridge, "build_step_mesh")))
        gmsh = make_fake_gmsh()
        marker_mapping = {
            "outer_boundary": {
                "dimension": 2,
                "entity_tags": [31, 32],
            },
            "fluid_volume": {
                "dimension": 3,
                "entity_tags": [101],
            },
        }
        mesh_options = {
            "Mesh.CharacteristicLengthMin": 0.05,
            "Mesh.CharacteristicLengthMax": 0.10,
        }

        with connection_temporary_directory("gmsh_step_interface_") as directory:
            step_path = directory / "input.step"
            step_path.write_text("mock STEP fixture; not production geometry\n", encoding="utf-8")
            step_before = step_path.read_bytes()
            output_directory = directory / "output"

            with mock.patch.dict(sys.modules, {"gmsh": gmsh}):
                gmsh_bridge.build_step_mesh(
                    step_path,
                    output_directory,
                    marker_mapping,
                    mesh_options,
                )

            self.assertEqual(step_path.read_bytes(), step_before)
            self.assertTrue((output_directory / "mesh.msh").is_file())
            self.assertTrue((output_directory / "mesh.su2").is_file())

        gmsh.option.setString.assert_any_call("Geometry.OCCTargetUnit", "M")
        gmsh.model.occ.importShapes.assert_called_once()
        imported_path = Path(gmsh.model.occ.importShapes.call_args.args[0]).resolve()
        self.assertEqual(imported_path, step_path.resolve())
        gmsh.model.occ.synchronize.assert_called_once_with()
        gmsh.model.mesh.generate.assert_called_once_with(3)
        gmsh.initialize.assert_called_once_with()
        gmsh.finalize.assert_called_once_with()
        gmsh.option.setNumber.assert_any_call(
            "Mesh.CharacteristicLengthMin", 0.05
        )
        gmsh.option.setNumber.assert_any_call(
            "Mesh.CharacteristicLengthMax", 0.10
        )

        event_names = [event[0] for event in gmsh.timeline]
        self.assertLess(event_names.index("setString"), event_names.index("importShapes"))
        self.assertLess(
            event_names.index("importShapes"), event_names.index("occ.synchronize")
        )

        physical_calls = gmsh.model.addPhysicalGroup.call_args_list
        self.assertTrue(
            any(call.args[0] == 2 and list(call.args[1]) == [31, 32] for call in physical_calls)
        )
        self.assertTrue(
            any(call.args[0] == 3 and list(call.args[1]) == [101] for call in physical_calls)
        )
        physical_names = {
            call.args[2] for call in gmsh.model.setPhysicalName.call_args_list
        }
        self.assertEqual(physical_names, set(marker_mapping))
        gmsh.fltk.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
