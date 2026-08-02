"""Fail-fast orchestration primitives for connection-stage commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tempfile
import traceback
import tomllib
from typing import Any
from uuid import uuid4

from .bridges import GmshBridge, ParaViewBridge, SU2Bridge
from .bridges.su2_bridge import (
    prepare_project_supersonic_pilot_case,
    validate_visualization_file,
)
from .marker_config import (
    MarkerConfigError,
    load_marker_config,
    validate_marker_config_document,
)
from .process import CommandRunner
from .rear_outlet_freeze import evaluate_rear_outlet_freeze
from .topology_smoke_mesh import build_topology_smoke_mesh
from .toolchain import ResolvedTool, Toolchain


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return sanitized or "pipeline"


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """One named callable in a fail-fast pipeline."""

    name: str
    action: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class StageRecord:
    """Serializable execution record for one attempted stage."""

    name: str
    status: str
    started_at: str
    ended_at: str
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Result of a fully successful pipeline run."""

    name: str
    started_at: str
    ended_at: str
    records: tuple[StageRecord, ...]
    log_path: Path


class PipelineStageError(RuntimeError):
    """Raised immediately when a stage fails.

    The original exception is retained through exception chaining and in
    ``cause``. Command failures therefore keep their unmodified stderr and
    command log references.
    """

    def __init__(
        self,
        *,
        pipeline_name: str,
        failed_stage: str,
        records: tuple[StageRecord, ...],
        log_path: Path,
        cause: Exception,
    ) -> None:
        super().__init__(
            f"pipeline {pipeline_name!r} stopped at stage {failed_stage!r}: {cause}"
        )
        self.pipeline_name = pipeline_name
        self.failed_stage = failed_stage
        self.records = records
        self.log_path = log_path
        self.cause = cause


