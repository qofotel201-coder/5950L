"""Standard-library tests for strict, read-only IGES metadata inspection."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from cfdpipe.iges_metadata import IgesMetadataError, inspect_iges_metadata


def _record(data: bytes, section: str, sequence: int) -> bytes:
    if len(data) > 72:
        raise AssertionError("test record payload exceeds 72 columns")
    record = data.ljust(72) + section.encode("ascii") + f"{sequence:7d}".encode()
    if len(record) != 80:
        raise AssertionError("test record is not exactly 80 columns")
    return record


def _field(value: int | str) -> bytes:
    if isinstance(value, int):
        encoded = f"{value:8d}".encode("ascii")
    else:
        encoded = value.encode("ascii").ljust(8)
    if len(encoded) != 8:
        raise AssertionError(f"invalid test directory field: {value!r}")
    return encoded


def _hollerith(value: str) -> bytes:
    encoded = value.encode("ascii")
    return str(len(encoded)).encode("ascii") + b"H" + encoded


def _global_stream() -> bytes:
    parameters = [
        b"1H,",
        b"1H;",
        _hollerith("Widget"),
        _hollerith("source.sldprt"),
        _hollerith("NativeCAD"),
        _hollerith("Exporter 1"),
        b"32",
        b"38",
        b"6",
        b"308",
        b"15",
        _hollerith("Widget receiver"),
        b"1.0",
        b"2",
        _hollerith("MM"),
        b"8",
        b"1.0",
        _hollerith("20260801.120000"),
        b"1E-06",
        b"1000.0",
        _hollerith("Auditor"),
        _hollerith("Test Org"),
        b"11",
        b"0",
        _hollerith("20260801.120500"),
    ]
    return b",".join(parameters) + b";"


def _directory_pair(
    *,
    entity_type: int,
    parameter_pointer: int,
    parameter_line_count: int,
    form: int,
    level: int,
    color: int,
    label: str,
    directory_sequence: int,
) -> tuple[bytes, bytes]:
    first = b"".join(
        (
            _field(entity_type),
            _field(parameter_pointer),
            _field(0),
            _field(0),
            _field(level),
            _field(0),
            _field(0),
            _field(0),
            _field("00000000"),
        )
    )
    second = b"".join(
        (
            _field(entity_type),
            _field(0),
            _field(color),
            _field(parameter_line_count),
            _field(form),
            _field(0),
            _field(0),
            _field(label),
            _field(0),
        )
    )
    return (
        _record(first, "D", directory_sequence),
        _record(second, "D", directory_sequence + 1),
    )


def _parameter_record(payload: bytes, directory_sequence: int, sequence: int) -> bytes:
    if len(payload) > 64:
        raise AssertionError("test parameter payload exceeds 64 columns")
    data = payload.ljust(64) + f"{directory_sequence:8d}".encode("ascii")
    return _record(data, "P", sequence)


def _fixture_records() -> list[bytes]:
    start = [_record(b"metadata fixture", "S", 1)]
    global_stream = _global_stream()
    global_records = [
        _record(global_stream[index : index + 72], "G", sequence)
        for sequence, index in enumerate(range(0, len(global_stream), 72), start=1)
    ]
    directory_records: list[bytes] = []
    directory_records.extend(
        _directory_pair(
            entity_type=144,
            parameter_pointer=1,
            parameter_line_count=1,
            form=0,
            level=0,
            color=0,
            label="SURFACE",
            directory_sequence=1,
        )
    )
    directory_records.extend(
        _directory_pair(
            entity_type=406,
            parameter_pointer=2,
            parameter_line_count=1,
            form=15,
            level=2,
            color=3,
            label="PROP1",
            directory_sequence=3,
        )
    )
    directory_records.extend(
        _directory_pair(
            entity_type=402,
            parameter_pointer=3,
            parameter_line_count=1,
            form=1,
            level=4,
            color=-7,
            label="GROUP1",
            directory_sequence=5,
        )
    )
    parameter_records = [
        _parameter_record(b"144,0;", 1, 1),
        _parameter_record(b"406,1,8Hwallname;", 3, 2),
        _parameter_record(b"402,2,1,3;", 5, 3),
    ]
    terminate_data = (
        f"S{len(start):7d}"
        f"G{len(global_records):7d}"
        f"D{len(directory_records):7d}"
        f"P{len(parameter_records):7d}"
    ).encode("ascii")
    terminate = [_record(terminate_data, "T", 1)]
    return start + global_records + directory_records + parameter_records + terminate


def _write_fixture(path: Path, records: list[bytes] | None = None) -> None:
    path.write_bytes(b"\r\n".join(records or _fixture_records()) + b"\r\n")


class IgesMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "fixture.igs"
        _write_fixture(self.source)

    def _make_source_read_only(self) -> None:
        if os.name == "nt":
            self.source.chmod(stat.S_IREAD)
        else:
            self.source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        self.addCleanup(
            lambda: self.source.exists()
            and self.source.chmod(stat.S_IRUSR | stat.S_IWUSR)
        )

    def test_extracts_global_directory_property_and_group_evidence(self) -> None:
        self._make_source_read_only()

        result = inspect_iges_metadata(self.source)

        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["output_path"])
        self.assertEqual(result["format"]["record_width"], 80)
        self.assertEqual(
            result["format"]["section_counts"],
            result["format"]["terminate_counts"] | {"T": 1},
        )
        global_metadata = result["global_metadata"]
        self.assertEqual(global_metadata["product_identifier_sender"], "Widget")
        self.assertEqual(global_metadata["iges_file_name"], "source.sldprt")
        self.assertEqual(global_metadata["units_flag"], 2)
        self.assertEqual(global_metadata["units_name"], "MM")
        self.assertEqual(global_metadata["generation_time"], "20260801.120000")

        directory = result["directory"]
        self.assertEqual(directory["entity_count"], 3)
        self.assertEqual(
            directory["entity_type_counts"], {"144": 1, "402": 1, "406": 1}
        )
        self.assertEqual(directory["level_counts"], {"0": 1, "2": 1, "4": 1})
        self.assertEqual(directory["color_counts"], {"-7": 1, "0": 1, "3": 1})
        self.assertEqual(
            [item["label"] for item in directory["entity_labels"]],
            ["SURFACE", "PROP1", "GROUP1"],
        )

        evidence = result["semantic_evidence"]
        self.assertTrue(evidence["has_property_or_group_evidence"])
        properties = evidence["type_406_properties"]
        self.assertEqual(properties["count"], 1)
        self.assertEqual(properties["form_counts"], {"15": 1})
        self.assertEqual(
            properties["entities"][0]["parameter_tokens"],
            ["406", "1", "wallname"],
        )
        groups = evidence["type_402_groups"]
        self.assertEqual(groups["count"], 1)
        self.assertEqual(groups["form_counts"], {"1": 1})
        self.assertEqual(
            groups["entities"][0]["parameter_tokens"],
            ["402", "2", "1", "3"],
        )
        self.assertEqual(evidence["type_308_subfigure_definitions"]["count"], 0)

        self.assertTrue(result["source"]["before"]["read_only"])
        self.assertEqual(result["source"]["before"], result["source"]["after"])
        self.assertTrue(result["source"]["unchanged"])
        self.assertEqual(set(self.root.iterdir()), {self.source})

    def test_explicit_json_output_is_valid_and_never_overwritten(self) -> None:
        output = self.root / "evidence" / "iges_metadata.json"

        result = inspect_iges_metadata(self.source, output)

        self.assertTrue(output.is_file())
        loaded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(loaded, result)
        self.assertEqual(result["output_path"], str(output.resolve()))
        original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        with self.assertRaisesRegex(IgesMetadataError, "refusing to overwrite"):
            inspect_iges_metadata(self.source, output)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), original_hash)

    def test_rejects_non_80_column_record_without_writing_output(self) -> None:
        records = _fixture_records()
        records[0] = records[0][:-1]
        _write_fixture(self.source, records)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output = self.root / "must_not_exist.json"

        with self.assertRaisesRegex(IgesMetadataError, "79 columns; expected 80"):
            inspect_iges_metadata(self.source, output)

        self.assertFalse(output.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_rejects_terminate_count_that_disagrees_with_sections(self) -> None:
        records = _fixture_records()
        terminate = bytearray(records[-1])
        terminate[:8] = b"S      2"
        records[-1] = bytes(terminate)
        _write_fixture(self.source, records)

        with self.assertRaisesRegex(
            IgesMetadataError, "terminate S count is 2; actual count is 1"
        ):
            inspect_iges_metadata(self.source)

    def test_rejects_parameter_record_pointing_to_wrong_directory_entry(self) -> None:
        records = _fixture_records()
        first_parameter_index = next(
            index for index, record in enumerate(records) if record[72:73] == b"P"
        )
        bad = bytearray(records[first_parameter_index])
        bad[64:72] = b"       9"
        records[first_parameter_index] = bytes(bad)
        _write_fixture(self.source, records)

        with self.assertRaisesRegex(
            IgesMetadataError, "points to directory entry 9; expected 1"
        ):
            inspect_iges_metadata(self.source)

    def test_module_has_no_cad_or_process_imports(self) -> None:
        module_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "cfdpipe"
            / "iges_metadata.py"
        )
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertFalse(
            imported
            & {"gmsh", "subprocess", "win32com", "pythoncom", "comtypes"}
        )


if __name__ == "__main__":
    unittest.main()
