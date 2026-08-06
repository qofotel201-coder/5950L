from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RearOutletMeshReclassificationTests(unittest.TestCase):
    def test_planar_face_is_retained_and_conical_face_moves(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "input.su2"
            output = root / "output.su2"
            report = root / "report.json"
            source.write_text(
                """NDIME= 3
NELEM= 1
10 0 1 2 3 0
NPOIN= 7
0 0 0 0
1 0 0 1
1 1 0 2
1 0 1 3
0.5 1 0 4
0.5 0 1 5
0.5 1 1 6
NMARK= 4
MARKER_TAG= front_conical_surface
MARKER_ELEMS= 1
5 0 2 1
MARKER_TAG= rear_outlet_1
MARKER_ELEMS= 2
5 1 2 3
5 1 4 5
MARKER_TAG= rear_outlet_2
MARKER_ELEMS= 1
5 0 3 2
MARKER_TAG= wall
MARKER_ELEMS= 1
5 1 2 4
""",
                encoding="utf-8",
            )
            expected = root / "expected.su2"
            expected.write_text(source.read_text(encoding="utf-8").replace(
                "MARKER_TAG= front_conical_surface\nMARKER_ELEMS= 1\n5 0 2 1\nMARKER_TAG= rear_outlet_1\nMARKER_ELEMS= 2\n5 1 2 3\n5 1 4 5",
                "MARKER_TAG= front_conical_surface\nMARKER_ELEMS= 2\n5 0 2 1\n5 1 4 5\nMARKER_TAG= rear_outlet_1\nMARKER_ELEMS= 1\n5 1 2 3",
            ), encoding="utf-8")
            contract = root / "contract.toml"
            contract.write_text(f'''schema = "cfdpipe.rear_outlet_marker_correction.v1"\nstatus = "APPROVED"\n[source_mesh]\nsha256 = "{hashlib.sha256(source.read_bytes()).hexdigest()}"\n[output_mesh]\nsha256 = "{hashlib.sha256(expected.read_bytes()).hexdigest()}"\n[classification]\noutlet_marker = "rear_outlet_1"\nfarfield_marker = "front_conical_surface"\nplane_x_m = 1.0\ntolerance_m = 2e-7\noriginal_outlet_faces = 2\nplanar_outlet_faces = 1\nmoved_to_farfield_faces = 1\n[policy]\nsource_geometry_unchanged = true\nboundary_conditions_unchanged = true\nvolume_topology_unchanged = true\nruntime_gmsh_tags_forbidden = true\nold_solution_is_diagnostic_only = true\nindependent_first_order_restart_required = true\n''', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "scripts/repro/reclassify_rear_outlet_mesh.py", "--input", str(source), "--output", str(output), "--report", str(report), "--contract", str(contract)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["counts"]["planar_outlet_faces"], 1)
            self.assertEqual(data["counts"]["moved_to_farfield_faces"], 1)
            text = output.read_text(encoding="utf-8")
            self.assertIn("MARKER_TAG= front_conical_surface\nMARKER_ELEMS= 2", text)
            self.assertIn("MARKER_TAG= rear_outlet_1\nMARKER_ELEMS= 1\n5 1 2 3", text)


if __name__ == "__main__":
    unittest.main()