class Pipeline:
    """Run named stages in order and stop on the first failure."""

    def __init__(self, output_dir: str | os.PathLike[str] = "runs/connection") -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()

    def run(
        self,
        stages: Iterable[PipelineStage],
        *,
        name: str = "connection-pipeline",
    ) -> PipelineResult:
        stage_list = tuple(stages)
        if not stage_list:
            raise ValueError("a pipeline must contain at least one stage")
        if any(not stage.name.strip() for stage in stage_list):
            raise ValueError("pipeline stage names must not be empty")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.output_dir / (
            f"{_safe_name(name)}-{os.getpid()}-{uuid4().hex[:10]}.json"
        )
        pipeline_started = _utc_now()
        records: list[StageRecord] = []

        for stage in stage_list:
            stage_started = _utc_now()
            try:
                stage.action()
            except Exception as exc:
                records.append(
                    StageRecord(
                        name=stage.name,
                        status="failed",
                        started_at=stage_started,
                        ended_at=_utc_now(),
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
                self._write_log(
                    log_path,
                    name=name,
                    status="failed",
                    started_at=pipeline_started,
                    ended_at=_utc_now(),
                    records=records,
                )
                error = PipelineStageError(
                    pipeline_name=name,
                    failed_stage=stage.name,
                    records=tuple(records),
                    log_path=log_path,
                    cause=exc,
                )
                raise error from exc

            records.append(
                StageRecord(
                    name=stage.name,
                    status="completed",
                    started_at=stage_started,
                    ended_at=_utc_now(),
                )
            )

        pipeline_ended = _utc_now()
        self._write_log(
            log_path,
            name=name,
            status="completed",
            started_at=pipeline_started,
            ended_at=pipeline_ended,
            records=records,
        )
        return PipelineResult(
            name=name,
            started_at=pipeline_started,
            ended_at=pipeline_ended,
            records=tuple(records),
            log_path=log_path,
        )

    @staticmethod
    def _write_log(
        path: Path,
        *,
        name: str,
        status: str,
        started_at: str,
        ended_at: str,
        records: list[StageRecord],
    ) -> None:
        payload = {
            "name": name,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "stages": [asdict(record) for record in records],
        }
        _atomic_write_text(
            path,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )


CONNECTION_KEYS = (
    "python_to_gmsh",
    "gmsh_to_su2_mesh",
    "python_to_su2",
    "su2_to_paraview_file",
    "python_to_pvbatch",
    "pvbatch_to_results",
)

_SUPPORTED_VISUALIZATION_SUFFIXES = {".vtk", ".vtu", ".pvtu", ".vtm"}
_OWNERSHIP_LEDGER = "connection_artifacts.json"
_REPORT_JSON = "connection_report.json"
_REPORT_MARKDOWN = "connection_report.md"
_CLEAN_MANIFEST = "clean_manifest.json"

# These names are reserved by the connection workflow.  The list deliberately
# excludes gmsh/pip_install.log and su2/discovery/**, which pre-date this
# end-to-end command and must survive --clean.
_BOOTSTRAP_MANAGED_FILES = (
    "tools/toolchain_manifest.json",
    "gmsh/gmsh_manifest.json",
    "gmsh/gmsh.log",
    "gmsh/smoke.msh",
    "gmsh/smoke.su2",
    "su2/run_manifest.json",
    "su2/smoke.cfg",
    "su2/smoke.su2",
    "su2/history.csv",
    "su2/connection_solution.vtu",
    "su2/connection_solution.vtk",
    "su2/connection_solution.pvtu",
    "su2/connection_solution.vtm",
    "su2/connection_solution_restart.dat",
    "su2/solver.stdout.log",
    "su2/solver.stderr.log",
    "su2/solver.command.json",
    "su2/su2_version.stdout.log",
    "su2/su2_version.stderr.log",
    "su2/su2_version.command.json",
    "su2/su2_sol.cfg",
    "su2/su2_sol.stdout.log",
    "su2/su2_sol.stderr.log",
    "su2/su2_sol.command.json",
    "paraview/paraview_manifest.json",
    "paraview/pvbatch_probe.json",
    "paraview/solution_inventory.json",
    "paraview/smoke_postprocess.json",
    "paraview/smoke_integral.csv",
    "paraview/pvbatch.stdout.log",
    "paraview/pvbatch.stderr.log",
    "paraview/pvbatch.command.json",
    "paraview/pvbatch.help.stdout.log",
    "paraview/pvbatch.help.stderr.log",
    "paraview/pvbatch.help.command.json",
    "paraview/pvbatch.probe.stdout.log",
    "paraview/pvbatch.probe.stderr.log",
    "paraview/pvbatch.probe.command.json",
    "paraview/pvbatch.inspect.stdout.log",
    "paraview/pvbatch.inspect.stderr.log",
    "paraview/pvbatch.inspect.command.json",
    _REPORT_JSON,
    _REPORT_MARKDOWN,
    _CLEAN_MANIFEST,
    _OWNERSHIP_LEDGER,
)

_COMMAND_ARCHIVE_DIRECTORIES = (
    "su2/command_archive",
    "paraview/command_archive",
)

class ConnectionTestError(RuntimeError):
    """Raised after a FAIL connection report has been written."""

    def __init__(
        self,
        message: str,
        *,
        report_path: Path | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.report_path = report_path
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """Successful end-to-end connection result."""

    report: dict[str, Any]
    report_json: Path
    report_markdown: Path
    pipeline_log: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(path.parent):
        raise ValueError(f"refusing to write through reparse directory: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _is_reparse(path.parent):
            raise ValueError(
                f"refusing to replace through reparse directory: {path.parent}"
            )
        if _lexists(path) and _is_reparse(path):
            raise ValueError(f"refusing to replace link/reparse output: {path}")
        os.replace(temporary, path)
    finally:
        if _lexists(temporary):
            temporary.unlink()


def _write_json(path: Path, payload: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return payload


def _is_reparse(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    is_junction = getattr(path, "is_junction", None)
    return bool(
        stat.S_ISLNK(details.st_mode)
        or (reparse_flag and attributes & reparse_flag)
        or (callable(is_junction) and is_junction())
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_relative_owned_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ownership ledger path must be a nonempty string")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise ValueError(f"ownership ledger path must be relative: {value!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"ownership ledger path must not contain a drive: {value!r}")
    raw_parts = normalized.split("/")
    if any(
        part in {"", ".", ".."}
        or ":" in part
        or part.endswith(".")
        or part.endswith(" ")
        for part in raw_parts
    ):
        raise ValueError(f"unsafe ownership ledger path: {value!r}")
    return Path(*raw_parts)


def _assert_safe_root(
    output_directory: Path,
    allowed_root: Path,
    trusted_root: Path,
) -> Path:
    if ".." in output_directory.expanduser().parts:
        raise ValueError("connection output must not contain '..' path traversal")
    lexical_output = Path(os.path.abspath(output_directory.expanduser()))
    lexical_allowed = Path(os.path.abspath(allowed_root.expanduser()))
    lexical_trusted = Path(os.path.abspath(trusted_root.expanduser()))
    if os.path.normcase(str(lexical_output)) != os.path.normcase(str(lexical_allowed)):
        raise ValueError(
            "connection output must exactly match the authorized root: "
            f"{lexical_output} != {lexical_allowed}"
        )
    if not lexical_trusted.is_dir() or _is_reparse(lexical_trusted):
        raise ValueError(f"trusted repository root is unsafe: {lexical_trusted}")
    try:
        relative = lexical_output.relative_to(lexical_trusted)
    except ValueError as exc:
        raise ValueError(
            f"connection root is outside trusted repository root: {lexical_output}"
        ) from exc
    current = lexical_trusted
    for part in relative.parts:
        current = current / part
        if _lexists(current):
            if _is_reparse(current) or not current.is_dir():
                raise ValueError(
                    f"connection root crosses an unsafe directory: {current}"
                )
        else:
            current.mkdir()
            if _is_reparse(current) or not current.is_dir():
                raise ValueError(
                    f"connection root directory creation was redirected: {current}"
                )
    if not lexical_output.is_dir():
        raise ValueError(f"connection output is not a directory: {lexical_output}")
    resolved_trusted = lexical_trusted.resolve(strict=True)
    resolved_output = lexical_output.resolve(strict=True)
    if resolved_output != resolved_trusted and resolved_trusted not in resolved_output.parents:
        raise ValueError("connection root resolves outside the trusted repository")
    return resolved_output


def _assert_safe_candidate(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"clean candidate escapes connection root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if _lexists(current) and _is_reparse(current):
            raise ValueError(f"clean candidate is or crosses a link/reparse point: {current}")
    resolved = path.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"clean candidate resolves outside connection root: {path}")


_DYNAMIC_OWNED_PATHS = (
    re.compile(r"^connect-test-\d+-[0-9a-f]{10}\.json$", re.IGNORECASE),
    re.compile(
        r"^gmsh/[A-Za-z0-9_.-]+_gmsh-cli-version_[0-9a-f]{8}"
        r"\.(?:json|stdout\.log|stderr\.log)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\.cfdpipe-clean-[A-Za-z0-9_.-]+/\d{6}\.artifact$",
        re.IGNORECASE,
    ),
)


def _owned_relative_allowed(relative: Path) -> bool:
    normalized = relative.as_posix()
    if normalized in _BOOTSTRAP_MANAGED_FILES:
        return True
    return any(pattern.fullmatch(normalized) for pattern in _DYNAMIC_OWNED_PATHS)


def _regular_file_snapshot(path: Path) -> dict[str, Any]:
    before = os.lstat(path)
    attributes = getattr(before, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or (reparse_flag and attributes & reparse_flag)
    ):
        raise ValueError(f"path is not a safe regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"file changed while being opened: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError(f"file changed during verification: {path}")
    return {
        "size_bytes": before.st_size,
        "sha256": digest.hexdigest(),
        "identity": (before.st_dev, before.st_ino, before.st_mtime_ns),
    }


def write_ownership_ledger(
    output_directory: Path,
    owned_paths: Iterable[Path],
    *,
    trusted_root: Path,
    virtual_files: Mapping[Path, tuple[int, str]] | None = None,
) -> Path:
    root = _assert_safe_root(output_directory, output_directory, trusted_root)
    records: dict[str, dict[str, Any]] = {}
    for candidate in owned_paths:
        path = Path(candidate).expanduser().resolve(strict=False)
        _assert_safe_candidate(path, root)
        relative = path.relative_to(root)
        if path.name == _OWNERSHIP_LEDGER or not _owned_relative_allowed(relative):
            raise ValueError(f"path is not in the connection ownership namespace: {path}")
        if not _lexists(path):
            continue
        snapshot = _regular_file_snapshot(path)
        normalized = os.path.normcase(relative.as_posix())
        if normalized in records:
            raise ValueError(f"duplicate ownership path: {relative.as_posix()}")
        records[normalized] = {
            "path": relative.as_posix(),
            "size_bytes": snapshot["size_bytes"],
            "sha256": snapshot["sha256"],
        }
    for virtual_path, (size_bytes, sha256_value) in (virtual_files or {}).items():
        path = Path(virtual_path).expanduser().resolve(strict=False)
        _assert_safe_candidate(path, root)
        relative = path.relative_to(root)
        if not _owned_relative_allowed(relative):
            raise ValueError(f"virtual path is outside ownership namespace: {path}")
        normalized = os.path.normcase(relative.as_posix())
        records[normalized] = {
            "path": relative.as_posix(),
            "size_bytes": int(size_bytes),
            "sha256": str(sha256_value).lower(),
        }
    ledger_path = root / _OWNERSHIP_LEDGER
    payload = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "root": str(root),
        "files": [records[key] for key in sorted(records)],
    }
    _write_json(ledger_path, payload)
    return ledger_path


def clean_connection_artifacts(
    output_directory: str | os.PathLike[str],
    *,
    allowed_root: str | os.PathLike[str],
    trusted_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Delete only owned connection artifacts after a full safety preflight.

    The connection root itself and unknown files are never deleted.  No
    recursive deletion primitive is used; known archive trees are inspected
    without following links and then removed file-by-file.
    """

    started_at = _utc_now()
    root = _assert_safe_root(
        Path(output_directory), Path(allowed_root), Path(trusted_root)
    )
    candidates: set[Path] = set()
    expected: dict[Path, tuple[int, str]] = {}
    ledger_path = root / _OWNERSHIP_LEDGER
    if _lexists(ledger_path):
        if _is_reparse(ledger_path) or not ledger_path.is_file():
            raise ValueError(f"ownership ledger is not a safe regular file: {ledger_path}")
        _regular_file_snapshot(ledger_path)
        ledger = _load_json(ledger_path)
        if ledger.get("schema_version") != 1:
            raise ValueError("ownership ledger schema_version is not 1")
        if ledger.get("root") != str(root):
            raise ValueError("ownership ledger root does not match connection root")
        entries = ledger.get("files", [])
        if not isinstance(entries, list):
            raise ValueError("ownership ledger files field must be a list")
        normalized_paths: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("ownership ledger entry must be an object")
            relative = _validate_relative_owned_path(entry.get("path"))
            if not _owned_relative_allowed(relative):
                raise ValueError(
                    f"ownership ledger path is outside managed namespace: {relative}"
                )
            normalized = os.path.normcase(relative.as_posix())
            if normalized in normalized_paths:
                raise ValueError(f"duplicate ownership ledger path: {relative}")
            normalized_paths.add(normalized)
            declared_hash = entry.get("sha256")
            if not isinstance(declared_hash, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", declared_hash
            ):
                raise ValueError(f"invalid ownership ledger SHA256: {entry!r}")
            declared_size = entry.get("size_bytes")
            if not isinstance(declared_size, int) or declared_size < 0:
                raise ValueError(f"invalid ownership ledger size: {entry!r}")
            candidate = root / relative
            if _lexists(candidate):
                candidates.add(candidate)
                expected[candidate] = (declared_size, declared_hash.lower())
        candidates.add(ledger_path)
    else:
        # Bootstrap migration for artifacts created before ownership ledgers
        # existed.  Only exact reserved filenames are eligible.
        for relative in _BOOTSTRAP_MANAGED_FILES:
            candidate = root / relative
            if _lexists(candidate):
                candidates.add(candidate)

    # Full preflight happens before the first move.
    for candidate in sorted(candidates, key=lambda item: str(item).lower()):
        _assert_safe_candidate(candidate, root)
        snapshot = _regular_file_snapshot(candidate)
        declared = expected.get(candidate)
        if declared is not None and (
            snapshot["size_bytes"] != declared[0]
            or snapshot["sha256"] != declared[1]
        ):
            raise ValueError(f"owned artifact changed since ledger creation: {candidate}")

    removed: list[str] = []
    moved: list[tuple[Path, Path]] = []
    quarantine = Path(tempfile.mkdtemp(prefix=".cfdpipe-clean-", dir=root))
    try:
        for index, candidate in enumerate(
            sorted(candidates, key=lambda item: str(item).lower()), start=1
        ):
            # Recheck immediately before the same-volume atomic move.
            snapshot = _regular_file_snapshot(candidate)
            declared = expected.get(candidate)
            if declared is not None and (
                snapshot["size_bytes"] != declared[0]
                or snapshot["sha256"] != declared[1]
            ):
                raise ValueError(
                    f"owned artifact changed before clean staging: {candidate}"
                )
            staged = quarantine / f"{index:06d}.artifact"
            os.replace(candidate, staged)
            moved.append((candidate, staged))
            removed.append(candidate.relative_to(root).as_posix())
    except BaseException as move_error:
        rollback_errors: list[str] = []
        for original, staged in reversed(moved):
            try:
                os.replace(staged, original)
            except OSError as rollback_error:
                rollback_errors.append(f"{original}: {rollback_error}")
        try:
            quarantine.rmdir()
        except OSError:
            pass
        if rollback_errors:
            raise RuntimeError(
                f"clean staging failed ({move_error}); rollback also failed: "
                + " | ".join(rollback_errors)
            ) from move_error
        raise

    cleanup_warnings: list[str] = []
    retained_quarantine: list[str] = []
    for _original, staged in moved:
        try:
            if _is_reparse(staged):
                staged.unlink()
            else:
                staged.unlink()
        except OSError as cleanup_error:
            cleanup_warnings.append(f"quarantine cleanup failed for {staged}: {cleanup_error}")
            retained_quarantine.append(staged.relative_to(root).as_posix())
    if not retained_quarantine:
        try:
            quarantine.rmdir()
        except OSError as cleanup_error:
            cleanup_warnings.append(
                f"empty quarantine directory could not be removed: {cleanup_error}"
            )

    payload = {
        "status": "PASS",
        "started_at": started_at,
        "ended_at": _utc_now(),
        "root": str(root),
        "removed": removed,
        "removed_count": len(removed),
        "unknown_files_preserved": True,
        "warnings": cleanup_warnings,
        "retained_quarantine_files": retained_quarantine,
    }
    _write_json(root / _CLEAN_MANIFEST, payload)
    return payload


def _preflight_connection_stage_paths(root: Path) -> None:
    """Reject output links or special files before any stage can write."""

    for name in ("tools", "gmsh", "su2", "paraview"):
        directory = root / name
        if _lexists(directory):
            _assert_safe_candidate(directory, root)
            if _is_reparse(directory) or not directory.is_dir():
                raise ValueError(
                    f"connection stage path must be a real directory: {directory}"
                )
    for relative in _BOOTSTRAP_MANAGED_FILES:
        candidate = root / relative
        if _lexists(candidate):
            _assert_safe_candidate(candidate, root)
            if _is_reparse(candidate) or not candidate.is_file():
                raise ValueError(
                    f"connection artifact path must be a regular file: {candidate}"
                )
    for relative in _COMMAND_ARCHIVE_DIRECTORIES:
        archive = root / relative
        if _lexists(archive):
            _assert_safe_candidate(archive, root)
            if _is_reparse(archive) or not archive.is_dir():
                raise ValueError(
                    f"connection command archive must be a real directory: {archive}"
                )


def _report_paths_are_safe(root: Path) -> bool:
    for name in (_REPORT_JSON, _REPORT_MARKDOWN):
        path = root / name
        if _lexists(path) and (_is_reparse(path) or not path.is_file()):
            return False
    return True


def _resolved_tool_record(tool: ResolvedTool, *, required: bool) -> dict[str, Any]:
    path = tool.path.expanduser().resolve(strict=True)
    return {
        "status": "PASS",
        "required": required,
        "path": str(path),
        "source": tool.source,
        "sha256": _sha256(path),
    }


def check_connection_tools(
    toolchain: Toolchain,
    output_directory: str | os.PathLike[str],
    *,
    controller_argv: Sequence[str] = (),
) -> tuple[dict[str, Any], dict[str, ResolvedTool | None]]:
    """Check Python, the Gmsh module, required tools, and optional tools."""

    output_dir = Path(output_directory).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "toolchain_manifest.json"
    started_at = _utc_now()
    python_path = Path(sys.executable).expanduser().resolve(strict=True)
    manifest: dict[str, Any] = {
        "status": "FAIL",
        "started_at": started_at,
        "ended_at": None,
        "controller": {
            "python_executable": str(python_path),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "argv": [str(value) for value in controller_argv],
            "sha256": _sha256(python_path),
        },
        "gmsh_python": {
            "status": "FAIL",
            "version": None,
            "module_path": None,
            "module_sha256": None,
        },
        "tools": {},
        "warnings": [],
        "error": None,
    }
    resolved: dict[str, ResolvedTool | None] = {}
    try:
        gmsh = importlib.import_module("gmsh")
        module_value = getattr(gmsh, "__file__", None)
        if not isinstance(module_value, str) or not module_value:
            raise ImportError("gmsh module has no file path")
        module_path = Path(module_value).expanduser().resolve(strict=True)
        gmsh_version = str(getattr(gmsh, "__version__", "")).strip()
        if not gmsh_version:
            raise ImportError("gmsh module has no __version__")
        manifest["gmsh_python"] = {
            "status": "PASS",
            "version": gmsh_version,
            "module_path": str(module_path),
            "module_sha256": _sha256(module_path),
        }

        for name in ("gmsh", "su2_cfd"):
            tool = toolchain.resolve(name)
            resolved[name] = tool
            manifest["tools"][name] = _resolved_tool_record(tool, required=True)

        for name in ("su2_sol", "mpiexec"):
            try:
                tool = toolchain.resolve_optional(name)
            except Exception as optional_error:
                resolved[name] = None
                record = {
                    "status": "WARN",
                    "required": False,
                    "path": None,
                    "source": None,
                    "sha256": None,
                    "error": str(optional_error),
                }
                manifest["tools"][name] = record
                manifest["warnings"].append(
                    f"optional tool {name} is unusable: {optional_error}"
                )
            else:
                resolved[name] = tool
                manifest["tools"][name] = (
                    {
                        "status": "MISSING_OPTIONAL",
                        "required": False,
                        "path": None,
                        "source": None,
                        "sha256": None,
                    }
                    if tool is None
                    else _resolved_tool_record(tool, required=False)
                )

        pvbatch = toolchain.resolve("pvbatch")
        resolved["pvbatch"] = pvbatch
        manifest["tools"]["pvbatch"] = _resolved_tool_record(
            pvbatch, required=True
        )
        manifest["status"] = "PASS"
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        manifest["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ),
        }
    finally:
        manifest["ended_at"] = _utc_now()
        _write_json(manifest_path, manifest)

    if manifest["status"] != "PASS":
        message = manifest["error"]["message"] if manifest["error"] else "unknown error"
        raise ConnectionTestError(f"required tool check failed: {message}")
    return manifest, resolved


def _file_record(value: Any, *, within: Path | None = None) -> dict[str, Any] | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not path.is_file() or _is_reparse(path):
        return None
    if within is not None and path != within and within not in path.parents:
        return None
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _command_ok(command: Any) -> bool:
    if not isinstance(command, Mapping):
        return False
    return bool(
        command.get("returncode") == 0
        and command.get("timed_out") is False
        and command.get("interrupted") is False
        and command.get("dry_run") is False
        and command.get("output_complete") is True
        and command.get("runner_error") is None
    )


def _command_script_name(command: Any) -> str | None:
    if not isinstance(command, Mapping):
        return None
    values = command.get("command")
    if not isinstance(values, list):
        return None
    expected = {
        "probe_pvbatch.py",
        "inspect_solution.py",
        "smoke_postprocess.py",
    }
    for value in values[1:]:
        name = Path(str(value)).name
        if name in expected:
            return name
    return None


def _manifest_for_attempt(
    state: Mapping[str, Any],
    name: str,
    path: Path,
) -> dict[str, Any] | None:
    payload = state.get("manifests", {}).get(name)
    if isinstance(payload, dict):
        return payload
    if not state.get("attempted", {}).get(name, False):
        return None
    try:
        loaded = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return loaded


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _current_time_window(
    started: Any,
    ended: Any,
    connection_started_at: str,
) -> bool:
    stage_start = _parse_time(started)
    stage_end = _parse_time(ended)
    connection_start = _parse_time(connection_started_at)
    return bool(
        stage_start is not None
        and stage_end is not None
        and connection_start is not None
        and stage_start >= connection_start
        and stage_end >= stage_start
    )


def _extract_log_lines(
    path: Path,
    patterns: Sequence[str],
    *,
    maximum: int = 20,
) -> list[str]:
    if not path.is_file() or _is_reparse(path):
        return []
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    selected: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(pattern.search(line) for pattern in compiled):
            selected.append(line.strip())
            if len(selected) >= maximum:
                break
    return selected


def _json_is_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_json_is_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_json_is_finite(item) for item in value)
    return True


def _section(
    evidence: dict[str, Any],
    checks: Mapping[str, bool],
) -> dict[str, Any]:
    evidence["checks"] = dict(checks)
    evidence["failed_checks"] = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if checks and all(checks.values()) else "FAIL",
        "evidence": [evidence],
    }


def _not_run_section(reason: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "evidence": [
            {
                "execution_state": "NOT_RUN_DUE_TO_UPSTREAM_FAILURE",
                "reason": reason,
                "checks": {},
                "failed_checks": ["stage_not_run"],
            }
        ],
    }


def build_connection_report(
    output_directory: str | os.PathLike[str],
    state: Mapping[str, Any],
    *,
    connection_started_at: str,
    connection_ended_at: str,
    pipeline_log: Path | None = None,
    failure: BaseException | None = None,
) -> dict[str, Any]:
    """Cross-check current-run manifests and build the six-link report."""

    root = Path(output_directory).expanduser().resolve(strict=True)
    gmsh_dir = (root / "gmsh").resolve(strict=False)
    su2_dir = (root / "su2").resolve(strict=False)
    paraview_dir = (root / "paraview").resolve(strict=False)
    tools = _manifest_for_attempt(
        state, "tools", root / "tools" / "toolchain_manifest.json"
    )
    gmsh = _manifest_for_attempt(
        state, "gmsh", root / "gmsh" / "gmsh_manifest.json"
    )
    su2 = _manifest_for_attempt(
        state, "su2", root / "su2" / "run_manifest.json"
    )
    paraview = _manifest_for_attempt(
        state, "paraview", root / "paraview" / "paraview_manifest.json"
    )
    attempted = state.get("attempted", {})

    report: dict[str, Any] = {}

    # Python -> Gmsh Python API.  The API is in-process, so no subprocess
    # return code is invented; the controller command and completed API call
    # are recorded separately from the real Gmsh CLI version command.
    if gmsh is None:
        gmsh_python = {} if tools is None else tools.get("gmsh_python", {})
        if gmsh_python.get("status") != "PASS":
            report["python_to_gmsh"] = _section(
                {
                    "execution_state": "FAILED_DURING_TOOL_CHECK",
                    "tool_manifest": str(root / "tools" / "toolchain_manifest.json"),
                    "gmsh_python": gmsh_python,
                    "error": None if tools is None else tools.get("error"),
                },
                {
                    "python_import_gmsh": gmsh_python.get("status") == "PASS",
                    "gmsh_api_stage_executed": False,
                },
            )
        elif attempted.get("gmsh", False):
            report["python_to_gmsh"] = _section(
                {
                    "execution_state": "GMSH_STAGE_FAILED",
                    "manifest_path": str(root / "gmsh" / "gmsh_manifest.json"),
                },
                {"current_gmsh_manifest_available": False},
            )
        else:
            report["python_to_gmsh"] = _not_run_section(
                "Gmsh stage was not run because an upstream tool check failed"
            )
    else:
        gmsh_log = gmsh_dir / "gmsh.log"
        gmsh_command = gmsh.get("gmsh_cli_command")
        module_record = _file_record(gmsh.get("python_module_path"))
        cli_record = _file_record(gmsh.get("gmsh_cli_path"))
        gmsh_log_lines = _extract_log_lines(
            gmsh_log,
            (r"gmsh_version:", r"python_module_path:", r"status:\s*PASS"),
        )
        gmsh_python = {} if tools is None else tools.get("gmsh_python", {})
        controller = {} if tools is None else tools.get("controller", {})
        evidence = {
            "execution_kind": "in_process_python_api",
            "python_executable": controller.get("python_executable"),
            "python_version": controller.get("python_version"),
            "controller_argv": controller.get("argv", []),
            "gmsh_module": module_record,
            "executable_paths": {"gmsh_cli": gmsh.get("gmsh_cli_path")},
            "software_versions": {
                "gmsh_python": gmsh.get("gmsh_version"),
                "gmsh_cli": gmsh.get("gmsh_cli_version"),
            },
            "api_call": "gmsh.initialize -> mesh operations -> gmsh.finalize",
            "commands": [gmsh_command] if isinstance(gmsh_command, Mapping) else [],
            "return_codes": [
                gmsh_command.get("returncode")
                if isinstance(gmsh_command, Mapping)
                else None
            ],
            "input_files": [],
            "output_files": [
                record
                for record in (
                    _file_record(gmsh_dir / "gmsh_manifest.json", within=gmsh_dir),
                    _file_record(gmsh_log, within=gmsh_dir),
                )
                if record is not None
            ],
            "sha256": {
                "python": controller.get("sha256"),
                "gmsh_module": None if module_record is None else module_record["sha256"],
                "gmsh_cli": None if cli_record is None else cli_record["sha256"],
            },
            "key_log_lines": gmsh_log_lines,
            "started_at": gmsh.get("started_at"),
            "ended_at": gmsh.get("ended_at"),
            "warnings": [],
        }
        report["python_to_gmsh"] = _section(
            evidence,
            {
                "tools_manifest_pass": tools is not None and tools.get("status") == "PASS",
                "python_import_gmsh": gmsh_python.get("status") == "PASS",
                "gmsh_manifest_pass": gmsh.get("status") == "PASS"
                and gmsh.get("error") is None,
                "gmsh_module_exists": module_record is not None,
                "gmsh_module_matches_tool_check": module_record is not None
                and module_record.get("sha256") == gmsh_python.get("module_sha256"),
                "gmsh_cli_exists": cli_record is not None,
                "gmsh_cli_command_pass": _command_ok(gmsh_command),
                "gmsh_versions_match": bool(gmsh.get("gmsh_version"))
                and gmsh.get("gmsh_version") == gmsh.get("gmsh_cli_version"),
                "gmsh_log_pass": any(
                    line.lower() == "status: pass" for line in gmsh_log_lines
                ),
                "current_time_window": _current_time_window(
                    gmsh.get("started_at"),
                    gmsh.get("ended_at"),
                    connection_started_at,
                ),
            },
        )

    # Gmsh -> SU2 text mesh.
    if gmsh is None:
        report["gmsh_to_su2_mesh"] = _not_run_section(
            "No current Gmsh manifest is available"
        )
    else:
        smoke_msh = _file_record(gmsh_dir / "smoke.msh", within=gmsh_dir)
        smoke_su2 = _file_record(gmsh_dir / "smoke.su2", within=gmsh_dir)
        validation = gmsh.get("su2_validation")
        validation = validation if isinstance(validation, Mapping) else {}
        markers = validation.get("markers")
        markers = markers if isinstance(markers, Mapping) else {}
        quality = validation.get("quality")
        quality = quality if isinstance(quality, Mapping) else {}
        physical_names = {
            entry.get("name")
            for entry in gmsh.get("markers", [])
            if isinstance(entry, Mapping)
        }
        expected_mesh_hash = gmsh.get("output_sha256", {}).get("smoke.su2")
        cross_hash_ok = True
        cross_path_ok = True
        if su2 is not None:
            cross_hash_ok = bool(
                expected_mesh_hash
                and expected_mesh_hash == su2.get("source_mesh_sha256")
                and expected_mesh_hash == su2.get("mesh_sha256")
            )
            source_path = Path(str(su2.get("source_mesh_path", ""))).resolve(
                strict=False
            )
            cross_path_ok = source_path == (gmsh_dir / "smoke.su2").resolve(
                strict=False
            )
        gmsh_mesh_log_lines = _extract_log_lines(
            gmsh_dir / "gmsh.log",
            (
                r"Writing.*smoke\.su2",
                r"Writing\s+\d+\s+elements",
                r"Done writing.*smoke\.su2",
                r"status:\s*PASS",
            ),
        )
        evidence = {
            "executable_paths": {"gmsh_cli": gmsh.get("gmsh_cli_path")},
            "software_versions": {"gmsh": gmsh.get("gmsh_version")},
            "commands": [gmsh.get("gmsh_cli_command")],
            "return_codes": [
                gmsh.get("gmsh_cli_command", {}).get("returncode")
                if isinstance(gmsh.get("gmsh_cli_command"), Mapping)
                else None
            ],
            "input_files": [],
            "output_files": [
                record for record in (smoke_msh, smoke_su2) if record is not None
            ],
            "sha256": {
                "smoke.msh": None if smoke_msh is None else smoke_msh["sha256"],
                "smoke.su2": None if smoke_su2 is None else smoke_su2["sha256"],
            },
            "mesh_counts": {
                "ndime": validation.get("ndime"),
                "nelem": validation.get("nelem"),
                "npoin": validation.get("npoin"),
                "nmark": validation.get("nmark"),
                "farfield_elements": markers.get("farfield"),
            },
            "physical_group_names": sorted(
                name for name in physical_names if isinstance(name, str)
            ),
            "key_log_lines": gmsh_mesh_log_lines,
            "started_at": gmsh.get("started_at"),
            "ended_at": gmsh.get("ended_at"),
            "warnings": [],
        }
        report["gmsh_to_su2_mesh"] = _section(
            evidence,
            {
                "gmsh_manifest_pass": gmsh.get("status") == "PASS",
                "smoke_msh_exists": smoke_msh is not None,
                "smoke_su2_exists": smoke_su2 is not None,
                "smoke_su2_hash_matches_manifest": smoke_su2 is not None
                and smoke_su2["sha256"] == expected_mesh_hash,
                "ndime_is_2": validation.get("ndime") == 2,
                "positive_counts": all(
                    isinstance(validation.get(name), int) and validation.get(name) > 0
                    for name in ("nelem", "npoin", "nmark")
                ),
                "farfield_marker_present": isinstance(markers.get("farfield"), int)
                and markers.get("farfield") > 0,
                "named_physical_groups": {"farfield", "fluid"}.issubset(
                    physical_names
                ),
                "mesh_quality_pass": quality.get("status") == "PASS"
                and quality.get("finite") is True,
                "cross_stage_hash_match": cross_hash_ok,
                "cross_stage_source_path_match": cross_path_ok,
            },
        )

    # Python -> SU2_CFD.
    if su2 is None:
        report["python_to_su2"] = (
            _section(
                {
                    "execution_state": "SU2_STAGE_FAILED",
                    "manifest_path": str(su2_dir / "run_manifest.json"),
                },
                {"current_su2_manifest_available": False},
            )
            if attempted.get("su2", False)
            else _not_run_section("SU2 was not run because Gmsh did not pass")
        )
    else:
        solver_record = None
        solver_command_file = _file_record(
            su2.get("solver_command_log"), within=su2_dir
        )
        if solver_command_file is not None:
            try:
                solver_record = _load_json(Path(solver_command_file["path"]))
            except (OSError, ValueError, json.JSONDecodeError):
                solver_record = None
        version_command = su2.get("su2_version_evidence")
        history_record = _file_record(su2.get("history_path"), within=su2_dir)
        config_record = _file_record(su2.get("config_path"), within=su2_dir)
        local_mesh_record = _file_record(su2.get("mesh_path"), within=su2_dir)
        solver_stdout = _file_record(su2.get("solver_stdout_log"), within=su2_dir)
        solver_stderr = _file_record(su2.get("solver_stderr_log"), within=su2_dir)
        solver_log_lines = _extract_log_lines(
            Path(str(su2.get("solver_stdout_log", ""))),
            (
                r"grid points",
                r"volume elements",
                r"Marker\s*=\s*farfield",
                r"ITER\s*=\s*5",
                r"Exit Success \(SU2_CFD\)",
            ),
        )
        expected_points = gmsh.get("node_count") if gmsh is not None else None
        expected_elements = gmsh.get("element_count") if gmsh is not None else None
        solver_command = (
            solver_record.get("command") if isinstance(solver_record, Mapping) else None
        )
        exact_serial_command = bool(
            isinstance(solver_command, list)
            and len(solver_command) == 2
            and solver_command[0] == su2.get("su2_cfd_path")
            and solver_command[1] == "smoke.cfg"
        )
        output_text = "\n".join(solver_log_lines).lower()
        evidence = {
            "executable_paths": {
                "SU2_CFD": su2.get("su2_cfd_path"),
                "SU2_SOL_optional": su2.get("su2_sol_path"),
                "MPI_optional": su2.get("mpiexec_path"),
            },
            "software_versions": {"SU2": su2.get("su2_version")},
            "commands": [
                command
                for command in (version_command, solver_record, su2.get("su2_sol_command"))
                if isinstance(command, Mapping)
            ],
            "return_codes": [
                command.get("returncode")
                for command in (version_command, solver_record, su2.get("su2_sol_command"))
                if isinstance(command, Mapping)
            ],
            "input_files": [
                record for record in (config_record, local_mesh_record) if record is not None
            ],
            "output_files": [
                record
                for record in (
                    history_record,
                    solver_stdout,
                    solver_stderr,
                    solver_command_file,
                )
                if record is not None
            ],
            "sha256": {
                "config": None if config_record is None else config_record["sha256"],
                "mesh": None if local_mesh_record is None else local_mesh_record["sha256"],
                "history": None if history_record is None else history_record["sha256"],
            },
            "iterations": su2.get("iterations"),
            "key_log_lines": solver_log_lines,
            "started_at": su2.get("start_time"),
            "ended_at": su2.get("end_time"),
            "warnings": su2.get("nonfatal_diagnostics", []),
            "diagnostic_classifications": su2.get(
                "diagnostic_classifications", []
            ),
        }
        sol_command = su2.get("su2_sol_command")
        report["python_to_su2"] = _section(
            evidence,
            {
                "su2_manifest_pass": su2.get("status") == "PASS"
                and su2.get("error") is None,
                "su2_executable_exists": _file_record(su2.get("su2_cfd_path"))
                is not None,
                "su2_version_present": isinstance(su2.get("su2_version"), str)
                and bool(su2.get("su2_version")),
                "version_command_pass": _command_ok(version_command),
                "solver_command_pass": _command_ok(solver_record),
                "serial_command_exact": exact_serial_command,
                "return_code_zero": su2.get("return_code") == 0,
                "not_timed_out": su2.get("timed_out") is False,
                "five_iterations_requested": su2.get("requested_iterations") == 5,
                "five_iterations_completed": su2.get("iterations") == 5,
                "history_exists": history_record is not None
                and history_record["size_bytes"] > 0,
                "config_hash_matches": config_record is not None
                and config_record["sha256"] == su2.get("config_sha256"),
                "mesh_hash_matches": local_mesh_record is not None
                and local_mesh_record["sha256"] == su2.get("mesh_sha256")
                and su2.get("mesh_sha256") == su2.get("source_mesh_sha256"),
                "no_fatal_matches": su2.get("fatal_matches") == [],
                "su2_sol_command_pass_if_used": sol_command is None
                or _command_ok(sol_command),
                "log_confirms_grid_points": expected_points is not None
                and f"{expected_points} grid points" in output_text,
                "log_confirms_volume_elements": expected_elements is not None
                and f"{expected_elements} volume elements" in output_text,
                "log_confirms_farfield": "marker = farfield" in output_text,
                "log_confirms_five_iterations": "iter = 5" in output_text,
                "log_confirms_success": "exit success (su2_cfd)" in output_text,
                "current_time_window": _current_time_window(
                    su2.get("start_time"),
                    su2.get("end_time"),
                    connection_started_at,
                ),
            },
        )

    # SU2 -> ParaView-readable file.
    if su2 is None:
        report["su2_to_paraview_file"] = _not_run_section(
            "No current SU2 manifest is available"
        )
    else:
        visualization = _file_record(su2.get("visualization_file"), within=su2_dir)
        validation = su2.get("visualization_validation")
        validation = validation if isinstance(validation, Mapping) else {}
        visualization_path = (
            None if visualization is None else Path(visualization["path"])
        )
        pv_cross_ok = True
        if paraview is not None and visualization is not None:
            pv_cross_ok = bool(
                Path(str(paraview.get("input_solution_path", ""))).resolve(
                    strict=False
                )
                == visualization_path
                and paraview.get("input_solution_sha256")
                == visualization.get("sha256")
            )
        mesh_validation = su2.get("mesh_validation")
        mesh_validation = (
            mesh_validation if isinstance(mesh_validation, Mapping) else {}
        )
        evidence = {
            "executable_paths": {"SU2_CFD": su2.get("su2_cfd_path")},
            "software_versions": {"SU2": su2.get("su2_version")},
            "commands": [
                command
                for command in (su2.get("command"), su2.get("su2_sol_command"))
                if command is not None
            ],
            "return_codes": [
                su2.get("return_code"),
                su2.get("su2_sol_return_code"),
            ],
            "input_files": [
                record
                for record in (_file_record(su2.get("mesh_path"), within=su2_dir),)
                if record is not None
            ],
            "output_files": [visualization] if visualization is not None else [],
            "sha256": {
                "visualization_file": None
                if visualization is None
                else visualization["sha256"]
            },
            "visualization_validation": dict(validation),
            "key_log_lines": _extract_log_lines(
                Path(str(su2.get("solver_stdout_log", ""))),
                (r"Writing.*PARAVIEW", r"Exit Success \(SU2_CFD\)"),
            ),
            "started_at": su2.get("start_time"),
            "ended_at": su2.get("end_time"),
            "warnings": su2.get("nonfatal_diagnostics", []),
        }
        report["su2_to_paraview_file"] = _section(
            evidence,
            {
                "su2_manifest_pass": su2.get("status") == "PASS",
                "visualization_exists": visualization is not None
                and visualization["size_bytes"] > 0,
                "supported_suffix": visualization_path is not None
                and visualization_path.suffix.lower()
                in _SUPPORTED_VISUALIZATION_SUFFIXES,
                "visualization_hash_matches": visualization is not None
                and visualization["sha256"] == validation.get("sha256"),
                "visualization_is_finite": validation.get("finite") is True,
                "positive_point_cell_counts": all(
                    isinstance(validation.get(name), int) and validation.get(name) > 0
                    for name in ("point_count", "cell_count")
                ),
                "counts_match_mesh": validation.get("point_count")
                == mesh_validation.get("npoin")
                and validation.get("cell_count") == mesh_validation.get("nelem"),
                "paraview_input_cross_check": pv_cross_ok,
            },
        )

    # Python -> pvbatch.  The optional --help capability probe is evidence and
    # a warning, but never a critical command.  The real probe script is the
    # process-connection gate.
    if paraview is None:
        report["python_to_pvbatch"] = (
            _section(
                {
                    "execution_state": "PARAVIEW_STAGE_FAILED_BEFORE_MANIFEST",
                    "manifest_path": str(paraview_dir / "paraview_manifest.json"),
                },
                {"current_paraview_manifest_available": False},
            )
            if attempted.get("paraview", False)
            else _not_run_section("pvbatch was not run because SU2 did not pass")
        )
    else:
        pv_commands = paraview.get("commands")
        pv_commands = pv_commands if isinstance(pv_commands, list) else []
        command_by_script = {
            name: command
            for command in pv_commands
            if (name := _command_script_name(command)) is not None
        }
        probe_command = command_by_script.get("probe_pvbatch.py")
        probe_record = _file_record(paraview_dir / "pvbatch_probe.json", within=paraview_dir)
        probe_payload: dict[str, Any] | None = None
        if probe_record is not None:
            try:
                probe_payload = _load_json(Path(probe_record["path"]))
            except (OSError, ValueError, json.JSONDecodeError):
                probe_payload = None
        pvbatch_record = _file_record(paraview.get("pvbatch_path"))
        help_commands = [
            command
            for command in pv_commands
            if isinstance(command, Mapping) and command.get("args") == ["--help"]
        ]
        evidence = {
            "executable_paths": {"pvbatch": paraview.get("pvbatch_path")},
            "software_versions": {
                "ParaView": paraview.get("paraview_version")
            },
            "commands": [
                {**dict(command), "critical": _command_script_name(command) is not None}
                for command in pv_commands
                if isinstance(command, Mapping)
            ],
            "return_codes": [
                command.get("returncode")
                for command in pv_commands
                if isinstance(command, Mapping)
            ],
            "input_files": [],
            "output_files": [probe_record] if probe_record is not None else [],
            "sha256": {
                "pvbatch": None
                if pvbatch_record is None
                else pvbatch_record["sha256"],
                "probe_json": None if probe_record is None else probe_record["sha256"],
            },
            "key_log_lines": _extract_log_lines(
                paraview_dir / "pvbatch.probe.stderr.log",
                (r"error", r"fatal", r"traceback"),
            ),
            "probe_payload": probe_payload,
            "started_at": paraview.get("started_at"),
            "ended_at": paraview.get("ended_at"),
            "warnings": paraview.get("warnings", []),
            "noncritical_help_commands": help_commands,
        }
        probe_python = (
            None if probe_payload is None else probe_payload.get("python_executable")
        )
        probe_python_matches = False
        if isinstance(probe_python, str) and isinstance(
            paraview.get("pvbatch_path"), str
        ):
            probe_python_matches = os.path.normcase(
                str(Path(probe_python).resolve(strict=False))
            ) == os.path.normcase(
                str(Path(paraview["pvbatch_path"]).resolve(strict=False))
            )
        report["python_to_pvbatch"] = _section(
            evidence,
            {
                "pvbatch_executable_exists": pvbatch_record is not None,
                "pvbatch_hash_matches": pvbatch_record is not None
                and pvbatch_record["sha256"] == paraview.get("pvbatch_sha256"),
                "probe_command_present": probe_command is not None,
                "probe_command_pass": _command_ok(probe_command),
                "probe_json_valid": probe_payload is not None
                and probe_payload.get("status") == "PASS",
                "probe_uses_pvbatch_interpreter": probe_python_matches,
                "paraview_version_present": isinstance(
                    paraview.get("paraview_version"), str
                )
                and bool(paraview.get("paraview_version")),
                "probe_version_matches": probe_payload is not None
                and probe_payload.get("paraview_version")
                == paraview.get("paraview_version"),
            },
        )

    # pvbatch -> JSON/CSV results.
    if paraview is None:
        report["pvbatch_to_results"] = _not_run_section(
            "No current ParaView manifest is available"
        )
    else:
        pv_commands = paraview.get("commands")
        pv_commands = pv_commands if isinstance(pv_commands, list) else []
        command_by_script = {
            name: command
            for command in pv_commands
            if (name := _command_script_name(command)) is not None
        }
        inspect_command = command_by_script.get("inspect_solution.py")
        postprocess_command = command_by_script.get("smoke_postprocess.py")
        inventory_record = _file_record(
            paraview_dir / "solution_inventory.json", within=paraview_dir
        )
        postprocess_record = _file_record(
            paraview_dir / "smoke_postprocess.json", within=paraview_dir
        )
        csv_record = _file_record(
            paraview_dir / "smoke_integral.csv", within=paraview_dir
        )
        inventory = None
        postprocess = None
        try:
            if inventory_record is not None:
                inventory = _load_json(Path(inventory_record["path"]))
            if postprocess_record is not None:
                postprocess = _load_json(Path(postprocess_record["path"]))
        except (OSError, ValueError, json.JSONDecodeError):
            inventory = None
            postprocess = None
        csv_rows: list[list[str]] = []
        if csv_record is not None:
            try:
                with Path(csv_record["path"]).open(
                    "r", encoding="utf-8-sig", newline=""
                ) as stream:
                    csv_rows = list(csv.reader(stream))
            except (OSError, csv.Error, UnicodeError):
                csv_rows = []
        output_records_ok = True
        manifest_outputs = paraview.get("output_files")
        if not isinstance(manifest_outputs, list):
            output_records_ok = False
            manifest_outputs = []
        else:
            for declared in manifest_outputs:
                if not isinstance(declared, Mapping):
                    output_records_ok = False
                    break
                current = _file_record(declared.get("path"), within=paraview_dir)
                if (
                    current is None
                    or current["sha256"] != declared.get("sha256")
                    or current["size_bytes"] != declared.get("size_bytes")
                ):
                    output_records_ok = False
                    break
        visualization_validation = (
            su2.get("visualization_validation", {}) if su2 is not None else {}
        )
        inventory_input = (
            None if inventory is None else inventory.get("input_file")
        )
        postprocess_input = (
            None if postprocess is None else postprocess.get("input_file")
        )
        expected_input = None if su2 is None else su2.get("visualization_file")
        versions = {
            value
            for value in (
                paraview.get("paraview_version"),
                None if inventory is None else inventory.get("paraview_version"),
                None
                if postprocess is None
                else postprocess.get("paraview_version"),
            )
            if value is not None
        }
        evidence = {
            "executable_paths": {"pvbatch": paraview.get("pvbatch_path")},
            "software_versions": {
                "ParaView": paraview.get("paraview_version")
            },
            "commands": [
                command
                for command in (inspect_command, postprocess_command)
                if isinstance(command, Mapping)
            ],
            "return_codes": [
                command.get("returncode")
                for command in (inspect_command, postprocess_command)
                if isinstance(command, Mapping)
            ],
            "input_files": [
                record
                for record in (
                    _file_record(expected_input, within=su2_dir)
                    if expected_input is not None
                    else None,
                )
                if record is not None
            ],
            "output_files": [
                record
                for record in (inventory_record, postprocess_record, csv_record)
                if record is not None
            ],
            "sha256": {
                "solution_inventory.json": None
                if inventory_record is None
                else inventory_record["sha256"],
                "smoke_postprocess.json": None
                if postprocess_record is None
                else postprocess_record["sha256"],
                "smoke_integral.csv": None
                if csv_record is None
                else csv_record["sha256"],
            },
            "dataset_type": None if inventory is None else inventory.get("dataset_type"),
            "number_of_points": None
            if inventory is None
            else inventory.get("number_of_points"),
            "number_of_cells": None
            if inventory is None
            else inventory.get("number_of_cells"),
            "detected_arrays": paraview.get("detected_arrays", {}),
            "key_log_lines": [
                f"solution_inventory status={None if inventory is None else inventory.get('status')}",
                f"smoke_postprocess status={None if postprocess is None else postprocess.get('status')}",
                f"smoke_integral data_rows={max(0, len(csv_rows) - 1)}",
            ],
            "started_at": paraview.get("started_at"),
            "ended_at": paraview.get("ended_at"),
            "warnings": paraview.get("warnings", []),
        }
        report["pvbatch_to_results"] = _section(
            evidence,
            {
                "paraview_manifest_pass": paraview.get("status") == "PASS"
                and paraview.get("error") is None,
                "inspect_command_pass": _command_ok(inspect_command),
                "postprocess_command_pass": _command_ok(postprocess_command),
                "inventory_json_valid": inventory is not None
                and inventory.get("status") == "PASS"
                and _json_is_finite(inventory),
                "postprocess_json_valid": postprocess is not None
                and postprocess.get("status") == "PASS"
                and _json_is_finite(postprocess),
                "inputs_match_su2_visualization": expected_input is not None
                and inventory_input == expected_input
                and postprocess_input == expected_input,
                "paraview_versions_match": len(versions) == 1,
                "dataset_type_present": inventory is not None
                and isinstance(inventory.get("dataset_type"), str)
                and bool(inventory.get("dataset_type")),
                "positive_point_cell_counts": inventory is not None
                and isinstance(inventory.get("number_of_points"), int)
                and inventory.get("number_of_points") > 0
                and isinstance(inventory.get("number_of_cells"), int)
                and inventory.get("number_of_cells") > 0,
                "inspect_postprocess_counts_match": inventory is not None
                and postprocess is not None
                and inventory.get("number_of_points")
                == postprocess.get("total_points")
                and inventory.get("number_of_cells")
                == postprocess.get("total_cells"),
                "counts_match_su2_visualization": inventory is not None
                and inventory.get("number_of_points")
                == visualization_validation.get("point_count")
                and inventory.get("number_of_cells")
                == visualization_validation.get("cell_count"),
                "integral_csv_has_data": len(csv_rows) >= 2
                and bool(csv_rows[0]),
                "manifest_output_hashes_match": output_records_ok,
                "input_hash_unchanged": su2 is not None
                and paraview.get("input_solution_sha256")
                == visualization_validation.get("sha256"),
                "current_time_window": _current_time_window(
                    paraview.get("started_at"),
                    paraview.get("ended_at"),
                    connection_started_at,
                ),
            },
        )

    report["overall"] = (
        "PASS"
        if all(report[key]["status"] == "PASS" for key in CONNECTION_KEYS)
        else "FAIL"
    )
    warnings: list[Any] = []
    if tools is not None:
        warnings.extend(tools.get("warnings", []))
    if su2 is not None:
        warnings.extend(su2.get("nonfatal_diagnostics", []))
    if paraview is not None:
        warnings.extend(paraview.get("warnings", []))
    report["metadata"] = {
        "schema_version": 1,
        "started_at": connection_started_at,
        "ended_at": connection_ended_at,
        "output_root": str(root),
        "pipeline_log": None if pipeline_log is None else str(pipeline_log),
        "stage_attempted": dict(attempted),
        "warnings": warnings,
        "failure": None
        if failure is None
        else {
            "type": type(failure).__name__,
            "message": str(failure),
        },
    }
    return report


def _render_connection_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# CFD connection report",
        "",
        f"Overall: **{report.get('overall', 'FAIL')}**",
        "",
        "| Connection | Status |",
        "|---|---|",
    ]
    for key in CONNECTION_KEYS:
        status = report.get(key, {}).get("status", "FAIL")
        lines.append(f"| `{key}` | **{status}** |")
    metadata = report.get("metadata", {})
    lines.extend(
        (
            "",
            "## Run metadata",
            "",
            f"- Started: `{metadata.get('started_at')}`",
            f"- Ended: `{metadata.get('ended_at')}`",
            f"- Output root: `{metadata.get('output_root')}`",
            f"- Pipeline log: `{metadata.get('pipeline_log')}`",
        )
    )
    warnings = metadata.get("warnings", [])
    if warnings:
        lines.extend(("", "## Preserved warnings", ""))
        lines.extend(f"- {warning}" for warning in warnings)
    for key in CONNECTION_KEYS:
        section = report.get(key, {})
        lines.extend(("", f"## {key}", "", f"Status: **{section.get('status')}**"))
        evidence_items = section.get("evidence", [])
        if evidence_items and isinstance(evidence_items[0], Mapping):
            evidence = evidence_items[0]
            failed = evidence.get("failed_checks", [])
            if failed:
                lines.append("\nFailed checks: " + ", ".join(f"`{item}`" for item in failed))
            outputs = evidence.get("output_files", [])
            if outputs:
                lines.append("\nOutputs:")
                for output in outputs:
                    if isinstance(output, Mapping):
                        lines.append(
                            f"- `{output.get('path')}` — SHA256 `{output.get('sha256')}`"
                        )
            key_lines = evidence.get("key_log_lines", [])
            if key_lines:
                lines.append("\nKey evidence lines:")
                lines.extend(f"- `{line}`" for line in key_lines)
    return "\n".join(lines) + "\n"


def write_connection_reports(
    output_directory: str | os.PathLike[str],
    report: Mapping[str, Any],
) -> tuple[Path, Path]:
    root = Path(output_directory).expanduser().resolve(strict=True)
    json_path = root / _REPORT_JSON
    markdown_path = root / _REPORT_MARKDOWN
    contents = {
        json_path: json.dumps(
            report, indent=2, ensure_ascii=False, allow_nan=False
        )
        + "\n",
        markdown_path: _render_connection_markdown(report),
    }
    temporary_paths: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    try:
        for destination, content in contents.items():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=root
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_paths[destination] = temporary
        for destination in contents:
            if _lexists(destination):
                if _is_reparse(destination) or not destination.is_file():
                    raise ValueError(f"unsafe report destination: {destination}")
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".backup", dir=root
                )
                os.close(descriptor)
                backup = Path(backup_name)
                os.replace(destination, backup)
                backups[destination] = backup
        for destination, temporary in temporary_paths.items():
            os.replace(temporary, destination)
            installed.append(destination)
    except BaseException as write_error:
        rollback_errors: list[str] = []
        for destination in reversed(installed):
            try:
                destination.unlink()
            except OSError as rollback_error:
                rollback_errors.append(f"remove {destination}: {rollback_error}")
        for destination, backup in backups.items():
            try:
                os.replace(backup, destination)
            except OSError as rollback_error:
                rollback_errors.append(f"restore {destination}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                f"report write failed ({write_error}); rollback failed: "
                + " | ".join(rollback_errors)
            ) from write_error
        raise
    finally:
        for temporary in temporary_paths.values():
            if _lexists(temporary):
                temporary.unlink()
    for backup in backups.values():
        if _lexists(backup):
            backup.unlink()
    return json_path, markdown_path


def _report_virtual_files(
    output_directory: Path,
    report: Mapping[str, Any],
) -> dict[Path, tuple[int, str]]:
    json_bytes = (
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    markdown_bytes = _render_connection_markdown(report).encode("utf-8")
    return {
        output_directory / _REPORT_JSON: (
            len(json_bytes),
            hashlib.sha256(json_bytes).hexdigest(),
        ),
        output_directory / _REPORT_MARKDOWN: (
            len(markdown_bytes),
            hashlib.sha256(markdown_bytes).hexdigest(),
        ),
    }


def _collect_owned_paths(
    root: Path,
    state: Mapping[str, Any],
    *,
    pipeline_log: Path | None,
    clean_payload: Mapping[str, Any] | None,
) -> set[Path]:
    paths: set[Path] = set()
    for relative in _BOOTSTRAP_MANAGED_FILES:
        path = root / relative
        if _lexists(path) and path.name != _OWNERSHIP_LEDGER:
            paths.add(path)
    if pipeline_log is not None:
        paths.add(pipeline_log)

    def add_command_paths(command: Any) -> None:
        if not isinstance(command, Mapping):
            return
        for key in ("stdout_log", "stderr_log", "metadata_log"):
            value = command.get(key)
            if isinstance(value, str):
                paths.add(Path(value))
        stdout_value = command.get("stdout_log")
        if isinstance(stdout_value, str) and stdout_value.endswith(".stdout.log"):
            inferred = Path(stdout_value[: -len(".stdout.log")] + ".json")
            if _lexists(inferred):
                paths.add(inferred)

    manifests = state.get("manifests", {})
    gmsh = manifests.get("gmsh") if isinstance(manifests, Mapping) else None
    if isinstance(gmsh, Mapping):
        add_command_paths(gmsh.get("gmsh_cli_command"))
    su2 = manifests.get("su2") if isinstance(manifests, Mapping) else None
    if isinstance(su2, Mapping):
        add_command_paths(su2.get("su2_version_evidence"))
        add_command_paths(su2.get("su2_sol_command"))
        for key in (
            "solver_stdout_log",
            "solver_stderr_log",
            "solver_command_log",
            "history_path",
            "visualization_file",
            "config_path",
            "mesh_path",
            "restart_file",
            "su2_sol_config_path",
        ):
            value = su2.get(key)
            if isinstance(value, str):
                paths.add(Path(value))
    paraview = manifests.get("paraview") if isinstance(manifests, Mapping) else None
    if isinstance(paraview, Mapping):
        for command in paraview.get("commands", []):
            add_command_paths(command)
        for record in paraview.get("output_files", []):
            if isinstance(record, Mapping) and isinstance(record.get("path"), str):
                paths.add(Path(record["path"]))
    if isinstance(clean_payload, Mapping):
        for relative in clean_payload.get("retained_quarantine_files", []):
            if isinstance(relative, str):
                paths.add(root / _validate_relative_owned_path(relative))
    return paths


class ConnectionTestRunner:
    """Run the real small Gmsh -> SU2 -> pvbatch connection workflow."""

    def __init__(
        self,
        *,
        output_directory: str | os.PathLike[str],
        allowed_output_root: str | os.PathLike[str],
        trusted_repository_root: str | os.PathLike[str],
        toolchain: Toolchain,
        paraview_script_directory: str | os.PathLike[str],
        nproc: int = 1,
        timeout_seconds: float = 300.0,
        live_output: bool = True,
        controller_argv: Sequence[str] = (),
    ) -> None:
        if nproc != 1:
            raise ValueError("connect-test is a serial quality gate; --nproc must be 1")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(
            timeout_seconds, bool
        ):
            raise TypeError("timeout_seconds must be a real number")
        if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        self.output_directory = Path(output_directory)
        self.allowed_output_root = Path(allowed_output_root)
        self.trusted_repository_root = Path(trusted_repository_root)
        self.toolchain = toolchain
        self.paraview_script_directory = Path(
            paraview_script_directory
        ).expanduser().resolve(strict=True)
        self.nproc = nproc
        self.timeout_seconds = float(timeout_seconds)
        self.live_output = bool(live_output)
        self.controller_argv = tuple(str(value) for value in controller_argv)

    def _runner(self, output_dir: Path) -> CommandRunner:
        return CommandRunner(
            output_dir=output_dir,
            live_output=self.live_output,
            allow_mnt_c_executables=self.toolchain.allow_mnt_c_executables,
        )

    def run(self, *, clean: bool = False) -> ConnectionTestResult:
        started_at = _utc_now()
        root = _assert_safe_root(
            self.output_directory,
            self.allowed_output_root,
            self.trusted_repository_root,
        )
        state: dict[str, Any] = {
            "attempted": {
                "tools": False,
                "gmsh": False,
                "su2": False,
                "paraview": False,
            },
            "manifests": {},
            "resolved_tools": {},
        }
        pipeline_log: Path | None = None
        pipeline_failure: BaseException | None = None
        clean_payload: dict[str, Any] | None = None

        if clean:
            try:
                clean_payload = clean_connection_artifacts(
                    root,
                    allowed_root=self.allowed_output_root,
                    trusted_root=self.trusted_repository_root,
                )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                pipeline_failure = error
                if not _report_paths_are_safe(root):
                    raise ConnectionTestError(
                        f"connection artifact clean failed and report path is unsafe: {error}",
                        cause=error,
                    ) from error
                _assert_safe_root(
                    self.output_directory,
                    self.allowed_output_root,
                    self.trusted_repository_root,
                )
                report = build_connection_report(
                    root,
                    state,
                    connection_started_at=started_at,
                    connection_ended_at=_utc_now(),
                    failure=error,
                )
                report_json, _ = write_connection_reports(root, report)
                raise ConnectionTestError(
                    f"connection artifact clean failed: {error}",
                    report_path=report_json,
                    cause=error,
                ) from error

        try:
            _preflight_connection_stage_paths(root)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise ConnectionTestError(
                f"connection output preflight failed: {error}",
                cause=error,
            ) from error

        # Replace any stale PASS report with a current FAIL placeholder before
        # external stages start.  A later ledger/report write failure can never
        # leave an old PASS on disk.
        initial_report = build_connection_report(
            root,
            state,
            connection_started_at=started_at,
            connection_ended_at=_utc_now(),
        )
        write_connection_reports(root, initial_report)

        tools_dir = root / "tools"
        gmsh_dir = root / "gmsh"
        su2_dir = root / "su2"
        paraview_dir = root / "paraview"

        def tools_stage() -> None:
            state["attempted"]["tools"] = True
            try:
                manifest, resolved = check_connection_tools(
                    self.toolchain,
                    tools_dir,
                    controller_argv=self.controller_argv,
                )
                state["manifests"]["tools"] = manifest
                state["resolved_tools"] = resolved
            finally:
                manifest_path = tools_dir / "toolchain_manifest.json"
                if (
                    "tools" not in state["manifests"]
                    and _lexists(manifest_path)
                    and not _is_reparse(manifest_path)
                    and manifest_path.is_file()
                ):
                    state["manifests"]["tools"] = _load_json(manifest_path)
            manifest = state["manifests"].get("tools")
            if not isinstance(manifest, Mapping) or manifest.get("status") != "PASS":
                raise ConnectionTestError("tool check manifest is not PASS")

        def gmsh_stage() -> None:
            state["attempted"]["gmsh"] = True
            try:
                manifest = GmshBridge(
                    runner=self._runner(gmsh_dir),
                    toolchain=self.toolchain,
                ).smoke(
                    output_directory=gmsh_dir,
                    timeout=self.timeout_seconds,
                )
                state["manifests"]["gmsh"] = manifest
            finally:
                manifest_path = gmsh_dir / "gmsh_manifest.json"
                if (
                    "gmsh" not in state["manifests"]
                    and _lexists(manifest_path)
                    and not _is_reparse(manifest_path)
                    and manifest_path.is_file()
                ):
                    state["manifests"]["gmsh"] = _load_json(manifest_path)
            manifest = state["manifests"].get("gmsh")
            if not isinstance(manifest, Mapping) or manifest.get("status") != "PASS":
                raise ConnectionTestError("Gmsh stage manifest is not PASS")

        def su2_stage() -> None:
            state["attempted"]["su2"] = True
            resolved = state["resolved_tools"]
            su2_cfd = resolved.get("su2_cfd")
            if not isinstance(su2_cfd, ResolvedTool):
                raise ConnectionTestError("SU2_CFD was not resolved by the tools stage")
            su2_sol = resolved.get("su2_sol")
            mpiexec = resolved.get("mpiexec")
            try:
                manifest = SU2Bridge(
                    self._runner(su2_dir),
                    su2_cfd=su2_cfd.path,
                    su2_sol=(
                        su2_sol.path if isinstance(su2_sol, ResolvedTool) else None
                    ),
                    mpiexec=(
                        mpiexec.path if isinstance(mpiexec, ResolvedTool) else None
                    ),
                ).run_smoke_case(
                    gmsh_dir / "smoke.su2",
                    su2_dir,
                    nproc=1,
                    max_iterations=5,
                    timeout_seconds=self.timeout_seconds,
                    live_output=self.live_output,
                )
                state["manifests"]["su2"] = manifest
            finally:
                manifest_path = su2_dir / "run_manifest.json"
                if (
                    "su2" not in state["manifests"]
                    and _lexists(manifest_path)
                    and not _is_reparse(manifest_path)
                    and manifest_path.is_file()
                ):
                    state["manifests"]["su2"] = _load_json(manifest_path)
            manifest = state["manifests"].get("su2")
            if not isinstance(manifest, Mapping) or manifest.get("status") != "PASS":
                raise ConnectionTestError("SU2 stage manifest is not PASS")

        def paraview_stage() -> None:
            state["attempted"]["paraview"] = True
            resolved = state["resolved_tools"]
            pvbatch = resolved.get("pvbatch")
            if not isinstance(pvbatch, ResolvedTool):
                raise ConnectionTestError("pvbatch was not resolved by the tools stage")
            try:
                manifest = ParaViewBridge(
                    self._runner(paraview_dir),
                    pvbatch=pvbatch.path,
                    script_dir=self.paraview_script_directory,
                    pvbatch_source=pvbatch.source,
                ).inspect_manifest(
                    su2_dir / "run_manifest.json",
                    paraview_dir,
                    timeout_seconds=self.timeout_seconds,
                    live_output=self.live_output,
                )
                state["manifests"]["paraview"] = manifest
            finally:
                manifest_path = paraview_dir / "paraview_manifest.json"
                if (
                    "paraview" not in state["manifests"]
                    and _lexists(manifest_path)
                    and not _is_reparse(manifest_path)
                    and manifest_path.is_file()
                ):
                    state["manifests"]["paraview"] = _load_json(manifest_path)
            manifest = state["manifests"].get("paraview")
            if not isinstance(manifest, Mapping) or manifest.get("status") != "PASS":
                raise ConnectionTestError("ParaView stage manifest is not PASS")

        stages = (
            PipelineStage("tools", tools_stage),
            PipelineStage("gmsh", gmsh_stage),
            PipelineStage("su2", su2_stage),
            PipelineStage("paraview", paraview_stage),
        )
        try:
            pipeline_result = Pipeline(root).run(stages, name="connect-test")
            pipeline_log = pipeline_result.log_path
        except PipelineStageError as error:
            pipeline_failure = error
            pipeline_log = error.log_path

        report = build_connection_report(
            root,
            state,
            connection_started_at=started_at,
            connection_ended_at=_utc_now(),
            pipeline_log=pipeline_log,
            failure=pipeline_failure,
        )
        _assert_safe_root(
            self.output_directory,
            self.allowed_output_root,
            self.trusted_repository_root,
        )
        owned_paths = _collect_owned_paths(
            root,
            state,
            pipeline_log=pipeline_log,
            clean_payload=clean_payload,
        )
        report_json = root / _REPORT_JSON
        report_markdown = root / _REPORT_MARKDOWN
        if report.get("overall") == "PASS" and pipeline_failure is None:
            try:
                write_ownership_ledger(
                    root,
                    owned_paths,
                    trusted_root=self.trusted_repository_root,
                    virtual_files=_report_virtual_files(root, report),
                )
                report_json, report_markdown = write_connection_reports(root, report)
            except BaseException as finalization_error:
                if isinstance(finalization_error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise ConnectionTestError(
                    f"connection report finalization failed: {finalization_error}",
                    report_path=report_json,
                    cause=finalization_error,
                ) from finalization_error
        else:
            report_json, report_markdown = write_connection_reports(root, report)
            try:
                write_ownership_ledger(
                    root,
                    _collect_owned_paths(
                        root,
                        state,
                        pipeline_log=pipeline_log,
                        clean_payload=clean_payload,
                    ),
                    trusted_root=self.trusted_repository_root,
                )
            except BaseException as ledger_error:
                report["metadata"]["ownership_ledger_error"] = {
                    "type": type(ledger_error).__name__,
                    "message": str(ledger_error),
                }
                write_connection_reports(root, report)

        if pipeline_failure is not None or report.get("overall") != "PASS":
            message = (
                f"connection pipeline failed: {pipeline_failure}"
                if pipeline_failure is not None
                else "connection evidence quality gate did not pass"
            )
            raise ConnectionTestError(
                message,
                report_path=report_json,
                cause=pipeline_failure,
            ) from pipeline_failure

        if pipeline_log is None:
            raise ConnectionTestError(
                "connection pipeline produced no execution log",
                report_path=report_json,
            )
        return ConnectionTestResult(
            report=report,
            report_json=report_json,
            report_markdown=report_markdown,
            pipeline_log=pipeline_log,
        )


def run_connect_test(
    *,
    output_directory: str | os.PathLike[str],
    allowed_output_root: str | os.PathLike[str],
    trusted_repository_root: str | os.PathLike[str],
    toolchain: Toolchain,
    paraview_script_directory: str | os.PathLike[str],
    clean: bool = False,
    nproc: int = 1,
    timeout_seconds: float = 300.0,
    live_output: bool = True,
    controller_argv: Sequence[str] = (),
) -> ConnectionTestResult:
    """Convenience entry point used by the CLI."""

    return ConnectionTestRunner(
        output_directory=output_directory,
        allowed_output_root=allowed_output_root,
        trusted_repository_root=trusted_repository_root,
        toolchain=toolchain,
        paraview_script_directory=paraview_script_directory,
        nproc=nproc,
        timeout_seconds=timeout_seconds,
        live_output=live_output,
        controller_argv=controller_argv,
    ).run(clean=clean)


# ---------------------------------------------------------------------------
# Real-project, connection-scale pipeline
# ---------------------------------------------------------------------------

REAL_CONNECTION_KEYS = (
    "step_to_gmsh",
    "gmsh_to_su2_mesh",
    "mesh_to_su2",
    "su2_to_visualization_file",
    "visualization_file_to_pvbatch",
    "pvbatch_to_json",
)

_REAL_REPORT_JSON = "real_connection_report.json"
_REAL_PIPELINE_MANIFEST = "pipeline_manifest.json"
_REAL_MAX_ELEMENTS = 300_000
_REAL_NONFINITE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[+-]?(?:nan|inf(?:inity)?))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


class RealConnectionError(RuntimeError):
    """Raised only after a truthful real-project FAIL report is persisted."""

    def __init__(
        self,
        message: str,
        *,
        report_path: Path | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.report_path = report_path
        self.cause = cause


class RealConnectionInputError(ValueError):
    """One or more real-project source-of-truth inputs are unsafe or invalid."""

    def __init__(self, issues: Sequence[Mapping[str, Any]]) -> None:
        normalized = tuple(dict(issue) for issue in issues)
        message = "; ".join(
            f"{issue.get('code', 'INVALID_INPUT')}: {issue.get('message', '')}"
            for issue in normalized
        )
        super().__init__(message or "invalid real-project inputs")
        self.issues = normalized


class RealConnectionConfigurationGateError(ValueError):
    """A project boundary/configuration decision is not frozen for SU2."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RealConnectionInputs:
    repository_root: Path
    step_path: Path
    pipeline_geometry_path: Path
    project_path: Path
    cases_path: Path
    markers_path: Path
    project: dict[str, Any]
    cases: tuple[dict[str, Any], ...]
    markers: dict[str, dict[str, Any]]
    measurements: dict[str, dict[str, Any]]
    marker_document: dict[str, Any]
    requested_case_id: str
    input_records: dict[str, dict[str, Any]]
    topology_smoke_path: Path
    topology_smoke_refinement: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RealConnectionResult:
    report: dict[str, Any]
    report_json: Path
    pipeline_manifest: Path


def _path_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _read_only(details: os.stat_result) -> bool:
    readonly_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    attributes = getattr(details, "st_file_attributes", 0)
    if readonly_flag and attributes & readonly_flag:
        return True
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(details.st_mode & write_bits)


def _input_file_record(
    value: str | os.PathLike[str],
    *,
    label: str,
    allowed_parent: Path,
    repository_root: Path,
    issues: list[dict[str, Any]],
    require_read_only: bool = False,
) -> tuple[Path, dict[str, Any]]:
    raw = Path(value).expanduser()
    lexical = Path(os.path.abspath(raw))
    record: dict[str, Any] = {
        "requested_path": str(raw),
        "path": str(lexical),
        "exists": False,
        "size_bytes": None,
        "sha256": None,
        "read_only": None,
    }
    if ".." in raw.parts:
        issues.append(
            {
                "code": f"UNSAFE_{label.upper()}_PATH",
                "message": f"{label} path must not contain '..'",
                "path": str(raw),
            }
        )
    if not _path_within(lexical, allowed_parent):
        issues.append(
            {
                "code": f"{label.upper()}_OUTSIDE_ALLOWED_DIRECTORY",
                "message": f"{label} must stay under {allowed_parent}",
                "path": str(lexical),
            }
        )
        return lexical, record

    try:
        relative = lexical.relative_to(repository_root)
    except ValueError:
        issues.append(
            {
                "code": f"{label.upper()}_OUTSIDE_REPOSITORY",
                "message": f"{label} resolves outside repository",
                "path": str(lexical),
            }
        )
        return lexical, record

    current = repository_root
    for component in relative.parts:
        current = current / component
        if _lexists(current) and _is_reparse(current):
            issues.append(
                {
                    "code": f"UNSAFE_{label.upper()}_REPARSE_PATH",
                    "message": f"{label} path crosses a link/junction/reparse point",
                    "path": str(current),
                }
            )
            return lexical, record

    if not _lexists(lexical):
        issues.append(
            {
                "code": f"MISSING_{label.upper()}",
                "message": f"required {label} file does not exist",
                "path": str(lexical),
            }
        )
        return lexical, record
    try:
        details = os.lstat(lexical)
    except OSError as error:
        issues.append(
            {
                "code": f"UNREADABLE_{label.upper()}",
                "message": str(error),
                "path": str(lexical),
            }
        )
        return lexical, record
    if not stat.S_ISREG(details.st_mode):
        issues.append(
            {
                "code": f"{label.upper()}_NOT_REGULAR_FILE",
                "message": f"{label} must be a regular file",
                "path": str(lexical),
            }
        )
        return lexical, record
    record.update(
        {
            "exists": True,
            "size_bytes": int(details.st_size),
            "sha256": _sha256(lexical),
            "read_only": _read_only(details),
        }
    )
    if require_read_only and not record["read_only"]:
        issues.append(
            {
                "code": f"{label.upper()}_NOT_READ_ONLY",
                "message": f"{label} must be read-only",
                "path": str(lexical),
            }
        )
    return lexical, record


def _load_toml_object(
    path: Path,
    *,
    label: str,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        issues.append(
            {
                "code": f"INVALID_{label.upper()}_TOML",
                "message": str(error),
                "path": str(path),
            }
        )
        return {}
    if not isinstance(payload, dict):
        issues.append(
            {
                "code": f"INVALID_{label.upper()}_ROOT",
                "message": f"{label} TOML root must be a table",
                "path": str(path),
            }
        )
        return {}
    return payload


def _load_cases(
    path: Path,
    *,
    issues: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    expected_fields = (
        "case_id",
        "mach",
        "altitude_km",
        "alpha_deg",
        "beta_deg",
        "role",
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != expected_fields:
                raise ValueError(
                    f"cases header must be exactly {list(expected_fields)!r}"
                )
            for line_number, raw in enumerate(reader, start=2):
                case_id = str(raw.get("case_id", "")).strip()
                role = str(raw.get("role", "")).strip()
                if not case_id or case_id in seen:
                    raise ValueError(
                        f"line {line_number} has empty or duplicate case_id {case_id!r}"
                    )
                seen.add(case_id)
                numeric: dict[str, float] = {}
                for name in ("mach", "altitude_km", "alpha_deg", "beta_deg"):
                    value = float(str(raw.get(name, "")))
                    if not math.isfinite(value):
                        raise ValueError(
                            f"line {line_number} field {name} is not finite"
                        )
                    numeric[name] = value
                if numeric["mach"] <= 0 or numeric["altitude_km"] < 0:
                    raise ValueError(
                        f"line {line_number} has invalid Mach or altitude"
                    )
                if not role:
                    raise ValueError(f"line {line_number} has empty role")
                rows.append({"case_id": case_id, **numeric, "role": role})
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        issues.append(
            {
                "code": "INVALID_CASES_CSV",
                "message": str(error),
                "path": str(path),
            }
        )
        return ()
    if not rows:
        issues.append(
            {
                "code": "EMPTY_CASES_CSV",
                "message": "cases.csv contains no cases",
                "path": str(path),
            }
        )
    return tuple(rows)


def _normalize_legacy_markers(
    document: Mapping[str, Any],
    project: Mapping[str, Any],
    *,
    path: Path,
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    raw_groups = document.get("markers")
    if not isinstance(raw_groups, Mapping):
        raw_groups = document.get("physical_groups")
    if not isinstance(raw_groups, Mapping) or not raw_groups:
        issues.append(
            {
                "code": "MISSING_MARKER_TABLE",
                "message": "markers.toml needs a non-empty [markers] table",
                "path": str(path),
            }
        )
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    entity_owner: dict[tuple[int, int], str] = {}
    role_owner: dict[str, str] = {}
    for key, raw_specification in raw_groups.items():
        if not isinstance(key, str) or not key.strip():
            issues.append(
                {
                    "code": "INVALID_MARKER_NAME",
                    "message": "marker table keys must be non-empty names",
                    "path": str(path),
                }
            )
            continue
        if not isinstance(raw_specification, Mapping):
            issues.append(
                {
                    "code": "INVALID_MARKER_SPECIFICATION",
                    "message": f"marker {key!r} must be a table",
                    "path": str(path),
                }
            )
            continue
        specification = dict(raw_specification)
        physical_name = str(specification.get("physical_name", key)).strip()
        role = str(specification.get("role", "")).strip()
        dimension = specification.get("dimension")
        solver_boundary = specification.get("solver_boundary")
        tags = specification.get("entity_tags")
        step_names = specification.get("step_names")
        if step_names is None and isinstance(specification.get("step_name"), str):
            step_names = [specification["step_name"]]
        geometry = specification.get("geometry")
        if geometry is None:
            geometry = specification.get("features")

        marker_errors: list[str] = []
        if not physical_name or physical_name in normalized:
            marker_errors.append("physical_name is empty or duplicated")
        if not role or role in role_owner:
            marker_errors.append("role is empty or duplicated")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension not in (2, 3)
        ):
            marker_errors.append("dimension must be 2 or 3")
        if not isinstance(solver_boundary, bool):
            marker_errors.append("solver_boundary must be boolean")
        elif dimension == 3 and solver_boundary:
            marker_errors.append(
                "dimension-3 domain groups cannot be solver boundaries"
            )
        if (
            not isinstance(tags, Sequence)
            or isinstance(tags, (str, bytes))
            or not tags
        ):
            marker_errors.append("entity_tags must be a non-empty integer list")
            normalized_tags: list[int] = []
        else:
            normalized_tags = []
            for tag in tags:
                if isinstance(tag, bool) or not isinstance(tag, int) or tag <= 0:
                    marker_errors.append(f"invalid entity tag {tag!r}")
                    continue
                if isinstance(dimension, int):
                    owner = entity_owner.get((dimension, tag))
                    if owner is not None:
                        marker_errors.append(
                            f"entity ({dimension}, {tag}) is also assigned to {owner!r}"
                        )
                    else:
                        entity_owner[(dimension, tag)] = physical_name
                normalized_tags.append(tag)
        if (
            not isinstance(step_names, Sequence)
            or isinstance(step_names, (str, bytes))
            or not step_names
            or any(not isinstance(value, str) or not value.strip() for value in step_names)
        ):
            marker_errors.append("step_names must preserve at least one STEP entity name")
            normalized_step_names: list[str] = []
        else:
            normalized_step_names = [str(value).strip() for value in step_names]
        if not isinstance(geometry, Mapping):
            marker_errors.append("geometry must contain marker feature fingerprints")
            normalized_geometry: dict[str, Any] = {}
        else:
            normalized_geometry = dict(geometry)
            required_features = {"area_m2", "centroid_m", "bounding_box_m"}
            missing_features = sorted(required_features - set(normalized_geometry))
            if missing_features:
                marker_errors.append(
                    f"geometry is missing features {missing_features!r}"
                )
        if marker_errors:
            issues.append(
                {
                    "code": "INVALID_MARKER",
                    "message": f"marker {key!r}: " + "; ".join(marker_errors),
                    "path": str(path),
                }
            )
            continue
        role_owner[role] = physical_name
        normalized[physical_name] = {
            **specification,
            "physical_name": physical_name,
            "role": role,
            "dimension": int(dimension),
            "solver_boundary": bool(solver_boundary),
            "entity_tags": normalized_tags,
            "step_names": normalized_step_names,
            "geometry": normalized_geometry,
        }

    boundaries = project.get("boundaries")
    if not isinstance(boundaries, Mapping):
        issues.append(
            {
                "code": "MISSING_PROJECT_BOUNDARIES",
                "message": "project.toml needs [boundaries]",
                "path": str(path),
            }
        )
        return normalized
    required_roles: list[str] = []
    for name in ("farfield_role", "wall_role", "measurement_surface_role"):
        value = boundaries.get(name)
        if not isinstance(value, str) or not value.strip():
            issues.append(
                {
                    "code": "MISSING_PROJECT_BOUNDARY_ROLE",
                    "message": f"project boundary {name} is missing",
                    "path": str(path),
                }
            )
        else:
            required_roles.append(value.strip())
    rear_roles = boundaries.get("rear_outlet_roles")
    if (
        not isinstance(rear_roles, Sequence)
        or isinstance(rear_roles, (str, bytes))
        or not rear_roles
    ):
        issues.append(
            {
                "code": "MISSING_REAR_OUTLET_ROLES",
                "message": "project rear_outlet_roles must be a non-empty list",
                "path": str(path),
            }
        )
    else:
        required_roles.extend(str(role).strip() for role in rear_roles)
    for role in required_roles:
        if role and role not in role_owner:
            issues.append(
                {
                    "code": "MISSING_REQUIRED_MARKER_ROLE",
                    "message": f"markers.toml has no marker for role {role!r}",
                    "path": str(path),
                }
            )
    fluid_groups = [
        specification
        for specification in normalized.values()
        if specification.get("dimension") == 3
        and specification.get("role") in {"fluid", "fluid_domain"}
    ]
    if len(fluid_groups) != 1:
        issues.append(
            {
                "code": "MISSING_OR_AMBIGUOUS_FLUID_MARKER",
                "message": "markers.toml needs exactly one dimension-3 fluid/fluid_domain marker",
                "path": str(path),
            }
        )
    measurement_role = boundaries.get("measurement_surface_role")
    if isinstance(measurement_role, str) and measurement_role in role_owner:
        measurement = normalized[role_owner[measurement_role]]
        expected_solver_boundary = bool(
            boundaries.get("measurement_surface_is_solver_boundary")
        )
        if measurement.get("solver_boundary") is not expected_solver_boundary:
            issues.append(
                {
                    "code": "MEASUREMENT_SURFACE_SOLVER_ROLE_MISMATCH",
                    "message": "measurement marker solver_boundary disagrees with project.toml",
                    "path": str(path),
                }
            )
    return normalized


def _normalize_marker_config_v2(
    document: Mapping[str, Any],
    project: Mapping[str, Any],
    *,
    path: Path,
    issues: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Project the strict tag-free marker document into pipeline views."""

    try:
        validated = validate_marker_config_document(document)
    except (MarkerConfigError, TypeError, ValueError) as error:
        issues.append(
            {
                "code": "INVALID_MARKERS_V2",
                "message": str(error),
                "path": str(path),
            }
        )
        return {}, {}

    boundaries = project.get("boundaries")
    if not isinstance(boundaries, Mapping):
        issues.append(
            {
                "code": "MISSING_PROJECT_BOUNDARIES",
                "message": "project.toml needs [boundaries]",
                "path": str(path),
            }
        )
        return {}, {}
    farfield_role = boundaries.get("farfield_role")
    wall_role = boundaries.get("wall_role")
    measurement_role = boundaries.get("measurement_surface_role")
    rear_roles = boundaries.get("rear_outlet_roles")
    if (
        not isinstance(farfield_role, str)
        or not farfield_role.strip()
        or not isinstance(wall_role, str)
        or not wall_role.strip()
        or not isinstance(measurement_role, str)
        or not measurement_role.strip()
        or not isinstance(rear_roles, Sequence)
        or isinstance(rear_roles, (str, bytes))
        or len(rear_roles) != 2
        or any(not isinstance(role, str) or not role.strip() for role in rear_roles)
    ):
        issues.append(
            {
                "code": "INVALID_PROJECT_BOUNDARY_ROLES",
                "message": "project boundary roles are incomplete or ambiguous",
                "path": str(path),
            }
        )
        return {}, {}

    expected_solver_roles = {
        farfield_role.strip(),
        wall_role.strip(),
        *(str(role).strip() for role in rear_roles),
    }
    normalized: dict[str, dict[str, Any]] = {}
    actual_solver_roles: set[str] = set()
    for raw in validated.get("solver_markers", []):
        if not isinstance(raw, Mapping):
            continue
        marker = dict(raw)
        name = str(marker.get("physical_name", "")).strip()
        role = str(marker.get("semantic_role", "")).strip()
        if name:
            normalized[name] = {
                **marker,
                "physical_name": name,
                "role": role,
                "dimension": 2,
                "solver_boundary": True,
            }
        actual_solver_roles.add(role)
    if actual_solver_roles != expected_solver_roles:
        issues.append(
            {
                "code": "MARKER_PROJECT_ROLE_MISMATCH",
                "message": (
                    "marker solver roles do not exactly match project.toml: "
                    f"expected={sorted(expected_solver_roles)!r}, "
                    f"actual={sorted(actual_solver_roles)!r}"
                ),
                "path": str(path),
            }
        )

    fluid = validated.get("fluid")
    if isinstance(fluid, Mapping):
        fluid_name = str(fluid.get("physical_name", "")).strip()
        if fluid_name:
            normalized[fluid_name] = {
                **dict(fluid),
                "physical_name": fluid_name,
                "role": str(fluid.get("semantic_role", "fluid")),
                "dimension": 3,
                "solver_boundary": False,
            }

    raw_measurement = validated.get("measurement")
    measurements: dict[str, dict[str, Any]] = {}
    if isinstance(raw_measurement, Mapping):
        measurement = dict(raw_measurement)
        name = str(measurement.get("name", "")).strip()
        role = str(measurement.get("semantic_role", "")).strip()
        if name:
            measurements[name] = {
                **measurement,
                "role": role,
                "dimension": 2,
                "solver_boundary": False,
            }
        if (
            role != measurement_role.strip()
            or measurement.get("solver_boundary") is not False
            or measurement.get("create_physical_group") is not False
            or boundaries.get("measurement_surface_is_solver_boundary") is not False
        ):
            issues.append(
                {
                    "code": "MEASUREMENT_SURFACE_PROJECT_MISMATCH",
                    "message": "measurement marker disagrees with project.toml",
                    "path": str(path),
                }
            )
    else:
        issues.append(
            {
                "code": "MISSING_MEASUREMENT_SURFACE",
                "message": "markers.v2 has no measurement surface",
                "path": str(path),
            }
        )
    return normalized, measurements


def _is_sha256_text(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None
    )


def _normalize_topology_smoke_config(
    document: Mapping[str, Any],
    *,
    path: Path,
    record: Mapping[str, Any],
    markers_record: Mapping[str, Any],
    pipeline_geometry_record: Mapping[str, Any],
    marker_document: Mapping[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_root = {
        "schema",
        "status",
        "policy",
        "provenance",
        "local_refinement",
    }
    if set(document) != expected_root:
        issues.append(
            {
                "code": "INVALID_TOPOLOGY_SMOKE_SCHEMA",
                "message": "topology_smoke.toml has unexpected or missing root keys",
                "path": str(path),
            }
        )
        return {}
    policy = document.get("policy")
    provenance = document.get("provenance")
    refinement = document.get("local_refinement")
    if (
        document.get("schema") != "cfdpipe.topology_smoke.v1"
        or document.get("status") != "CONFIGURED"
        or not isinstance(policy, Mapping)
        or set(policy)
        != {
            "topology_smoke_only",
            "production_mesh_eligible",
            "runtime_tags_present",
        }
        or policy.get("topology_smoke_only") is not True
        or policy.get("production_mesh_eligible") is not False
        or policy.get("runtime_tags_present") is not False
    ):
        issues.append(
            {
                "code": "UNSAFE_TOPOLOGY_SMOKE_POLICY",
                "message": "topology-smoke policy is missing or unsafe",
                "path": str(path),
            }
        )
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != {"markers_sha256", "pipeline_brep_sha256"}
        or provenance.get("markers_sha256") != markers_record.get("sha256")
        or provenance.get("pipeline_brep_sha256")
        != pipeline_geometry_record.get("sha256")
    ):
        issues.append(
            {
                "code": "STALE_TOPOLOGY_SMOKE_PROVENANCE",
                "message": (
                    "topology_smoke.toml hashes do not match the current marker "
                    "contract and pipeline BREP"
                ),
                "path": str(path),
            }
        )
    expected_refinement = {
        "surface_fingerprint_ids",
        "minimum_size_m",
        "distance_max_m",
        "sampling",
    }
    if not isinstance(refinement, Mapping) or set(refinement) != expected_refinement:
        issues.append(
            {
                "code": "INVALID_TOPOLOGY_SMOKE_REFINEMENT",
                "message": "local_refinement keys are incomplete or unexpected",
                "path": str(path),
            }
        )
        return {}
    raw_ids = refinement.get("surface_fingerprint_ids")
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= 55
        or any(not _is_sha256_text(value) for value in raw_ids)
    ):
        issues.append(
            {
                "code": "INVALID_TOPOLOGY_SMOKE_FINGERPRINTS",
                "message": "local refinement requires 1 to 55 SHA-256 fingerprints",
                "path": str(path),
            }
        )
        return {}
    fingerprint_ids = [str(value).casefold() for value in raw_ids]
    if len(fingerprint_ids) != len(set(fingerprint_ids)):
        issues.append(
            {
                "code": "DUPLICATE_TOPOLOGY_SMOKE_FINGERPRINT",
                "message": "local refinement surface fingerprints must be unique",
                "path": str(path),
            }
        )
    available: set[str] = set()
    for raw_marker in marker_document.get("solver_markers", []):
        if not isinstance(raw_marker, Mapping):
            continue
        for raw_member in raw_marker.get("members", []):
            if isinstance(raw_member, Mapping) and _is_sha256_text(
                raw_member.get("fingerprint_id")
            ):
                available.add(str(raw_member["fingerprint_id"]).casefold())
    missing = sorted(set(fingerprint_ids) - available)
    if missing:
        issues.append(
            {
                "code": "UNKNOWN_TOPOLOGY_SMOKE_FINGERPRINT",
                "message": "local refinement fingerprint is absent from markers.v2",
                "path": str(path),
                "fingerprint_ids": missing,
            }
        )
    try:
        minimum_size = float(refinement.get("minimum_size_m"))
        distance_max = float(refinement.get("distance_max_m"))
    except (TypeError, ValueError):
        minimum_size = math.nan
        distance_max = math.nan
    sampling = refinement.get("sampling")
    if (
        isinstance(refinement.get("minimum_size_m"), bool)
        or isinstance(refinement.get("distance_max_m"), bool)
        or not math.isfinite(minimum_size)
        or not math.isfinite(distance_max)
        or minimum_size < 0.001
        or distance_max <= 0.0
        or isinstance(sampling, bool)
        or not isinstance(sampling, int)
        or not 10 <= sampling <= 1000
    ):
        issues.append(
            {
                "code": "INVALID_TOPOLOGY_SMOKE_SIZES",
                "message": "local refinement size, distance or sampling is invalid",
                "path": str(path),
            }
        )
    if record.get("sha256") is None:
        issues.append(
            {
                "code": "UNHASHED_TOPOLOGY_SMOKE_CONFIG",
                "message": "topology_smoke.toml has no file hash evidence",
                "path": str(path),
            }
        )
    return {
        "surface_fingerprint_ids": sorted(fingerprint_ids),
        "minimum_size_m": minimum_size,
        "distance_max_m": distance_max,
        "sampling": sampling,
    }


def load_real_connection_inputs(
    *,
    step_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str],
    cases_path: str | os.PathLike[str],
    markers_path: str | os.PathLike[str],
    topology_smoke_path: str | os.PathLike[str] | None = None,
    case_id: str,
    trusted_repository_root: str | os.PathLike[str],
) -> RealConnectionInputs:
    """Load all real-project sources without importing Gmsh or running tools."""

    repository_root = Path(trusted_repository_root).expanduser().resolve(strict=True)
    if _is_reparse(repository_root) or not repository_root.is_dir():
        raise ValueError("trusted_repository_root must be a real directory")
    config_root = repository_root / "config"
    raw_geometry_root = repository_root / "geometry" / "raw"
    derived_geometry_root = repository_root / "geometry" / "derived"
    issues: list[dict[str, Any]] = []

    project_file, project_record = _input_file_record(
        project_path,
        label="project",
        allowed_parent=config_root,
        repository_root=repository_root,
        issues=issues,
    )
    cases_file, cases_record = _input_file_record(
        cases_path,
        label="cases",
        allowed_parent=config_root,
        repository_root=repository_root,
        issues=issues,
    )
    markers_file, markers_record = _input_file_record(
        markers_path,
        label="markers",
        allowed_parent=config_root,
        repository_root=repository_root,
        issues=issues,
    )
    requested_topology_smoke = (
        config_root / "topology_smoke.toml"
        if topology_smoke_path is None
        else topology_smoke_path
    )
    topology_smoke_file, topology_smoke_record = _input_file_record(
        requested_topology_smoke,
        label="topology_smoke_config",
        allowed_parent=config_root,
        repository_root=repository_root,
        issues=issues,
    )
    step_file, step_record = _input_file_record(
        step_path,
        label="step",
        allowed_parent=raw_geometry_root,
        repository_root=repository_root,
        issues=issues,
        require_read_only=True,
    )

    project = (
        _load_toml_object(project_file, label="project", issues=issues)
        if project_record["exists"]
        else {}
    )
    cases = (
        _load_cases(cases_file, issues=issues) if cases_record["exists"] else ()
    )
    marker_document: dict[str, Any] = {}
    if markers_record["exists"]:
        try:
            marker_document = load_marker_config(
                markers_file, repository_root=repository_root
            )
        except (MarkerConfigError, OSError, ValueError) as error:
            issues.append(
                {
                    "code": "INVALID_OR_STALE_MARKERS_V2",
                    "message": str(error),
                    "path": str(markers_file),
                }
            )
    topology_smoke_document = (
        _load_toml_object(
            topology_smoke_file,
            label="topology_smoke_config",
            issues=issues,
        )
        if topology_smoke_record["exists"]
        else {}
    )

    units = project.get("units") if isinstance(project, Mapping) else None
    if not isinstance(units, Mapping):
        issues.append(
            {
                "code": "MISSING_PROJECT_UNITS",
                "message": "project.toml needs [units]",
                "path": str(project_file),
            }
        )
    elif (
        str(units.get("source_geometry_length", "")).lower() != "mm"
        or str(units.get("solver_geometry_length", "")).lower() != "m"
        or str(units.get("solver_system", "")).upper() != "SI"
    ):
        issues.append(
            {
                "code": "UNSUPPORTED_PROJECT_UNIT_CONVERSION",
                "message": "real connection requires declared mm -> m geometry and SI solver units",
                "path": str(project_file),
            }
        )
    project_table = project.get("project") if isinstance(project, Mapping) else None
    configured_geometry = (
        project_table.get("geometry_file")
        if isinstance(project_table, Mapping)
        else None
    )
    if not isinstance(configured_geometry, str) or not configured_geometry.strip():
        issues.append(
            {
                "code": "MISSING_PROJECT_GEOMETRY_FILE",
                "message": "project.geometry_file is missing",
                "path": str(project_file),
            }
        )
    else:
        configured_path = Path(
            os.path.abspath(repository_root / configured_geometry)
        )
        if os.path.normcase(str(configured_path)) != os.path.normcase(str(step_file)):
            issues.append(
                {
                    "code": "STEP_PROJECT_PATH_MISMATCH",
                    "message": "--step does not match project.geometry_file",
                    "path": str(step_file),
                    "configured_path": str(configured_path),
                }
            )

    pipeline_geometry_file = derived_geometry_root / "__missing_pipeline_geometry__.brep"
    pipeline_geometry_record: dict[str, Any] = {
        "requested_path": None,
        "path": str(pipeline_geometry_file),
        "exists": False,
        "size_bytes": None,
        "sha256": None,
        "read_only": None,
    }
    if marker_document:
        source_contract = marker_document.get("source")
        if isinstance(source_contract, Mapping):
            declared_source_path = source_contract.get("path")
            declared_source_sha = source_contract.get("sha256")
            if isinstance(declared_source_path, str):
                resolved_declared_source = Path(
                    os.path.abspath(repository_root / declared_source_path)
                )
            else:
                resolved_declared_source = Path()
            if (
                os.path.normcase(str(resolved_declared_source))
                != os.path.normcase(str(step_file))
                or declared_source_sha != step_record.get("sha256")
            ):
                issues.append(
                    {
                        "code": "MARKERS_SOURCE_STEP_MISMATCH",
                        "message": "markers.v2 source STEP path/hash differs from --step",
                        "path": str(markers_file),
                    }
                )
        pipeline_contract = marker_document.get("pipeline_geometry")
        if not isinstance(pipeline_contract, Mapping):
            issues.append(
                {
                    "code": "MISSING_PIPELINE_GEOMETRY_CONTRACT",
                    "message": "markers.v2 has no pipeline_geometry table",
                    "path": str(markers_file),
                }
            )
        else:
            declared_pipeline_path = pipeline_contract.get("path")
            if (
                not isinstance(declared_pipeline_path, str)
                or not declared_pipeline_path.strip()
                or Path(declared_pipeline_path).is_absolute()
                or ".." in Path(declared_pipeline_path).parts
            ):
                issues.append(
                    {
                        "code": "UNSAFE_PIPELINE_GEOMETRY_PATH",
                        "message": "pipeline BREP path must be repository-relative",
                        "path": str(markers_file),
                    }
                )
            else:
                pipeline_geometry_file, pipeline_geometry_record = _input_file_record(
                    repository_root / declared_pipeline_path,
                    label="pipeline_geometry",
                    allowed_parent=derived_geometry_root,
                    repository_root=repository_root,
                    issues=issues,
                    require_read_only=True,
                )
                if pipeline_geometry_file.suffix.casefold() != ".brep":
                    issues.append(
                        {
                            "code": "PIPELINE_GEOMETRY_NOT_BREP",
                            "message": "pipeline geometry must be the repaired .brep",
                            "path": str(pipeline_geometry_file),
                        }
                    )
                if (
                    pipeline_contract.get("format") != "brep"
                    or pipeline_contract.get("pipeline_eligible") is not True
                    or pipeline_contract.get("read_only") is not True
                    or pipeline_contract.get("sha256")
                    != pipeline_geometry_record.get("sha256")
                ):
                    issues.append(
                        {
                            "code": "STALE_OR_INELIGIBLE_PIPELINE_GEOMETRY",
                            "message": "pipeline BREP policy/hash is invalid",
                            "path": str(pipeline_geometry_file),
                        }
                    )

    markers, measurements = (
        _normalize_marker_config_v2(
            marker_document,
            project,
            path=markers_file,
            issues=issues,
        )
        if marker_document
        else ({}, {})
    )
    topology_smoke_refinement = (
        _normalize_topology_smoke_config(
            topology_smoke_document,
            path=topology_smoke_file,
            record=topology_smoke_record,
            markers_record=markers_record,
            pipeline_geometry_record=pipeline_geometry_record,
            marker_document=marker_document,
            issues=issues,
        )
        if topology_smoke_document
        else {}
    )
    requested_case_id = str(case_id).strip()
    if not requested_case_id:
        issues.append(
            {
                "code": "EMPTY_CASE_ID",
                "message": "--case must be non-empty",
                "path": str(cases_file),
            }
        )
    if issues:
        raise RealConnectionInputError(issues)
    return RealConnectionInputs(
        repository_root=repository_root,
        step_path=step_file,
        pipeline_geometry_path=pipeline_geometry_file,
        project_path=project_file,
        cases_path=cases_file,
        markers_path=markers_file,
        project=dict(project),
        cases=tuple(cases),
        markers=markers,
        measurements=measurements,
        marker_document=marker_document,
        requested_case_id=requested_case_id,
        input_records={
            "project": project_record,
            "cases": cases_record,
            "markers": markers_record,
            "step": step_record,
            "pipeline_geometry": pipeline_geometry_record,
            "topology_smoke_config": topology_smoke_record,
        },
        topology_smoke_path=topology_smoke_file,
        topology_smoke_refinement=topology_smoke_refinement,
    )


def select_real_case(inputs: RealConnectionInputs) -> dict[str, Any]:
    selected = [
        dict(row)
        for row in inputs.cases
        if row.get("case_id") == inputs.requested_case_id
    ]
    if len(selected) != 1:
        raise RealConnectionConfigurationGateError(
            "CASE_NOT_FOUND_OR_AMBIGUOUS",
            f"cases.csv must contain exactly one {inputs.requested_case_id!r} row",
        )
    return selected[0]


def validate_project_su2_mesh(
    path: str | os.PathLike[str],
    required_markers: Iterable[str],
    *,
    max_elements: int = _REAL_MAX_ELEMENTS,
) -> dict[str, Any]:
    """Validate a three-dimensional, multi-marker SU2 connection mesh."""

    mesh_path = Path(path).expanduser().resolve(strict=True)
    if not mesh_path.is_file() or mesh_path.stat().st_size <= 0:
        raise ValueError(f"SU2 mesh is missing or empty: {mesh_path}")
    if (
        isinstance(max_elements, bool)
        or not isinstance(max_elements, int)
        or max_elements <= 0
        or max_elements > _REAL_MAX_ELEMENTS
    ):
        raise ValueError(f"max_elements must be between 1 and {_REAL_MAX_ELEMENTS}")
    text = mesh_path.read_text(encoding="utf-8", errors="strict")
    match = _REAL_NONFINITE_RE.search(text)
    if match is not None:
        raise ValueError(f"SU2 mesh contains non-finite token {match.group(0)!r}")

    counts: dict[str, int] = {}
    markers: dict[str, int] = {}
    pending_marker: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("%", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.upper()
        if key in {"NDIME", "NELEM", "NPOIN", "NMARK"}:
            token = value.split()[0] if value.split() else ""
            try:
                parsed = int(token)
            except ValueError as error:
                raise ValueError(
                    f"{key} at line {line_number} is not an integer"
                ) from error
            if key in counts:
                raise ValueError(f"duplicate {key} at line {line_number}")
            counts[key] = parsed
        elif key == "MARKER_TAG":
            if pending_marker is not None:
                raise ValueError(f"marker {pending_marker!r} has no MARKER_ELEMS")
            pending_marker = value.strip()
            if not pending_marker or pending_marker in markers:
                raise ValueError(f"empty or duplicate marker at line {line_number}")
        elif key == "MARKER_ELEMS":
            if pending_marker is None:
                raise ValueError(f"MARKER_ELEMS at line {line_number} has no tag")
            try:
                marker_elements = int(value.split()[0])
            except (ValueError, IndexError) as error:
                raise ValueError(
                    f"MARKER_ELEMS at line {line_number} is not an integer"
                ) from error
            if marker_elements <= 0:
                raise ValueError(f"marker {pending_marker!r} has no elements")
            markers[pending_marker] = marker_elements
            pending_marker = None
    if pending_marker is not None:
        raise ValueError(f"marker {pending_marker!r} has no MARKER_ELEMS")
    required_count_keys = {"NDIME", "NELEM", "NPOIN", "NMARK"}
    missing_counts = sorted(required_count_keys - set(counts))
    if missing_counts:
        raise ValueError(f"SU2 mesh is missing counts {missing_counts!r}")
    if counts["NDIME"] != 3:
        raise ValueError(f"real-project mesh NDIME must be 3, got {counts['NDIME']}")
    for name in ("NELEM", "NPOIN", "NMARK"):
        if counts[name] <= 0:
            raise ValueError(f"{name} must be positive")
    if counts["NELEM"] > max_elements:
        raise ValueError(
            f"NELEM={counts['NELEM']} exceeds connection cap {max_elements}"
        )
    if counts["NMARK"] != len(markers):
        raise ValueError(
            f"NMARK={counts['NMARK']} does not match parsed markers={len(markers)}"
        )
    required = {str(name).strip() for name in required_markers if str(name).strip()}
    missing_markers = sorted(required - set(markers))
    if missing_markers:
        raise ValueError(f"SU2 mesh is missing required markers {missing_markers!r}")
    unexpected_markers = sorted(set(markers) - required)
    if unexpected_markers:
        raise ValueError(
            f"SU2 mesh contains unexpected solver markers {unexpected_markers!r}"
        )
    return {
        "status": "PASS",
        "path": str(mesh_path),
        "sha256": _sha256(mesh_path),
        "size_bytes": mesh_path.stat().st_size,
        "ndime": counts["NDIME"],
        "nelem": counts["NELEM"],
        "npoin": counts["NPOIN"],
        "nmark": counts["NMARK"],
        "markers": markers,
        "required_markers": sorted(required),
        "max_elements": max_elements,
        "finite": True,
    }


def _real_not_run(reason: str, *, upstream_stage: str | None = None) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "reason": "NOT_RUN_DUE_TO_UPSTREAM_FAILURE",
        "detail": reason,
    }
    if upstream_stage is not None:
        evidence["upstream_stage"] = upstream_stage
    return {"status": "FAIL", "evidence": [evidence]}


def _new_real_state(controller_argv: Sequence[str]) -> dict[str, Any]:
    return {
        "attempted": {
            "input": False,
            "gmsh": False,
            "mesh_validation": False,
            "case_config": False,
            "su2": False,
            "visualization": False,
            "paraview": False,
        },
        "links": {
            key: _real_not_run("pipeline has not reached this link")
            for key in REAL_CONNECTION_KEYS
        },
        "manifests": {},
        "resolved_tools": {},
        "pilot_contract": None,
        "controller_argv": [str(value) for value in controller_argv],
    }


def build_real_connection_report(
    output_root: str | os.PathLike[str],
    state: Mapping[str, Any],
    *,
    started_at: str,
    ended_at: str,
    failure: BaseException | None = None,
    pipeline_log: Path | None = None,
) -> dict[str, Any]:
    """Build the six-link report strictly from current in-memory run state."""

    root = Path(output_root).expanduser().resolve()
    raw_links = state.get("links")
    raw_links = raw_links if isinstance(raw_links, Mapping) else {}
    report: dict[str, Any] = {}
    for key in REAL_CONNECTION_KEYS:
        section = raw_links.get(key)
        if not isinstance(section, Mapping):
            section = _real_not_run("no current-run evidence was recorded")
        status = section.get("status")
        evidence = section.get("evidence")
        if status not in {"PASS", "FAIL"}:
            status = "FAIL"
        if not isinstance(evidence, list) or not evidence:
            evidence = [{"reason": "MISSING_CURRENT_RUN_EVIDENCE"}]
            status = "FAIL"
        report[key] = {"status": status, "evidence": evidence}
    report["overall"] = (
        "PASS"
        if failure is None
        and all(report[key]["status"] == "PASS" for key in REAL_CONNECTION_KEYS)
        else "FAIL"
    )
    failure_payload: dict[str, Any] | None = None
    if failure is not None:
        failure_payload = {
            "type": type(failure).__name__,
            "message": str(failure),
            "traceback": "".join(
                traceback.format_exception(
                    type(failure), failure, failure.__traceback__
                )
            ),
        }
        if isinstance(failure, RealConnectionInputError):
            failure_payload["issues"] = [dict(issue) for issue in failure.issues]
        if isinstance(failure, RealConnectionConfigurationGateError):
            failure_payload["code"] = failure.code
    report["metadata"] = {
        "schema_version": 1,
        "started_at": started_at,
        "ended_at": ended_at,
        "output_root": str(root),
        "pipeline_log": None if pipeline_log is None else str(pipeline_log),
        "controller_argv": list(state.get("controller_argv", [])),
        "previous_run_archive": state.get("previous_run_archive"),
        "stage_attempted": dict(state.get("attempted", {})),
        "constraints": {
            "mesh_level": "smoke",
            "max_elements": _REAL_MAX_ELEMENTS,
            "characteristic_length_m": state.get(
                "smoke_characteristic_length_m"
            ),
            "facet_overlap_angle_tolerance_degrees": state.get(
                "facet_overlap_angle_tolerance_degrees"
            ),
            "gmsh_algorithm_3d": state.get("gmsh_algorithm_3d"),
            "boundary_layers": False,
            "nproc": 1,
            "max_iterations": state.get("max_iterations"),
            "gpu_enabled": False,
            "physical_interpretation": False,
            "connection_only": True,
            "production_eligible": False,
            "rear_outlet_boundary_frozen": False,
        },
        "resolved_tools": dict(state.get("resolved_tools", {})),
        "pilot_contract": state.get("pilot_contract"),
        "project_readiness": "BLOCKED_PENDING_PHYSICAL_BOUNDARY_VALIDATION",
        "production_eligible": False,
        "failure": failure_payload,
    }
    return report


def write_real_connection_report(
    output_root: str | os.PathLike[str], payload: Mapping[str, Any]
) -> Path:
    root = Path(output_root).expanduser().resolve()
    path = root / _REAL_REPORT_JSON
    _write_json(path, dict(payload))
    return path


def _write_real_stage(
    output_root: Path,
    relative_manifest: str,
    relative_log: str,
    payload: Mapping[str, Any],
) -> tuple[Path, Path]:
    manifest_path = output_root / relative_manifest
    log_path = output_root / relative_log
    _write_json(manifest_path, dict(payload))
    lines = [
        f"status: {payload.get('status')}",
        f"stage: {payload.get('stage')}",
    ]
    reason = payload.get("reason")
    if reason is not None:
        lines.append(f"reason: {reason}")
    error = payload.get("error")
    if isinstance(error, Mapping):
        lines.append(f"error_type: {error.get('type')}")
        lines.append(f"error_message: {error.get('message')}")
        if error.get("traceback"):
            lines.append(str(error["traceback"]))
    _atomic_write_text(log_path, "\n".join(lines) + "\n")
    return manifest_path, log_path


def _write_real_not_run_manifests(output_root: Path) -> None:
    placeholders = (
        ("mesh/gmsh_manifest.json", "mesh/gmsh.log", "gmsh"),
        ("mesh/mesh_manifest.json", "mesh/mesh_validation.log", "mesh_validation"),
        ("su2/config_manifest.json", "su2/config.log", "case_config"),
        ("su2/run_manifest.json", "su2/su2_stage.log", "su2"),
        (
            "paraview/paraview_manifest.json",
            "paraview/paraview_stage.log",
            "paraview",
        ),
    )
    for manifest, log, stage in placeholders:
        manifest_path = output_root / manifest
        log_path = output_root / log
        if manifest_path.exists() and log_path.exists():
            continue
        if manifest_path.is_file():
            try:
                payload = _load_json(manifest_path)
            except BaseException as error:
                payload = {
                    "status": "FAIL",
                    "stage": stage,
                    "reason": "UNREADABLE_EXISTING_STAGE_MANIFEST",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                }
            else:
                payload.setdefault("stage", stage)
        else:
            payload = {
                "status": "NOT_RUN",
                "stage": stage,
                "reason": "NOT_RUN_DUE_TO_UPSTREAM_FAILURE",
            }
        _write_real_stage(
            output_root,
            manifest,
            log,
            payload,
        )


def _validate_real_paraview_outputs(
    output_root: Path, manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate the exact JSON/CSV data products required by the real pipeline."""

    paraview_root = (output_root / "paraview").resolve(strict=True)
    required_names = (
        "solution_inventory.json",
        "smoke_postprocess.json",
        "smoke_integral.csv",
    )
    raw_records = manifest.get("output_files")
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
        raise RealConnectionError("ParaView manifest output_files must be a list")

    records_by_path: dict[Path, dict[str, Any]] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            continue
        raw_path = raw_record.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            resolved = Path(raw_path).expanduser().resolve(strict=True)
        except OSError as error:
            raise RealConnectionError(
                f"ParaView manifest declares a missing output: {raw_path}"
            ) from error
        if resolved in records_by_path:
            raise RealConnectionError(
                f"ParaView manifest declares an output more than once: {resolved}"
            )
        records_by_path[resolved] = dict(raw_record)

    validated: list[dict[str, Any]] = []
    for name in required_names:
        expected = (paraview_root / name).resolve(strict=True)
        if expected.parent != paraview_root or not expected.is_file():
            raise RealConnectionError(f"required ParaView output is missing: {expected}")
        record = records_by_path.get(expected)
        if record is None:
            raise RealConnectionError(
                f"ParaView manifest does not declare required output: {expected}"
            )
        size = expected.stat().st_size
        digest = _sha256(expected)
        if size <= 0:
            raise RealConnectionError(f"required ParaView output is empty: {expected}")
        if record.get("size_bytes") != size or record.get("sha256") != digest:
            raise RealConnectionError(
                f"ParaView output evidence is stale or incomplete: {expected}"
            )
        if expected.suffix.casefold() == ".json":
            _load_json(expected)
        else:
            with expected.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            if len(rows) < 2 or not rows[0]:
                raise RealConnectionError(
                    f"ParaView integral CSV has no header/data rows: {expected}"
                )
        validated.append(record)
    return validated


def _archive_previous_real_run(output_root: Path) -> Path | None:
    """Move only prior pipeline-owned evidence into a recoverable archive.

    Geometry-review, repair, marker and validation evidence is deliberately not
    included.  This keeps repeated ``pipeline run`` invocations reproducible
    without deleting or silently overwriting a previous mesh/solver attempt.
    """

    fixed_candidates = (
        output_root / "input",
        output_root / "mesh",
        output_root / "su2",
        output_root / "paraview",
        output_root / _REAL_REPORT_JSON,
        output_root / _REAL_PIPELINE_MANIFEST,
    )
    dynamic_candidates = tuple(
        path
        for path in output_root.glob("real-connection-*.json")
        if path.parent == output_root
    )
    candidates = tuple(
        path for path in (*fixed_candidates, *dynamic_candidates) if _lexists(path)
    )
    if not candidates:
        return None

    for candidate in candidates:
        _assert_safe_candidate(candidate, output_root)
        if _is_reparse(candidate):
            raise ValueError(f"previous run artifact is a link/reparse point: {candidate}")
        if candidate.is_dir():
            for descendant in candidate.rglob("*"):
                if _is_reparse(descendant):
                    raise ValueError(
                        "previous run artifact contains a link/reparse point: "
                        f"{descendant}"
                    )

    archive_parent = output_root / "run_archive"
    if _lexists(archive_parent):
        if _is_reparse(archive_parent) or not archive_parent.is_dir():
            raise ValueError(
                f"real-run archive parent is unsafe: {archive_parent}"
            )
    else:
        archive_parent.mkdir()
        if _is_reparse(archive_parent) or not archive_parent.is_dir():
            raise ValueError(
                f"real-run archive parent creation was redirected: {archive_parent}"
            )
    archive = archive_parent / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"_{uuid4().hex[:10]}"
    )
    if _lexists(archive):
        raise ValueError(f"real-run archive path already exists: {archive}")
    archive.mkdir(parents=True)
    moved: list[str] = []
    moved_paths: list[tuple[Path, Path]] = []
    try:
        for candidate in candidates:
            relative = candidate.relative_to(output_root)
            destination = archive / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidate.replace(destination)
            moved_paths.append((candidate, destination))
            moved.append(relative.as_posix())
        _write_json(
            archive / "archive_manifest.json",
            {
                "status": "PASS",
                "archived_at": _utc_now(),
                "source_root": str(output_root),
                "archive_root": str(archive),
                "moved_paths": sorted(moved),
                "operation": "recoverable_move_no_delete",
            },
        )
    except BaseException as archive_error:
        rollback_errors: list[str] = []
        for source, destination in reversed(moved_paths):
            try:
                if _lexists(source):
                    raise RuntimeError(f"rollback destination already exists: {source}")
                if _lexists(destination):
                    destination.replace(source)
            except BaseException as rollback_error:
                rollback_errors.append(f"{source}: {rollback_error}")
        try:
            if archive.exists() and not any(archive.iterdir()):
                archive.rmdir()
        except BaseException as cleanup_error:
            rollback_errors.append(f"archive cleanup: {cleanup_error}")
        if rollback_errors:
            raise RuntimeError(
                "real-run archive failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from archive_error
        raise
    return archive


def _real_input_request_evidence(
    *,
    step_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str],
    cases_path: str | os.PathLike[str],
    markers_path: str | os.PathLike[str],
    topology_smoke_path: str | os.PathLike[str],
    case_id: str,
) -> dict[str, Any]:
    return {
        "inputs": {
            "step": str(Path(os.path.abspath(Path(step_path).expanduser()))),
            "project": str(Path(os.path.abspath(Path(project_path).expanduser()))),
            "cases": str(Path(os.path.abspath(Path(cases_path).expanduser()))),
            "markers": str(Path(os.path.abspath(Path(markers_path).expanduser()))),
            "topology_smoke_config": str(
                Path(os.path.abspath(Path(topology_smoke_path).expanduser()))
            ),
        },
        "requested_case": str(case_id),
    }


def _project_required_mesh_markers(inputs: RealConnectionInputs) -> list[str]:
    """Return only two-dimensional boundaries that belong in the SU2 mesh.

    A post-processing measurement section can be dimension two while being
    explicitly declared ``solver_boundary = false``.  Such a section must not
    become a Gmsh Physical Group or an SU2 marker merely because it is stored
    beside boundary definitions.
    """

    return sorted(
        name
        for name, specification in inputs.markers.items()
        if specification.get("dimension") == 2
        and specification.get("solver_boundary") is True
    )


def _project_gmsh_marker_mapping(
    inputs: RealConnectionInputs,
) -> dict[str, dict[str, Any]]:
    """Return the persistent tag-free selector view used for audit only.

    Runtime Gmsh entity tags are resolved inside the owned BREP session and are
    never projected from ``markers.toml``.
    """

    return {
        name: {
            "dimension": specification["dimension"],
            "role": specification["role"],
            "solver_boundary": bool(specification["solver_boundary"]),
            "selector": (
                "stable_surface_fingerprints"
                if specification["dimension"] == 2
                else "stable_volume_fingerprints"
            ),
            "member_fingerprint_ids": list(
                specification.get("member_fingerprint_ids", [])
            ),
        }
        for name, specification in inputs.markers.items()
        if specification.get("dimension") == 3
        or specification.get("solver_boundary") is True
    }


def _solver_marker_name_for_role(
    inputs: RealConnectionInputs, role: object
) -> str:
    normalized_role = str(role).strip() if isinstance(role, str) else ""
    matches = [
        name
        for name, specification in inputs.markers.items()
        if specification.get("dimension") == 2
        and specification.get("solver_boundary") is True
        and specification.get("role") == normalized_role
    ]
    if len(matches) != 1:
        raise RealConnectionConfigurationGateError(
            "MISSING_OR_AMBIGUOUS_SOLVER_MARKER",
            f"role {normalized_role!r} must map to exactly one solver marker",
        )
    return matches[0]


def _rear_outlet_pilot_gate(
    inputs: RealConnectionInputs,
    pilot_approval_path: str | os.PathLike[str],
    *,
    mesh_level: str,
    nproc: int,
    max_iterations: int,
) -> dict[str, Any]:
    """Authorize only the separately approved, non-production outlet pilot."""

    project = inputs.project
    conditions = project.get("boundary_conditions")
    rear = conditions.get("rear_outlets") if isinstance(conditions, Mapping) else None
    if not isinstance(rear, Mapping):
        raise RealConnectionConfigurationGateError(
            "MISSING_REAR_OUTLET_CONFIGURATION",
            "project.toml has no [boundary_conditions.rear_outlets] table",
        )
    mode = rear.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise RealConnectionConfigurationGateError(
            "MISSING_REAR_OUTLET_MODE",
            "rear outlet mode is missing",
        )
    project_mode = mode.strip().upper()
    if project_mode != "TBD_AFTER_SUPERSONIC_PILOT":
        raise RealConnectionConfigurationGateError(
            "UNVERIFIED_REAR_OUTLET_MODE",
            f"rear outlet mode {mode!r} has no currently verified SU2 8.5.0 renderer",
        )

    approval = Path(pilot_approval_path).expanduser()
    if ".." in approval.parts:
        raise RealConnectionConfigurationGateError(
            "UNSAFE_REAR_OUTLET_PILOT_APPROVAL",
            "rear outlet pilot approval path must not contain '..'",
        )
    approval = Path(os.path.abspath(approval))
    config_root = inputs.repository_root / "config"
    if not _path_within(approval, config_root):
        raise RealConnectionConfigurationGateError(
            "UNSAFE_REAR_OUTLET_PILOT_APPROVAL",
            "rear outlet pilot approval must stay under repository config",
        )
    relative = approval.relative_to(inputs.repository_root)
    current = inputs.repository_root
    for component in relative.parts:
        current = current / component
        if _lexists(current) and _is_reparse(current):
            raise RealConnectionConfigurationGateError(
                "UNSAFE_REAR_OUTLET_PILOT_APPROVAL",
                "rear outlet pilot approval path crosses a link or reparse point",
            )
    if not approval.is_file():
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_BOUNDARY_NOT_FROZEN",
            "project mode is TBD_AFTER_SUPERSONIC_PILOT and no approved "
            "connection-only pilot contract is present; SU2_CFD must not start",
        )
    try:
        with approval.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_PILOT_APPROVAL",
            f"cannot parse rear outlet pilot approval: {error}",
        ) from error

    expected_root_keys = {
        "schema",
        "status",
        "approval",
        "provenance",
        "scope",
        "rear_outlet_boundary",
    }
    if set(document) != expected_root_keys:
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_PILOT_APPROVAL",
            "rear outlet pilot approval root keys are incomplete or unexpected",
        )
    approval_record = document.get("approval")
    provenance = document.get("provenance")
    scope = document.get("scope")
    boundary = document.get("rear_outlet_boundary")
    if (
        document.get("schema") != "cfdpipe.rear_outlet_pilot.v1"
        or document.get("status") != "APPROVED_FOR_CONNECTION_ONLY"
        or not isinstance(approval_record, Mapping)
        or set(approval_record) != {"authority", "approved_at_utc", "decision"}
        or approval_record.get("authority") != "project_owner"
        or approval_record.get("decision")
        != "implement_recommended_provisional_supersonic_connection_pilot"
        or not isinstance(approval_record.get("approved_at_utc"), str)
        or not str(approval_record["approved_at_utc"]).endswith("Z")
    ):
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_PILOT_APPROVAL",
            "rear outlet pilot approval status or project-owner decision is invalid",
        )

    expected_provenance_keys = {
        "project_sha256",
        "cases_sha256",
        "markers_sha256",
        "topology_smoke_sha256",
        "source_step_sha256",
    }
    expected_hashes = {
        "project_sha256": inputs.input_records["project"].get("sha256"),
        "cases_sha256": inputs.input_records["cases"].get("sha256"),
        "markers_sha256": inputs.input_records["markers"].get("sha256"),
        "topology_smoke_sha256": inputs.input_records[
            "topology_smoke_config"
        ].get("sha256"),
        "source_step_sha256": inputs.input_records["step"].get("sha256"),
    }
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != expected_provenance_keys
        or any(provenance.get(key) != value for key, value in expected_hashes.items())
    ):
        raise RealConnectionConfigurationGateError(
            "STALE_REAR_OUTLET_PILOT_APPROVAL",
            "rear outlet pilot approval hashes do not match current project inputs",
        )

    expected_scope_keys = {
        "case_id",
        "mesh_level",
        "solver",
        "nproc",
        "max_iterations",
        "gpu_enabled",
        "connection_only",
        "production_eligible",
        "physical_interpretation_allowed",
    }
    if (
        not isinstance(scope, Mapping)
        or set(scope) != expected_scope_keys
        or scope.get("case_id") != inputs.requested_case_id
        or scope.get("mesh_level") != mesh_level
        or scope.get("solver") != "EULER"
        or scope.get("nproc") != nproc
        or scope.get("max_iterations") != max_iterations
        or scope.get("gpu_enabled") is not False
        or scope.get("connection_only") is not True
        or scope.get("production_eligible") is not False
        or scope.get("physical_interpretation_allowed") is not False
    ):
        raise RealConnectionConfigurationGateError(
            "PILOT_SCOPE_MISMATCH",
            "runtime arguments exceed or differ from the approved connection-only scope",
        )

    boundaries = project.get("boundaries")
    rear_roles = (
        boundaries.get("rear_outlet_roles") if isinstance(boundaries, Mapping) else None
    )
    expected_boundary_keys = {
        "project_mode_required",
        "pilot_mode",
        "su2_option",
        "roles",
        "boundary_mode_frozen",
        "allow_static_pressure",
        "allow_back_pressure",
        "validation_status",
    }
    if (
        not isinstance(boundary, Mapping)
        or set(boundary) != expected_boundary_keys
        or boundary.get("project_mode_required") != project_mode
        or boundary.get("pilot_mode") != "PROVISIONAL_SUPERSONIC_PILOT"
        or boundary.get("su2_option") != "MARKER_SUPERSONIC_OUTLET"
        or boundary.get("roles") != rear_roles
        or boundary.get("boundary_mode_frozen") is not False
        or boundary.get("allow_static_pressure") is not False
        or boundary.get("allow_back_pressure") is not False
        or boundary.get("validation_status")
        != "NOT_EVALUATED_BY_CONNECTION_PILOT"
    ):
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_PILOT_BOUNDARY_CONTRACT",
            "rear outlet pilot boundary contract is incomplete, stale, or unsafe",
        )
    return {
        "status": "PASS",
        "approval_path": str(approval),
        "approval_sha256": _sha256(approval),
        "approval": dict(approval_record),
        "provenance": dict(provenance),
        "scope": dict(scope),
        "rear_outlet_boundary": dict(boundary),
        "project_configured_mode": project_mode,
        "effective_mode": "PROVISIONAL_SUPERSONIC_PILOT",
        "connection_only": True,
        "production_eligible": False,
        "boundary_mode_frozen": False,
        "pressure_or_backpressure_guessed": False,
    }


def _case_specific_rear_outlet_freeze_gate(
    inputs: RealConnectionInputs,
    freeze_path: Path,
) -> dict[str, Any]:
    """Authorize one evaluated design-case freeze without changing global policy."""

    project = inputs.project
    conditions = project.get("boundary_conditions")
    rear = conditions.get("rear_outlets") if isinstance(conditions, Mapping) else None
    if not isinstance(rear, Mapping):
        raise RealConnectionConfigurationGateError(
            "MISSING_REAR_OUTLET_CONFIGURATION",
            "project.toml has no [boundary_conditions.rear_outlets] table",
        )
    mode = rear.get("mode")
    if not isinstance(mode, str) or not mode.strip():
        raise RealConnectionConfigurationGateError(
            "MISSING_REAR_OUTLET_MODE",
            "rear outlet mode is missing",
        )
    project_mode = mode.strip().upper()
    if project_mode != "TBD_AFTER_SUPERSONIC_PILOT":
        raise RealConnectionConfigurationGateError(
            "UNVERIFIED_REAR_OUTLET_MODE",
            f"rear outlet mode {mode!r} has no currently verified SU2 8.5.0 renderer",
        )

    config_root = inputs.repository_root / "config"
    if not _path_within(freeze_path, config_root):
        raise RealConnectionConfigurationGateError(
            "UNSAFE_REAR_OUTLET_FREEZE_CONTRACT",
            "rear outlet freeze contract must stay under repository config",
        )
    relative = freeze_path.relative_to(inputs.repository_root)
    current = inputs.repository_root
    for component in relative.parts:
        current = current / component
        if _lexists(current) and _is_reparse(current):
            raise RealConnectionConfigurationGateError(
                "UNSAFE_REAR_OUTLET_FREEZE_CONTRACT",
                "rear outlet freeze contract path crosses a link or reparse point",
            )
    if not freeze_path.is_file():
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_FREEZE_CONTRACT",
            "rear outlet freeze path exists but is not a regular file",
        )

    freeze_sha_before = _sha256(freeze_path)
    try:
        evaluation = evaluate_rear_outlet_freeze(freeze_path)
    except Exception as error:
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_EVALUATION_FAILED",
            f"rear outlet freeze evaluator failed: {error}",
        ) from error
    if not isinstance(evaluation, Mapping):
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_EVALUATION_FAILED",
            "rear outlet freeze evaluator did not return an evidence report",
        )
    freeze_sha_after = _sha256(freeze_path)
    if freeze_sha_after != freeze_sha_before:
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_CONTRACT_CHANGED",
            "rear outlet freeze contract changed while its evidence was evaluated",
        )
    if evaluation.get("status") != "PASS":
        errors = evaluation.get("errors")
        detail = ""
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
            codes = [
                str(item.get("code"))
                for item in errors
                if isinstance(item, Mapping) and item.get("code")
            ]
            if codes:
                detail = ": " + ", ".join(codes[:8])
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_EVIDENCE_FAILED",
            "case-specific rear outlet freeze evidence did not PASS" + detail,
        )

    scope = evaluation.get("scope")
    evaluated_case = scope.get("case_id") if isinstance(scope, Mapping) else None
    if evaluated_case != inputs.requested_case_id:
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_CASE_MISMATCH",
            "case-specific rear outlet freeze cannot be borrowed by another case: "
            f"contract={evaluated_case!r}, requested={inputs.requested_case_id!r}",
        )
    if evaluation.get("boundary_mode_frozen") is not True:
        raise RealConnectionConfigurationGateError(
            "REAR_OUTLET_FREEZE_NOT_FROZEN",
            "freeze evidence PASS did not explicitly set boundary_mode_frozen=true",
        )
    if evaluation.get("production_eligible") is not False:
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_FREEZE_SCOPE",
            "case-specific boundary freeze must keep production_eligible=false",
        )

    boundary = evaluation.get("boundary")
    boundaries = project.get("boundaries")
    rear_roles = (
        boundaries.get("rear_outlet_roles")
        if isinstance(boundaries, Mapping)
        else None
    )
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("mode") != "supersonic_outlet"
        or boundary.get("su2_option") != "MARKER_SUPERSONIC_OUTLET"
        or boundary.get("rear_outlet_markers") != rear_roles
        or boundary.get("boundary_mode_frozen") is not True
        or boundary.get("allow_static_pressure") is not False
        or boundary.get("allow_back_pressure") is not False
    ):
        raise RealConnectionConfigurationGateError(
            "INVALID_REAR_OUTLET_FREEZE_BOUNDARY_CONTRACT",
            "evaluated freeze boundary is incomplete, case-unsafe, or requests pressure",
        )

    return {
        "status": "PASS",
        "approval_path": str(freeze_path),
        "approval_sha256": freeze_sha_after,
        "scope": dict(scope),
        "rear_outlet_boundary": dict(boundary),
        "freeze_evaluation": dict(evaluation),
        "project_configured_mode": project_mode,
        "effective_mode": "supersonic_outlet",
        "su2_option": "MARKER_SUPERSONIC_OUTLET",
        "case_specific": True,
        "connection_only": True,
        "production_eligible": False,
        "boundary_mode_frozen": True,
        "pressure_or_backpressure_guessed": False,
    }


