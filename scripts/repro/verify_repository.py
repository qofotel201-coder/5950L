"""Perform an offline, standard-library-only repository preflight.

This verifier intentionally does not import Gmsh, resolve executables, launch
subprocesses, access the network, or execute project code.  It validates the
portable repository layer and, when available, SHA-256 binds private CAD inputs
to ``config/markers.toml``.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import stat
import sys
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "cfdpipe.repository_preflight.v1"
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

REQUIRED_FILES = (
    "AGENTS.md",
    "PLAN.md",
    "README.md",
    "pyproject.toml",
    "requirements-gmsh.txt",
    ".gitignore",
    ".gitattributes",
    ".github/workflows/ci.yml",
    "config/project.toml",
    "config/cases.csv",
    "config/markers.toml",
    "config/tools.example.json",
    "config/reproducibility.json",
    "src/cfdpipe/__init__.py",
    "src/cfdpipe/__main__.py",
    "src/cfdpipe/cli.py",
    "src/cfdpipe/process.py",
    "scripts/repro/run_cad_free_tests.py",
    "scripts/repro/verify_repository.py",
)

JSON_CONFIGS = (
    "config/tools.example.json",
    "config/reproducibility.json",
)

PRIVATE_INPUT_SECTIONS = (
    ("source", "private_source_geometry"),
    ("pipeline_geometry", "private_pipeline_geometry"),
)

REQUIRED_CASE_COLUMNS = (
    "case_id",
    "mach",
    "altitude_km",
    "alpha_deg",
    "beta_deg",
    "role",
)

EXPECTED_TOOLCHAIN = {
    "python": {"version": "3.13.14"},
    "gmsh_python_api": {"version": "4.15.2"},
    "gmsh_cli": {
        "version": "4.15.2",
        "environment_override": "CFD_GMSH",
    },
    "su2_cfd": {
        "version": "8.5.0",
        "environment_override": "CFD_SU2_CFD",
    },
    "su2_sol": {
        "version": "8.5.0",
        "environment_override": "CFD_SU2_SOL",
        "required": False,
    },
    "mpi_launcher": {
        "environment_override": "CFD_MPIEXEC",
        "required_for_serial_reproduction": False,
    },
    "paraview_pvbatch": {
        "version": "6.2.0",
        "environment_override": "CFD_PVBATCH",
    },
}


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    path: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "message": message,
        "status": status,
    }
    if path is not None:
        result["path"] = path
    if details:
        result["details"] = dict(details)
    return result


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_absolute_cross_platform(path_text: str) -> bool:
    return (
        Path(path_text).is_absolute()
        or PureWindowsPath(path_text).is_absolute()
        or PurePosixPath(path_text).is_absolute()
    )


def _inside_root(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_or_reparse_point(path: Path) -> bool:
    """Return whether ``path`` is a symlink, junction, or other reparse point."""

    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            if is_junction(path):
                return True
        except OSError:
            return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(attributes & reparse_flag)


def _is_read_only(path: Path) -> bool:
    write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(stat.S_IMODE(path.stat().st_mode) & write_bits)


def _regular_repository_file(root: Path, relative: str) -> tuple[bool, str]:
    path = root / relative
    if not path.exists() and not path.is_symlink():
        return False, "Required repository file is missing."
    if _is_link_or_reparse_point(path):
        return False, "Required repository file must not be a symlink or reparse point."
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return False, f"Required repository file cannot be resolved: {exc}"
    if not _inside_root(root, resolved):
        return False, "Required repository file resolves outside the repository root."
    if not resolved.is_file():
        return False, "Required repository path is not a regular file."
    try:
        if resolved.stat().st_size <= 0:
            return False, "Required repository file is empty."
    except OSError as exc:
        return False, f"Required repository file cannot be inspected: {exc}"
    return True, "Required repository file is present, non-empty, and root-contained."


def _load_toml(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise ValueError("TOML root is not a table")
    return value


def _load_json(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def _python_policy_violations(
    path: Path,
    relative: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        line = getattr(exc, "lineno", None)
        violation: dict[str, Any] = {
            "kind": "python_parse_error",
            "path": relative,
            "message": str(exc),
        }
        if line is not None:
            violation["line"] = line
        return [violation], []

    allowed_paraview_script = relative.startswith("scripts/paraview/")
    process_module = relative == "src/cfdpipe/process.py"
    gmsh_aliases = {"gmsh"}
    fltk_aliases: set[str] = set()
    fltk_run_aliases: set[str] = set()
    aliases: dict[str, str] = {}
    violations: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
                if alias.name == "gmsh":
                    gmsh_aliases.add(alias.asname or alias.name)
                if alias.name == "subprocess" and not process_module:
                    violations.append(
                        {
                            "kind": "subprocess_import_outside_command_runner",
                            "line": node.lineno,
                            "path": relative,
                        }
                    )
                if (
                    not allowed_paraview_script
                    and (
                        alias.name == "paraview"
                        or alias.name.startswith("paraview.")
                    )
                ):
                    violations.append(
                        {
                            "kind": "ordinary_python_imports_paraview",
                            "line": node.lineno,
                            "path": relative,
                        }
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
            if node.module == "gmsh":
                for alias in node.names:
                    if alias.name == "fltk":
                        fltk_aliases.add(alias.asname or alias.name)
            elif node.module == "gmsh.fltk":
                for alias in node.names:
                    if alias.name == "run":
                        fltk_run_aliases.add(alias.asname or alias.name)
            if module == "subprocess" and not process_module:
                violations.append(
                    {
                        "kind": "subprocess_import_outside_command_runner",
                        "line": node.lineno,
                        "path": relative,
                    }
                )
            if (
                not allowed_paraview_script
                and (module == "paraview" or module.startswith("paraview."))
            ):
                violations.append(
                    {
                        "kind": "ordinary_python_imports_paraview",
                        "line": node.lineno,
                        "path": relative,
                    }
                )

    allowed_popen_calls: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                violations.append(
                    {
                        "kind": "subprocess_shell_keyword_true",
                        "line": node.lineno,
                        "path": relative,
                    }
                )

        called = _dotted_name(node.func)
        gmsh_gui_calls = {f"{alias}.fltk.run" for alias in gmsh_aliases}
        gmsh_gui_calls.update(f"{alias}.run" for alias in fltk_aliases)
        gmsh_gui_calls.update(fltk_run_aliases)
        if called in gmsh_gui_calls:
            violations.append(
                {
                    "kind": "gmsh_gui_run",
                    "line": node.lineno,
                    "path": relative,
                }
            )

        if called is None:
            continue
        head, separator, tail = called.partition(".")
        expanded = aliases.get(head, head) + (
            separator + tail if separator else ""
        )
        forbidden_external_call = (
            expanded
            in {
                "subprocess.Popen",
                "subprocess.run",
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "os.system",
                "os.popen",
                "os.startfile",
                "asyncio.create_subprocess_exec",
                "asyncio.create_subprocess_shell",
                "multiprocessing.Process",
            }
            or expanded.startswith("os.spawn")
            or expanded.startswith("os.exec")
            or "CreateProcess" in expanded
        )
        if not forbidden_external_call:
            continue
        if process_module and expanded == "subprocess.Popen":
            shell_keywords = [
                keyword for keyword in node.keywords if keyword.arg == "shell"
            ]
            allowed_popen_calls.append(
                {
                    "first_argument_is_command": bool(
                        node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "command"
                    ),
                    "line": node.lineno,
                    "path": relative,
                    "shell_explicitly_false": bool(
                        len(shell_keywords) == 1
                        and isinstance(shell_keywords[0].value, ast.Constant)
                        and shell_keywords[0].value.value is False
                    ),
                }
            )
        else:
            violations.append(
                {
                    "call": expanded,
                    "kind": "external_process_creation_outside_command_runner",
                    "line": node.lineno,
                    "path": relative,
                }
            )

    return violations, allowed_popen_calls


def _iter_python_files(root: Path) -> Iterable[tuple[Path, str]]:
    discovered: list[tuple[Path, str]] = []
    for relative_root in ("src", "scripts"):
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            discovered.append((path, relative))
    return iter(sorted(discovered, key=lambda item: item[1]))


def _validate_entrypoint(root: Path) -> dict[str, Any]:
    main_path = root / "src/cfdpipe/__main__.py"
    cli_path = root / "src/cfdpipe/cli.py"
    if not main_path.is_file() or not cli_path.is_file():
        return _check(
            "python_module_entrypoint",
            FAIL,
            "The cfdpipe module entrypoint is incomplete.",
            path="src/cfdpipe/__main__.py",
        )

    try:
        main_tree = ast.parse(
            main_path.read_text(encoding="utf-8"),
            filename="src/cfdpipe/__main__.py",
        )
        cli_tree = ast.parse(
            cli_path.read_text(encoding="utf-8"),
            filename="src/cfdpipe/cli.py",
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        return _check(
            "python_module_entrypoint",
            FAIL,
            f"Unable to parse the cfdpipe entrypoint: {exc}",
            path="src/cfdpipe/__main__.py",
        )

    imports_main = any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "cli"
        and any(alias.name == "main" for alias in node.names)
        for node in ast.walk(main_tree)
    )
    defines_main = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
        for node in cli_tree.body
    )
    if not imports_main or not defines_main:
        return _check(
            "python_module_entrypoint",
            FAIL,
            "python -m cfdpipe is not bound to cfdpipe.cli.main.",
            path="src/cfdpipe/__main__.py",
        )
    return _check(
        "python_module_entrypoint",
        PASS,
        "python -m cfdpipe is bound to cfdpipe.cli.main.",
        path="src/cfdpipe/__main__.py",
    )


def _aggregate(checks: Sequence[Mapping[str, Any]]) -> tuple[str, dict[str, int]]:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for check in checks:
        status = check.get("status")
        if status in counts:
            counts[status] += 1
        else:
            counts[FAIL] += 1
    if counts[FAIL]:
        overall = FAIL
    elif counts[WARN]:
        overall = WARN
    else:
        overall = PASS
    return overall, {
        "fail": counts[FAIL],
        "pass": counts[PASS],
        "total": len(checks),
        "warn": counts[WARN],
    }


def verify_repository(
    root: str | Path,
    *,
    require_private_inputs: bool = False,
) -> dict[str, Any]:
    """Return a deterministic offline preflight report for ``root``."""

    repository_root = Path(root).expanduser().resolve(strict=False)
    checks: list[dict[str, Any]] = []
    if not repository_root.is_dir():
        checks.append(
            _check(
                "repository_root",
                FAIL,
                "Repository root does not exist or is not a directory.",
                path=".",
            )
        )
        status, summary = _aggregate(checks)
        return {
            "checks": checks,
            "require_private_inputs": require_private_inputs,
            "schema": SCHEMA,
            "status": status,
            "summary": summary,
        }

    checks.append(
        _check(
            "repository_root",
            PASS,
            "Repository root is a directory.",
            path=".",
        )
    )

    for relative in REQUIRED_FILES:
        valid, message = _regular_repository_file(repository_root, relative)
        if valid:
            checks.append(
                _check(
                    f"required_file:{relative}",
                    PASS,
                    message,
                    path=relative,
                )
            )
        else:
            checks.append(
                _check(
                    f"required_file:{relative}",
                    FAIL,
                    message,
                    path=relative,
                )
            )

    parsed_toml: dict[str, Mapping[str, Any]] = {}
    for relative in ("config/project.toml", "config/markers.toml"):
        path = repository_root / relative
        if not path.is_file():
            continue
        try:
            parsed_toml[relative] = _load_toml(path)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as exc:
            checks.append(
                _check(
                    f"toml_parse:{relative}",
                    FAIL,
                    f"TOML parsing failed: {exc}",
                    path=relative,
                )
            )
        else:
            checks.append(
                _check(
                    f"toml_parse:{relative}",
                    PASS,
                    "TOML parsed as a top-level table.",
                    path=relative,
                )
            )

    case_ids: set[str] = set()
    cases_path = repository_root / "config/cases.csv"
    if cases_path.is_file():
        try:
            with cases_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                fields = list(reader.fieldnames or [])
            if not fields:
                raise ValueError("CSV header is missing")
            if not rows:
                raise ValueError("CSV has no data rows")
            missing_columns = [
                column for column in REQUIRED_CASE_COLUMNS if column not in fields
            ]
            if missing_columns:
                raise ValueError(
                    "CSV is missing required columns: " + ", ".join(missing_columns)
                )
            ordered_case_ids = [str(row.get("case_id") or "").strip() for row in rows]
            if any(not case_id for case_id in ordered_case_ids):
                raise ValueError("CSV contains an empty case_id")
            if len(set(ordered_case_ids)) != len(ordered_case_ids):
                raise ValueError("CSV case_id values must be unique")
            case_ids = set(ordered_case_ids)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            checks.append(
                _check(
                    "csv_parse:config/cases.csv",
                    FAIL,
                    f"CSV parsing failed: {exc}",
                    path="config/cases.csv",
                )
            )
        else:
            checks.append(
                _check(
                    "csv_parse:config/cases.csv",
                    PASS,
                    "CSV parsed with a header and at least one data row.",
                    path="config/cases.csv",
                    details={
                        "column_count": len(fields),
                        "required_columns": list(REQUIRED_CASE_COLUMNS),
                        "row_count": len(rows),
                        "unique_case_ids": len(case_ids),
                    },
                )
            )

    project_config = parsed_toml.get("config/project.toml")
    markers_config = parsed_toml.get("config/markers.toml")
    if markers_config is not None:
        marker_contract_valid = (
            markers_config.get("schema") == "cfdpipe.markers.v2"
            and markers_config.get("status") == PASS
        )
        checks.append(
            _check(
                "markers_contract",
                PASS if marker_contract_valid else FAIL,
                (
                    "markers.toml declares the v2 PASS contract."
                    if marker_contract_valid
                    else "markers.toml must declare schema cfdpipe.markers.v2 and status PASS."
                ),
                path="config/markers.toml",
            )
        )

    if project_config is not None and markers_config is not None:
        project_table = project_config.get("project")
        production_table = project_config.get("production")
        source_table = markers_config.get("source")
        project_geometry = (
            project_table.get("geometry_file")
            if isinstance(project_table, dict)
            else None
        )
        marker_geometry = (
            source_table.get("path") if isinstance(source_table, dict) else None
        )
        paths_match = (
            isinstance(project_geometry, str)
            and isinstance(marker_geometry, str)
            and project_geometry.replace("\\", "/")
            == marker_geometry.replace("\\", "/")
        )
        checks.append(
            _check(
                "project_marker_geometry_binding",
                PASS if paths_match else FAIL,
                (
                    "project.geometry_file matches markers.source.path."
                    if paths_match
                    else "project.geometry_file must exactly match markers.source.path."
                ),
                path="config/project.toml",
            )
        )

        design_case = (
            production_table.get("design_case")
            if isinstance(production_table, dict)
            else None
        )
        design_case_valid = (
            isinstance(design_case, str)
            and bool(design_case)
            and design_case in case_ids
        )
        checks.append(
            _check(
                "project_design_case",
                PASS if design_case_valid else FAIL,
                (
                    "project.production.design_case exists in cases.csv."
                    if design_case_valid
                    else "project.production.design_case must name a unique cases.csv row."
                ),
                path="config/project.toml",
            )
        )

    parsed_json: dict[str, Mapping[str, Any]] = {}
    json_paths = list(JSON_CONFIGS)
    if (repository_root / "config/tools.json").is_file():
        json_paths.append("config/tools.json")
    for relative in json_paths:
        path = repository_root / relative
        if not path.is_file():
            continue
        try:
            parsed_json[relative] = _load_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            checks.append(
                _check(
                    f"json_parse:{relative}",
                    FAIL,
                    f"JSON parsing failed: {exc}",
                    path=relative,
                )
            )
        else:
            checks.append(
                _check(
                    f"json_parse:{relative}",
                    PASS,
                    "JSON parsed as a top-level object.",
                    path=relative,
                )
            )

    example_tools = parsed_json.get("config/tools.example.json")
    if example_tools is not None:
        tools = example_tools.get("tools")
        expected_tools = {"gmsh", "su2_cfd", "su2_sol", "mpiexec", "pvbatch"}
        values_are_portable = (
            isinstance(tools, dict)
            and expected_tools.issubset(tools)
            and all(tools[name] is None for name in expected_tools)
            and example_tools.get("allow_mnt_c_executables") is False
        )
        checks.append(
            _check(
                "portable_tool_configuration_example",
                PASS if values_are_portable else FAIL,
                (
                    "Tool example contains portable null placeholders and safe defaults."
                    if values_are_portable
                    else "Tool example must use null placeholders and reject /mnt/c executables by default."
                ),
                path="config/tools.example.json",
            )
        )

    reproducibility = parsed_json.get("config/reproducibility.json")
    if reproducibility is not None:
        private_inputs = reproducibility.get("private_inputs")
        validation_tiers = reproducibility.get("validation_tiers")
        scope = reproducibility.get("scope")
        contract_valid = (
            reproducibility.get("schema") == "cfdpipe.reproducibility.v1"
            and reproducibility.get("baseline_status")
            == "VERIFIED_ON_REFERENCE_WORKSTATION"
            and reproducibility.get("platform")
            == {"operating_system": "Windows", "architecture": "x86_64"}
            and reproducibility.get("verified_toolchain") == EXPECTED_TOOLCHAIN
            and private_inputs
            == {
                "version_control_policy": "EXCLUDED_FROM_GIT",
                "integrity_contract": "config/markers.toml",
                "missing_default_preflight_status": WARN,
                "strict_preflight_option": "--require-private-inputs",
            }
            and isinstance(validation_tiers, dict)
            and validation_tiers.get("offline_repository_preflight")
            == "python scripts/repro/verify_repository.py"
            and validation_tiers.get("offline_repository_preflight_strict")
            == (
                "python scripts/repro/verify_repository.py "
                "--require-private-inputs"
            )
            and validation_tiers.get("cad_free_standard_library_tests")
            == "python -B scripts/repro/run_cad_free_tests.py"
            and validation_tiers.get("full_standard_library_tests")
            == "python -m unittest discover -s tests -v"
            and validation_tiers.get("full_tests_require_private_inputs") is True
            and isinstance(scope, dict)
            and scope.get("external_programs_started_by_offline_preflight") is False
            and scope.get("network_accessed_by_offline_preflight") is False
            and scope.get("production_mesh_or_cfd_included") is False
        )
        checks.append(
            _check(
                "reproducibility_contract",
                PASS if contract_valid else FAIL,
                (
                    "Reproducibility contract strictly binds versions, overrides, validation tiers, private inputs, and offline scope."
                    if contract_valid
                    else "Reproducibility contract differs from the verified baseline or required offline/private-input policy."
                ),
                path="config/reproducibility.json",
            )
        )

    checks.append(_validate_entrypoint(repository_root))

    python_files = list(_iter_python_files(repository_root))
    violations: list[dict[str, Any]] = []
    allowed_popen_calls: list[dict[str, Any]] = []
    for path, relative in python_files:
        file_violations, file_popen_calls = _python_policy_violations(
            path,
            relative,
        )
        violations.extend(file_violations)
        allowed_popen_calls.extend(file_popen_calls)
    if len(allowed_popen_calls) != 1:
        violations.append(
            {
                "count": len(allowed_popen_calls),
                "kind": "command_runner_popen_count",
                "path": "src/cfdpipe/process.py",
            }
        )
    else:
        popen_call = allowed_popen_calls[0]
        if not popen_call["shell_explicitly_false"]:
            violations.append(
                {
                    "kind": "command_runner_shell_not_explicitly_false",
                    "line": popen_call["line"],
                    "path": popen_call["path"],
                }
            )
        if not popen_call["first_argument_is_command"]:
            violations.append(
                {
                    "kind": "command_runner_argument_not_command_list",
                    "line": popen_call["line"],
                    "path": popen_call["path"],
                }
            )
    violations.sort(
        key=lambda item: (
            str(item.get("path", "")),
            int(item.get("line", 0)),
            str(item.get("kind", "")),
        )
    )
    checks.append(
        _check(
            "python_source_policy",
            FAIL if violations else PASS,
            (
                "Python policy violations were detected."
                if violations
                else "Python files parse; only CommandRunner creates a process with an argument list and shell disabled; GUI and ordinary ParaView imports are absent."
            ),
            details={
                "command_runner_popen_calls": len(allowed_popen_calls),
                "files_scanned": len(python_files),
                "violations": violations,
            },
        )
    )

    markers = parsed_toml.get("config/markers.toml")
    if markers is not None:
        for section_name, check_id in PRIVATE_INPUT_SECTIONS:
            section = markers.get(section_name)
            if not isinstance(section, dict):
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        f"markers.toml section [{section_name}] is missing or invalid.",
                        path="config/markers.toml",
                    )
                )
                continue

            relative = section.get("path")
            expected = section.get("sha256")
            declared_read_only = section.get("read_only")
            if declared_read_only is not True:
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        f"markers.toml [{section_name}].read_only must be true.",
                        path="config/markers.toml",
                    )
                )
                continue
            if not isinstance(relative, str) or not relative.strip():
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        f"markers.toml [{section_name}].path is missing or invalid.",
                        path="config/markers.toml",
                    )
                )
                continue
            relative = relative.replace("\\", "/")
            if not _is_sha256(expected):
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        f"markers.toml [{section_name}].sha256 is not a SHA-256 value.",
                        path=relative,
                    )
                )
                continue
            if _is_absolute_cross_platform(relative):
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        "Private input path must be repository-relative.",
                        path=relative,
                    )
                )
                continue

            candidate = (repository_root / relative).resolve(strict=False)
            if not _inside_root(repository_root, candidate):
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        "Private input path escapes the repository root.",
                        path=relative,
                    )
                )
                continue
            if not candidate.exists():
                missing_status = FAIL if require_private_inputs else WARN
                checks.append(
                    _check(
                        check_id,
                        missing_status,
                        (
                            "Required private input is missing in strict mode."
                            if require_private_inputs
                            else "Private input is not present; copy it locally before geometry or CFD stages."
                        ),
                        path=relative,
                        details={"expected_sha256": str(expected).lower()},
                    )
                )
                continue
            if not candidate.is_file():
                checks.append(
                    _check(
                        check_id,
                        FAIL,
                        "Private input path exists but is not a regular file.",
                        path=relative,
                    )
                )
                continue

            actual = _sha256_file(candidate)
            expected_lower = str(expected).lower()
            read_only = _is_read_only(candidate)
            matches = actual == expected_lower and read_only
            if actual != expected_lower:
                message = "Private input SHA-256 does not match markers.toml."
            elif not read_only:
                message = "Private input exists but is writable; the contract requires read-only bytes."
            else:
                message = "Private input exists, is read-only, and matches markers.toml."
            checks.append(
                _check(
                    check_id,
                    PASS if matches else FAIL,
                    message,
                    path=relative,
                    details={
                        "actual_sha256": actual,
                        "actual_read_only": read_only,
                        "declared_read_only": True,
                        "expected_sha256": expected_lower,
                    },
                )
            )

    status, summary = _aggregate(checks)
    return {
        "checks": checks,
        "require_private_inputs": require_private_inputs,
        "schema": SCHEMA,
        "status": status,
        "summary": summary,
    }


def render_report(report: Mapping[str, Any]) -> str:
    """Serialize ``report`` with deterministic key order and line endings."""

    return json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _resolve_output_path(root: Path, output: str | Path) -> Path:
    requested = Path(output).expanduser()
    if not requested.is_absolute():
        requested = root / requested
    requested = Path(os.path.abspath(requested))
    runs_root = Path(os.path.abspath(root / "runs"))
    if requested == runs_root or not _inside_root(runs_root, requested):
        raise ValueError("--output must name a file below the repository runs directory")
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(f"output already exists: {requested.name}")

    try:
        parent_parts = requested.parent.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("--output escapes the repository root") from exc
    current = root
    for part in parent_parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        if _is_link_or_reparse_point(current):
            raise ValueError(
                "--output ancestor must not be a symlink, junction, or reparse point"
            )
        if not current.is_dir():
            raise ValueError("--output ancestor is not a directory")

    resolved = requested.resolve(strict=False)
    resolved_runs_root = runs_root.resolve(strict=False)
    if not _inside_root(resolved_runs_root, resolved):
        raise ValueError("--output resolves outside the repository runs directory")
    return requested


def _write_output(root: Path, path: Path, payload: str) -> None:
    try:
        parent_parts = path.parent.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("report output parent escapes the repository root") from exc

    current = root
    for part in parent_parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            try:
                current.mkdir()
            except FileExistsError:
                pass
        if _is_link_or_reparse_point(current):
            raise ValueError(
                "report output ancestor became a symlink, junction, or reparse point"
            )
        if not current.is_dir():
            raise ValueError("report output ancestor is not a directory")
        if not _inside_root(root, current.resolve(strict=True)):
            raise ValueError("report output ancestor resolves outside the repository")

    revalidated = _resolve_output_path(root, path)
    if revalidated != path:
        raise ValueError("report output path changed during validation")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the portable repository layer without starting Gmsh, SU2, "
            "ParaView, MPI, or any other external program."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root (defaults to the root containing this script).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output file below the repository runs directory.",
    )
    parser.add_argument(
        "--require-private-inputs",
        action="store_true",
        help="Treat missing STEP/BREP inputs referenced by markers.toml as failures.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve(strict=False)
    report = verify_repository(
        root,
        require_private_inputs=args.require_private_inputs,
    )
    payload = render_report(report)

    if args.output is None:
        sys.stdout.write(payload)
    else:
        try:
            output_path = _resolve_output_path(root, args.output)
            _write_output(root, output_path, payload)
        except (OSError, ValueError) as exc:
            checks = list(report.get("checks", []))
            checks.append(
                _check(
                    "report_output",
                    FAIL,
                    f"Unable to write requested report: {exc}",
                )
            )
            status, summary = _aggregate(checks)
            failed_report = dict(report)
            failed_report.update(
                {"checks": checks, "status": status, "summary": summary}
            )
            sys.stdout.write(render_report(failed_report))
            return 1

    return 1 if report["status"] == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
