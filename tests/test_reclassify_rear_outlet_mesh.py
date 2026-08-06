from __future__ import annotations

import json
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
            result = subprocess.run(
                [sys.executable, "scripts/repro/reclassify_rear_outlet_mesh.py", "--input", str(source), "--output", str(output), "--report", str(report), "--plane-x", "1.0"],
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