def _rear_outlet_gate(
    inputs: RealConnectionInputs,
    pilot_approval_path: str | os.PathLike[str],
    *,
    mesh_level: str,
    nproc: int,
    max_iterations: int,
) -> dict[str, Any]:
    """Prefer a case-specific PASS freeze; otherwise retain the legacy pilot."""

    freeze_path = inputs.repository_root / "config" / "rear_outlet_freeze.toml"
    if _lexists(freeze_path):
        return _case_specific_rear_outlet_freeze_gate(inputs, freeze_path)
    return _rear_outlet_pilot_gate(
        inputs,
        pilot_approval_path,
        mesh_level=mesh_level,
        nproc=nproc,
        max_iterations=max_iterations,
    )


class RealConnectionRunner:
    """Run the real-input connection quality gates without production work."""

    def __init__(
        self,
        *,
        step_path: str | os.PathLike[str],
        project_path: str | os.PathLike[str],
        cases_path: str | os.PathLike[str],
        markers_path: str | os.PathLike[str],
        case_id: str,
        mesh_level: str,
        nproc: int,
        max_iterations: int,
        output_directory: str | os.PathLike[str],
        allowed_output_root: str | os.PathLike[str],
        trusted_repository_root: str | os.PathLike[str],
        toolchain: Toolchain,
        paraview_script_directory: str | os.PathLike[str],
        topology_smoke_path: str | os.PathLike[str] | None = None,
        rear_outlet_pilot_path: str | os.PathLike[str] | None = None,
        smoke_characteristic_length_m: float = 0.25,
        facet_overlap_angle_tolerance_degrees: float = 0.1,
        gmsh_algorithm_3d: int = 1,
        timeout_seconds: float = 300.0,
        live_output: bool = True,
        controller_argv: Sequence[str] = (),
    ) -> None:
        if mesh_level != "smoke":
            raise ValueError("real connection pipeline only permits --mesh-level smoke")
        if nproc != 1:
            raise ValueError("real connection pipeline is serial; --nproc must be 1")
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or not 1 <= max_iterations <= 5
        ):
            raise ValueError("--max-iterations must be an integer between 1 and 5")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(
            timeout_seconds, bool
        ):
            raise TypeError("timeout_seconds must be a real number")
        if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if (
            isinstance(smoke_characteristic_length_m, bool)
            or not isinstance(smoke_characteristic_length_m, (int, float))
            or not math.isfinite(float(smoke_characteristic_length_m))
            or not 0.01 <= float(smoke_characteristic_length_m) <= 2.0
        ):
            raise ValueError(
                "smoke_characteristic_length_m must be between 0.01 and 2.0 m"
            )
        if (
            isinstance(facet_overlap_angle_tolerance_degrees, bool)
            or not isinstance(facet_overlap_angle_tolerance_degrees, (int, float))
            or not math.isfinite(float(facet_overlap_angle_tolerance_degrees))
            or not 1.0e-6
            <= float(facet_overlap_angle_tolerance_degrees)
            <= 0.1
        ):
            raise ValueError(
                "facet_overlap_angle_tolerance_degrees must be between 1e-6 "
                "and 0.1"
            )
        if (
            isinstance(gmsh_algorithm_3d, bool)
            or not isinstance(gmsh_algorithm_3d, int)
            or gmsh_algorithm_3d not in {1, 4, 10}
        ):
            raise ValueError("gmsh_algorithm_3d must be 1, 4, or 10")
        self.step_path = Path(step_path)
        self.project_path = Path(project_path)
        self.cases_path = Path(cases_path)
        self.markers_path = Path(markers_path)
        self.topology_smoke_path = Path(
            topology_smoke_path
            if topology_smoke_path is not None
            else Path(trusted_repository_root) / "config" / "topology_smoke.toml"
        )
        self.rear_outlet_pilot_path = Path(
            rear_outlet_pilot_path
            if rear_outlet_pilot_path is not None
            else Path(trusted_repository_root) / "config" / "rear_outlet_pilot.toml"
        )
        self.case_id = str(case_id)
        self.mesh_level = mesh_level
        self.nproc = nproc
        self.max_iterations = max_iterations
        self.output_directory = Path(output_directory)
        self.allowed_output_root = Path(allowed_output_root)
        self.trusted_repository_root = Path(trusted_repository_root)
        self.toolchain = toolchain
        self.paraview_script_directory = Path(paraview_script_directory)
        self.smoke_characteristic_length_m = float(smoke_characteristic_length_m)
        self.facet_overlap_angle_tolerance_degrees = float(
            facet_overlap_angle_tolerance_degrees
        )
        self.gmsh_algorithm_3d = gmsh_algorithm_3d
        self.timeout_seconds = float(timeout_seconds)
        self.live_output = bool(live_output)
        self.controller_argv = tuple(str(value) for value in controller_argv)

    def _runner(self, output_dir: Path) -> CommandRunner:
        return CommandRunner(
            output_dir=output_dir,
            live_output=self.live_output,
            allow_mnt_c_executables=self.toolchain.allow_mnt_c_executables,
        )

    def run(self) -> RealConnectionResult:
        started_at = _utc_now()
        root = _assert_safe_root(
            self.output_directory,
            self.allowed_output_root,
            self.trusted_repository_root,
        )
        previous_run_archive = _archive_previous_real_run(root)
        state = _new_real_state(self.controller_argv)
        state["smoke_characteristic_length_m"] = (
            self.smoke_characteristic_length_m
        )
        state["facet_overlap_angle_tolerance_degrees"] = (
            self.facet_overlap_angle_tolerance_degrees
        )
        state["gmsh_algorithm_3d"] = self.gmsh_algorithm_3d
        state["max_iterations"] = self.max_iterations
        state["rear_outlet_pilot_path"] = str(
            Path(os.path.abspath(self.rear_outlet_pilot_path))
        )
        state["previous_run_archive"] = (
            None if previous_run_archive is None else str(previous_run_archive)
        )
        initial_report = build_real_connection_report(
            root,
            state,
            started_at=started_at,
            ended_at=_utc_now(),
        )
        report_path = write_real_connection_report(root, initial_report)
        request_evidence = _real_input_request_evidence(
            step_path=self.step_path,
            project_path=self.project_path,
            cases_path=self.cases_path,
            markers_path=self.markers_path,
            topology_smoke_path=self.topology_smoke_path,
            case_id=self.case_id,
        )
        inputs: RealConnectionInputs | None = None
        selected_case: dict[str, Any] | None = None
        mesh_validation: dict[str, Any] | None = None
        gmsh_manifest: dict[str, Any] | None = None
        config_manifest: dict[str, Any] | None = None
        su2_manifest: dict[str, Any] | None = None
        paraview_manifest: dict[str, Any] | None = None
        pilot_contract: dict[str, Any] | None = None
        su2_tool: ResolvedTool | None = None
        pipeline_failure: BaseException | None = None
        pipeline_log: Path | None = None

        def input_stage() -> None:
            nonlocal inputs
            state["attempted"]["input"] = True
            stage_started = _utc_now()
            try:
                inputs = load_real_connection_inputs(
                    step_path=self.step_path,
                    project_path=self.project_path,
                    cases_path=self.cases_path,
                    markers_path=self.markers_path,
                    topology_smoke_path=self.topology_smoke_path,
                    case_id=self.case_id,
                    trusted_repository_root=self.trusted_repository_root,
                )
            except BaseException as error:
                error_payload: dict[str, Any] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    ),
                }
                if isinstance(error, RealConnectionInputError):
                    error_payload["issues"] = [dict(issue) for issue in error.issues]
                manifest = {
                    "status": "FAIL",
                    "stage": "input",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    **request_evidence,
                    "error": error_payload,
                }
                state["manifests"]["input"] = manifest
                _write_real_stage(
                    root,
                    "input/input_manifest.json",
                    "input/input.log",
                    manifest,
                )
                state["links"]["step_to_gmsh"] = {
                    "status": "FAIL",
                    "evidence": [
                        {
                            **request_evidence,
                            "reason": "INPUT_PREFLIGHT_FAILED",
                            "issues": error_payload.get("issues", []),
                            "started_at": stage_started,
                            "ended_at": manifest["ended_at"],
                        }
                    ],
                }
                for key in REAL_CONNECTION_KEYS[1:]:
                    state["links"][key] = _real_not_run(
                        "real input preflight failed", upstream_stage="input"
                    )
                raise
            manifest = {
                "status": "PASS",
                "stage": "input",
                "started_at": stage_started,
                "ended_at": _utc_now(),
                "inputs": inputs.input_records,
                "requested_case": inputs.requested_case_id,
                "unit_conversion": {
                    "source_step_unit": "mm",
                    "pipeline_brep_unit": "m",
                    "conversion_stage": "validated shared-topology repair",
                    "meshing_import_scaling": "none; BREP is already SI",
                },
                "marker_names": sorted(inputs.markers),
                "measurement_names": sorted(inputs.measurements),
            }
            state["manifests"]["input"] = manifest
            _write_real_stage(
                root,
                "input/input_manifest.json",
                "input/input.log",
                manifest,
            )

        def gmsh_stage() -> None:
            nonlocal gmsh_manifest
            if inputs is None:
                raise RealConnectionError("input stage produced no inputs")
            state["attempted"]["gmsh"] = True
            stage_started = _utc_now()
            step_before_hash = _sha256(inputs.step_path)
            brep_before_hash = _sha256(inputs.pipeline_geometry_path)
            try:
                result = build_topology_smoke_mesh(
                    inputs.pipeline_geometry_path,
                    root / "mesh",
                    inputs.marker_document,
                    mesh_options={
                        "characteristic_length_m": (
                            self.smoke_characteristic_length_m
                        ),
                        "facet_overlap_angle_tolerance_degrees": (
                            self.facet_overlap_angle_tolerance_degrees
                        ),
                        "algorithm_3d": self.gmsh_algorithm_3d,
                        "local_refinement": inputs.topology_smoke_refinement,
                    },
                    max_elements=_REAL_MAX_ELEMENTS,
                )
                step_after_hash = _sha256(inputs.step_path)
                brep_after_hash = _sha256(inputs.pipeline_geometry_path)
                if step_after_hash != step_before_hash:
                    raise RealConnectionError("original STEP changed during Gmsh stage")
                if brep_after_hash != brep_before_hash:
                    raise RealConnectionError("pipeline BREP changed during Gmsh stage")
                gmsh_manifest = {
                    "status": "PASS",
                    "stage": "gmsh",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "source_step": {
                        "path": str(inputs.step_path),
                        "sha256_before": step_before_hash,
                        "sha256_after": step_after_hash,
                        "usage": "read-only provenance; not imported for meshing",
                    },
                    "pipeline_geometry": {
                        "path": str(inputs.pipeline_geometry_path),
                        "sha256_before": brep_before_hash,
                        "sha256_after": brep_after_hash,
                        "format": "brep",
                        "length_unit": "m",
                        "usage": "only CAD imported into the Gmsh meshing session",
                    },
                    "unit_conversion": (
                        "performed and independently verified in the repair stage; "
                        "no raw STEP import in this stage"
                    ),
                    "mesh_level": self.mesh_level,
                    "characteristic_length_m": self.smoke_characteristic_length_m,
                    "facet_overlap_angle_tolerance_degrees": (
                        self.facet_overlap_angle_tolerance_degrees
                    ),
                    "gmsh_algorithm_3d": self.gmsh_algorithm_3d,
                    "topology_smoke_config": dict(
                        inputs.input_records["topology_smoke_config"]
                    ),
                    "max_elements": _REAL_MAX_ELEMENTS,
                    **dict(result),
                }
            except BaseException as error:
                step_after_hash = _sha256(inputs.step_path)
                brep_after_hash = _sha256(inputs.pipeline_geometry_path)
                gmsh_manifest = {
                    "status": "FAIL",
                    "stage": "gmsh",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "source_step": {
                        "path": str(inputs.step_path),
                        "sha256_before": step_before_hash,
                        "sha256_after": step_after_hash,
                    },
                    "pipeline_geometry": {
                        "path": str(inputs.pipeline_geometry_path),
                        "sha256_before": brep_before_hash,
                        "sha256_after": brep_after_hash,
                    },
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                    },
                }
                state["links"]["step_to_gmsh"] = {
                    "status": "FAIL",
                    "evidence": [dict(gmsh_manifest)],
                }
                _write_real_stage(
                    root,
                    "mesh/gmsh_manifest.json",
                    "mesh/gmsh.log",
                    gmsh_manifest,
                )
                raise
            state["manifests"]["gmsh"] = gmsh_manifest
            state["links"]["step_to_gmsh"] = {
                "status": "PASS",
                "evidence": [dict(gmsh_manifest)],
            }
            _write_real_stage(
                root,
                "mesh/gmsh_manifest.json",
                "mesh/gmsh.log",
                gmsh_manifest,
            )

        def mesh_stage() -> None:
            nonlocal mesh_validation
            if inputs is None or gmsh_manifest is None:
                raise RealConnectionError("Gmsh stage produced no current manifest")
            state["attempted"]["mesh_validation"] = True
            stage_started = _utc_now()
            mesh_path = root / "mesh" / "mesh.su2"
            try:
                mesh_validation = validate_project_su2_mesh(
                    mesh_path,
                    _project_required_mesh_markers(inputs),
                    max_elements=_REAL_MAX_ELEMENTS,
                )
                manifest = {
                    "status": "PASS",
                    "stage": "mesh_validation",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "gmsh_topology": gmsh_manifest.get("topology"),
                    "gmsh_mesh_quality": (
                        gmsh_manifest.get("mesh", {}).get("quality")
                        if isinstance(gmsh_manifest.get("mesh"), Mapping)
                        else None
                    ),
                    "boundary_layer": (
                        gmsh_manifest.get("mesh", {}).get("boundary_layer")
                        if isinstance(gmsh_manifest.get("mesh"), Mapping)
                        else None
                    ),
                    **mesh_validation,
                }
            except BaseException as error:
                manifest = {
                    "status": "FAIL",
                    "stage": "mesh_validation",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "mesh_path": str(mesh_path),
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                    },
                }
                state["links"]["gmsh_to_su2_mesh"] = {
                    "status": "FAIL",
                    "evidence": [dict(manifest)],
                }
                _write_real_stage(
                    root,
                    "mesh/mesh_manifest.json",
                    "mesh/mesh_validation.log",
                    manifest,
                )
                raise
            state["manifests"]["mesh"] = manifest
            state["links"]["gmsh_to_su2_mesh"] = {
                "status": "PASS",
                "evidence": [dict(manifest)],
            }
            _write_real_stage(
                root,
                "mesh/mesh_manifest.json",
                "mesh/mesh_validation.log",
                manifest,
            )

        def case_config_stage() -> None:
            nonlocal selected_case, config_manifest, pilot_contract, su2_tool
            if inputs is None or mesh_validation is None:
                raise RealConnectionError("mesh quality gate did not produce evidence")
            state["attempted"]["case_config"] = True
            stage_started = _utc_now()
            selected_case = select_real_case(inputs)
            try:
                pilot_contract = _rear_outlet_gate(
                    inputs,
                    self.rear_outlet_pilot_path,
                    mesh_level=self.mesh_level,
                    nproc=self.nproc,
                    max_iterations=self.max_iterations,
                )
                state["pilot_contract"] = dict(pilot_contract)
                su2_tool = self.toolchain.resolve("su2_cfd")
                state["resolved_tools"]["su2_cfd"] = su2_tool.as_dict()

                boundaries = inputs.project.get("boundaries")
                if not isinstance(boundaries, Mapping):
                    raise RealConnectionConfigurationGateError(
                        "MISSING_PROJECT_BOUNDARIES",
                        "project.toml needs [boundaries]",
                    )
                farfield_marker = _solver_marker_name_for_role(
                    inputs, boundaries.get("farfield_role")
                )
                wall_marker = _solver_marker_name_for_role(
                    inputs, boundaries.get("wall_role")
                )
                rear_roles = boundaries.get("rear_outlet_roles")
                if (
                    not isinstance(rear_roles, Sequence)
                    or isinstance(rear_roles, (str, bytes))
                    or len(rear_roles) != 2
                ):
                    raise RealConnectionConfigurationGateError(
                        "INVALID_REAR_OUTLET_ROLES",
                        "project must define exactly two rear outlet roles",
                    )
                rear_markers = [
                    _solver_marker_name_for_role(inputs, role) for role in rear_roles
                ]
                prepared = prepare_project_supersonic_pilot_case(
                    su2_tool.path,
                    root / "mesh" / "mesh.su2",
                    root / "su2",
                    selected_case,
                    farfield_marker=farfield_marker,
                    wall_marker=wall_marker,
                    rear_outlet_markers=rear_markers,
                    max_iterations=self.max_iterations,
                    mesh_validation=mesh_validation,
                )
                config_manifest = {
                    "status": "PASS",
                    "stage": "case_config",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "selected_case": dict(selected_case),
                    "pilot_approval": dict(pilot_contract),
                    "su2_cfd": su2_tool.as_dict(),
                    **dict(prepared),
                }
            except BaseException as error:
                error_payload: dict[str, Any] = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": "".join(
                        traceback.format_exception(type(error), error, error.__traceback__)
                    ),
                }
                if isinstance(error, RealConnectionConfigurationGateError):
                    error_payload["code"] = error.code
                config_manifest = {
                    "status": "FAIL",
                    "stage": "case_config",
                    "started_at": stage_started,
                    "ended_at": _utc_now(),
                    "selected_case": selected_case,
                    "mesh_path": str(root / "mesh" / "mesh.su2"),
                    "mesh_sha256": mesh_validation["sha256"],
                    "nproc": 1,
                    "max_iterations": self.max_iterations,
                    "gpu_enabled": False,
                    "pilot_approval_path": str(
                        Path(os.path.abspath(self.rear_outlet_pilot_path))
                    ),
                    "error": error_payload,
                }
                state["manifests"]["config"] = config_manifest
                state["links"]["mesh_to_su2"] = {
                    "status": "FAIL",
                    "evidence": [
                        {
                            **dict(config_manifest),
                            "reason": "SU2_CONFIGURATION_GATE_BLOCKED",
                        }
                    ],
                }
                _write_real_stage(
                    root,
                    "su2/config_manifest.json",
                    "su2/config.log",
                    config_manifest,
                )
                raise
            state["manifests"]["config"] = config_manifest
            _write_real_stage(
                root,
                "su2/config_manifest.json",
                "su2/config.log",
                config_manifest,
            )

        def su2_stage() -> None:
            nonlocal su2_manifest
            state["attempted"]["su2"] = True
            if (
                config_manifest is None
                or config_manifest.get("status") != "PASS"
                or mesh_validation is None
                or su2_tool is None
            ):
                raise RealConnectionConfigurationGateError(
                    "SU2_NOT_AUTHORIZED_WITHOUT_PASS_CONFIG_GATE",
                    "SU2_CFD cannot run until the project configuration gate passes",
                )
            bridge = SU2Bridge(self._runner(root / "su2"), su2_tool.path)
            try:
                su2_manifest = bridge.run_project_pilot_case(
                    root / "su2" / "case.cfg",
                    root / "su2" / "mesh.su2",
                    root / "su2",
                    mesh_validation,
                    max_iterations=self.max_iterations,
                    timeout_seconds=self.timeout_seconds,
                    live_output=self.live_output,
                )
                if su2_manifest.get("status") != "PASS":
                    raise RealConnectionError("SU2 project pilot manifest is not PASS")
            except BaseException as error:
                manifest_path = root / "su2" / "run_manifest.json"
                if manifest_path.is_file():
                    try:
                        su2_manifest = _load_json(manifest_path)
                    except BaseException:
                        su2_manifest = None
                evidence = (
                    {**dict(su2_manifest), "status": "FAIL", "stage": "su2"}
                    if isinstance(su2_manifest, Mapping)
                    else {
                        "status": "FAIL",
                        "stage": "su2",
                        "reason": "SU2_PROJECT_PILOT_FAILED",
                        "error": {
                            "type": type(error).__name__,
                            "message": str(error),
                            "traceback": "".join(
                                traceback.format_exception(
                                    type(error), error, error.__traceback__
                                )
                            ),
                        },
                    }
                )
                su2_manifest = evidence
                state["manifests"]["su2"] = evidence
                state["links"]["mesh_to_su2"] = {
                    "status": "FAIL",
                    "evidence": [evidence],
                }
                _write_real_stage(
                    root,
                    "su2/run_manifest.json",
                    "su2/su2_stage.log",
                    evidence,
                )
                raise
            state["manifests"]["su2"] = su2_manifest
            state["links"]["mesh_to_su2"] = {
                "status": "PASS",
                "evidence": [dict(su2_manifest)],
            }
            _write_real_stage(
                root,
                "su2/run_manifest.json",
                "su2/su2_stage.log",
                su2_manifest,
            )

        def visualization_stage() -> None:
            state["attempted"]["visualization"] = True
            if su2_manifest is None or su2_manifest.get("status") != "PASS":
                raise RealConnectionError("SU2 produced no current PASS manifest")
            try:
                raw_visualization = su2_manifest.get("visualization_file")
                if not isinstance(raw_visualization, str) or not raw_visualization:
                    raise RealConnectionError(
                        "SU2 manifest has no visualization_file"
                    )
                visualization = Path(raw_visualization).expanduser().resolve(strict=True)
                su2_root = (root / "su2").resolve(strict=True)
                if visualization.parent != su2_root or not visualization.is_file():
                    raise RealConnectionError(
                        "SU2 visualization_file is outside the current SU2 run directory"
                    )
                validation = validate_visualization_file(
                    visualization,
                    expected_points=mesh_validation["npoin"]
                    if mesh_validation is not None
                    else None,
                    expected_cells=mesh_validation["nelem"]
                    if mesh_validation is not None
                    else None,
                )
                declared = su2_manifest.get("visualization_validation")
                if (
                    not isinstance(declared, Mapping)
                    or declared.get("sha256") != validation.get("sha256")
                ):
                    raise RealConnectionError(
                        "SU2 visualization validation/hash evidence is missing or stale"
                    )
                evidence = {
                    "visualization_file": str(visualization),
                    "visualization_validation": validation,
                    "su2_return_code": su2_manifest.get("return_code"),
                    "started_at": su2_manifest.get("start_time"),
                    "ended_at": su2_manifest.get("end_time"),
                }
            except BaseException as error:
                state["links"]["su2_to_visualization_file"] = {
                    "status": "FAIL",
                    "evidence": [
                        {
                            "reason": "SU2_VISUALIZATION_VALIDATION_FAILED",
                            "error": str(error),
                        }
                    ],
                }
                raise
            state["links"]["su2_to_visualization_file"] = {
                "status": "PASS",
                "evidence": [evidence],
            }

        def paraview_stage() -> None:
            nonlocal paraview_manifest
            state["attempted"]["paraview"] = True
            if state["links"]["su2_to_visualization_file"]["status"] != "PASS":
                raise RealConnectionError(
                    "pvbatch cannot run without a validated current SU2 result"
                )
            try:
                pvbatch_tool = self.toolchain.resolve("pvbatch")
                state["resolved_tools"]["pvbatch"] = pvbatch_tool.as_dict()
                bridge = ParaViewBridge(
                    self._runner(root / "paraview"),
                    pvbatch_tool.path,
                    self.paraview_script_directory,
                    pvbatch_source=pvbatch_tool.source,
                )
                paraview_manifest = bridge.inspect_manifest(
                    root / "su2" / "run_manifest.json",
                    root / "paraview",
                    timeout_seconds=self.timeout_seconds,
                    live_output=self.live_output,
                )
                if paraview_manifest.get("status") != "PASS":
                    raise RealConnectionError("ParaView project manifest is not PASS")
                generated_data_outputs = _validate_real_paraview_outputs(
                    root, paraview_manifest
                )
            except BaseException as error:
                manifest_path = root / "paraview" / "paraview_manifest.json"
                if manifest_path.is_file():
                    try:
                        paraview_manifest = _load_json(manifest_path)
                    except BaseException:
                        paraview_manifest = None
                evidence: dict[str, Any] = {
                    "status": "FAIL",
                    "stage": "paraview",
                    "reason": "PVBATCH_PROJECT_POSTPROCESS_FAILED",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error),
                        "traceback": "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                    },
                }
                if isinstance(paraview_manifest, Mapping):
                    evidence["bridge_manifest"] = dict(paraview_manifest)
                paraview_manifest = evidence
                state["manifests"]["paraview"] = evidence
                state["links"]["visualization_file_to_pvbatch"] = {
                    "status": "FAIL",
                    "evidence": [evidence],
                }
                state["links"]["pvbatch_to_json"] = {
                    "status": "FAIL",
                    "evidence": [evidence],
                }
                _write_real_stage(
                    root,
                    "paraview/paraview_manifest.json",
                    "paraview/paraview_stage.log",
                    evidence,
                )
                raise
            state["manifests"]["paraview"] = paraview_manifest
            state["links"]["visualization_file_to_pvbatch"] = {
                "status": "PASS",
                "evidence": [
                    {
                        "pvbatch_path": paraview_manifest.get("pvbatch_path"),
                        "paraview_version": paraview_manifest.get(
                            "paraview_version"
                        ),
                        "commands": paraview_manifest.get("commands"),
                        "input_solution_path": paraview_manifest.get(
                            "input_solution_path"
                        ),
                    }
                ],
            }
            state["links"]["pvbatch_to_json"] = {
                "status": "PASS",
                "evidence": [
                    {
                        "dataset_type": paraview_manifest.get("dataset_type"),
                        "number_of_points": paraview_manifest.get(
                            "number_of_points"
                        ),
                        "number_of_cells": paraview_manifest.get(
                            "number_of_cells"
                        ),
                        "detected_arrays": paraview_manifest.get(
                            "detected_arrays"
                        ),
                        "output_files": generated_data_outputs,
                    }
                ],
            }
            _write_real_stage(
                root,
                "paraview/paraview_manifest.json",
                "paraview/paraview_stage.log",
                paraview_manifest,
            )

        stages = (
            PipelineStage("input", input_stage),
            PipelineStage("gmsh-step", gmsh_stage),
            PipelineStage("mesh-validation", mesh_stage),
            PipelineStage("case-config", case_config_stage),
            PipelineStage("su2-cfd", su2_stage),
            PipelineStage("visualization", visualization_stage),
            PipelineStage("pvbatch", paraview_stage),
        )
        try:
            pipeline_result = Pipeline(root).run(stages, name="real-connection")
            pipeline_log = pipeline_result.log_path
        except PipelineStageError as error:
            pipeline_failure = error.cause
            pipeline_log = error.log_path

        if pipeline_failure is not None:
            failed_stage = (
                "unknown"
                if pipeline_log is None
                else _load_json(pipeline_log).get("stages", [{}])[-1].get(
                    "name", "unknown"
                )
            )
            for key in REAL_CONNECTION_KEYS:
                if state["links"][key]["status"] != "PASS" and not any(
                    isinstance(item, Mapping)
                    and item.get("reason")
                    not in {None, "NOT_RUN_DUE_TO_UPSTREAM_FAILURE"}
                    for item in state["links"][key]["evidence"]
                ):
                    state["links"][key] = _real_not_run(
                        str(pipeline_failure), upstream_stage=str(failed_stage)
                    )

        _write_real_not_run_manifests(root)
        ended_at = _utc_now()
        report = build_real_connection_report(
            root,
            state,
            started_at=started_at,
            ended_at=ended_at,
            failure=pipeline_failure,
            pipeline_log=pipeline_log,
        )
        report_path = write_real_connection_report(root, report)
        pipeline_payload = {
            "status": "PASS" if report["overall"] == "PASS" else "FAIL",
            "started_at": started_at,
            "ended_at": ended_at,
            "dynamic_pipeline_log": None if pipeline_log is None else str(pipeline_log),
            "previous_run_archive": (
                None if previous_run_archive is None else str(previous_run_archive)
            ),
            "stage_attempted": dict(state["attempted"]),
            "report": str(report_path),
        }
        pipeline_manifest = root / _REAL_PIPELINE_MANIFEST
        _write_json(pipeline_manifest, pipeline_payload)
        if report["overall"] != "PASS":
            raise RealConnectionError(
                f"real connection pipeline stopped safely: {pipeline_failure}",
                report_path=report_path,
                cause=pipeline_failure,
            ) from pipeline_failure
        return RealConnectionResult(
            report=report,
            report_json=report_path,
            pipeline_manifest=pipeline_manifest,
        )


