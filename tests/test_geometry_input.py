"""Tests for canonical read-only STEP materialization."""

from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from cfdpipe.geometry_input import (
    GeometryInputError,
    materialize_canonical_step,
    write_canonical_step_manifest,
)


def _make_read_only(path: Path) -> None:
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    path.chmod(path.stat().st_mode & ~writable)


class GeometryInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "delivered.step"
        self.source.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
        _make_read_only(self.source)
        self.destination = self.root / "geometry" / "raw" / "model1.step"

    def test_creates_byte_identical_read_only_new_input(self) -> None:
        before = self.source.stat()

        report = materialize_canonical_step(
            self.source,
            self.destination,
            repository_root=self.root,
        )

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(report["source"]["sha256"], report["canonical"]["sha256"])
        self.assertTrue(report["canonical"]["read_only"])
        self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)

    def test_existing_destination_is_never_overwritten(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"preserve")

        with self.assertRaisesRegex(GeometryInputError, "overwrite"):
            materialize_canonical_step(
                self.source,
                self.destination,
                repository_root=self.root,
            )

        self.assertEqual(self.destination.read_bytes(), b"preserve")

    def test_writable_source_is_rejected_before_creating_geometry(self) -> None:
        self.source.chmod(self.source.stat().st_mode | stat.S_IWUSR)

        with self.assertRaisesRegex(GeometryInputError, "read-only"):
            materialize_canonical_step(
                self.source,
                self.destination,
                repository_root=self.root,
            )

        self.assertFalse((self.root / "geometry").exists())

    def test_destination_must_be_directly_under_geometry_raw(self) -> None:
        outside = self.root / "runs" / "model1.step"

        with self.assertRaisesRegex(GeometryInputError, "geometry.raw"):
            materialize_canonical_step(
                self.source,
                outside,
                repository_root=self.root,
            )

        self.assertFalse(outside.exists())

    def test_parent_traversal_is_rejected(self) -> None:
        unsafe = self.root / "geometry" / "raw" / ".." / "model1.step"

        with self.assertRaisesRegex(GeometryInputError, "must not contain"):
            materialize_canonical_step(
                self.source,
                unsafe,
                repository_root=self.root,
            )

    def test_writes_new_only_manifest_at_fixed_evidence_path(self) -> None:
        report = materialize_canonical_step(
            self.source,
            self.destination,
            repository_root=self.root,
        )
        manifest = (
            self.root
            / "runs"
            / "real_connection"
            / "geometry_input"
            / "canonical_input.json"
        )

        written = write_canonical_step_manifest(
            report, manifest, repository_root=self.root
        )

        self.assertEqual(written, manifest)
        self.assertIn('"status": "PASS"', manifest.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(GeometryInputError, "overwrite"):
            write_canonical_step_manifest(
                report, manifest, repository_root=self.root
            )


if __name__ == "__main__":
    unittest.main()
