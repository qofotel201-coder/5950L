"""Safe SU2 command bridge and real connection-smoke workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import traceback
from typing import Any
import zipfile
import xml.etree.ElementTree as ElementTree

from ..atmosphere import AtmosphereState
from .gmsh_bridge import validate_su2_mesh
from ..process import CommandExecutionError, CommandResult


PathValue = str | os.PathLike[str]

VISUALIZATION_EXTENSIONS = frozenset({".vtk", ".vtu", ".pvtu", ".vtm"})
SOLUTION_PREFIX = "connection_solution"
RESTART_PREFIX = "connection_solution_restart"
_CONFIG_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$")
_VERSION_RE = re.compile(r'\bSU2\s+v(?P<version>\d+(?:\.\d+)+)\b', re.IGNORECASE)
_FATAL_RE = re.compile(
    r"\berror\b|\bfatal\b|\bnan\b|(?<![A-Za-z])[+-]?inf(?![A-Za-z])|segmentation\s+fault|"
    r"cannot\s+open\s+(?:the\s+)?mesh|marker[^\r\n]*not\s+found",
    re.IGNORECASE,
)
_NON_NAN_FATAL_RE = re.compile(
    r"\berror\b|\bfatal\b|(?<![A-Za-z])[+-]?inf(?![A-Za-z])|segmentation\s+fault|"
    r"cannot\s+open\s+(?:the\s+)?mesh|marker[^\r\n]*not\s+found",
    re.IGNORECASE,
)
_NONFATAL_2D_QUALITY_NAN_RE = re.compile(
    r"^\s*\|\s*Orthogonality\s+Angle\s*\(deg\.\)\s*\|"
    r"\s*[+-]?nan\s*\|\s*[+-]?nan\s*\|\s*$",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(r"\bwarning\b", re.IGNORECASE)
_NONFINITE_RE = re.compile(
    r"(?<![A-Za-z])(?:nan|[+-]?inf(?:inity)?)(?![A-Za-z])",
    re.IGNORECASE,
)
_DATA_ARRAY_TAG_RE = re.compile(rb"<DataArray\b([^>]*)/?>", re.IGNORECASE)
_XML_ATTRIBUTE_RE = re.compile(
    rb"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[\"']([^\"']*)[\"']"
)
_REFERENCE_CORE_KEYS = (
    "SOLVER",
    "MACH_NUMBER",
    "AOA",
    "MARKER_FAR",
    "OUTPUT_FILES",
    "TABULAR_FORMAT",
)
_REQUIRED_SYNTAX_TOKENS = (
    "SOLVER",
    "EULER",
    "MACH_NUMBER",
    "AOA",
    "SIDESLIP_ANGLE",
    "MESH_FILENAME",
    "MARKER_FAR",
    "TABULAR_FORMAT",
    "CSV",
    "CONV_FILENAME",
    "VOLUME_FILENAME",
    "RESTART_FILENAME",
    "OUTPUT_FILES",
    "RESTART",
    "HISTORY_OUTPUT",
    "RMS_RES",
    "OUTPUT_WRT_FREQ",
    "CONV_STARTITER",
    "CONV_RESIDUAL_MINVAL",
    "CONV_NUM_METHOD_FLOW",
    "SOLUTION_FILENAME",
)
_PROJECT_PILOT_SYNTAX_TOKENS = (
    "MARKER_EULER",
    "MARKER_SUPERSONIC_OUTLET",
)
_PROJECT_DIAGNOSTIC_SYNTAX_TOKENS = (
    "FREESTREAM_PRESSURE",
    "FREESTREAM_TEMPERATURE",
    "REF_AREA",
    "REF_LENGTH",
    "REF_ORIGIN_MOMENT_X",
    "REF_ORIGIN_MOMENT_Y",
    "REF_ORIGIN_MOMENT_Z",
)
_PROJECT_RANS_SYNTAX_TOKENS = (
    "RANS",
    "KIND_TURB_MODEL",
    "SST",
    "INIT_OPTION",
    "TD_CONDITIONS",
    "FREESTREAM_OPTION",
    "TEMPERATURE_FS",
    "FLUID_MODEL",
    "STANDARD_AIR",
    "VISCOSITY_MODEL",
    "SUTHERLAND",
    "FREESTREAM_TURBULENCEINTENSITY",
    "FREESTREAM_TURB2LAMVISCRATIO",
    "REF_DIMENSIONALIZATION",
    "DIMENSIONAL",
    "MARKER_HEATFLUX",
    "MARKER_MONITORING",
    "MARKER_PLOTTING",
    "NUM_METHOD_GRAD",
    "GREEN_GAUSS",
    "CFL_NUMBER",
    "CFL_ADAPT",
    "NO",
    "ROE",
    "MUSCL_FLOW",
    "CONV_NUM_METHOD_TURB",
    "SCALAR_UPWIND",
    "MUSCL_TURB",
    "TIME_DISCRE_FLOW",
    "TIME_DISCRE_TURB",
    "EULER_IMPLICIT",
    "LINEAR_SOLVER",
    "FGMRES",
    "LINEAR_SOLVER_PREC",
    "ILU",
    "LINEAR_SOLVER_ERROR",
    "LINEAR_SOLVER_ITER",
    "VOLUME_OUTPUT",
    "COORDINATES",
    "SOLUTION",
    "PRIMITIVE",
    "Y_PLUS",
    "SKIN_FRICTION-X",
    "SKIN_FRICTION-Y",
    "SKIN_FRICTION-Z",
    "SURFACE_FILENAME",
    "SURFACE_PARAVIEW",
    "AERO_COEFF",
    "LINSOL",
)
_PROJECT_RANS_RESTART_SYNTAX_TOKENS = (
    "RESTART_SOL",
    "READ_BINARY_RESTART",
    "SOLUTION_FILENAME",
    "MUSCL_FLOW",
    "MUSCL_TURB",
    "YES",
)
_SU2_MARKER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SU2_LOCAL_STEM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class SU2BridgeError(RuntimeError):
    """Raised when SU2 discovery, configuration, execution, or output fails."""


@dataclass(frozen=True)
class SU2Reference:
    """Version-local configuration and syntax evidence."""

    path: str
    sha256: str
    content: str
    iteration_key: str
    paraview_output: str
    syntax_evidence: tuple[dict[str, str], ...]
    supported_tokens: tuple[str, ...]

    def as_manifest(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "iteration_key": self.iteration_key,
            "paraview_output": self.paraview_output,
            "syntax_evidence": [dict(item) for item in self.syntax_evidence],
            "supported_tokens": list(self.supported_tokens),
        }


@dataclass(frozen=True)
class SU2AngleMapping:
    """Auditable mapping from project axes to SU2's 3-D angle convention."""

    project_alpha_deg: float
    project_beta_deg: float
    su2_aoa_deg: float
    su2_sideslip_angle_deg: float
    project_unit_vector_xyz: tuple[float, float, float]
    su2_unit_vector_xyz: tuple[float, float, float]
    maximum_component_error: float

    def as_manifest(self) -> dict[str, Any]:
        return {
            "convention": {
                "project": (
                    "positive alpha tilts +X toward +Y; positive beta tilts "
                    "the resulting direction toward +Z"
                ),
                "su2_8_5_0": (
                    "Ux=U*cos(AOA)*cos(AoS), Uy=U*sin(AoS), "
                    "Uz=U*sin(AOA)*cos(AoS)"
                ),
            },
            "project_alpha_deg": self.project_alpha_deg,
            "project_beta_deg": self.project_beta_deg,
            "su2_aoa_deg": self.su2_aoa_deg,
            "su2_sideslip_angle_deg": self.su2_sideslip_angle_deg,
            "project_unit_vector_xyz": list(self.project_unit_vector_xyz),
            "su2_unit_vector_xyz": list(self.su2_unit_vector_xyz),
            "maximum_component_error": self.maximum_component_error,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _config_assignments(content: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in content.splitlines():
        candidate = line.split("%", 1)[0]
        match = _CONFIG_ASSIGNMENT_RE.match(candidate)
        if match is not None:
            assignments[match.group(1).upper()] = match.group(2).strip()
    return assignments


def _token_in_evidence(token: str, payloads: Sequence[bytes]) -> bool:
    pattern = re.compile(
        rb"(?<![A-Z0-9_])" + re.escape(token.encode("ascii")) + rb"(?![A-Z0-9_])"
    )
    return any(pattern.search(payload.upper()) is not None for payload in payloads)


def _reference_priority(display_path: str, content: str) -> tuple[int, int, str]:
    normalized_path = display_path.replace("\\", "/").lower()
    upper_content = content.upper()
    score = 0
    if re.search(r"(?m)^\s*SOLVER\s*=\s*EULER\s*(?:%.*)?$", upper_content):
        score += 10_000
    score += 100 * sum(
        1 for key in _REFERENCE_CORE_KEYS if re.search(rf"\b{re.escape(key)}\b", upper_content)
    )
    if "config_template.cfg" in normalized_path:
        score += 1_000
    for keyword, bonus in (
        ("quickstart", 500),
        ("testcases", 400),
        ("tutorial", 300),
        ("example", 200),
    ):
        if keyword in normalized_path:
            score += bonus
            break
    return score, -len(display_path), display_path


def discover_su2_reference(su2_cfd: PathValue) -> SU2Reference:
    """Find the best Euler syntax reference shipped with the selected SU2."""

    executable = Path(su2_cfd).expanduser().resolve(strict=True)
    install_root = executable.parent.parent
    candidates: list[tuple[str, bytes]] = []

    for path in sorted(install_root.rglob("*.cfg")):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        candidates.append((str(path.resolve()), content))

    for archive_path in sorted(install_root.rglob("*.zip")):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in sorted(archive.namelist()):
                    if not member.lower().endswith(".cfg"):
                        continue
                    content = archive.read(member)
                    candidates.append(
                        (f"{archive_path.resolve()}::{member}", content)
                    )
        except (OSError, zipfile.BadZipFile, KeyError):
            continue

    decoded: list[tuple[str, bytes, str]] = []
    for display_path, content in candidates:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        decoded.append((display_path, content, text))
    if not decoded:
        raise SU2BridgeError(
            f"no SU2 configuration reference was found under {install_root}"
        )

    display_path, reference_bytes, reference_text = max(
        decoded,
        key=lambda item: _reference_priority(item[0], item[2]),
    )
    if not re.search(
        r"(?mi)^\s*SOLVER\s*=\s*EULER\s*(?:%.*)?$", reference_text
    ):
        raise SU2BridgeError(
            f"no shipped Euler configuration reference was found under {install_root}"
        )

    evidence: list[dict[str, str]] = [
        {
            "path": display_path,
            "sha256": _bytes_sha256(reference_bytes),
            "kind": "configuration",
        }
    ]
    payloads: list[bytes] = [reference_bytes]
    config_module = install_root / "bin" / "SU2" / "io" / "config.py"
    if config_module.is_file():
        config_bytes = config_module.read_bytes()
        payloads.append(config_bytes)
        evidence.append(
            {
                "path": str(config_module.resolve()),
                "sha256": _bytes_sha256(config_bytes),
                "kind": "configuration_parser",
            }
        )
    executable_bytes = executable.read_bytes()
    payloads.append(executable_bytes)
    evidence.append(
        {
            "path": str(executable),
            "sha256": _bytes_sha256(executable_bytes),
            "kind": "executable",
        }
    )

    supported = {
        token
        for token in (
            *_REQUIRED_SYNTAX_TOKENS,
            *_PROJECT_PILOT_SYNTAX_TOKENS,
            *_PROJECT_DIAGNOSTIC_SYNTAX_TOKENS,
            *_PROJECT_RANS_SYNTAX_TOKENS,
            *_PROJECT_RANS_RESTART_SYNTAX_TOKENS,
            "INNER_ITER",
            "ITER",
            "PARAVIEW",
            "PARAVIEW_ASCII",
            "PARAVIEW_LEGACY",
        )
        if _token_in_evidence(token, payloads)
    }
    missing = sorted(set(_REQUIRED_SYNTAX_TOKENS) - supported)
    if missing:
        raise SU2BridgeError(
            "current SU2 installation lacks syntax evidence for: "
            + ", ".join(missing)
        )
    if "INNER_ITER" in supported:
        iteration_key = "INNER_ITER"
    elif "ITER" in supported:
        iteration_key = "ITER"
    else:
        raise SU2BridgeError("current SU2 installation has no supported iteration key")
    paraview_output = next(
        (
            value
            for value in ("PARAVIEW", "PARAVIEW_ASCII", "PARAVIEW_LEGACY")
            if value in supported
        ),
        None,
    )
    if paraview_output is None:
        raise SU2BridgeError(
            "current SU2 installation has no evidenced ParaView volume output"
        )

    return SU2Reference(
        path=display_path,
        sha256=_bytes_sha256(reference_bytes),
        content=reference_text,
        iteration_key=iteration_key,
        paraview_output=paraview_output,
        syntax_evidence=tuple(evidence),
        supported_tokens=tuple(sorted(supported)),
    )


def render_smoke_config(reference: SU2Reference, max_iterations: int) -> str:
    """Render only the minimum keys evidenced by the selected SU2 install."""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if not 5 <= max_iterations <= 10:
        raise ValueError("max_iterations must be between 5 and 10")

    supported = set(reference.supported_tokens)
    missing = sorted(set(_REQUIRED_SYNTAX_TOKENS) - supported)
    if missing:
        raise SU2BridgeError(
            "SU2 reference lacks evidence for generated config syntax: "
            + ", ".join(missing)
        )
    if reference.iteration_key not in supported:
        raise SU2BridgeError("SU2 iteration key is not present in syntax evidence")
    if reference.paraview_output not in supported:
        raise SU2BridgeError("SU2 ParaView output value is not present in syntax evidence")

    assignments = _config_assignments(reference.content)
    convective_method = assignments.get("CONV_NUM_METHOD_FLOW", "").strip()
    if not convective_method:
        raise SU2BridgeError(
            "shipped Euler reference has no CONV_NUM_METHOD_FLOW assignment"
        )
    if any(character in convective_method for character in "\r\n"):
        raise SU2BridgeError("invalid CONV_NUM_METHOD_FLOW reference value")

    lines = [
        "% CFDPIPE Python-to-SU2 connection smoke case",
        f"% SU2 reference SHA256: {reference.sha256}",
        "SOLVER= EULER",
        "MACH_NUMBER= 0.3",
        "AOA= 0.0",
        "SIDESLIP_ANGLE= 0.0",
        "MARKER_FAR= ( farfield )",
        f"CONV_NUM_METHOD_FLOW= {convective_method}",
        f"{reference.iteration_key}= {max_iterations}",
        "CONV_RESIDUAL_MINVAL= -30",
        f"CONV_STARTITER= {max_iterations + 1}",
        "MESH_FILENAME= smoke.su2",
        "CONV_FILENAME= history",
        "VOLUME_FILENAME= connection_solution",
        "RESTART_FILENAME= connection_solution_restart",
        "TABULAR_FORMAT= CSV",
        "HISTORY_OUTPUT= ITER, RMS_RES",
        "OUTPUT_WRT_FREQ= 1",
        f"OUTPUT_FILES= RESTART, {reference.paraview_output}",
    ]
    rendered = "\n".join(lines) + "\n"
    if re.search(r"(?i)[A-Z]:[\\/]", rendered):
        raise SU2BridgeError("generated SU2 config contains an absolute Windows path")
    return rendered


def _project_case_number(case: Mapping[str, Any], key: str) -> float:
    raw = case.get(key)
    if isinstance(raw, bool):
        raise SU2BridgeError(f"project case {key} must be a finite number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise SU2BridgeError(f"project case {key} must be a finite number") from error
    if not math.isfinite(value):
        raise SU2BridgeError(f"project case {key} must be a finite number")
    return value


def _project_marker(value: object, *, label: str) -> str:
    marker = str(value).strip() if isinstance(value, str) else ""
    if not marker or _SU2_MARKER_RE.fullmatch(marker) is None:
        raise SU2BridgeError(f"{label} is not a safe SU2 marker name: {value!r}")
    return marker


def project_angles_to_su2(
    project_alpha_deg: float,
    project_beta_deg: float,
) -> SU2AngleMapping:
    """Convert project aerodynamic angles to SU2 8.5's 3-D convention.

    Project positive angle of attack is in the ``+X``/``+Y`` plane and
    positive sideslip points toward ``+Z``.  SU2 uses AOA for the ``+Z``
    component and SIDESLIP_ANGLE for ``+Y``.  The conversion is performed
    through the unit vector so simultaneous non-zero angles remain exact.
    """

    alpha = _project_case_number({"value": project_alpha_deg}, "value")
    beta = _project_case_number({"value": project_beta_deg}, "value")
    alpha_rad = math.radians(alpha)
    beta_rad = math.radians(beta)
    project_vector = (
        math.cos(alpha_rad) * math.cos(beta_rad),
        math.sin(alpha_rad) * math.cos(beta_rad),
        math.sin(beta_rad),
    )
    if project_vector[0] <= 0.0:
        raise SU2BridgeError(
            "project freestream direction must retain a positive +X component"
        )
    su2_sideslip_rad = math.asin(max(-1.0, min(1.0, project_vector[1])))
    su2_aoa_rad = math.atan2(project_vector[2], project_vector[0])
    su2_vector = (
        math.cos(su2_aoa_rad) * math.cos(su2_sideslip_rad),
        math.sin(su2_sideslip_rad),
        math.sin(su2_aoa_rad) * math.cos(su2_sideslip_rad),
    )
    maximum_error = max(
        abs(expected - actual)
        for expected, actual in zip(project_vector, su2_vector, strict=True)
    )
    if maximum_error > 1.0e-12:
        raise SU2BridgeError(
            f"project-to-SU2 direction conversion error {maximum_error} exceeds tolerance"
        )
    return SU2AngleMapping(
        project_alpha_deg=alpha,
        project_beta_deg=beta,
        su2_aoa_deg=math.degrees(su2_aoa_rad),
        su2_sideslip_angle_deg=math.degrees(su2_sideslip_rad),
        project_unit_vector_xyz=project_vector,
        su2_unit_vector_xyz=su2_vector,
        maximum_component_error=maximum_error,
    )


def render_project_supersonic_pilot_config(
    reference: SU2Reference,
    selected_case: Mapping[str, Any],
    *,
    farfield_marker: str,
    wall_marker: str,
    rear_outlet_markers: Sequence[str],
    max_iterations: int,
) -> str:
    """Render the audited, non-production 3-D supersonic outlet pilot.

    This renderer deliberately has no pressure or back-pressure input.  It is
    valid only for the connection-scale diagnostic gate owned by the real
    pipeline; it does not freeze a production boundary condition.
    """

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if not 1 <= max_iterations <= 5:
        raise ValueError("project pilot max_iterations must be between 1 and 5")
    supported = set(reference.supported_tokens)
    required = set(_REQUIRED_SYNTAX_TOKENS) | set(_PROJECT_PILOT_SYNTAX_TOKENS)
    missing = sorted(required - supported)
    if missing:
        raise SU2BridgeError(
            "current SU2 lacks project pilot syntax evidence for: "
            + ", ".join(missing)
        )
    if reference.iteration_key not in supported:
        raise SU2BridgeError("SU2 iteration key is not present in syntax evidence")
    if reference.paraview_output not in supported:
        raise SU2BridgeError("SU2 ParaView output value is not present in syntax evidence")

    assignments = _config_assignments(reference.content)
    convective_method = assignments.get("CONV_NUM_METHOD_FLOW", "").strip()
    if not convective_method or any(character in convective_method for character in "\r\n"):
        raise SU2BridgeError(
            "shipped Euler reference has no safe CONV_NUM_METHOD_FLOW assignment"
        )

    farfield = _project_marker(farfield_marker, label="farfield marker")
    wall = _project_marker(wall_marker, label="wall marker")
    if isinstance(rear_outlet_markers, (str, bytes)):
        raise TypeError("rear_outlet_markers must be a sequence")
    outlets = tuple(
        _project_marker(value, label="rear outlet marker")
        for value in rear_outlet_markers
    )
    if len(outlets) != 2 or len(set(outlets)) != 2:
        raise SU2BridgeError("project pilot requires exactly two distinct rear outlets")
    if farfield in {wall, *outlets} or wall in set(outlets):
        raise SU2BridgeError("project pilot boundary marker names must be distinct")

    mach = _project_case_number(selected_case, "mach")
    alpha = _project_case_number(selected_case, "alpha_deg")
    beta = _project_case_number(selected_case, "beta_deg")
    angle_mapping = project_angles_to_su2(alpha, beta)
    if mach <= 0.0:
        raise SU2BridgeError("project case Mach number must be greater than zero")

    lines = [
        "% CFDPIPE real-project connection-only supersonic outlet pilot",
        "% boundary_mode_frozen=false; production_eligible=false",
        f"% SU2 reference SHA256: {reference.sha256}",
        "SOLVER= EULER",
        f"MACH_NUMBER= {mach}",
        f"% project_alpha_deg= {alpha}; project_beta_deg= {beta}",
        f"AOA= {angle_mapping.su2_aoa_deg:.17g}",
        f"SIDESLIP_ANGLE= {angle_mapping.su2_sideslip_angle_deg:.17g}",
        f"MARKER_FAR= ( {farfield} )",
        f"MARKER_EULER= ( {wall} )",
        "MARKER_SUPERSONIC_OUTLET= ( " + ", ".join(outlets) + " )",
        f"CONV_NUM_METHOD_FLOW= {convective_method}",
        f"{reference.iteration_key}= {max_iterations}",
        "CONV_RESIDUAL_MINVAL= -30",
        f"CONV_STARTITER= {max_iterations + 1}",
        "MESH_FILENAME= mesh.su2",
        "CONV_FILENAME= history",
        "VOLUME_FILENAME= connection_solution",
        "RESTART_FILENAME= connection_solution_restart",
        "TABULAR_FORMAT= CSV",
        "HISTORY_OUTPUT= ITER, RMS_RES",
        "OUTPUT_WRT_FREQ= 1",
        f"OUTPUT_FILES= RESTART, {reference.paraview_output}",
    ]
    rendered = "\n".join(lines) + "\n"
    if re.search(r"(?i)[A-Z]:[\\/]", rendered):
        raise SU2BridgeError("generated project pilot config contains an absolute path")
    rendered_assignments = _config_assignments(rendered)
    forbidden = {
        "MARKER_OUTLET",
        "FREESTREAM_PRESSURE",
        "OUTLET_PRESSURE",
        "BACK_PRESSURE",
    }
    present_forbidden = sorted(forbidden & set(rendered_assignments))
    if present_forbidden:
        raise SU2BridgeError(
            "project pilot must not set pressure/back-pressure keys: "
            + ", ".join(present_forbidden)
        )
    return rendered


def _finite_positive(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise SU2BridgeError(f"{label} must be a finite positive number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise SU2BridgeError(f"{label} must be a finite positive number") from error
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise SU2BridgeError(f"{label} must be a finite positive number")
    return numeric


def render_project_outlet_diagnostic_config(
    reference: SU2Reference,
    selected_case: Mapping[str, Any],
    atmosphere: AtmosphereState,
    *,
    reference_area_m2: float,
    reference_length_m: float,
    moment_origin_m: Sequence[float],
    farfield_marker: str,
    wall_marker: str,
    rear_outlet_markers: Sequence[str],
    max_iterations: int,
) -> tuple[str, SU2AngleMapping]:
    """Render a bounded Euler diagnostic with explicit SI freestream state."""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if not 20 <= max_iterations <= 500:
        raise ValueError("outlet diagnostic max_iterations must be between 20 and 500")
    supported = set(reference.supported_tokens)
    required = (
        set(_REQUIRED_SYNTAX_TOKENS)
        | set(_PROJECT_PILOT_SYNTAX_TOKENS)
        | set(_PROJECT_DIAGNOSTIC_SYNTAX_TOKENS)
    )
    missing = sorted(required - supported)
    if missing:
        raise SU2BridgeError(
            "current SU2 lacks outlet diagnostic syntax evidence for: "
            + ", ".join(missing)
        )
    assignments = _config_assignments(reference.content)
    convective_method = assignments.get("CONV_NUM_METHOD_FLOW", "").strip()
    if not convective_method or any(character in convective_method for character in "\r\n"):
        raise SU2BridgeError(
            "shipped Euler reference has no safe CONV_NUM_METHOD_FLOW assignment"
        )

    mach = _project_case_number(selected_case, "mach")
    alpha = _project_case_number(selected_case, "alpha_deg")
    beta = _project_case_number(selected_case, "beta_deg")
    altitude_km = _project_case_number(selected_case, "altitude_km")
    if mach <= 0.0 or altitude_km < 0.0:
        raise SU2BridgeError("project Mach must be positive and altitude non-negative")
    if abs(atmosphere.geopotential_altitude_m - 1000.0 * altitude_km) > 1.0e-6:
        raise SU2BridgeError("atmosphere altitude does not match selected project case")
    area = _finite_positive(reference_area_m2, label="reference_area_m2")
    length = _finite_positive(reference_length_m, label="reference_length_m")
    if isinstance(moment_origin_m, (str, bytes)) or len(moment_origin_m) != 3:
        raise SU2BridgeError("moment_origin_m must contain exactly three coordinates")
    origin = tuple(
        _project_case_number({"value": value}, "value") for value in moment_origin_m
    )

    farfield = _project_marker(farfield_marker, label="farfield marker")
    wall = _project_marker(wall_marker, label="wall marker")
    if isinstance(rear_outlet_markers, (str, bytes)):
        raise TypeError("rear_outlet_markers must be a sequence")
    outlets = tuple(
        _project_marker(value, label="rear outlet marker")
        for value in rear_outlet_markers
    )
    if len(outlets) != 2 or len(set(outlets)) != 2:
        raise SU2BridgeError("outlet diagnostic requires exactly two distinct rear outlets")
    if farfield in {wall, *outlets} or wall in set(outlets):
        raise SU2BridgeError("outlet diagnostic marker names must be distinct")
    angle_mapping = project_angles_to_su2(alpha, beta)

    lines = [
        "% CFDPIPE bounded real-project rear-outlet diagnostic",
        "% topology-smoke Euler evidence only; production_eligible=false",
        f"% SU2 reference SHA256: {reference.sha256}",
        "SOLVER= EULER",
        f"MACH_NUMBER= {mach:.15g}",
        f"% project_alpha_deg= {alpha:.15g}; project_beta_deg= {beta:.15g}",
        f"AOA= {angle_mapping.su2_aoa_deg:.15g}",
        f"SIDESLIP_ANGLE= {angle_mapping.su2_sideslip_angle_deg:.15g}",
        f"FREESTREAM_PRESSURE= {atmosphere.static_pressure_pa:.15g}",
        f"FREESTREAM_TEMPERATURE= {atmosphere.static_temperature_k:.15g}",
        f"REF_AREA= {area:.15g}",
        f"REF_LENGTH= {length:.15g}",
        f"REF_ORIGIN_MOMENT_X= {origin[0]:.15g}",
        f"REF_ORIGIN_MOMENT_Y= {origin[1]:.15g}",
        f"REF_ORIGIN_MOMENT_Z= {origin[2]:.15g}",
        f"MARKER_FAR= ( {farfield} )",
        f"MARKER_EULER= ( {wall} )",
        "MARKER_SUPERSONIC_OUTLET= ( " + ", ".join(outlets) + " )",
        f"CONV_NUM_METHOD_FLOW= {convective_method}",
        f"{reference.iteration_key}= {max_iterations}",
        "CONV_RESIDUAL_MINVAL= -30",
        f"CONV_STARTITER= {max_iterations + 1}",
        "MESH_FILENAME= mesh.su2",
        "CONV_FILENAME= history",
        "VOLUME_FILENAME= connection_solution",
        "RESTART_FILENAME= connection_solution_restart",
        "TABULAR_FORMAT= CSV",
        "HISTORY_OUTPUT= ITER, RMS_RES",
        "OUTPUT_WRT_FREQ= 1",
        f"OUTPUT_FILES= RESTART, {reference.paraview_output}",
    ]
    rendered = "\n".join(lines) + "\n"
    if re.search(r"(?i)[A-Z]:[\\/]", rendered):
        raise SU2BridgeError("generated outlet diagnostic config contains an absolute path")
    rendered_assignments = _config_assignments(rendered)
    forbidden = {"MARKER_OUTLET", "OUTLET_PRESSURE", "BACK_PRESSURE"}
    present_forbidden = sorted(forbidden & set(rendered_assignments))
    if present_forbidden:
        raise SU2BridgeError(
            "outlet diagnostic must not set outlet pressure/back-pressure keys: "
            + ", ".join(present_forbidden)
        )
    return rendered, angle_mapping


def discover_su2_rans_reference(su2_cfd: PathValue) -> SU2Reference:
    """Require current-install evidence for the bounded SST RANS smoke syntax.

    The Windows distribution used by this project ships an Euler example but
    no RANS test case.  The example, parser and selected executable therefore
    form one hash-recorded evidence bundle; every RANS-only key/value must be
    present in that exact executable before a configuration may be rendered.
    The subsequent real short run remains the authoritative combination test.
    """

    reference = discover_su2_reference(su2_cfd)
    required = (
        set(_REQUIRED_SYNTAX_TOKENS)
        | set(_PROJECT_PILOT_SYNTAX_TOKENS)
        | set(_PROJECT_DIAGNOSTIC_SYNTAX_TOKENS)
        | set(_PROJECT_RANS_SYNTAX_TOKENS)
    )
    missing = sorted(required - set(reference.supported_tokens))
    if missing:
        raise SU2BridgeError(
            "current SU2 lacks bounded RANS/SST syntax evidence for: "
            + ", ".join(missing)
        )
    return reference


def _finite_nonnegative(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise SU2BridgeError(f"{label} must be a finite non-negative number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise SU2BridgeError(
            f"{label} must be a finite non-negative number"
        ) from error
    if not math.isfinite(numeric) or numeric < 0.0:
        raise SU2BridgeError(f"{label} must be a finite non-negative number")
    return numeric


def _evidenced_su2_token(
    value: object,
    *,
    label: str,
    supported: set[str],
) -> str:
    token = str(value).strip().upper() if isinstance(value, str) else ""
    if not token or re.fullmatch(r"[A-Z][A-Z0-9_-]*", token) is None:
        raise SU2BridgeError(f"{label} is not a safe SU2 token: {value!r}")
    if token not in supported:
        raise SU2BridgeError(
            f"{label}={token} is absent from current SU2 syntax evidence"
        )
    return token


def render_project_rans_smoke_config(
    reference: SU2Reference,
    selected_case: Mapping[str, Any],
    atmosphere: AtmosphereState,
    *,
    reference_area_m2: float,
    reference_length_m: float,
    moment_origin_m: Sequence[float],
    farfield_marker: str,
    wall_marker: str,
    rear_outlet_markers: Sequence[str],
    max_iterations: int,
    cfl_number: float,
    gradient_method: str,
    flow_convective_method: str,
    turbulence_convective_method: str,
    flow_time_discretization: str,
    turbulence_time_discretization: str,
    linear_solver: str,
    linear_solver_preconditioner: str,
    linear_solver_error: float,
    linear_solver_iterations: int,
    freestream_turbulence_intensity: float,
    freestream_turbulent_to_laminar_viscosity_ratio: float,
    volume_fields: Sequence[str],
    output_files: Sequence[str],
) -> tuple[str, SU2AngleMapping]:
    """Render the hash-evidenced, first-order serial SST startup case."""

    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
        raise TypeError("max_iterations must be an integer")
    if not 20 <= max_iterations <= 500:
        raise ValueError("RANS smoke max_iterations must be between 20 and 500")
    if isinstance(linear_solver_iterations, bool) or not isinstance(
        linear_solver_iterations, int
    ):
        raise TypeError("linear_solver_iterations must be an integer")
    if not 1 <= linear_solver_iterations <= 100:
        raise ValueError("linear_solver_iterations must be between 1 and 100")
    supported = set(reference.supported_tokens)
    required = (
        set(_REQUIRED_SYNTAX_TOKENS)
        | set(_PROJECT_PILOT_SYNTAX_TOKENS)
        | set(_PROJECT_DIAGNOSTIC_SYNTAX_TOKENS)
        | set(_PROJECT_RANS_SYNTAX_TOKENS)
    )
    missing = sorted(required - supported)
    if missing:
        raise SU2BridgeError(
            "current SU2 lacks bounded RANS/SST syntax evidence for: "
            + ", ".join(missing)
        )

    mach = _project_case_number(selected_case, "mach")
    alpha = _project_case_number(selected_case, "alpha_deg")
    beta = _project_case_number(selected_case, "beta_deg")
    altitude_km = _project_case_number(selected_case, "altitude_km")
    if mach <= 0.0 or altitude_km < 0.0:
        raise SU2BridgeError("project Mach must be positive and altitude non-negative")
    if abs(atmosphere.geopotential_altitude_m - 1000.0 * altitude_km) > 1.0e-6:
        raise SU2BridgeError("atmosphere altitude does not match selected project case")
    area = _finite_positive(reference_area_m2, label="reference_area_m2")
    length = _finite_positive(reference_length_m, label="reference_length_m")
    if isinstance(moment_origin_m, (str, bytes)) or len(moment_origin_m) != 3:
        raise SU2BridgeError("moment_origin_m must contain exactly three coordinates")
    origin = tuple(
        _project_case_number({"value": value}, "value") for value in moment_origin_m
    )

    farfield = _project_marker(farfield_marker, label="farfield marker")
    wall = _project_marker(wall_marker, label="wall marker")
    if isinstance(rear_outlet_markers, (str, bytes)):
        raise TypeError("rear_outlet_markers must be a sequence")
    outlets = tuple(
        _project_marker(value, label="rear outlet marker")
        for value in rear_outlet_markers
    )
    if len(outlets) != 2 or len(set(outlets)) != 2:
        raise SU2BridgeError("RANS smoke requires exactly two distinct rear outlets")
    if farfield in {wall, *outlets} or wall in set(outlets):
        raise SU2BridgeError("RANS smoke boundary marker names must be distinct")

    cfl = _finite_positive(cfl_number, label="cfl_number")
    if cfl > 1.0:
        raise SU2BridgeError("RANS smoke CFL must not exceed 1.0")
    linear_error = _finite_positive(linear_solver_error, label="linear_solver_error")
    turbulence_intensity = _finite_positive(
        freestream_turbulence_intensity,
        label="freestream_turbulence_intensity",
    )
    if turbulence_intensity > 1.0:
        raise SU2BridgeError("freestream turbulence intensity must not exceed 1.0")
    viscosity_ratio = _finite_positive(
        freestream_turbulent_to_laminar_viscosity_ratio,
        label="freestream_turbulent_to_laminar_viscosity_ratio",
    )
    gradient = _evidenced_su2_token(
        gradient_method, label="gradient_method", supported=supported
    )
    flow_convective = _evidenced_su2_token(
        flow_convective_method,
        label="flow_convective_method",
        supported=supported,
    )
    turbulence_convective = _evidenced_su2_token(
        turbulence_convective_method,
        label="turbulence_convective_method",
        supported=supported,
    )
    flow_time = _evidenced_su2_token(
        flow_time_discretization,
        label="flow_time_discretization",
        supported=supported,
    )
    turbulence_time = _evidenced_su2_token(
        turbulence_time_discretization,
        label="turbulence_time_discretization",
        supported=supported,
    )
    solver = _evidenced_su2_token(
        linear_solver, label="linear_solver", supported=supported
    )
    preconditioner = _evidenced_su2_token(
        linear_solver_preconditioner,
        label="linear_solver_preconditioner",
        supported=supported,
    )
    if isinstance(volume_fields, (str, bytes)) or not volume_fields:
        raise SU2BridgeError("volume_fields must be a non-empty sequence")
    fields = tuple(
        _evidenced_su2_token(value, label="volume field", supported=supported)
        for value in volume_fields
    )
    if fields != ("COORDINATES", "SOLUTION", "PRIMITIVE"):
        raise SU2BridgeError(
            "RANS smoke volume output must use the current SU2 groups "
            "COORDINATES, SOLUTION, PRIMITIVE"
        )
    if isinstance(output_files, (str, bytes)) or not output_files:
        raise SU2BridgeError("output_files must be a non-empty sequence")
    outputs = tuple(
        _evidenced_su2_token(value, label="output file", supported=supported)
        for value in output_files
    )
    if len(outputs) != len(set(outputs)) or not {
        "RESTART",
        reference.paraview_output,
        "SURFACE_PARAVIEW",
    }.issubset(outputs):
        raise SU2BridgeError(
            "RANS smoke outputs must uniquely include RESTART, volume ParaView, "
            "and SURFACE_PARAVIEW"
        )

    angle_mapping = project_angles_to_su2(alpha, beta)
    lines = [
        "% CFDPIPE bounded real-project first-order SST RANS smoke",
        "% diagnostic_only=true; production_eligible=false; convergence_claimed=false",
        "% rear outlet boundary is provisional and has no pressure/back-pressure",
        f"% SU2 reference SHA256: {reference.sha256}",
        "SOLVER= RANS",
        "KIND_TURB_MODEL= SST",
        f"MACH_NUMBER= {mach:.15g}",
        f"% project_alpha_deg= {alpha:.15g}; project_beta_deg= {beta:.15g}",
        f"AOA= {angle_mapping.su2_aoa_deg:.15g}",
        f"SIDESLIP_ANGLE= {angle_mapping.su2_sideslip_angle_deg:.15g}",
        "INIT_OPTION= TD_CONDITIONS",
        "FREESTREAM_OPTION= TEMPERATURE_FS",
        f"FREESTREAM_PRESSURE= {atmosphere.static_pressure_pa:.15g}",
        f"FREESTREAM_TEMPERATURE= {atmosphere.static_temperature_k:.15g}",
        f"FREESTREAM_TURBULENCEINTENSITY= {turbulence_intensity:.15g}",
        f"FREESTREAM_TURB2LAMVISCRATIO= {viscosity_ratio:.15g}",
        "FLUID_MODEL= STANDARD_AIR",
        "VISCOSITY_MODEL= SUTHERLAND",
        "REF_DIMENSIONALIZATION= DIMENSIONAL",
        f"REF_AREA= {area:.15g}",
        f"REF_LENGTH= {length:.15g}",
        f"REF_ORIGIN_MOMENT_X= {origin[0]:.15g}",
        f"REF_ORIGIN_MOMENT_Y= {origin[1]:.15g}",
        f"REF_ORIGIN_MOMENT_Z= {origin[2]:.15g}",
        f"MARKER_FAR= ( {farfield} )",
        f"MARKER_HEATFLUX= ( {wall}, 0.0 )",
        "MARKER_SUPERSONIC_OUTLET= ( " + ", ".join(outlets) + " )",
        f"MARKER_MONITORING= ( {wall} )",
        f"MARKER_PLOTTING= ( {wall} )",
        f"NUM_METHOD_GRAD= {gradient}",
        f"CFL_NUMBER= {cfl:.15g}",
        "CFL_ADAPT= NO",
        f"CONV_NUM_METHOD_FLOW= {flow_convective}",
        "MUSCL_FLOW= NO",
        f"CONV_NUM_METHOD_TURB= {turbulence_convective}",
        "MUSCL_TURB= NO",
        f"TIME_DISCRE_FLOW= {flow_time}",
        f"TIME_DISCRE_TURB= {turbulence_time}",
        f"LINEAR_SOLVER= {solver}",
        f"LINEAR_SOLVER_PREC= {preconditioner}",
        f"LINEAR_SOLVER_ERROR= {linear_error:.15g}",
        f"LINEAR_SOLVER_ITER= {linear_solver_iterations}",
        f"{reference.iteration_key}= {max_iterations}",
        "CONV_RESIDUAL_MINVAL= -30",
        f"CONV_STARTITER= {max_iterations + 1}",
        "MESH_FILENAME= mesh.su2",
        "CONV_FILENAME= history",
        "VOLUME_FILENAME= rans_solution",
        "SURFACE_FILENAME= rans_surface",
        "RESTART_FILENAME= rans_solution_restart",
        "TABULAR_FORMAT= CSV",
        "HISTORY_OUTPUT= ITER, RMS_RES, AERO_COEFF, LINSOL",
        "OUTPUT_WRT_FREQ= 1",
        "VOLUME_OUTPUT= " + ", ".join(fields),
        "OUTPUT_FILES= " + ", ".join(outputs),
    ]
    rendered = "\n".join(lines) + "\n"
    if re.search(r"(?i)[A-Z]:[\\/]", rendered):
        raise SU2BridgeError("generated RANS smoke config contains an absolute path")
    assignments = _config_assignments(rendered)
    forbidden = {
        "MARKER_EULER",
        "MARKER_OUTLET",
        "OUTLET_PRESSURE",
        "BACK_PRESSURE",
        "ENABLE_CUDA",
    }
    present_forbidden = sorted(forbidden & set(assignments))
    if present_forbidden:
        raise SU2BridgeError(
            "RANS smoke contains forbidden boundary/GPU keys: "
            + ", ".join(present_forbidden)
        )
    return rendered, angle_mapping


def _safe_local_su2_stem(value: object, *, label: str) -> str:
    """Return one unquoted local SU2 filename stem with no path semantics."""

    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{label} must be one local filename stem")
    if _SU2_LOCAL_STEM_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be one local filename stem")
    return value


def render_project_rans_restart_config(
    base_config: str,
    reference: SU2Reference,
    *,
    solution_filename: str,
    iterations: int,
    volume_prefix: str = "rans_second_order",
    surface_prefix: str = "rans_second_order_surface",
    restart_prefix: str = "rans_second_order_restart",
) -> str:
    """Render a bounded, second-order restart from a verified first-order RANS cfg.

    This is deliberately a pure renderer.  It does not discover or execute SU2,
    and it refuses to turn an arbitrary configuration into a restart case.  The
    caller must provide the already validated first-order project configuration;
    only restart, MUSCL, iteration, and output-prefix assignments are replaced.
    """

    if not isinstance(base_config, str) or not base_config.strip():
        raise TypeError("base_config must be a non-empty string")
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise TypeError("iterations must be an integer")
    if not 20 <= iterations <= 500:
        raise ValueError("RANS restart iterations must be between 20 and 500")

    solution = _safe_local_su2_stem(
        solution_filename, label="solution_filename"
    )
    volume = _safe_local_su2_stem(volume_prefix, label="volume_prefix")
    surface = _safe_local_su2_stem(surface_prefix, label="surface_prefix")
    restart = _safe_local_su2_stem(restart_prefix, label="restart_prefix")
    if len({solution, volume, surface, restart}) != 4:
        raise ValueError("RANS restart input and output stems must be distinct")

    supported = set(reference.supported_tokens)
    required_evidence = set(_PROJECT_RANS_RESTART_SYNTAX_TOKENS) | {
        reference.iteration_key,
        "CONV_STARTITER",
        "VOLUME_FILENAME",
        "SURFACE_FILENAME",
        "RESTART_FILENAME",
    }
    missing = sorted(required_evidence - supported)
    if missing:
        raise SU2BridgeError(
            "current SU2 lacks second-order RANS restart syntax evidence for: "
            + ", ".join(missing)
        )

    assignments: dict[str, str] = {}
    assignment_lines: dict[str, int] = {}
    for line_number, line in enumerate(base_config.splitlines(), start=1):
        active = line.split("%", 1)[0]
        if (
            "$(" in active
            or "`" in active
            or "&&" in active
            or "||" in active
            or ">" in active
            or "<" in active
            or re.search(r"(?i)\bshell\s*=", active)
        ):
            raise SU2BridgeError(
                f"base RANS config contains shell-related text on line {line_number}"
            )
        match = _CONFIG_ASSIGNMENT_RE.match(active)
        if match is None:
            continue
        key = match.group(1).upper()
        value = match.group(2).strip()
        if key in assignments:
            raise SU2BridgeError(
                f"base RANS config repeats {key} on lines "
                f"{assignment_lines[key]} and {line_number}"
            )
        assignments[key] = value
        assignment_lines[key] = line_number

    required_base = {
        "SOLVER",
        "KIND_TURB_MODEL",
        "MARKER_SUPERSONIC_OUTLET",
        "MUSCL_FLOW",
        "MUSCL_TURB",
        reference.iteration_key,
        "CONV_STARTITER",
        "MESH_FILENAME",
        "VOLUME_FILENAME",
        "SURFACE_FILENAME",
        "RESTART_FILENAME",
    }
    missing_base = sorted(required_base - set(assignments))
    if missing_base:
        raise SU2BridgeError(
            "base RANS config lacks verified first-order assignments: "
            + ", ".join(missing_base)
        )
    if assignments["SOLVER"].upper() != "RANS":
        raise SU2BridgeError("base RANS config must use SOLVER=RANS")
    if assignments["KIND_TURB_MODEL"].upper() != "SST":
        raise SU2BridgeError("base RANS config must use KIND_TURB_MODEL=SST")
    if assignments["MUSCL_FLOW"].upper() != "NO" or assignments[
        "MUSCL_TURB"
    ].upper() != "NO":
        raise SU2BridgeError("base RANS config must be a first-order MUSCL=NO case")
    if "RESTART_SOL" in assignments and assignments["RESTART_SOL"].upper() != "NO":
        raise SU2BridgeError("base RANS config must not already enable restart")

    marker_pattern = re.compile(
        rf"^\(\s*{_SU2_MARKER_RE.pattern[1:-1]}\s*,\s*"
        rf"{_SU2_MARKER_RE.pattern[1:-1]}\s*\)$"
    )
    if marker_pattern.fullmatch(assignments["MARKER_SUPERSONIC_OUTLET"]) is None:
        raise SU2BridgeError(
            "base RANS config must contain exactly two safe supersonic outlet markers"
        )

    forbidden_keys = {
        "MARKER_OUTLET",
        "OUTLET_PRESSURE",
        "BACK_PRESSURE",
        "MARKER_EULER",
    }
    for key, value in assignments.items():
        if (
            key in forbidden_keys
            or ("OUTLET" in key and "PRESSURE" in key)
            or ("BACK" in key and "PRESSURE" in key)
        ):
            raise SU2BridgeError(
                f"base RANS config contains forbidden outlet pressure key: {key}"
            )
        if "CUDA" in key:
            raise SU2BridgeError(f"base RANS config contains forbidden GPU key: {key}")
        if "SHELL" in key:
            raise SU2BridgeError(
                f"base RANS config contains forbidden shell-related key: {key}"
            )
        if re.search(r"(?i)(?:^|[\s,(])(?:[A-Z]:[\\/]|\\\\|/)", value):
            raise SU2BridgeError(
                f"base RANS config contains an absolute path in {key}"
            )

    for key in (
        "MESH_FILENAME",
        "CONV_FILENAME",
        "VOLUME_FILENAME",
        "SURFACE_FILENAME",
        "RESTART_FILENAME",
        "SOLUTION_FILENAME",
    ):
        if key not in assignments:
            continue
        value = assignments[key]
        if (
            not value
            or value != Path(value).name
            or "/" in value
            or "\\" in value
            or ":" in value
            or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None
        ):
            raise SU2BridgeError(
                f"base RANS config {key} must use one safe local filename"
            )

    replaced = {
        "RESTART_SOL",
        "READ_BINARY_RESTART",
        "SOLUTION_FILENAME",
        "MUSCL_FLOW",
        "MUSCL_TURB",
        reference.iteration_key,
        "CONV_STARTITER",
        "VOLUME_FILENAME",
        "SURFACE_FILENAME",
        "RESTART_FILENAME",
    }
    lines: list[str] = []
    header_written = False
    for line in base_config.splitlines():
        active = line.split("%", 1)[0]
        match = _CONFIG_ASSIGNMENT_RE.match(active)
        if match is not None and match.group(1).upper() in replaced:
            continue
        if re.search(r"(?i)^\s*%.*first-order.*RANS", line):
            if not header_written:
                lines.append(
                    "% CFDPIPE bounded real-project second-order SST RANS restart"
                )
                header_written = True
            continue
        lines.append(line)
    if not header_written:
        lines.insert(0, "% CFDPIPE bounded real-project second-order SST RANS restart")
    lines.extend(
        (
            "% restart input and all outputs are local stems; no pressure/back-pressure",
            "RESTART_SOL= YES",
            "READ_BINARY_RESTART= YES",
            f"SOLUTION_FILENAME= {solution}",
            "MUSCL_FLOW= YES",
            "MUSCL_TURB= YES",
            f"{reference.iteration_key}= {iterations}",
            f"CONV_STARTITER= {iterations + 1}",
            f"VOLUME_FILENAME= {volume}",
            f"SURFACE_FILENAME= {surface}",
            f"RESTART_FILENAME= {restart}",
        )
    )
    rendered = "\n".join(lines) + "\n"
    final_assignments = _config_assignments(rendered)
    if final_assignments.get("MARKER_SUPERSONIC_OUTLET") != assignments[
        "MARKER_SUPERSONIC_OUTLET"
    ]:
        raise SU2BridgeError("second-order restart changed the supersonic outlet markers")
    expected = {
        "RESTART_SOL": "YES",
        "READ_BINARY_RESTART": "YES",
        "SOLUTION_FILENAME": solution,
        "MUSCL_FLOW": "YES",
        "MUSCL_TURB": "YES",
        reference.iteration_key: str(iterations),
        "CONV_STARTITER": str(iterations + 1),
        "VOLUME_FILENAME": volume,
        "SURFACE_FILENAME": surface,
        "RESTART_FILENAME": restart,
    }
    if any(final_assignments.get(key) != value for key, value in expected.items()):
        raise SU2BridgeError("second-order restart replacement contract was not preserved")
    return rendered


def prepare_project_outlet_diagnostic_case(
    su2_cfd: PathValue,
    source_mesh_path: PathValue,
    output_directory: PathValue,
    selected_case: Mapping[str, Any],
    atmosphere: AtmosphereState,
    *,
    reference_area_m2: float,
    reference_length_m: float,
    moment_origin_m: Sequence[float],
    farfield_marker: str,
    wall_marker: str,
    rear_outlet_markers: Sequence[str],
    max_iterations: int,
    mesh_validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Prepare an isolated, bounded outlet diagnostic without starting SU2."""

    source_mesh = Path(source_mesh_path).expanduser().resolve(strict=True)
    if not source_mesh.is_file() or source_mesh.stat().st_size <= 0:
        raise SU2BridgeError(f"outlet diagnostic mesh is missing or empty: {source_mesh}")
    source_sha256 = _sha256(source_mesh)
    if (
        mesh_validation.get("status") != "PASS"
        or mesh_validation.get("ndime") != 3
        or mesh_validation.get("sha256") != source_sha256
    ):
        raise SU2BridgeError("outlet diagnostic requires matching PASS 3-D mesh evidence")
    for field in ("nelem", "npoin"):
        value = mesh_validation.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SU2BridgeError(f"outlet diagnostic mesh has invalid {field}")

    run_dir = Path(output_directory).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    local_mesh = run_dir / "mesh.su2"
    config_path = run_dir / "case.cfg"
    for output in (local_mesh, config_path):
        if output.exists():
            raise SU2BridgeError(f"outlet diagnostic refuses to overwrite: {output}")

    reference = discover_su2_reference(su2_cfd)
    rendered, angle_mapping = render_project_outlet_diagnostic_config(
        reference,
        selected_case,
        atmosphere,
        reference_area_m2=reference_area_m2,
        reference_length_m=reference_length_m,
        moment_origin_m=moment_origin_m,
        farfield_marker=farfield_marker,
        wall_marker=wall_marker,
        rear_outlet_markers=rear_outlet_markers,
        max_iterations=max_iterations,
    )
    shutil.copy2(source_mesh, local_mesh)
    if _sha256(local_mesh) != source_sha256:
        raise SU2BridgeError("outlet diagnostic mesh copy SHA256 mismatch")
    config_path.write_text(rendered, encoding="utf-8")
    return {
        "status": "PASS",
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "mesh_path": str(local_mesh),
        "mesh_sha256": _sha256(local_mesh),
        "source_mesh_path": str(source_mesh),
        "source_mesh_sha256": source_sha256,
        "selected_case": dict(selected_case),
        "atmosphere": atmosphere.as_dict(),
        "angle_mapping": angle_mapping.as_manifest(),
        "reference_quantities": {
            "area_m2": float(reference_area_m2),
            "length_m": float(reference_length_m),
            "moment_origin_m": [float(value) for value in moment_origin_m],
        },
        "markers": {
            "farfield": farfield_marker,
            "wall": wall_marker,
            "rear_outlets": list(rear_outlet_markers),
        },
        "reference_config": reference.as_manifest(),
        "requested_iterations": max_iterations,
        "scope": {
            "purpose": "rear_outlet_diagnostic",
            "mesh_level": "topology_smoke",
            "solver_model": "EULER",
            "nproc": 1,
            "gpu_enabled": False,
            "production_eligible": False,
            "boundary_mode_frozen": False,
            "outlet_pressure_or_backpressure_set": False,
        },
    }


def prepare_project_supersonic_pilot_case(
    su2_cfd: PathValue,
    source_mesh_path: PathValue,
    output_directory: PathValue,
    selected_case: Mapping[str, Any],
    *,
    farfield_marker: str,
    wall_marker: str,
    rear_outlet_markers: Sequence[str],
    max_iterations: int,
    mesh_validation: Mapping[str, Any],
) -> dict[str, Any]:
    """Create ``case.cfg`` and an identical local ``mesh.su2`` without running SU2."""

    source_mesh = Path(source_mesh_path).expanduser().resolve(strict=True)
    if not source_mesh.is_file() or source_mesh.stat().st_size <= 0:
        raise SU2BridgeError(f"project pilot mesh is missing or empty: {source_mesh}")
    if (
        mesh_validation.get("status") != "PASS"
        or mesh_validation.get("ndime") != 3
        or not isinstance(mesh_validation.get("nelem"), int)
        or int(mesh_validation["nelem"]) <= 0
        or not isinstance(mesh_validation.get("npoin"), int)
        or int(mesh_validation["npoin"]) <= 0
    ):
        raise SU2BridgeError("project pilot requires a PASS three-dimensional mesh validation")
    source_sha256 = _sha256(source_mesh)
    declared_sha256 = mesh_validation.get("sha256")
    if not isinstance(declared_sha256, str) or declared_sha256 != source_sha256:
        raise SU2BridgeError("project pilot source mesh hash differs from validation evidence")

    run_dir = Path(output_directory).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    local_mesh = run_dir / "mesh.su2"
    config_path = run_dir / "case.cfg"
    for output in (local_mesh, config_path):
        if output.exists():
            raise SU2BridgeError(f"project pilot refuses to overwrite existing file: {output}")

    reference = discover_su2_reference(su2_cfd)
    shutil.copy2(source_mesh, local_mesh)
    local_sha256 = _sha256(local_mesh)
    if local_sha256 != source_sha256:
        raise SU2BridgeError("project pilot mesh copy SHA256 does not match source")
    rendered = render_project_supersonic_pilot_config(
        reference,
        selected_case,
        farfield_marker=farfield_marker,
        wall_marker=wall_marker,
        rear_outlet_markers=rear_outlet_markers,
        max_iterations=max_iterations,
    )
    config_path.write_text(rendered, encoding="utf-8")
    angle_mapping = project_angles_to_su2(
        _project_case_number(selected_case, "alpha_deg"),
        _project_case_number(selected_case, "beta_deg"),
    )
    return {
        "status": "PASS",
        "config_path": str(config_path),
        "config_sha256": _sha256(config_path),
        "mesh_path": str(local_mesh),
        "mesh_sha256": local_sha256,
        "source_mesh_path": str(source_mesh),
        "source_mesh_sha256": source_sha256,
        "reference_config": reference.as_manifest(),
        "requested_iterations": max_iterations,
        "selected_case": dict(selected_case),
        "angle_mapping": angle_mapping.as_manifest(),
        "boundary_contract": {
            "mode": "PROVISIONAL_SUPERSONIC_PILOT",
            "su2_option": "MARKER_SUPERSONIC_OUTLET",
            "farfield_marker": farfield_marker,
            "wall_marker": wall_marker,
            "rear_outlet_markers": list(rear_outlet_markers),
            "pressure_or_backpressure_guessed": False,
            "boundary_mode_frozen": False,
        },
        "run_scope": {
            "purpose": "connection_only",
            "mesh_level": "topology_smoke",
            "solver_model": "EULER",
            "nproc": 1,
            "gpu_enabled": False,
            "production_eligible": False,
            "physical_interpretation_allowed": False,
        },
    }


def render_su2_sol_config(
    smoke_config: str,
    reference: SU2Reference,
    restart_filename: str,
) -> str:
    """Create a version-evidenced SU2_SOL export config for one local restart."""

    if Path(restart_filename).name != restart_filename or not restart_filename:
        raise ValueError("restart_filename must be a local filename")
    required = {"SOLUTION_FILENAME", "VOLUME_FILENAME", "OUTPUT_FILES"}
    supported = set(reference.supported_tokens)
    missing = sorted(required - supported)
    if missing or reference.paraview_output not in supported:
        raise SU2BridgeError(
            "SU2 reference lacks SU2_SOL export syntax evidence: "
            + ", ".join(missing or [reference.paraview_output])
        )

    replaced = required
    lines: list[str] = []
    for line in smoke_config.splitlines():
        candidate = line.split("%", 1)[0]
        match = _CONFIG_ASSIGNMENT_RE.match(candidate)
        if match is not None and match.group(1).upper() in replaced:
            continue
        lines.append(line)
    lines.extend(
        (
            f"SOLUTION_FILENAME= {restart_filename}",
            f"VOLUME_FILENAME= {SOLUTION_PREFIX}",
            f"OUTPUT_FILES= {reference.paraview_output}",
        )
    )
    rendered = "\n".join(lines) + "\n"
    if re.search(r"(?i)[A-Z]:[\\/]", rendered):
        raise SU2BridgeError("generated SU2_SOL config contains an absolute Windows path")
    return rendered


def classify_2d_quality_nan(
    stdout: str,
    stderr: str,
    mesh_validation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Classify only SU2's evidenced, pre-solver 2D quality statistic NaN."""

    if mesh_validation.get("ndime") != 2:
        return []
    quality = mesh_validation.get("quality")
    if not isinstance(quality, Mapping):
        return []
    required_quality = {
        "status": "PASS",
        "finite": True,
        "negative_signed_area_count": 0,
        "degenerate_triangle_count": 0,
        "duplicate_coordinate_count": 0,
        "nonmanifold_edge_count": 0,
        "boundary_component_count": 1,
    }
    if any(quality.get(key) != value for key, value in required_quality.items()):
        return []
    if quality.get("farfield_edge_count") != quality.get("boundary_edge_count"):
        return []
    if _NONFINITE_RE.search(stderr) is not None or _NON_NAN_FATAL_RE.search(stderr) is not None:
        return []

    lines = stdout.splitlines()
    diagnostic_indices = [
        index
        for index, line in enumerate(lines)
        if _NONFATAL_2D_QUALITY_NAN_RE.fullmatch(line) is not None
    ]
    if len(diagnostic_indices) != 1:
        return []
    diagnostic_index = diagnostic_indices[0]
    quality_heading = next(
        (
            index
            for index, line in enumerate(lines[:diagnostic_index])
            if "Computing mesh quality statistics for the dual control volumes"
            in line
        ),
        None,
    )
    solver_heading = next(
        (
            index
            for index, line in enumerate(lines[diagnostic_index + 1 :], start=diagnostic_index + 1)
            if "Begin Solver" in line
        ),
        None,
    )
    if quality_heading is None or solver_heading is None:
        return []

    return [
        {
            "code": "SU2_2D_DUAL_ORTHOGONALITY_STAT_UNAVAILABLE",
            "classification": "NONFATAL_PREPROCESSING_STATISTIC",
            "source": "stdout",
            "line_number": diagnostic_index + 1,
            "phase": "geometry_preprocessing",
            "raw_line": lines[diagnostic_index],
            "evidence": {
                "mesh_quality_status": quality.get("status"),
                "triangle_count": quality.get("triangle_count"),
                "minimum_area_m2": quality.get("minimum_area_m2"),
                "minimum_normalized_quality": quality.get(
                    "minimum_normalized_quality"
                ),
                "negative_signed_area_count": quality.get(
                    "negative_signed_area_count"
                ),
                "degenerate_triangle_count": quality.get(
                    "degenerate_triangle_count"
                ),
                "nonmanifold_edge_count": quality.get("nonmanifold_edge_count"),
                "farfield_covers_boundary": True,
            },
        }
    ]


def _allowed_diagnostic_keys(
    diagnostics: Sequence[Mapping[str, Any]],
) -> set[tuple[str, int, str]]:
    return {
        (
            str(diagnostic.get("source")),
            int(diagnostic.get("line_number", -1)),
            str(diagnostic.get("raw_line")),
        )
        for diagnostic in diagnostics
        if diagnostic.get("code")
        == "SU2_2D_DUAL_ORTHOGONALITY_STAT_UNAVAILABLE"
    }


def scan_fatal_output(
    stdout: str,
    stderr: str,
    *,
    allowed_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Return unique fatal-looking log fragments, preserving their text."""

    allowed = _allowed_diagnostic_keys(allowed_diagnostics)
    matches: list[str] = []
    for source_name, text in (("stdout", stdout), ("stderr", stderr)):
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _FATAL_RE.search(line) is not None:
                if (
                    (source_name, line_number, line) in allowed
                    and _NON_NAN_FATAL_RE.search(line) is None
                ):
                    continue
                entry = f"{source_name}:{line_number}: {line}"
                if entry not in matches:
                    matches.append(entry)
    return matches


def scan_nonfatal_diagnostics(
    stdout: str,
    stderr: str,
    *,
    allowed_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Record known nonfatal diagnostics without silently discarding them."""

    allowed = _allowed_diagnostic_keys(allowed_diagnostics)
    diagnostics: list[str] = []
    for source_name, text in (("stdout", stdout), ("stderr", stderr)):
        for line_number, line in enumerate(text.splitlines(), start=1):
            if (
                (source_name, line_number, line) in allowed
                or _WARNING_RE.search(line) is not None
            ):
                diagnostics.append(f"{source_name}:{line_number}: {line}")
    return diagnostics


def _file_signature(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, _sha256(path)


def snapshot_visualizations(run_directory: PathValue) -> dict[str, tuple[int, int, str]]:
    root = Path(run_directory).expanduser().resolve()
    result: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return result
    for path in root.iterdir():
        if path.is_file() and path.suffix.lower() in VISUALIZATION_EXTENSIONS:
            resolved = path.resolve()
            if _is_within(resolved, root):
                result[str(resolved)] = _file_signature(resolved)
    return result


def select_new_visualization(
    run_directory: PathValue,
    before: Mapping[str, tuple[int, int, str]],
    *,
    solution_prefix: str = SOLUTION_PREFIX,
) -> Path | None:
    """Select only a visualization created or changed by the current run."""

    root = Path(run_directory).expanduser().resolve()
    if (
        not isinstance(solution_prefix, str)
        or not solution_prefix
        or Path(solution_prefix).name != solution_prefix
    ):
        raise ValueError("solution_prefix must be one safe local filename prefix")
    normalized_prefix = solution_prefix.casefold()
    candidates: list[Path] = []
    for path in root.iterdir():
        if not path.is_file() or path.suffix.lower() not in VISUALIZATION_EXTENSIONS:
            continue
        resolved = path.resolve()
        if not _is_within(resolved, root):
            continue
        if not path.name.casefold().startswith(normalized_prefix) or path.stat().st_size <= 0:
            continue
        if before.get(str(resolved)) != _file_signature(resolved):
            candidates.append(resolved)
    if not candidates:
        return None
    extension_rank = {".pvtu": 0, ".vtm": 1, ".vtu": 2, ".vtk": 3}
    candidates.sort(
        key=lambda path: (
            0 if path.stem.casefold() == normalized_prefix else 1,
            extension_rank[path.suffix.lower()],
            str(path).lower(),
        )
    )
    return candidates[0]


def _xml_attributes(payload: bytes) -> dict[str, str]:
    return {
        match.group(1).decode("ascii"): match.group(2).decode("utf-8", "replace")
        for match in _XML_ATTRIBUTE_RE.finditer(payload)
    }


def _validate_vtu_ascii(
    path: Path,
    *,
    expected_points: int | None,
    expected_cells: int | None,
) -> dict[str, Any]:
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as error:
        raise SU2BridgeError(f"invalid VTU XML: {path}") from error
    piece = next(
        (
            element
            for element in root.iter()
            if str(element.tag).rsplit("}", 1)[-1] == "Piece"
        ),
        None,
    )
    if piece is None:
        raise SU2BridgeError(f"VTU has no Piece element: {path}")
    try:
        point_count = int(piece.attrib["NumberOfPoints"])
        cell_count = int(piece.attrib["NumberOfCells"])
    except (KeyError, ValueError) as error:
        raise SU2BridgeError(f"VTU Piece counts are invalid: {path}") from error
    if expected_points is not None and point_count != expected_points:
        raise SU2BridgeError(
            f"VTU point count {point_count} does not match mesh {expected_points}"
        )
    if expected_cells is not None and cell_count != expected_cells:
        raise SU2BridgeError(
            f"VTU cell count {cell_count} does not match mesh {expected_cells}"
        )

    float_arrays: list[str] = []
    float_value_count = 0
    for element in root.iter():
        if str(element.tag).rsplit("}", 1)[-1] != "DataArray":
            continue
        value_type = element.attrib.get("type", "")
        if value_type not in {"Float32", "Float64"}:
            continue
        if element.attrib.get("format", "ascii").lower() != "ascii":
            raise SU2BridgeError(f"unexpected non-ASCII DataArray in XML VTU: {path}")
        values = (element.text or "").split()
        for value in values:
            try:
                numeric = float(value)
            except ValueError as error:
                raise SU2BridgeError(
                    f"VTU float array contains invalid value {value!r}: {path}"
                ) from error
            if not math.isfinite(numeric):
                raise SU2BridgeError(f"VTU contains NaN or Inf: {path}")
        float_arrays.append(element.attrib.get("Name", "Points") or "Points")
        float_value_count += len(values)
    if not float_arrays or float_value_count <= 0:
        raise SU2BridgeError(f"VTU contains no finite floating-point data: {path}")
    return {
        "format": "VTU_ASCII",
        "point_count": point_count,
        "cell_count": cell_count,
        "float_arrays": float_arrays,
        "float_value_count": float_value_count,
        "finite": True,
    }


def _validate_vtu_appended_raw(
    path: Path,
    content: bytes,
    *,
    expected_points: int | None,
    expected_cells: int | None,
) -> dict[str, Any]:
    vtk_tag = re.search(rb"<VTKFile\b([^>]*)>", content, re.IGNORECASE)
    piece_tag = re.search(rb"<Piece\b([^>]*)>", content, re.IGNORECASE)
    appended_tag = re.search(rb"<AppendedData\b([^>]*)>", content, re.IGNORECASE)
    if vtk_tag is None or piece_tag is None or appended_tag is None:
        raise SU2BridgeError(f"VTU appended structure is incomplete: {path}")
    vtk_attributes = _xml_attributes(vtk_tag.group(1))
    piece_attributes = _xml_attributes(piece_tag.group(1))
    appended_attributes = _xml_attributes(appended_tag.group(1))
    if vtk_attributes.get("byte_order") != "LittleEndian":
        raise SU2BridgeError(f"unsupported VTU byte order: {path}")
    if "compressor" in vtk_attributes:
        raise SU2BridgeError(f"compressed VTU validation is unsupported: {path}")
    if appended_attributes.get("encoding", "").lower() != "raw":
        raise SU2BridgeError(f"unsupported VTU appended encoding: {path}")
    header_type = vtk_attributes.get("header_type", "UInt32")
    header_format = {"UInt32": "<I", "UInt64": "<Q"}.get(header_type)
    if header_format is None:
        raise SU2BridgeError(f"unsupported VTU header type {header_type!r}: {path}")
    try:
        point_count = int(piece_attributes["NumberOfPoints"])
        cell_count = int(piece_attributes["NumberOfCells"])
    except (KeyError, ValueError) as error:
        raise SU2BridgeError(f"VTU Piece counts are invalid: {path}") from error
    if expected_points is not None and point_count != expected_points:
        raise SU2BridgeError(
            f"VTU point count {point_count} does not match mesh {expected_points}"
        )
    if expected_cells is not None and cell_count != expected_cells:
        raise SU2BridgeError(
            f"VTU cell count {cell_count} does not match mesh {expected_cells}"
        )

    marker = content.find(b"_", appended_tag.end())
    if marker < 0:
        raise SU2BridgeError(f"VTU appended data marker is missing: {path}")
    data_start = marker + 1
    header_size = struct.calcsize(header_format)
    float_arrays: list[str] = []
    float_value_count = 0
    for tag_match in _DATA_ARRAY_TAG_RE.finditer(content[: appended_tag.start()]):
        attributes = _xml_attributes(tag_match.group(1))
        if attributes.get("format", "").lower() != "appended":
            continue
        value_type = attributes.get("type", "")
        if value_type not in {"Float32", "Float64"}:
            continue
        try:
            offset = int(attributes["offset"])
        except (KeyError, ValueError) as error:
            raise SU2BridgeError(f"VTU DataArray offset is invalid: {path}") from error
        block_start = data_start + offset
        if block_start < data_start or block_start + header_size > len(content):
            raise SU2BridgeError(f"VTU DataArray offset is out of bounds: {path}")
        byte_count = struct.unpack_from(header_format, content, block_start)[0]
        payload_start = block_start + header_size
        payload_end = payload_start + byte_count
        scalar_format = "<f" if value_type == "Float32" else "<d"
        scalar_size = struct.calcsize(scalar_format)
        if payload_end > len(content) or byte_count % scalar_size:
            raise SU2BridgeError(f"VTU DataArray payload is invalid: {path}")
        values = content[payload_start:payload_end]
        for (numeric,) in struct.iter_unpack(scalar_format, values):
            if not math.isfinite(numeric):
                raise SU2BridgeError(f"VTU contains NaN or Inf: {path}")
        float_arrays.append(attributes.get("Name", "Points") or "Points")
        float_value_count += byte_count // scalar_size
    if not float_arrays or float_value_count <= 0:
        raise SU2BridgeError(f"VTU contains no finite floating-point data: {path}")
    return {
        "format": "VTU_APPENDED_RAW",
        "point_count": point_count,
        "cell_count": cell_count,
        "float_arrays": float_arrays,
        "float_value_count": float_value_count,
        "finite": True,
    }


def validate_visualization_file(
    path: PathValue,
    *,
    expected_points: int | None = None,
    expected_cells: int | None = None,
) -> dict[str, Any]:
    """Verify the selected solution is nonempty and contains no float NaN/Inf."""

    visualization = Path(path).expanduser().resolve(strict=True)
    if not visualization.is_file() or visualization.stat().st_size <= 0:
        raise SU2BridgeError(f"visualization file is empty: {visualization}")
    content = visualization.read_bytes()
    if visualization.suffix.lower() == ".vtu":
        if re.search(rb"<AppendedData\b", content, re.IGNORECASE) is not None:
            validation = _validate_vtu_appended_raw(
                visualization,
                content,
                expected_points=expected_points,
                expected_cells=expected_cells,
            )
        else:
            validation = _validate_vtu_ascii(
                visualization,
                expected_points=expected_points,
                expected_cells=expected_cells,
            )
    else:
        decoded = content.decode("latin-1")
        if _NONFINITE_RE.search(decoded) is not None:
            raise SU2BridgeError(
                f"visualization descriptor contains NaN or Inf: {visualization}"
            )
        validation = {
            "format": visualization.suffix.lower().lstrip(".").upper(),
            "point_count": None,
            "cell_count": None,
            "float_arrays": [],
            "float_value_count": None,
            "finite": True,
            "method": "nonfinite_token_scan",
        }
    return {
        "path": str(visualization),
        "size_bytes": visualization.stat().st_size,
        "sha256": _sha256(visualization),
        **validation,
    }


def snapshot_restarts(run_directory: PathValue) -> dict[str, tuple[int, int, str]]:
    """Snapshot restart files directly inside the selected run directory."""

    root = Path(run_directory).expanduser().resolve()
    result: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return result
    for path in root.iterdir():
        if path.is_file() and path.name.lower().startswith(RESTART_PREFIX):
            resolved = path.resolve()
            result[str(resolved)] = _file_signature(resolved)
    return result


def select_new_restart(
    run_directory: PathValue,
    before: Mapping[str, tuple[int, int, str]],
) -> Path | None:
    """Return a nonempty connection restart created or changed by this run."""

    root = Path(run_directory).expanduser().resolve()
    candidates: list[Path] = []
    for path in root.iterdir():
        if (
            not path.is_file()
            or not path.name.lower().startswith(RESTART_PREFIX)
            or path.stat().st_size <= 0
        ):
            continue
        resolved = path.resolve()
        if before.get(str(resolved)) != _file_signature(resolved):
            candidates.append(resolved)
    candidates.sort(
        key=lambda path: (
            0 if path.name.lower() == f"{RESTART_PREFIX}.dat" else 1,
            str(path).lower(),
        )
    )
    return None if not candidates else candidates[0]


def _changed_history_files(
    run_directory: Path,
    before: Mapping[str, tuple[int, int, str]],
) -> list[Path]:
    candidates: list[Path] = []
    for path in run_directory.glob("*.csv"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not _is_within(resolved, run_directory):
            continue
        if before.get(str(resolved)) != _file_signature(resolved):
            candidates.append(resolved)
    candidates.sort(
        key=lambda path: (
            0 if path.name.lower() == "history.csv" else 1,
            0 if path.stem.lower().startswith("history") else 1,
            str(path).lower(),
        )
    )
    return candidates


def snapshot_history(run_directory: PathValue) -> dict[str, tuple[int, int, str]]:
    root = Path(run_directory).expanduser().resolve()
    result: dict[str, tuple[int, int, str]] = {}
    if not root.exists():
        return result
    for path in root.glob("*.csv"):
        if path.is_file():
            resolved = path.resolve()
            if _is_within(resolved, root):
                result[str(resolved)] = _file_signature(resolved)
    return result


def parse_history_iterations(path: PathValue) -> int:
    """Count distinct, parseable SU2 ``Inner_Iter`` values."""

    history_path = Path(path).expanduser().resolve(strict=True)
    content = history_path.read_text(encoding="utf-8-sig")
    if _NONFINITE_RE.search(content) is not None:
        raise SU2BridgeError(f"history CSV contains NaN or Inf: {history_path}")
    reader = csv.DictReader(line for line in content.splitlines() if line.strip())
    fieldnames = reader.fieldnames or []
    iteration_field = next(
        (name for name in fieldnames if name.strip().lower() == "inner_iter"),
        None,
    )
    if iteration_field is None:
        raise SU2BridgeError(
            f"history CSV lacks a parseable Inner_Iter column: {history_path}"
        )
    iterations: set[int] = set()
    for row_number, row in enumerate(reader, start=2):
        raw_value = (row.get(iteration_field) or "").strip()
        try:
            numeric = float(raw_value)
        except ValueError as error:
            raise SU2BridgeError(
                f"history CSV row {row_number} has invalid Inner_Iter: {raw_value!r}"
            ) from error
        if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
            raise SU2BridgeError(
                f"history CSV row {row_number} has invalid Inner_Iter: {raw_value!r}"
            )
        iterations.add(int(numeric))
    if not iterations:
        raise SU2BridgeError(f"history CSV has no iteration rows: {history_path}")
    return len(iterations)


def parse_su2_freestream_log(output: str) -> dict[str, Any]:
    """Parse the dimensional freestream table printed by SU2_CFD."""

    number = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)"

    def value_for(label: str) -> tuple[float, str]:
        pattern = re.compile(
            rf"(?mi)^\|\s*{re.escape(label)}\s*\|\s*{number}\s*\|.*$"
        )
        match = pattern.search(output)
        if match is None:
            raise SU2BridgeError(f"SU2 log lacks freestream row {label!r}")
        value = float(match.group(1))
        if not math.isfinite(value):
            raise SU2BridgeError(f"SU2 log has non-finite freestream {label}")
        return value, match.group(0).strip()

    pressure, pressure_line = value_for("Static Pressure")
    temperature, temperature_line = value_for("Temperature")
    velocity_x, velocity_x_line = value_for("Velocity-X")
    velocity_y, velocity_y_line = value_for("Velocity-Y")
    velocity_z, velocity_z_line = value_for("Velocity-Z")
    magnitude, magnitude_line = value_for("Velocity Magnitude")
    if magnitude <= 0.0:
        raise SU2BridgeError("SU2 log has non-positive freestream velocity magnitude")
    vector = (velocity_x, velocity_y, velocity_z)
    computed_magnitude = math.sqrt(sum(value * value for value in vector))
    if abs(computed_magnitude - magnitude) > 2.0e-5 * magnitude:
        raise SU2BridgeError("SU2 freestream component magnitude is inconsistent")
    return {
        "static_pressure_pa": pressure,
        "static_temperature_k": temperature,
        "velocity_m_s": list(vector),
        "velocity_magnitude_m_s": magnitude,
        "unit_vector_xyz": [value / magnitude for value in vector],
        "log_lines": [
            pressure_line,
            temperature_line,
            velocity_x_line,
            velocity_y_line,
            velocity_z_line,
            magnitude_line,
        ],
    }


class SU2Bridge:
    """Build SU2 commands and run a version-evidenced connection case."""

    def __init__(
        self,
        runner: Any,
        su2_cfd: PathValue,
        mpiexec: PathValue | None = None,
        su2_sol: PathValue | None = None,
    ) -> None:
        self.runner = runner
        self.su2_cfd = os.fspath(su2_cfd)
        self.mpiexec = None if mpiexec is None else os.fspath(mpiexec)
        self.su2_sol = None if su2_sol is None else os.fspath(su2_sol)

    @staticmethod
    def _validate_nproc(nproc: int) -> None:
        if isinstance(nproc, bool) or not isinstance(nproc, int):
            raise TypeError("nproc must be an integer")
        if nproc <= 0:
            raise ValueError("nproc must be greater than zero")

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> float:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(
            timeout_seconds, bool
        ):
            raise TypeError("timeout_seconds must be a real number")
        timeout_value = float(timeout_seconds)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        return timeout_value

    def _command_for_args(
        self,
        su2_args: Sequence[str],
        nproc: int,
    ) -> tuple[str, list[str]]:
        self._validate_nproc(nproc)
        arguments = [str(argument) for argument in su2_args]
        if nproc == 1:
            return self.su2_cfd, arguments
        if self.mpiexec is None:
            raise ValueError("mpirun or mpiexec is required when nproc is greater than one")
        return self.mpiexec, ["-np", str(nproc), self.su2_cfd, *arguments]

    def build_command(
        self,
        config_path: PathValue,
        nproc: int = 1,
    ) -> tuple[str, list[str]]:
        """Build the serial or MPI command without executing it."""

        return self._command_for_args([os.fspath(config_path)], nproc)

    def run(
        self,
        config_path: PathValue,
        *,
        nproc: int = 1,
        cwd: PathValue | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        live_output: bool | None = None,
        dry_run: bool = False,
        name: str | None = "su2-run",
    ) -> Any:
        """Compatibility wrapper for one explicitly supplied SU2 config."""

        executable, arguments = self.build_command(config_path, nproc=nproc)
        return self.runner.run(
            executable,
            args=arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            live_output=live_output,
            dry_run=dry_run,
            name=name,
        )

    def smoke(
        self,
        *,
        nproc: int = 1,
        cwd: PathValue | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        check: bool = True,
        live_output: bool | None = None,
        dry_run: bool = False,
        name: str | None = "su2-smoke-probe",
    ) -> Any:
        """Compatibility-only help probe; the public CLI uses a real case."""

        executable, arguments = self._command_for_args(["--help"], nproc)
        return self.runner.run(
            executable,
            args=arguments,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=check,
            live_output=live_output,
            dry_run=dry_run,
            name=name,
        )

    def probe_version(
        self,
        run_directory: Path,
        *,
        timeout_seconds: float,
    ) -> tuple[str, CommandResult]:
        timeout_value = self._validate_timeout(timeout_seconds)
        result = self.runner.run(
            self.su2_cfd,
            args=["--help"],
            cwd=run_directory,
            timeout=timeout_value,
            check=True,
            live_output=False,
            dry_run=False,
            name="su2-version",
            stdout_log=run_directory / "su2_version.stdout.log",
            stderr_log=run_directory / "su2_version.stderr.log",
            metadata_log=run_directory / "su2_version.command.json",
        )
        match = _VERSION_RE.search(result.stdout + "\n" + result.stderr)
        if match is None:
            raise SU2BridgeError("could not parse SU2 version from --help output")
        return match.group("version"), result

    def run_su2(
        self,
        config_path: PathValue,
        run_directory: PathValue,
        nproc: int = 1,
        timeout_seconds: float = 300,
        *,
        live_output: bool = True,
    ) -> CommandResult:
        """Run SU2_CFD with a relative config name and fixed live log paths."""

        self._validate_nproc(nproc)
        timeout_value = self._validate_timeout(timeout_seconds)
        run_dir = Path(run_directory).expanduser().resolve(strict=True)
        config = Path(config_path).expanduser().resolve(strict=True)
        if not config.is_file() or config.parent != run_dir:
            raise ValueError("config_path must be a file directly inside run_directory")
        executable, arguments = self.build_command(config.name, nproc=nproc)
        return self.runner.run(
            executable,
            args=arguments,
            cwd=run_dir,
            timeout=timeout_value,
            check=False,
            live_output=live_output,
            dry_run=False,
            name="su2-cfd-smoke",
            stdout_log=run_dir / "solver.stdout.log",
            stderr_log=run_dir / "solver.stderr.log",
            metadata_log=run_dir / "solver.command.json",
        )

    def _run_su2_sol(
        self,
        config_path: Path,
        restart_path: Path,
        run_directory: Path,
        timeout_seconds: float,
        *,
        live_output: bool,
        reason: str,
    ) -> CommandResult:
        if self.su2_sol is None:
            raise SU2BridgeError(
                "SU2_CFD generated no ParaView file and SU2_SOL is unavailable"
            )
        if reason != "visualization_fallback":
            raise SU2BridgeError("SU2_SOL is restricted to visualization fallback")
        run_dir = run_directory.expanduser().resolve(strict=True)
        config = config_path.expanduser().resolve(strict=True)
        restart = restart_path.expanduser().resolve(strict=True)
        if (
            not config.is_file()
            or config.parent != run_dir
            or config.name != "su2_sol.cfg"
        ):
            raise SU2BridgeError(
                "SU2_SOL config must be run_directory/su2_sol.cfg"
            )
        if (
            not restart.is_file()
            or restart.parent != run_dir
            or not restart.name.lower().startswith(RESTART_PREFIX)
            or restart.stat().st_size <= 0
        ):
            raise SU2BridgeError(
                "SU2_SOL requires a nonempty connection restart in run_directory"
            )
        return self.runner.run(
            self.su2_sol,
            args=[config.name],
            cwd=run_dir,
            timeout=timeout_seconds,
            check=False,
            live_output=live_output,
            dry_run=False,
            name="su2-sol-smoke",
            stdout_log=run_directory / "su2_sol.stdout.log",
            stderr_log=run_directory / "su2_sol.stderr.log",
            metadata_log=run_directory / "su2_sol.command.json",
        )

    @staticmethod
    def _apply_result(manifest: dict[str, Any], result: CommandResult) -> None:
        manifest.update(
            {
                "arguments": list(result.args),
                "command": list(result.command),
                "cwd": result.cwd,
                "return_code": result.returncode,
                "start_time": result.start_time,
                "end_time": result.end_time,
                "solver_stdout_log": str(result.stdout_log),
                "solver_stderr_log": str(result.stderr_log),
                "solver_command_log": str(result.metadata_log),
                "timed_out": result.timed_out,
            }
        )

    def run_project_pilot_case(
        self,
        config_path: PathValue,
        mesh_path: PathValue,
        run_directory: PathValue,
        mesh_validation: Mapping[str, Any],
        *,
        max_iterations: int,
        timeout_seconds: float = 300,
        live_output: bool = True,
    ) -> dict[str, Any]:
        """Run and validate one prepared 3-D connection-only project pilot."""

        timeout_value = self._validate_timeout(timeout_seconds)
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise TypeError("max_iterations must be an integer")
        if not 1 <= max_iterations <= 5:
            raise ValueError("project pilot max_iterations must be between 1 and 5")
        run_dir = Path(run_directory).expanduser().resolve(strict=True)
        config = Path(config_path).expanduser().resolve(strict=True)
        mesh = Path(mesh_path).expanduser().resolve(strict=True)
        if config.parent != run_dir or config.name != "case.cfg" or not config.is_file():
            raise SU2BridgeError("project pilot config must be run_directory/case.cfg")
        if mesh.parent != run_dir or mesh.name != "mesh.su2" or not mesh.is_file():
            raise SU2BridgeError("project pilot mesh must be run_directory/mesh.su2")
        if (
            mesh_validation.get("status") != "PASS"
            or mesh_validation.get("ndime") != 3
            or mesh_validation.get("sha256") != _sha256(mesh)
        ):
            raise SU2BridgeError("project pilot mesh differs from PASS validation evidence")
        expected_points = mesh_validation.get("npoin")
        expected_cells = mesh_validation.get("nelem")
        if (
            isinstance(expected_points, bool)
            or not isinstance(expected_points, int)
            or expected_points <= 0
            or isinstance(expected_cells, bool)
            or not isinstance(expected_cells, int)
            or expected_cells <= 0
        ):
            raise SU2BridgeError("project pilot mesh validation has invalid counts")

        config_content = config.read_text(encoding="utf-8")
        assignments = _config_assignments(config_content)
        required_assignments = {
            "SOLVER",
            "MACH_NUMBER",
            "AOA",
            "SIDESLIP_ANGLE",
            "MARKER_FAR",
            "MARKER_EULER",
            "MARKER_SUPERSONIC_OUTLET",
            "MESH_FILENAME",
            "CONV_FILENAME",
            "VOLUME_FILENAME",
            "OUTPUT_FILES",
        }
        missing_assignments = sorted(required_assignments - set(assignments))
        if missing_assignments:
            raise SU2BridgeError(
                "prepared project pilot config lacks assignments: "
                + ", ".join(missing_assignments)
            )
        if assignments["SOLVER"].strip().upper() != "EULER":
            raise SU2BridgeError("project pilot solver must be EULER")
        if assignments["MESH_FILENAME"].strip() != "mesh.su2":
            raise SU2BridgeError("project pilot MESH_FILENAME must be relative mesh.su2")
        forbidden = {
            "MARKER_OUTLET",
            "FREESTREAM_PRESSURE",
            "OUTLET_PRESSURE",
            "BACK_PRESSURE",
        }
        if forbidden & set(assignments):
            raise SU2BridgeError("project pilot config contains a pressure outlet key")

        manifest_path = run_dir / "run_manifest.json"
        overall_started = _utc_now()
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "connection_only": True,
            "production_eligible": False,
            "physical_interpretation_allowed": False,
            "boundary_mode_frozen": False,
            "boundary_validation_status": "NOT_EVALUATED_BY_CONNECTION_PILOT",
            "pressure_or_backpressure_guessed": False,
            "su2_cfd_path": str(Path(self.su2_cfd).expanduser().resolve()),
            "su2_version": None,
            "su2_version_evidence": None,
            "reference_config": None,
            "arguments": [],
            "command": [],
            "cwd": str(run_dir),
            "config_path": str(config),
            "config_sha256": _sha256(config),
            "mesh_path": str(mesh),
            "mesh_sha256": _sha256(mesh),
            "mesh_validation": dict(mesh_validation),
            "return_code": None,
            "requested_iterations": max_iterations,
            "iterations": 0,
            "history_path": None,
            "history_sha256": None,
            "visualization_file": None,
            "visualization_validation": None,
            "solver_stdout_log": None,
            "solver_stderr_log": None,
            "solver_command_log": None,
            "fatal_matches": [],
            "nonfatal_diagnostics": [],
            "timed_out": False,
            "start_time": overall_started,
            "end_time": None,
            "error": None,
        }
        result: CommandResult | None = None
        failure_stderr: str | None = None
        try:
            reference = discover_su2_reference(self.su2_cfd)
            manifest["reference_config"] = reference.as_manifest()
            for required_token in _PROJECT_PILOT_SYNTAX_TOKENS:
                if required_token not in reference.supported_tokens:
                    raise SU2BridgeError(
                        f"current SU2 has no evidence for {required_token}"
                    )
            try:
                version, version_result = self.probe_version(
                    run_dir, timeout_seconds=timeout_value
                )
            except CommandExecutionError as version_error:
                manifest["su2_version_evidence"] = version_error.result.as_metadata()
                failure_stderr = version_error.result.stderr
                raise SU2BridgeError(
                    "SU2 version probe failed; stderr:\n" + version_error.result.stderr
                ) from version_error
            manifest["su2_version"] = version
            manifest["su2_version_evidence"] = version_result.as_metadata()

            visualization_before = snapshot_visualizations(run_dir)
            history_before = snapshot_history(run_dir)
            result = self.run_su2(
                config,
                run_dir,
                nproc=1,
                timeout_seconds=timeout_value,
                live_output=live_output,
            )
            self._apply_result(manifest, result)
            if result.returncode != 0:
                failure_stderr = result.stderr
                raise SU2BridgeError(
                    f"SU2_CFD returned {result.returncode}; stderr:\n{result.stderr}"
                )
            fatal_matches = scan_fatal_output(result.stdout, result.stderr)
            manifest["fatal_matches"] = fatal_matches
            manifest["nonfatal_diagnostics"] = scan_nonfatal_diagnostics(
                result.stdout, result.stderr
            )
            if fatal_matches:
                failure_stderr = result.stderr
                raise SU2BridgeError(
                    "SU2_CFD logs contain fatal text: " + " | ".join(fatal_matches)
                )

            history_candidates = _changed_history_files(run_dir, history_before)
            history_path = next(
                (path for path in history_candidates if path.name.casefold() == "history.csv"),
                None,
            )
            if history_path is None:
                raise SU2BridgeError("SU2_CFD generated no new history.csv")
            iterations = parse_history_iterations(history_path)
            manifest["history_path"] = str(history_path)
            manifest["history_sha256"] = _sha256(history_path)
            manifest["iterations"] = iterations
            if iterations != max_iterations:
                raise SU2BridgeError(
                    f"SU2 history contains {iterations} iterations, expected exactly "
                    f"{max_iterations}"
                )

            visualization = select_new_visualization(run_dir, visualization_before)
            if visualization is None:
                raise SU2BridgeError(
                    "SU2_CFD generated no new connection_solution ParaView file"
                )
            validation = validate_visualization_file(
                visualization,
                expected_points=expected_points,
                expected_cells=expected_cells,
            )
            manifest["visualization_file"] = str(visualization)
            manifest["visualization_validation"] = validation
            manifest["status"] = "PASS"
            manifest["end_time"] = _utc_now()
            _write_json(manifest_path, manifest)
            return manifest
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, CommandExecutionError):
                result = error.result
            if result is not None:
                self._apply_result(manifest, result)
            manifest["status"] = "FAIL"
            manifest["end_time"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                "stderr": failure_stderr
                if failure_stderr is not None
                else (None if result is None else result.stderr),
            }
            _write_json(manifest_path, manifest)
            if isinstance(error, SU2BridgeError):
                raise
            raise SU2BridgeError(str(error)) from error

    def run_project_outlet_diagnostic_case(
        self,
        preparation: Mapping[str, Any],
        mesh_validation: Mapping[str, Any],
        *,
        max_iterations: int,
        timeout_seconds: float = 300,
        live_output: bool = True,
    ) -> dict[str, Any]:
        """Run one isolated serial Euler outlet diagnostic and audit its state."""

        timeout_value = self._validate_timeout(timeout_seconds)
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise TypeError("max_iterations must be an integer")
        if not 20 <= max_iterations <= 500:
            raise ValueError("outlet diagnostic max_iterations must be between 20 and 500")
        if preparation.get("status") != "PASS":
            raise SU2BridgeError("outlet diagnostic preparation is not PASS")
        raw_config = preparation.get("config_path")
        raw_mesh = preparation.get("mesh_path")
        if not isinstance(raw_config, str) or not isinstance(raw_mesh, str):
            raise SU2BridgeError("outlet diagnostic preparation lacks local paths")
        config = Path(raw_config).expanduser().resolve(strict=True)
        mesh = Path(raw_mesh).expanduser().resolve(strict=True)
        run_dir = config.parent
        if config.name != "case.cfg" or mesh != run_dir / "mesh.su2":
            raise SU2BridgeError("outlet diagnostic requires local case.cfg and mesh.su2")
        if preparation.get("config_sha256") != _sha256(config):
            raise SU2BridgeError("outlet diagnostic config hash changed after preparation")
        if preparation.get("mesh_sha256") != _sha256(mesh):
            raise SU2BridgeError("outlet diagnostic mesh hash changed after preparation")
        if (
            mesh_validation.get("status") != "PASS"
            or mesh_validation.get("ndime") != 3
            or mesh_validation.get("sha256") != _sha256(mesh)
        ):
            raise SU2BridgeError("outlet diagnostic mesh differs from PASS validation")
        expected_points = mesh_validation.get("npoin")
        expected_cells = mesh_validation.get("nelem")
        if (
            isinstance(expected_points, bool)
            or not isinstance(expected_points, int)
            or expected_points <= 0
            or isinstance(expected_cells, bool)
            or not isinstance(expected_cells, int)
            or expected_cells <= 0
        ):
            raise SU2BridgeError("outlet diagnostic mesh validation has invalid counts")

        assignments = _config_assignments(config.read_text(encoding="utf-8"))
        required_assignments = {
            "SOLVER",
            "MACH_NUMBER",
            "AOA",
            "SIDESLIP_ANGLE",
            "FREESTREAM_PRESSURE",
            "FREESTREAM_TEMPERATURE",
            "REF_AREA",
            "REF_LENGTH",
            "REF_ORIGIN_MOMENT_X",
            "REF_ORIGIN_MOMENT_Y",
            "REF_ORIGIN_MOMENT_Z",
            "MARKER_FAR",
            "MARKER_EULER",
            "MARKER_SUPERSONIC_OUTLET",
            "MESH_FILENAME",
            "CONV_FILENAME",
            "VOLUME_FILENAME",
            "OUTPUT_FILES",
        }
        missing = sorted(required_assignments - set(assignments))
        if missing:
            raise SU2BridgeError(
                "prepared outlet diagnostic config lacks assignments: " + ", ".join(missing)
            )
        if assignments["SOLVER"].strip().upper() != "EULER":
            raise SU2BridgeError("outlet diagnostic solver must be EULER")
        if assignments["MESH_FILENAME"].strip() != "mesh.su2":
            raise SU2BridgeError("outlet diagnostic mesh filename must be relative")
        if {"MARKER_OUTLET", "OUTLET_PRESSURE", "BACK_PRESSURE"} & set(assignments):
            raise SU2BridgeError("outlet diagnostic config contains an outlet pressure key")

        raw_mapping = preparation.get("angle_mapping")
        raw_atmosphere = preparation.get("atmosphere")
        if not isinstance(raw_mapping, Mapping) or not isinstance(raw_atmosphere, Mapping):
            raise SU2BridgeError("outlet diagnostic preparation lacks angle/atmosphere evidence")
        expected_unit_raw = raw_mapping.get("project_unit_vector_xyz")
        if (
            not isinstance(expected_unit_raw, Sequence)
            or isinstance(expected_unit_raw, (str, bytes))
            or len(expected_unit_raw) != 3
        ):
            raise SU2BridgeError("outlet diagnostic angle evidence has no unit vector")
        expected_unit = tuple(float(value) for value in expected_unit_raw)
        expected_pressure = float(raw_atmosphere["static_pressure_pa"])
        expected_temperature = float(raw_atmosphere["static_temperature_k"])
        expected_sound_speed = float(raw_atmosphere["speed_of_sound_m_s"])
        expected_mach = float(assignments["MACH_NUMBER"])

        manifest_path = run_dir / "run_manifest.json"
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "diagnostic": "rear_outlet_normal_mach_backflow_mass_balance",
            "connection_only": False,
            "production_eligible": False,
            "physical_interpretation_scope": "bounded_topology_smoke_euler_diagnostic",
            "boundary_mode_frozen": False,
            "outlet_pressure_or_backpressure_set": False,
            "su2_cfd_path": str(Path(self.su2_cfd).expanduser().resolve()),
            "su2_version": None,
            "su2_version_evidence": None,
            "reference_config": preparation.get("reference_config"),
            "preparation": dict(preparation),
            "config_path": str(config),
            "config_sha256": _sha256(config),
            "mesh_path": str(mesh),
            "mesh_sha256": _sha256(mesh),
            "mesh_validation": dict(mesh_validation),
            "angle_mapping": dict(raw_mapping),
            "atmosphere": dict(raw_atmosphere),
            "requested_iterations": max_iterations,
            "iterations": 0,
            "history_path": None,
            "history_sha256": None,
            "visualization_file": None,
            "visualization_validation": None,
            "freestream_log_evidence": None,
            "arguments": [],
            "command": [],
            "cwd": str(run_dir),
            "return_code": None,
            "solver_stdout_log": None,
            "solver_stderr_log": None,
            "solver_command_log": None,
            "fatal_matches": [],
            "nonfatal_diagnostics": [],
            "timed_out": False,
            "start_time": _utc_now(),
            "end_time": None,
            "error": None,
        }
        result: CommandResult | None = None
        failure_stderr: str | None = None
        try:
            reference = discover_su2_reference(self.su2_cfd)
            preparation_reference = preparation.get("reference_config")
            if (
                not isinstance(preparation_reference, Mapping)
                or preparation_reference.get("sha256") != reference.sha256
            ):
                raise SU2BridgeError("SU2 syntax reference changed after preparation")
            manifest["reference_config"] = reference.as_manifest()
            version, version_result = self.probe_version(
                run_dir, timeout_seconds=timeout_value
            )
            manifest["su2_version"] = version
            manifest["su2_version_evidence"] = version_result.as_metadata()

            visualization_before = snapshot_visualizations(run_dir)
            history_before = snapshot_history(run_dir)
            result = self.run_su2(
                config,
                run_dir,
                nproc=1,
                timeout_seconds=timeout_value,
                live_output=live_output,
            )
            self._apply_result(manifest, result)
            if result.returncode != 0:
                failure_stderr = result.stderr
                raise SU2BridgeError(
                    f"SU2_CFD returned {result.returncode}; stderr:\n{result.stderr}"
                )
            fatal_matches = scan_fatal_output(result.stdout, result.stderr)
            manifest["fatal_matches"] = fatal_matches
            manifest["nonfatal_diagnostics"] = scan_nonfatal_diagnostics(
                result.stdout, result.stderr
            )
            if fatal_matches:
                failure_stderr = result.stderr
                raise SU2BridgeError(
                    "SU2_CFD logs contain fatal text: " + " | ".join(fatal_matches)
                )

            freestream = parse_su2_freestream_log(result.stdout)
            actual_unit = tuple(float(value) for value in freestream["unit_vector_xyz"])
            component_error = max(
                abs(expected - actual)
                for expected, actual in zip(expected_unit, actual_unit, strict=True)
            )
            pressure_error = abs(
                float(freestream["static_pressure_pa"]) - expected_pressure
            ) / expected_pressure
            temperature_error = abs(
                float(freestream["static_temperature_k"]) - expected_temperature
            ) / expected_temperature
            expected_speed = expected_mach * expected_sound_speed
            speed_error = abs(
                float(freestream["velocity_magnitude_m_s"]) - expected_speed
            ) / expected_speed
            freestream.update(
                {
                    "expected_unit_vector_xyz": list(expected_unit),
                    "maximum_unit_vector_component_error": component_error,
                    "expected_velocity_magnitude_m_s": expected_speed,
                    "relative_velocity_magnitude_error": speed_error,
                    "relative_static_pressure_error": pressure_error,
                    "relative_static_temperature_error": temperature_error,
                    "stdout_sha256": _sha256(result.stdout_log),
                }
            )
            manifest["freestream_log_evidence"] = freestream
            if component_error > 1.0e-4:
                raise SU2BridgeError(
                    "SU2 freestream direction does not match project angle mapping"
                )
            if max(pressure_error, temperature_error, speed_error) > 1.0e-4:
                raise SU2BridgeError(
                    "SU2 freestream state does not match atmosphere/config evidence"
                )

            history_candidates = _changed_history_files(run_dir, history_before)
            history = next(
                (path for path in history_candidates if path.name.casefold() == "history.csv"),
                None,
            )
            if history is None:
                raise SU2BridgeError("SU2_CFD generated no new history.csv")
            iterations = parse_history_iterations(history)
            manifest["history_path"] = str(history)
            manifest["history_sha256"] = _sha256(history)
            manifest["iterations"] = iterations
            if iterations != max_iterations:
                raise SU2BridgeError(
                    f"SU2 history contains {iterations} iterations, expected {max_iterations}"
                )

            visualization = select_new_visualization(run_dir, visualization_before)
            if visualization is None:
                raise SU2BridgeError("SU2_CFD generated no new diagnostic visualization")
            manifest["visualization_file"] = str(visualization)
            manifest["visualization_validation"] = validate_visualization_file(
                visualization,
                expected_points=expected_points,
                expected_cells=expected_cells,
            )
            manifest["status"] = "PASS"
            manifest["end_time"] = _utc_now()
            _write_json(manifest_path, manifest)
            return manifest
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, CommandExecutionError):
                result = error.result
            if result is not None:
                self._apply_result(manifest, result)
            manifest["status"] = "FAIL"
            manifest["end_time"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                "stderr": failure_stderr
                if failure_stderr is not None
                else (None if result is None else result.stderr),
            }
            _write_json(manifest_path, manifest)
            if isinstance(error, SU2BridgeError):
                raise
            raise SU2BridgeError(str(error)) from error

    def run_smoke_case(
        self,
        mesh_path: PathValue,
        output_directory: PathValue,
        *,
        max_iterations: int = 5,
        nproc: int = 1,
        timeout_seconds: float = 300,
        live_output: bool = True,
    ) -> dict[str, Any]:
        """Prepare, run, validate, and manifest the real SU2 connection case."""

        self._validate_nproc(nproc)
        timeout_value = self._validate_timeout(timeout_seconds)
        if nproc != 1 and self.mpiexec is None:
            raise ValueError("mpirun or mpiexec is required when nproc is greater than one")
        if isinstance(max_iterations, bool) or not isinstance(max_iterations, int):
            raise TypeError("max_iterations must be an integer")
        if not 5 <= max_iterations <= 10:
            raise ValueError("max_iterations must be between 5 and 10")
        source_mesh = Path(mesh_path).expanduser().resolve(strict=True)
        if not source_mesh.is_file():
            raise ValueError(f"mesh path is not a file: {source_mesh}")
        mesh_validation = validate_su2_mesh(source_mesh)

        run_dir = Path(output_directory).expanduser().resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        config_path = run_dir / "smoke.cfg"
        local_mesh = run_dir / "smoke.su2"
        manifest_path = run_dir / "run_manifest.json"

        overall_started = _utc_now()
        manifest: dict[str, Any] = {
            "status": "FAIL",
            "su2_cfd_path": str(Path(self.su2_cfd).expanduser().resolve()),
            "su2_sol_path": None
            if self.su2_sol is None
            else str(Path(self.su2_sol).expanduser().resolve()),
            "mpiexec_path": None
            if self.mpiexec is None
            else str(Path(self.mpiexec).expanduser().resolve()),
            "su2_version": None,
            "su2_version_evidence": None,
            "reference_config": None,
            "arguments": [],
            "command": [],
            "cwd": str(run_dir),
            "config_path": str(config_path),
            "config_sha256": None,
            "mesh_path": str(local_mesh),
            "mesh_sha256": None,
            "source_mesh_path": str(source_mesh),
            "source_mesh_sha256": _sha256(source_mesh),
            "mesh_validation": mesh_validation,
            "return_code": None,
            "requested_iterations": max_iterations,
            "iterations": 0,
            "history_path": None,
            "history_sha256": None,
            "visualization_file": None,
            "visualization_validation": None,
            "solver_stdout_log": None,
            "solver_stderr_log": None,
            "solver_command_log": None,
            "restart_file": None,
            "su2_sol_config_path": None,
            "su2_sol_config_sha256": None,
            "su2_sol_command": None,
            "su2_sol_return_code": None,
            "su2_sol_stderr": None,
            "fatal_matches": [],
            "nonfatal_diagnostics": [],
            "diagnostic_classifications": [],
            "timed_out": False,
            "start_time": overall_started,
            "end_time": None,
            "error": None,
        }
        result: CommandResult | None = None
        failure_stderr: str | None = None
        try:
            reference = discover_su2_reference(self.su2_cfd)
            manifest["reference_config"] = reference.as_manifest()
            try:
                version, version_result = self.probe_version(
                    run_dir, timeout_seconds=timeout_value
                )
            except CommandExecutionError as version_error:
                manifest["su2_version_evidence"] = (
                    version_error.result.as_metadata()
                )
                failure_stderr = version_error.result.stderr
                raise SU2BridgeError(
                    "SU2 version probe failed; stderr:\n"
                    + version_error.result.stderr
                ) from version_error
            manifest["su2_version"] = version
            manifest["su2_version_evidence"] = version_result.as_metadata()

            if source_mesh != local_mesh.resolve():
                shutil.copy2(source_mesh, local_mesh)
            manifest["mesh_sha256"] = _sha256(local_mesh)
            if manifest["mesh_sha256"] != manifest["source_mesh_sha256"]:
                raise SU2BridgeError("copied smoke mesh SHA256 does not match source")

            config_path.write_text(
                render_smoke_config(reference, max_iterations), encoding="utf-8"
            )
            manifest["config_sha256"] = _sha256(config_path)
            visualization_before = snapshot_visualizations(run_dir)
            history_before = snapshot_history(run_dir)
            restart_before = snapshot_restarts(run_dir)

            result = self.run_su2(
                config_path,
                run_dir,
                nproc=nproc,
                timeout_seconds=timeout_value,
                live_output=live_output,
            )
            self._apply_result(manifest, result)
            if result.returncode != 0:
                raise SU2BridgeError(
                    f"SU2_CFD returned {result.returncode}; stderr:\n{result.stderr}"
                )
            diagnostic_classifications = classify_2d_quality_nan(
                result.stdout,
                result.stderr,
                mesh_validation,
            )
            manifest["diagnostic_classifications"] = diagnostic_classifications
            fatal_matches = scan_fatal_output(
                result.stdout,
                result.stderr,
                allowed_diagnostics=diagnostic_classifications,
            )
            manifest["fatal_matches"] = fatal_matches
            manifest["nonfatal_diagnostics"] = scan_nonfatal_diagnostics(
                result.stdout,
                result.stderr,
                allowed_diagnostics=diagnostic_classifications,
            )
            if fatal_matches:
                raise SU2BridgeError(
                    "SU2_CFD logs contain fatal text: " + " | ".join(fatal_matches)
                )

            history_candidates = _changed_history_files(run_dir, history_before)
            if not history_candidates:
                raise SU2BridgeError("SU2_CFD generated no new history CSV")
            history_path = history_candidates[0]
            iterations = parse_history_iterations(history_path)
            manifest["history_path"] = str(history_path)
            manifest["history_sha256"] = _sha256(history_path)
            manifest["iterations"] = iterations
            if iterations < max_iterations:
                raise SU2BridgeError(
                    f"SU2 history contains {iterations} rows, expected at least {max_iterations}"
                )

            visualization = select_new_visualization(
                run_dir, visualization_before
            )
            if visualization is None:
                restart_file = select_new_restart(run_dir, restart_before)
                if restart_file is None:
                    raise SU2BridgeError(
                        "SU2_CFD generated neither a new connection_solution "
                        "visualization nor a new nonempty connection restart; "
                        "SU2_SOL was not called"
                    )
                manifest["restart_file"] = str(restart_file)
                if self.su2_sol is None:
                    raise SU2BridgeError(
                        "SU2_CFD generated only a restart and SU2_SOL is unavailable"
                    )
                su2_sol_config = run_dir / "su2_sol.cfg"
                su2_sol_config.write_text(
                    render_su2_sol_config(
                        config_path.read_text(encoding="utf-8"),
                        reference,
                        restart_file.name,
                    ),
                    encoding="utf-8",
                )
                manifest["su2_sol_config_path"] = str(su2_sol_config)
                manifest["su2_sol_config_sha256"] = _sha256(su2_sol_config)
                try:
                    sol_result = self._run_su2_sol(
                        su2_sol_config,
                        restart_file,
                        run_dir,
                        timeout_value,
                        live_output=live_output,
                        reason="visualization_fallback",
                    )
                except CommandExecutionError as sol_error:
                    sol_result = sol_error.result
                    manifest["su2_sol_command"] = sol_result.as_metadata()
                    manifest["su2_sol_return_code"] = sol_result.returncode
                    manifest["su2_sol_stderr"] = sol_result.stderr
                    failure_stderr = sol_result.stderr
                    raise SU2BridgeError(
                        "SU2_SOL execution failed; stderr:\n" + sol_result.stderr
                    ) from sol_error
                manifest["su2_sol_command"] = sol_result.as_metadata()
                manifest["su2_sol_return_code"] = sol_result.returncode
                manifest["su2_sol_stderr"] = sol_result.stderr
                if sol_result.returncode != 0:
                    failure_stderr = sol_result.stderr
                    raise SU2BridgeError(
                        f"SU2_SOL returned {sol_result.returncode}; stderr:\n{sol_result.stderr}"
                    )
                sol_fatal = scan_fatal_output(sol_result.stdout, sol_result.stderr)
                if sol_fatal:
                    manifest["fatal_matches"].extend(sol_fatal)
                    failure_stderr = sol_result.stderr
                    raise SU2BridgeError(
                        "SU2_SOL logs contain fatal text: " + " | ".join(sol_fatal)
                    )
                manifest["nonfatal_diagnostics"].extend(
                    scan_nonfatal_diagnostics(sol_result.stdout, sol_result.stderr)
                )
                visualization = select_new_visualization(
                    run_dir, visualization_before
                )
            if visualization is None:
                raise SU2BridgeError("no new ParaView-readable solution was generated")
            manifest["visualization_file"] = str(visualization)
            visualization_validation = validate_visualization_file(
                visualization,
                expected_points=mesh_validation["npoin"],
                expected_cells=mesh_validation["nelem"],
            )
            manifest["visualization_validation"] = visualization_validation
            for diagnostic in manifest["diagnostic_classifications"]:
                evidence = diagnostic.get("evidence")
                if isinstance(evidence, dict):
                    evidence.update(
                        {
                            "solver_return_code": result.returncode,
                            "history_iteration_count": iterations,
                            "history_finite": True,
                            "visualization_finite": visualization_validation[
                                "finite"
                            ],
                        }
                    )
            manifest["status"] = "PASS"
            manifest["end_time"] = _utc_now()
            _write_json(manifest_path, manifest)
            return manifest
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, CommandExecutionError):
                result = error.result
            if result is not None:
                self._apply_result(manifest, result)
            manifest["status"] = "FAIL"
            manifest["end_time"] = _utc_now()
            manifest["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
                "stderr": failure_stderr
                if failure_stderr is not None
                else (None if result is None else result.stderr),
            }
            _write_json(manifest_path, manifest)
            if isinstance(error, SU2BridgeError):
                raise
            raise SU2BridgeError(str(error)) from error


def run_su2(
    config_path: PathValue,
    run_directory: PathValue,
    nproc: int = 1,
    timeout_seconds: float = 300,
    *,
    runner: Any,
    su2_cfd: PathValue,
    mpiexec: PathValue | None = None,
) -> CommandResult:
    """Module-level serial/MPI runner matching the documented interface."""

    return SU2Bridge(runner, su2_cfd, mpiexec=mpiexec).run_su2(
        config_path,
        run_directory,
        nproc=nproc,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "SU2Bridge",
    "SU2BridgeError",
    "SU2Reference",
    "classify_2d_quality_nan",
    "discover_su2_rans_reference",
    "discover_su2_reference",
    "parse_history_iterations",
    "prepare_project_supersonic_pilot_case",
    "render_project_supersonic_pilot_config",
    "render_project_rans_smoke_config",
    "render_smoke_config",
    "render_su2_sol_config",
    "run_su2",
    "scan_fatal_output",
    "scan_nonfatal_diagnostics",
    "select_new_restart",
    "select_new_visualization",
    "snapshot_restarts",
    "snapshot_visualizations",
    "validate_visualization_file",
]
