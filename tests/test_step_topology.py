"""Standard-library tests for the read-only STEP topology parser."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from cfdpipe.step_topology import (
    StepTopologyError,
    decode_step_string,
    map_step_solids_to_gmsh_volumes,
    parse_step_records,
    parse_step_topology,
)


def _advanced_faces(face_ids: list[int]) -> str:
    return "\n".join(
        f"#{face_id} = ADVANCED_FACE('', (), #900, .T.);" for face_id in face_ids
    )


def _multiline_fixture() -> str:
    small_faces = list(range(100, 106))
    large_outer_faces = list(range(200, 250))
    large_void_faces = list(range(300, 303))
    return "\n".join(
        (
            "ISO-10303-21;",
            "HEADER;",
            "ENDSEC;",
            "DATA;",
            "#1 = MANIFOLD_SOLID_BREP(",
            "  '\\X2\\65CB8F6C\\X0\\2',",
            "  #10",
            ");",
            "#10 = CLOSED_SHELL(",
            "  'small outer',",
            "  (" + ", ".join(f"#{item}" for item in small_faces) + ")",
            ");",
            "#2 = BREP_WITH_VOIDS(",
            "  'large solid',",
            "  #20,",
            "  (#21)",
            ");",
            "#20 = CLOSED_SHELL(",
            "  'large outer',",
            "  (" + ", ".join(f"#{item}" for item in large_outer_faces) + ")",
            ");",
            "#21 = ORIENTED_CLOSED_SHELL('', *, #22, .F.);",
            "#22 = CLOSED_SHELL(",
            "  'void shell',",
            "  (" + ", ".join(f"#{item}" for item in large_void_faces) + ")",
            ");",
            _advanced_faces(small_faces),
            _advanced_faces(large_outer_faces),
            _advanced_faces(large_void_faces),
            "#900 = PLANE('fixture plane');",
            "ENDSEC;",
            "END-ISO-10303-21;",
        )
    )


class StepTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def _write_fixture(self, text: str, name: str = "fixture.step") -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_multiline_real_syntax_and_retains_hash(self) -> None:
        path = self._write_fixture(_multiline_fixture())
        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()

        topology = parse_step_topology(path)

        self.assertEqual(topology["source_sha256"], expected_hash)
        self.assertEqual(topology["solid_count"], 2)
        solids = {solid["id"]: solid for solid in topology["solids"]}
        self.assertEqual(solids[1]["type"], "MANIFOLD_SOLID_BREP")
        self.assertEqual(solids[1]["name"], "旋转2")
        self.assertEqual(solids[1]["shell_ids"], [10])
        self.assertEqual(solids[1]["root_shell_ids"], [10])
        self.assertEqual(
            [(item["role_in_solid"], item["face_count"]) for item in solids[1]["shell_components"]],
            [("boundary_shell", 6)],
        )
        self.assertEqual(solids[1]["advanced_face_ids"], list(range(100, 106)))
        self.assertEqual(solids[1]["face_count"], 6)
        self.assertEqual(solids[2]["type"], "BREP_WITH_VOIDS")
        self.assertEqual(solids[2]["shell_ids"], [20, 21, 22])
        self.assertEqual(solids[2]["root_shell_ids"], [20, 21])
        self.assertEqual(
            [(item["role_in_solid"], item["face_count"]) for item in solids[2]["shell_components"]],
            [("outer_shell", 50), ("void_shell", 3)],
        )
        self.assertEqual(solids[2]["face_count"], 53)

        records = parse_step_records(_multiline_fixture())
        self.assertEqual(records[1].references, (10,))
        self.assertEqual(records[2].references, (20, 21))
        self.assertEqual(records[21].references, (22,))

    def test_decodes_x2_and_step_quote_escape(self) -> None:
        self.assertEqual(decode_step_string(r"'\X2\65CB8F6C\X0\2'"), "旋转2")
        self.assertEqual(decode_step_string("'pilot''s solid'"), "pilot's solid")
        with self.assertRaises(StepTopologyError):
            decode_step_string("\\X2\\65CB8F\\X0\\")

    def test_unique_six_and_fifty_three_face_mapping(self) -> None:
        topology = parse_step_topology(self._write_fixture(_multiline_fixture()))

        result = map_step_solids_to_gmsh_volumes(
            topology,
            [
                {"entity_tag": 502, "boundary_surfaces": list(range(1, 54))},
                {"entity_tag": 501, "boundary_surfaces": list(range(101, 107))},
            ],
        )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(
            result["matches"],
            [
                {"solid_id": 1, "volume_entity_tag": 501, "face_count": 6},
                {"solid_id": 2, "volume_entity_tag": 502, "face_count": 53},
            ],
        )
        self.assertTrue(
            all(item["status"] == "MATCHED" for item in result["solid_results"])
        )

    def test_duplicate_face_count_is_ambiguous_and_never_paired(self) -> None:
        topology = {
            "source_sha256": "a" * 64,
            "solids": [
                {"id": 1, "type": "MANIFOLD_SOLID_BREP", "name": "a", "face_count": 6},
                {"id": 2, "type": "MANIFOLD_SOLID_BREP", "name": "b", "face_count": 6},
            ],
        }

        result = map_step_solids_to_gmsh_volumes(
            topology,
            [
                {"entity_tag": 11, "boundary_surfaces": list(range(1, 7))},
                {"entity_tag": 12, "boundary_surfaces": list(range(11, 17))},
            ],
        )

        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(result["matches"], [])
        self.assertTrue(
            all(item["status"] == "AMBIGUOUS" for item in result["solid_results"])
        )
        self.assertTrue(
            all(item["volume_entity_tag"] is None for item in result["solid_results"])
        )

    def test_missing_reference_fails(self) -> None:
        path = self._write_fixture(
            """ISO-10303-21;
DATA;
#1 = MANIFOLD_SOLID_BREP(
  'broken',
  #999
);
ENDSEC;
END-ISO-10303-21;
"""
        )

        with self.assertRaisesRegex(StepTopologyError, "missing STEP entity #999"):
            parse_step_topology(path)

    def test_shell_cycle_fails(self) -> None:
        path = self._write_fixture(
            """ISO-10303-21;
DATA;
#1 = MANIFOLD_SOLID_BREP('cycle', #10);
#10 = ORIENTED_CLOSED_SHELL('', *, #11, .F.);
#11 = ORIENTED_CLOSED_SHELL('', *, #10, .F.);
ENDSEC;
END-ISO-10303-21;
"""
        )

        with self.assertRaisesRegex(StepTopologyError, "cycle"):
            parse_step_topology(path)


if __name__ == "__main__":
    unittest.main()