def run_real_connection(
    *,
    step_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str],
    cases_path: str | os.PathLike[str],
    markers_path: str | os.PathLike[str],
    case_id: str,
    mesh_level: str,
    nproc: int,
    max_iterations: int,
    output_directory: str | os.PathLike[str],
    allowed_output_root: str | os.PathLike[str],
    trusted_repository_root: str | os.PathLike[str],
    toolchain: Toolchain,
    paraview_script_directory: str | os.PathLike[str],
    topology_smoke_path: str | os.PathLike[str] | None = None,
    rear_outlet_pilot_path: str | os.PathLike[str] | None = None,
    smoke_characteristic_length_m: float = 0.25,
    facet_overlap_angle_tolerance_degrees: float = 0.1,
    gmsh_algorithm_3d: int = 1,
    timeout_seconds: float = 300.0,
    live_output: bool = True,
    controller_argv: Sequence[str] = (),
) -> RealConnectionResult:
    """CLI-facing entry point for the real-input connection quality gates."""

    return RealConnectionRunner(
        step_path=step_path,
        project_path=project_path,
        cases_path=cases_path,
        markers_path=markers_path,
        case_id=case_id,
        mesh_level=mesh_level,
        nproc=nproc,
        max_iterations=max_iterations,
        output_directory=output_directory,
        allowed_output_root=allowed_output_root,
        trusted_repository_root=trusted_repository_root,
        toolchain=toolchain,
        paraview_script_directory=paraview_script_directory,
        topology_smoke_path=topology_smoke_path,
        rear_outlet_pilot_path=rear_outlet_pilot_path,
        smoke_characteristic_length_m=smoke_characteristic_length_m,
        facet_overlap_angle_tolerance_degrees=(
            facet_overlap_angle_tolerance_degrees
        ),
        gmsh_algorithm_3d=gmsh_algorithm_3d,
        timeout_seconds=timeout_seconds,
        live_output=live_output,
        controller_argv=controller_argv,
    ).run()
