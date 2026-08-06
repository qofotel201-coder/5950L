from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.repro.finalize_first_order_acceptance import valid_consecutive_counts


ROOT = Path(__file__).resolve().parents[1]


BASE_CONFIG = """\
SOLVER= RANS
KIND_TURB_MODEL= SST
SST_OPTIONS= V2003m
CONV_NUM_METHOD_FLOW= ROE
MARKER_SUPERSONIC_OUTLET= ( rear_outlet_1, rear_outlet_2 )
MUSCL_FLOW= NO
MUSCL_TURB= NO
MESH_FILENAME= old_mesh.su2
RESTART_SOL= YES
SOLUTION_FILENAME= old_restart
RESTART_FILENAME= old_output
VOLUME_FILENAME= old_solution
SURFACE_FILENAME= old_surface
CFL_NUMBER= 0.5
ITER= 500
CONV_STARTITER= 501
RAMP_MUSCL= NO
OUTPUT_FILES= RESTART, PARAVIEW, SURFACE_PARAVIEW
"""


class FormalRANSCheckpointToolTests(unittest.TestCase):
    def test_finalizer_accepts_later_adjacent_passing_windows(self) -> None:
        self.assertTrue(valid_consecutive_counts(1, 2))
        self.assertTrue(valid_consecutive_counts(2, 3))
        self.assertFalse(valid_consecutive_counts(0, 1))
        self.assertFalse(valid_consecutive_counts(2, 4))

    def test_first_order_renderer_creates_independent_medium_initial_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cfg"
            mesh = root / "medium mesh.su2"
            output = root / "initial.cfg"
            base.write_text(BASE_CONFIG, encoding="utf-8")
            mesh.write_text("mesh", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/repro/render_first_order_checkpoint_config.py"),
                    "--base",
                    str(base),
                    "--mesh",
                    str(mesh),
                    "--output",
                    str(output),
                    "--cfl",
                    "0.1",
                ],
                check=True,
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertIn(f"MESH_FILENAME= {mesh.resolve()}", rendered)
            self.assertIn("RESTART_SOL= NO", rendered)
            self.assertIn("CFL_NUMBER= 0.1", rendered)
            self.assertIn("OUTPUT_FILES= RESTART\n", rendered)
            self.assertEqual(rendered.count("MESH_FILENAME="), 1)
            self.assertEqual(rendered.count("RESTART_SOL="), 1)
            self.assertNotIn("old_mesh.su2", rendered)

    def test_first_order_renderer_requires_explicit_restart_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cfg"
            mesh = root / "mesh.su2"
            output = root / "continued.cfg"
            base.write_text(BASE_CONFIG, encoding="utf-8")
            mesh.write_text("mesh", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/repro/render_first_order_checkpoint_config.py"),
                    "--base",
                    str(base),
                    "--mesh",
                    str(mesh),
                    "--output",
                    str(output),
                    "--restart",
                ],
                check=True,
            )
            self.assertIn("RESTART_SOL= YES", output.read_text(encoding="utf-8"))

    def test_first_order_full_output_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cfg"
            mesh = root / "mesh.su2"
            output = root / "diagnostic.cfg"
            base.write_text(BASE_CONFIG, encoding="utf-8")
            mesh.write_text("mesh", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/repro/render_first_order_checkpoint_config.py"),
                    "--base",
                    str(base),
                    "--mesh",
                    str(mesh),
                    "--output",
                    str(output),
                    "--full-output",
                ],
                check=True,
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertIn(
                "OUTPUT_FILES= RESTART, PARAVIEW, SURFACE_PARAVIEW", rendered
            )
            self.assertEqual(rendered.count("OUTPUT_FILES="), 1)

    def test_renderer_rejects_changed_sst_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.cfg"
            mesh = root / "mesh.su2"
            base.write_text(BASE_CONFIG.replace("V2003m", "V1994"), encoding="utf-8")
            mesh.write_text("mesh", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/repro/render_first_order_checkpoint_config.py"),
                    "--base",
                    str(base),
                    "--mesh",
                    str(mesh),
                    "--output",
                    str(root / "output.cfg"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("frozen first-order contract mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
