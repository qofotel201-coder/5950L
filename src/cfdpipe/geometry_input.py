"""Safe materialization of the configured read-only geometry input.

The project was initially delivered with its STEP file at the repository
root, while ``config/project.toml`` names ``geometry/raw/model1.step``.  This
module establishes that canonical input without moving, renaming or modifying
the delivered file.  Existing targets are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any
from uuid import uuid4


PathLike = str | os.PathLike[str]
_STEP_SUFFIXES = {".step", ".stp"}


class GeometryInputError(RuntimeError):
    """Raised when a canonical geometry input cannot be created safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_read_only(path: Path) -> bool:
    metadata = path.stat()
    attributes = getattr(metadata, "st_file_attributes", None)
    if attributes is not None:
        flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return bool(attributes & flag)
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(metadata.st_mode & writable)


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & flag)


def _has_reparse_ancestor(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_reparse(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _snapshot(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    return {
        "path": str(path),
        "size_bytes": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "read_only": _is_read_only(path),
        "sha256": _sha256(path),
    }


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def materialize_canonical_step(
    source_path: PathLike,
    destination_path: PathLike,
    *,
    repository_root: PathLike | None = None,
) -> dict[str, Any]:
    """Create one byte-identical, read-only STEP under ``geometry/raw``.

    The source must already be an ordinary read-only STEP inside the
    repository.  The destination must be a new direct child of
    ``geometry/raw``.  Copying is staged in that same directory, verified by
    size and SHA-256, then atomically published.  The source snapshot must be
    unchanged after publication.
    """

    root = (
        Path(repository_root).expanduser().resolve(strict=True)
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    source_lexical = Path(source_path).expanduser()
    destination_lexical = Path(destination_path).expanduser()
    if ".." in source_lexical.parts or ".." in destination_lexical.parts:
        raise GeometryInputError("geometry paths must not contain '..'")

    source = source_lexical.resolve(strict=True)
    if not _within(source, root):
        raise GeometryInputError(f"STEP source is outside the repository: {source}")
    if source.suffix.casefold() not in _STEP_SUFFIXES:
        raise GeometryInputError(f"STEP source has an unsupported suffix: {source}")
    if _is_reparse(source) or _has_reparse_ancestor(source.parent):
        raise GeometryInputError(f"STEP source uses a reparse path: {source}")
    if not stat.S_ISREG(source.lstat().st_mode):
        raise GeometryInputError(f"STEP source is not an ordinary file: {source}")
    if not _is_read_only(source):
        raise GeometryInputError(f"STEP source must be read-only: {source}")

    raw_root = root / "geometry" / "raw"
    destination = Path(os.path.abspath(destination_lexical))
    if destination.parent != raw_root or destination.suffix.casefold() not in _STEP_SUFFIXES:
        raise GeometryInputError(
            f"canonical STEP must be a direct .step/.stp child of {raw_root}: "
            f"{destination}"
        )
    if destination == source:
        raise GeometryInputError("canonical STEP destination equals its source")
    if destination.exists() or destination.is_symlink():
        raise GeometryInputError(
            f"refusing to overwrite canonical STEP: {destination}"
        )
    if _has_reparse_ancestor(destination.parent):
        raise GeometryInputError(
            f"canonical STEP destination uses a reparse ancestor: {destination}"
        )

    before = _snapshot(source)
    raw_root.mkdir(parents=True, exist_ok=True)
    if _has_reparse_ancestor(raw_root):
        raise GeometryInputError(
            f"canonical STEP destination became a reparse path: {raw_root}"
        )
    temporary = raw_root / f".{destination.name}.{uuid4().hex}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise GeometryInputError(f"temporary STEP path already exists: {temporary}")

    try:
        shutil.copy2(source, temporary)
        temporary_snapshot = _snapshot(temporary)
        if (
            temporary_snapshot["sha256"] != before["sha256"]
            or temporary_snapshot["size_bytes"] != before["size_bytes"]
        ):
            raise GeometryInputError("staged canonical STEP differs from its source")
        if destination.exists() or destination.is_symlink():
            raise GeometryInputError(
                f"canonical STEP appeared during copy: {destination}"
            )
        os.replace(temporary, destination)
        writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        destination.chmod(destination.stat().st_mode & ~writable)
        canonical = _snapshot(destination)
        if (
            canonical["sha256"] != before["sha256"]
            or canonical["size_bytes"] != before["size_bytes"]
            or not canonical["read_only"]
        ):
            raise GeometryInputError(
                "published canonical STEP failed hash, size or read-only verification"
            )
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()

    after = _snapshot(source)
    if after != before:
        raise GeometryInputError("STEP source changed while creating canonical input")
    return {
        "status": "PASS",
        "operation": "byte_identical_read_only_copy",
        "source": before,
        "canonical": canonical,
        "source_unchanged": True,
        "overwrote_existing_file": False,
    }


def write_canonical_step_manifest(
    report: dict[str, Any],
    manifest_path: PathLike,
    *,
    repository_root: PathLike | None = None,
) -> Path:
    """Persist one new-only manifest at the fixed geometry-input evidence path."""

    root = (
        Path(repository_root).expanduser().resolve(strict=True)
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    path_raw = Path(manifest_path).expanduser()
    if ".." in path_raw.parts:
        raise GeometryInputError("geometry input manifest path must not contain '..'")
    path = Path(os.path.abspath(path_raw))
    expected = (
        root
        / "runs"
        / "real_connection"
        / "geometry_input"
        / "canonical_input.json"
    )
    if path != expected:
        raise GeometryInputError(
            f"geometry input manifest must exactly be written to {expected}"
        )
    if path.exists() or path.is_symlink():
        raise GeometryInputError(f"refusing to overwrite geometry input manifest: {path}")
    if _has_reparse_ancestor(path.parent):
        raise GeometryInputError(
            f"geometry input manifest uses a reparse ancestor: {path}"
        )
    if report.get("status") != "PASS" or report.get("source_unchanged") is not True:
        raise GeometryInputError("refusing to persist a non-PASS canonical input report")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    return path


__all__ = [
    "GeometryInputError",
    "materialize_canonical_step",
    "write_canonical_step_manifest",
]
