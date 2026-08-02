"""Safe ordinary-Python launcher for headless geometry-review rendering.

This module deliberately has no ParaView imports.  ParaView APIs are confined
to ``scripts/paraview/render_geometry_review.py``, which is interpreted only by
an explicitly supplied ``pvbatch`` executable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from ..process import CommandExecutionError, CommandResult


PathValue = str | os.PathLike[str]
RENDER_VIEWS = frozenset({"isometric", "XY", "XZ", "YZ"})


class GeometryReviewParaViewError(RuntimeError):
    """Raised when pvbatch execution or geometry-render evidence is invalid."""

    def __init__(
        self,
        message: str,
        *,
        result: CommandResult | None = None,
    ) -> None:
        self.result = result
        self.stderr = None if result is None else result.stderr
        self.returncode = None if result is None else result.returncode
        if result is not None and result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        super().__init__(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(path: Path) -> tuple[int, int, str] | None:
    if not path.is_file():
        return None
    details = path.stat()
    return details.st_mtime_ns, details.st_size, _sha256(path)


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GeometryReviewParaViewError(
            f"{label} is not valid UTF-8 JSON: {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise GeometryReviewParaViewError(f"{label} root must be a JSON object: {path}")
    return payload


def _normalize_groups(payload: Mapping[str, Any]) -> dict[str, tuple[int, ...]]:
    raw_groups = payload.get("groups")
    if not isinstance(raw_groups, (Mapping, list)):
        raise GeometryReviewParaViewError(
            "geometry groups JSON needs a non-empty 'groups' object or list"
        )

    entries: list[tuple[object, object]] = []
    if isinstance(raw_groups, Mapping):
        entries.extend(raw_groups.items())
    else:
        for index, entry in enumerate(raw_groups):
            if not isinstance(entry, Mapping):
                raise GeometryReviewParaViewError(
                    f"geometry groups entry {index} must be an object"
                )
            entries.append((entry.get("name"), entry))

    normalized: dict[str, tuple[int, ...]] = {}
    for raw_name, raw_specification in entries:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise GeometryReviewParaViewError("every geometry group needs a name")
        name = raw_name.strip()
        if name.lower() == "all":
            raise GeometryReviewParaViewError("'all' is a reserved geometry group name")
        if name in normalized:
            raise GeometryReviewParaViewError(f"duplicate geometry group {name!r}")

        if isinstance(raw_specification, Mapping):
            raw_tags = raw_specification.get("surface_tags")
            if raw_tags is None:
                raw_tags = raw_specification.get("entity_tags")
        else:
            raw_tags = raw_specification
        if (
            not isinstance(raw_tags, Sequence)
            or isinstance(raw_tags, (str, bytes, os.PathLike))
            or not raw_tags
        ):
            raise GeometryReviewParaViewError(
                f"geometry group {name!r} needs non-empty surface_tags"
            )
        tags: list[int] = []
        for raw_tag in raw_tags:
            if isinstance(raw_tag, bool) or not isinstance(raw_tag, int) or raw_tag <= 0:
                raise GeometryReviewParaViewError(
                    f"geometry group {name!r} has invalid surface tag {raw_tag!r}"
                )
            if raw_tag not in tags:
                tags.append(raw_tag)
        normalized[name] = tuple(tags)
    if not normalized:
        raise GeometryReviewParaViewError("geometry groups JSON contains no groups")
    return normalized


def _resolved_existing_file(
    value: PathValue,
    *,
    label: str,
    allowed_root: Path,
) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise GeometryReviewParaViewError(f"{label} path must not contain '..'")
    try:
        path = raw.resolve(strict=True)
    except OSError as error:
        raise GeometryReviewParaViewError(f"{label} does not exist: {raw}") from error
    if path != allowed_root and allowed_root not in path.parents:
        raise GeometryReviewParaViewError(
            f"{label} must stay under allowed root {allowed_root}: {path}"
        )
    if not path.is_file() or path.stat().st_size <= 0:
        raise GeometryReviewParaViewError(f"{label} is missing or empty: {path}")
    return path


def _resolved_output_directory(value: PathValue, *, allowed_root: Path) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise GeometryReviewParaViewError("output directory must not contain '..'")
    path = raw.resolve(strict=False)
    if path != allowed_root and allowed_root not in path.parents:
        raise GeometryReviewParaViewError(
            f"output directory must stay under allowed root {allowed_root}: {path}"
        )
    return path


def _count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GeometryReviewParaViewError(f"{field} must be a positive JSON integer")
    return value


class GeometryReviewParaViewBridge:
    """Launch one repository geometry-review script through explicit pvbatch."""

    def __init__(
        self,
        runner: Any,
        pvbatch: PathValue,
        script_path: PathValue | None = None,
    ) -> None:
        self.runner = runner
        self.pvbatch = Path(pvbatch).expanduser()
        if self.pvbatch.stem.lower() != "pvbatch":
            raise GeometryReviewParaViewError(
                "geometry review accepts pvbatch only; ParaView GUI and pvpython "
                f"are forbidden: {self.pvbatch}"
            )
        if script_path is None:
            script_path = (
                Path(__file__).resolve().parents[3]
                / "scripts"
                / "paraview"
                / "render_geometry_review.py"
            )
        self.script_path = Path(script_path).expanduser()

    @staticmethod
    def _timeout(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("timeout_seconds must be a real number")
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        return timeout

    def build_command(
        self,
        input_path: PathValue,
        groups_path: PathValue,
        output_directory: PathValue,
    ) -> list[str]:
        executable = self.pvbatch.resolve(strict=True)
        if not executable.is_file() or executable.stem.lower() != "pvbatch":
            raise GeometryReviewParaViewError(f"invalid pvbatch executable: {executable}")
        script = self.script_path.resolve(strict=True)
        if not script.is_file() or script.suffix.lower() != ".py":
            raise GeometryReviewParaViewError(
                f"geometry review script is missing or invalid: {script}"
            )
        return [
            str(executable),
            str(script),
            "--input",
            str(Path(input_path)),
            "--groups",
            str(Path(groups_path)),
            "--output-directory",
            str(Path(output_directory)),
        ]

    def _validate_manifest(
        self,
        payload: Mapping[str, Any],
        *,
        output_directory: Path,
        input_path: Path,
        groups_path: Path,
        expected_groups: Mapping[str, tuple[int, ...]],
        result: CommandResult,
    ) -> dict[str, Any]:
        if payload.get("status") != "PASS":
            raise GeometryReviewParaViewError(
                "geometry render manifest status is not PASS", result=result
            )
        for field in ("paraview_version", "dataset_type", "tag_array"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise GeometryReviewParaViewError(
                    f"geometry render manifest {field} is missing", result=result
                )
        _count(payload.get("number_of_points"), field="number_of_points")
        _count(payload.get("number_of_cells"), field="number_of_cells")
        declared_input = payload.get("input_file")
        if not isinstance(declared_input, str) or Path(declared_input).resolve() != input_path:
            raise GeometryReviewParaViewError(
                "geometry render manifest input_file does not match the requested VTK",
                result=result,
            )
        declared_input_hash = payload.get("input_sha256")
        if (
            not isinstance(declared_input_hash, str)
            or declared_input_hash.lower() != _sha256(input_path).lower()
        ):
            raise GeometryReviewParaViewError(
                "geometry render manifest input_sha256 is missing or mismatched",
                result=result,
            )
        declared_groups_file = payload.get("groups_file")
        if (
            not isinstance(declared_groups_file, str)
            or Path(declared_groups_file).expanduser().resolve() != groups_path
            or not groups_path.is_file()
        ):
            raise GeometryReviewParaViewError(
                "geometry render manifest groups_file is missing or mismatched",
                result=result,
            )
        declared_groups_hash = payload.get("groups_sha256")
        if (
            not isinstance(declared_groups_hash, str)
            or declared_groups_hash.lower() != _sha256(groups_path).lower()
        ):
            raise GeometryReviewParaViewError(
                "geometry render manifest groups_sha256 is missing or mismatched",
                result=result,
            )

        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, list):
            raise GeometryReviewParaViewError(
                "geometry render manifest groups must be a list", result=result
            )
        group_records: dict[str, Mapping[str, Any]] = {}
        for record in raw_groups:
            if not isinstance(record, Mapping):
                raise GeometryReviewParaViewError(
                    "geometry render group record must be an object", result=result
                )
            name = record.get("name")
            if not isinstance(name, str) or not name or name in group_records:
                raise GeometryReviewParaViewError(
                    "geometry render group names are missing or duplicated", result=result
                )
            group_records[name] = record
        expected_names = {"all", *expected_groups}
        if set(group_records) != expected_names:
            raise GeometryReviewParaViewError(
                "geometry render manifest groups do not match requested groups",
                result=result,
            )
        if group_records["all"].get("surface_tags") != sorted(
            {tag for tags in expected_groups.values() for tag in tags}
        ):
            raise GeometryReviewParaViewError(
                "geometry render manifest all group tags do not match the request",
                result=result,
            )
        for name, tags in expected_groups.items():
            declared_tags = group_records[name].get("surface_tags")
            if declared_tags != list(tags):
                raise GeometryReviewParaViewError(
                    f"geometry render group {name!r} tags do not match request",
                    result=result,
                )

        raw_images = payload.get("images")
        if not isinstance(raw_images, list):
            raise GeometryReviewParaViewError(
                "geometry render manifest images must be a list", result=result
            )
        seen: set[tuple[str, str]] = set()
        declared_image_paths: set[Path] = set()
        for record in raw_images:
            if not isinstance(record, Mapping):
                raise GeometryReviewParaViewError(
                    "geometry render image record must be an object", result=result
                )
            group = record.get("group")
            view = record.get("view")
            raw_path = record.get("path")
            if group not in expected_names or view not in RENDER_VIEWS:
                raise GeometryReviewParaViewError(
                    "geometry render image has an unknown group or view", result=result
                )
            key = (str(group), str(view))
            if key in seen:
                raise GeometryReviewParaViewError(
                    f"duplicate geometry render image {key!r}", result=result
                )
            seen.add(key)
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise GeometryReviewParaViewError(
                    "geometry render image path is missing", result=result
                )
            image_path = Path(raw_path).expanduser()
            if not image_path.is_absolute():
                image_path = output_directory / image_path
            try:
                image_path = image_path.resolve(strict=True)
            except OSError as error:
                raise GeometryReviewParaViewError(
                    f"geometry render image is missing: {image_path}", result=result
                ) from error
            if output_directory not in image_path.parents:
                raise GeometryReviewParaViewError(
                    f"geometry render image escaped output directory: {image_path}",
                    result=result,
                )
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                raise GeometryReviewParaViewError(
                    f"geometry render image is missing or empty: {image_path}",
                    result=result,
                )
            declared_image_paths.add(image_path)
            declared_hash = record.get("sha256")
            if (
                not isinstance(declared_hash, str)
                or len(declared_hash) != 64
                or declared_hash.lower() != _sha256(image_path).lower()
            ):
                raise GeometryReviewParaViewError(
                    f"geometry render image SHA256 is missing or mismatched: {image_path}",
                    result=result,
                )
        expected_images = {
            (group, view) for group in expected_names for view in RENDER_VIEWS
        }
        if seen != expected_images:
            raise GeometryReviewParaViewError(
                "geometry render manifest does not contain four images for every group",
                result=result,
            )
        actual_image_paths: set[Path] = set()
        for candidate in output_directory.iterdir():
            if candidate.suffix.casefold() != ".png":
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as error:
                raise GeometryReviewParaViewError(
                    f"geometry render output contains an invalid PNG path: {candidate}",
                    result=result,
                ) from error
            if output_directory not in resolved.parents or not resolved.is_file():
                raise GeometryReviewParaViewError(
                    f"geometry render PNG escaped output directory: {candidate}",
                    result=result,
                )
            actual_image_paths.add(resolved)
        if actual_image_paths != declared_image_paths:
            extras = sorted(str(path) for path in actual_image_paths - declared_image_paths)
            missing = sorted(str(path) for path in declared_image_paths - actual_image_paths)
            raise GeometryReviewParaViewError(
                "geometry render output PNG inventory does not exactly match this "
                f"run's manifest; extras={extras}, missing={missing}",
                result=result,
            )
        return dict(payload)

    def run(
        self,
        input_path: PathValue,
        groups_path: PathValue,
        output_directory: PathValue,
        *,
        allowed_output_root: PathValue,
        timeout_seconds: float = 300.0,
        live_output: bool = True,
    ) -> dict[str, Any]:
        timeout = self._timeout(timeout_seconds)
        allowed_root = Path(allowed_output_root).expanduser().resolve(strict=True)
        if not allowed_root.is_dir():
            raise GeometryReviewParaViewError(
                f"allowed output root is not a directory: {allowed_root}"
            )
        source = _resolved_existing_file(
            input_path, label="geometry VTK input", allowed_root=allowed_root
        )
        if source.suffix.lower() != ".vtk":
            raise GeometryReviewParaViewError(
                f"geometry review input must be a .vtk file: {source}"
            )
        groups_file = _resolved_existing_file(
            groups_path, label="geometry groups JSON", allowed_root=allowed_root
        )
        groups = _normalize_groups(
            _load_json_object(groups_file, label="geometry groups JSON")
        )
        output = _resolved_output_directory(output_directory, allowed_root=allowed_root)
        output.mkdir(parents=True, exist_ok=True)
        manifest_path = output / "geometry_render_manifest.json"
        manifest_before = _signature(manifest_path)
        command = self.build_command(source, groups_file, output)
        try:
            result = self.runner.run(
                command[0],
                args=command[1:],
                cwd=output,
                timeout=timeout,
                check=False,
                live_output=live_output,
                dry_run=False,
                name="geometry-review-pvbatch",
                stdout_log=output / "pvbatch.stdout.log",
                stderr_log=output / "pvbatch.stderr.log",
                metadata_log=output / "pvbatch.command.json",
            )
        except CommandExecutionError as error:
            raise GeometryReviewParaViewError(
                "pvbatch geometry review failed", result=error.result
            ) from error
        if result.returncode != 0 or result.timed_out:
            raise GeometryReviewParaViewError(
                f"pvbatch geometry review returned {result.returncode}", result=result
            )
        manifest_after = _signature(manifest_path)
        if manifest_after is None or manifest_after[1] <= 0:
            raise GeometryReviewParaViewError(
                f"pvbatch did not create geometry_render_manifest.json: {manifest_path}",
                result=result,
            )
        if manifest_after == manifest_before:
            raise GeometryReviewParaViewError(
                "pvbatch did not update geometry_render_manifest.json in this run",
                result=result,
            )
        try:
            payload = _load_json_object(manifest_path, label="geometry render manifest")
        except GeometryReviewParaViewError as error:
            raise GeometryReviewParaViewError(str(error), result=result) from error
        return self._validate_manifest(
            payload,
            output_directory=output,
            input_path=source,
            groups_path=groups_file,
            expected_groups=groups,
            result=result,
        )


def run_geometry_review(
    runner: Any,
    pvbatch: PathValue,
    input_path: PathValue,
    groups_path: PathValue,
    output_directory: PathValue,
    *,
    allowed_output_root: PathValue,
    script_path: PathValue | None = None,
    timeout_seconds: float = 300.0,
    live_output: bool = True,
) -> dict[str, Any]:
    """Convenience wrapper around :class:`GeometryReviewParaViewBridge`."""

    return GeometryReviewParaViewBridge(
        runner,
        pvbatch,
        script_path=script_path,
    ).run(
        input_path,
        groups_path,
        output_directory,
        allowed_output_root=allowed_output_root,
        timeout_seconds=timeout_seconds,
        live_output=live_output,
    )


__all__ = [
    "GeometryReviewParaViewBridge",
    "GeometryReviewParaViewError",
    "run_geometry_review",
]
