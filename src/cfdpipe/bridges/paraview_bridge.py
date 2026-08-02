"""Headless ParaView bridge: ordinary Python only launches ``pvbatch``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import traceback
from typing import Any

from ..process import CommandExecutionError, CommandResult


PathValue = str | os.PathLike[str]
SUPPORTED_VISUALIZATION_SUFFIXES = frozenset({".vtk", ".vtu", ".pvtu", ".vtm"})
OFFSCREEN_OPTIONS = (
    "--force-offscreen-rendering",
    "--use-offscreen-rendering",
)


class ParaViewBridgeError(RuntimeError):
    """Raised for pvbatch discovery, execution, or output validation failures."""

    def __init__(
        self,
        message: str,
        *,
        result: CommandResult | None = None,
    ) -> None:
        self.result = result
        self.stderr = None if result is None else result.stderr
        if result is not None and result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        super().__init__(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _load_json_object(path: PathValue, *, label: str) -> dict[str, Any]:
    json_path = Path(path).expanduser().resolve(strict=True)
    if not json_path.is_file() or json_path.stat().st_size <= 0:
        raise ParaViewBridgeError(f"{label} is missing or empty: {json_path}")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ParaViewBridgeError(
            f"{label} is not valid UTF-8 JSON: {json_path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ParaViewBridgeError(f"{label} root must be a JSON object: {json_path}")
    return payload


def visualization_from_manifest(
    manifest_path: PathValue,
) -> tuple[Path, dict[str, Any]]:
    """Read exactly ``visualization_file`` from one explicitly supplied manifest."""

    source_manifest = Path(manifest_path).expanduser().resolve(strict=True)
    payload = _load_json_object(source_manifest, label="SU2 run manifest")
    if payload.get("status") != "PASS":
        raise ParaViewBridgeError(
            f"SU2 run manifest status is not PASS: {source_manifest}"
        )
    raw_value = payload.get("visualization_file")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ParaViewBridgeError(
            f"SU2 run manifest lacks visualization_file: {source_manifest}"
        )
    input_path = Path(raw_value).expanduser()
    if not input_path.is_absolute():
        input_path = source_manifest.parent / input_path
    try:
        input_path = input_path.resolve(strict=True)
    except OSError as error:
        raise ParaViewBridgeError(
            f"visualization_file does not exist: {input_path}"
        ) from error
    if not input_path.is_file() or input_path.stat().st_size <= 0:
        raise ParaViewBridgeError(
            f"visualization_file is missing or empty: {input_path}"
        )
    if input_path.suffix.lower() not in SUPPORTED_VISUALIZATION_SUFFIXES:
        raise ParaViewBridgeError(
            "visualization_file must be .vtk, .vtu, .pvtu, or .vtm: "
            f"{input_path}"
        )
    return input_path, payload


def _validate_count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ParaViewBridgeError(f"{field} must be a non-negative JSON integer")
    return value


def validate_inventory(payload: Mapping[str, Any]) -> None:
    """Validate the stable contract emitted by ``inspect_solution.py``."""

    if payload.get("status") != "PASS":
        raise ParaViewBridgeError("solution inventory status is not PASS")
    dataset_type = payload.get("dataset_type")
    if not isinstance(dataset_type, str) or not dataset_type.strip():
        raise ParaViewBridgeError("solution inventory dataset_type is missing")
    points = _validate_count(
        payload.get("number_of_points"), field="number_of_points"
    )
    cells = _validate_count(
        payload.get("number_of_cells"), field="number_of_cells"
    )
    _validate_count(payload.get("block_count"), field="block_count")
    if points == 0 and cells == 0:
        raise ParaViewBridgeError(
            "ParaView opened a dataset with both point and cell counts equal to zero"
        )
    for field in (
        "point_data_arrays",
        "cell_data_arrays",
        "field_data_arrays",
    ):
        if not isinstance(payload.get(field), list):
            raise ParaViewBridgeError(f"solution inventory {field} must be a list")


def _validate_postprocess(payload: Mapping[str, Any]) -> None:
    if payload.get("status") != "PASS":
        raise ParaViewBridgeError("smoke postprocess status is not PASS")
    points = _validate_count(payload.get("total_points"), field="total_points")
    cells = _validate_count(payload.get("total_cells"), field="total_cells")
    if points == 0 and cells == 0:
        raise ParaViewBridgeError(
            "postprocess input has both point and cell counts equal to zero"
        )
    if not isinstance(payload.get("integrable_arrays"), list):
        raise ParaViewBridgeError("integrable_arrays must be a list")


def _validate_integral_csv(path: Path) -> int:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ParaViewBridgeError(f"smoke integral CSV is missing or empty: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        raise ParaViewBridgeError(f"invalid smoke integral CSV: {path}: {error}") from error
    if not rows or not rows[0]:
        raise ParaViewBridgeError(f"smoke integral CSV has no header: {path}")
    return max(0, len(rows) - 1)


def _signature(path: Path) -> tuple[int, int, str] | None:
    if not path.is_file():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, _sha256(path)


def _require_changed_output(
    path: Path,
    before: tuple[int, int, str] | None,
    *,
    label: str,
) -> None:
    after = _signature(path)
    if after is None or after[1] <= 0:
        raise ParaViewBridgeError(f"{label} was not generated: {path}")
    if after == before:
        raise ParaViewBridgeError(f"{label} was not updated by this pvbatch run: {path}")


def _command_record(result: CommandResult) -> dict[str, Any]:
    return {
        **result.as_metadata(),
        "metadata_log": str(result.metadata_log),
    }


def _output_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


class ParaViewBridge:
    """Select repository scripts and delegate all ParaView work to pvbatch."""

    def __init__(
        self,
        runner: Any,
        pvbatch: PathValue,
        script_dir: PathValue | None = None,
        *,
        pvbatch_source: str | None = None,
    ) -> None:
        self.runner = runner
        self.pvbatch = os.fspath(pvbatch)
        if Path(self.pvbatch).stem.lower() != "pvbatch":
            raise ParaViewBridgeError(
                "ParaViewBridge accepts pvbatch only; paraview GUI and pvpython "
                f"are forbidden: {self.pvbatch}"
            )
        self.pvbatch_source = pvbatch_source
        if script_dir is None:
            script_dir = Path(__file__).resolve().parents[3] / "scripts" / "paraview"
        self.script_dir = Path(script_dir).expanduser().resolve()

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> float:
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a real number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        return timeout

    def _script_path(self, script: PathValue) -> Path:
        script_path = Path(script).expanduser()
        if not script_path.is_absolute():
            script_path = self.script_dir / script_path
        script_path = script_path.resolve()
        if script_path != self.script_dir and self.script_dir not in script_path.parents:
            raise ParaViewBridgeError(
                f"pvbatch script must stay under {self.script_dir}: {script_path}"
            )
        if script_path.suffix.lower() != ".py":
            raise ParaViewBridgeError(f"pvbatch script must be Python: {script_path}")
        return script_path

    def build_command(
        self,
        script: PathValue,
        script_arguments: Sequence[PathValue] = (),
        *,
        pvbatch_options: Sequence[str] = (),
    ) -> list[str]:
        if isinstance(script_arguments, (str, bytes, os.PathLike)):
            raise TypeError("script_arguments must be a sequence")
        if isinstance(pvbatch_options, (str, bytes, os.PathLike)):
            raise TypeError("pvbatch_options must be a sequence")
        script_path = self._script_path(script)
        return [
            self.pvbatch,
            *(str(option) for option in pvbatch_options),
            str(script_path),
            *(os.fspath(argument) for argument in script_arguments),
        ]

    def run_script(
        self,
        script: PathValue,
        args: Sequence[PathValue] = (),
        *,
        cwd: PathValue | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        live_output: bool | None = None,
        dry_run: bool = False,
        name: str | None = None,
    ) -> Any:
        """Compatibility wrapper for one explicitly selected pvbatch script."""

        command = self.build_command(script, args)
        return self.runner.run(
            command[0],
            args=command[1:],
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            live_output=live_output,
            dry_run=dry_run,
            name=name,
        )

    def run_pvbatch(
        self,
        script_path: PathValue,
        script_arguments: Sequence[PathValue],
        run_directory: PathValue,
        timeout_seconds: float = 300,
        *,
        pvbatch_options: Sequence[str] = (),
        live_output: bool = True,
        log_stem: str = "pvbatch",
    ) -> CommandResult:
        """Run one repository script safely and validate any declared JSON output."""

        timeout = self._validate_timeout(timeout_seconds)
        run_dir = Path(run_directory).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        script = self._script_path(script_path)
        if not script.is_file():
            raise ParaViewBridgeError(f"pvbatch script does not exist: {script}")
        command = self.build_command(
            script,
            script_arguments,
            pvbatch_options=pvbatch_options,
        )
        result = self.runner.run(
            command[0],
            args=command[1:],
            cwd=run_dir,
            timeout=timeout,
            check=False,
            live_output=live_output,
            dry_run=False,
            name=log_stem,
            stdout_log=run_dir / f"{log_stem}.stdout.log",
            stderr_log=run_dir / f"{log_stem}.stderr.log",
            metadata_log=run_dir / f"{log_stem}.command.json",
        )
        if result.returncode != 0:
            raise ParaViewBridgeError(
                f"pvbatch returned {result.returncode}", result=result
            )

        arguments = [os.fspath(argument) for argument in script_arguments]
        if "--output" in arguments:
            output_index = arguments.index("--output") + 1
            if output_index >= len(arguments):
                raise ParaViewBridgeError("--output has no JSON path", result=result)
            output_path = Path(arguments[output_index]).expanduser()
            if not output_path.is_absolute():
                output_path = run_dir / output_path
            payload = _load_json_object(output_path, label="pvbatch JSON output")
            count_pairs = (
                ("number_of_points", "number_of_cells"),
                ("total_points", "total_cells"),
            )
            for point_field, cell_field in count_pairs:
                if point_field in payload and cell_field in payload:
                    points = _validate_count(payload[point_field], field=point_field)
                    cells = _validate_count(payload[cell_field], field=cell_field)
                    if points == 0 and cells == 0:
                        raise ParaViewBridgeError(
                            "pvbatch JSON reports zero points and zero cells",
                            result=result,
                        )
        return result

    def _run_help(
        self,
        run_directory: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[CommandResult, str | None, str | None]:
        """Probe optional CLI capabilities without treating help as availability.

        Some Windows ParaView builds do not terminate for ``--help``.  The
        authoritative availability gate is the following minimal pvbatch
        script, so a bounded help failure only disables optional flags and is
        retained as a manifest warning with its full command record.
        """

        help_timeout = min(timeout_seconds, 10.0)
        try:
            result = self.runner.run(
                self.pvbatch,
                args=["--help"],
                cwd=run_directory,
                timeout=help_timeout,
                check=False,
                live_output=False,
                dry_run=False,
                name="pvbatch.help",
                stdout_log=run_directory / "pvbatch.help.stdout.log",
                stderr_log=run_directory / "pvbatch.help.stderr.log",
                metadata_log=run_directory / "pvbatch.help.command.json",
            )
        except CommandExecutionError as error:
            result = error.result
        warning: str | None = None
        if result.returncode != 0 or result.timed_out:
            warning = (
                "pvbatch help capability probe was unavailable; no offscreen "
                f"option was used (returncode={result.returncode}, "
                f"timed_out={result.timed_out})"
            )
            return result, None, warning
        help_text = result.stdout + "\n" + result.stderr
        selected = next(
            (option for option in OFFSCREEN_OPTIONS if option in help_text),
            None,
        )
        return result, selected, warning

    def inspect_manifest(
        self,
        su2_manifest_path: PathValue,
        output_directory: PathValue,
        *,
        timeout_seconds: float = 300,
        live_output: bool = True,
    ) -> dict[str, Any]:
        """Probe pvbatch, inspect the manifest-selected result, and integrate it."""

        timeout = self._validate_timeout(timeout_seconds)
        run_dir = Path(output_directory).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / "paraview_manifest.json"
        source_manifest = Path(su2_manifest_path).expanduser().resolve(strict=False)
        started_at = _utc_now()
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "started_at": started_at,
            "ended_at": None,
            "pvbatch_path": str(Path(self.pvbatch).expanduser().resolve(strict=False)),
            "pvbatch_source": self.pvbatch_source,
            "pvbatch_sha256": None,
            "paraview_version": None,
            "offscreen_option": None,
            "su2_manifest_path": str(source_manifest),
            "su2_manifest_sha256": None,
            "input_solution_path": None,
            "input_solution_sha256": None,
            "scripts": {},
            "commands": [],
            "return_code": None,
            "dataset_type": None,
            "number_of_points": None,
            "number_of_cells": None,
            "detected_arrays": {},
            "output_files": [],
            "warnings": [],
            "error": None,
        }
        command_results: list[CommandResult] = []
        try:
            pvbatch_path = Path(self.pvbatch).expanduser().resolve(strict=True)
            if not pvbatch_path.is_file() or pvbatch_path.stem.lower() != "pvbatch":
                raise ParaViewBridgeError(f"invalid pvbatch executable: {pvbatch_path}")
            manifest["pvbatch_path"] = str(pvbatch_path)
            manifest["pvbatch_sha256"] = _sha256(pvbatch_path)

            input_path, source_payload = visualization_from_manifest(source_manifest)
            manifest["su2_manifest_path"] = str(source_manifest.resolve(strict=True))
            manifest["su2_manifest_sha256"] = _sha256(source_manifest.resolve(strict=True))
            manifest["input_solution_path"] = str(input_path)
            input_sha256 = _sha256(input_path)
            manifest["input_solution_sha256"] = input_sha256
            source_validation = source_payload.get("visualization_validation")
            if isinstance(source_validation, Mapping):
                declared_sha256 = source_validation.get("sha256")
                if (
                    isinstance(declared_sha256, str)
                    and declared_sha256.lower() != input_sha256.lower()
                ):
                    raise ParaViewBridgeError(
                        "visualization_file SHA256 does not match the SU2 manifest"
                    )

            scripts: dict[str, Path] = {}
            for name, filename in (
                ("probe", "probe_pvbatch.py"),
                ("inspect", "inspect_solution.py"),
                ("postprocess", "smoke_postprocess.py"),
            ):
                script = self._script_path(filename)
                if not script.is_file():
                    raise ParaViewBridgeError(f"required pvbatch script is missing: {script}")
                scripts[name] = script
                manifest["scripts"][name] = {
                    "path": str(script),
                    "sha256": _sha256(script),
                }

            help_result, offscreen_option, help_warning = self._run_help(
                run_dir,
                timeout_seconds=timeout,
            )
            command_results.append(help_result)
            manifest["commands"].append(_command_record(help_result))
            if help_warning is not None:
                manifest["warnings"].append(help_warning)
            manifest["offscreen_option"] = offscreen_option
            pvbatch_options: tuple[str, ...] = (
                () if offscreen_option is None else (offscreen_option,)
            )

            probe_output = run_dir / "pvbatch_probe.json"
            probe_before = _signature(probe_output)
            probe_result = self.run_pvbatch(
                scripts["probe"],
                ("--output", str(probe_output)),
                run_dir,
                timeout,
                pvbatch_options=pvbatch_options,
                live_output=False,
                log_stem="pvbatch.probe",
            )
            command_results.append(probe_result)
            manifest["commands"].append(_command_record(probe_result))
            _require_changed_output(
                probe_output, probe_before, label="pvbatch probe JSON"
            )
            probe_payload = _load_json_object(
                probe_output, label="pvbatch probe JSON"
            )
            if probe_payload.get("status") != "PASS":
                raise ParaViewBridgeError("pvbatch probe status is not PASS")
            version = probe_payload.get("paraview_version")
            if not isinstance(version, str) or not version.strip():
                raise ParaViewBridgeError("pvbatch probe has no ParaView version")
            manifest["paraview_version"] = version

            inventory_path = run_dir / "solution_inventory.json"
            inventory_before = _signature(inventory_path)
            inspect_result = self.run_pvbatch(
                scripts["inspect"],
                (
                    "--input",
                    str(input_path),
                    "--output",
                    str(inventory_path),
                ),
                run_dir,
                timeout,
                pvbatch_options=pvbatch_options,
                live_output=live_output,
                log_stem="pvbatch.inspect",
            )
            command_results.append(inspect_result)
            manifest["commands"].append(_command_record(inspect_result))
            _require_changed_output(
                inventory_path, inventory_before, label="solution inventory JSON"
            )
            inventory = _load_json_object(
                inventory_path, label="solution inventory JSON"
            )
            validate_inventory(inventory)

            postprocess_path = run_dir / "smoke_postprocess.json"
            integral_path = run_dir / "smoke_integral.csv"
            postprocess_before = _signature(postprocess_path)
            integral_before = _signature(integral_path)
            post_result = self.run_pvbatch(
                scripts["postprocess"],
                (
                    "--input",
                    str(input_path),
                    "--output-directory",
                    str(run_dir),
                ),
                run_dir,
                timeout,
                pvbatch_options=pvbatch_options,
                live_output=live_output,
                log_stem="pvbatch",
            )
            command_results.append(post_result)
            manifest["commands"].append(_command_record(post_result))
            _require_changed_output(
                postprocess_path,
                postprocess_before,
                label="smoke postprocess JSON",
            )
            _require_changed_output(
                integral_path,
                integral_before,
                label="smoke integral CSV",
            )
            postprocess = _load_json_object(
                postprocess_path, label="smoke postprocess JSON"
            )
            _validate_postprocess(postprocess)
            csv_row_count = _validate_integral_csv(integral_path)

            versions = {
                str(payload.get("paraview_version"))
                for payload in (probe_payload, inventory, postprocess)
            }
            if versions != {version}:
                raise ParaViewBridgeError(
                    f"inconsistent ParaView versions across scripts: {sorted(versions)}"
                )
            if (
                inventory["number_of_points"] != postprocess["total_points"]
                or inventory["number_of_cells"] != postprocess["total_cells"]
            ):
                raise ParaViewBridgeError(
                    "inspect and postprocess point/cell counts do not match"
                )
            if _sha256(input_path) != input_sha256:
                raise ParaViewBridgeError("pvbatch modified the input solution file")

            output_paths = (
                probe_output,
                inventory_path,
                postprocess_path,
                integral_path,
                run_dir / "pvbatch.stdout.log",
                run_dir / "pvbatch.stderr.log",
                run_dir / "pvbatch.command.json",
                run_dir / "pvbatch.inspect.stdout.log",
                run_dir / "pvbatch.inspect.stderr.log",
                run_dir / "pvbatch.inspect.command.json",
            )
            manifest["output_files"] = [
                _output_record(path) for path in output_paths if path.is_file()
            ]
            manifest["dataset_type"] = inventory["dataset_type"]
            manifest["number_of_points"] = inventory["number_of_points"]
            manifest["number_of_cells"] = inventory["number_of_cells"]
            manifest["detected_arrays"] = {
                "point": inventory["point_data_arrays"],
                "cell": inventory["cell_data_arrays"],
                "field": inventory["field_data_arrays"],
            }
            manifest["integral_csv_row_count"] = csv_row_count
            manifest["return_code"] = post_result.returncode
            manifest["status"] = "PASS"
            manifest["ended_at"] = _utc_now()
            _write_json(manifest_path, manifest)
            return manifest
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            error_result = getattr(error, "result", None)
            if isinstance(error_result, CommandResult):
                if error_result not in command_results:
                    command_results.append(error_result)
                    manifest["commands"].append(_command_record(error_result))
                manifest["return_code"] = error_result.returncode
            manifest["status"] = "FAIL"
            manifest["ended_at"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                "stderr": (
                    None
                    if not isinstance(error_result, CommandResult)
                    else error_result.stderr
                ),
            }
            _write_json(manifest_path, manifest)
            if isinstance(error, ParaViewBridgeError):
                raise
            if isinstance(error, CommandExecutionError):
                raise ParaViewBridgeError(
                    str(error), result=error.result
                ) from error
            raise ParaViewBridgeError(str(error)) from error

    def inspect(
        self,
        args: Sequence[PathValue] = (),
        **run_options: Any,
    ) -> Any:
        run_options.setdefault("name", "paraview-inspect")
        return self.run_script("inspect_solution.py", args, **run_options)

    def smoke(
        self,
        args: Sequence[PathValue] = (),
        **run_options: Any,
    ) -> Any:
        run_options.setdefault("name", "paraview-smoke")
        return self.run_script("smoke_postprocess.py", args, **run_options)


def run_pvbatch(
    script_path: PathValue,
    script_arguments: Sequence[PathValue],
    run_directory: PathValue,
    timeout_seconds: float = 300,
    *,
    runner: Any,
    pvbatch: PathValue,
    pvbatch_options: Sequence[str] = (),
) -> CommandResult:
    """Module-level interface for one safe pvbatch script invocation."""

    return ParaViewBridge(runner, pvbatch).run_pvbatch(
        script_path,
        script_arguments,
        run_directory,
        timeout_seconds,
        pvbatch_options=pvbatch_options,
    )


__all__ = [
    "ParaViewBridge",
    "ParaViewBridgeError",
    "run_pvbatch",
    "validate_inventory",
    "visualization_from_manifest",
]
