"""Run the standard-library test subset that needs no private CAD or run bundle."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAD_FREE_TEST_MODULES = (
    "tests.test_atmosphere",
    "tests.test_process",
    "tests.test_toolchain",
    "tests.test_external_process_policy",
    "tests.test_gmsh_bridge",
    "tests.test_su2_bridge",
    "tests.test_paraview_bridge",
    "tests.test_connect_pipeline",
    "tests.test_resource_gate",
    "tests.test_worker_memory",
    "tests.test_boundary_layer_design",
    "tests.test_rans_convergence",
    "tests.test_mesh_audit",
    "tests.test_iges_metadata",
    "tests.test_reproducibility",
)


def main() -> int:
    """Load the explicit CAD-free suite and return a conventional status code."""

    sys.path.insert(0, str(REPOSITORY_ROOT))
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    suite = unittest.defaultTestLoader.loadTestsFromNames(CAD_FREE_TEST_MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
