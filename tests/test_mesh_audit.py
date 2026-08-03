"""Tests for the bounded-memory SU2 coarse-mesh text audit."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest

from cfdpipe.mesh_audit import audit_su2_mesh


def _valid_mesh() -> str:
    return """% compact mixed three-dimensional mesh
NDIME= 3
NELEM= 3
10 0 1 2 3 0
13 0 1 2 4 5 6 1
14 0 1 5 4 7 2
NPOIN= 8
0.0 0.0 0.0 0
1.0 0.0 0.0 1
0.0 1.0 0.0 2
0.0 0.0 1.0 3
1.0 1.0 0.0 4
1.0 0.0 1.0 5
0.0 1.0 1.0 6
1.0 1.0 1.0 7
NMARK= 2
MARKER_TAG= farfield
MARKER_ELEMS= 1
5 0 1 2
MARKER_TAG= vehicle_internal_and_external_walls
MARKER_ELEMS= 2
5 0 1 3
9 0 1 5 4
"""


class SU2MeshAuditTests(unittest.TestCase):
    def _audit(
        self,
        text: str,
        *,
        target_element_range: tuple[int, int] = (3, 3),
        expected_markers: tuple[str, ...] = (
            "farfield",
            "vehicle_internal_and_external_walls",
        ),
    ) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "mesh.su2"
            path.write_text(text, encoding="utf-8", newline="\n")
            return audit_su2_mesh(
                path,
                target_element_range=target_element_range,
                expected_markers=expected_markers,
            )

    def test_valid_mixed_mesh_returns_json_serializable_pass_evidence(self) -> None:
        text = _valid_mesh()
        result = self._audit(text)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["schema"], "cfdpipe.su2_mesh_audit.v1")
        self.assertEqual(result["ndime"], 3)
        self.assertEqual(result["nelem"], 3)
        self.assertEqual(result["npoin"], 8)
        self.assertEqual(result["nmark"], 2)
        self.assertEqual(
            result["volume_element_types"],
            {"Prism 6": 1, "Pyramid 5": 1, "Tetrahedron 4": 1},
        )
        self.assertEqual(
            result["marker_element_counts"],
            {"farfield": 1, "vehicle_internal_and_external_walls": 2},
        )
        self.assertEqual(
            result["marker_element_types"],
            {
                "farfield": {"Triangle 3": 1},
                "vehicle_internal_and_external_walls": {
                    "Quadrilateral 4": 1,
                    "Triangle 3": 1,
                },
            },
        )
        self.assertEqual(
            result["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        self.assertIsNone(result["error"])
        json.dumps(result, allow_nan=False)

    def test_target_range_and_exact_nonempty_marker_contract_fail_closed(self) -> None:
        cases = (
            (
                {"target_element_range": (4, 10)},
                "outside target element range",
            ),
            (
                {"expected_markers": ("farfield",)},
                "markers do not exactly match",
            ),
            (
                {},
                "marker element count must be positive",
            ),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                text = _valid_mesh()
                if message.startswith("marker element"):
                    text = text.replace("MARKER_ELEMS= 1", "MARKER_ELEMS= 0", 1)
                result = self._audit(text, **overrides)
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(message, result["error"]["message"])
                json.dumps(result, allow_nan=False)

    def test_rejects_empty_or_nonfinite_file(self) -> None:
        for text, message in (
            ("", "empty"),
            (_valid_mesh().replace("0.0 0.0 0.0 0", "NaN 0.0 0.0 0"), "NaN or Inf"),
            (_valid_mesh() + "% infinity\n", "NaN or Inf"),
        ):
            with self.subTest(message=message):
                result = self._audit(text)
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(message, result["error"]["message"])

    def test_rejects_duplicate_or_missing_key_counts(self) -> None:
        duplicate = _valid_mesh().replace("NELEM= 3", "NELEM= 3\nNDIME= 3")
        missing = _valid_mesh().split("NMARK= 2", 1)[0]
        for text, message in (
            (duplicate, "duplicate NDIME"),
            (missing, "missing NMARK"),
        ):
            with self.subTest(message=message):
                result = self._audit(text)
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(message, result["error"]["message"])

    def test_rejects_unsupported_volume_type(self) -> None:
        text = _valid_mesh().replace("10 0 1 2 3 0", "12 0 1 2 3 4 5 6 7 0")
        result = self._audit(text)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("unsupported volume element type 12", result["error"]["message"])

    def test_rejects_out_of_range_volume_and_marker_node_indices(self) -> None:
        cases = (
            (_valid_mesh().replace("10 0 1 2 3 0", "10 0 1 2 8 0"), "volume node index"),
            (_valid_mesh().replace("5 0 1 2\nMARKER_TAG", "5 0 1 8\nMARKER_TAG"), "marker node index"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                result = self._audit(text)
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(message, result["error"]["message"])

    def test_rejects_noncanonical_point_indices_without_storing_point_set(self) -> None:
        text = _valid_mesh().replace("1.0 1.0 1.0 7", "1.0 1.0 1.0 99")
        result = self._audit(text)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("point index", result["error"]["message"])

    def test_parser_source_uses_stream_iteration_not_whole_file_helpers(self) -> None:
        source = inspect.getsource(
            __import__("cfdpipe.mesh_audit", fromlist=["mesh_audit"])
        )
        self.assertNotIn(".read_text(", source)
        self.assertNotIn(".splitlines(", source)
        self.assertNotIn("volume_connectivity", source)


if __name__ == "__main__":
    unittest.main()
