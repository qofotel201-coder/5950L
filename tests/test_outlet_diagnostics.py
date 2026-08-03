from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cfdpipe.outlet_diagnostics import (
    OutletDiagnosticError,
    analyze_outlets,
    map_mesh_points_to_solution,
    parse_topology_smoke_su2,
    split_linear_triangle,
)


class OutletDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _mesh(self) -> Path:
        path = self.root / "mesh.su2"
        path.write_text(
            """NDIME= 3
NELEM= 1
10 0 1 2 3 0
NPOIN= 4
0 0 0 0
1 0 0 1
0 1 0 2
0 0 1 3
NMARK= 4
MARKER_TAG= farfield
MARKER_ELEMS= 1
5 0 2 1
MARKER_TAG= outlet_1
MARKER_ELEMS= 1
5 0 1 3
MARKER_TAG= outlet_2
MARKER_ELEMS= 1
5 0 3 2
MARKER_TAG= wall
MARKER_ELEMS= 1
5 1 2 3
""",
            encoding="utf-8",
        )
        return path

    def _hybrid_mesh(self) -> Path:
        path = self.root / "hybrid.su2"
        path.write_text(
            """NDIME= 3
NELEM= 5
13 0 2 1 4 6 5 0
13 0 1 3 4 5 7 1
13 0 3 2 4 7 6 2
13 1 2 3 5 6 7 3
10 4 5 6 7 4
NPOIN= 8
0 0 0 0
1 0 0 1
0 1 0 2
0 0 1 3
0.125 0.125 0.125 4
0.625 0.125 0.125 5
0.125 0.625 0.125 6
0.125 0.125 0.625 7
NMARK= 4
MARKER_TAG= farfield
MARKER_ELEMS= 1
5 0 2 1
MARKER_TAG= outlet_1
MARKER_ELEMS= 1
5 0 1 3
MARKER_TAG= outlet_2
MARKER_ELEMS= 1
5 0 3 2
MARKER_TAG= wall
MARKER_ELEMS= 1
5 1 2 3
""",
            encoding="utf-8",
        )
        return path

    def _prism_with_exterior_quads(self) -> Path:
        path = self.root / "exterior-quad.su2"
        path.write_text(
            """NDIME= 3
NELEM= 1
13 0 1 2 3 4 5 0
NPOIN= 6
0 0 0 0
1 0 0 1
0 1 0 2
0 0 1 3
1 0 1 4
0 1 1 5
NMARK= 2
MARKER_TAG= bottom
MARKER_ELEMS= 1
5 0 1 2
MARKER_TAG= top
MARKER_ELEMS= 1
5 3 5 4
""",
            encoding="utf-8",
        )
        return path

    def test_parser_and_uniform_closed_domain_flux(self) -> None:
        mesh = parse_topology_smoke_su2(self._mesh())
        points = [mesh.points[index] for index in range(4)]
        density = [2.0] * 4
        velocity = [(1.0, 2.0, 3.0)] * 4
        momentum = [(2.0, 4.0, 6.0)] * 4
        mach = [4.0] * 4

        payload = analyze_outlets(
            mesh,
            solution_points=points,
            density=density,
            momentum=momentum,
            velocity=velocity,
            mach=mach,
            outlet_markers=("outlet_1", "outlet_2"),
            farfield_marker="farfield",
            wall_marker="wall",
        )

        self.assertEqual(payload["computation_status"], "PASS")
        self.assertTrue(payload["topology"]["coverage_complete"])
        self.assertAlmostEqual(
            payload["mass_balance"]["global_signed_mass_flow_kg_s"], 0.0, places=12
        )
        self.assertEqual(
            payload["coordinate_mapping"]["mode"],
            "identity_verified_by_coordinates",
        )
        self.assertEqual(sum(item["face_count"] for item in payload["markers"].values()), 4)

    def test_no_slip_wall_zero_velocity_does_not_require_wall_normal_mach(self) -> None:
        mesh = parse_topology_smoke_su2(self._mesh())
        points = [mesh.points[index] for index in range(4)]
        velocity = [(1.0, 0.0, 0.0)] + [(0.0, 0.0, 0.0)] * 3
        density = [1.0] * 4
        payload = analyze_outlets(
            mesh,
            solution_points=points,
            density=density,
            momentum=velocity,
            velocity=velocity,
            mach=[2.0, 0.0, 0.0, 0.0],
            outlet_markers=("outlet_1", "outlet_2"),
            farfield_marker="farfield",
            wall_marker="wall",
        )

        wall = payload["markers"]["wall"]
        self.assertFalse(wall["normal_mach_evaluated"])
        self.assertIsNone(wall["normal_mach_vertex_min"])
        self.assertIsNone(wall["backflow_area_fraction"])
        self.assertAlmostEqual(payload["mass_balance"]["wall_signed_mass_flow_kg_s"], 0.0)

    def test_linear_split_analytically_closes(self) -> None:
        points = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        split = split_linear_triangle(points, (-1.0, 1.0, 1.0))

        self.assertAlmostEqual(split["below_area"] + split["above_area"], 0.5)
        self.assertAlmostEqual(
            split["below_integral"] + split["above_integral"], 1.0 / 6.0
        )
        self.assertLess(split["below_integral"], 0.0)
        self.assertGreater(split["above_integral"], 0.0)

    def test_parser_and_flux_support_closed_tet_prism_hybrid(self) -> None:
        mesh = parse_topology_smoke_su2(self._hybrid_mesh())
        self.assertEqual(len(mesh.tetrahedra), 1)
        self.assertEqual(len(mesh.prisms), 4)

        points = [mesh.points[index] for index in range(8)]
        payload = analyze_outlets(
            mesh,
            solution_points=points,
            density=[2.0] * 8,
            momentum=[(2.0, 4.0, 6.0)] * 8,
            velocity=[(1.0, 2.0, 3.0)] * 8,
            mach=[4.0] * 8,
            outlet_markers=("outlet_1", "outlet_2"),
            farfield_marker="farfield",
            wall_marker="wall",
        )

        self.assertEqual(payload["computation_status"], "PASS")
        self.assertEqual(payload["mesh"]["tetrahedron_count"], 1)
        self.assertEqual(payload["mesh"]["prism_count"], 4)
        self.assertEqual(payload["mesh"]["volume_element_count"], 5)
        self.assertEqual(payload["topology"]["exterior_face_count"], 4)
        self.assertEqual(
            payload["topology"]["exterior_quadrilateral_face_count"], 0
        )
        self.assertAlmostEqual(
            payload["mass_balance"]["global_signed_mass_flow_kg_s"],
            0.0,
            places=12,
        )

    def test_parser_rejects_unmarked_exterior_prism_quadrilateral(self) -> None:
        with self.assertRaisesRegex(
            OutletDiagnosticError, "exterior quadrilateral"
        ):
            parse_topology_smoke_su2(self._prism_with_exterior_quads())

    def test_coordinate_mapping_supports_verified_reordering(self) -> None:
        mesh_points = {0: (0.0, 0.0, 0.0), 1: (1.0, 0.0, 0.0)}
        mapping, evidence = map_mesh_points_to_solution(
            mesh_points, [(1.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
        )

        self.assertEqual(mapping, {0: 1, 1: 0})
        self.assertEqual(evidence["mode"], "unique_coordinate_match")

    def test_incomplete_marker_coverage_fails(self) -> None:
        mesh = parse_topology_smoke_su2(self._mesh())
        with self.assertRaisesRegex(OutletDiagnosticError, "marker set mismatch"):
            analyze_outlets(
                mesh,
                solution_points=[mesh.points[index] for index in range(4)],
                density=[1.0] * 4,
                momentum=[(1.0, 0.0, 0.0)] * 4,
                velocity=[(1.0, 0.0, 0.0)] * 4,
                mach=[1.0] * 4,
                outlet_markers=("outlet_1", "outlet_2"),
                farfield_marker="missing_farfield",
                wall_marker="wall",
            )


if __name__ == "__main__":
    unittest.main()
