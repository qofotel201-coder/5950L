import unittest
from pathlib import Path

from cfdpipe.coarse_projection_evidence import _is_sha256, _matching_path


class EmbeddedProjectionPathTests(unittest.TestCase):
    def test_windows_config_path_matches_linux_repository_path(self):
        self.assertTrue(
            _matching_path(
                r"C:\Users\operator\5950\config\coarse_mesh.toml",
                Path("/root/autodl-tmp/5950L/config/coarse_mesh.toml"),
            )
        )

    def test_windows_run_path_matches_linux_repository_path(self):
        self.assertTrue(
            _matching_path(
                r"C:\Users\operator\5950\runs\mesh\coarse\projection_001",
                Path("/root/autodl-tmp/5950L/runs/mesh/coarse/projection_001"),
            )
        )

    def test_repository_relative_suffix_must_match_exactly(self):
        self.assertFalse(
            _matching_path(
                r"C:\Users\operator\5950\runs\mesh\coarse\projection_002",
                Path("/root/autodl-tmp/5950L/runs/mesh/coarse/projection_001"),
            )
        )

    def test_legacy_contract_identity_must_remain_a_sha256(self):
        self.assertTrue(_is_sha256("a" * 64))
        self.assertFalse(_is_sha256("legacy"))


if __name__ == "__main__":
    unittest.main()
